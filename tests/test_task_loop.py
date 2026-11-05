import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.caps import CapAction, CapResolution
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import (
    AgentError,
    AgentRequest,
    AgentResult,
    PipelineContext,
    RoutingRunner,
    advance,
    run_pipeline,
)
from orchestrator.publish import PublishError
from orchestrator.state import ProjectState, Stage, Status, load_state, save_state
from orchestrator.trace import trace_path
from schemas import DevResult
from tests.factories import make_config, new_state
from tests.test_pipeline import FakeCaps, FakeGates, changes, reject
from tests.test_schemas import task_data

TASKS = {
    "tasks": [
        task_data(id="T1", title="Create the table"),
        task_data(id="T2", title="Add the API", depends_on=["T1"]),
    ]
}


def dev_json(task_id: str, attempt: int = 0, summary: str | None = None) -> str:
    branch = f"agent/{task_id}" if attempt == 0 else f"agent/{task_id}-r{attempt}"
    return DevResult(
        task_id=task_id,
        branch=branch,
        summary=summary or f"Built {task_id}",
        deviations_from_plan=["Used SQLite"],
        known_gaps=["No index"],
    ).model_dump_json()


class DevAgent:
    """Acts like the Developer runner: returns a DevResult for the task it was asked to build."""

    def __init__(self, cost: float = 0.3) -> None:
        self.cost = cost
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        if request.stage != Stage.DEVELOPER:  # QA and the career coach, once the tasks are done
            return AgentResult(output=f"{request.stage.value} text", cost_usd=0.1)
        self.requests.append(request)
        assert request.task_id is not None
        n = sum(1 for r in self.requests if r.task_id == request.task_id)
        return AgentResult(
            output=dev_json(request.task_id, request.attempt, f"Built {request.task_id} (try {n})"),
            cost_usd=self.cost,
            prompt_version="v1",
        )


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, str]] = []
        self.repo = Path(".")

    def publish(self, result: DevResult, *, title: str) -> DevResult:
        if self.fail:
            raise PublishError("push failed: protected branch")
        self.published.append((result.branch, title))
        n = len(self.published)
        return result.model_copy(update={"pr_url": f"https://github.com/me/p/pull/{n}"})

    def diff_stat(self, branch: str) -> str:
        return f" {branch}: 2 files changed, 10 insertions(+)"


class Recorder(FakeGates):
    pass


def make_ctx(
    tmp_path: Path,
    agent: Any,
    gates: Any = None,
    publisher: FakePublisher | None = None,
    caps: Any = None,
) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent,
        gates=gates or FakeGates(),
        caps=caps or FakeCaps(),
        publisher=publisher if publisher is not None else FakePublisher(),
    )


def at_developer(tmp_path: Path, **fields: Any) -> ProjectState:
    """A project whose Architect output is approved and which is ready for the Developer."""
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(
        new_state(),
        {
            "tasks.json": store.write("tasks.json", json.dumps(TASKS), revision=1),
            "architecture.md": store.write("architecture.md", "# Architecture", revision=1),
        },
    )
    state = approve_artifacts(
        state, ["tasks.json", "architecture.md"], now=datetime(2026, 11, 4, tzinfo=UTC)
    )
    return state.evolve(stage=Stage.DEVELOPER, **fields)


# --- the loop --------------------------------------------------------------------------------


def test_each_task_is_built_published_and_reviewed_in_dependency_order(tmp_path: Path) -> None:
    agent, publisher, gates = DevAgent(), FakePublisher(), FakeGates()

    state = run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, agent, gates, publisher))

    assert [r.task_id for r in agent.requests] == ["T1", "T2"]
    assert [title for _, title in publisher.published] == [
        "T1: Create the table",
        "T2: Add the API",
    ]
    assert state.completed_tasks == ["T1", "T2"]
    assert [g.stage for g in gates.requests if g.stage == Stage.DEVELOPER] == [Stage.DEVELOPER] * 2


def test_the_developer_is_given_the_approved_task_list_and_architecture(tmp_path: Path) -> None:
    agent = DevAgent()

    advance(at_developer(tmp_path), make_ctx(tmp_path, agent))

    [request] = agent.requests
    assert set(request.inputs) == {"tasks.json", "architecture.md"}
    assert request.task_id == "T1"
    assert request.attempt == 0


def test_the_next_task_does_not_start_until_the_current_one_is_approved(tmp_path: Path) -> None:
    agent = DevAgent()
    state = at_developer(tmp_path)

    state = advance(state, make_ctx(tmp_path, agent, FakeGates(changes("fix T1"))))

    # T1 needs changes, so T2 (which depends on it) must not start yet.
    assert [r.task_id for r in agent.requests] == ["T1"]
    assert state.completed_tasks == []
    assert state.current_task_id == "T1"


