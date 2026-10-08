"""Short sharing conflicts should not discard a fully serialized K-line update."""

from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import os
from pathlib import Path
import pickle
import threading

import pandas as pd
import pytest

from chanlun.file_db_mixins import kline_cache
from chanlun.persistence import file_db
from chanlun.persistence.file_db import FileCacheDB


def frame(price):
    result = pd.DataFrame({"date": pd.date_range("2026-09-11T19:57:00Z", periods=3, freq="min"),
                           "open": [price] * 3, "high": [price] * 3, "low": [price] * 3,
                           "close": [price] * 3, "volume": [100] * 3})
    result.attrs.update(price_basis_provider="longbridge", price_basis_adjustment="forward")
    return result


@pytest.fixture
def saved(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = tmp_path
    assert storage.save_klines_parquet("us", "RETRY.US", "5m", frame(10.))
    return storage, storage._kline_parquet_path("us", "RETRY.US", "5m")


@pytest.mark.skipif(os.name != "nt", reason="real Windows read-handle sharing semantics")
def test_real_read_handle_conflict_recovers_after_reader_closes(saved, monkeypatch):
    storage, path = saved
    conflict = threading.Event()
    original = kline_cache.os.replace
    errors = []

    def replace(source, target):
        try:
            return original(source, target)
        except PermissionError as exc:
            errors.append(exc.winerror)
            conflict.set()
            raise

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    reader = path.open("rb")
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(storage.save_klines_parquet, "us", "RETRY.US", "5m", frame(20.))
        try:
            assert conflict.wait(2), "the test did not observe a real sharing conflict"
        finally:
            reader.close()
        assert writing.result(timeout=2), errors
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "RETRY.US", "5m"), frame(20.))


def test_temporary_replacement_failure_reuses_the_serialized_file(saved, monkeypatch):
    storage, path = saved
    attempts, serializations, delays = [], [], []
    original_replace = kline_cache.os.replace
    original_serialize = pd.DataFrame.to_parquet

    def replace(source, target):
        attempts.append(Path(source))
        if len(attempts) < 3:
            raise PermissionError("controlled temporary sharing conflict")
        return original_replace(source, target)

    def serialize(value, *args, **kwargs):
        serializations.append(True)
        return original_serialize(value, *args, **kwargs)

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    monkeypatch.setattr(pd.DataFrame, "to_parquet", serialize)
    monkeypatch.setattr(file_db.time, "sleep", delays.append)
    assert storage.save_klines_parquet("us", "RETRY.US", "5m", frame(30.))
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert serializations == [True]
    assert len(delays) == 2 and 0 < sum(delays) <= 0.5
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "RETRY.US", "5m"), frame(30.))


def test_persistent_permission_failure_is_bounded_and_preserves_old_cache(saved, monkeypatch):
    storage, path = saved
    before = path.read_bytes()
    attempts, delays = [], []

    def replace(source, target):
        attempts.append(Path(source))
        raise PermissionError("controlled persistent sharing conflict")

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    monkeypatch.setattr(file_db.time, "sleep", delays.append)
    assert storage.save_klines_parquet("us", "RETRY.US", "5m", frame(40.)) is False
    assert 1 < len(attempts) <= 5 and 0 < sum(delays) <= 0.5
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp-*"))


def test_non_permission_io_failure_is_not_retried(saved, monkeypatch):
    storage, path = saved
    before = path.read_bytes()
    attempts, delays = [], []

    def replace(source, target):
        attempts.append(True)
        raise OSError(errno.ENOSPC, "controlled disk-full failure")

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    monkeypatch.setattr(file_db.time, "sleep", delays.append)
    assert storage.save_klines_parquet("us", "RETRY.US", "5m", frame(50.)) is False
    assert attempts == [True] and delays == []
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp-*"))


def test_serialization_permission_failure_is_not_retried(saved, monkeypatch):
    storage, path = saved
    before = path.read_bytes()
    serializations, delays = [], []

    def denied(*args, **kwargs):
        serializations.append(True)
        raise PermissionError("controlled temporary-file creation failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", denied)
    monkeypatch.setattr(file_db.time, "sleep", delays.append)
    assert storage.save_klines_parquet("us", "RETRY.US", "5m", frame(60.)) is False
    assert serializations == [True] and delays == []
    assert path.read_bytes() == before


def test_pickle_writer_preserves_existing_bounded_retry_behavior(saved, monkeypatch):
    storage, path = saved
    target = path.parent / "state.pkl"
    attempts, delays = [], []
    original = file_db.os.replace

    def replace(source, destination):
        attempts.append(True)
        if len(attempts) < 3:
            raise PermissionError("controlled pickle sharing conflict")
        return original(source, destination)

    monkeypatch.setattr(file_db.os, "replace", replace)
    monkeypatch.setattr(file_db.time, "sleep", delays.append)
    storage._atomic_write_pickle_blocking(target, {"version": 2})
    assert len(attempts) == 3 and len(delays) == 2 and sum(delays) <= 0.5
    assert pickle.loads(target.read_bytes()) == {"version": 2}


def test_other_symbol_is_not_blocked_by_a_retry_delay(saved, monkeypatch):
    storage, path = saved
    retrying, release = threading.Event(), threading.Event()
    original = kline_cache.os.replace
    failed = []

    def replace(source, target):
        if Path(target) == path and not failed:
            failed.append(True)
            raise PermissionError("controlled short sharing conflict")
        return original(source, target)

    def pause(delay):
        retrying.set()
        assert release.wait(5)

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    monkeypatch.setattr(file_db.time, "sleep", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(storage.save_klines_parquet, "us", "RETRY.US", "5m", frame(70.))
        try:
            assert retrying.wait(2)
            assert storage.save_klines_parquet("us", "OTHER.US", "5m", frame(80.))
            assert not writing.done()
        finally:
            release.set()
        assert writing.result(timeout=2)
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "RETRY.US", "5m"), frame(70.))


@pytest.mark.skipif(os.name != "nt", reason="real Windows read-handle sharing semantics")
def test_forced_history_refresh_waits_for_recovered_invalidation_before_fetch(saved, monkeypatch):
    from chanlun.exchange.closed_history_cache import load_closed_us_history, _NAMESPACE
    storage, _ = saved
    observed = pd.Timestamp("2026-09-12T12:00:00+08:00")
    arguments = dict(code="RETRY.US", frequency="1m", canonical=True,
                     observed=observed, end=observed, storage=storage)
    load_closed_us_history(**arguments, loader=lambda: frame(10.))
    code = hashlib.sha256(b"RETRY.US").hexdigest()[:32]
    path = storage._kline_parquet_path(_NAMESPACE, code, "1m_canonical")
    conflict = threading.Event()
    original = kline_cache.os.replace
    fetched = []

    def replace(source, target):
        try:
            return original(source, target)
        except PermissionError:
            conflict.set()
            raise

    def loader():
        invalidated = storage.load_klines_parquet(_NAMESPACE, code, "1m_canonical")
        assert invalidated is not None and invalidated.empty
        fetched.append(True)
        return frame(90.)

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    reader = path.open("rb")
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(load_closed_us_history, **arguments, force=True, loader=loader)
        try:
            assert conflict.wait(2)
            assert fetched == []
        finally:
            reader.close()
        pd.testing.assert_frame_equal(refresh.result(timeout=2), frame(90.))
    assert fetched == [True]
