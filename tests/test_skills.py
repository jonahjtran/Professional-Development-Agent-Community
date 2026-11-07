from pathlib import Path

import pytest

from orchestrator.config import load_config
from orchestrator.skills import SkillError, load_skills

REPO = Path(__file__).parent.parent


def make_skill(root: Path, name: str, body: str, frontmatter: bool = True) -> None:
    folder = root / ".claude" / "skills" / name
    folder.mkdir(parents=True)
    head = f"---\nname: {name}\ndescription: A skill.\n---\n\n" if frontmatter else ""
    (folder / "SKILL.md").write_text(head + body)


def test_a_skill_is_loaded_without_its_front_matter(tmp_path: Path) -> None:
    make_skill(tmp_path, "tdd", "# Test-Driven Development\n\nWrite the failing test first.")

    text = load_skills(tmp_path, ["tdd"])

    assert "Write the failing test first." in text
    assert "description: A skill." not in text and "name: tdd" not in text


def test_each_skill_is_introduced_by_name_so_the_agent_knows_to_follow_it(tmp_path: Path) -> None:
    make_skill(tmp_path, "tdd", "Red, green, refactor.")
    make_skill(tmp_path, "review", "Check the diff.")

    text = load_skills(tmp_path, ["tdd", "review"])

    assert text.index("tdd") < text.index("Red, green, refactor.") < text.index("review")
    assert "Check the diff." in text
    assert "follow" in text.lower()


def test_no_skills_means_no_text(tmp_path: Path) -> None:
    assert load_skills(tmp_path, []) == ""


def test_a_skill_that_is_not_there_is_an_error_naming_it(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="tdd"):
        load_skills(tmp_path, ["tdd"])


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "a b"])
def test_a_skill_name_that_could_leave_the_skills_folder_is_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(SkillError):
        load_skills(tmp_path, [name])


def test_a_skill_without_front_matter_loads_as_it_is(tmp_path: Path) -> None:
    make_skill(tmp_path, "plain", "Just text.", frontmatter=False)

    assert "Just text." in load_skills(tmp_path, ["plain"])


def test_the_real_tdd_skill_in_this_repo_loads_and_asks_for_a_failing_test_first() -> None:
    text = load_skills(REPO, ["tdd"])

    assert "RED" in text and "GREEN" in text and "REFACTOR" in text
    assert "failing test" in text.lower()


def test_config_yaml_gives_the_developer_the_tdd_skill() -> None:
    config = load_config(REPO / "config.yaml")

    assert config.developer.skills == ["tdd"]
