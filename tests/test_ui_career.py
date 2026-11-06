from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.artifacts import ArtifactStore, record_artifacts
from orchestrator.state import ProjectState, Stage, save_state
from orchestrator.ui import data
from orchestrator.ui.app import create_app
from schemas import CareerPlan, Evidence, EvidenceSet, EvidenceType, ResumeAction
from tests.factories import make_config
from tests.test_career_rules import bullet, change, entry
from tests.test_career_stage import good_plan
from tests.test_ui import make_project
from tests.test_ui_actions import GATE

EVIDENCE = EvidenceSet(
    items=[
        Evidence(id="qa", type=EvidenceType.QA, ref="r", description="9 of 9 checks passed"),
        Evidence(id="cost", type=EvidenceType.METRIC, ref="r", description="Spend was $4.20"),
    ]
)


def plan_with_amp() -> CareerPlan:
    add = change(
        ResumeAction.ADD,
        bullet("Passed 9 checks for M&A teams", ["qa", "ghost"]),
        replaces="Old project",
        tech="Python & SQL",
    )
    skip = change(ResumeAction.SKIP, bullets=[])
    website = entry(evidence_ids=["cost"], scope="Client work <b>x</b>")
    data_ = good_plan().model_dump(mode="json")
    data_["resume_changes"] = [add.model_dump(mode="json"), skip.model_dump(mode="json")]
    data_["website_entry"] = website.model_dump(mode="json")
    data_["readme_md"] = "# Project README\n\n<script>alert(1)</script>"
    data_["case_study_md"] = "# Case study"
    return CareerPlan.model_validate(data_)


def project_at_career(runs: Path, *, plan: bool = True, evidence: bool = True) -> ProjectState:
    store = ArtifactStore(runs, "demo")
    state = make_project(runs, stage=Stage.CAREER_COACH)
    written = {}
    if plan:
        written["career_plan.json"] = store.write(
            "career_plan.json", plan_with_amp().model_dump_json(), revision=1
        )
    if evidence:
        written["evidence.json"] = store.write(
            "evidence.json", EVIDENCE.model_dump_json(), revision=1
        )
    state = record_artifacts(state, written)
    save_state(runs, state)
    return state


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def client(runs: Path) -> TestClient:
    return TestClient(create_app(runs, make_config()))


CAREER_GATE = {**GATE, "stage": "career_coach", "title": "Review the career_coach output"}


# --- the data --------------------------------------------------------------------------------


def test_each_bullet_is_paired_with_the_evidence_it_cites_and_unknown_ids_are_dropped(
    runs: Path,
) -> None:
    wb = data.career_workbench(runs, project_at_career(runs))

    assert wb is not None
    [add, skip] = wb.changes
    [shown] = add.bullets
    assert shown.evidence == [("qa", "qa", "9 of 9 checks passed")]  # "ghost" is not shown
    assert skip.bullets == [] and skip.latex == ""


def test_a_resume_change_comes_with_paste_ready_latex(runs: Path) -> None:
    wb = data.career_workbench(runs, project_at_career(runs))

    assert wb is not None
    latex = wb.changes[0].latex
    assert "\\resumeProject" in latex and "M\\&A teams" in latex and "Python \\& SQL" in latex
    assert "% Remove first: Old project" in latex


def test_the_website_entry_comes_with_its_evidence(runs: Path) -> None:
    wb = data.career_workbench(runs, project_at_career(runs))

    assert wb is not None and wb.website_evidence == [("cost", "metric", "Spend was $4.20")]


def test_without_a_plan_or_evidence_there_is_no_workbench(runs: Path) -> None:
    assert data.career_workbench(runs, project_at_career(runs, plan=False)) is None
    assert data.career_workbench(runs, project_at_career(runs, evidence=False)) is None


def test_a_damaged_plan_is_not_a_workbench_and_not_an_error(runs: Path) -> None:
    state = project_at_career(runs)
    (runs / "demo" / state.artifacts["career_plan.json"].path).write_text("{broken")

    assert data.career_workbench(runs, state) is None
