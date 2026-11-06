from pathlib import Path

import pytest

from orchestrator.rules import (
    Rule,
    RuleStore,
    active_rules,
    global_store,
    matches,
    project_store,
)


def test_a_rule_can_match_one_exact_command_or_a_prefix_with_any_arguments() -> None:
    exact = Rule(id="r1", argv=["npx", "next", "lint"], any_args=False)
    prefix = Rule(id="r2", argv=["npx", "next"], any_args=True)

    assert matches([exact], ["npx", "next", "lint"]) is exact
    assert matches([exact], ["npx", "next", "lint", "--fix"]) is None
    assert matches([prefix], ["npx", "next", "build", "--x"]) is prefix
    assert matches([prefix], ["npx", "nextjs"]) is None  # whole words, not string prefixes
    assert matches([prefix], ["npx"]) is None


def test_a_rule_names_the_binary_not_its_path() -> None:
    rule = Rule(id="r1", argv=["npx", "next"], any_args=True)

    assert matches([rule], ["/usr/local/bin/npx", "next", "lint"]) is rule


def test_rules_are_saved_and_loaded(tmp_path: Path) -> None:
    store = RuleStore(tmp_path / "rules.json")

    added = store.add(["npm", "run", "x"], any_args=False)

    assert [r.id for r in RuleStore(tmp_path / "rules.json").load()] == [added.id]


def test_adding_the_same_rule_twice_keeps_one(tmp_path: Path) -> None:
    store = RuleStore(tmp_path / "rules.json")

    first = store.add(["npx", "next"], any_args=True)
    second = store.add(["npx", "next"], any_args=True)

    assert first.id == second.id and len(store.load()) == 1


def test_a_rule_can_be_revoked(tmp_path: Path) -> None:
    store = RuleStore(tmp_path / "rules.json")
    rule = store.add(["npx", "next"], any_args=True)

    assert store.revoke(rule.id) is True
    assert store.load() == [] and store.revoke(rule.id) is False


def test_using_a_rule_records_when_and_how_often(tmp_path: Path) -> None:
    store = RuleStore(tmp_path / "rules.json")
    rule = store.add(["npx", "next"], any_args=True)

    store.touch(rule.id)
    store.touch(rule.id)

    saved = store.load()[0]
    assert saved.uses == 2 and saved.last_used is not None


def test_a_damaged_rules_file_means_no_rules_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{not json")

    assert RuleStore(path).load() == []


def test_a_rule_cannot_be_empty_or_hold_a_shell_operator(tmp_path: Path) -> None:
    store = RuleStore(tmp_path / "rules.json")

    for bad in ([], ["npm", "&&", "ls"], ["x;y"], ["a", "$(b)"], ["a", "`b`"], ["a", "|", "b"]):
        with pytest.raises(ValueError):
            store.add(bad, any_args=False)


def test_active_rules_combine_the_project_and_everywhere_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_STUDIO_HOME", str(tmp_path / "home"))
    here = project_store(tmp_path, "demo").add(["npm", "run", "x"], any_args=False)
    everywhere = global_store().add(["npx", "next"], any_args=True)
    project_store(tmp_path, "other").add(["make"], any_args=False)

    ids = {r.id for r in active_rules(tmp_path, "demo")}

    assert ids == {here.id, everywhere.id}
