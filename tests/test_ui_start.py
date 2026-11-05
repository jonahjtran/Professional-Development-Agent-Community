import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.newproject import NewProject, NewProjectError
from orchestrator.ui import health
from orchestrator.ui.app import create_app, start_command
from tests.factories import make_config
from tests.test_ui import make_project

CONFIG = Path(__file__).parent.parent / "config.yaml"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
PROFILE_FILES = {
    "resume.yaml": "sections:\n  - {name: Projects, max_bullets: 2, entries: []}\n",
    "goals.yaml": "tracks:\n  tech_consulting: {target_roles: [consultant]}\n",
}


def git_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    return path


def profile(tmp_path: Path, origin: str = "https://github.com/me/career-profile.git") -> Path:
    folder = git_repo(tmp_path / "career-profile", origin)
    for name, text in PROFILE_FILES.items():
        (folder / name).write_text(text)
    return folder


def website(tmp_path: Path, origin: str = "https://github.com/me/site.git") -> Path:
    folder = git_repo(tmp_path / "site", origin)
    (folder / "src" / "data").mkdir(parents=True, exist_ok=True)
    (folder / "src" / "data" / "projects.js").write_text("export const projects = [];")
    return folder


def fake_run(logins: dict[str, str] | None = None) -> Any:
    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["gh", "api", "user"]:
            login = (logins or {}).get("login", "me")
            return subprocess.CompletedProcess(args, 0 if login else 1, login, "")
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)

    return run


def everything(_name: str) -> str:
    return "/usr/bin/tool"


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "sk-ant-secret-value",
        "GITHUB_TOKEN": "ghp_supersecrettoken",
        "AGENT_STUDIO_PROFILE": str(profile(tmp_path)),
        "AGENT_STUDIO_WEBSITE": str(website(tmp_path)),
        **extra,
    }


def by_name(checks: list[health.Check]) -> dict[str, health.Check]:
    return {c.name: c for c in checks}


def run_all(tmp_path: Path, env: dict[str, str] | None = None, **kw: Any) -> list[health.Check]:
    return health.run_checks(
        env if env is not None else env_for(tmp_path),
        repo_root=tmp_path,
        which=kw.pop("which", everything),
        run=kw.pop("run", fake_run()),
        fetch=kw.pop("fetch", lambda _t: (200, None)),
        now=NOW,
        **kw,
    )


# --- the health checks -----------------------------------------------------------------------


def test_a_fully_set_up_machine_has_nothing_failing(tmp_path: Path) -> None:
    checks = run_all(tmp_path)

    assert health.blocking(checks) == []
    assert by_name(checks)["GitHub login (gh)"].detail == "Logged in as me."


def test_missing_keys_tools_and_folders_are_each_reported_with_a_fix(tmp_path: Path) -> None:
    checks = by_name(run_all(tmp_path, {}, which=lambda _n: None))

    for name in (
        "Anthropic API key",
        "GitHub token",
        "Career profile",
        "gitleaks",
        "uv",
        "GitHub login (gh)",
    ):
        assert checks[name].status == health.FAIL, name
    assert checks["Career profile"].fix and checks["gitleaks"].fix
    assert checks["node"].status == health.WARN  # only the website build needs it
    assert checks["Website folder"].status == health.WARN


def test_no_secret_value_ever_appears_in_a_result(tmp_path: Path) -> None:
    text = repr(run_all(tmp_path, fetch=lambda _t: (401, None)))

    assert "ghp_supersecrettoken" not in text and "sk-ant-secret-value" not in text


@pytest.mark.parametrize(
    ("expires", "status", "words"),
    [
        ("2026-12-20 12:00:00 UTC", health.OK, "Expires in 45 days"),
        ("2026-11-08 12:00:00 UTC", health.WARN, "Expires in 3 day"),
        ("2026-11-01 12:00:00 UTC", health.FAIL, "expired"),
    ],
)
def test_the_token_expiry_is_graded_by_how_close_it_is(
    tmp_path: Path, expires: str, status: str, words: str
) -> None:
    checks = by_name(run_all(tmp_path, fetch=lambda _t: (200, expires)))

    assert checks["GitHub token expiry"].status == status
    assert words in checks["GitHub token expiry"].detail


def test_a_token_that_never_expires_or_one_github_rejects(tmp_path: Path) -> None:
    ok = by_name(run_all(tmp_path, fetch=lambda _t: (200, None)))
    rejected = by_name(run_all(tmp_path, fetch=lambda _t: (401, None)))

    assert "does not expire" in ok["GitHub token expiry"].detail
    assert rejected["GitHub token"].status == health.FAIL and "GitHub token expiry" not in rejected


