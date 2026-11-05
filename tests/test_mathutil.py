import pytest

from orchestrator.mathutil import clamp


def test_clamp_above_range_returns_high() -> None:
    assert clamp(5, 0, 3) == 3


def test_clamp_below_range_returns_low() -> None:
    assert clamp(-1, 0, 3) == 0


def test_clamp_inside_range_returns_value() -> None:
    assert clamp(1.5, 0, 3) == 1.5


def test_clamp_at_bounds_returns_bound() -> None:
    assert clamp(0, 0, 3) == 0
    assert clamp(3, 0, 3) == 3


def test_clamp_with_equal_bounds_returns_that_bound() -> None:
    assert clamp(7, 2, 2) == 2


def test_clamp_raises_when_low_greater_than_high() -> None:
    with pytest.raises(ValueError):
        clamp(1, 3, 0)
