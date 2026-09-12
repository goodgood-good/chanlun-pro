"""Shared price-boundary predicates, independent of lifecycle and signal models."""


def return_outside_core(
    *, direction: str, low_tick: int, high_tick: int, zd_tick: int, zg_tick: int,
) -> bool:
    """A return must be strictly outside the closed core; contact is not a third point.

    This only checks price geometry. Callers must also prove direction, source
    grade, independent departure, first-return ownership and confirmation time.
    """
    if direction == "up":
        return low_tick > zg_tick
    if direction == "down":
        return high_tick < zd_tick
    raise ValueError("unknown departure direction")
