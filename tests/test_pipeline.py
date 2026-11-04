from pathlib import Path

import pytest

from orchestrator.caps import CapAction, CapRequest, CapResolution
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import (
    AgentError,
    AgentRequest,
    AgentResult,
    PipelineContext,
    advance,
    run_pipeline,
)
from orchestrator.state import (
    GateOutcome as Outcome,
)
from orchestrator.state import (
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    load_state,
)
from orchestrator.trace import trace_path
from tests.factories import make_config, new_state


class FakeAgent:
    """Records every call. `qa_results` is the pass/fail answer for each QA call, in order."""

    def __init__(self, cost: float = 0.1, qa_results: list[bool] | None = None) -> None:
        self.cost = cost
        self.qa_results = list(qa_results or [])
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        n = len(self.requests)
        passed = None
        if request.stage == Stage.QA:
            passed = self.qa_results.pop(0) if self.qa_results else True
        return AgentResult(
            output=f"{request.stage.value} draft #{n}", cost_usd=self.cost, passed=passed
        )

    @property
    def stages(self) -> list[Stage]:
        return [r.stage for r in self.requests]


class FakeGates:
    """A scripted human at gates. Approves when the script runs out."""

    def __init__(self, *decisions: GateDecision) -> None:
        self.decisions = list(decisions)
        self.requests: list[GateRequest] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        if self.decisions:
            return self.decisions.pop(0)
        return GateDecision(outcome=GateOutcome.APPROVE)


class FakeCaps:
    def __init__(self, *resolutions: CapResolution) -> None:
        self.resolutions = list(resolutions)
        self.requests: list[CapRequest] = []

    def resolve(self, request: CapRequest) -> CapResolution:
        self.requests.append(request)
        return self.resolutions.pop(0)


def approve() -> GateDecision:
    return GateDecision(outcome=GateOutcome.APPROVE)


def changes(text: str = "Tighten it") -> GateDecision:
    return GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments=text)


def reject(halt: bool = False) -> GateDecision:
    return GateDecision(outcome=GateOutcome.REJECT, comments="Wrong", halt=halt)


def make_ctx(
    tmp_path: Path,
    agent: FakeAgent | None = None,
    gates: FakeGates | None = None,
    caps: FakeCaps | None = None,
) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent or FakeAgent(),
        gates=gates or FakeGates(),
        caps=caps or FakeCaps(),
    )


def start(**changes_: object) -> ProjectState:
    return new_state().evolve(**changes_)


# --- the happy path --------------------------------------------------------------------------


def test_a_fully_approved_run_visits_every_stage_in_order_and_finishes(tmp_path: Path) -> None:
    agent = FakeAgent()
    ctx = make_ctx(tmp_path, agent)

    state = run_pipeline(start(), ctx)

    assert state.status == Status.DONE
    assert agent.stages == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.CAREER_COACH,
    ]


def test_gates_follow_the_product_owner_architect_qa_and_career_coach(tmp_path: Path) -> None:
    gates = FakeGates()

    state = run_pipeline(start(), make_ctx(tmp_path, gates=gates))

    assert [r.stage for r in state.gate_history] == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.QA,
        Stage.CAREER_COACH,
    ]
    assert all(r.outcome == Outcome.APPROVE for r in state.gate_history)


def test_the_cost_of_each_agent_call_is_recorded(tmp_path: Path) -> None:
    state = run_pipeline(start(), make_ctx(tmp_path, FakeAgent(cost=0.2)))

    assert state.cost_usd[Stage.PRODUCT_OWNER] == pytest.approx(0.2)
    assert state.total_cost_usd == pytest.approx(1.0)


def test_the_human_is_shown_the_draft_at_the_gate(tmp_path: Path) -> None:
    gates = FakeGates()

    run_pipeline(start(), make_ctx(tmp_path, gates=gates))

    assert gates.requests[0].stage == Stage.PRODUCT_OWNER
    assert "product_owner draft #1" in gates.requests[0].summary
    assert gates.requests[0].cost_usd == pytest.approx(0.1)


def test_state_is_saved_to_disk_and_matches_the_final_state(tmp_path: Path) -> None:
    state = run_pipeline(start(), make_ctx(tmp_path))

    assert load_state(tmp_path, "demo") == state


