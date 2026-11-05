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


class Allowances(BaseModel):
    """Extra room the human has granted, added on top of the limits in config.yaml.

    Kept in the state, not the config, so a decision to raise a cap is saved with the project.
    """

    extra_project_usd: Annotated[float, Field(ge=0)] = 0.0
    extra_stage_usd: dict[Stage, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)
    extra_revisions: dict[Stage, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)

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
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    completed_tasks: list[str] = Field(default_factory=list)  # Developer tasks approved so far
    qa_loops: Annotated[int, Field(ge=0)] = 0  # times QA has sent the work back to the Developer

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
