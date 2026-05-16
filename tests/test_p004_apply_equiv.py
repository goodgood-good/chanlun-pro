"""P-004 .apply 向量化前后等价对照测试。

每个测试对应一类 apply → 向量化 变换形状，
用 view("int64") 比较纳秒整数，避免 tz-label 差异干扰。
"""
import datetime
import sys

import numpy as np
import pandas as pd
import pytz

sys.path.insert(0, "src")


# ---------------------------------------------------------------------------
# 类 A：astimezone(UTC) apply  vs  dt.tz_convert  (exchange.py ~837)
# ---------------------------------------------------------------------------
def test_classA_tz_convert_to_utc():
    """类 A：tz-aware series.apply(dt.astimezone(UTC)) ≡ .dt.tz_convert('UTC')"""
    s = pd.Series(
        pd.date_range("2024-01-01", periods=50, freq="1h", tz="Asia/Shanghai")
    )
    old = s.apply(lambda dt: dt.astimezone(pytz.UTC))
    new = s.dt.tz_convert(pytz.UTC)
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


# ---------------------------------------------------------------------------
# 类 B-1：条件加 timedelta (exchange.py ~585, 期货日线夜盘偏移)
# ---------------------------------------------------------------------------
def test_classB1_conditional_shift_hours():
    """类 B-1：hour in [21,22,23,0,1,2] → +3h，其余不变，用 .mask() 向量化"""
    s = pd.Series(
        pd.date_range("2024-01-01 00:00", periods=48, freq="1h", tz="Asia/Shanghai")
    )
    old = s.apply(
        lambda x: (
            x + datetime.timedelta(hours=3) if x.hour in [21, 22, 23, 0, 1, 2] else x
        )
    )
    mask = s.dt.hour.isin([21, 22, 23, 0, 1, 2])
    new = s.mask(mask, s + pd.Timedelta(hours=3))
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


# ---------------------------------------------------------------------------
# 类 B-2：replace(hour=15, minute=0, second=0) on naive DatetimeIndex
#          (exchange.py ~912, ~934)
# ---------------------------------------------------------------------------
def test_classB2_replace_hour_naive():
    """类 B-2：naive dt.replace(hour=15, min=0, sec=0) ≡ normalize() + Timedelta"""
    # 模拟 groupby trade_day 后得到的 naive DatetimeIndex values
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    s = pd.Series(dates)
    old = s.apply(lambda x: x.replace(hour=15, minute=0, second=0))
    new = s.dt.normalize() + pd.Timedelta(hours=15)
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


# ---------------------------------------------------------------------------
# 类 C-1：fromtimestamp(ms/1e3).astimezone(tz)  →  pd.to_datetime(unit='ms',utc=True).dt.tz_convert
#          (exchange_binance*.py ~288, ~370, ~286, ~368)
# ---------------------------------------------------------------------------
def test_classC1_fromtimestamp_ms_to_localtz():
    """类 C-1：ms 时间戳 fromtimestamp(x/1e3).astimezone(tz) ≡ pd.to_datetime(unit='ms',utc=True).tz_convert"""
    tz = pytz.timezone("Asia/Shanghai")
    # 模拟交易所返回的毫秒时间戳
    ms_series = pd.Series(
        [1704067200000, 1704070800000, 1704074400000, 1704078000000, 1704081600000],
        dtype="int64",
    )
    old = ms_series.apply(
        lambda x: datetime.datetime.fromtimestamp(x / 1e3).astimezone(tz)
    )
    new = pd.to_datetime(ms_series, unit="ms", utc=True).dt.tz_convert(tz)
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


# ---------------------------------------------------------------------------
# 类 C-2：baostock/ib/futu __convert_date: replace(hour=H, min=M) when 00:00:00
#          exchange_baostock.py ~197  → replace(hour=15, minute=0)
#          exchange_ib.py ~168        → replace(hour=9, minute=30)
#          exchange_futu.py ~174      → replace(hour=16, minute=0)
# ---------------------------------------------------------------------------
def _make_naive_series_with_midnight():
    """构造含 00:00:00 和非零时刻的 naive datetime series"""
    dates = pd.to_datetime(
        [
            "2024-01-02 00:00:00",
            "2024-01-02 09:30:00",
            "2024-01-03 00:00:00",
            "2024-01-03 14:30:00",
            "2024-01-04 00:00:00",
        ]
    )
    return pd.Series(dates)


