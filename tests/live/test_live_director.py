"""Live director test: the real model chooses the next agent for a tiny and for a large idea.

Costs a few cents (two calls on the strong tier) and needs ANTHROPIC_API_KEY. Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k director
"""

from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.director import ApiDirector, Facts, check
from orchestrator.state import Stage
from tests.factories import new_state

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"


def facts(current: Stage) -> Facts:
    return Facts(
        current=current,
        tasks_planned=False,
        tasks_open=False,
        qa_report=False,
        approved=frozenset(),
        moves=1,
        max_moves=40,
        needs_task_list=True,
        needs_qa_report=True,
    )


@pytest.mark.parametrize(
    "idea",
    [
        "Add a --version flag to the existing CLI that prints 1.2.0.",
        "A multi-user marketplace with payments, messaging, reviews and an admin console.",
    ],
)
def test_the_director_chooses_a_step_the_rules_allow(idea: str, tmp_path: Path) -> None:
    state = new_state().evolve(idea=idea, stage=Stage.INTAKE)
    director = ApiDirector(load_config(CONFIG), tmp_path)

    step = director.decide(state, Stage.INTAKE, facts(Stage.INTAKE))

    assert step is not None
    print(f"\n{idea[:50]!r} -> {step.action} {step.agent}: {step.reason} | note: {step.note}")
    assert check(step, facts(Stage.INTAKE)) is None
