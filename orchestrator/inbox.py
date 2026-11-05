"""Asking the human through files, so any front end can answer: the terminal, a web page, a script.

When the pipeline needs a decision it writes one file, `runs/<project>/pending/<id>.json`, and
waits for `runs/<project>/answers/<id>.json`. A UI lists pending files and writes answers. Because
the files outlive the processes, a UI can restart, or the run can crash and be resumed, without
losing the question. The first valid answer wins; once taken, the question moves to `done/` with
who answered, which doubles as an audit trail.

Answers are checked with the same models the terminal backends use, so a front end cannot send
something the pipeline would not accept. A bad answer is set aside and the run keeps waiting.
"""

import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.caps import CapAction, CapRequest, CapResolution
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.halt import halt_requested
from orchestrator.state import atomic_write
from orchestrator.tool_hooks import ToolAnswer, ToolRequest

_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ID = re.compile(r"^[a-f0-9]{12}$")
HEARTBEAT_SECONDS = 5.0
ALIVE_WITHIN_SECONDS = 20.0


class PendingKind(StrEnum):
    GATE = "gate"
    CAP = "cap"
    MERGE = "merge"
    TOOL = "tool"


class Pending(BaseModel):
    """One question waiting for a human."""

    id: str
    project: str
    kind: PendingKind
    created: datetime
    request: dict[str, Any]

    model_config = ConfigDict(frozen=True)


class AnswerRejected(Exception):
    """An answer could not be used; the message says why."""


def _project_dir(runs_dir: Path, project: str) -> Path:
    if not _PROJECT_NAME.match(project):
        raise ValueError(f"unsafe project name: {project!r}")
    return runs_dir / project


def _read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _read_all(folder: Path) -> list[dict[str, Any]]:
    """Every JSON file in a folder. A file that vanishes mid-listing (answered) is skipped."""
    found = []
    for path in folder.glob("*.json"):
        try:
            found.append(_read(path))
        except FileNotFoundError:
            continue
    return found


class Inbox:
    """One project's questions and answers."""

    def __init__(
        self,
        runs_dir: Path,
        project: str,
        *,
        poll_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = _project_dir(runs_dir, project)
        self._runs_dir = runs_dir
        self._project = project
        self._poll = poll_seconds
        self._sleep = sleep

    # --- the side that asks ---------------------------------------------------------------

    def ask[T](
        self,
        kind: PendingKind,
        request: BaseModel,
        parse: Callable[[dict[str, Any]], T],
        *,
        if_halted: T,
    ) -> T:
        """Write the question, wait for a valid answer, and return it parsed.

        Questions left by an earlier run of this project are cleared first: only one question is
        ever open per project, and the run that is asking now is the one that matters. If the
        project is halted while waiting, `if_halted` is returned so the run can stop cleanly.
        """
        self._clear_stale()
        pending = Pending(
            id=uuid.uuid4().hex[:12],
            project=self._project,
            kind=kind,
            created=datetime.now(UTC),
            request=request.model_dump(mode="json"),
        )
        atomic_write(
            self._root / "pending" / f"{pending.id}.json", pending.model_dump_json(indent=2)
        )
        answer_path = self._root / "answers" / f"{pending.id}.json"
        while True:
            if answer_path.exists():
                try:
                    parsed = parse(_read(answer_path))
                except (ValueError, ValidationError, AnswerRejected) as error:
                    self._set_aside(pending.id, answer_path, str(error))
                else:
                    self._finish(pending, answer_path)
                    return parsed
            elif halt_requested(self._runs_dir, self._project):
                self._finish(pending, None, answered_by="halt")
                return if_halted
            else:
                self._sleep(self._poll)

    def _clear_stale(self) -> None:
        for folder in ("pending", "answers"):
            for path in (self._root / folder).glob("*.json"):
                path.unlink(missing_ok=True)

    def _set_aside(self, pending_id: str, answer_path: Path, reason: str) -> None:
        invalid = self._root / "answers" / f"{pending_id}.invalid"
        atomic_write(invalid, json.dumps({"reason": reason}, indent=2))
        answer_path.unlink(missing_ok=True)

    def _finish(
        self, pending: Pending, answer_path: Path | None, answered_by: str = "human"
    ) -> None:
        record: dict[str, Any] = pending.model_dump(mode="json")
        record["answered_at"] = datetime.now(UTC).isoformat()
        record["answered_by"] = answered_by
        if answer_path is not None:
            record["answer"] = _read(answer_path)
            answer_path.unlink(missing_ok=True)
        atomic_write(self._root / "done" / f"{pending.id}.json", json.dumps(record, indent=2))
        (self._root / "pending" / f"{pending.id}.json").unlink(missing_ok=True)
        (self._root / "answers" / f"{pending.id}.invalid").unlink(missing_ok=True)


# --- the side that answers (a UI, a script) -------------------------------------------------


def pending_for(runs_dir: Path, project: str) -> list[Pending]:
    folder = _project_dir(runs_dir, project) / "pending"
    return sorted(
        (Pending.model_validate(item) for item in _read_all(folder)), key=lambda p: p.created
    )


def all_pending(runs_dir: Path) -> list[Pending]:
    """Every open question across every project, oldest first."""
    found: list[Pending] = []
    for folder in sorted(runs_dir.glob("*/pending")):
        if _PROJECT_NAME.match(folder.parent.name):
            found += pending_for(runs_dir, folder.parent.name)
    return sorted(found, key=lambda p: p.created)


def answer(runs_dir: Path, project: str, pending_id: str, payload: dict[str, Any]) -> None:
    """Write an answer. Raises AnswerRejected if the question is gone (someone answered first)."""
    if not _ID.match(pending_id):
        raise AnswerRejected("not a question id")
    root = _project_dir(runs_dir, project)
    if not (root / "pending" / f"{pending_id}.json").exists():
        raise AnswerRejected("this question is no longer open: it was answered or the run moved on")
    target = root / "answers" / f"{pending_id}.json"
    if target.exists():
        raise AnswerRejected("this question has already been answered")
    atomic_write(target, json.dumps(payload, indent=2))


def rejection_reason(runs_dir: Path, project: str, pending_id: str) -> str | None:
    """Why the last answer to a question was refused, if it was."""
    path = _project_dir(runs_dir, project) / "answers" / f"{pending_id}.invalid"
    return str(_read(path)["reason"]) if path.exists() else None


def answered(runs_dir: Path, project: str) -> list[dict[str, Any]]:
    """Closed questions with their answers, oldest first."""
    folder = _project_dir(runs_dir, project) / "done"
    return sorted(_read_all(folder), key=lambda r: str(r["answered_at"]))


# --- is the run alive? ----------------------------------------------------------------------


def _marker(runs_dir: Path, project: str) -> Path:
    return _project_dir(runs_dir, project) / "run.json"


@contextlib.contextmanager
def run_marker(
    runs_dir: Path, project: str, *, beat_seconds: float = HEARTBEAT_SECONDS
) -> Iterator[None]:
    """While a run is going, keep `run.json` fresh so a UI can tell it from a crashed one."""
    path = _marker(runs_dir, project)
    started = datetime.now(UTC).isoformat()
    stop = threading.Event()

    def write(ended: str | None = None) -> None:
        atomic_write(
            path,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started": started,
                    "heartbeat": datetime.now(UTC).isoformat(),
                    "ended": ended,
                },
                indent=2,
            ),
        )

    def beat() -> None:
        while not stop.wait(beat_seconds):
            write()

    write()
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        write(ended=datetime.now(UTC).isoformat())


