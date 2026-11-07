"""Live coordinator test: the real model routes four kinds of request to the right agent.

Costs a few cents (four calls on the strong tier) and needs ANTHROPIC_API_KEY. Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k coordinator
"""

from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.coordinator import Coordinator, choose
from orchestrator.state import Stage, Status
from tests.factories import new_state

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

CASES = [
    # (what the person says, the agents that would be right)
    ("Make the code adjustments to pass the PRD and implement full functionality", {"architect"}),
    ("The requirements are wrong: users should also be able to export a CSV", {"product_owner"}),
    ("Rename the 'Save' button on the settings page to 'Apply'", {"developer", "architect"}),
    ("Rewrite the case study to focus on the data model", {"career_coach"}),
]


@pytest.mark.parametrize(("request_text", "right"), CASES)
def test_the_coordinator_routes_a_request_to_a_sensible_agent(
    request_text: str, right: set[str], tmp_path: Path
) -> None:
    state = new_state().evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, idea="A coffee chat tracker"
    )
    coordinator = Coordinator(load_config(CONFIG), tmp_path)

    decision = choose(coordinator, tmp_path, state, request_text)

    print(f"\n{request_text!r} -> {decision.agent.value}: {decision.reason}")
    assert decision.routed
    assert decision.agent.value in right
