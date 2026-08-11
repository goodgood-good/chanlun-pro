"""所有行情源共用的默认 K 线回看窗口。"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import Mapping


# 跨行情源统一回看天数。
_LOOKBACK_DAYS_RAW: dict[str, int] = {
    # 1m 在递归结构上下文与远程行情延迟之间取 30 天窗口。
    "1m": 30,
    "2m": 60,
    "3m": 60,
    "5m": 90,
    "10m": 180,
    "15m": 180,
    # 30m / 日线图需要更长的结构上下文。
    # 30m 两年约 4,000 根 A 股 K 线，日线六年约 1,500 根，仍在可控量级。
    "30m": 365 * 2,
    "60m": 365 * 2,
    "120m": 365 * 2,
    "d": 365 * 6,
    "w": 365 * 10,
    "m": 365 * 30,
    "y": 365 * 30,
}

# 暴露 read-only 视图, 避免调用方意外 mutate
DEFAULT_LOOKBACK_DAYS: Mapping[str, int] = MappingProxyType(_LOOKBACK_DAYS_RAW)


def get_lookback_days(frequency: str) -> int:
    """返回 frequency 对应的默认回看天数。

    Args:
        frequency: 已知 key 见 ``DEFAULT_LOOKBACK_DAYS``。

    Raises:
        ValueError: frequency 未知 (避免静默返回 0 导致空数据)。
    """
    try:
        return DEFAULT_LOOKBACK_DAYS[frequency]
    except KeyError:
        known = sorted(DEFAULT_LOOKBACK_DAYS.keys())
        raise ValueError(
            f"Unknown frequency {frequency!r}; "
            f"known frequencies: {known}"
        ) from None


def get_lookback_timedelta(frequency: str) -> timedelta:
    """返回现行周期对应的默认回看时长；未知周期直接拒绝。"""

    return timedelta(days=get_lookback_days(frequency))
