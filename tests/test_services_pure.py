"""services 层纯函数测试集（Tier 4 收尾保险）。

覆盖 L1+Tier4 重构中迁出的纯函数：
- chart_cache: _stable_hash / _build_cache_key / _build_chart_cache_entry /
              _normalize_cache_entry / _cache_entry_recently_validated
- chart_compute: _shape_time / _merge_shape_lists / _merge_chart_data /
                _SafeLockRegistry
- stock_list: _process_stock_list
"""
import datetime
import time

import pytest

from cl_app.services.chart_cache import (  # noqa: E402
    _CACHE_REVALIDATION_INTERVAL,
    _build_cache_key,
    _build_chart_cache_entry,
    _cache_entry_recently_validated,
    _normalize_cache_entry,
    _stable_hash,
)
from cl_app.services.chart_compute import (  # noqa: E402
    _SafeLockRegistry,
    _merge_chart_data,
    _merge_shape_lists,
    _shape_time,
)
from cl_app.services.stock_list import _process_stock_list  # noqa: E402


# =============================================================================
# chart_cache pure functions
# =============================================================================


class TestStableHash:
    def test_dict_order_independence(self):
        # JSON sort_keys=True 保证不同 dict 顺序产出相同 hash
        h1 = _stable_hash({"a": 1, "b": 2, "c": 3})
        h2 = _stable_hash({"c": 3, "b": 2, "a": 1})
        assert h1 == h2

    def test_consistent_across_calls(self):
        obj = {"x": [1, 2, 3], "y": "test"}
        assert _stable_hash(obj) == _stable_hash(obj)

    def test_different_objects_different_hash(self):
        assert _stable_hash({"a": 1}) != _stable_hash({"a": 2})

    def test_handles_non_jsonable_via_str_fallback(self):
        # datetime 不是直接 JSON 可序列化，但 str() 兜底（json default=str）
        h = _stable_hash({"time": datetime.datetime(2025, 1, 1)})
        assert isinstance(h, str)
        assert len(h) == 32  # md5 hex 长度


class TestBuildCacheKey:
    def test_format(self):
        k = _build_cache_key("a", "000001", "5m", {"x": 1})
        # 格式:schema_version + market + code + freq + hash 五段。
        # schema_version 是 cache_key 跨版本失效的开关——bump 后旧磁盘 entry
        # 自动被绕过。
        parts = k.split("_")
        assert parts[0].startswith("v"), f"schema version 必须以 'v' 开头,实际 {parts[0]!r}"
        assert parts[1] == "a"
        assert parts[2] == "000001"
        assert parts[3] == "5m"
        assert len(parts[4]) == 32

    def test_same_inputs_same_key(self):
        cfg = {"foo": "bar"}
        assert _build_cache_key("a", "X", "1d", cfg) == _build_cache_key("a", "X", "1d", cfg)

    def test_different_market_different_key(self):
        cfg = {"foo": "bar"}
        assert _build_cache_key("a", "X", "1d", cfg) != _build_cache_key(
            "us", "X", "1d", cfg
        )


class TestBuildChartCacheEntry:
    def test_full_snapshot_fields(self):
        e = _build_chart_cache_entry(
            {"t": [10, 20, 30], "c": [1, 2, 3]}, is_full_snapshot=True
        )
        assert {"data", "min_time", "max_time", "validated_at", "is_full_snapshot"} <= set(e.keys())
        assert e["min_time"] == 10
        assert e["max_time"] == 30
        assert e["is_full_snapshot"] is True

    def test_empty_data(self):
        e = _build_chart_cache_entry({}, is_full_snapshot=False)
        assert e["min_time"] is None
        assert e["max_time"] is None
        assert e["is_full_snapshot"] is False

    def test_validated_at_explicit(self):
        e = _build_chart_cache_entry(
            {"t": [1]}, is_full_snapshot=True, validated_at=12345.0
        )
        assert e["validated_at"] == 12345.0


class TestNormalizeCacheEntry:
    def test_none_returns_none(self):
        assert _normalize_cache_entry(None) is None

    def test_normalized_returned_as_is(self):
        e = {
            "data": {"t": [1]},
            "validated_at": 100.0,
            "min_time": 1,
            "max_time": 1,
            "is_full_snapshot": True,
        }
        assert _normalize_cache_entry(e) is e

    def test_legacy_dict_wrapped(self):
        # 老格式：直接是 chart data dict，没有 validated_at
        legacy = {"t": [1, 2], "c": [10, 20]}
        e = _normalize_cache_entry(legacy)
        assert e is not None
        assert "validated_at" in e
        assert e["data"] == legacy
        assert e["is_full_snapshot"] is True

    def test_non_dict_returns_none(self):
        assert _normalize_cache_entry(42) is None
        assert _normalize_cache_entry("abc") is None
        assert _normalize_cache_entry([1, 2]) is None


