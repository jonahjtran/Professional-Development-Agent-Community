from pathlib import Path

import pytest

from orchestrator.config import LocalConfig, load_config
from orchestrator.developer_backend import (
    BACKENDS,
    check_local,
    developer_env,
    developer_model,
    get_developer_backend,
    set_global_backend,
    set_project_backend,
)
from orchestrator.project_settings import get_autonomy, set_autonomy
from tests.factories import make_config

REPO_CONFIG = Path(__file__).parent.parent / "config.yaml"
LOCAL = LocalConfig(endpoint="http://localhost:11434", model="qwen3-coder:test", min_context=65536)


# --- the models in config.yaml ----------------------------------------------------------------


def test_the_developer_defaults_to_sonnet_5_5_and_the_other_agents_keep_opus() -> None:
    config = load_config(REPO_CONFIG)

    assert config.models.developer == "claude-sonnet-5-5"
    assert config.models.strong == "claude-opus-5-5"
    assert config.models.fast == "claude-haiku-5-5"


def test_the_local_model_is_set_in_config_yaml() -> None:
    config = load_config(REPO_CONFIG)

    assert config.local.endpoint == "http://localhost:11434"
    assert config.local.model == "qwen3-coder:30b"  # the only size that fits a laptop (30B-A3B)
    assert config.local.min_context >= 64_000  # Claude Code needs a large context


def test_a_config_without_the_new_blocks_still_loads_with_defaults() -> None:
    config = make_config()

    assert config.models.developer == "claude-sonnet-5-5"
    assert config.local.endpoint.startswith("http://localhost")


# --- which backend a project uses -------------------------------------------------------------


def test_claude_is_the_default(tmp_path: Path) -> None:
    choice = get_developer_backend(tmp_path / "runs", "demo")

    assert (choice.backend, choice.source) == ("claude", "default")


