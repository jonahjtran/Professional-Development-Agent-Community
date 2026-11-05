import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.pipeline import AgentRequest, AgentResult, PipelineContext, advance, run_pipeline
from orchestrator.render import render_prd
from orchestrator.stages import prd_stage
from orchestrator.state import Stage, Status, load_state
from orchestrator.trace import trace_path
from schemas import PRD, Track
from tests.factories import make_config, new_state
from tests.test_pipeline import FakeCaps, FakeGates, changes, reject
from tests.test_schemas import prd_data


def prd_json(problem: str = "Orgs track events in spreadsheets.", **overrides: Any) -> str:
    return PRD.model_validate(prd_data(problem=problem, **overrides)).model_dump_json()


class PrdAgent:
    """Returns one PRD per call (the given problems, in order) and records every request."""

    def __init__(self, *problems: str, cost: float = 0.2) -> None:
        self.problems = list(problems) or ["Orgs track events in spreadsheets."]
        self.cost = cost
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        problem = self.problems[min(len(self.requests), len(self.problems)) - 1]
        return AgentResult(output=prd_json(problem), cost_usd=self.cost, prompt_version="v1")


def ctx_for(tmp_path: Path, agent: Any, gates: FakeGates | None = None) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent,
        gates=gates or FakeGates(),
        caps=FakeCaps(),
        stages={Stage.PRODUCT_OWNER: prd_stage()},
    )


def at_product_owner(**fields: Any) -> Any:
    return new_state().evolve(stage=Stage.PRODUCT_OWNER, **fields)


# --- track -----------------------------------------------------------------------------------


def test_a_project_defaults_to_the_engineering_track() -> None:
    assert new_state().track == Track.ENGINEERING


def test_the_track_survives_save_and_load(tmp_path: Path) -> None:
    from orchestrator.state import save_state

    save_state(tmp_path, new_state().evolve(track=Track.CONSULTING))

    assert load_state(tmp_path, "demo").track == Track.CONSULTING


def test_studio_run_saves_the_chosen_track(tmp_path: Path) -> None:
    config = Path(__file__).parent.parent / "config.yaml"

    CliRunner().invoke(
        app,
        ["run", "Track events", "--slug", "demo", "--track", "consulting"]
        + ["--runs-dir", str(tmp_path), "--config", str(config)],
        input="reject\nNo\ny\n",
    )

    assert load_state(tmp_path, "demo").track == Track.CONSULTING


def test_the_agent_is_told_the_track(tmp_path: Path) -> None:
    agent = PrdAgent()
    state = at_product_owner(track=Track.CONSULTING)

    advance(state, ctx_for(tmp_path, agent))

    assert agent.requests[0].track == "consulting"


# --- rendering -------------------------------------------------------------------------------


def test_the_rendered_prd_has_a_section_for_each_part_of_the_document() -> None:
    text = render_prd(PRD.model_validate(prd_data()))

    for heading in ("Problem", "Users", "Goals", "Success metrics", "Scope", "Non-goals", "Risks"):
        assert f"## {heading}" in text
    assert "Student orgs track events" in text
    assert "Weekly active officers" in text and "login logs" in text


def test_consulting_sections_appear_only_when_present() -> None:
    plain = render_prd(PRD.model_validate(prd_data()))
    consulting = render_prd(
        PRD.model_validate(
            prd_data(track="consulting", stakeholders=["Advisor"], adoption_risks=["Habit"])
        )
    )

    assert "Stakeholders" not in plain
    assert "## Stakeholders" in consulting and "## Adoption risks" in consulting


# --- the stage -------------------------------------------------------------------------------


def test_the_product_owner_output_is_stored_as_json_and_markdown_artifacts(tmp_path: Path) -> None:
    gates = FakeGates()

    state = advance(at_product_owner(), ctx_for(tmp_path, PrdAgent(), gates))

    assert set(state.artifacts) == {"prd.json", "prd.md"}
    assert (tmp_path / "demo" / state.artifacts["prd.md"].path).read_text().startswith("# ")
    assert PRD.model_validate_json(
        (tmp_path / "demo" / state.artifacts["prd.json"].path).read_text()
    )


