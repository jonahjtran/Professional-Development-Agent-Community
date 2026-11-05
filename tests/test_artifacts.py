import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.artifacts import (
    STAGE_INPUTS,
    ArtifactStore,
    approve_artifacts,
    record_artifacts,
    stage_inputs,
)
from orchestrator.state import Artifact, ProjectState, Stage, load_state, save_state
from tests.factories import new_state

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- state -----------------------------------------------------------------------------------


def test_a_new_project_has_no_artifacts() -> None:
    assert new_state().artifacts == {}


def test_artifacts_survive_save_and_load(tmp_path: Path) -> None:
    artifact = Artifact(path="artifacts/prd.json", content_hash=sha("x"), revision=2)
    state = new_state().evolve(artifacts={"prd.json": artifact})

    save_state(tmp_path, state)

    assert load_state(tmp_path, "demo").artifacts["prd.json"] == artifact


def test_an_old_state_file_without_artifacts_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "demo" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"project_slug": "demo", "idea": "x"}')

    assert load_state(tmp_path, "demo").artifacts == {}


def test_an_artifact_starts_unapproved() -> None:
    assert Artifact(path="p", content_hash="h", revision=1).approved_at is None


# --- the store -------------------------------------------------------------------------------


def test_writing_an_artifact_saves_the_file_and_returns_its_record(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")

    artifact = store.write("prd.md", "# PRD", revision=1)

    assert (tmp_path / "demo" / artifact.path).read_text() == "# PRD"
    assert artifact.content_hash == sha("# PRD")
    assert artifact.revision == 1
    assert artifact.approved_at is None


def test_reading_an_artifact_returns_what_was_written(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    artifact = store.write("prd.md", "# PRD", revision=1)

    assert store.read(artifact) == "# PRD"


def test_a_changed_file_fails_the_hash_check_on_read(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    artifact = store.write("prd.md", "# PRD", revision=1)
    (tmp_path / "demo" / artifact.path).write_text("tampered")

    with pytest.raises(ValueError, match="hash"):
        store.read(artifact)


def test_nested_names_such_as_adrs_are_allowed(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path, "demo").write("adr/001-x.md", "text", revision=1)

    assert (tmp_path / "demo" / artifact.path).exists()


@pytest.mark.parametrize("name", ["../escape.md", "/abs.md", "a/../../b.md", "", "a//b.md"])
def test_unsafe_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        ArtifactStore(tmp_path, "demo").write(name, "x", revision=1)


def test_a_missing_file_raises_a_clear_error(tmp_path: Path) -> None:
    artifact = Artifact(path="artifacts/nope.md", content_hash=sha("x"), revision=1)

    with pytest.raises(FileNotFoundError):
        ArtifactStore(tmp_path, "demo").read(artifact)


# --- recording and approval ------------------------------------------------------------------


def test_recording_adds_artifacts_to_the_state_without_changing_the_original(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path, "demo")
    original = new_state()

    state = record_artifacts(original, {"prd.md": store.write("prd.md", "a", revision=1)})

    assert "prd.md" in state.artifacts
    assert original.artifacts == {}


def test_a_new_draft_replaces_the_record_and_clears_approval(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(new_state(), {"prd.md": store.write("prd.md", "v1", revision=1)})
    state = approve_artifacts(state, ["prd.md"], now=NOW)

    state = record_artifacts(state, {"prd.md": store.write("prd.md", "v2", revision=2)})

    assert state.artifacts["prd.md"].revision == 2
    assert state.artifacts["prd.md"].approved_at is None


def test_approving_stamps_the_time_on_the_named_artifacts_only(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(
        new_state(),
        {
            "prd.md": store.write("prd.md", "a", revision=1),
            "prd.json": store.write("prd.json", "{}", revision=1),
        },
    )

    state = approve_artifacts(state, ["prd.json"], now=NOW)

    assert state.artifacts["prd.json"].approved_at == NOW
    assert state.artifacts["prd.md"].approved_at is None


def test_approving_an_unknown_artifact_is_an_error() -> None:
    with pytest.raises(ValueError, match="prd.json"):
        approve_artifacts(new_state(), ["prd.json"], now=NOW)


# --- declared stage inputs -------------------------------------------------------------------


def approved_prd(tmp_path: Path) -> ProjectState:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(
        new_state(),
        {
            "prd.json": store.write("prd.json", '{"problem": "p"}', revision=1),
            "prd.md": store.write("prd.md", "# PRD", revision=1),
        },
    )
    return approve_artifacts(state, ["prd.json", "prd.md"], now=NOW)


def test_the_architect_declares_only_the_approved_prd_json() -> None:
    assert STAGE_INPUTS[Stage.ARCHITECT] == ("prd.json",)


def test_the_product_owner_needs_no_artifacts() -> None:
    assert stage_inputs(new_state(), Stage.PRODUCT_OWNER, ArtifactStore(Path("."), "demo")) == {}


def test_a_stage_gets_its_declared_inputs_and_nothing_else(tmp_path: Path) -> None:
    state = approved_prd(tmp_path)

    inputs = stage_inputs(state, Stage.ARCHITECT, ArtifactStore(tmp_path, "demo"))

    assert inputs == {"prd.json": '{"problem": "p"}'}


def test_an_unapproved_input_is_refused(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "demo")
    state = record_artifacts(new_state(), {"prd.json": store.write("prd.json", "{}", revision=1)})

    with pytest.raises(ValueError, match="approved"):
        stage_inputs(state, Stage.ARCHITECT, store)


def test_a_missing_input_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="prd.json"):
        stage_inputs(new_state(), Stage.ARCHITECT, ArtifactStore(tmp_path, "demo"))
