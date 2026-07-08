"""Round12 数据源 HIGH-1: 期货 gm 夜盘 30m/60m 合成复制粘贴笔误(22:00/22:30 起点误写 21:00/21:30,
区间过宽)+ 循环 last-wins 覆盖 → 21:00/21:30 目标 bar 整段消失、成交量灌错 → 缠论分型/笔/买卖点错位。
所有生产调用(kcharts/tv_chart/backtest/exchange_db)走默认 gm, 任一夜盘品种 30m/60m 合成即中招。"""

import numpy as np
import pandas as pd

from chanlun.exchange.exchange import convert_futures_kline_frequency


def _night_1m(code="rb2401"):
    # 21:00:00 .. 22:59:00 共 120 根 1m, 每根 vol=1(夜盘时段)
    dates = pd.date_range("2024-01-02 21:00", periods=120, freq="1min", tz="Asia/Shanghai")
    n = len(dates)
    return pd.DataFrame(
        {
            "code": [code] * n,
            "date": dates,
            "open": np.full(n, 100.0),
            "high": np.full(n, 100.0),
            "low": np.full(n, 100.0),
            "close": np.full(n, 100.0),
            "volume": np.ones(n),
        }
    )


def _times_vols(df):
    times = [pd.Timestamp(t).strftime("%H:%M") for t in df["date"]]
    vols = {pd.Timestamp(r["date"]).strftime("%H:%M"): float(r["volume"]) for _, r in df.iterrows()}
    return times, vols


def test_gm_futures_30m_night_session_bins():
    times, vols = _times_vols(convert_futures_kline_frequency(_night_1m(), "30m", "gm"))
    # 修复前: 只出 22:00(vol=30)/22:30(vol=90), 21:00/21:30 整段丢失
    assert times == ["21:00", "21:30", "22:00", "22:30"], times
    assert all(vols[t] == 30 for t in times), vols


def test_gm_futures_60m_night_session_bins():
    times, vols = _times_vols(convert_futures_kline_frequency(_night_1m(), "60m", "gm"))
    # 修复前: 只出 22:00(vol=120), 21:00 丢失(整个夜盘塌成一根)
    assert times == ["21:00", "22:00"], times
    assert all(vols[t] == 60 for t in times), vols