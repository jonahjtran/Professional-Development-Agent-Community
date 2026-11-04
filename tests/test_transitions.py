"""Every transition the orchestrator can make, as one table.

Each case starts the project in a known state, takes exactly one `advance` step with a scripted
human, and checks where it ends up. If a new transition is added to the pipeline, it should get a
row here.
"""

from pathlib import Path

import pytest

from orchestrator.caps import CapAction, CapResolution
from orchestrator.gates import GateDecision, GateOutcome
from orchestrator.pipeline import advance
from orchestrator.state import InterventionKind, ProjectState, Stage, Status
from tests.test_pipeline import (
    FailingAgent,
    FakeAgent,
    FakeCaps,
    FakeGates,
    changes,
    make_ctx,
    reject,
    start,
)

GATED_NEXT = [
    (Stage.PRODUCT_OWNER, Stage.ARCHITECT),
    (Stage.ARCHITECT, Stage.DEVELOPER),
    (Stage.QA, Stage.CAREER_COACH),
]
ALL_GATED = [*GATED_NEXT, (Stage.CAREER_COACH, None)]


def at_gate(stage: Stage, **fields: object) -> ProjectState:
    return start(
        stage=stage,
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.GATE, "detail": "Review"},
        **fields,
    )


def at_revision_cap(stage: Stage) -> ProjectState:
    return start(
        stage=stage,
        revisions={stage: 3},
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.REVISION_CAP, "detail": "used all 3 revisions"},
        gate_history=[
            {
                "stage": stage,
                "outcome": GateOutcome.REQUEST_CHANGES,
                "comments": "more",
                "ts": "2026-11-04T00:00:00Z",
            }
        ],
    )


# --- intake and ungated stages ---------------------------------------------------------------


def test_intake_moves_to_the_product_owner(tmp_path: Path) -> None:
    state = advance(start(), make_ctx(tmp_path))

    assert (state.stage, state.status) == (Stage.PRODUCT_OWNER, Status.RUNNING)


def test_the_developer_moves_straight_to_qa_with_no_gate(tmp_path: Path) -> None:
    state = advance(start(stage=Stage.DEVELOPER), make_ctx(tmp_path))

    assert (state.stage, state.status) == (Stage.QA, Status.RUNNING)


@pytest.mark.parametrize("stage", [Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.CAREER_COACH])
def test_a_gated_agent_stage_stops_at_its_gate_after_running(tmp_path: Path, stage: Stage) -> None:
    gates = FakeGates()

    state = advance(start(stage=stage), make_ctx(tmp_path, FakeAgent(), gates))

    # The scripted human approves, so the step ends on the next stage (or done).
    assert len(gates.requests) == 1
    assert state.gate_history[-1].stage == stage


# --- the QA loop -----------------------------------------------------------------------------


def test_qa_failure_returns_to_the_developer_and_counts_the_loop(tmp_path: Path) -> None:
    state = advance(start(stage=Stage.QA), make_ctx(tmp_path, FakeAgent(qa_results=[False])))

    assert (state.stage, state.status, state.qa_loops) == (Stage.DEVELOPER, Status.RUNNING, 1)


def test_qa_failure_at_the_loop_limit_goes_to_the_human(tmp_path: Path) -> None:
    gates = FakeGates()
    ctx = make_ctx(tmp_path, FakeAgent(qa_results=[False]), gates)

    advance(start(stage=Stage.QA, qa_loops=2), ctx)

    assert gates.requests[0].stage == Stage.QA


def test_qa_pass_goes_to_the_gate_and_approval_resets_the_loop_count(tmp_path: Path) -> None:
    state = advance(start(stage=Stage.QA, qa_loops=1), make_ctx(tmp_path))

    assert (state.stage, state.qa_loops) == (Stage.CAREER_COACH, 0)


# --- gate outcomes at every gate -------------------------------------------------------------


@pytest.mark.parametrize(("stage", "next_stage"), GATED_NEXT)
def test_approve_moves_to_the_next_stage(tmp_path: Path, stage: Stage, next_stage: Stage) -> None:
    state = advance(at_gate(stage), make_ctx(tmp_path))

    assert (state.stage, state.status) == (next_stage, Status.RUNNING)


def test_approving_the_last_gate_finishes_the_project(tmp_path: Path) -> None:
    state = advance(at_gate(Stage.CAREER_COACH), make_ctx(tmp_path))

    assert state.status == Status.DONE


@pytest.mark.parametrize(("stage", "_next"), ALL_GATED)
def test_request_changes_stays_on_the_stage_and_counts_a_revision(
    tmp_path: Path, stage: Stage, _next: Stage | None
) -> None:
    state = advance(at_gate(stage), make_ctx(tmp_path, gates=FakeGates(changes())))

    assert (state.stage, state.status) == (stage, Status.RUNNING)
    assert state.revisions == {stage: 1}


@pytest.mark.parametrize(("stage", "_next"), ALL_GATED)
def test_reject_restarts_the_stage(tmp_path: Path, stage: Stage, _next: Stage | None) -> None:
    state = advance(at_gate(stage), make_ctx(tmp_path, gates=FakeGates(reject())))

    assert (state.stage, state.status) == (stage, Status.RUNNING)
    assert state.revisions == {}


