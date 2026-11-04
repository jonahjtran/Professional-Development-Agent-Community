from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orchestrator.budget import BudgetScope, assess_budget, pause_for_budget
from orchestrator.caps import CapAction, CapResolution, resolve_cap
from orchestrator.gates import GateDecision, GateOutcome, apply_decision, open_gate
from orchestrator.state import InterventionKind, ProjectState, Stage, Status
from tests.factories import make_config, new_state

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def at_revision_cap(stage: Stage = Stage.PRODUCT_OWNER) -> ProjectState:
    state = open_gate(new_state().evolve(stage=stage, revisions={stage: 3}), title="Review")
    request = GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="One more pass")
    paused = apply_decision(state, make_config(), request, now=NOW)
    assert paused.intervention is not None
    assert paused.intervention.kind == InterventionKind.REVISION_CAP
    return paused


def at_budget_pause(stage: Stage = Stage.PRODUCT_OWNER, **spend: float) -> ProjectState:
    costs = {Stage(name): usd for name, usd in spend.items()} or {stage: 1.5}
    state = new_state().evolve(stage=stage, cost_usd=costs)
    return pause_for_budget(state, assess_budget(state, make_config(), stage))


def approve_anyway() -> CapResolution:
    return CapResolution(action=CapAction.APPROVE_ANYWAY)


def raise_by(amount: float) -> CapResolution:
    return CapResolution(action=CapAction.RAISE, amount=amount)


def abandon() -> CapResolution:
    return CapResolution(action=CapAction.ABANDON)


# --- the resolution itself -------------------------------------------------------------------


def test_raising_needs_a_positive_amount() -> None:
    with pytest.raises(ValidationError):
        CapResolution(action=CapAction.RAISE)
    with pytest.raises(ValidationError):
        CapResolution(action=CapAction.RAISE, amount=0)
    with pytest.raises(ValidationError):
        CapResolution(action=CapAction.RAISE, amount=-1)


def test_only_raising_takes_an_amount() -> None:
    with pytest.raises(ValidationError):
        CapResolution(action=CapAction.ABANDON, amount=2)


# --- guards ----------------------------------------------------------------------------------


def test_resolving_needs_a_cap_pause() -> None:
    with pytest.raises(ValueError, match="cap"):
        resolve_cap(new_state(), make_config(), abandon(), now=NOW)


def test_a_gate_pause_is_not_a_cap_pause() -> None:
    gate = open_gate(new_state().evolve(stage=Stage.ARCHITECT), title="Review")

    with pytest.raises(ValueError, match="cap"):
        resolve_cap(gate, make_config(), abandon(), now=NOW)


# --- revision cap ----------------------------------------------------------------------------


