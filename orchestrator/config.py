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

    def for_tier(self, tier: Tier) -> str:
        return self.strong if tier == "strong" else self.fast


class Budget(_Strict):
    project_cap_usd: float = Field(gt=0)
    warn_at_fraction: float = Field(gt=0, le=1)
    stage_caps_usd: StageValues[Annotated[float, Field(gt=0)]]


class Limits(_Strict):
    max_revisions: int = Field(ge=0)
    max_qa_loops: int = Field(ge=0)
    max_web_searches: int = Field(ge=0)  # per agent call, for agents allowed to search the web
    max_turns: StageValues[Annotated[int, Field(ge=1)]]


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


class Config(_Strict):
    models: Models
    budget: Budget
    limits: Limits
    pricing: Pricing


def load_config(path: Path) -> Config:
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
