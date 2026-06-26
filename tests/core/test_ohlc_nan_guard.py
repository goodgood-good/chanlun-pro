"""审计 D4-HIGH-2:process_kline_values(live/walk-forward 主快路径)OHLC NaN/Inf 兜底。

原仅 volume 有 pd.isna 兜底,OHLC 裸 float() → 坏 bar 把 NaN 灌进 klines → bi/xd `.val`
比较静默失效(nan>x 与 nan<x 皆 False)+ MACD inc≠batch。修复与 _convert 的 ffill 对齐:
坏值顶上一根 → 无前根则 bfill 本 bar 任一有限 OHLC → 全非有限丢弃该 bar。
"""
import math

import pandas as pd

from chanlun.core.kline_data_processor import KlineDataProcessor


def _feed(p, i, o, h, l, c, v=1.0):
    return p.process_kline_values(
        pd.Timestamp("2024-01-01 09:30:00") + pd.Timedelta(minutes=i), o, h, l, c, v
    )


def _all_finite(k):
    return (
        math.isfinite(k.o)
        and math.isfinite(k.h)
        and math.isfinite(k.l)
        and math.isfinite(k.c)
    )


def test_process_kline_values_nan_ohlc_does_not_enter_klines():
    p = KlineDataProcessor()
    for i in range(5):
        _feed(p, i, 100 + i, 101 + i, 99 + i, 100 + i)
    assert all(_all_finite(k) for k in p.klines)
    # 坏 bar:OHLC 全 NaN
    _feed(p, 5, float("nan"), float("nan"), float("nan"), float("nan"))
    assert all(_all_finite(k) for k in p.klines), "NaN OHLC 灌进了 klines(D4-HIGH-2 未修)"
    # Inf 同样兜底
    _feed(p, 6, float("inf"), float("inf"), float("-inf"), float("inf"))
    assert all(_all_finite(k) for k in p.klines), "Inf OHLC 灌进了 klines"
    # 后续正常 bar 不受污染
    _feed(p, 7, 106, 107, 105, 106)
    assert math.isfinite(p.klines[-1].c)


def test_process_kline_values_partial_nan_ffills_from_prev():
    p = KlineDataProcessor()
    for i in range(3):
        _feed(p, i, 100 + i, 101 + i, 99 + i, 100 + i)
    prev_c = p.klines[-1].c
    _feed(p, 3, 103, 104, 102, float("nan"))  # 仅 close=NaN → 顶上一根 close
    assert math.isfinite(p.klines[-1].c)
    assert p.klines[-1].c == prev_c


def test_process_kline_values_clean_data_unchanged():
    """干净数据零改变:走守卫旁路,OHLC 原值进 klines。"""
    p = KlineDataProcessor()
    _feed(p, 0, 100.0, 101.5, 98.5, 100.7)
    k = p.klines[-1]
    assert (k.o, k.h, k.l, k.c) == (100.0, 101.5, 98.5, 100.7)
