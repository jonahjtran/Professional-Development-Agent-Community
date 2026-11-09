"""The task list across projects, and cancelling ("killing") a task.

A project runs one agent at a time, so *pausing* a task is pausing its project (`halt.py`) and the
work is kept. *Killing* a task cancels it: it is taken out of the task list, and if it was the one
being built, its folder and branch are discarded when they hold no commits (a branch with commits
is never touched). The Architect owns the order and content of the plan, so nothing here lets a
person add, edit or reorder tasks. The goal above the tasks (a change you asked for, or a plan the
Architect is making) can be stopped too: `stop_goal` drops it and puts the project back.

A kill asked while the run is still going cannot be applied at once, because the run owns the
saved state. It is queued (`cancel_requests.json`) and the run applies it when it stops; a project
that is already paused is changed straight away.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.rework import put_aside_open_questions
from orchestrator.state import (
    GateOutcome,
    Handoff,
    ProjectState,
    Stage,
    StageVisit,
    Status,
    atomic_write,
)
from orchestrator.worktree import WorktreeError, discard_unused
from schemas import TaskList


class TaskControlError(ValueError):
    """The change cannot be made; the message says why, in words for the person."""


@dataclass(frozen=True)
class TaskRow:
    id: str
    title: str
    state: str  # working, in_review, ready, waiting, done
    waiting_on: list[str] = field(default_factory=list)
    pr_url: str | None = None
    can_kill: bool = False
    kill_blocked: str | None = None  # why it cannot be killed, when it cannot


def _task_list(runs_dir: Path, state: ProjectState) -> TaskList | None:
    record = state.artifacts.get("tasks.json")
    if record is None:
        return None
    try:
        return TaskList.model_validate_json(
            ArtifactStore(runs_dir, state.project_slug).read(record)
        )
    except (ValidationError, ValueError, OSError):
        return None


def _pr_urls(runs_dir: Path, state: ProjectState) -> dict[str, str]:
    store = ArtifactStore(runs_dir, state.project_slug)
    urls: dict[str, str] = {}
    for name, record in state.artifacts.items():
        if not name.startswith("dev/"):
            continue
        try:
            report = json.loads(store.read(record))
            if report.get("pr_url"):
                urls[str(report["task_id"])] = str(report["pr_url"])
        except (ValueError, KeyError, OSError, TypeError, AttributeError):
            continue
    return urls


def task_rows(runs_dir: Path, state: ProjectState) -> list[TaskRow]:
    """Every task in the project's list, in the order the Architect set, with what you may do."""
    listing = _task_list(runs_dir, state)
    if listing is None:
        return []
    done = set(state.completed_tasks)
    urls = _pr_urls(runs_dir, state)
    rows: list[TaskRow] = []
    for task in listing.tasks:
        unmet = [d for d in task.depends_on if d not in done]
        dependents = [t.id for t in listing.tasks if task.id in t.depends_on and t.id not in done]
        in_review = (
            task.id == state.current_task_id
            and state.status == Status.AWAITING_HUMAN
            and f"dev/{task.id}.json" in state.artifacts
        )
        if task.id in done:
            status = "done"
        elif in_review:
            status = "in_review"
        elif task.id == state.current_task_id:
            status = "working"
        elif unmet:
            status = "waiting"
        else:
            status = "ready"
        blocked = _kill_blocked(status, dependents)
        rows.append(
            TaskRow(
                id=task.id,
                title=task.title,
                state=status,
                waiting_on=unmet if status == "waiting" else [],
                pr_url=urls.get(task.id),
                can_kill=blocked is None,
                kill_blocked=blocked,
            )
        )
    return rows


def _kill_blocked(status: str, dependents: list[str]) -> str | None:
    if status == "done":
        return "it is finished"
    if status == "in_review":
        return "it is waiting for your review: reject it there instead"
    if dependents:
        verb = "depends" if len(dependents) == 1 else "depend"
        return f"{', '.join(dependents)} {verb} on it: cancel that first"
    return None


# --- cancelling a task -------------------------------------------------------------------------


def _cancelled_path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "cancelled_tasks.json"


def cancelled_tasks(runs_dir: Path, project: str) -> list[dict[str, str]]:
    """The tasks that were cancelled, newest last: id, title, when."""
    try:
        raw = json.loads(_cancelled_path(runs_dir, project).read_text())
    except (FileNotFoundError, ValueError):
        return []
    return [
        {k: str(v) for k, v in item.items()}
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict)
    ]


def cancel_task(
    runs_dir: Path,
    state: ProjectState,
    task_id: str,
    *,
    repo: Path | None,
    now: datetime | None = None,
) -> ProjectState:
    """Cancel a task: take it out of the task list and discard its unused work.

    The project must not be running (the run owns the saved state). Raises TaskControlError, with
    a reason, when the task is unknown, finished, in review, or needed by another task.
    """
    if state.status == Status.RUNNING:
        raise TaskControlError("Pause the project first, then cancel the task.")
    listing = _task_list(runs_dir, state)
    record = state.artifacts.get("tasks.json")
    if listing is None or record is None:
        raise TaskControlError("the project has no task list yet")
    row = next((r for r in task_rows(runs_dir, state) if r.id == task_id), None)
    if row is None:
        raise TaskControlError(f"there is no task {task_id or '(none)'} in this project")
    if not row.can_kill:
        raise TaskControlError(f"{task_id} cannot be cancelled: {row.kill_blocked}")
    when = now or datetime.now(UTC)
    store = ArtifactStore(runs_dir, state.project_slug)
    kept = [t.model_dump() for t in listing.tasks if t.id != task_id]
    written = {
        "tasks.json": store.write(
            "tasks.json",
            TaskList.model_validate({"tasks": kept}).model_dump_json(indent=2),
            revision=record.revision + 1,
        )
    }
    updated = approve_artifacts(record_artifacts(state, written), list(written), now=when)
    if updated.current_task_id == task_id:
        updated = updated.evolve(current_task_id=None)
    if repo is not None:
        try:
            discard_unused(repo, task_id)  # only a branch with no commits is ever removed
        except WorktreeError:
            pass  # the folder can be tidied later; the task is cancelled either way
    _remember(runs_dir, state.project_slug, task_id, row.title, when)
    return updated


