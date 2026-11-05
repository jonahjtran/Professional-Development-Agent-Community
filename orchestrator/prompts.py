"""Versioned agent prompts, kept as files in `agents/prompts/` named `<agent>.v<N>.md`.

Prompts are code that changes behavior, so they are versioned files and the version used goes into
every trace event. They are judged by evals (Phase 5), not unit tests.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "agents" / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str  # "v1", "v2", ...
    text: str


def load_prompt(
    name: str, prompts_dir: Path = DEFAULT_PROMPTS_DIR, *, version: str | None = None
) -> Prompt:
    """Load the highest version of a prompt, or a specific one."""
    pattern = re.compile(rf"^{re.escape(name)}\.v(\d+)\.md$")
    found = {
        int(match.group(1)): path
        for path in prompts_dir.glob(f"{name}.v*.md")
        if (match := pattern.match(path.name))
    }
    if not found:
        raise FileNotFoundError(f"no prompt named {name} in {prompts_dir}")
    if version is None:
        number = max(found)
    else:
        number = int(version.removeprefix("v"))
        if number not in found:
            raise FileNotFoundError(f"prompt {name} has no version {version} in {prompts_dir}")
    text = found[number].read_text()
    if not text.strip():
        raise ValueError(f"prompt {name} v{number} is empty")
    return Prompt(name=name, version=f"v{number}", text=text)
