"""Loading the person's own career data: a structured resume and their goals.

It lives in a private repository, never in this one, because it holds contact details. The
location comes from AGENT_STUDIO_PROFILE.
"""

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from schemas import Goals, ResumeData
from schemas._base import Schema

PROFILE_ENV = "AGENT_STUDIO_PROFILE"


class ProfileError(Exception):
    """The profile could not be loaded; the message says what to fix."""


def profile_dir(env: Mapping[str, str] = os.environ) -> Path:
    value = env.get(PROFILE_ENV, "").strip()
    if not value:
        raise ProfileError(
            f"{PROFILE_ENV} is not set. Point it at your private career-profile folder "
            "(it holds resume.yaml and goals.yaml)."
        )
    return Path(value).expanduser()


def load_profile(folder: Path) -> tuple[ResumeData, Goals]:
    if not folder.is_dir():
        raise ProfileError(f"{folder} does not exist")
    return _load(folder / "resume.yaml", ResumeData), _load(folder / "goals.yaml", Goals)


def _load[T: Schema](path: Path, model: type[T]) -> T:
    if not path.exists():
        raise ProfileError(f"{path} is missing")
    try:
        return model.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValidationError) as error:
        raise ProfileError(f"{path} is not valid: {error}") from error
