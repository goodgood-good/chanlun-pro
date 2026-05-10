# -*- coding: utf-8 -*-
"""集成测试:模拟 TradingView 向左滚动场景,验证缠论 XD 不会跳变。

不启动 Flask,只直接调用核心服务层(prepend_klines_and_replace_cache),
通过 mock chart_data_cache 的初始 state 模拟"用户首屏已缓存 100 根",
然后调用一次 prepend 模拟"向左拉了 100 根更早的 K 线",
最后断言:重算后的 chart_data 与"一次性算 200 根 K 线"等价。
"""
import numpy as np
import pandas as pd
import pytest


def _make_klines(start: str, periods: int, base_price: float = 100.0):
    dates = pd.date_range(start=start, periods=periods, freq="1min")
    t = np.arange(periods)
    closes = base_price + 5 * np.sin(t / 6.0) + t * 0.05
    return pd.DataFrame({
        "date": dates,
        "open": closes - 0.05 * np.sin(t / 3.0),
        "high": closes + 0.6,
        "low": closes - 0.6,
        "close": closes,
        "volume": 1000 + (t % 7) * 50,
    })


@pytest.fixture
def cl_config():
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


def test_backward_scroll_xd_matches_full_compute(cl_config):
    """场景:
       1. 用户首屏看后 100 根 → 我们写入 chart_data_cache;
       2. 用户向左滚动,前端请求"前 100 根"窄范围;
       3. 服务端调用 prepend_klines_and_replace_cache;
       4. 整体替换后的 chart_data 与"一次性算 200 根"应当 1:1 等价。
    """
    from cl_app.services import chart_cache, kline_recompute

    full = _make_klines("2024-01-01 09:30", 200)
    later = full.iloc[100:].reset_index(drop=True)
    earlier = full.iloc[:100].reset_index(drop=True)

    # 1) 首屏缓存(后 100 根)
    cache_key = chart_cache._build_cache_key("a", "BCK.001", "1m", cl_config)
    initial_chart_data = kline_recompute.recompute_chart_data_from_klines(
        "a", "BCK.001", "1m", cl_config, later
    )
    chart_cache._set_chart_cache_entry(cache_key, initial_chart_data, is_full_snapshot=True)

    # 2) 向左滚动:把 earlier 加进来
    actual = kline_recompute.prepend_klines_and_replace_cache(
        "a", "BCK.001", "1m", cl_config, earlier, cache_key=cache_key
    )

    # 3) 期望:基于完整 200 根 K 线一次性算
    expected = kline_recompute.recompute_chart_data_from_klines(
        "a", "BCK.001", "1m", cl_config, full
    )

    # 时间序列一致
    assert actual["t"] == expected["t"]
    # 缓存项被整体替换
    cache_entry = chart_cache._get_chart_cache_entry(cache_key)
    assert cache_entry["min_time"] == expected["t"][0]
    assert cache_entry["max_time"] == expected["t"][-1]
    assert cache_entry["is_full_snapshot"] is True

    # XD/笔/分型按身份签名比较
    def _sig(shape):
        pts = shape.get("points")
        if isinstance(pts, list) and len(pts) >= 2:
            return (pts[0]["time"], round(pts[0]["price"], 6),
                    pts[-1]["time"], round(pts[-1]["price"], 6))
        if isinstance(pts, dict):
            return (pts["time"], round(pts["price"], 6))
        return None

    for key in ("xds", "bis", "fxs"):
        a = sorted(filter(None, (_sig(s) for s in actual.get(key, []))))
        e = sorted(filter(None, (_sig(s) for s in expected.get(key, []))))
        assert a == e, f"{key} 在向左滚动重算后与全量结果不一致"
