"""Sending a project back to an agent yourself, at any time it is paused or finished.

The orchestrator moves back on its own rules (a failed QA, a design problem), but you can also
steer it. `send_back` reopens the project at the agent you choose with your instruction. It works
whether the project is paused or already done, so a finished project can be changed.

What each agent does with the instruction:

- Product Owner / Career coach: revise their own document with your comment, then you review it
  as at any gate. After a changed PRD the Architect plans only the tasks the change needs.
- Architect: turns your instruction into new tasks (finished tasks stay finished).
- Developer: your instruction becomes a new task it builds directly.
- QA: runs again.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.journey import LABELS
from orchestrator.state import Handoff, ProjectState, Rework, Stage, StageVisit, Status
from schemas import Task, TaskList

REWORKABLE = (
    Stage.PRODUCT_OWNER,
    Stage.ARCHITECT,
    Stage.DEVELOPER,
    Stage.QA,
    Stage.CAREER_COACH,
)
_ORDER = (Stage.INTAKE, *REWORKABLE)
_MAX_NOTE = 2000


class ReworkError(ValueError):
    """The project cannot be sent there now; the message says why."""


def send_back(
    runs_dir: Path,
    state: ProjectState,
    to: Stage,
    comment: str,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> ProjectState:
    """The project reopened at `to` with your instruction. The caller saves it and resumes.

    With `dry_run` nothing is moved or written: it only checks that the send-back is allowed.
    """
    comment = comment.strip()
    if not 1 <= len(comment) <= _MAX_NOTE:
        raise ReworkError(f"say what to change, in 1 to {_MAX_NOTE} characters")
    if to not in REWORKABLE:
        raise ReworkError(f"a project cannot be sent back to {to.value}")
    if state.status not in (Status.HALTED, Status.DONE):
        raise ReworkError("Pause the project first (or wait until it is done), then send it back.")
    if _ORDER.index(to) > _ORDER.index(state.stage):
        raise ReworkError(
            f"the project has not reached the {LABELS[to]} yet, so it cannot be sent there"
        )
    when = now or datetime.now(UTC)
    if not dry_run:
        _put_aside_open_questions(runs_dir, state.project_slug)

    changes: dict[str, object] = {
        "status": Status.RUNNING,
        "intervention": None,
        "current_task_id": None,
        "qa_loops": 0,
        "handoff": None,
        "rework": Rework(origin=to, note=comment, ts=when),
    }
    if to == Stage.ARCHITECT:
        changes["handoff"] = Handoff(
            purpose="human_design", note=f"The person asked for this change:\n{comment}"
        )
    if to == Stage.DEVELOPER:
        changes["artifacts"] = _add_task(runs_dir, state, comment, write=not dry_run)
    visit = StageVisit(
        stage=to,
        reason=f"you sent it back: {comment.splitlines()[0][:80]}",
        ts=when,
        came_from=state.stage,
    )
    return state.evolve(**changes, stage=to, path=[*state.path, visit])


def _put_aside_open_questions(runs_dir: Path, slug: str) -> None:
    """Questions the old run left open are no longer the question; keep them, do not answer them."""
    pending = runs_dir / slug / "pending"
    if not pending.is_dir():
        return
    aside = runs_dir / slug / "stale"
    for path in pending.glob("*.json"):
        aside.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(aside / path.name))


def _add_task(
    runs_dir: Path, state: ProjectState, comment: str, *, write: bool = True
) -> dict[str, object]:
    """Add your instruction to the task list as a task for the Developer."""
    record = state.artifacts.get("tasks.json")
    if record is None:
        raise ReworkError("the project has no task list yet, so there is nothing for the Developer")
    store = ArtifactStore(runs_dir, state.project_slug)
    existing = TaskList.model_validate_json(store.read(record))
    number = 1 + sum(1 for t in existing.tasks if t.id.startswith("H"))
    first, _, rest = comment.partition("\n")
    task = Task(
        id=f"H{number}-1",
        title=first.strip()[:120],
        description=comment,
        acceptance_criteria=[
            "The change described is implemented, and a test proves it",
            "The existing tests still pass",
        ],
        depends_on=[],
        size="S",  # type: ignore[arg-type]
    )
    merged = TaskList.model_validate(
        {"tasks": [*[t.model_dump() for t in existing.tasks], task.model_dump()]}
    )
    if not write:
        return dict(state.artifacts)
    written = {
        "tasks.json": store.write(
            "tasks.json", merged.model_dump_json(indent=2), revision=record.revision + 1
        )
    }
    updated = approve_artifacts(
        record_artifacts(state, written), list(written), now=datetime.now(UTC)
    )
    return dict(updated.artifacts)
