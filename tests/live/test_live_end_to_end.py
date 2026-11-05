"""End to end on a real, throwaway repository. This is the full new-project flow:

  studio new  ->  PRD  ->  architecture  ->  design record PR  ->  one Developer task and its PR

It creates a PRIVATE repository on your GitHub account (through your own `gh` login), runs the real
agents (a few dollars at most), stops after the first task is approved, then closes the pull
requests, deletes the repository and removes the local clone. If your `gh` login lacks permission to
delete repositories, the repository is left behind and the test says how to remove it.

    AGENT_STUDIO_LIVE_E2E=1 uv run --env-file .env pytest tests/live --live -s -k end_to_end
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator.api_runner import ApiRunner, ValidatingRunner
from orchestrator.caps import CapRequest, CapResolution
from orchestrator.config import load_config
from orchestrator.default_specs import default_api_schemas, default_api_specs, default_stage_specs
from orchestrator.dev_runner import DevRunner
from orchestrator.gates import GateDecision, GateOutcome, GateRequest
from orchestrator.halt import request_halt
from orchestrator.newproject import create_project, run_with_own_login
from orchestrator.pipeline import PipelineContext, RoutingRunner, run_pipeline
from orchestrator.prompts import load_prompt
from orchestrator.publish import Publisher, load_token, strip_credentials
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.tool_hooks import ToolAnswer, ToolRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGENT_STUDIO_LIVE_E2E") != "1",
        reason="creates and deletes a real repository: set AGENT_STUDIO_LIVE_E2E=1",
    ),
]

ROOT = Path(__file__).parent.parent.parent
IDEA = "A tiny command-line tool that prints a random programming joke from a built-in list."
MAX_SPEND_USD = 6.0


class Human:
    """Approves the PRD, the architecture and the first task, then hits the halt switch."""

    def __init__(self, runs_dir: Path, slug: str) -> None:
        self._runs_dir, self._slug = runs_dir, slug
        self.gates: list[Stage] = []

    def decide(self, request: GateRequest) -> GateDecision:
        self.gates.append(request.stage)
        if request.stage == Stage.DEVELOPER:
            request_halt(self._runs_dir, self._slug)  # stop before the second task starts
        return GateDecision(outcome=GateOutcome.APPROVE)

    def ask(self, request: ToolRequest) -> ToolAnswer:
        return ToolAnswer(allow=False, message="No one can approve this now. Work without it.")

    def resolve(self, request: CapRequest) -> CapResolution:
        raise AssertionError("a budget or revision cap was hit during the end-to-end test")


def _pr_url(runs_dir: Path, state: ProjectState, artifact: str) -> str:
    path = runs_dir / state.project_slug / state.artifacts[artifact].path
    url = json.loads(path.read_text())["pr_url"]
    assert isinstance(url, str)
    return url


def _gh(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run_with_own_login(["gh", *args], cwd)


def test_a_new_project_goes_from_idea_to_a_reviewed_first_task_end_to_end(tmp_path: Path) -> None:
    name = f"agent-studio-e2e-{int(time.time())}"
    projects = tmp_path / "projects"
    runs_dir = tmp_path / "runs"
    config = load_config(ROOT / "config.yaml")
    created = create_project(name, projects, confirm=lambda question: True)
    slug = name
    try:
        human = Human(runs_dir, slug)
        planner = ValidatingRunner(ApiRunner(config, default_api_specs()), default_api_schemas())
        developer = DevRunner(config, runs_dir, created.path, human, load_prompt("developer"))
        ctx = PipelineContext(
            config=config,
            runs_dir=runs_dir,
            runner=RoutingRunner(
                {Stage.PRODUCT_OWNER: planner, Stage.ARCHITECT: planner, Stage.DEVELOPER: developer}
            ),
            gates=human,
            caps=human,
            stages=default_stage_specs(),
            publisher=Publisher(created.path, load_token(), expected_url=created.url),
            repo=created.path,
        )
        state = ProjectState(
            project_slug=slug, idea=IDEA, repo_path=str(created.path), repo_url=created.url
        )

        state = run_pipeline(state, ctx)

        assert state.status == Status.HALTED  # by the halt switch, after the first task
        assert state.completed_tasks, "the first Developer task should have been approved"
        assert human.gates[:3] == [Stage.PRODUCT_OWNER, Stage.ARCHITECT, Stage.DEVELOPER]
        design_pr = _pr_url(runs_dir, state, "design_record.json")
        task_pr = _pr_url(runs_dir, state, f"dev/{state.completed_tasks[0]}.json")
        assert design_pr.startswith("https://github.com/")
        assert task_pr.startswith("https://github.com/")
        assert design_pr != task_pr
        assert state.total_cost_usd < MAX_SPEND_USD
        print(f"\nend to end: ${state.total_cost_usd:.2f}")
        print(f"  design record: {design_pr}\n  first task: {task_pr}")
    finally:
        repo_name = f"{created.owner}/{created.name}"
        for pr in _gh(
            "pr", "list", "--repo", repo_name, "--json", "url", "-q", ".[].url", cwd=ROOT
        ).stdout.split():
            _gh("pr", "close", pr, "--delete-branch", cwd=ROOT)
        deleted = _gh("repo", "delete", repo_name, "--yes", cwd=ROOT)
        if deleted.returncode != 0:
            print(
                f"\nCould not delete {repo_name} ({deleted.stderr.strip()}). Delete it by hand: "
                "GitHub, the repository, Settings, Danger Zone, or run "
                "`gh auth refresh -s delete_repo` and then `gh repo delete "
                f"{repo_name} --yes`."
            )
        print(f"origin was {strip_credentials(created.url)}")
