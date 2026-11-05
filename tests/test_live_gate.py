from tests.conftest import live_skip_reason


def test_live_tests_are_skipped_unless_asked_for() -> None:
    assert live_skip_reason(live_flag=False, api_key="sk-x") is not None


def test_live_tests_need_an_api_key() -> None:
    assert live_skip_reason(live_flag=True, api_key=None) is not None
    assert live_skip_reason(live_flag=True, api_key="") is not None


def test_live_tests_run_when_asked_for_and_a_key_is_set() -> None:
    assert live_skip_reason(live_flag=True, api_key="sk-x") is None
