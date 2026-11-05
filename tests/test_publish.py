import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator.publish import Publisher, PublishError, load_token
from schemas import DevResult

TOKEN = "ghp_SECRETTOKEN123"
HTTPS = "https://github.com/me/project.git"
SSH = "git@github.com:me/project.git"


class Call:
    def __init__(self, args: list[str], env: dict[str, str], cwd: Path, input_text: str | None):
        self.args, self.env, self.cwd, self.input_text = args, env, cwd, input_text


class FakeRun:
    """Stands in for running commands. `script` maps a command prefix to its (code, out, err)."""

    def __init__(self, remote: str = HTTPS, **script: tuple[int, str, str]) -> None:
        self.calls: list[Call] = []
        self.remote = remote
        self.script = script

    def __call__(
        self, args: list[str], *, env: dict[str, str], cwd: Path, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(Call(args, env, cwd, input_text))
        key = " ".join(args[:3])
        for prefix, (code, out, err) in self.script.items():
            if key.startswith(prefix.replace("_", " ")):
                return subprocess.CompletedProcess(args, code, out, err)
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(args, 0, self.remote + "\n", "")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, "\n", "")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/me/project/pull/9\n", ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    def commands(self, *prefix: str) -> list[Call]:
        return [c for c in self.calls if c.args[: len(prefix)] == list(prefix)]


def dev_result(**overrides: Any) -> DevResult:
    fields: dict[str, Any] = {
        "task_id": "T1",
        "branch": "agent/T1",
        "summary": "Added the events table.",
        "deviations_from_plan": ["Used SQLite instead of Postgres"],
        "known_gaps": ["No index yet"],
    }
    return DevResult(**{**fields, **overrides})


def publisher(run: FakeRun, tmp_path: Path) -> Publisher:
    return Publisher(tmp_path, TOKEN, run=run)


def publish(run: FakeRun, tmp_path: Path, result: DevResult | None = None) -> DevResult:
    return publisher(run, tmp_path).publish(result or dev_result(), title="Create the events table")


# --- the push --------------------------------------------------------------------------------


def test_the_branch_is_pushed_with_a_plain_push_from_the_repo(tmp_path: Path) -> None:
    run = FakeRun()

    publish(run, tmp_path)

    [push] = run.commands("git", "push")
    assert push.args == ["git", "push", "--set-upstream", "origin", "agent/T1"]
    assert push.cwd == tmp_path


@pytest.mark.parametrize("flag", ["--force", "-f", "--force-with-lease", "--delete", "--mirror"])
def test_no_dangerous_push_flag_is_ever_used(tmp_path: Path, flag: str) -> None:
    run = FakeRun()

    publish(run, tmp_path)

    assert all(flag not in c.args for c in run.calls)


def test_main_is_never_pushed(tmp_path: Path) -> None:
    for branch in ("main", "master", "feature/x", "develop"):
        run = FakeRun()
        with pytest.raises(PublishError, match="agent/"):
            publish(run, tmp_path, dev_result(branch=branch))
        assert run.calls == []


def test_an_https_remote_gets_the_token_through_the_environment_not_the_arguments(
    tmp_path: Path,
) -> None:
    run = FakeRun(remote=HTTPS)

    publish(run, tmp_path)

    [push] = run.commands("git", "push")
    expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert push.env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert push.env["GIT_CONFIG_KEY_0"].endswith("extraheader")
    assert push.env["GIT_TERMINAL_PROMPT"] == "0"
    assert all(TOKEN not in " ".join(c.args) for c in run.calls)


def test_an_ssh_remote_uses_the_users_own_ssh_key_and_no_token_header(tmp_path: Path) -> None:
    run = FakeRun(remote=SSH)

    publish(run, tmp_path)

    [push] = run.commands("git", "push")
    assert not any(k.startswith("GIT_CONFIG") for k in push.env)


def test_a_failed_push_is_an_error_and_never_opens_a_pr(tmp_path: Path) -> None:
    run = FakeRun(git_push=(1, "", "rejected: protected branch"))

    with pytest.raises(PublishError, match="push"):
        publish(run, tmp_path)

    assert run.commands("gh") == []


# --- the pull request ------------------------------------------------------------------------


def test_the_pr_is_opened_against_main_from_the_task_branch(tmp_path: Path) -> None:
    run = FakeRun()

    result = publish(run, tmp_path)

    [create] = run.commands("gh", "pr", "create")
    args = create.args
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--head") + 1] == "agent/T1"
    assert args[args.index("--title") + 1] == "Create the events table"
    assert create.env["GH_TOKEN"] == TOKEN
    assert result.pr_url == "https://github.com/me/project/pull/9"


