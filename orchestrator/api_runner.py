"""Runs agents through the raw Anthropic Messages API, for agents that need no tool loop.

The Product Owner and Architect take text in and return a document, so one API call (plus a
resume if a server-side tool pauses the turn) is enough. The API returns token counts but not
dollars, so cost is computed from the prices in config.yaml.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
from pydantic import ValidationError

from orchestrator.config import Config, Tier
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult, AgentRunner
from orchestrator.prompts import Prompt
from orchestrator.state import Stage
from schemas._base import Schema

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_WEB_SEARCH_TOOL = "web_search_20260209"
_MAX_RESUMES = 5  # a server-side tool loop that pauses this many times is not converging


class MessagesClient(Protocol):
    """The part of `anthropic.Anthropic()` this module uses, so tests can fake it."""

    messages: Any


@dataclass(frozen=True)
class ApiAgentSpec:
    """One agent's fixed setup. `tier` is looked up in config.yaml, never a model name here."""

    stage: Stage
    prompt: Prompt
    tier: Tier
    schema: type[Schema]
    web_search: bool = False
    effort: Effort = "medium"
    max_tokens: int = 16000


class ApiRunner:
    """An AgentRunner that calls the Messages API with a JSON-schema output format."""

    def __init__(
        self,
        config: Config,
        specs: Iterable[ApiAgentSpec],
        *,
        client: MessagesClient | None = None,
    ) -> None:
        self._config = config
        self._specs = {spec.stage: spec for spec in specs}
        self._client: Any = client if client is not None else anthropic.Anthropic()

    def run(self, request: AgentRequest) -> AgentResult:
        spec = self._specs.get(request.stage)
        if spec is None:
            raise AgentError(f"no agent is set up for the {request.stage.value} stage")

        params: dict[str, Any] = {
            "model": self._config.models.for_tier(spec.tier),
            "max_tokens": spec.max_tokens,
            "system": spec.prompt.text,
            "output_config": {
                "effort": spec.effort,
                # transform_schema moves constraints the API rejects (like minLength) into
                # descriptions; Pydantic still enforces them when we validate the answer.
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(spec.schema),
                },
            },
        }
        if spec.web_search:
            params["tools"] = [
                {
                    "type": _WEB_SEARCH_TOOL,
                    "name": "web_search",
                    "max_uses": self._config.limits.max_web_searches,
                }
            ]

        messages: list[dict[str, Any]] = [{"role": "user", "content": _user_message(request)}]
        cost = 0.0
        tokens_in = tokens_out = total_searches = 0
        for _ in range(_MAX_RESUMES + 1):
            try:
                response = self._client.messages.create(messages=messages, **params)
            except anthropic.APIError as error:
                # Missing key, rate limit, network down: halt cleanly, keeping the cost so far.
                raise AgentError(f"API error: {error}", cost_usd=cost) from error
            call_cost, call_in, call_out, call_searches = self._price(spec, response)
            cost += call_cost
            tokens_in += call_in
            tokens_out += call_out
            total_searches += call_searches

            if response.stop_reason == "pause_turn":
                messages = [*messages, {"role": "assistant", "content": response.content}]
                continue
            if response.stop_reason == "refusal":
                raise AgentError("the model declined the request (refusal)", cost_usd=cost)
            if response.stop_reason == "max_tokens":
                raise AgentError("the output was cut off by max_tokens", cost_usd=cost)
            texts = [b.text for b in response.content if b.type == "text"]
            if not texts:
                raise AgentError("the response contained no text block", cost_usd=cost)
            return AgentResult(
                output=texts[-1],
                cost_usd=cost,
                model=response.model,
                prompt_version=spec.prompt.version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                web_searches=total_searches,
            )
        raise AgentError("the turn kept pausing and never finished (paused)", cost_usd=cost)

    def _price(self, spec: ApiAgentSpec, response: Any) -> tuple[float, int, int, int]:
        usage = response.usage
        if usage is None:
            raise AgentError("the response reported no usage, so its cost is unknown")
        prices = self._config.pricing.for_tier(spec.tier)
        searches = getattr(usage.server_tool_use, "web_search_requests", 0) or 0
        cost = (
            usage.input_tokens * prices.input
            + usage.output_tokens * prices.output
            + (usage.cache_read_input_tokens or 0) * prices.cache_read
            + (usage.cache_creation_input_tokens or 0) * prices.cache_write
        ) / 1_000_000 + searches * self._config.pricing.web_search_usd_per_request
        return cost, usage.input_tokens, usage.output_tokens, searches


