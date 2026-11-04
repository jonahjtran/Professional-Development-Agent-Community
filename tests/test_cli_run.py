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
    assert "halted" in result.output.lower()
    assert load_state(tmp_path, "demo").status == Status.HALTED


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
