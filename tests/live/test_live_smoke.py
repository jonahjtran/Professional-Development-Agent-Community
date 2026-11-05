"""Live smoke test: one real idea through the Product Owner and Architect.

Costs real money (a few dollars at most) and needs ANTHROPIC_API_KEY. It is skipped unless you
pass --live:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s
"""

import json
from pathlib import Path

import pytest

from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.config import load_config
from orchestrator.default_specs import default_api_schemas, default_api_specs, default_stage_specs
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import PipelineContext, advance
from orchestrator.state import ProjectState, Stage, Status
from schemas import PRD, TaskList

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"
IDEA = "A small web app where student club officers plan events and track RSVPs."
SMOKE_TEST_BUDGET_USD = 5.0


class ScriptedHuman:
    def __init__(self, *decisions: GateDecision) -> None:
        self.decisions = list(decisions)
        self.requests: list[GateRequest] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        if self.decisions:
            return self.decisions.pop(0)
        return GateDecision(outcome=GateOutcome.APPROVE)


class NoCaps:
    def resolve(self, request: object) -> object:
        raise AssertionError("a budget or revision cap was hit during the smoke test")


def make_ctx(tmp_path: Path, human: ScriptedHuman) -> PipelineContext:
    config = load_config(CONFIG)
    return PipelineContext(
        config=config,
        runs_dir=tmp_path,
        runner=ValidatingRunner(ApiRunner(config, default_api_specs()), default_api_schemas()),
        gates=human,
        caps=NoCaps(),  # type: ignore[arg-type]
        stages=default_stage_specs(),
    )


def run_until_developer(state: ProjectState, ctx: PipelineContext) -> ProjectState:
    while state.status == Status.RUNNING and state.stage != Stage.DEVELOPER:
        state = advance(state, ctx)
    return state


def test_an_idea_becomes_a_valid_prd_and_task_list_with_costs_recorded(tmp_path: Path) -> None:
    human = ScriptedHuman()
    ctx = make_ctx(tmp_path, human)

    state = run_until_developer(ProjectState(project_slug="smoke", idea=IDEA), ctx)

    assert state.status == Status.RUNNING and state.stage == Stage.DEVELOPER
    project = tmp_path / "smoke"
    PRD.model_validate_json((project / state.artifacts["prd.json"].path).read_text())
    tasks = TaskList.model_validate_json((project / state.artifacts["tasks.json"].path).read_text())
    assert tasks.tasks

    cost = json.loads((project / "cost.json").read_text())
    assert cost["stages"]["product_owner"] > 0
    assert cost["stages"]["architect"] > 0
    assert cost["total_usd"] < SMOKE_TEST_BUDGET_USD
    print(f"\nlive smoke test cost: ${cost['total_usd']:.2f} {cost['stages']}")


def test_comments_at_the_gate_change_the_next_draft(tmp_path: Path) -> None:
    comment = "Add a non-goal explicitly excluding payments and ticket sales."
    human = ScriptedHuman(GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments=comment))
    ctx = make_ctx(tmp_path, human)
    state = ProjectState(project_slug="smoke-revise", idea=IDEA)

    while state.status == Status.RUNNING and state.stage != Stage.ARCHITECT:
        state = advance(state, ctx)

    prd = PRD.model_validate_json(
        (tmp_path / "smoke-revise" / state.artifacts["prd.json"].path).read_text()
    )
    assert state.artifacts["prd.json"].revision == 2
    assert any("payment" in item.lower() for item in prd.non_goals)
