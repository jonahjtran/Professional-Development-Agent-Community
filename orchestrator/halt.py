"""The kill switch: `studio halt <project>` drops a flag file that the running pipeline checks.

A flag file works across processes with no server: the person at another terminal (or, later, a
UI) writes it, and the pipeline sees it before its next step. The Developer's tool hook checks it
before every tool call, so a running agent stops within one step.
"""

import re
from pathlib import Path

_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def halt_path(runs_dir: Path, project: str) -> Path:
    if not _PROJECT_NAME.match(project):
        raise ValueError(f"unsafe project name: {project!r}")
    return runs_dir / project / "HALT"


def request_halt(runs_dir: Path, project: str) -> None:
    path = halt_path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halt requested\n")


def halt_requested(runs_dir: Path, project: str) -> bool:
    return halt_path(runs_dir, project).exists()


def clear_halt(runs_dir: Path, project: str) -> None:
    halt_path(runs_dir, project).unlink(missing_ok=True)
