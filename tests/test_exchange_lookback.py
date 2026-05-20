"""tests/test_exchange_lookback.py — US-005 验证 _lookback.py 行为。

不依赖 alpaca/polygon/futu/cq/qmt 任何 SDK, 只测纯函数。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from chanlun.exchange._lookback import (
    DEFAULT_LOOKBACK_DAYS,
    get_lookback_days,
    get_lookback_timedelta,
)


def test_default_lookback_days_covers_minimum_frequencies():
    """AC: 字典至少覆盖 11 个周期; 必含 5 个 exchange 实际用到的全集。"""
    must_have = {"1m", "5m", "15m", "30m", "60m", "120m", "d", "w", "m", "y"}
    assert must_have.issubset(DEFAULT_LOOKBACK_DAYS.keys())
    assert len(DEFAULT_LOOKBACK_DAYS) >= 11


def test_lookback_values_match_commit_8d2ba0b_baseline():
    """锚定: 与现行 5 个 exchange 一致 (commit 8d2ba0b 对齐值)。

    任何对本表的修改都要 in this test 里跟着改, 强制提醒"修一个 = 修所有 5 个 exchange"。
    """
    assert DEFAULT_LOOKBACK_DAYS["1m"] == 365
    assert DEFAULT_LOOKBACK_DAYS["5m"] == 90
    assert DEFAULT_LOOKBACK_DAYS["15m"] == 180
    assert DEFAULT_LOOKBACK_DAYS["30m"] == 365
    assert DEFAULT_LOOKBACK_DAYS["60m"] == 365 * 2
    assert DEFAULT_LOOKBACK_DAYS["120m"] == 365 * 2
    assert DEFAULT_LOOKBACK_DAYS["d"] == 365 * 3
    assert DEFAULT_LOOKBACK_DAYS["w"] == 365 * 10
    assert DEFAULT_LOOKBACK_DAYS["m"] == 365 * 30
    assert DEFAULT_LOOKBACK_DAYS["y"] == 365 * 30


def test_get_lookback_days_returns_int():
    assert get_lookback_days("1m") == 365
    assert isinstance(get_lookback_days("d"), int)


def test_get_lookback_days_unknown_raises_value_error():
    """AC: 未知 frequency 抛 ValueError (而非静默返回 0)。"""
    with pytest.raises(ValueError, match="Unknown frequency"):
        get_lookback_days("999x")
    with pytest.raises(ValueError, match="known frequencies"):
        get_lookback_days("")


def test_get_lookback_timedelta_wraps_int_days():
    assert get_lookback_timedelta("1m") == timedelta(days=365)
    assert get_lookback_timedelta("d") == timedelta(days=365 * 3)


def test_default_lookback_days_is_immutable():
    """MappingProxyType 防止调用方意外 mutate 共享表。"""
    with pytest.raises(TypeError):
        DEFAULT_LOOKBACK_DAYS["1m"] = 999  # type: ignore[index]
