"""Connection admission failures end history work; they never mean no bars."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
import pickle
import threading
import time
import weakref

import pytest
import pytz

from chanlun.exchange import exchange_cq as cq
from chanlun.exchange import quote_capacity
from chanlun.exchange.errors import LongbridgeConnectionLimitError


CQ = cq.ExchangeChangQiao.__wrapped__
TZ = pytz.timezone("Asia/Shanghai")


def limit_error():
    error = cq.OpenApiException.__new__(cq.OpenApiException)
    error.code = None
    error.message = "connections limitation is hit, limit = 10, online = 10"
    return error


def test_connection_limit_is_not_a_retryable_rate_or_transport_failure():
    assert not cq.is_retryable_exception(limit_error())
    assert not cq.is_retryable_exception(LongbridgeConnectionLimitError())
    rate = limit_error()
    rate.code = 429002
    rate.message = "rate limit of 1-second interval has been reached"
    assert cq.is_retryable_exception(rate)
    assert cq.is_retryable_exception(TimeoutError())


def test_public_error_roundtrips_without_sdk_or_private_message():
    result = pickle.loads(pickle.dumps(LongbridgeConnectionLimitError(17)))
    assert result.code == "longbridge_connection_limit" and result.retry_after == 17
    assert str(result) == result.public_message
    assert "online" not in str(result)


def test_quote_limit_cools_down_without_rebuilding_or_extending_deadline():
    exchange = object.__new__(CQ)
    calls = []
    exchange._rebuild_quote_ctx = lambda: pytest.fail("an account cap is not a hung context")

    def rejected():
        calls.append("request")
        raise limit_error()

    with ThreadPoolExecutor(max_workers=1) as executor:
        exchange._quote_call_executor = executor
        with pytest.raises(LongbridgeConnectionLimitError):
            exchange._quote_call(rejected)
        deadline = exchange._connection_limit_until
        for _ in range(3):
            with pytest.raises(LongbridgeConnectionLimitError):
                exchange._quote_call(rejected)
        assert calls == ["request"]
        assert exchange._connection_limit_until == deadline
        exchange._connection_limit_until = time.monotonic() - 1
        assert exchange._quote_call(lambda: "recovered") == "recovered"


def test_history_page_does_not_retry_connection_limit(monkeypatch):
    exchange = object.__new__(CQ)
    exchange.rate_limiter = SimpleNamespace(wait=lambda: None)
    exchange._quote_ctx = lambda: object()
    calls = []

    def rejected(fn, **kwargs):
        calls.append("request")
        raise limit_error()

    exchange._quote_call = rejected
    monkeypatch.setattr(cq.LbQuotaTracker, "instance", lambda: SimpleNamespace(is_exhausted=lambda n: False))
    monkeypatch.setattr(cq.config, "LB_QUOTA_MONTHLY_LIMIT", 100)
    with pytest.raises(cq.OpenApiException):
        exchange._fetch_candlesticks_api("QQQ.US", cq.Period.Min_1, cq.AdjustType.ForwardAdjust,
                                         1000, TZ.localize(datetime(2026, 10, 9)), cq.TradeSessions.Intraday)
    assert calls == ["request"]


def test_segment_propagates_explicit_admission_error_instead_of_failed_status():
    exchange = object.__new__(CQ)

    def rejected(**kwargs):
        raise limit_error()

    exchange._fetch_candlesticks_api = rejected
    with pytest.raises(LongbridgeConnectionLimitError):
        exchange._fetch_segment_data("TSLA.US", cq.Period.Min_30, cq.AdjustType.ForwardAdjust,
                                     TZ.localize(datetime(2026, 10, 9)), TZ.localize(datetime(2026, 9, 1)))


def test_history_stops_pending_pages_and_skips_serial_repair(monkeypatch):
    exchange = object.__new__(CQ)
    futures = []

    def submit(*args):
        future = Future()
        if not futures:
            future.set_exception(LongbridgeConnectionLimitError())
        futures.append(future)
        return future

    exchange.executor = SimpleNamespace(submit=submit)
    exchange._to_lb_symbol = lambda code: code
    exchange._market_of_code = lambda code: "us"
    exchange._fetch_segment_data = lambda *args, **kwargs: pytest.fail("must not serially repair admission failures")
    with pytest.raises(LongbridgeConnectionLimitError):
        exchange._fetch_klines("TSLA.US", "30m", start_date="2025-01-01", end_date="2026-10-09")
    assert len(futures) > 1 and all(f.cancelled() for f in futures[1:])
    count = len(futures)
    with pytest.raises(LongbridgeConnectionLimitError):
        exchange._fetch_klines("QQQ.US", "1m", start_date="2026-10-01", end_date="2026-10-09")
    assert len(futures) == count


def test_worker_release_disposes_only_initialized_context_before_slot(monkeypatch):
    exchange = object.__new__(CQ)
    order = []
    exchange._quote_context_lock = threading.Lock()
    exchange._quote_context = SimpleNamespace(close=lambda: order.append("closed"))
    monkeypatch.setattr(cq, "_quote_instances", weakref.WeakSet((exchange,)))
    monkeypatch.setattr(quote_capacity, "is_worker_quote_process", lambda: True)
    monkeypatch.setattr(quote_capacity, "release_worker_quote_slot", lambda: order.append("released"))
    monkeypatch.setattr(cq, "QuoteContextBridge", lambda config: pytest.fail("cleanup cannot open a context"))
    cq.release_worker_quote_context()
    assert exchange._quote_context is None and order == ["closed", "released"]
    exchange._rebuild_quote_ctx()
    assert exchange._quote_context is None
    cq.release_worker_quote_context()
    assert order == ["closed", "released", "released"]


def test_worker_context_creation_failure_returns_its_slot(monkeypatch):
    exchange = object.__new__(CQ)
    exchange._quote_context_lock = threading.Lock()
    exchange._quote_context = None
    exchange.config = object()
    order = []
    monkeypatch.setattr(quote_capacity, "acquire_worker_quote_slot", lambda: order.append("acquired"))
    monkeypatch.setattr(quote_capacity, "release_worker_quote_slot", lambda: order.append("released"))

    def fail(config):
        raise RuntimeError("creation failed")

    monkeypatch.setattr(cq, "QuoteContextBridge", fail)
    with pytest.raises(RuntimeError, match="creation failed"):
        exchange._quote_ctx()
    assert order == ["acquired", "released"] and exchange._quote_context is None
