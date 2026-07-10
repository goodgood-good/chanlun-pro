"""convert_tdx_futures_kline_frequency 合成 3m 必须把 date 钉到网格右边界(bin 结束时刻),
与兄弟频率 2m/5m 一致(新R6-E1)。修复前 exchange.py:560 网格对齐列表漏 '3m' → 3m 保留
resample 的 date='last'(源末根时间戳), 实时进行中 bin 会随续 1m bar 到来逐分钟漂移
(同已修 us_tdx 10m date=last 漂移致 SSE merge 累积假 bar, 86024fa8)。
gm/tq 版 convert_futures_kline_frequency(exchange.py:362 shift 列表)明确含 '3m', 坐实笔误。"""

import pandas as pd

from chanlun.exchange.exchange import convert_tdx_futures_kline_frequency


def _mk_1m(n, start="2024-01-02 09:01:00"):
    # 通达信期货 eob 口径: 时间戳=bar 结束时刻, 1m 连续
    idx = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame(
        {
            "code": ["IF2401"] * n,
            "date": idx,
            "open": range(n),
            "close": range(n),
            "high": range(n),
            "low": range(n),
            "volume": [1] * n,
        }
    )


def test_futures_3m_inprogress_bin_date_snapped_to_grid():
    # 09:01..09:05 共5根: bin(09:03,09:06] 仅到09:05(进行中, 缺09:06)
    # 修复前 date='last'=09:05(漂移); 修复后钉到网格右边界 09:03+3min=09:06
    k = _mk_1m(5, "2024-01-02 09:01:00")
    out = convert_tdx_futures_kline_frequency(k, "3m")
    last_date = pd.Timestamp(out.iloc[-1]["date"])
    assert last_date == pd.Timestamp("2024-01-02 09:06:00"), f"got {last_date}"


def test_futures_3m_closed_bin_unchanged():
    # 完整 bin last==网格, 修复前后皆 09:03(历史 bar 逐字节不变的佐证)
    k = _mk_1m(3, "2024-01-02 09:01:00")  # 09:01,09:02,09:03 满一个bin
    out = convert_tdx_futures_kline_frequency(k, "3m")
    first_date = pd.Timestamp(out.iloc[0]["date"])
    assert first_date == pd.Timestamp("2024-01-02 09:03:00"), f"got {first_date}"


def test_futures_2m_sibling_still_grid_aligned():
    # 防呆: 兄弟频率 2m(已在网格列表)行为不变
    k = _mk_1m(3, "2024-01-02 09:01:00")
    out = convert_tdx_futures_kline_frequency(k, "2m")
    # bin(09:00,09:02]=09:01,09:02 → 网格右边界 09:02
    assert pd.Timestamp(out.iloc[0]["date"]) == pd.Timestamp("2024-01-02 09:02:00")
