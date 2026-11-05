"""Checks that tell you what will break a run before it does.

Each check says what it found and, when something is wrong, what to do. Secrets are only ever
tested for presence: no value is read into a message. The token's expiry comes from one call to
GitHub; if that cannot be made (offline) the check says so instead of failing the page.
"""

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from orchestrator.career_publish import repo_owner
from orchestrator.career_stage import WEBSITE_ENV
from orchestrator.newproject import run_with_own_login
from orchestrator.profile import PROFILE_ENV, ProfileError, load_profile
from orchestrator.publish import strip_credentials

OK, WARN, FAIL = "ok", "warn", "fail"
Which = Callable[[str], str | None]
OwnLogin = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
FetchToken = Callable[[str], tuple[int, str | None]]  # token -> (HTTP status, expiry header)


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # ok, warn, fail
    detail: str
    fix: str = ""


def fetch_token_info(token: str) -> tuple[int, str | None]:
    """Ask GitHub about the token. Returns the HTTP status and the expiry header, if any."""
    request = urllib.request.Request(
        "https://api.github.com/rate_limit",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "agent-studio-health"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, response.headers.get("github-authentication-token-expiration")
    except urllib.error.HTTPError as error:
        return error.code, None


def _token_expiry(expires: str, now: datetime) -> Check:
    try:
        when = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=UTC)
    except ValueError:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return Check("GitHub token expiry", WARN, f"GitHub reported expiry '{expires}'")
    days = (when - now).days
    if days < 0:
        return Check("GitHub token expiry", FAIL, "The token has expired.", "Create a new token.")
    if days < 7:
        return Check("GitHub token expiry", WARN, f"Expires in {days} day(s).", "Renew it soon.")
    return Check("GitHub token expiry", OK, f"Expires in {days} days.")


def _github_token(env: Mapping[str, str], fetch: FetchToken | None, now: datetime) -> list[Check]:
    token = env.get("GITHUB_TOKEN", "").strip()
    if not token:
        return [Check("GitHub token", FAIL, "GITHUB_TOKEN is not set.", "Add it to .env.")]
    checks = [Check("GitHub token", OK, "GITHUB_TOKEN is set.")]
    if fetch is None:
        return checks
    try:
        status, expires = fetch(token)
    except (OSError, ValueError):
        return [*checks, Check("GitHub token expiry", WARN, "Could not reach GitHub to check it.")]
    if status in (401, 403):
        return [
            Check("GitHub token", FAIL, "GitHub rejected the token.", "Create a new token."),
        ]
    if expires:
        checks.append(_token_expiry(expires, now))
    else:
        checks.append(Check("GitHub token expiry", OK, "The token does not expire."))
    return checks


def _origin(path: Path, run: OwnLogin) -> str | None:
    done = run(["git", "remote", "get-url", "origin"], path)
    return strip_credentials(done.stdout) if done.returncode == 0 and done.stdout.strip() else None


def run_checks(
    env: Mapping[str, str] = os.environ,
    *,
    repo_root: Path,
    which: Which = shutil.which,
    run: OwnLogin = run_with_own_login,
    fetch: FetchToken | None = fetch_token_info,
    now: datetime | None = None,
    network: bool = True,
) -> list[Check]:
    """Everything that can stop a full run, in the order you would fix it."""
    checks: list[Check] = []
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    checks.append(
        Check("Anthropic API key", OK, "ANTHROPIC_API_KEY is set.")
        if key
        else Check("Anthropic API key", FAIL, "ANTHROPIC_API_KEY is not set.", "Add it to .env.")
    )
    checks += _github_token(env, fetch if network else None, now or datetime.now(UTC))

    if which("gh") is None:
        checks.append(Check("GitHub login (gh)", FAIL, "gh is not installed.", "brew install gh"))
    elif network:
        done = run(["gh", "api", "user", "--jq", ".login"], repo_root)
        login = done.stdout.strip()
        checks.append(
            Check("GitHub login (gh)", OK, f"Logged in as {login}.")
            if done.returncode == 0 and login
            else Check("GitHub login (gh)", FAIL, "gh is not logged in.", "Run: gh auth login")
        )
    for tool, why, level in (
        ("uv", "runs the QA checks", FAIL),
        ("gitleaks", "the QA secret scan; without it every QA report fails", FAIL),
        ("node", "the website build check", WARN),
        ("npm", "the website build check", WARN),
    ):
        found = which(tool) is not None
        checks.append(
            Check(tool, OK, "Installed.")
            if found
            else Check(tool, level, f"Not installed ({why}).", f"Install {tool}.")
        )
    checks += _profile_checks(env, run)
    return checks


def _profile_checks(env: Mapping[str, str], run: OwnLogin) -> list[Check]:
    checks: list[Check] = []
    profile = env.get(PROFILE_ENV, "").strip()
    owners: dict[str, str | None] = {}
    if not profile:
        checks.append(
            Check("Career profile", FAIL, f"{PROFILE_ENV} is not set.", "Add it to .env.")
        )
    else:
        folder = Path(profile).expanduser()
        try:
            load_profile(folder)
        except ProfileError as error:
            checks.append(
                Check("Career profile", FAIL, str(error), "Fix resume.yaml or goals.yaml.")
            )
        else:
            origin = _origin(folder, run)
            owners["profile"] = repo_owner(origin) if origin else None
            checks.append(
                Check("Career profile", OK, f"Loaded from {folder}.")
                if origin
                else Check(
                    "Career profile", FAIL, "It is not a git clone with an origin.", "Clone it."
                )
            )
    site = env.get(WEBSITE_ENV, "").strip()
    if not site:
        checks.append(
            Check(
                "Website folder",
                WARN,
                f"{WEBSITE_ENV} is not set, so no website entry can be made.",
            )
        )
    else:
        folder = Path(site).expanduser()
        origin = _origin(folder, run) if folder.is_dir() else None
        owners["website"] = repo_owner(origin) if origin else None
        has_file = (folder / "src" / "data" / "projects.js").exists()
        if origin and has_file:
            checks.append(Check("Website folder", OK, f"{folder} has src/data/projects.js."))
        else:
            checks.append(
                Check(
                    "Website folder",
                    FAIL,
                    f"{folder} is not a clone of your site.",
                    "Check the path.",
                )
            )
    known = {o for o in owners.values() if o}
    if len(known) > 1:
        checks.append(
            Check("Same GitHub account", FAIL, "The profile and website are on different accounts.")
        )
    return checks


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == FAIL]
