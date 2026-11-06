"""Things only the human can do: the agent cannot, so the app says so out loud.

Unlike a question, an action item never blocks the run. It sits in `runs/<project>/todos.json`
until the human marks it done (or, for a pause caused by an error, until the project resumes).
Sources: the Developer's own `needs_from_human` list, a pause caused by an error, and a QA tool
that was not installed.
"""

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from orchestrator.state import atomic_write

_LOCK = threading.Lock()


class Todo(BaseModel):
    id: str
    project: str
    title: str
    detail: str = ""
    source: str  # developer | error | qa
    key: str
    created: datetime
    done_at: datetime | None = None


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "todos.json"


def _load(runs_dir: Path, project: str) -> list[Todo]:
    try:
        raw = json.loads(_path(runs_dir, project).read_text())
        return [Todo.model_validate(t) for t in raw["todos"]]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ValidationError):
        return []


def _save(runs_dir: Path, project: str, todos: list[Todo]) -> None:
    path = _path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"todos": [t.model_dump(mode="json") for t in todos]}
    atomic_write(path, json.dumps(body, indent=2))


def add_todo(
    runs_dir: Path, project: str, *, key: str, title: str, source: str, detail: str = ""
) -> Todo:
    """Add an item, or return the open one with the same key (a done item is raised again)."""
    with _LOCK:
        todos = _load(runs_dir, project)
        for existing in todos:
            if existing.key == key and existing.done_at is None:
                return existing
        todo = Todo(
            id=hashlib.sha1(f"{key}{datetime.now(UTC).isoformat()}".encode()).hexdigest()[:10],
            project=project,
            title=title,
            detail=detail,
            source=source,
            key=key,
            created=datetime.now(UTC),
        )
        _save(runs_dir, project, [*todos, todo])
        return todo


def open_todos(runs_dir: Path, project: str) -> list[Todo]:
    return [t for t in _load(runs_dir, project) if t.done_at is None]


def all_open_todos(runs_dir: Path) -> list[Todo]:
    found: list[Todo] = []
    if runs_dir.is_dir():
        for folder in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            found.extend(open_todos(runs_dir, folder.name))
    return found


def mark_done(runs_dir: Path, project: str, todo_id: str) -> bool:
    with _LOCK:
        todos = _load(runs_dir, project)
        for todo in todos:
            if todo.id == todo_id and todo.done_at is None:
                todo.done_at = datetime.now(UTC)
                _save(runs_dir, project, todos)
                return True
        return False


def close_source(runs_dir: Path, project: str, source: str) -> int:
    with _LOCK:
        todos = _load(runs_dir, project)
        closed = 0
        for todo in todos:
            if todo.source == source and todo.done_at is None:
                todo.done_at = datetime.now(UTC)
                closed += 1
        if closed:
            _save(runs_dir, project, todos)
        return closed
