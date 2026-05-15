"""tests/test_apply_higher_macd.py — apply_higher_macd_to_chart_data 单元测试。

新算法(resample-based HTF MACD)测试覆盖:
- _resolve_higher_target_freq: 频率映射 + 未知 freq
- _bin_keys_for_higher: 5m / 30m / d / w / M 各分支, 含跨夜断层与未知市场
- _resample_closes_to_higher: 演化模式 + bin 切换 + 空输入
- apply_higher_macd_to_chart_data: 端到端 (短/长序列, NaN→None,
  未知 freq, 空 closes, numerical equivalence, 跨夜污染验证)
"""

from __future__ import annotations

from cl_app.services.chart_compute import apply_higher_macd_to_chart_data


def test_apply_short_series_no_op():
    """close 数量不足时不写 higher_macd_* 字段。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(5)],
        "c": [100.0] * 5,
    }
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    assert "higher_macd_dif" not in chart_data
    assert "higher_macd_dea" not in chart_data
    assert "higher_macd_hist" not in chart_data


def test_apply_long_series_writes_fields():
    """足够长的 close 序列会写 higher_macd_*。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0 + i * 0.1 for i in range(500)],
    }
    cfg = {"idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", cfg)
    assert "higher_macd_dif" in chart_data
    assert "higher_macd_dea" in chart_data
    assert "higher_macd_hist" in chart_data
    assert len(chart_data["higher_macd_dif"]) == 500


def test_apply_nan_replaced_with_none():
    """MACD 计算结果中头部 slow+signal 根都是 NaN, 应转成 None。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0 + i * 0.1 for i in range(500)],
    }
    apply_higher_macd_to_chart_data(chart_data, "1m", "a", {})
    # 头部应有 None (talib 在 slow+signal-1 根之前都返回 NaN)
    assert chart_data["higher_macd_dif"][0] is None
    # 末段应是浮点数
    assert isinstance(chart_data["higher_macd_dif"][-1], float)


def test_apply_unknown_frequency_no_op():
    """未知 frequency → target_freq=None → 不改 chart_data。"""
    chart_data = {
        "t": [1700000000 + i * 60 for i in range(500)],
        "c": [100.0] * 500,
    }
    before = dict(chart_data)
    apply_higher_macd_to_chart_data(chart_data, "999x", "a", {})
    assert chart_data == before


def test_apply_empty_closes_no_op():
    """close 为空时也不该崩。"""
    chart_data = {"t": [], "c": []}
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


def test_resample_evolving_close_same_bin():
    """同一 bin 内多根 close: higher_closes 取 bin 内最后一根 (演化模式)。"""
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([100.0, 101.0, 102.0, 103.0], dtype=float)
    bin_keys = np.array([1, 1, 1, 1], dtype=np.int64)  # 全部同 bin
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert len(higher_closes) == 1
    assert higher_closes[0] == 103.0  # bin 内 last close
    assert list(low2high) == [0, 0, 0, 0]


def test_resample_bin_switches():
    """bin 切换: higher_closes 长度增加, low2high 对应索引递增。"""
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([100.0, 101.0, 200.0, 201.0, 300.0], dtype=float)
    bin_keys = np.array([1, 1, 2, 2, 3], dtype=np.int64)
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert list(higher_closes) == [101.0, 201.0, 300.0]
    assert list(low2high) == [0, 0, 1, 1, 2]


def test_resample_empty_input():
    import numpy as np
    from cl_app.services.chart_compute import _resample_closes_to_higher

    closes = np.array([], dtype=float)
    bin_keys = np.array([], dtype=np.int64)
    higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)
    assert len(higher_closes) == 0
    assert len(low2high) == 0


def test_apply_numerical_equivalence_to_real_5m_macd():
    """新算法 HTF 投影回 1m 后, 必须 == 直接对手动合成 5m closes 跑
    talib.MACD(12,26,9) 得到的 hist 投影。
    """
    import numpy as np
    import talib
    from cl_app.services.chart_compute import (
        _bin_keys_for_higher,
        _resample_closes_to_higher,
    )

    # 构造 500 根 1m, 时间戳连续 60s 步长 (无跨夜)
    base = 1700000000
    t = np.array([base + i * 60 for i in range(500)], dtype=np.int64)
    c = np.array([100.0 + i * 0.1 for i in range(500)], dtype=float)
    chart_data = {"t": t.tolist(), "c": c.tolist()}
    cfg = {"idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9}
    apply_higher_macd_to_chart_data(chart_data, "1m", "us", cfg)

    # 手动合成 5m closes 跑 ref MACD
    bin_keys = _bin_keys_for_higher(t, "5m", "us")
    higher_closes, low2high = _resample_closes_to_higher(c, bin_keys)
    _, _, ref_hist = talib.MACD(higher_closes, 12, 26, 9)

    # 投影回 1m, 与 apply 输出对比
    actual = chart_data["higher_macd_hist"]
    for i in range(500):
        expected = ref_hist[low2high[i]]
        a = actual[i]
        if np.isnan(expected):
            assert a is None, f"i={i}: expected None, got {a}"
        else:
            assert a is not None, f"i={i}: expected {expected}, got None"
            assert abs(a - expected) < 1e-6, f"i={i}: expected {expected}, got {a}"


def test_apply_no_overnight_contamination():
    """跨夜两根 1m 必须落在不同 5m bin, 避免 EMA 穿越夜间。"""
    import numpy as np
    from cl_app.services.chart_compute import (
        _bin_keys_for_higher,
        apply_higher_macd_to_chart_data,
    )

    base = 1700000000
    # 前 300 根模拟"昨日", 后 300 根跨 17h 间隔后开盘
    t_with_overnight = (
        [base + i * 60 for i in range(300)]
        + [base + 300 * 60 + 17 * 3600 + i * 60 for i in range(300)]
    )
    c_with_overnight = (
        [100.0 + i * 0.1 for i in range(300)]
        + [200.0 + i * 0.1 for i in range(300)]
    )

    cd = {"t": t_with_overnight, "c": c_with_overnight}
    apply_higher_macd_to_chart_data(cd, "1m", "us", {})
    # 关键契约: 跨夜两根 1m bin_keys 必不同 (EMA 不再穿越夜间)
    bin_keys_b = _bin_keys_for_higher(
        np.array(t_with_overnight, dtype=np.int64), "5m", "us"
    )
    assert bin_keys_b[299] != bin_keys_b[300], (
        f"跨夜两根必须落不同 bin: bin[299]={bin_keys_b[299]}, "
        f"bin[300]={bin_keys_b[300]}"
    )
    # apply 完成后字段必然写入 (600 根 1m 合成约 120 根 5m, 远超 35 根门槛)
    assert "higher_macd_hist" in cd
    assert len(cd["higher_macd_hist"]) == 600
