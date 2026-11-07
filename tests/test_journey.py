"""The project journey: the orchestrator may move back as well as forward, and records each move."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.journey import (
    can_move,
    direction,
    going_back,
    path_summary,
    visit_counts,
)
from orchestrator.state import ProjectState, Stage, StageVisit, load_state, save_state
from tests.factories import new_state

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def visit(stage: Stage, reason: str = "next", came_from: Stage | None = None) -> StageVisit:
    return StageVisit(stage=stage, reason=reason, ts=NOW, came_from=came_from)


# --- which moves are allowed ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (Stage.INTAKE, Stage.PRODUCT_OWNER),
        (Stage.PRODUCT_OWNER, Stage.ARCHITECT),
        (Stage.ARCHITECT, Stage.DEVELOPER),
        (Stage.DEVELOPER, Stage.QA),
        (Stage.QA, Stage.CAREER_COACH),
        (Stage.QA, Stage.DEVELOPER),  # back
        (Stage.QA, Stage.ARCHITECT),  # back, past one agent
        (Stage.DEVELOPER, Stage.ARCHITECT),
        (Stage.ARCHITECT, Stage.PRODUCT_OWNER),
        (Stage.CAREER_COACH, Stage.QA),
        (Stage.CAREER_COACH, Stage.PRODUCT_OWNER),
    ],
)
def test_the_orchestrator_may_step_forward_or_go_back_to_any_earlier_agent(
    src: Stage, dst: Stage
) -> None:
    assert can_move(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (Stage.PRODUCT_OWNER, Stage.DEVELOPER),  # skipping an agent forward
        (Stage.ARCHITECT, Stage.CAREER_COACH),
        (Stage.QA, Stage.QA),  # staying put is not a move
        (Stage.DEVELOPER, Stage.INTAKE),  # intake happens once
        (Stage.QA, Stage.INTAKE),
    ],
)
def test_it_may_not_skip_an_agent_going_forward_or_return_to_intake(src: Stage, dst: Stage) -> None:
    assert not can_move(src, dst)


def test_a_move_is_forward_or_back() -> None:
    assert direction(Stage.ARCHITECT, Stage.DEVELOPER) == "forward"
    assert direction(Stage.QA, Stage.ARCHITECT) == "back"


# --- reading the journey ----------------------------------------------------------------------


def test_a_new_project_has_no_journey() -> None:
    state = new_state()

    assert state.path == [] and visit_counts(state) == {}


def test_visits_are_counted_per_agent_including_returns() -> None:
    state = new_state().evolve(
        stage=Stage.QA,
        path=[
            visit(Stage.PRODUCT_OWNER),
            visit(Stage.ARCHITECT),
            visit(Stage.DEVELOPER),
            visit(Stage.QA),
            visit(Stage.ARCHITECT, "QA failed", Stage.QA),
            visit(Stage.DEVELOPER, "fix round 1", Stage.ARCHITECT),
            visit(Stage.QA),
        ],
    )

    counts = visit_counts(state)

    assert counts[Stage.ARCHITECT] == 2 and counts[Stage.DEVELOPER] == 2 and counts[Stage.QA] == 2
    assert counts[Stage.PRODUCT_OWNER] == 1


def test_a_project_saved_before_the_journey_existed_counts_the_agents_it_has_passed() -> None:
    state = new_state().evolve(stage=Stage.DEVELOPER)  # no path recorded

    counts = visit_counts(state)

    assert counts == {Stage.PRODUCT_OWNER: 1, Stage.ARCHITECT: 1, Stage.DEVELOPER: 1}


def test_going_back_is_reported_only_while_the_agent_we_went_back_to_is_current() -> None:
    state = new_state().evolve(
        stage=Stage.ARCHITECT,
        path=[
            visit(Stage.PRODUCT_OWNER),
            visit(Stage.ARCHITECT),
            visit(Stage.DEVELOPER),
            visit(Stage.QA),
            visit(Stage.ARCHITECT, "QA failed", Stage.QA),
        ],
    )

    back = going_back(state)

    assert back is not None and back.came_from == Stage.QA and back.reason == "QA failed"
    moved_on = state.evolve(stage=Stage.DEVELOPER, path=[*state.path, visit(Stage.DEVELOPER)])
    assert going_back(moved_on) is None


def test_a_forward_only_journey_is_never_going_back() -> None:
    state = new_state().evolve(
        stage=Stage.ARCHITECT, path=[visit(Stage.PRODUCT_OWNER), visit(Stage.ARCHITECT)]
    )

    assert going_back(state) is None


def test_the_path_reads_as_a_line_with_returns_marked() -> None:
    state = new_state().evolve(
        stage=Stage.DEVELOPER,
        path=[
            visit(Stage.PRODUCT_OWNER),
            visit(Stage.ARCHITECT),
            visit(Stage.DEVELOPER),
            visit(Stage.QA),
            visit(Stage.ARCHITECT, "QA failed", Stage.QA),
            visit(Stage.DEVELOPER, "fix round 1", Stage.ARCHITECT),
        ],
    )

    assert path_summary(state) == [
        "Product Owner",
        "Architect",
        "Developer",
        "QA",
        "↩ Architect",
        "Developer",
    ]


# --- saved with the project ------------------------------------------------------------------


def test_the_journey_is_saved_and_loaded_with_the_project(tmp_path: Path) -> None:
    state: ProjectState = new_state().evolve(
        stage=Stage.ARCHITECT,
        path=[visit(Stage.PRODUCT_OWNER), visit(Stage.ARCHITECT, "QA failed", Stage.QA)],
    )

    save_state(tmp_path, state)

    loaded = load_state(tmp_path, "demo")
    assert [v.stage for v in loaded.path] == [Stage.PRODUCT_OWNER, Stage.ARCHITECT]
    assert loaded.path[1].reason == "QA failed" and loaded.path[1].came_from == Stage.QA