def test_the_pr_body_explains_the_work_and_its_gaps(tmp_path: Path) -> None:
    run = FakeRun()

    publish(run, tmp_path)

    [create] = run.commands("gh", "pr", "create")
    body = create.input_text or ""
    assert "Added the events table." in body
    assert "Used SQLite instead of Postgres" in body
    assert "No index yet" in body
    assert "T1" in body


def test_the_pr_body_carries_no_ai_attribution_lines(tmp_path: Path) -> None:
    run = FakeRun()

    publish(run, tmp_path)

    body = (run.commands("gh", "pr", "create")[0].input_text or "").lower()
    assert "generated with" not in body
    assert "co-authored" not in body


def test_an_empty_gaps_section_says_none(tmp_path: Path) -> None:
    run = FakeRun()

    publish(run, tmp_path, dev_result(deviations_from_plan=[], known_gaps=[]))

    body = run.commands("gh", "pr", "create")[0].input_text or ""
    assert body.count("None") >= 2


def test_a_revision_updates_the_existing_pr_instead_of_opening_another(tmp_path: Path) -> None:
    run = FakeRun(gh_pr_list=(0, "https://github.com/me/project/pull/4\n", ""))

    result = publish(run, tmp_path)

    assert run.commands("gh", "pr", "create") == []
    assert len(run.commands("git", "push")) == 1  # the new commits still go up
    assert result.pr_url == "https://github.com/me/project/pull/4"


def test_a_failed_pr_creation_is_an_error(tmp_path: Path) -> None:
    run = FakeRun(gh_pr_create=(1, "", "GraphQL: something went wrong"))

    with pytest.raises(PublishError, match="pull request"):
        publish(run, tmp_path)


def test_output_that_is_not_a_url_is_an_error(tmp_path: Path) -> None:
    run = FakeRun(gh_pr_create=(0, "created!\n", ""))

    with pytest.raises(PublishError, match="URL"):
        publish(run, tmp_path)


def test_the_original_result_is_not_changed(tmp_path: Path) -> None:
    original = dev_result()

    publish(FakeRun(), tmp_path, original)

    assert original.pr_url is None


# --- the token never leaks -------------------------------------------------------------------


def test_the_token_is_removed_from_error_messages(tmp_path: Path) -> None:
    run = FakeRun(git_push=(1, "", f"fatal: auth failed for https://x:{TOKEN}@github.com/"))

    with pytest.raises(PublishError) as error:
        publish(run, tmp_path)

    assert TOKEN not in str(error.value)
    assert "***" in str(error.value)


def test_the_token_is_removed_from_pr_errors_too(tmp_path: Path) -> None:
    run = FakeRun(gh_pr_create=(1, "", f"bad credentials {TOKEN}"))

    with pytest.raises(PublishError) as error:
        publish(run, tmp_path)

    assert TOKEN not in str(error.value)


def test_the_encoded_form_of_the_token_is_removed_too(tmp_path: Path) -> None:
    encoded = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    run = FakeRun(git_push=(1, "", f"sent header Authorization: Basic {encoded}"))

    with pytest.raises(PublishError) as error:
        publish(run, tmp_path)

    assert encoded not in str(error.value)


def test_the_token_is_not_in_the_publishers_repr(tmp_path: Path) -> None:
    assert TOKEN not in repr(publisher(FakeRun(), tmp_path))


# --- loading the token -----------------------------------------------------------------------


def test_the_token_comes_from_the_environment() -> None:
    assert load_token({"GITHUB_TOKEN": "  ghp_abc \n"}) == "ghp_abc"


@pytest.mark.parametrize("env", [{}, {"GITHUB_TOKEN": ""}, {"GITHUB_TOKEN": "   "}])
def test_a_missing_token_is_a_clear_error(env: dict[str, str]) -> None:
    with pytest.raises(PublishError, match="GITHUB_TOKEN"):
        load_token(env)


def test_the_diff_summary_compares_the_branch_with_main(tmp_path: Path) -> None:
    run = FakeRun(git_diff=(0, " a.py | 2 +-\n 1 file changed\n", ""))

    stat = publisher(run, tmp_path).diff_stat("agent/T1")

    [call] = run.commands("git", "diff")
    assert call.args == ["git", "diff", "--stat", "main...agent/T1"]
    assert "1 file changed" in stat


def test_an_empty_diff_says_so(tmp_path: Path) -> None:
    run = FakeRun(git_diff=(0, "", ""))

    assert "no file changes" in publisher(run, tmp_path).diff_stat("agent/T1")
