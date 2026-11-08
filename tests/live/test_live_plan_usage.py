"""Live plan-usage test: Claude Code reports your real plan windows on a tiny call.

Spends one tiny message from your Claude plan (no API credits) and needs you to be signed in
(`claude auth login`). It checks that the report really carries the windows Studio reads (a
number from 0 up for each, and when it resets). A fake API key is set on purpose, as in the
subscription test. Run it with:

    ANTHROPIC_API_KEY=sk-ant-fake uv run pytest tests/live --live -s -k plan_usage
"""

from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.plan_usage import load_reading, sdk_probe, snapshot

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"


def test_a_tiny_call_returns_the_real_plan_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-not-a-real-key")

    reading = snapshot(tmp_path, probe=sdk_probe(load_config(CONFIG)))

    for name, window in reading.windows.items():
        print(
            f"{name}: {window.utilization:.0%} used, resets_at={window.resets_at}, {window.status}"
        )
    print("overage:", reading.overage_status, "in use:", reading.using_overage)
    assert "five_hour" in reading.windows
    assert all(w.utilization >= 0 for w in reading.windows.values())
    assert all(w.resets_at for w in reading.windows.values())
    kept = load_reading(tmp_path)
    assert kept is not None and set(kept.windows) == set(reading.windows)
