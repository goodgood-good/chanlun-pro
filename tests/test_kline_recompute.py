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
