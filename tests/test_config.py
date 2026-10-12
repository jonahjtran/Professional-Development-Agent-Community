from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from orchestrator.config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def valid_config() -> dict[str, Any]:
    return {
        "models": {"strong": "strong-model-id", "fast": "fast-model-id"},
        "budget": {
            "project_cap_usd": 20.0,
            "warn_at_fraction": 0.8,
            "stage_caps_usd": {
                "product_owner": 1.5,
                "architect": 2.5,
                "developer": 2.0,
                "qa": 0.5,
                "career_coach": 2.0,
            },
        },
        "limits": {
            "max_revisions": 3,
            "max_qa_loops": 2,
            "max_turns": {
                "product_owner": 20,
                "architect": 20,
                "developer": 40,
                "qa": 20,
                "career_coach": 20,
            },
        },
    }


def write_yaml(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_loads_a_valid_config(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, valid_config())

    config = load_config(path)

    assert isinstance(config, Config)
    assert config.budget.project_cap_usd == 20.0
    assert config.limits.max_revisions == 3


def test_shipped_config_sets_a_twenty_dollar_project_cap() -> None:
    config = load_config(REPO_ROOT / "config.yaml")

    assert config.budget.project_cap_usd == 20.0


def test_tier_resolves_to_a_concrete_model_name(tmp_path: Path) -> None:
    config = load_config(write_yaml(tmp_path, valid_config()))

    assert config.models.for_tier("strong") == "strong-model-id"
    assert config.models.for_tier("fast") == "fast-model-id"


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_rejects_a_missing_section(tmp_path: Path) -> None:
    data = valid_config()
    del data["budget"]

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))


def test_rejects_a_non_positive_project_cap(tmp_path: Path) -> None:
    data = valid_config()
    data["budget"]["project_cap_usd"] = 0

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))


def test_rejects_a_warning_fraction_above_one(tmp_path: Path) -> None:
    data = valid_config()
    data["budget"]["warn_at_fraction"] = 1.5

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))


def test_rejects_a_misspelled_key(tmp_path: Path) -> None:
    data = valid_config()
    data["budget"]["project_cap_usdd"] = 20.0

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))


def test_rejects_a_stage_cap_that_is_not_positive(tmp_path: Path) -> None:
    data = valid_config()
    data["budget"]["stage_caps_usd"]["developer"] = -1

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))


def test_rejects_zero_max_turns(tmp_path: Path) -> None:
    data = valid_config()
    data["limits"]["max_turns"]["qa"] = 0

    with pytest.raises(ValidationError):
        load_config(write_yaml(tmp_path, data))
