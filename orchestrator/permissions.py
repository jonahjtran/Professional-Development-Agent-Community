"""The Developer permission policy: decide whether one tool call is allowed, needs the human, or
is denied.

This is a pure function of the call and a small context, so it can be tested with a big table and
no SDK. It is one layer of several (see docs/plans/phase-3.md): the agent also works in its own
worktree, holds no tokens, and cannot merge. Matching on shell text is leaky by nature, so
anything that cannot be understood is sent to the human instead of being guessed at.

Verdicts:
  ALLOW  ordinary development inside the worktree
  ASK    needs a human: installs, deletions, network, unlisted commands, new environment variables
  DENY   never: leaving the worktree, secrets, pushing, force flags, sudo, piping into a shell
"""

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class PolicyContext:
    """What the policy needs to know about where the agent is working."""

    worktree: Path
    protected: tuple[str, ...] = ()  # extra paths (relative to the worktree) the agent may not edit
    current_branch: str | None = None  # None skips the branch checks
    branch_prefix: str = "agent/"


_OK = Decision(Verdict.ALLOW, "allowed")
_ALWAYS_PROTECTED = (".github",)

# --- names that must never be read ------------------------------------------------------------

_SECRET_FILES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
)
_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube"}
_SAMPLE_SECRETS = (".env", ".env.local", ".netrc", "id_rsa", "id_ed25519")
_GLOB_CHARS = "*?["


def _is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def _secret_reason(text: str) -> str | None:
    """Why `text` (a path, a glob, or a bare token) names something secret, or None."""
    if not text or " " in text:  # free text such as a commit message, not a path
        return None
    parts = [p for p in re.split(r"[\\/]", text) if p]
    if not parts:
        return None
    for i, part in enumerate(parts):
        if part in _SECRET_DIRS or (part == ".config" and parts[i + 1 : i + 2] == ["gh"]):
            return f"{text} is a credentials folder"
    name = parts[-1]
    if name in _SAFE_ENV_TEMPLATES:
        return None
    if any(c in name for c in _GLOB_CHARS):
        # A glob that starts with a dot or a wildcard character could expand to a secret file.
        if name[0] in ".?[" and any(fnmatch.fnmatchcase(s, name) for s in _SAMPLE_SECRETS):
            return f"{text} could match a secret file"
        return None
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_FILES):
        return f"{text} is a secret file"
    return None


# --- paths ------------------------------------------------------------------------------------


def _real(raw: str, base: Path) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.realpath(path))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_protected(rel: Path, ctx: PolicyContext) -> bool:
    text = rel.as_posix()
    for prefix in (*_ALWAYS_PROTECTED, *ctx.protected):
        prefix = prefix.rstrip("/")
        if text == prefix or text.startswith(prefix + "/"):
            return True
    return False


def _check_path(
    raw: str, ctx: PolicyContext, base: Path, *, write: bool = False, allow_env_file: bool = False
) -> Decision | None:
    """A DENY decision if `raw` may not be touched, or None if it is fine."""
    root = ctx.worktree.resolve()
    if not (allow_env_file and _is_env_file(Path(raw).name)):
        secret = _secret_reason(raw)
        if secret:
            return Decision(Verdict.DENY, secret)
    if raw == "/dev/null":
        return None
    resolved = _real(raw, base)
    if not _inside(resolved, root):
        return Decision(Verdict.DENY, f"{raw} is outside the worktree")
    rel = resolved.relative_to(root)
    if not allow_env_file:
        secret = _secret_reason(rel.as_posix())
        if secret:
            return Decision(Verdict.DENY, f"{raw} resolves to a secret ({secret})")
    if ".git" in rel.parts:
        return Decision(Verdict.DENY, f"{raw} is inside git's own files")
    if write and _is_protected(rel, ctx):
        return Decision(Verdict.DENY, f"{rel.as_posix()} is a protected path; a human must edit it")
    return None


# --- new environment variables ----------------------------------------------------------------

