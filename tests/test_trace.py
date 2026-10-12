import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.state import Stage
from orchestrator.trace import EventType, TraceEvent, TraceWriter, trace_path


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_trace_path_is_inside_the_project_run_directory(tmp_path: Path) -> None:
    assert trace_path(tmp_path, "demo") == tmp_path / "demo" / "trace.jsonl"


def test_event_defaults_to_a_timezone_aware_utc_timestamp() -> None:
    event = TraceEvent(project="demo", event=EventType.TRANSITION)

    assert event.ts.tzinfo is not None
    assert event.ts.utcoffset() == UTC.utcoffset(None)


def test_write_appends_one_json_object_per_line(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, "demo")

    writer.write(TraceEvent(project="demo", event=EventType.TRANSITION, stage=Stage.INTAKE))
    writer.write(TraceEvent(project="demo", event=EventType.GATE, stage=Stage.PRODUCT_OWNER))

    events = read_lines(trace_path(tmp_path, "demo"))
    assert [e["event"] for e in events] == ["transition", "gate"]
    assert [e["stage"] for e in events] == ["intake", "product_owner"]


def test_write_creates_the_run_directory(tmp_path: Path) -> None:
    TraceWriter(tmp_path, "demo").write(TraceEvent(project="demo", event=EventType.TRANSITION))

    assert trace_path(tmp_path, "demo").is_file()


def test_a_new_writer_keeps_events_from_an_earlier_run(tmp_path: Path) -> None:
    first = TraceWriter(tmp_path, "demo")
    first.write(TraceEvent(project="demo", event=EventType.AGENT_START))

    second = TraceWriter(tmp_path, "demo")
    second.write(TraceEvent(project="demo", event=EventType.AGENT_END))

    events = read_lines(trace_path(tmp_path, "demo"))
    assert [e["event"] for e in events] == ["agent_start", "agent_end"]


def test_event_records_usage_model_and_prompt_version(tmp_path: Path) -> None:
    event = TraceEvent(
        ts=datetime(2026, 10, 12, 9, 30, tzinfo=UTC),
        project="demo",
        stage=Stage.ARCHITECT,
        event=EventType.AGENT_END,
        agent="architect",
        model="strong-model-id",
        prompt_version="architect-v1",
        tokens_in=1200,
        tokens_out=800,
        cost_usd=0.42,
        latency_ms=5300,
    )

    TraceWriter(tmp_path, "demo").write(event)

    [line] = read_lines(trace_path(tmp_path, "demo"))
    assert line["ts"] == "2026-10-12T09:30:00Z"
    assert line["agent"] == "architect"
    assert line["model"] == "strong-model-id"
    assert line["prompt_version"] == "architect-v1"
    assert line["tokens_in"] == 1200
    assert line["tokens_out"] == 800
    assert line["cost_usd"] == 0.42
    assert line["latency_ms"] == 5300


def test_unset_fields_are_written_as_null_so_every_line_has_the_same_keys(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, "demo")
    writer.write(TraceEvent(project="demo", event=EventType.TRANSITION))
    writer.write(TraceEvent(project="demo", event=EventType.AGENT_END, agent="qa", cost_usd=0.1))

    first, second = read_lines(trace_path(tmp_path, "demo"))
    assert first.keys() == second.keys()
    assert first["cost_usd"] is None


def test_event_specific_details_are_kept(tmp_path: Path) -> None:
    event = TraceEvent(
        project="demo",
        event=EventType.TOOL_DENIED,
        details={"tool": "Bash", "command": "git push --force origin main"},
    )

    TraceWriter(tmp_path, "demo").write(event)

    [line] = read_lines(trace_path(tmp_path, "demo"))
    assert line["details"] == {"tool": "Bash", "command": "git push --force origin main"}


def test_newlines_in_text_do_not_break_the_one_event_per_line_format(tmp_path: Path) -> None:
    event = TraceEvent(
        project="demo",
        event=EventType.GATE,
        details={"comments": "line one\nline two"},
    )

    TraceWriter(tmp_path, "demo").write(event)

    lines = trace_path(tmp_path, "demo").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["details"]["comments"] == "line one\nline two"


def test_rejects_negative_usage_numbers() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(project="demo", event=EventType.AGENT_END, cost_usd=-0.01)
    with pytest.raises(ValidationError):
        TraceEvent(project="demo", event=EventType.AGENT_END, tokens_in=-1)


def test_rejects_an_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        TraceEvent.model_validate({"project": "demo", "event": "made_up"})


def test_rejects_an_unsafe_project_slug() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(project="../escape", event=EventType.TRANSITION)
