import json
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from orchestrator.career_publish import CareerPublishError, GitCareerPublisher, repo_owner
from orchestrator.cli import AgentKind, _context, _show_career_results, app
from orchestrator.publish import PublishError
from orchestrator.state import Stage
from schemas import DevResult
from tests.test_career_pipeline import Coach, FakeCareer, approve, at_career, make_ctx
from tests.test_career_rules import resume
from tests.test_career_stage import good_plan
from tests.test_pipeline import FakeGates

GIT = ["git", "-c", "user.email=a@b.c", "-c", "user.name=tester"]
RESUME_TEXT = "# header comment\n" + yaml.safe_dump(resume(3, 2).model_dump(mode="json"))
PROJECTS_JS = 'export const projects = [\n    {\n        slug: "jamtown",\n    },\n];\n'


def git(repo: Path, *args: str) -> str:
    done = subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def make_clone(root: Path, name: str, files: dict[str, str]) -> Path:
    seed = root / f"{name}-seed"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    for path, text in files.items():
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "x")
    origin = root / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    clone = root / name
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return clone


class FakePulls:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.opened: list[tuple[str, str]] = []

    def publish(self, result: DevResult, *, title: str) -> DevResult:
        if self.fail:
            raise PublishError("push failed")
        self.opened.append((result.branch, title))
        return result.model_copy(
            update={"pr_url": f"https://github.com/me/x/pull/{len(self.opened)}"}
        )


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.project = make_clone(tmp_path, "project", {"README.md": "# stub"})
        self.profile = make_clone(tmp_path, "career-profile", {"resume.yaml": RESUME_TEXT})
        self.site = make_clone(tmp_path, "site", {"src/data/projects.js": PROJECTS_JS})
        self.project_pulls, self.profile_pulls, self.site_pulls = (
            FakePulls(),
            FakePulls(),
            FakePulls(),
        )
        self.builds: list[str] = []
        self.gh_calls: list[list[str]] = []
        self.gh_fails = False
        self.build_fails = False

    def build(self, path: Path) -> None:
        self.builds.append((path / "src/data/projects.js").read_text())
        if self.build_fails:
            raise CareerPublishError("`npm run build` failed")

    def own_login(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.gh_calls.append(args)
        return subprocess.CompletedProcess(args, 1 if self.gh_fails else 0, "", "no admin rights")

    def publisher(self, with_site: bool = True) -> GitCareerPublisher:
        return GitCareerPublisher(
            project_repo=self.project,
            project_url="https://github.com/me/project.git",
            project_pulls=self.project_pulls,
            profile_repo=self.profile,
            profile_pulls=self.profile_pulls,
            website_repo=self.site if with_site else None,
            website_pulls=self.site_pulls if with_site else None,
            build=self.build,
            own_login=self.own_login,
        )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def show(repo: Path, branch: str, path: str) -> str:
    return git(repo, "show", f"{branch}:{path}")


# --- project docs ----------------------------------------------------------------------------


def test_the_readme_and_case_study_go_on_their_own_branch_with_a_pull_request(fx: Fixture) -> None:
    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"readme", "case_study"})

    assert out == {"project docs": "https://github.com/me/x/pull/1"}
    assert fx.project_pulls.opened == [("agent/career-docs", "docs: README and case study")]
    assert show(fx.project, "agent/career-docs", "README.md") == "# R"
    assert show(fx.project, "agent/career-docs", "docs/case_study.md") == "# C"
    assert git(fx.project, "rev-parse", "main") == git(fx.project, "rev-parse", "origin/main")


def test_only_the_approved_document_is_written(fx: Fixture) -> None:
    fx.publisher().publish(slug="demo", plan=good_plan(), approved={"case_study"})

    assert show(fx.project, "agent/career-docs", "README.md") == "# stub"  # untouched
    assert show(fx.project, "agent/career-docs", "docs/case_study.md") == "# C"


def test_publishing_again_reuses_the_branch_and_opens_the_pull_request_once_more(
    fx: Fixture,
) -> None:
    fx.publisher().publish(slug="demo", plan=good_plan(), approved={"readme"})
    first = git(fx.project, "rev-parse", "agent/career-docs")

    fx.publisher().publish(slug="demo", plan=good_plan(), approved={"readme"})

    assert git(fx.project, "rev-parse", "agent/career-docs") == first
    assert len(fx.project_pulls.opened) == 2


# --- the resume ------------------------------------------------------------------------------


def test_the_resume_data_and_paste_ready_snippets_go_to_the_private_profile_repo(
    fx: Fixture,
) -> None:
    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"resume:1"})

    assert list(out) == ["resume proposal"] and out["resume proposal"].startswith("https://")
    assert fx.profile_pulls.opened == [("agent/career-demo", "Resume proposal from demo")]
    data = show(fx.profile, "agent/career-demo", "resume.yaml")
    assert data.startswith("# header comment")
    titles = [e["title"] for s in yaml.safe_load(data)["sections"] for e in s["entries"]]
    assert "Agent Studio" in titles
    snippets = show(fx.profile, "agent/career-demo", "proposals/demo/resume_snippets.tex")
    assert "\\resumeProject" in snippets and "\\resumeItem{Passed 9 checks}" in snippets
    notes = show(fx.profile, "agent/career-demo", "proposals/demo/resume_changes.md")
    assert "Paste `resume_snippets.tex` into the Overleaf file" in notes
    assert fx.project_pulls.opened == []  # nothing else was opened


