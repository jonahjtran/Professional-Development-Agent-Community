import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.cli import AgentKind, _context
from orchestrator.halt import halt_requested
from orchestrator.inbox import PendingKind, all_pending, answered, run_marker
from orchestrator.pipeline import run_pipeline
from orchestrator.state import (
    GateOutcome,
    GateRecord,
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
)
from orchestrator.trace import EventType
from orchestrator.ui.app import create_app
from tests.factories import make_config
from tests.test_ui import NOW, event, make_project, trace
from tests.test_ui_actions import CONFIG, GATE, Launched, write_pending
from tests.test_ui_career import CAREER_GATE, project_at_career
from tests.test_ui_start import Creator, env_for, run_all


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def launched() -> Launched:
    return Launched()


@pytest.fixture
def creator() -> Creator:
    return Creator()


@pytest.fixture
def client(
    runs: Path,
    launched: Launched,
    creator: Creator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    for key, value in env_for(tmp_path).items():
        monkeypatch.setenv(key, value)
    app = create_app(
        runs,
        make_config(),
        config_path=CONFIG,
        launcher=launched,
        projects_dir=tmp_path / "clones",
        creator=creator,
        health=lambda: run_all(tmp_path),
    )
    return TestClient(app)


class FakeExplainer:
    def __init__(self, result: Any = None) -> None:
        from orchestrator.explain import ExplainResult, ToolExplanation

        self.calls: list[Any] = []
        self.result = result or ExplainResult(
            ToolExplanation(
                what="Installs packages.",
                why="The task needs them.",
                effect="Writes node_modules.",
                risk="low",
                unsure=False,
            ),
            None,
            0.001,
        )

    def explain(self, item: Any) -> Any:
        self.calls.append(item)
        return self.result


@pytest.fixture
def explainer() -> FakeExplainer:
    return FakeExplainer()


@pytest.fixture
def explaining_client(runs: Path, tmp_path: Path, explainer: FakeExplainer) -> TestClient:
    app = create_app(
        runs,
        make_config(),
        config_path=CONFIG,
        projects_dir=tmp_path / "clones",
        explainer=explainer,
        health=lambda: run_all(tmp_path),
    )
    return TestClient(app)


def csrf(client: TestClient) -> str:
    return str(client.get("/api/session").json()["csrf"])


def post(client: TestClient, url: str, body: dict[str, Any] | None = None, **headers: str) -> Any:
    base = {"x-csrf-token": csrf(client)}
    return client.post(url, json=body or {}, headers={**base, **headers})


def question_url(slug: str, qid: str) -> str:
    return f"/api/projects/{slug}/questions/{qid}"


# --- reading projects ------------------------------------------------------------------------


def test_projects_are_listed_with_their_state_and_those_needing_you_come_first(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, "quiet", stage=Stage.ARCHITECT, cost_usd={Stage.ARCHITECT: 1.5})
    make_project(runs, "asking")
    write_pending(runs, PendingKind.GATE, GATE, slug="asking")

    listed = client.get("/api/projects").json()

    assert [p["slug"] for p in listed] == ["asking", "quiet"]
    assert listed[0]["pending"] == 1
    assert listed[1]["stage"] == "architect" and listed[1]["cost_usd"] == 1.5
    assert listed[1]["cap_usd"] == 20.0 and listed[1]["alive"] is False


def test_no_projects_is_an_empty_list(client: TestClient) -> None:
    assert client.get("/api/projects").json() == []


def test_a_project_has_its_timeline_spend_decisions_documents_and_open_questions(
    client: TestClient, runs: Path
) -> None:
    make_project(
        runs,
        artifacts={"prd.md": "# PRD"},
        stage=Stage.QA,
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail="Review the qa output"),
        cost_usd={Stage.QA: 1.0},
        gate_history=[
            GateRecord(
                stage=Stage.ARCHITECT, outcome=GateOutcome.APPROVE, comments="first", ts=NOW
            ),
            GateRecord(stage=Stage.QA, outcome=GateOutcome.APPROVE, comments="second", ts=NOW),
        ],
    )
    qid = write_pending(runs, PendingKind.GATE, GATE)

    project = client.get("/api/projects/demo").json()

    assert project["summary"]["detail"] == "Review the qa output"
    assert [s["state"] for s in project["stages"]] == [
        "done",
        "done",
        "done",
        "waiting",
        "upcoming",
    ]
    qa = project["stages"][3]
    assert (qa["label"], qa["spent"], qa["cap"]) == ("QA", 1.0, 2.5)
    assert project["warn_fraction"] == 0.8
    assert project["helper"] == {"explain_usd": 0.0, "chat_usd": 0.0}
    assert [g["comments"] for g in project["gates"]] == ["second", "first"]
    assert project["artifacts"] == ["prd.md"]
    [waiting] = project["pending"]
    assert waiting["id"] == qid and waiting["title"] == "Review: Review the qa output"


