"""When real QA fails, the Architect plans fixes, the Developer builds them, and QA runs again."""

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.caps import CapAction, CapResolution
from orchestrator.fix_planner import FixRequest, FixResult
from orchestrator.pipeline import (
    AgentError,
    AgentRequest,
    AgentResult,
    PipelineContext,
)
from orchestrator.qa_prepare import QaMaterials
from orchestrator.stages import qa_stage
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.todos import open_todos
from schemas import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    DevResult,
    FixPlan,
    QaAssessment,
    QaReport,
    TaskList,
    Verdict,
)
from tests.factories import make_config
from tests.test_pipeline import FakeCaps, FakeGates
from tests.test_qa_pipeline import FakeQa, at_qa, check
from tests.test_schemas import task_data


def failing(name: str = "tests") -> CheckResult:
    return check(name, CheckStatus.FAILED)


def fix_task(task_id: str = "F1-1", **over: Any) -> dict[str, Any]:
    return task_data(
        id=task_id,
        title="Make the tests pass",
        description="The events test fails: the table is missing a column.",
        acceptance_criteria=["A test proves the column exists"],
        depends_on=over.pop("depends_on", []),
        **over,
    )


def plan(*tasks: dict[str, Any], needs_human: list[str] | None = None) -> FixPlan:
    return FixPlan.model_validate(
        {
            "analysis": "The failing test points at a missing column.",
            "tasks": list(tasks),
            "needs_human": needs_human or [],
        }
    )


class FakePlanner:
    def __init__(self, *plans: FixPlan | Exception) -> None:
        self.plans = list(plans)
        self.requests: list[FixRequest] = []

    def plan(self, request: FixRequest) -> FixResult:
        self.requests.append(request)
        outcome = self.plans.pop(0) if self.plans else plan(fix_task())
        if isinstance(outcome, Exception):
            raise outcome
        return FixResult(plan=outcome, cost_usd=0.2, model="architect-model")


class QaAgent:
    """Judges every criterion it is shown as `pass`, and records each request."""

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        tasks = TaskList.model_validate_json(request.inputs["tasks.json"]).tasks
        assessment = QaAssessment(
            criteria=[
                CriterionVerdict(
                    task_id=t.id, criterion=c, verdict=Verdict.PASS, evidence="tests/t.py::t"
                )
                for t in tasks
                for c in t.acceptance_criteria
            ],
            summary="Judged.",
        )
        return AgentResult(output=assessment.model_dump_json(), cost_usd=0.1, prompt_version="v1")


class DevAgent:
    """Builds whatever task it is asked for."""

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        assert request.task_id is not None
        dev = DevResult(
            task_id=request.task_id,
            branch=f"agent/{request.task_id}",
            summary=f"Built {request.task_id}",
            deviations_from_plan=[],
            known_gaps=[],
        )
        return AgentResult(output=dev.model_dump_json(), cost_usd=0.3, prompt_version="v1")


class Routing:
    def __init__(self, qa: QaAgent, dev: DevAgent) -> None:
        self.qa, self.dev = qa, dev

    def run(self, request: AgentRequest) -> AgentResult:
        return (self.qa if request.stage == Stage.QA else self.dev).run(request)


class FakePublisher:
    repo = Path(".")

    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, result: DevResult, *, title: str) -> DevResult:
        self.published.append(result.task_id)
        return result.model_copy(
            update={"pr_url": f"https://github.com/me/p/pull/{result.task_id}"}
        )

    def diff_stat(self, branch: str) -> str:
        return " 1 file changed"


class Merges:
    def wait_for_merge(self, pr_urls: list[str]) -> bool:
        return True


