"""The task list across projects, and cancelling ("killing") a task."""

from pathlib import Path

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.state import ProjectState, Status
from orchestrator.task_control import (
    TaskControlError,
    apply_pending_cancels,
    cancel_task,
    cancelled_tasks,
    pending_cancels,
    request_cancel,
    task_rows,
)
from schemas import TaskList
from tests.test_task_loop import at_developer


def ids(tmp_path: Path, state: ProjectState) -> list[str]:
    store = ArtifactStore(tmp_path, "demo")
    tasks = TaskList.model_validate_json(store.read(state.artifacts["tasks.json"])).tasks
    return [t.id for t in tasks]


def state_at(tmp_path: Path, **fields: object) -> ProjectState:
    return at_developer(tmp_path, **fields)


# --- the rows ---------------------------------------------------------------------------------


def test_each_unfinished_task_has_a_state_and_what_you_may_do_with_it(tmp_path: Path) -> None:
    state = state_at(tmp_path, status=Status.RUNNING, current_task_id="T1")

    rows = {r.id: r for r in task_rows(tmp_path, state)}

    assert rows["T1"].state == "working" and not rows["T1"].can_kill  # T2 needs it
    assert rows["T2"].state == "waiting" and rows["T2"].waiting_on == ["T1"]
    assert rows["T2"].can_kill  # nothing depends on it


def test_the_working_task_can_be_killed_when_nothing_depends_on_it(tmp_path: Path) -> None:
    state = state_at(tmp_path, status=Status.RUNNING, current_task_id="T2", completed_tasks=["T1"])

    rows = {r.id: r for r in task_rows(tmp_path, state)}

    assert rows["T2"].state == "working" and rows["T2"].can_kill


def test_a_finished_task_is_listed_as_done_and_cannot_be_killed(tmp_path: Path) -> None:
    state = state_at(tmp_path, status=Status.RUNNING, completed_tasks=["T1"])

    rows = {r.id: r for r in task_rows(tmp_path, state)}

    assert rows["T1"].state == "done" and not rows["T1"].can_kill
    assert rows["T2"].state == "ready"


def test_a_task_awaiting_your_review_is_in_review_and_not_killable(tmp_path: Path) -> None:
    from orchestrator.artifacts import record_artifacts
    from orchestrator.state import Intervention, InterventionKind

    state = state_at(
        tmp_path,
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail="Review T1"),
        current_task_id="T1",
    )

    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(state, {"dev/T1.json": store.write("dev/T1.json", "{}", revision=1)})

    rows = {r.id: r for r in task_rows(tmp_path, state)}

    assert rows["T1"].state == "in_review" and not rows["T1"].can_kill
    assert "review" in (rows["T1"].kill_blocked or "").lower()


def test_a_task_others_depend_on_cannot_be_killed_and_says_which(tmp_path: Path) -> None:
    rows = {r.id: r for r in task_rows(tmp_path, state_at(tmp_path))}

    assert not rows["T1"].can_kill
    assert "T2" in (rows["T1"].kill_blocked or "")


# --- killing a task ---------------------------------------------------------------------------


def test_a_task_nothing_depends_on_is_removed_from_the_list(tmp_path: Path) -> None:
    state = cancel_task(tmp_path, state_at(tmp_path, status=Status.HALTED), "T2", repo=None)

    assert ids(tmp_path, state) == ["T1"]
    assert state.artifacts["tasks.json"].approved_at is not None


def test_the_working_task_is_cancelled_and_the_next_one_is_picked_afresh(tmp_path: Path) -> None:
    state = state_at(tmp_path, status=Status.HALTED, current_task_id="T2")

    state = cancel_task(tmp_path, state, "T2", repo=None)

    assert state.current_task_id is None and ids(tmp_path, state) == ["T1"]


def test_a_task_others_depend_on_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TaskControlError, match="T2"):
        cancel_task(tmp_path, state_at(tmp_path, status=Status.HALTED), "T1", repo=None)


