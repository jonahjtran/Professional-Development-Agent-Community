"""What each agent is doing, worked out from facts the system already records.

Nothing here calls a model: the status comes from the saved state, the one-line "doing" from the
latest trace events (tool calls put into plain words), the agenda from `tasks.json`, and the notes
from what the agents reported. So the picture is always as true as the records.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orchestrator.artifacts import ArtifactStore
from orchestrator.developer_backend import read_running_model
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.trace import EventType, TraceEvent, trace_path
from schemas import TaskList

STAGES = (Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER, Stage.QA, Stage.CAREER_COACH)
LABELS = {
    Stage.PRODUCT_OWNER: "Product Owner",
    Stage.ARCHITECT: "Architect",
    Stage.DEVELOPER: "Developer",
    Stage.QA: "QA",
    Stage.CAREER_COACH: "Career coach",
}
_WRITES = {
    Stage.PRODUCT_OWNER: ("Writing the product requirements", "Wrote the product requirements"),
    Stage.ARCHITECT: ("Designing the architecture and task list", "Designed the architecture"),
    Stage.DEVELOPER: ("Building the tasks", "Built the tasks"),
    Stage.QA: ("Running the checks and judging the criteria", "Checked the finished project"),
    Stage.CAREER_COACH: ("Writing career material", "Wrote the career material"),
}
_RECENT = 8


@dataclass(frozen=True)
class AgentActivity:
    stage: str
    name: str
    status: str  # done | working | waiting_for_you | paused | stopped | upcoming
    doing: str
    waiting_on: str | None = None
    task: dict[str, str] | None = None
    agenda: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    model: str | None = None  # the exact model id
    backend: str | None = None  # claude | local
    model_source: str | None = None  # running | used | configured


def humanize(tool: str, summary: str) -> str:
    """One tool call in plain words. The command text is never shown, only what kind of work."""
    name = os.path.basename(summary.split("\n", 1)[0].strip())
    if tool == "Edit":
        return f"Editing {name}"
    if tool == "Write":
        return f"Writing {name}"
    if tool == "Read":
        return f"Reading {name}"
    if tool in ("Grep", "Glob"):
        return "Searching the code"
    if tool != "Bash":
        return "Working"
    words = summary.replace("|", " ; ").split()
    first = words[0] if words else ""
    text = " ".join(words)
    if (
        "vitest" in text
        or "pytest" in text
        or "jest" in text
        or text.startswith(("npm test", "npm run test"))
    ):
        return "Running tests"
    if "tsc" in text.split() or "mypy" in text:
        return "Checking types"
    if "lint" in text or "eslint" in text or "ruff" in text or "prettier" in text:
        return "Checking style"
    if text.startswith(("npm run build", "npx next build")) or " build" in text[:20]:
        return "Building the project"
    if first == "git":
        if "commit" in words[:3]:
            return "Committing work"
        return "Checking what changed"
    if text.startswith(("npm install", "npm ci", "pnpm", "yarn", "pip install", "uv sync")):
        return "Installing dependencies"
    if first in ("ls", "cat", "pwd", "find", "head", "tail", "tree", "stat", "wc", "echo"):
        return "Looking around the project"
    return "Running a command"


def _events(runs_dir: Path, slug: str, stage: Stage) -> list[TraceEvent]:
    """This stage's tool events, newest first. Unreadable lines are skipped."""
    path = trace_path(runs_dir, slug)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    found: list[TraceEvent] = []
    for line in reversed(lines):
        try:
            event = TraceEvent.model_validate_json(line)
        except ValidationError:
            continue
        if event.stage == stage and event.event in (EventType.TOOL_CALL, EventType.TOOL_DENIED):
            found.append(event)
            if len(found) >= _RECENT:
                break
    return found


def _recent(events: list[TraceEvent]) -> list[str]:
    out: list[str] = []
    for event in events:
        details = event.details or {}
        tool, summary = str(details.get("tool", "")), str(details.get("summary", ""))
        if event.event == EventType.TOOL_DENIED:
            out.append(f"Was refused: {summary}")
        else:
            out.append(humanize(tool, summary))
    return out


def _last_model(runs_dir: Path, slug: str, stage: Stage) -> str | None:
    """The model that answered the stage's most recent agent call (as the API reported it)."""
    try:
        lines = trace_path(runs_dir, slug).read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = TraceEvent.model_validate_json(line)
        except ValidationError:
            continue
        if event.stage == stage and event.event == EventType.AGENT_END and event.model:
            return event.model
    return None


def _model_info(
    runs_dir: Path,
    state: ProjectState,
    stage: Stage,
    configured: dict[str, tuple[str, str]] | None,
    local_model: str | None,
) -> tuple[str | None, str | None, str | None]:
    """(model id, backend, where it came from) for one agent."""
    if stage == Stage.DEVELOPER:
        running = read_running_model(runs_dir, state.project_slug)
        if running and running[0] == state.current_task_id:
            return running[2], running[1], "running"
    used = _last_model(runs_dir, state.project_slug, stage)
    if used is not None:
        return used, "local" if local_model and used == local_model else "claude", "used"
    if configured and stage.value in configured:
        model, backend = configured[stage.value]
        return model, backend, "configured"
    return None, None, None


