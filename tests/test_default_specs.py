from pathlib import Path

import pytest

from orchestrator.cli import AgentKind, make_runner
from orchestrator.default_specs import default_specs
from orchestrator.echo_agent import EchoAgent
from orchestrator.runner import SdkRunner
from orchestrator.state import Stage
from tests.factories import make_config

UNSAFE_TOOLS = {"Bash", "Write", "Edit", "NotebookEdit"}


def test_only_the_stages_that_are_safe_without_a_sandbox_have_specs() -> None:
    assert {spec.stage for spec in default_specs()} == {Stage.PRODUCT_OWNER, Stage.ARCHITECT}


def test_no_default_spec_can_run_commands_or_change_files() -> None:
    for spec in default_specs():
        assert not UNSAFE_TOOLS & set(spec.tools), spec.stage


def test_default_specs_use_the_strong_tier_and_a_versioned_prompt() -> None:
    for spec in default_specs():
        assert spec.tier == "strong"
        assert spec.prompt_version
        assert spec.system_prompt


def test_the_echo_option_makes_the_echo_agent(tmp_path: Path) -> None:
    assert isinstance(make_runner(AgentKind.ECHO, make_config(), tmp_path), EchoAgent)


def test_the_claude_option_makes_the_sdk_runner(tmp_path: Path) -> None:
    assert isinstance(make_runner(AgentKind.CLAUDE, make_config(), tmp_path), SdkRunner)


def test_an_unknown_agent_option_is_rejected() -> None:
    with pytest.raises(ValueError):
        AgentKind("gpt")
