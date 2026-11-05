import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.newproject import (
    NewProject,
    NewProjectError,
    create_project,
    default_projects_dir,
    run_with_own_login,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "Test User")
        monkeypatch.setenv(f"{key}_EMAIL", "test@example.invalid")


class FakeGh:
    """Stands in for the command runner. `gh` calls are faked; every other command (git) is real.

    A fake `gh repo create --clone` makes the folder, runs `git init`, and points `origin` at a
    local bare repository, which plays the part of GitHub.
    """

    def __init__(
        self, tmp_path: Path, *, owner: str = "me", exists_on_github: bool = False
    ) -> None:
        self.tmp_path = tmp_path
        self.owner = owner
        self.exists_on_github = exists_on_github
        self.logged_in = True
        self.create_fails = False
        self.local_branch = "main"  # what `git clone` of an empty repository checks out
        self.origin_override: str | None = None
        self.calls: list[list[str]] = []
        self.remote = tmp_path / "github" / "remote.git"

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args == ["git", "remote", "get-url", "origin"] and self.origin_override:
            return subprocess.CompletedProcess(args, 0, self.origin_override + "\n", "")
        if args[0] != "gh":
            return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        if args[1:3] == ["api", "user"]:
            if not self.logged_in:
                return subprocess.CompletedProcess(args, 1, "", "You are not logged in")
            return subprocess.CompletedProcess(args, 0, self.owner + "\n", "")
        if args[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(args, 0 if self.exists_on_github else 1, "", "")
        if args[1:3] == ["repo", "create"]:
            if self.create_fails:
                return subprocess.CompletedProcess(args, 1, "", "name already taken")
            name = args[3].split("/")[1]
            subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
            target = cwd / name
            subprocess.run(["git", "init", "-q", "-b", self.local_branch, str(target)], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(self.remote)], cwd=target, check=True
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command {args}")

    def gh(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix) + 1] == ["gh", *prefix]]


def make(tmp_path: Path, fake: FakeGh, name: str = "habit-tracker", **kwargs: object) -> NewProject:
    kwargs.setdefault("confirm", lambda question: True)
    return create_project(name, tmp_path / "Github", run=fake, **kwargs)  # type: ignore[arg-type]


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


# --- creating --------------------------------------------------------------------------------


def test_a_private_repository_is_created_and_cloned_under_the_projects_folder(
    tmp_path: Path,
) -> None:
    fake = FakeGh(tmp_path)

    project = make(tmp_path, fake)

    assert project.path == tmp_path / "Github" / "habit-tracker"
    assert (project.owner, project.name, project.public) == ("me", "habit-tracker", False)
    [create] = fake.gh("repo", "create")
    assert create[3] == "me/habit-tracker"
    assert "--private" in create and "--public" not in create and "--clone" in create


def test_a_public_repository_can_be_requested(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)

    project = make(tmp_path, fake, public=True)

    assert project.public is True
    [create] = fake.gh("repo", "create")
    assert "--public" in create and "--private" not in create