def test_advance_makes_one_step_and_saves(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)

    state = advance(start(), ctx)  # INTAKE -> PRODUCT_OWNER, no agent yet

    assert state.stage == Stage.PRODUCT_OWNER
    assert state.status == Status.RUNNING
    assert load_state(tmp_path, "demo") == state


def test_a_finished_project_cannot_advance(tmp_path: Path) -> None:
    done = start(status=Status.DONE)

    with pytest.raises(ValueError, match="finished"):
        advance(done, make_ctx(tmp_path))


# --- request changes and reject --------------------------------------------------------------


def test_requesting_changes_reruns_the_agent_with_the_draft_and_the_comments(
    tmp_path: Path,
) -> None:
    agent = FakeAgent()
    gates = FakeGates(changes("Add non-goals"))

    state = run_pipeline(start(), make_ctx(tmp_path, agent, gates))

    assert agent.stages[:2] == [Stage.PRODUCT_OWNER, Stage.PRODUCT_OWNER]
    second = agent.requests[1]
    assert second.comments == "Add non-goals"
    assert second.previous_draft == "product_owner draft #1"
    assert second.revision == 1
    assert state.status == Status.DONE


def test_the_first_run_of_a_stage_has_no_previous_draft_or_comments(tmp_path: Path) -> None:
    agent = FakeAgent()

    run_pipeline(start(), make_ctx(tmp_path, agent))

    assert agent.requests[0].previous_draft is None
    assert agent.requests[0].comments is None
    assert agent.requests[0].idea == "A tool that tracks student org events."


def test_a_rejection_restarts_the_stage_from_scratch(tmp_path: Path) -> None:
    agent = FakeAgent()
    gates = FakeGates(reject())

    run_pipeline(start(), make_ctx(tmp_path, agent, gates))

    assert agent.stages[:2] == [Stage.PRODUCT_OWNER, Stage.PRODUCT_OWNER]
    assert agent.requests[1].previous_draft is None


def test_a_rejection_with_halt_stops_the_project(tmp_path: Path) -> None:
    agent = FakeAgent()
    gates = FakeGates(reject(halt=True))

    state = run_pipeline(start(), make_ctx(tmp_path, agent, gates))

    assert state.status == Status.HALTED
    assert agent.stages == [Stage.PRODUCT_OWNER]


# --- revision cap ----------------------------------------------------------------------------


def over_the_revision_cap() -> tuple[FakeGates, ProjectState]:
    gates = FakeGates(changes("again"))
    return gates, start(stage=Stage.PRODUCT_OWNER, revisions={Stage.PRODUCT_OWNER: 3})


def test_hitting_the_revision_cap_asks_the_human_and_abandon_halts(tmp_path: Path) -> None:
    gates, state = over_the_revision_cap()
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))

    result = run_pipeline(state, make_ctx(tmp_path, gates=gates, caps=caps))

    assert result.status == Status.HALTED
    assert caps.requests[0].kind == InterventionKind.REVISION_CAP


def test_approving_anyway_at_the_revision_cap_moves_on(tmp_path: Path) -> None:
    gates, state = over_the_revision_cap()
    caps = FakeCaps(CapResolution(action=CapAction.APPROVE_ANYWAY))
    agent = FakeAgent()

    result = run_pipeline(state, make_ctx(tmp_path, agent, gates, caps))

    assert result.status == Status.DONE
    assert agent.stages.count(Stage.PRODUCT_OWNER) == 1


def test_raising_the_revision_cap_runs_the_pending_revision(tmp_path: Path) -> None:
    gates, state = over_the_revision_cap()
    caps = FakeCaps(CapResolution(action=CapAction.RAISE, amount=1))
    agent = FakeAgent()

    result = run_pipeline(state, make_ctx(tmp_path, agent, gates, caps))

    assert agent.stages.count(Stage.PRODUCT_OWNER) == 2
    assert agent.requests[1].comments == "again"
    assert result.status == Status.DONE


# --- budget ----------------------------------------------------------------------------------


def test_an_exceeded_budget_stops_before_the_agent_is_called(tmp_path: Path) -> None:
    agent = FakeAgent()
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))
    state = start(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: 1.5})

    result = run_pipeline(state, make_ctx(tmp_path, agent, caps=caps))

    assert result.status == Status.HALTED
    assert agent.requests == []
    assert caps.requests[0].kind == InterventionKind.BUDGET_CAP


