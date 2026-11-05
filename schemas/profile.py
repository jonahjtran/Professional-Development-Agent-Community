from typing import Annotated

from pydantic import Field

from schemas._base import Schema, Text


class ResumeEntry(Schema):
    title: Text
    subtitle: str | None = None
    dates: str | None = None
    bullets: list[Text]


class ResumeSection(Schema):
    name: Text
    max_bullets: Annotated[int, Field(ge=1)]  # the one-page budget for the section
    entries: list[ResumeEntry]

    @property
    def bullet_count(self) -> int:
        return sum(len(e.bullets) for e in self.entries)


class ResumeData(Schema):
    """A structured copy of the resume, kept in the private profile repo."""

    sections: Annotated[list[ResumeSection], Field(min_length=1)]

    def section(self, name: str) -> ResumeSection | None:
        wanted = name.strip().lower()
        return next((s for s in self.sections if s.name.lower() == wanted), None)


class TrackGoal(Schema):
    target_roles: list[Text]
    target_orgs: list[Text] = Field(default_factory=list)
    emphasis: list[Text] = Field(default_factory=list)


class Goals(Schema):
    tracks: dict[str, TrackGoal]
    timeline: str = ""
    avoid: list[Text] = Field(default_factory=list)