@pytest.mark.parametrize("task_id", ["T99", ""])
def test_an_unknown_task_is_refused(tmp_path: Path, task_id: str) -> None:
    with pytest.raises(TaskControlError, match="no task"):
        cancel_task(tmp_path, state_at(tmp_path, status=Status.HALTED), task_id, repo=None)


def test_a_finished_task_cannot_be_cancelled(tmp_path: Path) -> None:
    state = state_at(tmp_path, status=Status.HALTED, completed_tasks=["T1", "T2"])

    with pytest.raises(TaskControlError, match="finished"):
        cancel_task(tmp_path, state, "T2", repo=None)


def test_a_project_still_running_must_be_paused_first(tmp_path: Path) -> None:
    with pytest.raises(TaskControlError, match="Pause"):
        cancel_task(tmp_path, state_at(tmp_path, status=Status.RUNNING), "T2", repo=None)


def test_a_cancelled_task_is_remembered_with_when_and_why(tmp_path: Path) -> None:
    cancel_task(tmp_path, state_at(tmp_path, status=Status.HALTED), "T2", repo=None)

    [kept] = cancelled_tasks(tmp_path, "demo")
    assert kept["id"] == "T2" and kept["title"] == "Add the API" and kept["ts"]


# --- a kill asked of a run that is still going -------------------------------------------------


def test_a_kill_asked_while_running_waits_until_the_run_stops(tmp_path: Path) -> None:
    request_cancel(tmp_path, "demo", "T2")
    request_cancel(tmp_path, "demo", "T2")  # asking twice is the same as once

    assert pending_cancels(tmp_path, "demo") == ["T2"]


def test_pending_kills_are_applied_once_the_project_is_paused(tmp_path: Path) -> None:
    request_cancel(tmp_path, "demo", "T2")
    state = state_at(tmp_path, status=Status.HALTED)

    state = apply_pending_cancels(tmp_path, state, repo=None)

    assert ids(tmp_path, state) == ["T1"]
    assert pending_cancels(tmp_path, "demo") == []


def test_a_pending_kill_that_can_no_longer_apply_is_dropped_not_retried(tmp_path: Path) -> None:
    request_cancel(tmp_path, "demo", "T1")  # T2 depends on it, so it cannot be cancelled
    state = state_at(tmp_path, status=Status.HALTED)

    state = apply_pending_cancels(tmp_path, state, repo=None)

    assert ids(tmp_path, state) == ["T1", "T2"] and pending_cancels(tmp_path, "demo") == []


# --- the run applies a queued kill when it stops ---------------------------------------------


def test_a_run_that_stops_applies_the_kill_that_was_queued(tmp_path: Path) -> None:
    from orchestrator.halt import request_halt
    from orchestrator.pipeline import advance
    from tests.test_pipeline import FakeGates
    from tests.test_task_loop import DevAgent, FakePublisher, make_ctx

    state = state_at(tmp_path, status=Status.RUNNING, current_task_id="T2", completed_tasks=["T1"])
    request_cancel(tmp_path, "demo", "T2")
    request_halt(tmp_path, "demo")
    ctx = make_ctx(tmp_path, DevAgent(), FakeGates(), FakePublisher())

    stopped = advance(state, ctx)

    assert stopped.status == Status.HALTED and stopped.current_task_id is None
    assert ids(tmp_path, stopped) == ["T1"] and pending_cancels(tmp_path, "demo") == []


def test_a_halt_with_nothing_queued_changes_no_task(tmp_path: Path) -> None:
    from orchestrator.halt import request_halt
    from orchestrator.pipeline import advance
    from tests.test_pipeline import FakeGates
    from tests.test_task_loop import DevAgent, FakePublisher, make_ctx

    state = state_at(tmp_path, status=Status.RUNNING, current_task_id="T2", completed_tasks=["T1"])
    request_halt(tmp_path, "demo")
    ctx = make_ctx(tmp_path, DevAgent(), FakeGates(), FakePublisher())

    stopped = advance(state, ctx)

    assert stopped.status == Status.HALTED and ids(tmp_path, stopped) == ["T1", "T2"]
