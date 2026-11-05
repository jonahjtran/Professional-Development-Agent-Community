import shlex
import sys
from pathlib import Path

from orchestrator.config import CheckCommand, QaConfig
from orchestrator.qa_checks import clean_env, collect_tests, redact, run_checks
from schemas import CheckStatus

PY = shlex.quote(sys.executable)


def config(
    *checks: tuple[str, str], timeout: int = 20, tail: int = 5, requires: str | None = None
) -> QaConfig:
    return QaConfig(
        timeout_seconds=timeout,
        tail_lines=tail,
        checks=[CheckCommand(name=n, command=c, requires=requires) for n, c in checks],
    )


def py(code: str) -> str:
    return f"{PY} -c {shlex.quote(code)}"


# --- running checks --------------------------------------------------------------------------


def test_a_command_that_exits_zero_passes(tmp_path: Path) -> None:
    [result] = run_checks(config(("ok", py("print('all good')"))), tmp_path)

    assert result.status == CheckStatus.PASSED
    assert result.output_tail == "all good"


def test_a_command_that_exits_nonzero_fails_and_keeps_its_output(tmp_path: Path) -> None:
    [result] = run_checks(
        config(("bad", py("import sys; print('1 failed'); sys.exit(1)"))), tmp_path
    )

    assert result.status == CheckStatus.FAILED
    assert "1 failed" in result.output_tail


def test_checks_run_in_the_worktree(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here")

    [result] = run_checks(config(("cwd", py("print(open('marker.txt').read())"))), tmp_path)

    assert result.output_tail == "here"


def test_a_command_is_not_run_through_a_shell(tmp_path: Path) -> None:
    run_checks(config(("no shell", f"{PY} -c pass ; touch injected")), tmp_path)

    assert not (tmp_path / "injected").exists()


def test_a_command_that_runs_too_long_fails_with_a_timeout_note(tmp_path: Path) -> None:
    slow = py("import time; print('started', flush=True); time.sleep(30)")

    [result] = run_checks(config(("slow", slow), timeout=1), tmp_path)

    assert result.status == CheckStatus.FAILED
    assert "timed out after 1 seconds" in result.output_tail


def test_a_program_that_is_not_installed_is_skipped_not_passed(tmp_path: Path) -> None:
    cfg = config(("scan", "gitleaks detect"), requires="gitleaks")

    [result] = run_checks(cfg, tmp_path, which=lambda _name: None)

    assert result.status == CheckStatus.SKIPPED
    assert "gitleaks is not installed" in result.output_tail


def test_a_missing_executable_without_a_requires_is_also_skipped(tmp_path: Path) -> None:
    [result] = run_checks(config(("x", "definitely-not-a-real-program --flag")), tmp_path)

    assert result.status == CheckStatus.SKIPPED


def test_every_check_runs_even_after_one_fails(tmp_path: Path) -> None:
    cfg = config(("a", py("import sys; sys.exit(1)")), ("b", py("print('second')")))

    results = run_checks(cfg, tmp_path)

    assert [(r.name, r.status) for r in results] == [
        ("a", CheckStatus.FAILED),
        ("b", CheckStatus.PASSED),
    ]


def test_only_the_last_lines_of_output_are_kept(tmp_path: Path) -> None:
    [result] = run_checks(
        config(("long", py("print('\\n'.join(map(str, range(100))))")), tail=3), tmp_path
    )

    assert result.output_tail == "97\n98\n99"


# --- secrets ---------------------------------------------------------------------------------


def test_checks_do_not_see_secret_environment_variables(tmp_path: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_TOKEN": "ghp_supersecretvalue1234",
        "ANTHROPIC_API_KEY": "k",
    }
    code = "import os; print(os.environ.get('GITHUB_TOKEN'), os.environ.get('ANTHROPIC_API_KEY'))"

    [result] = run_checks(config(("env", py(code))), tmp_path, env=env)

    assert result.output_tail == "None None"


def test_clean_env_keeps_ordinary_variables_and_drops_the_outer_virtualenv() -> None:
    env = {"PATH": "/bin", "HOME": "/h", "VIRTUAL_ENV": "/outer", "MY_PASSWORD": "x"}

    assert clean_env(env) == {"PATH": "/bin", "HOME": "/h"}


def test_secret_values_and_token_shapes_are_removed_from_output() -> None:
    env = {"GITHUB_TOKEN": "realtokenvalue123"}
    text = (
        "auth realtokenvalue123 and github_pat_11ABCDEFG0123456789abcdef "
        "and sk-ant-api03-abcdefghijklmnop"
    )

    cleaned = redact(text, env)

    assert "realtokenvalue123" not in cleaned
    assert "github_pat_" not in cleaned and "sk-ant-" not in cleaned
    assert cleaned.count("***") == 3


def test_short_secret_values_do_not_blank_out_ordinary_words() -> None:
    assert redact("a yes answer", {"MY_TOKEN": "yes"}) == "a yes answer"


def test_a_secret_printed_by_a_check_never_reaches_the_report(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin"}
    printer = py("print('key is sk-ant-api03-abcdefghijklmnopqrstuv')")

    [result] = run_checks(config(("leak", printer)), tmp_path, env=env)

    assert "sk-ant-" not in result.output_tail


# --- collecting tests ------------------------------------------------------------------------


def write(root: Path, name: str, text: str = "def test_x(): pass") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_test_files_are_collected_under_their_paths(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_a.py", "def test_a(): assert 1")
    write(tmp_path, "pkg/thing_test.py")
    write(tmp_path, "src/app.py", "def app(): ...")

    text = collect_tests(tmp_path)

    assert "## tests/test_a.py" in text and "def test_a(): assert 1" in text
    assert "## pkg/thing_test.py" in text
    assert "src/app.py" not in text


def test_virtualenvs_and_dependencies_are_not_collected(tmp_path: Path) -> None:
    write(tmp_path, ".venv/lib/site-packages/pkg/tests/test_dep.py")
    write(tmp_path, "node_modules/x/test_y.py")
    write(tmp_path, "tests/test_real.py")

    text = collect_tests(tmp_path)

    assert "test_real.py" in text and "test_dep" not in text and "test_y" not in text


def test_a_project_without_tests_says_so(tmp_path: Path) -> None:
    write(tmp_path, "app.py", "x = 1")

    assert collect_tests(tmp_path) == "No test files were found."


def test_a_very_large_test_file_is_truncated(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_big.py", "# " + "x" * 50_000)

    assert "(file truncated)" in collect_tests(tmp_path)


def test_files_beyond_the_total_size_are_listed_but_omitted(tmp_path: Path) -> None:
    for i in range(10):
        write(tmp_path, f"tests/test_{i}.py", "# " + "x" * 19_000)

    text = collect_tests(tmp_path)

    assert text.count("omitted") >= 1
    assert all(f"test_{i}.py" in text for i in range(10))


def test_the_shipped_config_runs_tests_lint_types_audit_and_a_secret_scan() -> None:
    from orchestrator.config import load_config

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")

    assert [c.name for c in cfg.qa.checks] == [
        "tests",
        "lint",
        "types",
        "dependency audit",
        "secret scan",
    ]
    assert cfg.qa.checks[-1].requires == "gitleaks"
