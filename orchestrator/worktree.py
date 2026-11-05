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
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

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


def default_worktree_root(repo: Path) -> Path:
    """Where a repo's task worktrees live: outside the repo and outside your Github folder.

    `~/.cache/agent-studio/worktrees/<repo name>`, or under $AGENT_STUDIO_WORKTREES if set.
    """
    base = os.environ.get("AGENT_STUDIO_WORKTREES") or str(
        Path.home() / ".cache" / "agent-studio" / "worktrees"
    )
    return Path(base) / repo.name


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
    repo: Path,
    task_id: str,
    *,
    base: str = "main",
    root: Path | None = None,
    resume: bool = False,
) -> Worktree:
    """Create `agent/<task_id>` in a new worktree, cut from the latest `base`.

    With `resume`, an existing task branch is checked out again (for a revision) instead of
    being created.
    """
    if not _TASK_ID.match(task_id):
        raise WorktreeError(f"unsafe task id: {task_id!r}")
    repo = repo.resolve()
    if not _ok(repo, "rev-parse", "--git-dir"):
        raise WorktreeError(f"{repo} is not a git repository")

    branch = f"{BRANCH_PREFIX}{task_id}"
    path = (root or default_worktree_root(repo)) / task_id
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
    if resume and _ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        _git(repo, "worktree", "add", "--quiet", str(path), branch)
    else:
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


_CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}


def uncommitted_paths(worktree: Worktree) -> list[str]:
    """Files with uncommitted work, ignoring the cache folders that running tests creates."""
    out = _git(worktree.path, "status", "--porcelain", "--untracked-files=all").stdout
    paths = [line[3:] for line in out.splitlines()]
    return [p for p in paths if not _CACHE_DIRS & set(Path(p).parts) and not p.endswith(".pyc")]


def has_uncommitted_changes(worktree: Worktree) -> bool:
    return bool(uncommitted_paths(worktree))


def protected_for(repo: Path, *, studio_root: Path) -> tuple[str, ...]:
    """Paths the agent may not edit: the orchestrator's control files, if it works on itself."""
    if repo.resolve() == studio_root.resolve():
        return SELF_PROTECTED
    return ()


def head_sha(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def branch_sha(repo: Path, branch: str) -> str | None:
    """The commit a local branch points at, or None if there is no such branch."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def local_branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").stdout.split())


def delete_branch(repo: Path, branch: str) -> None:
    _git(repo, "branch", "-D", branch)


def discard_unused(repo: Path, task_id: str) -> bool:
    """Remove the folder and branch of a crashed run that never made a commit.

    Returns True if something was removed. A branch that has any commits is never touched.
    """
    branch = f"{BRANCH_PREFIX}{task_id}"
    if branch_sha(repo, branch) is None:
        return False
    base = "main" if branch_sha(repo, "main") else "HEAD"
    if int(_git(repo, "rev-list", "--count", f"{base}..{branch}").stdout.strip()) > 0:
        return False
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree ") and line.endswith(f"/{task_id}"):
            _git(repo, "worktree", "remove", "--force", line.removeprefix("worktree "))
    _git(repo, "worktree", "prune")
    delete_branch(repo, branch)
    return True


def branch_has_commits(repo: Path, task_id: str) -> bool:
    """True if `agent/<task_id>` exists and has commits its base branch does not."""
    branch = f"{BRANCH_PREFIX}{task_id}"
    if branch_sha(repo, branch) is None:
        return False
    base = "main" if branch_sha(repo, "main") else "HEAD"
    return int(_git(repo, "rev-list", "--count", f"{base}..{branch}").stdout.strip()) > 0


def commit_all(path: Path, message: str) -> None:
    """Stage everything in a worktree and commit it, as the person running the orchestrator."""
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)


def checkout_detached(
    repo: Path, name: str, *, base: str = "main", root: Path | None = None
) -> Path:
    """A throwaway folder holding the latest `base` from the remote, on no branch.

    Used by QA, which only reads and runs the finished project. Any leftover folder with the same
    name from an earlier run is replaced.
    """
    if not _TASK_ID.match(name):
        raise WorktreeError(f"unsafe name: {name!r}")
    repo = repo.resolve()
    path = (root or default_worktree_root(repo)) / name
    ref = base
    if _ok(repo, "remote", "get-url", "origin"):
        fetched = _git(repo, "fetch", "--quiet", "origin", base, check=False)
        if fetched.returncode != 0:
            raise WorktreeError(f"could not fetch {base} from origin: {fetched.stderr.strip()}")
        ref = f"origin/{base}"
    if path.exists():
        remove_checkout(repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--quiet", "--detach", str(path), ref)
    return path


def remove_checkout(repo: Path, path: Path) -> None:
    """Delete a folder made by `checkout_detached`, and forget it."""
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    _git(repo, "worktree", "prune")
