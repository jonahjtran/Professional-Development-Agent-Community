import os
from pathlib import Path

import pytest


def live_skip_reason(*, live_flag: bool, api_key: str | None) -> str | None:
    """Why live tests (real API calls, real money) should be skipped, or None if they may run."""
    if not live_flag:
        return "live tests are opt-in: pass --live"
    if not api_key:
        return "live tests need ANTHROPIC_API_KEY to be set"
    return None


@pytest.fixture(autouse=True)
def _worktrees_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never create worktrees in the real ~/.cache; they go under the test's tmp folder."""
    monkeypatch.setenv("AGENT_STUDIO_WORKTREES", str(tmp_path / "worktrees"))


@pytest.fixture(autouse=True)
def _studio_home_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read or write the real ~/.config/agent-studio (the everywhere rules)."""
    monkeypatch.setenv("AGENT_STUDIO_HOME", str(tmp_path / "studio-home"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", default=False, help="run live API tests")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: calls the real Claude API and costs money")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    reason = live_skip_reason(
        live_flag=bool(config.getoption("--live")), api_key=os.environ.get("ANTHROPIC_API_KEY")
    )
    if reason is None:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
