from concurrent.futures import Future,CancelledError
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.exchange import exchange_cq as cq, quote_capacity as capacity
from tests.exchange.test_cq_connection_limit import CQ, TZ
from tests.exchange.test_screening_history_cache import source as source, append
from chanlun.exchange import screening_history_cache as cache


@pytest.mark.parametrize('error',[capacity.QuoteConnectionCancelled('owning run cancelled'),cq.LongbridgeHistoryBusy()])
def test_cancelled_or_paced_history_is_not_retryable_and_not_a_provider_error(monkeypatch,error):
    assert not cq.is_retryable_exception(error)
    assert cq.is_retryable_exception(TimeoutError())
    exchange = object.__new__(CQ)
    exchange._fetch_candlesticks_api = lambda **kwargs: (_ for _ in ()).throw(error)
    monkeypatch.setattr(cq.LogUtil, 'error', lambda *args: pytest.fail('cancellation is not a source error'))
    with pytest.raises(type(error)):
        exchange._fetch_segment_data('QQQ.US', cq.Period.Min_5, cq.AdjustType.ForwardAdjust,
            TZ.localize(datetime(2026,10,27)), TZ.localize(datetime(2026,10,1)))


@pytest.mark.parametrize('error',[capacity.QuoteConnectionCancelled('run cancelled'),cq.LongbridgeHistoryBusy()])
def test_cancelled_or_paced_segment_cancels_pending_pages_without_serial_repair(error):
    exchange = object.__new__(CQ)
    futures = []
    def submit(*args):
        future = Future()
        if not futures:
            future.set_exception(error)
        futures.append(future)
        return future
    exchange.executor = SimpleNamespace(submit=submit)
    exchange._to_lb_symbol = lambda code: code
    exchange._market_of_code = lambda code: 'us'
    exchange._fetch_segment_data = lambda *args: pytest.fail('cancelled work must not be repaired')
    with pytest.raises(type(error)):
        exchange._fetch_klines('QQQ.US', '30m', start_date='2025-01-01', end_date='2026-10-27')
    assert len(futures)>1 and all(f.cancelled() for f in futures[1:])


def test_executor_cancellation_cannot_be_reported_as_a_missing_history_segment(monkeypatch):
    exchange=object.__new__(CQ);futures=[]
    def submit(*args):
        future=Future()
        if not futures:future.set_exception(CancelledError())
        futures.append(future);return future
    exchange.executor=SimpleNamespace(submit=submit)
    exchange._to_lb_symbol=lambda code:code
    exchange._market_of_code=lambda code:'us'
    monkeypatch.setattr(cq.LogUtil,'error',lambda *_:pytest.fail('executor cancellation is not a source hole'))
    with pytest.raises(capacity.QuoteConnectionCancelled):
        exchange._fetch_klines('QQQ.US','30m',start_date='2025-01-01',end_date='2026-10-27')
    assert all(f.cancelled() for f in futures[1:])


@pytest.mark.parametrize('error',[capacity.QuoteConnectionCancelled('run cancelled'),cq.LongbridgeHistoryBusy()])
def test_cancelled_or_paced_tail_preserves_committed_prefix_and_next_attempt_can_recover(source,error):
    storage, frame, end, calls, read = source
    before = read()
    append(frame, end)
    def cancelled(overlap):
        assert overlap == before.date.iloc[-64]
        raise error
    with pytest.raises(type(error)):
        read(end+pd.Timedelta(minutes=1), loader=cancelled)
    recovered = read(end+pd.Timedelta(minutes=1))
    assert recovered.attrs[cache.META]['mode'] == 'tail'
    assert recovered.attrs[cache.META]['through'] == end.timestamp()+60
    assert len(recovered) == len(before)+1