class TestCacheEntryRecentlyValidated:
    def test_recent_returns_true(self):
        e = {"validated_at": time.time()}
        assert _cache_entry_recently_validated(e) is True

    def test_old_returns_false(self):
        e = {"validated_at": time.time() - _CACHE_REVALIDATION_INTERVAL - 10}
        assert _cache_entry_recently_validated(e) is False

    def test_missing_validated_at_returns_false(self):
        assert _cache_entry_recently_validated({}) is False
        assert _cache_entry_recently_validated({"data": {}}) is False

    def test_non_dict_returns_false(self):
        assert _cache_entry_recently_validated(None) is False
        assert _cache_entry_recently_validated("abc") is False


# =============================================================================
# chart_compute pure functions
# =============================================================================


class TestShapeTime:
    def test_dict_with_points_list_takes_last(self):
        # last_point.time 取最后一个 point 的 time
        shape = {"points": [{"time": 100}, {"time": 200}]}
        assert _shape_time(shape) == 200

    def test_dict_with_points_dict(self):
        shape = {"points": {"time": 50}}
        assert _shape_time(shape) == 50

    def test_non_dict_returns_none(self):
        assert _shape_time(None) is None
        assert _shape_time(42) is None
        assert _shape_time("abc") is None

    def test_empty_points(self):
        assert _shape_time({"points": []}) is None

    def test_no_points_field(self):
        assert _shape_time({"id": "no_points_here"}) is None


class TestMergeShapeLists:
    def test_both_empty(self):
        assert _merge_shape_lists([], []) == []
        assert _merge_shape_lists(None, None) == []

    def test_existing_empty_returns_new_copy(self):
        new = [{"points": [{"time": 100}]}]
        result = _merge_shape_lists([], new)
        assert result == new
        assert result is not new  # 应是新 list

    def test_new_empty_returns_existing_copy(self):
        existing = [{"points": [{"time": 100}]}]
        result = _merge_shape_lists(existing, [])
        assert result == existing

    def test_merge_dedupes_by_start_point_identity(self):
        """新实现按"起点 (time, price)"身份去重, new 覆盖 old。

        旧行为 (test_overlap_window_drops_existing): 用 new 末点 time 划区间, 区间内的
        existing 全 drop —— 注释明确指出该行为已废弃, 原因见 _merge_shape_lists
        docstring (中间段 end_time 落在区间内会被永久删除 → 线段不连续)。

        新行为: 起点 (time, price) 不冲突的 existing 全部保留, 起点冲突时 new 覆盖。
        本测试锁定新语义, 防止未来谁误回退到旧的区间切割。
        """
        existing = [
            {"id": "old_a", "points": [{"time": 50, "price": 10.0}, {"time": 60}]},
            {"id": "old_b", "points": [{"time": 150, "price": 12.0}, {"time": 160}]},
        ]
        new = [
            {"id": "old_b_v2", "points": [{"time": 150, "price": 12.0}, {"time": 200}]},  # 同起点, 覆盖 old_b
            {"id": "new_c", "points": [{"time": 100, "price": 11.0}, {"time": 130}]},
        ]
        merged = _merge_shape_lists(existing, new)
        ids = [s["id"] for s in merged]
        # old_a (起点 t=50, p=10.0) 起点不冲突 → 保留
        assert "old_a" in ids
        # old_b (起点 t=150, p=12.0) 被 old_b_v2 同起点身份覆盖 → drop
        assert "old_b" not in ids
        assert "old_b_v2" in ids
        # new_c (起点 t=100, p=11.0) 全新身份 → 加入
        assert "new_c" in ids

    def test_result_sorted_by_time(self):
        existing = [{"id": "a", "points": [{"time": 100}]}]
        new = [{"id": "b", "points": [{"time": 50}]}]
        merged = _merge_shape_lists(existing, new)
        times = [_shape_time(s) for s in merged]
        assert times == sorted(times)


class TestMergeChartData:
    def test_existing_empty_returns_new(self):
        new = {"t": [1, 2], "c": [10, 20]}
        assert _merge_chart_data({}, new) == new

    def test_new_empty_returns_existing(self):
        existing = {"t": [1, 2], "c": [10, 20]}
        assert _merge_chart_data(existing, {}) == existing

    def test_aligned_merge_overlap_uses_new(self):
        # 时间 overlap 处，new 值覆盖 existing
        existing = {"t": [1, 2], "c": [10, 20]}
        new = {"t": [2, 3], "c": [25, 30]}
        merged = _merge_chart_data(existing, new)
        assert merged["t"] == [1, 2, 3]
        # t=2: new=25 覆盖 existing=20
        assert merged["c"] == [10, 25, 30]

    def test_new_none_does_not_override_existing(self):
        # new 中的 None 不应该把 existing 的有效值刷掉
        existing = {"t": [1, 2], "c": [10, 20]}
        new = {"t": [1, 2], "c": [None, 25]}
        merged = _merge_chart_data(existing, new)
        # t=1: new=None → 保留 existing=10
        # t=2: new=25 覆盖 existing=20
        assert merged["c"] == [10, 25]

    def test_shapes_merged_via_shape_lists(self):
        # bis 是 shape 列表，走 _merge_shape_lists
        existing = {"t": [1, 2], "bis": [{"points": [{"time": 50}]}]}
        new = {"t": [2, 3], "bis": [{"points": [{"time": 200}]}]}
        merged = _merge_chart_data(existing, new)
        assert len(merged["bis"]) == 2  # outside-window + new


