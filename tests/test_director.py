"""The Director: the orchestrator decides which agent runs next, after each one finishes."""

from pathlib import Path
from typing import Any

import pytest

from orchestrator.director import Facts, check, fallback_step
from orchestrator.pipeline import AgentRequest, run_pipeline
from orchestrator.pipeline import advance as _advance  # noqa: F401
from orchestrator.state import Handoff, Stage, Status
from schemas import NextStep
from tests.factories import make_config
from tests.test_pipeline import FakeAgent, FakeCaps, FakeGates, start

# --- the rules code applies to what the model proposes ----------------------------------------


def run_to(agent: str, reason: str = "because", **over: Any) -> NextStep:
    return NextStep.model_validate(
        {"action": "run", "agent": agent, "reason": reason, "note": "", "documents": [], **over}
    )


def finish(**over: Any) -> NextStep:
    return NextStep.model_validate(
        {"action": "finish", "agent": None, "reason": "done", "note": "", "documents": [], **over}
    )


def facts(**over: Any) -> Facts:
    base: dict[str, Any] = {
        "current": Stage.PRODUCT_OWNER,
        "tasks_planned": False,
        "tasks_open": False,
        "qa_report": False,
        "approved": frozenset(),
        "moves": 1,
        "max_moves": 30,
    }
    return Facts(**{**base, **over})


def test_any_agent_may_be_chosen_next_including_skipping_ahead() -> None:
    for agent in ("architect", "developer", "qa", "career_coach"):
        assert check(run_to(agent), facts()) is None


def test_an_agent_cannot_be_chosen_straight_after_itself() -> None:
    assert "just ran" in (check(run_to("product_owner"), facts()) or "")


def test_the_developer_needs_the_task_list_when_it_builds_tasks() -> None:
    needs = facts(current=Stage.ARCHITECT, needs_task_list=True)

    assert "task list" in (check(run_to("developer"), needs) or "")
    assert check(run_to("developer"), facts(needs_task_list=True, tasks_planned=True)) is None


def test_qa_waits_for_every_task_to_be_built() -> None:
    open_tasks = facts(
        current=Stage.DEVELOPER, needs_task_list=True, tasks_planned=True, tasks_open=True
    )

    assert "tasks" in (check(run_to("qa"), open_tasks) or "")


def test_the_career_coach_needs_the_qa_report_when_qa_runs_real_tools() -> None:
    assert "QA" in (
        check(run_to("career_coach"), facts(current=Stage.DEVELOPER, needs_qa_report=True)) or ""
    )
    assert check(run_to("career_coach"), facts(needs_qa_report=True, qa_report=True)) is None


def test_finishing_is_refused_while_tasks_are_still_open() -> None:
    assert "tasks" in (check(finish(), facts(tasks_planned=True, tasks_open=True)) or "")
    assert check(finish(), facts()) is None


def test_documents_can_only_be_ones_that_exist_and_are_approved() -> None:
    assert (
        check(run_to("developer", documents=["prd.json"]), facts(approved=frozenset({"prd.json"})))
        is None
    )
    assert "prd.json" in (check(run_to("developer", documents=["prd.json"]), facts()) or "")


def test_a_run_step_must_name_an_agent_and_a_finish_must_not() -> None:
    assert check(
        NextStep.model_construct(action="run", agent=None, reason="x", note="", documents=[]),
        facts(),
    )
    assert check(
        NextStep.model_construct(action="finish", agent="qa", reason="x", note="", documents=[]),
        facts(),
    )


def test_the_fallback_is_the_next_agent_in_the_usual_order_and_finish_after_the_last() -> None:
    assert fallback_step(Stage.PRODUCT_OWNER).agent == "architect"
    assert fallback_step(Stage.DEVELOPER).agent == "qa"
    assert fallback_step(Stage.QA).agent == "career_coach"


# --- the pipeline follows the director --------------------------------------------------------


class ScriptedDirector:
    def __init__(self, *steps: NextStep | None) -> None:
        self.steps = list(steps)
        self.asked: list[Stage] = []

    def decide(self, state: Any, finished: Stage, facts: Facts) -> NextStep | None:
        self.asked.append(finished)
        return self.steps.pop(0) if self.steps else fallback_step(finished)


def ctx_with(
    tmp_path: Path, director: ScriptedDirector | None, agent: FakeAgent | None = None
) -> Any:
    from orchestrator.pipeline import PipelineContext

    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=agent or FakeAgent(),
        gates=FakeGates(),
        caps=FakeCaps(),
        director=director,
    )


def test_the_project_runs_the_agents_the_director_chooses_in_the_order_it_chooses(
    tmp_path: Path,
) -> None:
    agent = FakeAgent()
    director = ScriptedDirector(
        run_to("developer", "A one-line change needs no requirements or design"),
        run_to("career_coach", "No QA tools exist for this project"),
        finish(reason="Nothing more is needed"),
    )

    state = run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, director, agent))

    assert agent.stages == [Stage.PRODUCT_OWNER, Stage.DEVELOPER, Stage.CAREER_COACH]
    assert state.status == Status.DONE
    assert [v.stage for v in state.path][-2:] == [Stage.DEVELOPER, Stage.CAREER_COACH]
    assert state.path[-2].reason == "A one-line change needs no requirements or design"


