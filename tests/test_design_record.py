import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator.artifacts import ArtifactStore, approve_artifacts, record_artifacts
from orchestrator.designrecord import DesignRecordError, publish_design_record
from orchestrator.pipeline import PipelineContext, advance
from orchestrator.publish import PublishError
from orchestrator.state import ProjectState, Stage, Status
from orchestrator.trace import trace_path
from schemas import DevResult
from tests.factories import make_config, new_state
from tests.test_architect_stage import StageAgent, make_ctx
from tests.test_pipeline import FakeCaps, FakeGates

NOW = datetime(2026, 11, 5, tzinfo=UTC)
GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# project\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[DevResult, str]] = []
        self.repo = Path(".")

    def publish(self, result: DevResult, *, title: str) -> DevResult:
        if self.fail:
            raise PublishError("push failed: nope")
        self.published.append((result, title))
        return result.model_copy(
            update={"pr_url": f"https://github.com/me/p/pull/{len(self.published)}"}
        )

    def diff_stat(self, branch: str) -> str:
        return "stat"


def approved_state(tmp_path: Path, *, approve: bool = True) -> ProjectState:
    store = ArtifactStore(tmp_path, "demo")
    artifacts = {
        "prd.json": store.write("prd.json", '{"problem": "p"}', revision=1),
        "prd.md": store.write("prd.md", "# PRD\nproblem", revision=1),
        "architecture.json": store.write("architecture.json", "{}", revision=1),
        "architecture.md": store.write("architecture.md", "# Architecture", revision=1),
        "tasks.json": store.write("tasks.json", '{"tasks": []}', revision=1),
        "adr/001-use-sqlite.md": store.write("adr/001-use-sqlite.md", "# ADR 001", revision=1),
        "adr/002-use-fastapi.md": store.write("adr/002-use-fastapi.md", "# ADR 002", revision=1),
    }
    state = record_artifacts(new_state(), artifacts)
    return approve_artifacts(state, list(artifacts), now=NOW) if approve else state


def publish(repo: Path, tmp_path: Path, publisher: FakePublisher | None = None, **kw: Any) -> str:
    return publish_design_record(
        approved_state(tmp_path, **kw),
        repo=repo,
        runs_dir=tmp_path,
        publisher=publisher or FakePublisher(),
    )


# --- the branch and its files ----------------------------------------------------------------


def test_the_design_record_is_a_single_commit_on_its_own_branch(repo: Path, tmp_path: Path) -> None:
    publish(repo, tmp_path)

    assert git(repo, "rev-list", "--count", "main..agent/plan") == "1"
    assert git(repo, "log", "-1", "--format=%s", "agent/plan") == (
        "docs: add the approved product requirements and architecture"
    )


def test_the_documents_land_under_docs_with_their_approved_content(
    repo: Path, tmp_path: Path
) -> None:
    publish(repo, tmp_path)

    def show(path: str) -> str:
        return git(repo, "show", f"agent/plan:{path}")

    assert show("docs/prd.md") == "# PRD\nproblem"
    assert show("docs/architecture.md") == "# Architecture"
    assert show("docs/tasks.json") == '{"tasks": []}'
    assert show("docs/adr/001-use-sqlite.md") == "# ADR 001"
    assert show("docs/adr/002-use-fastapi.md") == "# ADR 002"


def test_internal_json_copies_are_not_committed(repo: Path, tmp_path: Path) -> None:
    publish(repo, tmp_path)

    files = git(repo, "ls-tree", "-r", "--name-only", "agent/plan").splitlines()

    assert "docs/prd.json" not in files and "docs/architecture.json" not in files


def test_main_and_the_users_checkout_are_untouched_and_the_worktree_is_removed(
    repo: Path, tmp_path: Path
) -> None:
    before = git(repo, "rev-parse", "main")
    (repo / "wip.txt").write_text("my uncommitted work")

    publish(repo, tmp_path)

    assert git(repo, "rev-parse", "main") == before
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "wip.txt").read_text() == "my uncommitted work"
    assert "plan" not in git(repo, "worktree", "list").replace("agent/plan", "")


def test_the_commit_carries_no_attribution_trailer(repo: Path, tmp_path: Path) -> None:
    publish(repo, tmp_path)

    body = git(repo, "log", "-1", "--format=%B", "agent/plan")

    assert "Co-Authored-By" not in body and "Generated with" not in body


# --- publishing ------------------------------------------------------------------------------


