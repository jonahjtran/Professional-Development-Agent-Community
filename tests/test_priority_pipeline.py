"""The Architect decides a priority request between tasks; the project carries on either way."""

from pathlib import Path

import pytest

from orchestrator.pipeline import AgentError, PipelineContext, advance
from orchestrator.prioritizer import PriorityRequest, PriorityResult
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.task_control import pending_priorities, priority_log, request_priority
from schemas import TaskOrder
from tests.factories import make_config
from tests.test_pipeline import FakeCaps, FakeGates
from tests.test_priority_pipeline_support import Dev, three_tasks_state  # noqa: F401
from tests.test_task_loop import FakePublisher


class FakePrioritizer:
    def __init__(self, order: list[str] | None = None, reason: str = "Because.") -> None:
        self.order = order
        self.reason = reason
        self.requests: list[PriorityRequest] = []

    def prioritize(self, request: PriorityRequest) -> PriorityResult:
        self.requests.append(request)
        order = self.order or ["T1", "T2", "T3"]
        return PriorityResult(TaskOrder(order=order, reason=self.reason), cost_usd=0.4)


class FailingPrioritizer:
    def prioritize(self, request: PriorityRequest) -> PriorityResult:
        raise AgentError("the Architect is unavailable", cost_usd=0.1)


def ctx_for(tmp_path: Path, dev: Dev, prioritizer: object | None) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=dev,
        gates=FakeGates(),
        caps=FakeCaps(),
        publisher=FakePublisher(),
        prioritizer=prioritizer,  # type: ignore[arg-type]
    )


def asked(tmp_path: Path, state: ProjectState) -> None:
    request_priority(tmp_path, state, "T3", "The demo is Friday")


def first_built(dev: Dev) -> str | None:
    return dev.requests[0].task_id if dev.requests else None


def test_the_architects_order_decides_which_task_is_built_next(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    dev = Dev()
    prioritizer = FakePrioritizer(["T3", "T1", "T2"], "T3 has no dependencies.")

    advance(state, ctx_for(tmp_path, dev, prioritizer))

    assert first_built(dev) == "T3"
    [entry] = priority_log(tmp_path, "demo")
    assert entry["status"] == "moved" and entry["architect_reason"] == "T3 has no dependencies."


def test_the_architect_is_given_the_unfinished_tasks_and_the_request(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    prioritizer = FakePrioritizer()

    advance(state, ctx_for(tmp_path, Dev(), prioritizer))

    [seen] = prioritizer.requests
    assert "T3" in seen.tasks_json and seen.asked[0]["reason"] == "The demo is Friday"
    assert seen.architecture.startswith("# Architecture")


def test_the_architect_may_keep_the_order_and_that_is_logged(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    dev = Dev()

    advance(
        state, ctx_for(tmp_path, dev, FakePrioritizer(["T1", "T2", "T3"], "Keep it: T1 first."))
    )

    assert first_built(dev) == "T1"
    assert priority_log(tmp_path, "demo")[0]["status"] == "kept"


def test_an_invalid_order_is_not_applied_and_the_project_carries_on(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    dev = Dev()

    advance(state, ctx_for(tmp_path, dev, FakePrioritizer(["T2", "T1", "T3"])))  # T2 before T1

    assert first_built(dev) == "T1"
    entry = priority_log(tmp_path, "demo")[0]
    assert entry["status"] == "not_applied" and "T2" in entry["architect_reason"]


def test_a_failed_architect_call_does_not_stop_the_project(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    dev = Dev()

    after = advance(state, ctx_for(tmp_path, dev, FailingPrioritizer()))

    assert first_built(dev) == "T1" and after.status != Status.HALTED
    entry = priority_log(tmp_path, "demo")[0]
    assert entry["status"] == "not_applied" and "unavailable" in entry["architect_reason"]
    assert after.cost_usd[Stage.ARCHITECT] == pytest.approx(0.1)  # the failed call is billed


def test_the_architects_call_is_billed_to_the_architect(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)

    after = advance(state, ctx_for(tmp_path, Dev(), FakePrioritizer()))

    assert after.cost_usd[Stage.ARCHITECT] == pytest.approx(0.4)


def test_without_a_request_the_architect_is_not_called(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    prioritizer = FakePrioritizer()

    advance(state, ctx_for(tmp_path, Dev(), prioritizer))

    assert prioritizer.requests == []


def test_a_request_waits_when_there_is_no_architect_to_ask(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path)
    asked(tmp_path, state)
    dev = Dev()

    advance(state, ctx_for(tmp_path, dev, None))

    assert first_built(dev) == "T1" and len(pending_priorities(tmp_path, "demo")) == 1


def test_a_task_being_built_is_never_moved_by_a_later_request(tmp_path: Path) -> None:
    state = three_tasks_state(tmp_path, current_task_id="T1")
    asked(tmp_path, state)
    dev = Dev()
    prioritizer = FakePrioritizer(["T3", "T1", "T2"])

    advance(state, ctx_for(tmp_path, dev, prioritizer))

    assert first_built(dev) == "T1" and prioritizer.requests == []  # decided at the next gap
