from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.gates import (
    GateDecision,
    GateOutcome,
    GateRequest,
    apply_decision,
    open_gate,
    run_gate,
)
from orchestrator.state import (
    Allowances,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    load_state,
    save_state,
)
from tests.factories import make_config, new_state

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def waiting_at(stage: Stage, **changes: object) -> ProjectState:
    """A project that has opened the gate for `stage` and is waiting for the human."""
    return open_gate(new_state().evolve(stage=stage, **changes), title=f"Review the {stage} output")


class ScriptedBackend:
    """A fake human: returns the decisions it was given, in order, and records what it was asked."""

    def __init__(self, *decisions: GateDecision) -> None:
        self._decisions = list(decisions)
        self.requests: list[GateRequest] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        return self._decisions.pop(0)


def approve(comments: str = "") -> GateDecision:
    return GateDecision(outcome=GateOutcome.APPROVE, comments=comments)


def request_changes(comments: str = "Tighten the scope") -> GateDecision:
    return GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments=comments)


def reject(halt: bool = False, comments: str = "") -> GateDecision:
    return GateDecision(outcome=GateOutcome.REJECT, comments=comments, halt=halt)


# --- decisions -------------------------------------------------------------------------------


def test_request_changes_requires_comments() -> None:
    with pytest.raises(ValidationError):
        GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="  ")


def test_only_a_rejection_can_halt() -> None:
    with pytest.raises(ValidationError):
        GateDecision(outcome=GateOutcome.APPROVE, halt=True)


# --- opening a gate --------------------------------------------------------------------------


def test_opening_a_gate_waits_for_a_human_with_a_gate_intervention() -> None:
    state = open_gate(new_state().evolve(stage=Stage.PRODUCT_OWNER), title="Review the PRD")

    assert state.status == Status.AWAITING_HUMAN
    assert state.intervention is not None
    assert state.intervention.kind == InterventionKind.GATE
    assert state.intervention.detail == "Review the PRD"
    assert state.stage == Stage.PRODUCT_OWNER


# --- approve ---------------------------------------------------------------------------------


