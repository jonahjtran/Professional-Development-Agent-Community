from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from orchestrator.config import Config
from orchestrator.state import Intervention, InterventionKind, ProjectState, Stage, Status


class BudgetLevel(StrEnum):
    OK = "ok"
    WARNING = "warning"
    EXCEEDED = "exceeded"


class BudgetAssessment(BaseModel):
    level: BudgetLevel
    detail: str

    model_config = ConfigDict(frozen=True)


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
    """
    budget = config.budget
    project_spent = state.total_cost_usd

    if project_spent >= budget.project_cap_usd:
        return BudgetAssessment(
            level=BudgetLevel.EXCEEDED,
            detail=(
                f"Project spend ${project_spent:.2f} reached the "
                f"${budget.project_cap_usd:.2f} project cap"
            ),
        )

    stage_spent = state.cost_usd.get(stage, 0.0)
    stage_cap = getattr(budget.stage_caps_usd, stage.value, None)
    if stage_cap is not None and stage_spent >= stage_cap:
        return BudgetAssessment(
            level=BudgetLevel.EXCEEDED,
            detail=f"{stage.value} spend ${stage_spent:.2f} reached its ${stage_cap:.2f} stage cap",
        )

    warn_at = budget.project_cap_usd * budget.warn_at_fraction
    if project_spent >= warn_at:
        return BudgetAssessment(
            level=BudgetLevel.WARNING,
            detail=(
                f"Project spend ${project_spent:.2f} is at {budget.warn_at_fraction:.0%} of the "
                f"${budget.project_cap_usd:.2f} project cap"
            ),
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
