from cl_app.services import chart_cache


def test_chart_cache_weight_is_byte_based_and_conservative() -> None:
    small = {"data": {"t": [1], "c": [1.0]}}
    large = {"data": {"payload": "x" * 300_000}}

    assert chart_cache._chart_cache_entry_weight(large) > (
        chart_cache._chart_cache_entry_weight(small)
    )
    metrics = chart_cache.chart_cache_metrics()
    assert metrics["max_bytes"] >= 16 * 1024 * 1024
    assert metrics["max_entries"] > 0
    assert metrics["estimated_bytes"] <= metrics["max_bytes"]
    assert chart_cache.chart_data_cache.ttl >= 12 * 60 * 60


def test_weighted_cache_counts_capacity_evictions() -> None:
    cache = chart_cache._WeightedTTLCache(
        maxsize=10,
        ttl=60,
        getsizeof=lambda value: value,
    )

    cache["first"] = 6
    cache["second"] = 6

    assert "first" not in cache
    assert cache["second"] == 6
    assert cache.capacity_evictions == 1


def test_prepared_weight_avoids_reserializing_inside_cache_assignment(monkeypatch) -> None:
    key = "prepared-weight-test"
    entry = {"data": {"payload": "x" * 1_000}}
    prepared = chart_cache._CHART_CACHE_MIN_ENTRY_WEIGHT + 1

    def unexpected_json_dump(*_args, **_kwargs):
        raise AssertionError("entry weight was recomputed while assigning the cache")

    monkeypatch.setattr(chart_cache.json, "dumps", unexpected_json_dump)
    with chart_cache.cache_lock:
        try:
            assert chart_cache._put_chart_cache_ram(
                key,
                entry,
                prepared_weight=prepared,
            ) is True
            assert chart_cache.chart_data_cache[key] is entry
        finally:
            chart_cache.chart_data_cache.pop(key, None)


def test_persisted_weight_avoids_reserializing_restored_entry(monkeypatch) -> None:
    key = "persisted-weight-test"
    entry = chart_cache._build_chart_cache_entry(
        {"t": [1], "c": [1.0]},
        is_full_snapshot=True,
    )
    persisted_weight = chart_cache._CHART_CACHE_MIN_ENTRY_WEIGHT + 17
    entry[chart_cache._RESIDENT_WEIGHT_FIELD] = persisted_weight
    monkeypatch.setattr(chart_cache.fdb, "get_chart_cache", lambda _key: entry)

    def unexpected_json_dump(*_args, **_kwargs):
        raise AssertionError("persisted entry weight must be reused")

    monkeypatch.setattr(chart_cache.json, "dumps", unexpected_json_dump)
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
    try:
        restored = chart_cache._get_chart_cache_entry(key)
        assert restored is entry
        assert chart_cache.chart_data_cache.getsizeof(restored) == persisted_weight
    finally:
        with chart_cache.cache_lock:
            chart_cache.chart_data_cache.pop(key, None)


def test_validation_mark_never_reads_disk_for_a_nonresident_entry(monkeypatch) -> None:
    key = "validation-mark-ram-only-test"
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)

    def unexpected_disk_read(_key):
        raise AssertionError("a scalar validation mark must not unpickle disk data")

    monkeypatch.setattr(chart_cache.fdb, "get_chart_cache", unexpected_disk_read)
    assert chart_cache._mark_chart_cache_validated(key) is False


def test_validation_mark_atomically_refreshes_only_the_resident_record() -> None:
    key = "validation-mark-resident-test"
    entry = chart_cache._build_chart_cache_entry(
        {"t": [1], "c": [1.0]},
        is_full_snapshot=True,
        validated_at=1.0,
    )
    prepared_weight = chart_cache._chart_cache_entry_weight(entry)
    entry[chart_cache._RESIDENT_WEIGHT_FIELD] = prepared_weight
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
        chart_cache._put_chart_cache_ram(
            key,
            entry,
            prepared_weight=prepared_weight,
        )
    try:
        assert chart_cache._mark_chart_cache_validated(key) is True
        with chart_cache.cache_lock:
            refreshed = chart_cache.chart_data_cache[key]
        assert refreshed is not entry
        assert refreshed["data"] is entry["data"]
        assert refreshed["validated_at"] > 1.0
    finally:
        with chart_cache.cache_lock:
            chart_cache.chart_data_cache.pop(key, None)
