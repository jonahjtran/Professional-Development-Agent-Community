import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.cli import AgentKind, _context
from orchestrator.halt import halt_requested
from orchestrator.inbox import PendingKind, all_pending, answered, run_marker
from orchestrator.pipeline import run_pipeline
from orchestrator.state import (
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
)
from orchestrator.ui.app import create_app, resume_command
from orchestrator.ui.notify import _applescript_quote, watch
from tests.factories import make_config
from tests.test_ui import make_project

CONFIG = Path(__file__).parent.parent / "config.yaml"


def write_pending(
    runs: Path, kind: PendingKind, request: dict[str, Any], slug: str = "demo"
) -> str:
    folder = runs / slug / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    pending_id = f"{len(list(folder.glob('*.json'))) + 1:012x}"
    body = {
        "id": pending_id,
        "project": slug,
        "kind": kind.value,
        "created": datetime(2026, 11, 5, 12, 0, tzinfo=UTC).isoformat(),
        "request": request,
    }
    (folder / f"{pending_id}.json").write_text(json.dumps(body))
    return pending_id


GATE = {
    "stage": "qa",
    "title": "Review the qa output",
    "summary": "# Report\n\n<script>alert(1)</script>",
    "cost_usd": 2.5,
    "items": [{"id": "readme", "label": "Replace README"}],
    "questions": ["How many users?"],
}


class Launched:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, Path]] = []

    def __call__(self, command: list[str], cwd: Path, log: Path) -> None:
        self.calls.append((command, cwd, log))


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def launched() -> Launched:
    return Launched()


@pytest.fixture
def client(runs: Path, launched: Launched) -> TestClient:
    app = create_app(runs, make_config(), config_path=CONFIG, launcher=launched)
    return TestClient(app)


def _token_from(client: TestClient) -> str:
    return client.get("/").text.split('<meta name="csrf" content="')[1].split('"')[0]


# --- the inbox -------------------------------------------------------------------------------


