"""Small numeric helpers."""

__all__ = ["clamp"]


def clamp(value: float, low: float, high: float) -> float:
    """Return value limited to the closed range [low, high].

    Raises ValueError if low is greater than high.
    """
    if low > high:
        raise ValueError(f"low ({low}) must not be greater than high ({high})")
    return max(low, min(value, high))
