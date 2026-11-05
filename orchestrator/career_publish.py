"""Publishing the career items the person approved, each as its own pull request.

Nothing here merges anything: the project docs, the resume proposal and the website entry are all
opened as pull requests on `agent/` branches, and the person merges them (for the website,
merging is what deploys it). The GitHub description and topics are the one direct change, and
they run as the person, with their own `gh` login, because repository settings are beyond the
agent token on purpose. Each target succeeds or fails on its own, so one problem never blocks
the others.
"""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import yaml

from orchestrator.career_stage import WEBSITE_PROJECTS
from orchestrator.latex import render_snippets
from orchestrator.newproject import run_with_own_login
from orchestrator.publish import PublishError, normalize_repo_url
from orchestrator.qa_checks import clean_env, redact
from orchestrator.resume_update import ResumeUpdateError, apply_changes, dump_resume
from orchestrator.website_entry import WebsiteEntryError, insert_entry
from orchestrator.worktree import (
    WorktreeError,
    branch_has_commits,
    commit_all,
    create_worktree,
    delete_branch,
    discard_unused,
    remove_worktree,
)
from schemas import CareerPlan, DevResult, ResumeChange, ResumeData

Edit = Callable[[Path], None]
Verify = Callable[[Path], None]
OwnLogin = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
_BUILD_TIMEOUT = 900


class CareerPublishError(Exception):
    """One target could not be published; the message says why."""


class PullRequests(Protocol):
    def publish(self, result: DevResult, *, title: str) -> DevResult: ...


class CareerPublisher(Protocol):
    def publish(self, *, slug: str, plan: CareerPlan, approved: set[str]) -> dict[str, str]:
        """Publish the approved items. Returns, per target, a URL or a 'failed: ...' note."""
        ...


def repo_owner(url: str) -> str | None:
    """The account a repository address belongs to, or None if it is not a GitHub address."""
    match = re.match(r"^github\.com/([^/]+)/[^/]+$", normalize_repo_url(url))
    return match.group(1) if match else None


