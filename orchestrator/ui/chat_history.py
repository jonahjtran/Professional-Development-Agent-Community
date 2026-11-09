"""Past chats: every finished turn is kept, so a chat can be read and continued later.

One small file per chat in `runs/_studio/chats/<id>.json`: which repository and model it used, which
project it was opened from, what it has cost, and each turn (what you wrote; what the assistant
said as safe HTML, the steps it took, the proposals it made). Nothing here is a secret: it is the
text already shown on screen. A damaged file is skipped, never fatal.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.state import atomic_write

_ID = re.compile(r"^[a-f0-9]{12}$")
_TITLE = 60


def _folder(runs_dir: Path) -> Path:
    return runs_dir / "_studio" / "chats"


def _path(runs_dir: Path, chat_id: str) -> Path | None:
    return _folder(runs_dir) / f"{chat_id}.json" if _ID.match(chat_id) else None


def load_chat(runs_dir: Path, chat_id: str) -> dict[str, Any] | None:
    path = _path(runs_dir, chat_id)
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("turns"), list) else None


def remember(
    runs_dir: Path,
    chat_id: str,
    *,
    repo: str,
    tier: str,
    project: str | None,
    cost: float,
    you: str,
    assistant: dict[str, Any],
    now: datetime | None = None,
) -> None:
    """Add one finished turn (what you wrote and what came back) to the chat."""
    path = _path(runs_dir, chat_id)
    if path is None:
        return
    stamp = (now or datetime.now(UTC)).isoformat()
    chat = load_chat(runs_dir, chat_id) or {
        "id": chat_id,
        "title": " ".join(you.split())[:_TITLE],
        "created": stamp,
        "turns": [],
    }
    chat.update(repo=repo, tier=tier, project=project, cost_usd=cost, updated=stamp)
    chat["turns"] = [
        *chat["turns"],
        {"role": "you", "text": you},
        {"role": "assistant", **assistant},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(chat, indent=2))


def list_chats(runs_dir: Path, *, project: str | None = None) -> list[dict[str, Any]]:
    """Summaries, newest first; only the chats opened from `project` when one is given."""
    found: list[dict[str, Any]] = []
    for path in _folder(runs_dir).glob("*.json") if _folder(runs_dir).is_dir() else []:
        chat = load_chat(runs_dir, path.stem)
        if chat is None or (project is not None and chat.get("project") != project):
            continue
        found.append(
            {
                "id": chat["id"],
                "title": chat.get("title", ""),
                "repo": chat.get("repo", "studio"),
                "project": chat.get("project"),
                "updated": chat.get("updated", ""),
                "turns": len(chat["turns"]),
                "cost_usd": chat.get("cost_usd", 0.0),
            }
        )
    return sorted(found, key=lambda c: str(c["updated"]), reverse=True)


def delete_chat(runs_dir: Path, chat_id: str) -> bool:
    path = _path(runs_dir, chat_id)
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True


def transcript(chat: dict[str, Any], limit: int = 6000) -> str:
    """The conversation as plain text for the assistant, newest turns kept when it is long."""
    lines = []
    for turn in chat["turns"]:
        who = "Person" if turn.get("role") == "you" else "Assistant"
        lines.append(f"{who}: {turn.get('text', '')}")
    text = "\n\n".join(lines)
    return text[-limit:]
