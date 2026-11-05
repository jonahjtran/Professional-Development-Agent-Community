"""Documents that agents produce, and the rule that a stage reads only what it declared.

Files live under `runs/<slug>/artifacts/`. The state keeps a record of each (path, content hash,
revision, approval time), so a later stage can check it is reading exactly what the human approved.
"""

import hashlib
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath

from orchestrator.state import Artifact, ProjectState, Stage, atomic_write

# The approved artifacts each stage may read. Anything else is out of reach.
STAGE_INPUTS: dict[Stage, tuple[str, ...]] = {
    Stage.PRODUCT_OWNER: (),
    Stage.ARCHITECT: ("prd.json",),
}


class ArtifactStore:
    """Reads and writes one project's artifact files."""

    def __init__(self, runs_dir: Path, slug: str) -> None:
        self._project_dir = runs_dir / slug

    def write(self, name: str, text: str, *, revision: int) -> Artifact:
        path = self._relative_path(name)
        atomic_write(self._project_dir / path, text)
        return Artifact(path=path, content_hash=_hash(text), revision=revision)

    def read(self, artifact: Artifact) -> str:
        text = (self._project_dir / artifact.path).read_text()
        if _hash(text) != artifact.content_hash:
            raise ValueError(f"{artifact.path} changed since it was recorded (hash mismatch)")
        return text

    @staticmethod
    def _relative_path(name: str) -> str:
        parts = PurePosixPath(name).parts
        if not name or name.startswith("/") or "//" in name or ".." in parts:
            raise ValueError(f"unsafe artifact name: {name!r}")
        return str(PurePosixPath("artifacts", *parts))


def record_artifacts(state: ProjectState, new: Mapping[str, Artifact]) -> ProjectState:
    """Add or replace artifact records. A replaced artifact is a new draft, so it is unapproved."""
    return state.evolve(artifacts={**state.artifacts, **new})


def approve_artifacts(state: ProjectState, names: Iterable[str], *, now: datetime) -> ProjectState:
    """Stamp the named artifacts as approved by the human."""
    approved = dict(state.artifacts)
    for name in names:
        if name not in approved:
            raise ValueError(f"cannot approve {name}: no such artifact")
        approved[name] = approved[name].model_copy(update={"approved_at": now})
    return state.evolve(artifacts=approved)


def stage_inputs(state: ProjectState, stage: Stage, store: ArtifactStore) -> dict[str, str]:
    """The text of each approved artifact `stage` declares as an input, and nothing else."""
    inputs: dict[str, str] = {}
    for name in STAGE_INPUTS.get(stage, ()):
        artifact = state.artifacts.get(name)
        if artifact is None:
            raise ValueError(f"{stage.value} needs {name}, which does not exist yet")
        if artifact.approved_at is None:
            raise ValueError(f"{stage.value} needs {name}, which is not approved")
        inputs[name] = store.read(artifact)
    return inputs


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
