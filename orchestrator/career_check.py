"""The checks a career plan must pass before the person sees it.

The model's JSON is first checked against the schema; this adds the rules a schema cannot state,
using the inputs the coach was given. Problems go back to the model once, like a schema error.
"""

import yaml

from orchestrator.career_rules import plan_problems
from orchestrator.pipeline import AgentRequest
from schemas import CareerPlan, EvidenceSet, Goals, ResumeData
from schemas._base import Schema


def check_career_plan(request: AgentRequest, model: Schema) -> list[str]:
    assert isinstance(model, CareerPlan)
    evidence = EvidenceSet.model_validate_json(request.inputs["evidence.json"])
    resume = ResumeData.model_validate(yaml.safe_load(request.inputs["resume.yaml"]))
    goals = Goals.model_validate(yaml.safe_load(request.inputs["goals.yaml"]))
    problems = plan_problems(model, evidence, resume)
    judged = {fit.track.value for fit in model.fit}
    if judged != set(goals.tracks):
        problems.append(
            f"fit must cover exactly the tracks in goals.yaml ({', '.join(sorted(goals.tracks))}); "
            f"it covers {', '.join(sorted(judged))}"
        )
    return problems
