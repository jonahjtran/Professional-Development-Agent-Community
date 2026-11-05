import re
from typing import Annotated

from pydantic import Field

from schemas._base import Schema, Text


class Component(Schema):
    name: Text
    responsibility: Text
    tech: Text


class TechChoice(Schema):
    choice: Text
    alternatives: Annotated[list[Text], Field(min_length=1)]
    rationale: Text


class Architecture(Schema):
    """The architecture the Architect produces."""

    components: Annotated[list[Component], Field(min_length=1)]
    data_flow: Text
    interfaces: list[Text]
    tech_choices: Annotated[list[TechChoice], Field(min_length=1)]
    risks: list[Text]


class ADR(Schema):
    """One architecture decision record; each tech choice is also written as one."""

    number: Annotated[int, Field(ge=1)]
    title: Text
    context: Text
    decision: Text
    consequences: Text

    @property
    def filename(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"{self.number:03d}-{slug}.md"
