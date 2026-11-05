"""Runs the Developer agent on one task in its own git worktree.

The agent edits and commits in the worktree and may not push. This runner starts the agent with a
strict setup (confined folder, explicit tools, sandbox, hidden secrets, permission hooks, a dollar
and turn limit) and afterwards checks the repository itself instead of trusting what the agent says.
"""

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from pydantic import ValidationError

from orchestrator.config import Config
from orchestrator.halt import halt_requested
from orchestrator.permissions import PolicyContext
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.prompts import Prompt
from orchestrator.tool_hooks import ToolPermissionBackend, ToolPermissionHooks, git_current_branch
from orchestrator.worktree import (
    Worktree,
    WorktreeError,
    branch_sha,
    commits_ahead,
    create_worktree,
    delete_branch,
    discard_unused,
    head_sha,
    local_branches,
    protected_for,
    remove_worktree,
    uncommitted_paths,
)
from schemas import DevReport, DevResult, TaskList
from schemas.tasks import Task

_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|PRIVATE_?KEY", re.I)
_STUDIO_ROOT = Path(__file__).resolve().parent.parent
_FOLLOW_UP = (
    "Your last message was not a valid report. Reply with only a JSON object with the fields "
    "task_id, summary, deviations_from_plan and known_gaps. Problem: {problem}"
)

ClientFactory = Callable[[ClaudeAgentOptions], Any]


@dataclass
class _Turn:
    result: ResultMessage
    model: str | None


class DevRunner:
    """An AgentRunner for the Developer stage."""

    def __init__(
        self,
        config: Config,
        runs_dir: Path,
        repo: Path,
        backend: ToolPermissionBackend,
        prompt: Prompt,
        *,
        studio_root: Path = _STUDIO_ROOT,
        base: str = "main",
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._repo = repo
        self._backend = backend
        self._prompt = prompt
        self._studio_root = studio_root
        self._base = base
        self._client_factory = client_factory

    def run(self, request: AgentRequest) -> AgentResult:
        task = _find_task(request)
        revising = request.previous_draft is not None
        # A rejected task starts again on a fresh branch, so the earlier pull request is untouched.
        branch_id = task.id if request.attempt == 0 else f"{task.id}-r{request.attempt}"
        try:
            if not revising:
                discard_unused(self._repo, branch_id)  # leftovers of a crash, with no work in them
            branches_before = local_branches(self._repo)
            worktree = create_worktree(self._repo, branch_id, base=self._base, resume=revising)
        except WorktreeError as error:
            raise AgentError(f"worktree problem: {error}") from error
        try:
            return asyncio.run(self._session(request, task, worktree, branches_before))
        finally:
            remove_worktree(worktree)  # the branch, with its commits, stays in the repository
            for stray in local_branches(self._repo) - branches_before - {worktree.branch}:
                delete_branch(self._repo, stray)  # a branch the agent made against the rules

    # --- the SDK session -------------------------------------------------------------------

    def _options(self, request: AgentRequest, task: Task, worktree: Worktree) -> ClaudeAgentOptions:
        hooks = ToolPermissionHooks(
            policy=PolicyContext(
                worktree=worktree.path,
                protected=protected_for(self._repo, studio_root=self._studio_root),
            ),
            backend=self._backend,
            runs_dir=self._runs_dir,
            project=request.project,
            task_id=task.id,
            halt_check=lambda: halt_requested(self._runs_dir, request.project),
        )
        return ClaudeAgentOptions(
            model=self._config.models.for_tier("strong"),
            cwd=worktree.path,
            tools=list(_TOOLS),
            allowed_tools=list(_TOOLS),
            system_prompt=self._prompt.text,
            max_turns=self._config.limits.max_turns.developer,
            max_budget_usd=self._config.limits.max_task_usd,
            setting_sources=[],
            # The sandbox blocks writes outside the worktree and all network. git is excluded
            # because the sandbox protects .git; the permission policy guards git instead.
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "excludedCommands": ["git"],
            },
            env=_hidden_env(),
            **hooks.sdk_options(),
        )

    async def _session(
        self, request: AgentRequest, task: Task, worktree: Worktree, branches_before: set[str]
    ) -> AgentResult:
        head_before = head_sha(worktree.path)
        base_before = branch_sha(self._repo, self._base)
        options = self._options(request, task, worktree)

        async with self._client_factory(options) as client:
            await client.query(self._task_prompt(request, task, worktree))
            turn = await _collect(client)
            report, problem = _parse_report(turn.result.result or "")
            if report is None:
                await client.query(_FOLLOW_UP.format(problem=problem))
                turn = await _collect(client)  # the SDK reports a running total for the session
                report, problem = _parse_report(turn.result.result or "")

        cost = turn.result.total_cost_usd or 0.0

        def fail(message: str) -> AgentError:
            return AgentError(message, cost_usd=cost)

        if report is None:
            raise fail(f"the Developer's final report was not valid: {problem}")
        self._verify(worktree, head_before, base_before, branches_before, fail)

        usage = turn.result.usage or {}
        result = DevResult(
            task_id=task.id,
            branch=worktree.branch,
            summary=report.summary,
            deviations_from_plan=report.deviations_from_plan,
            known_gaps=report.known_gaps,
        )
        return AgentResult(
            output=result.model_dump_json(indent=2),
            cost_usd=cost,
            model=turn.model or self._config.models.for_tier("strong"),
            prompt_version=self._prompt.version,
            tokens_in=usage.get("input_tokens"),
            tokens_out=usage.get("output_tokens"),
        )

    def _verify(
        self,
        worktree: Worktree,
        head_before: str,
        base_before: str | None,
        branches_before: set[str],
        fail: Callable[[str], AgentError],
    ) -> None:
        """Check the repository itself; the agent's own claims are not evidence."""
        extra = local_branches(self._repo) - branches_before - {worktree.branch}
        if extra:  # run() deletes them once the worktree is gone
            raise fail(f"the Developer created extra branches: {', '.join(sorted(extra))}")
        if branch_sha(self._repo, self._base) != base_before:
            raise fail(f"the Developer changed {self._base}")
        if git_current_branch(worktree.path) != worktree.branch:
            raise fail(f"the Developer left its branch {worktree.branch}")
        if head_sha(worktree.path) == head_before or commits_ahead(worktree) == 0:
            raise fail("the Developer made no commits")
        left_over = uncommitted_paths(worktree)
        if left_over:
            shown = ", ".join(left_over[:5])
            raise fail(f"the Developer left uncommitted changes, which would be lost: {shown}")

    def _task_prompt(self, request: AgentRequest, task: Task, worktree: Worktree) -> str:
        parts = [
            f"Your task is {task.id}: {task.title}",
            f"Description:\n{task.description}",
            "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria),
            f"Depends on (already done): {', '.join(task.depends_on) or 'nothing'}",
            f"Architecture:\n{request.inputs.get('architecture.md', '(none)')}",
            f"You are on branch {worktree.branch}, cut from {worktree.base}. Commit your work "
            "here. Do not push, and do not create or switch branches.",
        ]
        if request.previous_draft is not None:
            parts.append(f"Your previous report:\n{request.previous_draft}")
        if request.comments:
            parts.append(f"Feedback to apply with new commits:\n{request.comments}")
        return "\n\n".join(parts)


