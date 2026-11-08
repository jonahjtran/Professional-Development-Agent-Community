"""Shared set-up for the priority tests: three unfinished tasks and a Developer that builds them."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.pipeline import AgentRequest, AgentResult
from orchestrator.state import ProjectState
from schemas import DevResult, TaskList
from tests.test_schemas import task_data
from tests.test_task_loop import at_developer


def three_tasks_state(tmp_path: Path, **fields: Any) -> ProjectState:
    """T1, then T2 (needs T1), T3 (needs nothing): all unfinished."""
    state = at_developer(tmp_path, **fields)
    store = ArtifactStore(tmp_path, "demo")
    listing = TaskList.model_validate(
        {
            "tasks": [
                task_data(id="T1", title="Create the table"),
                task_data(id="T2", title="Add the API", depends_on=["T1"]),
                task_data(id="T3", title="Add dark mode"),
            ]
        }
    )
    record = state.artifacts["tasks.json"]
    written = {
        "tasks.json": store.write(
            "tasks.json", listing.model_dump_json(), revision=record.revision + 1
        )
    }
    return approve_artifacts(
        record_artifacts(state, written), ["tasks.json"], now=datetime(2026, 11, 7, tzinfo=UTC)
    )


class Dev:
    """Builds whichever task it is asked for and remembers the order it was asked in."""

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        assert request.task_id is not None
        result = DevResult(
            task_id=request.task_id,
            branch=f"agent/{request.task_id}",
            summary=f"Built {request.task_id}",
            deviations_from_plan=[],
            known_gaps=[],
        )
        return AgentResult(output=result.model_dump_json(), cost_usd=0.3, prompt_version="v1")
