import re
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from orchestrator import __version__
from orchestrator.cli_cap import CliCapBackend
from orchestrator.cli_gate import CliGateBackend
from orchestrator.config import load_config
from orchestrator.echo_agent import EchoAgent
from orchestrator.pipeline import PipelineContext, run_pipeline
from orchestrator.state import ProjectState, Status, load_state, state_path

app = typer.Typer(
    name="studio",
    help="Agent Studio: a human-gated multi-agent development pipeline.",
    no_args_is_help=True,
)

ConfigOption = Annotated[Path, typer.Option("--config", help="Path to config.yaml.")]
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


def _context(config_path: Path, runs_dir: Path) -> PipelineContext:
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        raise _fail(f"Config file not found: {config_path}") from None
    except ValidationError as error:
        raise _fail(f"Invalid config {config_path}:\n{error}") from None
    return PipelineContext(
        config=config,
        runs_dir=runs_dir,
        runner=EchoAgent(),
        gates=CliGateBackend(),
        caps=CliCapBackend(),
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
) -> None:
    """Start a new project and run it until it is done or halted."""
    try:
        project = ProjectState(project_slug=slug or slugify(idea), idea=idea)
    except (ValueError, ValidationError) as error:
        raise _fail(f"Invalid project name: {error}") from None
    if state_path(runs_dir, project.project_slug).exists():
        raise _fail(
            f"Project {project.project_slug} already exists. "
            f"Continue it with: studio resume {project.project_slug}"
        )
    ctx = _context(config, runs_dir)
    _finish(run_pipeline(project, ctx))


@app.command()
def resume(
    project: Annotated[str, typer.Argument(help="Project name.")],
    config: ConfigOption = Path("config.yaml"),
    runs_dir: RunsDirOption = Path("runs"),
) -> None:
    """Continue a project from its last saved step, after a crash or Ctrl-C."""
    try:
        state = load_state(runs_dir, project)
    except FileNotFoundError:
        raise _fail(f"No project named {project} in {runs_dir}") from None
    if state.status in (Status.DONE, Status.HALTED):
        typer.echo(f"Project {project} is {state.status.value}; nothing to resume.")
        return
    _finish(run_pipeline(state, _context(config, runs_dir)))