def test_approving_returns_the_project_to_running_and_clears_the_intervention() -> None:
    state = apply_decision(waiting_at(Stage.PRODUCT_OWNER), make_config(), approve(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None


def test_approving_is_recorded_in_the_gate_history() -> None:
    state = apply_decision(
        waiting_at(Stage.ARCHITECT), make_config(), approve("Looks good"), now=NOW
    )

    [record] = state.gate_history
    assert record.stage == Stage.ARCHITECT
    assert record.outcome == GateOutcome.APPROVE
    assert record.comments == "Looks good"
    assert record.ts == NOW


# --- request changes -------------------------------------------------------------------------


def test_requesting_changes_counts_a_revision_for_that_stage() -> None:
    state = apply_decision(
        waiting_at(Stage.PRODUCT_OWNER), make_config(), request_changes(), now=NOW
    )

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.revisions == {Stage.PRODUCT_OWNER: 1}


def test_requesting_changes_records_the_comments() -> None:
    state = apply_decision(
        waiting_at(Stage.PRODUCT_OWNER), make_config(), request_changes("Add non-goals"), now=NOW
    )

    assert state.gate_history[-1].outcome == GateOutcome.REQUEST_CHANGES
    assert state.gate_history[-1].comments == "Add non-goals"


def test_revisions_are_counted_separately_for_each_stage() -> None:
    state = waiting_at(Stage.ARCHITECT, revisions={Stage.PRODUCT_OWNER: 2})

    state = apply_decision(state, make_config(), request_changes(), now=NOW)

    assert state.revisions == {Stage.PRODUCT_OWNER: 2, Stage.ARCHITECT: 1}


def test_the_last_allowed_revision_is_still_accepted() -> None:
    state = waiting_at(Stage.PRODUCT_OWNER, revisions={Stage.PRODUCT_OWNER: 2})

    state = apply_decision(state, make_config(), request_changes(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.revisions[Stage.PRODUCT_OWNER] == 3


def test_requesting_changes_past_the_revision_cap_stops_for_a_human() -> None:
    state = waiting_at(Stage.PRODUCT_OWNER, revisions={Stage.PRODUCT_OWNER: 3})

    state = apply_decision(state, make_config(), request_changes("One more pass"), now=NOW)

    assert state.status == Status.AWAITING_HUMAN
    assert state.intervention is not None
    assert state.intervention.kind == InterventionKind.REVISION_CAP
    assert "product_owner" in state.intervention.detail
    assert state.revisions[Stage.PRODUCT_OWNER] == 3


def test_a_request_past_the_cap_is_still_recorded_in_the_history() -> None:
    state = waiting_at(Stage.PRODUCT_OWNER, revisions={Stage.PRODUCT_OWNER: 3})

    state = apply_decision(state, make_config(), request_changes("One more pass"), now=NOW)

    assert state.gate_history[-1].comments == "One more pass"


# --- reject ----------------------------------------------------------------------------------


def test_rejecting_without_halting_restarts_the_stage() -> None:
    state = apply_decision(waiting_at(Stage.ARCHITECT), make_config(), reject(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.intervention is None
    assert state.stage == Stage.ARCHITECT
    assert state.gate_history[-1].outcome == GateOutcome.REJECT


def test_rejecting_does_not_reset_the_revision_count() -> None:
    state = waiting_at(Stage.ARCHITECT, revisions={Stage.ARCHITECT: 2})

    state = apply_decision(state, make_config(), reject(), now=NOW)

    assert state.revisions == {Stage.ARCHITECT: 2}


def test_rejecting_with_halt_stops_the_project() -> None:
    state = apply_decision(
        waiting_at(Stage.ARCHITECT),
        make_config(),
        reject(halt=True, comments="Wrong idea"),
        now=NOW,
    )

    assert state.status == Status.HALTED
    assert state.intervention is None
    assert state.gate_history[-1].comments == "Wrong idea"


# --- guards and history ----------------------------------------------------------------------


def test_a_decision_needs_an_open_gate() -> None:
    with pytest.raises(ValueError, match="open gate"):
        apply_decision(new_state(), make_config(), approve(), now=NOW)


def test_a_decision_cannot_answer_a_different_kind_of_pause() -> None:
    paused = new_state().evolve(
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.BUDGET_CAP, "detail": "Over budget"},
    )

    with pytest.raises(ValueError, match="open gate"):
        apply_decision(paused, make_config(), approve(), now=NOW)


def test_history_keeps_every_decision_in_order() -> None:
    config = make_config()
    state = apply_decision(
        waiting_at(Stage.PRODUCT_OWNER), config, request_changes("First"), now=NOW
    )
    state = apply_decision(open_gate(state, title="Review again"), config, approve(), now=NOW)

    assert [r.outcome for r in state.gate_history] == [
        GateOutcome.REQUEST_CHANGES,
        GateOutcome.APPROVE,
    ]


def test_gate_history_survives_save_and_load(tmp_path: Path) -> None:
    state = apply_decision(
        waiting_at(Stage.PRODUCT_OWNER), make_config(), request_changes("Add risks"), now=NOW
    )

    save_state(tmp_path, state)

    assert load_state(tmp_path, "demo") == state


# --- running a gate through a backend --------------------------------------------------------


def test_run_gate_shows_the_human_the_stage_the_summary_and_the_cost_so_far() -> None:
    backend = ScriptedBackend(approve())
    state = new_state().evolve(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: 0.75})

    run_gate(state, make_config(), backend, title="Review the PRD", summary="PRD draft 1", now=NOW)

    [request] = backend.requests
    assert request.stage == Stage.PRODUCT_OWNER
    assert request.title == "Review the PRD"
    assert request.summary == "PRD draft 1"
    assert request.cost_usd == pytest.approx(0.75)


def test_run_gate_applies_the_decision_the_backend_returns() -> None:
    backend = ScriptedBackend(request_changes("Add non-goals"))
    state = new_state().evolve(stage=Stage.PRODUCT_OWNER)

    result = run_gate(state, make_config(), backend, title="Review the PRD", now=NOW)

    assert result.revisions == {Stage.PRODUCT_OWNER: 1}
    assert result.gate_history[-1].comments == "Add non-goals"


def test_run_gate_reports_the_waiting_state_before_asking_the_human() -> None:
    waiting_statuses: list[Status] = []

    class RecordingBackend(ScriptedBackend):
        def decide(self, request: GateRequest) -> GateDecision:
            # By now the caller must already have been given the waiting state to save.
            assert waiting_statuses == [Status.AWAITING_HUMAN]
            return super().decide(request)

    backend = RecordingBackend(approve())
    state = new_state().evolve(stage=Stage.PRODUCT_OWNER)

    run_gate(
        state,
        make_config(),
        backend,
        title="Review the PRD",
        on_waiting=lambda s: waiting_statuses.append(s.status),
        now=NOW,
    )

    assert len(backend.requests) == 1


# --- extra revisions granted by the human -------------------------------------------------


def test_extra_revisions_let_a_stage_go_past_the_configured_cap() -> None:
    state = waiting_at(
        Stage.PRODUCT_OWNER,
        revisions={Stage.PRODUCT_OWNER: 3},
        allowances=Allowances(extra_revisions={Stage.PRODUCT_OWNER: 2}),
    )

    state = apply_decision(state, make_config(), request_changes(), now=NOW)

    assert state.status == Status.RUNNING
    assert state.revisions[Stage.PRODUCT_OWNER] == 4


def test_the_raised_revision_cap_still_applies_once_it_is_used_up() -> None:
    state = waiting_at(
        Stage.PRODUCT_OWNER,
        revisions={Stage.PRODUCT_OWNER: 5},
        allowances=Allowances(extra_revisions={Stage.PRODUCT_OWNER: 2}),
    )

    state = apply_decision(state, make_config(), request_changes(), now=NOW)

    assert state.intervention is not None
    assert state.intervention.kind == InterventionKind.REVISION_CAP
    assert "5 revisions" in state.intervention.detail


def test_extra_revisions_for_one_stage_do_not_help_another() -> None:
    state = waiting_at(
        Stage.ARCHITECT,
        revisions={Stage.ARCHITECT: 3},
        allowances=Allowances(extra_revisions={Stage.PRODUCT_OWNER: 2}),
    )

    state = apply_decision(state, make_config(), request_changes(), now=NOW)

    assert state.intervention is not None
    assert state.intervention.kind == InterventionKind.REVISION_CAP


# --- approving items one by one --------------------------------------------------------------


def test_items_can_only_be_approved_when_the_stage_is_approved() -> None:
    import pytest as _pytest

    assert GateDecision(outcome=GateOutcome.APPROVE, approved_items=["readme"]).approved_items == [
        "readme"
    ]
    with _pytest.raises(ValueError, match="items can only be approved"):
        GateDecision(outcome=GateOutcome.REJECT, approved_items=["readme"])
    with _pytest.raises(ValueError, match="items can only be approved"):
        GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="x", approved_items=["a"])


def test_a_decision_can_carry_answers_to_questions_with_any_outcome() -> None:
    decision = GateDecision(
        outcome=GateOutcome.REQUEST_CHANGES, comments="x", answers={"How many users?": "40"}
    )

    assert decision.answers == {"How many users?": "40"}
