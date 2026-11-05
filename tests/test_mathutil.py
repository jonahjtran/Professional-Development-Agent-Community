import pytest

from orchestrator.mathutil import clamp


def test_clamp_limits_a_value_above_the_range_to_high() -> None:
    assert clamp(5, 0, 3) == 3


def test_clamp_limits_a_value_below_the_range_to_low() -> None:
    assert clamp(-1, 0, 3) == 0


def test_clamp_returns_a_value_inside_the_range_unchanged() -> None:
    assert clamp(1.5, 0, 3) == 1.5


def test_clamp_accepts_the_range_bounds() -> None:
    assert clamp(0, 0, 3) == 0
    assert clamp(3, 0, 3) == 3


def test_clamp_allows_an_empty_width_range() -> None:
    assert clamp(7, 2, 2) == 2


def test_clamp_raises_when_low_is_above_high() -> None:
    with pytest.raises(ValueError, match="low"):
        clamp(1, 3, 0)