def test_the_inbox_lists_every_open_question_with_a_link(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    page = client.get("/inbox").text

    assert f'href="/p/demo/q/{qid}"' in page and "Review: Review the qa output" in page


def test_an_empty_inbox_says_so(client: TestClient) -> None:
    assert "Nothing is waiting for you" in client.get("/inbox").text


def test_the_pending_api_feeds_the_notifier(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    [item] = client.get("/api/pending").json()

    assert item["id"] == qid and item["url"] == f"/p/demo/q/{qid}" and item["project"] == "demo"


def test_the_project_page_links_straight_to_its_open_question(
    client: TestClient, runs: Path
) -> None:
    make_project(
        runs,
        status=Status.AWAITING_HUMAN,
        stage=Stage.QA,
        intervention=Intervention(kind=InterventionKind.GATE, detail="x"),
    )
    qid = write_pending(runs, PendingKind.GATE, GATE)

    assert f"/p/demo/q/{qid}" in client.get("/p/demo").text


# --- the decision pages ----------------------------------------------------------------------


def test_a_gate_shows_its_summary_safely_with_items_questions_and_buttons(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    page = client.get(f"/p/demo/q/{qid}").text

    assert "<h1>Report</h1>" in page and "<script>alert" not in page
    assert "Replace README" in page and "How many users?" in page
    for button in ("Approve", "Request changes", "Reject"):
        assert button in page


def test_each_kind_of_question_has_its_own_form(client: TestClient, runs: Path) -> None:
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
        runs, PendingKind.TOOL, {"tool": "Bash", "summary": "pip install x<y>", "reason": "r"}
    )

    assert "Approve anyway" in client.get(f"/p/demo/q/{cap}").text
    assert "https://github.com/me/p/pull/1" in client.get(f"/p/demo/q/{merge}").text
    tool_page = client.get(f"/p/demo/q/{tool}").text
    assert "pip install x&lt;y&gt;" in tool_page and "Allow once" in tool_page


def test_a_question_that_is_gone_is_not_found_and_an_answered_one_says_so(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)

    assert client.get("/p/demo/q/aaaaaaaaaaaa").status_code == 404

    done = runs / "demo" / "done"
    done.mkdir(parents=True)
    (done / "aaaaaaaaaaaa.json").write_text(
        json.dumps({"id": "aaaaaaaaaaaa", "answered_at": "2026-11-05T12:00:00+00:00"})
    )
    assert "already answered" in client.get("/p/demo/q/aaaaaaaaaaaa").text


def test_a_refused_answer_is_explained_when_the_page_is_shown_again(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    answers = runs / "demo" / "answers"
    answers.mkdir()
    (answers / f"{qid}.invalid").write_text(json.dumps({"reason": "unknown items: x"}))

    assert "unknown items: x" in client.get(f"/p/demo/q/{qid}").text


# --- answering -------------------------------------------------------------------------------


def post(client: TestClient, url: str, data: dict[str, Any], **kw: Any) -> Any:
    return client.post(
        url, data={"csrf": _token_from(client), **data}, follow_redirects=False, **kw
    )


def test_approving_writes_the_answer_with_ticked_items_and_answers(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)

    response = post(
        client,
        f"/p/demo/q/{qid}",
        {"outcome": "approve", "comments": "ok", "items": "readme", "answer0": "40"},
    )

    assert response.status_code == 303 and response.headers["location"] == "/inbox"
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

    response = post(client, f"/p/demo/q/{qid}", {"outcome": "request_changes", "comments": ""})

    assert response.status_code == 400 and "say what should change" in response.text
    assert not (runs / "demo" / "answers").exists()


def test_answering_a_question_someone_else_already_answered_is_told_it_was_too_late(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    answers = runs / "demo" / "answers"
    answers.mkdir()
    (answers / f"{qid}.json").write_text("{}")

    response = post(client, f"/p/demo/q/{qid}", {"outcome": "approve"})

    assert response.status_code == 409 and "already been answered" in response.text


def test_a_post_without_the_form_token_or_from_another_site_is_refused(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.GATE, GATE)
    url = f"/p/demo/q/{qid}"

    assert client.post(url, data={"outcome": "approve"}).status_code == 403
    assert client.post(url, data={"csrf": "wrong", "outcome": "approve"}).status_code == 403
    cross = client.post(
        url,
        data={"csrf": _token_from(client), "outcome": "approve"},
        headers={"origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    assert not (runs / "demo" / "answers").exists()


def test_a_same_site_origin_is_accepted(client: TestClient, runs: Path) -> None:
    make_project(runs)
    qid = write_pending(runs, PendingKind.MERGE, {"pr_urls": ["u"]})

    response = post(
        client,
        f"/p/demo/q/{qid}",
        {"merged": "yes"},
        headers={"origin": "http://testserver", "host": "testserver"},
    )

    assert response.status_code == 303


# --- controls --------------------------------------------------------------------------------


def test_halt_drops_the_kill_switch_file_for_a_running_project(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, stage=Stage.DEVELOPER)

    response = post(client, "/p/demo/halt", {})

    assert response.status_code == 303 and halt_requested(runs, "demo")


def test_halting_a_finished_project_does_nothing(client: TestClient, runs: Path) -> None:
    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE)

    response = post(client, "/p/demo/halt", {})

    assert "already done" in response.text and not halt_requested(runs, "demo")


def test_resume_starts_a_detached_run_with_the_projects_own_settings(
    client: TestClient, runs: Path, launched: Launched
) -> None:
    make_project(runs, stage=Stage.DEVELOPER, agent="full")

    response = post(client, "/p/demo/resume", {})

    assert response.status_code == 303
    [(command, cwd, log)] = launched.calls
    assert command[-9:] == [
        "resume",
        "demo",
        "--agent",
        "full",
        "--ui",
        "--config",
        str(CONFIG.resolve()),
        "--runs-dir",
        str(runs.resolve()),
    ]
    assert cwd == CONFIG.resolve().parent and log == runs / "demo" / "run.log"


def test_resume_refuses_a_live_a_finished_or_unrecorded_project(
    client: TestClient, runs: Path, launched: Launched
) -> None:
    make_project(runs, stage=Stage.DEVELOPER)  # no agent recorded
    assert "resume it from the terminal" in post(client, "/p/demo/resume", {}).text

    make_project(runs, stage=Stage.CAREER_COACH, status=Status.DONE, agent="full")
    assert "nothing to resume" in post(client, "/p/demo/resume", {}).text

    make_project(runs, stage=Stage.DEVELOPER, agent="full")
    with run_marker(runs, "demo", beat_seconds=60):
        assert "already running" in post(client, "/p/demo/resume", {}).text
    assert launched.calls == []


def test_the_resume_command_uses_the_agent_kind_the_project_recorded() -> None:
    state = ProjectState(project_slug="p", idea="x", agent="echo")

    command = resume_command(state, Path("config.yaml"), Path("runs"))

    assert command is not None and command[command.index("--agent") + 1] == "echo"
    assert resume_command(ProjectState(project_slug="p", idea="x"), Path("c"), Path("r")) is None


# --- notifications ---------------------------------------------------------------------------


def test_the_watcher_announces_only_new_questions(runs: Path) -> None:
    make_project(runs)
    write_pending(runs, PendingKind.GATE, GATE)  # open before the watcher starts
    heard: list[tuple[str, str]] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch,
        args=(runs, lambda t, b: heard.append((t, b)), stop),
        kwargs={"interval": 0.02},
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)
    assert heard == []

    write_pending(runs, PendingKind.MERGE, {"pr_urls": ["u"]})
    time.sleep(0.15)
    stop.set()
    thread.join(timeout=2)

    assert heard == [("Agent Studio needs you", "demo: Merge your pull requests")]


def test_text_put_into_an_applescript_is_quoted() -> None:
    assert _applescript_quote('say "hi"\\\n') == '"say \\"hi\\"\\\\ "'


# --- the whole thing, from the browser's side ------------------------------------------------


def test_a_project_runs_to_the_end_answered_only_through_the_web_pages(
    runs: Path, launched: Launched
) -> None:
    web = TestClient(create_app(runs, make_config(), config_path=CONFIG, launcher=launched))
    ctx = _context(CONFIG, runs, AgentKind.ECHO, project="demo", ui=True)
    result: dict[str, ProjectState] = {}

    def run() -> None:
        result["state"] = run_pipeline(ProjectState(project_slug="demo", idea="An idea"), ctx)

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    answered_through_web = 0
    deadline = time.time() + 30
    while runner.is_alive() and time.time() < deadline:
        items = web.get("/api/pending").json()
        if items:
            if web.get(items[0]["url"]).status_code != 200:
                continue  # answered by someone else (or the run) in the meantime
            response = web.post(
                items[0]["url"],
                data={"csrf": _token_from(web), "outcome": "approve"},
                follow_redirects=False,
            )
            if response.status_code == 303:
                answered_through_web += 1
            else:  # the run has not picked up our earlier answer yet
                assert response.status_code == 409
        time.sleep(0.02)
    runner.join(timeout=5)

    assert result["state"].status == Status.DONE
    assert answered_through_web >= 4
    assert all_pending(runs) == [] and len(answered(runs, "demo")) == answered_through_web


def test_answering_a_question_that_has_already_closed_shows_a_message_not_an_error_page(
    client: TestClient, runs: Path
) -> None:
    make_project(runs)

    response = post(client, "/p/demo/q/aaaaaaaaaaaa", {"outcome": "approve"})

    assert response.status_code == 409 and "no longer open" in response.text