def test_a_resume_change_that_breaks_the_page_budget_fails_without_a_pull_request(
    tmp_path: Path,
) -> None:
    full = Fixture(tmp_path)
    (full.profile / "resume.yaml").write_text(yaml.safe_dump(resume(2, 2).model_dump(mode="json")))
    git(full.profile, "add", "-A")
    git(full.profile, "commit", "-q", "-m", "full")
    git(full.profile, "push", "-q", "origin", "main")

    out = full.publisher().publish(slug="demo", plan=good_plan(), approved={"resume:1"})

    assert "page budget" in out["resume proposal"] and out["resume proposal"].startswith("failed")
    assert full.profile_pulls.opened == []
    assert "agent/career-demo" not in git(full.profile, "branch")


# --- the website -----------------------------------------------------------------------------


def test_the_website_pull_request_adds_the_entry_after_the_build_passes(fx: Fixture) -> None:
    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"website"})

    assert out["website"].startswith("https://")
    assert fx.site_pulls.opened == [("agent/portfolio-demo", "Add Agent Studio to the projects")]
    changed = show(fx.site, "agent/portfolio-demo", "src/data/projects.js")
    assert changed.startswith('export const projects = [\n    {\n        slug: "agent-studio"')
    assert changed.endswith(PROJECTS_JS[len("export const projects = [\n") :].rstrip())
    assert 'slug: "agent-studio"' in fx.builds[0]  # the build ran on the edited file


def test_only_the_projects_file_changes_on_the_website_branch(fx: Fixture) -> None:
    fx.publisher().publish(slug="demo", plan=good_plan(), approved={"website"})

    files = git(fx.site, "diff", "--name-only", "main...agent/portfolio-demo").splitlines()

    assert files == ["src/data/projects.js"]


def test_a_failing_build_opens_nothing_and_leaves_no_branch_behind(fx: Fixture) -> None:
    fx.build_fails = True

    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"website"})

    assert out["website"].startswith("failed:") and "npm run build" in out["website"]
    assert fx.site_pulls.opened == []
    assert "agent/portfolio-demo" not in git(fx.site, "branch")
    assert git(fx.site, "worktree", "list").count("\n") == 0  # only the clone itself


def test_a_duplicate_slug_fails_cleanly(fx: Fixture) -> None:
    plan = good_plan()
    assert plan.website_entry is not None
    dup = plan.model_copy(
        update={"website_entry": plan.website_entry.model_copy(update={"slug": "jamtown"})}
    )

    out = fx.publisher().publish(slug="demo", plan=dup, approved={"website"})

    assert "already has" in out["website"] and fx.site_pulls.opened == []


def test_without_a_website_folder_the_website_item_fails_with_an_explanation(fx: Fixture) -> None:
    out = fx.publisher(with_site=False).publish(slug="demo", plan=good_plan(), approved={"website"})

    assert "AGENT_STUDIO_WEBSITE" in out["website"]


# --- settings, and isolation -----------------------------------------------------------------


def test_the_description_and_topics_are_set_with_the_persons_own_login(fx: Fixture) -> None:
    plan = good_plan().model_copy(
        update={"repo_description": "A gated pipeline.", "repo_topics": ["multi-agent", "python"]}
    )

    out = fx.publisher().publish(slug="demo", plan=plan, approved={"repo_meta"})

    assert out == {"repository settings": "updated https://github.com/me/project"}
    assert fx.gh_calls == [
        [
            "gh",
            "repo",
            "edit",
            "me/project",
            "--description",
            "A gated pipeline.",
            "--add-topic",
            "multi-agent",
            "--add-topic",
            "python",
        ]
    ]


def test_a_settings_failure_is_reported_not_raised(fx: Fixture) -> None:
    fx.gh_fails = True

    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"repo_meta"})

    assert out["repository settings"] == "failed: gh repo edit failed: no admin rights"


def test_one_failing_target_does_not_stop_the_others(fx: Fixture) -> None:
    fx.build_fails = True

    out = fx.publisher().publish(
        slug="demo", plan=good_plan(), approved={"readme", "resume:1", "website"}
    )

    assert out["project docs"].startswith("https://")
    assert out["resume proposal"].startswith("https://")
    assert out["website"].startswith("failed:")


def test_a_push_failure_is_reported_with_its_reason(fx: Fixture) -> None:
    fx.project_pulls.fail = True

    out = fx.publisher().publish(slug="demo", plan=good_plan(), approved={"readme"})

    assert out == {"project docs": "failed: push failed"}


def test_nothing_approved_publishes_nothing(fx: Fixture) -> None:
    assert fx.publisher().publish(slug="demo", plan=good_plan(), approved=set()) == {}
    assert fx.project_pulls.opened == [] and fx.gh_calls == []


