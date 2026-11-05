import subprocess
from pathlib import Path

import pytest
import typer

from orchestrator.cli import AgentKind, _new_project, _repo_for_resume
from orchestrator.publish import Publisher, PublishError, normalize_repo_url
from orchestrator.state import load_state, save_state
from schemas import DevResult, Track
from tests.factories import new_state
from tests.test_publish import HTTPS, SSH, TOKEN, FakeRun, dev_result


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    path = tmp_path / "my-project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "remote", "add", "origin", "https://github.com/me/my-project.git")
    return path


# --- state -----------------------------------------------------------------------------------


def test_a_new_project_has_no_repository_recorded() -> None:
    state = new_state()

    assert state.repo_path is None and state.repo_url is None


def test_the_repository_survives_save_and_load(tmp_path: Path) -> None:
    save_state(
        tmp_path, new_state().evolve(repo_path="/work/p", repo_url="https://github.com/me/p.git")
    )

    loaded = load_state(tmp_path, "demo")

    assert (loaded.repo_path, loaded.repo_url) == ("/work/p", "https://github.com/me/p.git")


# --- comparing repository addresses ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/p.git",
        "https://github.com/me/p",
        "https://github.com/me/p/",
        "HTTPS://GitHub.com/Me/P.git",
        "git@github.com:me/p.git",
        "ssh://git@github.com/me/p.git",
        "https://someuser:sometoken@github.com/me/p.git",
    ],
)
def test_different_spellings_of_the_same_repository_compare_equal(url: str) -> None:
    assert normalize_repo_url(url) == "github.com/me/p"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/other.git",
        "https://github.com/someone/p.git",
        "https://evil.example/me/p.git",
    ],
)
def test_different_repositories_compare_different(url: str) -> None:
    assert normalize_repo_url(url) != "github.com/me/p"


def test_the_normal_form_never_contains_a_credential() -> None:
    assert "sometoken" not in normalize_repo_url("https://u:sometoken@github.com/me/p.git")


# --- the push guard --------------------------------------------------------------------------


def publish_with(remote: str, expected: str, tmp_path: Path) -> tuple[FakeRun, DevResult | None]:
    run = FakeRun(remote=remote)
    publisher = Publisher(tmp_path, TOKEN, expected_url=expected, run=run)
    return run, publisher.publish(dev_result(), title="t")


def test_pushing_to_the_recorded_repository_works_over_https_or_ssh(tmp_path: Path) -> None:
    for remote in (HTTPS, SSH):
        run, result = publish_with(remote, HTTPS, tmp_path)
        assert result is not None and result.pr_url
        assert len(run.commands("git", "push")) == 1


def test_a_different_origin_is_refused_before_anything_is_pushed(tmp_path: Path) -> None:
    run = FakeRun(remote="https://github.com/me/some-other-repo.git")
    publisher = Publisher(tmp_path, TOKEN, expected_url=HTTPS, run=run)

    with pytest.raises(PublishError, match="origin"):
        publisher.publish(dev_result(), title="t")

    assert run.commands("git", "push") == []
    assert run.commands("gh") == []


def test_the_refusal_names_both_repositories_but_never_the_token(tmp_path: Path) -> None:
    run = FakeRun(remote="https://x:ghp_LEAKED@github.com/me/other.git")
    publisher = Publisher(tmp_path, TOKEN, expected_url=HTTPS, run=run)

    with pytest.raises(PublishError) as error:
        publisher.publish(dev_result(), title="t")

    message = str(error.value)
    assert "github.com/me/other" in message and "github.com/me/project" in message
    assert "ghp_LEAKED" not in message and TOKEN not in message


# --- starting a project ----------------------------------------------------------------------


def start(repo: Path | None, agent: AgentKind = AgentKind.FULL) -> object:
    return _new_project("An idea", "demo", Track.ENGINEERING, agent, repo)


def test_a_full_run_needs_a_repository(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit) as error:
        start(None)

    assert error.value.exit_code == 1


def test_the_repository_and_its_address_are_recorded_in_the_project(project_repo: Path) -> None:
    state = _new_project("An idea", "demo", Track.ENGINEERING, AgentKind.FULL, project_repo)

    assert state.repo_path == str(project_repo.resolve())
    assert state.repo_url == "https://github.com/me/my-project.git"


def test_a_repository_without_an_origin_is_refused(tmp_path: Path) -> None:
    bare = tmp_path / "no-origin"
    bare.mkdir()
    git(bare, "init", "-q", "-b", "main")

    with pytest.raises(typer.Exit):
        start(bare)


def test_something_that_is_not_a_git_repository_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(typer.Exit):
        start(plain)


def test_other_agents_do_not_need_or_record_a_repository(tmp_path: Path) -> None:
    for agent in (AgentKind.ECHO, AgentKind.CLAUDE, AgentKind.API):
        state = _new_project("An idea", "demo", Track.ENGINEERING, agent, None)
        assert state.repo_path is None


# --- resuming --------------------------------------------------------------------------------


def test_resuming_uses_the_repository_recorded_in_the_project(project_repo: Path) -> None:
    state = new_state().evolve(repo_path=str(project_repo.resolve()), repo_url="u")

    assert _repo_for_resume(state, AgentKind.FULL, None) == project_repo.resolve()


def test_resuming_with_a_different_repository_is_refused(
    project_repo: Path, tmp_path: Path
) -> None:
    state = new_state().evolve(repo_path=str(project_repo.resolve()), repo_url="u")

    with pytest.raises(typer.Exit):
        _repo_for_resume(state, AgentKind.FULL, tmp_path)


def test_resuming_with_the_same_repository_is_fine(project_repo: Path) -> None:
    state = new_state().evolve(repo_path=str(project_repo.resolve()), repo_url="u")

    assert _repo_for_resume(state, AgentKind.FULL, project_repo) == project_repo.resolve()


def test_resuming_a_full_run_for_a_project_with_no_repository_is_refused() -> None:
    with pytest.raises(typer.Exit):
        _repo_for_resume(new_state(), AgentKind.FULL, None)


def test_other_agents_resume_without_a_repository() -> None:
    assert _repo_for_resume(new_state(), AgentKind.ECHO, None) is None


def test_a_credential_inside_the_origin_address_is_never_stored(tmp_path: Path) -> None:
    repo = tmp_path / "leaky"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "https://someuser:sometoken@github.com/me/leaky.git")

    state = _new_project("An idea", "demo", Track.ENGINEERING, AgentKind.FULL, repo)

    assert state.repo_url == "https://github.com/me/leaky.git"
    assert "sometoken" not in state.model_dump_json()
