import typer
from typer.testing import CliRunner

from orchestrator.cli_gate import CliGateBackend
from orchestrator.gates import GateBackend, GateDecision, GateOutcome, GateRequest, run_gate
from orchestrator.state import ProjectState, Stage
from tests.factories import make_config, new_state

runner = CliRunner()


def make_request(summary: str = "PRD draft 1") -> GateRequest:
    return GateRequest(
        stage=Stage.PRODUCT_OWNER, title="Review the PRD", summary=summary, cost_usd=0.75
    )


def ask(request: GateRequest, keystrokes: str) -> tuple[GateDecision, str]:
    """Run the terminal gate as a tiny CLI program, typing `keystrokes` at its prompts."""
    decisions: list[GateDecision] = []
    app = typer.Typer()

    @app.command()
    def gate() -> None:
        decisions.append(CliGateBackend().decide(request))

    result = runner.invoke(app, [], input=keystrokes)
    assert result.exit_code == 0, result.output
    return decisions[0], result.output


# --- what the human sees ---------------------------------------------------------------------


def test_shows_the_stage_title_summary_and_cost_so_far() -> None:
    _, output = ask(make_request(), "approve\n\n")

    assert "product_owner" in output
    assert "Review the PRD" in output
    assert "PRD draft 1" in output
    assert "$0.75" in output


def test_prints_the_summary_as_plain_text_not_as_formatting_markup() -> None:
    _, output = ask(make_request("[bold]not markup[/bold]"), "approve\n\n")

    assert "[bold]not markup[/bold]" in output


# --- approve ---------------------------------------------------------------------------------


def test_approve_with_no_comment() -> None:
    decision, _ = ask(make_request(), "approve\n\n")

    assert decision == GateDecision(outcome=GateOutcome.APPROVE)


def test_approve_with_a_comment() -> None:
    decision, _ = ask(make_request(), "approve\nLooks great\n")

    assert decision == GateDecision(outcome=GateOutcome.APPROVE, comments="Looks great")


# --- request changes -------------------------------------------------------------------------


def test_changes_collects_what_should_change() -> None:
    decision, _ = ask(make_request(), "changes\nAdd non-goals\n")

    assert decision == GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="Add non-goals")


def test_changes_asks_again_when_the_comment_is_blank() -> None:
    decision, output = ask(make_request(), "changes\n\n   \nAdd risks\n")

    assert decision.comments == "Add risks"
    assert "what should change" in output.lower()


# --- reject ----------------------------------------------------------------------------------


def test_reject_restarts_the_stage_by_default() -> None:
    decision, _ = ask(make_request(), "reject\nWrong scope\nn\n")

    assert decision == GateDecision(outcome=GateOutcome.REJECT, comments="Wrong scope", halt=False)


def test_reject_can_halt_the_project() -> None:
    decision, _ = ask(make_request(), "reject\n\ny\n")

    assert decision == GateDecision(outcome=GateOutcome.REJECT, halt=True)


# --- bad input -------------------------------------------------------------------------------


def test_an_unknown_choice_asks_again() -> None:
    decision, output = ask(make_request(), "maybe\napprove\n\n")

    assert decision.outcome == GateOutcome.APPROVE
    assert "approve" in output and "changes" in output and "reject" in output


def test_the_choice_is_not_case_sensitive() -> None:
    decision, _ = ask(make_request(), "APPROVE\n\n")

    assert decision.outcome == GateOutcome.APPROVE


# --- fits the gate machinery -----------------------------------------------------------------


def test_the_terminal_gate_is_a_gate_backend() -> None:
    backend: GateBackend = CliGateBackend()

    assert callable(backend.decide)


def test_run_gate_applies_a_decision_typed_at_the_terminal() -> None:
    results: list[ProjectState] = []
    app = typer.Typer()

    @app.command()
    def gate() -> None:
        state = new_state().evolve(stage=Stage.PRODUCT_OWNER)
        results.append(run_gate(state, make_config(), CliGateBackend(), title="Review the PRD"))

    result = runner.invoke(app, [], input="changes\nAdd non-goals\n")

    assert result.exit_code == 0, result.output
    assert results[0].revisions == {Stage.PRODUCT_OWNER: 1}
    assert results[0].gate_history[-1].comments == "Add non-goals"
