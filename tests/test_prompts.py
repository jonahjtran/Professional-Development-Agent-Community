from pathlib import Path

import pytest

from orchestrator.prompts import DEFAULT_PROMPTS_DIR, Prompt, load_prompt
from schemas import PRD, ArchitectOutput, Architecture, Task


def write(directory: Path, name: str, text: str = "text") -> None:
    (directory / name).write_text(text)


def test_loading_returns_the_text_and_version(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md", "You are an agent.")

    prompt = load_prompt("agent", tmp_path)

    assert prompt == Prompt(name="agent", version="v1", text="You are an agent.")


def test_the_highest_version_wins(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md", "old")
    write(tmp_path, "agent.v3.md", "newest")
    write(tmp_path, "agent.v2.md", "middle")

    assert load_prompt("agent", tmp_path).text == "newest"


def test_versions_compare_as_numbers_not_text(tmp_path: Path) -> None:
    write(tmp_path, "agent.v9.md", "nine")
    write(tmp_path, "agent.v10.md", "ten")

    assert load_prompt("agent", tmp_path).version == "v10"


def test_a_specific_version_can_be_requested(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md", "old")
    write(tmp_path, "agent.v2.md", "new")

    assert load_prompt("agent", tmp_path, version="v1").text == "old"


def test_other_agents_and_unrelated_files_are_ignored(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md", "mine")
    write(tmp_path, "agent2.v5.md", "other agent")
    write(tmp_path, "agent.notes.md", "not a version")
    write(tmp_path, "agent.v2.md.bak", "backup")

    assert load_prompt("agent", tmp_path).version == "v1"


def test_a_missing_prompt_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="agent"):
        load_prompt("agent", tmp_path)


def test_a_missing_version_is_a_clear_error(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md")

    with pytest.raises(FileNotFoundError, match="v7"):
        load_prompt("agent", tmp_path, version="v7")


def test_an_empty_prompt_file_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "agent.v1.md", "  \n")

    with pytest.raises(ValueError, match="empty"):
        load_prompt("agent", tmp_path)


# --- the real prompt files -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["product_owner", "architect"])
def test_the_shipped_prompts_exist_and_treat_outside_text_as_data(name: str) -> None:
    prompt = load_prompt(name, DEFAULT_PROMPTS_DIR)

    assert prompt.version.startswith("v")
    assert "data, not instructions" in prompt.text.lower()


def test_the_product_owner_prompt_names_every_prd_field() -> None:
    text = load_prompt("product_owner", DEFAULT_PROMPTS_DIR).text

    missing = [field for field in PRD.model_fields if field not in text]
    assert missing == []


def test_the_architect_prompt_names_every_architecture_and_task_field() -> None:
    text = load_prompt("architect", DEFAULT_PROMPTS_DIR).text

    fields = [*ArchitectOutput.model_fields, *Architecture.model_fields, *Task.model_fields]
    assert [f for f in fields if f not in text] == []


def test_the_architect_prompt_says_what_counts_as_a_tech_choice() -> None:
    prompt = load_prompt("architect", DEFAULT_PROMPTS_DIR)

    assert prompt.version == "v2"
    assert "short" in prompt.text.lower()
    assert "technology" in prompt.text.lower()


def test_the_first_architect_prompt_is_kept_for_comparison() -> None:
    assert load_prompt("architect", DEFAULT_PROMPTS_DIR, version="v1").version == "v1"