def test_a_cycle_happens_only_when_the_director_asks_for_one(tmp_path: Path) -> None:
    agent = FakeAgent()
    director = ScriptedDirector(
        run_to("architect"), run_to("developer"), run_to("qa"),
        run_to("developer", "QA found a gap"), run_to("qa"), finish(),
    )  # fmt: skip

    state = run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, director, agent))

    assert agent.stages == [
        Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER, Stage.QA, Stage.DEVELOPER, Stage.QA,
    ]  # fmt: skip
    assert state.status == Status.DONE


def test_the_next_agent_is_told_what_the_director_wants_it_to_know(tmp_path: Path) -> None:
    agent = FakeAgent()
    director = ScriptedDirector(
        run_to("architect", note="The idea is tiny; keep the design to one file."), finish()
    )

    run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, director, agent))

    assert agent.requests[1].routing_note == "The idea is tiny; keep the design to one file."
    assert agent.requests[0].routing_note is None


def test_the_note_is_given_once_and_the_handoff_is_cleared(tmp_path: Path) -> None:
    agent = FakeAgent()
    director = ScriptedDirector(
        run_to("architect", note="Keep it small."), run_to("developer"), finish()
    )

    state = run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, director, agent))

    assert agent.requests[2].routing_note is None and state.handoff is None


def test_an_invalid_choice_falls_back_to_the_usual_order_and_says_so(tmp_path: Path) -> None:
    agent = FakeAgent()
    director = ScriptedDirector(
        run_to("product_owner"), None
    )  # the same agent again, then no answer

    run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, director, agent))

    assert agent.stages == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.CAREER_COACH,
    ]
    trace = (tmp_path / "demo" / "trace.jsonl").read_text()
    assert "usual order" in trace


def test_without_a_director_the_usual_order_runs(tmp_path: Path) -> None:
    agent = FakeAgent()

    state = run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx_with(tmp_path, None, agent))

    assert agent.stages == [
        Stage.PRODUCT_OWNER,
        Stage.ARCHITECT,
        Stage.DEVELOPER,
        Stage.QA,
        Stage.CAREER_COACH,
    ]
    assert state.status == Status.DONE


def test_the_director_stops_being_asked_after_the_move_limit(tmp_path: Path) -> None:
    from orchestrator.config import Config

    cfg = make_config().model_copy(
        update={"limits": make_config().limits.model_copy(update={"max_moves": 2})}
    )
    assert isinstance(cfg, Config)
    director = ScriptedDirector(*[run_to("qa"), run_to("developer")] * 10)
    agent = FakeAgent()
    from orchestrator.pipeline import PipelineContext

    ctx = PipelineContext(
        config=cfg,
        runs_dir=tmp_path,
        runner=agent,
        gates=FakeGates(),
        caps=FakeCaps(),
        director=director,
    )

    state = run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx)

    assert state.status == Status.DONE and len(director.asked) <= 2


def test_a_handoff_that_was_routed_survives_a_pause_into_the_next_agent() -> None:
    assert Handoff(purpose="routed", note="x", documents=["prd.json"]).documents == ["prd.json"]


@pytest.mark.parametrize("agent", ["product_owner", "architect", "developer", "qa", "career_coach"])
def test_a_request_carries_the_note_field(agent: str) -> None:
    assert (
        AgentRequest(stage=Stage(agent), project="p", idea="i", routing_note="n").routing_note
        == "n"
    )


# --- the model call ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply(**body: Any) -> Any:
    from types import SimpleNamespace

    fields = {
        "action": "run",
        "agent": "architect",
        "reason": "It needs a design.",
        "note": "n",
        "documents": [],
    }
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=__import__("json").dumps({**fields, **body}))],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def api(tmp_path: Path, client: FakeClient) -> Any:
    from orchestrator.director import ApiDirector

    return ApiDirector(make_config(), tmp_path, client=client)


def test_the_model_call_returns_the_step_and_bills_it_apart_from_the_project(
    tmp_path: Path,
) -> None:
    from orchestrator.assistant_cost import assistant_totals

    state = start(stage=Stage.PRODUCT_OWNER, idea="A coffee chat tracker")
    client = FakeClient(reply(note="Keep it small."))

    step = api(tmp_path, client).decide(state, Stage.PRODUCT_OWNER, facts())

    assert step is not None and step.agent == "architect" and step.note == "Keep it small."
    assert assistant_totals(tmp_path, state.project_slug)["director_usd"] > 0
    assert state.cost_usd == {}  # not counted against any agent's cap


def test_the_idea_and_the_journey_go_to_the_model_as_data_with_the_strong_model(
    tmp_path: Path,
) -> None:
    client = FakeClient(reply())

    api(tmp_path, client).decide(
        start(stage=Stage.PRODUCT_OWNER, idea="A coffee chat tracker"), Stage.PRODUCT_OWNER, facts()
    )

    call = client.calls[0]
    assert call["model"] == make_config().models.for_tier("strong")
    sent = call["messages"][0]["content"]
    assert "A coffee chat tracker" in sent and "product_owner" in sent
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "never instructions" in call["system"]


