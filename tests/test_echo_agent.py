from pathlib import Path

from orchestrator.echo_agent import EchoAgent
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import AgentRequest, PipelineContext, run_pipeline
from orchestrator.state import Stage, Status
from tests.factories import make_config, new_state


def request(stage: Stage, comments: str | None = None) -> AgentRequest:
    return AgentRequest(stage=stage, project="demo", idea="Track events", comments=comments)


def test_the_echo_agent_echoes_the_stage_and_the_idea() -> None:
    result = EchoAgent().run(request(Stage.ARCHITECT))

    assert "architect" in result.output
    assert "Track events" in result.output


def test_the_echo_agent_shows_the_comments_it_was_asked_to_address() -> None:
    result = EchoAgent().run(request(Stage.PRODUCT_OWNER, comments="Add non-goals"))

    assert "Add non-goals" in result.output


def test_the_echo_agent_reports_a_fixed_cost() -> None:
    assert EchoAgent(cost_usd=0.25).run(request(Stage.DEVELOPER)).cost_usd == 0.25


def test_qa_passes_unless_told_to_fail_first() -> None:
    agent = EchoAgent(qa_failures=1)

    results = [agent.run(request(Stage.QA)).passed for _ in range(3)]

    assert results == [False, True, True]


def test_only_qa_reports_pass_or_fail() -> None:
    assert EchoAgent().run(request(Stage.DEVELOPER)).passed is None


class AutoApprove:
    def decide(self, request: GateRequest) -> GateDecision:
        return GateDecision(outcome=GateOutcome.APPROVE)


class NoCaps:
    def resolve(self, request: object) -> object:
        raise AssertionError("no cap should be hit")


def test_the_echo_agent_drives_a_whole_pipeline_run(tmp_path: Path) -> None:
    ctx = PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=EchoAgent(qa_failures=1),
        gates=AutoApprove(),
        caps=NoCaps(),  # type: ignore[arg-type]
    )

    assert run_pipeline(new_state(), ctx).status == Status.DONE
