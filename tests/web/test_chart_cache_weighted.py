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
