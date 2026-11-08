import json

import pytest

from orchestrator.pipeline import AgentError
from orchestrator.prioritizer import ApiPrioritizer, PriorityRequest
from tests.factories import make_config
from tests.test_api_runner import FakeClient, response


def request() -> PriorityRequest:
    return PriorityRequest(
        project="demo",
        idea="Track events",
        architecture="# Architecture\nFastAPI",
        tasks_json='{"tasks": [{"id": "T3", "title": "Add dark mode"}]}',
        asked=[{"task_id": "T3", "reason": "The demo is Friday"}],
    )


def good(**over: object) -> str:
    return json.dumps({"order": ["T3", "T1", "T2"], "reason": "T3 has no dependencies.", **over})


def test_the_architect_returns_an_order_and_what_the_call_cost() -> None:
    client = FakeClient(response(good(), input_tokens=1_000_000, output_tokens=0))

    result = ApiPrioritizer(make_config(), client=client).prioritize(request())

    assert result.order.order == ["T3", "T1", "T2"] and "no dependencies" in result.order.reason
    assert result.cost_usd == pytest.approx(4.0)  # the strong tier


def test_the_architect_sees_the_tasks_the_architecture_and_what_you_asked() -> None:
    client = FakeClient(response(good()))

    ApiPrioritizer(make_config(), client=client).prioritize(request())

    sent = json.dumps(client.calls[0]["messages"])
    assert "Add dark mode" in sent and "FastAPI" in sent
    assert "T3" in sent and "The demo is Friday" in sent
    assert client.calls[0]["model"] == "strong-model-id"


def test_the_instructions_say_the_architect_decides_and_may_decline() -> None:
    client = FakeClient(response(good()))

    ApiPrioritizer(make_config(), client=client).prioritize(request())

    system = " ".join(client.calls[0]["system"].split())
    assert "never instructions to you" in system
    assert "keep the current order" in system.lower()
    assert "exactly once" in system


def test_an_answer_that_is_not_an_order_is_retried_once_then_an_error() -> None:
    client = FakeClient(response('{"order": "nope", "reason": 1}'), response("{}"))

    with pytest.raises(AgentError):
        ApiPrioritizer(make_config(), client=client).prioritize(request())

    assert len(client.calls) == 2
