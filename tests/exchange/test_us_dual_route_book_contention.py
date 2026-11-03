"""Routing metadata contention must not corrupt source admission or valid windows."""
from contextlib import contextmanager
import multiprocessing
from threading import Event
import time

import pandas as pd
import pytest

from chanlun.exchange.errors import SourcePreparationPending
from chanlun.exchange.quote_capacity import QuoteConnectionBusy, QuoteConnectionCancelled, quote_request_scope
from chanlun.exchange.us_dual_history import DualUSHistory, USDualSourcePending
from chanlun.persistence.file_lock import InterprocessLockTimeout, interprocess_file_lock
from tests.exchange.test_us_dual_history import END, Source, frame, router
from tests.exchange.test_screening_history_cache import source as history_source, append


def hold_route_lock(path, ready, release):
    with interprocess_file_lock(path, timeout_seconds=2):
        ready.set()
        release.wait(10)


@contextmanager
def locked_book(path):
    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    child = context.Process(target=hold_route_lock, args=(str(path), ready, release))
    child.start()
    try:
        assert ready.wait(5), 'fixture did not acquire the actual route lock'
        yield
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate();child.join(timeout=2)
        assert not child.is_alive()
        child.close()


def busy_record(*args, **kwargs):
    raise InterprocessLockTimeout('fixture routing book contention')


def test_real_book_read_contention_is_typed_pending_before_any_provider_call(tmp_path):
    route, native, lb = router(tmp_path)
    with locked_book(tmp_path / 'routes.lock'):
        started = time.monotonic()
        with pytest.raises(USDualSourcePending) as caught:
            route.klines('QQQ.US', '5m', end_date=END)
        assert isinstance(caught.value, SourcePreparationPending)
        assert caught.value.preparation['retry_after'] == 3
        assert caught.value.reasons == {'routing': {'code': 'route_book_busy', 'progress_token': None}}
        assert not native.calls and not lb.calls
        assert time.monotonic() - started < 2, 'routing wait was silently extended'
    assert route.klines('QQQ.US', '5m', end_date=END).attrs['price_basis_provider'] == 'longbridge'
    assert len(lb.calls) == 1 and not native.calls


def test_verified_window_is_returned_unchanged_when_only_success_record_is_busy(tmp_path, monkeypatch):
    route, native, lb = router(tmp_path)
    original = route._read
    produced = []
    def read(*args, **kwargs):
        value = original(*args, **kwargs);produced.append(value);return value
    monkeypatch.setattr(route, '_read', read)
    monkeypatch.setattr(route, '_record', busy_record)
    result = route.klines('QQQ.US', '5m', end_date=END)
    assert result is produced[0]
    assert result.attrs['price_basis_provider'] == 'longbridge'
    assert len(lb.calls) == 1 and not native.calls


def test_busy_failed_source_record_does_not_start_an_extra_provider_attempt(tmp_path, monkeypatch):
    route, native, lb = router(tmp_path, error=QuoteConnectionBusy('capacity'))
    with monkeypatch.context() as patch:
        patch.setattr(route, '_record', busy_record)
        with pytest.raises(USDualSourcePending) as caught:
            route.klines('QQQ.US', '5m', end_date=END)
        assert caught.value.reasons['routing']['code'] == 'route_book_busy'
    assert len(lb.calls) == 1 and not native.calls
    lb.error = None
    assert route.klines('QQQ.US', '5m', end_date=END).attrs['price_basis_provider'] == 'longbridge'
    assert len(lb.calls) == 2 and not native.calls


def test_failed_promotion_keeps_actual_provider_pinned_for_peer_reads(tmp_path, monkeypatch):
    route, native, lb = router(tmp_path, error=QuoteConnectionBusy('capacity'))
    original_record = route._record
    def record(*args, **kwargs):
        if kwargs.get('error') is None: return busy_record()
        return original_record(*args, **kwargs)
    monkeypatch.setattr(route, '_record', record)
    main = route.klines('QQQ.US', '5m', end_date=END)
    assert main.attrs['price_basis_provider'] == 'usmart'
    assert route._selected('QQQ.US') == 'longbridge', 'failed metadata writes do not pretend to promote a route'
    peer = route.klines('QQQ.US', '5m', end_date=END,
                       args={'_history_provider': main.attrs['price_basis_provider']})
    assert peer.attrs['price_basis_provider'] == 'usmart' and peer.volume.eq(200).all()
    assert len(lb.calls) == 1 and len(native.calls) == 2