def test_raising_the_cap_lets_the_agent_run(tmp_path: Path) -> None:
    agent = FakeAgent(cost=0.0)
    caps = FakeCaps(CapResolution(action=CapAction.RAISE, amount=1.0))
    state = start(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: 1.5})

    result = run_pipeline(state, make_ctx(tmp_path, agent, caps=caps))

    assert result.status == Status.DONE
    assert agent.stages[0] == Stage.PRODUCT_OWNER


def test_a_raise_that_is_too_small_asks_again(tmp_path: Path) -> None:
    caps = FakeCaps(
        CapResolution(action=CapAction.RAISE, amount=0.5),
        CapResolution(action=CapAction.ABANDON),
    )
    state = start(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: 2.0})

    result = run_pipeline(state, make_ctx(tmp_path, caps=caps))

    assert len(caps.requests) == 2
    assert result.status == Status.HALTED


def test_continuing_at_a_warning_runs_the_agent_without_asking_again(tmp_path: Path) -> None:
    agent = FakeAgent(cost=0.0)
    caps = FakeCaps(CapResolution(action=CapAction.APPROVE_ANYWAY))
    state = start(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: 1.2})

    run_pipeline(state, make_ctx(tmp_path, agent, caps=caps))

    # One question for the product owner. The warning does not repeat for later stages.
    assert len(caps.requests) == 1
    assert agent.stages[0] == Stage.PRODUCT_OWNER


def test_the_warning_comes_back_when_the_work_returns_to_a_stage(tmp_path: Path) -> None:
    # The Developer is at 85% of its cap; QA fails once, so the Developer runs twice.
    agent = FakeAgent(cost=0.0, qa_results=[False, True])
    caps = FakeCaps(
        CapResolution(action=CapAction.APPROVE_ANYWAY),
        CapResolution(action=CapAction.APPROVE_ANYWAY),
    )
    state = start(stage=Stage.DEVELOPER, cost_usd={Stage.DEVELOPER: 8.5})

    run_pipeline(state, make_ctx(tmp_path, agent, caps=caps))

    assert agent.stages.count(Stage.DEVELOPER) == 2
    assert len(caps.requests) == 2


# --- the QA loop -----------------------------------------------------------------------------


def test_a_failing_qa_sends_the_work_back_to_the_developer_with_the_report(
    tmp_path: Path,
) -> None:
    agent = FakeAgent(qa_results=[False, True])

    state = run_pipeline(start(), make_ctx(tmp_path, agent))

    assert agent.stages[2:6] == [Stage.DEVELOPER, Stage.QA, Stage.DEVELOPER, Stage.QA]
    second_dev = agent.requests[4]
    assert second_dev.comments is not None and "qa draft" in second_dev.comments
    assert state.status == Status.DONE
    assert state.qa_loops == 0


def test_the_qa_loop_stops_after_the_configured_maximum_and_goes_to_the_human(
    tmp_path: Path,
) -> None:
    agent = FakeAgent(qa_results=[False, False, False])
    gates = FakeGates()

    run_pipeline(start(), make_ctx(tmp_path, agent, gates))

    assert agent.stages.count(Stage.DEVELOPER) == 3  # first try + 2 loops
    qa_gate = next(r for r in gates.requests if r.stage == Stage.QA)
    assert "failed" in qa_gate.summary.lower()


# --- pausing and resuming --------------------------------------------------------------------


class Crash(BaseException):
    """Stands in for Ctrl-C or a killed process."""


class CrashingGates(FakeGates):
    def decide(self, request: GateRequest) -> GateDecision:
        raise Crash


def test_a_crash_at_a_gate_leaves_the_saved_state_waiting_at_that_gate(tmp_path: Path) -> None:
    with pytest.raises(Crash):
        run_pipeline(start(), make_ctx(tmp_path, gates=CrashingGates()))

    saved = load_state(tmp_path, "demo")
    assert saved.status == Status.AWAITING_HUMAN
    assert saved.intervention is not None
    assert saved.intervention.kind == InterventionKind.GATE
    assert saved.stage == Stage.PRODUCT_OWNER


