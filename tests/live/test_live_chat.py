"""Live chat test: the real assistant answers a question about this repository.

Costs a few cents (the fast model, a couple of tool calls) and needs ANTHROPIC_API_KEY. It checks
what the fakes cannot: that the SDK accepts our options (read-only tools, the in-process studio MCP
server, the PreToolUse guard, partial streaming) and that the assistant really uses them. Run it:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k chat
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from orchestrator.config import load_config
from orchestrator.explain import ExplainResult
from orchestrator.ui.chat import ChatService

pytestmark = pytest.mark.live

ROOT = Path(__file__).parent.parent.parent


class NoExplainer:
    def explain(self, item: Any) -> ExplainResult:
        return ExplainResult(None, "not used here")


async def ask(chat: ChatService, message: str, session: str | None = None) -> list[dict[str, Any]]:
    return [
        e
        async for e in chat.turn(
            message, session_id=session, repo="studio", tier="fast", context=None
        )
    ]


def test_the_assistant_reads_the_code_and_uses_the_studio_tools(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    chat = ChatService(
        runs,
        load_config(ROOT / "config.yaml"),
        NoExplainer(),
        studio_root=ROOT,
        projects_dir=tmp_path / "clones",
    )

    async def scenario() -> list[dict[str, Any]]:
        try:
            first = await ask(
                chat,
                "Read orchestrator/permissions.py and tell me in one sentence what it does. "
                "Then call the list_projects tool and say how many projects there are.",
            )
            return first
        finally:
            await chat.close()

    events = asyncio.run(scenario())
    kinds = [e["type"] for e in events]
    steps = [e["text"] for e in events if e["type"] == "step"]
    done = events[-1]

    assert "error" not in kinds, events
    assert done["type"] == "done" and done["html"]
    assert any("permissions.py" in s for s in steps)  # it read the file
    assert any("List projects" in s for s in steps)  # and used the MCP tool
    assert 0 < done["cost_usd"] < 0.5