def test_being_offline_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    def offline(_token: str) -> tuple[int, str | None]:
        raise OSError("no network")

    checks = by_name(run_all(tmp_path, fetch=offline))

    assert checks["GitHub token expiry"].status == health.WARN
    assert health.blocking(list(checks.values())) == []


def test_not_being_logged_in_to_gh_fails_and_the_network_can_be_skipped(tmp_path: Path) -> None:
    out = by_name(run_all(tmp_path, run=fake_run({"login": ""})))
    quick = by_name(run_all(tmp_path, network=False))

    assert out["GitHub login (gh)"].status == health.FAIL
    assert "GitHub login (gh)" not in quick  # skipped without the network
    assert "GitHub token expiry" not in quick


def test_a_profile_that_is_not_a_clone_or_not_valid_fails(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    folder = tmp_path / "career-profile"
    subprocess.run(["rm", "-rf", str(folder / ".git")], check=True)

    assert by_name(run_all(tmp_path, env))["Career profile"].status == health.FAIL

    (folder / "goals.yaml").write_text("tracks: {x: {}}\n")
    assert "goals.yaml is not valid" in by_name(run_all(tmp_path, env))["Career profile"].detail


def test_a_website_folder_that_is_not_the_site_fails(tmp_path: Path) -> None:
    env = env_for(tmp_path)
    (tmp_path / "site" / "src" / "data" / "projects.js").unlink()

    assert by_name(run_all(tmp_path, env))["Website folder"].status == health.FAIL


def test_the_profile_and_website_must_be_on_the_same_account(tmp_path: Path) -> None:
    profile(tmp_path, "https://github.com/me/career-profile.git")
    website(tmp_path, "https://github.com/someone-else/site.git")
    env = {
        "ANTHROPIC_API_KEY": "k",
        "GITHUB_TOKEN": "t",
        "AGENT_STUDIO_PROFILE": str(tmp_path / "career-profile"),
        "AGENT_STUDIO_WEBSITE": str(tmp_path / "site"),
    }

    assert by_name(run_all(tmp_path, env))["Same GitHub account"].status == health.FAIL


# --- the app ---------------------------------------------------------------------------------


class Launched:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, Path]] = []

    def __call__(self, command: list[str], cwd: Path, log: Path) -> None:
        self.calls.append((command, cwd, log))


class Creator:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, Path, bool]] = []

    def __call__(
        self, name: str, projects_dir: Path, *, public: bool, confirm: Any, **_: Any
    ) -> NewProject:
        assert confirm("sure?") is True  # the browser's confirmation page stands in for this
        self.calls.append((name, projects_dir, public))
        if self.error:
            raise NewProjectError(self.error)
        return NewProject(
            name=name,
            owner="me",
            path=projects_dir / name,
            url=f"https://github.com/me/{name}",
            public=public,
        )


@pytest.fixture
def web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Launched, Creator, Path]:
    for key, value in env_for(tmp_path).items():
        monkeypatch.setenv(key, value)
    runs = tmp_path / "runs"
    runs.mkdir()
    launched, creator = Launched(), Creator()
    app = create_app(
        runs,
        make_config(),
        config_path=CONFIG,
        launcher=launched,
        projects_dir=tmp_path / "clones",
        creator=creator,
        health=lambda: run_all(tmp_path),
    )
    return TestClient(app), launched, creator, runs


def token(client: TestClient) -> str:
    return client.get("/").text.split('<meta name="csrf" content="')[1].split('"')[0]


def submit(client: TestClient, path: str, **fields: str) -> Any:
    data = {
        "name": "my-project",
        "idea": "A tool that tracks student org events.",
        "track": "engineering",
        "agent": "full",
        "visibility": "private",
        **fields,
    }
    return client.post(path, data={"csrf": token(client), **data}, follow_redirects=False)


def test_the_health_page_lists_every_check_and_says_when_nothing_blocks(
    web: tuple[TestClient, Launched, Creator, Path],
) -> None:
    client = web[0]

    page = client.get("/health").text

    assert "Anthropic API key" in page and "gitleaks" in page
    assert "Nothing will stop a full run" in page


def test_the_health_page_counts_what_will_stop_a_run(tmp_path: Path) -> None:
    app = create_app(
        tmp_path,
        make_config(),
        config_path=CONFIG,
        health=lambda: [health.Check("gitleaks", health.FAIL, "Not installed.", "brew")],
    )

    page = TestClient(app).get("/health").text

    assert "1 thing(s) will stop a full run" in page and "Not installed." in page


