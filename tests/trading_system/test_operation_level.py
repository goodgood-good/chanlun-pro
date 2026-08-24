import pytest

from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
    is_one_minute_segment_level,
)


@pytest.mark.parametrize(
    ("frequency", "level", "expected"),
    (
        ("5m", 0, True),
        ("5", 0, True),
        ("5m", 1, False),
        ("5m", 2, False),
        ("1m", 0, False),
        ("30m", 0, False),
        ("5m", -1, False),
        ("5m", True, False),
    ),
)
def test_only_physical_five_minute_level_zero_is_trade_lane(
    frequency,
    level,
    expected,
) -> None:
    assert is_five_minute_trade_level(frequency, level) is expected


@pytest.mark.parametrize(
    ("frequency", "level", "expected"),
    (
        ("1m", 0, True),
        ("1", 0, True),
        ("1m", 1, False),
        ("1m", 2, False),
        ("5m", 0, False),
        ("1m", -1, False),
        ("1m", True, False),
    ),
)
def test_only_physical_one_minute_level_zero_is_segment_difference_level(
    frequency,
    level,
    expected,
) -> None:
    assert is_one_minute_segment_level(frequency, level) is expected
