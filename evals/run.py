"""Run an eval from the command line.

    uv run --env-file .env python -m evals.run --agent qa
    uv run --env-file .env python -m evals.run --agent qa --prompt-version v1 --runs 3

Each run calls the real API (a few cents for the QA set) and appends one row to evals/results.csv.
Change one thing at a time (the prompt version, the model tier), rerun, and compare the rows.
"""

import dataclasses
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from evals.qa_cases import CASES
from evals.qa_eval import EvalBudgetError, append_result, format_summary, run_eval
from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.config import Tier, load_config
from orchestrator.default_specs import default_api_schemas, default_api_specs
from orchestrator.pipeline import AgentRunner
from orchestrator.prompts import load_prompt
from orchestrator.state import Stage

ROOT = Path(__file__).resolve().parent.parent
app = typer.Typer(add_completion=False, help="Run an agent eval.")


def qa_runner(config_path: Path, prompt_version: str | None, tier: Tier | None) -> AgentRunner:
    """The real QA agent, optionally pinned to a prompt version or model tier for comparison."""
    config = load_config(config_path)
    specs = []
    for spec in default_api_specs():
        if spec.stage == Stage.QA:
            spec = dataclasses.replace(
                spec,
                prompt=load_prompt("qa", version=prompt_version),
                tier=tier or spec.tier,
            )
        specs.append(spec)
    return ValidatingRunner(ApiRunner(config, specs), default_api_schemas())


def git_sha() -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


@app.command()
def main(
    agent: Annotated[str, typer.Option(help="Which agent to evaluate (only qa so far).")],
    prompt_version: Annotated[str | None, typer.Option(help="e.g. v1; default: newest.")] = None,
    tier: Annotated[Tier | None, typer.Option(help="strong or fast; default: the spec's.")] = None,
    runs: Annotated[int, typer.Option(min=1, help="Times to run every case.")] = 1,
    max_usd: Annotated[float, typer.Option(help="Stop if the eval spends more than this.")] = 2.0,
    output: Annotated[Path, typer.Option(help="Results table to append to.")] = (
        ROOT / "evals" / "results.csv"
    ),
    config: Annotated[Path, typer.Option(help="Path to config.yaml.")] = ROOT / "config.yaml",
) -> None:
    if agent != "qa":
        typer.echo(f"No eval for {agent!r} yet; only 'qa' exists.", err=True)
        raise typer.Exit(1)
    runner = qa_runner(config, prompt_version, tier)
    try:
        summary = run_eval(CASES, runner, runs=runs, max_usd=max_usd)
    except EvalBudgetError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(format_summary(summary))
    append_result(output, summary, git_sha=git_sha(), dataset="qa", runs=runs)
    typer.echo(f"Appended a row to {output}")


if __name__ == "__main__":
    app()