class TestSafeLockRegistry:
    def test_same_key_returns_same_lock(self):
        reg = _SafeLockRegistry()
        # 必须保留强引用，否则被 WeakValueDictionary GC
        l1 = reg.get("foo")
        l2 = reg.get("foo")
        assert l1 is l2

    def test_different_keys_different_locks(self):
        reg = _SafeLockRegistry()
        l1 = reg.get("foo")
        l2 = reg.get("bar")  # noqa: F841 — 保留引用避免 GC
        assert l1 is not l2

    def test_contains_after_get(self):
        reg = _SafeLockRegistry()
        l = reg.get("mykey")  # noqa: F841 — 保留引用
        assert "mykey" in reg

    def test_get_lock_is_usable(self):
        # 拿到的锁应该是个 RLock，可以正常 acquire/release
        reg = _SafeLockRegistry()
        lock = reg.get("usable_test")
        with lock:
            # re-entrant 也 OK（RLock）
            with lock:
                pass


# =============================================================================
# stock_list pure functions
# =============================================================================


class TestProcessStockList:
    def test_adds_lowercase_fields(self):
        result = _process_stock_list([{"code": "ABC123", "name": "测试名"}])
        assert result[0]["code_lower"] == "abc123"
        assert result[0]["name_lower"] == "测试名"

    def test_pinyin_initials_for_chinese_name(self):
        # 测=c, 试=s
        result = _process_stock_list([{"code": "X", "name": "测试"}])
        assert result[0]["pinyin_initials"] == "cs"

    def test_pinyin_initials_handles_alpha_name(self):
        # 全英文 name，pinyin lib 不应抛异常
        result = _process_stock_list([{"code": "X", "name": "IBM"}])
        assert isinstance(result[0]["pinyin_initials"], str)

    def test_preserves_original_extra_fields(self):
        input_stock = {"code": "X", "name": "A", "extra": "preserved", "type": "stock_cn"}
        result = _process_stock_list([input_stock])
        assert result[0]["extra"] == "preserved"
        assert result[0]["type"] == "stock_cn"
        # 不修改原对象
        assert "code_lower" not in input_stock

    def test_empty_input(self):
        assert _process_stock_list([]) == []


# =============================================================================
# file_db SafeUnpickler（C — pickle 防御加固）
# =============================================================================


class TestChartCacheSafeUnpickler:
    """验证 _ChartCacheSafeUnpickler 拒绝任意 class/function 引用，
    仅放原生数据通过。
    """

    def test_pure_dict_loads_fine(self):
        import io
        import pickle

        from chanlun.file_db import _ChartCacheSafeUnpickler

        # 模拟典型 chart cache entry：纯原生类型
        entry = {
            "data": {"t": [1, 2, 3], "c": [10.5, None, 20.0]},
            "min_time": 1,
            "max_time": 3,
            "validated_at": 1234567890.0,
            "is_full_snapshot": True,
        }
        buf = io.BytesIO(pickle.dumps(entry))
        loaded = _ChartCacheSafeUnpickler(buf).load()
        assert loaded == entry

    def test_rejects_class_reference(self):
        import io
        import pickle

        from chanlun.file_db import _ChartCacheSafeUnpickler

        # 一个有 class 引用的 pickle（datetime 是 builtin 模块的 class，触发 find_class）
        import datetime as dt

        buf = io.BytesIO(pickle.dumps(dt.datetime(2025, 1, 1)))
        with pytest.raises(pickle.UnpicklingError):
            _ChartCacheSafeUnpickler(buf).load()

    def test_rejects_malicious_reduce_payload(self):
        """模拟攻击者放进 chart cache pkl 的 RCE payload。"""
        import io
        import pickle

        from chanlun.file_db import _ChartCacheSafeUnpickler

        class Exploit:
            def __reduce__(self):
                # 经典 pickle RCE 模式：在反序列化时调用 os.system
                import os

                return (os.system, ("echo PWNED",))

        buf = io.BytesIO(pickle.dumps(Exploit()))
        with pytest.raises(pickle.UnpicklingError):
            _ChartCacheSafeUnpickler(buf).load()
