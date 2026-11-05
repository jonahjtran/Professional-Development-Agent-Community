"""The orchestrator: a deterministic state machine that moves a project through its stages.

It never asks an LLM what to do next. Each call to `advance` does one unit of progress and saves
the state, so a crash at any point resumes from the last saved step.

Stage order:  intake -> product_owner -> architect -> developer -> qa -> career_coach -> done.
Gates (human decisions) follow product_owner, architect, qa and career_coach. A failing QA sends
the work back to the developer, up to `limits.max_qa_loops` times, then goes to the human.
"""

import difflib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.artifacts import (
    STAGE_INPUTS,
    ArtifactStore,
    approve_artifacts,
    record_artifacts,
    stage_inputs,
)
from orchestrator.budget import (
    BudgetLevel,
    assess_budget,
    pause_for_budget,
    record_cost,
    write_cost_report,
)
from orchestrator.caps import CapAction, CapBackend, cap_request, resolve_cap
from orchestrator.config import Config
from orchestrator.designrecord import DesignRecordError, publish_design_record
from orchestrator.gates import GateBackend, GateRequest, apply_decision, open_gate
from orchestrator.halt import halt_requested
from orchestrator.publish import PublishError
from orchestrator.stages import StageSpec
from orchestrator.state import (
    GateOutcome,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    save_state,
)
from orchestrator.trace import EventType, TraceEvent, TraceWriter
from orchestrator.worktree import WorktreeError
from schemas import DevResult, TaskList
from schemas.tasks import Task

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
    track: str = "engineering"
    task_id: str | None = None  # the Developer builds one task per run
    attempt: int = 0  # 0 for the first try at a task; 1, 2, ... after each rejection
    inputs: dict[str, str] = Field(default_factory=dict)  # approved artifacts, by name

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
    web_searches: int = 0  # web searches run during the call (each one is billed)

    model_config = ConfigDict(frozen=True)


