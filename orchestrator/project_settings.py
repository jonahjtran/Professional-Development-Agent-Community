"""Per-project settings the human can change while a project runs (today: the autonomy level).

cautious: saved always-allow rules are ignored.
standard: the built-in allowed list plus the human's saved rules (the default).
autonomous: also allows restoring what a lockfile already lists (`npm ci`).
None of these levels can allow anything the policy refuses.
"""

import json
from pathlib import Path
from typing import Any

from orchestrator.state import atomic_write

AUTONOMY_LEVELS = ("cautious", "standard", "autonomous")


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "settings.json"


def read_settings(path: Path) -> dict[str, Any]:
    """A settings file as a dict; a missing or damaged file is empty."""
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def update_settings(path: Path, **changes: Any) -> None:
    """Change some settings and keep the others. A value of None removes the setting."""
    settings = read_settings(path)
    for key, value in changes.items():
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(settings, indent=2))


def project_settings_path(runs_dir: Path, project: str) -> Path:
    return _path(runs_dir, project)


def get_autonomy(runs_dir: Path, project: str) -> str:
    level = read_settings(_path(runs_dir, project)).get("autonomy")
    return level if level in AUTONOMY_LEVELS else "standard"


def set_autonomy(runs_dir: Path, project: str, level: str) -> None:
    if level not in AUTONOMY_LEVELS:
        raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY_LEVELS)}")
    update_settings(_path(runs_dir, project), autonomy=level)
