"""Git worktrees: a separate working folder of the same repository, on its own branch.

The Developer never works in your checkout. Each task gets `agent/<task-id>`, cut from an
up-to-date `main`, in a folder next to the repo. Your uncommitted work and your checked-out branch
are never touched, and untracked files such as `.env` do not exist in the worktree at all.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "agent/"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._]*$")

# When the target is the orchestrator's own repository, these cannot be edited by the agent.
SELF_PROTECTED = (
    ".github",
    "CLAUDE.md",
    "config.yaml",
    ".claude",
    "orchestrator/permissions.py",
    "orchestrator/tool_hooks.py",
    "orchestrator/worktree.py",
)


class WorktreeError(Exception):
    """A worktree could not be created or inspected; the message says why."""


@dataclass(frozen=True)
class Worktree:
    repo: Path  # the main repository
    path: Path  # the worktree folder
    branch: str  # agent/<task-id>
    base: str  # the ref the branch was cut from, e.g. "origin/main" or "main"


def _ok(cwd: Path, *args: str) -> bool:
    """True if the git command succeeds."""
    return _git(cwd, *args, check=False).returncode == 0


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang waiting for a password
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
        )
    except (FileNotFoundError, NotADirectoryError) as error:
        raise WorktreeError(f"cannot run git in {cwd}: {error}") from error
    if check and result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def create_worktree(
    repo: Path, task_id: str, *, base: str = "main", root: Path | None = None
) -> Worktree:
    """Create `agent/<task_id>` in a new worktree, cut from the latest `base`."""
    if not _TASK_ID.match(task_id):
        raise WorktreeError(f"unsafe task id: {task_id!r}")
    repo = repo.resolve()
    if not _ok(repo, "rev-parse", "--git-dir"):
        raise WorktreeError(f"{repo} is not a git repository")

    branch = f"{BRANCH_PREFIX}{task_id}"
    path = (root or repo.parent / f"{repo.name}-worktrees") / task_id
    # git itself refuses an existing branch or a non-empty folder, with a clear message.
    base_ref = base
    if _ok(repo, "remote", "get-url", "origin"):
        # Branch from what is on the remote, not from a local main that may be behind.
        fetched = _git(repo, "fetch", "--quiet", "origin", base, check=False)
        if fetched.returncode != 0:
            raise WorktreeError(f"could not fetch {base} from origin: {fetched.stderr.strip()}")
        base_ref = f"origin/{base}"
    if (
        _git(
            repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}", check=False
        ).returncode
        != 0
    ):
        raise WorktreeError(f"base branch {base_ref} does not exist")

    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base_ref)
    return Worktree(repo=repo, path=path, branch=branch, base=base_ref)


def remove_worktree(worktree: Worktree, *, delete_branch: bool = False) -> None:
    """Delete the worktree folder. The branch is kept unless `delete_branch` is set."""
    _git(worktree.repo, "worktree", "remove", "--force", str(worktree.path))
    if delete_branch:
        _git(worktree.repo, "branch", "-D", worktree.branch)


def commits_ahead(worktree: Worktree) -> int:
    """How many commits the task branch has that its base does not."""
    out = _git(worktree.path, "rev-list", "--count", f"{worktree.base}..HEAD").stdout.strip()
    return int(out)


def has_uncommitted_changes(worktree: Worktree) -> bool:
    return bool(_git(worktree.path, "status", "--porcelain").stdout.strip())


def protected_for(repo: Path, *, studio_root: Path) -> tuple[str, ...]:
    """Paths the agent may not edit: the orchestrator's control files, if it works on itself."""
    if repo.resolve() == studio_root.resolve():
        return SELF_PROTECTED
    return ()
