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
    assert [r.state for r in running.stages] == ["done", "done", "current", "upcoming", "upcoming"]
    assert [r.state for r in waiting.stages] == ["done", "done", "done", "waiting", "upcoming"]


def test_a_finished_project_shows_every_stage_done(runs: Path) -> None:
    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE)

    view = data.project_view(runs, make_config(), "demo")

    assert view is not None and {r.state for r in view.stages} == {"done"}


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


# --- the pages -------------------------------------------------------------------------------


def test_the_front_page_lists_projects_with_their_state_and_spend(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, stage=Stage.ARCHITECT, cost_usd={Stage.ARCHITECT: 1.5})

    page = client.get("/").text

    assert 'href="/p/demo"' in page and "architect" in page and "$1.50 of $20" in page


def test_the_front_page_has_a_message_when_there_are_no_projects(client: TestClient) -> None:
    assert "No projects yet" in client.get("/").text


def test_the_project_page_shows_the_timeline_spend_decisions_and_documents(
    client: TestClient, runs: Path
) -> None:
    make_project(
        runs,
        artifacts={"prd.md": "# PRD"},
        stage=Stage.DEVELOPER,
        cost_usd={Stage.DEVELOPER: 1.0},
        gate_history=[
            GateRecord(
                stage=Stage.ARCHITECT, outcome=GateOutcome.APPROVE, comments="looks good", ts=NOW
            )
        ],
    )

    page = client.get("/p/demo").text

    for label in ("Product Owner", "Architect", "Developer", "QA", "Career coach"):
        assert label in page
    assert "$1.00 of $" in page and "looks good" in page and 'href="/p/demo/a/prd.md"' in page
    assert "80% warning" in page


def test_a_project_waiting_for_the_human_says_what_for(client: TestClient, runs: Path) -> None:
    make_project(
        runs,
        status=Status.AWAITING_HUMAN,
        stage=Stage.QA,
        intervention=Intervention(kind=InterventionKind.GATE, detail="Review the qa output"),
    )

    assert "Review the qa output" in client.get("/p/demo").text


def test_a_run_that_is_not_active_says_how_to_resume_it(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)  # running status, but no heartbeat

    page = client.get("/p/demo").text

    assert "not active" in page and "studio resume demo --agent full --ui" in page


def test_an_artifact_is_shown_rendered_and_a_script_in_it_is_not_run(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, artifacts={"prd.md": "# PRD\n\n<script>alert(1)</script>"})

    page = client.get("/p/demo/a/prd.md").text

    assert "<h1>PRD</h1>" in page and "<script>alert" not in page and "approved" in page


def test_a_json_artifact_is_pretty_printed_and_escaped(client: TestClient, runs: Path) -> None:
    make_project(runs, artifacts={"x.json": '{"a": "<b>"}'})

    page = client.get("/p/demo/a/x.json").text

    assert "&lt;b&gt;" in page and "<b>" not in page


def test_only_documents_the_project_recorded_can_be_opened(client: TestClient, runs: Path) -> None:
    make_project(runs, artifacts={"prd.md": "# PRD"})

    assert client.get("/p/demo/a/nope.md").status_code == 404
    assert client.get("/p/demo/a/../state.json").status_code == 404
    assert client.get("/p/demo/a/%2e%2e/state.json").status_code == 404


def test_unknown_or_unsafe_project_names_are_not_found(client: TestClient, runs: Path) -> None:
    make_project(runs)

    assert client.get("/p/nobody").status_code == 404
    assert client.get("/p/%2e%2e").status_code == 404
    assert client.get("/p/Bad_Name").status_code == 404


def test_the_trace_page_lists_events_and_filters_by_type(client: TestClient, runs: Path) -> None:
    make_project(runs)
    trace(
        runs,
        "demo",
        event(
            EventType.AGENT_END,
            agent="architect",
            model="opus",
            prompt_version="v2",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.1234,
            latency_ms=2500,
        ),
        event(EventType.GATE, details={"outcome": "approve"}),
    )

    everything = client.get("/p/demo/trace").text
    only_gates = client.get("/p/demo/trace?type=gate").text

    assert "opus" in everything and "100 / 50" in everything and "$0.1234" in everything
    assert "2.5s" in everything and "approve" in everything
    assert "opus" not in only_gates and "approve" in only_gates
    assert client.get("/p/demo/trace?type=nonsense").status_code == 404


def test_requests_with_a_foreign_host_header_are_refused(client: TestClient, runs: Path) -> None:
    make_project(runs)

    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_the_stylesheet_is_served_and_the_api_docs_are_off(client: TestClient) -> None:
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


# --- the command -----------------------------------------------------------------------------


def test_the_ui_command_serves_on_this_machine_only(
    runs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_the_ui_command_stops_early_without_a_config(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["ui", "--config", str(tmp_path / "nope.yaml")])

    assert result.exit_code == 1 and "Config file not found" in result.output
