import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.gates import GateDecision, GateOutcome
from orchestrator.pipeline import (
    AgentRequest,
    AgentResult,
    PipelineContext,
    advance,
    run_pipeline,
)
from orchestrator.qa_prepare import QaError, QaMaterials
from orchestrator.stages import qa_stage
from orchestrator.state import (
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    load_state,
)
from orchestrator.trace import trace_path
from schemas import (
    CheckResult,
    CheckStatus,
    CriterionVerdict,
    DevResult,
    QaAssessment,
    QaReport,
    TaskList,
    Verdict,
)
from tests.factories import make_config, new_state
from tests.test_pipeline import FakeCaps, FakeGates, changes
from tests.test_schemas import task_data

CRITERION = "Table exists after migrate"
TASKS = TaskList.model_validate(
    {"tasks": [task_data(id="T1"), task_data(id="T2", title="API", acceptance_criteria=["Serves"])]}
)


def check(name: str = "tests", status: CheckStatus = CheckStatus.PASSED) -> CheckResult:
    return CheckResult(name=name, command=name, status=status, output_tail="output")


class FakeQa:
    """Stands in for the real tools. `waves` is how many times a PR is unmerged before it merges."""

    def __init__(
        self,
        checks: list[CheckResult] | None = None,
        unmerged_calls: int = 0,
        error: str | None = None,
    ) -> None:
        self.checks = checks if checks is not None else [check()]
        self.unmerged_left = unmerged_calls
        self.error = error
        self.asked: list[list[str]] = []
        self.runs = 0

    def unmerged(self, pr_urls: list[str]) -> list[str]:
        self.asked.append(pr_urls)
        if self.unmerged_left > 0:
            self.unmerged_left -= 1
            return pr_urls
        return []

    def run(self) -> QaMaterials:
        if self.error:
            raise QaError(self.error)
        self.runs += 1
        return QaMaterials(checks=self.checks, tests_md=f"## tests/test_a.py (run {self.runs})")


class FakeMerges:
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.shown: list[list[str]] = []

    def wait_for_merge(self, pr_urls: list[str]) -> bool:
        self.shown.append(pr_urls)
        return self.answers.pop(0) if self.answers else True


class QaAgent:
    """Judges every criterion with `verdict`, and records each request."""

    def __init__(self, verdict: Verdict = Verdict.PASS) -> None:
        self.verdict = verdict
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        assessment = QaAssessment(
            criteria=[
                CriterionVerdict(
                    task_id=t.id,
                    criterion=c,
                    verdict=self.verdict,
                    evidence="tests/test_a.py::test_x",
                )
                for t in TASKS.tasks
                for c in t.acceptance_criteria
            ],
            summary="Judged.",
        )
        return AgentResult(output=assessment.model_dump_json(), cost_usd=0.1, prompt_version="v1")


def at_qa(tmp_path: Path) -> ProjectState:
    """A project whose two Developer tasks are approved, ready for QA."""
    store = ArtifactStore(tmp_path, "demo")
    state = new_state()
    artifacts = {"tasks.json": store.write("tasks.json", TASKS.model_dump_json(), revision=1)}
    for n, task in enumerate(TASKS.tasks, start=1):
        dev = DevResult(
            task_id=task.id,
            branch=f"agent/{task.id}",
            pr_url=f"https://github.com/me/p/pull/{n}",
            summary="Built",
            deviations_from_plan=[],
            known_gaps=[],
        )
        artifacts[f"dev/{task.id}.json"] = store.write(
            f"dev/{task.id}.json", dev.model_dump_json(), revision=1
        )
    state = record_artifacts(state, artifacts)
    state = approve_artifacts(state, list(artifacts), now=datetime(2026, 11, 5, tzinfo=UTC))
    return state.evolve(stage=Stage.QA, completed_tasks=["T1", "T2"])


def make_ctx(
    tmp_path: Path,
    agent: Any,
    qa: FakeQa | None,
    gates: FakeGates | None = None,
    merges: FakeMerges | None = None,
) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent,
        gates=gates or FakeGates(),
        caps=FakeCaps(),
        stages={Stage.QA: qa_stage()},
        qa=qa,
        merges=merges,
    )


def run_qa(
    tmp_path: Path, agent: QaAgent, qa: FakeQa, gates: FakeGates | None = None, **kw: Any
) -> ProjectState:
    # Stop after the QA gate: the Career Coach has no agent here.
    gates = gates or FakeGates()
    ctx = make_ctx(tmp_path, agent, qa, gates, kw.get("merges"))
    state = at_qa(tmp_path)
    while state.stage == Stage.QA and state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)
    return state


