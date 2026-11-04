import typer
from typer.testing import CliRunner

from orchestrator.budget import assess_budget, pause_for_budget
from orchestrator.caps import (
    CapAction,
    CapBackend,
    CapRequest,
    CapResolution,
    cap_request,
    run_cap_pause,
)
from orchestrator.cli_cap import CliCapBackend
from orchestrator.gates import GateDecision, GateOutcome, apply_decision, open_gate
from orchestrator.state import InterventionKind, ProjectState, Stage, Status
from tests.factories import make_config, new_state

runner = CliRunner()


def revision_cap_pause() -> ProjectState:
    state = open_gate(
        new_state().evolve(stage=Stage.PRODUCT_OWNER, revisions={Stage.PRODUCT_OWNER: 3}),
        title="Review",
    )
    decision = GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="More")
    return apply_decision(state, make_config(), decision)


def budget_pause(spent: float) -> ProjectState:
    state = new_state().evolve(stage=Stage.PRODUCT_OWNER, cost_usd={Stage.PRODUCT_OWNER: spent})
    return pause_for_budget(state, assess_budget(state, make_config(), Stage.PRODUCT_OWNER))


def ask(request: CapRequest, keystrokes: str) -> tuple[CapResolution, str]:
    results: list[CapResolution] = []
    app = typer.Typer()

    @app.command()
    def pause() -> None:
        results.append(CliCapBackend().resolve(request))

    result = runner.invoke(app, [], input=keystrokes)
    assert result.exit_code == 0, result.output
    return results[0], result.output


# --- building the question -------------------------------------------------------------------


def test_a_revision_cap_request_allows_approving_and_counts_in_revisions() -> None:
    request = cap_request(revision_cap_pause(), make_config())

    assert request.kind == InterventionKind.REVISION_CAP
    assert request.stage == Stage.PRODUCT_OWNER
    assert request.can_approve_anyway is True
    assert request.unit == "revisions"
    assert "revisions" in request.detail


def test_a_budget_warning_request_allows_continuing_and_counts_in_dollars() -> None:
    request = cap_request(budget_pause(1.2), make_config())

    assert request.can_approve_anyway is True
    assert request.unit == "usd"


def test_a_reached_budget_cap_request_does_not_allow_continuing() -> None:
    request = cap_request(budget_pause(1.5), make_config())

    assert request.can_approve_anyway is False


# --- what the human sees and chooses ----------------------------------------------------------


def test_shows_the_stage_and_why_the_run_stopped() -> None:
    request = cap_request(budget_pause(1.5), make_config())

    _, output = ask(request, "abandon\n")

    assert "product_owner" in output
    assert "$1.50" in output


def test_abandon() -> None:
    resolution, _ = ask(cap_request(budget_pause(1.5), make_config()), "abandon\n")

    assert resolution == CapResolution(action=CapAction.ABANDON)


def test_raise_a_dollar_cap() -> None:
    resolution, _ = ask(cap_request(budget_pause(1.5), make_config()), "raise\n2.5\n")

    assert resolution == CapResolution(action=CapAction.RAISE, amount=2.5)


def test_raise_asks_again_for_a_bad_amount() -> None:
    resolution, output = ask(
        cap_request(budget_pause(1.5), make_config()), "raise\nlots\n-1\n0\n1\n"
    )

    assert resolution.amount == 1.0
    assert "positive" in output.lower()


def test_raise_revisions_must_be_a_whole_number() -> None:
    resolution, output = ask(cap_request(revision_cap_pause(), make_config()), "raise\n1.5\n2\n")

    assert resolution == CapResolution(action=CapAction.RAISE, amount=2)
    assert "whole" in output.lower()


def test_approve_is_offered_at_a_revision_cap() -> None:
    resolution, _ = ask(cap_request(revision_cap_pause(), make_config()), "approve\n")

    assert resolution == CapResolution(action=CapAction.APPROVE_ANYWAY)


def test_approve_is_not_a_choice_once_a_budget_cap_is_reached() -> None:
    resolution, output = ask(cap_request(budget_pause(1.5), make_config()), "approve\nabandon\n")

    assert resolution.action == CapAction.ABANDON
    assert "raise" in output and "abandon" in output


# --- fits the pause machinery -----------------------------------------------------------------


def test_the_terminal_is_a_cap_backend() -> None:
    backend: CapBackend = CliCapBackend()

    assert callable(backend.resolve)


class ScriptedCapBackend:
    def __init__(self, resolution: CapResolution) -> None:
        self.resolution = resolution
        self.requests: list[CapRequest] = []

    def resolve(self, request: CapRequest) -> CapResolution:
        self.requests.append(request)
        return self.resolution


def test_run_cap_pause_asks_then_applies_the_answer() -> None:
    backend = ScriptedCapBackend(CapResolution(action=CapAction.RAISE, amount=3.0))

    state = run_cap_pause(budget_pause(1.5), make_config(), backend)

    assert state.status == Status.RUNNING
    assert state.allowances.extra_stage_usd == {Stage.PRODUCT_OWNER: 3.0}
    assert len(backend.requests) == 1