def test_classC2_baostock_convert_date():
    """类 C-2 baostock：midnight → replace(hour=15)，其余不变"""
    s = _make_naive_series_with_midnight()
    old = s.apply(
        lambda dt: dt.replace(hour=15, minute=0)
        if (dt.hour == 0 and dt.minute == 0 and dt.second == 0)
        else dt
    )
    mask = (s.dt.hour == 0) & (s.dt.minute == 0) & (s.dt.second == 0)
    new = s.mask(mask, s + pd.Timedelta(hours=15))
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


def test_classC2_ib_convert_date():
    """类 C-2 ib：midnight (tz-aware) → replace(hour=9, minute=30)，其余不变"""
    tz = pytz.timezone("America/New_York")
    s_naive = _make_naive_series_with_midnight()
    s = s_naive.dt.tz_localize(tz)
    old = s.apply(
        lambda dt: dt.replace(hour=9, minute=30)
        if (dt.hour == 0 and dt.minute == 0 and dt.second == 0)
        else dt
    )
    mask = (s.dt.hour == 0) & (s.dt.minute == 0) & (s.dt.second == 0)
    new = s.mask(mask, s + pd.Timedelta(hours=9, minutes=30))
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


def test_classC2_futu_convert_date():
    """类 C-2 futu：midnight (tz-aware) → replace(hour=16, minute=0)，其余不变"""
    tz = pytz.timezone("Asia/Hong_Kong")
    s_naive = _make_naive_series_with_midnight()
    s = s_naive.dt.tz_localize(tz)
    old = s.apply(
        lambda dt: dt.replace(hour=16, minute=0)
        if (dt.hour == 0 and dt.minute == 0 and dt.second == 0)
        else dt
    )
    mask = (s.dt.hour == 0) & (s.dt.minute == 0) & (s.dt.second == 0)
    new = s.mask(mask, s + pd.Timedelta(hours=16))
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


# ---------------------------------------------------------------------------
# 类 C-3：get_ny_future_trade_day 向量化 (exchange.py ~906, ~928)
# ---------------------------------------------------------------------------
def _get_ny_future_trade_day_apply(dt):
    """原始 get_ny_future_trade_day 函数逻辑（逐元素版）"""
    if dt.hour < 6:
        trade_day = (dt - pd.Timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        trade_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return trade_day


def _get_ny_future_trade_day_vectorized(s: pd.Series) -> pd.Series:
    """向量化版 get_ny_future_trade_day"""
    normalized = s.dt.normalize()
    mask = s.dt.hour < 6
    return normalized.mask(mask, normalized - pd.Timedelta(days=1))


def test_classC3_ny_future_trade_day():
    """类 C-3：get_ny_future_trade_day apply 向量化等价"""
    tz = pytz.timezone("America/New_York")
    s = pd.Series(
        pd.date_range("2024-01-01 00:00", periods=48, freq="1h", tz=tz)
    )
    old = s.apply(_get_ny_future_trade_day_apply)
    new = _get_ny_future_trade_day_vectorized(s)
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()


def test_classC3_ny_future_trade_week():
    """类 C-3：get_ny_future_trade_week apply 向量化等价（依赖 trade_day）"""
    import sys

    sys.path.insert(0, "src")
    from chanlun.exchange.exchange import get_ny_future_trade_week

    tz = pytz.timezone("America/New_York")
    s = pd.Series(
        pd.date_range("2024-01-01 00:00", periods=7 * 24, freq="1h", tz=tz)
    )
    old = s.apply(get_ny_future_trade_week)

    # 向量化版
    trade_day = _get_ny_future_trade_day_vectorized(s)
    new = trade_day - pd.to_timedelta(trade_day.dt.dayofweek, unit="D")
    assert (old.view("int64").to_numpy() == new.view("int64").to_numpy()).all()
