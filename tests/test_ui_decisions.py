from datetime import UTC, datetime
from typing import Any

import pytest

from orchestrator.inbox import Pending, PendingKind
from orchestrator.ui.decisions import FormError, describe, payload_from_form

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def pending(kind: PendingKind, **request: Any) -> Pending:
    return Pending(id="a" * 12, project="demo", kind=kind, created=NOW, request=request)


GATE = {
    "stage": "career_coach",
    "title": "Review the career plan",
    "summary": "# plan",
    "cost_usd": 1.5,
    "items": [{"id": "readme", "label": "README"}, {"id": "website", "label": "Website"}],
    "questions": ["How many users?", "Built alone?"],
}


def gate(form: dict[str, Any], items: list[str] | None = None) -> dict[str, Any]:
    return payload_from_form(pending(PendingKind.GATE, **GATE), form, items or [])


# --- gates -----------------------------------------------------------------------------------


def test_approving_carries_the_ticked_items_and_the_answers() -> None:
    payload = gate(
        {"outcome": "approve", "comments": "good", "answer0": " 40 students ", "answer1": ""},
        ["readme"],
    )

    assert payload == {
        "outcome": "approve",
        "comments": "good",
        "approved_items": ["readme"],
        "answers": {"How many users?": "40 students"},
    }


def test_a_forged_item_id_is_dropped_because_only_offered_items_count() -> None:
    payload = gate({"outcome": "approve"}, ["readme", "delete-everything"])

    assert payload["approved_items"] == ["readme"]


def test_answers_are_keyed_by_the_stored_question_not_by_anything_in_the_form() -> None:
    payload = gate({"outcome": "approve", "answer1": "yes", "answer9": "ghost"})

    assert payload["answers"] == {"Built alone?": "yes"}


def test_requesting_changes_needs_a_comment_and_carries_no_items() -> None:
    with pytest.raises(FormError, match="what should change"):
        gate({"outcome": "request_changes", "comments": "  "})

    payload = gate({"outcome": "request_changes", "comments": "tighten it"}, ["readme"])

    assert "approved_items" not in payload and payload["comments"] == "tighten it"


def test_rejecting_can_halt_the_project() -> None:
    assert gate({"outcome": "reject", "halt": "on"})["halt"] is True
    assert gate({"outcome": "reject"})["halt"] is False


def test_an_unknown_outcome_is_refused() -> None:
    with pytest.raises(FormError, match="choose approve"):
        gate({"outcome": "maybe"})
    with pytest.raises(FormError):
        gate({})


# --- the other kinds -------------------------------------------------------------------------


def cap(**request: Any) -> Pending:
    base: dict[str, Any] = {"can_approve_anyway": True, "unit": "usd", "detail": "d"}
    return pending(PendingKind.CAP, **{**base, **request})


def test_a_cap_can_be_approved_anyway_raised_or_abandoned() -> None:
    assert payload_from_form(cap(), {"action": "approve_anyway"}, []) == {
        "action": "approve_anyway"
    }
    assert payload_from_form(cap(), {"action": "abandon"}, []) == {"action": "abandon"}
    assert payload_from_form(cap(), {"action": "raise", "amount": "5.5"}, []) == {
        "action": "raise",
        "amount": 5.5,
    }


def test_approving_anyway_is_refused_when_the_pause_does_not_allow_it() -> None:
    with pytest.raises(FormError, match="cannot be approved anyway"):
        payload_from_form(cap(can_approve_anyway=False), {"action": "approve_anyway"}, [])


@pytest.mark.parametrize("amount", ["", "abc", "0", "-3"])
def test_a_raise_needs_a_positive_number(amount: str) -> None:
    with pytest.raises(FormError):
        payload_from_form(cap(), {"action": "raise", "amount": amount}, [])


def test_revisions_are_raised_in_whole_numbers() -> None:
    with pytest.raises(FormError, match="whole number"):
        payload_from_form(cap(unit="revisions"), {"action": "raise", "amount": "1.5"}, [])
    assert payload_from_form(cap(unit="revisions"), {"action": "raise", "amount": "2"}, []) == {
        "action": "raise",
        "amount": 2.0,
    }


def test_the_merge_and_tool_questions() -> None:
    merge = pending(PendingKind.MERGE, pr_urls=["u"])
    tool = pending(PendingKind.TOOL, tool="Bash", summary="ls", reason="r")

    assert payload_from_form(merge, {"merged": "yes"}, []) == {"merged": True}
    assert payload_from_form(merge, {"merged": "no"}, []) == {"merged": False}
    assert payload_from_form(tool, {"allow": "yes", "message": "ok"}, []) == {
        "allow": True,
        "message": "ok",
    }
    assert payload_from_form(tool, {"allow": "no"}, []) == {"allow": False, "message": ""}
    with pytest.raises(FormError):
        payload_from_form(merge, {}, [])
    with pytest.raises(FormError):
        payload_from_form(tool, {"allow": "perhaps"}, [])


# --- describing questions --------------------------------------------------------------------


def test_each_kind_has_a_title_and_a_line_for_the_inbox() -> None:
    assert describe(pending(PendingKind.GATE, **GATE)) == (
        "Review: Review the career plan",
        "Spent so far $1.50",
    )
    assert describe(cap(detail="80% of the cap")) == ("Limit reached", "80% of the cap")
    assert describe(pending(PendingKind.MERGE, pr_urls=["a", "b"]))[1].startswith("2 pull request")
    assert describe(pending(PendingKind.TOOL, tool="Bash", summary="s", reason="why")) == (
        "Permission needed: Bash",
        "why",
    )
