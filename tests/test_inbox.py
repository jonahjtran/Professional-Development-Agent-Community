import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orchestrator.caps import CapAction, CapRequest
from orchestrator.cli import AgentKind, _context
from orchestrator.gates import GateDecision, GateItem, GateOutcome, GateRequest
from orchestrator.halt import request_halt
from orchestrator.inbox import (
    AnswerRejected,
    FileCapBackend,
    FileGateBackend,
    FileMergeBackend,
    FileToolBackend,
    Inbox,
    PendingKind,
    all_pending,
    answer,
    answered,
    pending_for,
    rejection_reason,
    run_is_alive,
    run_marker,
)
from orchestrator.pipeline import run_pipeline
from orchestrator.state import InterventionKind, ProjectState, Stage, Status
from orchestrator.tool_hooks import ToolRequest
from tests.factories import new_state

PROJECT = "demo"


class Human:
    """A person at a web page: waits for a pending question, then answers it from a thread."""

    def __init__(self, runs_dir: Path, respond: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.runs_dir = runs_dir
        self.respond = respond
        self.seen: list[Any] = []
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def __enter__(self) -> "Human":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _loop(self) -> None:
        done: set[str] = set()
        while not self._stop.is_set():
            for item in all_pending(self.runs_dir):
                if item.id in done:
                    continue
                done.add(item.id)
                self.seen.append(item)
                try:
                    answer(self.runs_dir, item.project, item.id, self.respond(item.request))
                except AnswerRejected as error:
                    self.errors.append(str(error))
            time.sleep(0.01)


def inbox(tmp_path: Path) -> Inbox:
    return Inbox(tmp_path, PROJECT, poll_seconds=0.01)


def gate_request(**fields: Any) -> GateRequest:
    base: dict[str, Any] = {
        "stage": Stage.PRODUCT_OWNER,
        "title": "Review the PRD",
        "summary": "the PRD",
        "cost_usd": 0.5,
    }
    return GateRequest(**{**base, **fields})


# --- asking and answering --------------------------------------------------------------------


def test_a_question_is_written_as_a_file_and_the_answer_comes_back_parsed(tmp_path: Path) -> None:
    with Human(tmp_path, lambda _req: {"outcome": "approve", "comments": "ok"}) as human:
        decision = FileGateBackend(inbox(tmp_path)).decide(gate_request())

    assert decision == GateDecision(outcome=GateOutcome.APPROVE, comments="ok")
    [seen] = human.seen
    assert seen.kind == PendingKind.GATE and seen.project == PROJECT
    assert seen.request["title"] == "Review the PRD" and seen.request["summary"] == "the PRD"


def test_an_answered_question_moves_to_done_with_who_answered_and_what(tmp_path: Path) -> None:
    with Human(tmp_path, lambda _req: {"outcome": "approve"}):
        FileGateBackend(inbox(tmp_path)).decide(gate_request())

    assert pending_for(tmp_path, PROJECT) == []
    [record] = answered(tmp_path, PROJECT)
    assert record["answered_by"] == "human" and record["answer"] == {"outcome": "approve"}
    assert record["kind"] == "gate" and record["request"]["title"] == "Review the PRD"
    assert not list((tmp_path / PROJECT / "answers").glob("*.json"))


def test_questions_from_an_earlier_run_are_cleared_when_a_new_one_asks(tmp_path: Path) -> None:
    stale = tmp_path / PROJECT / "pending" / "aaaaaaaaaaaa.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")

    with Human(tmp_path, lambda _req: {"outcome": "approve"}):
        FileGateBackend(inbox(tmp_path)).decide(gate_request())

    assert not stale.exists()


def test_an_invalid_answer_is_set_aside_with_a_reason_and_the_run_keeps_waiting(
    tmp_path: Path,
) -> None:
    replies = iter([{"outcome": "request_changes"}, {"outcome": "approve"}])  # first lacks comments
    reasons: list[str | None] = []

    def respond(_request: dict[str, Any]) -> dict[str, Any]:
        return next(replies)

    class PatientHuman(Human):
        def _loop(self) -> None:
            while not self._stop.is_set():
                for item in pending_for(self.runs_dir, PROJECT):
                    if item.id not in {s.id for s in self.seen}:
                        self.seen.append(item)
                        answer(self.runs_dir, PROJECT, item.id, next(replies))
                    else:
                        reason = rejection_reason(self.runs_dir, PROJECT, item.id)
                        if reason and not reasons:
                            reasons.append(reason)
                            answer(self.runs_dir, PROJECT, item.id, next(replies))
                time.sleep(0.01)

    with PatientHuman(tmp_path, respond):
        decision = FileGateBackend(inbox(tmp_path)).decide(gate_request())

    assert decision.outcome == GateOutcome.APPROVE
    assert reasons and "comments" in (reasons[0] or "")


