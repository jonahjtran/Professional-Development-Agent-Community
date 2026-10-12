import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.state import Slug, Stage


class EventType(StrEnum):
    AGENT_START = "agent_start"
    TOOL_CALL = "tool_call"
    TOOL_DENIED = "tool_denied"
    AGENT_END = "agent_end"
    GATE = "gate"
    TRANSITION = "transition"


class TraceEvent(BaseModel):
    """One line of trace.jsonl. Unset fields are written as null so every line has the same keys."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    project: Slug
    stage: Stage | None = None
    event: EventType
    agent: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    tokens_in: Annotated[int, Field(ge=0)] | None = None
    tokens_out: Annotated[int, Field(ge=0)] | None = None
    cost_usd: Annotated[float, Field(ge=0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


def trace_path(runs_dir: Path, slug: str) -> Path:
    return runs_dir / slug / "trace.jsonl"


class TraceWriter:
    """Append-only writer for a project's trace.jsonl. Existing lines are never rewritten."""

    def __init__(self, runs_dir: Path, slug: str) -> None:
        self._path = trace_path(runs_dir, slug)

    def write(self, event: TraceEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # model_dump_json escapes newlines, so one event is always exactly one line.
        line = event.model_dump_json() + "\n"
        with self._path.open("a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