def test_the_new_project_form_offers_the_choices(
    web: tuple[TestClient, Launched, Creator, Path],
) -> None:
    page = web[0].get("/new").text

    for text in ("The idea, in a paragraph", "engineering", "consulting", "Practice", "Private"):
        assert text in page


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "Bad Name"}, "lowercase letters"),
        ({"idea": "short"}, "10 to 2000 characters"),
        ({"track": "astronaut"}, "Choose a track"),
        ({"agent": "other"}, "Choose how to run it"),
        ({"visibility": "public"}, "confirm you want a public repository"),
    ],
)
def test_a_bad_form_is_sent_back_with_what_to_fix(
    web: tuple[TestClient, Launched, Creator, Path], fields: dict[str, str], message: str
) -> None:
    response = submit(web[0], "/new/preview", **fields)

    assert response.status_code == 400 and message in response.text
    assert "my-project" in response.text or "Bad Name" in response.text  # what you typed is kept


def test_a_name_that_is_taken_by_a_project_or_a_folder_is_refused(
    web: tuple[TestClient, Launched, Creator, Path], tmp_path: Path
) -> None:
    client, _launched, _creator, runs = web
    make_project(runs, "taken")
    (tmp_path / "clones" / "folder-taken").mkdir(parents=True)

    assert "already exists" in submit(client, "/new/preview", name="taken").text
    assert (
        "already exists on this computer"
        in submit(client, "/new/preview", name="folder-taken").text
    )
    assert submit(client, "/new/preview", name="folder-taken", agent="echo").status_code == 200


def test_a_full_run_is_blocked_when_a_key_or_the_profile_is_missing(
    web: tuple[TestClient, Launched, Creator, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = web[0]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("AGENT_STUDIO_PROFILE")

    full = submit(client, "/new/preview")
    practice = submit(client, "/new/preview", agent="echo")

    assert full.status_code == 400
    assert "Anthropic API key" in full.text and "Career profile" in full.text
    assert practice.status_code == 200  # the dummy agent needs neither


def test_the_confirmation_says_exactly_what_will_be_created(
    web: tuple[TestClient, Launched, Creator, Path], tmp_path: Path
) -> None:
    page = submit(web[0], "/new/preview", visibility="public", public_ok="on").text

    assert "create a <strong>public</strong> GitHub repository" in page
    assert str(tmp_path / "clones" / "my-project") in page and "spend real money" in page
    assert 'action="/new/start"' in page


def test_confirming_creates_the_repository_and_starts_a_detached_run(
    web: tuple[TestClient, Launched, Creator, Path], tmp_path: Path
) -> None:
    client, launched, creator, runs = web

    response = submit(client, "/new/start")

    assert response.status_code == 303 and response.headers["location"] == "/p/my-project"
    assert creator.calls == [("my-project", tmp_path / "clones", False)]
    [(command, cwd, log)] = launched.calls
    assert command[command.index("--agent") + 1] == "full" and "--ui" in command
    assert command[command.index("--repo") + 1] == str(tmp_path / "clones" / "my-project")
    assert command[command.index("--slug") + 1] == "my-project"
    assert "A tool that tracks student org events." in command
    assert cwd == CONFIG.resolve().parent and log == runs / "my-project" / "run.log"


def test_a_practice_run_creates_no_repository(
    web: tuple[TestClient, Launched, Creator, Path],
) -> None:
    client, launched, creator, _runs = web

    submit(client, "/new/start", agent="echo")

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

    response = submit(TestClient(app), "/new/start")

    assert response.status_code == 409 and "already exists on GitHub" in response.text
    assert launched.calls == []


def test_starting_a_project_needs_the_form_token(
    web: tuple[TestClient, Launched, Creator, Path],
) -> None:
    client, launched, creator, _runs = web

    response = client.post(
        "/new/start", data={"name": "my-project", "idea": "x" * 20, "agent": "echo"}
    )

    assert response.status_code == 403 and launched.calls == [] and creator.calls == []


def test_the_command_passes_the_idea_as_one_argument_not_through_a_shell() -> None:
    command = start_command(
        idea="a; rm -rf /",
        slug="p",
        agent="echo",
        track="engineering",
        repo=None,
        config_path=CONFIG,
        runs_dir=Path("runs"),
    )

    assert "a; rm -rf /" in command and command[command.index("run") + 1] == "a; rm -rf /"


def test_a_project_that_is_still_starting_shows_its_log_until_the_state_exists(
    web: tuple[TestClient, Launched, Creator, Path],
) -> None:
    client, _l, _c, runs = web
    (runs / "my-project").mkdir()
    (runs / "my-project" / "run.log").write_text("Starting...\n<script>alert(1)</script>\n")

    page = client.get("/p/my-project").text

    assert "Starting my-project" in page and 'http-equiv="refresh"' in page
    assert "&lt;script&gt;" in page and "<script>alert" not in page
    assert client.get("/p/never-started").status_code == 404