def test_it_is_published_as_a_pull_request_and_returns_its_address(
    repo: Path, tmp_path: Path
) -> None:
    publisher = FakePublisher()

    url = publish(repo, tmp_path, publisher)

    assert url == "https://github.com/me/p/pull/1"
    [(result, title)] = publisher.published
    assert result.branch == "agent/plan" and result.task_id == "plan"
    assert title == "docs: product requirements and architecture"
    assert "PRD" in result.summary or "requirements" in result.summary


def test_a_publishing_failure_is_raised_and_the_branch_is_kept_for_a_retry(
    repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(PublishError):
        publish(repo, tmp_path, FakePublisher(fail=True))

    assert git(repo, "rev-list", "--count", "main..agent/plan") == "1"


# --- refusing and resuming -------------------------------------------------------------------


def test_documents_that_are_not_approved_are_never_committed(repo: Path, tmp_path: Path) -> None:
    publisher = FakePublisher()

    with pytest.raises(DesignRecordError, match="approved"):
        publish(repo, tmp_path, publisher, approve=False)

    assert publisher.published == []
    assert "agent/plan" not in git(repo, "branch", "--format=%(refname:short)").split()


def test_a_missing_document_is_refused(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(DesignRecordError, match="prd.md"):
        publish_design_record(
            new_state(),
            repo=repo,
            runs_dir=tmp_path,
            publisher=FakePublisher(),
        )


def test_resuming_after_a_failed_publish_reuses_the_commit_instead_of_making_another(
    repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(PublishError):
        publish(repo, tmp_path, FakePublisher(fail=True))

    url = publish(repo, tmp_path, FakePublisher())

    assert git(repo, "rev-list", "--count", "main..agent/plan") == "1"
    assert url.endswith("/pull/1")


def test_an_empty_leftover_branch_is_thrown_away_and_redone(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "agent/plan")  # a crashed earlier run that never committed

    publish(repo, tmp_path)

    assert git(repo, "rev-list", "--count", "main..agent/plan") == "1"


# --- inside the pipeline ---------------------------------------------------------------------


def pipeline_ctx(tmp_path: Path, repo: Path, publisher: FakePublisher | None) -> PipelineContext:
    ctx = make_ctx(tmp_path, StageAgent())
    return replace(ctx, publisher=publisher, repo=repo)


def at_architect_gate(tmp_path: Path, repo: Path, publisher: FakePublisher | None) -> Any:
    ctx = pipeline_ctx(tmp_path, repo, publisher)
    state = advance(new_state().evolve(stage=Stage.PRODUCT_OWNER), ctx)  # PRD, approved
    return advance(state, ctx), ctx  # the Architect runs and is approved


def test_after_the_architect_is_approved_the_design_record_pr_is_opened_first(
    tmp_path: Path, repo: Path
) -> None:
    publisher = FakePublisher()

    state, _ = at_architect_gate(tmp_path, repo, publisher)

    assert state.stage == Stage.DEVELOPER and state.status == Status.RUNNING
    [(result, _title)] = publisher.published
    assert result.branch == "agent/plan"
    record = state.artifacts["design_record.json"]
    assert json.loads((tmp_path / "demo" / record.path).read_text())["pr_url"].endswith("/pull/1")
    assert git(repo, "show", "agent/plan:docs/architecture.md").startswith("# Architecture")


def test_the_design_record_is_traced(tmp_path: Path, repo: Path) -> None:
    at_architect_gate(tmp_path, repo, FakePublisher())

    events = [json.loads(x) for x in trace_path(tmp_path, "demo").read_text().splitlines()]
    assert any(e["details"].get("design_record_pr") for e in events)


def test_a_failure_to_publish_it_halts_the_project(tmp_path: Path, repo: Path) -> None:
    state, _ = at_architect_gate(tmp_path, repo, FakePublisher(fail=True))

    assert state.status == Status.HALTED


def test_without_a_publisher_nothing_is_committed(tmp_path: Path, repo: Path) -> None:
    state, _ = at_architect_gate(tmp_path, repo, None)

    assert state.stage == Stage.DEVELOPER
    assert "design_record.json" not in state.artifacts
    assert "agent/plan" not in git(repo, "branch", "--format=%(refname:short)").split()


def test_the_pipeline_context_defaults_to_no_repo() -> None:
    ctx = PipelineContext(
        config=make_config(),
        runs_dir=Path("."),
        runner=StageAgent(),
        gates=FakeGates(),
        caps=FakeCaps(),
    )

    assert ctx.repo is None
