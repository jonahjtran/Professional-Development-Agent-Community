import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.inbox import PendingKind
from orchestrator.state import (
    ProjectState,
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


# --- the decision pages ----------------------------------------------------------------------


# --- answering -------------------------------------------------------------------------------


def post(client: TestClient, url: str, data: dict[str, Any], **kw: Any) -> Any:
    return client.post(
        url, data={"csrf": _token_from(client), **data}, follow_redirects=False, **kw
    )


# --- controls --------------------------------------------------------------------------------


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
