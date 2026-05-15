"""tests/core/test_smoke.py — 验证 US-001 脚手架本身工作正常。

不测算法正确性 (那是 US-002/003/004 的事)。只验证:
- fixture 能返回 CL 对象
- 同 seed 两次调用结果完全一致 (确定性)
- 合成 K 线满足 OHLC 不变量
- with_gap 真的产生时间缺口
- 三种 trend 都能跑通 process_klines
- cl_snapshot 是可比较的 plain dict
"""

import pandas as pd

from tests.core.conftest import cl_snapshot


def test_fixture_returns_cl_with_correct_klines_count(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(100)
    assert cd is not None
    assert len(cd.get_klines()) == 100


def test_fixture_is_deterministic(cl_with_synthetic_klines):
    """同 seed 两次调用，snapshot 完全一致。"""
    cd1 = cl_with_synthetic_klines(80, seed=42)
    cd2 = cl_with_synthetic_klines(80, seed=42)
    assert cl_snapshot(cd1) == cl_snapshot(cd2)


def test_fixture_different_seeds_give_different_results(cl_with_synthetic_klines):
    """不同 seed 应产生不同序列 (排除小概率巧合)。"""
    cd1 = cl_with_synthetic_klines(80, seed=42)
    cd2 = cl_with_synthetic_klines(80, seed=7)
    # 至少 bis 列表不一样 (合成数据足够长，碰撞概率极低)
    assert cl_snapshot(cd1) != cl_snapshot(cd2)


def test_make_klines_df_ohlc_invariants(make_klines_df):
    df = make_klines_df(50, seed=42)
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["high"] >= df["low"]).all()


def test_make_klines_df_with_gap_produces_time_gap(make_klines_df):
    df = make_klines_df(100, seed=42, with_gap=True)
    deltas = df["date"].diff().dropna()
    # 至少有一根时间间隔 > 1 小时 (默认 freq=1min，gap=1d)
    assert (deltas > pd.Timedelta(hours=1)).any()


def test_fixture_supports_all_trends(cl_with_synthetic_klines):
    for trend in ("up", "down", "oscillate"):
        cd = cl_with_synthetic_klines(60, trend=trend)
        assert cd is not None
        assert len(cd.get_klines()) == 60


def test_cl_snapshot_returns_plain_dict(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(50)
    snap = cl_snapshot(cd)
    assert isinstance(snap, dict)
    assert "klines_count" in snap
    assert "fxs" in snap
    assert "bis" in snap
    assert "xds" in snap
    assert "bi_zss" in snap
    assert "xd_zss" in snap
    assert snap["klines_count"] == 50
