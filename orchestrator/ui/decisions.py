"""Turning a submitted form into the answer the pipeline expects, and describing open questions.

The form never decides what is valid: the answer is checked again, with the same models the
terminal uses, when the run reads it. This module only maps fields to the answer's shape. Question
texts and item ids come from the stored question, never from the form, so a forged field cannot
invent a question or approve an item the gate did not offer.
"""

from collections.abc import Mapping
from typing import Any

from orchestrator.inbox import Pending, PendingKind


class FormError(ValueError):
    """The submitted form is missing something; the message says what."""


def _text(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _gate(pending: Pending, form: Mapping[str, Any], items: list[str]) -> dict[str, Any]:
    outcome = _text(form, "outcome")
    if outcome not in {"approve", "request_changes", "reject"}:
        raise FormError("choose approve, request changes or reject")
    payload: dict[str, Any] = {"outcome": outcome, "comments": _text(form, "comments")}
    if outcome == "request_changes" and not payload["comments"]:
        raise FormError("say what should change")
    if outcome == "reject":
        payload["halt"] = _text(form, "halt") == "on"
    if outcome == "approve":
        offered = [item["id"] for item in pending.request.get("items", [])]
        payload["approved_items"] = [i for i in offered if i in items]
    questions = pending.request.get("questions", [])
    payload["answers"] = {q: a for n, q in enumerate(questions) if (a := _text(form, f"answer{n}"))}
    return payload


def _cap(pending: Pending, form: Mapping[str, Any]) -> dict[str, Any]:
    action = _text(form, "action")
    if action == "approve_anyway":
        if not pending.request.get("can_approve_anyway"):
            raise FormError("this pause cannot be approved anyway")
        return {"action": "approve_anyway"}
    if action == "abandon":
        return {"action": "abandon"}
    if action == "raise":
        try:
            amount = float(_text(form, "amount"))
        except ValueError:
            raise FormError("enter how much to raise it by") from None
        if amount <= 0:
            raise FormError("the amount must be more than zero")
        if pending.request.get("unit") == "revisions" and amount != int(amount):
            raise FormError("revisions must be a whole number")
        return {"action": "raise", "amount": amount}
    raise FormError("choose approve anyway, raise or abandon")


def payload_from_form(
    pending: Pending, form: Mapping[str, Any], items: list[str]
) -> dict[str, Any]:
    """The answer to `pending` from a submitted form. `items` are the ticked item ids."""
    if pending.kind == PendingKind.GATE:
        return _gate(pending, form, items)
    if pending.kind == PendingKind.CAP:
        return _cap(pending, form)
    if pending.kind == PendingKind.MERGE:
        choice = _text(form, "merged")
        if choice not in {"yes", "no"}:
            raise FormError("say whether you merged them")
        return {"merged": choice == "yes"}
    if pending.kind == PendingKind.TOOL:
        choice = _text(form, "allow")
        if choice not in {"yes", "no"}:
            raise FormError("choose allow or deny")
        return {"allow": choice == "yes", "message": _text(form, "message")}
    raise FormError(f"unknown question kind {pending.kind}")


_KIND_LABEL = {
    PendingKind.GATE: "Review",
    PendingKind.CAP: "Limit reached",
    PendingKind.MERGE: "Merge your pull requests",
    PendingKind.TOOL: "Permission needed",
}


def describe(pending: Pending) -> tuple[str, str]:
    """A short title and one line of detail for the inbox."""
    request = pending.request
    label = _KIND_LABEL[pending.kind]
    if pending.kind == PendingKind.GATE:
        return (
            f"{label}: {request.get('title', 'a stage')}",
            f"Spent so far ${request.get('cost_usd', 0):.2f}",
        )
    if pending.kind == PendingKind.CAP:
        return label, str(request.get("detail", ""))
    if pending.kind == PendingKind.MERGE:
        urls = request.get("pr_urls", [])
        return label, f"{len(urls)} pull request(s) must be merged before QA"
    return f"{label}: {request.get('tool', 'a tool')}", str(request.get("reason", ""))
