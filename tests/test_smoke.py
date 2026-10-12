import orchestrator


def test_package_exposes_version() -> None:
    assert orchestrator.__version__ == "0.1.0"
