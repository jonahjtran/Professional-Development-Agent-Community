"""What the human may choose to remember when approving a command.

The options are worked out by code from the command itself, never taken from the page: each
unlisted command in the line can be remembered exactly, or (when that is safe) as a prefix that
allows any further arguments. Anything the policy refuses, or asks about for a special reason,
cannot be remembered at all.
"""

import os
from dataclasses import dataclass, field

from orchestrator.permissions import (
    _ASSIGNMENT,
    Decision,
    PolicyContext,
    Verdict,
    is_ruleable,
    segment_decisions,
)
from orchestrator.tool_hooks import SUMMARY_LIMIT

# Words after which "any arguments" would let the rule install or publish whatever is named.
_NO_WILDCARD_WORDS = {
    "install", "i", "add", "remove", "uninstall", "publish", "exec", "x", "dlx", "create",
    "init", "link", "login", "adduser", "run-script", "pack",
}  # fmt: skip
# Programs whose arguments are themselves code or other commands.
_NO_WILDCARD_PROGRAMS = {
    "python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "sh", "bash", "zsh",
    "fish", "env", "xargs", "sudo", "eval", "exec", "ssh", "docker", "kubectl", "make", "just",
    "uv", "pip", "pip3", "cargo", "go", "gradle", "mvn",
}  # fmt: skip


@dataclass(frozen=True)
class RulePart:
    label: str
    exact: list[str]
    any_prefix: list[str] | None


@dataclass(frozen=True)
class RuleOptions:
    available: bool
    reason: str | None = None
    parts: list[RulePart] = field(default_factory=list)


def _words(argv: list[str]) -> list[str]:
    argv = [a for a in argv]
    while argv and _ASSIGNMENT.match(argv[0]):
        argv.pop(0)
    return [os.path.basename(argv[0]), *argv[1:]] if argv else []


def _part(words: list[str]) -> RulePart:
    prefix: list[str] | None = None
    if (
        len(words) > 2
        and words[0] not in _NO_WILDCARD_PROGRAMS
        and not words[1].startswith("-")
        and not _NO_WILDCARD_WORDS & set(words)
    ):
        prefix = words[:2]
    return RulePart(label=" ".join(words), exact=words, any_prefix=prefix)


def rule_options(tool: str, command: str, ctx: PolicyContext) -> RuleOptions:
    if tool != "Bash":
        return RuleOptions(False, "Only shell commands can be remembered.")
    if len(command) >= SUMMARY_LIMIT and command.endswith("..."):
        return RuleOptions(False, "This command was shortened, so it cannot be remembered safely.")
    analysed = segment_decisions(command, ctx)
    if isinstance(analysed, Decision):
        return RuleOptions(False, f"This cannot be remembered: {analysed.reason}.")
    parts: list[RulePart] = []
    for argv, decision in analysed:
        if decision.verdict == Verdict.ALLOW:
            continue
        if not is_ruleable(decision):
            return RuleOptions(False, f"This cannot be remembered: {decision.reason}.")
        words = _words(argv)
        if words:
            parts.append(_part(words))
    if not parts:
        return RuleOptions(False, "Nothing to remember: this is already allowed.")
    return RuleOptions(True, None, parts)
