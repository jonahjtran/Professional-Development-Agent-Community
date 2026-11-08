"""Asking the Architect to prioritize a task. The Architect decides; code checks its answer."""

from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.state import ProjectState, Status
from orchestrator.task_control import (
    TaskControlError,
    apply_order,
    pending_priorities,
    priority_log,
    request_priority,
    resolve_priorities,
)
from schemas import Task, TaskList, TaskOrder
from tests.test_schemas import task_data
from tests.test_task_loop import at_developer


def three_tasks(tmp_path: Path, **fields: Any) -> ProjectState:
    """T1, then T2 (needs T1), T3 (needs nothing): all unfinished."""
    from datetime import UTC, datetime

    from orchestrator.artifacts import approve_artifacts, record_artifacts

    state = at_developer(tmp_path, **fields)
    store = ArtifactStore(tmp_path, "demo")
    listing = TaskList.model_validate(
        {
            "tasks": [
                task_data(id="T1", title="Create the table"),
                task_data(id="T2", title="Add the API", depends_on=["T1"]),
                task_data(id="T3", title="Add dark mode"),
            ]
        }
    )
    record = state.artifacts["tasks.json"]
    written = {
        "tasks.json": store.write(
            "tasks.json", listing.model_dump_json(), revision=record.revision + 1
        )
    }
    state = approve_artifacts(
        record_artifacts(state, written), ["tasks.json"], now=datetime(2026, 11, 7, tzinfo=UTC)
    )
    return state


def order_of(tmp_path: Path, state: ProjectState) -> list[str]:
    store = ArtifactStore(tmp_path, "demo")
    tasks: list[Task] = TaskList.model_validate_json(
        store.read(state.artifacts["tasks.json"])
    ).tasks
    return [t.id for t in tasks]


# --- asking -----------------------------------------------------------------------------------


def test_asking_records_the_request_with_your_reason(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.HALTED)

    request_priority(tmp_path, state, "T3", "The demo is Friday")

    [entry] = pending_priorities(tmp_path, "demo")
    assert entry["task_id"] == "T3" and entry["reason"] == "The demo is Friday"
    assert entry["status"] == "asked"


def test_asking_twice_for_the_same_task_keeps_one_request(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.HALTED)

    request_priority(tmp_path, state, "T3", "first")
    request_priority(tmp_path, state, "T3", "second")

    assert [e["reason"] for e in pending_priorities(tmp_path, "demo")] == ["second"]


@pytest.mark.parametrize(
    ("task_id", "fields", "message"),
    [
        ("T9", {}, "no task"),
        ("T1", {"completed_tasks": ["T1"]}, "finished"),
        ("T1", {"status": Status.RUNNING, "current_task_id": "T1"}, "already being built"),
    ],
)
def test_a_task_that_cannot_be_prioritized_is_refused(
    tmp_path: Path, task_id: str, fields: dict[str, Any], message: str
) -> None:
    state = three_tasks(tmp_path, **{"status": Status.HALTED, **fields})

    with pytest.raises(TaskControlError, match=message):
        request_priority(tmp_path, state, task_id, "now")


def test_the_reason_is_optional_and_bounded(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.HALTED)

    request_priority(tmp_path, state, "T3", "")
    assert pending_priorities(tmp_path, "demo")[0]["reason"] == ""

    with pytest.raises(TaskControlError, match="too long"):
        request_priority(tmp_path, state, "T3", "x" * 501)


# --- applying the Architect's order ------------------------------------------------------------


def test_the_architects_order_replaces_the_order_of_the_unfinished_tasks(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.RUNNING)

    state = apply_order(tmp_path, state, TaskOrder(order=["T3", "T1", "T2"], reason="demo"))

    assert order_of(tmp_path, state) == ["T3", "T1", "T2"]
    assert state.artifacts["tasks.json"].approved_at is not None


def test_finished_tasks_stay_first_and_in_their_order(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.RUNNING, completed_tasks=["T1"])

    state = apply_order(tmp_path, state, TaskOrder(order=["T3", "T2"], reason="demo"))

    assert order_of(tmp_path, state) == ["T1", "T3", "T2"]


@pytest.mark.parametrize(
    ("order", "message"),
    [
        (["T1", "T2"], "every unfinished task"),  # T3 missing
        (["T1", "T2", "T3", "T3"], "every unfinished task"),  # duplicate
        (["T1", "T2", "T9"], "every unfinished task"),  # unknown
        (["T2", "T1", "T3"], "T2 before T1"),  # breaks a dependency
    ],
)
def test_an_order_that_is_not_valid_is_refused_and_changes_nothing(
    tmp_path: Path, order: list[str], message: str
) -> None:
    state = three_tasks(tmp_path, status=Status.RUNNING)

    with pytest.raises(TaskControlError, match=message):
        apply_order(tmp_path, state, TaskOrder(order=order, reason="x"))

    assert order_of(tmp_path, state) == ["T1", "T2", "T3"]


# --- the log of what the Architect did ---------------------------------------------------------


def test_the_outcome_is_logged_against_the_request(tmp_path: Path) -> None:
    state = three_tasks(tmp_path, status=Status.HALTED)
    request_priority(tmp_path, state, "T3", "The demo is Friday")

    resolve_priorities(tmp_path, "demo", "moved", "It has no dependencies and the demo needs it.")

    assert pending_priorities(tmp_path, "demo") == []
    [entry] = priority_log(tmp_path, "demo")
    assert entry["status"] == "moved" and "demo needs it" in entry["architect_reason"]
    assert entry["task_id"] == "T3"
