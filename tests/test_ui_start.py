import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.newproject import NewProject, NewProjectError
from orchestrator.ui import health
from orchestrator.ui.app import start_command

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
