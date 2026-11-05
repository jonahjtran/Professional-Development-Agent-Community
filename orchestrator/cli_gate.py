from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from orchestrator.gates import GateDecision, GateOutcome, GateRequest

_CHOICES = {
    "approve": GateOutcome.APPROVE,
    "changes": GateOutcome.REQUEST_CHANGES,
    "reject": GateOutcome.REJECT,
}


class CliGateBackend:
    """A GateBackend that asks the human at the terminal."""

    def decide(self, request: GateRequest) -> GateDecision:
        console = Console()
        self._show(console, request)
        answers = self._ask_questions(console, request)

        choice = Prompt.ask(
            "Decision", choices=list(_CHOICES), case_sensitive=False, console=console
        )
        outcome = _CHOICES[choice]

        if outcome == GateOutcome.REQUEST_CHANGES:
            comments = self._ask_what_to_change(console)
            return GateDecision(outcome=outcome, comments=comments, answers=answers)

        comments = Prompt.ask(
            "Comments (optional)", default="", show_default=False, console=console
        ).strip()
        if outcome == GateOutcome.REJECT:
            halt = Confirm.ask(
                "Halt the project instead of restarting this stage?", default=False, console=console
            )
            return GateDecision(outcome=outcome, comments=comments, halt=halt, answers=answers)
        approved = [
            item.id
            for item in request.items
            if Confirm.ask(f"Approve: {item.label}?", default=True, console=console)
        ]
        return GateDecision(
            outcome=outcome, comments=comments, approved_items=approved, answers=answers
        )

    @staticmethod
    def _show(console: Console, request: GateRequest) -> None:
        # Text objects print literally. The summary comes from an agent, so Rich must not
        # interpret any markup in it.
        console.print(Text(f"Gate: {request.stage.value}", style="bold"))
        console.print(Text(f"Spent so far: ${request.cost_usd:.2f}"))
        console.print(Panel(Text(request.summary), title=Text(request.title)))

    @staticmethod
    def _ask_questions(console: Console, request: GateRequest) -> dict[str, str]:
        answers: dict[str, str] = {}
        for question in request.questions:
            answer = Prompt.ask(
                escape(f"Question: {question} (blank to skip)"),
                default="",
                show_default=False,
                console=console,
            ).strip()
            if answer:
                answers[question] = answer
        return answers

    @staticmethod
    def _ask_what_to_change(console: Console) -> str:
        while True:
            comments = Prompt.ask("What should change?", console=console).strip()
            if comments:
                return comments
            console.print("Please say what should change.")
