"""tests/test_apply_higher_macd.py — P5 third step: apply_higher_macd_to_chart_data。

测试覆盖:
- ratio 解析 (HIGHER_MACD_RATIO / 30m_TO_D / D_TO_W / w=4 / m=12 / 未知 freq)
- close 序列足/不足时 MACD 输出
- NaN 处理 (头部 slow+signal 根都是 NaN)
- 错误兜底 (close 含非数值时仍不崩)
"""

from __future__ import annotations

from cl_app.services.chart_compute import (
    HIGHER_MACD_RATIO,
    MARKET_30M_TO_D_RATIO,
    MARKET_D_TO_W_RATIO,
    _resolve_higher_macd_ratio,
    apply_higher_macd_to_chart_data,
    should_lazy_apply_higher_macd,
)


def test_ratio_resolution_table_hit():
    """已知 frequency 直接从 HIGHER_MACD_RATIO 表取。"""
    for freq, expected in HIGHER_MACD_RATIO.items():
        assert _resolve_higher_macd_ratio(freq, "a") == expected


def test_ratio_30m_uses_market_table():
    """30m 走 MARKET_30M_TO_D_RATIO; HIGHER_MACD_RATIO 里没有 30m 才走特殊表。"""
    if "30m" not in HIGHER_MACD_RATIO:
        for market, expected in MARKET_30M_TO_D_RATIO.items():
            assert _resolve_higher_macd_ratio("30m", market) == expected
        # 未知 market 默认 8
        assert _resolve_higher_macd_ratio("30m", "unknown_market") == 8


def test_ratio_d_uses_market_table():
    if "d" not in HIGHER_MACD_RATIO:
        for market, expected in MARKET_D_TO_W_RATIO.items():
            assert _resolve_higher_macd_ratio("d", market) == expected
        assert _resolve_higher_macd_ratio("d", "unknown") == 5


def test_ratio_w_and_m_hardcoded():
    if "w" not in HIGHER_MACD_RATIO:
        assert _resolve_higher_macd_ratio("w", "a") == 4
    if "m" not in HIGHER_MACD_RATIO:
        assert _resolve_higher_macd_ratio("m", "a") == 12


def test_ratio_unknown_frequency_returns_none():
    assert _resolve_higher_macd_ratio("999x", "a") is None


def test_apply_short_series_no_op():
    """close 数量不足时不写 higher_macd_* 字段。"""
    chart_data = {"c": [100.0] * 5}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    assert "higher_macd_dif" not in chart_data
    assert "higher_macd_dea" not in chart_data
    assert "higher_macd_hist" not in chart_data


def test_apply_long_series_writes_fields():
    """足够长的 close 序列会写 higher_macd_*。"""
    # ratio for 30m (8 倍 d) — fast=96, slow=208, signal=72 → 至少需要 280 根
    # 用 1m × 5 倍 = fast=60, slow=130, signal=45 → 至少 175 根
    chart_data = {"c": [100.0 + i * 0.1 for i in range(500)]}
    cfg = {"idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", cfg)
    assert "higher_macd_dif" in chart_data
    assert "higher_macd_dea" in chart_data
    assert "higher_macd_hist" in chart_data
    assert len(chart_data["higher_macd_dif"]) == 500


def test_apply_nan_replaced_with_none():
    """MACD 计算结果中头部 slow+signal 根都是 NaN, 应转成 None。"""
    chart_data = {"c": [100.0 + i * 0.1 for i in range(500)]}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    # 头部应有 None (talib 在 slow+signal-1 根之前都返回 NaN)
    assert chart_data["higher_macd_dif"][0] is None
    # 末段应是浮点数
    assert isinstance(chart_data["higher_macd_dif"][-1], float)


def test_apply_unknown_frequency_no_op():
    """未知 frequency → ratio=None → 不改 chart_data。"""
    chart_data = {"c": [100.0] * 500}
    before = dict(chart_data)
    apply_higher_macd_to_chart_data(chart_data, "999x", "a", {})
    assert chart_data == before


def test_apply_empty_closes_no_op():
    """close 为空时也不该崩。"""
    chart_data = {"c": []}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    assert "higher_macd_dif" not in chart_data


# =============================================================================
# M1: should_lazy_apply_higher_macd — cache hit 路径是否需要 lazy 补算 HTF
# =============================================================================


def test_lazy_no_ratio_frequency_skips():
    """无高周期倍率的 frequency(15m 等):HTF 本就该缺失,不应触发 lazy 补算。

    历史 bug:旧逻辑只看 len(htf) != bar_count,对 15m/60m 等周期恒为真,
    每次 polling 都冗余重写缓存 + 异步写盘。
    """
    chart_data = {"t": [1, 2, 3], "higher_macd_hist": []}
    assert should_lazy_apply_higher_macd(chart_data, "15m", "a") is False


def test_lazy_ratio_frequency_missing_htf_needs_apply():
    """有高周期倍率的 frequency(5m)且 HTF 缺失 → 需要补算。"""
    chart_data = {"t": [1, 2, 3], "higher_macd_hist": []}
    assert should_lazy_apply_higher_macd(chart_data, "5m", "a") is True


def test_lazy_ratio_frequency_complete_htf_skips():
    """HTF 已齐(长度与 bar 数一致)→ 不重复补算。"""
    chart_data = {"t": [1, 2, 3], "higher_macd_hist": [0.1, 0.2, 0.3]}
    assert should_lazy_apply_higher_macd(chart_data, "5m", "a") is False


def test_lazy_empty_chart_data_skips():
    """无 bar → 不补算。"""
    assert should_lazy_apply_higher_macd({"t": []}, "5m", "a") is False


# =============================================================================
# M1/M2 补强:apply_higher_macd_to_chart_data 返回"是否真正写入"
# =============================================================================


def test_apply_returns_true_when_fields_written():
    """有倍率 + bar 数充足 → 写入字段并返回 True。"""
    chart_data = {"c": [100.0 + i * 0.1 for i in range(500)]}
    assert apply_higher_macd_to_chart_data(chart_data, "1m", "a", {}) is True


def test_apply_returns_false_for_short_series():
    """bar 数不足 → 不写字段,返回 False(调用方据此跳过冗余回写)。"""
    chart_data = {"c": [100.0] * 5}
    assert apply_higher_macd_to_chart_data(chart_data, "1m", "a", {}) is False


def test_apply_returns_false_for_unknown_frequency():
    """无高周期倍率的 frequency → 不写字段,返回 False。"""
    chart_data = {"c": [100.0] * 500}
    assert apply_higher_macd_to_chart_data(chart_data, "999x", "a", {}) is False
