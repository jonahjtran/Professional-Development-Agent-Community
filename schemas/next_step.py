from typing import Literal

from schemas._base import Schema, Text


class NextStep(Schema):
    """What the orchestrator decides after an agent finishes: which agent runs next, or finish."""

    action: Literal["run", "finish"]
    # The agent to run (when action is "run"); null when the project is finished.
    agent: Literal["product_owner", "architect", "developer", "qa", "career_coach"] | None
    reason: Text  # why, in words a person follows
    # What the next agent should know that its documents do not say (may be empty).
    note: str
    # Approved documents to give the next agent, by name (may be empty: it gets its usual ones).
    documents: list[str]