def test_approving_an_item_the_gate_never_offered_is_refused(tmp_path: Path) -> None:
    request = gate_request(items=[GateItem(id="readme", label="README")])
    sent = iter(
        [
            {"outcome": "approve", "approved_items": ["nope"]},
            {"outcome": "approve", "approved_items": ["readme"]},
        ]
    )

    class Retry(Human):
        def _loop(self) -> None:
            first = True
            while not self._stop.is_set():
                for item in pending_for(self.runs_dir, PROJECT):
                    if first:
                        first = False
                        answer(self.runs_dir, PROJECT, item.id, next(sent))
                    elif rejection_reason(self.runs_dir, PROJECT, item.id):
                        answer(self.runs_dir, PROJECT, item.id, next(sent))
                        return
                time.sleep(0.01)

    with Retry(tmp_path, lambda r: r):
        decision = FileGateBackend(inbox(tmp_path)).decide(request)

    assert decision.approved_items == ["readme"]


def test_a_halt_while_waiting_returns_the_stop_answer_instead_of_hanging(tmp_path: Path) -> None:
    def halt_soon() -> None:
        time.sleep(0.1)
        request_halt(tmp_path, PROJECT)

    threading.Thread(target=halt_soon, daemon=True).start()

    decision = FileGateBackend(inbox(tmp_path)).decide(gate_request())

    assert decision.outcome == GateOutcome.REJECT and decision.halt
    [record] = answered(tmp_path, PROJECT)
    assert record["answered_by"] == "halt"


def test_answering_a_question_that_is_gone_or_already_answered_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AnswerRejected, match="no longer open"):
        answer(tmp_path, PROJECT, "abcdefabcdef", {"outcome": "approve"})
    with pytest.raises(AnswerRejected, match="not a question id"):
        answer(tmp_path, PROJECT, "../../etc", {"outcome": "approve"})
    pending = tmp_path / PROJECT / "pending"
    pending.mkdir(parents=True)
    (pending / "abcdefabcdef.json").write_text("{}")
    answer(tmp_path, PROJECT, "abcdefabcdef", {"outcome": "approve"})
    with pytest.raises(AnswerRejected, match="already been answered"):
        answer(tmp_path, PROJECT, "abcdefabcdef", {"outcome": "reject"})


def test_an_unsafe_project_name_is_refused() -> None:
    with pytest.raises(ValueError, match="unsafe project name"):
        Inbox(Path("."), "../x")


def test_every_open_question_across_projects_is_listed_oldest_first(tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        folder = tmp_path / name / "pending"
        folder.mkdir(parents=True)
        pending: dict[str, Any] = {
            "id": "a" * 12 if name == "alpha" else "b" * 12,
            "project": name,
            "kind": "gate",
            "created": datetime(
                2026, 11, 5, 12, 0 if name == "beta" else 5, tzinfo=UTC
            ).isoformat(),
            "request": {},
        }
        (folder / f"{pending['id']}.json").write_text(json.dumps(pending))

    assert [p.project for p in all_pending(tmp_path)] == ["beta", "alpha"]


def test_a_question_answered_while_the_list_is_being_read_is_skipped_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import inbox as inbox_module

    folder = tmp_path / PROJECT / "pending"
    folder.mkdir(parents=True)
    ghost = folder / "aaaaaaaaaaaa.json"  # listed, then gone before it is read
    real_glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [ghost, *real_glob(self, pattern)])

    assert inbox_module.pending_for(tmp_path, PROJECT) == []


# --- the other backends ----------------------------------------------------------------------


def test_a_budget_pause_is_answered_through_the_inbox(tmp_path: Path) -> None:
    request = CapRequest(
        kind=InterventionKind.BUDGET_CAP,
        stage=Stage.DEVELOPER,
        detail="80% of the cap",
        can_approve_anyway=True,
        unit="usd",
    )
    with Human(tmp_path, lambda _r: {"action": "raise", "amount": 5}) as human:
        resolution = FileCapBackend(inbox(tmp_path)).resolve(request)

    assert resolution.action == CapAction.RAISE and resolution.amount == 5
    assert human.seen[0].kind == PendingKind.CAP and human.seen[0].request["unit"] == "usd"


