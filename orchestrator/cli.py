from typing import Annotated

import typer

from orchestrator import __version__

app = typer.Typer(
    name="studio",
    help="Agent Studio: a human-gated multi-agent development pipeline.",
    no_args_is_help=True,
)


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
