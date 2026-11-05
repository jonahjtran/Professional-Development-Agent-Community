import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.halt import clear_halt, halt_requested, request_halt
from orchestrator.permissions import PolicyContext
from orchestrator.pipeline import PipelineContext, advance, run_pipeline
from orchestrator.state import Stage, Status, load_state, save_state
from orchestrator.tool_hooks import ToolPermissionHooks
from orchestrator.trace import trace_path
from tests.factories import make_config, new_state
from tests.test_pipeline import FakeAgent, FakeCaps, FakeGates, start

runner = CliRunner()


# --- the flag --------------------------------------------------------------------------------


def test_no_halt_is_requested_by_default(tmp_path: Path) -> None:
    assert halt_requested(tmp_path, "demo") is False


def test_requesting_a_halt_is_seen_and_can_be_cleared(tmp_path: Path) -> None:
    request_halt(tmp_path, "demo")

    assert halt_requested(tmp_path, "demo") is True

    clear_halt(tmp_path, "demo")

    assert halt_requested(tmp_path, "demo") is False


def test_halting_one_project_does_not_halt_another(tmp_path: Path) -> None:
    request_halt(tmp_path, "one")

    assert halt_requested(tmp_path, "two") is False


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "UPPER", "-x"])
def test_unsafe_project_names_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="project name"):
        request_halt(tmp_path, name)


def test_clearing_when_nothing_was_requested_is_fine(tmp_path: Path) -> None:
    clear_halt(tmp_path, "demo")


# --- the pipeline ----------------------------------------------------------------------------


def make_ctx(tmp_path: Path, agent: FakeAgent, gates: FakeGates | None = None) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent,
        gates=gates or FakeGates(),
        caps=FakeCaps(),
    )


def test_a_requested_halt_stops_the_pipeline_before_the_next_agent_call(tmp_path: Path) -> None:
    agent = FakeAgent()
    request_halt(tmp_path, "demo")

    state = advance(start(stage=Stage.PRODUCT_OWNER), make_ctx(tmp_path, agent))

    assert state.status == Status.HALTED
    assert agent.requests == []
    assert load_state(tmp_path, "demo").status == Status.HALTED


def test_a_halt_requested_mid_run_stops_after_the_current_step(tmp_path: Path) -> None:
    agent = FakeAgent()

    class HaltingHuman(FakeGates):
        def decide(self, request: Any) -> Any:
            request_halt(tmp_path, "demo")  # the human hits the kill switch while reviewing
            return super().decide(request)

    state = run_pipeline(start(), make_ctx(tmp_path, agent, HaltingHuman()))

    assert state.status == Status.HALTED
    assert agent.stages == [Stage.PRODUCT_OWNER]  # nothing ran after the first gate


def test_the_halt_is_traced(tmp_path: Path) -> None:
    request_halt(tmp_path, "demo")

    advance(start(stage=Stage.PRODUCT_OWNER), make_ctx(tmp_path, FakeAgent()))

    events = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    halt = next(e for e in events if e["event"] == "transition")
    assert halt["details"]["to"] == "halted"
    assert "halt" in halt["details"]["reason"]


def test_a_halt_while_waiting_at_a_gate_takes_effect_when_the_pipeline_next_runs(
    tmp_path: Path,
) -> None:
    waiting = start(
        stage=Stage.ARCHITECT,
        status=Status.AWAITING_HUMAN,
        intervention={"kind": "gate", "detail": "Review"},
    )
    request_halt(tmp_path, "demo")

    state = advance(waiting, make_ctx(tmp_path, FakeAgent()))

    assert state.status == Status.HALTED
    assert state.intervention is None


# --- stopping a running Developer ------------------------------------------------------------


