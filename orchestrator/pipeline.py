"""The orchestrator: a deterministic state machine that moves a project through its stages.

It never asks an LLM what to do next. Each call to `advance` does one unit of progress and saves
the state, so a crash at any point resumes from the last saved step.

Stage order:  intake -> product_owner -> architect -> developer -> qa -> career_coach -> done.
Gates (human decisions) follow product_owner, architect, qa and career_coach. A failing QA sends
the work back to the developer, up to `limits.max_qa_loops` times, then goes to the human.
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from orchestrator.budget import (
    BudgetLevel,
    assess_budget,
    pause_for_budget,
    record_cost,
    write_cost_report,
)
from orchestrator.caps import CapAction, CapBackend, cap_request, resolve_cap
from orchestrator.config import Config
from orchestrator.gates import GateBackend, GateRequest, apply_decision, open_gate
from orchestrator.state import (
    GateOutcome,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    save_state,
)
from orchestrator.trace import EventType, TraceEvent, TraceWriter

__all__ = [
    "AgentError",
    "AgentRequest",
    "AgentResult",
    "AgentRunner",
    "PipelineContext",
    "advance",
    "run_pipeline",
]

_ORDER = [
    Stage.INTAKE,
    Stage.PRODUCT_OWNER,
    Stage.ARCHITECT,
    Stage.DEVELOPER,
    Stage.QA,
    Stage.CAREER_COACH,
]
_GATED = {Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.QA, Stage.CAREER_COACH}


class AgentRequest(BaseModel):
    """What an agent is asked to do. `comments` is feedback it must address."""

    stage: Stage
    project: str
    idea: str
    previous_draft: str | None = None
    comments: str | None = None
    revision: int = 0

    model_config = ConfigDict(frozen=True)


class AgentResult(BaseModel):
    """What an agent produced. `passed` is only meaningful for QA."""

    output: str
    cost_usd: float
    model: str = "dummy"
    prompt_version: str = "v0"
    passed: bool | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    model_config = ConfigDict(frozen=True)


class AgentError(Exception):
    """An agent call failed. `cost_usd` is what the failed call still cost, so it can be billed."""

    def __init__(self, message: str, *, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class AgentRunner(Protocol):
    """Runs one agent call. A dummy echo agent now; the Agent SDK wrapper later."""

    def run(self, request: AgentRequest) -> AgentResult: ...


@dataclass(frozen=True)
class PipelineContext:
    """Everything the pipeline needs from the outside world, passed in so tests can fake it."""

    config: Config
    runs_dir: Path
    runner: AgentRunner
    gates: GateBackend
    caps: CapBackend


def run_pipeline(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Advance until the project is done or halted. Also used to resume a saved project."""
    while state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)
    return state


