"""kline_recompute.prepend：数据没变时跳过全量重算(省 CPU)，数据变则照常重算。

收盘/非交易时段标的会被前端轮询/SSE 每隔几秒触发同口径重算。本组测试钉死
"末根 OHLC 与缓存完全相同 → 跳过 recompute_chart_data_from_klines"的优化，
并验证盘中更新(末根变)/追加新根时不会被误跳过。
"""
import pandas as pd
import pytest

from cl_app.services import chart_cache, kline_recompute


def _chart_data(ts, prices):
    """构造最小 chart_data(t/o/h/l/c/v 等长，OHLC 同价简化)。"""
    n = len(ts)
    return {
        "t": list(ts),
        "o": list(prices),
        "h": list(prices),
        "l": list(prices),
        "c": list(prices),
        "v": [10] * n,
    }


def _klines_df(ts, prices):
    """构造 ex.klines() 同构 DataFrame(date 带 UTC tz)。"""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(ts), unit="s", utc=True),
            "open": list(prices),
            "high": list(prices),
            "low": list(prices),
            "close": list(prices),
            "volume": [10] * len(ts),
        }
    )


@pytest.fixture
def spy_recompute(monkeypatch):
    """spy recompute_chart_data_from_klines：记录是否被调用，避免真跑 CL。"""
    calls = []

    def _fake(*args, **kwargs):
        calls.append(1)
        return {"t": [], "recomputed": True}

    monkeypatch.setattr(kline_recompute, "recompute_chart_data_from_klines", _fake)
    monkeypatch.setattr(chart_cache, "_set_chart_cache_entry", lambda *a, **k: None)
    return calls


def test_skip_recompute_when_data_unchanged(monkeypatch, spy_recompute):
    """末根 OHLC 与缓存完全相同(收盘/数据停)→ 跳过全量重算、直接复用缓存。"""
    cached = _chart_data([1000, 1060], [10, 11])
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry", lambda k: {"data": cached})
    new = _klines_df([1000, 1060], [10, 11])  # 与缓存末根一致

    result = kline_recompute.prepend_klines_and_replace_cache(
        "a", "X", "1m", {}, new, "a:X:1m",
    )
    assert spy_recompute == []  # recompute 未被调用(跳过)
    assert result is cached  # 直接返回缓存 data


def test_recompute_when_last_bar_changed(monkeypatch, spy_recompute):
    """末根 OHLC 变化(盘中刷新)→ 照常全量重算。"""
    cached = _chart_data([1000, 1060], [10, 11])
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry", lambda k: {"data": cached})
    new = _klines_df([1000, 1060], [10, 12])  # 末根 close 11→12

    result = kline_recompute.prepend_klines_and_replace_cache(
        "a", "X", "1m", {}, new, "a:X:1m",
    )
    assert spy_recompute == [1]  # recompute 被调用
    assert result == {"t": [], "recomputed": True}


def test_recompute_when_new_bar_appended(monkeypatch, spy_recompute):
    """追加新根(根数变)→ 照常全量重算。"""
    cached = _chart_data([1000, 1060], [10, 11])
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry", lambda k: {"data": cached})
    new = _klines_df([1000, 1060, 1120], [10, 11, 12])  # 多一根

    result = kline_recompute.prepend_klines_and_replace_cache(
        "a", "X", "1m", {}, new, "a:X:1m",
    )
    assert spy_recompute == [1]  # recompute 被调用
    assert result == {"t": [], "recomputed": True}
