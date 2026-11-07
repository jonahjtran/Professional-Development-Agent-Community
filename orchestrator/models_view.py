"""Which model each agent uses, worked out from the config and the agents' own specs."""

from dataclasses import dataclass

from orchestrator.config import Config
from orchestrator.default_specs import default_api_specs
from orchestrator.developer_backend import developer_model
from orchestrator.state import Stage

_NAMES = {
    Stage.PRODUCT_OWNER: ("product_owner", "Product Owner"),
    Stage.ARCHITECT: ("architect", "Architect"),
    Stage.QA: ("qa", "QA"),
    Stage.CAREER_COACH: ("career_coach", "Career coach"),
}


@dataclass(frozen=True)
class AgentModel:
    id: str
    name: str
    backend: str  # claude | local
    model: str
    note: str = ""


def agent_models(config: Config, developer_backend: str) -> list[AgentModel]:
    """Every agent in pipeline order, with the model it uses when asked now.

    Tiers come from each agent's own spec, so this stays true if a spec changes.
    """
    tiers = {spec.stage: spec.tier for spec in default_api_specs()}
    rows = [
        AgentModel(
            "product_owner",
            "Product Owner",
            "claude",
            config.models.for_tier(tiers[Stage.PRODUCT_OWNER]),
        ),
        AgentModel(
            "architect", "Architect", "claude", config.models.for_tier(tiers[Stage.ARCHITECT])
        ),
        AgentModel(
            "developer",
            "Developer",
            developer_backend,
            developer_model(developer_backend, config),
            "Chosen per project or as the default; applies from the next task.",
        ),
        AgentModel("qa", "QA", "claude", config.models.for_tier(tiers[Stage.QA])),
        AgentModel(
            "career_coach",
            "Career coach",
            "claude",
            config.models.for_tier(tiers[Stage.CAREER_COACH]),
        ),
        AgentModel(
            "chat",
            "Chat assistant",
            "claude",
            config.models.for_tier("strong"),
            f"Strong by default; the Fast switch uses {config.models.for_tier('fast')}.",
        ),
        AgentModel(
            "explanations", "Approval explanations", "claude", config.models.for_tier("fast")
        ),
    ]
    return rows
