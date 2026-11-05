import pytest

from orchestrator.mathutil import clamp


def test_a_value_above_the_range_becomes_high() -> None:
    assert clamp(5, 0, 3) == 3


def test_a_value_below_the_range_becomes_low() -> None:
    assert clamp(-1, 0, 3) == 0


def test_a_value_inside_the_range_is_unchanged() -> None:
    assert clamp(1.5, 0, 3) == 1.5


def test_the_bounds_themselves_are_inside_the_range() -> None:
    assert clamp(0, 0, 3) == 0
    assert clamp(3, 0, 3) == 3


def test_an_empty_range_of_one_point_returns_that_point() -> None:
    assert clamp(7, 2, 2) == 2


def test_low_greater_than_high_raises_value_error() -> None:
    with pytest.raises(ValueError):
        clamp(1, 3, 0)