def make_ctx(
    tmp_path: Path,
    qa: FakeQa,
    planner: FakePlanner | None,
    *,
    gates: FakeGates | None = None,
    max_loops: int = 2,
    caps: FakeCaps | None = None,
) -> tuple[PipelineContext, QaAgent, DevAgent]:
    qa_agent, dev_agent = QaAgent(), DevAgent()
    config = make_config()
    config = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_qa_loops": max_loops})}
    )
    ctx = PipelineContext(
        config=config,
        runs_dir=tmp_path,
        runner=Routing(qa_agent, dev_agent),
        gates=gates or FakeGates(),
        caps=caps or FakeCaps(),
        stages={Stage.QA: qa_stage()},
        qa=qa,
        merges=Merges(),
        publisher=FakePublisher(),
        fixer=planner,
    )
    return ctx, qa_agent, dev_agent


class QaThenFixed(FakeQa):
    """Fails the first `bad_runs` runs, then passes."""

    def __init__(self, bad_runs: int = 1) -> None:
        super().__init__()
        self.bad_runs = bad_runs

    def run(self) -> QaMaterials:
        self.runs += 1
        checks = [failing()] if self.runs <= self.bad_runs else [check()]
        return QaMaterials(checks=checks, tests_md=f"## tests/test_a.py (run {self.runs})")


def with_architecture(tmp_path: Path, state: ProjectState) -> ProjectState:
    from datetime import UTC, datetime

    from orchestrator.artifacts import approve_artifacts, record_artifacts

    store = ArtifactStore(tmp_path, "demo")
    written = {
        "architecture.md": store.write("architecture.md", "# Architecture\nFastAPI", revision=1)
    }
    state = record_artifacts(state, written)
    return approve_artifacts(state, list(written), now=datetime(2026, 11, 5, tzinfo=UTC))


def run_until_gate(tmp_path: Path, ctx: PipelineContext) -> ProjectState:
    """Run through the QA gate (the test gates answer at once) and stop before the Career Coach."""
    from orchestrator.pipeline import advance

    state = with_architecture(tmp_path, at_qa(tmp_path))
    for _ in range(80):
        state = advance(state, ctx)
        if state.stage == Stage.CAREER_COACH or state.status != Status.RUNNING:
            return state
    raise AssertionError("did not get through QA")


def tasks_of(tmp_path: Path, state: ProjectState) -> TaskList:
    store = ArtifactStore(tmp_path, "demo")
    return TaskList.model_validate_json(store.read(state.artifacts["tasks.json"]))


def trace_events(tmp_path: Path) -> list[dict[str, Any]]:
    from orchestrator.trace import trace_path

    return [json.loads(line) for line in trace_path(tmp_path, "demo").read_text().splitlines()]


# --- the loop ---------------------------------------------------------------------------------


def test_a_failed_qa_report_goes_to_the_architect_not_the_human(tmp_path: Path) -> None:
    planner = FakePlanner()
    gates = FakeGates()
    ctx, _qa_agent, _dev = make_ctx(tmp_path, QaThenFixed(1), planner, gates=gates)

    run_until_gate(tmp_path, ctx)

    assert len(planner.requests) == 1
    qa_gates = [g for g in gates.requests if g.stage == Stage.QA]
    # the human sees only the passing rerun, not the failed first report
    assert len(qa_gates) == 1 and qa_gates[0].summary.startswith("# QA report: PASSED")


def test_the_architect_sees_the_failed_report_the_architecture_and_the_tasks(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    ctx, _a, _d = make_ctx(tmp_path, QaThenFixed(1), planner)

    run_until_gate(tmp_path, ctx)

    request = planner.requests[0]
    report = QaReport.model_validate_json(request.report_json)
    assert not report.passed and report.checks[0].status == CheckStatus.FAILED
    assert "T1" in request.tasks_json and request.round == 1
    assert request.project == "demo"


def test_the_fix_tasks_are_added_after_the_existing_ones_and_built_then_qa_runs_again(
    tmp_path: Path,
) -> None:
    qa = QaThenFixed(1)
    ctx, _qa_agent, dev = make_ctx(tmp_path, qa, FakePlanner())

    state = run_until_gate(tmp_path, ctx)

    assert [t.id for t in tasks_of(tmp_path, state).tasks] == ["T1", "T2", "F1-1"]
    assert [r.task_id for r in dev.requests] == ["F1-1"]  # only the new task is built
    assert state.completed_tasks == ["T1", "T2", "F1-1"]
    assert qa.runs == 2  # QA ran again on the fixed project


def test_a_fix_task_is_a_new_task_not_feedback_on_a_finished_one(tmp_path: Path) -> None:
    ctx, _q, dev = make_ctx(tmp_path, QaThenFixed(1), FakePlanner())

    run_until_gate(tmp_path, ctx)

    request = dev.requests[0]
    assert request.previous_draft is None and request.comments is None
    assert request.attempt == 0


def test_each_fix_task_is_still_reviewed_by_the_human_as_a_pull_request(tmp_path: Path) -> None:
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), FakePlanner(), gates=gates)

    run_until_gate(tmp_path, ctx)

    dev_gates = [g for g in gates.requests if g.stage == Stage.DEVELOPER]
    assert len(dev_gates) == 1 and "Built F1-1" in dev_gates[0].summary


