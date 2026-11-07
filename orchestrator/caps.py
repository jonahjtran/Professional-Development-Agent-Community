"""Answering the pauses that happen when a limit is reached: the revision cap and the budget cap.

Like gates.py, this only changes state. Asking the human, and deciding when to check limits
again, belong to the pipeline.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from orchestrator.budget import BudgetLevel, BudgetScope, assess_budget
from orchestrator.config import Config
from orchestrator.state import (
    Allowances,
    GateOutcome,
    GateRecord,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
)

__all__ = [
    "CapAction",
    "CapBackend",
    "CapRequest",
    "CapResolution",
    "cap_request",
    "resolve_cap",
    "run_cap_pause",
]


_CAP_KINDS = (InterventionKind.REVISION_CAP, InterventionKind.BUDGET_CAP)


class CapAction(StrEnum):
    APPROVE_ANYWAY = "approve_anyway"
    RAISE = "raise"
    ABANDON = "abandon"


class CapResolution(BaseModel):
    """What the human chose at a cap pause. `amount` is dollars, or a revision count."""

    action: CapAction
    amount: float | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _check_amount(self) -> Self:
        if self.action == CapAction.RAISE:
            if self.amount is None or self.amount <= 0:
                raise ValueError("raising a cap needs a positive amount")
        elif self.amount is not None:
            raise ValueError("only raising a cap takes an amount")
        return self


class CapRequest(BaseModel):
    """What the human is shown at a cap pause."""

    kind: InterventionKind
    stage: Stage
    detail: str
    can_approve_anyway: bool
    unit: Literal["usd", "revisions"]

    model_config = ConfigDict(frozen=True)


class CapBackend(Protocol):
    """Where the answer to a cap pause comes from: a terminal prompt now, Slack or web later."""

    def resolve(self, request: CapRequest) -> CapResolution: ...


def cap_request(state: ProjectState, config: Config) -> CapRequest:
    """Describe the open cap pause: why it stopped and which answers are allowed."""
    if state.intervention is None or state.intervention.kind not in _CAP_KINDS:
        raise ValueError("no open cap pause to resolve")
    kind = state.intervention.kind
    if kind == InterventionKind.REVISION_CAP:
        can_approve = True
    else:
        can_approve = assess_budget(state, config, state.stage).level != BudgetLevel.EXCEEDED
    return CapRequest(
        kind=kind,
        stage=state.stage,
        detail=state.intervention.detail,
        can_approve_anyway=can_approve,
        unit="revisions" if kind == InterventionKind.REVISION_CAP else "usd",
    )


def run_cap_pause(
    state: ProjectState,
    config: Config,
    backend: CapBackend,
    *,
    now: datetime | None = None,
) -> ProjectState:
    """Ask the human about the open cap pause and apply the answer."""
    resolution = backend.resolve(cap_request(state, config))
    return resolve_cap(state, config, resolution, now=now)


def resolve_cap(
    state: ProjectState,
    config: Config,
    resolution: CapResolution,
    *,
    now: datetime | None = None,
    stage: Stage | None = None,
) -> ProjectState:
    """Apply the human's answer to a revision-cap or budget-cap pause; return the new state.

    Approve-anyway and raise return the project to running. Abandon halts it. Raising never
    re-checks the budget: the pipeline checks before every agent call and will pause again if
    the raise was too small. Continuing at a budget *warning* is remembered against the cap in force
    (`allowances.warned`), so it is not asked again until the cap changes.
    """
    if state.intervention is None or state.intervention.kind not in _CAP_KINDS:
        raise ValueError("no open cap pause to resolve")
    kind = state.intervention.kind

    if resolution.action == CapAction.ABANDON:
        history = state.gate_history
        if kind == InterventionKind.REVISION_CAP:
            history = [*history, _record(state, GateOutcome.REJECT, now)]
        return state.evolve(status=Status.HALTED, intervention=None, gate_history=history)

    running = {"status": Status.RUNNING, "intervention": None}

    if kind == InterventionKind.REVISION_CAP:
        if resolution.action == CapAction.APPROVE_ANYWAY:
            record = _record(state, GateOutcome.APPROVE, now)
            return state.evolve(**running, gate_history=[*state.gate_history, record])
        return _raise_revisions(state, resolution)

    assessment = assess_budget(state, config, stage or state.stage)
    if resolution.action == CapAction.APPROVE_ANYWAY:
        if assessment.level == BudgetLevel.EXCEEDED:
            raise ValueError("a cap has been reached: raise it or abandon, approve is not allowed")
        # Remember it, so the same warning is not asked again before the cap is reached.
        warned = {**state.allowances.warned, **assessment.warned}
        allowances = Allowances(**{**state.allowances.model_dump(), "warned": warned})
        return state.evolve(**running, allowances=allowances)

    assert resolution.amount is not None
    allowances = state.allowances
    if assessment.scope == BudgetScope.PROJECT:
        raised = {
            **allowances.model_dump(),
            "extra_project_usd": allowances.extra_project_usd + resolution.amount,
        }
    else:
        extra = {
            **allowances.extra_stage_usd,
            state.stage: allowances.extra_stage_usd.get(state.stage, 0.0) + resolution.amount,
        }
        raised = {**allowances.model_dump(), "extra_stage_usd": extra}
    return state.evolve(**running, allowances=Allowances.model_validate(raised))


def _raise_revisions(state: ProjectState, resolution: CapResolution) -> ProjectState:
    assert resolution.amount is not None
    if resolution.amount != int(resolution.amount):
        raise ValueError("revisions are raised by a whole number")
    extra = int(resolution.amount)
    allowances = state.allowances
    extra_revisions = {
        **allowances.extra_revisions,
        state.stage: allowances.extra_revisions.get(state.stage, 0) + extra,
    }
    return state.evolve(
        status=Status.RUNNING,
        intervention=None,
        allowances=Allowances.model_validate(
            {**allowances.model_dump(), "extra_revisions": extra_revisions}
        ),
        # The change request that hit the cap is now allowed, so count the pending revision.
        revisions={**state.revisions, state.stage: state.revisions.get(state.stage, 0) + 1},
    )


def _record(state: ProjectState, outcome: GateOutcome, now: datetime | None) -> GateRecord:
    return GateRecord(
        stage=state.stage,
        outcome=outcome,
        comments="",
        ts=now or datetime.now(UTC),
    )
