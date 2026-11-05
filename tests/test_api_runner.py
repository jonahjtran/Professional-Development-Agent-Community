import json
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.api_runner import ApiAgentSpec, ApiRunner
from orchestrator.pipeline import AgentError, AgentRequest, AgentRunner
from orchestrator.prompts import Prompt
from orchestrator.state import Stage
from schemas import PRD
from tests.factories import make_config


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def usage(
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read: int = 0,
    cache_write: int = 0,
    searches: int | None = None,
) -> SimpleNamespace:
    server = None if searches is None else SimpleNamespace(web_search_requests=searches)
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        server_tool_use=server,
    )


def response(
    text: str = "{}", stop_reason: str = "end_turn", model: str = "claude-real", **usage_args: Any
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[text_block(text)],
        stop_reason=stop_reason,
        model=model,
        usage=usage(**usage_args),
    )


class FakeClient:
    """Stands in for anthropic.Anthropic(): replays responses and records every request."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._responses.pop(0)


SPEC = ApiAgentSpec(
    stage=Stage.PRODUCT_OWNER,
    prompt=Prompt(name="product_owner", version="v7", text="You are the product owner."),
    tier="strong",
    schema=PRD,
    web_search=True,
)


def runner(client: FakeClient, *specs: ApiAgentSpec) -> ApiRunner:
    return ApiRunner(make_config(), specs or (SPEC,), client=client)


def request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "stage": Stage.PRODUCT_OWNER,
        "project": "demo",
        "idea": "Track events",
    }
    return AgentRequest(**{**fields, **overrides})


def test_the_api_runner_is_an_agent_runner() -> None:
    agent: AgentRunner = runner(FakeClient(response()))

    assert callable(agent.run)


# --- the request -----------------------------------------------------------------------------


def test_the_call_uses_the_tier_model_prompt_effort_and_output_schema() -> None:
    client = FakeClient(response())

    runner(client).run(request())

    call = client.calls[0]
    assert call["model"] == "strong-model-id"
    assert call["system"] == "You are the product owner."
    assert call["max_tokens"] == 16000
    assert call["output_config"]["effort"] == "medium"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert "problem" in fmt["schema"]["properties"]


def test_the_schema_sent_to_the_api_has_no_min_length_which_the_api_rejects() -> None:
    client = FakeClient(response())

    runner(client).run(request())

    assert '"minLength"' not in json.dumps(client.calls[0]["output_config"]["format"]["schema"])


def test_web_search_is_offered_with_a_limit_from_config() -> None:
    client = FakeClient(response())

    runner(client).run(request())

    [tool] = client.calls[0]["tools"]
    assert tool["type"] == "web_search_20260209"
    assert tool["name"] == "web_search"
    assert tool["max_uses"] == 5


def test_an_agent_without_web_search_is_given_no_tools() -> None:
    spec = ApiAgentSpec(
        stage=Stage.PRODUCT_OWNER, prompt=SPEC.prompt, tier="strong", schema=PRD, web_search=False
    )
    client = FakeClient(response())

    runner(client, spec).run(request())

    assert "tools" not in client.calls[0]


def test_the_user_message_contains_the_idea_track_and_approved_inputs() -> None:
    client = FakeClient(response())

    runner(client).run(request(track="consulting", inputs={"prd.json": '{"problem": "p"}'}))

    content = client.calls[0]["messages"][0]["content"]
    assert "Track events" in content
    assert "consulting" in content
    assert "prd.json" in content and '{"problem": "p"}' in content


def test_a_revision_includes_the_previous_draft_and_the_feedback() -> None:
    client = FakeClient(response())

    runner(client).run(request(previous_draft="old draft", comments="Add non-goals"))

    content = client.calls[0]["messages"][0]["content"]
    assert "old draft" in content and "Add non-goals" in content


# --- the result ------------------------------------------------------------------------------


def test_the_result_carries_output_model_prompt_version_and_tokens() -> None:
    client = FakeClient(response('{"a": 1}', model="claude-opus-5-5"))

    result = runner(client).run(request())

    assert result.output == '{"a": 1}'
    assert result.model == "claude-opus-5-5"
    assert result.prompt_version == "v7"
    assert (result.tokens_in, result.tokens_out) == (1000, 500)


def test_cost_comes_from_tokens_and_the_configured_prices() -> None:
    # strong: $4 in, $20 out, $0.20 cache read, $5 cache write, per million tokens.
    client = FakeClient(
        response(
            input_tokens=1_000_000, output_tokens=100_000, cache_read=500_000, cache_write=200_000
        )
    )

    result = runner(client).run(request())

    assert result.cost_usd == pytest.approx(4.0 + 2.0 + 0.1 + 1.0)


def test_the_fast_tier_is_priced_with_the_fast_prices() -> None:
    spec = ApiAgentSpec(stage=Stage.PRODUCT_OWNER, prompt=SPEC.prompt, tier="fast", schema=PRD)
    client = FakeClient(response(input_tokens=1_000_000, output_tokens=1_000_000))

    result = runner(client, spec).run(request())

    assert result.cost_usd == pytest.approx(0.1 + 0.5)


def test_each_web_search_is_billed() -> None:
    client = FakeClient(response(input_tokens=0, output_tokens=0, searches=3))

    result = runner(client).run(request())

    assert result.cost_usd == pytest.approx(0.03)


def test_a_paused_turn_is_resumed_and_both_calls_are_billed() -> None:
    paused = response("", stop_reason="pause_turn", input_tokens=1_000_000, output_tokens=0)
    done = response('{"ok": true}', input_tokens=1_000_000, output_tokens=0)
    client = FakeClient(paused, done)

    result = runner(client).run(request())

    assert result.output == '{"ok": true}'
    assert len(client.calls) == 2
    assert result.cost_usd == pytest.approx(8.0)
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_a_turn_that_never_finishes_stops_after_a_few_resumes() -> None:
    client = FakeClient(*[response("", stop_reason="pause_turn") for _ in range(10)])

    with pytest.raises(AgentError, match="paused"):
        runner(client).run(request())

    assert len(client.calls) <= 6


# --- failures --------------------------------------------------------------------------------


def test_a_refusal_is_an_error_that_still_carries_its_cost() -> None:
    client = FakeClient(
        response("", stop_reason="refusal", input_tokens=1_000_000, output_tokens=0)
    )

    with pytest.raises(AgentError, match="refus") as error:
        runner(client).run(request())

    assert error.value.cost_usd == pytest.approx(4.0)


def test_output_cut_off_by_max_tokens_is_an_error() -> None:
    client = FakeClient(response('{"half', stop_reason="max_tokens"))

    with pytest.raises(AgentError, match="max_tokens"):
        runner(client).run(request())


def test_a_response_with_no_text_is_an_error() -> None:
    empty = SimpleNamespace(content=[], stop_reason="end_turn", model="m", usage=usage())

    with pytest.raises(AgentError, match="text"):
        runner(FakeClient(empty)).run(request())


def test_a_response_with_no_usage_is_an_error_so_spend_is_never_silently_zero() -> None:
    no_usage = SimpleNamespace(
        content=[text_block("{}")], stop_reason="end_turn", model="m", usage=None
    )

    with pytest.raises(AgentError, match="usage"):
        runner(FakeClient(no_usage)).run(request())


def test_a_stage_with_no_spec_is_an_error() -> None:
    with pytest.raises(AgentError, match="architect"):
        runner(FakeClient(response())).run(request(stage=Stage.ARCHITECT))


# --- API errors halt the run cleanly -----------------------------------------------------------


def test_an_api_error_becomes_an_agent_error_so_the_run_halts_instead_of_crashing() -> None:
    import anthropic
    import httpx2

    class Failing(FakeClient):
        def create(self, **kwargs: Any) -> SimpleNamespace:
            raise anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            )

    with pytest.raises(AgentError, match="API error"):
        runner(Failing()).run(request())


def test_an_api_error_after_a_paused_turn_keeps_the_cost_already_spent() -> None:
    import anthropic
    import httpx2

    class FailsSecond(FakeClient):
        def create(self, **kwargs: Any) -> SimpleNamespace:
            if self.calls:
                raise anthropic.APIConnectionError(
                    request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
                )
            return super().create(**kwargs)

    client = FailsSecond(
        response("", stop_reason="pause_turn", input_tokens=1_000_000, output_tokens=0)
    )

    with pytest.raises(AgentError) as error:
        runner(client).run(request())

    assert error.value.cost_usd == pytest.approx(4.0)


# --- search count ------------------------------------------------------------------------------


def test_the_result_reports_how_many_web_searches_were_run() -> None:
    result = runner(FakeClient(response(searches=3))).run(request())

    assert result.web_searches == 3


def test_no_server_tool_usage_means_zero_searches() -> None:
    result = runner(FakeClient(response())).run(request())

    assert result.web_searches == 0


def test_searches_are_summed_across_a_paused_turn() -> None:
    paused = response("", stop_reason="pause_turn", searches=2)
    done = response("{}", searches=1)

    result = runner(FakeClient(paused, done)).run(request())

    assert result.web_searches == 3
