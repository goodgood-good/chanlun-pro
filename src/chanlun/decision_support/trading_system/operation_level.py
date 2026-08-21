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

    A recursive level belongs to the effective frequency declared by the
    strict level catalog.  Therefore 5m/L1 is 30m context, not a second 5m
    order lane; only 5m/L0 can create a trade setup or notification.
    """

    if source_frequency != "5m":
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

    The physical 1m chart contains recursive lanes, but 1m/L1 is an effective
    5m structure.  It can be useful as same-level context; it is not the
    subordinate 1-minute locator required by interval nesting and must not be
    counted or announced as a strict 1m segment-difference point.
    """

    if source_frequency != "1m":
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