def run_is_alive(runs_dir: Path, project: str, *, now: datetime | None = None) -> bool:
    """True if a run of this project wrote a heartbeat recently and its process still exists."""
    path = _marker(runs_dir, project)
    if not path.exists():
        return False
    data = _read(path)
    if data.get("ended"):
        return False
    age = ((now or datetime.now(UTC)) - datetime.fromisoformat(data["heartbeat"])).total_seconds()
    if age > ALIVE_WITHIN_SECONDS:
        return False
    try:
        os.kill(int(data["pid"]), 0)
    except OSError:
        return False
    return True


# --- the backends the pipeline uses ---------------------------------------------------------


def _valid_gate(request: GateRequest) -> Callable[[dict[str, Any]], GateDecision]:
    def parse(payload: dict[str, Any]) -> GateDecision:
        decision = GateDecision.model_validate(payload)
        unknown = set(decision.approved_items) - {item.id for item in request.items}
        if unknown:
            raise AnswerRejected(f"unknown items: {', '.join(sorted(unknown))}")
        return decision

    return parse


class FileGateBackend:
    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    def decide(self, request: GateRequest) -> GateDecision:
        return self._inbox.ask(
            PendingKind.GATE,
            request,
            _valid_gate(request),
            if_halted=GateDecision(outcome=GateOutcome.REJECT, comments="halted", halt=True),
        )


class FileCapBackend:
    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    def resolve(self, request: CapRequest) -> CapResolution:
        return self._inbox.ask(
            PendingKind.CAP,
            request,
            CapResolution.model_validate,
            if_halted=CapResolution(action=CapAction.ABANDON),
        )


class MergeQuestion(BaseModel):
    pr_urls: list[str]


class MergeAnswer(BaseModel):
    merged: bool  # True: check again; False: give up

    model_config = ConfigDict(extra="forbid")


class FileMergeBackend:
    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    def wait_for_merge(self, pr_urls: list[str]) -> bool:
        answer_ = self._inbox.ask(
            PendingKind.MERGE,
            MergeQuestion(pr_urls=pr_urls),
            MergeAnswer.model_validate,
            if_halted=MergeAnswer(merged=False),
        )
        return answer_.merged


class ToolQuestion(BaseModel):
    tool: str
    summary: str
    reason: str


class ToolReply(BaseModel):
    allow: bool
    message: str = ""

    model_config = ConfigDict(extra="forbid")


class FileToolBackend:
    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox

    def ask(self, request: ToolRequest) -> ToolAnswer:
        reply = self._inbox.ask(
            PendingKind.TOOL,
            ToolQuestion(tool=request.tool, summary=request.summary, reason=request.reason),
            ToolReply.model_validate,
            if_halted=ToolReply(allow=False, message="the project was halted"),
        )
        return ToolAnswer(allow=reply.allow, message=reply.message)
