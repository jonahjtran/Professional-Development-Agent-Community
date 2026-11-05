from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.artifacts import ArtifactStore, record_artifacts
from orchestrator.inbox import PendingKind
from orchestrator.state import ProjectState, Stage, save_state
from orchestrator.ui import data
from orchestrator.ui.app import create_app
from schemas import CareerPlan, Evidence, EvidenceSet, EvidenceType, ResumeAction
from tests.factories import make_config
from tests.test_career_rules import bullet, change, entry
from tests.test_career_stage import good_plan
from tests.test_ui import make_project
from tests.test_ui_actions import GATE, write_pending

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


# --- the page --------------------------------------------------------------------------------


def test_the_career_gate_shows_each_claim_beside_its_evidence(
    client: TestClient, runs: Path
) -> None:
    project_at_career(runs)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    page = client.get(f"/p/demo/q/{qid}").text

    assert "Fit with your goals" in page and "strategy consulting" in page
    assert "Passed 9 checks for M&amp;A teams" in page
    assert "9 of 9 checks passed" in page and "Replaces: Old project" in page
    assert "LaTeX for Overleaf" in page and 'class="copy"' in page


def test_the_website_entry_is_previewed_and_its_text_is_escaped(
    client: TestClient, runs: Path
) -> None:
    project_at_career(runs)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    page = client.get(f"/p/demo/q/{qid}").text

    assert "Agent Studio" in page and "Merging it is what puts this live" in page
    assert "Client work &lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert "Spend was $4.20" in page


def test_the_readme_and_case_study_are_shown_with_raw_html_escaped(
    client: TestClient, runs: Path
) -> None:
    project_at_career(runs)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    page = client.get(f"/p/demo/q/{qid}").text

    assert "<h1>Project README</h1>" in page and "<h1>Case study</h1>" in page
    assert "<script>alert" not in page


def test_the_items_questions_and_decision_buttons_are_still_there(
    client: TestClient, runs: Path
) -> None:
    project_at_career(runs)
    qid = write_pending(runs, PendingKind.GATE, CAREER_GATE)

    page = client.get(f"/p/demo/q/{qid}").text

    assert "Replace README" in page and "How many users?" in page
    assert "Approve</button>" in page and "Request changes" in page


def test_other_gates_and_a_career_gate_without_a_plan_use_the_plain_view(
    client: TestClient, runs: Path
) -> None:
    make_project(runs, stage=Stage.QA)
    qa = write_pending(runs, PendingKind.GATE, GATE)

    plain = client.get(f"/p/demo/q/{qa}").text

    assert "Fit with your goals" not in plain and "<h1>Report</h1>" in plain

    make_project(runs, stage=Stage.CAREER_COACH)  # no plan stored
    career = write_pending(runs, PendingKind.GATE, CAREER_GATE, slug="demo")
    assert "Fit with your goals" not in client.get(f"/p/demo/q/{career}").text


def test_the_copy_script_reads_the_snippet_from_the_page() -> None:
    script = (Path(data.__file__).parent / "static" / "notify.js").read_text()

    assert "pre.latex" in script and "clipboard.writeText" in script
