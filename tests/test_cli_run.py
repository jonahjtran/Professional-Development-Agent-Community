import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from orchestrator.cli import app, slugify
from orchestrator.state import InterventionKind, Status, load_state

runner = CliRunner()
REPO_CONFIG = Path(__file__).parent.parent / "config.yaml"
FOUR_APPROVALS = "approve\n\n" * 4


def invoke(tmp_path: Path, *args: str, input: str = "", config: Path = REPO_CONFIG):  # type: ignore[no-untyped-def]
    return runner.invoke(
        app, [*args, "--runs-dir", str(tmp_path), "--config", str(config)], input=input
    )


def small_budget_config(tmp_path: Path, project_cap: float) -> Path:
    raw = yaml.safe_load(REPO_CONFIG.read_text())
    raw["budget"]["project_cap_usd"] = project_cap
    path = tmp_path / "small.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


# --- slugs -----------------------------------------------------------------------------------


def test_slugify_makes_a_safe_directory_name() -> None:
    assert (
        slugify("A tool that tracks Student-Org events!") == "a-tool-that-tracks-student-org-events"
    )


def test_slugify_limits_the_length() -> None:
    assert len(slugify("word " * 50)) <= 40


def test_slugify_rejects_text_with_no_usable_characters() -> None:
    with pytest.raises(ValueError, match="slug"):
        slugify("!!!")


# --- studio run ------------------------------------------------------------------------------


