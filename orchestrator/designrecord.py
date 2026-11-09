"""Committing the approved design record into a project's own repository.

Once the human approves the Architect's output, the PRD, architecture, decision records and task
list are opened as the repository's first pull request, under `docs/`. The orchestrator does this
itself (it is trusted code, not the agent), on the branch `agent/plan`.
"""

from pathlib import Path
from typing import Protocol

from orchestrator.artifacts import ArtifactStore
from orchestrator.state import ProjectState
from orchestrator.worktree import (
    branch_has_commits,
    commit_all,
    create_worktree,
    discard_unused,
    remove_worktree,
)
from schemas import DevResult

PLAN_TASK_ID = "plan"
_COMMIT_MESSAGE = "docs: add the approved product requirements and architecture"
_PR_TITLE = "docs: product requirements and architecture"
_DOCS = {
    "prd.md": "docs/prd.md",
    "architecture.md": "docs/architecture.md",
    "tasks.json": "docs/tasks.json",
}


class PullRequestPublisher(Protocol):
    """Anything that can push a branch and open its pull request (publish.Publisher)."""

    def publish(self, result: DevResult, *, title: str) -> DevResult: ...


class DesignRecordError(Exception):
    """The design record could not be built; the message says which document is the problem."""


def _approved_text(state: ProjectState, store: ArtifactStore, name: str) -> str:
    artifact = state.artifacts.get(name)
    if artifact is None:
        raise DesignRecordError(f"{name} does not exist, so the design record cannot be built")
    if artifact.approved_at is None:
        raise DesignRecordError(f"{name} is not approved, so it is not committed")
    return store.read(artifact)


def publish_design_record(
    state: ProjectState, *, repo: Path, runs_dir: Path, publisher: PullRequestPublisher
) -> str:
    """Commit the approved documents to `agent/plan`, open the PR, and return its address."""
    store = ArtifactStore(runs_dir, state.project_slug)
    # The PRD is missing when the orchestrator decided the project needed no Product Owner.
    files = {
        dest: _approved_text(state, store, name)
        for name, dest in _DOCS.items()
        if name != "prd.md" or name in state.artifacts
    }
    for name in sorted(n for n in state.artifacts if n.startswith("adr/")):
        files[f"docs/{name}"] = _approved_text(state, store, name)

    if not branch_has_commits(repo, PLAN_TASK_ID):
        discard_unused(repo, PLAN_TASK_ID)  # an empty leftover from a crashed run
        worktree = create_worktree(repo, PLAN_TASK_ID)
        try:
            for dest, text in files.items():
                target = worktree.path / dest
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            commit_all(worktree.path, _COMMIT_MESSAGE)
        finally:
            remove_worktree(worktree)  # the branch with its commit stays in the repository

    result = DevResult(
        task_id=PLAN_TASK_ID,
        branch=f"agent/{PLAN_TASK_ID}",
        summary=(
            "Adds the approved product requirements, architecture, decision records and task "
            "list under docs/."
        ),
        deviations_from_plan=[],
        known_gaps=[],
    )
    published = publisher.publish(result, title=_PR_TITLE)
    assert published.pr_url is not None
    return published.pr_url