def test_the_plan_is_kept_as_a_document_and_in_the_trace(tmp_path: Path) -> None:
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), FakePlanner())

    state = run_until_gate(tmp_path, ctx)

    assert "qa_fix_plan_1.json" in state.artifacts and "qa_fix_plan_1.md" in state.artifacts
    text = ArtifactStore(tmp_path, "demo").read(state.artifacts["qa_fix_plan_1.md"])
    assert "missing column" in text and "F1-1" in text
    assert any(
        e["event"] == "transition" and "qa_fix" in (e["details"] or {})
        for e in trace_events(tmp_path)
    )


def test_the_architects_cost_and_its_trace_events_are_recorded_under_the_architect(
    tmp_path: Path,
) -> None:
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), FakePlanner())

    state = run_until_gate(tmp_path, ctx)

    assert state.cost_usd[Stage.ARCHITECT] == pytest.approx(0.2)
    ends = [
        e for e in trace_events(tmp_path) if e["event"] == "agent_end" and e["stage"] == "architect"
    ]
    assert ends and ends[0]["model"] == "architect-model"


def test_approving_the_passing_report_moves_on_and_resets_the_loop_count(tmp_path: Path) -> None:
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), FakePlanner())

    state = run_until_gate(tmp_path, ctx)

    assert state.stage == Stage.CAREER_COACH and state.qa_loops == 0


def test_the_loop_is_a_recorded_move_back_to_the_developer_and_forward_to_qa_again(
    tmp_path: Path,
) -> None:
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), FakePlanner())

    state = run_until_gate(tmp_path, ctx)

    # (the test project starts at QA, so its path begins with the move back)
    assert [v.stage for v in state.path] == [Stage.DEVELOPER, Stage.QA, Stage.CAREER_COACH]
    back = state.path[0]
    assert back.came_from == Stage.QA and back.reason == "QA failed: fix round 1"


# --- when it does not loop --------------------------------------------------------------------


def test_after_the_allowed_rounds_the_human_decides(tmp_path: Path) -> None:
    planner = FakePlanner(plan(fix_task("F1-1")), plan(fix_task("F2-1")))
    qa = QaThenFixed(bad_runs=9)  # never gets fixed
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, qa, planner, gates=gates, max_loops=2)

    state = run_until_gate(tmp_path, ctx)

    assert len(planner.requests) == 2 and qa.runs == 3
    qa_gate = [g for g in gates.requests if g.stage == Stage.QA][0]
    assert "QA FAILED" in qa_gate.summary and "2" in qa_gate.summary
    assert [t.id for t in tasks_of(tmp_path, state).tasks] == ["T1", "T2", "F1-1", "F2-1"]


def test_with_zero_rounds_allowed_a_failure_goes_straight_to_the_human(tmp_path: Path) -> None:
    planner = FakePlanner()
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(9), planner, gates=gates, max_loops=0)

    run_until_gate(tmp_path, ctx)

    assert planner.requests == []
    assert "QA FAILED" in [g for g in gates.requests if g.stage == Stage.QA][0].summary


def test_without_an_architect_wired_in_a_failure_goes_to_the_human(tmp_path: Path) -> None:
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(9), None, gates=gates)

    run_until_gate(tmp_path, ctx)

    assert "QA FAILED" in [g for g in gates.requests if g.stage == Stage.QA][0].summary


