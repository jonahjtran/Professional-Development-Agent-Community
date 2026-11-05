"""Live career coach test: the real coach inside the pipeline, stopping before publishing.

Costs about twenty cents and needs ANTHROPIC_API_KEY. It uses made-up resume data and no GitHub
calls: there is no publisher, and the gate approves nothing, so nothing could be published even if
the coach asked. The invented project has almost no evidence (only its cost), so a good coach
stays modest and asks questions. Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k career
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from evals.career_cases import CASES
from evals.career_eval import GOALS, WEBSITE, resume_for
from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.career_stage import CareerMaterials
from orchestrator.config import load_config
from orchestrator.default_specs import default_api_checks, default_api_schemas, default_api_specs
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import PipelineContext, advance
from orchestrator.stages import career_stage
from orchestrator.state import ProjectState, Stage, Status
from schemas import CareerPlan, ResumeAction

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"
CASE = next(c for c in CASES if c.name == "strong_tech_fit")


class Preparer:
    def materials(self) -> CareerMaterials:
        return CareerMaterials(
            resume_yaml=yaml.safe_dump(resume_for(CASE)),
            goals_yaml=yaml.safe_dump(GOALS),
            website_projects_js=WEBSITE,
            current_readme=CASE.readme,
        )


class Human:
    def __init__(self) -> None:
        self.requests: list[GateRequest] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        return GateDecision(outcome=GateOutcome.APPROVE, approved_items=[])  # approve nothing


class NoCaps:
    def resolve(self, request: object) -> object:
        raise AssertionError("a cap was hit during the career live test")


def test_the_coach_writes_an_honest_plan_and_nothing_is_published(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    store = ArtifactStore(tmp_path, "career-live")
    prd = {
        "problem": CASE.project,
        "users": ["a small company"],
        "track": "engineering",
        "goals": [CASE.project],
        "success_metrics": [{"name": "works", "target": "yes", "how_measured": "tests"}],
        "scope": [CASE.project],
        "non_goals": ["a mobile app"],
        "risks": ["scope creep"],
    }
    files = {"prd.json": json.dumps(prd), "architecture.md": f"# Architecture\n{CASE.project}\n"}
    state = record_artifacts(
        ProjectState(project_slug="career-live", idea="x", stage=Stage.CAREER_COACH),
        {n: store.write(n, t, revision=1) for n, t in files.items()},
    )
    state = approve_artifacts(state, list(files), now=datetime.now(UTC))
    human = Human()
    ctx = PipelineContext(
        config=config,
        runs_dir=tmp_path,
        runner=ValidatingRunner(
            ApiRunner(config, default_api_specs()), default_api_schemas(), default_api_checks()
        ),
        gates=human,
        caps=NoCaps(),  # type: ignore[arg-type]
        stages={Stage.CAREER_COACH: career_stage()},
        career=Preparer(),
    )
    while state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)

    plan = CareerPlan.model_validate_json(store.read(state.artifacts["career_plan.json"]))
    spent = state.cost_usd.get(Stage.CAREER_COACH, 0)
    print(f"\ncost ${spent:.3f}\n{human.requests[-1].summary[:3000]}")
    assert state.status == Status.DONE
    assert [i.id for i in human.requests[-1].items][:2] == ["readme", "case_study"]
    assert plan.readme_md.strip() and plan.case_study_md.strip()
    assert "career_published.json" not in state.artifacts  # nothing was published
    assert all(c.action == ResumeAction.SKIP or c.bullets for c in plan.resume_changes)
