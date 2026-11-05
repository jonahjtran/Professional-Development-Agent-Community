import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.gates import GateDecision, GateOutcome
from orchestrator.pipeline import AgentRequest, AgentResult, PipelineContext, advance, run_pipeline
from orchestrator.render import render_adr, render_architecture
from orchestrator.stages import architect_stage, prd_stage
from orchestrator.state import Artifact, Stage, Status
from schemas import ADR, PRD, ArchitectOutput, TaskList
from tests.factories import make_config, new_state
from tests.test_pipeline import FakeCaps, FakeGates, changes
from tests.test_product_owner_stage import prd_json
from tests.test_schemas import architecture_data, task_data


def architect_json(*, criteria: str = "Reports weekly active officers", **arch: Any) -> str:
    return ArchitectOutput.model_validate(
        {
            "architecture": architecture_data(**arch),
            "task_list": {"tasks": [task_data(acceptance_criteria=[criteria])]},
        }
    ).model_dump_json()


class StageAgent:
    """Answers each stage with a valid document, and records every request."""

    def __init__(self, *, architect_outputs: list[str] | None = None) -> None:
        self.architect_outputs = architect_outputs or [architect_json()]
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if request.stage == Stage.PRODUCT_OWNER:
            return AgentResult(output=prd_json(), cost_usd=0.2)
        if request.stage == Stage.ARCHITECT:
            n = sum(1 for r in self.requests if r.stage == Stage.ARCHITECT)
            return AgentResult(
                output=self.architect_outputs[min(n, len(self.architect_outputs)) - 1],
                cost_usd=0.3,
            )
        return AgentResult(output=f"{request.stage.value} text", cost_usd=0.1)

    def for_stage(self, stage: Stage) -> list[AgentRequest]:
        return [r for r in self.requests if r.stage == stage]


def make_ctx(tmp_path: Path, agent: StageAgent, gates: FakeGates | None = None) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent,
        gates=gates or FakeGates(),
        caps=FakeCaps(),
        stages={Stage.PRODUCT_OWNER: prd_stage(), Stage.ARCHITECT: architect_stage()},
    )


# --- rendering -------------------------------------------------------------------------------


def output() -> ArchitectOutput:
    return ArchitectOutput.model_validate_json(architect_json())


def test_the_architecture_markdown_covers_every_part_and_the_tasks() -> None:
    text = render_architecture(output(), unmapped=[])

    for heading in ("Components", "Data flow", "Interfaces", "Tech choices", "Risks", "Tasks"):
        assert f"## {heading}" in text
    assert "FastAPI" in text and "SQLite" in text and "Create the events table" in text
    assert "Warnings" not in text


def test_unmapped_metrics_are_flagged_for_the_human() -> None:
    text = render_architecture(output(), unmapped=["Event creation time"])

    assert "## Warnings" in text
    assert "Event creation time" in text


def test_an_adr_is_rendered_with_its_number_title_and_sections() -> None:
    adr = ADR(
        number=2,
        title="Use SQLite",
        context="One user",
        decision="SQLite",
        consequences="No concurrent writers",
    )

    text = render_adr(adr)

    assert text.startswith("# ADR 002: Use SQLite")
    for heading in ("Context", "Decision", "Consequences"):
        assert f"## {heading}" in text


# --- the stage -------------------------------------------------------------------------------


def at_architect(tmp_path: Path, agent: StageAgent, gates: FakeGates | None = None) -> Any:
    """Run the pipeline up to the point where the Architect has just been asked."""
    ctx = make_ctx(tmp_path, agent, gates or FakeGates())
    state = new_state().evolve(stage=Stage.PRODUCT_OWNER)
    state = advance(state, ctx)  # product owner, approved
    return state, ctx


def test_the_architect_receives_only_the_approved_prd_json(tmp_path: Path) -> None:
    agent = StageAgent()
    state, ctx = at_architect(tmp_path, agent)

    advance(state, ctx)

    [request] = agent.for_stage(Stage.ARCHITECT)
    assert list(request.inputs) == ["prd.json"]
    assert PRD.model_validate_json(request.inputs["prd.json"]).problem


def test_the_architect_cannot_start_before_the_prd_is_approved(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, StageAgent())
    state = new_state().evolve(stage=Stage.ARCHITECT)

    with pytest.raises(ValueError, match="prd.json"):
        advance(state, ctx)


