"""Placeholder agent specs, used until the real prompts and schemas arrive in Phase 2.

Only stages that are safe without a sandbox get a spec. The Developer and QA need the Phase 3
worktree and permission callback before they may run commands or write files, and the Architect
and Product Owner get read-only tools for now. A stage with no spec makes the run halt cleanly.
"""

from pathlib import Path

from orchestrator.api_runner import ApiAgentSpec
from orchestrator.prompts import DEFAULT_PROMPTS_DIR, load_prompt
from orchestrator.runner import AgentSpec
from orchestrator.stages import StageSpec, architect_stage, prd_stage
from orchestrator.state import Stage
from schemas import PRD, ArchitectOutput
from schemas._base import Schema


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


def default_api_specs(prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> tuple[ApiAgentSpec, ...]:
    """The agents that run on the raw Messages API, with their versioned prompts."""
    return (
        ApiAgentSpec(
            stage=Stage.PRODUCT_OWNER,
            prompt=load_prompt("product_owner", prompts_dir),
            tier="strong",
            schema=PRD,
            web_search=True,
        ),
        ApiAgentSpec(
            stage=Stage.ARCHITECT,
            prompt=load_prompt("architect", prompts_dir),
            tier="strong",
            schema=ArchitectOutput,
        ),
    )


def default_api_schemas() -> dict[Stage, type[Schema]]:
    return {spec.stage: spec.schema for spec in default_api_specs()}


def default_stage_specs() -> dict[Stage, StageSpec]:
    """How each API-run stage's validated output becomes stored artifacts."""
    return {Stage.PRODUCT_OWNER: prd_stage(), Stage.ARCHITECT: architect_stage()}
