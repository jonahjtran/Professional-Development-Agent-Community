"""Which model runs the Developer: Claude through Anthropic's API, or an open-source model locally.

The Developer always runs inside Claude Code's harness, so its tools, permission policy and sandbox
are the same either way. For the local backend, Claude Code is pointed at Ollama's
Anthropic-compatible endpoint with a placeholder token (the real Anthropic key is blanked), and
every model name Claude Code might ask for is set to the local model.

The choice is per project (it overrides) or global, is read at the start of every task, and so can
be switched at any time: a task already running finishes on the model it started with.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.config import Config, LocalConfig
from orchestrator.envsafe import hidden_env
from orchestrator.project_settings import project_settings_path, read_settings, update_settings
from orchestrator.rules import home_dir
from orchestrator.state import atomic_write

BACKENDS = ("claude", "local")
_KEY = "developer_backend"
_TIMEOUT = 2.0


@dataclass(frozen=True)
class BackendChoice:
    backend: str  # claude | local
    source: str  # project | global | default


def _global_path() -> Path:
    return home_dir() / "settings.json"


def get_developer_backend(runs_dir: Path, project: str) -> BackendChoice:
    mine = read_settings(project_settings_path(runs_dir, project)).get(_KEY)
    if mine in BACKENDS:
        return BackendChoice(mine, "project")
    everywhere = read_settings(_global_path()).get(_KEY)
    if everywhere in BACKENDS:
        return BackendChoice(everywhere, "global")
    return BackendChoice("claude", "default")


def get_default_backend() -> BackendChoice:
    """The choice for a project that has none of its own: the global one, else Claude."""
    everywhere = read_settings(_global_path()).get(_KEY)
    if everywhere in BACKENDS:
        return BackendChoice(everywhere, "global")
    return BackendChoice("claude", "default")


def _check(backend: str | None) -> None:
    if backend is not None and backend not in BACKENDS:
        raise ValueError(f"the backend must be one of {', '.join(BACKENDS)}")


def set_global_backend(backend: str) -> None:
    _check(backend)
    update_settings(_global_path(), **{_KEY: backend})


def set_project_backend(runs_dir: Path, project: str, backend: str | None) -> None:
    """Choose for one project, or None to follow the global choice."""
    _check(backend)
    update_settings(project_settings_path(runs_dir, project), **{_KEY: backend})


def developer_model(backend: str, config: Config) -> str:
    return config.local.model if backend == "local" else config.models.developer


def developer_env(backend: str, local: LocalConfig) -> dict[str, str]:
    """Environment overrides for the Developer's Claude Code process."""
    env = hidden_env()
    if backend != "local":
        return env
    return {
        **env,
        "ANTHROPIC_BASE_URL": local.endpoint,
        "ANTHROPIC_AUTH_TOKEN": "ollama",  # the local server does not check it
        "ANTHROPIC_API_KEY": "",  # never send the real key to a local server
        "ANTHROPIC_MODEL": local.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": local.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": local.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": local.model,
        "ANTHROPIC_SMALL_FAST_MODEL": local.model,
        "CLAUDE_CODE_SUBAGENT_MODEL": local.model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


# --- the model a task is running on -----------------------------------------------------------


def _running_path(runs_dir: Path, project: str) -> Path:
    return runs_dir / project / "developer_model.json"


def record_running_model(
    runs_dir: Path, project: str, task_id: str, backend: str, model: str
) -> None:
    """Note which model a task starts on, so the UI can say what is being used right now."""
    path = _running_path(runs_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"task_id": task_id, "backend": backend, "model": model}))


def read_running_model(runs_dir: Path, project: str) -> tuple[str, str, str] | None:
    """(task id, backend, model) of the latest task to start, or None."""
    try:
        raw = json.loads(_running_path(runs_dir, project).read_text())
        return str(raw["task_id"]), str(raw["backend"]), str(raw["model"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None


# --- is the local server ready ----------------------------------------------------------------


@dataclass(frozen=True)
class LocalStatus:
    ok: bool
    reachable: bool
    installed: bool
    context: int | None  # the loaded model's context length, when it is loaded
    detail: str
    fix: str = ""


Fetch = Callable[[str, float], Any]


def _http_json(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (a local URL)
        return json.loads(response.read())


def _normal(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def check_local(local: LocalConfig, *, fetch: Fetch = _http_json) -> LocalStatus:
    base = local.endpoint.rstrip("/")
    try:
        tags = fetch(f"{base}/api/tags", _TIMEOUT)
    except (OSError, ValueError, urllib.error.URLError):
        return LocalStatus(
            False,
            False,
            False,
            None,
            f"Ollama is not answering at {local.endpoint}.",
            "Start it with `OLLAMA_CONTEXT_LENGTH=65536 ollama serve` (or open the Ollama app).",
        )
    installed = {_normal(str(m.get("name", ""))) for m in tags.get("models", [])}
    if _normal(local.model) not in installed:
        return LocalStatus(
            False,
            True,
            False,
            None,
            f"The model {local.model} is not installed in Ollama.",
            f"Run `ollama pull {local.model}`, or set local.model in config.yaml to a tag "
            "from `ollama list`.",
        )
    context: int | None = None
    try:
        for running in fetch(f"{base}/api/ps", _TIMEOUT).get("models", []):
            if _normal(str(running.get("name", ""))) == _normal(local.model):
                value = running.get("context_length")
                context = int(value) if value is not None else None
    except (OSError, ValueError, urllib.error.URLError):
        context = None
    if context is not None and context < local.min_context:
        return LocalStatus(
            False,
            True,
            True,
            context,
            f"{local.model} is loaded with a context of {context}, below the "
            f"{local.min_context} Claude Code needs.",
            f"Restart Ollama with `OLLAMA_CONTEXT_LENGTH={local.min_context} ollama serve`.",
        )
    detail = f"{local.model} is installed" + (
        f" and loaded (context {context})." if context else "; it loads on first use."
    )
    return LocalStatus(True, True, True, context, detail)
