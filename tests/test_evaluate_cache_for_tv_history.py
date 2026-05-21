"""tests/test_evaluate_cache_for_tv_history.py — P5 first step 单元测试。

evaluate_cache_for_tv_history 从 tv_history 内嵌 closure 抽出, 现在可独立测试
所有 cache hit / miss 分支, 不依赖完整 Flask 集成。
"""

from __future__ import annotations

import time


from cl_app.services.chart_cache import (
    _CACHE_REVALIDATION_INTERVAL,
    _SNAPSHOT_STALE_AFTER,
    evaluate_cache_for_tv_history,
)


def _entry(
    *,
    is_full_snapshot: bool = True,
    min_time: int = 1000,
    max_time: int = 2000,
    validated_at: float | None = None,
) -> dict:
    return {
        "data": {"t": [min_time, max_time], "c": [100.0, 110.0]},
        "min_time": min_time,
        "max_time": max_time,
        "validated_at": validated_at if validated_at is not None else time.time(),
        "is_full_snapshot": is_full_snapshot,
    }


# === 非 range request (firstDataRequest=true) 分支 ===

def test_first_request_cache_empty():
    """cache miss: entry=None。"""
    hit, data, reason = evaluate_cache_for_tv_history(None, 0, 0, is_range_request=False)
    assert (hit, data, reason) == (False, None, "cache_empty")


def test_first_request_full_snapshot_fresh_hits():
    """firstDataRequest=true + full snapshot + 时效内 → hit。"""
    e = _entry(is_full_snapshot=True)
    hit, data, reason = evaluate_cache_for_tv_history(e, 0, 0, is_range_request=False)
    assert hit is True
    assert reason is None


def test_first_request_partial_snapshot_miss():
    """firstDataRequest=true + partial snapshot → miss。"""
    e = _entry(is_full_snapshot=False)
    hit, _, reason = evaluate_cache_for_tv_history(e, 0, 0, is_range_request=False)
    assert (hit, reason) == (False, "cache_partial_snapshot")


def test_first_request_stale_full_snapshot_miss():
    """firstDataRequest=true + full snapshot 但过期 → miss。"""
    e = _entry(is_full_snapshot=True, validated_at=time.time() - _SNAPSHOT_STALE_AFTER - 100)
    hit, _, reason = evaluate_cache_for_tv_history(e, 0, 0, is_range_request=False)
    assert (hit, reason) == (False, "cache_stale_snapshot")


# === range request (firstDataRequest=false, from/to >0) 分支 ===

def test_range_request_cache_empty():
    hit, _, reason = evaluate_cache_for_tv_history(None, 100, 200, is_range_request=True)
    assert (hit, reason) == (False, "cache_empty")


def test_range_request_no_coverage():
    """range request 但 entry 缺 min/max_time → miss。"""
    e = _entry(min_time=None, max_time=None)
    hit, _, reason = evaluate_cache_for_tv_history(e, 100, 200, is_range_request=True)
    assert (hit, reason) == (False, "cache_no_coverage")


def test_range_request_head_gap():
    """请求起点 < cache.min_time → cache_head_gap。"""
    e = _entry(min_time=1000, max_time=2000)
    hit, _, reason = evaluate_cache_for_tv_history(e, 500, 1500, is_range_request=True)
    assert (hit, reason) == (False, "cache_head_gap")


def test_range_request_within_coverage_hits():
    """请求完全在 cache 区间内 → hit。"""
    e = _entry(min_time=1000, max_time=2000)
    hit, data, reason = evaluate_cache_for_tv_history(e, 1200, 1800, is_range_request=True)
    assert hit is True
    assert reason is None


def test_range_request_tail_gap_recently_validated_hits():
    """请求末端 > cache.max_time 但 entry 30s 内被验证 → 仍 hit。"""
    e = _entry(min_time=1000, max_time=2000, validated_at=time.time() - 5)
    hit, _, reason = evaluate_cache_for_tv_history(e, 1500, 2500, is_range_request=True)
    assert hit is True


def test_range_request_tail_gap_stale_miss():
    """请求末端 > cache.max_time 且 entry 未及时验证 → cache_tail_gap。"""
    e = _entry(min_time=1000, max_time=2000, validated_at=time.time() - _CACHE_REVALIDATION_INTERVAL - 5)
    hit, _, reason = evaluate_cache_for_tv_history(e, 1500, 2500, is_range_request=True)
    assert (hit, reason) == (False, "cache_tail_gap")


# === 边界 ===

def test_from_equal_min_time_is_within_coverage():
    """from == min_time 不算 head_gap (左闭右开)。"""
    e = _entry(min_time=1000, max_time=2000)
    hit, _, reason = evaluate_cache_for_tv_history(e, 1000, 1500, is_range_request=True)
    assert hit is True


def test_to_equal_max_time_is_within_coverage():
    """to == max_time 不算 tail_gap。"""
    e = _entry(min_time=1000, max_time=2000)
    hit, _, reason = evaluate_cache_for_tv_history(e, 1500, 2000, is_range_request=True)
    assert hit is True