def report_of(tmp_path: Path, state: ProjectState) -> QaReport:
    store = ArtifactStore(tmp_path, "demo")
    return QaReport.model_validate_json(store.read(state.artifacts["test_report.json"]))


# --- the happy path --------------------------------------------------------------------------


def test_the_tools_run_and_the_agent_sees_their_results_and_the_tests(tmp_path: Path) -> None:
    agent, qa = QaAgent(), FakeQa()

    run_qa(tmp_path, agent, qa)

    [request] = agent.requests
    assert set(request.inputs) == {"tasks.json", "qa_checks.json", "qa_tests.md"}
    assert json.loads(request.inputs["qa_checks.json"])[0]["name"] == "tests"
    assert "test_a.py" in request.inputs["qa_tests.md"]


def test_a_clean_project_produces_a_passing_report_and_moves_to_the_career_coach(
    tmp_path: Path,
) -> None:
    state = run_qa(tmp_path, QaAgent(), FakeQa())

    assert state.stage == Stage.CAREER_COACH
    assert report_of(tmp_path, state).passed


def test_the_human_sees_the_rendered_report_at_the_qa_gate(tmp_path: Path) -> None:
    gates = FakeGates()

    run_qa(tmp_path, QaAgent(), FakeQa(), gates)

    [request] = [r for r in gates.requests if r.stage == Stage.QA]
    assert "QA report: PASSED" in request.summary
    assert "QA FAILED" not in request.summary


def test_the_tool_results_are_recorded_in_the_trace(tmp_path: Path) -> None:
    run_qa(tmp_path, QaAgent(), FakeQa([check(), check("lint", CheckStatus.FAILED)]))

    events = [json.loads(line) for line in trace_path(tmp_path, "demo").read_text().splitlines()]
    recorded = [e["details"]["qa_checks"] for e in events if "qa_checks" in e.get("details", {})]
    assert recorded == [{"tests": "passed", "lint": "failed"}]


# --- failures go to the human, never back to the Developer ------------------------------------


def test_a_failed_tool_fails_the_report_even_though_the_agent_passed_everything(
    tmp_path: Path,
) -> None:
    gates = FakeGates()

    state = run_qa(tmp_path, QaAgent(), FakeQa([check(status=CheckStatus.FAILED)]), gates)

    assert not report_of(tmp_path, state).passed
    [request] = [r for r in gates.requests if r.stage == Stage.QA]
    assert "QA report: FAILED" in request.summary
    assert "QA FAILED" in request.summary
    assert state.qa_loops == 0  # no automatic trip back to the Developer


def test_a_failing_verdict_fails_the_report(tmp_path: Path) -> None:
    state = run_qa(tmp_path, QaAgent(Verdict.FAIL), FakeQa())

    assert not report_of(tmp_path, state).passed


def test_untested_criteria_are_shown_but_do_not_fail_the_report(tmp_path: Path) -> None:
    gates = FakeGates()

    state = run_qa(tmp_path, QaAgent(Verdict.UNTESTED), FakeQa(), gates)

    assert report_of(tmp_path, state).passed
    [request] = [r for r in gates.requests if r.stage == Stage.QA]
    assert "UNTESTED" in request.summary


def test_a_skipped_tool_fails_the_report(tmp_path: Path) -> None:
    state = run_qa(tmp_path, QaAgent(), FakeQa([check("secret scan", CheckStatus.SKIPPED)]))

    assert not report_of(tmp_path, state).passed


# --- running again ---------------------------------------------------------------------------


def test_asking_for_changes_runs_the_tools_again_on_the_current_project(tmp_path: Path) -> None:
    qa = FakeQa()
    gates = FakeGates(changes("I fixed the lint errors"))

    run_qa(tmp_path, QaAgent(), qa, gates)

    assert qa.runs == 2
    store = ArtifactStore(tmp_path, "demo")
    state = load_state(tmp_path, "demo")
    assert "run 2" in store.read(state.artifacts["qa_tests.md"])
    assert state.artifacts["qa_checks.json"].revision == 2


def test_the_agent_gets_the_humans_feedback_on_a_rerun(tmp_path: Path) -> None:
    agent = QaAgent()

    run_qa(tmp_path, agent, FakeQa(), FakeGates(changes("Look harder at T2")))

    assert agent.requests[1].comments == "Look harder at T2"
    assert agent.requests[1].previous_draft is not None