def test_run_takes_an_idea_through_every_gate_to_done(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", "--slug", "demo", input=FOUR_APPROVALS)

    assert result.exit_code == 0, result.output
    state = load_state(tmp_path, "demo")
    assert state.status == Status.DONE
    assert state.idea == "Track events"
    assert "done" in result.output.lower()


def test_run_derives_the_slug_from_the_idea(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", input=FOUR_APPROVALS)

    assert result.exit_code == 0, result.output
    assert load_state(tmp_path, "track-events").status == Status.DONE


def test_run_writes_the_trace_and_cost_report(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input=FOUR_APPROVALS)

    assert (tmp_path / "demo" / "trace.jsonl").exists()
    report = json.loads((tmp_path / "demo" / "cost.json").read_text())
    assert report["total_usd"] > 0


def test_run_refuses_to_overwrite_an_existing_project(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input=FOUR_APPROVALS)

    result = invoke(tmp_path, "run", "Something else", "--slug", "demo")

    assert result.exit_code != 0
    assert "resume" in result.output.lower()
    assert load_state(tmp_path, "demo").idea == "Track events"


def test_run_rejects_a_bad_slug(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", "--slug", "../escape")

    assert result.exit_code != 0


def test_run_reports_a_missing_config_file(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", config=tmp_path / "nope.yaml")

    assert result.exit_code != 0
    assert "config" in result.output.lower()


def test_a_halted_project_exits_nonzero_and_says_so(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", "--slug", "demo", input="reject\nNo\ny\n")

    assert result.exit_code == 2
    assert "paused" in result.output.lower()
    assert load_state(tmp_path, "demo").status == Status.HALTED  # stored as halted, shown as paused


def test_a_budget_cap_stops_the_run_and_abandon_halts_it(tmp_path: Path) -> None:
    config = small_budget_config(tmp_path, project_cap=0.15)

    result = invoke(
        tmp_path,
        "run",
        "Track events",
        "--slug",
        "demo",
        input="approve\n\n" * 2 + "abandon\n",
        config=config,
    )

    assert result.exit_code == 2
    assert load_state(tmp_path, "demo").status == Status.HALTED


# --- studio resume ---------------------------------------------------------------------------


def test_resume_continues_a_run_that_stopped_at_a_gate(tmp_path: Path) -> None:
    # The terminal input ends at the second gate, as if the process was killed there.
    first = invoke(tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n")
    assert first.exit_code != 0
    waiting = load_state(tmp_path, "demo")
    assert waiting.status == Status.AWAITING_HUMAN
    assert waiting.intervention is not None
    assert waiting.intervention.kind == InterventionKind.GATE

    result = invoke(tmp_path, "resume", "demo", input="approve\n\n" * 3)

    assert result.exit_code == 0, result.output
    assert load_state(tmp_path, "demo").status == Status.DONE


def test_a_paused_project_can_be_resumed_and_finished(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="reject\nNo\ny\n")
    assert load_state(tmp_path, "demo").status == Status.HALTED

    result = invoke(tmp_path, "resume", "demo", input="approve\n\n" * 5)

    assert result.exit_code == 0, result.output
    assert load_state(tmp_path, "demo").status == Status.DONE


def test_resume_clears_an_old_pause_request_so_the_run_does_not_stop_at_once(
    tmp_path: Path,
) -> None:
    from orchestrator.halt import halt_requested, request_halt

    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n")
    request_halt(tmp_path, "demo")

    result = invoke(tmp_path, "resume", "demo", input="approve\n\n" * 5)

    assert result.exit_code == 0, result.output
    assert not halt_requested(tmp_path, "demo")


def test_resuming_closes_the_items_raised_by_an_error_pause(tmp_path: Path) -> None:
    from orchestrator.todos import add_todo, open_todos

    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="reject\nNo\ny\n")
    add_todo(tmp_path, "demo", key="e", title="demo paused", source="error")
    add_todo(tmp_path, "demo", key="d", title="Add a key", source="developer")

    invoke(tmp_path, "resume", "demo", input="approve\n\n" * 5)

    assert [t.title for t in open_todos(tmp_path, "demo")] == ["Add a key"]


def test_resume_with_ui_answers_through_files_and_keeps_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import cli
    from orchestrator.inbox import FileGateBackend, run_is_alive

    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n")
    seen: dict[str, object] = {}

    def fake_pipeline(state, ctx):  # type: ignore[no-untyped-def]
        seen["gates"] = ctx.gates
        seen["alive"] = run_is_alive(tmp_path, "demo")
        return state.evolve(status=Status.DONE, intervention=None)

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)

    result = invoke(tmp_path, "resume", "demo", "--ui")

    assert result.exit_code == 0, result.output
    assert isinstance(seen["gates"], FileGateBackend)  # not the terminal prompt
    assert seen["alive"] is True  # so the UI does not offer to start a second run


def test_resume_does_not_rerun_the_agent_that_already_finished(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n")
    spent = load_state(tmp_path, "demo").total_cost_usd

    invoke(tmp_path, "resume", "demo", input="reject\nNo\ny\n")

    # The architect's draft was already paid for; resuming only asked the question again.
    assert load_state(tmp_path, "demo").total_cost_usd == pytest.approx(spent)


def test_resume_of_an_unknown_project_is_an_error(tmp_path: Path) -> None:
    result = invoke(tmp_path, "resume", "ghost")

    assert result.exit_code != 0
    assert "ghost" in result.output


def test_resume_of_a_finished_project_says_there_is_nothing_to_do(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input=FOUR_APPROVALS)

    result = invoke(tmp_path, "resume", "demo")

    assert result.exit_code == 0
    assert "nothing" in result.output.lower()


# --- choosing the agent ----------------------------------------------------------------------


def test_run_accepts_the_echo_agent_option(tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "run", "Track events", "--slug", "demo", "--agent", "echo", input=FOUR_APPROVALS
    )

    assert result.exit_code == 0, result.output


def test_run_rejects_an_unknown_agent(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", "--agent", "gpt")

    assert result.exit_code != 0


def test_resume_accepts_the_agent_option(tmp_path: Path) -> None:
    invoke(tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n")

    result = invoke(tmp_path, "resume", "demo", "--agent", "echo", input="approve\n\n" * 3)

    assert result.exit_code == 0, result.output


# --- studio send-back ------------------------------------------------------------------------


def send_back_cli(tmp_path: Path, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["send-back", *args, "--runs-dir", str(tmp_path)])


def finished_project(tmp_path: Path) -> None:
    result = invoke(tmp_path, "run", "Track events", "--slug", "demo", input=FOUR_APPROVALS)
    assert result.exit_code == 0, result.output
    assert load_state(tmp_path, "demo").status == Status.DONE


def test_a_finished_project_can_be_sent_back_and_then_resumed_to_finish_again(
    tmp_path: Path,
) -> None:
    finished_project(tmp_path)

    sent = send_back_cli(
        tmp_path,
        "demo",
        "--to",
        "product_owner",
        "--comment",
        "The PRD missed offline mode",
    )

    assert sent.exit_code == 0, sent.output
    assert "studio resume demo" in sent.output
    state = load_state(tmp_path, "demo")
    assert state.status == Status.RUNNING and state.stage.value == "product_owner"
    assert state.rework is not None and state.rework.note == "The PRD missed offline mode"

    result = invoke(tmp_path, "resume", "demo", input=FOUR_APPROVALS)

    assert result.exit_code == 0, result.output
    assert load_state(tmp_path, "demo").status == Status.DONE
    assert load_state(tmp_path, "demo").rework is None


def test_without_an_agent_the_coordinator_chooses_and_the_cli_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import cli
    from schemas import Route

    class Chooser:
        def route(self, state: object, request: str, allowed: object) -> Route:
            return Route(agent="architect", reason="It spans several parts.", instruction="Plan.")

    monkeypatch.setattr(cli, "_coordinator", lambda runs_dir, config: Chooser())
    finished_project(tmp_path)

    sent = send_back_cli(tmp_path, "demo", "--comment", "Make it pass the PRD")

    assert sent.exit_code == 0, sent.output
    assert "Architect" in sent.output and "It spans several parts." in sent.output
    state = load_state(tmp_path, "demo")
    assert state.stage.value == "architect" and state.handoff is not None
    assert "Make it pass the PRD" in state.handoff.note


def test_sending_back_an_unknown_project_is_an_error(tmp_path: Path) -> None:
    result = send_back_cli(tmp_path, "ghost", "--to", "qa", "--comment", "x")

    assert result.exit_code == 1 and "ghost" in result.output


def test_a_running_project_must_be_paused_before_it_is_sent_back(tmp_path: Path) -> None:
    invoke(
        tmp_path, "run", "Track events", "--slug", "demo", input="approve\n\n"
    )  # stops at a gate

    result = send_back_cli(tmp_path, "demo", "--to", "product_owner", "--comment", "x")

    assert result.exit_code == 1 and "Pause" in result.output


def test_an_unknown_agent_is_refused(tmp_path: Path) -> None:
    finished_project(tmp_path)

    result = send_back_cli(tmp_path, "demo", "--to", "intake", "--comment", "x")

    assert result.exit_code != 0


def test_the_instruction_is_required(tmp_path: Path) -> None:
    finished_project(tmp_path)

    result = send_back_cli(tmp_path, "demo", "--to", "qa", "--comment", "  ")

    assert result.exit_code == 1 and "1 to 2000" in result.output
