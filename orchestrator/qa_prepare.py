"""Getting a finished project ready for QA: confirm it is merged, then run the real tools.

QA looks at the integrated project, so every approved Developer pull request must be merged into
`main` first. The tools then run in a throwaway checkout of the remote `main`, never in the
developer's own folder.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orchestrator.config import QaConfig
from orchestrator.qa_checks import collect_tests, run_checks
from orchestrator.worktree import WorktreeError, checkout_detached, remove_checkout
from schemas import CheckResult


class QaError(Exception):
    """QA could not be prepared (for example, the remote could not be reached)."""


@dataclass(frozen=True)
class QaMaterials:
    checks: list[CheckResult]
    tests_md: str


class QaPreparer(Protocol):
    def unmerged(self, pr_urls: list[str]) -> list[str]:
        """The pull requests among `pr_urls` that are not merged yet."""
        ...

    def run(self) -> QaMaterials:
        """Run the real tools on the project's merged `main` and gather its tests."""
        ...


class MergeBackend(Protocol):
    """Where the wait for merges is answered: a terminal prompt now."""

    def wait_for_merge(self, pr_urls: list[str]) -> bool:
        """Show the pull requests to merge. True means check again; False means give up."""
        ...


class PullRequests(Protocol):
    def is_merged(self, pr_url: str) -> bool: ...


class GitQaPreparer:
    def __init__(
        self, repo: Path, pulls: PullRequests, config: QaConfig, *, base: str = "main"
    ) -> None:
        self._repo = repo
        self._pulls = pulls
        self._config = config
        self._base = base

    def unmerged(self, pr_urls: list[str]) -> list[str]:
        return [url for url in pr_urls if not self._pulls.is_merged(url)]

    def run(self) -> QaMaterials:
        try:
            path = checkout_detached(self._repo, "qa", base=self._base)
        except WorktreeError as error:
            raise QaError(str(error)) from error
        try:
            return QaMaterials(checks=run_checks(self._config, path), tests_md=collect_tests(path))
        finally:
            remove_checkout(self._repo, path)
