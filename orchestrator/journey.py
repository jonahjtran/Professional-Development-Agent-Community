"""The project's journey: forward through the agents and back again, with every move recorded.

The orchestrator is a deterministic state machine, but it is not a straight line. It may go back
to any earlier agent (QA to the Architect for a fix plan, the Developer to the Architect when a
task cannot be built as designed), so no agent is ever simply "done" until the project is. This
module holds the rules for which moves are allowed and reads the recorded path.
"""

from typing import Literal

from orchestrator.state import ProjectState, Stage, StageVisit

ORDER = (
    Stage.INTAKE,
    Stage.PRODUCT_OWNER,
    Stage.ARCHITECT,
    Stage.DEVELOPER,
    Stage.QA,
    Stage.CAREER_COACH,
)
LABELS = {
    Stage.INTAKE: "Intake",
    Stage.PRODUCT_OWNER: "Product Owner",
    Stage.ARCHITECT: "Architect",
    Stage.DEVELOPER: "Developer",
    Stage.QA: "QA",
    Stage.CAREER_COACH: "Career coach",
}
_RANK = {stage: n for n, stage in enumerate(ORDER)}


def can_move(src: Stage, dst: Stage) -> bool:
    """Forward only to the next agent; back to any earlier agent (never to intake)."""
    if dst == Stage.INTAKE or src == dst:
        return False
    return _RANK[dst] == _RANK[src] + 1 or _RANK[dst] < _RANK[src]


def direction(src: Stage, dst: Stage) -> Literal["forward", "back"]:
    return "back" if _RANK[dst] < _RANK[src] else "forward"


def visit_counts(state: ProjectState) -> dict[Stage, int]:
    """How many times each agent has been the current one.

    A project saved before the journey was recorded has no path; it is assumed to have passed each
    agent up to the current one once.
    """
    if not state.path:
        if state.stage == Stage.INTAKE:
            return {}
        return {s: 1 for s in ORDER if s != Stage.INTAKE and _RANK[s] <= _RANK[state.stage]}
    counts: dict[Stage, int] = {}
    for visit in state.path:
        counts[visit.stage] = counts.get(visit.stage, 0) + 1
    return counts


def going_back(state: ProjectState) -> StageVisit | None:
    """The move that brought the project back to the current agent, if it was a move back."""
    if not state.path:
        return None
    last = state.path[-1]
    if (
        last.stage == state.stage
        and last.came_from is not None
        and direction(last.came_from, last.stage) == "back"
    ):
        return last
    return None


def path_summary(state: ProjectState) -> list[str]:
    """The journey as a line of agent names, a move back marked with ↩."""
    out: list[str] = []
    for visit in state.path:
        back = visit.came_from is not None and direction(visit.came_from, visit.stage) == "back"
        out.append(("↩ " if back else "") + LABELS[visit.stage])
    return out
