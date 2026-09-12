import asyncio
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
import threading
import time
from types import SimpleNamespace

import pytest

from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.exchange import exchange_cq
from chanlun.exchange.longbridge_quote import QuoteContextBridge


def test_lazy_connection_overlaps_requests_and_preserves_arguments_and_results():
    created = []
    entered = threading.Event()

    class Context:
        def __init__(self):
            self.arrivals = 0
            self.both = asyncio.Event()

        async def history_candlesticks_by_offset(self, symbol, *, count):
            self.arrivals += 1
            entered.set()
            if self.arrivals == 2:
                self.both.set()
            await self.both.wait()
            return symbol, count

    def create(config):
        created.append(config)
        return Context()

    bridge = QuoteContextBridge("config", factory=create)
    try:
        assert not created  # Constructing the provider does not fetch/prewarm.
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(bridge.history_candlesticks_by_offset, "AAPL.US", count=1000)
            assert entered.wait(1)
            second = pool.submit(bridge.history_candlesticks_by_offset, "TSLA.US", count=500)
            assert first.result(timeout=2) == ("AAPL.US", 1000)
            assert second.result(timeout=2) == ("TSLA.US", 500)
        assert created == ["config"]
        assert not bridge.connecting
    finally:
        bridge.close()
    assert not bridge._thread.is_alive()


def test_request_deadline_cancels_task_and_preserves_connection_for_next_call():
    cancelled = threading.Event()

    class Context:
        async def quote(self, symbols):
            if symbols == ["slow"]:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return symbols

    bridge = QuoteContextBridge(None, factory=lambda _: Context(), request_timeout=0.05)
    try:
        with pytest.raises(TimeoutError):
            bridge.quote(["slow"])
        assert cancelled.wait(1)
        assert bridge.quote(["AAPL.US"]) == ["AAPL.US"]
    finally:
        bridge.close()


def test_close_cancels_waiters_and_rejects_new_calls():
    entered = threading.Event()

    class Context:
        async def quote(self, symbols):
            entered.set()
            await asyncio.Event().wait()

    bridge = QuoteContextBridge(None, factory=lambda _: Context())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge.quote, ["AAPL.US"])
        assert entered.wait(1)
        bridge.close()
        bridge.close()
        with pytest.raises(CancelledError):
            future.result(timeout=1)
    assert not bridge._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.quote(["TSLA.US"])


def test_sdk_error_is_propagated_and_initial_connection_grace_is_bounded():
    error = ValueError("quote authority unavailable")

    class Context:
        async def quote(self, symbols):
            raise error

    bridge = QuoteContextBridge(None, factory=lambda _: Context())
    try:
        with pytest.raises(ValueError) as caught:
            bridge.quote(["AAPL.US"])
        assert caught.value is error
        assert bridge.connecting
        bridge._created_at = time.monotonic() - 21
        assert not bridge.connecting
    finally:
        bridge.close()


@pytest.mark.parametrize("connecting", [True, False])
def test_short_exchange_deadline_preserves_only_a_pending_first_handshake(connecting):
    cq = object.__new__(ExchangeChangQiao.__wrapped__)
    cq._quote_context = SimpleNamespace(connecting=connecting)
    rebuilt = []
    cq._rebuild_quote_ctx = lambda: rebuilt.append(True)
    released = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        cq._quote_call_executor = pool
        try:
            with pytest.raises(TimeoutError):
                cq._quote_call(lambda: released.wait(1), timeout=0.02)
            assert rebuilt == ([] if connecting else [True])
        finally:
            released.set()


def test_history_waits_for_one_handshake_then_restores_normal_deadline(monkeypatch):
    class Context:
        async def history_candlesticks_by_offset(self, **kwargs):
            return kwargs

    tracker = SimpleNamespace(is_exhausted=lambda limit: False, add_symbol=lambda symbol: None)
    monkeypatch.setattr(exchange_cq.LbQuotaTracker, "instance", lambda: tracker)
    monkeypatch.setattr(exchange_cq.config, "LB_QUOTA_MONTHLY_LIMIT", 100)
    bridge = QuoteContextBridge(None, factory=lambda _: Context())
    cq = object.__new__(ExchangeChangQiao.__wrapped__)
    cq._quote_ctx = lambda: bridge
    cq.rate_limiter = SimpleNamespace(wait=lambda: None)
    timeouts = []

    def quote_call(fn, timeout):
        timeouts.append(timeout)
        return fn()

    cq._quote_call = quote_call
    arguments = dict(symbol="AAPL.US", period="1m", adjust_type="forward", count=1000,
                     time_cursor="closed-cutoff", trade_sessions="intraday")
    try:
        first = cq._fetch_candlesticks_api(**arguments)
        second = cq._fetch_candlesticks_api(**arguments)
        assert first == second == dict(symbol="AAPL.US", period="1m", adjust_type="forward",
                                       forward=False, count=1000, time="closed-cutoff", trade_sessions="intraday")
        assert 5 < timeouts[0] <= 20
        assert timeouts[1] == 5
    finally:
        bridge.close()
