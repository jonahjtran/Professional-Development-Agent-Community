from typing import Annotated

from pydantic import Field

from schemas._base import Schema, Text


class TaskOrder(Schema):
    """The Architect's decision after being asked to prioritize: the order of the unfinished tasks.

    `order` lists every unfinished task id exactly once, first to build first. Keeping the current
    order is a valid answer: the Architect owns the priorities and may decline, saying why.
    """

    order: Annotated[list[Text], Field(min_length=1, max_length=200)]
    reason: Text
