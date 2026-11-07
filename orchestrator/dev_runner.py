"""Runs the Developer agent on one task in its own git worktree.

The agent edits and commits in the worktree and may not push. This runner starts the agent with a
strict setup (confined folder, explicit tools, sandbox, hidden secrets, permission hooks, a dollar
and turn limit) and afterwards checks the repository itself instead of trusting what the agent says.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from pydantic import ValidationError

from orchestrator.config import Config, LocalConfig
from orchestrator.developer_backend import (
    LocalStatus,
    check_local,
    developer_env,
    developer_model,
    get_developer_backend,
    record_running_model,
)
from orchestrator.halt import halt_requested
from orchestrator.notes import consume_notes, pending_notes
from orchestrator.permissions import PolicyContext
from orchestrator.pipeline import AgentError, AgentRequest, AgentResult
from orchestrator.project_settings import get_autonomy
from orchestrator.prompts import Prompt
from orchestrator.rules import active_rules
from orchestrator.skills import SkillError, load_skills
from orchestrator.tool_hooks import ToolPermissionBackend, ToolPermissionHooks, git_current_branch
from orchestrator.worktree import (
    Worktree,
    WorktreeError,
    branch_sha,
    commits_ahead,
    create_worktree,
    delete_branch,
    discard_unused,
    has_uncommitted_changes,
    head_sha,
    leftover_work,
    local_branches,
    protected_for,
    remove_worktree,
    uncommitted_paths,
)
from schemas import DevReport, DevResult, TaskList
from schemas.tasks import Task

_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
_STUDIO_ROOT = Path(__file__).resolve().parent.parent
_FOLLOW_UP = (
    "Your last message was not a valid report. Reply with only a JSON object with the fields "
    "task_id, summary, deviations_from_plan, known_gaps and (optionally) needs_from_human and "
    "needs_architect. Problem: {problem}"
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
        local_check: Callable[[LocalConfig], LocalStatus] = check_local,
    ) -> None:
        self._config = config
        self._runs_dir = runs_dir
        self._repo = repo
        self._backend = backend
        self._prompt = prompt
        self._studio_root = studio_root
        self._base = base
        self._client_factory = client_factory
        self._local_check = local_check

    def run(self, request: AgentRequest) -> AgentResult:
        task = _find_task(request)
        revising = request.previous_draft is not None
        try:
            skills = load_skills(self._studio_root, self._config.developer.skills)
        except SkillError as error:
            raise AgentError(str(error)) from error
        # Read at the start of every task, so a switch applies from the next task on.
        backend = get_developer_backend(self._runs_dir, request.project).backend
        if backend == "local":
            status = self._local_check(self._config.local)
            if not status.ok:
                raise AgentError(f"{status.detail} {status.fix}".strip())
        if request.task_id:
            record_running_model(
                self._runs_dir,
                request.project,
                request.task_id,
                backend,
                developer_model(backend, self._config),
            )
        # A rejected task starts again on a fresh branch, so the earlier pull request is untouched.
        branch_id = task.id if request.attempt == 0 else f"{task.id}-r{request.attempt}"
        try:
            # A run that was killed mid-task leaves its branch and folder behind. If there is real
            # work in them, carry on from it; if there is none, throw the empty leftovers away.
            interrupted = not revising and leftover_work(self._repo, branch_id)
            if not revising and not interrupted:
                discard_unused(self._repo, branch_id)
            branches_before = local_branches(self._repo)
            worktree = create_worktree(
                self._repo, branch_id, base=self._base, resume=revising or interrupted
            )
        except WorktreeError as error:
            raise AgentError(f"worktree problem: {error}") from error
        notes = pending_notes(self._runs_dir, request.project)
        try:
            result = asyncio.run(
                self._session(
                    request,
                    task,
                    worktree,
                    branches_before,
                    interrupted=interrupted,
                    notes=[n.text for n in notes],
                    backend=backend,
                    skills=skills,
                )
            )
            consume_notes(self._runs_dir, request.project, [n.id for n in notes])
            return result
        finally:
            _clean_up(worktree)  # the branch, with its commits, stays in the repository
            for stray in local_branches(self._repo) - branches_before - {worktree.branch}:
                delete_branch(self._repo, stray)  # a branch the agent made against the rules

    # --- the SDK session -------------------------------------------------------------------

    def _options(
        self,
        request: AgentRequest,
        task: Task,
        worktree: Worktree,
        backend: str = "claude",
        skills: str = "",
    ) -> ClaudeAgentOptions:
        hooks = ToolPermissionHooks(
            policy=PolicyContext(
                worktree=worktree.path,
                protected=protected_for(self._repo, studio_root=self._studio_root),
                rules=lambda: active_rules(self._runs_dir, request.project),
                autonomy=get_autonomy(self._runs_dir, request.project),
            ),
            backend=self._backend,
            runs_dir=self._runs_dir,
            project=request.project,
            task_id=task.id,
            halt_check=lambda: halt_requested(self._runs_dir, request.project),
        )
        return ClaudeAgentOptions(
            model=developer_model(backend, self._config),
            cwd=worktree.path,
            tools=list(_TOOLS),
            allowed_tools=list(_TOOLS),
            system_prompt=f"{self._prompt.text}\n\n{skills}" if skills else self._prompt.text,
            max_turns=self._config.limits.max_turns.developer,
            max_budget_usd=self._config.limits.max_task_usd,
            setting_sources=[],
            # The sandbox blocks writes outside the worktree and, apart from the package
            # registries in config.yaml, all network (anything else is asked about, see
            # permissions._network). git is excluded because the sandbox protects .git; the
            # permission policy guards git instead.
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "excludedCommands": ["git"],
                "network": {"allowedDomains": list(self._config.sandbox.allowed_domains)},
            },
            env=developer_env(backend, self._config.local),
            **hooks.sdk_options(),
        )

    async def _session(
        self,
        request: AgentRequest,
        task: Task,
        worktree: Worktree,
        branches_before: set[str],
        *,
        interrupted: bool = False,
        notes: list[str] | None = None,
        backend: str = "claude",
        skills: str = "",
    ) -> AgentResult:
        head_before = head_sha(worktree.path)
        base_before = branch_sha(self._repo, self._base)
        options = self._options(request, task, worktree, backend, skills)

        async with self._client_factory(options) as client:
            await client.query(self._task_prompt(request, task, worktree, interrupted, notes or []))
            turn = await _collect(client, priced=backend != "local")
            report, problem = _parse_report(turn.result.result or "")
            if report is None:
                await client.query(_FOLLOW_UP.format(problem=problem))
                turn = await _collect(client, priced=backend != "local")  # a running total
                report, problem = _parse_report(turn.result.result or "")

        # A local model costs nothing, whatever price the SDK guesses for an unknown model name.
        cost = 0.0 if backend == "local" else turn.result.total_cost_usd or 0.0

        def fail(message: str) -> AgentError:
            return AgentError(message, cost_usd=cost)

        if report is None:
            raise fail(f"the Developer's final report was not valid: {problem}")
        self._verify(
            worktree, head_before, base_before, branches_before, fail, interrupted=interrupted
        )

        usage = turn.result.usage or {}
        result = DevResult(
            task_id=task.id,
            branch=worktree.branch,
            summary=report.summary,
            deviations_from_plan=report.deviations_from_plan,
            known_gaps=report.known_gaps,
            needs_from_human=report.needs_from_human,
            needs_architect=report.needs_architect,
        )
        return AgentResult(
            output=result.model_dump_json(indent=2),
            cost_usd=cost,
            model=turn.model or developer_model(backend, self._config),
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
        *,
        interrupted: bool = False,
    ) -> None:
        """Check the repository itself; the agent's own claims are not evidence."""
        extra = local_branches(self._repo) - branches_before - {worktree.branch}
        if extra:  # run() deletes them once the worktree is gone
            raise fail(f"the Developer created extra branches: {', '.join(sorted(extra))}")
        if branch_sha(self._repo, self._base) != base_before:
            raise fail(f"the Developer changed {self._base}")
        if git_current_branch(worktree.path) != worktree.branch:
            raise fail(f"the Developer left its branch {worktree.branch}")
        # A task picked up after a kill may already have all its commits: a session that finds
        # nothing left to do is fine as long as the branch has work on it.
        no_new_work = head_sha(worktree.path) == head_before and not interrupted
        if no_new_work or commits_ahead(worktree) == 0:
            raise fail("the Developer made no commits")
        left_over = uncommitted_paths(worktree)
        if left_over:
            shown = ", ".join(left_over[:5])
            raise fail(f"the Developer left uncommitted changes, which would be lost: {shown}")

    def _task_prompt(
        self,
        request: AgentRequest,
        task: Task,
        worktree: Worktree,
        interrupted: bool = False,
        notes: list[str] | None = None,
    ) -> str:
        parts = [
            f"Your task is {task.id}: {task.title}",
            f"Description:\n{task.description}",
            "Acceptance criteria:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria),
            f"Depends on (already done): {', '.join(task.depends_on) or 'nothing'}",
            f"Architecture:\n{request.inputs.get('architecture.md', '(none)')}",
            f"You are on branch {worktree.branch}, cut from {worktree.base}. Commit your work "
            "here. Do not push, and do not create or switch branches.",
        ]
        if interrupted:
            parts.append(
                "An earlier attempt at this task was interrupted. Its commits and any "
                "uncommitted changes are already on this branch: run `git log` and `git status`, "
                "check what is there against the acceptance criteria, and continue from it "
                "instead of starting over."
            )
        if notes:
            parts.append(
                "Notes from the person who runs this project (their own instructions, follow "
                "them unless they conflict with your rules):\n" + "\n".join(f"- {n}" for n in notes)
            )
        if request.previous_draft is not None:
            parts.append(f"Your previous report:\n{request.previous_draft}")
        if request.comments:
            parts.append(f"Feedback to apply with new commits:\n{request.comments}")
        return "\n\n".join(parts)


# --- helpers ----------------------------------------------------------------------------------


def _clean_up(worktree: Worktree) -> None:
    """Remove the task folder, unless it holds uncommitted work or cannot be removed.

    Uncommitted files are kept so the next attempt can carry on from them (`leftover_work`).
    A cleanup problem must never replace the error that explains why the task failed.
    """
    try:
        if has_uncommitted_changes(worktree):
            return
        remove_worktree(worktree)
    except WorktreeError:
        pass  # the next start sorts out a folder git no longer knows about


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


async def _collect(client: Any, *, priced: bool = True) -> _Turn:
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
    if priced and result.total_cost_usd is None:
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