def advance(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Do one unit of progress and save the result."""
    if state.status in (Status.DONE, Status.HALTED):
        raise ValueError(f"the project is finished ({state.status.value}); nothing to advance")
    new = (
        _answer_pause(state, ctx)
        if state.status == Status.AWAITING_HUMAN
        else _run_stage(state, ctx)
    )
    _save(new, ctx)
    return new


# --- running a stage -------------------------------------------------------------------------


def _run_stage(
    state: ProjectState, ctx: PipelineContext, *, budget_checked: bool = False
) -> ProjectState:
    if state.stage == Stage.INTAKE:
        return _move(state, ctx, Stage.PRODUCT_OWNER)

    if not budget_checked:
        state = _check_budget(state, ctx)
        if state.status != Status.RUNNING:
            return state

    stage = state.stage
    request = _build_request(state, ctx)
    _trace(ctx, state, EventType.AGENT_START, agent=stage.value)
    started = time.monotonic()
    try:
        result = ctx.runner.run(request)
    except AgentError as error:
        # An unrecoverable failure: bill what it cost, record it, and stop for good.
        _trace(
            ctx,
            state,
            EventType.AGENT_END,
            agent=stage.value,
            cost_usd=error.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            details={"error": str(error)},
        )
        return record_cost(state, stage, error.cost_usd).evolve(status=Status.HALTED)
    _trace(
        ctx,
        state,
        EventType.AGENT_END,
        agent=stage.value,
        model=result.model,
        prompt_version=result.prompt_version,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    state = record_cost(state, stage, result.cost_usd)

    if stage == Stage.QA and result.passed is False:
        if state.qa_loops < ctx.config.limits.max_qa_loops:
            _write_draft(ctx, state, result.output)
            return _move(state.evolve(qa_loops=state.qa_loops + 1), ctx, Stage.DEVELOPER)
        output = (
            f"{result.output}\n\n"
            f"QA FAILED: the Developer's {state.qa_loops} retries did not fix it."
        )
    else:
        output = result.output
    _write_draft(ctx, state, output)

    if stage not in _GATED:
        return _move(state, ctx, _next_stage(stage))

    waiting = open_gate(state, title=f"Review the {stage.value} output")
    _save(waiting, ctx)  # save before asking, so a crash resumes at this gate
    return _ask_gate(waiting, ctx)


def _build_request(state: ProjectState, ctx: PipelineContext) -> AgentRequest:
    stage = state.stage
    draft = _read_draft(ctx, state, stage)
    last = state.gate_history[-1] if state.gate_history else None
    previous: str | None = None
    comments: str | None = None
    if stage == Stage.DEVELOPER and state.qa_loops > 0:
        previous, comments = draft, _read_draft(ctx, state, Stage.QA)
    elif last is not None and last.stage == stage:
        if last.outcome == GateOutcome.REQUEST_CHANGES:
            previous, comments = draft, last.comments
        elif last.outcome == GateOutcome.REJECT:
            comments = last.comments or None
    return AgentRequest(
        stage=stage,
        project=state.project_slug,
        idea=state.idea,
        previous_draft=previous,
        comments=comments,
        revision=state.revisions.get(stage, 0),
    )


# --- budget ----------------------------------------------------------------------------------


def _check_budget(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Pause for the human while the next call would be over budget or past the warning line.

    Returns a RUNNING state when the call may go ahead, or a HALTED one if the human abandons.
    Continuing at a warning lets this one call through; the next call is checked again.
    """
    while True:
        assessment = assess_budget(state, ctx.config, state.stage)
        if assessment.level == BudgetLevel.OK:
            return state
        paused = pause_for_budget(state, assessment)
        _save(paused, ctx)
        resolution = ctx.caps.resolve(cap_request(paused, ctx.config))
        state = resolve_cap(paused, ctx.config, resolution)
        _trace(ctx, state, EventType.GATE, details={"cap": resolution.action.value})
        if state.status != Status.RUNNING:
            return state
        if resolution.action == CapAction.APPROVE_ANYWAY:
            return state


# --- gates and pauses ------------------------------------------------------------------------


def _answer_pause(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    assert state.intervention is not None
    kind = state.intervention.kind
    if kind == InterventionKind.GATE:
        return _ask_gate(state, ctx)
    if kind == InterventionKind.REVISION_CAP:
        return _settle(_ask_cap(state, ctx), ctx)
    if kind == InterventionKind.BUDGET_CAP:
        resolution = ctx.caps.resolve(cap_request(state, ctx.config))
        resolved = resolve_cap(state, ctx.config, resolution)
        _trace(ctx, resolved, EventType.GATE, details={"cap": resolution.action.value})
        if resolved.status == Status.RUNNING and resolution.action == CapAction.APPROVE_ANYWAY:
            return _run_stage(resolved, ctx, budget_checked=True)
        return resolved
    raise ValueError(f"cannot answer a {kind.value} pause yet")


def _ask_gate(waiting: ProjectState, ctx: PipelineContext) -> ProjectState:
    assert waiting.intervention is not None
    request = GateRequest(
        stage=waiting.stage,
        title=waiting.intervention.detail,
        summary=_read_draft(ctx, waiting, waiting.stage) or "",
        cost_usd=waiting.total_cost_usd,
    )
    decision = ctx.gates.decide(request)
    state = apply_decision(waiting, ctx.config, decision)
    _trace(
        ctx,
        state,
        EventType.GATE,
        details={"outcome": decision.outcome.value, "comments": decision.comments},
    )
    if state.status == Status.AWAITING_HUMAN:  # the revision cap was hit
        _save(state, ctx)
        state = _ask_cap(state, ctx)
    return _settle(state, ctx)


def _ask_cap(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    resolution = ctx.caps.resolve(cap_request(state, ctx.config))
    resolved = resolve_cap(state, ctx.config, resolution)
    _trace(ctx, resolved, EventType.GATE, details={"cap": resolution.action.value})
    return resolved


def _settle(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """After a human decision: move on if the stage was approved, otherwise stay to re-run it."""
    if state.status != Status.RUNNING:
        return state
    last = state.gate_history[-1]
    if last.outcome != GateOutcome.APPROVE:
        return state
    if state.stage == Stage.CAREER_COACH:
        _trace(ctx, state, EventType.TRANSITION, details={"to": "done"})
        return state.evolve(status=Status.DONE)
    return _move(state.evolve(qa_loops=0), ctx, _next_stage(state.stage))


# --- helpers ---------------------------------------------------------------------------------


def _next_stage(stage: Stage) -> Stage:
    return _ORDER[_ORDER.index(stage) + 1]


def _move(state: ProjectState, ctx: PipelineContext, to: Stage) -> ProjectState:
    _trace(ctx, state, EventType.TRANSITION, details={"from": state.stage.value, "to": to.value})
    return state.evolve(stage=to)


def _save(state: ProjectState, ctx: PipelineContext) -> None:
    save_state(ctx.runs_dir, state)
    write_cost_report(ctx.runs_dir, state)


def _trace(ctx: PipelineContext, state: ProjectState, event: EventType, **fields: object) -> None:
    TraceWriter(ctx.runs_dir, state.project_slug).write(
        TraceEvent.model_validate(
            {"project": state.project_slug, "stage": state.stage, "event": event, **fields}
        )
    )


def _draft_path(ctx: PipelineContext, state: ProjectState, stage: Stage) -> Path:
    return ctx.runs_dir / state.project_slug / "drafts" / f"{stage.value}.md"


def _write_draft(ctx: PipelineContext, state: ProjectState, text: str) -> None:
    path = _draft_path(ctx, state, state.stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _read_draft(ctx: PipelineContext, state: ProjectState, stage: Stage) -> str | None:
    path = _draft_path(ctx, state, stage)
    return path.read_text() if path.exists() else None
