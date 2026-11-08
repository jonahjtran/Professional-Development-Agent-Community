"""A Developer that cannot build a task as designed sends the project back to the Architect."""

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore
from orchestrator.caps import CapAction, CapResolution
from orchestrator.fix_types import FixRequest, FixResult
from orchestrator.pipeline import AgentRequest, AgentResult, PipelineContext, advance
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.todos import open_todos
from orchestrator.trace import trace_path
from schemas import DevResult, FixPlan, TaskList
from tests.factories import make_config
from tests.test_pipeline import FakeCaps, FakeGates
from tests.test_qa_fix_loop import fix_task
from tests.test_task_loop import FakePublisher, at_developer

PROBLEM = "The design stores events in SQLite, but two users need to write at once."


def replan_task(task_id: str = "R1-1", **over: Any) -> dict[str, Any]:
    return fix_task(task_id, **over)


def plan(*tasks: dict[str, Any], needs_human: list[str] | None = None) -> FixPlan:
    return FixPlan.model_validate(
        {
            "analysis": "SQLite cannot take concurrent writers.",
            "tasks": list(tasks),
            "needs_human": needs_human or [],
        }
    )


class FakePlanner:
    def __init__(self, *plans: FixPlan) -> None:
        self.plans = list(plans)
        self.requests: list[FixRequest] = []

    def plan(self, request: FixRequest) -> FixResult:
        self.requests.append(request)
        outcome = self.plans.pop(0) if self.plans else plan(replan_task())
        return FixResult(plan=outcome, cost_usd=0.2, model="architect-model")


class Dev:
    """Builds each task. `raises` maps a task id to the design problems it reports."""

    def __init__(self, raises: dict[str, list[str]] | None = None) -> None:
        self.raises = raises or {}
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
            needs_architect=self.raises.get(request.task_id, []),
        )
        return AgentResult(output=result.model_dump_json(), cost_usd=0.3, prompt_version="v1")


def ctx_for(
    tmp_path: Path,
    dev: Dev,
    planner: FakePlanner | None,
    *,
    gates: FakeGates | None = None,
    max_replans: int = 2,
    caps: FakeCaps | None = None,
) -> PipelineContext:
    config = make_config()
    config = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_replans": max_replans})}
    )
    return PipelineContext(
        config=config,
        runs_dir=tmp_path,
        runner=dev,
        gates=gates or FakeGates(),
        caps=caps or FakeCaps(),
        publisher=FakePublisher(),
        fixer=planner,
    )


def run_developer(tmp_path: Path, ctx: PipelineContext) -> ProjectState:
    """Run the Developer stage until it reaches QA (QA has no agent here)."""
    state = at_developer(tmp_path)
    for _ in range(80):
        state = advance(state, ctx)
        if state.stage == Stage.QA or state.status != Status.RUNNING:
            return state
    raise AssertionError("the Developer stage did not finish")


def tasks_of(tmp_path: Path, state: ProjectState) -> list[str]:
    store = ArtifactStore(tmp_path, "demo")
    return [
        t.id for t in TaskList.model_validate_json(store.read(state.artifacts["tasks.json"])).tasks
    ]


def events(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace_path(tmp_path, "demo").read_text().splitlines()]


# --- the escalation ---------------------------------------------------------------------------


def test_a_design_problem_sends_the_project_back_to_the_architect_then_on_to_the_developer(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner)

    state = run_developer(tmp_path, ctx)

    assert [v.stage for v in state.path][:2] == [Stage.ARCHITECT, Stage.DEVELOPER]
    back = state.path[0]
    assert back.came_from == Stage.DEVELOPER and back.stage == Stage.ARCHITECT
    assert "T1" in back.reason and "design" in back.reason.lower()


def test_the_architect_is_given_the_problem_the_finished_tasks_and_the_architecture(
    tmp_path: Path,
) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner)

    run_developer(tmp_path, ctx)

    request = planner.requests[0]
    assert request.kind == "design_problem" and request.round == 1
    assert PROBLEM in request.problem and "T1" in request.problem
    assert "Architecture" in request.architecture and "T2" in request.tasks_json


def test_the_new_tasks_are_added_built_and_the_original_tasks_still_finish(
    tmp_path: Path,
) -> None:
    dev = Dev({"T1": [PROBLEM]})
    ctx = ctx_for(tmp_path, dev, FakePlanner(plan(replan_task("R1-1"))))

    state = run_developer(tmp_path, ctx)

    assert tasks_of(tmp_path, state) == ["T1", "T2", "R1-1"]
    assert [r.task_id for r in dev.requests] == ["T1", "T2", "R1-1"]
    assert state.completed_tasks == ["T1", "T2", "R1-1"]
    assert "replan_1.md" in state.artifacts


