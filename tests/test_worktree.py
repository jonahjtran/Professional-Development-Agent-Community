import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import (
    SELF_PROTECTED,
    WorktreeError,
    commits_ahead,
    create_worktree,
    has_uncommitted_changes,
    protected_for,
    remove_worktree,
)

GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


# --- creating --------------------------------------------------------------------------------


def test_a_worktree_is_a_new_branch_cut_from_main(repo: Path) -> None:
    wt = create_worktree(repo, "T1")

    assert wt.branch == "agent/T1"
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T1"
    assert (wt.path / "app.py").read_text() == "print('hi')\n"
    assert git(wt.path, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")


def test_the_worktree_lives_outside_the_repo_next_to_it(repo: Path) -> None:
    wt = create_worktree(repo, "T1")

    assert wt.path == repo.parent / "project-worktrees" / "T1"
    assert repo not in wt.path.parents


def test_a_custom_root_can_be_used(repo: Path, tmp_path: Path) -> None:
    wt = create_worktree(repo, "T1", root=tmp_path / "elsewhere")

    assert wt.path == tmp_path / "elsewhere" / "T1"


def test_the_users_own_checkout_is_left_alone(repo: Path) -> None:
    (repo / "scratch.txt").write_text("my uncommitted work")
    (repo / "app.py").write_text("print('edited')\n")

    create_worktree(repo, "T1")

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "scratch.txt").read_text() == "my uncommitted work"
    assert (repo / "app.py").read_text() == "print('edited')\n"


def test_untracked_secrets_never_appear_in_the_worktree(repo: Path) -> None:
    (repo / ".env").write_text("TOKEN=secret")

    wt = create_worktree(repo, "T1")

    assert not (wt.path / ".env").exists()


def test_the_worktree_is_a_linked_checkout_not_a_copy(repo: Path) -> None:
    wt = create_worktree(repo, "T1")

    assert (wt.path / ".git").is_file()  # a pointer back to the real repository


# --- keeping main up to date -----------------------------------------------------------------


def test_with_an_origin_the_branch_starts_from_the_latest_origin_main(
    repo: Path, tmp_path: Path
) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "fetch", "-q", "origin")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    (other / "new.py").write_text("x = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "newer")
    git(other, "push", "-q", "origin", "main")

    wt = create_worktree(repo, "T1")

    assert (wt.path / "new.py").exists()  # local main was behind; the new branch was not


def test_a_broken_origin_is_a_clear_error_not_a_stale_branch(repo: Path) -> None:
    git(repo, "remote", "add", "origin", "/nonexistent/path.git")

    with pytest.raises(WorktreeError, match="fetch"):
        create_worktree(repo, "T1")


def test_without_an_origin_local_main_is_used(repo: Path) -> None:
    assert create_worktree(repo, "T1").base == "main"


# --- refusing --------------------------------------------------------------------------------


def test_a_branch_that_already_exists_is_refused(repo: Path) -> None:
    git(repo, "branch", "agent/T1")

    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(repo, "T1")


def test_a_second_worktree_for_the_same_task_is_refused(repo: Path) -> None:
    create_worktree(repo, "T1")

    with pytest.raises(WorktreeError):
        create_worktree(repo, "T1")


def test_something_that_is_not_a_git_repo_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorktreeError, match="git repository"):
        create_worktree(plain, "T1")


def test_a_missing_base_branch_is_refused(repo: Path) -> None:
    with pytest.raises(WorktreeError, match="base"):
        create_worktree(repo, "T1", base="nope")


@pytest.mark.parametrize("task_id", ["../x", "a b", "", "-rf", "a/b", "T1;rm"])
def test_unsafe_task_ids_are_refused(repo: Path, task_id: str) -> None:
    with pytest.raises(WorktreeError, match="task id"):
        create_worktree(repo, task_id)


# --- inspecting and removing -----------------------------------------------------------------


def test_commits_ahead_counts_the_agents_commits(repo: Path) -> None:
    wt = create_worktree(repo, "T1")
    assert commits_ahead(wt) == 0

    (wt.path / "b.py").write_text("y = 2\n")
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-q", "-m", "add b")

    assert commits_ahead(wt) == 1


def test_uncommitted_changes_are_detected(repo: Path) -> None:
    wt = create_worktree(repo, "T1")
    assert not has_uncommitted_changes(wt)

    (wt.path / "b.py").write_text("y = 2\n")

    assert has_uncommitted_changes(wt)


def test_removing_a_worktree_deletes_the_folder_and_keeps_the_branch(repo: Path) -> None:
    wt = create_worktree(repo, "T1")

    remove_worktree(wt)

    assert not wt.path.exists()
    assert "agent/T1" in branches(repo)
    assert "T1" not in git(repo, "worktree", "list")


def test_removing_can_also_delete_the_branch(repo: Path) -> None:
    wt = create_worktree(repo, "T1")

    remove_worktree(wt, delete_branch=True)

    assert "agent/T1" not in branches(repo)


def test_a_removed_worktree_can_be_created_again_after_its_branch_is_deleted(repo: Path) -> None:
    remove_worktree(create_worktree(repo, "T1"), delete_branch=True)

    assert create_worktree(repo, "T1").path.exists()


# --- protecting the orchestrator when it works on itself -------------------------------------


def test_working_on_the_studio_repo_itself_protects_its_control_files(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    studio.mkdir()

    protected = protected_for(studio, studio_root=studio)

    assert protected == SELF_PROTECTED
    assert {"CLAUDE.md", "config.yaml", ".claude"} <= set(protected)
    assert any(p.endswith("permissions.py") for p in protected)


def test_working_on_another_repo_adds_no_extra_protection(tmp_path: Path) -> None:
    (tmp_path / "studio").mkdir()
    (tmp_path / "other").mkdir()

    assert protected_for(tmp_path / "other", studio_root=tmp_path / "studio") == ()


def test_a_symlinked_path_to_the_studio_repo_still_counts_as_it(tmp_path: Path) -> None:
    studio = tmp_path / "studio"
    studio.mkdir()
    (tmp_path / "alias").symlink_to(studio)

    assert protected_for(tmp_path / "alias", studio_root=studio) == SELF_PROTECTED


def test_an_existing_folder_is_refused_even_without_a_branch(repo: Path) -> None:
    target = repo.parent / "project-worktrees" / "T1"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("someone's files")

    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(repo, "T1")

    assert (target / "leftover.txt").exists()


def test_resuming_checks_out_the_existing_branch_with_its_commits(repo: Path) -> None:
    first = create_worktree(repo, "T1")
    (first.path / "b.py").write_text("y = 2\n")
    git(first.path, "add", "-A")
    git(first.path, "commit", "-q", "-m", "work")
    remove_worktree(first)

    again = create_worktree(repo, "T1", resume=True)

    assert (again.path / "b.py").exists()
    assert commits_ahead(again) == 1
    assert git(again.path, "rev-parse", "--abbrev-ref", "HEAD") == "agent/T1"


def test_resuming_with_no_existing_branch_just_creates_it(repo: Path) -> None:
    assert create_worktree(repo, "T1", resume=True).branch == "agent/T1"
