"""A project you sent back works through your change and finishes again."""

from dataclasses import replace
from pathlib import Path

from orchestrator.artifacts import ArtifactStore
from orchestrator.pipeline import PipelineContext, advance, run_pipeline
from orchestrator.rework import send_back
from orchestrator.state import Intervention, InterventionKind, ProjectState, Stage, Status
from orchestrator.todos import open_todos
from schemas import FixPlan, TaskList
from tests.test_design_escalation import Dev, FakePlanner, ctx_for, plan, replan_task
from tests.test_pipeline import FakeAgent, FakeGates, make_ctx, start
from tests.test_task_loop import at_developer

# --- the whole pipeline, with the simple agents -----------------------------------------------


def test_a_finished_project_sent_back_to_the_product_owner_revises_the_prd_and_finishes_again(
    tmp_path: Path,
) -> None:
    agent = FakeAgent()
    ctx = make_ctx(tmp_path, agent)
    done = run_pipeline(start(), ctx)
    assert done.status == Status.DONE
    first_prd_calls = agent.stages.count(Stage.PRODUCT_OWNER)

    sent = send_back(tmp_path, done, Stage.PRODUCT_OWNER, "The PRD missed offline mode")
    again = run_pipeline(sent, ctx)

    assert again.status == Status.DONE and again.rework is None  # finished: the rework is over
    po_requests = [r for r in agent.requests if r.stage == Stage.PRODUCT_OWNER]
    assert len(po_requests) == first_prd_calls + 1
    revised = po_requests[-1]
    assert revised.previous_draft is not None  # it sees the PRD it wrote
    assert revised.comments == "The PRD missed offline mode"  # and your instruction
    assert agent.stages[-5:] == [  # then the agents after it run again, as usual
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.CAREER_COACH,
    ]


def test_the_journey_shows_the_move_back_you_asked_for(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FakeAgent())
    done = run_pipeline(start(), ctx)

    sent = send_back(tmp_path, done, Stage.ARCHITECT, "Rethink the schema")
    again = run_pipeline(sent, ctx)

    back = next(v for v in again.path if "you sent it back" in v.reason)
    assert back.stage == Stage.ARCHITECT and back.came_from == Stage.CAREER_COACH


def test_the_career_coach_sent_back_revises_its_material_with_your_comment(
    tmp_path: Path,
) -> None:
    agent = FakeAgent()
    ctx = make_ctx(tmp_path, agent)
    done = run_pipeline(start(), ctx)

    sent = send_back(tmp_path, done, Stage.CAREER_COACH, "Mention the QA fix loop")
    run_pipeline(sent, ctx)

    coach = [r for r in agent.requests if r.stage == Stage.CAREER_COACH][-1]
    assert coach.comments == "Mention the QA fix loop" and coach.previous_draft is not None


def test_a_second_request_for_changes_is_not_overridden_by_your_first_instruction(
    tmp_path: Path,
) -> None:
    from orchestrator.gates import GateDecision, GateOutcome

    agent = FakeAgent()
    gates = FakeGates(
        GateDecision(outcome=GateOutcome.APPROVE),  # PO
        GateDecision(outcome=GateOutcome.APPROVE),  # Architect
        GateDecision(outcome=GateOutcome.APPROVE),  # QA
        GateDecision(outcome=GateOutcome.APPROVE),  # coach
    )
    ctx = make_ctx(tmp_path, agent, gates)
    done = run_pipeline(start(), ctx)
    sent = send_back(tmp_path, done, Stage.PRODUCT_OWNER, "First instruction")
    ctx = replace(
        ctx,
        gates=FakeGates(
            GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="Second thoughts")
        ),
    )

    run_pipeline(sent, ctx)

    comments = [r.comments for r in agent.requests if r.stage == Stage.PRODUCT_OWNER]
    assert comments[-2:] == ["First instruction", "Second thoughts"]


# --- the full project: new tasks only ---------------------------------------------------------


def full_ctx(
    tmp_path: Path, dev: Dev, planner: FakePlanner, gates: FakeGates | None = None
) -> PipelineContext:
    return ctx_for(tmp_path, dev, planner, gates=gates)


def run_to_qa(tmp_path: Path, state: ProjectState, ctx: PipelineContext) -> ProjectState:
    for _ in range(80):
        state = advance(state, ctx)
        if state.stage == Stage.QA or state.status != Status.RUNNING:
            return state
    raise AssertionError("did not reach QA")


