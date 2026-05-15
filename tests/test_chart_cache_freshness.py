"""tests/test_chart_cache_freshness.py — M3 _entry_freshness 统一接口。

验证 polling (30s) 与 first_request (3600s) 双阈值通过同一函数路由,
旧接口 (_cache_entry_recently_validated / _full_snapshot_is_stale) 仍工作。
"""

from __future__ import annotations

import time

import pytest

from cl_app.services.chart_cache import (
    _CACHE_REVALIDATION_INTERVAL,
    _SNAPSHOT_STALE_AFTER,
    _cache_entry_recently_validated,
    _entry_freshness,
    _full_snapshot_is_stale,
)


def _entry(secs_ago: float) -> dict:
    return {
        "data": {},
        "validated_at": time.time() - secs_ago,
        "is_full_snapshot": True,
    }


def test_polling_mode_30s_threshold():
    """polling 模式: 30s 内 fresh, 超过 stale。"""
    assert _entry_freshness(_entry(5), mode="polling") == "fresh"
    assert _entry_freshness(_entry(_CACHE_REVALIDATION_INTERVAL + 1), mode="polling") == "stale"


def test_first_request_mode_3600s_threshold():
    """first_request 模式: 3600s 内 fresh, 超过 stale。"""
    assert _entry_freshness(_entry(60), mode="first_request") == "fresh"
    assert _entry_freshness(_entry(_SNAPSHOT_STALE_AFTER + 1), mode="first_request") == "stale"


def test_polling_stale_at_31s_first_request_still_fresh():
    """关键: 31s 时刻, polling 已 stale 但 first_request 仍 fresh —— 双阈值正交工作。

    这是把"polling 频率 + 重启识别"两个语义解耦的核心。
    """
    e = _entry(_CACHE_REVALIDATION_INTERVAL + 1)  # 31s ago
    assert _entry_freshness(e, mode="polling") == "stale"
    assert _entry_freshness(e, mode="first_request") == "fresh"


@pytest.mark.parametrize("bad_entry", [None, "not a dict", {}, {"validated_at": None}, {"validated_at": -1}])
def test_unknown_returned_for_malformed(bad_entry):
    """非 dict / 缺字段 / 非法值 → unknown (上游按 stale 处理)。"""
    assert _entry_freshness(bad_entry, mode="polling") == "unknown"
    assert _entry_freshness(bad_entry, mode="first_request") == "unknown"


def test_legacy_recently_validated_still_works():
    """旧 API _cache_entry_recently_validated 委托给 polling 模式, 行为不变。"""
    assert _cache_entry_recently_validated(_entry(5)) is True
    assert _cache_entry_recently_validated(_entry(_CACHE_REVALIDATION_INTERVAL + 1)) is False


def test_legacy_full_snapshot_is_stale_still_works():
    """旧 API _full_snapshot_is_stale 委托给 first_request 模式, 行为不变。"""
    assert _full_snapshot_is_stale(_entry(60)) is False
    assert _full_snapshot_is_stale(_entry(_SNAPSHOT_STALE_AFTER + 1)) is True
    # unknown / malformed 仍按 stale 处理 (与旧实现一致)
    assert _full_snapshot_is_stale(None) is True
    assert _full_snapshot_is_stale({}) is True


def test_persist_chart_cache_async_snapshots_entry():
    """回归: _persist_chart_cache_async 应当 deepcopy entry, 避免主线程在 cache_lock
    外 in-place 修改 entry["data"] (例如 tv_history cache hit 路径 lazy 补算
    apply_higher_macd_to_chart_data) 时, 异步 pickle.dump 抛
    "RuntimeError: dictionary changed size during iteration"。
    """
    import threading
    from cl_app.services import chart_cache

    captured: dict = {}
    captured_event = threading.Event()

    def fake_set_chart_cache(key, entry):
        captured["key"] = key
        captured["entry"] = entry
        captured_event.set()

    orig_set = chart_cache.fdb.set_chart_cache
    chart_cache.fdb.set_chart_cache = fake_set_chart_cache
    try:
        entry = {
            "data": {"t": [1, 2, 3], "c": [100.0, 101.0, 102.0]},
            "min_time": 1,
            "is_full_snapshot": True,
        }
        chart_cache._persist_chart_cache_async("test_snapshot_key", entry)
        # 立即在调用方线程 in-place 修改 entry["data"], 模拟 lazy 补算 / 后续路径
        entry["data"]["t"].append(4)
        entry["data"]["new_field"] = "added"
        del entry["data"]["c"]

        assert captured_event.wait(timeout=5), "disk worker did not complete in time"
        snapshot = captured["entry"]
        # 关键断言: worker 拿到的 snapshot 不应受 in-place 改动影响
        assert snapshot["data"]["t"] == [1, 2, 3], (
            f"snapshot should NOT contain in-place append; got t={snapshot['data']['t']}"
        )
        assert "new_field" not in snapshot["data"], (
            "snapshot should NOT contain new in-place key 'new_field'"
        )
        assert "c" in snapshot["data"], (
            "snapshot should NOT lose 'c' key after caller deletes from original entry"
        )
        assert snapshot["data"]["c"] == [100.0, 101.0, 102.0]
    finally:
        chart_cache.fdb.set_chart_cache = orig_set
