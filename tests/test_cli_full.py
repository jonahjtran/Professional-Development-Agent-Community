import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator.cli import AgentKind, _context, app, make_runner
from orchestrator.pipeline import RoutingRunner
from orchestrator.publish import Publisher
from tests.factories import make_config

runner = CliRunner()
REPO_CONFIG = Path(__file__).parent.parent / "config.yaml"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    return path


def test_the_full_option_sends_each_stage_to_its_own_runner(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    assert isinstance(make_runner(AgentKind.FULL, make_config(), tmp_path, git_repo), RoutingRunner)


def test_the_full_option_publishes_pull_requests_and_stores_documents(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_real")

    ctx = _context(REPO_CONFIG, tmp_path, AgentKind.FULL, git_repo)

    assert isinstance(ctx.publisher, Publisher)
    assert set(ctx.stages) >= {"product_owner", "architect"}


def test_the_other_options_have_no_publisher(tmp_path: Path, git_repo: Path) -> None:
    for kind in (AgentKind.ECHO, AgentKind.CLAUDE):
        assert _context(REPO_CONFIG, tmp_path, kind, git_repo).publisher is None


def test_full_stops_early_without_a_github_token(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    result = runner.invoke(
        app,
        ["run", "An idea", "--slug", "demo", "--agent", "full", "--repo", str(git_repo)]
        + ["--runs-dir", str(tmp_path / "runs"), "--config", str(REPO_CONFIG)],
    )

    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.output
    assert not (tmp_path / "runs" / "demo").exists()  # nothing was started or spent


def test_full_stops_early_when_the_repo_is_not_a_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    plain = tmp_path / "plain"
    plain.mkdir()

    result = runner.invoke(
        app,
        ["run", "An idea", "--slug", "demo", "--agent", "full", "--repo", str(plain)]
        + ["--runs-dir", str(tmp_path / "runs"), "--config", str(REPO_CONFIG)],
    )

    assert result.exit_code == 1
    assert "git repository" in result.output


def test_resume_also_accepts_the_repo_option(tmp_path: Path, git_repo: Path) -> None:
    result = runner.invoke(
        app,
        ["resume", "ghost", "--repo", str(git_repo), "--runs-dir", str(tmp_path)]
        + ["--config", str(REPO_CONFIG)],
    )

    assert "No project named ghost" in result.output
