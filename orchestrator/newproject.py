"""Creating the repository for a new project: `studio new <name>`.

It uses your own `gh` login, never the agents' token, because creating a repository on your
account is your decision and should not be something the agent token can do. It asks first and
refuses names that already exist, locally or on GitHub.
"""

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.publish import strip_credentials

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_README = "# {name}\n\nStarted with Agent Studio.\n"
_GITIGNORE = """\
# Secrets
.env
.env.*
!.env.example

# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/

# Node
node_modules/

# macOS
.DS_Store
"""

Run = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


class NewProjectError(Exception):
    """The project could not be created; the message says why and what, if anything, was made."""


@dataclass(frozen=True)
class NewProject:
    name: str
    owner: str
    path: Path
    url: str
    public: bool


def default_projects_dir() -> Path:
    """Where project clones go: `~/Desktop/Github`, or $AGENT_STUDIO_PROJECTS if set."""
    override = os.environ.get("AGENT_STUDIO_PROJECTS")
    return Path(override) if override else Path.home() / "Desktop" / "Github"


def run_with_own_login(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command as you: without the agents' GitHub token, so `gh` uses your own login."""
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def create_project(
    name: str,
    projects_dir: Path,
    *,
    public: bool = False,
    confirm: Callable[[str], bool],
    run: Run = run_with_own_login,
) -> NewProject:
    """Create `<owner>/<name>` on GitHub, clone it to `projects_dir`, and make its first commit."""
    if not _NAME.match(name):
        raise NewProjectError(
            f"unsafe project name {name!r}: use lowercase letters, digits and hyphens (max 60)"
        )
    target = projects_dir / name
    if target.exists():
        raise NewProjectError(f"{target} already exists; choose another name")
    projects_dir.mkdir(parents=True, exist_ok=True)

    who = run(["gh", "api", "user", "-q", ".login"], projects_dir)
    if who.returncode != 0 or not who.stdout.strip():
        raise NewProjectError("gh is not signed in. Run: gh auth login")
    owner = who.stdout.strip()
    if run(["gh", "repo", "view", f"{owner}/{name}"], projects_dir).returncode == 0:
        raise NewProjectError(f"{owner}/{name} already exists on GitHub; choose another name")

    visibility = "public" if public else "private"
    if not confirm(
        f"Create {visibility} repository {owner}/{name} on GitHub and clone it to {target}?"
    ):
        raise NewProjectError("cancelled by you; nothing was created")

    flag = "--public" if public else "--private"
    created = run(["gh", "repo", "create", f"{owner}/{name}", flag, "--clone"], projects_dir)
    if created.returncode != 0:
        raise NewProjectError(f"gh could not create the repository: {created.stderr.strip()}")
    if not (target / ".git").exists():
        raise NewProjectError(f"the repository was created but {target} is not a git clone")

    (target / "README.md").write_text(_README.format(name=name))
    (target / ".gitignore").write_text(_GITIGNORE)
    for step in (
        ["git", "add", "-A"],
        ["git", "commit", "-m", "chore: initial commit"],
        ["git", "branch", "-M", "main"],
        ["git", "push", "-u", "origin", "main"],
    ):
        done = run(step, target)
        if done.returncode != 0:
            raise NewProjectError(
                f"{owner}/{name} was created and cloned, but `{' '.join(step)}` failed: "
                f"{done.stderr.strip()}. Finish the first commit by hand."
            )
    origin = run(["git", "remote", "get-url", "origin"], target).stdout
    return NewProject(name, owner, target, strip_credentials(origin), public)
