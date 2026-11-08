"""Your real Claude plan usage: what Anthropic reports, kept per call, task and project."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.plan_usage import (
    PlanUsageError,
    Reading,
    Window,
    load_reading,
    project_usage,
    reading_from_raw,
    record_task_usage,
    save_reading,
    snapshot,
    task_window_change,
    tokens_from,
)

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)

RAW = {
    "status": "allowed",
    "resetsAt": 1794133200,
    "rateLimitType": "five_hour",
    "overageStatus": "allowed",
    "overageResetsAt": 1796083200,
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.2, "resetsAt": 1794133200, "status": "allowed"},
        "seven_day": {"utilization": 0.05, "resetsAt": 1794686400, "status": "allowed"},
    },
}


def reading(five: float = 0.2, week: float = 0.05, *, resets: int = 1794133200) -> Reading:
    return Reading(
        ts=NOW,
        windows={
            "five_hour": Window(utilization=five, resets_at=resets, status="allowed"),
            "seven_day": Window(utilization=week, resets_at=1794686400, status="allowed"),
        },
        overage_status="allowed",
        using_overage=False,
    )


# --- reading what Claude Code reports ----------------------------------------------------------


def test_the_windows_come_from_what_the_server_reported() -> None:
    parsed = reading_from_raw(RAW, now=NOW)

    assert parsed is not None
    assert parsed.windows["five_hour"] == Window(0.2, 1794133200, "allowed")
    assert parsed.windows["seven_day"].utilization == pytest.approx(0.05)
    assert parsed.overage_status == "allowed" and parsed.using_overage is False
    assert parsed.ts == NOW


def test_a_report_with_only_the_typed_fields_still_gives_that_window() -> None:
    raw = {"status": "allowed_warning", "resetsAt": 5, "rateLimitType": "five_hour"}

    parsed = reading_from_raw({**raw, "utilization": 0.9}, now=NOW)

    assert parsed is not None and parsed.windows["five_hour"].utilization == pytest.approx(0.9)
    assert parsed.windows["five_hour"].status == "allowed_warning"


def test_a_report_with_no_numbers_gives_no_reading() -> None:
    assert reading_from_raw({"status": "allowed", "rateLimitType": "five_hour"}, now=NOW) is None
    assert reading_from_raw({}, now=NOW) is None
    assert (
        reading_from_raw({"unifiedWindows": {"five_hour": {"status": "allowed"}}}, now=NOW) is None
    )


def test_a_bad_value_is_ignored_not_trusted() -> None:
    raw = {"unifiedWindows": {"five_hour": {"utilization": "lots", "resetsAt": "soon"}}}

    assert reading_from_raw(raw, now=NOW) is None


def test_the_latest_reading_is_kept_and_read_back(tmp_path: Path) -> None:
    save_reading(tmp_path, reading(0.3))

    loaded = load_reading(tmp_path)

    assert loaded is not None and loaded.windows["five_hour"].utilization == pytest.approx(0.3)
    assert loaded.ts == NOW


def test_no_reading_yet_or_a_damaged_file_is_simply_none(tmp_path: Path) -> None:
    assert load_reading(tmp_path) is None
    (tmp_path / "_studio").mkdir()
    (tmp_path / "_studio" / "plan_windows.json").write_text("{broken")
    assert load_reading(tmp_path) is None


# --- how far a task moved the windows ----------------------------------------------------------


def test_a_task_is_measured_from_the_reading_before_it_to_the_one_after() -> None:
    change = task_window_change(
        before=reading(0.20, 0.05), first=reading(0.21), last=reading(0.26, 0.06)
    )

    assert change["five_hour"] == pytest.approx(0.06)
    assert change["seven_day"] == pytest.approx(0.01)


def test_without_an_earlier_reading_the_first_one_of_the_task_is_the_start() -> None:
    change = task_window_change(before=None, first=reading(0.21), last=reading(0.26))

    assert change["five_hour"] == pytest.approx(0.05)


def test_an_earlier_reading_from_a_window_that_has_reset_is_not_used() -> None:
    old = reading(0.90, resets=1794000000)  # a different five-hour window

    change = task_window_change(before=old, first=reading(0.02), last=reading(0.05))

    assert change["five_hour"] == pytest.approx(0.03)  # measured inside the new window


def test_a_window_that_reset_during_the_task_has_no_change_to_report() -> None:
    change = task_window_change(
        before=None, first=reading(0.95, resets=1794000000), last=reading(0.01)
    )

    assert "five_hour" not in change


def test_a_change_is_never_negative() -> None:
    change = task_window_change(before=reading(0.30), first=reading(0.30), last=reading(0.29))

    assert change["five_hour"] == 0.0


# --- tokens ------------------------------------------------------------------------------------


def test_tokens_are_the_counts_the_api_reported() -> None:
    usage = {
        "input_tokens": 120,
        "output_tokens": 45,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 300,
        "service_tier": "standard",
    }

    assert tokens_from(usage) == {
        "input": 120,
        "output": 45,
        "cache_read": 9000,
        "cache_write": 300,
    }


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": "many"}])
def test_missing_or_odd_token_counts_are_zero(usage: Any) -> None:
    assert tokens_from(usage) == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


# --- per task and per project ------------------------------------------------------------------


def test_each_task_is_recorded_with_its_tokens_and_what_it_moved(tmp_path: Path) -> None:
    record_task_usage(
        tmp_path,
        "demo",
        task_id="T1",
        model="claude-sonnet-5-5",
        billing="subscription",
        tokens={"input": 10, "output": 5, "cache_read": 100, "cache_write": 7},
        change={"five_hour": 0.04, "seven_day": 0.01},
        now=NOW,
    )

    [row] = project_usage(tmp_path, "demo")["tasks"]

    assert row["task_id"] == "T1" and row["billing"] == "subscription"
    assert row["tokens"]["output"] == 5 and row["change"]["five_hour"] == pytest.approx(0.04)
    assert row["ts"] == NOW.isoformat()


def test_a_project_adds_up_its_tasks(tmp_path: Path) -> None:
    for n, pts in enumerate((0.02, 0.03), start=1):
        record_task_usage(
            tmp_path,
            "demo",
            task_id=f"T{n}",
            model="m",
            billing="subscription",
            tokens={"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
            change={"five_hour": pts},
            now=NOW,
        )

    totals = project_usage(tmp_path, "demo")["totals"]

    assert totals["tokens"]["input"] == 20 and totals["tokens"]["output"] == 10
    assert totals["change"]["five_hour"] == pytest.approx(0.05)
    assert totals["tasks"] == 2


def test_a_project_with_no_records_has_empty_totals(tmp_path: Path) -> None:
    usage = project_usage(tmp_path, "nobody")

    assert usage["tasks"] == [] and usage["totals"]["tasks"] == 0


# --- checking now ------------------------------------------------------------------------------


def test_checking_now_reads_the_windows_and_keeps_them(tmp_path: Path) -> None:
    def probe() -> list[dict[str, Any]]:
        return [RAW]

    result = snapshot(tmp_path, probe=probe, now=NOW)

    assert result.windows["five_hour"].utilization == pytest.approx(0.2)
    loaded = load_reading(tmp_path)
    assert loaded is not None and loaded.windows["seven_day"].utilization == pytest.approx(0.05)


def test_checking_now_uses_the_latest_report_it_got(tmp_path: Path) -> None:
    later = {**RAW, "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1794133200}}}

    result = snapshot(tmp_path, probe=lambda: [RAW, later], now=NOW)

    assert result.windows["five_hour"].utilization == pytest.approx(0.4)


def test_checking_now_without_a_report_says_so(tmp_path: Path) -> None:
    with pytest.raises(PlanUsageError, match="signed in"):
        snapshot(tmp_path, probe=lambda: [], now=NOW)


def test_a_probe_that_fails_is_reported_in_plain_words(tmp_path: Path) -> None:
    def broken() -> list[dict[str, Any]]:
        raise OSError("claude is not installed")

    with pytest.raises(PlanUsageError, match="not installed"):
        snapshot(tmp_path, probe=broken, now=NOW)
