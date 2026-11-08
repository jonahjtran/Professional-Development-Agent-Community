"""The types the pipeline and the Architect's prioritizer share (kept apart to avoid a cycle)."""

from dataclasses import dataclass
from typing import Protocol

from schemas import TaskOrder


@dataclass(frozen=True)
class PriorityRequest:
    project: str
    idea: str
    architecture: str
    tasks_json: str  # the unfinished tasks, in their current order
    asked: list[dict[str, str]]  # what the person asked: task_id and (optionally) a reason


@dataclass(frozen=True)
class PriorityResult:
    order: TaskOrder
    cost_usd: float
    model: str | None = None


class Prioritizer(Protocol):
    def prioritize(self, request: PriorityRequest) -> PriorityResult:
        """Decide the order, or raise AgentError (with the cost so far)."""
        ...