_ENV_REFERENCES = (
    re.compile(r"""os\.environ\[\s*['"]([A-Za-z_]\w*)['"]\s*\]"""),
    re.compile(r"""os\.environ\.get\(\s*['"]([A-Za-z_]\w*)['"]"""),
    re.compile(r"""\bgetenv\(\s*['"]([A-Za-z_]\w*)['"]"""),
    re.compile(r"""process\.env\.([A-Za-z_]\w*)"""),
    re.compile(r"""process\.env\[\s*['"]([A-Za-z_]\w*)['"]"""),
    re.compile(r"""\$\{\{\s*secrets\.([A-Za-z_]\w*)"""),
)


def _env_names(text: str) -> set[str]:
    return {m.group(1) for pattern in _ENV_REFERENCES for m in pattern.finditer(text)}


def _new_env_names(old: str, new: str) -> list[str]:
    return sorted(_env_names(new) - _env_names(old))


# --- file tools -------------------------------------------------------------------------------


def _file_tool(tool: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    root = ctx.worktree.resolve()
    if tool in ("Read", "Glob", "Grep"):
        raw = tool_input.get("file_path") or tool_input.get("path") or "."
        denied = _check_path(str(raw), ctx, root)
        if denied:
            return denied
        # A glob can name a secret file too. (Grep's "pattern" is a regex, so it is not checked.)
        patterns = [tool_input.get("glob", "")]
        if tool != "Grep":
            patterns.append(tool_input.get("pattern", ""))
        for pattern in patterns:
            secret = _secret_reason(str(pattern))
            if secret:
                return Decision(Verdict.DENY, secret)
        return _OK

    # Edit and Write
    raw = str(tool_input.get("file_path", ""))
    is_env = _is_env_file(Path(raw).name)
    denied = _check_path(raw, ctx, root, write=True, allow_env_file=is_env)
    if denied:
        return denied
    if is_env:
        return Decision(
            Verdict.ASK, f"{raw} holds environment values; a human must add them, not the agent"
        )
    if tool == "Edit":
        old, new = str(tool_input.get("old_string", "")), str(tool_input.get("new_string", ""))
    else:
        target = _real(raw, root)
        old = target.read_text(errors="replace") if target.is_file() else ""
        new = str(tool_input.get("content", ""))
    added = _new_env_names(old, new)
    if added:
        return Decision(
            Verdict.ASK,
            f"this change needs a new environment variable: {', '.join(added)}. "
            "A human must add it to .env or the CI secrets",
        )
    return _OK


# --- bash -------------------------------------------------------------------------------------

_SEPARATORS = set(";&|\n")
_REDIRECT_CHARS = set("<>&")
_VARIABLE = re.compile(r"\$[A-Za-z_]")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_ALWAYS_DENIED = {
    "sudo": "privilege escalation",
    "su": "privilege escalation",
    "doas": "privilege escalation",
    "ssh": "remote access",
    "scp": "remote access",
    "sftp": "remote access",
    "rsync": "remote access",
    "nc": "raw network access",
    "ncat": "raw network access",
    "netcat": "raw network access",
    "telnet": "raw network access",
    "env": "reading or changing the environment",
    "printenv": "reading the environment",
    "set": "reading the environment",
    "declare": "reading the environment",
    "typeset": "reading the environment",
    "compgen": "reading the environment",
}
_FILE_UTILITIES = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "sort", "uniq", "diff", "tree", "pwd",
    "echo", "printf", "true", "false", "mkdir", "touch", "cp", "mv", "tee", "sed", "find",
    "which", "stat", "file", "basename", "dirname", "realpath", "date", "cut", "tr", "test", "[",
}  # fmt: skip
_WRITING_UTILITIES = {"mkdir", "touch", "cp", "mv", "tee", "rm", "rmdir"}
_RUNNERS = {"pytest", "ruff", "mypy"}
_RUNNER_MODULES = {"pytest", "ruff", "mypy", "unittest"}
_FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf"}

