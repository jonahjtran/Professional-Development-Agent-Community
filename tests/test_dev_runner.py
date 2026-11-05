import json
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from orchestrator.dev_runner import DevRunner
from orchestrator.pipeline import AgentError, AgentRequest, AgentRunner
from orchestrator.prompts import Prompt
from orchestrator.tool_hooks import ToolAnswer, ToolRequest
from orchestrator.worktree import SELF_PROTECTED
from schemas import DevResult
from tests.factories import make_config
from tests.test_schemas import task_data

GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]
REPORT = json.dumps(
    {
        "task_id": "T1",
        "summary": "Added the events table.",
        "deviations_from_plan": [],
        "known_gaps": ["No index yet"],
    }
)


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 3,
        "session_id": "s",
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "result": REPORT,
    }
    return ResultMessage(**{**fields, **overrides})


def commit_a_file(cwd: Path) -> None:
    (cwd / "events.py").write_text("TABLE = 'events'\n")
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", "add events table")


class FakeClient:
    """Stands in for ClaudeSDKClient. Each turn runs an 'agent' that really edits the worktree."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        turns: list[Callable[[Path], list[Any]]],
        prompts: list[str],
    ) -> None:
        self.options = options
        self._turns = turns
        self._prompts = prompts
        self._pending: list[Any] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self._prompts.append(prompt)
        assert self.options.cwd is not None
        turn = self._turns.pop(0)
        self._pending = turn(Path(self.options.cwd))

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._pending:
            yield message


class Harness:
    def __init__(self, repo: Path, tmp_path: Path, *turns: Callable[[Path], list[Any]]) -> None:
        self.turns = list(turns) or [self.good_turn]
        self.prompts: list[str] = []
        self.options: list[ClaudeAgentOptions] = []
        self.answers: list[ToolAnswer] = []
        self.repo = repo
        self.tmp_path = tmp_path

    @staticmethod
    def good_turn_two(cwd: Path) -> list[Any]:
        (cwd / "more.py").write_text("X = 2\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", "revise")
        return [result_message()]

    @staticmethod
    def good_turn(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        return [
            AssistantMessage(content=[TextBlock(text="working")], model="claude-opus-5-5"),
            result_message(),
        ]

    def factory(self, options: ClaudeAgentOptions) -> FakeClient:
        self.options.append(options)
        return FakeClient(options, self.turns, self.prompts)

    def ask(self, request: ToolRequest) -> ToolAnswer:
        return self.answers.pop(0) if self.answers else ToolAnswer(allow=False)

    def runner(self, *, studio_root: Path | None = None) -> DevRunner:
        return DevRunner(
            make_config(),
            self.tmp_path / "runs",
            self.repo,
            self,
            Prompt(name="developer", version="v3", text="You are the Developer."),
            studio_root=studio_root or self.tmp_path / "not-the-repo",
            client_factory=self.factory,
        )


def request(**overrides: Any) -> AgentRequest:
    tasks = {"tasks": [task_data(), task_data(id="T2", title="Second", depends_on=["T1"])]}
    fields: dict[str, Any] = {
        "stage": "developer",
        "project": "demo",
        "idea": "Track events",
        "task_id": "T1",
        "inputs": {"tasks.json": json.dumps(tasks), "architecture.md": "# Architecture\nFastAPI"},
    }
    return AgentRequest(**{**fields, **overrides})


def test_the_dev_runner_is_an_agent_runner(repo: Path, tmp_path: Path) -> None:
    runner: AgentRunner = Harness(repo, tmp_path).runner()

    assert callable(runner.run)


# --- the options handed to the SDK ------------------------------------------------------------


def test_the_agent_is_confined_to_its_worktree_with_a_strict_setup(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    harness = Harness(repo, tmp_path)

    harness.runner().run(request())

    options = harness.options[0]
    assert Path(str(options.cwd)) == tmp_path / "project-worktrees" / "T1"
    assert options.model == "strong-model-id"
    assert options.max_turns == 40
    assert options.max_budget_usd == 2.0
    assert options.setting_sources == []
    assert options.tools == ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
    assert options.allowed_tools == options.tools
    assert options.system_prompt == "You are the Developer."
    assert options.sandbox is not None
    assert options.sandbox["enabled"] is True
    assert options.sandbox["excludedCommands"] == ["git"]
    assert options.can_use_tool is not None
    assert options.hooks is not None and "PreToolUse" in options.hooks


def test_secrets_are_hidden_from_the_agents_shell(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("MY_SERVICE_PASSWORD", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("HARMLESS_SETTING", "1")
    harness = Harness(repo, tmp_path)

    harness.runner().run(request())

    env = harness.options[0].env
    assert env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert env["GITHUB_TOKEN"] == ""
    assert env["MY_SERVICE_PASSWORD"] == ""
    assert "ANTHROPIC_API_KEY" not in env  # blanking it would break the agent's own login
    assert "HARMLESS_SETTING" not in env


def test_working_on_the_studio_repo_itself_protects_its_control_files(
    repo: Path, tmp_path: Path
) -> None:
    import asyncio

    harness = Harness(repo, tmp_path)
    harness.runner(studio_root=repo).run(request())
    assert harness.options[0].hooks is not None
    hook: Any = harness.options[0].hooks["PreToolUse"][0].hooks[0]
    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "CLAUDE.md", "old_string": "a", "new_string": "b"},
        "tool_use_id": "t",
    }

    output: Any = asyncio.run(hook(data, "t", {"signal": None}))

    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "CLAUDE.md" in SELF_PROTECTED


# --- the task prompt -------------------------------------------------------------------------


def test_the_agent_is_told_the_task_the_branch_and_the_rules(repo: Path, tmp_path: Path) -> None:
    harness = Harness(repo, tmp_path)

    harness.runner().run(request())

    prompt = harness.prompts[0]
    assert "Create the events table" in prompt
    assert "Table exists after migrate" in prompt  # acceptance criteria
    assert "agent/T1" in prompt
    assert "FastAPI" in prompt  # the architecture
    assert "Second" not in prompt  # only this task, not the whole list
    assert "do not push" in prompt.lower()


def test_a_revision_includes_the_comments_and_the_previous_summary(
    repo: Path, tmp_path: Path
) -> None:
    harness = Harness(repo, tmp_path)

    harness.runner().run(request(comments="Add a docstring", previous_draft=REPORT))

    assert "Add a docstring" in harness.prompts[0]
    assert "Added the events table." in harness.prompts[0]


def test_a_revision_continues_on_the_existing_branch(repo: Path, tmp_path: Path) -> None:
    harness = Harness(repo, tmp_path, Harness.good_turn, Harness.good_turn_two)
    runner = harness.runner()
    runner.run(request())

    runner.run(request(comments="more", previous_draft=REPORT))

    assert git(repo, "rev-list", "--count", "main..agent/T1") == "2"


# --- what comes back -------------------------------------------------------------------------


def test_the_result_is_a_dev_result_for_this_branch_with_cost_and_tokens(
    repo: Path, tmp_path: Path
) -> None:
    result = Harness(repo, tmp_path).runner().run(request())

    dev = DevResult.model_validate_json(result.output)
    assert (dev.task_id, dev.branch, dev.pr_url) == ("T1", "agent/T1", None)
    assert dev.summary == "Added the events table."
    assert dev.known_gaps == ["No index yet"]
    assert result.cost_usd == pytest.approx(0.5)
    assert (result.tokens_in, result.tokens_out) == (100, 50)
    assert result.model == "claude-opus-5-5"
    assert result.prompt_version == "v3"


def test_the_branch_keeps_the_work_and_the_worktree_is_cleaned_up(
    repo: Path, tmp_path: Path
) -> None:
    Harness(repo, tmp_path).runner().run(request())

    assert git(repo, "rev-list", "--count", "main..agent/T1") == "1"
    assert git(repo, "show", "agent/T1:events.py") == "TABLE = 'events'"
    assert not (tmp_path / "project-worktrees" / "T1").exists()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_report_wrapped_in_a_code_fence_is_accepted(repo: Path, tmp_path: Path) -> None:
    def fenced(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        return [result_message(result=f"Done!\n```json\n{REPORT}\n```")]

    result = Harness(repo, tmp_path, fenced).runner().run(request())

    assert DevResult.model_validate_json(result.output).task_id == "T1"


# --- the orchestrator checks the agent's work itself ------------------------------------------


def test_no_commits_is_an_error_even_if_the_agent_says_it_is_done(
    repo: Path, tmp_path: Path
) -> None:
    def lazy(cwd: Path) -> list[Any]:
        return [result_message()]

    with pytest.raises(AgentError, match="no commits") as error:
        Harness(repo, tmp_path, lazy).runner().run(request())

    assert error.value.cost_usd == pytest.approx(0.5)  # the call still cost money
    assert not (tmp_path / "project-worktrees" / "T1").exists()


def test_uncommitted_changes_are_an_error_because_they_would_be_lost(
    repo: Path, tmp_path: Path
) -> None:
    def messy(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        (cwd / "forgotten.py").write_text("x = 1\n")
        return [result_message()]

    with pytest.raises(AgentError, match="uncommitted"):
        Harness(repo, tmp_path, messy).runner().run(request())


def test_leaving_the_task_branch_is_an_error(repo: Path, tmp_path: Path) -> None:
    def wanderer(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        git(cwd, "checkout", "-q", "--detach")  # off the task branch, with no new branch made
        return [result_message()]

    with pytest.raises(AgentError, match="left its branch"):
        Harness(repo, tmp_path, wanderer).runner().run(request())


def test_creating_extra_branches_is_an_error_and_they_are_removed(
    repo: Path, tmp_path: Path
) -> None:
    def brancher(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        git(cwd, "branch", "side-quest")  # stays on the task branch
        return [result_message()]

    with pytest.raises(AgentError, match="extra branches: side-quest"):
        Harness(repo, tmp_path, brancher).runner().run(request())

    assert "side-quest" not in git(repo, "branch", "--format=%(refname:short)").split()


def test_a_commit_on_main_is_an_error(repo: Path, tmp_path: Path) -> None:
    def rogue(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        git(repo, "update-ref", "refs/heads/main", "agent/T1")
        return [result_message()]

    with pytest.raises(AgentError, match="main"):
        Harness(repo, tmp_path, rogue).runner().run(request())


# --- the report and failures -----------------------------------------------------------------


def test_an_invalid_report_gets_one_follow_up_in_the_same_session(
    repo: Path, tmp_path: Path
) -> None:
    def bad_then_good(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        return [result_message(result="all done, no json", total_cost_usd=0.4)]

    def fixed(cwd: Path) -> list[Any]:
        # The SDK reports a running total for the session, so the second result already includes
        # the first turn's cost.
        return [result_message(total_cost_usd=0.5)]

    harness = Harness(repo, tmp_path, bad_then_good, fixed)

    result = harness.runner().run(request())

    assert len(harness.prompts) == 2
    assert "valid" in harness.prompts[1].lower()
    assert result.cost_usd == pytest.approx(0.5)  # the session total, not 0.4 + 0.5
    assert len(harness.options) == 1  # one session, not a second agent run


def test_two_invalid_reports_fail_with_the_total_cost(repo: Path, tmp_path: Path) -> None:
    totals = iter([0.4, 0.8])

    def bad(cwd: Path) -> list[Any]:
        if not (cwd / "events.py").exists():
            commit_a_file(cwd)
        return [result_message(result="nope", total_cost_usd=next(totals))]

    with pytest.raises(AgentError, match="report") as error:
        Harness(repo, tmp_path, bad, bad).runner().run(request())

    assert error.value.cost_usd == pytest.approx(0.8)


def test_hitting_the_budget_is_an_error_that_still_bills_the_cost(
    repo: Path, tmp_path: Path
) -> None:
    def broke(cwd: Path) -> list[Any]:
        return [result_message(subtype="error_max_budget_usd", is_error=True, total_cost_usd=2.01)]

    with pytest.raises(AgentError, match="error_max_budget_usd") as error:
        Harness(repo, tmp_path, broke).runner().run(request())

    assert error.value.cost_usd == pytest.approx(2.01)


def test_a_missing_cost_is_an_error(repo: Path, tmp_path: Path) -> None:
    def free(cwd: Path) -> list[Any]:
        commit_a_file(cwd)
        return [result_message(total_cost_usd=None)]

    with pytest.raises(AgentError, match="cost"):
        Harness(repo, tmp_path, free).runner().run(request())


def test_an_unknown_task_is_an_error_before_anything_is_created(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="T9"):
        Harness(repo, tmp_path).runner().run(request(task_id="T9"))

    assert not (tmp_path / "project-worktrees").exists()


def test_a_request_with_no_task_is_an_error(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="needs a task id"):
        Harness(repo, tmp_path).runner().run(request(task_id=None))


def test_a_crash_in_the_session_still_removes_the_worktree(repo: Path, tmp_path: Path) -> None:
    def crash(cwd: Path) -> list[Any]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        Harness(repo, tmp_path, crash).runner().run(request())

    assert not (tmp_path / "project-worktrees" / "T1").exists()


def test_a_worktree_problem_becomes_an_agent_error(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "agent/T1")  # a leftover branch from an earlier, unfinished run

    with pytest.raises(AgentError, match="worktree"):
        Harness(repo, tmp_path).runner().run(request())


def test_nothing_in_the_environment_is_changed_for_the_orchestrator_itself(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

    Harness(repo, tmp_path).runner().run(request())

    assert os.environ["GITHUB_TOKEN"] == "ghp_fake"
