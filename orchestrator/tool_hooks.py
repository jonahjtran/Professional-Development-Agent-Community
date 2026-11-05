"""Connects the permission policy to the Agent SDK, asks the human, and traces every decision.

One `PreToolUse` hook sees every tool call (the SDK does not call `can_use_tool` for tools that are
already allowed) and applies the policy. A call that needs the human is passed on to
`can_use_tool`, which asks through a `ToolPermissionBackend`: a terminal prompt now, a UI later.
If the policy itself crashes, the call is blocked.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    HookContext,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from orchestrator.permissions import Decision, PolicyContext, Verdict, evaluate
from orchestrator.state import Stage
from orchestrator.trace import EventType, TraceEvent, TraceWriter

_SUMMARY_LIMIT = 400


@dataclass(frozen=True)
class ToolRequest:
    """What the human is shown when the agent wants something that needs approval."""

    tool: str
    summary: str
    reason: str


@dataclass(frozen=True)
class ToolAnswer:
    allow: bool
    message: str = ""  # shown to the agent when the call is refused


class ToolPermissionBackend(Protocol):
    """Where the human answer to a tool request comes from: a terminal now, a UI later."""

    def ask(self, request: ToolRequest) -> ToolAnswer: ...


def git_current_branch(worktree: Path) -> str | None:
    """The branch checked out in `worktree`, or None if it cannot be read."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return result.stdout.strip() or None


def summarize(tool: str, tool_input: dict[str, Any]) -> str:
    """A short description of a call for the trace and the human. Never includes file contents."""
    if tool == "Bash":
        text = str(tool_input.get("command", ""))
    else:
        text = str(
            tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern", "")
        )
    return text if len(text) <= _SUMMARY_LIMIT else text[: _SUMMARY_LIMIT - 3] + "..."


class ToolPermissionHooks:
    """The hook and callback for one Developer run, bound to one task and one worktree."""

    def __init__(
        self,
        *,
        policy: PolicyContext,
        backend: ToolPermissionBackend,
        runs_dir: Path,
        project: str,
        task_id: str,
        branch: Callable[[], str | None] | None = None,
        halt_check: Callable[[], bool] | None = None,
    ) -> None:
        self._policy = policy
        self._backend = backend
        self._trace = TraceWriter(runs_dir, project)
        self._project = project
        self._task_id = task_id
        self._branch = branch or (lambda: git_current_branch(policy.worktree))
        self._halt_check = halt_check or (lambda: False)

    def sdk_options(self) -> dict[str, Any]:
        """Keyword arguments to merge into `ClaudeAgentOptions`."""
        return {
            "hooks": {
                # on_failure="block": if our hook crashes or returns nonsense, the call is blocked.
                "PreToolUse": [HookMatcher(hooks=[self.pre_tool_use], on_failure="block")]
            },
            "can_use_tool": self.can_use_tool,
        }

    def _decide(self, tool: str, tool_input: dict[str, Any]) -> Decision:
        try:
            policy = PolicyContext(
                worktree=self._policy.worktree,
                protected=self._policy.protected,
                ask_paths=self._policy.ask_paths,
                current_branch=self._branch(),
                branch_prefix=self._policy.branch_prefix,
            )
            return evaluate(tool, tool_input, policy)
        except Exception as error:  # fail closed: a bug in the policy must never allow a call
            return Decision(Verdict.DENY, f"the permission policy failed ({error!r}); blocking")

    def _record(
        self, event: EventType, tool: str, tool_input: dict[str, Any], verdict: str, reason: str
    ) -> None:
        self._trace.write(
            TraceEvent(
                project=self._project,
                stage=Stage.DEVELOPER,
                event=event,
                agent="developer",
                details={
                    "task_id": self._task_id,
                    "tool": tool,
                    "summary": summarize(tool, tool_input),
                    "verdict": verdict,
                    "reason": reason,
                },
            )
        )

    async def pre_tool_use(self, data: Any, tool_use_id: str | None, context: HookContext) -> Any:
        tool, tool_input = data["tool_name"], data["tool_input"]
        if self._halt_check():  # `studio halt`: stop the agent at its next tool call
            reason = "a halt was requested with studio halt"
            self._record(EventType.TOOL_DENIED, tool, tool_input, "halt", reason)
            return {
                "continue_": False,
                "stopReason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }
        decision = self._decide(tool, tool_input)
        if decision.verdict == Verdict.DENY:
            self._record(EventType.TOOL_DENIED, tool, tool_input, "deny", decision.reason)
        else:
            self._record(
                EventType.TOOL_CALL, tool, tool_input, decision.verdict.value, decision.reason
            )
        if decision.verdict == Verdict.ALLOW:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision.verdict.value,
                "permissionDecisionReason": decision.reason,
            }
        }

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        decision = self._decide(tool_name, tool_input)  # the policy has the final say
        if decision.verdict == Verdict.DENY:
            self._record(EventType.TOOL_DENIED, tool_name, tool_input, "deny", decision.reason)
            return PermissionResultDeny(message=decision.reason)
        if decision.verdict == Verdict.ALLOW:
            return PermissionResultAllow()
        reason = context.decision_reason or decision.reason
        answer = self._backend.ask(
            ToolRequest(tool=tool_name, summary=summarize(tool_name, tool_input), reason=reason)
        )
        if answer.allow:
            self._record(EventType.TOOL_CALL, tool_name, tool_input, "human_allowed", reason)
            return PermissionResultAllow()
        self._record(EventType.TOOL_DENIED, tool_name, tool_input, "human_denied", reason)
        return PermissionResultDeny(
            message=answer.message or "The human did not approve this action. Try another way."
        )
