"""Paying for the Developer with your Claude subscription instead of API credits."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from orchestrator.developer_backend import developer_env
from orchestrator.developer_billing import (
    BILLINGS,
    SubscriptionStatus,
    check_subscription,
    get_default_billing,
    get_developer_billing,
    set_global_billing,
)
from orchestrator.pipeline import AgentError
from tests.factories import make_config
from tests.test_dev_runner import Harness, commit_a_file, git, request


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


# --- the setting ---------------------------------------------------------------------------


def test_the_default_is_api_credits(tmp_path: Path) -> None:
    choice = get_developer_billing(tmp_path / "runs", "demo")

    assert (choice.billing, choice.source) == ("api", "default")
    assert BILLINGS == ("api", "subscription")


def test_a_global_choice_applies_to_every_project(tmp_path: Path) -> None:
    set_global_billing("subscription")

    assert get_default_billing().billing == "subscription"
    choice = get_developer_billing(tmp_path / "runs", "demo")
    assert (choice.billing, choice.source) == ("subscription", "global")


def test_a_leftover_project_setting_is_ignored_the_global_one_applies_everywhere(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    set_global_billing("subscription")
    (runs / "demo").mkdir(parents=True)
    (runs / "demo" / "settings.json").write_text('{"developer_billing": "api"}')

    choice = get_developer_billing(runs, "demo")

    assert (choice.billing, choice.source) == ("subscription", "global")


def test_an_unknown_billing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one of"):
        set_global_billing("free")
    with pytest.raises(ValueError, match="one of"):
        set_global_billing("free")


# --- what the Developer's process is given -------------------------------------------------


def test_a_subscription_task_gets_no_api_key() -> None:
    env = developer_env("claude", make_config().local, "subscription")

    assert env["ANTHROPIC_API_KEY"] == ""  # blank, so Claude Code falls back to your sign-in


def test_an_api_task_is_left_alone() -> None:
    env = developer_env("claude", make_config().local, "api")

    assert "ANTHROPIC_API_KEY" not in env


def test_billing_does_not_change_a_local_task() -> None:
    local = make_config().local

    assert developer_env("local", local, "subscription") == developer_env("local", local, "api")


def test_the_credentials_other_than_the_key_are_still_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-real-oauth-token")

    env = developer_env("claude", make_config().local, "subscription")

    assert env["GITHUB_TOKEN"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # the sign-in itself must reach Claude Code


# --- is the Developer signed in ------------------------------------------------------------


def status_of(**fields: Any) -> Any:
    return lambda: json.dumps(fields)


def test_signed_in_with_a_claude_account_is_ready() -> None:
    status = check_subscription(run=status_of(loggedIn=True, authMethod="claude.ai"))

    assert status.ok and not status.skipped


def test_not_signed_in_says_how_to_sign_in() -> None:
    status = check_subscription(run=status_of(loggedIn=False))

    assert not status.ok
    assert "claude auth login" in status.fix and "setup-token" in status.fix


def test_a_task_that_would_still_use_an_api_key_is_not_ready() -> None:
    status = check_subscription(run=status_of(loggedIn=True, authMethod="api_key"))

    assert not status.ok and "API key" in status.detail
    assert "claude auth login" in status.fix


@pytest.mark.parametrize("output", [None, "", "not json", "[]"])
def test_when_the_check_cannot_run_the_task_is_not_blocked(output: str | None) -> None:
    status = check_subscription(run=lambda: output)

    assert status.ok and status.skipped  # a real sign-in problem is reported by the task itself


def test_the_check_never_returns_the_account_details() -> None:
    status = check_subscription(
        run=status_of(loggedIn=True, authMethod="claude.ai", email="me@example.com", orgId="o")
    )

    assert "me@example.com" not in repr(status)


def test_the_real_check_blanks_the_api_key_and_reads_only_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(
            cmd, 0, '{"loggedIn": true, "authMethod": "claude.ai"}', ""
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr("orchestrator.developer_billing.subprocess.run", fake_run)

    assert check_subscription().ok
    assert seen["cmd"][-2:] == ["auth", "status"]
    assert seen["env"]["ANTHROPIC_API_KEY"] == ""


def test_a_missing_claude_program_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr("orchestrator.developer_billing.subprocess.run", missing)

    status = check_subscription()

    assert status.ok and status.skipped


# --- the Developer runner ------------------------------------------------------------------


class SubscriptionHarness(Harness):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.subscription = SubscriptionStatus(True, "signed in", "")

    def runner(self, *, studio_root: Path | None = None) -> Any:
        runner = super().runner(studio_root=studio_root)
        runner._subscription_check = lambda: self.subscription
        return runner


def test_a_subscription_task_runs_without_the_api_key(repo: Path, tmp_path: Path) -> None:
    set_global_billing("subscription")
    harness = SubscriptionHarness(repo, tmp_path)

    harness.runner().run(request())

    assert harness.options[0].env["ANTHROPIC_API_KEY"] == ""


def test_an_api_task_does_not_check_the_sign_in(repo: Path, tmp_path: Path) -> None:
    harness = SubscriptionHarness(repo, tmp_path)
    harness.subscription = SubscriptionStatus(False, "not signed in", "Sign in.")

    harness.runner().run(request())

    assert len(harness.options) == 1 and "ANTHROPIC_API_KEY" not in harness.options[0].env


def test_not_signed_in_pauses_with_how_to_fix_it_and_starts_nothing(
    repo: Path,
    tmp_path: Path,
) -> None:
    set_global_billing("subscription")
    harness = SubscriptionHarness(repo, tmp_path)
    harness.subscription = SubscriptionStatus(
        False, "You are not signed in to Claude.", "Run `claude auth login`."
    )

    with pytest.raises(AgentError) as error:
        harness.runner().run(request())

    assert "not signed in" in str(error.value) and "claude auth login" in str(error.value)
    assert harness.options == []
    assert not (tmp_path / "worktrees" / "project" / "T1").exists()


def test_a_local_task_ignores_the_subscription_setting(repo: Path, tmp_path: Path) -> None:
    from orchestrator.developer_backend import set_global_backend

    set_global_backend("local")
    set_global_billing("subscription")
    harness = SubscriptionHarness(repo, tmp_path)
    harness.subscription = SubscriptionStatus(False, "not signed in", "")

    harness.runner().run(request())

    assert len(harness.options) == 1  # Ollama does not use your Claude account


def failing_turn(error: str | None, subtype: str = "error_during_execution") -> Any:
    def turn(cwd: Path) -> list[Any]:
        return [
            AssistantMessage(
                content=[TextBlock(text="x")],
                model="claude-sonnet-5-5",
                error=error,  # type: ignore[arg-type]
            ),
            ResultMessage(
                subtype=subtype,
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="s",
                total_cost_usd=None,
                usage={},
                result=None,
            ),
        ]

    return turn


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("authentication_failed", "claude auth login"),
        ("rate_limit", "usage limit"),
        ("billing_error", "plan"),
    ],
)
def test_a_subscription_failure_says_what_happened_and_what_to_do(
    repo: Path,
    tmp_path: Path,
    error: str,
    expected: str,
) -> None:
    set_global_billing("subscription")
    harness = SubscriptionHarness(repo, tmp_path, failing_turn(error))

    with pytest.raises(AgentError) as raised:
        harness.runner().run(request())

    assert expected in str(raised.value)


def test_a_plain_failure_is_unchanged(repo: Path, tmp_path: Path) -> None:
    set_global_billing("subscription")
    harness = SubscriptionHarness(repo, tmp_path, failing_turn(None))

    with pytest.raises(AgentError, match="the Developer failed"):
        harness.runner().run(request())


def test_a_subscription_task_does_not_need_the_sdk_to_report_a_price(
    repo: Path,
    tmp_path: Path,
) -> None:
    set_global_billing("subscription")

    def unpriced(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        from tests.test_dev_runner import result_message

        return [result_message(total_cost_usd=None)]

    result = SubscriptionHarness(repo, tmp_path, unpriced).runner().run(request())

    assert result.cost_usd == 0.0  # nothing to bill: it came out of your plan


def test_a_subscription_task_still_counts_the_estimate_the_sdk_gives(
    repo: Path,
    tmp_path: Path,
) -> None:
    set_global_billing("subscription")

    result = SubscriptionHarness(repo, tmp_path).runner().run(request())

    assert result.cost_usd == pytest.approx(0.5)  # an estimate, kept so the caps still guard usage
