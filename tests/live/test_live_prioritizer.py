"""Live prioritizer test: the real Architect decides an order after a priority request.

Costs a few cents (one call on the strong tier) and needs ANTHROPIC_API_KEY. It checks what the
fakes cannot: that the prompt makes the model return an order the orchestrator accepts (every
unfinished task once, dependencies first) and that it moves a task the person asked for.
Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k prioritizer
"""

import json
from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.prioritizer import ApiPrioritizer, PriorityRequest
from orchestrator.task_control import TaskControlError, apply_order
from tests.test_priority_pipeline_support import three_tasks_state

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"


def test_the_architect_moves_an_independent_task_up_and_the_order_is_valid(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)  # T1, T2 (needs T1), T3 (independent)
    from orchestrator.artifacts import ArtifactStore
    from schemas import TaskList

    store = ArtifactStore(tmp_path, "demo")
    listing = TaskList.model_validate_json(store.read(state.artifacts["tasks.json"]))
    request = PriorityRequest(
        project="demo",
        idea="Track events for a student org",
        architecture="# Architecture\nA FastAPI service with a SQLite table.",
        tasks_json=listing.model_dump_json(indent=2),
        asked=[{"task_id": "T3", "reason": "The demo is on Friday and it needs dark mode."}],
    )

    result = ApiPrioritizer(load_config(CONFIG)).prioritize(request)

    print("\norder:", result.order.order, "\nreason:", result.order.reason)
    try:
        apply_order(tmp_path, state, result.order)
        accepted = True
    except TaskControlError as problem:
        print("refused:", problem)
        accepted = False
    assert accepted
    assert result.order.order.index("T1") < result.order.order.index("T2")
    assert result.order.order.index("T3") < result.order.order.index("T2")  # it was moved up
    assert json.dumps(result.order.reason)  # and it says why
