"""SSE chart refresh change detection and bounded cache behavior."""

import pytest

from cl_app.services.sse_refresh import decide_push


def test_decide_push_first_time():
    push, signature = decide_push(None, {"t": [1]})
    assert push is True
    assert isinstance(signature, str)


def test_decide_push_unchanged():
    chart_data = {"t": [1, 2], "bis": []}
    _, first = decide_push(None, chart_data)
    push, second = decide_push(first, chart_data)
    assert push is False
    assert second == first


def test_decide_push_changed():
    _, first = decide_push(None, {"t": [1]})
    push, second = decide_push(first, {"t": [1, 2]})
    assert push is True
    assert second != first


def test_recompute_skips_negatively_cached(monkeypatch):
    from cl_app.services import chart_cache, sse_refresh

    monkeypatch.setattr(chart_cache, "_is_negatively_cached", lambda _key: True)
    called = []
    monkeypatch.setattr(
        sse_refresh,
        "get_exchange",
        lambda *_args, **_kwargs: called.append(1),
    )

    result = sse_refresh.recompute_chart_data(
        "a", "SZ.000001", "5m", {}, "cache-key"
    )

    assert result is None
    assert called == []


@pytest.mark.parametrize("entry", [None, {"data": {"_initial_structure_build_id": "pending"}}])
def test_sse_cannot_start_a_second_build_before_initial_candles_complete(monkeypatch, entry):
    from cl_app.services import chart_cache, sse_refresh

    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry_ram_only", lambda _key: entry)
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda *_args: pytest.fail("must leave initial loading to history"))
    assert sse_refresh.recompute_chart_data("us", "TSLA.US", "1m", {}, "opened-chart") is None


def test_sse_refresh_resumes_after_initial_snapshot_is_complete(monkeypatch):
    from types import SimpleNamespace
    import pandas as pd
    from cl_app.services import chart_cache, sse_refresh, kline_recompute

    frame = pd.DataFrame({"date": [pd.Timestamp("2026-01-05", tz="UTC")]})
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry_ram_only", lambda _key: {"data": {"t": [1]}})
    monkeypatch.setattr(chart_cache, "_is_negatively_cached", lambda _key: False)
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda *_args: SimpleNamespace(klines=lambda *_args: frame))
    calls = []
    monkeypatch.setattr(kline_recompute, "prepend_klines_and_replace_cache", lambda *args: calls.append(args) or {"t": [2]})
    assert sse_refresh.recompute_chart_data("us", "TSLA.US", "1m", {}, "opened-chart") == {"t": [2]}
    assert len(calls) == 1


def test_sse_delivers_recent_full_history_without_starting_duplicate_work(monkeypatch):
    import time
    from cl_app.services import chart_cache, sse_refresh

    data = {"t": [1], "strict_structure_mode": "replace"}
    entry = {"data": data, "validated_at": time.time(), "is_full_snapshot": True}
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry_ram_only", lambda _key: entry)
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda *_args: pytest.fail("duplicate provider fetch"))
    assert sse_refresh.recompute_chart_data("us", "TSLA.US", "1m", {}, "opened-chart") is data