class AgentError(Exception):
    """An agent call failed. `cost_usd` is what the failed call still cost, so it can be billed."""

    def __init__(self, message: str, *, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class AgentRunner(Protocol):
    """Runs one agent call. A dummy echo agent now; the Agent SDK wrapper later."""

    def run(self, request: AgentRequest) -> AgentResult: ...


class TaskPublisher(Protocol):
    """Publishes a finished Developer task as a pull request (see publish.Publisher)."""

    def publish(self, result: DevResult, *, title: str) -> DevResult: ...

    def diff_stat(self, branch: str) -> str: ...


class RoutingRunner:
    """Sends each stage to its own runner, for when stages use different kinds of agent."""

    def __init__(self, runners: Mapping[Stage, AgentRunner], default: AgentRunner | None = None):
        self._runners = dict(runners)
        self._default = default

    def run(self, request: AgentRequest) -> AgentResult:
        runner = self._runners.get(request.stage, self._default)
        if runner is None:
            raise AgentError(f"no agent is set up for the {request.stage.value} stage")
        return runner.run(request)


@dataclass(frozen=True)
class PipelineContext:
    """Everything the pipeline needs from the outside world, passed in so tests can fake it."""

    config: Config
    runs_dir: Path
    runner: AgentRunner
    gates: GateBackend
    caps: CapBackend
    # Stages whose output is a validated document stored as artifacts. Others stay plain text.
    stages: Mapping[Stage, StageSpec] = field(default_factory=dict)
    # When set, the Developer stage becomes a loop: one task at a time, each published as a pull
    # request and reviewed at a gate. When None, the Developer stage is a plain agent call.
    publisher: TaskPublisher | None = None
    # The project's own repository; with a publisher it receives the design record as a first PR.
    repo: Path | None = None


def run_pipeline(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Advance until the project is done or halted. Also used to resume a saved project."""
    while state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)
    return state


def advance(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Do one unit of progress and save the result."""
    if state.status in (Status.DONE, Status.HALTED):
        raise ValueError(f"the project is finished ({state.status.value}); nothing to advance")
    if halt_requested(ctx.runs_dir, state.project_slug):  # `studio halt`: stop before any step
        _trace(
            ctx,
            state,
            EventType.TRANSITION,
            details={"to": "halted", "reason": "a halt was requested with studio halt"},
        )
        halted = state.evolve(status=Status.HALTED, intervention=None)
        _save(halted, ctx)
        return halted
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

    stage = state.stage
    loop = stage == Stage.DEVELOPER and ctx.publisher is not None
    task: Task | None = None
    if loop:
        task = _next_task(state, ctx)
        if task is None:  # every task is built and approved
            return _move(state, ctx, Stage.QA)
        state = state.evolve(current_task_id=task.id)

    if not budget_checked:
        state = _check_budget(state, ctx)
        if state.status != Status.RUNNING:
            return state

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
    state = record_cost(state, stage, result.cost_usd)
    spec = ctx.stages.get(stage)
    summary: str | None = None
    details: dict[str, object] = {}
    try:
        if spec is not None:
            state, summary = _store_artifacts(state, ctx, spec, result.output)
            details = {"artifacts": sorted(state.artifacts)}
        elif task is not None:
            state, summary, details = _publish_task(state, ctx, task, result.output)
    except ValidationError as error:
        _trace_agent_end(ctx, state, result, started, details={"error": f"not valid: {error}"})
        return state.evolve(status=Status.HALTED)
    except PublishError as error:
        _trace_agent_end(ctx, state, result, started, details={"error": str(error)})
        return state.evolve(status=Status.HALTED)
    _trace_agent_end(ctx, state, result, started, details=details)

    if stage == Stage.QA and result.passed is False:
        if state.qa_loops < ctx.config.limits.max_qa_loops:
            _write_draft(ctx, state, result.output)
            return _move(state.evolve(qa_loops=state.qa_loops + 1), ctx, Stage.DEVELOPER)
        output = (
            f"{result.output}\n\n"
            f"QA FAILED: the Developer's {state.qa_loops} retries did not fix it."
        )
    else:
        output = summary if summary is not None else result.output
    _write_draft(ctx, state, output)

    if stage not in _GATED and not loop:
        return _move(state, ctx, _next_stage(stage))

    waiting = open_gate(state, title=f"Review the {stage.value} output")
    _save(waiting, ctx)  # save before asking, so a crash resumes at this gate
    return _ask_gate(waiting, ctx)


def _build_request(state: ProjectState, ctx: PipelineContext) -> AgentRequest:
    stage = state.stage
    spec = ctx.stages.get(stage)
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    draft = _read_draft(ctx, state, stage)
    loop = stage == Stage.DEVELOPER and ctx.publisher is not None
    if spec is not None and spec.json_name in state.artifacts:
        # Revise the document itself, not the version rendered for the human.
        draft = store.read(state.artifacts[spec.json_name])
    if loop and f"dev/{state.current_task_id}.json" in state.artifacts:
        draft = store.read(state.artifacts[f"dev/{state.current_task_id}.json"])
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
        track=state.track.value,
        inputs=stage_inputs(state, stage, store)
        if (spec or loop) and stage in STAGE_INPUTS
        else {},
        task_id=state.current_task_id if loop else None,
        attempt=_attempt(state) if loop else 0,
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
    unapproved = [name for name, a in state.artifacts.items() if a.approved_at is None]
    state = approve_artifacts(state, unapproved, now=datetime.now(UTC))
    if state.stage == Stage.DEVELOPER and ctx.publisher is not None:
        # One task approved: record it and stay on the Developer for the next one.
        assert state.current_task_id is not None
        return state.evolve(
            completed_tasks=[*state.completed_tasks, state.current_task_id],
            current_task_id=None,
            revisions={**state.revisions, Stage.DEVELOPER: 0},
        )
    if state.stage == Stage.ARCHITECT and ctx.publisher is not None and ctx.repo is not None:
        try:
            state = _publish_design_record(state, ctx)
        except (DesignRecordError, PublishError, WorktreeError) as error:
            _trace(ctx, state, EventType.TRANSITION, details={"error": str(error)})
            return state.evolve(status=Status.HALTED)
    if state.stage == Stage.CAREER_COACH:
        _trace(ctx, state, EventType.TRANSITION, details={"to": "done"})
        return state.evolve(status=Status.DONE)
    return _move(state.evolve(qa_loops=0), ctx, _next_stage(state.stage))


# --- helpers ---------------------------------------------------------------------------------


def _store_artifacts(
    state: ProjectState, ctx: PipelineContext, spec: StageSpec, output: str
) -> tuple[ProjectState, str]:
    """Validate the agent's document, save its artifacts, and build the text for the human.

    Raises pydantic.ValidationError if the output is not a valid document.
    """
    model = spec.schema.model_validate_json(output)
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    inputs = stage_inputs(state, state.stage, store) if state.stage in STAGE_INPUTS else {}
    previous_md = (
        store.read(state.artifacts[spec.md_name]) if spec.md_name in state.artifacts else None
    )
    previous_json = state.artifacts.get(spec.json_name)
    revision = previous_json.revision + 1 if previous_json else 1
    written = {
        name: store.write(name, text, revision=revision)
        for name, text in spec.build(model, inputs).items()
    }
    kept = {
        name: a
        for name, a in state.artifacts.items()
        if not any(name.startswith(prefix) for prefix in spec.replaces)
    }
    state = record_artifacts(state.evolve(artifacts=kept), written)
    new_md = store.read(written[spec.md_name])
    return state, f"{new_md}\n\n---\n{_changes_since(previous_md, new_md)}"


def _changes_since(previous: str | None, current: str) -> str:
    if previous is None:
        return "This is the first draft; there is nothing to compare it with."
    diff = list(
        difflib.unified_diff(
            previous.splitlines(), current.splitlines(), "previous", "this draft", lineterm=""
        )
    )
    if not diff:
        return "No changes since the previous draft."
    return "Changes since the previous draft:\n" + "\n".join(diff)


def _trace_agent_end(
    ctx: PipelineContext,
    state: ProjectState,
    result: AgentResult,
    started: float,
    *,
    details: dict[str, object],
) -> None:
    _trace(
        ctx,
        state,
        EventType.AGENT_END,
        agent=state.stage.value,
        model=result.model,
        prompt_version=result.prompt_version,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        latency_ms=int((time.monotonic() - started) * 1000),
        details={**details, "web_searches": result.web_searches},
    )


def _publish_design_record(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Open the approved PRD, architecture and decisions as the project repo's first PR."""
    assert ctx.publisher is not None and ctx.repo is not None
    url = publish_design_record(
        state,
        repo=ctx.repo,
        runs_dir=ctx.runs_dir,
        publisher=ctx.publisher,
    )
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    artifact = store.write("design_record.json", json.dumps({"pr_url": url}, indent=2), revision=1)
    state = record_artifacts(state, {"design_record.json": artifact})
    state = approve_artifacts(state, ["design_record.json"], now=datetime.now(UTC))
    _trace(ctx, state, EventType.TRANSITION, details={"design_record_pr": url})
    return state


def _next_task(state: ProjectState, ctx: PipelineContext) -> Task | None:
    """The task to build now: the one in progress, else the first one not yet approved.

    TaskList guarantees tasks are listed after everything they depend on, so the first
    unfinished task always has its dependencies done.
    """
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    raw = stage_inputs(state, Stage.DEVELOPER, store)["tasks.json"]
    tasks = TaskList.model_validate_json(raw).tasks
    if state.current_task_id is not None:
        return next(t for t in tasks if t.id == state.current_task_id)
    done = set(state.completed_tasks)
    return next((t for t in tasks if t.id not in done), None)


def _attempt(state: ProjectState) -> int:
    """How many times the current task was rejected since the last approval (0 on a first try)."""
    rejected = 0
    for record in reversed(state.gate_history):
        if record.stage != Stage.DEVELOPER:
            continue
        if record.outcome == GateOutcome.APPROVE:
            break
        if record.outcome == GateOutcome.REJECT:
            rejected += 1
    return rejected


def _publish_task(
    state: ProjectState, ctx: PipelineContext, task: Task, output: str
) -> tuple[ProjectState, str, dict[str, object]]:
    """Publish a finished task as a pull request, record it, and build the text for the gate.

    Raises pydantic.ValidationError for a malformed result and PublishError if publishing fails.
    """
    assert ctx.publisher is not None
    result = DevResult.model_validate_json(output)
    result = ctx.publisher.publish(result, title=f"{task.id}: {task.title}")
    stat = ctx.publisher.diff_stat(result.branch)
    name = f"dev/{task.id}.json"
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    previous = state.artifacts.get(name)
    artifact = store.write(
        name, result.model_dump_json(indent=2), revision=previous.revision + 1 if previous else 1
    )
    state = record_artifacts(state, {name: artifact})

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "None"

    summary = (
        f"Pull request: {result.pr_url}\n"
        f"Branch: {result.branch}\n\n"
        f"## Summary\n{result.summary}\n\n"
        f"## Deviations from the plan\n{bullets(result.deviations_from_plan)}\n\n"
        f"## Known gaps\n{bullets(result.known_gaps)}\n\n"
        f"## Changes\n{stat}\n"
    )
    return state, summary, {"task_id": task.id, "pr_url": result.pr_url, "artifacts": [name]}


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
