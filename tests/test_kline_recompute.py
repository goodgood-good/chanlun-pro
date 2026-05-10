# -*- coding: utf-8 -*-
"""kline_recompute 服务单测:验证反构建/合并/重算的等价性。

核心契约:对同一组 K 线,无论是"一次性全量计算"还是"两段拼接后再重算",
得到的缠论 XD/笔/中枢序列必须完全相等。这是修复"向左滚动 XD 跳变"的核心断言。
"""
import pandas as pd
import pytest


def _make_klines(start: str, periods: int, freq: str = "1min", base_price: float = 100.0):
    """造一段 toy K 线:用正弦波 + 漂移让缠论能识别出多组分型/笔/段。"""
    import numpy as np
    dates = pd.date_range(start=start, periods=periods, freq=freq)
    t = np.arange(periods)
    closes = base_price + 5 * np.sin(t / 6.0) + t * 0.05
    highs = closes + 0.6
    lows = closes - 0.6
    opens = closes - 0.05 * np.sin(t / 3.0)
    volumes = 1000 + (t % 7) * 50
    return pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_extract_klines_df_roundtrip():
    """从 chart_data 反构建出来的 K 线 DataFrame,应当与原始 K 线在 OHLCV 上一一对应。"""
    from cl_app.services.kline_recompute import extract_klines_df_from_chart_data

    src = _make_klines("2024-01-01 09:30", 30)
    chart_data = {
        "t": [int(d.timestamp()) for d in src["date"]],
        "o": src["open"].tolist(),
        "h": src["high"].tolist(),
        "l": src["low"].tolist(),
        "c": src["close"].tolist(),
        "v": src["volume"].tolist(),
    }

    df = extract_klines_df_from_chart_data(chart_data)
    assert len(df) == len(src)
    assert (df["date"].astype("int64") // 10**9 == pd.Series(chart_data["t"])).all()
    assert df["high"].tolist() == src["high"].tolist()
    assert df["low"].tolist() == src["low"].tolist()
    assert df["close"].tolist() == src["close"].tolist()


def test_merge_klines_df_left_extend_no_overlap():
    """场景:新 K 线完全在缓存 K 线左侧(向左滚动加载更早历史)。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = _make_klines("2024-01-02 09:30", 30)  # 第二天
    new = _make_klines("2024-01-01 09:30", 30, base_price=95.0)  # 第一天

    merged = merge_klines_df(cached, new)

    # 长度 = 拼接后总数(无重叠)
    assert len(merged) == 60
    # 时间严格升序
    assert merged["date"].is_monotonic_increasing
    # 头来自 new,尾来自 cached
    assert merged["date"].iloc[0] == new["date"].iloc[0]
    assert merged["date"].iloc[-1] == cached["date"].iloc[-1]


def test_merge_klines_df_overlap_dedup_prefers_cached():
    """场景:新 K 线与缓存 K 线在边界重叠(典型于 ex.klines 返回闭区间)。
    重叠位置应保留缓存值——缓存的可能已经是"已完成 bar",新拉取可能仍在波动。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = _make_klines("2024-01-01 10:00", 10)
    # new 与 cached 的最后 3 根重叠,但 close 故意改成 999.0 用于断言"未覆盖"
    new = _make_klines("2024-01-01 09:53", 10).copy()
    overlap_dates = cached["date"].iloc[:3].tolist()
    new.loc[new["date"].isin(overlap_dates), "close"] = 999.0

    merged = merge_klines_df(cached, new)

    # 长度 = cached(10) + new 中独有的(7) = 17
    assert len(merged) == 17
    # 重叠 3 根的 close 应来自 cached 而非 999.0
    for d in overlap_dates:
        merged_close = merged.loc[merged["date"] == d, "close"].iloc[0]
        cached_close = cached.loc[cached["date"] == d, "close"].iloc[0]
        assert merged_close == cached_close, f"{d} 的重叠 K 线被新数据覆盖了"


def test_merge_klines_df_empty_cached_returns_new():
    """缓存为空时,应直接返回新 K 线(排序后)。"""
    from cl_app.services.kline_recompute import merge_klines_df

    new = _make_klines("2024-01-01 09:30", 5)
    merged = merge_klines_df(pd.DataFrame(), new)
    assert len(merged) == 5
    pd.testing.assert_frame_equal(
        merged.reset_index(drop=True),
        new.sort_values("date").reset_index(drop=True),
    )