def test_resuming_at_a_gate_asks_again_without_rerunning_the_agent(tmp_path: Path) -> None:
    with pytest.raises(Crash):
        run_pipeline(start(), make_ctx(tmp_path, gates=CrashingGates()))
    agent = FakeAgent()
    gates = FakeGates()

    state = run_pipeline(load_state(tmp_path, "demo"), make_ctx(tmp_path, agent, gates))

    assert state.status == Status.DONE
    assert agent.stages.count(Stage.PRODUCT_OWNER) == 0
    assert "product_owner draft #1" in gates.requests[0].summary


def test_resuming_at_a_revision_cap_pause_asks_the_cap_question(tmp_path: Path) -> None:
    waiting = start(
        stage=Stage.PRODUCT_OWNER,
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.REVISION_CAP, "detail": "used all 3 revisions"},
    )
    caps = FakeCaps(CapResolution(action=CapAction.ABANDON))

    result = run_pipeline(waiting, make_ctx(tmp_path, caps=caps))

    assert result.status == Status.HALTED


def test_resuming_at_a_budget_warning_runs_the_agent_after_continue_without_asking_twice(
    tmp_path: Path,
) -> None:
    waiting = start(
        stage=Stage.PRODUCT_OWNER,
        cost_usd={Stage.PRODUCT_OWNER: 1.2},
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.BUDGET_CAP, "detail": "at 80%"},
    )
    caps = FakeCaps(CapResolution(action=CapAction.APPROVE_ANYWAY))
    agent = FakeAgent(cost=0.0)

    run_pipeline(waiting, make_ctx(tmp_path, agent, caps=caps))

    assert len(caps.requests) == 1
    assert agent.stages[0] == Stage.PRODUCT_OWNER


def test_a_pause_kind_the_pipeline_cannot_answer_yet_is_an_error(tmp_path: Path) -> None:
    waiting = start(
        status=Status.AWAITING_HUMAN,
        intervention={"kind": InterventionKind.TOOL_PERMISSION, "detail": "rm -rf"},
    )

    with pytest.raises(ValueError, match="tool_permission"):
        advance(waiting, make_ctx(tmp_path))


# --- the trace -------------------------------------------------------------------------------


def trace_events(tmp_path: Path) -> list[str]:
    import json

    lines = trace_path(tmp_path, "demo").read_text().splitlines()
    return [json.loads(line)["event"] for line in lines]


def test_the_run_is_traced_with_transitions_agent_calls_and_gates(tmp_path: Path) -> None:
    run_pipeline(start(), make_ctx(tmp_path))

    events = trace_events(tmp_path)
    assert events.count("agent_start") == 5
    assert events.count("agent_end") == 5
    assert events.count("gate") == 4
    assert events.count("transition") >= 5


# --- agent failures and token counts ---------------------------------------------------------


class FailingAgent(FakeAgent):
    def run(self, request: AgentRequest) -> AgentResult:
        raise AgentError("error_max_turns", cost_usd=0.7)


def test_an_agent_failure_halts_the_project_and_keeps_the_cost(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, FailingAgent())

    state = run_pipeline(start(), ctx)

    assert state.status == Status.HALTED
    assert state.cost_usd[Stage.PRODUCT_OWNER] == pytest.approx(0.7)
    assert load_state(tmp_path, "demo") == state


def test_an_agent_failure_is_traced(tmp_path: Path) -> None:
    import json

    run_pipeline(start(), make_ctx(tmp_path, FailingAgent()))

    lines = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    end = next(e for e in lines if e["event"] == "agent_end")
    assert "error_max_turns" in end["details"]["error"]
    assert end["cost_usd"] == pytest.approx(0.7)


def test_token_counts_reach_the_trace(tmp_path: Path) -> None:
    import json

    class Counting(FakeAgent):
        def run(self, request: AgentRequest) -> AgentResult:
            return AgentResult(output="x", cost_usd=0.1, tokens_in=11, tokens_out=7)

    run_pipeline(start(), make_ctx(tmp_path, Counting()))

    lines = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    end = next(e for e in lines if e["event"] == "agent_end")
    assert (end["tokens_in"], end["tokens_out"]) == (11, 7)
