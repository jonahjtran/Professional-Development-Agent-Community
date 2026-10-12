from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.state import (
    Intervention,
    InterventionKind,
    ProjectState,
    Stage,
    Status,
    load_state,
    save_state,
    state_path,
)


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
    state = new_state().evolve(
        stage=Stage.ARCHITECT,
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=InterventionKind.GATE, detail="Approve the PRD"),
        current_task_id="T-3",
        revisions={Stage.PRODUCT_OWNER: 2},
    )

    save_state(tmp_path, state)
    loaded = load_state(tmp_path, "demo")

    assert loaded == state


def test_save_creates_the_run_directory(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())

    assert (tmp_path / "demo" / "state.json").is_file()


def test_save_overwrites_the_previous_state(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())
    save_state(tmp_path, new_state().evolve(stage=Stage.DEVELOPER))

    assert load_state(tmp_path, "demo").stage == Stage.DEVELOPER


def test_save_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    save_state(tmp_path, new_state())

    assert [p.name for p in (tmp_path / "demo").iterdir()] == ["state.json"]


def test_failed_save_keeps_the_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(tmp_path, new_state())
    state = new_state().evolve(stage=Stage.QA)

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


def awaiting(kind: InterventionKind = InterventionKind.BUDGET_CAP) -> ProjectState:
    return new_state().evolve(
        status=Status.AWAITING_HUMAN,
        intervention=Intervention(kind=kind, detail="Spent $16.00 of the $20.00 cap"),
    )


def test_new_project_has_no_intervention() -> None:
    assert new_state().intervention is None


def test_awaiting_human_requires_an_intervention() -> None:
    with pytest.raises(ValidationError):
        new_state().evolve(status=Status.AWAITING_HUMAN)


@pytest.mark.parametrize("status", [Status.RUNNING, Status.HALTED, Status.DONE])
def test_only_awaiting_human_may_carry_an_intervention(status: Status) -> None:
    intervention = Intervention(kind=InterventionKind.QA_FAILED, detail="2 loops used")

    with pytest.raises(ValidationError):
        new_state().evolve(status=status, intervention=intervention)


@pytest.mark.parametrize("kind", list(InterventionKind))
def test_every_intervention_kind_can_be_requested(kind: InterventionKind) -> None:
    state = awaiting(kind)

    assert state.status == Status.AWAITING_HUMAN
    assert state.intervention is not None
    assert state.intervention.kind == kind


def test_resolving_an_intervention_returns_the_project_to_running() -> None:
    state = awaiting().evolve(status=Status.RUNNING, intervention=None)

    assert state.status == Status.RUNNING
    assert state.intervention is None


def test_intervention_requires_a_non_empty_detail() -> None:
    with pytest.raises(ValidationError):
        Intervention(kind=InterventionKind.GATE, detail="")


def test_intervention_survives_save_and_load(tmp_path: Path) -> None:
    state = awaiting(InterventionKind.REVISION_CAP)

    save_state(tmp_path, state)

    assert load_state(tmp_path, "demo") == state


def test_loading_a_file_with_an_inconsistent_intervention_is_rejected(tmp_path: Path) -> None:
    path = state_path(tmp_path, "demo")
    path.parent.mkdir()
    path.write_text(
        '{"project_slug": "demo", "idea": "x", "status": "awaiting_human", "intervention": null}'
    )

    with pytest.raises(ValidationError):
        load_state(tmp_path, "demo")


def test_state_cannot_be_modified_in_place() -> None:
    state = new_state()

    with pytest.raises(ValidationError):
        state.stage = Stage.QA


def test_evolve_returns_a_new_state_and_leaves_the_original_unchanged() -> None:
    original = new_state()

    changed = original.evolve(stage=Stage.ARCHITECT)

    assert changed.stage == Stage.ARCHITECT
    assert original.stage == Stage.INTAKE


def test_evolve_validates_the_changed_values() -> None:
    with pytest.raises(ValidationError):
        new_state().evolve(project_slug="../escape")


def test_rejects_a_negative_stage_cost() -> None:
    with pytest.raises(ValidationError):
        new_state().evolve(cost_usd={Stage.QA: -0.5})


def test_total_cost_is_the_sum_of_the_stage_costs() -> None:
    state = new_state().evolve(cost_usd={Stage.PRODUCT_OWNER: 0.75, Stage.ARCHITECT: 1.25})

    assert state.total_cost_usd == pytest.approx(2.0)
