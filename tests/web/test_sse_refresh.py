"""SSE chart refresh change detection and bounded cache behavior."""

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
