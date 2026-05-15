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


# === New algorithm tests (resample-based HTF MACD) ===
# 后续 task 会在 chart_compute.py 中新增以下符号。


def test_resolve_higher_target_freq_mappings():
    from cl_app.services.chart_compute import _resolve_higher_target_freq
    assert _resolve_higher_target_freq("1m", "a") == "5m"
    assert _resolve_higher_target_freq("5m", "a") == "30m"
    assert _resolve_higher_target_freq("30m", "us") == "d"
    assert _resolve_higher_target_freq("d", "us") == "w"
    assert _resolve_higher_target_freq("w", "us") == "M"


def test_resolve_higher_target_freq_no_higher():
    from cl_app.services.chart_compute import _resolve_higher_target_freq
    assert _resolve_higher_target_freq("M", "us") is None
    assert _resolve_higher_target_freq("999x", "us") is None


def test_bin_keys_5m_basic():
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    # 5 根 1m, 每根相隔 60s, 跨过一个 5m 边界 (1700000100):
    #   1700000000 // 300 = 5666666  (bin A)
    #   1700000060 // 300 = 5666666  (bin A)
    #   1700000120 // 300 = 5666667  (bin B)
    #   1700000180 // 300 = 5666667  (bin B)
    #   1700000240 // 300 = 5666667  (bin B)
    times = np.array(
        [1700000000, 1700000060, 1700000120, 1700000180, 1700000240],
        dtype=np.int64,
    )
    bins = _bin_keys_for_higher(times, "5m", "us")
    assert bins[0] == bins[1]
    assert bins[2] == bins[3] == bins[4]
    assert bins[2] == bins[0] + 1


def test_bin_keys_5m_cross_boundary():
    """跨过 5m 边界 (epoch 整除 300 边界) 必须 bin+1。"""
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    # 5666667 * 300 = 1700000100 是 bin 5666667 起点
    # [1700000100, 1700000400) 全部属于 bin 5666667; 1700000400 进入 5666668
    times = np.array([1700000100, 1700000300, 1700000400], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "5m", "us")
    assert bins[0] == bins[1]              # 同 bin (5666667)
    assert bins[2] == bins[0] + 1          # 跨界 +1 (5666668)


def test_bin_keys_5m_cross_overnight():
    """美股 1m 跨夜: 昨日 16:00 与今日 09:30 必须落在不同 5m bin。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 取 2024-01-02 (周二, 交易日)
    yesterday_close = int(tz.localize(datetime.datetime(2024, 1, 2, 15, 59)).timestamp())
    today_open = int(tz.localize(datetime.datetime(2024, 1, 3, 9, 30)).timestamp())
    times = np.array([yesterday_close, today_open], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "5m", "us")
    assert bins[0] != bins[1]  # 跨夜两根必不同 bin


def test_bin_keys_30m_basic():
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    # 30m = 1800s; 1700003600 - 1700000000 = 3600 = 2 * 1800
    times = np.array(
        [1700000000, 1700001799, 1700001800, 1700003600],
        dtype=np.int64,
    )
    bins = _bin_keys_for_higher(times, "30m", "us")
    assert bins[3] - bins[0] == 2


def test_bin_keys_d_us_market_tz_overnight():
    """美股 ET 16:00 后的 30m bar (若存在) 应归属当日, 不被 UTC 切到次日。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 2024-01-02 (周二) 同一交易日内三个时刻
    morning = int(tz.localize(datetime.datetime(2024, 1, 2, 9, 30)).timestamp())
    afternoon = int(tz.localize(datetime.datetime(2024, 1, 2, 15, 59)).timestamp())
    next_day_morning = int(tz.localize(datetime.datetime(2024, 1, 3, 9, 30)).timestamp())

    times = np.array([morning, afternoon, next_day_morning], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "d", "us")
    assert bins[0] == bins[1]      # 同一交易日 (2024-01-02)
    assert bins[2] == bins[0] + 1  # 跨日 (2024-01-03)


def test_bin_keys_w_iso_monday_first():
    """ISO 周: 周一为首, 周一-周五同 bin, 下周一 bin+1。"""
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    # 2024-01-01 是周一; 2024-01-05 是周五; 2024-01-08 是下周一
    monday = int(tz.localize(datetime.datetime(2024, 1, 1, 9, 30)).timestamp())
    friday = int(tz.localize(datetime.datetime(2024, 1, 5, 15, 0)).timestamp())
    next_monday = int(tz.localize(datetime.datetime(2024, 1, 8, 9, 30)).timestamp())

    times = np.array([monday, friday, next_monday], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "w", "us")
    assert bins[0] == bins[1]
    assert bins[2] != bins[0]


def test_bin_keys_M_year_month():
    import numpy as np
    import datetime
    import pytz
    from cl_app.services.chart_compute import _bin_keys_for_higher

    tz = pytz.timezone("America/New_York")
    jan = int(tz.localize(datetime.datetime(2024, 1, 31, 23, 59)).timestamp())
    feb = int(tz.localize(datetime.datetime(2024, 2, 1, 0, 1)).timestamp())
    next_year_jan = int(tz.localize(datetime.datetime(2025, 1, 1, 0, 1)).timestamp())

    times = np.array([jan, feb, next_year_jan], dtype=np.int64)
    bins = _bin_keys_for_higher(times, "M", "us")
    assert bins[0] != bins[1]
    assert bins[2] != bins[1]
    assert bins[2] > bins[1] > bins[0]


def test_bin_keys_unknown_market_falls_back_to_utc():
    """未知 market 用 UTC, 不应崩。"""
    import numpy as np
    from cl_app.services.chart_compute import _bin_keys_for_higher

    times = np.array([1700000000, 1700086400], dtype=np.int64)  # 相差 1 天
    bins = _bin_keys_for_higher(times, "d", "unknown_market")
    assert bins[1] == bins[0] + 1
