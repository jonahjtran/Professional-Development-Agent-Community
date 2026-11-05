import json

import pytest
import yaml

from orchestrator.api_runner import ValidatingRunner
from orchestrator.artifacts import STAGE_INPUTS
from orchestrator.career_check import check_career_plan
from orchestrator.default_specs import (
    default_api_checks,
    default_api_schemas,
    default_api_specs,
    default_stage_specs,
)
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.render import render_career_plan
from orchestrator.stages import career_stage
from orchestrator.state import Stage
from schemas import CareerPlan, ResumeAction
from tests.test_career_rules import bullet, change, entry, evidence, plan, resume

GOALS = {
    "tracks": {
        "strategy_consulting": {"target_roles": ["strategy consultant"]},
        "tech_consulting": {"target_roles": ["technology consultant"]},
    }
}


def inputs(max_bullets: int = 3) -> dict[str, str]:
    return {
        "evidence.json": evidence().model_dump_json(),
        "resume.yaml": yaml.safe_dump(resume(max_bullets, 2).model_dump(mode="json")),
        "goals.yaml": yaml.safe_dump(GOALS),
    }


def good_plan() -> CareerPlan:
    data = plan(
        change(ResumeAction.ADD, bullet("Passed 9 checks", ["qa"])), website=entry()
    ).model_dump(mode="json")
    data["fit"] = [
        {"track": "strategy_consulting", "decision": "skip", "reason": "Too technical."},
        {"track": "tech_consulting", "decision": "include", "reason": "AI implementation."},
    ]
    return CareerPlan.model_validate(data)


def request(**fields: object) -> AgentRequest:
    base: dict[str, object] = {
        "stage": Stage.CAREER_COACH,
        "project": "demo",
        "idea": "x",
        "inputs": inputs(),
    }
    return AgentRequest.model_validate({**base, **fields})


# --- the check -------------------------------------------------------------------------------


def test_a_sound_plan_has_no_problems() -> None:
    assert check_career_plan(request(), good_plan()) == []


def test_a_plan_that_breaks_the_honesty_rules_is_reported() -> None:
    bad = good_plan().model_copy(
        update={"resume_changes": [change(ResumeAction.ADD, bullet("Cut cost 80%", ["qa"]))]}
    )

    assert any("states 80" in p for p in check_career_plan(request(), bad))


def test_the_plan_must_judge_exactly_the_tracks_in_the_goals() -> None:
    one_track = good_plan().model_copy(update={"fit": good_plan().fit[:1]})

    problems = check_career_plan(request(), one_track)

    assert any("goals.yaml" in p and "strategy_consulting, tech_consulting" in p for p in problems)


# --- the stage -------------------------------------------------------------------------------


def test_the_stage_stores_the_plan_a_readable_version_and_the_documents_to_publish() -> None:
    spec = career_stage()

    built = spec.build(good_plan(), {})

    assert set(built) == {
        "career_plan.json",
        "career_plan.md",
        "career/README.md",
        "career/case_study.md",
    }
    assert CareerPlan.model_validate_json(built["career_plan.json"]) == good_plan()
    assert built["career/README.md"] == "# R"


def test_the_gate_text_shows_fit_resume_website_questions_and_documents() -> None:
    with_question = good_plan().model_copy(update={"questions": ["How many people use it?"]})

    text = render_career_plan(with_question)

    for heading in ("Fit with your goals", "Resume", "Website", "Questions for you", "README"):
        assert f"## {heading}" in text
    assert "**strategy_consulting**: skip. Too technical." in text
    assert "ADD: Agent Studio (Projects)" in text and "[qa]" in text
    assert "How many people use it?" in text


def test_a_project_that_is_skipped_everywhere_says_so() -> None:
    nothing = good_plan().model_copy(
        update={"resume_changes": [], "website_entry": None, "website_reason": "Not relevant."}
    )

    text = render_career_plan(nothing)

    assert "No resume change is proposed." in text and "No website entry. Not relevant." in text
    assert "Questions for you" not in text


def test_the_coach_is_a_strong_tier_agent_with_the_plan_schema_and_a_versioned_prompt() -> None:
    spec = next(s for s in default_api_specs() if s.stage == Stage.CAREER_COACH)

    assert (spec.tier, spec.schema, spec.web_search) == ("strong", CareerPlan, False)
    assert spec.prompt.version == "v2"
    assert default_api_schemas()[Stage.CAREER_COACH] is CareerPlan
    assert Stage.CAREER_COACH in default_api_checks()


def test_the_prompt_names_every_input_the_stage_declares() -> None:
    spec = next(s for s in default_api_specs() if s.stage == Stage.CAREER_COACH)

    for name in STAGE_INPUTS[Stage.CAREER_COACH]:
        assert name in spec.prompt.text, name


def test_the_coach_is_not_switched_on_until_its_inputs_are_prepared() -> None:
    assert Stage.CAREER_COACH not in default_stage_specs()


# --- the check feeds the retry ---------------------------------------------------------------


class Scripted:
    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.requests: list[AgentRequest] = []

    def run(self, req: AgentRequest) -> AgentResult:
        self.requests.append(req)
        return AgentResult(output=self.outputs.pop(0), cost_usd=0.5)


def runner(inner: Scripted) -> ValidatingRunner:
    return ValidatingRunner(
        inner, {Stage.CAREER_COACH: CareerPlan}, {Stage.CAREER_COACH: check_career_plan}
    )


def bad_json() -> str:
    bad = good_plan().model_copy(
        update={"resume_changes": [change(ResumeAction.ADD, bullet("Cut cost 80%", ["qa"]))]}
    )
    return bad.model_dump_json()


def test_a_plan_that_fails_a_rule_is_sent_back_once_with_the_problems() -> None:
    inner = Scripted(bad_json(), good_plan().model_dump_json())

    result = runner(inner).run(request())

    assert json.loads(result.output)["readme_md"] == "# R"
    assert "states 80" in (inner.requests[1].comments or "")
    assert inner.requests[1].previous_draft == bad_json()
    assert result.cost_usd == 1.0  # both calls are paid for


def test_a_plan_that_fails_a_rule_twice_halts_with_the_problems() -> None:
    inner = Scripted(bad_json(), bad_json())

    with pytest.raises(AgentError, match="states 80") as error:
        runner(inner).run(request())

    assert error.value.cost_usd == 1.0


def test_stages_without_a_check_are_unaffected() -> None:
    inner = Scripted(good_plan().model_dump_json())

    runner(inner).run(request(stage=Stage.ARCHITECT, inputs={}))  # no schema for this stage either

    assert len(inner.requests) == 1
