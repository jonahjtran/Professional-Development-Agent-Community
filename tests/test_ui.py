import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.cli import app as cli_app
from orchestrator.inbox import Inbox, PendingKind
from orchestrator.state import (
    Allowances,
    GateOutcome,
    GateRecord,
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    save_state,
)
from orchestrator.trace import EventType, TraceEvent, TraceWriter
from orchestrator.ui import data
from orchestrator.ui.app import create_app
from orchestrator.ui.render import artifact_kind, markdown_html, pretty_json
from schemas import DevResult
from tests.factories import make_config

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def make_project(
    runs_dir: Path, slug: str = "demo", artifacts: dict[str, str] | None = None, **fields: Any
) -> ProjectState:
    store = ArtifactStore(runs_dir, slug)
    state = ProjectState(project_slug=slug, idea="A tool that tracks student org events.")
    written = {n: store.write(n, t, revision=1) for n, t in (artifacts or {}).items()}
    state = approve_artifacts(record_artifacts(state, written), list(written), now=NOW)
    state = state.evolve(**fields)
    save_state(runs_dir, state)
    return state


def trace(runs_dir: Path, slug: str, *events: TraceEvent) -> None:
    writer = TraceWriter(runs_dir, slug)
    for event in events:
        writer.write(event)


def event(kind: EventType, **fields: Any) -> TraceEvent:
    return TraceEvent(project="demo", event=kind, ts=NOW, **fields)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def client(runs: Path) -> TestClient:
    runs.mkdir(exist_ok=True)
    return TestClient(create_app(runs, make_config()))


# --- the data --------------------------------------------------------------------------------


def test_only_folders_with_a_readable_state_are_projects(runs: Path) -> None:
    make_project(runs, "alpha")
    (runs / "notes").mkdir()
    (runs / "broken").mkdir()
    (runs / "broken" / "state.json").write_text("{not json")

    assert [p.slug for p in data.list_projects(runs, make_config())] == ["alpha"]


def test_projects_that_need_you_come_first_and_count_their_questions(runs: Path) -> None:
    make_project(runs, "quiet")
    make_project(runs, "asking")
    Inbox(runs, "asking", poll_seconds=0.01)  # no question yet
    folder = runs / "asking" / "pending"
    folder.mkdir(parents=True)
    pending = {
        "id": "a" * 12,
        "project": "asking",
        "kind": PendingKind.GATE.value,
        "created": NOW.isoformat(),
        "request": {},
    }
    (folder / f"{'a' * 12}.json").write_text(json.dumps(pending))

    projects = data.list_projects(runs, make_config())

    assert [(p.slug, p.pending) for p in projects] == [("asking", 1), ("quiet", 0)]


def test_the_summary_carries_the_status_what_it_waits_for_and_the_cap(runs: Path) -> None:
    make_project(
        runs,
        stage=Stage.ARCHITECT,
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail="Review the architecture"),
        cost_usd={Stage.PRODUCT_OWNER: 0.5, Stage.ARCHITECT: 0.75},
        allowances=Allowances(extra_project_usd=5.0),
    )

    [summary] = data.list_projects(runs, make_config())

    assert summary.detail == "Review the architecture"
    assert summary.cost_usd == pytest.approx(1.25) and summary.cap_usd == 25.0


def test_the_timeline_marks_done_current_waiting_and_upcoming_stages(runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    running = data.project_view(runs, make_config(), "demo")
    make_project(
        runs,
        stage=Stage.QA,
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail="x"),
    )
    waiting = data.project_view(runs, make_config(), "demo")

    assert running is not None and waiting is not None
    # an agent that has finished is "finished", not "done": the project may still go back to it
    assert [r.state for r in running.stages] == [
        "finished",
        "finished",
        "current",
        "upcoming",
        "upcoming",
    ]
    assert [r.state for r in waiting.stages] == [
        "finished",
        "finished",
        "finished",
        "waiting",
        "upcoming",
    ]


def test_a_finished_project_shows_every_stage_done(runs: Path) -> None:
    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE)

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None and {r.state for r in view.stages} == {"done"}


def visit(stage: Stage, reason: str = "next", came_from: Stage | None = None) -> Any:
    from orchestrator.state import StageVisit

    return StageVisit(stage=stage, reason=reason, ts=NOW, came_from=came_from)


def test_an_agent_the_project_went_back_to_shows_how_many_times_it_has_been_visited(
    runs: Path,
) -> None:
    make_project(
        runs,
        stage=Stage.DEVELOPER,
        path=[
            visit(Stage.PRODUCT_OWNER),
            visit(Stage.ARCHITECT),
            visit(Stage.DEVELOPER),
            visit(Stage.QA),
            visit(Stage.ARCHITECT, "QA failed", Stage.QA),
            visit(Stage.DEVELOPER, "QA failed: fix round 1", Stage.ARCHITECT),
        ],
    )

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None
    rows = {r.stage: r for r in view.stages}
    assert rows[Stage.ARCHITECT].visits == 2 and rows[Stage.ARCHITECT].returned
    assert rows[Stage.PRODUCT_OWNER].visits == 1 and not rows[Stage.PRODUCT_OWNER].returned
    # QA finished once and will be needed again: it is upcoming, and it remembers its visit
    assert rows[Stage.QA].state == "upcoming" and rows[Stage.QA].visits == 1


