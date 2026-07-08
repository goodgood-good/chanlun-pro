"""Round12 final-gate MEDIUM-2: _compute_kline_signature 漏末根 O/H/L。末根 high/low 变而
close/date/根数不变时, signature(末根只含 last_date+last_close, hist_fp 又刻意排除末根)完全相同
→ path-1(entry.signature==sig)判完全命中, 返回**陈旧** CL 对象(不重算)→ 下发陈旧末根 O/H/L +
末根附近顶/底分型缺失。修复=末根 open/high/low 追加进 signature 末尾(不动 0-8 索引避免破坏
_is_extending_signature)+ bump schema 版本。"""

import pandas as pd
import pytest

from cl_app.services.cl_object_cache import get_or_compute_cl, stats, clear_all


def _klines_df(ts, prices):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(ts), unit="s", utc=True),
            "open": list(prices),
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": list(prices),
            "volume": [1000] * len(ts),
        }
    )


class _FakeCL:
    instances = []

    def __init__(self, *a, **k):
        _FakeCL.instances.append(self)
        self.klines = None

    def process_klines(self, klines):
        self.klines = klines


@pytest.fixture
def mock_cl(monkeypatch):
    _FakeCL.instances = []
    monkeypatch.setattr("chanlun.core.cl.CL", _FakeCL)
    clear_all()
    yield
    clear_all()


def test_last_bar_high_change_not_served_stale(mock_cl):
    """末根 high 冲高(close/date/根数不变)→ signature 须失配 → 不返回陈旧 cd。"""
    n = 60
    ts = [1000 + i * 60 for i in range(n)]
    prices = [10.0 + i * 0.1 for i in range(n)]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts, prices))  # full #1

    df2 = _klines_df(ts, prices)
    df2.loc[df2.index[-1], "high"] = float(df2.iloc[-1]["high"]) + 8.0  # 仅末根 high +8
    cd2 = get_or_compute_cl("us", "T", "5m", None, df2)

    served = float(cd2.klines.iloc[-1]["high"])
    expected = float(df2.iloc[-1]["high"])
    # 修复前: path-1 命中陈旧 cd1(其 klines 末根 high 为旧值)→ served != expected
    assert abs(served - expected) < 1e-9, f"末根high变被path-1陈旧命中: served={served} expected={expected}"


def test_last_bar_low_change_not_served_stale(mock_cl):
    """末根 low 下探(close/date/根数不变)→ 同理不应陈旧命中。"""
    n = 60
    ts = [1000 + i * 60 for i in range(n)]
    prices = [10.0 + i * 0.1 for i in range(n)]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts, prices))
    df2 = _klines_df(ts, prices)
    df2.loc[df2.index[-1], "low"] = float(df2.iloc[-1]["low"]) - 8.0
    cd2 = get_or_compute_cl("us", "T", "5m", None, df2)
    assert abs(float(cd2.klines.iloc[-1]["low"]) - float(df2.iloc[-1]["low"])) < 1e-9


def test_pure_append_still_incremental_after_fix(mock_cl):
    """回归:纯追加(历史+末根不变, 加新bar)仍走增量, 不因新末根字段退化。"""
    n = 60
    ts = [1000 + i * 60 for i in range(n)]
    prices = [10.0 + i * 0.1 for i in range(n)]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts, prices))
    ts2 = ts + [1000 + n * 60]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts2, prices + [16.0]))
    assert stats()["incremental_extends"] == 1, f"纯追加应仍走增量: {stats()}"