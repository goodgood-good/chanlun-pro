"""锁定 _merge_chart_data 的合并语义(审查曾点名此函数零单测=回归脆弱点)。

重点覆盖 M-1 修复:HTF(跨周期)MACD 三列与 OHLCV 的"None 覆盖规则"必须不同——
- OHLCV:新值 None 时保留旧值(新窗口没覆盖到的旧 bar 应保留历史)。
- HTF MACD:new 覆盖到的 bar 一律以 new 为准(含 None),否则首个有效高周期桶之前的
  None 会被旧算法残留点污染(边界出现"旧值幽灵")。
"""
from cl_app.services.chart_compute import _merge_chart_data


def test_merge_htf_macd_new_none_overwrites_old():
    """M-1 核心:同一 bar 上 new 算出 None,必须覆盖 existing 的旧非 None HTF 值。"""
    existing = {
        "t": [100, 200, 300],
        "c": [1.0, 2.0, 3.0],
        "higher_macd_dif": [10.0, 20.0, 30.0],
        "higher_macd_dea": [11.0, 21.0, 31.0],
        "higher_macd_hist": [1.0, 2.0, 3.0],
    }
    new = {
        "t": [100, 200, 300],
        "c": [1.0, 2.0, 3.0],
        # 分桶边界漂移导致前两根这次算成 None
        "higher_macd_dif": [None, None, 35.0],
        "higher_macd_dea": [None, None, 36.0],
        "higher_macd_hist": [None, None, 4.0],
    }
    merged = _merge_chart_data(existing, new)
    # 修复前会保留旧值得到 [10.0, 20.0, 35.0];修复后 new 覆盖到的 bar 一律以 new 为准
    assert merged["higher_macd_dif"] == [None, None, 35.0]
    assert merged["higher_macd_dea"] == [None, None, 36.0]
    assert merged["higher_macd_hist"] == [None, None, 4.0]


def test_merge_ohlc_none_preserves_old():
    """回归护栏:OHLCV 仍走"None 不覆盖旧值"(M-1 不能误伤 OHLC 的历史保留)。"""
    existing = {"t": [100, 200, 300], "c": [1.0, 2.0, 3.0], "o": [1.0, 2.0, 3.0]}
    new = {"t": [100, 200, 300], "c": [1.0, 2.0, None], "o": [None, 2.5, 3.5]}
    merged = _merge_chart_data(existing, new)
    assert merged["c"] == [1.0, 2.0, 3.0]   # 末根新值 None → 保留旧 3.0
    assert merged["o"] == [1.0, 2.5, 3.5]   # 首根新值 None → 保留旧 1.0


def test_merge_htf_macd_uncovered_bar_keeps_existing():
    """new 未覆盖到的 bar(只在 existing 里)保留旧 HTF 值;覆盖到的以 new 为准。"""
    existing = {
        "t": [100, 200, 300],
        "higher_macd_dif": [10.0, 20.0, 30.0],
    }
    new = {
        "t": [200, 300, 400],
        "higher_macd_dif": [None, 35.0, 40.0],
    }
    merged = _merge_chart_data(existing, new)
    # bar100 只在 existing→保留10.0;bar200 在 new→None;bar300 在 new→35.0;bar400 只在 new→40.0
    assert merged["t"] == [100, 200, 300, 400]
    assert merged["higher_macd_dif"] == [10.0, None, 35.0, 40.0]


def test_merge_empty_sides():
    """空侧短路:existing 空返回 new,new 空返回 existing。"""
    payload = {"t": [1, 2], "c": [1.0, 2.0]}
    assert _merge_chart_data({}, payload) is payload
    assert _merge_chart_data(payload, {}) is payload
