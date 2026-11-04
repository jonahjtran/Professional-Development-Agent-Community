from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from orchestrator.caps import CapAction, CapRequest, CapResolution


class CliCapBackend:
    """A CapBackend that asks the human at the terminal."""

    def resolve(self, request: CapRequest) -> CapResolution:
        console = Console()
        console.print(Text(f"Paused at {request.stage.value}", style="bold"))
        console.print(Text(request.detail))

        choices = ["raise", "abandon"]
        if request.can_approve_anyway:
            choices.insert(0, "approve")
        choice = Prompt.ask("Decision", choices=choices, case_sensitive=False, console=console)

        if choice == "approve":
            return CapResolution(action=CapAction.APPROVE_ANYWAY)
        if choice == "abandon":
            return CapResolution(action=CapAction.ABANDON)
        return CapResolution(action=CapAction.RAISE, amount=self._ask_amount(console, request))

    @staticmethod
    def _ask_amount(console: Console, request: CapRequest) -> float:
        whole = request.unit == "revisions"
        question = "Extra revisions to allow" if whole else "Extra dollars to allow"
        while True:
            text = Prompt.ask(question, console=console).strip()
            try:
                amount = float(text)
            except ValueError:
                console.print("Please enter a positive number.")
                continue
            if amount <= 0:
                console.print("Please enter a positive number.")
            elif whole and amount != int(amount):
                console.print("Revisions must be a whole number.")
            else:
                return amount
