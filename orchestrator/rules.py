"""Always-allow rules: commands the human approved once and chose to remember.

A rule names a command by its words (`npx next lint`), either exactly or as a prefix that allows
any further arguments. It is applied by `permissions.py` as a last step, after every refusal and
every special case, so a rule can never allow something the policy refuses.
Rules live per project (`runs/<project>/rules.json`) or everywhere (`~/.config/agent-studio`).
"""

import json
import os
import re
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from orchestrator.state import atomic_write

_LOCK = threading.Lock()
_FORBIDDEN = re.compile(r"[;&|<>`$()\n\\]")


class Rule(BaseModel):
    id: str
    argv: list[str]
    any_args: bool = False
    created: datetime | None = None
    last_used: datetime | None = None
    uses: int = 0


def describe(rule: Rule) -> str:
    return " ".join(rule.argv) + (" …any arguments" if rule.any_args else "")


def matches(rules: Sequence[Rule], argv: Sequence[str]) -> Rule | None:
    """The first rule that covers this command, or None. The binary is compared by name only."""
    if not argv:
        return None
    words = [os.path.basename(argv[0]), *argv[1:]]
    for rule in rules:
        n = len(rule.argv)
        if not n or len(words) < n:
            continue
        if words[:n] == rule.argv and (rule.any_args or len(words) == n):
            return rule
    return None


class RuleStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[Rule]:
        try:
            raw = json.loads(self._path.read_text())
            return [Rule.model_validate(r) for r in raw["rules"]]
        except (FileNotFoundError, ValueError, KeyError, TypeError, ValidationError):
            return []

    def _save(self, rules: list[Rule]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = {"rules": [r.model_dump(mode="json") for r in rules]}
        atomic_write(self._path, json.dumps(body, indent=2))

    def add(self, argv: list[str], *, any_args: bool) -> Rule:
        if not argv or any(not w or _FORBIDDEN.search(w) for w in argv):
            raise ValueError("a rule is plain words, with no shell operators")
        with _LOCK:
            rules = self.load()
            for existing in rules:
                if existing.argv == argv and existing.any_args == any_args:
                    return existing
            rule = Rule(
                id=uuid.uuid4().hex[:8], argv=argv, any_args=any_args, created=datetime.now(UTC)
            )
            self._save([*rules, rule])
            return rule

    def revoke(self, rule_id: str) -> bool:
        with _LOCK:
            rules = self.load()
            kept = [r for r in rules if r.id != rule_id]
            if len(kept) == len(rules):
                return False
            self._save(kept)
            return True

    def touch(self, rule_id: str) -> None:
        with _LOCK:
            rules = self.load()
            for rule in rules:
                if rule.id == rule_id:
                    rule.uses += 1
                    rule.last_used = datetime.now(UTC)
                    self._save(rules)
                    return


def home_dir() -> Path:
    return Path(os.environ.get("AGENT_STUDIO_HOME") or Path.home() / ".config" / "agent-studio")


def global_store() -> RuleStore:
    return RuleStore(home_dir() / "rules.json")


def project_store(runs_dir: Path, project: str) -> RuleStore:
    return RuleStore(runs_dir / project / "rules.json")


def active_rules(runs_dir: Path, project: str) -> list[Rule]:
    return [*project_store(runs_dir, project).load(), *global_store().load()]


def touch_rule(runs_dir: Path, project: str, rule_id: str) -> None:
    """Record a use in whichever list holds the rule."""
    for store in (project_store(runs_dir, project), global_store()):
        if any(r.id == rule_id for r in store.load()):
            store.touch(rule_id)
            return