# --- waiting for merges ----------------------------------------------------------------------


def test_every_approved_task_pull_request_is_checked(tmp_path: Path) -> None:
    qa = FakeQa()

    run_qa(tmp_path, QaAgent(), qa)

    assert qa.asked[0] == ["https://github.com/me/p/pull/1", "https://github.com/me/p/pull/2"]


def test_unmerged_pull_requests_make_qa_wait_until_the_human_says_they_are_merged(
    tmp_path: Path,
) -> None:
    agent, qa, merges = QaAgent(), FakeQa(unmerged_calls=2), FakeMerges(True, True)

    state = run_qa(tmp_path, agent, qa, merges=merges)

    assert len(merges.shown) == 2  # asked twice: still unmerged after the first "yes"
    assert qa.runs == 1 and len(agent.requests) == 1
    assert state.stage == Stage.CAREER_COACH


def test_the_wait_is_saved_before_asking_so_a_crash_resumes_at_the_question(
    tmp_path: Path,
) -> None:
    class Crash(Exception):
        pass

    class CrashingMerges(FakeMerges):
        def wait_for_merge(self, pr_urls: list[str]) -> bool:
            raise Crash

    ctx = make_ctx(tmp_path, QaAgent(), FakeQa(unmerged_calls=1), merges=CrashingMerges())
    try:
        run_pipeline(at_qa(tmp_path), ctx)
    except Crash:
        pass

    saved = load_state(tmp_path, "demo")
    assert saved.status == Status.AWAITING_HUMAN
    assert saved.intervention is not None
    assert saved.intervention.kind == InterventionKind.MERGE_PENDING


def test_resuming_a_merge_wait_checks_again_and_carries_on(tmp_path: Path) -> None:
    agent, qa = QaAgent(), FakeQa()
    waiting = at_qa(tmp_path).evolve(
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.MERGE_PENDING, detail="Merge these"),
    )

    state = advance(waiting, make_ctx(tmp_path, agent, qa))

    assert qa.runs == 1 and len(agent.requests) == 1
    assert state.status != Status.HALTED


def test_giving_up_on_the_merge_halts_without_running_anything(tmp_path: Path) -> None:
    agent, qa = QaAgent(), FakeQa(unmerged_calls=1)

    state = run_qa(tmp_path, agent, qa, merges=FakeMerges(False))

    assert state.status == Status.HALTED
    assert qa.runs == 0 and agent.requests == []


def test_unmerged_pull_requests_without_a_way_to_ask_halt_the_run(tmp_path: Path) -> None:
    agent, qa = QaAgent(), FakeQa(unmerged_calls=1)

    state = run_qa(tmp_path, agent, qa)

    assert state.status == Status.HALTED
    assert agent.requests == []


def test_a_failure_preparing_qa_halts_without_calling_the_agent(tmp_path: Path) -> None:
    agent = QaAgent()

    state = run_qa(tmp_path, agent, FakeQa(error="could not fetch main from origin"))

    assert state.status == Status.HALTED
    assert agent.requests == []


def test_without_a_qa_preparer_the_stage_is_unchanged(tmp_path: Path) -> None:
    ctx = PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=_Plain(),
        gates=FakeGates(),
        caps=FakeCaps(),
    )
    state = at_qa(tmp_path)

    state = advance(state, ctx)

    assert state.stage == Stage.CAREER_COACH or state.status == Status.AWAITING_HUMAN


class _Plain:
    def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(output="qa text", cost_usd=0.1, passed=True)


def test_a_reject_at_the_qa_gate_reruns_qa(tmp_path: Path) -> None:
    gates = FakeGates(GateDecision(outcome=GateOutcome.REJECT, comments="redo"))
    qa = FakeQa()

    run_qa(tmp_path, QaAgent(), qa, gates)

    assert qa.runs == 2


def test_a_failing_report_goes_to_the_human_even_if_the_config_allows_qa_loops(
    tmp_path: Path,
) -> None:
    assert make_config().limits.max_qa_loops > 0
    gates = FakeGates()

    state = run_qa(tmp_path, QaAgent(), FakeQa([check(status=CheckStatus.FAILED)]), gates)

    assert state.stage != Stage.DEVELOPER
    assert [r.stage for r in gates.requests if r.stage == Stage.QA]