def test_when_every_task_is_done_the_project_moves_on_to_qa(tmp_path: Path) -> None:
    state = at_developer(tmp_path, completed_tasks=["T1", "T2"])
    agent = DevAgent()

    state = advance(state, make_ctx(tmp_path, agent))

    assert state.stage == Stage.QA
    assert agent.requests == []  # no Developer call when there is nothing left to build


def test_the_developers_cost_and_pull_request_are_recorded(tmp_path: Path) -> None:
    state = advance(at_developer(tmp_path), make_ctx(tmp_path, DevAgent(cost=0.4)))

    assert state.cost_usd[Stage.DEVELOPER] == pytest.approx(0.4)
    saved = state.artifacts["dev/T1.json"]
    text = (tmp_path / "demo" / saved.path).read_text()
    assert DevResult.model_validate_json(text).pr_url == "https://github.com/me/p/pull/1"
    assert saved.approved_at is not None


# --- the PR gate -----------------------------------------------------------------------------


def test_the_human_sees_the_pr_link_the_summary_the_gaps_and_the_diff(tmp_path: Path) -> None:
    gates = FakeGates()

    advance(at_developer(tmp_path), make_ctx(tmp_path, DevAgent(), gates))

    request: GateRequest = gates.requests[0]
    assert request.stage == Stage.DEVELOPER
    for expected in (
        "https://github.com/me/p/pull/1",
        "Built T1",
        "Used SQLite",
        "No index",
        "2 files changed",
    ):
        assert expected in request.summary


def test_approving_resets_the_stage_revision_count_for_the_next_task(tmp_path: Path) -> None:
    gates = FakeGates(changes("one"), changes("two"), GateDecision(outcome=GateOutcome.APPROVE))
    state = at_developer(tmp_path)

    state = run_pipeline(state, make_ctx(tmp_path, DevAgent(), gates))

    assert state.revisions.get(Stage.DEVELOPER, 0) == 0


def test_requesting_changes_reruns_the_same_task_with_the_previous_report_and_comments(
    tmp_path: Path,
) -> None:
    agent, publisher = DevAgent(), FakePublisher()
    gates = FakeGates(changes("Add a docstring"))

    run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, agent, gates, publisher))

    first, second = agent.requests[0], agent.requests[1]
    assert second.task_id == "T1" and second.attempt == 0
    assert second.comments == "Add a docstring"
    assert second.previous_draft is not None
    assert DevResult.model_validate_json(second.previous_draft).summary == "Built T1 (try 1)"
    assert first.previous_draft is None
    assert [b for b, _ in publisher.published[:2]] == ["agent/T1", "agent/T1"]


def test_a_rejection_starts_the_task_again_on_a_fresh_branch(tmp_path: Path) -> None:
    agent, publisher = DevAgent(), FakePublisher()
    gates = FakeGates(reject())

    run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, agent, gates, publisher))

    first, second = agent.requests[0], agent.requests[1]
    assert (first.attempt, second.attempt) == (0, 1)
    assert second.previous_draft is None
    assert publisher.published[1][0] == "agent/T1-r1"


def test_a_rejection_with_halt_stops_the_project(tmp_path: Path) -> None:
    state = run_pipeline(
        at_developer(tmp_path), make_ctx(tmp_path, DevAgent(), FakeGates(reject(halt=True)))
    )

    assert state.status == Status.HALTED
    assert state.completed_tasks == []


# --- failures --------------------------------------------------------------------------------


def test_a_publish_failure_halts_the_run_and_keeps_the_cost(tmp_path: Path) -> None:
    state = run_pipeline(
        at_developer(tmp_path),
        make_ctx(tmp_path, DevAgent(cost=0.4), publisher=FakePublisher(fail=True)),
    )

    assert state.status == Status.HALTED
    assert state.cost_usd[Stage.DEVELOPER] == pytest.approx(0.4)
    events = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    assert (
        "protected branch"
        in next(e for e in events if e["event"] == "agent_end")["details"]["error"]
    )


def test_an_agent_failure_halts_the_run(tmp_path: Path) -> None:
    class Failing(DevAgent):
        def run(self, request: AgentRequest) -> AgentResult:
            raise AgentError("the Developer made no commits", cost_usd=0.6)

    state = run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, Failing()))

    assert state.status == Status.HALTED
    assert state.cost_usd[Stage.DEVELOPER] == pytest.approx(0.6)