def test_the_global_choice_applies_to_every_project(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    set_global_backend("local")

    choice = get_developer_backend(runs, "demo")

    assert (choice.backend, choice.source) == ("local", "global")


def test_a_project_choice_beats_the_global_one_and_can_be_cleared(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    set_global_backend("local")

    set_project_backend(runs, "demo", "claude")
    assert get_developer_backend(runs, "demo").backend == "claude"
    assert get_developer_backend(runs, "demo").source == "project"
    assert get_developer_backend(runs, "other").backend == "local"

    set_project_backend(runs, "demo", None)
    assert get_developer_backend(runs, "demo").source == "global"


def test_an_unknown_backend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_global_backend("gpt")
    with pytest.raises(ValueError):
        set_project_backend(tmp_path, "demo", "gpt")


def test_a_damaged_settings_file_falls_back_to_claude(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "settings.json").write_text("{nope")

    assert get_developer_backend(tmp_path, "demo").backend == "claude"


def test_the_two_settings_do_not_overwrite_each_other(tmp_path: Path) -> None:
    set_autonomy(tmp_path, "demo", "autonomous")
    set_project_backend(tmp_path, "demo", "local")

    assert get_autonomy(tmp_path, "demo") == "autonomous"
    assert get_developer_backend(tmp_path, "demo").backend == "local"
    set_autonomy(tmp_path, "demo", "cautious")
    assert get_developer_backend(tmp_path, "demo").backend == "local"


def test_the_backends_are_claude_and_local() -> None:
    assert BACKENDS == ("claude", "local")


# --- what each backend runs -------------------------------------------------------------------


def test_claude_uses_the_developer_model_from_config() -> None:
    assert developer_model("claude", make_config()) == "claude-sonnet-5-5"


def test_local_uses_the_local_model() -> None:
    config = make_config().model_copy(update={"local": LOCAL})

    assert developer_model("local", config) == "qwen3-coder:test"


def test_the_local_backend_points_claude_code_at_the_local_server_with_no_real_key() -> None:
    env = developer_env("local", LOCAL)

    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    assert env["ANTHROPIC_API_KEY"] == ""
    # Claude Code's own helper models must be the local model too, or it would ask for Claude names
    for name in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert env[name] == "qwen3-coder:test", name
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_the_claude_backend_adds_nothing_that_redirects_the_agent() -> None:
    env = developer_env("claude", LOCAL)

    assert "ANTHROPIC_BASE_URL" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_the_local_backend_hides_other_secrets_from_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

    env = developer_env("local", LOCAL)

    assert env["GITHUB_TOKEN"] == "" and env["CLAUDE_CODE_SUBAGENT_MODEL"] == "qwen3-coder:test"


# --- is the local server ready ----------------------------------------------------------------


def fetcher(routes: dict[str, object]):  # type: ignore[no-untyped-def]
    def fetch(url: str, timeout: float) -> object:
        for suffix, reply in routes.items():
            if url.endswith(suffix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise OSError(f"no route {url}")

    return fetch


def test_a_ready_server_passes() -> None:
    status = check_local(
        LOCAL,
        fetch=fetcher(
            {
                "/api/tags": {"models": [{"name": "qwen3-coder:test"}]},
                "/api/ps": {"models": [{"name": "qwen3-coder:test", "context_length": 131072}]},
            }
        ),
    )

    assert status.ok and status.reachable and status.installed
    assert status.context == 131072


def test_an_unreachable_server_says_how_to_start_it() -> None:
    status = check_local(LOCAL, fetch=fetcher({"/api/tags": OSError("refused")}))

    assert not status.ok and not status.reachable
    assert "ollama serve" in status.fix.lower()


def test_a_missing_model_says_how_to_pull_it() -> None:
    status = check_local(LOCAL, fetch=fetcher({"/api/tags": {"models": [{"name": "other:1b"}]}}))

    assert status.reachable and not status.installed and not status.ok
    assert "ollama pull qwen3-coder:test" in status.fix


def test_a_loaded_model_with_a_small_context_is_a_failure_with_the_fix() -> None:
    status = check_local(
        LOCAL,
        fetch=fetcher(
            {
                "/api/tags": {"models": [{"name": "qwen3-coder:test"}]},
                "/api/ps": {"models": [{"name": "qwen3-coder:test", "context_length": 4096}]},
            }
        ),
    )

    assert not status.ok and status.context == 4096
    assert "OLLAMA_CONTEXT_LENGTH" in status.fix


def test_a_model_that_is_not_loaded_yet_passes_with_the_context_unknown() -> None:
    status = check_local(
        LOCAL,
        fetch=fetcher(
            {"/api/tags": {"models": [{"name": "qwen3-coder:test"}]}, "/api/ps": {"models": []}}
        ),
    )

    assert status.ok and status.context is None


def test_a_tag_without_latest_matches_the_installed_name() -> None:
    config = LocalConfig(endpoint="http://localhost:11434", model="qwen3-coder", min_context=1)

    status = check_local(
        config,
        fetch=fetcher(
            {"/api/tags": {"models": [{"name": "qwen3-coder:latest"}]}, "/api/ps": {"models": []}}
        ),
    )

    assert status.installed


# --- the model a task is running on -----------------------------------------------------------


def test_the_model_a_task_started_on_is_recorded_and_can_be_read_back(tmp_path: Path) -> None:
    from orchestrator.developer_backend import read_running_model, record_running_model

    record_running_model(tmp_path, "demo", "T3", "local", "qwen3-coder:27b")

    assert read_running_model(tmp_path, "demo") == ("T3", "local", "qwen3-coder:27b")
    assert read_running_model(tmp_path, "other") is None


def test_a_damaged_record_is_none(tmp_path: Path) -> None:
    from orchestrator.developer_backend import read_running_model

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "developer_model.json").write_text("{nope")

    assert read_running_model(tmp_path, "demo") is None