def test_the_architects_cost_and_trace_are_recorded_as_its_own_work(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), FakePlanner())

    state = run_developer(tmp_path, ctx)

    assert state.cost_usd[Stage.ARCHITECT] == pytest.approx(0.2)
    ends = [e for e in events(tmp_path) if e["event"] == "agent_end" and e["stage"] == "architect"]
    assert ends and ends[0]["model"] == "architect-model"
    assert ends[0]["details"]["purpose"] == "design_review"


def test_the_gate_shows_the_problem_the_developer_raised(tmp_path: Path) -> None:
    gates = FakeGates()
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), FakePlanner(), gates=gates)

    run_developer(tmp_path, ctx)

    first = gates.requests[0].summary
    assert "Needs the Architect" in first and PROBLEM in first


def test_a_task_with_no_problem_does_not_visit_the_architect(tmp_path: Path) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, Dev(), planner)

    state = run_developer(tmp_path, ctx)

    assert planner.requests == []
    assert Stage.ARCHITECT not in [v.stage for v in state.path]


def test_the_visit_waits_until_the_task_is_approved(tmp_path: Path) -> None:
    from orchestrator.gates import GateDecision, GateOutcome

    planner = FakePlanner()
    gates = FakeGates(GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="Tighten it"))
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner, gates=gates)
    state = at_developer(tmp_path)

    state = advance(state, ctx)  # T1 built, the human asks for changes

    assert planner.requests == [] and state.stage == Stage.DEVELOPER


# --- when it does not escalate ----------------------------------------------------------------


def test_with_no_architect_wired_in_the_problem_goes_to_the_human_as_an_action_item(
    tmp_path: Path,
) -> None:
    dev = Dev({"T1": [PROBLEM]})
    ctx = ctx_for(tmp_path, dev, None)

    state = run_developer(tmp_path, ctx)

    assert [t.title for t in open_todos(tmp_path, "demo")] == [PROBLEM]
    assert state.completed_tasks == ["T1", "T2"] and Stage.ARCHITECT not in [
        v.stage for v in state.path
    ]


def test_an_architect_that_finds_nothing_to_change_hands_the_problem_to_the_human(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(plan(needs_human=["Decide whether SQLite is acceptable"]))
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner)

    state = run_developer(tmp_path, ctx)

    titles = [t.title for t in open_todos(tmp_path, "demo")]
    assert "Decide whether SQLite is acceptable" in titles
    assert state.completed_tasks == ["T1", "T2"]  # the Developer carried on
    assert [v.stage for v in state.path][:2] == [Stage.ARCHITECT, Stage.DEVELOPER]


def test_an_architect_that_says_the_design_already_covers_it_answers_the_developer_not_you(
    tmp_path: Path,
) -> None:
    from orchestrator.notes import pending_notes

    refuted = FixPlan(analysis="The Developer is mistaken: app.py already covers configuration.")
    dev = Dev({"T1": [PROBLEM]})
    ctx = ctx_for(tmp_path, dev, FakePlanner(refuted))

    state = run_developer(tmp_path, ctx)

    assert open_todos(tmp_path, "demo") == []  # no alert for a problem that was answered
    assert state.completed_tasks == ["T1", "T2"] and state.stage == Stage.QA
    # the answer reached the Developer's next task as a note
    texts = [n.text for n in pending_notes(tmp_path, "demo")]
    assert any("mistaken" in t and "no design change" in t.lower() for t in texts)


def test_after_the_allowed_visits_a_problem_goes_to_the_human_instead(tmp_path: Path) -> None:
    planner = FakePlanner(plan(replan_task("R1-1")))
    dev = Dev({"T1": [PROBLEM], "R1-1": ["Another design problem"]})
    ctx = ctx_for(tmp_path, dev, planner, max_replans=1)

    state = run_developer(tmp_path, ctx)

    assert len(planner.requests) == 1
    assert "Another design problem" in [t.title for t in open_todos(tmp_path, "demo")]
    assert state.stage == Stage.QA


def test_with_zero_visits_allowed_the_architect_is_never_called(tmp_path: Path) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner, max_replans=0)

    run_developer(tmp_path, ctx)

    assert planner.requests == []
    assert [t.title for t in open_todos(tmp_path, "demo")] == [PROBLEM]


