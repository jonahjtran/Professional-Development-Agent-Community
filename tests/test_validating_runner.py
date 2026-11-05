import json
from typing import Any

import pytest

from orchestrator.api_runner import ValidatingRunner
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.state import Stage
from schemas import TaskList
from tests.test_schemas import task_data


class ScriptedRunner:
    """Returns the given outputs in order and records each request."""

    def __init__(self, *outputs: str, cost: float = 0.5) -> None:
        self.outputs = list(outputs)
        self.cost = cost
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(
            output=self.outputs.pop(0), cost_usd=self.cost, tokens_in=10, tokens_out=5
        )


GOOD = json.dumps({"tasks": [task_data()]})
BAD = json.dumps({"tasks": []})


def request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {"stage": Stage.ARCHITECT, "project": "demo", "idea": "x"}
    return AgentRequest(**{**fields, **overrides})


def validating(inner: ScriptedRunner) -> ValidatingRunner:
    return ValidatingRunner(inner, {Stage.ARCHITECT: TaskList})


def test_valid_output_passes_through_with_one_call() -> None:
    inner = ScriptedRunner(GOOD)

    result = validating(inner).run(request())

    assert TaskList.model_validate_json(result.output).tasks[0].id == "T1"
    assert len(inner.requests) == 1
    assert result.cost_usd == pytest.approx(0.5)


def test_invalid_output_is_retried_once_with_the_error() -> None:
    inner = ScriptedRunner(BAD, GOOD)

    result = validating(inner).run(request(comments="Make it smaller"))

    assert len(inner.requests) == 2
    retry = inner.requests[1]
    assert retry.previous_draft == BAD
    assert retry.comments is not None
    assert "tasks" in retry.comments  # the validation error names the field
    assert "Make it smaller" in retry.comments  # the human's feedback is kept
    assert TaskList.model_validate_json(result.output)


def test_both_calls_are_billed_and_tokens_summed() -> None:
    result = validating(ScriptedRunner(BAD, GOOD)).run(request())

    assert result.cost_usd == pytest.approx(1.0)
    assert (result.tokens_in, result.tokens_out) == (20, 10)


def test_output_that_is_not_json_is_retried_too() -> None:
    inner = ScriptedRunner("not json at all", GOOD)

    validating(inner).run(request())

    assert len(inner.requests) == 2


def test_two_invalid_answers_raise_with_the_total_cost() -> None:
    inner = ScriptedRunner(BAD, BAD)

    with pytest.raises(AgentError, match="validation") as error:
        validating(inner).run(request())

    assert error.value.cost_usd == pytest.approx(1.0)
    assert len(inner.requests) == 2


def test_a_dependency_cycle_is_caught_and_retried() -> None:
    cyclic = json.dumps(
        {"tasks": [task_data(id="A", depends_on=["B"]), task_data(id="B", depends_on=["A"])]}
    )
    inner = ScriptedRunner(cyclic, GOOD)

    validating(inner).run(request())

    assert inner.requests[1].comments is not None
    assert "cycle" in inner.requests[1].comments


def test_a_failure_raised_by_the_inner_runner_is_not_swallowed() -> None:
    class Failing:
        def run(self, request: AgentRequest) -> AgentResult:
            raise AgentError("refused", cost_usd=0.3)

    with pytest.raises(AgentError, match="refused"):
        ValidatingRunner(Failing(), {Stage.ARCHITECT: TaskList}).run(request())


def test_the_retry_cost_of_a_failed_second_call_is_kept() -> None:
    class FailsSecond:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request: AgentRequest) -> AgentResult:
            self.calls += 1
            if self.calls == 1:
                return AgentResult(output=BAD, cost_usd=0.5)
            raise AgentError("refused", cost_usd=0.3)

    with pytest.raises(AgentError) as error:
        ValidatingRunner(FailsSecond(), {Stage.ARCHITECT: TaskList}).run(request())

    assert error.value.cost_usd == pytest.approx(0.8)


def test_stages_without_a_schema_pass_through_unchanged() -> None:
    inner = ScriptedRunner("free text")

    result = validating(inner).run(request(stage=Stage.QA))

    assert result.output == "free text"