# --- helpers ----------------------------------------------------------------------------------


def _find_task(request: AgentRequest) -> Task:
    if not request.task_id:
        raise AgentError("the Developer needs a task id, and none was given")
    raw = request.inputs.get("tasks.json")
    if raw is None:
        raise AgentError("the Developer needs the approved tasks.json as an input")
    try:
        tasks = TaskList.model_validate_json(raw).tasks
    except ValidationError as error:
        raise AgentError(f"tasks.json is not a valid task list: {error}") from error
    for task in tasks:
        if task.id == request.task_id:
            return task
    raise AgentError(f"task {request.task_id} is not in tasks.json")


def _hidden_env() -> dict[str, str]:
    """Environment overrides for the agent's subprocess: scrub credentials it must not see.

    The SDK flag strips Anthropic and cloud credentials from the agent's shell. Other secrets in
    our own environment (a GitHub token, say) are blanked here. Anthropic and Claude variables are
    left alone because blanking them would break the agent's own login.
    """
    hidden = {
        name: ""
        for name in os.environ
        if _SECRET_NAME.search(name) and not name.startswith(("ANTHROPIC_", "CLAUDE"))
    }
    return {**hidden, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}


async def _collect(client: Any) -> _Turn:
    model: str | None = None
    result: ResultMessage | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            model = message.model
        elif isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise AgentError("the Developer ended without a result message")
    if result.is_error:
        raise AgentError(
            f"the Developer failed: {result.subtype} {result.errors or ''}".strip(),
            cost_usd=result.total_cost_usd or 0.0,
        )
    if result.total_cost_usd is None:
        raise AgentError("the SDK reported no cost, so spend cannot be tracked")
    return _Turn(result, model)


def _parse_report(text: str) -> tuple[DevReport | None, str]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, "no JSON object found"
    try:
        return DevReport.model_validate(json.loads(text[start : end + 1])), ""
    except (ValueError, ValidationError) as error:
        return None, str(error)
