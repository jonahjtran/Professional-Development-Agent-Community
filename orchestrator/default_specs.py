"""Placeholder agent specs, used until the real prompts and schemas arrive in Phase 2.

Only stages that are safe without a sandbox get a spec. The Developer and QA need the Phase 3
worktree and permission callback before they may run commands or write files, and the Architect
and Product Owner get read-only tools for now. A stage with no spec makes the run halt cleanly.
"""

from orchestrator.runner import AgentSpec
from orchestrator.state import Stage


def default_specs() -> tuple[AgentSpec, ...]:
    return (
        AgentSpec(
            stage=Stage.PRODUCT_OWNER,
            system_prompt=(
                "You are a product owner. Turn the project idea into a short product "
                "requirements document: goals, users, scope, non-goals, and success measures."
            ),
            prompt_version="placeholder-0",
            tools=("Read", "WebSearch"),
            tier="strong",
        ),
        AgentSpec(
            stage=Stage.ARCHITECT,
            system_prompt=(
                "You are a software architect. From the approved requirements, describe the "
                "architecture and break the work into small ordered tasks."
            ),
            prompt_version="placeholder-0",
            tools=("Read",),
            tier="strong",
        ),
    )
