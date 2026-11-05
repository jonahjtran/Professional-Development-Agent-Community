"""Getting the career coach's inputs ready, and turning its plan into things to approve.

The coach reads the person's resume data and goals (a private folder), the portfolio site's
project list and the project's current README (both from the latest `main` of their repositories).
All of that is gathered here by code. Nothing the coach says is trusted until the person approves
each item at the gate.
"""

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orchestrator.gates import GateItem
from schemas import CareerPlan, Evidence, EvidenceType, ResumeAction

WEBSITE_ENV = "AGENT_STUDIO_WEBSITE"
WEBSITE_PROJECTS = "src/data/projects.js"


class CareerError(Exception):
    """The coach's inputs could not be prepared; the message says what to fix."""


@dataclass(frozen=True)
class CareerMaterials:
    resume_yaml: str
    goals_yaml: str
    website_projects_js: str
    current_readme: str


class CareerPreparer(Protocol):
    def materials(self) -> CareerMaterials:
        """Read the person's profile, the website's projects and the project's README."""
        ...


def website_dir(env: Mapping[str, str] = os.environ) -> Path | None:
    value = env.get(WEBSITE_ENV, "").strip()
    return Path(value).expanduser() if value else None


class GitCareerPreparer:
    def __init__(
        self, profile: Path, project_repo: Path, website_repo: Path | None, *, base: str = "main"
    ) -> None:
        self._profile = profile
        self._project = project_repo
        self._website = website_repo
        self._base = base

    def materials(self) -> CareerMaterials:
        website = ""
        if self._website is not None:
            website = self._show(self._website, WEBSITE_PROJECTS, required=True)
        return CareerMaterials(
            resume_yaml=self._read(self._profile / "resume.yaml"),
            goals_yaml=self._read(self._profile / "goals.yaml"),
            website_projects_js=website,
            current_readme=self._show(self._project, "README.md", required=False),
        )

    @staticmethod
    def _read(path: Path) -> str:
        if not path.exists():
            raise CareerError(f"{path} is missing")
        return path.read_text()

    def _show(self, repo: Path, path: str, *, required: bool) -> str:
        """A file from the latest remote `main` of a repository, or '' if it is not there."""
        if not (repo / ".git").exists():
            raise CareerError(f"{repo} is not a git repository")
        fetched = _git(repo, "fetch", "--quiet", "origin", self._base)
        if fetched.returncode != 0:
            raise CareerError(f"could not fetch {self._base} in {repo}: {fetched.stderr.strip()}")
        shown = _git(repo, "show", f"origin/{self._base}:{path}")
        if shown.returncode != 0:
            if required:
                raise CareerError(f"{path} is not on {self._base} in {repo}")
            return ""
        return shown.stdout


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=False
    )


def gate_items(plan: CareerPlan, *, website: bool) -> list[GateItem]:
    """What the person approves or skips one by one at the career gate."""
    items = [
        GateItem(id="readme", label="Replace README.md in the project repository (pull request)"),
        GateItem(id="case_study", label="Add docs/case_study.md to the project (pull request)"),
    ]
    for number, change in enumerate(plan.resume_changes, start=1):
        if change.action != ResumeAction.SKIP:
            items.append(
                GateItem(
                    id=f"resume:{number}",
                    label=f"Resume ({change.section}): {change.action.value} {change.entry}",
                )
            )
    if plan.website_entry is not None and website:
        items.append(
            GateItem(
                id="website",
                label=f"Website: add '{plan.website_entry.title}' (pull request you merge)",
            )
        )
    if plan.repo_description or plan.repo_topics:
        items.append(
            GateItem(id="repo_meta", label="Set the GitHub description and topics (your login)")
        )
    return items


def human_evidence(answers: list[tuple[str, str]]) -> list[Evidence]:
    """The person's own answers, as evidence the coach may rest numbers and claims on."""
    return [
        Evidence(
            id=f"human-{number}",
            type=EvidenceType.HUMAN,
            ref="you",
            description=f"You said, in answer to '{question}': {answer}",
        )
        for number, (question, answer) in enumerate(answers, start=1)
    ]
