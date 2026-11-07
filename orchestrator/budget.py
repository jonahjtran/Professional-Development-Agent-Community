import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from orchestrator.config import Config
from orchestrator.state import (
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    atomic_write,
)


class BudgetLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


class BudgetScope(StrEnum):
    """Which limit an assessment is about: the whole project or the stage about to run."""

    PROJECT = "project"
    STAGE = "stage"


class BudgetAssessment(BaseModel):
    level: BudgetLevel
    detail: str
    scope: BudgetScope | None = None  # None when within budget
    warned: dict[str, float] = {}  # for a warning: each limit warned about -> its cap

    model_config = ConfigDict(frozen=True)


def _reached(spent: float, threshold: float) -> bool:
    """True once `spent` has reached `threshold`.

    Thresholds like 80% of $1.50 come out as 1.2000000000000002 in floating point, so a plain
    `>=` would miss spend that is exactly on the line. A billionth of a dollar is far below any
    real cost, so it is safe slack.
    """
    return spent >= threshold - 1e-9


def record_cost(state: ProjectState, stage: Stage, usd: float) -> ProjectState:
    """Return a new state with `usd` added to `stage`'s spend."""
    if usd < 0:
        raise ValueError(f"cost cannot be negative: {usd}")
    spent = state.cost_usd.get(stage, 0.0)
    return state.evolve(cost_usd={**state.cost_usd, stage: spent + usd})


def assess_budget(state: ProjectState, config: Config, stage: Stage) -> BudgetAssessment:
    """Decide whether the next agent call for `stage` may proceed.

    Reaching a cap counts as exceeded, so the run stops before the next call.
    The project cap is checked first, then the stage's own cap, then the warning threshold.
    Caps are the config values plus any extra room the human granted (state.allowances).
    The warning applies to the project total and to the stage about to run.
    """
    budget = config.budget
    allowances = state.allowances
    project_cap = budget.project_cap_usd + allowances.extra_project_usd
    project_spent = state.total_cost_usd

    if _reached(project_spent, project_cap):
        return BudgetAssessment(
            level=BudgetLevel.EXCEEDED,
            scope=BudgetScope.PROJECT,
            detail=(
                f"Project spend ${project_spent:.2f} reached the ${project_cap:.2f} project cap"
            ),
        )

    stage_spent = state.cost_usd.get(stage, 0.0)
    base_stage_cap = getattr(budget.stage_caps_usd, stage.value, None)
    stage_cap = (
        None
        if base_stage_cap is None
        else base_stage_cap + allowances.extra_stage_usd.get(stage, 0.0)
    )
    if stage_cap is not None and _reached(stage_spent, stage_cap):
        return BudgetAssessment(
            level=BudgetLevel.EXCEEDED,
            scope=BudgetScope.STAGE,
            detail=f"{stage.value} spend ${stage_spent:.2f} reached its ${stage_cap:.2f} stage cap",
        )

    # Warnings are recomputed from current spend on every check.
    # The exception: a warning the human already continued past stays quiet for that cap.
    acknowledged = allowances.warned
    warnings: list[str] = []
    warned: dict[str, float] = {}
    project_warned = (
        _reached(project_spent, project_cap * budget.warn_at_fraction)
        and acknowledged.get("project") != project_cap
    )
    if project_warned:
        warned["project"] = project_cap
        warnings.append(
            f"Project spend ${project_spent:.2f} is at {budget.warn_at_fraction:.0%} of the "
            f"${project_cap:.2f} project cap"
        )
    if (
        stage_cap is not None
        and _reached(stage_spent, stage_cap * budget.warn_at_fraction)
        and acknowledged.get(stage.value) != stage_cap
    ):
        warned[stage.value] = stage_cap
        warnings.append(
            f"{stage.value} spend ${stage_spent:.2f} is at {budget.warn_at_fraction:.0%} of its "
            f"${stage_cap:.2f} stage cap"
        )
    if warnings:
        return BudgetAssessment(
            level=BudgetLevel.WARNING,
            scope=BudgetScope.PROJECT if project_warned else BudgetScope.STAGE,
            detail="; ".join(warnings),
            warned=warned,
        )

    return BudgetAssessment(level=BudgetLevel.OK, detail="Within budget")


def pause_for_budget(state: ProjectState, assessment: BudgetAssessment) -> ProjectState:
    """Stop the run and ask the human, using the assessment's reason."""
    if assessment.level == BudgetLevel.OK:
        raise ValueError("cannot pause: the project is within budget")
    return state.evolve(
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.BUDGET_CAP, detail=assessment.detail),
    )


def cost_report_path(runs_dir: Path, slug: str) -> Path:
    return runs_dir / slug / "cost.json"


def write_cost_report(runs_dir: Path, state: ProjectState) -> None:
    """Write cost.json: dollars per stage and the project total."""
    report = {
        "stages": {stage.value: usd for stage, usd in state.cost_usd.items()},
        "total_usd": state.total_cost_usd,
    }
    atomic_write(cost_report_path(runs_dir, state.project_slug), json.dumps(report, indent=2))