def test_a_cap_answer_that_raises_without_an_amount_is_refused(tmp_path: Path) -> None:
    sent = iter([{"action": "raise"}, {"action": "abandon"}])
    request = CapRequest(
        kind=InterventionKind.REVISION_CAP,
        stage=Stage.ARCHITECT,
        detail="d",
        can_approve_anyway=False,
        unit="revisions",
    )

    class Retry(Human):
        def _loop(self) -> None:
            sent_first = False
            while not self._stop.is_set():
                for item in pending_for(self.runs_dir, PROJECT):
                    if not sent_first:
                        sent_first = True
                        answer(self.runs_dir, PROJECT, item.id, next(sent))
                    elif rejection_reason(self.runs_dir, PROJECT, item.id):
                        answer(self.runs_dir, PROJECT, item.id, next(sent))
                        return
                time.sleep(0.01)

    with Retry(tmp_path, lambda r: r):
        resolution = FileCapBackend(inbox(tmp_path)).resolve(request)

    assert resolution.action == CapAction.ABANDON


def test_the_merge_question_lists_the_pull_requests_and_returns_the_answer(tmp_path: Path) -> None:
    with Human(tmp_path, lambda _r: {"merged": True}) as human:
        merged = FileMergeBackend(inbox(tmp_path)).wait_for_merge(
            ["https://github.com/me/p/pull/1"]
        )

    assert merged is True
    assert human.seen[0].request == {"pr_urls": ["https://github.com/me/p/pull/1"]}


def test_a_tool_request_shows_what_the_developer_wants_and_returns_allow_or_deny(
    tmp_path: Path,
) -> None:
    request = ToolRequest(tool="Bash", summary="pip install requests", reason="installs a package")
    with Human(tmp_path, lambda _r: {"allow": False, "message": "use the stdlib"}) as human:
        reply = FileToolBackend(inbox(tmp_path)).ask(request)

    assert reply.allow is False and reply.message == "use the stdlib"
    assert human.seen[0].request["summary"] == "pip install requests"


def test_halting_denies_a_waiting_tool_request_and_abandons_a_waiting_merge(tmp_path: Path) -> None:
    request_halt(tmp_path, PROJECT)

    assert FileToolBackend(inbox(tmp_path)).ask(ToolRequest("Bash", "x", "y")).allow is False
    assert FileMergeBackend(inbox(tmp_path)).wait_for_merge(["u"]) is False


# --- is the run alive ------------------------------------------------------------------------


def test_a_running_marker_is_alive_and_an_ended_one_is_not(tmp_path: Path) -> None:
    assert not run_is_alive(tmp_path, PROJECT)

    with run_marker(tmp_path, PROJECT, beat_seconds=0.05):
        assert run_is_alive(tmp_path, PROJECT)

    assert not run_is_alive(tmp_path, PROJECT)


def test_the_heartbeat_keeps_moving_while_the_run_goes(tmp_path: Path) -> None:
    with run_marker(tmp_path, PROJECT, beat_seconds=0.05):
        first = json.loads((tmp_path / PROJECT / "run.json").read_text())["heartbeat"]
        time.sleep(0.2)
        later = json.loads((tmp_path / PROJECT / "run.json").read_text())["heartbeat"]

    assert later > first


def test_a_marker_with_an_old_heartbeat_or_a_dead_process_is_not_alive(tmp_path: Path) -> None:
    with run_marker(tmp_path, PROJECT, beat_seconds=60):
        assert not run_is_alive(tmp_path, PROJECT, now=datetime.now(UTC) + timedelta(seconds=60))
        marker = tmp_path / PROJECT / "run.json"
        data = json.loads(marker.read_text())
        data["pid"] = 2**22 + 12345  # a process id that does not exist
        marker.write_text(json.dumps(data))
        assert not run_is_alive(tmp_path, PROJECT)


# --- the whole pipeline through the inbox ----------------------------------------------------


def test_a_whole_project_runs_to_the_end_with_every_question_answered_through_files(
    tmp_path: Path,
) -> None:
    config = Path(__file__).parent.parent / "config.yaml"
    ctx = _context(config, tmp_path, AgentKind.ECHO, project=PROJECT, ui=True)
    state: ProjectState = new_state()

    with Human(tmp_path, lambda _request: {"outcome": "approve"}) as human:
        finished = run_pipeline(state, ctx)

    assert finished.status == Status.DONE
    assert len(human.seen) >= 4  # one gate each for the product owner, architect, QA, coach
    assert pending_for(tmp_path, PROJECT) == []
    assert all(r["answered_by"] == "human" for r in answered(tmp_path, PROJECT))
