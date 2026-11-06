import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import typer
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from typer.testing import CliRunner

from orchestrator.cli_tool import CliToolBackend
from orchestrator.permissions import PolicyContext
from orchestrator.state import Stage
from orchestrator.tool_hooks import (
    ToolAnswer,
    ToolPermissionBackend,
    ToolPermissionHooks,
    ToolRequest,
    git_current_branch,
)
from orchestrator.trace import trace_path


class ScriptedBackend:
    def __init__(self, *answers: ToolAnswer) -> None:
        self.answers = list(answers)
        self.requests: list[ToolRequest] = []

    def ask(self, request: ToolRequest) -> ToolAnswer:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else ToolAnswer(allow=False)


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    return root


def make_hooks(
    tmp_path: Path, wt: Path, backend: ToolPermissionBackend | None = None, branch: str = "agent/t1"
) -> ToolPermissionHooks:
    return ToolPermissionHooks(
        policy=PolicyContext(worktree=wt),
        backend=backend or ScriptedBackend(),
        runs_dir=tmp_path / "runs",
        project="demo",
        task_id="T1",
        branch=lambda: branch,
    )


def pre(hooks: ToolPermissionHooks, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "tu1",
    }
    return dict(asyncio.run(hooks.pre_tool_use(data, "tu1", {"signal": None})))


def decision_of(output: dict[str, Any]) -> str | None:
    decision: str | None = output.get("hookSpecificOutput", {}).get("permissionDecision")
    return decision


def events(tmp_path: Path) -> list[dict[str, Any]]:
    path = trace_path(tmp_path / "runs", "demo")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- the hook ----------------------------------------------------------------------------------


def test_an_allowed_call_returns_no_opinion_and_is_traced(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt)

    output = pre(hooks, "Bash", {"command": "pytest -q"})

    assert output == {}
    [event] = events(tmp_path)
    assert event["event"] == "tool_call"
    assert event["details"]["verdict"] == "allow"
    assert event["details"]["tool"] == "Bash"
    assert event["details"]["summary"] == "pytest -q"
    assert event["details"]["task_id"] == "T1"
    assert event["stage"] == "developer"


def test_a_denied_call_is_blocked_with_the_reason_and_traced(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt)

    output = pre(hooks, "Bash", {"command": "git push origin main"})

    assert decision_of(output) == "deny"
    assert "push" in output["hookSpecificOutput"]["permissionDecisionReason"]
    [event] = events(tmp_path)
    assert event["event"] == "tool_denied"
    assert event["details"]["verdict"] == "deny"
    assert "push" in event["details"]["reason"]


def test_reading_dot_env_is_blocked_and_traced(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt)

    output = pre(hooks, "Read", {"file_path": ".env"})

    assert decision_of(output) == "deny"
    assert events(tmp_path)[0]["event"] == "tool_denied"


def test_a_call_that_needs_the_human_is_sent_to_them_with_the_reason(
    tmp_path: Path, wt: Path
) -> None:
    hooks = make_hooks(tmp_path, wt)

    output = pre(hooks, "Bash", {"command": "pip install requests"})

    assert decision_of(output) == "ask"
    assert "not on the allowed list" in output["hookSpecificOutput"]["permissionDecisionReason"]
    assert events(tmp_path)[0]["details"]["verdict"] == "ask"


def test_the_branch_is_read_at_call_time(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt, branch="main")

    assert decision_of(pre(hooks, "Bash", {"command": "git commit -m x"})) == "deny"


