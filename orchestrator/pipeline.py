"""The orchestrator: a deterministic state machine that moves a project through its stages.

It never asks an LLM what to do next. Each call to `advance` does one unit of progress and saves
the state, so a crash at any point resumes from the last saved step.

Stage order:  intake -> product_owner -> architect -> developer -> qa -> career_coach -> done.
Gates (human decisions) follow product_owner, architect, qa and career_coach. With the dummy QA
agent, a failing QA sends the work back to the developer up to `limits.max_qa_loops` times. With the
real QA tools (`ctx.qa`), QA runs once on the merged project and a failing report goes to the human.
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
from orchestrator.career_publish import CareerPublisher
from orchestrator.career_stage import CareerError, CareerPreparer, gate_items, human_evidence
from orchestrator.config import Config
from orchestrator.designrecord import DesignRecordError, publish_design_record
from orchestrator.evidence import build_evidence
from orchestrator.gates import (
    GateBackend,
    GateDecision,
    GateItem,
    GateRequest,
    apply_decision,
    open_gate,
)
from orchestrator.halt import halt_requested
from orchestrator.publish import PublishError
from orchestrator.qa_prepare import MergeBackend, QaError, QaPreparer
from orchestrator.stages import StageSpec
from orchestrator.state import (
    GateOutcome,
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    save_state,
)
from orchestrator.todos import add_todo
from orchestrator.trace import EventType, TraceEvent, TraceWriter
from orchestrator.worktree import WorktreeError
from schemas import CareerPlan, CheckStatus, DevResult, EvidenceSet, QaReport, TaskList
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
    # When set, the QA stage first checks the Developer pull requests are merged, then runs the real
    # tools on the merged project and hands their results to the QA agent.
    qa: QaPreparer | None = None
    merges: MergeBackend | None = None
    # When set, the career stage first prepares the coach's inputs (profile, website, README).
    career: CareerPreparer | None = None
    # When set, approved career items are published as pull requests (see career_publish).
    career_publisher: CareerPublisher | None = None


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

    if stage == Stage.QA and ctx.qa is not None:
        state = _prepare_qa(state, ctx, ctx.qa)
        if state.status != Status.RUNNING:
            return state
    if stage == Stage.CAREER_COACH and ctx.career is not None:
        state = _prepare_career(state, ctx, ctx.career)
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
        _raise_pause_todo(ctx, state, str(error))
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
        _raise_pause_todo(ctx, state, f"the {stage.value} result was not valid")
        return state.evolve(status=Status.HALTED)
    except PublishError as error:
        _trace_agent_end(ctx, state, result, started, details={"error": str(error)})
        _raise_pause_todo(ctx, state, str(error))
        return state.evolve(status=Status.HALTED)
    _trace_agent_end(ctx, state, result, started, details=details)

    if stage == Stage.QA and "test_report.json" in state.artifacts:
        # The orchestrator's own pass/fail rule, not the model's word.
        store = ArtifactStore(ctx.runs_dir, state.project_slug)
        report = QaReport.model_validate_json(store.read(state.artifacts["test_report.json"]))
        result = result.model_copy(update={"passed": report.passed})
        for check in report.checks:
            if check.status == CheckStatus.SKIPPED:
                add_todo(
                    ctx.runs_dir,
                    state.project_slug,
                    key=f"qa:{check.name}",
                    title=f"Install what QA needs to run the {check.name}",
                    detail=check.output_tail or f"The {check.name} did not run, so QA cannot pass.",
                    source="qa",
                )

    if stage == Stage.QA and result.passed is False:
        # With real QA tools there is no task to send back, so a failure always goes to the human.
        if ctx.qa is None and state.qa_loops < ctx.config.limits.max_qa_loops:
            _write_draft(ctx, state, result.output)
            return _move(state.evolve(qa_loops=state.qa_loops + 1), ctx, Stage.DEVELOPER)
        shown = summary if summary is not None else result.output
        output = f"{shown}\n\n---\n{_qa_failed_note(state.qa_loops)}"
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
        _save(state, ctx)  # so the UI stops showing "awaiting human" while the agent works
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
    if kind == InterventionKind.MERGE_PENDING:
        return _run_stage(state.evolve(status=Status.RUNNING, intervention=None), ctx)
    if kind == InterventionKind.BUDGET_CAP:
        resolution = ctx.caps.resolve(cap_request(state, ctx.config))
        resolved = resolve_cap(state, ctx.config, resolution)
        _save(resolved, ctx)
        _trace(ctx, resolved, EventType.GATE, details={"cap": resolution.action.value})
        if resolved.status == Status.RUNNING and resolution.action == CapAction.APPROVE_ANYWAY:
            return _run_stage(resolved, ctx, budget_checked=True)
        return resolved
    raise ValueError(f"cannot answer a {kind.value} pause yet")


def _ask_gate(waiting: ProjectState, ctx: PipelineContext) -> ProjectState:
    assert waiting.intervention is not None
    items, questions = _career_extras(waiting, ctx)
    request = GateRequest(
        stage=waiting.stage,
        title=waiting.intervention.detail,
        summary=_read_draft(ctx, waiting, waiting.stage) or "",
        cost_usd=waiting.total_cost_usd,
        items=items,
        questions=questions,
    )
    decision = ctx.gates.decide(request)
    state = apply_decision(waiting, ctx.config, decision)
    state = _record_career_decision(state, ctx, decision)
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
        state = _publish_career(state, ctx)
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

    for need in result.needs_from_human:
        add_todo(
            ctx.runs_dir,
            state.project_slug,
            key=f"developer:{task.id}:{need}",
            title=need,
            detail=f"Raised by the Developer on {task.id}: {task.title}",
            source="developer",
        )
    needs = (
        f"## Needs you\n{bullets(result.needs_from_human)}\n\n" if result.needs_from_human else ""
    )
    summary = (
        f"Pull request: {result.pr_url}\n"
        f"Branch: {result.branch}\n\n"
        f"## Summary\n{result.summary}\n\n"
        f"{needs}"
        f"## Deviations from the plan\n{bullets(result.deviations_from_plan)}\n\n"
        f"## Known gaps\n{bullets(result.known_gaps)}\n\n"
        f"## Changes\n{stat}\n"
    )
    return state, summary, {"task_id": task.id, "pr_url": result.pr_url, "artifacts": [name]}


def _raise_pause_todo(ctx: PipelineContext, state: ProjectState, reason: str) -> None:
    """Tell the human the project stopped on its own and why; resuming closes this item."""
    add_todo(
        ctx.runs_dir,
        state.project_slug,
        key=f"error:{state.stage.value}:{reason}",
        title=f"{state.project_slug} paused at the {state.stage.value} stage",
        detail=reason,
        source="error",
    )


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


def _qa_failed_note(retries: int) -> str:
    if retries:
        return f"QA FAILED: the Developer's {retries} retries did not fix it."
    return "QA FAILED. Approve to accept the work as it is, or ask for QA to run again."


def _prepare_qa(state: ProjectState, ctx: PipelineContext, qa: QaPreparer) -> ProjectState:
    """Wait for the Developer pull requests to be merged, run the real tools, store the results.

    The results become approved inputs for the QA agent: they are machine output, not a draft
    for the human to review, and each QA run replaces the last.
    """
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    urls = _task_pr_urls(state, store)
    try:
        waiting = qa.unmerged(urls)
        while waiting:
            if ctx.merges is None:
                raise QaError("these pull requests are not merged: " + ", ".join(waiting))
            paused = state.evolve(
                status=Status.AWAITING_HUMAN,
                intervention=Intervention(
                    kind=InterventionKind.MERGE_PENDING,
                    detail="Merge these pull requests, then check again: " + ", ".join(waiting),
                ),
            )
            _save(paused, ctx)  # save before asking, so a crash resumes at this question
            if not ctx.merges.wait_for_merge(waiting):
                _trace(ctx, paused, EventType.TRANSITION, details={"to": "halted"})
                return paused.evolve(status=Status.HALTED, intervention=None)
            waiting = qa.unmerged(waiting)
        materials = qa.run()
    except (QaError, PublishError) as error:
        _trace(ctx, state, EventType.TRANSITION, details={"error": str(error)})
        return state.evolve(status=Status.HALTED, intervention=None)

    previous = state.artifacts.get("qa_checks.json")
    revision = previous.revision + 1 if previous else 1
    checks_json = json.dumps([c.model_dump(mode="json") for c in materials.checks], indent=2)
    written = {
        "qa_checks.json": store.write("qa_checks.json", checks_json, revision=revision),
        "qa_tests.md": store.write("qa_tests.md", materials.tests_md, revision=revision),
    }
    state = approve_artifacts(
        record_artifacts(state.evolve(status=Status.RUNNING, intervention=None), written),
        list(written),
        now=datetime.now(UTC),
    )
    _trace(
        ctx,
        state,
        EventType.TRANSITION,
        details={"qa_checks": {c.name: c.status.value for c in materials.checks}},
    )
    return state


def _task_pr_urls(state: ProjectState, store: ArtifactStore) -> list[str]:
    """The pull request of every approved task: the latest attempt, if it was rejected before."""
    urls: list[str] = []
    for task_id in state.completed_tasks:
        artifact = state.artifacts.get(f"dev/{task_id}.json")
        if artifact is None:
            continue
        pr_url = DevResult.model_validate_json(store.read(artifact)).pr_url
        if pr_url:
            urls.append(pr_url)
    return urls


# --- the career stage ------------------------------------------------------------------------


def _career_answers(state: ProjectState, store: ArtifactStore) -> list[tuple[str, str]]:
    artifact = state.artifacts.get("career_answers.json")
    if artifact is None:
        return []
    return [(item["question"], item["answer"]) for item in json.loads(store.read(artifact))]


def _prepare_career(
    state: ProjectState, ctx: PipelineContext, career: CareerPreparer
) -> ProjectState:
    """Gather everything the career coach may read, as approved inputs.

    Evidence is rebuilt on every run so the person's answers at the gate (kept in
    career_answers.json) become evidence the next draft can use.
    """
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    try:
        materials = career.materials()
    except CareerError as error:
        _trace(ctx, state, EventType.TRANSITION, details={"error": str(error)})
        return state.evolve(status=Status.HALTED)
    eval_results = ctx.repo / "evals" / "results.csv" if ctx.repo is not None else None
    base = build_evidence(state, ctx.runs_dir, eval_results=eval_results)
    evidence = EvidenceSet(items=[*base.items, *human_evidence(_career_answers(state, store))])
    texts = {
        "evidence.json": evidence.model_dump_json(indent=2),
        "resume.yaml": materials.resume_yaml,
        "goals.yaml": materials.goals_yaml,
        "website_projects.js": materials.website_projects_js,
        "current_readme.md": materials.current_readme,
    }
    previous = state.artifacts.get("evidence.json")
    revision = previous.revision + 1 if previous else 1
    written = {n: store.write(n, text, revision=revision) for n, text in texts.items()}
    state = approve_artifacts(
        record_artifacts(state, written), list(written), now=datetime.now(UTC)
    )
    _trace(ctx, state, EventType.TRANSITION, details={"career_inputs": sorted(written)})
    return state


def _career_extras(state: ProjectState, ctx: PipelineContext) -> tuple[list[GateItem], list[str]]:
    """The items to approve and the questions to ask at the career gate (none at other gates)."""
    artifact = state.artifacts.get("career_plan.json")
    if state.stage != Stage.CAREER_COACH or artifact is None:
        return [], []
    plan = CareerPlan.model_validate_json(
        ArtifactStore(ctx.runs_dir, state.project_slug).read(artifact)
    )
    return gate_items(plan, website=bool(_website_projects(state, ctx))), list(plan.questions)


def _website_projects(state: ProjectState, ctx: PipelineContext) -> str:
    artifact = state.artifacts.get("website_projects.js")
    if artifact is None:
        return ""
    return ArtifactStore(ctx.runs_dir, state.project_slug).read(artifact).strip()


def _record_career_decision(
    state: ProjectState, ctx: PipelineContext, decision: GateDecision
) -> ProjectState:
    """Keep the person's answers (they become evidence) and which items they approved."""
    if (
        state.stage != Stage.CAREER_COACH
        or "career_plan.json" not in state.artifacts
        or state.status == Status.HALTED
    ):
        return state
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    written = {}
    if decision.answers:
        answers = [
            *_career_answers(state, store),
            *((q, a) for q, a in decision.answers.items()),
        ]
        previous = state.artifacts.get("career_answers.json")
        text = json.dumps([{"question": q, "answer": a} for q, a in answers], indent=2)
        written["career_answers.json"] = store.write(
            "career_answers.json", text, revision=previous.revision + 1 if previous else 1
        )
    if decision.outcome == GateOutcome.APPROVE:
        text = json.dumps({"approved_items": decision.approved_items}, indent=2)
        written["career_approved.json"] = store.write("career_approved.json", text, revision=1)
    return record_artifacts(state, written) if written else state


def _publish_career(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    """Open a pull request for each approved career item, and record where each one went.

    A failure in one target is recorded and never stops the others or the project finishing.
    """
    publisher = ctx.career_publisher
    plan_artifact = state.artifacts.get("career_plan.json")
    approved_artifact = state.artifacts.get("career_approved.json")
    if publisher is None or plan_artifact is None or approved_artifact is None:
        return state
    store = ArtifactStore(ctx.runs_dir, state.project_slug)
    plan = CareerPlan.model_validate_json(store.read(plan_artifact))
    approved = set(json.loads(store.read(approved_artifact))["approved_items"])
    results = publisher.publish(slug=state.project_slug, plan=plan, approved=approved)
    _trace(ctx, state, EventType.TRANSITION, details={"career_published": results})
    if not results:
        return state
    artifact = store.write("career_published.json", json.dumps(results, indent=2), revision=1)
    return approve_artifacts(
        record_artifacts(state, {"career_published.json": artifact}),
        ["career_published.json"],
        now=datetime.now(UTC),
    )