def test_the_architect_stores_the_design_the_task_list_and_one_adr_per_tech_choice(
    tmp_path: Path,
) -> None:
    state, ctx = at_architect(tmp_path, StageAgent())

    state = advance(state, ctx)

    assert set(state.artifacts) >= {
        "architecture.md",
        "architecture.json",
        "tasks.json",
        "adr/001-use-sqlite.md",
    }
    tasks_path = tmp_path / "demo" / state.artifacts["tasks.json"].path
    assert TaskList.model_validate_json(tasks_path.read_text()).tasks[0].id == "T1"


def test_every_tech_choice_gets_its_own_numbered_adr(tmp_path: Path) -> None:
    arch = architect_json(
        tech_choices=[
            {"choice": "SQLite", "alternatives": ["Postgres"], "rationale": "simple"},
            {"choice": "FastAPI", "alternatives": ["Flask"], "rationale": "typed"},
        ]
    )
    state, ctx = at_architect(tmp_path, StageAgent(architect_outputs=[arch]))

    state = advance(state, ctx)

    adrs = sorted(n for n in state.artifacts if n.startswith("adr/"))
    assert adrs == ["adr/001-use-sqlite.md", "adr/002-use-fastapi.md"]


def test_the_human_sees_the_design_and_a_warning_about_unmapped_metrics(tmp_path: Path) -> None:
    gates = FakeGates()
    agent = StageAgent(architect_outputs=[architect_json(criteria="Something unrelated")])
    state, ctx = at_architect(tmp_path, agent, gates)

    advance(state, ctx)

    summary = gates.requests[-1].summary
    assert gates.requests[-1].stage == Stage.ARCHITECT
    assert "## Components" in summary
    assert "Weekly active officers" in summary and "## Warnings" in summary


def test_a_fully_mapped_design_has_no_warning(tmp_path: Path) -> None:
    gates = FakeGates()
    state, ctx = at_architect(tmp_path, StageAgent(), gates)

    advance(state, ctx)

    assert "## Warnings" not in gates.requests[-1].summary


def test_approving_marks_the_whole_set_approved(tmp_path: Path) -> None:
    state, ctx = at_architect(tmp_path, StageAgent())

    state = advance(state, ctx)

    assert state.stage == Stage.DEVELOPER
    assert all(a.approved_at is not None for a in state.artifacts.values())


def test_comments_revise_the_whole_design_and_replace_the_adrs(tmp_path: Path) -> None:
    first = architect_json()
    second = architect_json(
        tech_choices=[{"choice": "Postgres", "alternatives": ["SQLite"], "rationale": "scale"}]
    )
    agent = StageAgent(architect_outputs=[first, second])
    gates = FakeGates(GateDecision(outcome=GateOutcome.APPROVE), changes("Use a real database"))
    state, ctx = at_architect(tmp_path, agent, gates)

    state = run_pipeline(state, ctx)

    second_request = agent.for_stage(Stage.ARCHITECT)[1]
    assert second_request.comments == "Use a real database"
    assert second_request.previous_draft is not None
    assert ArchitectOutput.model_validate_json(second_request.previous_draft)
    assert state.artifacts["tasks.json"].revision == 2
    # The first draft's ADR is gone; only the revised design's ADR remains.
    assert [n for n in state.artifacts if n.startswith("adr/")] == ["adr/001-use-postgres.md"]
    assert state.status == Status.DONE


def test_an_invalid_task_list_halts_the_run_when_nothing_retries_it(tmp_path: Path) -> None:
    cyclic = json.dumps(
        {
            "architecture": architecture_data(),
            "task_list": {
                "tasks": [task_data(id="A", depends_on=["B"]), task_data(id="B", depends_on=["A"])]
            },
        }
    )
    state, ctx = at_architect(tmp_path, StageAgent(architect_outputs=[cyclic]))

    state = advance(state, ctx)

    assert state.status == Status.HALTED
    assert "tasks.json" not in state.artifacts


def test_the_full_pipeline_runs_from_idea_to_done_with_both_real_stages(tmp_path: Path) -> None:
    agent = StageAgent()
    ctx = make_ctx(tmp_path, agent)

    state = run_pipeline(new_state(), ctx)

    assert state.status == Status.DONE
    assert [r.stage for r in agent.requests][:2] == [Stage.PRODUCT_OWNER, Stage.ARCHITECT]
    assert isinstance(state.artifacts["prd.json"], Artifact)
