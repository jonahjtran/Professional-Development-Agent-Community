"""Running the real QA tools, and gathering the project's tests for the QA agent to read.

The numbers in a QA report come from here, not from a model. The tools run as plain subprocesses
(never through a shell) in a throwaway worktree. They execute the project's own code, so they get
an environment with secret-looking variables removed, and their output is scrubbed before it is
kept. A tool that is not installed is "skipped", which fails the report instead of passing it.
"""

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from orchestrator.config import CheckCommand, QaConfig
from orchestrator.envsafe import SECRET_ENV_NAME
from schemas import CheckResult, CheckStatus

Which = Callable[[str], str | None]

_TOKEN_SHAPES = re.compile(r"(?:github_pat_|ghp_|gho_|ghs_|sk-ant-|sk-)[A-Za-z0-9_\-]{16,}")
_MIN_SECRET_LENGTH = 8  # shorter values would blank out ordinary words
_SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages"}
_MAX_FILE_CHARS = 20_000
_MAX_TOTAL_CHARS = 120_000


def clean_env(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The environment checks run in: no secret-looking variables, and no outer virtualenv."""
    return {
        name: value
        for name, value in env.items()
        if not SECRET_ENV_NAME.search(name) and name not in {"VIRTUAL_ENV", "PYTHONPATH"}
    }


def redact(text: str, env: Mapping[str, str] = os.environ) -> str:
    """Remove the values of secret-looking variables and anything shaped like an API token."""
    for name, value in env.items():
        if SECRET_ENV_NAME.search(name) and len(value) >= _MIN_SECRET_LENGTH:
            text = text.replace(value, "***")
    return _TOKEN_SHAPES.sub("***", text)


def run_checks(
    config: QaConfig,
    worktree: Path,
    *,
    env: Mapping[str, str] = os.environ,
    which: Which = shutil.which,
) -> list[CheckResult]:
    """Run each configured check in `worktree`, in order, and record what happened."""
    return [_run_one(check, config, worktree, env, which) for check in config.checks]


def _run_one(
    check: CheckCommand, config: QaConfig, worktree: Path, env: Mapping[str, str], which: Which
) -> CheckResult:
    def result(status: CheckStatus, output: str) -> CheckResult:
        return CheckResult(
            name=check.name,
            command=check.command,
            status=status,
            output_tail=_tail(redact(output, env), config.tail_lines),
        )

    if check.requires and which(check.requires) is None:
        return result(CheckStatus.SKIPPED, f"{check.requires} is not installed")
    try:
        done = subprocess.run(
            shlex.split(check.command),
            cwd=worktree,
            env=clean_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        return result(CheckStatus.SKIPPED, f"cannot run {check.command!r}: {error}")
    except subprocess.TimeoutExpired as error:
        partial = _decode(error.stdout) + _decode(error.stderr)
        return result(
            CheckStatus.FAILED, f"timed out after {config.timeout_seconds} seconds\n{partial}"
        )
    status = CheckStatus.PASSED if done.returncode == 0 else CheckStatus.FAILED
    return result(status, done.stdout + done.stderr)


def _decode(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode(errors="replace")


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def collect_tests(worktree: Path) -> str:
    """The project's test files as Markdown, one heading per file, for the QA agent to read."""
    files = sorted(p for p in worktree.rglob("*.py") if _is_test_file(p, worktree))
    sections: list[str] = []
    used = 0
    for path in files:
        relative = path.relative_to(worktree).as_posix()
        text = path.read_text(errors="replace")
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + "\n# ... (file truncated)"
        if used + len(text) > _MAX_TOTAL_CHARS:
            sections.append(f"## {relative}\n(omitted: the test files together are too large)")
            continue
        used += len(text)
        sections.append(f"## {relative}\n```python\n{text}\n```")
    if not sections:
        return "No test files were found."
    return "\n\n".join(sections) + "\n"


def _is_test_file(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    if _SKIPPED_DIRS & set(parts):
        return False
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or "tests" in parts[:-1]