def test_a_crash_inside_the_policy_blocks_the_call(
    tmp_path: Path, wt: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr("orchestrator.tool_hooks.evaluate", boom)

    output = pre(make_hooks(tmp_path, wt), "Bash", {"command": "ls"})

    assert decision_of(output) == "deny"
    assert "policy" in output["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_the_trace_never_contains_file_contents(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt)

    pre(hooks, "Write", {"file_path": "src/a.py", "content": "SECRET-VALUE-123"})
    pre(
        hooks,
        "Edit",
        {"file_path": "src/a.py", "old_string": "OLD-SECRET", "new_string": "NEW-SECRET"},
    )

    text = trace_path(tmp_path / "runs", "demo").read_text()
    assert "SECRET" not in text
    assert "src/a.py" in text


def test_a_very_long_command_is_shortened_in_the_trace(tmp_path: Path, wt: Path) -> None:
    pre(make_hooks(tmp_path, wt), "Bash", {"command": "echo " + "x" * 5000})

    assert len(events(tmp_path)[0]["details"]["summary"]) <= 400


# --- asking the human ------------------------------------------------------------------------


def permission_context() -> ToolPermissionContext:
    return ToolPermissionContext(decision_reason="needs approval")


def ask(hooks: ToolPermissionHooks, tool: str, tool_input: dict[str, Any]) -> Any:
    return asyncio.run(hooks.can_use_tool(tool, tool_input, permission_context()))


def test_an_approval_lets_the_call_through_and_is_traced(tmp_path: Path, wt: Path) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=True))
    hooks = make_hooks(tmp_path, wt, backend)

    result = ask(hooks, "Bash", {"command": "pip install requests"})

    assert isinstance(result, PermissionResultAllow)
    [request] = backend.requests
    assert request.tool == "Bash" and "pip install" in request.summary
    assert request.reason == "needs approval"
    assert events(tmp_path)[0]["details"]["verdict"] == "human_allowed"


def test_a_refusal_blocks_the_call_and_tells_the_agent_why(tmp_path: Path, wt: Path) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=False, message="Use the stdlib instead"))
    hooks = make_hooks(tmp_path, wt, backend)

    result = ask(hooks, "Bash", {"command": "pip install requests"})

    assert isinstance(result, PermissionResultDeny)
    assert "Use the stdlib instead" in result.message
    event = events(tmp_path)[0]
    assert event["event"] == "tool_denied" and event["details"]["verdict"] == "human_denied"


def test_a_refusal_without_a_message_still_explains_itself(tmp_path: Path, wt: Path) -> None:
    result = ask(make_hooks(tmp_path, wt), "Bash", {"command": "pip install x"})

    assert isinstance(result, PermissionResultDeny)
    assert result.message


def test_the_callback_re_checks_the_policy_so_a_denied_call_never_reaches_the_human(
    tmp_path: Path, wt: Path
) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=True))
    hooks = make_hooks(tmp_path, wt, backend)

    result = ask(hooks, "Bash", {"command": "git push"})

    assert isinstance(result, PermissionResultDeny)
    assert backend.requests == []


def test_a_sandbox_network_request_reaches_the_human_with_the_host_and_is_traced(
    tmp_path: Path, wt: Path
) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=True))
    hooks = make_hooks(tmp_path, wt, backend)

    result = ask(hooks, "SandboxNetworkAccess", {"host": "example.com", "port": 443})

    assert isinstance(result, PermissionResultAllow)
    [request] = backend.requests
    assert request.tool == "SandboxNetworkAccess"
    assert request.summary == "host=example.com, port=443"
    assert events(tmp_path)[-1]["details"]["verdict"] == "human_allowed"


def test_a_network_request_the_human_refuses_is_denied_with_their_words(
    tmp_path: Path, wt: Path
) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=False, message="no downloads"))
    hooks = make_hooks(tmp_path, wt, backend)

    result = ask(hooks, "SandboxNetworkAccess", {"host": "example.com"})

    assert isinstance(result, PermissionResultDeny) and result.message == "no downloads"


def test_a_request_for_this_machine_never_reaches_the_human(tmp_path: Path, wt: Path) -> None:
    backend = ScriptedBackend(ToolAnswer(allow=True))
    hooks = make_hooks(tmp_path, wt, backend)

    for host in ("localhost:8765", "127.0.0.1", "169.254.169.254"):
        result = ask(hooks, "SandboxNetworkAccess", {"host": host})
        assert isinstance(result, PermissionResultDeny), host

    assert backend.requests == []
    assert events(tmp_path)[-1]["event"] == "tool_denied"


# --- wiring into the SDK options -------------------------------------------------------------


def test_the_options_install_a_blocking_hook_and_the_callback(tmp_path: Path, wt: Path) -> None:
    hooks = make_hooks(tmp_path, wt)

    options = hooks.sdk_options()

    [matcher] = options["hooks"]["PreToolUse"]
    assert matcher.matcher is None  # every tool
    assert matcher.on_failure == "block"  # a crashing hook must not let calls through
    assert options["can_use_tool"] == hooks.can_use_tool


