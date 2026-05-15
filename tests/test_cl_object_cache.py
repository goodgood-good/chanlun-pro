"""tests/test_cl_object_cache.py — US-009 验证进程内 cl 对象 LRU 缓存。

AC:
- cache hit 同 signature 直接返回 (主路径)
- signature 不同时新建 + 全量 (避免 xds 累积 bug)
- 等价性: 走 cache vs 直接全量 cl_snapshot md5 相等
- LRU maxsize 触发淘汰
- 线程安全: 并发调用不丢/不串
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


@pytest.fixture(autouse=True)
def clear_cl_cache():
    """每个测试前清空全局 cache, 保证用例隔离。"""
    from cl_app.services.cl_object_cache import clear_all
    clear_all()
    yield
    clear_all()


def _snap_md5(cd: CL) -> str:
    snap = cl_snapshot(cd)
    blob = json.dumps(snap, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def test_cache_hit_returns_same_instance():
    """同 key + 同 K 线 signature, 两次调用返回同一个 CL 实例。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df = _generate_kline_df(100, seed=42, multi_freq=True)
    cd1 = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df)
    cd2 = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df)

    assert cd1 is cd2, "同 signature 应该返回同一个 CL 实例 (cache hit)"
    s = stats()
    assert s["hits"] == 1
    assert s["misses"] == 1


def test_signature_change_creates_new_cd():
    """K 线 signature 变化 (末根 close 改变) → 新建 cd, 不复用旧实例。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl

    df1 = _generate_kline_df(100, seed=42, multi_freq=True)
    df2 = df1.copy()
    df2.loc[df2.index[-1], "close"] = df2["close"].iloc[-1] + 5.0

    cd1 = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df1)
    cd2 = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df2)

    assert cd1 is not cd2, "signature 改变应触发新建"


def test_different_keys_isolated():
    """不同 (market, code, frequency, cl_config) 不互相影响。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl

    df = _generate_kline_df(100, seed=42, multi_freq=True)

    cd_a = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df)
    cd_b = get_or_compute_cl("us", "TEST.001", "1m", DEFAULT_CL_CONFIG, df)  # 不同 market
    cd_c = get_or_compute_cl("a", "TEST.002", "1m", DEFAULT_CL_CONFIG, df)  # 不同 code
    cd_d = get_or_compute_cl("a", "TEST.001", "5m", DEFAULT_CL_CONFIG, df)  # 不同 freq
    cd_e = get_or_compute_cl(
        "a", "TEST.001", "1m", {**DEFAULT_CL_CONFIG, "fx_qy": "fx_qy_middle"}, df
    )  # 不同 cl_config

    # 所有 5 个调用都是不同 key, 都是 miss
    instances = {id(cd_a), id(cd_b), id(cd_c), id(cd_d), id(cd_e)}
    assert len(instances) == 5, "5 个不同 key 应产生 5 个独立 CL 实例"


def test_cache_result_equivalent_to_direct_compute():
    """等价性: 走 cache 出来的 CL 对象 vs 直接 CL().process_klines() 应 snapshot 相同。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl

    df = _generate_kline_df(500, seed=42, multi_freq=True, trend="up")

    cd_cached = get_or_compute_cl("a", "TEST.001", "1m", DEFAULT_CL_CONFIG, df)
    cd_direct = CL("TEST.001", "1m", dict(DEFAULT_CL_CONFIG))
    cd_direct.process_klines(df)

    assert _snap_md5(cd_cached) == _snap_md5(cd_direct), (
        "cache 路径 vs 直接全量计算的 snapshot md5 必须相等 "
        "(任何不等都意味着 cache 内部偷偷做了有副作用的事)"
    )


def test_cl_config_dict_ordering_invariant():
    """同 cl_config 内容不同插入顺序应 hash 到同一 key (cache hit)。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df = _generate_kline_df(80, seed=42, multi_freq=True)
    cfg_a = {"a": 1, "b": 2, "c": 3}
    cfg_b = {"c": 3, "b": 2, "a": 1}  # 顺序不同

    cd1 = get_or_compute_cl("m", "T", "1m", cfg_a, df)
    cd2 = get_or_compute_cl("m", "T", "1m", cfg_b, df)
    assert cd1 is cd2
    assert stats()["hits"] == 1


def test_invalidate_clears_specific_key():
    from cl_app.services.cl_object_cache import get_or_compute_cl, invalidate, stats

    df = _generate_kline_df(50, seed=42, multi_freq=True)
    get_or_compute_cl("a", "X", "1m", DEFAULT_CL_CONFIG, df)
    assert stats()["size"] == 1

    n = invalidate("a", "X", "1m", DEFAULT_CL_CONFIG)
    assert n == 1
    assert stats()["size"] == 0


def test_invalidate_prefix_clears_all_configs():
    from cl_app.services.cl_object_cache import get_or_compute_cl, invalidate, stats

    df = _generate_kline_df(50, seed=42, multi_freq=True)
    get_or_compute_cl("a", "X", "1m", {"k": 1}, df)
    get_or_compute_cl("a", "X", "1m", {"k": 2}, df)
    assert stats()["size"] == 2

    n = invalidate("a", "X", "1m", cl_config=None)
    assert n == 2
    assert stats()["size"] == 0


def test_web_batch_get_cl_datas_uses_cache():
    """cl_utils.web_batch_get_cl_datas 接入 cache: 重复调用同 K 线复用 cd。"""
    from chanlun.cl_utils import web_batch_get_cl_datas
    from cl_app.services.cl_object_cache import stats

    df = _generate_kline_df(100, seed=42, multi_freq=True)
    cls1 = web_batch_get_cl_datas("a", "TEST.001", {"1m": df}, DEFAULT_CL_CONFIG)
    cls2 = web_batch_get_cl_datas("a", "TEST.001", {"1m": df}, DEFAULT_CL_CONFIG)

    assert cls1[0] is cls2[0], "web_batch_get_cl_datas 重复调用必须走 cache"
    s = stats()
    assert s["hits"] == 1
    assert s["misses"] == 1
