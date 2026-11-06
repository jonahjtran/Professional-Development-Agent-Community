"""Telling the person something needs them, even when no browser tab is open (macOS)."""

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from orchestrator.inbox import all_pending
from orchestrator.todos import all_open_todos
from orchestrator.ui.decisions import describe

Notify = Callable[[str, str], None]


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def mac_script(title: str, body: str, sound: str | None = None) -> str:
    script = (
        f"display notification {_applescript_quote(body)} with title {_applescript_quote(title)}"
    )
    if sound:
        script += f" sound name {_applescript_quote(sound)}"
    return script


def mac_notify(title: str, body: str) -> None:
    """Show a macOS notification. Failures are ignored: a missed alert must not stop the UI."""
    _run(mac_script(title, body))


def mac_alert(title: str, body: str) -> None:
    """The louder kind, with a sound, for things only the human can do."""
    _run(mac_script(title, body, sound="Glass"))


def _run(script: str) -> None:
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def watch(
    runs_dir: Path,
    notify: Notify,
    stop: threading.Event,
    *,
    interval: float = 5.0,
    alert: Notify | None = None,
) -> None:
    """Notify once per new question, and alert once per new action item.

    Ones already open when this starts are not announced. Action items (things only the human can
    do) go to `alert`, the louder channel; without one they use `notify`.
    """
    loud = alert or notify
    seen = {p.id for p in all_pending(runs_dir)}
    seen_todos = {t.id for t in all_open_todos(runs_dir)}
    while not stop.wait(interval):
        for pending in all_pending(runs_dir):
            if pending.id not in seen:
                seen.add(pending.id)
                notify("Agent Studio needs you", f"{pending.project}: {describe(pending)[0]}")
        for todo in all_open_todos(runs_dir):
            if todo.id not in seen_todos:
                seen_todos.add(todo.id)
                loud("You need to do something", f"{todo.project}: {todo.title}")
