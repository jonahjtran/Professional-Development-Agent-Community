from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from schemas._base import Schema, Text


class Track(StrEnum):
    ENGINEERING = "engineering"
    CONSULTING = "consulting"
    BOTH = "both"


class SuccessMetric(Schema):
    name: Text
    target: Text
    how_measured: Text


class PRD(Schema):
    """The product requirements document the Product Owner produces."""

    problem: Text
    users: Annotated[list[Text], Field(min_length=1)]
    track: Track
    goals: Annotated[list[Text], Field(min_length=1)]
    success_metrics: Annotated[list[SuccessMetric], Field(min_length=1)]
    scope: Annotated[list[Text], Field(min_length=1)]
    non_goals: Annotated[list[Text], Field(min_length=1)]
    risks: list[Text]
    assumptions: list[Text] = Field(default_factory=list)
    stakeholders: list[Text] | None = None
    adoption_risks: list[Text] | None = None

    @model_validator(mode="after")
    def _consulting_needs_its_extra_fields(self) -> Self:
        if self.track in (Track.CONSULTING, Track.BOTH):
            for name in ("stakeholders", "adoption_risks"):
                if not getattr(self, name):
                    raise ValueError(f"{name} is required when track is {self.track.value}")
        return self
