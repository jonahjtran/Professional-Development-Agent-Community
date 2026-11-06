"""Per-project settings the human can change while a project runs (today: the autonomy level).

cautious: saved always-allow rules are ignored.
standard: the built-in allowed list plus the human's saved rules (the default).
autonomous: also allows restoring what a lockfile already lists (`npm ci`).
None of these levels can allow anything the policy refuses.
"""

import json
from pathlib import Path

from orchestrator.state import atomic_write

AUTONOMY_LEVELS = ("cautious", "standard", "autonomous")


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "settings.json"


def get_autonomy(runs_dir: Path, project: str) -> str:
    try:
        level = json.loads(_path(runs_dir, project).read_text()).get("autonomy")
    except (FileNotFoundError, ValueError, AttributeError):
        return "standard"
    return level if level in AUTONOMY_LEVELS else "standard"


def set_autonomy(runs_dir: Path, project: str, level: str) -> None:
    if level not in AUTONOMY_LEVELS:
        raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY_LEVELS)}")
    path = _path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"autonomy": level}, indent=2))
