"""Run an eval from the command line.

    uv run --env-file .env python -m evals.run --agent qa
    uv run --env-file .env python -m evals.run --agent qa --prompt-version v1 --runs 3
    uv run --env-file .env python -m evals.run --agent career_coach

Each run calls the real API (a few cents for the QA set) and appends one row to evals/results.csv.
Change one thing at a time (the prompt version, the model tier), rerun, and compare the rows.
"""

import dataclasses
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from evals.career_cases import CASES as CAREER_CASES
from evals.career_eval import (
    append_career_result,
    format_career_summary,
    run_career_eval,
)
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


def career_runner(config_path: Path, prompt_version: str | None, tier: Tier | None) -> AgentRunner:
    """The real career coach, bare: no retry, so the eval measures its first attempt."""
    config = load_config(config_path)
    specs = []
    for spec in default_api_specs():
        if spec.stage == Stage.CAREER_COACH:
            spec = dataclasses.replace(
                spec,
                prompt=load_prompt("career_coach", version=prompt_version),
                tier=tier or spec.tier,
            )
        specs.append(spec)
    return ApiRunner(config, specs)


def git_sha() -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


@app.command()
def main(
    agent: Annotated[str, typer.Option(help="Which agent to evaluate: qa or career_coach.")],
    prompt_version: Annotated[str | None, typer.Option(help="e.g. v1; default: newest.")] = None,
    tier: Annotated[Tier | None, typer.Option(help="strong or fast; default: the spec's.")] = None,
    runs: Annotated[int, typer.Option(min=1, help="Times to run every case.")] = 1,
    max_usd: Annotated[float, typer.Option(help="Stop if the eval spends more than this.")] = 2.0,
    output: Annotated[Path, typer.Option(help="Results table to append to.")] = (
        ROOT / "evals" / "results.csv"
    ),
    config: Annotated[Path, typer.Option(help="Path to config.yaml.")] = ROOT / "config.yaml",
) -> None:
    if agent not in ("qa", "career_coach"):
        typer.echo(f"No eval for {agent!r} yet; only 'qa' and 'career_coach' exist.", err=True)
        raise typer.Exit(1)
    try:
        if agent == "qa":
            summary = run_eval(
                CASES, qa_runner(config, prompt_version, tier), runs=runs, max_usd=max_usd
            )
            typer.echo(format_summary(summary))
            append_result(output, summary, git_sha=git_sha(), dataset="qa", runs=runs)
        else:
            career = run_career_eval(
                CAREER_CASES,
                career_runner(config, prompt_version, tier),
                runs=runs,
                max_usd=max_usd,
            )
            typer.echo(format_career_summary(career))
            append_career_result(output, career, git_sha=git_sha(), runs=runs)
    except EvalBudgetError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Appended a row to {output}")


if __name__ == "__main__":
    app()
