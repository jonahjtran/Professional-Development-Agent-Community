from enum import StrEnum
from typing import Annotated

from pydantic import Field

from schemas._base import Schema, Text


class Size(StrEnum):
    SMALL = "S"
    MEDIUM = "M"
    LARGE = "L"


class Task(Schema):
    id: Text
    title: Text
    description: Text
    acceptance_criteria: Annotated[list[Text], Field(min_length=1)]
    depends_on: list[Text] = Field(default_factory=list)
    size: Size


class TaskList(Schema):
    """The ordered task list the Architect produces; the Developer builds one task per run."""

    tasks: Annotated[list[Task], Field(min_length=1)]
