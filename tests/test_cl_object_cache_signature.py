"""tests/test_cl_object_cache_signature.py — 验证 signature 含中段 ref-bar (R3)。

复权事件下旧 3 元组 signature (len, last_date, last_close) 会假命中:
全部 bar 等比例缩放后末根 date 不变, last_close 量级也一致, signature
旧值意外命中 → 错误复用 cd 状态。

新 9 元组加入中段 ref-bar OHLC, 复权后任何中段 OHLC 字段都会变, 强制 miss。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    from cl_app.services.cl_object_cache import clear_all
    clear_all()
    yield
    clear_all()


def _mk_df(n=200, base=100.0, scale=1.0, seed=42):
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=float)
    closes = base + 5 * np.sin(t / 6.0) + 0.02 * t + rng.normal(0, 0.05, size=n)
    closes *= scale  # 模拟复权: 全部价格等比例缩放
    highs = closes + 0.6
    lows = closes - 0.6
    opens = closes - 0.05 * np.sin(t / 3.0)
    volumes = (1000 + (t.astype(int) % 7) * 50).astype(float)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01 09:30", periods=n, freq="1min", tz="UTC"),
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes,
    })


def test_signature_distinguishes_split_event():
    """复权事件 (全部 bar 等比例 0.5×): 旧 3 元组 signature 在末根 close 量级相近时
    可能错误命中; 新 8 元组中段 ref-bar 一定能识别。
    """
    from cl_app.services.cl_object_cache import _compute_kline_signature

    df_pre = _mk_df(n=200, scale=1.0)
    df_post = _mk_df(n=200, scale=0.5)  # 假设发生 2:1 拆股, 价格全部减半

    sig_pre = _compute_kline_signature(df_pre)
    sig_post = _compute_kline_signature(df_post)

    assert sig_pre != sig_post, (
        f"复权后 signature 必须变化:\n  pre:  {sig_pre}\n  post: {sig_post}"
    )


def test_signature_stable_for_same_data():
    """同一份 K 线 signature 必须稳定 (避免无谓 miss)。"""
    from cl_app.services.cl_object_cache import _compute_kline_signature
    df = _mk_df(n=200)
    assert _compute_kline_signature(df) == _compute_kline_signature(df)


def test_signature_changes_when_ref_ohlc_modified():
    """ref-bar (绝对位置 _ref_bar_index(n)) 任一 OHLC 改变 → signature 一定变化。"""
    from cl_app.services.cl_object_cache import _compute_kline_signature, _ref_bar_index

    df1 = _mk_df(n=200)
    sig1 = _compute_kline_signature(df1)

    # F1: 用绝对位置 ref_idx (min(50, n//4))
    n = len(df1)
    ref_idx = _ref_bar_index(n)
    df2 = df1.copy()
    df2.loc[df2.index[ref_idx], "high"] = df2["high"].iloc[ref_idx] + 1.0

    sig2 = _compute_kline_signature(df2)
    assert sig1 != sig2


def test_signature_changes_when_last_close_changed():
    """末根 close 改变 (实时 tick) → signature 变化, 主路径仍工作。"""
    from cl_app.services.cl_object_cache import _compute_kline_signature

    df1 = _mk_df(n=200)
    df2 = df1.copy()
    df2.loc[df2.index[-1], "close"] = df2["close"].iloc[-1] + 5.0

    assert _compute_kline_signature(df1) != _compute_kline_signature(df2)


def test_signature_length_consistent_across_short_and_long():
    """短序列 (<12) 与长序列 signature 都是 8 元组, 便于 tuple 比较。"""
    from cl_app.services.cl_object_cache import _compute_kline_signature
    short = _mk_df(n=5)
    long_ = _mk_df(n=200)
    assert len(_compute_kline_signature(short)) == len(_compute_kline_signature(long_)) == 8


def test_signature_empty_df():
    from cl_app.services.cl_object_cache import _compute_kline_signature
    sig = _compute_kline_signature(pd.DataFrame())
    assert sig[0] == 0
    assert len(sig) == 8


def test_split_event_triggers_cache_miss():
    """端到端: 同 key 的两次调用, 第二次喂入复权后数据, 不应 cache hit。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df_pre = _mk_df(n=200, scale=1.0)
    df_post = _mk_df(n=200, scale=0.5)

    cd_pre = get_or_compute_cl("a", "T.SPLIT", "1m", {}, df_pre)
    cd_post = get_or_compute_cl("a", "T.SPLIT", "1m", {}, df_post)

    assert cd_pre is not cd_post, "复权后应触发 cache miss 重建, 不能复用旧 cd"
    s = stats()
    assert s["misses"] == 2
    assert s["hits"] == 0
