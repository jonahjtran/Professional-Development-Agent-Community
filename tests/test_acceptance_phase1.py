"""Phase 1 acceptance: the real `studio` program, run as a separate process.

These start the CLI the way a user would, feed it keystrokes, and kill it for real. They use the
free echo agent, so no API calls are made.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import yaml

from orchestrator.state import InterventionKind, Status, load_state

REPO = Path(__file__).parent.parent
CONFIG = REPO / "config.yaml"
LAUNCH = "from orchestrator.cli import app; app()"


def studio(tmp_path: Path, *args: str, config: Path = CONFIG) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", LAUNCH, *args, "--runs-dir", str(tmp_path), "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=REPO,
    )


def wait_until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def waiting_at_gate(tmp_path: Path, stage: str) -> bool:
    try:
        state = load_state(tmp_path, "demo")
    except (FileNotFoundError, ValueError):
        return False
    return (
        state.status == Status.AWAITING_HUMAN
        and state.intervention is not None
        and state.intervention.kind == InterventionKind.GATE
        and state.stage == stage
    )


def finish(process: subprocess.Popen[str], keystrokes: str) -> tuple[int, str]:
    out, _ = process.communicate(keystrokes, timeout=30)
    return process.returncode, out


def test_killing_the_process_mid_run_and_resuming_continues_correctly(tmp_path: Path) -> None:
    first = studio(tmp_path, "run", "Track events", "--slug", "demo")
    assert first.stdin is not None
    first.stdin.write("approve\n\n")  # approve the PRD
    first.stdin.flush()
    wait_until(lambda: waiting_at_gate(tmp_path, "architect"))
    spent_before = load_state(tmp_path, "demo").total_cost_usd

    first.kill()  # SIGKILL: no cleanup, like a crash or a closed laptop
    first.wait()

    saved = load_state(tmp_path, "demo")
    assert saved.status == Status.AWAITING_HUMAN
    assert saved.total_cost_usd == spent_before

    second = studio(tmp_path, "resume", "demo")
    code, out = finish(second, "approve\n\n" * 3)

    assert code == 0, out
    final = load_state(tmp_path, "demo")
    assert final.status == Status.DONE
    assert final.total_cost_usd > spent_before
    # The architect was not run a second time: its draft was already paid for.
    events = [json.loads(x) for x in (tmp_path / "demo" / "trace.jsonl").read_text().splitlines()]
    architect_starts = [
        e for e in events if e["event"] == "agent_start" and e["agent"] == "architect"
    ]
    assert len(architect_starts) == 1


def test_a_run_survives_a_kill_at_every_gate(tmp_path: Path) -> None:
    stages = ["product_owner", "architect", "qa", "career_coach"]
    approvals_done = 0
    process = studio(tmp_path, "run", "Track events", "--slug", "demo")
    for stage in stages:
        assert process.stdin is not None
        wait_until(partial(waiting_at_gate, tmp_path, stage))
        process.kill()
        process.wait()
        assert load_state(tmp_path, "demo").stage == stage
        process = studio(tmp_path, "resume", "demo")
        assert process.stdin is not None
        process.stdin.write("approve\n\n")
        process.stdin.flush()
        approvals_done += 1
    code, out = finish(process, "")

    assert code == 0, out
    assert load_state(tmp_path, "demo").status == Status.DONE
    assert approvals_done == 4


def test_a_small_budget_cap_halts_the_run_before_the_next_agent_call(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text())
    raw["budget"]["project_cap_usd"] = 0.15  # the echo agent costs $0.10 per call
    config = tmp_path / "small.yaml"
    config.write_text(yaml.safe_dump(raw))

    process = studio(tmp_path, "run", "Track events", "--slug", "demo", config=config)
    code, out = finish(process, "approve\n\n" * 2 + "abandon\n")

    assert code == 2, out
    state = load_state(tmp_path, "demo")
    assert state.status == Status.HALTED
    assert state.total_cost_usd < 0.35  # stopped before the Developer, not after every stage
    assert "paused" in out.lower()