_GIT_READ = {
    "status", "diff", "log", "show", "blame", "grep", "ls-files", "rev-parse", "show-ref",
    "diff-tree", "cat-file", "describe", "shortlog", "ls-tree", "rev-list", "name-rev",
    "check-ignore", "show-branch", "reflog",
}  # fmt: skip
_GIT_LOCAL_WRITE = {"add", "commit", "mv", "restore", "stash"}
_GIT_DENIED = {
    "push": "pushing is done by the orchestrator, never the agent",
    "remote": "changing remotes",
    "config": "changing git configuration",
    "fetch": "network access",
    "pull": "network access",
    "clone": "network access",
    "submodule": "network access",
    "worktree": "managing worktrees",
    "filter-branch": "rewriting history",
    "update-ref": "rewriting refs",
    "credential": "credentials",
}
_GIT_FORCE_FLAGS = {"-f", "--force", "--force-with-lease"}


@dataclass
class _Segment:
    argv: list[str]
    redirects: list[tuple[str, str]]  # (kind, target) with kind "read" or "write"
    piped: bool  # fed by the previous command through a pipe


def _ask(reason: str) -> Decision:
    return Decision(Verdict.ASK, reason)


def _deny(reason: str) -> Decision:
    return Decision(Verdict.DENY, reason)


def _combine(decisions: list[Decision]) -> Decision:
    for wanted in (Verdict.DENY, Verdict.ASK):
        reasons = [d.reason for d in decisions if d.verdict == wanted]
        if reasons:
            return Decision(wanted, "; ".join(dict.fromkeys(reasons)))
    return _OK


def _split(command: str) -> list[_Segment] | Decision:
    """Split a command line into simple commands, or return an ASK if it cannot be understood."""
    for marker in ("$(", "`", "${", "<(", ">("):
        if marker in command:
            return _ask(f"cannot analyze this command (it contains {marker!r})")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return _ask("cannot understand this command (unbalanced quotes)")

    segments: list[_Segment] = []
    current = _Segment([], [], False)
    next_is_piped = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token or token[0] not in ";&|()<>\n":
            current.argv.append(token)
            continue
        chars = set(token)
        if "(" in chars or ")" in chars:
            return _ask("cannot analyze this command (it uses a subshell)")
        if chars <= _SEPARATORS:
            if current.argv or current.redirects:
                segments.append(current)
            next_is_piped = token in ("|", "|&")
            current = _Segment([], [], next_is_piped)
            continue
        if chars <= _REDIRECT_CHARS and ("<" in chars or ">" in chars):
            if token in ("<<", "<<<"):
                return _ask("cannot analyze this command (it uses a here-document)")
            target = tokens[i] if i < len(tokens) else ""
            if token == ">&" and (target.isdigit() or target == "-"):
                i += 1
                continue
            i += 1
            current.redirects.append(("read" if token == "<" else "write", target))
            continue
        return _ask("cannot analyze this command (unusual shell syntax)")
    if current.argv or current.redirects:
        segments.append(current)
    return segments


def _bash(command: str, ctx: PolicyContext) -> Decision:
    segments = _split(command)
    if isinstance(segments, Decision):
        return segments
    root = ctx.worktree.resolve()
    cwd = root
    decisions: list[Decision] = []
    for segment in segments:
        decision, cwd = _segment(segment, ctx, cwd)
        decisions.append(decision)
    return _combine(decisions)


def _segment(segment: _Segment, ctx: PolicyContext, cwd: Path) -> tuple[Decision, Path]:
    decisions: list[Decision] = []
    for kind, target in segment.redirects:
        denied = _check_path(target, ctx, cwd, write=kind == "write")
        if denied:
            decisions.append(denied)
    argv = list(segment.argv)
    while argv and _ASSIGNMENT.match(argv[0]):
        match = _ASSIGNMENT.match(argv[0])
        assert match is not None
        decisions.append(_ask(f"sets the environment variable {match.group(1)}"))
        argv.pop(0)
    if not argv:
        return _combine(decisions), cwd

    command, args = os.path.basename(argv[0]), argv[1:]
    for token in argv:
        secret = _secret_reason(token)
        if secret:
            decisions.append(_deny(secret))

    if command == "cd":
        target = args[0] if args else "~"
        denied = _check_path(target, ctx, cwd)
        if denied:
            decisions.append(denied)
            return _combine(decisions), cwd
        return _combine(decisions), _real(target, cwd)

    decisions.append(_command(command, args, segment.piped, ctx, cwd))
    return _combine(decisions), cwd


