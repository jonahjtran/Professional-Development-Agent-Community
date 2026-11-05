import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.career_stage import (
    CareerError,
    CareerMaterials,
    GitCareerPreparer,
    gate_items,
    human_evidence,
    website_dir,
)
from orchestrator.gates import GateDecision, GateOutcome
from orchestrator.pipeline import AgentRequest, AgentResult, PipelineContext, advance
from orchestrator.stages import career_stage
from orchestrator.state import ProjectState, Stage, Status
from schemas import CareerPlan, EvidenceSet, EvidenceType, ResumeAction
from tests.factories import make_config, new_state
from tests.test_career_rules import change, entry, plan
from tests.test_career_stage import good_plan
from tests.test_pipeline import FakeCaps, FakeGates
from tests.test_product_owner_stage import prd_json

GOALS = yaml.safe_dump(
    {
        "tracks": {
            "strategy_consulting": {"target_roles": ["a"]},
            "tech_consulting": {"target_roles": ["b"]},
        }
    }
)
RESUME = yaml.safe_dump(
    {
        "sections": [
            {"name": "Projects", "max_bullets": 3, "entries": [{"title": "Old", "bullets": ["b"]}]}
        ]
    }
)


class FakeCareer:
    def __init__(self, website: str = "export const projects = [];", error: str | None = None):
        self.website = website
        self.error = error
        self.calls = 0

    def materials(self) -> CareerMaterials:
        self.calls += 1
        if self.error:
            raise CareerError(self.error)
        return CareerMaterials(RESUME, GOALS, self.website, "# stub readme")


class Coach:
    """Answers with a valid plan and records each request."""

    def __init__(self, plan_: CareerPlan | None = None) -> None:
        self.plan = plan_ or good_plan().model_copy(update={"questions": ["How many users?"]})
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(output=self.plan.model_dump_json(), cost_usd=0.3, prompt_version="v2")


def at_career(tmp_path: Path) -> ProjectState:
    store = ArtifactStore(tmp_path, "demo")
    files = {"prd.json": prd_json(), "architecture.md": "# Architecture"}
    state = record_artifacts(
        new_state(), {n: store.write(n, t, revision=1) for n, t in files.items()}
    )
    state = approve_artifacts(state, list(files), now=datetime(2026, 11, 5, tzinfo=UTC))
    return state.evolve(stage=Stage.CAREER_COACH)


def make_ctx(
    tmp_path: Path, coach: Coach, career: FakeCareer | None, gates: Any
) -> PipelineContext:
    return PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=coach,
        gates=gates,
        caps=FakeCaps(),
        stages={Stage.CAREER_COACH: career_stage()},
        career=career,
    )


def run_career(
    tmp_path: Path, coach: Coach, gates: Any, career: FakeCareer | None = None
) -> ProjectState:
    ctx = make_ctx(tmp_path, coach, career or FakeCareer(), gates)
    state = at_career(tmp_path)
    while state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)
    return state


def approve(*items: str, answers: dict[str, str] | None = None) -> GateDecision:
    return GateDecision(
        outcome=GateOutcome.APPROVE, approved_items=list(items), answers=answers or {}
    )


# --- gate items ------------------------------------------------------------------------------


def test_the_gate_offers_each_publishable_item_but_not_skipped_resume_changes() -> None:
    skipped = change(ResumeAction.SKIP, bullets=[])
    full = plan(change(ResumeAction.ADD), skipped, website=entry()).model_copy(
        update={"repo_description": "d", "repo_topics": ["agents"]}
    )

    ids = [i.id for i in gate_items(full, website=True)]

    assert ids == ["readme", "case_study", "resume:1", "website", "repo_meta"]


def test_there_is_no_website_item_without_a_website_or_an_entry() -> None:
    with_entry = plan(change(ResumeAction.SKIP, bullets=[]), website=entry())

    assert "website" not in [i.id for i in gate_items(with_entry, website=False)]
    assert "website" not in [i.id for i in gate_items(plan(), website=True)]


def test_answers_become_human_evidence_the_coach_can_cite() -> None:
    [item] = human_evidence([("How many users?", "40 students")])

    assert item.id == "human-1" and item.type == EvidenceType.HUMAN and item.ref == "you"
    assert "How many users?" in item.description and "40 students" in item.description


def test_the_website_folder_comes_from_the_environment() -> None:
    assert website_dir({"AGENT_STUDIO_WEBSITE": "~/site"}) == Path("~/site").expanduser()
    assert website_dir({}) is None


# --- preparing and running the stage ---------------------------------------------------------


def test_the_coach_is_given_every_input_it_declares(tmp_path: Path) -> None:
    coach = Coach()

    run_career(tmp_path, coach, FakeGates(approve("readme")))

    [request] = coach.requests
    assert set(request.inputs) == {
        "prd.json",
        "architecture.md",
        "evidence.json",
        "resume.yaml",
        "goals.yaml",
        "website_projects.js",
        "current_readme.md",
    }
    assert request.inputs["current_readme.md"] == "# stub readme"
    assert EvidenceSet.model_validate_json(request.inputs["evidence.json"]).get("cost")


def test_the_gate_lists_the_items_and_the_coachs_questions(tmp_path: Path) -> None:
    gates = FakeGates(approve("readme"))

    run_career(tmp_path, Coach(), gates)

    [request] = [r for r in gates.requests if r.stage == Stage.CAREER_COACH]
    assert [i.id for i in request.items] == ["readme", "case_study", "resume:1", "website"]
    assert request.questions == ["How many users?"]
    assert "# Career plan" in request.summary