def make_hooks(tmp_path: Path, wt: Path, halted: bool) -> ToolPermissionHooks:
    class Nobody:
        def ask(self, request: Any) -> Any:
            raise AssertionError("no one should be asked")

    return ToolPermissionHooks(
        policy=PolicyContext(worktree=wt),
        backend=Nobody(),
        runs_dir=tmp_path / "runs",
        project="demo",
        task_id="T1",
        branch=lambda: "agent/T1",
        halt_check=lambda: halted,
    )


def pre(hooks: ToolPermissionHooks, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}
    return dict(asyncio.run(hooks.pre_tool_use(data, "t", {"signal": None})))


def test_a_halt_stops_the_running_agent_at_its_next_tool_call(tmp_path: Path) -> None:
    output = pre(make_hooks(tmp_path, tmp_path, halted=True), "Bash", {"command": "pytest"})

    assert output["continue_"] is False
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "halt" in output["hookSpecificOutput"]["permissionDecisionReason"]
    events = [json.loads(x) for x in trace_path(tmp_path / "runs", "demo").read_text().splitlines()]
    assert events[0]["event"] == "tool_denied"
    assert events[0]["details"]["verdict"] == "halt"


def test_without_a_halt_tool_calls_proceed_as_usual(tmp_path: Path) -> None:
    output = pre(make_hooks(tmp_path, tmp_path, halted=False), "Bash", {"command": "pytest"})

    assert output == {}


def test_the_developer_runner_checks_the_projects_halt_flag(tmp_path: Path) -> None:
    from tests.test_dev_runner import Harness, git, request

    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    harness = Harness(repo, tmp_path)
    harness.runner().run(request())
    assert harness.options[0].hooks is not None
    hook: Any = harness.options[0].hooks["PreToolUse"][0].hooks[0]
    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
    }
    request_halt(tmp_path / "runs", "demo")

    output: Any = asyncio.run(hook(data, "t", {"signal": None}))

    assert output["continue_"] is False


# --- the command -----------------------------------------------------------------------------


def invoke(tmp_path: Path, *args: str) -> Any:
    return runner.invoke(app, [*args, "--runs-dir", str(tmp_path)])


def test_studio_halt_sets_the_flag_for_a_running_project(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())

    result = invoke(tmp_path, "halt", "demo")

    assert result.exit_code == 0, result.output
    assert halt_requested(tmp_path, "demo")
    assert "stop" in result.output.lower()


def test_halting_an_unknown_project_is_an_error(tmp_path: Path) -> None:
    result = invoke(tmp_path, "halt", "ghost")

    assert result.exit_code == 1
    assert "ghost" in result.output
    assert not halt_requested(tmp_path, "ghost")


def test_halting_a_finished_project_says_there_is_nothing_to_stop(tmp_path: Path) -> None:
    save_state(tmp_path, new_state().evolve(status=Status.DONE))

    result = invoke(tmp_path, "halt", "demo")

    assert result.exit_code == 0
    assert "nothing to" in result.output.lower()
    assert not halt_requested(tmp_path, "demo")


def test_a_leftover_flag_does_not_block_a_different_new_project(tmp_path: Path) -> None:
    request_halt(tmp_path, "old")
    agent = FakeAgent()

    state = run_pipeline(new_state(), make_ctx(tmp_path, agent))

    assert state.status == Status.DONE


def test_halting_a_real_running_process_from_another_terminal(tmp_path: Path) -> None:
    from tests.test_acceptance_phase1 import finish, studio, wait_until, waiting_at_gate

    process = studio(tmp_path, "run", "Track events", "--slug", "demo")
    wait_until(lambda: waiting_at_gate(tmp_path, "product_owner"))

    stopped = invoke(tmp_path, "halt", "demo")  # a second terminal hits the switch
    code, out = finish(process, "approve\n\n")  # the human then answers the open gate

    assert stopped.exit_code == 0
    assert code == 2, out
    final = load_state(tmp_path, "demo")
    assert final.status == Status.HALTED
    assert final.stage == Stage.ARCHITECT  # it stopped before running the next agent
