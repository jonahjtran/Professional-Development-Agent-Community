"""A Developer task keeps the real plan usage Claude Code reports while it works."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from orchestrator.developer_billing import set_project_billing
from orchestrator.pipeline import AgentError
from orchestrator.plan_usage import Reading, Window, load_reading, project_usage, save_reading
from tests.test_dev_runner import commit_a_file, git, request, result_message
from tests.test_developer_billing import SubscriptionHarness

RESETS = 1794133200


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def limit(five: float, week: float = 0.05) -> RateLimitEvent:
    raw = {
        "status": "allowed",
        "resetsAt": RESETS,
        "rateLimitType": "five_hour",
        "overageStatus": "allowed",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": RESETS, "status": "allowed"},
            "seven_day": {"utilization": week, "resetsAt": 1794686400, "status": "allowed"},
        },
    }
    info = RateLimitInfo(status="allowed", resets_at=RESETS, rate_limit_type="five_hour", raw=raw)
    return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")


def working_turn(*events: RateLimitEvent, usage: dict[str, Any] | None = None) -> Any:
    def turn(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        return [
            *events,
            AssistantMessage(content=[TextBlock(text="working")], model="claude-sonnet-5-5"),
            result_message(
                usage=usage
                or {
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 300,
                }
            ),
        ]

    return turn


def before(five: float) -> Reading:
    return Reading(
        ts=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        windows={
            "five_hour": Window(five, RESETS, "allowed"),
            "seven_day": Window(0.05, 1794686400, "allowed"),
        },
    )


def test_a_task_records_its_real_tokens_and_how_far_it_moved_the_windows(
    repo: Path, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    set_project_billing(runs, "demo", "subscription")
    save_reading(runs, before(0.10))
    harness = SubscriptionHarness(repo, tmp_path, working_turn(limit(0.12), limit(0.16, 0.06)))

    harness.runner().run(request())

    [row] = project_usage(runs, "demo")["tasks"]
    assert row["task_id"] == "T1" and row["billing"] == "subscription"
    assert row["tokens"] == {"input": 120, "output": 45, "cache_read": 9000, "cache_write": 300}
    assert row["change"]["five_hour"] == pytest.approx(0.06)  # from the reading before, 0.10
    assert row["change"]["seven_day"] == pytest.approx(0.01)


def test_the_latest_reading_is_kept_for_the_usage_page(repo: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    set_project_billing(runs, "demo", "subscription")

    SubscriptionHarness(repo, tmp_path, working_turn(limit(0.12), limit(0.16))).runner().run(
        request()
    )

    latest = load_reading(runs)
    assert latest is not None and latest.windows["five_hour"].utilization == pytest.approx(0.16)


def test_without_an_earlier_reading_the_first_one_in_the_task_is_the_start(
    repo: Path, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    set_project_billing(runs, "demo", "subscription")

    SubscriptionHarness(repo, tmp_path, working_turn(limit(0.12), limit(0.16))).runner().run(
        request()
    )

    [row] = project_usage(runs, "demo")["tasks"]
    assert row["change"]["five_hour"] == pytest.approx(0.04)


def test_a_task_on_api_credits_still_records_its_tokens(repo: Path, tmp_path: Path) -> None:
    harness = SubscriptionHarness(repo, tmp_path)  # no rate-limit reports on an API key

    harness.runner().run(request())

    [row] = project_usage(tmp_path / "runs", "demo")["tasks"]
    assert row["billing"] == "api" and row["change"] == {}
    assert row["tokens"]["input"] == 100 and row["tokens"]["output"] == 50
    assert load_reading(tmp_path / "runs") is None


def test_a_local_task_records_nothing(repo: Path, tmp_path: Path) -> None:
    from orchestrator.developer_backend import set_project_backend

    set_project_backend(tmp_path / "runs", "demo", "local")

    SubscriptionHarness(repo, tmp_path).runner().run(request())

    assert project_usage(tmp_path / "runs", "demo")["tasks"] == []


def test_a_task_that_fails_still_records_what_it_used(repo: Path, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    set_project_billing(runs, "demo", "subscription")

    def failing(cwd: Path) -> list[Any]:
        return [
            limit(0.12),
            limit(0.20),
            ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.1,
                usage={"input_tokens": 7, "output_tokens": 3},
                result=None,
            ),
        ]

    with pytest.raises(AgentError):
        SubscriptionHarness(repo, tmp_path, failing).runner().run(request())

    [row] = project_usage(runs, "demo")["tasks"]
    assert row["tokens"]["input"] == 7
    assert row["change"]["five_hour"] == pytest.approx(0.08)


def test_a_problem_writing_the_record_never_breaks_the_task(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("orchestrator.dev_runner.record_task_usage", broken)

    result = SubscriptionHarness(repo, tmp_path).runner().run(request())

    assert result.output  # the task still finished