def test_a_result_that_is_not_a_dev_result_halts_the_run(tmp_path: Path) -> None:
    class Garbage(DevAgent):
        def run(self, request: AgentRequest) -> AgentResult:
            return AgentResult(output="not json", cost_usd=0.2)

    state = run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, Garbage()))

    assert state.status == Status.HALTED
    assert state.cost_usd[Stage.DEVELOPER] == pytest.approx(0.2)


def test_a_budget_pause_applies_to_each_developer_call(tmp_path: Path) -> None:
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    state = at_developer(tmp_path, cost_usd={Stage.DEVELOPER: 10.0})  # at the stage cap

    state = advance(state, make_ctx(tmp_path, DevAgent(), caps=caps))

    assert state.status == Status.HALTED


# --- resuming in the middle of the loop ------------------------------------------------------


class Crash(BaseException):
    pass


class CrashAtSecondGate(FakeGates):
    def decide(self, request: GateRequest) -> GateDecision:
        if len(self.requests) >= 1:
            raise Crash
        return super().decide(request)


def test_a_crash_at_the_second_tasks_gate_resumes_there_without_rebuilding_it(
    tmp_path: Path,
) -> None:
    agent = DevAgent()
    ctx = make_ctx(tmp_path, agent, CrashAtSecondGate())
    with pytest.raises(Crash):
        run_pipeline(at_developer(tmp_path), ctx)
    saved = load_state(tmp_path, "demo")
    assert saved.status == Status.AWAITING_HUMAN
    assert saved.completed_tasks == ["T1"]

    resume_agent, gates = DevAgent(), FakeGates()
    state = run_pipeline(saved, make_ctx(tmp_path, resume_agent, gates))

    assert resume_agent.requests == []
    assert state.completed_tasks == ["T1", "T2"]
    assert "pull/2" in gates.requests[0].summary


def test_completed_tasks_survive_save_and_load(tmp_path: Path) -> None:
    save_state(tmp_path, new_state().evolve(completed_tasks=["T1", "T2"]))

    assert load_state(tmp_path, "demo").completed_tasks == ["T1", "T2"]


# --- without a publisher the old behavior is unchanged ----------------------------------------


def test_without_a_publisher_the_developer_stage_works_as_before(tmp_path: Path) -> None:
    class Plain(DevAgent):
        def run(self, request: AgentRequest) -> AgentResult:
            return AgentResult(output=f"{request.stage.value} text", cost_usd=0.1)

    ctx = PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=Plain(),
        gates=FakeGates(),
        caps=FakeCaps(),
    )

    state = run_pipeline(new_state(), ctx)

    assert state.status == Status.DONE
    assert state.completed_tasks == []


# --- routing different runners per stage -----------------------------------------------------


def test_the_router_sends_each_stage_to_its_own_runner() -> None:
    a, b = DevAgent(), DevAgent()
    router = RoutingRunner({Stage.DEVELOPER: a, Stage.ARCHITECT: b})

    router.run(AgentRequest(stage=Stage.DEVELOPER, project="p", idea="x", task_id="T1"))

    assert len(a.requests) == 1 and b.requests == []


def test_the_router_uses_the_default_for_other_stages() -> None:
    class Recording:
        def __init__(self) -> None:
            self.stages: list[Stage] = []

        def run(self, request: AgentRequest) -> AgentResult:
            self.stages.append(request.stage)
            return AgentResult(output="x", cost_usd=0.0)

    default = Recording()
    router = RoutingRunner({}, default=default)

    router.run(AgentRequest(stage=Stage.QA, project="p", idea="x"))

    assert default.stages == [Stage.QA]


def test_a_stage_with_no_runner_and_no_default_is_an_error() -> None:
    with pytest.raises(AgentError, match="qa"):
        RoutingRunner({}).run(AgentRequest(stage=Stage.QA, project="p", idea="x"))


def test_a_new_task_starts_on_the_first_branch_even_after_an_earlier_rejection(
    tmp_path: Path,
) -> None:
    agent = DevAgent()
    gates = FakeGates(reject(), GateDecision(outcome=GateOutcome.APPROVE))

    run_pipeline(at_developer(tmp_path), make_ctx(tmp_path, agent, gates))

    assert [(r.task_id, r.attempt) for r in agent.requests] == [("T1", 0), ("T1", 1), ("T2", 0)]


def test_rejections_at_earlier_stages_do_not_count_as_developer_attempts(tmp_path: Path) -> None:
    from orchestrator.state import GateRecord

    earlier = GateRecord(
        stage=Stage.PRODUCT_OWNER,
        outcome=GateOutcome.REJECT,
        comments="redo",
        ts=datetime(2026, 11, 4, tzinfo=UTC),
    )
    agent = DevAgent()

    advance(at_developer(tmp_path, gate_history=[earlier]), make_ctx(tmp_path, agent))

    assert agent.requests[0].attempt == 0