def test_sent_back_to_the_architect_only_the_new_tasks_are_planned_and_built(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(plan(replan_task("R1-1")))
    dev = Dev()
    ctx = full_ctx(tmp_path, dev, planner)
    done = at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, completed_tasks=["T1", "T2"]
    )

    sent = send_back(tmp_path, done, Stage.ARCHITECT, "Store events in Postgres")
    state = run_to_qa(tmp_path, sent, ctx)

    request = planner.requests[0]
    assert "Store events in Postgres" in request.problem
    assert [r.task_id for r in dev.requests] == ["R1-1"]  # T1 and T2 are not rebuilt
    assert state.completed_tasks == ["T1", "T2", "R1-1"] and state.stage == Stage.QA


def test_a_second_change_to_the_architect_gets_its_own_round_and_ids(tmp_path: Path) -> None:
    planner = FakePlanner(plan(replan_task("R1-1")), plan(replan_task("R2-1")))
    dev = Dev()
    ctx = full_ctx(tmp_path, dev, planner)
    done = at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, completed_tasks=["T1", "T2"]
    )

    first = run_to_qa(tmp_path, send_back(tmp_path, done, Stage.ARCHITECT, "Change one"), ctx)
    second = run_to_qa(
        tmp_path,
        send_back(
            tmp_path,
            first.evolve(status=Status.DONE, stage=Stage.CAREER_COACH),
            Stage.ARCHITECT,
            "Change two",
        ),
        ctx,
    )

    assert [r.round for r in planner.requests] == [1, 2]
    store = ArtifactStore(tmp_path, "demo")
    ids = [
        t.id for t in TaskList.model_validate_json(store.read(second.artifacts["tasks.json"])).tasks
    ]
    assert ids == ["T1", "T2", "R1-1", "R2-1"]


def test_an_architect_that_finds_nothing_to_change_for_your_request_tells_you(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(FixPlan(analysis="The design already supports that."))
    ctx = full_ctx(tmp_path, Dev(), planner)
    done = at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, completed_tasks=["T1", "T2"]
    )

    run_to_qa(tmp_path, send_back(tmp_path, done, Stage.ARCHITECT, "Support offline"), ctx)

    todos = open_todos(tmp_path, "demo")
    assert any("already supports that" in t.detail for t in todos)


def test_sent_back_to_the_developer_it_builds_your_instruction_as_a_task(tmp_path: Path) -> None:
    dev = Dev()
    ctx = full_ctx(tmp_path, dev, FakePlanner())
    done = at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, completed_tasks=["T1", "T2"]
    )

    sent = send_back(tmp_path, done, Stage.DEVELOPER, "Add a dark mode toggle")
    state = run_to_qa(tmp_path, sent, ctx)

    assert [r.task_id for r in dev.requests] == ["H1-1"]
    assert state.completed_tasks == ["T1", "T2", "H1-1"]


def test_a_changed_prd_sends_the_architect_to_plan_only_what_the_change_needs(
    tmp_path: Path,
) -> None:
    planner = FakePlanner(plan(replan_task("R1-1")))
    dev = Dev()
    gates = FakeGates()
    ctx = full_ctx(tmp_path, dev, planner, gates)
    done = at_developer(tmp_path).evolve(
        stage=Stage.CAREER_COACH, status=Status.DONE, completed_tasks=["T1", "T2"]
    )
    sent = send_back(tmp_path, done, Stage.PRODUCT_OWNER, "Add offline mode")
    # the Product Owner has revised the PRD and its gate is open
    waiting = sent.evolve(
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(
            kind=InterventionKind.GATE, detail="Review the product_owner output"
        ),
    )

    state = run_to_qa(tmp_path, waiting, ctx)

    assert "Add offline mode" in planner.requests[0].problem
    assert "requirements changed" in planner.requests[0].problem.lower()
    assert [r.task_id for r in dev.requests] == ["R1-1"] and state.stage == Stage.QA
    assert state.rework is not None and state.rework.origin_done is True


def test_finishing_again_ends_the_rework(tmp_path: Path) -> None:
    agent = FakeAgent()
    ctx = make_ctx(tmp_path, agent)
    done = run_pipeline(start(), ctx)

    again = run_pipeline(send_back(tmp_path, done, Stage.QA, "Check again"), ctx)

    assert again.status == Status.DONE and again.rework is None
