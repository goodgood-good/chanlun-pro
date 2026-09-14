"""Bounded chart-only market-state reads, including the real history cache path."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from cl_app.services import chart_compute
from cl_app.services.chart_market_state import ChartMarketStateCache


class _Clock:
    value = 0.0

    def __call__(self):
        return self.value


class _FakeEx:
    def __init__(self, trading):
        self.trading = trading
        self.calls = 0

    def now_trading(self, market):
        self.calls += 1
        return self.trading


@pytest.fixture
def market_cache(monkeypatch):
    cache = ChartMarketStateCache()
    monkeypatch.setattr(chart_compute, "_chart_market_state_cache", cache)
    yield cache
    cache.shutdown()


@pytest.mark.parametrize("state", [True, False])
def test_market_now_trading_caches_fast_boolean_within_ttl(monkeypatch, market_cache, state):
    fake = _FakeEx(state)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: fake)
    assert chart_compute.market_now_trading("a", now=1000.0) is state
    assert chart_compute.market_now_trading("a", now=1005.0) is state
    assert fake.calls == 1


def test_market_now_trading_refreshes_after_ttl(monkeypatch, market_cache):
    fake = _FakeEx(False)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: fake)
    assert chart_compute.market_now_trading("a", now=2000.0) is False
    fake.trading = True
    assert chart_compute.market_now_trading("a", now=2999.0) is True
    assert fake.calls == 2


@pytest.mark.parametrize("unknown", [None, 0, 1, "false", "true", object()])
def test_unknown_or_non_boolean_cannot_claim_closed_market(monkeypatch, market_cache, unknown):
    fake = _FakeEx(unknown)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: fake)
    assert chart_compute.market_now_trading("us") is True
    assert chart_compute.market_now_trading("us") is True
    assert fake.calls == 1


@pytest.mark.parametrize("phase", ["exchange", "state"])
def test_market_now_trading_error_defaults_trading(monkeypatch, market_cache, phase):
    def boom(*_args):
        raise RuntimeError("provider unavailable")

    if phase == "exchange":
        monkeypatch.setattr(chart_compute, "get_exchange", boom)
    else:
        monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: object())
        monkeypatch.setattr(chart_compute, "exchange_market_now_trading", boom)
    assert chart_compute.market_now_trading("a") is True


def test_invalid_market_never_starts_provider(monkeypatch, market_cache):
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: pytest.fail("invalid market"))
    assert chart_compute.market_now_trading("not-a-market") is True
    assert market_cache._executor is None


def test_market_state_helper_participates_in_chart_producer_identity():
    from cl_app.services.chart_producer_identity import chart_producer_files

    assert any(path.name == "chart_market_state.py" for path in chart_producer_files())


def test_four_concurrent_readers_finish_while_one_provider_remains_blocked():
    cache = ChartMarketStateCache()
    barrier = threading.Barrier(4)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider():
        calls.append(1)
        entered.set()
        release.wait(5)
        return False

    def read():
        barrier.wait(timeout=2)
        started = time.monotonic()
        return cache.read("us", provider), time.monotonic() - started

    try:
        with ThreadPoolExecutor(max_workers=4) as readers:
            futures = [readers.submit(read) for _ in range(4)]
            assert entered.wait(1)
            results = [future.result(timeout=1) for future in futures]
        assert all(value is True and elapsed < 0.5 for value, elapsed in results)
        assert not release.is_set()
        assert calls == [1]
        for now in (100.0, 200.0, 500.0):
            assert cache.read("us", provider, now=now) is True
        assert calls == [1]
    finally:
        release.set()
        cache.shutdown()


def test_wait_budget_never_exceeds_fifty_ms(monkeypatch):
    cache = ChartMarketStateCache()
    entered, release = threading.Event(), threading.Event()
    budgets = []

    def provider():
        entered.set()
        release.wait(5)

    try:
        assert cache.read("us", provider) is True
        assert entered.wait(1)
        flight = cache._flights["us"]
        original_wait = flight.finished.wait

        def measured_wait(timeout):
            budgets.append(timeout)
            return original_wait(timeout)

        monkeypatch.setattr(flight.finished, "wait", measured_wait)
        assert cache.read("us", provider) is True
        assert len(budgets) == 1 and 0 <= budgets[0] <= 0.05
    finally:
        release.set()
        cache.shutdown()


def test_ttl_begins_when_the_provider_completes_not_when_it_was_queued():
    clock = _Clock()
    cache = ChartMarketStateCache(clock=clock)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider():
        calls.append(1)
        entered.set()
        release.wait(5)
        return False

    try:
        assert cache.read("us", provider) is True
        assert entered.wait(1)
        flight = cache._flights["us"]
        clock.value = 25.0
        release.set()
        assert flight.finished.wait(1)
        assert cache._cache["us"] == (False, 25.0)
        clock.value = 54.99
        assert cache.read("us", provider) is False
        assert calls == [1]
        clock.value = 55.0
        assert cache.read("us", lambda: True) is True
    finally:
        release.set()
        cache.shutdown()


def test_expired_false_is_never_reused_while_refresh_is_pending():
    clock = _Clock()
    cache = ChartMarketStateCache(clock=clock)
    entered, release = threading.Event(), threading.Event()

    def blocked_refresh():
        entered.set()
        release.wait(5)
        return False

    try:
        assert cache.read("us", lambda: False) is False
        clock.value = 30.0
        assert cache.read("us", blocked_refresh) is True
        assert entered.wait(1)
        assert cache.read("us", blocked_refresh) is True
        assert "us" not in cache._cache
    finally:
        release.set()
        cache.shutdown()


def test_pool_and_pending_calls_stay_bounded_across_many_markets():
    cache = ChartMarketStateCache(max_workers=2, max_pending=2)
    release = threading.Event()
    calls = []

    def blocked():
        calls.append(threading.current_thread().name)
        release.wait(5)
        return False

    try:
        with ThreadPoolExecutor(max_workers=12) as readers:
            futures = [readers.submit(cache.read, str(index), blocked) for index in range(12)]
            assert all(future.result(timeout=1) is True for future in futures)
        assert len(calls) <= 2
        assert len(cache._flights) <= 4
        assert len(cache._executor._threads) == 2
        assert all(thread.daemon for thread in cache._executor._threads)
        assert cache._executor._queue.qsize() <= 2
    finally:
        cache.shutdown()
        release.set()


def test_shutdown_does_not_wait_for_provider_or_publish_its_late_false():
    cache = ChartMarketStateCache()
    release = threading.Event()

    def blocked():
        release.wait(5)
        return False

    try:
        assert cache.read("us", blocked) is True
        flight = cache._flights["us"]
        cache.shutdown()
        assert cache.read("us", blocked) is True
        release.set()
        assert flight.finished.wait(1)
        assert cache._cache == {}
    finally:
        release.set()
        cache.shutdown()


def test_four_real_history_cache_hits_do_not_wait_for_market_state(monkeypatch, market_cache):
    from cl_app import create_app
    from cl_app.blueprints import tv
    from cl_app.services import chart_cache

    app = create_app(test_config={
        "TESTING": True, "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
    })
    entered, release = threading.Event(), threading.Event()
    provider_calls = []

    class BlockedExchange:
        def now_trading(self, market):
            provider_calls.append(market)
            entered.set()
            release.wait(5)
            return False

    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: BlockedExchange())
    monkeypatch.setattr(tv, "market_now_trading", chart_compute.market_now_trading)
    monkeypatch.setattr(tv, "query_cl_chart_config", lambda *_args: {})
    monkeypatch.setattr(tv, "_mark_user_request", lambda *_args: None)
    monkeypatch.setattr(tv, "submit_revalidation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tv.market_frequencys, "cached_snapshot", lambda *_args: {})
    monkeypatch.setattr(tv, "fetch_klines_and_compute_cl_data", lambda *_args, **_kwargs: pytest.fail("must hit snapshot"))
    chart_data = {"t": [1700000000, 1700000300], **{
        key: [1.0, 1.0] for key in ("o", "h", "l", "c", "v")
    }}
    entry = chart_cache._build_chart_cache_entry(
        chart_data, is_full_snapshot=True, validated_at=time.time(),
    )
    monkeypatch.setattr(tv, "_get_chart_cache_entry", lambda _key: entry)
    barrier = threading.Barrier(4)

    def request(resolution):
        with app.test_client() as client:
            barrier.wait(timeout=2)
            response = client.get(
                "/tv/history", query_string={
                    "symbol": "us:AAPL.US", "resolution": resolution,
                    "firstDataRequest": "true", "from": 0, "to": int(time.time()) + 60,
                },
            )
            return response.status_code, response.get_json()

    try:
        with ThreadPoolExecutor(max_workers=4) as readers:
            futures = [readers.submit(request, resolution) for resolution in ("1", "5", "30", "1D")]
            assert entered.wait(1)
            results = [future.result(timeout=1) for future in futures]
        assert not release.is_set()
        assert provider_calls == ["us"]
        assert all(status == 200 and data["s"] == "ok" and len(data["t"]) == 2 for status, data in results)
    finally:
        release.set()
