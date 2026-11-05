from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from orchestrator.tool_hooks import ToolAnswer, ToolRequest


class CliToolBackend:
    """A ToolPermissionBackend that asks the human at the terminal."""

    def ask(self, request: ToolRequest) -> ToolAnswer:
        console = Console()
        # Text prints literally: the command comes from an agent, so Rich must not read markup.
        console.print(Text(f"The Developer wants to use {request.tool}:", style="bold"))
        console.print(Text(request.summary))
        console.print(Text(f"Why you are being asked: {request.reason}"))
        if Confirm.ask("Allow this once?", default=False, console=console):
            return ToolAnswer(allow=True)
        message = Prompt.ask(
            "Tell the agent why (optional)", default="", show_default=False, console=console
        ).strip()
        return ToolAnswer(allow=False, message=message)
