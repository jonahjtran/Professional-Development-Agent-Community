from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["strong", "fast"]


class _Strict(BaseModel):
    """Base for config sections: immutable, and unknown keys are errors (catches typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StageValues[T](_Strict):
    """One value per agent stage."""

    product_owner: T
    architect: T
    developer: T
    qa: T
    career_coach: T


class Models(_Strict):
    strong: str
    fast: str
    # The Claude model the Developer uses when its backend is Claude (the local model is under
    # `local`). Sonnet is the default: it is much cheaper than Opus for long coding sessions.
    developer: str = "claude-sonnet-5-5"

    def for_tier(self, tier: Tier) -> str:
        return self.strong if tier == "strong" else self.fast


class Budget(_Strict):
    project_cap_usd: float = Field(gt=0)
    warn_at_fraction: float = Field(gt=0, le=1)
    stage_caps_usd: StageValues[Annotated[float, Field(gt=0)]]


class Limits(_Strict):
    max_revisions: int = Field(ge=0)
    max_qa_loops: int = Field(ge=0)
    # Times the Developer may send the project back to the Architect about a design problem.
    max_replans: int = Field(default=2, ge=0)
    # Agent visits in one project after which the orchestrator stops choosing and follows the usual
    # order, so a project cannot loop forever.
    max_moves: int = Field(default=40, ge=1)
    max_task_usd: float = Field(gt=0)  # spend limit for one Developer task (SDK-enforced)
    max_web_searches: int = Field(ge=0)  # per agent call, for agents allowed to search the web
    max_turns: StageValues[Annotated[int, Field(ge=1)]]


class CheckCommand(_Strict):
    """One tool the QA stage runs. It is skipped if the `requires` program is not on PATH."""

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)  # split like a shell would, but run without a shell
    requires: str | None = None


class QaConfig(_Strict):
    timeout_seconds: int = Field(gt=0)  # per check
    tail_lines: int = Field(gt=0)  # how much of each check's output the report keeps
    checks: list[CheckCommand] = Field(min_length=1)


class TierPricing(_Strict):
    """Dollars per million tokens for one model tier."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class Pricing(_Strict):
    strong: TierPricing
    fast: TierPricing
    web_search_usd_per_request: float = Field(ge=0)

    def for_tier(self, tier: Tier) -> TierPricing:
        return self.strong if tier == "strong" else self.fast


class SandboxConfig(_Strict):
    """Hosts the Developer's sandbox may reach without asking. Any other host is asked about."""

    allowed_domains: list[str] = Field(default_factory=list)


class DeveloperConfig(_Strict):
    """What the Developer is told to follow, whichever model runs it."""

    # Names of skills in this project's `.claude/skills` folder, added to its instructions.
    skills: list[str] = Field(default_factory=list)


class LocalConfig(_Strict):
    """An open-source model served on this computer by Ollama, for the Developer agent.

    Claude Code talks to it through Ollama's Anthropic-compatible endpoint, so the Developer keeps
    the same tools, permission policy and sandbox as with Claude.
    """

    endpoint: str = "http://localhost:11434"
    model: str = "qwen3-coder:30b"  # the Ollama tag, see `ollama list`
    min_context: int = Field(default=65536, ge=1)  # Claude Code needs a large context window


class AssistantConfig(_Strict):
    """The chat assistant: its dollar cap per chat and the turns one question may take."""

    max_chat_usd: float = Field(default=1.0, gt=0)
    max_turns: int = Field(default=15, ge=1)


class Config(_Strict):
    models: Models
    budget: Budget
    limits: Limits
    pricing: Pricing
    qa: QaConfig
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    local: LocalConfig = Field(default_factory=LocalConfig)
    developer: DeveloperConfig = Field(default_factory=DeveloperConfig)


def load_config(path: Path) -> Config:
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
