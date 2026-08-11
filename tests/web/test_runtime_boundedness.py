import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cl_app.handlers import sse_stream
from cl_app.services import chart_revalidate, constants, stock_list


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_metadata_owner_returns_fallback_at_deadline_and_late_success_is_cached():
    entered = threading.Event()
    release = threading.Event()

    def builder(_key, _market):
        entered.set()
        release.wait(timeout=2)
        return ["ready"]

    metadata = constants._LazyMarketDict(
        builder,
        markets=[("a", object())],
        fallback_factory=list,
        retry_seconds=30,
        load_timeout_seconds=0.05,
    )

    started_at = time.monotonic()
    assert metadata["a"] == []
    elapsed = time.monotonic() - started_at

    assert entered.is_set()
    assert elapsed < 0.25
    assert metadata.status("a") == {"state": "failed", "ready": False}

    release.set()
    assert _wait_until(
        lambda: metadata.status("a") == {"state": "loaded", "ready": True}
    )
    assert metadata["a"] == ["ready"]
    metadata.shutdown(timeout=0.1)


def test_symbol_preload_is_singleton_stoppable_and_marks_hung_attempt(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocking_refresh(_exchange, skip_if_disk_warm=False):
        assert skip_if_disk_warm is True
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(stock_list, "PRELOAD_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(stock_list, "PRELOAD_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(stock_list, "PRELOAD_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(stock_list, "_warm_cache_from_disk", lambda: None)
    monkeypatch.setattr(stock_list, "_preload_single_exchange", blocking_refresh)

    first = stock_list.start_symbol_preload_thread()
    second = stock_list.start_symbol_preload_thread()
    try:
        assert first is second
        assert entered.wait(timeout=1)
        assert _wait_until(
            lambda: stock_list.get_symbol_readiness("a")["status"] == "degraded"
        )
        assert "timeout" in stock_list.get_symbol_readiness("a")["last_error"].lower()

        first.stop()
        first.join(timeout=0.5)
        assert first.is_alive() is False
    finally:
        release.set()
        stock_list.shutdown_symbol_preload(timeout=0.5)


def test_repeated_symbol_cache_misses_share_one_refresh_watchdog(monkeypatch):
    attempt = {
        "done": threading.Event(),
        "thread": None,
        "started_at": time.monotonic(),
        "timed_out": False,
    }
    watchdogs = []

    class _Thread:
        def __init__(self, **kwargs):
            watchdogs.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(stock_list, "_start_preload_attempt", lambda _market: attempt)
    monkeypatch.setattr(stock_list.threading, "Thread", _Thread)

    for _ in range(20):
        stock_list._trigger_async_refresh("a")

    assert len(watchdogs) == 1


def test_revalidation_timeout_is_observable_and_active_work_is_bounded(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocking(*_args):
        entered.set()
        release.wait(timeout=2)

    monkeypatch.setattr(chart_revalidate, "_do_revalidate", blocking)
    monkeypatch.setattr(chart_revalidate, "_REVALIDATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(chart_revalidate, "_MAX_ACTIVE_REVALIDATIONS", 1)
    assert chart_revalidate.shutdown_revalidation(wait=True) is True
    chart_revalidate.start_revalidation_runtime()

    assert chart_revalidate.submit_revalidation("a", "X", "5m", {}, "K1") is True
    assert entered.wait(timeout=1)
    assert _wait_until(
        lambda: chart_revalidate.revalidation_status()["timed_out"] == 1
    )
    assert chart_revalidate.submit_revalidation("a", "Y", "5m", {}, "K2") is False
    assert chart_revalidate.revalidation_status()["active"] == 1

    release.set()
    assert _wait_until(lambda: chart_revalidate.revalidation_status()["active"] == 0)


def test_sse_recompute_timeout_does_not_block_ioloop_or_overqueue(monkeypatch):
    async def scenario():
        entered = threading.Event()
        release = threading.Event()

        def blocking(*_args):
            entered.set()
            release.wait(timeout=2)
            return {"t": [1]}

        monkeypatch.setattr(sse_stream, "recompute_chart_data", blocking)
        monkeypatch.setattr(sse_stream, "_RECOMPUTE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(sse_stream, "_RECOMPUTE_MAX_PENDING", 1)
        assert sse_stream.shutdown_sse_runtime() is True
        sse_stream.start_sse_runtime()
        pool = ThreadPoolExecutor(max_workers=1)
        ctx = {}
        args = ("a", "X", "1m", {}, "K")
        try:
            started = time.monotonic()
            completed, value = await sse_stream._run_recompute_bounded(pool, ctx, args)
            elapsed = time.monotonic() - started
            assert completed is False
            assert value is None
            assert entered.is_set()
            assert elapsed < 0.25
            assert sse_stream.sse_runtime_status() == {
                "inflight": 1,
                "timed_out": 1,
                "closed": False,
            }

            other_ctx = {}
            completed, value = await sse_stream._run_recompute_bounded(
                pool, other_ctx, ("a", "Y", "1m", {}, "K2")
            )
            assert completed is False
            assert value is None
            assert sse_stream.sse_runtime_status()["inflight"] == 1
        finally:
            release.set()
            assert await asyncio.to_thread(
                _wait_until, lambda: sse_stream.sse_runtime_status()["inflight"] == 0
            )
            pool.shutdown(wait=True)

    asyncio.run(scenario())
