"""Notes from the human for the Developer's next task (for example, from the chat).

A note is the person's own instruction, so it goes into the Developer's task prompt. It is used
once: it is marked consumed only after a task that carried it succeeds, so a failed attempt keeps
it.
"""

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from orchestrator.state import atomic_write

_LOCK = threading.Lock()
MAX_NOTE_CHARS = 2000


class Note(BaseModel):
    id: str
    text: str
    created: datetime
    consumed_at: datetime | None = None


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "notes.json"


def _load(runs_dir: Path, project: str) -> list[Note]:
    try:
        raw = json.loads(_path(runs_dir, project).read_text())
        return [Note.model_validate(n) for n in raw["notes"]]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ValidationError):
        return []


def _save(runs_dir: Path, project: str, notes: list[Note]) -> None:
    path = _path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"notes": [n.model_dump(mode="json") for n in notes]}, indent=2))


def add_note(runs_dir: Path, project: str, text: str) -> Note:
    text = text.strip()
    if not text or len(text) > MAX_NOTE_CHARS:
        raise ValueError(f"a note is 1 to {MAX_NOTE_CHARS} characters")
    with _LOCK:
        note = Note(id=uuid.uuid4().hex[:8], text=text, created=datetime.now(UTC))
        _save(runs_dir, project, [*_load(runs_dir, project), note])
        return note


def pending_notes(runs_dir: Path, project: str) -> list[Note]:
    return [n for n in _load(runs_dir, project) if n.consumed_at is None]


def consume_notes(runs_dir: Path, project: str, ids: list[str]) -> None:
    with _LOCK:
        notes = _load(runs_dir, project)
        for note in notes:
            if note.id in ids and note.consumed_at is None:
                note.consumed_at = datetime.now(UTC)
        _save(runs_dir, project, notes)
