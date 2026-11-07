"""The pipeline records every move between agents, forward and back, with the reason."""

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.journey import direction
from orchestrator.pipeline import _goto, run_pipeline
from orchestrator.state import Stage, Status
from orchestrator.trace import trace_path
from tests.test_pipeline import FakeAgent, make_ctx, start


def transitions(tmp_path: Path) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in trace_path(tmp_path, "demo").read_text().splitlines()]
    return [
        e["details"]
        for e in events
        if e["event"] == "transition" and "direction" in (e["details"] or {})
    ]


def test_a_straight_run_records_each_agent_it_moved_to_with_a_reason(tmp_path: Path) -> None:
    state = run_pipeline(start(), make_ctx(tmp_path))

    assert state.status == Status.DONE
    assert [v.stage for v in state.path] == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.CAREER_COACH,
    ]
    assert all(v.reason for v in state.path)
    assert (
        state.path[0].came_from == Stage.INTAKE and state.path[1].came_from == Stage.PRODUCT_OWNER
    )


def test_each_move_is_traced_with_its_direction_and_reason(tmp_path: Path) -> None:
    run_pipeline(start(), make_ctx(tmp_path))

    moves = transitions(tmp_path)

    assert [m["to"] for m in moves][:2] == ["product_owner", "architect"]
    assert all(m["direction"] == "forward" and m["reason"] for m in moves)


def test_a_qa_that_sends_the_work_back_is_a_recorded_move_back(tmp_path: Path) -> None:
    agent = FakeAgent(qa_results=[False, True])

    state = run_pipeline(start(), make_ctx(tmp_path, agent))

    stages = [v.stage for v in state.path]
    assert stages == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.DEVELOPER,  # back
        Stage.QA,
        Stage.CAREER_COACH,
    ]
    back = state.path[4]
    assert back.came_from == Stage.QA and "QA" in back.reason
    assert [m["direction"] for m in transitions(tmp_path)].count("back") == 1


def test_an_illegal_move_is_refused_before_anything_changes(tmp_path: Path) -> None:
    state = start(stage=Stage.PRODUCT_OWNER)

    with pytest.raises(ValueError, match="cannot move"):
        _goto(state, make_ctx(tmp_path), Stage.DEVELOPER, "skipping the Architect")

    assert direction(Stage.QA, Stage.ARCHITECT) == "back"


def test_a_legal_move_back_is_recorded(tmp_path: Path) -> None:
    state = start(stage=Stage.QA)

    moved = _goto(state, make_ctx(tmp_path), Stage.ARCHITECT, "the design needs another look")

    assert moved.stage == Stage.ARCHITECT
    assert moved.path[-1].came_from == Stage.QA
    assert moved.path[-1].reason == "the design needs another look"
