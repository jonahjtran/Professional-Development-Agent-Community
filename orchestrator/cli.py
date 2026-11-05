import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from orchestrator import __version__
from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.cli_cap import CliCapBackend
from orchestrator.cli_gate import CliGateBackend
from orchestrator.cli_tool import CliToolBackend
from orchestrator.config import Config, load_config
from orchestrator.default_specs import (
    default_api_schemas,
    default_api_specs,
    default_specs,
    default_stage_specs,
)
from orchestrator.dev_runner import DevRunner
from orchestrator.echo_agent import EchoAgent
from orchestrator.halt import request_halt
from orchestrator.pipeline import AgentRunner, PipelineContext, RoutingRunner, run_pipeline
from orchestrator.prompts import load_prompt
from orchestrator.publish import Publisher, PublishError, load_token
from orchestrator.runner import SdkRunner
from orchestrator.state import ProjectState, Stage, Status, load_state, state_path
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
    Path, typer.Option("--repo", help="The git repository the Developer works in.")
]
RunsDirOption = Annotated[Path, typer.Option("--runs-dir", help="Where projects are stored.")]

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
    kind: AgentKind, config: Config, runs_dir: Path, repo: Path = Path(".")
) -> AgentRunner:
    if kind == AgentKind.CLAUDE:
        return SdkRunner(config, runs_dir, default_specs())
    if kind in (AgentKind.API, AgentKind.FULL):
        planner = ValidatingRunner(ApiRunner(config, default_api_specs()), default_api_schemas())
        if kind == AgentKind.API:
            return planner
        developer = DevRunner(
            config, runs_dir, repo.resolve(), CliToolBackend(), load_prompt("developer")
        )
        return RoutingRunner(
            {
                Stage.PRODUCT_OWNER: planner,
                Stage.ARCHITECT: planner,
                Stage.DEVELOPER: developer,
            }
        )
    return EchoAgent()


def _context(
    config_path: Path, runs_dir: Path, agent: AgentKind, repo: Path = Path(".")
) -> PipelineContext:
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        raise _fail(f"Config file not found: {config_path}") from None
    except ValidationError as error:
        raise _fail(f"Invalid config {config_path}:\n{error}") from None
    publisher: Publisher | None = None
    if agent == AgentKind.FULL:
        if not (repo / ".git").exists():
            raise _fail(f"{repo} is not a git repository; pass --repo")
        try:
            publisher = Publisher(repo.resolve(), load_token())
        except PublishError as error:
            raise _fail(str(error)) from None
    return PipelineContext(
        config=config,
        runs_dir=runs_dir,
        runner=make_runner(agent, config, runs_dir, repo),
        gates=CliGateBackend(),
        caps=CliCapBackend(),
        stages=default_stage_specs() if agent in (AgentKind.API, AgentKind.FULL) else {},
        publisher=publisher,
    )


def _finish(state: ProjectState) -> None:
    if state.status == Status.DONE:
        typer.echo(f"Project {state.project_slug} is done. Spent ${state.total_cost_usd:.2f}.")
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
    repo: RepoOption = Path("."),
    track: Annotated[Track, typer.Option(help="Which kind of PRD to write.")] = Track.ENGINEERING,
) -> None:
    """Start a new project and run it until it is done or halted."""
    try:
        project = ProjectState(project_slug=slug or slugify(idea), idea=idea, track=track)
    except (ValueError, ValidationError) as error:
        raise _fail(f"Invalid project name: {error}") from None
    if state_path(runs_dir, project.project_slug).exists():
        raise _fail(
            f"Project {project.project_slug} already exists. "
            f"Continue it with: studio resume {project.project_slug}"
        )
    ctx = _context(config, runs_dir, agent, repo)
    _finish(run_pipeline(project, ctx))


@app.command()
def resume(
    project: Annotated[str, typer.Argument(help="Project name.")],
    config: ConfigOption = Path("config.yaml"),
    runs_dir: RunsDirOption = Path("runs"),
    agent: AgentOption = AgentKind.ECHO,
    repo: RepoOption = Path("."),
) -> None:
    """Continue a project from its last saved step, after a crash or Ctrl-C."""
    try:
        state = load_state(runs_dir, project)
    except FileNotFoundError:
        raise _fail(f"No project named {project} in {runs_dir}") from None
    if state.status in (Status.DONE, Status.HALTED):
        typer.echo(f"Project {project} is {state.status.value}; nothing to resume.")
        return
    _finish(run_pipeline(state, _context(config, runs_dir, agent, repo)))


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
