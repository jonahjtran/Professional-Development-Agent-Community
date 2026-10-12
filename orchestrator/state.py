import contextlib
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ProjectState(BaseModel):
    """Everything needed to resume a project after a crash or Ctrl-C."""

    project_slug: Slug
    idea: str
    stage: Stage = Stage.INTAKE
    status: Status = Status.RUNNING
    current_task_id: str | None = None
    revisions: dict[Stage, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    intervention: Intervention | None = None

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

    def evolve(self, **changes: Any) -> Self:
        """Return a copy with `changes` applied, fully re-validated."""
        return type(self).model_validate({**dict(self), **changes})


def state_path(runs_dir: Path, slug: str) -> Path:
    return runs_dir / slug / "state.json"


def save_state(runs_dir: Path, state: ProjectState) -> None:
    """Write state.json atomically: write a temp file, then rename it into place.

    A crash mid-write leaves the previous state.json untouched.
    """
    path = state_path(runs_dir, state.project_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(state.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def load_state(runs_dir: Path, slug: str) -> ProjectState:
    return ProjectState.model_validate_json(state_path(runs_dir, slug).read_text())
