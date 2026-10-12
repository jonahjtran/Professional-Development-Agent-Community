import contextlib
import os
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Stage(StrEnum):
    INTAKE = "intake"
    PRODUCT_OWNER = "product_owner"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    CAREER_COACH = "career_coach"


class Status(StrEnum):
    RUNNING = "running"
    AWAITING_GATE = "awaiting_gate"
    HALTED = "halted"
    DONE = "done"


# Slugs become directory names, so they must not be able to escape runs/.
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class ProjectState(BaseModel):
    """Everything needed to resume a project after a crash or Ctrl-C."""

    project_slug: Slug
    idea: str
    stage: Stage = Stage.INTAKE
    status: Status = Status.RUNNING
    current_task_id: str | None = None
    revisions: dict[Stage, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


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