def test_the_journey_lists_every_move_with_its_direction_and_why(runs: Path) -> None:
    make_project(
        runs,
        stage=Stage.ARCHITECT,
        path=[
            visit(Stage.PRODUCT_OWNER),
            visit(Stage.ARCHITECT),
            visit(Stage.DEVELOPER),
            visit(Stage.QA),
            visit(Stage.ARCHITECT, "QA failed", Stage.QA),
        ],
    )

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None
    steps = view.journey.path
    assert [(s.label, s.direction) for s in steps][-2:] == [
        ("QA", "forward"),
        ("Architect", "back"),
    ]
    assert view.journey.going_back is not None
    assert view.journey.going_back.reason == "QA failed"
    assert view.journey.going_back.came_from == "QA"


def test_a_project_that_never_went_back_has_no_going_back_notice(runs: Path) -> None:
    make_project(
        runs, stage=Stage.ARCHITECT, path=[visit(Stage.PRODUCT_OWNER), visit(Stage.ARCHITECT)]
    )

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None and view.journey.going_back is None


def test_stage_spend_is_shown_against_its_cap_including_extra_the_human_granted(runs: Path) -> None:
    make_project(
        runs,
        stage=Stage.ARCHITECT,
        cost_usd={Stage.ARCHITECT: 2.0},
        allowances=Allowances(extra_stage_usd={Stage.ARCHITECT: 1.0}),
    )

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None
    row = next(r for r in view.stages if r.stage == Stage.ARCHITECT)
    assert (row.spent, row.cap) == (2.0, 3.5) and row.fraction == pytest.approx(2.0 / 3.5)


def test_decisions_are_listed_newest_first(runs: Path) -> None:
    history = [
        GateRecord(
            stage=Stage.PRODUCT_OWNER, outcome=GateOutcome.APPROVE, comments="first", ts=NOW
        ),
        GateRecord(stage=Stage.ARCHITECT, outcome=GateOutcome.APPROVE, comments="second", ts=NOW),
    ]
    make_project(runs, gate_history=history)

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None and [g.comments for g in view.gates] == ["second", "first"]


def test_pull_requests_are_collected_from_every_place_they_are_recorded(runs: Path) -> None:
    dev = DevResult(
        task_id="T1",
        branch="agent/T1",
        pr_url="https://github.com/me/p/pull/2",
        summary="s",
        deviations_from_plan=[],
        known_gaps=[],
    )
    make_project(
        runs,
        artifacts={
            "design_record.json": json.dumps({"pr_url": "https://github.com/me/p/pull/1"}),
            "dev/T1.json": dev.model_dump_json(),
            "career_published.json": json.dumps(
                {"website": "https://github.com/me/site/pull/3", "settings": "failed: no rights"}
            ),
        },
        completed_tasks=["T1"],
    )

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None
    assert [(link.label, link.url) for link in view.links] == [
        ("Design record", "https://github.com/me/p/pull/1"),
        ("Task T1", "https://github.com/me/p/pull/2"),
        ("website", "https://github.com/me/site/pull/3"),
    ]


def test_the_trace_is_newest_first_filterable_and_skips_damaged_lines(runs: Path) -> None:
    make_project(runs)
    trace(
        runs,
        "demo",
        event(EventType.AGENT_START, agent="architect"),
        event(EventType.AGENT_END, agent="architect", cost_usd=0.3),
        event(EventType.GATE),
    )
    with (runs / "demo" / "trace.jsonl").open("a") as f:
        f.write("not json\n")

    assert [e.event for e in data.read_trace(runs, "demo")] == [
        EventType.GATE,
        EventType.AGENT_END,
        EventType.AGENT_START,
    ]
    assert [e.event for e in data.read_trace(runs, "demo", event=EventType.AGENT_END)] == [
        EventType.AGENT_END
    ]
    assert len(data.read_trace(runs, "demo", limit=2)) == 2
    assert data.read_trace(runs, "nobody") == []


# --- rendering safely ------------------------------------------------------------------------


def test_markdown_is_rendered_but_raw_html_is_escaped() -> None:
    html = markdown_html(
        "# Title\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "<h1>Title</h1>" in html and "<table>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_json_is_pretty_printed_and_other_text_is_left_alone() -> None:
    assert pretty_json('{"a":1}') == '{\n  "a": 1\n}'
    assert pretty_json("not json") == "not json"
    assert artifact_kind("prd.md") == "markdown" and artifact_kind("tasks.json") == "code"


# --- the command -----------------------------------------------------------------------------


def built_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    monkeypatch.setattr("orchestrator.ui.app.FRONTEND", dist)


def test_the_ui_command_serves_on_this_machine_only(
    runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built_app(tmp_path, monkeypatch)
    served: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(kw))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    config = Path(__file__).parent.parent / "config.yaml"

    result = CliRunner().invoke(
        cli_app,
        ["ui", "--config", str(config), "--runs-dir", str(runs), "--no-open", "--port", "9123"],
    )

    assert result.exit_code == 0, result.output
    assert served["host"] == "127.0.0.1" and served["port"] == 9123
    assert "http://127.0.0.1:9123" in result.output and opened == []


def test_the_ui_command_says_how_to_build_the_app_when_it_is_missing(
    runs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator.ui.app.FRONTEND", tmp_path / "nothing")
    config = Path(__file__).parent.parent / "config.yaml"

    result = CliRunner().invoke(cli_app, ["ui", "--config", str(config), "--runs-dir", str(runs)])

    assert result.exit_code == 1
    assert "npm --prefix frontend ci && npm --prefix frontend run build" in result.output


def test_the_ui_command_stops_early_without_a_config(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["ui", "--config", str(tmp_path / "nope.yaml")])

    assert result.exit_code == 1 and "Config file not found" in result.output