# --- helpers ---------------------------------------------------------------------------------


def test_the_current_branch_comes_from_git(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "agent/t9"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *cmd], cwd=repo, check=True
        )

    assert git_current_branch(repo) == "agent/t9"
    assert git_current_branch(tmp_path / "missing") is None


def test_the_stage_in_the_trace_is_the_developer() -> None:
    assert Stage.DEVELOPER.value == "developer"


# --- the terminal prompt ---------------------------------------------------------------------

runner = CliRunner()


def ask_terminal(request: ToolRequest, keystrokes: str) -> tuple[ToolAnswer, str]:
    answers: list[ToolAnswer] = []
    app = typer.Typer()

    @app.command()
    def go() -> None:
        answers.append(CliToolBackend().ask(request))

    result = runner.invoke(app, [], input=keystrokes)
    assert result.exit_code == 0, result.output
    return answers[0], result.output


REQUEST = ToolRequest(tool="Bash", summary="pip install requests", reason="not on the allowed list")


def test_the_terminal_shows_what_the_agent_wants_and_why() -> None:
    _, output = ask_terminal(REQUEST, "n\n\n")

    assert "pip install requests" in output
    assert "not on the allowed list" in output


def test_answering_yes_allows_the_call() -> None:
    answer, _ = ask_terminal(REQUEST, "y\n")

    assert answer == ToolAnswer(allow=True)


def test_answering_no_denies_and_can_say_why() -> None:
    answer, _ = ask_terminal(REQUEST, "n\nUse the stdlib\n")

    assert answer == ToolAnswer(allow=False, message="Use the stdlib")


def test_markup_in_a_command_is_printed_literally() -> None:
    request = ToolRequest(tool="Bash", summary="echo [bold]hi[/bold]", reason="r")

    _, output = ask_terminal(request, "n\n\n")

    assert "[bold]hi[/bold]" in output


def test_a_repos_own_approval_paths_are_passed_through_to_the_policy(
    tmp_path: Path, wt: Path
) -> None:
    hooks = ToolPermissionHooks(
        policy=PolicyContext(worktree=wt, ask_paths=("docs",)),
        backend=ScriptedBackend(),
        runs_dir=tmp_path / "runs",
        project="demo",
        task_id="T1",
        branch=lambda: "agent/T1",
    )

    asked = pre(hooks, "Write", {"file_path": "docs/plan.md", "content": "x"})
    ci = pre(hooks, "Write", {"file_path": ".github/ci.yml", "content": "x"})

    assert decision_of(asked) == "ask"
    assert decision_of(ci) is None  # not in this repo's approval list, so it is simply allowed


# --- saved always-allow rules ---------------------------------------------------------------


def test_a_saved_rule_lets_the_command_through_traces_it_and_counts_the_use(
    tmp_path: Path, wt: Path
) -> None:
    from orchestrator.rules import project_store

    rule = project_store(tmp_path / "runs", "demo").add(["npx", "next", "lint"], any_args=False)
    hooks = ToolPermissionHooks(
        policy=PolicyContext(worktree=wt, rules=lambda: [rule]),
        backend=ScriptedBackend(),
        runs_dir=tmp_path / "runs",
        project="demo",
        task_id="T1",
        branch=lambda: "agent/t1",
    )

    output = pre(hooks, "Bash", {"command": "npx next lint"})

    assert output == {}
    [event] = events(tmp_path)
    assert event["details"]["verdict"] == "allow" and event["details"]["rule"] == rule.id
    assert project_store(tmp_path / "runs", "demo").load()[0].uses == 1


def test_the_rules_and_autonomy_survive_the_per_call_policy_copy(tmp_path: Path, wt: Path) -> None:
    (wt / "package-lock.json").write_text("{}")
    hooks = ToolPermissionHooks(
        policy=PolicyContext(worktree=wt, autonomy="autonomous"),
        backend=ScriptedBackend(),
        runs_dir=tmp_path / "runs",
        project="demo",
        task_id="T1",
        branch=lambda: "agent/t1",
    )

    assert pre(hooks, "Bash", {"command": "npm ci"}) == {}
