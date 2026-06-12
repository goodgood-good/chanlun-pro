"""tests/core/test_macd_htf.py — 高周期 MACD(compute_higher_macd)单元测试。

背驰力度按原文应在「高一级别」上度量：本项目以线段为最低级别走势类型，
故 1m K 线的背驰力度用 5m MACD、5m 用 30m，以此类推。``compute_higher_macd``
按时间戳把当前周期重采样到高一周期、算 MACD、再线性插值回每根 K 线。
"""

from __future__ import annotations

import datetime

import numpy as np
import pytest

from chanlun.core.cl_interface import Kline
from chanlun.core.macd import MACD
from chanlun.core.macd_htf import HigherMACDCalculator, compute_higher_macd


def _klines(n: int, step_min: int = 1, start_close: float = 100.0) -> list[Kline]:
    """构造 n 根等间隔 K 线，close 走一段正弦+漂移，时间戳从固定起点递增。"""
    base = datetime.datetime(2024, 1, 2, 9, 30, 0)
    out = []
    for i in range(n):
        c = start_close + 0.1 * i + 5.0 * np.sin(i / 7.0)
        out.append(
            Kline(
                index=i,
                date=base + datetime.timedelta(minutes=i * step_min),
                h=c + 0.5,
                l=c - 0.5,
                o=c,
                c=float(c),
                a=1000.0,
            )
        )
    return out


def test_no_higher_freq_returns_none():
    """最高周期(y)无更高对照 → None。"""
    assert compute_higher_macd(_klines(500), "y") is None


def test_empty_klines_returns_none():
    """空 K 线 → None。"""
    assert compute_higher_macd([], "1m") is None


def test_too_few_buckets_returns_none():
    """高周期桶数不足 slow+signal → None（回退原生）。

    35 根 1m → 仅 7 个 5m 桶，远不足 slow(26)+signal(9)=35。
    """
    assert compute_higher_macd(_klines(35), "1m") is None


def test_returns_per_bar_arrays_aligned_to_klines():
    """1m → 5m：返回的 dif/dea/hist 均与输入 K 线等长（per-bar）。"""
    klines = _klines(600)  # 600 根 1m → 120 个 5m 桶，足够
    res = compute_higher_macd(klines, "1m")
    assert res is not None
    assert set(res.keys()) == {"dif", "dea", "hist"}
    for key in ("dif", "dea", "hist"):
        assert len(res[key]) == len(klines), f"{key} 长度须与 K 线等长"
    # 插值结果无 NaN（core MACD 早期填 0 不填 NaN）
    assert not np.isnan(np.asarray(res["dif"], dtype=float)).any()


def test_bucket_end_values_match_direct_higher_macd():
    """桶末根上的高周期 MACD 严格等于「直接对重采样 close 算 MACD」。

    1m→5m：每 5 根 1m 归并为 1 根 5m（取桶内最后一根 close），桶末根是
    插值锚点 → 该位置的值应与独立算出的高周期 MACD 完全一致。
    """
    klines = _klines(600)  # 整 120 个 5m 桶，每桶恰 5 根
    res = compute_higher_macd(klines, "1m")
    assert res is not None

    # 独立重采样：每 5 根取最后一根 close
    bucket_closes = [klines[i * 5 + 4].c for i in range(len(klines) // 5)]
    macd = MACD(fast_period=12, slow_period=26, signal_period=9)
    macd.process_macd(
        [Kline(index=i, date=None, h=0.0, l=0.0, o=0.0, c=c, a=0.0)
         for i, c in enumerate(bucket_closes)]
    )

    # 桶 b 的桶末根 = 第 b*5+4 根 1m K 线
    for b in range(len(bucket_closes)):
        anchor = b * 5 + 4
        assert res["dif"][anchor] == macd.dif[b], f"桶{b} dif 不一致"
        assert res["dea"][anchor] == macd.dea[b], f"桶{b} dea 不一致"


def test_5m_to_30m_resamples_by_timestamp():
    """5m → 30m：按时间戳每 6 根 5m 归并为 1 根 30m。"""
    klines = _klines(600, step_min=5)  # 600 根 5m → 100 个 30m 桶
    res = compute_higher_macd(klines, "5m")
    assert res is not None
    assert len(res["hist"]) == len(klines)


def _assert_macd_result_equal(left, right):
    if right is None:
        assert left is None
        return
    assert left is not None
    for key in ("dif", "dea", "hist"):
        assert np.asarray(left[key], dtype=float) == pytest.approx(
            np.asarray(right[key], dtype=float)
        )


def test_incremental_higher_macd_matches_full_recompute_on_appends():
    """Stateful higher-MACD must match the full reference helper at every step."""
    calc = HigherMACDCalculator("1m")
    klines = _klines(260)

    for end in range(1, len(klines) + 1):
        incremental = calc.update(klines[:end])
        full = compute_higher_macd(klines[:end], "1m")
        _assert_macd_result_equal(incremental, full)


def test_incremental_higher_macd_matches_full_recompute_on_last_bar_update():
    calc = HigherMACDCalculator("1m")
    klines = _klines(260)
    base = list(klines[:220])
    _assert_macd_result_equal(calc.update(base), compute_higher_macd(base, "1m"))

    updated = list(base)
    last = updated[-1]
    updated[-1] = Kline(
        index=last.index,
        date=last.date,
        h=last.h + 3.0,
        l=last.l,
        o=last.o,
        c=last.c + 2.5,
        a=last.a,
    )

    incremental = calc.update(updated)
    full = compute_higher_macd(updated, "1m")
    _assert_macd_result_equal(incremental, full)