def test_the_human_sees_the_rendered_prd_at_the_gate(tmp_path: Path) -> None:
    gates = FakeGates()

    advance(at_product_owner(), ctx_for(tmp_path, PrdAgent(), gates))

    assert "## Success metrics" in gates.requests[0].summary
    assert "Orgs track events in spreadsheets." in gates.requests[0].summary


def test_approving_marks_the_artifacts_approved(tmp_path: Path) -> None:
    state = advance(at_product_owner(), ctx_for(tmp_path, PrdAgent()))

    assert all(a.approved_at is not None for a in state.artifacts.values())
    assert state.stage == Stage.ARCHITECT


def test_artifacts_stay_unapproved_while_changes_are_requested(tmp_path: Path) -> None:
    gates = FakeGates(changes("Add non-goals"))

    state = advance(at_product_owner(), ctx_for(tmp_path, PrdAgent(), gates))

    assert state.stage == Stage.PRODUCT_OWNER
    assert all(a.approved_at is None for a in state.artifacts.values())


def test_comments_produce_a_revised_draft_with_a_diff_and_a_new_revision(tmp_path: Path) -> None:
    agent = PrdAgent("First problem statement.", "Second problem statement.")
    gates = FakeGates(changes("Sharpen the problem"))

    state = run_pipeline(at_product_owner(), ctx_for(tmp_path, agent, gates))

    second = agent.requests[1]
    assert second.comments == "Sharpen the problem"
    assert second.previous_draft is not None
    # The agent revises the document itself (JSON), not the Markdown shown to the human.
    assert PRD.model_validate_json(second.previous_draft).problem == "First problem statement."
    summary = gates.requests[1].summary
    assert "-First problem statement." in summary
    assert "+Second problem statement." in summary
    assert state.artifacts["prd.json"].revision == 2


def test_the_first_draft_says_there_is_nothing_to_compare(tmp_path: Path) -> None:
    gates = FakeGates()

    advance(at_product_owner(), ctx_for(tmp_path, PrdAgent(), gates))

    assert "first draft" in gates.requests[0].summary.lower()


def test_an_unchanged_revision_says_so(tmp_path: Path) -> None:
    agent = PrdAgent("Same.", "Same.")
    gates = FakeGates(changes("Tweak"))

    run_pipeline(at_product_owner(), ctx_for(tmp_path, agent, gates))

    assert "no changes" in gates.requests[1].summary.lower()


def test_a_rejection_starts_over_without_the_old_draft(tmp_path: Path) -> None:
    agent = PrdAgent("One.", "Two.")
    gates = FakeGates(reject())

    run_pipeline(at_product_owner(), ctx_for(tmp_path, agent, gates))

    assert agent.requests[1].previous_draft is None


def test_output_that_is_not_a_valid_prd_halts_the_run_and_keeps_the_cost(tmp_path: Path) -> None:
    class Bad:
        def run(self, request: AgentRequest) -> AgentResult:
            return AgentResult(output='{"problem": ""}', cost_usd=0.4)

    state = run_pipeline(at_product_owner(), ctx_for(tmp_path, Bad()))

    assert state.status == Status.HALTED
    assert state.cost_usd[Stage.PRODUCT_OWNER] == pytest.approx(0.4)
    assert state.artifacts == {}
    events = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    assert "valid" in next(e for e in events if e["event"] == "agent_end")["details"]["error"]


def test_the_trace_lists_the_artifacts_the_agent_produced(tmp_path: Path) -> None:
    advance(at_product_owner(), ctx_for(tmp_path, PrdAgent()))

    events = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    end = next(e for e in events if e["event"] == "agent_end")
    assert sorted(end["details"]["artifacts"]) == ["prd.json", "prd.md"]


def test_a_stage_without_a_spec_still_works_as_before(tmp_path: Path) -> None:
    from orchestrator.echo_agent import EchoAgent

    ctx = PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=EchoAgent(),
        gates=FakeGates(),
        caps=FakeCaps(),
    )

    done = run_pipeline(new_state(), ctx)

    assert done.status == Status.DONE
    assert done.artifacts == {}
