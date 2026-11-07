from typing import Literal

from schemas._base import Schema, Text


class Route(Schema):
    """Where the coordinator sends a person's request, and why."""

    agent: Literal["product_owner", "architect", "developer", "qa", "career_coach"]
    reason: Text
    # The person's request restated for that agent: specific, with no new requirements added.
    instruction: Text
