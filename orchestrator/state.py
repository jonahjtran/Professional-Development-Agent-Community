import contextlib
import os
import tempfile
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas import Track


class Stage(StrEnum):
    INTAKE = "intake"
    PRODUCT_OWNER = "product_owner"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    CAREER_COACH = "career_coach"


class Status(StrEnum):
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    HALTED = "halted"
    DONE = "done"


# Slugs become directory names, so they must not be able to escape runs/.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class InterventionKind(StrEnum):
    """Why the run is waiting for a human. Tells `resume` which question to ask."""

    GATE = "gate"
    REVISION_CAP = "revision_cap"
    BUDGET_CAP = "budget_cap"
    MERGE_PENDING = "merge_pending"  # QA is waiting for the Developer pull requests to be merged
    QA_FAILED = "qa_failed"
    TOOL_PERMISSION = "tool_permission"


class Intervention(BaseModel):
    kind: InterventionKind
    detail: Annotated[str, Field(min_length=1)]

    model_config = ConfigDict(extra="forbid", frozen=True)


class GateOutcome(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class GateRecord(BaseModel):
    """One human decision at a gate, kept for the audit trail."""

    stage: Stage
    outcome: GateOutcome
    comments: str = ""
    ts: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)


class StageVisit(BaseModel):
    """One time an agent became the current one: where from, and why.

    The orchestrator may move back as well as forward, so a stage can be visited more than once.
    """

    stage: Stage
    reason: str
    ts: datetime
    came_from: Stage | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class Handoff(BaseModel):
    """Why the orchestrator moved to the current agent, when the visit has a special purpose.

    qa_fix: the Architect is planning fixes for a failed QA report.
    design_review: the Architect is resolving a design problem the Developer raised.
    human_design: the Architect is turning a change you asked for into tasks.
    qa_gate: QA is being returned to, only to show you its failed report (nothing could be fixed).
    routed: the orchestrator chose this agent next; `note` is what it wants the agent to know and
        `documents` the approved documents it wants it to have (empty: the agent's usual ones).
    """

    purpose: str
    note: str = ""
    documents: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True)


class Rework(BaseModel):
    """You sent the project back to an agent to change something, and it is still being worked in.

    It lives from the moment you send the project back until the project is finished again, so
    the agents that run after the one you chose know to make only the change you asked for.
    """

    origin: Stage
    note: str
    ts: datetime
    origin_done: bool = False  # the agent you chose has finished and you approved its work

    model_config = ConfigDict(extra="forbid", frozen=True)


class Allowances(BaseModel):
    """Extra room the human has granted, added on top of the limits in config.yaml.

    Kept in the state, not the config, so a decision to raise a cap is saved with the project.
    """

    extra_project_usd: Annotated[float, Field(ge=0)] = 0.0
    extra_stage_usd: dict[Stage, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)
    extra_revisions: dict[Stage, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    # Budget warnings the human chose to continue past: "project" or a stage name -> the cap that
    # was in force. That warning is not raised again until the cap changes; the cap still stops.
    warned: dict[str, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class Artifact(BaseModel):
    """A document an agent produced: where it is, a hash of its content, and its approval."""

    path: str  # relative to the project's run directory
    content_hash: str
    revision: Annotated[int, Field(ge=1)]
    approved_at: datetime | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectState(BaseModel):
    """Everything needed to resume a project after a crash or Ctrl-C."""

    project_slug: Slug
    idea: str
    stage: Stage = Stage.INTAKE
    status: Status = Status.RUNNING
    current_task_id: str | None = None
    revisions: dict[Stage, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    intervention: Intervention | None = None
    cost_usd: dict[Stage, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)
    gate_history: list[GateRecord] = Field(default_factory=list)
    allowances: Allowances = Field(default_factory=Allowances)
    track: Track = Track.ENGINEERING
    repo_path: str | None = None  # the project's own git repository (set for --agent full)
    repo_url: str | None = None  # its origin address, with no credentials in it
    agent: str | None = None  # which agent kind runs it (echo, claude, api, full), for resuming
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    completed_tasks: list[str] = Field(default_factory=list)  # Developer tasks approved so far
    qa_loops: Annotated[int, Field(ge=0)] = 0  # times QA has sent the work back to the Developer
    # Every agent the orchestrator has moved to, in order, with why. Older projects have none.
    path: list[StageVisit] = Field(default_factory=list)
    handoff: Handoff | None = None  # set while an agent is visited for a special purpose
    rework: Rework | None = None  # set while a change you asked for is being worked through

    # Frozen so status and intervention can never be half-updated; change state with evolve().
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _intervention_matches_status(self) -> Self:
        waiting = self.status == Status.AWAITING_HUMAN
        if waiting and self.intervention is None:
            raise ValueError("awaiting_human requires an intervention")
        if not waiting and self.intervention is not None:
            raise ValueError("only awaiting_human may carry an intervention")
        return self

    @property
    def total_cost_usd(self) -> float:
        return sum(self.cost_usd.values())

    def evolve(self, **changes: Any) -> Self:
        """Return a copy with `changes` applied, fully re-validated."""
        return type(self).model_validate({**dict(self), **changes})


def state_path(runs_dir: Path, slug: str) -> Path:
    return runs_dir / slug / "state.json"


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: write a temp file, then rename it into place.

    A crash mid-write leaves the previous file untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def save_state(runs_dir: Path, state: ProjectState) -> None:
    """Write state.json atomically."""
    atomic_write(state_path(runs_dir, state.project_slug), state.model_dump_json(indent=2))


def load_state(runs_dir: Path, slug: str) -> ProjectState:
    return ProjectState.model_validate_json(state_path(runs_dir, slug).read_text())
