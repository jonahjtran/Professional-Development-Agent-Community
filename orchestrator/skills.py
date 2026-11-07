"""Skills from this project's `.claude/skills` folder, given to the Developer as instructions.

The Developer works in a project's own worktree with no settings files loaded, so it cannot see
this repository's `.claude` folder. The skills named in `config.yaml` (`developer.skills`) are read
here and added to its system prompt, so it follows them on every task.
"""

import re
from pathlib import Path

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


class SkillError(Exception):
    """A configured skill cannot be used; the message says which and why."""


def _read(root: Path, name: str) -> str:
    if not _NAME.match(name):
        raise SkillError(f"{name!r} is not a valid skill name")
    path = root / ".claude" / "skills" / name / "SKILL.md"
    try:
        text = path.read_text()
    except OSError as error:
        raise SkillError(
            f"the skill {name!r} is set in config.yaml but {path} cannot be read"
        ) from error
    return _FRONT_MATTER.sub("", text, count=1).strip()


def load_skills(root: Path, names: list[str]) -> str:
    """The skills as one block of instructions, or "" when there are none."""
    if not names:
        return ""
    parts = [
        "# Skills you must follow",
        "The person who runs this project requires you to follow the skills below on every "
        "task. They are written for any language: discover this project's own tools and "
        "commands, and use those.",
    ]
    for name in names:
        parts.append(f"## Skill: {name}\n\n{_read(root, name)}")
    return "\n\n".join(parts)
