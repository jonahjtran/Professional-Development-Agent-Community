from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from orchestrator.pipeline import AgentError, AgentRequest, AgentRunner
from orchestrator.runner import AgentSpec, SdkRunner
from orchestrator.state import Stage
from tests.factories import make_config


def result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 2,
        "session_id": "s1",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 120, "output_tokens": 45},
        "result": "the draft",
    }
    return ResultMessage(**{**fields, **overrides})


class FakeQuery:
    """Stands in for claude_agent_sdk.query: records how it was called, replays messages."""

    def __init__(self, *messages: Any) -> None:
        self.messages = messages
        self.prompt: Any = None
        self.options: ClaudeAgentOptions | None = None

    def __call__(
        self, *, prompt: Any, options: ClaudeAgentOptions | None = None
    ) -> AsyncIterator[Any]:
        self.prompt, self.options = prompt, options
        return self._replay()

    async def _replay(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message


SPEC = AgentSpec(
    stage=Stage.PRODUCT_OWNER,
    system_prompt="You are the product owner.",
    prompt_version="po-v1",
    tools=("Read", "WebSearch"),
    tier="strong",
)


def make_runner(query: FakeQuery, tmp_path: Path, *specs: AgentSpec) -> SdkRunner:
    return SdkRunner(
        make_config(),
        tmp_path,
        specs or (SPEC,),
        query_fn=query,
    )


def request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "stage": Stage.PRODUCT_OWNER,
        "project": "demo",
        "idea": "Track events",
    }
    return AgentRequest(**{**fields, **overrides})


def test_the_runner_is_an_agent_runner(tmp_path: Path) -> None:
    runner: AgentRunner = make_runner(FakeQuery(result_message()), tmp_path)

    assert callable(runner.run)


# --- how the SDK is called -------------------------------------------------------------------


def test_options_come_from_the_spec_and_the_config(tmp_path: Path) -> None:
    query = FakeQuery(result_message())

    make_runner(query, tmp_path).run(request())

    options = query.options
    assert options is not None
    assert options.model == "strong-model-id"  # the "strong" tier, from config
    assert options.max_turns == 20  # product_owner, from config
    assert options.system_prompt == "You are the product owner."
    assert options.tools == ["Read", "WebSearch"]
    assert options.allowed_tools == ["Read", "WebSearch"]
    assert options.permission_mode == "dontAsk"
    assert options.cwd == tmp_path / "demo"


def test_the_fast_tier_uses_the_fast_model(tmp_path: Path) -> None:
    spec = AgentSpec(
        stage=Stage.QA, system_prompt="qa", prompt_version="qa-v1", tools=(), tier="fast"
    )
    query = FakeQuery(result_message())

    make_runner(query, tmp_path, spec).run(request(stage=Stage.QA))

    assert query.options is not None
    assert query.options.model == "fast-model-id"
    assert query.options.max_turns == 20


def test_the_prompt_contains_the_idea(tmp_path: Path) -> None:
    query = FakeQuery(result_message())

    make_runner(query, tmp_path).run(request())

    assert "Track events" in query.prompt


def test_a_revision_prompt_contains_the_previous_draft_and_the_comments(tmp_path: Path) -> None:
    query = FakeQuery(result_message())

    make_runner(query, tmp_path).run(
        request(previous_draft="old draft", comments="Add non-goals", revision=1)
    )

    assert "old draft" in query.prompt
    assert "Add non-goals" in query.prompt


def test_a_first_prompt_has_no_revision_section(tmp_path: Path) -> None:
    query = FakeQuery(result_message())

    make_runner(query, tmp_path).run(request())

    assert "Previous draft" not in query.prompt


# --- what comes back -------------------------------------------------------------------------


def test_the_result_carries_text_cost_tokens_model_and_prompt_version(tmp_path: Path) -> None:
    assistant = AssistantMessage(content=[TextBlock(text="thinking")], model="claude-real-id")
    query = FakeQuery(assistant, result_message())

    result = make_runner(query, tmp_path).run(request())

    assert result.output == "the draft"
    assert result.cost_usd == pytest.approx(0.42)
    assert result.tokens_in == 120
    assert result.tokens_out == 45
    assert result.model == "claude-real-id"
    assert result.prompt_version == "po-v1"


def test_the_configured_model_is_reported_when_the_sdk_names_none(tmp_path: Path) -> None:
    result = make_runner(FakeQuery(result_message()), tmp_path).run(request())

    assert result.model == "strong-model-id"


def test_the_assistant_text_is_used_when_there_is_no_final_result_text(tmp_path: Path) -> None:
    assistant = AssistantMessage(
        content=[TextBlock(text="part one"), TextBlock(text="part two")], model="m"
    )
    query = FakeQuery(assistant, result_message(result=None))

    result = make_runner(query, tmp_path).run(request())

    assert result.output == "part one\npart two"


def test_missing_token_counts_are_left_unset(tmp_path: Path) -> None:
    query = FakeQuery(result_message(usage=None))

    result = make_runner(query, tmp_path).run(request())

    assert result.tokens_in is None and result.tokens_out is None


# --- failures --------------------------------------------------------------------------------


def test_an_error_result_raises_with_the_cost_already_spent(tmp_path: Path) -> None:
    query = FakeQuery(result_message(subtype="error_max_turns", is_error=True, total_cost_usd=0.9))

    with pytest.raises(AgentError, match="error_max_turns") as error:
        make_runner(query, tmp_path).run(request())

    assert error.value.cost_usd == pytest.approx(0.9)


def test_a_run_that_ends_without_a_result_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="result"):
        make_runner(FakeQuery(), tmp_path).run(request())


def test_a_result_with_no_cost_raises_so_spend_is_never_silently_zero(tmp_path: Path) -> None:
    query = FakeQuery(result_message(total_cost_usd=None))

    with pytest.raises(AgentError, match="cost"):
        make_runner(query, tmp_path).run(request())


def test_a_stage_with_no_agent_spec_raises(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="developer"):
        make_runner(FakeQuery(result_message()), tmp_path).run(request(stage=Stage.DEVELOPER))
