from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.config import Config
from orchestrator.state import (
    GateOutcome,
    GateRecord,
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
)

__all__ = [
    "GateBackend",
    "GateDecision",
    "GateItem",
    "GateOutcome",
    "GateRequest",
    "apply_decision",
    "open_gate",
    "run_gate",
]


class GateItem(BaseModel):
    """One thing at a gate the human can approve or skip on its own."""

    id: str
    label: str

    model_config = ConfigDict(frozen=True)


class GateRequest(BaseModel):
    """What the human is shown at a gate."""

    stage: Stage
    title: str
    summary: str = ""
    cost_usd: float
    items: list[GateItem] = Field(default_factory=list)  # approve or skip each separately
    questions: list[str] = Field(default_factory=list)  # facts the agent needs from the human

    model_config = ConfigDict(frozen=True)


class GateDecision(BaseModel):
    """What the human decided. `halt` only applies to a rejection."""

    outcome: GateOutcome
    comments: str = ""
    halt: bool = False
    approved_items: list[str] = Field(default_factory=list)  # ids of the items approved
    answers: dict[str, str] = Field(default_factory=dict)  # question -> the human's answer

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.outcome == GateOutcome.REQUEST_CHANGES and not self.comments.strip():
            raise ValueError("request_changes needs comments saying what to change")
        if self.halt and self.outcome != GateOutcome.REJECT:
            raise ValueError("only a rejection can halt the project")
        if self.approved_items and self.outcome != GateOutcome.APPROVE:
            raise ValueError("items can only be approved when the stage is approved")
        return self


class GateBackend(Protocol):
    """Where the human decision comes from: a terminal prompt now, Slack or a web UI later."""

    def decide(self, request: GateRequest) -> GateDecision: ...


def open_gate(state: ProjectState, *, title: str) -> ProjectState:
    """Mark the project as waiting for a human decision at its current stage."""
    return state.evolve(
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail=title),
    )


def apply_decision(
    state: ProjectState,
    config: Config,
    decision: GateDecision,
    *,
    now: datetime | None = None,
) -> ProjectState:
    """Record the human's decision and move the project on.

    Approve and reject-and-restart return the project to running; the pipeline reads the last
    gate_history entry to see which one happened. A request for changes beyond the revision cap
    stops for a human again instead of re-running the agent.
    """
    if (
        state.status != Status.AWAITING_HUMAN
        or state.intervention is None
        or state.intervention.kind != InterventionKind.GATE
    ):
        raise ValueError("no open gate to decide")

    record = GateRecord(
        stage=state.stage,
        outcome=decision.outcome,
        comments=decision.comments,
        ts=now or datetime.now(UTC),
    )
    history = [*state.gate_history, record]
    running = {"status": Status.RUNNING, "intervention": None, "gate_history": history}

    if decision.outcome == GateOutcome.REQUEST_CHANGES:
        used = state.revisions.get(state.stage, 0)
        max_revisions = config.limits.max_revisions + state.allowances.extra_revisions.get(
            state.stage, 0
        )
        if used >= max_revisions:
            return state.evolve(
                gate_history=history,
                intervention=Intervention(
                    kind=InterventionKind.REVISION_CAP,
                    detail=(
                        f"{state.stage.value} has used all {max_revisions} revisions "
                        "and changes were requested again"
                    ),
                ),
            )
        return state.evolve(**running, revisions={**state.revisions, state.stage: used + 1})

    if decision.outcome == GateOutcome.REJECT and decision.halt:
        return state.evolve(**{**running, "status": Status.HALTED})

    return state.evolve(**running)


def run_gate(
    state: ProjectState,
    config: Config,
    backend: GateBackend,
    *,
    title: str,
    summary: str = "",
    on_waiting: Callable[[ProjectState], None] | None = None,
    now: datetime | None = None,
) -> ProjectState:
    """Open the gate, ask the human, and apply the answer.

    `on_waiting` is called with the waiting state before the human is asked, so the caller can
    save it. A crash while the human is deciding then resumes at the gate.
    """
    waiting = open_gate(state, title=title)
    if on_waiting is not None:
        on_waiting(waiting)
    request = GateRequest(
        stage=state.stage, title=title, summary=summary, cost_usd=state.total_cost_usd
    )
    return apply_decision(waiting, config, backend.decide(request), now=now)
