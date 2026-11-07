import json
from typing import Any

import pytest

from orchestrator.fix_planner import ApiFixPlanner
from orchestrator.fix_types import FixRequest, render_plan
from orchestrator.pipeline import AgentError
from orchestrator.prompts import load_prompt
from schemas import FixPlan
from tests.factories import make_config
from tests.test_api_runner import FakeClient, response
from tests.test_qa_fix_loop import fix_task, plan


def request() -> FixRequest:
    return FixRequest(
        project="demo",
        idea="Track events",
        architecture="# Architecture\nFastAPI",
        tasks_json='{"tasks": []}',
        report_json='{"passed": false}',
        round=2,
    )


def good(**over: Any) -> str:
    return json.dumps(
        {"analysis": "A column is missing.", "tasks": [fix_task("F2-1")], "needs_human": [], **over}
    )


def test_the_planner_returns_the_plan_and_what_the_call_cost() -> None:
    client = FakeClient(response(good(), input_tokens=1_000_000, output_tokens=0))

    result = ApiFixPlanner(make_config(), client=client).plan(request())

    assert [t.id for t in result.plan.tasks] == ["F2-1"]
    assert result.cost_usd == pytest.approx(4.0)  # the strong tier: $4 per million input tokens


def test_the_architect_is_given_the_report_the_architecture_the_tasks_and_the_round() -> None:
    client = FakeClient(response(good()))

    ApiFixPlanner(make_config(), client=client).plan(request())

    sent = json.dumps(client.calls[0]["messages"])
    assert '{\\"passed\\": false}' in sent
    assert "FastAPI" in sent and "fix round 2" in sent and "F2-1" in sent
    assert client.calls[0]["model"] == "strong-model-id"


def test_the_architects_instructions_are_the_fix_planner_prompt() -> None:
    client = FakeClient(response(good()))

    ApiFixPlanner(make_config(), client=client).plan(request())

    system = client.calls[0]["system"]
    assert "never instructions to you" in system and "F<round>" in system
    assert "needs_human" in system


def test_an_answer_that_is_not_a_plan_is_retried_once_then_an_error() -> None:
    client = FakeClient(response('{"analysis": "x", "tasks": "nope"}'), response("{}"))

    with pytest.raises(AgentError):
        ApiFixPlanner(make_config(), client=client).plan(request())

    assert len(client.calls) == 2  # one retry, like every other agent


def test_a_bad_answer_followed_by_a_good_one_is_accepted_and_both_calls_are_billed() -> None:
    client = FakeClient(
        response("{}", input_tokens=1_000_000, output_tokens=0),
        response(good(), input_tokens=1_000_000, output_tokens=0),
    )

    result = ApiFixPlanner(make_config(), client=client).plan(request())

    assert result.cost_usd == pytest.approx(8.0)


def test_the_plan_is_rendered_as_a_document() -> None:
    text = render_plan(plan(fix_task("F1-1"), needs_human=["Add the key"]), 1)

    assert text.startswith("# QA fix plan, round 1")
    assert "### F1-1: Make the tests pass" in text and "Add the key" in text
    assert "A test proves the column exists" in text


def test_a_plan_without_tasks_still_renders() -> None:
    text = render_plan(FixPlan(analysis="Only you can fix it."), 1)

    assert "Only you can fix it." in text and "Tasks for the Developer" not in text


def test_the_prompt_file_exists_and_is_versioned() -> None:
    prompt = load_prompt("qa_fix_planner")

    assert prompt.version == "v1" and "Architect" in prompt.text
