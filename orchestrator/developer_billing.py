"""Who pays for the Developer: your Anthropic API credits, or your Claude subscription.

The Developer runs inside Claude Code, which signs in with an API key or with a Claude account. By
default Studio hands it your API key. With the subscription choice the key is blanked for the
Developer's process (only that process), so Claude Code falls back to the account you signed in
with (`claude auth login`, or a token from `claude setup-token`). Only the Developer can do this:
the planning agents call the Messages API directly, which needs a key.

Anthropic's terms allow a subscription for ordinary individual use of Claude Code and the Agent
SDK, and ask developers building products for other people to use API keys. This is a local tool
for one person; do not use it to put other people's requests through your plan.

The choice is per project (it overrides) or global, is read at the start of every task, and so can
be switched at any time. Under a subscription the dollar figures are estimates (nothing is billed
per call), and the caps still apply, as a guard on how much of your plan a project may use.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.project_settings import project_settings_path, read_settings, update_settings
from orchestrator.rules import home_dir

BILLINGS = ("api", "subscription")
_KEY = "developer_billing"
_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class BillingChoice:
    billing: str  # api | subscription
    source: str  # project | global | default


def _global_path() -> Path:
    return home_dir() / "settings.json"


def get_default_billing() -> BillingChoice:
    """The choice for a project that has none of its own: the global one, else API credits."""
    everywhere = read_settings(_global_path()).get(_KEY)
    if everywhere in BILLINGS:
        return BillingChoice(everywhere, "global")
    return BillingChoice("api", "default")


def get_developer_billing(runs_dir: Path, project: str) -> BillingChoice:
    mine = read_settings(project_settings_path(runs_dir, project)).get(_KEY)
    if mine in BILLINGS:
        return BillingChoice(mine, "project")
    return get_default_billing()


def _check(billing: str | None) -> None:
    if billing is not None and billing not in BILLINGS:
        raise ValueError(f"the billing must be one of {', '.join(BILLINGS)}")


def set_global_billing(billing: str) -> None:
    _check(billing)
    update_settings(_global_path(), **{_KEY: billing})


def set_project_billing(runs_dir: Path, project: str, billing: str | None) -> None:
    """Choose for one project, or None to follow the global choice."""
    _check(billing)
    update_settings(project_settings_path(runs_dir, project), **{_KEY: billing})


# --- is Claude Code signed in with an account ---------------------------------------------------


@dataclass(frozen=True)
class SubscriptionStatus:
    ok: bool
    detail: str
    fix: str = ""
    skipped: bool = False  # the check could not run; the task itself will report a real problem


def _run_status() -> str | None:
    """`claude auth status` with the API key blanked, the way the Developer will run."""
    try:
        done = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            env={**os.environ, "ANTHROPIC_API_KEY": ""},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


_SIGN_IN = (
    "Run `claude auth login` and sign in with your Claude account, or put the token from "
    "`claude setup-token` in CLAUDE_CODE_OAUTH_TOKEN."
)


def check_subscription(*, run: Callable[[], str | None] = _run_status) -> SubscriptionStatus:
    """Whether a Developer task without the API key would sign in with your Claude account.

    Only whether you are signed in and how are read; the account's details are never kept.
    """
    skipped = SubscriptionStatus(True, "the sign-in could not be checked", skipped=True)
    output = run()
    if not output:
        return skipped
    try:
        status = json.loads(output)
    except ValueError:
        return skipped
    if not isinstance(status, dict):
        return skipped
    if not status.get("loggedIn"):
        return SubscriptionStatus(
            False, "Claude Code is not signed in to a Claude account.", _SIGN_IN
        )
    if status.get("authMethod") == "api_key":
        return SubscriptionStatus(
            False,
            "Claude Code would still use an API key, not your Claude subscription.",
            _SIGN_IN,
        )
    return SubscriptionStatus(True, "signed in with your Claude account")
