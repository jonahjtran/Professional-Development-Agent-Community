"""Telling the person something needs them, even when no browser tab is open (macOS)."""

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from orchestrator.inbox import all_pending
from orchestrator.ui.decisions import describe

Notify = Callable[[str, str], None]


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def mac_notify(title: str, body: str) -> None:
    """Show a macOS notification. Failures are ignored: a missed alert must not stop the UI."""
    script = (
        f"display notification {_applescript_quote(body)} with title {_applescript_quote(title)}"
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def watch(runs_dir: Path, notify: Notify, stop: threading.Event, *, interval: float = 5.0) -> None:
    """Notify once per new question. Ones already open when this starts are not announced."""
    seen = {p.id for p in all_pending(runs_dir)}
    while not stop.wait(interval):
        for pending in all_pending(runs_dir):
            if pending.id not in seen:
                seen.add(pending.id)
                notify("Agent Studio needs you", f"{pending.project}: {describe(pending)[0]}")
