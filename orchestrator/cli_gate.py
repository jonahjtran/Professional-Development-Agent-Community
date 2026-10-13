from rich.console import Console
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

        choice = Prompt.ask(
            "Decision", choices=list(_CHOICES), case_sensitive=False, console=console
        )
        outcome = _CHOICES[choice]

        if outcome == GateOutcome.REQUEST_CHANGES:
            return GateDecision(outcome=outcome, comments=self._ask_what_to_change(console))

        comments = Prompt.ask(
            "Comments (optional)", default="", show_default=False, console=console
        ).strip()
        if outcome == GateOutcome.REJECT:
            halt = Confirm.ask(
                "Halt the project instead of restarting this stage?", default=False, console=console
            )
            return GateDecision(outcome=outcome, comments=comments, halt=halt)
        return GateDecision(outcome=outcome, comments=comments)

    @staticmethod
    def _show(console: Console, request: GateRequest) -> None:
        # Text objects print literally. The summary comes from an agent, so Rich must not
        # interpret any markup in it.
        console.print(Text(f"Gate: {request.stage.value}", style="bold"))
        console.print(Text(f"Spent so far: ${request.cost_usd:.2f}"))
        console.print(Panel(Text(request.summary), title=Text(request.title)))

    @staticmethod
    def _ask_what_to_change(console: Console) -> str:
        while True:
            comments = Prompt.ask("What should change?", console=console).strip()
            if comments:
                return comments
            console.print("Please say what should change.")
