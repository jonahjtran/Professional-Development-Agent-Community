import json
import re
import subprocess
from contextlib import AbstractContextManager, nullcontext
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from orchestrator import __version__
from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.career_publish import GitCareerPublisher, repo_owner
from orchestrator.career_stage import GitCareerPreparer, website_dir
from orchestrator.cli_cap import CliCapBackend
from orchestrator.cli_gate import CliGateBackend
from orchestrator.cli_merge import CliMergeBackend
from orchestrator.cli_tool import CliToolBackend
from orchestrator.config import Config, load_config
from orchestrator.default_specs import (
    default_api_checks,
    default_api_schemas,
    default_api_specs,
    default_specs,
    default_stage_specs,
)
from orchestrator.dev_runner import DevRunner
from orchestrator.echo_agent import EchoAgent
from orchestrator.halt import request_halt
from orchestrator.inbox import (
    FileCapBackend,
    FileGateBackend,
    FileMergeBackend,
    FileToolBackend,
    Inbox,
    run_marker,
)
from orchestrator.newproject import NewProjectError, create_project, default_projects_dir
from orchestrator.pipeline import AgentRunner, PipelineContext, RoutingRunner, run_pipeline
from orchestrator.profile import ProfileError, load_profile, profile_dir
from orchestrator.prompts import load_prompt
from orchestrator.publish import Publisher, PublishError, load_token, strip_credentials
from orchestrator.qa_prepare import GitQaPreparer
from orchestrator.runner import SdkRunner
from orchestrator.stages import StageSpec, career_stage
from orchestrator.state import ProjectState, Stage, Status, load_state, state_path
from orchestrator.tool_hooks import ToolPermissionBackend
from schemas import Track

app = typer.Typer(
    name="studio",
    help="Agent Studio: a human-gated multi-agent development pipeline.",
    no_args_is_help=True,
)


class AgentKind(StrEnum):
    ECHO = "echo"  # the free dummy agent, for trying the flow
    CLAUDE = "claude"  # real Claude calls through the Agent SDK (costs money)
    API = "api"  # real Claude calls through the Messages API (costs money)
    FULL = "full"  # API for planning, then the Developer builds tasks and opens pull requests


AgentOption = Annotated[AgentKind, typer.Option("--agent", help="Which agent runs the stages.")]
ConfigOption = Annotated[Path, typer.Option("--config", help="Path to config.yaml.")]
RepoOption = Annotated[
    Path | None,
    typer.Option("--repo", help="The project's own git repository (required for --agent full)."),
]
RunsDirOption = Annotated[Path, typer.Option("--runs-dir", help="Where projects are stored.")]
UiOption = Annotated[
    bool,
    typer.Option(
        "--ui", help="Ask questions through files (for the web UI) instead of at the terminal."
    ),
]

_MAX_SLUG_LENGTH = 40
_HALTED_EXIT_CODE = 2


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Agent Studio: a human-gated multi-agent development pipeline."""


def slugify(text: str) -> str:
    """Turn an idea into a safe project directory name."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:_MAX_SLUG_LENGTH].strip("-")
    if not slug:
        raise ValueError("cannot make a slug from that text; pass --slug")
    return slug


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(1)


def make_runner(
    kind: AgentKind,
    config: Config,
    runs_dir: Path,
    repo: Path | None = None,
    tools: ToolPermissionBackend | None = None,
) -> AgentRunner:
    if kind == AgentKind.CLAUDE:
        return SdkRunner(config, runs_dir, default_specs())
    if kind in (AgentKind.API, AgentKind.FULL):
        planner = ValidatingRunner(
            ApiRunner(config, default_api_specs()), default_api_schemas(), default_api_checks()
        )
        if kind == AgentKind.API:
            return planner
        if repo is None:
            raise ValueError("the full pipeline needs the project's repository")
        developer = DevRunner(
            config, runs_dir, repo.resolve(), tools or CliToolBackend(), load_prompt("developer")
        )
        return RoutingRunner(
            {
                Stage.PRODUCT_OWNER: planner,
                Stage.ARCHITECT: planner,
                Stage.DEVELOPER: developer,
                Stage.QA: planner,
                Stage.CAREER_COACH: planner,
            }
        )
    return EchoAgent()