def test_the_first_commit_has_a_readme_a_gitignore_and_is_on_main_and_pushed(
    tmp_path: Path,
) -> None:
    fake = FakeGh(tmp_path)

    project = make(tmp_path, fake)

    assert "habit-tracker" in (project.path / "README.md").read_text()
    ignore = (project.path / ".gitignore").read_text()
    assert ".env" in ignore and "__pycache__" in ignore and ".venv" in ignore
    assert git(project.path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(project.path, "rev-list", "--count", "HEAD") == "1"
    assert git(project.path, "log", "-1", "--format=%s") == "chore: initial commit"
    assert git(fake.remote, "rev-parse", "main") == git(project.path, "rev-parse", "HEAD")


def test_the_first_commit_message_has_no_attribution_trailer(tmp_path: Path) -> None:
    project = make(tmp_path, FakeGh(tmp_path))

    body = git(project.path, "log", "-1", "--format=%B")

    assert "Co-Authored-By" not in body and "Generated with" not in body


def test_the_result_carries_the_address_without_credentials(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)

    project = make(tmp_path, fake)

    assert project.url == str(fake.remote)


def test_the_branch_is_called_main_even_if_git_checked_out_another_name(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)
    fake.local_branch = "master"  # an empty repository clones onto git's default branch name

    project = make(tmp_path, fake)

    assert git(project.path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(fake.remote, "rev-parse", "main") == git(project.path, "rev-parse", "HEAD")


def test_a_credential_in_the_origin_address_is_removed_from_the_result(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)
    fake.origin_override = "https://someuser:sometoken@github.com/me/habit-tracker.git"

    project = make(tmp_path, fake)

    assert project.url == "https://github.com/me/habit-tracker.git"
    assert "sometoken" not in repr(project)


def test_the_projects_folder_is_created_if_it_does_not_exist(tmp_path: Path) -> None:
    assert not (tmp_path / "Github").exists()

    make(tmp_path, FakeGh(tmp_path))

    assert (tmp_path / "Github").is_dir()


# --- asking first ----------------------------------------------------------------------------


def test_the_human_is_asked_with_the_exact_name_visibility_and_folder(tmp_path: Path) -> None:
    questions: list[str] = []

    def ask(question: str) -> bool:
        questions.append(question)
        return True

    make(tmp_path, FakeGh(tmp_path), confirm=ask)

    [question] = questions
    assert "me/habit-tracker" in question
    assert "private" in question
    assert str(tmp_path / "Github" / "habit-tracker") in question


def test_declining_creates_nothing(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)

    with pytest.raises(NewProjectError, match="cancel"):
        make(tmp_path, fake, confirm=lambda q: False)

    assert fake.gh("repo", "create") == []
    assert not (tmp_path / "Github" / "habit-tracker").exists()


# --- refusing --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../escape", "a b", "", "UPPER", "-flag", "x" * 61, "a/b"])
def test_unsafe_names_are_refused(tmp_path: Path, name: str) -> None:
    fake = FakeGh(tmp_path)

    with pytest.raises(NewProjectError, match="name"):
        make(tmp_path, fake, name=name)

    assert fake.calls == []


def test_an_existing_local_folder_is_refused_and_left_alone(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)
    existing = tmp_path / "Github" / "habit-tracker"
    existing.mkdir(parents=True)
    (existing / "mine.txt").write_text("keep me")

    with pytest.raises(NewProjectError, match="already exists"):
        make(tmp_path, fake)

    assert (existing / "mine.txt").read_text() == "keep me"
    assert fake.gh("repo", "create") == []


def test_an_existing_github_repository_is_refused(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path, exists_on_github=True)

    with pytest.raises(NewProjectError, match="already exists on GitHub"):
        make(tmp_path, fake)

    assert fake.gh("repo", "create") == []


def test_not_being_signed_in_to_gh_is_a_clear_error(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)
    fake.logged_in = False

    with pytest.raises(NewProjectError, match="gh auth login"):
        make(tmp_path, fake)


def test_a_failed_creation_is_reported_and_nothing_is_committed(tmp_path: Path) -> None:
    fake = FakeGh(tmp_path)
    fake.create_fails = True

    with pytest.raises(NewProjectError, match="name already taken"):
        make(tmp_path, fake)

    assert [c for c in fake.calls if c[:2] == ["git", "commit"]] == []


# --- using your own login --------------------------------------------------------------------


def test_the_commands_run_without_the_agents_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_for_gh")
    monkeypatch.setenv("GH_TOKEN", "ghp_not_for_gh_either")

    done = run_with_own_login(
        [
            "python3",
            "-c",
            "import os; print(os.environ.get('GITHUB_TOKEN'), os.environ.get('GH_TOKEN'))",
        ],
        tmp_path,
    )

    assert done.stdout.strip() == "None None"


def test_the_default_projects_folder_is_your_github_folder_on_the_desktop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_STUDIO_PROJECTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_projects_dir() == tmp_path / "Desktop" / "Github"


def test_the_projects_folder_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_STUDIO_PROJECTS", str(tmp_path / "elsewhere"))

    assert default_projects_dir() == tmp_path / "elsewhere"


# --- the command -----------------------------------------------------------------------------


def stub(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the real repository creation; it asks the human, like the real one does."""
    asked: list[str] = []

    def fake_create(
        name: str, projects_dir: Path, *, public: bool, confirm: Callable[[str], bool]
    ) -> NewProject:
        answer = confirm("Create it?")
        asked.append(str(answer))
        if not answer:
            raise NewProjectError("cancelled by you; nothing was created")
        return NewProject(name, "me", projects_dir / name, "https://github.com/me/x.git", public)

    monkeypatch.setattr("orchestrator.cli.create_project", fake_create)
    return asked


def test_studio_new_prints_where_it_went_and_what_to_run_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub(monkeypatch)

    result = runner.invoke(app, ["new", "habit-tracker", "--projects-dir", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "habit-tracker") in result.output
    assert "studio run" in result.output and "--agent full" in result.output
    assert "--repo" in result.output


def test_studio_new_asks_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked = stub(monkeypatch)

    result = runner.invoke(
        app, ["new", "habit-tracker", "--projects-dir", str(tmp_path)], input="n\n"
    )

    assert asked == ["False"]
    assert result.exit_code == 1 and "cancelled" in result.output


def test_answering_yes_creates_the_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked = stub(monkeypatch)

    result = runner.invoke(
        app, ["new", "habit-tracker", "--projects-dir", str(tmp_path)], input="y\n"
    )

    assert asked == ["True"]
    assert result.exit_code == 0, result.output


def test_a_public_project_is_flagged_in_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub(monkeypatch)

    result = runner.invoke(
        app, ["new", "habit-tracker", "--public", "--projects-dir", str(tmp_path), "--yes"]
    )

    assert "public" in result.output.lower()
