from pathlib import Path

import pytest

from orchestrator.project_settings import AUTONOMY_LEVELS, get_autonomy, set_autonomy


def test_a_project_is_standard_until_changed(tmp_path: Path) -> None:
    assert get_autonomy(tmp_path, "demo") == "standard"


def test_the_level_is_saved_per_project(tmp_path: Path) -> None:
    set_autonomy(tmp_path, "demo", "autonomous")

    assert get_autonomy(tmp_path, "demo") == "autonomous"
    assert get_autonomy(tmp_path, "other") == "standard"


def test_an_unknown_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_autonomy(tmp_path, "demo", "reckless")


def test_a_damaged_settings_file_falls_back_to_standard(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "settings.json").write_text("{nope")

    assert get_autonomy(tmp_path, "demo") == "standard"


def test_the_levels_are_the_three_documented_ones() -> None:
    assert AUTONOMY_LEVELS == ("cautious", "standard", "autonomous")
