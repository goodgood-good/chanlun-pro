"""Parquet cleanup and publication must agree on the file being removed."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pandas as pd
import pytest

from chanlun.file_db_mixins import kline_cache
from chanlun.persistence.file_db import FileCacheDB
from chanlun.persistence.file_lock import interprocess_file_lock


def frame(price):
    result = pd.DataFrame({"date": pd.date_range("2026-09-11T19:57:00Z", periods=3, freq="min"),
                           "open": [price] * 3, "high": [price] * 3, "low": [price] * 3,
                           "close": [price] * 3, "volume": [100] * 3})
    result.attrs.update(price_basis_provider="longbridge", price_basis_adjustment="forward")
    return result


@pytest.fixture
def aged(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = tmp_path
    assert storage.save_klines_parquet("us", "CACHE.US", "5m", frame(10.))
    path = storage._kline_parquet_path("us", "CACHE.US", "5m")
    stamp = time.time() - 16 * 24 * 60 * 60
    os.utime(path, (stamp, stamp))
    return storage, path


def test_refreshed_parquet_survives_a_previous_age_check(aged, monkeypatch):
    storage, path = aged
    original = Path.stat
    replaced = []

    def stat(target, *args, **kwargs):
        value = original(target, *args, **kwargs)
        if target == path and not replaced:
            replaced.append(True)
            assert storage.save_klines_parquet("us", "CACHE.US", "5m", frame(20.))
        return value

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        assert storage.clear_tdx_old_klines("us") is True
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(20.))


def test_cleanup_blocks_same_file_publication_but_not_another_symbol(aged, monkeypatch):
    storage, path = aged
    deleting, release = threading.Event(), threading.Event()
    original = Path.unlink

    def unlink(target, *args, **kwargs):
        if target == path:
            deleting.set()
            assert release.wait(5)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(storage.clear_tdx_old_klines, "us")
        try:
            assert deleting.wait(2)
            replacement = pool.submit(storage.save_klines_parquet, "us", "CACHE.US", "5m", frame(30.))
            assert storage.save_klines_parquet("us", "OTHER.US", "5m", frame(40.))
            assert not replacement.done(), "a new parquet file was published inside an old cleanup"
        finally:
            release.set()
        assert cleanup.result(timeout=2)
        assert replacement.result(timeout=2)
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(30.))


def test_cleanup_skips_an_active_parquet_publication(aged, monkeypatch):
    storage, path = aged
    publishing, release = threading.Event(), threading.Event()
    original = kline_cache.os.replace

    def replace(source, target):
        if Path(target) == path:
            publishing.set()
            assert release.wait(5)
        return original(source, target)

    monkeypatch.setattr(kline_cache.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(storage.save_klines_parquet, "us", "CACHE.US", "5m", frame(50.))
        try:
            assert publishing.wait(2)
            cleanup = pool.submit(storage.clear_tdx_old_klines, "us")
            assert cleanup.result(timeout=0.5)
            assert path.is_file(), "cleanup removed the file while publication was active"
        finally:
            release.set()
        assert writing.result(timeout=2)
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(50.))


def test_publication_can_run_inside_the_existing_history_transaction(aged):
    storage, path = aged
    with interprocess_file_lock(path.with_suffix(".parquet.lock")):
        assert storage.save_klines_parquet("us", "CACHE.US", "5m", frame(60.))
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(60.))


def test_cleanup_preserves_recent_caches_and_auxiliary_files(aged):
    storage, path = aged
    assert storage.save_klines_parquet("us", "RECENT.US", "5m", frame(70.))
    auxiliary = path.with_suffix(".parquet.lock")
    auxiliary.write_bytes(b"lock")
    assert storage.clear_tdx_old_klines("us")
    assert not path.exists()
    assert auxiliary.read_bytes() == b"lock"
    assert storage.load_klines_parquet("us", "RECENT.US", "5m") is not None


def test_other_process_reports_contention_before_publishing_during_cleanup(aged, monkeypatch):
    storage, path = aged
    deleting, release = threading.Event(), threading.Event()
    original = Path.unlink

    def unlink(target, *args, **kwargs):
        if target == path:
            deleting.set()
            assert release.wait(15)
        return original(target, *args, **kwargs)

    program = """
from pathlib import Path
import sys
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / 'src')]
import tests.conftest
import pandas as pd
from chanlun.persistence.file_db import FileCacheDB
from chanlun.persistence import file_lock
original = file_lock._try_lock
reported = False
def acquire(handle):
    global reported
    result = original(handle)
    if not result and not reported:
        reported = True
        print('blocked', flush=True)
    return result
file_lock._try_lock = acquire
storage = FileCacheDB.__new__(FileCacheDB)
storage.klines_path = Path(sys.argv[2])
value = pd.DataFrame({'date': pd.date_range('2026-09-11T19:57:00Z', periods=3, freq='min'),
                      'open': [80.] * 3, 'high': [80.] * 3, 'low': [80.] * 3,
                      'close': [80.] * 3, 'volume': [100] * 3})
value.attrs.update(price_basis_provider='longbridge', price_basis_adjustment='forward')
assert storage.save_klines_parquet('us', 'CACHE.US', '5m', value)
print('saved', flush=True)
"""
    monkeypatch.setattr(Path, "unlink", unlink)
    process = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        cleanup = pool.submit(storage.clear_tdx_old_klines, "us")
        try:
            assert deleting.wait(2)
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", program,
                 str(Path(__file__).resolve().parents[2]), str(storage.klines_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            phase = pool.submit(process.stdout.readline).result(timeout=10).strip()
            assert phase == "blocked", "the other process bypassed cleanup: " + phase
            assert process.poll() is None
        finally:
            release.set()
            if process is not None:
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0, stderr
        assert cleanup.result(timeout=2)
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(80.))


def test_large_frame_serialization_does_not_hold_the_publication_lock(aged, monkeypatch):
    storage, path = aged
    serializing, release = threading.Event(), threading.Event()
    original = pd.DataFrame.to_parquet

    def serialize(value, *args, **kwargs):
        if value.close.iloc[0] == 90.:
            serializing.set()
            assert release.wait(5)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", serialize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(storage.save_klines_parquet, "us", "CACHE.US", "5m", frame(90.))
        try:
            assert serializing.wait(2)
            assert storage.clear_tdx_old_klines("us")
            assert not path.exists(), "serialization unnecessarily blocked an expiry cleanup"
        finally:
            release.set()
        assert writing.result(timeout=2)
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(90.))


def test_failed_replacement_releases_lock_and_removes_its_temp_file(aged, monkeypatch):
    storage, path = aged
    original = kline_cache.os.replace

    def failed(source, target):
        if Path(target) == path:
            raise OSError("controlled parquet publication failure")
        return original(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(kline_cache.os, "replace", failed)
        assert storage.save_klines_parquet("us", "CACHE.US", "5m", frame(100.)) is False
    assert not list(path.parent.glob("*.tmp-*"))
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "CACHE.US", "5m"), frame(10.))
    assert storage.clear_tdx_old_klines("us")
    assert not path.exists()
    assert storage.save_klines_parquet("us", "CACHE.US", "5m", frame(110.))
