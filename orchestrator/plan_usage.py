"""Your real Claude plan usage: what Anthropic reports, kept per call, per task and per project.

Claude Code reports your plan's limit windows (the five-hour window, the weekly one, ...) as it
works: how much of each you have used, as a fraction, and when it resets. That is the figure
`/usage` shows in Claude Code, so it is measured, not estimated, and it is the only real usage
number a subscription gives. Studio keeps:

- the latest reading (`runs/_studio/plan_windows.json`), refreshed by every Developer task and by
  "Check now" (one tiny message from your plan);
- per Developer task (`runs/<project>/plan_usage.json`): the real token counts the API reported and
  how far the task moved each window, measured from the reading before it to the one after.

The server reports whole percentages, so a small task may move a window by nothing; the token
counts are exact. Dollar costs elsewhere in Studio stay estimates under a subscription.

Nothing here reads your login token or calls an unpublished endpoint: the figures come only from
what the Agent SDK itself emits (`RateLimitEvent`).
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.config import Config
from orchestrator.state import atomic_write


class PlanUsageError(Exception):
    """The plan usage could not be read; the message says why in plain words."""


@dataclass(frozen=True)
class Window:
    utilization: float  # the fraction of this window used, 0.0 to 1.0 (can pass 1 on overage)
    resets_at: int | None  # when the window resets, as a Unix timestamp
    status: str  # allowed | allowed_warning | rejected


@dataclass(frozen=True)
class Reading:
    ts: datetime  # when it was read
    windows: dict[str, Window]  # five_hour, seven_day, seven_day_opus, ...
    overage_status: str | None = None  # pay-as-you-go beyond the plan, when it exists
    using_overage: bool = False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _timestamp(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def reading_from_raw(raw: dict[str, Any], *, now: datetime | None = None) -> Reading | None:
    """A reading from one rate-limit report, or None if it holds no usage figure."""
    windows: dict[str, Window] = {}
    unified = raw.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, item in unified.items():
            if not isinstance(item, dict):
                continue
            used = _number(item.get("utilization"))
            if used is not None:
                windows[str(name)] = Window(
                    used, _timestamp(item.get("resetsAt")), str(item.get("status", "allowed"))
                )
    if not windows:
        used = _number(raw.get("utilization"))
        name = raw.get("rateLimitType")
        if used is not None and isinstance(name, str):
            windows[name] = Window(
                used, _timestamp(raw.get("resetsAt")), str(raw.get("status", "allowed"))
            )
    if not windows:
        return None
    overage = raw.get("overageStatus")
    return Reading(
        ts=now or datetime.now(UTC),
        windows=windows,
        overage_status=overage if isinstance(overage, str) else None,
        using_overage=bool(raw.get("isUsingOverage")),
    )


# --- the latest reading -----------------------------------------------------------------------


def _reading_path(runs_dir: Path) -> Path:
    return runs_dir / "_studio" / "plan_windows.json"


def save_reading(runs_dir: Path, reading: Reading) -> None:
    path = _reading_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "ts": reading.ts.isoformat(),
        "overage_status": reading.overage_status,
        "using_overage": reading.using_overage,
        "windows": {
            name: {"utilization": w.utilization, "resets_at": w.resets_at, "status": w.status}
            for name, w in reading.windows.items()
        },
    }
    atomic_write(path, json.dumps(body, indent=2))


def load_reading(runs_dir: Path) -> Reading | None:
    try:
        raw = json.loads(_reading_path(runs_dir).read_text())
        windows = {
            str(name): Window(
                float(w["utilization"]),
                _timestamp(w.get("resets_at")),
                str(w.get("status", "allowed")),
            )
            for name, w in raw["windows"].items()
        }
        return Reading(
            ts=datetime.fromisoformat(raw["ts"]),
            windows=windows,
            overage_status=raw.get("overage_status"),
            using_overage=bool(raw.get("using_overage")),
        )
    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
        return None


# --- how far a task moved the windows ---------------------------------------------------------


def task_window_change(
    *, before: Reading | None, first: Reading | None, last: Reading
) -> dict[str, float]:
    """For each window, how much of it the task used (a fraction, never negative).

    The start is the reading from before the task when it is from the same window, else the first
    reading seen during the task. A window that reset in between cannot be compared, so it is left
    out rather than guessed.
    """
    change: dict[str, float] = {}
    for name, end in last.windows.items():
        for start_reading in (before, first):
            start = start_reading.windows.get(name) if start_reading else None
            if start is not None and start.resets_at == end.resets_at:
                change[name] = max(0.0, end.utilization - start.utilization)
                break
    return change


def tokens_from(usage: Any) -> dict[str, int]:
    """The token counts the API reported for a call (all zero when it reported none)."""
    source = usage if isinstance(usage, dict) else {}

    def count(key: str) -> int:
        value = source.get(key)
        return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0

    return {
        "input": count("input_tokens"),
        "output": count("output_tokens"),
        "cache_read": count("cache_read_input_tokens"),
        "cache_write": count("cache_creation_input_tokens"),
    }


# --- per task and per project -----------------------------------------------------------------


def _tasks_path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "plan_usage.json"


def _read_tasks(runs_dir: Path, project: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(_tasks_path(runs_dir, project).read_text())
    except (FileNotFoundError, ValueError):
        return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def record_task_usage(
    runs_dir: Path,
    project: str,
    *,
    task_id: str,
    model: str,
    billing: str,
    tokens: dict[str, int],
    change: dict[str, float],
    now: datetime | None = None,
) -> None:
    rows = _read_tasks(runs_dir, project)
    rows.append(
        {
            "task_id": task_id,
            "model": model,
            "billing": billing,
            "tokens": tokens,
            "change": change,
            "ts": (now or datetime.now(UTC)).isoformat(),
        }
    )
    path = _tasks_path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(rows, indent=2))


def project_usage(runs_dir: Path, project: str) -> dict[str, Any]:
    """Every recorded task of a project and their totals."""
    rows = _read_tasks(runs_dir, project)
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    change: dict[str, float] = {}
    for row in rows:
        for key in tokens:
            tokens[key] += int((row.get("tokens") or {}).get(key, 0))
        for name, value in (row.get("change") or {}).items():
            change[name] = change.get(name, 0.0) + float(value)
    return {"tasks": rows, "totals": {"tasks": len(rows), "tokens": tokens, "change": change}}


# --- checking now ------------------------------------------------------------------------------


def snapshot(
    runs_dir: Path,
    *,
    probe: Callable[[], list[dict[str, Any]]],
    now: datetime | None = None,
) -> Reading:
    """Read the windows now (the probe sends one tiny message) and keep the latest reading."""
    try:
        reports = probe()
    except (OSError, RuntimeError, ValueError) as error:
        raise PlanUsageError(f"Could not check your plan usage: {error}") from error
    latest: Reading | None = None
    for raw in reports:
        parsed = reading_from_raw(raw, now=now)
        if parsed is not None:
            latest = parsed
    if latest is None:
        raise PlanUsageError(
            "Claude Code reported no plan usage. Check that you are signed in with your "
            "Claude account (`claude auth login`) and that the Developer is set to use "
            "your subscription."
        )
    save_reading(runs_dir, latest)
    return latest


def sdk_probe(config: Config) -> Callable[[], list[dict[str, Any]]]:
    """The real probe: one tiny Claude Code call on the cheapest model, API key blanked."""

    def probe() -> list[dict[str, Any]]:
        from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, query

        async def ask() -> list[dict[str, Any]]:
            seen: list[dict[str, Any]] = []
            async for message in query(
                prompt="Reply with the single word ok.",
                options=ClaudeAgentOptions(
                    model=config.models.for_tier("fast"),
                    tools=[],
                    max_turns=1,
                    env={"ANTHROPIC_API_KEY": ""},
                ),
            ):
                if isinstance(message, RateLimitEvent):
                    seen.append(message.rate_limit_info.raw)
            return seen

        return asyncio.run(ask())

    return probe