def test_approving_anyway_at_the_revision_cap_accepts_the_stage() -> None:
    state = resolve_cap(at_revision_cap(), make_config(), approve_anyway(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.gate_history[-1].outcome == GateOutcome.APPROVE
    assert state.gate_history[-1].stage == Stage.PRODUCT_OWNER
    assert state.revisions == {Stage.PRODUCT_OWNER: 3}


def test_raising_the_revision_cap_grants_rooms_and_runs_the_pending_revision() -> None:
    state = resolve_cap(at_revision_cap(), make_config(), raise_by(2), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.allowances.extra_revisions == {Stage.PRODUCT_OWNER: 2}
    assert state.revisions == {Stage.PRODUCT_OWNER: 4}


def test_raised_revisions_add_up_across_pauses() -> None:
    first = resolve_cap(at_revision_cap(), make_config(), raise_by(1), now=NOW)
    again = first.evolve(
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.REVISION_CAP, "detail": "used all"},
    )

    state = resolve_cap(again, make_config(), raise_by(2), now=NOW)

    assert state.allowances.extra_revisions == {Stage.PRODUCT_OWNER: 3}


def test_revisions_must_be_raised_by_a_whole_number() -> None:
    with pytest.raises(ValueError, match="whole"):
        resolve_cap(at_revision_cap(), make_config(), raise_by(1.5), now=NOW)


def test_abandoning_at_the_revision_cap_halts_and_records_a_rejection() -> None:
    state = resolve_cap(at_revision_cap(), make_config(), abandon(), now=NOW)

    assert state.status == Status.HALTED
    assert state.intervention is None
    assert state.gate_history[-1].outcome == GateOutcome.REJECT


# --- budget: which cap was hit ---------------------------------------------------------------


def test_the_assessment_says_whether_the_project_or_the_stage_cap_was_hit() -> None:
    config = make_config()
    stage_hit = new_state().evolve(cost_usd={Stage.PRODUCT_OWNER: 1.5})
    project_hit = new_state().evolve(cost_usd={Stage.DEVELOPER: 20.0})

    assert assess_budget(stage_hit, config, Stage.PRODUCT_OWNER).scope == BudgetScope.STAGE
    assert assess_budget(project_hit, config, Stage.DEVELOPER).scope == BudgetScope.PROJECT


def test_a_project_warning_has_project_scope_and_a_stage_only_warning_has_stage_scope() -> None:
    config = make_config()
    project = new_state().evolve(cost_usd={Stage.DEVELOPER: 16.0})
    stage = new_state().evolve(cost_usd={Stage.PRODUCT_OWNER: 1.2})

    assert assess_budget(project, config, Stage.ARCHITECT).scope == BudgetScope.PROJECT
    assert assess_budget(stage, config, Stage.PRODUCT_OWNER).scope == BudgetScope.STAGE


def test_an_ok_assessment_has_no_scope() -> None:
    assert assess_budget(new_state(), make_config(), Stage.QA).scope is None


# --- budget cap ------------------------------------------------------------------------------


def test_raising_a_stage_cap_adds_to_that_stage_only() -> None:
    state = resolve_cap(at_budget_pause(), make_config(), raise_by(1.0), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.allowances.extra_stage_usd == {Stage.PRODUCT_OWNER: 1.0}
    assert state.allowances.extra_project_usd == 0.0


def test_raising_the_project_cap_adds_to_the_project_allowance() -> None:
    paused = at_budget_pause(Stage.DEVELOPER, developer=20.0)

    state = resolve_cap(paused, make_config(), raise_by(5.0), now=NOW)

    assert state.allowances.extra_project_usd == 5.0
    assert state.allowances.extra_stage_usd == {}


def test_a_raise_that_clears_the_cap_lets_the_next_check_pass() -> None:
    state = resolve_cap(at_budget_pause(), make_config(), raise_by(5.0), now=NOW)

    assert assess_budget(state, make_config(), Stage.PRODUCT_OWNER).level.value == "ok"


def test_abandoning_at_a_budget_pause_halts_the_project() -> None:
    state = resolve_cap(at_budget_pause(), make_config(), abandon(), now=NOW)

    assert state.status == Status.HALTED
    assert state.intervention is None


def test_a_budget_pause_cannot_be_approved_anyway_once_a_cap_is_hit() -> None:
    with pytest.raises(ValueError, match="raise"):
        resolve_cap(at_budget_pause(), make_config(), approve_anyway(), now=NOW)


def test_continuing_at_a_warning_resumes_without_changing_any_cap() -> None:
    paused = at_budget_pause(Stage.PRODUCT_OWNER, product_owner=1.2)

    state = resolve_cap(paused, make_config(), approve_anyway(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.allowances == paused.allowances
    assert state.gate_history == paused.gate_history


def test_the_warning_still_shows_on_the_next_check_after_continuing() -> None:
    paused = at_budget_pause(Stage.PRODUCT_OWNER, product_owner=1.2)

    state = resolve_cap(paused, make_config(), approve_anyway(), now=NOW)

    assert assess_budget(state, make_config(), Stage.PRODUCT_OWNER).level.value == "warning"
