"""Actions the chat proposes and only the human can carry out.

The assistant never changes anything consequential itself. It writes a proposal; the server builds
the card the human sees from the proposal's own fields; a click on Confirm (which needs the session
token) runs it. A proposal is bound to its exact action and arguments, so changed arguments are a
new proposal, and each proposal can be decided once.
"""

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from orchestrator.state import atomic_write

ACTIONS = (
    "answer_question",
    "resume_project",
    "set_autonomy",
    "leave_note",
    "set_developer_backend",
    "send_back",
)
Status = Literal["pending", "confirmed", "dismissed", "failed"]
_LOCK = threading.Lock()


class Proposal(BaseModel):
    id: str
    project: str
    action: str
    args: dict[str, Any]
    summary: str
    created: datetime
    status: Status = "pending"
    result: str | None = None


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "proposals.json"


def _load(runs_dir: Path, project: str) -> list[Proposal]:
    try:
        raw = json.loads(_path(runs_dir, project).read_text())
        return [Proposal.model_validate(p) for p in raw["proposals"]]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ValidationError):
        return []


def _save(runs_dir: Path, project: str, proposals: list[Proposal]) -> None:
    path = _path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"proposals": [p.model_dump(mode="json") for p in proposals]}
    atomic_write(path, json.dumps(body, indent=2))


def create_proposal(
    runs_dir: Path, project: str, action: str, args: dict[str, Any], summary: str
) -> Proposal:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    with _LOCK:
        proposal = Proposal(
            id=uuid.uuid4().hex[:10],
            project=project,
            action=action,
            args=args,
            summary=summary,
            created=datetime.now(UTC),
        )
        _save(runs_dir, project, [*_load(runs_dir, project), proposal])
        return proposal


def get_proposal(runs_dir: Path, project: str, proposal_id: str) -> Proposal | None:
    return next((p for p in _load(runs_dir, project) if p.id == proposal_id), None)


def list_proposals(
    runs_dir: Path, *, project: str | None = None, status: Status | None = None
) -> list[Proposal]:
    projects = [project] if project else _projects(runs_dir)
    found = [p for slug in projects for p in _load(runs_dir, slug)]
    return [p for p in found if status is None or p.status == status]


def _projects(runs_dir: Path) -> list[str]:
    if not runs_dir.is_dir():
        return []
    return sorted(p.name for p in runs_dir.iterdir() if p.is_dir())


def set_status(
    runs_dir: Path, project: str, proposal_id: str, status: Status, result: str
) -> Proposal | None:
    """Decide a pending proposal once. None if it is unknown or already decided."""
    with _LOCK:
        proposals = _load(runs_dir, project)
        for proposal in proposals:
            if proposal.id == proposal_id and proposal.status == "pending":
                proposal.status = status
                proposal.result = result
                _save(runs_dir, project, proposals)
                return proposal
        return None