def test_a_tool_that_is_not_installed_is_not_something_the_architect_can_fix(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    gates = FakeGates()
    ctx, _q, _d = make_ctx(
        tmp_path,
        FakeQa([check("secret scan", CheckStatus.SKIPPED)]),
        planner,
        gates=gates,
    )

    run_until_gate(tmp_path, ctx)

    assert planner.requests == []
    assert "QA FAILED" in [g for g in gates.requests if g.stage == Stage.QA][0].summary


def test_a_planner_failure_falls_back_to_the_human_and_still_bills_nothing_twice(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(AgentError("the model is unreachable", cost_usd=0.05))
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(9), planner, gates=gates)

    state = run_until_gate(tmp_path, ctx)

    assert state.status == Status.RUNNING and state.stage == Stage.CAREER_COACH  # not halted
    assert state.cost_usd[Stage.ARCHITECT] == pytest.approx(0.05)
    summary = [g for g in gates.requests if g.stage == Stage.QA][0].summary
    assert "QA FAILED" in summary and "unreachable" in summary


def test_a_plan_with_nothing_to_build_goes_to_the_human_and_raises_what_only_they_can_do(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(plan(needs_human=["Add DATABASE_URL to the CI secrets"]))
    gates = FakeGates()
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(9), planner, gates=gates)

    run_until_gate(tmp_path, ctx)

    assert "QA FAILED" in [g for g in gates.requests if g.stage == Stage.QA][0].summary
    assert [t.title for t in open_todos(tmp_path, "demo")] == ["Add DATABASE_URL to the CI secrets"]


def test_what_only_the_human_can_do_is_raised_even_when_there_are_tasks_to_build(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(plan(fix_task(), needs_human=["Set the Apollo key"]))
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(1), planner)

    run_until_gate(tmp_path, ctx)

    assert [t.title for t in open_todos(tmp_path, "demo")] == ["Set the Apollo key"]


@pytest.mark.parametrize(
    "bad",
    [
        plan(fix_task("T1")),  # an id that already exists
        plan(fix_task("F1-1", depends_on=["nope"])),  # a dependency that does not exist
        plan(fix_task("F1-1"), fix_task("F1-1")),  # the same id twice
    ],
)
def test_an_invalid_plan_changes_nothing_and_goes_to_the_human(
    tmp_path: Path, bad: FixPlan
) -> None:
    planner = FakePlanner(bad)
    gates = FakeGates()
    ctx, qa_agent, dev = make_ctx(tmp_path, QaThenFixed(9), planner, gates=gates)

    state = run_until_gate(tmp_path, ctx)

    assert [t.id for t in tasks_of(tmp_path, state).tasks] == ["T1", "T2"]
    assert dev.requests == []
    assert "QA FAILED" in [g for g in gates.requests if g.stage == Stage.QA][0].summary


def test_a_fix_task_may_depend_on_an_earlier_fix_task(tmp_path: Path) -> None:
    planner = FakePlanner(plan(fix_task("F1-1"), fix_task("F1-2", depends_on=["F1-1"])))
    ctx, _q, dev = make_ctx(tmp_path, QaThenFixed(1), planner)

    run_until_gate(tmp_path, ctx)

    assert [r.task_id for r in dev.requests] == ["F1-1", "F1-2"]


def test_an_exceeded_architect_budget_asks_the_human_before_calling_the_model(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    ctx, _q, _d = make_ctx(tmp_path, QaThenFixed(9), planner, caps=caps)
    state = with_architecture(tmp_path, at_qa(tmp_path)).evolve(
        cost_usd={Stage.ARCHITECT: 5.0}  # past the Architect's cap
    )

    from orchestrator.pipeline import advance

    for _ in range(10):
        state = advance(state, ctx)
        if state.status != Status.RUNNING:
            break

    assert planner.requests == [] and state.status == Status.HALTED
    assert caps.requests and caps.requests[0].stage == Stage.ARCHITECT
