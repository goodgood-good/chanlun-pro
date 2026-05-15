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
