"""The Agent SDK wrapper: runs one agent call and reports what it cost.

It implements the same `AgentRunner` interface as the dummy echo agent, so the pipeline does not
know which one it is using. SDK names were checked against the installed `claude-agent-sdk`.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    TextBlock,
    query,
)

from orchestrator.config import Config
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.state import Stage

QueryFn = Callable[..., AsyncIterator[Message]]


@dataclass(frozen=True)
class AgentSpec:
    """One agent's fixed setup. `tier` is looked up in config.yaml, never a model name here."""

    stage: Stage
    system_prompt: str
    prompt_version: str
    tools: tuple[str, ...]
    tier: Literal["strong", "fast"]


class SdkRunner:
    """Runs agents through the Claude Agent SDK."""

    def __init__(
        self,
        config: Config,
        runs_dir: Path,
        specs: Iterable[AgentSpec],
        *,
        query_fn: QueryFn = query,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._specs = {spec.stage: spec for spec in specs}
        self._query = query_fn

    def run(self, request: AgentRequest) -> AgentResult:
        spec = self._specs.get(request.stage)
        if spec is None:
            raise AgentError(f"no agent is set up for the {request.stage.value} stage")
        return asyncio.run(self._run(spec, request))

    async def _run(self, spec: AgentSpec, request: AgentRequest) -> AgentResult:
        model = self._config.models.for_tier(spec.tier)
        options = ClaudeAgentOptions(
            model=model,
            max_turns=getattr(self._config.limits.max_turns, spec.stage.value),
            system_prompt=spec.system_prompt,
            tools=list(spec.tools),
            allowed_tools=list(spec.tools),
            # Anything not on the allowlist is denied outright, never prompted for.
            permission_mode="dontAsk",
            cwd=self._runs_dir / request.project,
        )
        texts: list[str] = []
        reported_model: str | None = None
        result: ResultMessage | None = None
        async for message in self._query(prompt=_prompt(request), options=options):
            if isinstance(message, AssistantMessage):
                reported_model = message.model
                texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
            elif isinstance(message, ResultMessage):
                result = message

        if result is None:
            raise AgentError("the agent finished without a result message")
        if result.is_error:
            raise AgentError(
                f"agent failed: {result.subtype} {result.errors or ''}".strip(),
                cost_usd=result.total_cost_usd or 0.0,
            )
        if result.total_cost_usd is None:
            raise AgentError("the SDK reported no cost, so spend cannot be tracked")
        usage: dict[str, Any] = result.usage or {}
        return AgentResult(
            output=result.result if result.result is not None else "\n".join(texts),
            cost_usd=result.total_cost_usd,
            model=reported_model or model,
            prompt_version=spec.prompt_version,
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
        )


def _prompt(request: AgentRequest) -> str:
    parts = [f"Project idea:\n{request.idea}"]
    if request.previous_draft is not None:
        parts.append(f"Previous draft:\n{request.previous_draft}")
    if request.comments:
        parts.append(f"Feedback to address:\n{request.comments}")
    return "\n\n".join(parts)