# --- the owner check -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "owner"),
    [
        ("https://github.com/me/project.git", "me"),
        ("git@github.com:Me/project.git", "me"),
        ("https://x-access-token:t@github.com/me/project", "me"),
        ("https://gitlab.com/me/project", None),
    ],
)
def test_the_owner_of_a_repository_address(url: str, owner: str | None) -> None:
    assert repo_owner(url) == owner


def _full_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_url: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/me/project.git"],
        cwd=project,
        check=True,
    )
    folder = tmp_path / "profile"
    folder.mkdir()
    (folder / "resume.yaml").write_text("sections:\n  - {name: P, max_bullets: 1, entries: []}\n")
    (folder / "goals.yaml").write_text("tracks:\n  tech_consulting: {target_roles: [a]}\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=folder, check=True)
    subprocess.run(["git", "remote", "add", "origin", profile_url], cwd=folder, check=True)
    monkeypatch.setenv("AGENT_STUDIO_PROFILE", str(folder))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_not_real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return project


def test_a_profile_repository_on_someone_elses_account_is_refused_at_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _full_env(tmp_path, monkeypatch, "https://github.com/stranger/career-profile.git")
    config = Path(__file__).parent.parent / "config.yaml"

    result = CliRunner().invoke(
        app,
        ["run", "An idea", "--slug", "demo", "--agent", "full", "--repo", str(project)]
        + ["--runs-dir", str(tmp_path / "runs"), "--config", str(config)],
    )

    assert result.exit_code == 1 and "must all be on your own account" in result.output


def test_a_profile_that_is_not_a_clone_is_refused_at_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _full_env(tmp_path, monkeypatch, "https://github.com/me/career-profile.git")
    subprocess.run(["rm", "-rf", str(tmp_path / "profile" / ".git")], check=True)
    config = Path(__file__).parent.parent / "config.yaml"

    result = CliRunner().invoke(
        app,
        ["run", "An idea", "--slug", "demo", "--agent", "full", "--repo", str(project)]
        + ["--runs-dir", str(tmp_path / "runs"), "--config", str(config)],
    )

    assert result.exit_code == 1 and "needs to be a git clone" in result.output


def test_the_full_context_builds_a_publisher_when_everything_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _full_env(tmp_path, monkeypatch, "https://github.com/me/career-profile.git")
    config = Path(__file__).parent.parent / "config.yaml"

    ctx = _context(
        config, tmp_path / "runs", AgentKind.FULL, project, "https://github.com/me/project.git"
    )

    assert isinstance(ctx.career_publisher, GitCareerPublisher)


# --- inside the pipeline ---------------------------------------------------------------------


class RecordingPublisher:
    def __init__(self, results: dict[str, str]) -> None:
        self.results = results
        self.calls: list[tuple[str, set[str]]] = []

    def publish(self, *, slug: str, plan: object, approved: set[str]) -> dict[str, str]:
        self.calls.append((slug, approved))
        return self.results


def run_with_publisher(tmp_path: Path, publisher: RecordingPublisher | None, *items: str):  # type: ignore[no-untyped-def]
    from orchestrator.pipeline import advance
    from orchestrator.state import Status

    ctx = make_ctx(tmp_path, Coach(), FakeCareer(), FakeGates(approve(*items)))
    ctx = ctx.__class__(**{**ctx.__dict__, "career_publisher": publisher})
    state = at_career(tmp_path)
    while state.status in (Status.RUNNING, Status.AWAITING_HUMAN):
        state = advance(state, ctx)
    return state


def test_approved_items_are_published_and_where_they_went_is_recorded(tmp_path: Path) -> None:
    publisher = RecordingPublisher({"website": "https://github.com/me/site/pull/3"})

    state = run_with_publisher(tmp_path, publisher, "website", "readme")

    assert publisher.calls == [("demo", {"website", "readme"})]
    store_path = tmp_path / "demo" / state.artifacts["career_published.json"].path
    assert json.loads(store_path.read_text()) == {"website": "https://github.com/me/site/pull/3"}


def test_the_project_still_finishes_when_a_target_failed(tmp_path: Path) -> None:
    from orchestrator.state import Status

    state = run_with_publisher(
        tmp_path, RecordingPublisher({"website": "failed: build"}), "website"
    )

    assert state.status == Status.DONE


def test_nothing_is_published_without_a_publisher_or_when_nothing_was_approved(
    tmp_path: Path,
) -> None:
    assert "career_published.json" not in run_with_publisher(tmp_path, None, "readme").artifacts
    empty = RecordingPublisher({})
    state = run_with_publisher(tmp_path / "again", empty)
    assert "career_published.json" not in state.artifacts


def test_the_final_message_lists_each_pull_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = run_with_publisher(
        tmp_path, RecordingPublisher({"website": "https://github.com/me/site/pull/3"}), "website"
    )

    _show_career_results(state, tmp_path)

    out = capsys.readouterr().out
    assert "website: https://github.com/me/site/pull/3" in out
    assert "nothing was merged for you" in out


def test_no_career_results_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    from tests.factories import new_state

    _show_career_results(new_state().evolve(stage=Stage.CAREER_COACH), Path("."))

    assert capsys.readouterr().out == ""