def test_a_run_marked_running_without_a_heartbeat_is_reported_as_not_alive(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    assert client.get("/api/projects/demo").json()["summary"]["alive"] is False

    with run_marker(runs, "demo", beat_seconds=60):
        assert client.get("/api/projects/demo").json()["summary"]["alive"] is True


def test_a_project_that_is_still_starting_shows_its_log(client: TestClient, runs: Path) -> None:
    (runs / "fresh").mkdir()
    (runs / "fresh" / "run.log").write_text("Starting...\nline two\n")

    started = client.get("/api/projects/fresh").json()

    assert started == {"starting": True, "slug": "fresh", "log": "Starting...\nline two"}
    assert client.get("/api/projects/never-started").status_code == 404


def test_unknown_or_unsafe_project_names_are_not_found_as_json(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)

    for bad in ("nobody", "%2e%2e", "Bad_Name"):
        response = client.get(f"/api/projects/{bad}")
        assert response.status_code == 404 and "error" in response.json(), bad


# --- documents and the trace -----------------------------------------------------------------


def test_a_markdown_document_is_sent_rendered_with_raw_html_escaped(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, artifacts={"prd.md": "# PRD\n\n<script>alert(1)</script>"})

    doc = client.get("/api/projects/demo/artifacts/prd.md").json()

    assert doc["kind"] == "markdown" and "<h1>PRD</h1>" in doc["html"]
    assert "<script>" not in doc["html"] and "&lt;script&gt;" in doc["html"]
    assert doc["revision"] == 1 and doc["approved_at"] is not None and doc["text"] is None


def test_a_json_document_is_sent_pretty_printed(client: TestClient, runs: Path) -> None:
    make_project(runs, artifacts={"x.json": '{"a":"<b>"}'})

    doc = client.get("/api/projects/demo/artifacts/x.json").json()

    assert doc["kind"] == "code" and doc["text"] == '{\n  "a": "<b>"\n}' and doc["html"] is None


def test_only_documents_the_project_recorded_can_be_read(client: TestClient, runs: Path) -> None:
    make_project(runs, artifacts={"prd.md": "# PRD"})

    for name in ("nope.md", "../state.json", "%2e%2e/state.json"):
        assert client.get(f"/api/projects/demo/artifacts/{name}").status_code == 404, name


def test_the_trace_lists_newest_first_and_filters_by_type(client: TestClient, runs: Path) -> None:
    make_project(runs)
    trace(
        runs,
        "demo",
        event(EventType.AGENT_END, agent="architect", model="opus", tokens_in=100, cost_usd=0.12),
        event(EventType.GATE, details={"outcome": "approve"}),
    )

    everything = client.get("/api/projects/demo/trace").json()
    gates = client.get("/api/projects/demo/trace?type=gate").json()

    assert [e["event"] for e in everything["events"]] == ["gate", "agent_end"]
    assert (
        everything["events"][1]["model"] == "opus" and EventType.GATE.value in everything["types"]
    )
    assert [e["event"] for e in gates["events"]] == ["gate"] and gates["selected"] == "gate"
    assert client.get("/api/projects/demo/trace?type=nonsense").status_code == 404


# --- questions -------------------------------------------------------------------------------


def test_the_inbox_lists_every_open_question(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    [item] = client.get("/api/inbox").json()

    assert item["id"] == qid and item["project"] == "demo" and item["kind"] == "gate"
    assert item["title"] == "Review: Review the qa output"


def test_a_gate_question_has_its_summary_items_and_questions_and_the_summary_is_escaped(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    detail = client.get(question_url("demo", qid)).json()

    assert detail["status"] == "open" and detail["kind"] == "gate"
    assert "<h1>Report</h1>" in detail["summary_html"] and "<script>" not in detail["summary_html"]
    assert detail["request"]["items"] == [{"id": "readme", "label": "Replace README"}]
    assert detail["request"]["questions"] == ["How many users?"] and detail["workbench"] is None


def test_each_kind_of_question_carries_what_its_form_needs(client: TestClient, runs: Path) -> None:
    make_project(runs)
    cap = write_pending(
        runs,
        PendingKind.CAP,
        {
            "kind": "budget_cap",
            "stage": "developer",
            "detail": "80% of the cap",
            "can_approve_anyway": True,
            "unit": "usd",
        },
    )
    merge = write_pending(runs, PendingKind.MERGE, {"pr_urls": ["https://github.com/me/p/pull/1"]})
    tool = write_pending(
        runs, PendingKind.TOOL, {"tool": "Bash", "summary": "pip install x", "reason": "r"}
    )

    assert client.get(question_url("demo", cap)).json()["request"]["can_approve_anyway"] is True
    assert client.get(question_url("demo", merge)).json()["request"]["pr_urls"] == [
        "https://github.com/me/p/pull/1"
    ]
    assert client.get(question_url("demo", tool)).json()["request"]["tool"] == "Bash"


def test_a_closed_question_is_reported_answered_or_not_found(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    assert client.get(question_url("demo", "aaaaaaaaaaaa")).status_code == 404

    done = runs / "demo" / "done"
    done.mkdir(parents=True)
    (done / "aaaaaaaaaaaa.json").write_text(
        json.dumps({"id": "aaaaaaaaaaaa", "answered_at": "2026-11-05T12:00:00+00:00"})
    )

    assert client.get(question_url("demo", "aaaaaaaaaaaa")).json()["status"] == "answered"


def test_a_refused_answer_is_explained_when_the_question_is_read_again(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    answers = runs / "demo" / "answers"
    answers.mkdir()
    (answers / f"{qid}.invalid").write_text(json.dumps({"reason": "unknown items: x"}))

    assert client.get(question_url("demo", qid)).json()["refused"] == "unknown items: x"


def test_the_career_gate_carries_the_workbench_with_evidence_latex_and_escaped_documents(
    client: TestClient, runs: Path
) -> None:
    project_at_career(runs)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    wb = client.get(question_url("demo", qid)).json()["workbench"]

    assert [f["track"] for f in wb["fit"]] == ["strategy_consulting", "tech_consulting"]
    [add, skip] = wb["changes"]
    assert add["action"] == "add" and add["replaces"] == "Old project"
    assert add["bullets"][0]["evidence"] == [
        {"id": "qa", "type": "qa", "description": "9 of 9 checks passed"}
    ]
    assert "\\resumeProject" in add["latex"] and skip["latex"] == ""
    assert wb["website"]["title"] == "Agent Studio"
    assert wb["website"]["evidence"][0]["description"] == "Spend was $4.20"
    assert "<h1>Project README</h1>" in wb["readme_html"] and "<script>" not in wb["readme_html"]
    assert "<h1>Case study</h1>" in wb["case_study_html"]


def test_a_career_gate_without_a_plan_has_no_workbench(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.CAREER_COACH)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    assert client.get(question_url("demo", qid)).json()["workbench"] is None


def test_the_health_report_lists_checks_and_counts_the_failing_ones(
    client: TestClient, tmp_path: Path
) -> None:
    app = create_app(
        tmp_path,
        make_config(),
        health=lambda: run_all(tmp_path, {}, which=lambda _n: None),
    )

    report = TestClient(app).get("/api/health").json()

    assert report["failing"] >= 4 and {"name", "status", "detail", "fix"} <= set(
        report["checks"][0]
    )
    assert "ghp_" not in json.dumps(client.get("/api/health").json())


# --- answering -------------------------------------------------------------------------------


def test_approving_writes_the_answer_with_the_ticked_items_and_answers(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    response = post(
        client,
        question_url("demo", qid),
        {"fields": {"outcome": "approve", "comments": "ok", "answer0": "40"}, "items": ["readme"]},
    )

    assert response.status_code == 200 and response.json() == {"ok": True}
    written = json.loads((runs / "demo" / "answers" / f"{qid}.json").read_text())
    assert written == {
        "outcome": "approve",
        "comments": "ok",
        "approved_items": ["readme"],
        "answers": {"How many users?": "40"},
    }


def test_a_form_missing_something_is_sent_back_with_the_reason_and_nothing_is_written(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    response = post(
        client,
        question_url("demo", qid),
        {"fields": {"outcome": "request_changes", "comments": ""}},
    )

    assert response.status_code == 400 and "say what should change" in response.json()["error"]
    assert not (runs / "demo" / "answers").exists()


def test_answers_to_closed_or_already_answered_questions_are_told_too_late(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    gone = post(client, question_url("demo", "aaaaaaaaaaaa"), {"fields": {"outcome": "approve"}})
    assert gone.status_code == 409 and "no longer open" in gone.json()["error"]

    qid = write_pending(runs, PendingKind.GATE, GATE)
    answers = runs / "demo" / "answers"
    answers.mkdir()
    (answers / f"{qid}.json").write_text("{}")
    again = post(client, question_url("demo", qid), {"fields": {"outcome": "approve"}})
    assert again.status_code == 409 and "already been answered" in again.json()["error"]


def test_a_malformed_body_is_refused(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    url = question_url("demo", qid)
    headers = {"x-csrf-token": csrf(client), "content-type": "application/json"}

    assert client.post(url, content="{nope", headers=headers).status_code == 400
    assert client.post(url, content="[1]", headers=headers).status_code == 400
    assert post(client, url, {"fields": [], "items": {}}).status_code == 400


# --- changes need proof they came from the app -----------------------------------------------


def test_a_change_without_the_session_token_is_refused(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    url = question_url("demo", qid)
    body = {"fields": {"outcome": "approve"}}

    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={"x-csrf-token": "wrong"}).status_code == 403
    assert not (runs / "demo" / "answers").exists()


def test_a_change_that_is_not_json_is_refused_because_a_plain_form_post_cannot_be_forged(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    response = client.post(
        question_url("demo", qid),
        data={"outcome": "approve"},
        headers={"x-csrf-token": csrf(client)},
    )

    assert response.status_code == 415


def test_a_change_from_another_site_is_refused_and_one_from_this_site_is_accepted(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.MERGE, {"pr_urls": ["u"]})
    url = question_url("demo", qid)

    evil = post(client, url, {"fields": {"merged": "yes"}}, origin="https://evil.example")
    ok = post(
        client, url, {"fields": {"merged": "yes"}}, origin="http://testserver", host="testserver"
    )

    assert evil.status_code == 403 and ok.status_code == 200


def test_the_token_endpoint_only_answers_this_machine(client: TestClient) -> None:
    assert client.get("/api/session", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/projects", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/api/projects", headers={"host": "localhost:8765"}).status_code == 200


# --- explaining approvals --------------------------------------------------------------------


def test_a_tool_request_can_be_explained(
    explaining_client: TestClient, runs: Path, explainer: FakeExplainer
) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    tool = write_pending(
        runs, PendingKind.TOOL, {"tool": "Bash", "summary": "npm install", "reason": "asks"}
    )

    body = explaining_client.get(question_url("demo", tool) + "/explanation").json()

    assert body["explanation"]["risk"] == "low" and body["error"] is None
    assert explainer.calls[0].summary == "npm install" and explainer.calls[0].question_id == tool


def test_only_tool_requests_are_explained(explaining_client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    merge = write_pending(runs, PendingKind.MERGE, {"pr_urls": []})

    response = explaining_client.get(question_url("demo", merge) + "/explanation")

    assert response.status_code == 404


def test_a_failed_explanation_is_reported_not_raised(
    explaining_client: TestClient, runs: Path, explainer: FakeExplainer
) -> None:
    from orchestrator.explain import ExplainResult

    explainer.result = ExplainResult(None, "The explanation service is unavailable.")
    make_project(runs, stage=Stage.DEVELOPER)
    tool = write_pending(runs, PendingKind.TOOL, {"tool": "Bash", "summary": "ls", "reason": "r"})

    response = explaining_client.get(question_url("demo", tool) + "/explanation")

    assert response.status_code == 200
    assert response.json() == {
        "explanation": None,
        "error": "The explanation service is unavailable.",
        "cost_usd": 0.0,
    }


# --- proposals from the chat ------------------------------------------------------------------


def propose(runs: Path, action: str, args: dict[str, Any], summary: str = "A proposal") -> str:
    from orchestrator.proposals import create_proposal

    return create_proposal(runs, "demo", action, args, summary).id


def test_pending_proposals_are_listed(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    pid = propose(runs, "set_autonomy", {"level": "autonomous"}, "Set demo to autonomous")

    body = client.get("/api/proposals").json()

    assert [(p["id"], p["summary"], p["status"]) for p in body["proposals"]] == [
        (pid, "Set demo to autonomous", "pending")
    ]


def test_confirming_an_answer_carries_it_out_like_the_normal_form(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    gate = write_pending(runs, PendingKind.GATE, GATE)
    pid = propose(
        runs,
        "answer_question",
        {
            "question_id": gate,
            "fields": {"outcome": "approve", "comments": ""},
            "items": ["readme"],
        },
    )

    response = post(client, f"/api/projects/demo/proposals/{pid}/confirm")

    assert response.status_code == 200
    answers = list((runs / "demo" / "answers").glob("*.json"))
    assert len(answers) == 1
    assert json.loads(answers[0].read_text())["outcome"] == "approve"
    assert client.get("/api/proposals?status=confirmed").json()["proposals"][0]["id"] == pid


def test_confirming_a_command_decision_can_save_the_rule(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npx next lint")
    pid = propose(
        runs,
        "answer_question",
        {
            "question_id": qid,
            "fields": {"allow": "yes", "message": "", "remember": "project", "rule_modes": "exact"},
            "items": [],
        },
    )

    post(client, f"/api/projects/demo/proposals/{pid}/confirm")

    assert project_store(runs, "demo").load()[0].argv == ["npx", "next", "lint"]


def test_a_proposal_for_a_question_that_has_since_closed_fails_without_harm(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    pid = propose(
        runs,
        "answer_question",
        {"question_id": "gone", "fields": {"outcome": "approve", "comments": ""}, "items": []},
    )

    response = post(client, f"/api/projects/demo/proposals/{pid}/confirm")

    assert response.status_code == 409 and "no longer open" in response.json()["error"]
    assert client.get("/api/proposals?status=failed").json()["proposals"][0]["id"] == pid


def test_confirming_resume_starts_a_run(client: TestClient, runs: Path, launched: Launched) -> None:
    make_project(runs, stage=Stage.DEVELOPER, status=Status.HALTED, agent="full")
    pid = propose(runs, "resume_project", {})

    assert post(client, f"/api/projects/demo/proposals/{pid}/confirm").status_code == 200

    assert len(launched.calls) == 1


def test_confirming_autonomy_and_a_note_applies_them(client: TestClient, runs: Path) -> None:
    from orchestrator.notes import pending_notes
    from orchestrator.project_settings import get_autonomy

    make_project(runs, stage=Stage.DEVELOPER)
    a = propose(runs, "set_autonomy", {"level": "autonomous"})
    n = propose(runs, "leave_note", {"text": "Use server actions"})

    post(client, f"/api/projects/demo/proposals/{a}/confirm")
    post(client, f"/api/projects/demo/proposals/{n}/confirm")

    assert get_autonomy(runs, "demo") == "autonomous"
    assert [x.text for x in pending_notes(runs, "demo")] == ["Use server actions"]


def test_a_proposal_can_be_dismissed_and_does_nothing(client: TestClient, runs: Path) -> None:
    from orchestrator.project_settings import get_autonomy

    make_project(runs, stage=Stage.DEVELOPER)
    pid = propose(runs, "set_autonomy", {"level": "autonomous"})

    assert post(client, f"/api/projects/demo/proposals/{pid}/dismiss").status_code == 200

    assert get_autonomy(runs, "demo") == "standard"
    assert client.get("/api/proposals?status=dismissed").json()["proposals"][0]["id"] == pid


def test_a_proposal_can_only_be_decided_once(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    pid = propose(runs, "set_autonomy", {"level": "autonomous"})

    assert post(client, f"/api/projects/demo/proposals/{pid}/confirm").status_code == 200
    assert post(client, f"/api/projects/demo/proposals/{pid}/confirm").status_code == 409


def test_deciding_a_proposal_needs_the_session_token(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    pid = propose(runs, "set_autonomy", {"level": "autonomous"})

    response = client.post(f"/api/projects/demo/proposals/{pid}/confirm", json={})

    assert response.status_code == 403


# --- what each agent is doing ----------------------------------------------------------------


def test_the_activity_of_each_agent_is_served(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)

    people = client.get("/api/projects/demo/activity").json()["agents"]

    assert [p["stage"] for p in people] == [
        "product_owner",
        "architect",
        "developer",
        "qa",
        "career_coach",
    ]
    by = {p["stage"]: p for p in people}
    assert by["product_owner"]["status"] == "done"
    assert by["qa"]["status"] == "upcoming" and by["qa"]["waiting_on"] == "Developer"
    assert by["developer"]["status"] == "stopped"  # no live run was started in this test


def test_a_live_run_shows_the_agent_as_working(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)

    with run_marker(runs, "demo", beat_seconds=60):
        people = client.get("/api/projects/demo/activity").json()["agents"]

    assert {p["stage"]: p for p in people}["developer"]["status"] == "working"


def test_the_activity_of_an_unknown_project_is_a_404(client: TestClient) -> None:
    assert client.get("/api/projects/ghost/activity").status_code == 404


# --- action items ----------------------------------------------------------------------------


def test_action_items_are_listed_with_their_project(client: TestClient, runs: Path) -> None:
    from orchestrator.todos import add_todo

    make_project(runs, stage=Stage.DEVELOPER)
    todo = add_todo(
        runs, "demo", key="k", title="Add the key", detail="In .env", source="developer"
    )

    body = client.get("/api/todos").json()

    [mine] = [t for t in body["todos"] if t["id"] == todo.id]
    assert mine["project"] == "demo" and mine["title"] == "Add the key"
    assert mine["can_mark_done"] is True


def test_an_action_item_can_be_marked_done(client: TestClient, runs: Path) -> None:
    from orchestrator.todos import add_todo, open_todos

    make_project(runs, stage=Stage.DEVELOPER)
    todo = add_todo(runs, "demo", key="k", title="Add the key", source="developer")

    assert post(client, f"/api/projects/demo/todos/{todo.id}/done").status_code == 200

    assert open_todos(runs, "demo") == []
    assert post(client, f"/api/projects/demo/todos/{todo.id}/done").status_code == 404


def test_a_failing_health_check_is_listed_as_an_item_that_clears_itself(
    runs: Path, tmp_path: Path
) -> None:
    app = create_app(
        runs,
        make_config(),
        config_path=CONFIG,
        projects_dir=tmp_path / "clones",
        health=lambda: run_all(tmp_path, env={}),  # no keys at all
    )

    body = TestClient(app).get("/api/todos").json()

    system = [t for t in body["todos"] if t["source"] == "system"]
    assert system and all(t["can_mark_done"] is False and t["project"] is None for t in system)


def test_a_project_lists_its_own_open_items(client: TestClient, runs: Path) -> None:
    from orchestrator.todos import add_todo

    make_project(runs, stage=Stage.DEVELOPER)
    add_todo(runs, "demo", key="k", title="Add the key", source="developer")

    assert [t["title"] for t in client.get("/api/projects/demo").json()["todos"]] == ["Add the key"]


# --- always allow ----------------------------------------------------------------------------


def tool_question(runs: Path, command: str) -> str:
    return write_pending(
        runs, PendingKind.TOOL, {"tool": "Bash", "summary": command, "reason": "not listed"}
    )


def test_a_tool_request_lists_what_can_be_remembered(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npx next lint --fix")

    body = client.get(question_url("demo", qid) + "/rule-options").json()

    assert body["available"] is True
    assert body["parts"] == [
        {
            "label": "npx next lint --fix",
            "exact": ["npx", "next", "lint", "--fix"],
            "any_label": "npx next …any arguments",
        }
    ]


def test_a_refused_command_has_nothing_to_remember(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "git push origin main")

    body = client.get(question_url("demo", qid) + "/rule-options").json()

    assert body["available"] is False and body["reason"]


def test_allowing_with_remember_saves_a_project_rule(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npx next lint --fix")

    response = post(
        client,
        question_url("demo", qid),
        {"fields": {"allow": "yes", "remember": "project", "rule_modes": "any"}, "items": []},
    )

    assert response.status_code == 200
    [rule] = project_store(runs, "demo").load()
    assert rule.argv == ["npx", "next"] and rule.any_args is True


def test_remember_everywhere_saves_a_global_rule(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import global_store, project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npx next lint")

    post(
        client,
        question_url("demo", qid),
        {"fields": {"allow": "yes", "remember": "global", "rule_modes": "exact"}, "items": []},
    )

    assert global_store().load()[0].argv == ["npx", "next", "lint"]
    assert project_store(runs, "demo").load() == []


def test_denying_never_saves_a_rule(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npx next lint")

    post(
        client,
        question_url("demo", qid),
        {"fields": {"allow": "no", "remember": "project", "rule_modes": "exact"}, "items": []},
    )

    assert project_store(runs, "demo").load() == []


def test_a_wider_pattern_is_ignored_when_it_is_not_offered(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "npm install left-pad")

    post(
        client,
        question_url("demo", qid),
        {"fields": {"allow": "yes", "remember": "project", "rule_modes": "any"}, "items": []},
    )

    [rule] = project_store(runs, "demo").load()
    assert rule.argv == ["npm", "install", "left-pad"] and rule.any_args is False


def test_a_command_that_cannot_be_remembered_is_still_allowed_once(
    client: TestClient, runs: Path
) -> None:
    from orchestrator.rules import project_store

    make_project(runs, stage=Stage.DEVELOPER)
    qid = tool_question(runs, "FOO=1 pytest")

    response = post(
        client,
        question_url("demo", qid),
        {"fields": {"allow": "yes", "remember": "project", "rule_modes": "exact"}, "items": []},
    )

    assert response.status_code == 200 and project_store(runs, "demo").load() == []


def test_rules_are_listed_and_can_be_revoked(client: TestClient, runs: Path) -> None:
    from orchestrator.rules import global_store, project_store

    make_project(runs, stage=Stage.DEVELOPER)
    mine = project_store(runs, "demo").add(["npx", "next"], any_args=True)
    everywhere = global_store().add(["npm", "run", "x"], any_args=False)

    listed = client.get("/api/rules").json()

    assert [r["id"] for r in listed["projects"]["demo"]] == [mine.id]
    assert listed["projects"]["demo"][0]["description"] == "npx next …any arguments"
    assert [r["id"] for r in listed["global"]] == [everywhere.id]

    assert (
        post(client, "/api/rules/revoke", {"scope": "global", "id": everywhere.id}).status_code
        == 200
    )
    assert (
        post(
            client, "/api/rules/revoke", {"scope": "project", "project": "demo", "id": mine.id}
        ).status_code
        == 200
    )
    assert client.get("/api/rules").json() == {"global": [], "projects": {"demo": []}}


def test_revoking_an_unknown_rule_is_a_404(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)

    assert post(client, "/api/rules/revoke", {"scope": "global", "id": "nope"}).status_code == 404


def test_the_autonomy_level_is_shown_and_can_be_changed(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)
    assert client.get("/api/projects/demo").json()["autonomy"] == "standard"

    assert post(client, "/api/projects/demo/autonomy", {"level": "autonomous"}).status_code == 200
    assert client.get("/api/projects/demo").json()["autonomy"] == "autonomous"
    assert post(client, "/api/projects/demo/autonomy", {"level": "reckless"}).status_code == 400


# --- controls --------------------------------------------------------------------------------


def test_pause_stops_a_running_project_from_spending_more(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER)

    assert post(client, "/api/projects/demo/pause").status_code == 200
    assert halt_requested(runs, "demo")


def test_pausing_a_finished_project_is_refused(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE)

    response = post(client, "/api/projects/demo/pause")

    assert response.status_code == 409 and "already done" in response.json()["error"]
    assert not halt_requested(runs, "demo")


def test_pausing_a_paused_project_is_refused(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.DEVELOPER, status=Status.HALTED)

    response = post(client, "/api/projects/demo/pause")

    assert response.status_code == 409 and "already paused" in response.json()["error"]


def test_a_paused_project_can_be_resumed(
    client: TestClient, runs: Path, launched: Launched
) -> None:
    make_project(runs, stage=Stage.DEVELOPER, status=Status.HALTED, agent="full")

    assert post(client, "/api/projects/demo/resume").status_code == 200

    assert len(launched.calls) == 1


def test_resume_starts_a_detached_run_with_the_projects_own_settings(
    client: TestClient, runs: Path, launched: Launched
) -> None:
    make_project(runs, stage=Stage.DEVELOPER, agent="full")

    assert post(client, "/api/projects/demo/resume").status_code == 200

    [(command, cwd, log)] = launched.calls
    assert command[-9:] == [
        "resume", "demo", "--agent", "full", "--ui", "--config",
        str(CONFIG.resolve()), "--runs-dir", str(runs.resolve()),
    ]  # fmt: skip
    assert cwd == CONFIG.resolve().parent and log == runs / "demo" / "run.log"


def test_resume_refuses_a_live_a_finished_or_unrecorded_project(
    client: TestClient, runs: Path, launched: Launched
) -> None:
    make_project(runs, stage=Stage.DEVELOPER)  # no agent recorded
    assert (
        "resume it from the terminal" in post(client, "/api/projects/demo/resume").json()["error"]
    )

    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE, agent="full")
    assert "nothing to resume" in post(client, "/api/projects/demo/resume").json()["error"]

    make_project(runs, stage=Stage.DEVELOPER, agent="full")
    with run_marker(runs, "demo", beat_seconds=60):
        assert "already running" in post(client, "/api/projects/demo/resume").json()["error"]
    assert launched.calls == []


# --- starting a project ----------------------------------------------------------------------

FORM = {
    "name": "my-project",
    "idea": "A tool that tracks student org events.",
    "track": "engineering",
    "agent": "full",
    "visibility": "private",
}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "Bad Name"}, "lowercase letters"),
        ({"idea": "short"}, "10 to 2000 characters"),
        ({"track": "astronaut"}, "Choose a track"),
        ({"agent": "other"}, "Choose how to run it"),
        ({"visibility": "nowhere"}, "Choose private or public"),
    ],
)
def test_a_bad_form_is_sent_back_with_what_to_fix(
    client: TestClient, changes: dict[str, str], message: str
) -> None:
    response = post(client, "/api/new/preview", {**FORM, **changes})

    body = response.json()
    assert response.status_code == 400 and body["ok"] is False
    assert any(message in p for p in body["problems"])
    assert body["fields"]["name"] == changes.get("name", "my-project")


def test_a_name_that_is_taken_by_a_project_or_a_folder_is_refused(
    client: TestClient, runs: Path, tmp_path: Path
) -> None:
    make_project(runs, "taken")
    (tmp_path / "clones" / "folder-taken").mkdir(parents=True)

    assert any(
        "already exists" in p
        for p in post(client, "/api/new/preview", {**FORM, "name": "taken"}).json()["problems"]
    )
    folder = post(client, "/api/new/preview", {**FORM, "name": "folder-taken"}).json()
    assert any("already exists on this computer" in p for p in folder["problems"])
    assert (
        post(
            client, "/api/new/preview", {**FORM, "name": "folder-taken", "agent": "echo"}
        ).status_code
        == 200
    )


def test_a_full_run_is_blocked_when_a_key_or_the_profile_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("AGENT_STUDIO_PROFILE")

    full = post(client, "/api/new/preview", FORM)
    practice = post(client, "/api/new/preview", {**FORM, "agent": "echo"})

    assert full.status_code == 400
    text = " ".join(full.json()["problems"])
    assert "Anthropic API key" in text and "Career profile" in text
    assert practice.status_code == 200


def test_the_preview_says_where_the_repository_will_be_cloned(
    client: TestClient, tmp_path: Path
) -> None:
    body = post(client, "/api/new/preview", {**FORM, "visibility": "public"}).json()

    assert body["ok"] is True and body["clone"] == str(tmp_path / "clones" / "my-project")
    assert body["fields"]["visibility"] == "public"


def test_confirming_creates_the_repository_and_starts_a_detached_run(
    client: TestClient, runs: Path, launched: Launched, creator: Creator, tmp_path: Path
) -> None:
    response = post(client, "/api/new/start", FORM)

    assert response.status_code == 200 and response.json() == {"ok": True, "slug": "my-project"}
    assert creator.calls == [("my-project", tmp_path / "clones", False)]
    [(command, cwd, log)] = launched.calls
    assert command[command.index("--agent") + 1] == "full" and "--ui" in command
    assert command[command.index("--repo") + 1] == str(tmp_path / "clones" / "my-project")
    assert command[command.index("--slug") + 1] == "my-project"
    assert "A tool that tracks student org events." in command
    assert cwd == CONFIG.resolve().parent and log == runs / "my-project" / "run.log"


def test_a_practice_run_creates_no_repository(
    client: TestClient, launched: Launched, creator: Creator
) -> None:
    post(client, "/api/new/start", {**FORM, "agent": "echo"})

    assert creator.calls == []
    [(command, _cwd, _log)] = launched.calls
    assert "--repo" not in command and command[command.index("--agent") + 1] == "echo"


def test_a_repository_that_could_not_be_created_stops_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in env_for(tmp_path).items():
        monkeypatch.setenv(key, value)
    launched = Launched()
    app = create_app(
        tmp_path / "runs",
        make_config(),
        config_path=CONFIG,
        launcher=launched,
        projects_dir=tmp_path / "clones",
        creator=Creator("my-project already exists on GitHub"),
    )

    response = post(TestClient(app), "/api/new/start", FORM)

    assert response.status_code == 409 and "already exists on GitHub" in response.json()["error"]
    assert launched.calls == []


def test_starting_a_project_needs_the_session_token(
    client: TestClient, launched: Launched, creator: Creator
) -> None:
    response = client.post("/api/new/start", json={**FORM, "agent": "echo"})

    assert response.status_code == 403 and launched.calls == [] and creator.calls == []


# --- the React app ---------------------------------------------------------------------------


@pytest.fixture
def built(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log('app')")
    (dist / "favicon.svg").write_text("<svg/>")
    return dist


def test_the_built_app_is_served_for_the_front_page_and_every_client_side_route(
    tmp_path: Path, built: Path
) -> None:
    web = TestClient(create_app(tmp_path / "runs", make_config(), frontend_dir=built))

    for path in ("/", "/inbox", "/p/demo", "/new", "/p/demo/a/prd.md"):
        response = web.get(path)
        assert response.status_code == 200 and "<div id=root>" in response.text, path
    assert web.get("/assets/app.js").text == "console.log('app')"
    assert web.get("/favicon.svg").text == "<svg/>"


def test_unknown_api_paths_are_json_errors_not_the_app(tmp_path: Path, built: Path) -> None:
    web = TestClient(create_app(tmp_path / "runs", make_config(), frontend_dir=built))

    response = web.get("/api/nothing-here")

    assert response.status_code == 404 and response.json() == {"error": "no such endpoint"}


def test_nothing_outside_the_app_folder_is_ever_served(tmp_path: Path, built: Path) -> None:
    (tmp_path / "secret.txt").write_text("top secret")
    web = TestClient(create_app(tmp_path / "runs", make_config(), frontend_dir=built))

    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        assert "top secret" not in web.get(path).text, path


def test_the_api_docs_are_not_exposed(tmp_path: Path, built: Path) -> None:
    web = TestClient(create_app(tmp_path / "runs", make_config(), frontend_dir=built))

    assert "openapi" not in web.get("/openapi.json").text.lower()
    assert "swagger" not in web.get("/docs").text.lower()


def test_without_a_built_app_the_page_says_how_to_build_it(tmp_path: Path) -> None:
    web = TestClient(
        create_app(tmp_path / "runs", make_config(), frontend_dir=tmp_path / "missing")
    )

    response = web.get("/")

    assert response.status_code == 503 and "npm --prefix frontend" in response.text
    assert web.get("/api/projects").status_code == 200  # the API works without the app


# --- the whole thing, from the browser's side ------------------------------------------------


def test_a_project_runs_to_the_end_answered_only_through_the_api(
    runs: Path, launched: Launched
) -> None:
    web = TestClient(create_app(runs, make_config(), config_path=CONFIG, launcher=launched))
    ctx = _context(CONFIG, runs, AgentKind.ECHO, project="demo", ui=True)
    result: dict[str, ProjectState] = {}

    def run() -> None:
        result["state"] = run_pipeline(ProjectState(project_slug="demo", idea="An idea"), ctx)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    approved = 0
    deadline = time.time() + 30
    while runner.is_alive() and time.time() < deadline:
        items = web.get("/api/inbox").json()
        if items:
            url = question_url(items[0]["project"], items[0]["id"])
            if web.get(url).json().get("status") == "open":
                response = post(web, url, {"fields": {"outcome": "approve"}})
                if response.status_code == 200:
                    approved += 1
                else:  # the run has not picked up our earlier answer yet
                    assert response.status_code == 409
        time.sleep(0.02)
    runner.join(timeout=5)

    assert result["state"].status == Status.DONE
    assert approved >= 4
    assert all_pending(runs) == [] and len(answered(runs, "demo")) == approved
