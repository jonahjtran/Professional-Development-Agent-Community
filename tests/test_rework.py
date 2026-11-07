"""Sending a project back to an agent yourself, from paused or finished."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.rework import REWORKABLE, ReworkError, send_back
from orchestrator.state import Handoff, ProjectState, Stage, StageVisit, Status
from schemas import TaskList
from tests.factories import new_state
from tests.test_task_loop import at_developer

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def finished(tmp_path: Path) -> ProjectState:
    """A completed project with its tasks and architecture on record."""
    return at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH,
        status=Status.DONE,
        completed_tasks=["T1", "T2"],
        path=[StageVisit(stage=Stage.CAREER_COACH, reason="QA passed", ts=NOW, came_from=Stage.QA)],
    )


def paused(tmp_path: Path, stage: Stage = Stage.DEVELOPER) -> ProjectState:
    return at_developer(tmp_path).evolve(stage=stage, status=Status.HALTED)


# --- from where, and to where -----------------------------------------------------------------


def test_a_finished_project_can_be_sent_back_and_is_running_again(tmp_path: Path) -> None:
    state = send_back(
        tmp_path, finished(tmp_path), Stage.PRODUCT_OWNER, "The PRD missed offline mode", now=NOW
    )

    assert state.status == Status.RUNNING and state.stage == Stage.PRODUCT_OWNER
    assert state.intervention is None and state.current_task_id is None
    visit = state.path[-1]
    assert visit.stage == Stage.PRODUCT_OWNER and visit.came_from == Stage.CAREER_COACH
    assert "you sent it back" in visit.reason and "offline mode" in visit.reason


def test_a_paused_project_can_be_sent_back(tmp_path: Path) -> None:
    state = send_back(tmp_path, paused(tmp_path), Stage.ARCHITECT, "Rethink the schema", now=NOW)

    assert state.status == Status.RUNNING and state.stage == Stage.ARCHITECT


@pytest.mark.parametrize("status", [Status.RUNNING, Status.AWAITING_HUMAN])
def test_a_project_that_is_running_must_be_paused_first(tmp_path: Path, status: Status) -> None:
    fields = {"status": status}
    if status == Status.AWAITING_HUMAN:
        fields["intervention"] = {"kind": "gate", "detail": "Review"}  # type: ignore[assignment]
    state = at_developer(tmp_path).evolve(**fields)

    with pytest.raises(ReworkError, match="[Pp]ause"):
        send_back(tmp_path, state, Stage.ARCHITECT, "Change it", now=NOW)


@pytest.mark.parametrize("target", list(REWORKABLE))
def test_every_agent_is_a_valid_place_to_send_a_finished_project(
    tmp_path: Path, target: Stage
) -> None:
    state = send_back(tmp_path, finished(tmp_path), target, "Change it", now=NOW)

    assert state.stage == target and state.status == Status.RUNNING


def test_a_project_cannot_be_sent_forward_past_where_it_has_got_to(tmp_path: Path) -> None:
    with pytest.raises(ReworkError, match="not reached"):
        send_back(tmp_path, paused(tmp_path, Stage.ARCHITECT), Stage.QA, "Skip ahead", now=NOW)


def test_the_current_agent_can_be_asked_to_do_it_again(tmp_path: Path) -> None:
    state = send_back(tmp_path, paused(tmp_path, Stage.QA), Stage.QA, "Run it again", now=NOW)

    assert state.stage == Stage.QA and state.path[-1].stage == Stage.QA


def test_intake_is_not_a_place_to_go_back_to(tmp_path: Path) -> None:
    with pytest.raises(ReworkError):
        send_back(tmp_path, finished(tmp_path), Stage.INTAKE, "Start over", now=NOW)


@pytest.mark.parametrize("comment", ["", "   ", "x" * 2001])
def test_the_instruction_is_required_and_bounded(tmp_path: Path, comment: str) -> None:
    with pytest.raises(ReworkError, match="1 to 2000"):
        send_back(tmp_path, finished(tmp_path), Stage.ARCHITECT, comment, now=NOW)


# --- what it records --------------------------------------------------------------------------


def test_the_instruction_is_kept_as_the_rework(tmp_path: Path) -> None:
    state = send_back(
        tmp_path, finished(tmp_path), Stage.PRODUCT_OWNER, "Add offline mode", now=NOW
    )

    assert state.rework is not None
    assert state.rework.origin == Stage.PRODUCT_OWNER and state.rework.note == "Add offline mode"
    assert state.rework.origin_done is False


def test_the_loop_counters_start_afresh(tmp_path: Path) -> None:
    state = finished(tmp_path).evolve(qa_loops=2)

    after = send_back(tmp_path, state, Stage.QA, "Check again", now=NOW)

    assert after.qa_loops == 0


def test_questions_left_open_by_the_old_run_are_put_aside_not_answered(tmp_path: Path) -> None:
    pending = tmp_path / "demo" / "pending"
    pending.mkdir(parents=True)
    (pending / "abc.json").write_text(json.dumps({"id": "abc"}))

    send_back(tmp_path, paused(tmp_path), Stage.ARCHITECT, "Rethink it", now=NOW)

    assert list(pending.glob("*.json")) == []
    assert (tmp_path / "demo" / "stale" / "abc.json").exists()


# --- what each agent is asked to do -----------------------------------------------------------


def test_the_architect_is_visited_with_your_instruction_as_the_problem(tmp_path: Path) -> None:
    state = send_back(
        tmp_path, finished(tmp_path), Stage.ARCHITECT, "Store events in Postgres", now=NOW
    )

    assert state.handoff == Handoff(
        purpose="human_design", note=state.handoff.note if state.handoff else ""
    )
    assert state.handoff is not None and "Store events in Postgres" in state.handoff.note


def test_the_developer_gets_your_instruction_as_a_new_task(tmp_path: Path) -> None:
    state = send_back(
        tmp_path, finished(tmp_path), Stage.DEVELOPER, "Add a dark mode toggle", now=NOW
    )

    store = ArtifactStore(tmp_path, "demo")
    tasks = TaskList.model_validate_json(store.read(state.artifacts["tasks.json"])).tasks
    assert [t.id for t in tasks] == ["T1", "T2", "H1-1"]
    added = tasks[-1]
    assert "Add a dark mode toggle" in added.description and added.acceptance_criteria
    assert state.artifacts["tasks.json"].approved_at is not None
    assert state.handoff is None


def test_a_second_instruction_to_the_developer_gets_its_own_task_id(tmp_path: Path) -> None:
    first = send_back(tmp_path, finished(tmp_path), Stage.DEVELOPER, "Add dark mode", now=NOW)
    again = first.evolve(status=Status.DONE, stage=Stage.CAREER_COACH)

    second = send_back(tmp_path, again, Stage.DEVELOPER, "Add a footer", now=NOW)

    store = ArtifactStore(tmp_path, "demo")
    ids = [
        t.id for t in TaskList.model_validate_json(store.read(second.artifacts["tasks.json"])).tasks
    ]
    assert ids == ["T1", "T2", "H1-1", "H2-1"]


def test_the_title_of_the_new_task_is_the_first_line_of_the_instruction(tmp_path: Path) -> None:
    state = send_back(
        tmp_path,
        finished(tmp_path),
        Stage.DEVELOPER,
        "Add a dark mode toggle\nIt should persist.",
        now=NOW,
    )

    store = ArtifactStore(tmp_path, "demo")
    task = TaskList.model_validate_json(store.read(state.artifacts["tasks.json"])).tasks[-1]
    assert task.title == "Add a dark mode toggle" and "persist" in task.description


def test_a_project_with_no_task_list_cannot_be_sent_to_the_developer(tmp_path: Path) -> None:
    state = new_state().evolve(stage=Stage.QA, status=Status.HALTED)

    with pytest.raises(ReworkError, match="task list"):
        send_back(tmp_path, state, Stage.DEVELOPER, "Add it", now=NOW)


def test_a_valid_task_list_still_validates_after_many_sends(tmp_path: Path) -> None:
    state = finished(tmp_path)
    for n in range(3):
        state = send_back(tmp_path, state, Stage.DEVELOPER, f"Change {n}", now=NOW).evolve(
            status=Status.DONE, stage=Stage.CAREER_COACH
        )

    store = ArtifactStore(tmp_path, "demo")
    assert len(TaskList.model_validate_json(store.read(state.artifacts["tasks.json"])).tasks) == 5
