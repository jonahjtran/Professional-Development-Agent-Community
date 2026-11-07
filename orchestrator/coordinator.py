"""The coordinator: decide which agent should handle a person's request.

The person talks to the orchestrator, not to a particular agent. For a paused or finished project
they say what they want changed; one model call reads the request and the project's documents and
picks the agent (with a reason, and the request restated for that agent). The choice is checked in
code: only an agent the project has already reached may be chosen, and if the call fails or answers
badly the request goes to the Architect, who turns any request into tasks. Nothing here moves the
project; `rework.send_back` does that with the chosen agent.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from orchestrator.assistant_cost import record_assistant_cost
from orchestrator.config import Config
from orchestrator.explain import scrub
from orchestrator.journey import LABELS, ORDER
from orchestrator.prompts import DEFAULT_PROMPTS_DIR, Prompt, load_prompt
from orchestrator.rework import REWORKABLE
from orchestrator.state import ProjectState, Stage
from schemas import Route

_DOCUMENTS = ("prd.md", "architecture.md", "tasks.json", "qa_report.json")
_MAX_DOCUMENT_CHARS = 12_000
_MAX_INSTRUCTION = 2000


@dataclass(frozen=True)
class Decision:
    agent: Stage
    reason: str
    instruction: str  # what the agent is told: the request restated, with the person's own words
    routed: bool  # False when the coordinator could not decide and the default was used


def allowed_agents(state: ProjectState) -> tuple[Stage, ...]:
    """The agents the project has reached (a project cannot be sent past where it got to)."""
    if state.stage not in ORDER:
        return ()
    reached = ORDER.index(state.stage)
    return tuple(s for s in REWORKABLE if ORDER.index(s) <= reached)


def _documents(runs_dir: Path, project: str) -> str:
    parts: list[str] = []
    for name in _DOCUMENTS:
        try:
            text = (runs_dir / project / "artifacts" / name).read_text(errors="replace")
        except OSError:
            continue
        parts.append(f"--- {name} ---\n{scrub_keep(text)[:_MAX_DOCUMENT_CHARS]}")
    return "\n\n".join(parts) or "(no documents yet)"


def scrub_keep(text: str) -> str:
    """Drop anything that looks like a credential but keep the length (the caller cuts it)."""
    return scrub(text[:200_000]).replace(" [cut]", "")


class Routing(Protocol):
    def route(self, state: ProjectState, request: str, allowed: tuple[Stage, ...]) -> Route | None:
        """The chosen agent, or None when it cannot decide."""
        ...


class Coordinator:
    def __init__(
        self,
        config: Config,
        runs_dir: Path,
        *,
        prompt: Prompt | None = None,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._prompt = prompt or load_prompt("coordinator", prompts_dir)
        self._client = client

    def route(self, state: ProjectState, request: str, allowed: tuple[Stage, ...]) -> Route | None:
        names = ", ".join(s.value for s in allowed)
        message = (
            f"Project idea: {scrub(state.idea)[:500]}\n"
            f"Stage reached: {state.stage.value}\n"
            f"Allowed agents: {names}\n\n"
            f"The person's request:\n{scrub(request)}\n\n"
            f"{_documents(self._runs_dir, state.project_slug)}"
        )
        try:
            client: Any = self._client if self._client is not None else anthropic.Anthropic()
            response: Any = client.messages.create(
                model=self._config.models.for_tier("strong"),
                max_tokens=1000,
                system=self._prompt.text,
                messages=[{"role": "user", "content": message}],
                output_config={
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(Route)}
                },
            )
        except anthropic.APIError:
            return None
        usage = response.usage
        prices = self._config.pricing.strong
        cost = (
            usage.input_tokens * prices.input
            + usage.output_tokens * prices.output
            + (usage.cache_read_input_tokens or 0) * prices.cache_read
            + (usage.cache_creation_input_tokens or 0) * prices.cache_write
        ) / 1_000_000
        record_assistant_cost(self._runs_dir, state.project_slug, "coordinator_usd", cost)
        texts = [b.text for b in response.content if b.type == "text"]
        try:
            return Route.model_validate(json.loads(texts[-1]))
        except (IndexError, ValueError, ValidationError):
            return None


def choose(coordinator: Routing, runs_dir: Path, state: ProjectState, request: str) -> Decision:
    """Pick the agent for `request`, falling back to the Architect (or the earliest agent)."""
    allowed = allowed_agents(state)
    if not allowed:
        raise ValueError("this project has not reached an agent that can take a change yet")
    words = request.strip()
    if len(allowed) == 1:  # the project is still at the first agent: there is no choice
        only = allowed[0]
        reason = f"The project has only reached the {LABELS[only]}, so it goes there."
        return Decision(only, reason, words, routed=True)
    route = coordinator.route(state, words, allowed)
    if route is not None and Stage(route.agent) in allowed:
        agent = Stage(route.agent)
        restated = route.instruction.strip()
        text = restated if restated == words else f"{restated}\n\nThe person's own words: {words}"
        return Decision(agent, route.reason.strip(), text[:_MAX_INSTRUCTION], routed=True)
    fallback = Stage.ARCHITECT if Stage.ARCHITECT in allowed else allowed[0]
    why = (
        f"The {LABELS[fallback]} takes it: the coordinator could not decide, and the "
        f"{LABELS[fallback]} turns any request into tasks."
        if fallback == Stage.ARCHITECT
        else f"The {LABELS[fallback]} takes it: the coordinator could not decide."
    )
    return Decision(fallback, why, words[:_MAX_INSTRUCTION], routed=False)
