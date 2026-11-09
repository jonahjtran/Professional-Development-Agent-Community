"""The Director: the orchestrator decides which agent runs next, after each one finishes.

The agents are what the orchestrator can use; it decides which are needed, what each is told and
given, and whether any agent runs again. There is no fixed order and no required loop. One model
call (the strong tier) reads the idea, the journey so far and what exists, and answers with a
`NextStep`. Code checks the answer against rules that cannot be argued with (the Developer needs a
task list, QA needs the built tasks, the Career coach needs the QA report, a project cannot finish
with tasks open, a project cannot loop forever). If the call fails or the answer breaks a rule, the
project follows the usual order and the trace says so. The person still approves every agent's work.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from orchestrator.assistant_cost import record_assistant_cost
from orchestrator.config import Config
from orchestrator.coordinator import _documents, scrub_keep
from orchestrator.journey import LABELS, ORDER, path_summary
from orchestrator.prompts import DEFAULT_PROMPTS_DIR, Prompt, load_prompt
from orchestrator.state import ProjectState, Stage
from schemas import NextStep

EVENTS = {
    "finished": "an agent finished its work",
    "qa_failed": "QA ran and FAILED",
    "design_problem": "the Developer reported a problem with the design or the task list",
    "task_stuck": "the Developer could not start a task and made no changes",
}

AGENTS = (
    Stage.PRODUCT_OWNER,
    Stage.ARCHITECT,
    Stage.DEVELOPER,
    Stage.QA,
    Stage.CAREER_COACH,
)


@dataclass(frozen=True)
class Facts:
    """What code knows when the next step is chosen and checked."""

    current: Stage  # the agent that just finished
    tasks_planned: bool  # an approved task list exists
    tasks_open: bool  # some of its tasks are not built and approved yet
    qa_report: bool  # QA has produced its report
    approved: frozenset[str]  # names of approved documents
    moves: int  # agent visits so far
    max_moves: int
    needs_task_list: bool = False  # the Developer builds from a task list in this run
    needs_qa_report: bool = False  # the Career coach reads QA's report in this run
    # What happened: "finished" (an agent finished), "qa_failed", "design_problem" (the Developer
    # reported a problem with the design) or "task_stuck" (the Developer could not start a task).
    event: str = "finished"
    problem: str = ""  # what went wrong, for the three failure events
    can_plan: bool = False  # the Architect can plan fixes for a failure


class Directing(Protocol):
    def decide(self, state: ProjectState, finished: Stage, facts: Facts) -> NextStep | None:
        """The next step, or None when it cannot decide."""
        ...


def check(step: NextStep, facts: Facts) -> str | None:
    """Why `step` may not be followed, or None when it may."""
    failure = facts.event != "finished"
    if step.action in ("finish", "ask_person"):
        if step.agent is not None:
            return f"a {step.action} must not name an agent"
    if step.action == "ask_person":
        return None if failure else "nothing went wrong, so there is nothing to ask you"
    if step.action == "finish":
        if failure:
            return "the project cannot finish while a problem is open"
        if facts.tasks_planned and facts.tasks_open:
            return "the project cannot finish while tasks are still open"
        return None
    if step.agent is None:
        return "a run must name an agent"
    agent = Stage(step.agent)
    carry_on = facts.event == "design_problem" and agent == Stage.DEVELOPER
    if agent == facts.current and not carry_on:
        return f"the {LABELS[agent]} is the one that just ran, and nothing about it would change"
    if agent == Stage.DEVELOPER and facts.needs_task_list and not facts.tasks_planned:
        return "the Developer needs the Architect's task list first"
    if agent == Stage.QA and facts.needs_task_list and not facts.tasks_planned:
        return "QA needs the task list, which does not exist yet"
    if agent == Stage.QA and facts.tasks_planned and facts.tasks_open:
        return "QA runs after the tasks are built, and tasks are still open"
    if agent == Stage.CAREER_COACH and facts.needs_qa_report and not facts.qa_report:
        return "the Career coach needs the QA report, so QA must run first"
    missing = [name for name in step.documents if name not in facts.approved]
    if missing:
        return f"{', '.join(missing)} is not an approved document"
    return None


def fallback_step(finished: Stage) -> NextStep:
    """The usual order: the next agent after `finished`, or finish after the last."""
    reason = "The usual order was followed."
    position = ORDER.index(finished)
    if position + 1 >= len(ORDER):
        return NextStep(action="finish", agent=None, reason=reason, note="", documents=[])
    nxt = ORDER[position + 1]
    return NextStep(action="run", agent=nxt.value, reason=reason, note="", documents=[])  # type: ignore[arg-type]


class ApiDirector:
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
        self._prompt = prompt or load_prompt("director", prompts_dir)
        self._client = client

    def decide(self, state: ProjectState, finished: Stage, facts: Facts) -> NextStep | None:
        rework = (
            f"\nA change the person asked for: {scrub_keep(state.rework.note)}"
            if state.rework
            else ""
        )
        problem = f"The problem: {scrub_keep(facts.problem)[:2500]}\n" if facts.problem else ""
        message = (
            f"Project idea: {scrub_keep(state.idea)[:1000]}{rework}\n"
            f"Journey so far: {' → '.join(path_summary(state)) or '(just started)'}\n"
            f"What happened: {EVENTS.get(facts.event, facts.event)}\n"
            f"{problem}"
            f"The agent concerned: {finished.value}\n"
            f"The Architect can plan fixes: {facts.can_plan}\n"
            f"Task list planned: {facts.tasks_planned}; tasks still open: {facts.tasks_open}; "
            f"QA report exists: {facts.qa_report}\n"
            f"Approved documents you may hand on: {', '.join(sorted(facts.approved)) or '(none)'}\n"
            f"Agent visits so far: {facts.moves} of {facts.max_moves}\n\n"
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
                    "format": {
                        "type": "json_schema",
                        "schema": anthropic.transform_schema(NextStep),
                    }
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
        record_assistant_cost(self._runs_dir, state.project_slug, "director_usd", cost)
        texts = [b.text for b in response.content if b.type == "text"]
        try:
            return NextStep.model_validate(json.loads(texts[-1]))
        except (IndexError, ValueError, ValidationError):
            return None
