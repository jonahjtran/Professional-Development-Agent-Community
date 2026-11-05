"""Live QA test: the real QA agent and real tools judge a tiny seeded project.

Costs a few cents (the fast model reads a handful of small files) and needs ANTHROPIC_API_KEY and
`uv` on the PATH. It makes no GitHub calls: the "remote" is a local bare repository and the pull
requests are treated as merged. Run it with:

    ANTHROPIC_API_KEY=... uv run pytest tests/live --live -s -k qa
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.config import CheckCommand, QaConfig, load_config
from orchestrator.default_specs import default_api_schemas, default_api_specs, default_stage_specs
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.pipeline import PipelineContext, advance
from orchestrator.qa_prepare import GitQaPreparer
from orchestrator.state import ProjectState, Stage, Status
from schemas import CheckStatus, QaReport, TaskList, Verdict

pytestmark = pytest.mark.live

CONFIG = Path(__file__).parent.parent.parent / "config.yaml"
QA_BUDGET_USD = 0.5

TASKS = {
    "tasks": [
        {
            "id": "T1",
            "title": "Greeting and doubling",
            "description": "Two small functions.",
            "acceptance_criteria": [
                "greet(name) returns 'Hello, <name>!'",
                "double(n) returns n multiplied by 2",
            ],
            "depends_on": [],
            "size": "S",
        }
    ]
}

PYPROJECT = '[tool.pytest.ini_options]\npythonpath = ["."]\n'
APP = "def greet(name):\n    return f'Hello, {name}!'\n\n\ndef double(n):\n    return n * 3\n"
TEST_GREET = (
    "from app import greet\n\n\ndef test_greet():\n    assert greet('Ada') == 'Hello, Ada!'\n"
)
TEST_DOUBLE = "from app import double\n\n\ndef test_double():\n    assert double(4) == 8\n"
FIXED_APP = APP.replace("n * 3", "n * 2")


class Human:
    def __init__(self) -> None:
        self.requests: list[GateRequest] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.requests.append(request)
        return GateDecision(outcome=GateOutcome.APPROVE)


class NoCaps:
    def resolve(self, request: object) -> object:
        raise AssertionError("a cap was hit during the QA live test")


class AllMerged:
    def is_merged(self, pr_url: str) -> bool:
        return True


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make_remote_project(root: Path, files: dict[str, str]) -> Path:
    """A local 'GitHub' holding the files on main, and a clone of it."""
    seed = root / "seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    for name, text in files.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "project")
    origin = root / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


def run_qa(tmp_path: Path, files: dict[str, str]) -> tuple[QaReport, ProjectState, Human]:
    config = load_config(CONFIG)
    qa_config = QaConfig(
        timeout_seconds=240,
        tail_lines=30,
        checks=[
            CheckCommand(
                name="tests", command="uv run --no-project --with pytest pytest -q", requires="uv"
            ),
            CheckCommand(
                name="lint", command="uv run --no-project --with ruff ruff check .", requires="uv"
            ),
        ],
    )
    clone = make_remote_project(tmp_path / "remote", files)
    runs = tmp_path / "runs"
    store = ArtifactStore(runs, "qa-live")
    tasks_text = TaskList.model_validate(TASKS).model_dump_json()
    state = record_artifacts(
        ProjectState(project_slug="qa-live", idea="Tiny functions", stage=Stage.QA),
        {"tasks.json": store.write("tasks.json", tasks_text, revision=1)},
    )
    state = approve_artifacts(state, ["tasks.json"], now=datetime.now(UTC))
    human = Human()
    ctx = PipelineContext(
        config=config,
        runs_dir=runs,
        runner=ValidatingRunner(ApiRunner(config, default_api_specs()), default_api_schemas()),
        gates=human,
        caps=NoCaps(),  # type: ignore[arg-type]
        stages=default_stage_specs(),
        qa=GitQaPreparer(clone, AllMerged(), qa_config),
    )
    while state.stage == Stage.QA and state.status == Status.RUNNING:
        state = advance(state, ctx)
    report = QaReport.model_validate_json(store.read(state.artifacts["test_report.json"]))
    print(f"\nQA cost ${state.cost_usd.get(Stage.QA, 0):.4f}\n{human.requests[-1].summary}")
    return report, state, human


def verdicts(report: QaReport) -> dict[str, Verdict]:
    return {c.criterion.split(" ")[0]: c.verdict for c in report.criteria}


def test_a_seeded_bug_and_an_uncovered_criterion_fail_the_report(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": PYPROJECT,
        "app.py": APP,
        "tests/test_greet.py": TEST_GREET,
        "tests/test_double.py": TEST_DOUBLE,
    }

    report, state, human = run_qa(tmp_path, files)

    by_name = {c.name: c.status for c in report.checks}
    assert by_name["tests"] == CheckStatus.FAILED  # double(4) returns 12, the test expects 8
    assert not report.passed
    assert "QA FAILED" in human.requests[-1].summary  # the human is told at the gate
    assert state.cost_usd.get(Stage.QA, 0) < QA_BUDGET_USD
    assert verdicts(report)["double(n)"] in (Verdict.FAIL, Verdict.UNTESTED)


def test_a_criterion_with_no_test_is_untested_and_a_clean_project_passes(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": PYPROJECT,
        "app.py": FIXED_APP,
        "tests/test_greet.py": TEST_GREET,
    }  # nothing covers double()

    report, _state, _ = run_qa(tmp_path, files)

    assert all(c.status == CheckStatus.PASSED for c in report.checks)
    seen = verdicts(report)
    assert seen["greet(name)"] == Verdict.PASS
    assert seen["double(n)"] == Verdict.UNTESTED
    assert report.passed  # untested is shown but does not fail the report
