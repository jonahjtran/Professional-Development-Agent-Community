from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from schemas._base import Schema, Text
from schemas.prd import PRD


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

    @model_validator(mode="after")
    def _dependencies_are_sound(self) -> Self:
        ids = [task.id for task in self.tasks]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids: {', '.join(duplicates)}")
        known = set(ids)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep == task.id:
                    raise ValueError(f"task {task.id} depends on itself")
                if dep not in known:
                    raise ValueError(f"task {task.id} depends on {dep}, which does not exist")
        cycle = _find_cycle({task.id: task.depends_on for task in self.tasks})
        if cycle:
            raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")
        seen: set[str] = set()
        for task in self.tasks:
            for dep in task.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"task {task.id} comes before {dep}, which it depends on; "
                        "list dependencies first"
                    )
            seen.add(task.id)
        return self


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one dependency cycle as a list of ids (first id repeated at the end), or None."""
    done: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in path:
            return [*path[path.index(node) :], node]
        if node in done:
            return None
        path.append(node)
        for dep in graph[node]:
            found = visit(dep)
            if found:
                return found
        path.pop()
        done.add(node)
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def unmapped_metrics(prd: PRD, plan: TaskList) -> list[str]:
    """Success metrics that no task's acceptance criteria mention, in PRD order.

    A metric counts as covered when its name appears (ignoring case and extra spaces) in any
    criterion. This is a flag for the human at the gate, not an error: a metric may be
    measured outside the task list.
    """
    text = _normalize(" ".join(c for task in plan.tasks for c in task.acceptance_criteria))
    return [m.name for m in prd.success_metrics if _normalize(m.name) not in text]


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())
