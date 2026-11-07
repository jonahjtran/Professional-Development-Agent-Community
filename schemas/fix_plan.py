from typing import Annotated

from pydantic import Field

from schemas._base import Schema, Text
from schemas.tasks import Task


class FixPlan(Schema):
    """What the Architect decides after a failed QA report.

    `tasks` are new tasks for the Developer (they are added to the end of the task list).
    `needs_human` are failures only the person can fix (a missing secret, an account to set up).
    """

    analysis: Text
    tasks: Annotated[list[Task], Field(max_length=12)] = Field(default_factory=list)
    needs_human: list[Text] = Field(default_factory=list)