def _non_flags(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _command(command: str, args: list[str], piped: bool, ctx: PolicyContext, cwd: Path) -> Decision:
    if command in _ALWAYS_DENIED:
        return _deny(f"{command}: {_ALWAYS_DENIED[command]} is not allowed")
    if command in ("export", "declare") or command == "setx":
        names = [a.split("=", 1)[0] for a in args if "=" in a or command == "setx"]
        if command == "setx":
            names = names or args[:1]
        if names:
            return _ask(f"sets the environment variable {', '.join(names)}")
        return _deny("export without a value prints the environment")
    if command in _SHELLS:
        if piped:
            return _deny("piping into a shell is not allowed")
        if args[:1] == ["-c"] and len(args) >= 2:
            return _bash(args[1], ctx)
        return _ask(f"{command} is not on the allowed list (it runs other code)")
    if command in ("eval", "source", "."):
        return _ask(f"cannot analyze {command}")
    if command in ("curl", "wget"):
        return _ask(f"{command} needs network access")
    if command == "rm":
        if any(
            a in ("--recursive", "--force")
            or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("rRf"))
            for a in args
        ):
            return _deny("recursive or forced delete is not allowed")
        return _combine([_paths(args, ctx, cwd, write=True), _ask("deleting files needs approval")])
    if command == "rmdir":
        return _combine([_paths(args, ctx, cwd, write=True), _ask("deleting files needs approval")])
    if command == "git":
        return _git(args, ctx, cwd)
    if command in _RUNNERS:
        return _OK
    if command in ("python", "python3"):
        if args[:1] == ["-m"] and len(args) >= 2 and args[1] in _RUNNER_MODULES:
            return _OK
        return _ask(f"{command} {' '.join(args[:2])} is not on the allowed list")
    if command == "uv":
        return _uv(args, ctx, cwd)
    if command in _FILE_UTILITIES:
        return _utility(command, args, ctx, cwd)
    return _ask(f"{command} is not on the allowed list")


def _uv(args: list[str], ctx: PolicyContext, cwd: Path) -> Decision:
    if args[:1] != ["run"]:
        return _ask(f"uv {' '.join(args[:1])} is not on the allowed list")
    rest = args[1:]
    while rest and rest[0] in ("--frozen", "--locked", "--offline"):
        rest = rest[1:]
    if not rest:
        return _ask("uv run with no command is not on the allowed list")
    inner = os.path.basename(rest[0])
    if inner in _RUNNERS or (
        inner in ("python", "python3")
        and rest[1:2] == ["-m"]
        and rest[2:3]
        and rest[2] in _RUNNER_MODULES
    ):
        return _OK
    return _ask(f"uv run {inner} is not on the allowed list")


def _paths(args: list[str], ctx: PolicyContext, cwd: Path, *, write: bool) -> Decision:
    decisions: list[Decision] = []
    for arg in args:
        value = arg
        if arg.startswith("-"):
            if "=" not in arg:
                continue
            value = arg.split("=", 1)[1]
            if not (value.startswith(("/", "~")) or ".." in value.split("/")):
                continue
        if " " in value:
            continue  # free text, not a path
        denied = _check_path(value, ctx, cwd, write=write)
        if denied:
            decisions.append(denied)
    return _combine(decisions)


def _utility(command: str, args: list[str], ctx: PolicyContext, cwd: Path) -> Decision:
    if command in ("echo", "printf") and any(_VARIABLE.search(a) for a in args):
        return _deny("printing a variable can reveal secrets")
    if command == "find" and any(a in _FIND_UNSAFE for a in args):
        return _ask("find with -exec or -delete is not on the allowed list")
    if any(_VARIABLE.search(a) for a in args):
        return _ask("cannot analyze a command that expands variables")
    write = command in _WRITING_UTILITIES or (
        command == "sed" and any(a.startswith("-i") for a in args)
    )
    return _paths(args, ctx, cwd, write=write)


