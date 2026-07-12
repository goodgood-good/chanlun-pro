from cl_app.services import chart_cache


def test_negative_cache_has_a_strict_capacity(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(chart_cache.time, "time", lambda: now[0])
    monkeypatch.setattr(chart_cache, "_NEGATIVE_CACHE_MAX_SIZE", 3)
    with chart_cache._negative_cache_lock:
        chart_cache._negative_cache.clear()

    for index in range(4):
        now[0] += 1
        chart_cache._mark_negative_cache(f"k{index}", ttl=300)

    with chart_cache._negative_cache_lock:
        assert len(chart_cache._negative_cache) == 3
        assert "k0" not in chart_cache._negative_cache
        assert set(chart_cache._negative_cache) == {"k1", "k2", "k3"}