@pytest.mark.parametrize(("stage", "_next"), ALL_GATED)
def test_reject_with_halt_stops_the_project(
    tmp_path: Path, stage: Stage, _next: Stage | None
) -> None:
    state = advance(at_gate(stage), make_ctx(tmp_path, gates=FakeGates(reject(halt=True))))

    assert state.status == Status.HALTED


# --- revision cap at every gate --------------------------------------------------------------


@pytest.mark.parametrize(("stage", "next_stage"), GATED_NEXT)
def test_revision_cap_approve_anyway_moves_on(
    tmp_path: Path, stage: Stage, next_stage: Stage
) -> None:
    caps = FakeCaps(CapResolution(action=CapAction.APPROVE_ANYWAY))

    state = advance(at_revision_cap(stage), make_ctx(tmp_path, caps=caps))

    assert (state.stage, state.status) == (next_stage, Status.RUNNING)


@pytest.mark.parametrize(("stage", "_next"), ALL_GATED)
def test_revision_cap_raise_runs_one_more_revision(
    tmp_path: Path, stage: Stage, _next: Stage | None
) -> None:
    caps = FakeCaps(CapResolution(action=CapAction.RAISE, amount=2))

    state = advance(at_revision_cap(stage), make_ctx(tmp_path, caps=caps))

    assert (state.stage, state.status) == (stage, Status.RUNNING)
    assert state.revisions[stage] == 4
    assert state.allowances.extra_revisions == {stage: 2}


@pytest.mark.parametrize(("stage", "_next"), ALL_GATED)
def test_revision_cap_abandon_halts(tmp_path: Path, stage: Stage, _next: Stage | None) -> None:
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))

    state = advance(at_revision_cap(stage), make_ctx(tmp_path, caps=caps))

    assert state.status == Status.HALTED


# --- budget at every agent stage -------------------------------------------------------------

AGENT_STAGES = [
    Stage.PRODUCT_OWNER,
    Stage.ARCHITECT,
    Stage.DEVELOPER,
    Stage.QA,
    Stage.CAREER_COACH,
]
STAGE_CAPS = {
    "product_owner": 1.5,
    "architect": 2.5,
    "developer": 10.0,
    "qa": 2.5,
    "career_coach": 2.0,
}


@pytest.mark.parametrize("stage", AGENT_STAGES)
def test_a_reached_stage_cap_blocks_the_call_and_abandon_halts(
    tmp_path: Path, stage: Stage
) -> None:
    agent = FakeAgent()
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    spent = {stage: STAGE_CAPS[stage.value]}

    state = advance(start(stage=stage, cost_usd=spent), make_ctx(tmp_path, agent, caps=caps))

    assert state.status == Status.HALTED
    assert agent.requests == []


@pytest.mark.parametrize("stage", AGENT_STAGES)
def test_raising_a_reached_stage_cap_lets_the_call_run(tmp_path: Path, stage: Stage) -> None:
    agent = FakeAgent(cost=0.0)
    # Big enough that spend is also back under the 80% warning line of the new cap.
    caps = FakeCaps(CapResolution(action=CapAction.RAISE, amount=10.0))
    spent = {stage: STAGE_CAPS[stage.value]}

    advance(start(stage=stage, cost_usd=spent), make_ctx(tmp_path, agent, caps=caps))

    assert agent.stages == [stage]


@pytest.mark.parametrize("stage", AGENT_STAGES)
def test_continuing_at_a_stage_warning_lets_the_call_run(tmp_path: Path, stage: Stage) -> None:
    agent = FakeAgent(cost=0.0)
    caps = FakeCaps(CapResolution(action=CapAction.APPROVE_ANYWAY))
    spent = {stage: STAGE_CAPS[stage.value] * 0.85}

    advance(start(stage=stage, cost_usd=spent), make_ctx(tmp_path, agent, caps=caps))

    assert agent.stages == [stage]
    assert len(caps.requests) == 1


def test_a_reached_project_cap_blocks_every_stage(tmp_path: Path) -> None:
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    state = start(stage=Stage.ARCHITECT, cost_usd={Stage.DEVELOPER: 20.0})

    result = advance(state, make_ctx(tmp_path, caps=caps))

    assert result.status == Status.HALTED


# --- failure and terminal states -------------------------------------------------------------


@pytest.mark.parametrize("stage", AGENT_STAGES)
def test_an_agent_failure_halts_from_any_stage(tmp_path: Path, stage: Stage) -> None:
    state = advance(start(stage=stage), make_ctx(tmp_path, FailingAgent()))

    assert state.status == Status.HALTED


@pytest.mark.parametrize("status", [Status.DONE, Status.HALTED])
def test_finished_projects_cannot_advance(tmp_path: Path, status: Status) -> None:
    with pytest.raises(ValueError, match="finished"):
        advance(start(status=status), make_ctx(tmp_path))


def test_a_decision_type_the_table_does_not_know_is_not_silently_accepted() -> None:
    assert {o.value for o in GateOutcome} == {"approve", "request_changes", "reject"}
    assert GateDecision(outcome=GateOutcome.APPROVE).halt is False