def _remember(runs_dir: Path, project: str, task_id: str, title: str, when: datetime) -> None:
    items = cancelled_tasks(runs_dir, project)
    items.append({"id": task_id, "title": title, "ts": when.isoformat()})
    atomic_write(_cancelled_path(runs_dir, project), json.dumps(items, indent=2))


# --- a kill asked of a run that is still going --------------------------------------------------


def _requests_path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "cancel_requests.json"


def pending_cancels(runs_dir: Path, project: str) -> list[str]:
    try:
        raw = json.loads(_requests_path(runs_dir, project).read_text())
    except (FileNotFoundError, ValueError):
        return []
    return [str(i) for i in raw] if isinstance(raw, list) else []


def request_cancel(runs_dir: Path, project: str, task_id: str) -> None:
    """Queue a kill for when the run stops (asking twice is the same as asking once)."""
    queued = pending_cancels(runs_dir, project)
    if task_id not in queued:
        queued.append(task_id)
    path = _requests_path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(queued))


def apply_pending_cancels(
    runs_dir: Path, state: ProjectState, *, repo: Path | None
) -> ProjectState:
    """Carry out the queued kills. One that can no longer apply is dropped, never retried."""
    queued = pending_cancels(runs_dir, state.project_slug)
    if not queued:
        return state
    for task_id in queued:
        try:
            if task_id == GOAL:
                state = stop_goal(runs_dir, state)
            else:
                state = cancel_task(runs_dir, state, task_id, repo=repo)
        except TaskControlError:
            continue
    _requests_path(runs_dir, state.project_slug).unlink(missing_ok=True)
    return state


# --- stopping the current goal ------------------------------------------------------------------

GOAL = "@goal"  # in the queue of stops, "the current goal" instead of a task id

_PLANNING = {
    "human_design": "Plan the tasks for a change you asked for",
    "qa_fix": "Plan fixes for the failed QA report",
    "design_review": "Resolve a design problem the Developer raised",
}


def current_goal(state: ProjectState) -> str | None:
    """What the project is working on above the level of one task, or None.

    That is a change you asked for (from Change something, the chat or the coordinator) while the
    agent you chose is still working on it, or a plan the Architect is making after a failed QA or a
    design problem. Once the work is tasks, each task is stopped on its own.
    """
    if state.status == Status.DONE and state.rework is None:
        return None
    rework = state.rework
    if rework is not None and not rework.origin_done:
        return rework.note.strip().splitlines()[0][:120] if rework.note.strip() else "A change"
    if state.handoff is not None and state.handoff.purpose in _PLANNING:
        return _PLANNING[state.handoff.purpose]
    return None


def stop_goal(runs_dir: Path, state: ProjectState) -> ProjectState:
    """Drop the current goal and put the project back where it was before it began.

    The project is left paused (a finished project is finished again). Tasks the Architect already
    planned for the goal stay in the task list, and each can be stopped on its own.
    """
    goal = current_goal(state)
    if goal is None:
        raise TaskControlError("there is nothing to stop: no change or plan is being worked on")
    put_aside_open_questions(runs_dir, state.project_slug)
    when = datetime.now(UTC)
    rework = state.rework
    began = next(
        (v for v in reversed(state.path) if rework is not None and v.ts == rework.ts), None
    )
    if began is None and state.path and state.path[-1].stage == state.stage:
        began = state.path[-1]
    back = began.came_from if began is not None else None
    handoff = state.handoff
    changes: dict[str, object] = {
        "rework": None,
        "handoff": None,
        "intervention": None,
        "current_task_id": None,
        "status": Status.HALTED,
    }
    if back is not None:
        was_finished = (
            back == Stage.CAREER_COACH
            and bool(state.gate_history)
            and state.gate_history[-1].stage == Stage.CAREER_COACH
            and state.gate_history[-1].outcome == GateOutcome.APPROVE
        )
        changes["stage"] = back
        if was_finished:
            changes["status"] = Status.DONE
        if handoff is not None and handoff.purpose == "qa_fix" and back == Stage.QA:
            changes["handoff"] = Handoff(
                purpose="qa_gate", note="You stopped the fix plan, so the failed report is yours."
            )
        visit = StageVisit(
            stage=back, reason=f"you stopped: {goal}"[:200], ts=when, came_from=state.stage
        )
        changes["path"] = [*state.path, visit]
    return state.evolve(**changes)


def request_stop_goal(runs_dir: Path, project: str) -> None:
    """Queue stopping the goal for when the live run stops."""
    request_cancel(runs_dir, project, GOAL)