def test_the_approved_items_are_recorded_and_the_project_finishes(tmp_path: Path) -> None:
    state = run_career(tmp_path, Coach(), FakeGates(approve("readme", "website")))

    assert state.status == Status.DONE
    store = ArtifactStore(tmp_path, "demo")
    recorded = json.loads(store.read(state.artifacts["career_approved.json"]))
    assert recorded == {"approved_items": ["readme", "website"]}


def test_approving_nothing_is_allowed_and_records_nothing_approved(tmp_path: Path) -> None:
    state = run_career(tmp_path, Coach(), FakeGates(approve()))

    store = ArtifactStore(tmp_path, "demo")
    assert json.loads(store.read(state.artifacts["career_approved.json"])) == {"approved_items": []}


def test_answers_given_at_the_gate_become_evidence_for_the_next_draft(tmp_path: Path) -> None:
    coach = Coach()
    gates = FakeGates(
        GateDecision(
            outcome=GateOutcome.REQUEST_CHANGES,
            comments="Use the number of users",
            answers={"How many users?": "40 students"},
        ),
        approve("readme"),
    )

    run_career(tmp_path, coach, gates)

    first, second = coach.requests
    assert EvidenceSet.model_validate_json(first.inputs["evidence.json"]).get("human-1") is None
    item = EvidenceSet.model_validate_json(second.inputs["evidence.json"]).get("human-1")
    assert item is not None and "40 students" in item.description
    assert second.comments == "Use the number of users"


def test_inputs_are_prepared_again_on_every_run(tmp_path: Path) -> None:
    career = FakeCareer()
    changes = GateDecision(outcome=GateOutcome.REQUEST_CHANGES, comments="again")

    state = run_career(tmp_path, Coach(), FakeGates(changes, approve()), career)

    assert career.calls == 2
    assert state.artifacts["evidence.json"].revision == 2


def test_a_project_without_a_website_is_offered_no_website_item(tmp_path: Path) -> None:
    gates = FakeGates(approve())

    run_career(tmp_path, Coach(), gates, FakeCareer(website=""))

    [request] = [r for r in gates.requests if r.stage == Stage.CAREER_COACH]
    assert "website" not in [i.id for i in request.items]


def test_a_failure_preparing_the_inputs_halts_without_calling_the_coach(tmp_path: Path) -> None:
    coach = Coach()

    state = run_career(tmp_path, coach, FakeGates(), FakeCareer(error="resume.yaml is missing"))

    assert state.status == Status.HALTED and coach.requests == []


def test_without_a_career_preparer_the_stage_behaves_as_before(tmp_path: Path) -> None:
    from orchestrator.echo_agent import EchoAgent

    ctx = PipelineContext(
        config=make_config(),
        runs_dir=tmp_path,
        runner=EchoAgent(),
        gates=FakeGates(),
        caps=FakeCaps(),
    )

    state = advance(at_career(tmp_path), ctx)

    assert "career_approved.json" not in state.artifacts


# --- the real preparer, with real git --------------------------------------------------------

GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]


def git(repo: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True)


def make_clone(root: Path, name: str, files: dict[str, str]) -> Path:
    seed = root / f"{name}-seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    for path, text in files.items():
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "x")
    origin = root / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = root / name
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    folder = tmp_path / "career-profile"
    folder.mkdir()
    (folder / "resume.yaml").write_text(RESUME)
    (folder / "goals.yaml").write_text(GOALS)
    return folder


def test_the_preparer_reads_the_profile_and_the_latest_remote_files(
    tmp_path: Path, profile: Path
) -> None:
    project = make_clone(tmp_path, "project", {"README.md": "# real readme"})
    site = make_clone(tmp_path, "site", {"src/data/projects.js": "export const projects = [];"})

    found = GitCareerPreparer(profile, project, site).materials()

    assert found.resume_yaml == RESUME and found.goals_yaml == GOALS
    assert found.website_projects_js == "export const projects = [];"
    assert found.current_readme == "# real readme"


def test_a_missing_readme_is_an_empty_string_but_a_missing_website_file_is_an_error(
    tmp_path: Path, profile: Path
) -> None:
    project = make_clone(tmp_path, "project", {"app.py": "x"})
    site = make_clone(tmp_path, "site", {"index.html": "x"})

    assert GitCareerPreparer(profile, project, None).materials().current_readme == ""
    with pytest.raises(CareerError, match="projects.js is not on main"):
        GitCareerPreparer(profile, project, site).materials()


def test_no_website_means_an_empty_projects_file(tmp_path: Path, profile: Path) -> None:
    project = make_clone(tmp_path, "project", {"README.md": "r"})

    assert GitCareerPreparer(profile, project, None).materials().website_projects_js == ""


def test_a_missing_profile_file_or_unfetchable_remote_is_a_career_error(
    tmp_path: Path, profile: Path
) -> None:
    project = make_clone(tmp_path, "project", {"README.md": "r"})
    (profile / "goals.yaml").unlink()
    with pytest.raises(CareerError, match="goals.yaml is missing"):
        GitCareerPreparer(profile, project, None).materials()

    (profile / "goals.yaml").write_text(GOALS)
    git(project, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(CareerError, match="could not fetch"):
        GitCareerPreparer(profile, project, None).materials()


def test_a_folder_that_is_not_a_repository_is_a_career_error(tmp_path: Path, profile: Path) -> None:
    with pytest.raises(CareerError, match="not a git repository"):
        GitCareerPreparer(profile, tmp_path, None).materials()