def npm_build(path: Path) -> None:
    """The site's own install and build must pass before its pull request is opened."""
    for args in (["npm", "ci", "--no-audit", "--no-fund"], ["npm", "run", "build"]):
        try:
            done = subprocess.run(
                args,
                cwd=path,
                env=clean_env(),
                capture_output=True,
                text=True,
                timeout=_BUILD_TIMEOUT,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise CareerPublishError(f"`{' '.join(args)}` could not run: {error}") from error
        if done.returncode != 0:
            tail = "\n".join(redact(done.stdout + done.stderr).strip().splitlines()[-15:])
            raise CareerPublishError(f"`{' '.join(args)}` failed, so nothing was opened:\n{tail}")


class GitCareerPublisher:
    def __init__(
        self,
        *,
        project_repo: Path,
        project_url: str,
        project_pulls: PullRequests,
        profile_repo: Path,
        profile_pulls: PullRequests,
        website_repo: Path | None = None,
        website_pulls: PullRequests | None = None,
        build: Verify = npm_build,
        own_login: OwnLogin = run_with_own_login,
    ) -> None:
        self._project = project_repo
        self._project_url = project_url
        self._project_pulls = project_pulls
        self._profile = profile_repo
        self._profile_pulls = profile_pulls
        self._website = website_repo
        self._website_pulls = website_pulls
        self._build = build
        self._own_login = own_login

    def publish(self, *, slug: str, plan: CareerPlan, approved: set[str]) -> dict[str, str]:
        results: dict[str, str] = {}
        steps: list[tuple[str, bool, Callable[[], str]]] = [
            (
                "project docs",
                bool({"readme", "case_study"} & approved),
                lambda: self._docs(plan, approved),
            ),
            (
                "resume proposal",
                any(i.startswith("resume:") for i in approved),
                lambda: self._resume(slug, plan, approved),
            ),
            ("website", "website" in approved, lambda: self._site(slug, plan)),
            ("repository settings", "repo_meta" in approved, lambda: self._settings(plan)),
        ]
        for target, wanted, step in steps:
            if not wanted:
                continue
            try:
                results[target] = step()
            except (
                CareerPublishError,
                PublishError,
                WorktreeError,
                ResumeUpdateError,
                WebsiteEntryError,
            ) as error:
                results[target] = f"failed: {error}"
        return results

    # --- targets ---------------------------------------------------------------------------

    def _docs(self, plan: CareerPlan, approved: set[str]) -> str:
        files = {}
        if "readme" in approved:
            files["README.md"] = plan.readme_md
        if "case_study" in approved:
            files["docs/case_study.md"] = plan.case_study_md

        def edit(path: Path) -> None:
            for name, text in files.items():
                target = path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)

        return self._branch(
            self._project,
            self._project_pulls,
            "career-docs",
            edit,
            message="docs: add the README and case study",
            title="docs: README and case study",
            summary="Adds the README and case study written by the career coach from the "
            "project's own evidence.",
        )

    def _resume(self, slug: str, plan: CareerPlan, approved: set[str]) -> str:
        changes: list[ResumeChange] = []
        for item in sorted(approved):
            if item.startswith("resume:"):
                changes.append(plan.resume_changes[int(item.split(":")[1]) - 1])
        notes: list[str] = []

        def edit(path: Path) -> None:
            source = (path / "resume.yaml").read_text()
            current = ResumeData.model_validate(yaml.safe_load(source))
            updated, found = apply_changes(current, changes)
            notes.extend(found)
            (path / "resume.yaml").write_text(dump_resume(updated, source))
            folder = path / "proposals" / slug
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "resume_snippets.tex").write_text(render_snippets(changes))
            lines = [f"# Resume changes from {slug}", ""]
            for change in changes:
                lines.append(f"## {change.action.value.upper()}: {change.entry} ({change.section})")
                lines.append(change.reason)
                if change.replaces:
                    lines.append(f"Replaces: {change.replaces}")
                lines += [f"- {b.text}" for b in change.bullets]
                lines.append("")
            lines += [f"Note: {n}" for n in notes]
            lines.append("Paste `resume_snippets.tex` into the Overleaf file, then compile.")
            (folder / "resume_changes.md").write_text("\n".join(lines) + "\n")

        return self._branch(
            self._profile,
            self._profile_pulls,
            f"career-{slug}",
            edit,
            message=f"feat: resume proposal from {slug}",
            title=f"Resume proposal from {slug}",
            summary="Updates resume.yaml with the approved changes and adds paste-ready LaTeX "
            "snippets for the Overleaf resume.",
        )

    def _site(self, slug: str, plan: CareerPlan) -> str:
        if self._website is None or self._website_pulls is None:
            raise CareerPublishError("no website folder is set (AGENT_STUDIO_WEBSITE)")
        if plan.website_entry is None:
            raise CareerPublishError("the plan has no website entry")
        entry = plan.website_entry

        def edit(path: Path) -> None:
            target = path / WEBSITE_PROJECTS
            target.write_text(insert_entry(target.read_text(), entry))

        return self._branch(
            self._website,
            self._website_pulls,
            f"portfolio-{slug}",
            edit,
            message=f"feat: add {entry.title} to the projects",
            title=f"Add {entry.title} to the projects",
            summary=f"Adds a project entry for {entry.title} to {WEBSITE_PROJECTS}. "
            "The site's install and build passed before this was opened. Merging deploys it.",
            verify=self._build,
        )

    def _settings(self, plan: CareerPlan) -> str:
        match = re.match(r"^github\.com/([^/]+/[^/]+)$", normalize_repo_url(self._project_url))
        if match is None:
            raise CareerPublishError(f"{self._project_url} is not a GitHub repository")
        args = ["gh", "repo", "edit", match.group(1)]
        if plan.repo_description:
            args += ["--description", plan.repo_description]
        for topic in plan.repo_topics:
            args += ["--add-topic", topic]
        done = self._own_login(args, self._project)
        if done.returncode != 0:
            raise CareerPublishError(f"gh repo edit failed: {done.stderr.strip()}")
        return f"updated https://github.com/{match.group(1)}"

    # --- one pull request ------------------------------------------------------------------

    def _branch(
        self,
        repo: Path,
        pulls: PullRequests,
        task_id: str,
        edit: Edit,
        *,
        message: str,
        title: str,
        summary: str,
        verify: Verify | None = None,
    ) -> str:
        """Commit the edit on `agent/<task_id>` (reusing an earlier commit) and open its PR."""
        if not branch_has_commits(repo, task_id):
            discard_unused(repo, task_id)
            worktree = create_worktree(repo, task_id)
            try:
                edit(worktree.path)
                commit_all(worktree.path, message)
                if verify is not None:
                    verify(worktree.path)
            except Exception:
                remove_worktree(worktree)
                delete_branch(repo, worktree.branch)  # nothing half-done is left behind
                raise
            remove_worktree(worktree)
        result = DevResult(
            task_id=task_id,
            branch=f"agent/{task_id}",
            summary=summary,
            deviations_from_plan=[],
            known_gaps=[],
        )
        published = pulls.publish(result, title=title)
        if published.pr_url is None:
            raise CareerPublishError("the pull request has no address")
        return published.pr_url
