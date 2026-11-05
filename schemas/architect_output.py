from schemas._base import Schema
from schemas.architecture import Architecture
from schemas.tasks import TaskList


class ArchitectOutput(Schema):
    """Everything the Architect returns in one call: the design and the ordered task list."""

    architecture: Architecture
    task_list: TaskList
