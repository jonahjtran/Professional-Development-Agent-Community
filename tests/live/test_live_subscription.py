"""Live subscription test: a Developer-style call with the API key blanked signs in with your plan.

Spends one tiny message from your Claude plan (no API credits) and needs you to be signed in
(`claude auth login`). A fake API key is put in the environment on purpose: it proves that blanking
the key for the call, the way `developer_env` does for a subscription task, makes Claude Code use
your account instead. Run it with:

    uv run pytest tests/live --live -s -k subscription
"""

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, query

from orchestrator.config import load_config
from orchestrator.developer_backend import developer_env
from orchestrator.developer_billing import check_subscription

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"


def test_a_call_without_the_api_key_uses_the_claude_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-not-a-real-key")
    config = load_config(CONFIG)
    status = check_subscription()
    print("\nsign-in check:", status.ok, status.detail)
    assert status.ok, f"{status.detail} {status.fix}"

    async def ask() -> tuple[str | None, ResultMessage | None]:
        source: str | None = None
        result: ResultMessage | None = None
        async for message in query(
            prompt="Reply with the single word ok.",
            options=ClaudeAgentOptions(
                model=config.models.for_tier("fast"),
                cwd=tmp_path,
                tools=[],
                max_turns=1,
                env=developer_env("claude", config.local, "subscription"),
            ),
        ):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                source = message.data.get("apiKeySource")
            if isinstance(message, ResultMessage):
                result = message
        return source, result

    source, result = asyncio.run(ask())

    print("key source:", source, "| error:", result.is_error if result else None)
    assert result is not None and not result.is_error
    assert source != "ANTHROPIC_API_KEY"  # the fake key was not what signed in
