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


@pytest.fixture
def cl_config_min():
    """精简 cl_config,只填必需字段,其余由 cl.CL._init_default_config 兜底。"""
    return {
        "chart_show_fx": "1",
        "chart_show_bi": "1",
        "chart_show_xd": "1",
        "chart_show_bi_zs": "1",
        "chart_show_xd_zs": "1",
        "chart_show_bi_mmd": "1",
        "chart_show_xd_mmd": "1",
        "chart_show_bi_bc": "1",
        "chart_show_xd_bc": "1",
        "zs_bi_type": ["zs_type_bz"],
        "zs_xd_type": ["zs_type_bz"],
        "idx_macd_fast": 12,
        "idx_macd_slow": 26,
        "idx_macd_signal": 9,
    }


def test_recompute_chart_data_from_klines_returns_full_xd(cl_config_min):
    """全量重算应输出非空的 K 线时间序列 t,以及形如 list 的 xds 数组。"""
    from cl_app.services.kline_recompute import recompute_chart_data_from_klines

    klines = _make_klines("2024-01-01 09:30", 200)
    chart_data = recompute_chart_data_from_klines("a", "TEST.001", "1m", cl_config_min, klines)
    assert chart_data is not None
    assert isinstance(chart_data.get("t"), list) and len(chart_data["t"]) == 200
    assert isinstance(chart_data.get("xds"), list)


def test_recompute_split_then_merge_equals_full(cl_config_min):
    """**核心断言**:把 K 线分两段(模拟向左滚动)后合并重算 vs 一次性全量计算,
    生成的 xds / bis / fxs 数组必须 1:1 相等。这是本次修复要保证的不变量。"""
    from cl_app.services.kline_recompute import (
        merge_klines_df,
        recompute_chart_data_from_klines,
    )

    full = _make_klines("2024-01-01 09:30", 400)

    # 1) 一次性全量
    expected = recompute_chart_data_from_klines(
        "a", "TEST.002", "1m", cl_config_min, full
    )

    # 2) 模拟用户:先看后半段,再向左滚动加载前半段
    later_half = full.iloc[200:].reset_index(drop=True)
    earlier_half = full.iloc[:200].reset_index(drop=True)
    merged = merge_klines_df(later_half, earlier_half)
    actual = recompute_chart_data_from_klines(
        "a", "TEST.002", "1m", cl_config_min, merged
    )

    assert actual["t"] == expected["t"], "K 线时间序列不一致"

    def _shape_signature(shape):
        # 用 (起点, 终点) 作为身份;linestyle 允许差异(end pending 可能不同)
        pts = shape.get("points")
        if isinstance(pts, list) and len(pts) >= 2:
            return (pts[0]["time"], pts[0]["price"], pts[-1]["time"], pts[-1]["price"])
        if isinstance(pts, dict):
            return (pts["time"], pts["price"])
        return None

    for key in ("xds", "bis", "fxs"):
        a = sorted(filter(None, (_shape_signature(s) for s in actual.get(key, []))))
        e = sorted(filter(None, (_shape_signature(s) for s in expected.get(key, []))))
        assert a == e, f"{key} 在分段重算 vs 全量 间不一致"
