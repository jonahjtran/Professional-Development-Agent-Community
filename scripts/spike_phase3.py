"""Phase 3 spike: observe how the Agent SDK's hooks, permissions and sandbox really behave.

Throwaway, opt-in, a few cents: it uses the fast model in a scratch folder and records what the
SDK reports, not what the model says. Run it with:

    uv run --env-file .env python scripts/spike_phase3.py

It never prints the value of any environment variable.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
)

from orchestrator.config import load_config

CONFIG = load_config(Path("config.yaml"))
MODEL = CONFIG.models.fast  # cheap: hook and sandbox behavior does not depend on the model


class Probe:
    """Records every hook call and every can_use_tool call for one experiment."""

    def __init__(self, deny_marker: str | None = None, ask_marker: str | None = None) -> None:
        self.hook_calls: list[tuple[str, dict[str, Any]]] = []
        self.permission_calls: list[tuple[str, dict[str, Any]]] = []
        self.deny_marker = deny_marker
        self.ask_marker = ask_marker
        self.ask_reasons: list[str | None] = []

    async def pre_tool_use(self, data: Any, tool_use_id: str | None, context: HookContext) -> Any:
        self.hook_calls.append((data["tool_name"], data["tool_input"]))
        if self.deny_marker and self.deny_marker in json.dumps(data["tool_input"]):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "blocked by the spike hook",
                }
            }
        if self.ask_marker and self.ask_marker in json.dumps(data["tool_input"]):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": "new environment variable needed",
                }
            }
        return {}

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultDeny:
        self.permission_calls.append((tool_name, tool_input))
        self.ask_reasons.append(context.decision_reason)
        return PermissionResultDeny(message="denied by the spike callback")


async def run(
    prompt: str,
    cwd: Path,
    probe: Probe,
    *,
    tools: list[str],
    allowed: list[str],
    sandbox: bool,
    mode: str = "dontAsk",
    env: dict[str, str] | None = None,
    excluded: list[str] | None = None,
) -> tuple[str, float]:
    options = ClaudeAgentOptions(
        model=MODEL,
        cwd=cwd,
        tools=tools,
        allowed_tools=allowed,
        permission_mode=mode,  # type: ignore[arg-type]
        setting_sources=[],
        max_turns=6,
        max_budget_usd=0.25,
        env=env or {},
        sandbox=(
            {"enabled": True, "autoAllowBashIfSandboxed": True, "excludedCommands": excluded or []}
            if sandbox
            else None
        ),  # type: ignore[typeddict-item]
        hooks={"PreToolUse": [HookMatcher(hooks=[probe.pre_tool_use], on_failure="block")]},
        can_use_tool=probe.can_use_tool,
    )
    text, cost = "", 0.0
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                text, cost = message.result or "", message.total_cost_usd or 0.0
    return text, cost


def show(
    title: str, probe: Probe, text: str, cost: float, extra: dict[str, Any] | None = None
) -> None:
    print(f"\n=== {title} (cost ${cost:.3f})")
    for name, tool_input in probe.hook_calls:
        print(f"  hook saw      : {name} {json.dumps(tool_input)[:150]}")
    for name, tool_input in probe.permission_calls:
        print(f"  can_use_tool  : {name} {json.dumps(tool_input)[:150]}")
    for key, value in (extra or {}).items():
        print(f"  {key:14}: {value}")
    print(f"  model said    : {text[:300]!r}")


async def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="spike-"))
    outside = Path(f"/tmp/spike-outside-{uuid.uuid4().hex[:8]}.txt")
    total = 0.0
    names_prompt = (
        "Run exactly this command and tell me which of these names appear in its output: "
        "ANTHROPIC_API_KEY, GITHUB_TOKEN, SPIKE_CANARY, HOME. Command: env | cut -d= -f1 | sort"
    )
    os.environ["SPIKE_CANARY"] = "canary-value"  # a stand-in for a secret such as GITHUB_TOKEN

    # E1: what does the agent's Bash see in its environment? (names only, never values)
    for label, env, sandbox in (
        ("default", {}, False),
        ("sandbox on", {}, True),
        ("blank the canary via env=", {"SPIKE_CANARY": ""}, False),
        ("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1", {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}, False),
    ):
        probe = Probe()
        text, cost = await run(
            names_prompt, scratch, probe, tools=["Bash"], allowed=["Bash"], sandbox=sandbox, env=env
        )
        total += cost
        show(f"E1 environment visible to Bash ({label})", probe, text, cost)

    # E1b: after blanking, is the value empty (not just hidden from the name list)?
    probe = Probe()
    text, cost = await run(
        'Run exactly: [ -z "$SPIKE_CANARY" ] && echo EMPTY || echo SET   and report the word.',
        scratch,
        probe,
        tools=["Bash"],
        allowed=["Bash"],
        sandbox=False,
        env={"SPIKE_CANARY": ""},
    )
    total += cost
    show("E1b value of a variable blanked through env=", probe, text, cost)

    # E2: do hooks fire for tools that are already allowed? is can_use_tool called for them?
    (scratch / "a.txt").write_text("hello")
    probe = Probe()
    text, cost = await run(
        "Run `echo hi`, then read the file a.txt, then say done.",
        scratch,
        probe,
        tools=["Bash", "Read"],
        allowed=["Bash", "Read"],
        sandbox=False,
    )
    total += cost
    show("E2 hook and callback coverage for allowed tools", probe, text, cost)

    # E3: can a hook block a Bash call?
    probe = Probe(deny_marker="DENYME-TEST")
    text, cost = await run(
        "Run the command: echo DENYME-TEST > blocked.txt . Then tell me whether it worked.",
        scratch,
        probe,
        tools=["Bash"],
        allowed=["Bash"],
        sandbox=False,
    )
    total += cost
    show("E3 hook deny", probe, text, cost, {"file created": (scratch / "blocked.txt").exists()})

    # E4: what does the sandbox allow? each probe is its own run so one failure hides nothing
    for label, command in (
        ("write inside cwd", "echo hi > inside.txt"),
        ("write outside cwd", f"echo x > {outside}"),
        ("read home dotfile", "head -c 20 ~/.zshrc"),
        ("network", "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://example.com"),
    ):
        probe = Probe()
        text, cost = await run(
            f"Run this one command and report only its exit status and any error text: {command}",
            scratch,
            probe,
            tools=["Bash"],
            allowed=["Bash"],
            sandbox=True,
        )
        total += cost
        show(
            f"E4 sandbox: {label}",
            probe,
            text,
            cost,
            {"inside.txt": (scratch / "inside.txt").exists(), "outside file": outside.exists()},
        )
        outside.unlink(missing_ok=True)

    # E5: for a non-trivial un-allowed command in default mode, is can_use_tool the one asked?
    probe = Probe()
    text, cost = await run(
        "Run the command `touch asked.txt` and report the result.",
        scratch,
        probe,
        tools=["Bash", "Read"],
        allowed=["Read"],
        sandbox=False,
        mode="default",
    )
    total += cost
    show(
        "E5 un-allowed command in default mode",
        probe,
        text,
        cost,
        {"asked.txt created": (scratch / "asked.txt").exists()},
    )

    # E6: git commit inside a git WORKTREE, with the sandbox on and git excluded from it
    repo = Path(tempfile.mkdtemp(prefix="spike-repo-"))
    work = Path(tempfile.mkdtemp(prefix="spike-wt-")) / "wt"
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "a@b.c"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
        ["git", "worktree", "add", "-q", "-b", "agent/t1", str(work)],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    probe = Probe()
    text, cost = await run(
        "Run this one command and report its exit status and error text: "
        "echo hi > f.txt && git add f.txt && git commit -q -m 'add f'",
        work,
        probe,
        tools=["Bash"],
        allowed=["Bash"],
        sandbox=True,
        excluded=["git"],
    )
    total += cost
    log = (
        subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", "agent/t1"], capture_output=True, text=True
        )
        .stdout.strip()
        .splitlines()
    )
    show(
        "E6 git commit in a worktree (sandbox on, git excluded)",
        probe,
        text,
        cost,
        {"commits on branch": len(log)},
    )

    # E7: a hook that answers "ask" even though the tool is allowed: does it reach can_use_tool?
    probe = Probe(ask_marker="ASKME-TEST")
    text, cost = await run(
        "Run the command: echo ASKME-TEST > asked.txt . Then tell me whether it worked.",
        scratch,
        probe,
        tools=["Bash"],
        allowed=["Bash"],
        sandbox=False,
    )
    total += cost
    show(
        "E7 hook answers ask for an allowed tool",
        probe,
        text,
        cost,
        {
            "reason given to callback": probe.ask_reasons,
            "asked.txt": (scratch / "asked.txt").exists(),
        },
    )

    print(f"\nTotal spike cost: ${total:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
