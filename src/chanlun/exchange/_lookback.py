"""src/chanlun/exchange/_lookback.py — 跨 exchange 的统一回看天数表 (US-005)。

历史: 2026-05-14 之前, ``exchange_alpaca / polygon / futu / cq / qmt`` 5 个文件
各自维护一份"frequency -> lookback days"的 if-elif 或字典。数值口径已经在
2026-05-14 commit 中对齐到同一组数, 但**结构仍是 5 份独立拷贝** —— 下次需要
调整, 仍要改 5 处, 任一处漏改就会出现"美股 1m 拿 30 天 / 港股 1m 拿 90 天"
的隐性不一致。

本模块提供单一来源:
- ``DEFAULT_LOOKBACK_DAYS``: 不可变 dict[str, int], frequency -> days
- ``get_lookback_days(frequency)``: 查询函数, 未知 frequency 抛 ValueError
- ``get_lookback_timedelta(frequency)``: 包装成 timedelta, 调用方常用

注意: 数值口径出处见 cq:670 / qmt:75 / alpaca:179 / polygon:124 / futu:154 中
注释"2026-05-14 与 ... 对齐统一 lookback"。修改本表 = 修改全部 5 个 exchange
的行为, 需在 commit message 里说明动机 (例如数据源新增更长历史, 或为避免
BSON payload 过大下调)。
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import Mapping, Optional


# 跨 exchange 统一回看天数 (与 commit 8d2ba0b 中 5 个 exchange 文件实际值对齐)
_LOOKBACK_DAYS_RAW: dict[str, int] = {
    # 1m 回看 30 天(2026-06-24 从 20 上调,用户要求整体扩大 1m 图表时间区间):
    #   历史:60 天(长桥美股 12 段+限流 → 切标的卡 20-31s)→ 20 天(2026-06-23 为美股提速降)
    #   → 30 天(本次,折中:多看~50%历史/多级递归层级更易凑齐,美股切标的回到 ~6 段≈10-15s,
    #   A股 QMT 本地源基本不受影响)。再大可继续上调,但美股切标的会更慢;真实拉到多少由 exchange 返回为准。
    "1m": 30,
    "2m": 60,
    "3m": 60,
    "5m": 90,
    "10m": 180,
    "15m": 180,
    # 30m / 日线图需要更长的结构上下文：2026-08-08 起均扩大为原来的 2 倍。
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


def get_lookback_timedelta(
    frequency: str, default: Optional[timedelta] = None
) -> timedelta:
    """返回 frequency 对应的默认回看 timedelta (天数包装)。

    Args:
        frequency: K 线周期 key, 见 ``DEFAULT_LOOKBACK_DAYS``。
        default: frequency 未知时的兜底值; ``None`` 时抛 ValueError (与 ``get_lookback_days`` 一致)。
            调用方可显式传 ``timedelta(days=30)`` 等兜底, 替代各处独立的 try/except。
    """
    try:
        return timedelta(days=get_lookback_days(frequency))
    except ValueError:
        if default is not None:
            return default
        raise
