from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.state import ProjectState, Stage, Status, load_state, save_state, state_path


def new_state(slug: str = "demo") -> ProjectState:
    return ProjectState(project_slug=slug, idea="A tool that tracks student org events.")


def test_new_project_starts_at_intake_and_running() -> None:
    state = new_state()

    assert state.stage == Stage.INTAKE
    assert state.status == Status.RUNNING
    assert state.current_task_id is None
    assert state.revisions == {}


def test_state_path_is_inside_the_project_run_directory(tmp_path: Path) -> None:
    assert state_path(tmp_path, "demo") == tmp_path / "demo" / "state.json"


@pytest.mark.parametrize("bad_slug", ["", "../escape", "has/slash", "Has Caps", "-leading-dash"])
def test_rejects_unsafe_project_slugs(bad_slug: str) -> None:
    with pytest.raises(ValidationError):
        new_state(bad_slug)


def test_rejects_a_negative_revision_count() -> None:
    with pytest.raises(ValidationError):
        ProjectState(project_slug="demo", idea="x", revisions={Stage.PRODUCT_OWNER: -1})


def test_save_then_load_round_trips_the_state(tmp_path: Path) -> None:
    state = new_state()
    state.stage = Stage.ARCHITECT
    state.status = Status.AWAITING_GATE
    state.current_task_id = "T-3"
    state.revisions = {Stage.PRODUCT_OWNER: 2}

    save_state(tmp_path, state)
    loaded = load_state(tmp_path, "demo")

    assert loaded == state


def test_save_creates_the_run_directory(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())

    assert (tmp_path / "demo" / "state.json").is_file()


def test_save_overwrites_the_previous_state(tmp_path: Path) -> None:
    state = new_state()
    save_state(tmp_path, state)
    state.stage = Stage.DEVELOPER
    save_state(tmp_path, state)

    assert load_state(tmp_path, "demo").stage == Stage.DEVELOPER


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())

    assert [p.name for p in (tmp_path / "demo").iterdir()] == ["state.json"]


def test_failed_save_keeps_the_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = new_state()
    save_state(tmp_path, state)
    state.stage = Stage.QA

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("orchestrator.state.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        save_state(tmp_path, state)
    monkeypatch.undo()

    assert load_state(tmp_path, "demo").stage == Stage.INTAKE
    assert [p.name for p in (tmp_path / "demo").iterdir()] == ["state.json"]


def test_load_missing_state_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path, "demo")


def test_load_corrupt_state_raises_validation_error(tmp_path: Path) -> None:
    path = state_path(tmp_path, "demo")
    path.parent.mkdir()
    path.write_text('{"project_slug": "demo"')

    with pytest.raises(ValidationError):
        load_state(tmp_path, "demo")