def _tasks(runs_dir: Path, state: ProjectState) -> TaskList | None:
    record = state.artifacts.get("tasks.json")
    if record is None:
        return None
    try:
        return TaskList.model_validate_json(
            ArtifactStore(runs_dir, state.project_slug).read(record)
        )
    except (ValidationError, ValueError, OSError):
        return None


def _pr_urls(runs_dir: Path, state: ProjectState) -> dict[str, str]:
    store = ArtifactStore(runs_dir, state.project_slug)
    urls: dict[str, str] = {}
    for name, record in state.artifacts.items():
        if not name.startswith("dev/"):
            continue
        try:
            report = json.loads(store.read(record))
            if report.get("pr_url"):
                urls[str(report["task_id"])] = str(report["pr_url"])
        except (ValueError, KeyError, OSError, TypeError):
            continue
    return urls


def _agenda(runs_dir: Path, state: ProjectState, tasks: TaskList | None) -> list[dict[str, Any]]:
    if tasks is None:
        return []
    done = set(state.completed_tasks)
    urls = _pr_urls(runs_dir, state)
    rows: list[dict[str, Any]] = []
    for task in tasks.tasks:
        unmet = [d for d in task.depends_on if d not in done]
        if task.id in done:
            status = "done"
        elif task.id == state.current_task_id:
            status = "working"
        elif unmet:
            status = "waiting"
        else:
            status = "ready"
        rows.append(
            {
                "id": task.id,
                "title": task.title,
                "state": status,
                "waiting_on": unmet if status == "waiting" else [],
                "pr_url": urls.get(task.id),
            }
        )
    return rows


def _notes(runs_dir: Path, state: ProjectState, stage: Stage) -> list[str]:
    if stage != Stage.DEVELOPER:
        return []
    store = ArtifactStore(runs_dir, state.project_slug)
    notes: list[str] = []
    for name in sorted(n for n in state.artifacts if n.startswith("dev/")):
        try:
            report = json.loads(store.read(state.artifacts[name]))
            notes.append(f"{report['task_id']}: {report['summary']}")
            notes += [f"Deviation: {d}" for d in report.get("deviations_from_plan", [])]
            notes += [f"Gap: {g}" for g in report.get("known_gaps", [])]
            notes += [f"Needs you: {n}" for n in report.get("needs_from_human", [])]
        except (ValueError, KeyError, OSError, TypeError):
            continue
    return notes


def project_activity(
    runs_dir: Path,
    state: ProjectState,
    *,
    alive: bool,
    configured: dict[str, tuple[str, str]] | None = None,
    local_model: str | None = None,
) -> list[AgentActivity]:
    tasks = _tasks(runs_dir, state)
    order = {s: n for n, s in enumerate(STAGES)}
    current = order.get(state.stage)
    people: list[AgentActivity] = []
    for stage in STAGES:
        label = LABELS[stage]
        is_developer = stage == Stage.DEVELOPER
        events = _events(runs_dir, state.project_slug, stage)
        recent = _recent(events)
        model, backend, source = _model_info(runs_dir, state, stage, configured, local_model)
        common: dict[str, Any] = {
            "stage": stage.value,
            "name": label,
            "model": model,
            "backend": backend,
            "model_source": source,
            "agenda": _agenda(runs_dir, state, tasks) if is_developer else [],
            "notes": _notes(runs_dir, state, stage),
        }
        running, finished = _WRITES[stage]
        if state.status == Status.DONE or (current is not None and order[stage] < current):
            people.append(
                AgentActivity(
                    status="done", doing=f"Finished: {finished.lower()}", recent=recent, **common
                )
            )
            continue
        if current is None or order[stage] > current:
            before = LABELS[STAGES[order[stage] - 1]] if order[stage] > 0 else None
            doing = f"Waiting for the {before} to finish" if before else "Waiting to start"
            people.append(
                AgentActivity(status="upcoming", doing=doing, waiting_on=before, **common)
            )
            continue
        task = None
        if is_developer and state.current_task_id and tasks is not None:
            match = next((t for t in tasks.tasks if t.id == state.current_task_id), None)
            if match is not None:
                task = {"id": match.id, "title": match.title}
        if state.status == Status.HALTED:
            status, doing = "paused", "Paused: nothing is running and nothing is being spent"
        elif state.status == Status.AWAITING_HUMAN:
            detail = state.intervention.detail if state.intervention else "your decision"
            status, doing = "waiting_for_you", f"Waiting for you: {detail}"
        elif not alive:
            status, doing = "stopped", "Not running: Resume to continue"
        else:
            status = "working"
            step = recent[0] if recent else running
            doing = f"{task['id']}: {task['title']}. {step}" if task else step
            if task is not None and not recent:
                doing = f"{task['id']}: {task['title']}"
        people.append(AgentActivity(status=status, doing=doing, task=task, recent=recent, **common))
    return people
