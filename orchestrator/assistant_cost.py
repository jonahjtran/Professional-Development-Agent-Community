"""A separate ledger for what the helper features cost (explanations, chat).

Helper spend is shown to the human but is never added to a project's own spend or its caps, so a
chat cannot make a project look over budget and a project's numbers stay about the pipeline.
"""

import json
import threading
from pathlib import Path

from orchestrator.state import atomic_write

_KINDS = ("explain_usd", "chat_usd")
_LOCK = threading.Lock()


def _path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "assistant_cost.json"


def assistant_totals(runs_dir: Path, project: str) -> dict[str, float]:
    totals = dict.fromkeys(_KINDS, 0.0)
    try:
        raw = json.loads(_path(runs_dir, project).read_text())
    except (FileNotFoundError, ValueError):
        return totals
    if isinstance(raw, dict):
        for kind in _KINDS:
            value = raw.get(kind)
            if isinstance(value, int | float):
                totals[kind] = float(value)
    return totals


def record_assistant_cost(runs_dir: Path, project: str, kind: str, usd: float) -> None:
    if kind not in _KINDS:
        raise ValueError(f"unknown helper cost kind: {kind}")
    with _LOCK:
        totals = assistant_totals(runs_dir, project)
        totals[kind] += usd
        path = _path(runs_dir, project)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(totals, indent=2))
