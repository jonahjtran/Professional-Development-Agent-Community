from typer.testing import CliRunner

from orchestrator.cli import app

runner = CliRunner()


def test_help_exits_successfully() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0


def test_help_describes_agent_studio() -> None:
    result = runner.invoke(app, ["--help"])

    assert "Agent Studio" in result.output


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "0.1.0" in result.output