def test_an_invalid_plan_changes_nothing_and_the_developer_carries_on(tmp_path: Path) -> None:
    planner = FakePlanner(plan(replan_task("T1")))  # reuses an existing id
    dev = Dev({"T1": [PROBLEM]})
    ctx = ctx_for(tmp_path, dev, planner)

    state = run_developer(tmp_path, ctx)

    assert tasks_of(tmp_path, state) == ["T1", "T2"] and state.completed_tasks == ["T1", "T2"]
    assert PROBLEM in [t.title for t in open_todos(tmp_path, "demo")]


def test_an_exceeded_architect_budget_asks_the_human_first(tmp_path: Path) -> None:
    planner = FakePlanner()
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    ctx = ctx_for(tmp_path, Dev({"T1": [PROBLEM]}), planner, caps=caps)
    state = at_developer(tmp_path).evolve(cost_usd={Stage.ARCHITECT: 5.0})

    for _ in range(20):
        state = advance(state, ctx)
        if state.status != Status.RUNNING:
            break

    assert planner.requests == [] and state.status == Status.HALTED
    assert caps.requests and caps.requests[0].stage == Stage.ARCHITECT


# --- a Developer that cannot start a task and made no commits ----------------------------------


class StuckDev(Dev):
    """Cannot build the task ids in `stuck` (no commits, a design problem); builds the rest."""

    def __init__(self, *stuck: str) -> None:
        super().__init__()
        self.stuck = set(stuck)

    def run(self, request: AgentRequest) -> AgentResult:
        from orchestrator.pipeline import NeedsArchitectError

        if request.task_id in self.stuck:
            self.requests.append(request)
            raise NeedsArchitectError(
                "the Developer made no commits (it said: no specific change)",
                problems=["The task names no specific change"],
                cost_usd=0.07,
            )
        return super().run(request)


def test_a_task_the_developer_cannot_start_goes_back_to_the_architect(tmp_path: Path) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, StuckDev("T1"), planner)
    state = at_developer(tmp_path)

    state = advance(state, ctx)

    assert state.stage == Stage.ARCHITECT and state.status == Status.RUNNING
    assert state.handoff is not None and state.handoff.purpose == "design_review"
    assert "T1" in state.handoff.note and "names no specific change" in state.handoff.note
    assert state.cost_usd[Stage.DEVELOPER] == pytest.approx(0.07)  # the failed call is billed
    assert "design problem raised on T1" in state.path[-1].reason


def test_the_architect_is_told_what_the_stuck_task_was_meant_to_do(tmp_path: Path) -> None:
    planner = FakePlanner()
    ctx = ctx_for(tmp_path, StuckDev("T1"), planner)

    state = advance(at_developer(tmp_path), ctx)
    advance(state, ctx)

    sent = planner.requests[0].problem
    assert "T1" in sent and "Create the table" in sent  # its title and text reach the Architect
    assert "Add the table and a migration." in sent


def test_a_task_you_added_by_sending_the_project_back_is_replaced_by_the_architects_tasks(
    tmp_path: Path,
) -> None:
    from orchestrator.rework import send_back

    state = at_developer(tmp_path).evolve(status=Status.HALTED)
    state = send_back(tmp_path, state, Stage.DEVELOPER, "Make everything pass the PRD")
    assert "H1-1" in tasks_of(tmp_path, state)
    planner = FakePlanner(plan(replan_task("R1-1")))
    ctx = ctx_for(tmp_path, StuckDev("H1-1"), planner)

    for _ in range(40):
        state = advance(state, ctx)
        if state.stage == Stage.QA or state.status != Status.RUNNING:
            break

    ids = tasks_of(tmp_path, state)
    assert "H1-1" not in ids and "R1-1" in ids
    assert state.current_task_id is None or state.current_task_id != "H1-1"
    assert "Make everything pass the PRD" in planner.requests[0].problem


def test_a_stuck_task_from_the_architects_own_plan_is_kept(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path, StuckDev("T1"), FakePlanner())

    state = advance(at_developer(tmp_path), ctx)

    assert "T1" in tasks_of(tmp_path, state)


def test_when_the_replans_are_used_up_a_stuck_task_pauses_with_what_it_said(
    tmp_path: Path,
) -> None:
    ctx = ctx_for(tmp_path, StuckDev("T1"), FakePlanner(), max_replans=0)

    state = advance(at_developer(tmp_path), ctx)

    assert state.status == Status.HALTED and state.stage == Stage.DEVELOPER
    assert any(
        "no commits" in t.title or "no commits" in t.detail for t in open_todos(tmp_path, "demo")
    )


def test_without_an_architect_available_a_stuck_task_pauses(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path, StuckDev("T1"), None)

    state = advance(at_developer(tmp_path), ctx)

    assert state.status == Status.HALTED