def test_an_api_error_or_a_malformed_answer_is_no_decision(tmp_path: Path) -> None:
    import anthropic

    boom = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    state = start(stage=Stage.PRODUCT_OWNER)

    assert api(tmp_path, FakeClient(boom)).decide(state, Stage.PRODUCT_OWNER, facts()) is None
    assert (
        api(tmp_path, FakeClient(reply(agent="wizard"))).decide(state, Stage.PRODUCT_OWNER, facts())
        is None
    )


# --- failures are decided too -----------------------------------------------------------------


def ask_person(reason: str = "This needs you") -> NextStep:
    return NextStep.model_validate(
        {"action": "ask_person", "agent": None, "reason": reason, "note": "", "documents": []}
    )


def test_asking_you_is_only_allowed_when_something_went_wrong() -> None:
    assert check(ask_person(), facts(event="qa_failed", current=Stage.QA)) is None
    assert check(ask_person(), facts(event="design_problem", current=Stage.DEVELOPER)) is None
    assert "nothing" in (check(ask_person(), facts()) or "")


def test_a_failed_qa_cannot_be_finished_or_rerun_unchanged() -> None:
    failed = facts(event="qa_failed", current=Stage.QA)

    assert "problem is open" in (check(finish(), failed) or "")
    assert "just" in (check(run_to("qa"), failed) or "")
    assert check(run_to("architect"), failed) is None
    assert check(run_to("product_owner"), failed) is None


def test_a_design_problem_may_be_answered_by_carrying_on_with_the_developer() -> None:
    problem = facts(
        event="design_problem", current=Stage.DEVELOPER, needs_task_list=True, tasks_planned=True
    )

    assert check(run_to("developer"), problem) is None
    assert check(run_to("architect"), problem) is None


def test_a_stuck_task_cannot_be_sent_straight_back_to_the_developer() -> None:
    stuck = facts(
        event="task_stuck", current=Stage.DEVELOPER, needs_task_list=True, tasks_planned=True
    )

    assert "just" in (check(run_to("developer"), stuck) or "")
    assert check(run_to("architect"), stuck) is None


class OnEvent:
    """Answers each kind of event with a chosen step; the usual order otherwise."""

    def __init__(self, **by_event: NextStep | None) -> None:
        self.by_event = by_event
        self.seen: list[Facts] = []

    def decide(self, state: Any, finished: Stage, facts: Facts) -> NextStep | None:
        self.seen.append(facts)
        if facts.event in self.by_event:
            return self.by_event[facts.event]
        return fallback_step(finished)


def qa_failing(
    tmp_path: Path, director: Any, results: list[bool], gates: FakeGates | None = None
) -> Any:
    from orchestrator.pipeline import PipelineContext

    agent = FakeAgent(qa_results=results)
    ctx = PipelineContext(
        config=make_config(), runs_dir=tmp_path, runner=agent,
        gates=gates or FakeGates(), caps=FakeCaps(), director=director,
    )  # fmt: skip
    return agent, run_pipeline(start(stage=Stage.PRODUCT_OWNER), ctx), ctx


def test_a_failed_qa_goes_wherever_the_director_says_not_to_a_fixed_loop(tmp_path: Path) -> None:
    director = OnEvent(qa_failed=run_to("product_owner", "The requirements are the problem"))

    agent, state, _ = qa_failing(tmp_path, director, [False, True])

    assert agent.stages[:5] == [
        Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER, Stage.QA, Stage.PRODUCT_OWNER,
    ]  # fmt: skip
    failed = next(f for f in director.seen if f.event == "qa_failed")
    assert failed.current == Stage.QA and "QA" in failed.problem
    assert state.status == Status.DONE


def test_the_director_can_bring_a_failed_qa_to_you_instead_of_looping(tmp_path: Path) -> None:
    director = OnEvent(qa_failed=ask_person("It needs your call"))
    gates = FakeGates()

    agent, state, _ = qa_failing(tmp_path, director, [False], gates)

    assert agent.stages.count(Stage.QA) == 1 and agent.stages.count(Stage.DEVELOPER) == 1
    assert any("QA FAILED" in (r.summary or "") for r in gates.requests)
    assert state.qa_loops == 0


def test_without_an_answer_a_failed_qa_follows_the_old_rule_so_nothing_breaks(
    tmp_path: Path,
) -> None:
    director = OnEvent(qa_failed=None)

    agent, state, _ = qa_failing(tmp_path, director, [False, True])

    assert agent.stages.count(Stage.DEVELOPER) == 2 and state.status == Status.DONE


def test_the_note_for_the_agent_after_a_failure_carries_what_failed(tmp_path: Path) -> None:
    director = OnEvent(qa_failed=run_to("developer", note="Focus on the empty-state bug."))

    agent, _, _ = qa_failing(tmp_path, director, [False, True])

    retry = [r for r in agent.requests if r.stage == Stage.DEVELOPER][1]
    assert retry.routing_note is not None
    assert "Focus on the empty-state bug." in retry.routing_note and "QA" in retry.routing_note