def _git(args: list[str], ctx: PolicyContext, cwd: Path) -> Decision:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        option = args[i]
        if option == "-C":
            denied = _check_path(args[i + 1] if i + 1 < len(args) else "", ctx, cwd)
            if denied:
                return denied
            i += 2
            continue
        if option in ("-c",) or option.startswith(("--git-dir", "--work-tree", "--exec-path")):
            return _deny("git -c, --git-dir and --work-tree could change what git runs")
        if option in ("--no-pager", "--paginate", "-P", "--no-optional-locks"):
            i += 1
            continue
        return _ask(f"git option {option} is not on the allowed list")
    if i >= len(args):
        return _OK
    sub, rest = args[i], args[i + 1 :]
    if sub in _GIT_DENIED:
        return _deny(f"git {sub}: {_GIT_DENIED[sub]}")
    if any(a in _GIT_FORCE_FLAGS for a in rest):
        return _deny(f"git {sub} with a force flag is not allowed")

    if sub in _GIT_READ or sub in _GIT_LOCAL_WRITE:
        # Commit messages and similar are free text; every other plain argument is a path.
        checked = _OK if sub == "commit" else _paths(_non_flags(rest), ctx, cwd, write=False)
        if checked.verdict == Verdict.DENY:
            return checked

    on_agent_branch = ctx.current_branch is None or ctx.current_branch.startswith(ctx.branch_prefix)
    if sub in _GIT_LOCAL_WRITE and not on_agent_branch:
        return _deny(
            f"git {sub} is only allowed on {ctx.branch_prefix} branches, not {ctx.current_branch}"
        )
    if sub in _GIT_READ or sub in _GIT_LOCAL_WRITE:
        return _OK
    if sub in ("checkout", "switch"):
        return _git_switch(sub, rest, ctx, cwd)
    if sub == "branch":
        return _git_branch(rest, ctx)
    return _ask(f"git {sub} needs approval")


def _git_switch(sub: str, rest: list[str], ctx: PolicyContext, cwd: Path) -> Decision:
    prefix = ctx.branch_prefix
    if "--" in rest:
        return _OK  # restoring files from the index
    if rest[:1] in (["-b"], ["-c"], ["-B"], ["-C"]) and len(rest) >= 2:
        name = rest[1]
        if name.startswith(prefix):
            return _OK
        return _deny(f"new branches must start with {prefix}, not {name}")
    targets = _non_flags(rest)
    if len(targets) != 1:
        return _ask(f"git {sub} with these arguments needs approval")
    target = targets[0]
    if target.startswith(prefix) or target == ctx.current_branch:
        return _OK
    if (cwd / target).exists():
        return _OK  # restoring a file, not switching branches
    return _deny(f"git {sub} to {target}: only {prefix} branches are allowed")


def _git_branch(rest: list[str], ctx: PolicyContext) -> Decision:
    if any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy") for a in rest):
        return _ask("git branch deleting or renaming needs approval")
    names = _non_flags(rest)
    listing = {"--show-current", "-a", "-r", "-v", "-vv", "--list", "--all", "--remotes"}
    if not names or any(a in listing for a in rest):
        return _OK
    if all(n.startswith(ctx.branch_prefix) for n in names):
        return _OK
    return _deny(f"new branches must start with {ctx.branch_prefix}")


# --- entry point ------------------------------------------------------------------------------


def evaluate(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision:
    """Decide what to do with one tool call from the Developer."""
    if tool_name in ("Read", "Glob", "Grep", "Edit", "Write"):
        return _file_tool(tool_name, tool_input, ctx)
    if tool_name == "Bash":
        return _bash(str(tool_input.get("command", "")), ctx)
    return _deny(f"the {tool_name} tool is not available to the Developer")
