"""Frozen mapping between physical recursive evidence and the trade level."""

from __future__ import annotations

from chanlun.core.strict_structure.level_catalog import effective_frequency

FIVE_MINUTE_TRADE_RECURSIVE_LEVELS = (0,)
ONE_MINUTE_SEGMENT_RECURSIVE_LEVELS = (0,)


def is_five_minute_trade_level(
    source_frequency: str,
    recursive_level: int,
) -> bool:
    """Return whether evidence is the physical 5m level used for orders.

    Recursive structure does not change the source bar period. Only 5m/L0 can
    create a trade setup or notification; 5m/L1+ is recursive context derived
    from the same physical chart.
    """

    if recursive_level != FIVE_MINUTE_TRADE_RECURSIVE_LEVELS[0]:
        return False
    try:
        return effective_frequency(source_frequency, recursive_level) == "5m"
    except (TypeError, ValueError):
        return False


def is_one_minute_segment_level(
    source_frequency: str,
    recursive_level: int,
) -> bool:
    """Return whether evidence is genuinely below the physical 5m trade level.

    The physical 1m chart contains recursive context lanes, but only its base
    segment-difference stream is the subordinate witness required by interval
    nesting. It cannot independently create a trade-level setup.
    """

    if recursive_level != ONE_MINUTE_SEGMENT_RECURSIVE_LEVELS[0]:
        return False
    try:
        return effective_frequency(source_frequency, recursive_level) == "1m"
    except (TypeError, ValueError):
        return False


__all__ = (
    "FIVE_MINUTE_TRADE_RECURSIVE_LEVELS",
    "ONE_MINUTE_SEGMENT_RECURSIVE_LEVELS",
    "is_five_minute_trade_level",
    "is_one_minute_segment_level",
)