@pytest.mark.parametrize('where', ['before_read', 'busy_read', 'successful_record', 'failed_record'])
def test_cancellation_wins_over_route_metadata_wait(tmp_path, monkeypatch, where):
    route, native, lb = router(tmp_path, error=QuoteConnectionBusy('capacity') if where == 'failed_record' else None)
    cancel = Event()
    if where == 'before_read':
        cancel.set()
        monkeypatch.setattr(route, '_book', lambda *a: pytest.fail('cancelled request touched the book'))
    elif where == 'busy_read':
        def selected(*args): cancel.set();return busy_record()
        monkeypatch.setattr(route, '_selected', selected)
    else:
        def record(*args, **kwargs): cancel.set();return busy_record()
        monkeypatch.setattr(route, '_record', record)
    with quote_request_scope(cancel), pytest.raises(QuoteConnectionCancelled):
        route.klines('QQQ.US', '5m', end_date=END)
    assert not native.calls
    assert len(lb.calls) == (0 if where in {'before_read', 'busy_read'} else 1)


def test_contention_cannot_promote_an_invalid_window(tmp_path, monkeypatch):
    route, native, lb = router(tmp_path)
    invalid = frame('longbridge', label='start');invalid.loc[0, 'low'] = 30
    lb.klines = lambda *a, **k: invalid
    monkeypatch.setattr(route, '_record', busy_record)
    with pytest.raises(USDualSourcePending): route.klines('QQQ.US', '5m', end_date=END)
    assert not native.calls


def test_successful_provider_cache_is_reused_while_route_book_remains_locked(tmp_path):
    from chanlun.exchange.screening_history_cache import load_screening_history
    from chanlun.persistence.file_db import FileCacheDB
    storage = object.__new__(FileCacheDB);storage.klines_path = tmp_path / 'source-cache'
    downloads = []
    class CachedLongbridge(Source):
        def klines(self, code, frequency, *, start_date=None, end_date=None, args=None):
            end = pd.Timestamp(end_date)
            def download(overlap):
                downloads.append(overlap)
                return frame('longbridge', label='start')
            return load_screening_history(code=code, frequency=frequency, end=end,
                observed=end + pd.Timedelta(seconds=5), start=pd.Timestamp('2026-10-27 19:50Z'),
                loader=download, storage=storage)
    native = Source('usmart');lb = CachedLongbridge('longbridge', label='start')
    route = DualUSHistory(native, lambda: lb, root=tmp_path / 'routes')
    with locked_book(route.root / 'routes.lock'):
        query = {'_history_provider': 'longbridge'}
        first = route.klines('QQQ.US', '5m', end_date=END, args=query)
        cache_file = next(storage.klines_path.rglob('*.parquet'));before = cache_file.read_bytes()
        second = route.klines('QQQ.US', '5m', end_date=END, args=query)
        pd.testing.assert_frame_equal(first, second)
        assert cache_file.read_bytes() == before
    assert downloads == [None] and not native.calls


def test_route_wait_keeps_verified_prefix_without_same_call_full_refresh(tmp_path, monkeypatch, history_source):
    storage, candles, end, calls, read = history_source
    read();saved = next(storage.klines_path.rglob('*.parquet'));before = saved.read_bytes()
    append(candles, end)
    route, native, lb = router(tmp_path / 'routes')
    monkeypatch.setattr(route, '_selected', busy_record)
    attempts = []
    def unavailable(overlap):
        attempts.append(overlap)
        return route.klines('QQQ.US', '1m', end_date=(end + pd.Timedelta(minutes=1)).isoformat())
    with pytest.raises(USDualSourcePending): read(end + pd.Timedelta(minutes=1), loader=unavailable)
    assert len(attempts) == 1 and attempts[0] is not None
    assert saved.read_bytes() == before
    assert not lb.calls and not native.calls
