from pathlib import Path

import pytest

from orchestrator.profile import PROFILE_ENV, ProfileError, load_profile, profile_dir

RESUME = """
sections:
  - name: Projects
    max_bullets: 4
    entries:
      - title: Mental Health Tracker
        subtitle: FastAPI, PostgreSQL
        bullets: ["Exposed 23 REST endpoints."]
"""
GOALS = """
tracks:
  strategy_consulting:
    target_roles: [technology consulting]
    emphasis: [client impact]
  tech_consulting:
    target_roles: [technology consultant]
timeline: "Summer 2027 recruiting"
"""


def write(folder: Path, resume: str = RESUME, goals: str = GOALS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "resume.yaml").write_text(resume)
    (folder / "goals.yaml").write_text(goals)
    return folder


def test_the_resume_and_goals_are_loaded_from_the_profile_folder(tmp_path: Path) -> None:
    resume, goals = load_profile(write(tmp_path / "career-profile"))

    section = resume.section("projects")
    assert section is not None and section.bullet_count == 1 and section.max_bullets == 4
    assert goals.tracks["tech_consulting"].target_roles == ["technology consultant"]
    assert goals.timeline == "Summer 2027 recruiting"


def test_the_profile_folder_comes_from_the_environment() -> None:
    assert profile_dir({PROFILE_ENV: "~/x"}) == Path("~/x").expanduser()


def test_an_unset_profile_folder_says_how_to_set_it() -> None:
    with pytest.raises(ProfileError, match=PROFILE_ENV):
        profile_dir({})


def test_a_missing_folder_or_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="does not exist"):
        load_profile(tmp_path / "nowhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProfileError, match="resume.yaml is missing"):
        load_profile(empty)


def test_an_invalid_resume_names_the_file(tmp_path: Path) -> None:
    folder = write(tmp_path / "p", resume="sections: []")

    with pytest.raises(ProfileError, match="resume.yaml is not valid"):
        load_profile(folder)


def test_a_misspelled_key_is_an_error_not_silently_ignored(tmp_path: Path) -> None:
    folder = write(tmp_path / "p", goals=GOALS.replace("timeline", "timelnie"))

    with pytest.raises(ProfileError, match="goals.yaml is not valid"):
        load_profile(folder)


def test_yaml_that_does_not_parse_is_reported(tmp_path: Path) -> None:
    folder = write(tmp_path / "p", goals="tracks: [unclosed")

    with pytest.raises(ProfileError, match="goals.yaml is not valid"):
        load_profile(folder)
