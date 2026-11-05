import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.cli_merge import CliMergeBackend
from orchestrator.config import CheckCommand, QaConfig
from orchestrator.qa_prepare import GitQaPreparer, QaError
from schemas import CheckStatus

GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]


def git(repo: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True)


class FakePulls:
    def __init__(self, merged: set[str]) -> None:
        self.merged = merged

    def is_merged(self, pr_url: str) -> bool:
        return pr_url in self.merged


def qa_config(command: str) -> QaConfig:
    return QaConfig(
        timeout_seconds=30,
        tail_lines=5,
        checks=[CheckCommand(name="show", command=command)],
    )


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone whose remote main has app.py and tests/test_app.py."""
    seed = tmp_path / "seed"
    (seed / "tests").mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text("x = 1\n")
    (seed / "tests" / "test_app.py").write_text("def test_x(): assert True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "first")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    path = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True)
    return path


def test_only_the_unmerged_pull_requests_are_reported(clone: Path) -> None:
    prep = GitQaPreparer(clone, FakePulls({"pr1"}), qa_config("true"))

    assert prep.unmerged(["pr1", "pr2", "pr3"]) == ["pr2", "pr3"]


def test_the_tools_run_in_a_checkout_of_the_remote_main_and_its_tests_are_collected(
    clone: Path,
) -> None:
    command = f"{shlex.quote(sys.executable)} -c \"print(open('app.py').read().strip())\""
    prep = GitQaPreparer(clone, FakePulls(set()), qa_config(command))

    materials = prep.run()

    assert [(c.name, c.status, c.output_tail) for c in materials.checks] == [
        ("show", CheckStatus.PASSED, "x = 1")
    ]
    assert "## tests/test_app.py" in materials.tests_md


def test_the_checkout_is_removed_afterwards_and_the_clone_is_untouched(clone: Path) -> None:
    prep = GitQaPreparer(clone, FakePulls(set()), qa_config("true"))

    prep.run()

    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout
    assert worktrees.count("\n") == 1  # only the clone itself
    assert subprocess.run(
        ["git", "branch"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.split() == ["*", "main"]


def test_the_checkout_is_removed_even_if_running_the_tools_blows_up(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kw: object) -> None:
        raise RuntimeError("tool runner crashed")

    monkeypatch.setattr("orchestrator.qa_prepare.run_checks", boom)
    prep = GitQaPreparer(clone, FakePulls(set()), qa_config("true"))

    with pytest.raises(RuntimeError):
        prep.run()

    worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout
    assert worktrees.count("\n") == 1


def test_a_remote_that_cannot_be_fetched_is_a_qa_error(clone: Path) -> None:
    git(clone, "remote", "set-url", "origin", str(clone.parent / "missing.git"))
    prep = GitQaPreparer(clone, FakePulls(set()), qa_config("true"))

    with pytest.raises(QaError, match="could not fetch"):
        prep.run()


def test_the_terminal_backend_lists_the_pull_requests_and_returns_the_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []

    def confirm(question: str, default: bool = False) -> bool:
        asked.append(question)
        return False

    monkeypatch.setattr("typer.confirm", confirm)

    answer = CliMergeBackend().wait_for_merge(["https://github.com/me/p/pull/1"])

    assert answer is False
    assert "https://github.com/me/p/pull/1" in capsys.readouterr().out
    assert asked
