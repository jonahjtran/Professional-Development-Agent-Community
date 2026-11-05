"""Pushing a finished task branch and opening its pull request.

This is the orchestrator's job, never the agent's: the GitHub token lives here and nowhere the
agent can see. The token is passed to child processes only through their environment (never the
argument list, which other programs can read), and is scrubbed from every error message.
"""

import base64
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from schemas import DevResult

BRANCH_PREFIX = "agent/"
Run = Callable[..., subprocess.CompletedProcess[str]]


class PublishError(Exception):
    """Publishing failed; the message never contains the token."""


def run_command(
    args: list[str], *, env: dict[str, str], cwd: Path, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env={**os.environ, **env},
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def load_token(env: Mapping[str, str] = os.environ) -> str:
    token = env.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise PublishError("GITHUB_TOKEN is not set; add it to .env to open pull requests")
    return token


class Publisher:
    def __init__(
        self, repo: Path, token: str, *, run: Run = run_command, base: str = "main"
    ) -> None:
        self._repo = repo
        self._token = token
        self._run = run
        self._base = base

    def __repr__(self) -> str:
        return f"Publisher(repo={self._repo!r}, base={self._base!r})"  # never shows the token

    def publish(self, result: DevResult, *, title: str) -> DevResult:
        """Push the task branch, open or find its pull request, and return the result + URL."""
        branch = result.branch
        if not branch.startswith(BRANCH_PREFIX) or branch == self._base:
            raise PublishError(f"only {BRANCH_PREFIX} branches are published, not {branch}")
        self._push(branch)
        url = self._existing_pr(branch) or self._open_pr(branch, title, _body(result))
        return result.model_copy(update={"pr_url": url})

    # --- steps -----------------------------------------------------------------------------

    def _push(self, branch: str) -> None:
        remote = self._call(["git", "remote", "get-url", "origin"], {}, "reading the remote")
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if remote.stdout.strip().startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
            env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        # A plain push of one new or existing task branch: never forced, never main.
        self._call(["git", "push", "--set-upstream", "origin", branch], env, "git push")

    def _existing_pr(self, branch: str) -> str | None:
        out = self._call(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            {"GH_TOKEN": self._token},
            "looking for an existing pull request",
        ).stdout.strip()
        return out if out.startswith("http") else None

    def _open_pr(self, branch: str, title: str, body: str) -> str:
        done = self._call(
            [
                "gh",
                "pr",
                "create",
                "--base",
                self._base,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            {"GH_TOKEN": self._token},
            "opening the pull request",
            input_text=body,
        )
        lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
        if not lines or not lines[-1].startswith("http"):
            raise PublishError("opening the pull request did not return a URL")
        return lines[-1]

    def _call(
        self, args: list[str], env: dict[str, str], what: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        done = self._run(args, env=env, cwd=self._repo, input_text=input_text)
        if done.returncode != 0:
            raise PublishError(f"{what} failed: {self._redact(done.stderr.strip())}")
        return done

    def _redact(self, text: str) -> str:
        encoded = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return text.replace(encoded, "***").replace(self._token, "***")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "None"


def _body(result: DevResult) -> str:
    return (
        f"## Summary\n{result.summary}\n\n"
        f"## Deviations from the plan\n{_bullets(result.deviations_from_plan)}\n\n"
        f"## Known gaps\n{_bullets(result.known_gaps)}\n\n"
        f"Task {result.task_id}, built on branch `{result.branch}`.\n"
    )
