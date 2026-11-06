from pathlib import Path

import pytest

from orchestrator.permissions import PolicyContext
from orchestrator.rule_options import rule_options


@pytest.fixture
def ctx(tmp_path: Path) -> PolicyContext:
    root = tmp_path / "wt"
    root.mkdir()
    return PolicyContext(worktree=root, current_branch="agent/t1")


def options(command: str, ctx: PolicyContext, tool: str = "Bash"):  # type: ignore[no-untyped-def]
    return rule_options(tool, command, ctx)


def test_a_simple_command_offers_exact_and_a_wider_pattern(ctx: PolicyContext) -> None:
    result = options("npx next lint --fix", ctx)

    assert result.available and len(result.parts) == 1
    part = result.parts[0]
    assert part.label == "npx next lint --fix"
    assert part.exact == ["npx", "next", "lint", "--fix"]
    assert part.any_prefix == ["npx", "next"]


def test_a_command_with_no_extra_words_offers_only_exact(ctx: PolicyContext) -> None:
    part = options("npx next", ctx).parts[0]

    assert part.exact == ["npx", "next"] and part.any_prefix is None


@pytest.mark.parametrize(
    "command",
    ["npm install left-pad", "npm add x", "npm publish", "pnpm dlx thing go", "npm exec thing x"],
)
def test_installing_and_publishing_are_never_offered_as_any_arguments(
    ctx: PolicyContext, command: str
) -> None:
    result = options(command, ctx)

    assert result.available
    assert all(p.any_prefix is None for p in result.parts), command


@pytest.mark.parametrize(
    "command", ["python3 tool.py --go", "node x.js", "make deploy", "uv sync", "env FOO bar"]
)
def test_programs_that_run_arbitrary_code_are_remembered_exactly_only(
    ctx: PolicyContext, command: str
) -> None:
    result = options(command, ctx)

    for part in result.parts:
        assert part.any_prefix is None, command


def test_a_compound_command_offers_each_unlisted_part(ctx: PolicyContext) -> None:
    result = options("npx next lint && npx next build && ls", ctx)

    assert [p.label for p in result.parts] == ["npx next lint", "npx next build"]  # ls is allowed


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "git push origin main",
        "sudo ls",
        "cat .env",
        "curl https://example.com",
        "echo $(whoami)",
        "FOO=1 pytest",
        "bash install.sh",
        "mytool > ../outside.txt",
    ],
)
def test_refused_or_special_commands_cannot_be_remembered(ctx: PolicyContext, command: str) -> None:
    result = options(command, ctx)

    assert result.available is False and result.reason, command


def test_something_already_allowed_has_nothing_to_remember(ctx: PolicyContext) -> None:
    result = options("pytest -q", ctx)

    assert result.available is False and "already" in (result.reason or "")


def test_only_commands_can_be_remembered(ctx: PolicyContext) -> None:
    result = options("host=example.com", ctx, tool="SandboxNetworkAccess")

    assert result.available is False


def test_a_shortened_command_cannot_be_remembered(ctx: PolicyContext) -> None:
    from orchestrator.tool_hooks import summarize

    long = "npx next lint " + " ".join(f"--flag{i}" for i in range(200))
    shown = summarize("Bash", {"command": long})

    result = options(shown, ctx)

    assert result.available is False and "shortened" in (result.reason or "")