def _origin_url(repo: Path) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    return strip_credentials(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


def _new_project(
    idea: str, slug: str | None, track: Track, agent: AgentKind, repo: Path | None
) -> ProjectState:
    """Validate the arguments and build the state of a new project; stops early on a problem."""
    repo_path = repo_url = None
    if agent == AgentKind.FULL:
        if repo is None:
            raise _fail("--agent full needs --repo PATH: the project's own git repository")
        if not (repo / ".git").exists():
            raise _fail(f"{repo} is not a git repository; pass --repo")
        repo_url = _origin_url(repo)
        if repo_url is None:
            raise _fail(f"{repo} has no 'origin' remote, and pull requests need one")
        repo_path = str(repo.resolve())
    try:
        return ProjectState(
            project_slug=slug or slugify(idea),
            idea=idea,
            track=track,
            repo_path=repo_path,
            repo_url=repo_url,
        )
    except (ValueError, ValidationError) as error:
        raise _fail(f"Invalid project name: {error}") from None


def _repo_for_resume(state: ProjectState, agent: AgentKind, repo: Path | None) -> Path | None:
    """The repository to resume in: the one recorded in the project, never a different one."""
    if agent != AgentKind.FULL:
        return None
    if state.repo_path is None:
        raise _fail(
            f"Project {state.project_slug} has no repository recorded; it was not a full run"
        )
    recorded = Path(state.repo_path)
    if repo is not None and repo.resolve() != recorded.resolve():
        raise _fail(f"Project {state.project_slug} belongs to {recorded}, not {repo}")
    return recorded


def _context(
    config_path: Path,
    runs_dir: Path,
    agent: AgentKind,
    repo: Path | None = None,
    repo_url: str | None = None,
    *,
    project: str | None = None,
    ui: bool = False,
) -> PipelineContext:
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        raise _fail(f"Config file not found: {config_path}") from None
    except ValidationError as error:
        raise _fail(f"Invalid config {config_path}:\n{error}") from None
    inbox = Inbox(runs_dir, project) if ui and project else None
    publisher: Publisher | None = None
    career: GitCareerPreparer | None = None
    career_publisher: GitCareerPublisher | None = None
    if agent == AgentKind.FULL:
        if repo is None or repo_url is None:
            raise _fail("--agent full needs the project's repository")
        try:
            publisher = Publisher(repo.resolve(), load_token(), expected_url=repo_url)
            folder = profile_dir()
            load_profile(folder)  # fail now, not hours later at the career stage
        except (PublishError, ProfileError) as error:
            raise _fail(str(error)) from None
        site = website_dir()
        career = GitCareerPreparer(folder, repo.resolve(), site)
        career_publisher = _career_publisher(publisher, repo, repo_url, folder, site)
    return PipelineContext(
        config=config,
        runs_dir=runs_dir,
        runner=make_runner(
            agent, config, runs_dir, repo, FileToolBackend(inbox) if inbox else None
        ),
        gates=FileGateBackend(inbox) if inbox else CliGateBackend(),
        caps=FileCapBackend(inbox) if inbox else CliCapBackend(),
        stages=_stages(agent),
        publisher=publisher,
        repo=repo.resolve() if (agent == AgentKind.FULL and repo is not None) else None,
        qa=GitQaPreparer(repo.resolve(), publisher, config.qa) if publisher and repo else None,
        merges=(FileMergeBackend(inbox) if inbox else CliMergeBackend()) if publisher else None,
        career=career,
        career_publisher=career_publisher,
    )


def _career_publisher(
    project_publisher: Publisher, repo: Path, repo_url: str, profile: Path, site: Path | None
) -> GitCareerPublisher:
    """Pull-request publishers for the profile and website repos, which must be your own."""
    token = load_token()
    profile_url = _origin_url(profile)
    if profile_url is None:
        raise _fail(
            f"{profile} needs to be a git clone with an origin, so the resume can be proposed"
        )
    site_url = _origin_url(site) if site is not None else None
    if site is not None and site_url is None:
        raise _fail(f"{site} needs to be a git clone with an origin")
    owners = {repo_owner(u) for u in (repo_url, profile_url, site_url) if u is not None}
    if len(owners) != 1 or None in owners:
        raise _fail("the project, profile and website repositories must all be on your own account")
    return GitCareerPublisher(
        project_repo=repo.resolve(),
        project_url=repo_url,
        project_pulls=project_publisher,
        profile_repo=profile.resolve(),
        profile_pulls=Publisher(profile.resolve(), token, expected_url=profile_url),
        website_repo=site.resolve() if site is not None else None,
        website_pulls=(
            Publisher(site.resolve(), token, expected_url=site_url)
            if site is not None and site_url is not None
            else None
        ),
    )


def _stages(agent: AgentKind) -> dict[Stage, StageSpec]:
    if agent == AgentKind.FULL:
        return {**default_stage_specs(), Stage.CAREER_COACH: career_stage()}
    return default_stage_specs() if agent == AgentKind.API else {}


def _show_career_results(state: ProjectState, runs_dir: Path) -> None:
    artifact = state.artifacts.get("career_published.json")
    if artifact is None:
        return
    results = json.loads((runs_dir / state.project_slug / artifact.path).read_text())
    typer.echo("Career items:")
    for target, outcome in results.items():
        typer.echo(f"  {target}: {outcome}")
    typer.echo("Merge the pull requests yourself; nothing was merged for you.")


def _marked(runs_dir: Path, project: str, ui: bool) -> AbstractContextManager[None]:
    """While a run answers through files, keep a heartbeat so the UI can tell it is alive."""
    return run_marker(runs_dir, project) if ui else nullcontext()


def _finish(state: ProjectState, runs_dir: Path) -> None:
    if state.status == Status.DONE:
        typer.echo(f"Project {state.project_slug} is done. Spent ${state.total_cost_usd:.2f}.")
        _show_career_results(state, runs_dir)
        return
    typer.echo(f"Project {state.project_slug} halted. Spent ${state.total_cost_usd:.2f}.")
    raise typer.Exit(_HALTED_EXIT_CODE)


@app.command()
def run(
    idea: Annotated[str, typer.Argument(help="One-paragraph project idea.")],
    slug: Annotated[str | None, typer.Option(help="Project name (default: from the idea).")] = None,
    config: ConfigOption = Path("config.yaml"),
    runs_dir: RunsDirOption = Path("runs"),
    agent: AgentOption = AgentKind.ECHO,
    repo: RepoOption = None,
    track: Annotated[Track, typer.Option(help="Which kind of PRD to write.")] = Track.ENGINEERING,
    ui: UiOption = False,
) -> None:
    """Start a new project and run it until it is done or halted."""
    project = _new_project(idea, slug, track, agent, repo)
    if state_path(runs_dir, project.project_slug).exists():
        raise _fail(
            f"Project {project.project_slug} already exists. "
            f"Continue it with: studio resume {project.project_slug}"
        )
    ctx = _context(
        config, runs_dir, agent, repo, project.repo_url, project=project.project_slug, ui=ui
    )
    with _marked(runs_dir, project.project_slug, ui):
        finished = run_pipeline(project, ctx)
    _finish(finished, runs_dir)


@app.command()
def resume(
    project: Annotated[str, typer.Argument(help="Project name.")],
    config: ConfigOption = Path("config.yaml"),
    runs_dir: RunsDirOption = Path("runs"),
    agent: AgentOption = AgentKind.ECHO,
    repo: RepoOption = None,
    ui: UiOption = False,
) -> None:
    """Continue a project from its last saved step, after a crash or Ctrl-C."""
    try:
        state = load_state(runs_dir, project)
    except FileNotFoundError:
        raise _fail(f"No project named {project} in {runs_dir}") from None
    if state.status in (Status.DONE, Status.HALTED):
        typer.echo(f"Project {project} is {state.status.value}; nothing to resume.")
        return
    project_repo = _repo_for_resume(state, agent, repo)
    _finish(
        run_pipeline(state, _context(config, runs_dir, agent, project_repo, state.repo_url)),
        runs_dir,
    )


@app.command()
def ui(
    config: ConfigOption = Path("config.yaml"),
    runs_dir: RunsDirOption = Path("runs"),
    port: Annotated[int, typer.Option(help="Port to listen on (this machine only).")] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the browser.")
    ] = True,
) -> None:
    """Open the web UI. It listens on this machine only."""
    import threading
    import webbrowser

    import uvicorn

    from orchestrator.ui.app import create_app

    try:
        loaded = load_config(config)
    except FileNotFoundError:
        raise _fail(f"Config file not found: {config}") from None
    except ValidationError as error:
        raise _fail(f"Invalid config {config}:\n{error}") from None
    url = f"http://127.0.0.1:{port}"
    typer.echo(f"Agent Studio UI at {url} (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    uvicorn.run(create_app(runs_dir, loaded), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def halt(
    project: Annotated[str, typer.Argument(help="Project name.")],
    runs_dir: RunsDirOption = Path("runs"),
) -> None:
    """Stop a running project: it halts before its next step, and a running Developer stops."""
    try:
        state = load_state(runs_dir, project)
    except FileNotFoundError:
        raise _fail(f"No project named {project} in {runs_dir}") from None
    if state.status in (Status.DONE, Status.HALTED):
        typer.echo(f"Project {project} is {state.status.value}; nothing to stop.")
        return
    request_halt(runs_dir, project)
    typer.echo(f"Halt requested. Project {project} will stop before its next step.")


@app.command()
def new(
    name: Annotated[str, typer.Argument(help="Project name (also the repository name).")],
    public: Annotated[bool, typer.Option("--public", help="Make the repository public.")] = False,
    projects_dir: Annotated[
        Path | None, typer.Option(help="Where to clone it (default: ~/Desktop/Github).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Create a new GitHub repository for a project, with your own gh login, and clone it."""

    def confirm(question: str) -> bool:
        return yes or typer.confirm(question, default=False)

    try:
        project = create_project(
            name, projects_dir or default_projects_dir(), public=public, confirm=confirm
        )
    except NewProjectError as error:
        raise _fail(str(error)) from None
    visibility = "public" if project.public else "private"
    typer.echo(f"Created {visibility} repository {project.owner}/{project.name} at {project.path}")
    typer.echo("Start building in it with:")
    typer.echo(
        f'  studio run "<your idea>" --slug {project.name} --agent full --repo {project.path}'
    )
    if project.public:
        typer.echo("Remember to enable branch protection on main (Settings, Rules, Rulesets).")
    else:
        typer.echo(
            "Note: private repositories have no branch protection on GitHub's free plan; "
            "the orchestrator only pushes agent/ branches and never forces."
        )
