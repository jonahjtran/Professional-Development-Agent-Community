"""The types the pipeline and the Architect's fix planner share (kept apart to avoid a cycle)."""

from dataclasses import dataclass
from typing import Protocol

from schemas import FixPlan


@dataclass(frozen=True)
class FixRequest:
    project: str
    idea: str
    architecture: str
    tasks_json: str
    report_json: str
    round: int  # 1 for the first fix attempt


@dataclass(frozen=True)
class FixResult:
    plan: FixPlan
    cost_usd: float
    model: str | None = None


class FixPlanner(Protocol):
    def plan(self, request: FixRequest) -> FixResult:
        """Plan the fixes, or raise AgentError (with the cost so far)."""
        ...


def render_plan(plan: FixPlan, round_number: int) -> str:
    """The plan as a document for the human."""
    lines = [f"# QA fix plan, round {round_number}", "", plan.analysis, ""]
    if plan.tasks:
        lines.append("## Tasks for the Developer")
        for task in plan.tasks:
            lines += ["", f"### {task.id}: {task.title}", "", task.description, ""]
            lines += [f"- {c}" for c in task.acceptance_criteria]
    if plan.needs_human:
        lines += ["", "## Only you can do these", ""]
        lines += [f"- {item}" for item in plan.needs_human]
    return "\n".join(lines).strip() + "\n"