def _user_message(request: AgentRequest) -> str:
    parts = [f"Project idea:\n{request.idea}", f"Track: {request.track}"]
    for name, text in request.inputs.items():
        parts.append(f"Approved input {name}:\n{text}")
    if request.previous_draft is not None:
        parts.append(f"Previous draft:\n{request.previous_draft}")
    if request.routing_note:
        parts.append(
            f"A note from the orchestrator, who chose you for this:\n{request.routing_note}"
        )
    if request.comments:
        parts.append(f"Feedback to address:\n{request.comments}")
    return "\n\n".join(parts)


Check = Callable[[AgentRequest, Schema], list[str]]


class ValidatingRunner:
    """Wraps a runner: checks its output against the stage's schema, with one retry.

    A stage may also have a `check`: rules a schema cannot express (for example, every number must
    appear in the evidence). It returns problems in plain words, and they are handled exactly like
    a schema error: shown to the model once, then a second failure halts the run.

    On a validation failure the model is asked once more, shown its bad answer and the exact
    error. A second failure halts the run. Every call's cost is kept.
    """

    def __init__(
        self,
        inner: AgentRunner,
        schemas: Mapping[Stage, type[Schema]],
        checks: Mapping[Stage, Check] | None = None,
    ) -> None:
        self._inner = inner
        self._schemas = dict(schemas)
        self._checks = dict(checks or {})

    def run(self, request: AgentRequest) -> AgentResult:
        schema = self._schemas.get(request.stage)
        if schema is None:
            return self._inner.run(request)

        first = self._inner.run(request)
        problem = self._problem(request, schema, first.output)
        if problem is None:
            return _canonical(schema, first)

        feedback = f"Your last answer failed validation: {problem}\nReturn corrected JSON."
        comments = f"{request.comments}\n\n{feedback}" if request.comments else feedback
        retry_request = request.model_copy(
            update={"previous_draft": first.output, "comments": comments}
        )
        try:
            second = self._inner.run(retry_request)
        except AgentError as error:
            raise AgentError(str(error), cost_usd=first.cost_usd + error.cost_usd) from error
        total = second.model_copy(
            update={
                "cost_usd": first.cost_usd + second.cost_usd,
                "tokens_in": _add(first.tokens_in, second.tokens_in),
                "tokens_out": _add(first.tokens_out, second.tokens_out),
                "web_searches": first.web_searches + second.web_searches,
            }
        )
        problem = self._problem(request, schema, second.output)
        if problem is not None:
            raise AgentError(f"output failed validation twice: {problem}", cost_usd=total.cost_usd)
        return _canonical(schema, total)

    def _problem(self, request: AgentRequest, schema: type[Schema], output: str) -> str | None:
        try:
            model = schema.model_validate_json(output)
        except ValidationError as error:
            return str(error)
        check = self._checks.get(request.stage)
        problems = check(request, model) if check else []
        return "; ".join(problems) if problems else None


def _canonical(schema: type[Schema], result: AgentResult) -> AgentResult:
    """Re-serialize validated output so artifacts are stored in one consistent format."""
    text = schema.model_validate_json(result.output).model_dump_json(indent=2)
    return result.model_copy(update={"output": text})


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None and b is None else (a or 0) + (b or 0)
