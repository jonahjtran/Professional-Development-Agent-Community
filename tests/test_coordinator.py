import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic

from orchestrator.assistant_cost import assistant_totals
from orchestrator.coordinator import Coordinator, allowed_agents, choose
from orchestrator.state import ProjectState, Stage, Status
from tests.factories import make_config, new_state


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply(agent: str = "architect", reason: str = "It spans several parts.") -> Any:
    body = {"agent": agent, "reason": reason, "instruction": "Plan tasks for the dashboard."}
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(body))],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def at(stage: Stage) -> ProjectState:
    return new_state().evolve(stage=stage, status=Status.HALTED)


def coordinator(tmp_path: Path, client: FakeClient) -> Coordinator:
    return Coordinator(make_config(), tmp_path, client=client)


def test_only_agents_the_project_has_reached_can_be_chosen() -> None:
    assert allowed_agents(at(Stage.ARCHITECT)) == (Stage.PRODUCT_OWNER, Stage.ARCHITECT)
    assert Stage.CAREER_COACH in allowed_agents(at(Stage.CAREER_COACH))
    assert allowed_agents(at(Stage.INTAKE)) == ()


def test_the_coordinator_picks_the_agent_and_says_why(tmp_path: Path) -> None:
    client = FakeClient(reply("architect", "The change is big and vague."))

    decision = choose(coordinator(tmp_path, client), tmp_path, at(Stage.DEVELOPER), "Make it work")

    assert decision.agent == Stage.ARCHITECT
    assert decision.reason == "The change is big and vague."
    assert "Plan tasks for the dashboard." in decision.instruction
    assert "Make it work" in decision.instruction  # the person's own words are kept
    assert decision.routed is True


def test_the_instruction_and_project_are_sent_as_data_with_the_allowed_agents(
    tmp_path: Path,
) -> None:
    client = FakeClient(reply())

    choose(coordinator(tmp_path, client), tmp_path, at(Stage.ARCHITECT), "Add dark mode")

    sent = json.dumps(client.calls[0]["messages"])
    assert "Add dark mode" in sent
    assert "product_owner" in sent and "architect" in sent
    assert "developer" not in sent.split("Allowed agents")[1].split("\\n\\n")[0]


def test_an_agent_the_project_has_not_reached_falls_back(tmp_path: Path) -> None:
    client = FakeClient(reply("qa"))

    decision = choose(coordinator(tmp_path, client), tmp_path, at(Stage.ARCHITECT), "Re-test")

    assert decision.agent == Stage.ARCHITECT
    assert decision.routed is False


def test_when_the_service_is_down_a_request_goes_to_the_architect(tmp_path: Path) -> None:
    error = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    client = FakeClient(error)

    decision = choose(coordinator(tmp_path, client), tmp_path, at(Stage.DEVELOPER), "Fix it")

    assert decision.agent == Stage.ARCHITECT
    assert decision.routed is False
    assert "Architect" in decision.reason


def test_an_unusable_answer_falls_back(tmp_path: Path) -> None:
    junk = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="not json")],
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )

    decision = choose(coordinator(tmp_path, FakeClient(junk)), tmp_path, at(Stage.QA), "Something")

    assert decision.agent == Stage.ARCHITECT
    assert decision.routed is False


def test_a_project_still_at_the_product_owner_goes_there(tmp_path: Path) -> None:
    decision = choose(
        coordinator(tmp_path, FakeClient(reply("architect"))),
        tmp_path,
        at(Stage.PRODUCT_OWNER),
        "Change the scope",
    )

    assert decision.agent == Stage.PRODUCT_OWNER


def test_the_cost_goes_to_the_assistant_ledger(tmp_path: Path) -> None:
    state = at(Stage.DEVELOPER)

    choose(coordinator(tmp_path, FakeClient(reply())), tmp_path, state, "Change it")

    assert assistant_totals(tmp_path, state.project_slug).get("coordinator_usd", 0) > 0
