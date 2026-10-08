"""A cache rejected for its price basis must stay unusable after I/O failures."""

from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
import json
import subprocess
import sys
import threading

import pytest

from chanlun.persistence import file_db
from chanlun.persistence.file_db import FileCacheDB
from cl_app.services import chart_cache


@pytest.fixture
def stored(monkeypatch):
    file_db.start_pickle_writes()
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    chart_cache.start_chart_cache_runtime()
    key = "denied-invalidation"
    entry = chart_cache._build_chart_cache_entry({"t": [1], "c": [10.]}, is_full_snapshot=True)
    chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
    yield key, entry
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    file_db.shutdown_pickle_writes(wait=True)
    file_db.start_pickle_writes()
    chart_cache.start_chart_cache_runtime()
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
        getattr(chart_cache, "_failed_chart_cache_invalidations", {}).pop(key, None)


def deny_cache_delete(monkeypatch, key):
    original = Path.unlink
    target = chart_cache.fdb._chart_cache_path_for(key)

    def unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError("controlled occupied cache")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return target


def test_failed_delete_is_not_reloaded_and_survives_a_new_storage_instance(stored, monkeypatch):
    key, entry = stored
    target = deny_cache_delete(monkeypatch, key)
    chart_cache._delete_chart_cache_entry(key)
    assert target.is_file(), "the test must actually preserve the occupied old file"
    assert chart_cache._get_chart_cache_entry(key) is None
    other = FileCacheDB.__new__(FileCacheDB)
    other.chart_cache_path = chart_cache.fdb.chart_cache_path
    assert other.get_chart_cache(key) is None


def test_successful_replacement_clears_invalidation_and_restores_cache(stored, monkeypatch):
    key, entry = stored
    deny_cache_delete(monkeypatch, key)
    chart_cache._delete_chart_cache_entry(key)
    assert chart_cache.fdb.get_chart_cache(key) is None
    replacement = {**entry, "data": {"t": [1], "c": [20.]}}
    chart_cache.fdb.set_chart_cache(key, replacement).result(timeout=2)
    assert chart_cache.fdb.get_chart_cache(key) == replacement


def test_failed_replacement_keeps_old_file_unusable(stored, monkeypatch):
    key, entry = stored
    deny_cache_delete(monkeypatch, key)
    chart_cache._delete_chart_cache_entry(key)

    def failure(*args, **kwargs):
        raise OSError("controlled replacement failure")

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", failure)
    pending = chart_cache.fdb.set_chart_cache(key, {**entry, "data": {"t": [1], "c": [30.]}})
    with pytest.raises(OSError, match="replacement failure"):
        pending.result(timeout=2)
    assert chart_cache.fdb.get_chart_cache(key) is None


def test_failure_to_persist_invalidation_blocks_disk_reads_in_the_running_service(stored, monkeypatch):
    key, entry = stored
    original = chart_cache.fdb._atomic_write_pickle_blocking
    denied = deny_cache_delete(monkeypatch, key)

    def failure(path, obj, **kwargs):
        if path != denied:
            raise OSError("controlled marker storage failure")
        return original(path, obj, **kwargs)

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", failure)
    chart_cache._delete_chart_cache_entry(key)
    assert chart_cache._get_chart_cache_entry(key) is None
    # A newly calculated RAM value is useful, but a failed disk write cannot
    # release the old file from quarantine after the RAM entry is evicted.
    monkeypatch.setattr(chart_cache, "_persist_chart_cache_async", lambda *args: None)
    chart_cache._set_chart_cache_entry(key, {"t": [1], "c": [40.]}, True)
    assert chart_cache._get_chart_cache_entry(key)["data"]["c"] == [40.]
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
    assert chart_cache._get_chart_cache_entry(key) is None


def test_marker_created_during_an_old_disk_read_rejects_its_result(stored, monkeypatch):
    key, entry = stored
    entered, release = threading.Event(), threading.Event()
    original = chart_cache.fdb._chart_cache_path_for(key).open
    from chanlun.file_db_mixins import chart_data

    class LateReader:
        def __enter__(self):
            self.stream = original("rb")
            return self.stream

        def __exit__(self, *error):
            self.stream.close()
            entered.set()
            assert release.wait(5)
            return False

    monkeypatch.setattr(chart_data, "open", lambda *_args: LateReader(), raising=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(chart_cache.fdb.get_chart_cache, key)
        try:
            assert entered.wait(2)
            chart_cache.fdb.delete_chart_cache(key)
        finally:
            release.set()
        assert reading.result(timeout=2) is None


def test_older_writer_cannot_clear_a_newer_invalidation(stored, monkeypatch):
    key, entry = stored
    chart_cache.fdb.delete_chart_cache(key)
    entered, release = threading.Event(), threading.Event()
    original = chart_cache.fdb._atomic_write_pickle_blocking
    target = chart_cache.fdb._chart_cache_path_for(key)

    def write(path, obj, **kwargs):
        if path == target:
            entered.set()
            assert release.wait(5)
        return original(path, obj, **kwargs)

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    pending = chart_cache.fdb.set_chart_cache(key, entry)
    try:
        assert entered.wait(2)
        chart_cache.fdb.delete_chart_cache(key)
    finally:
        release.set()
    pending.result(timeout=2)
    assert target.is_file()
    assert chart_cache.fdb.get_chart_cache(key) is None


def test_canceled_replacement_leaves_invalidation_intact(stored, monkeypatch):
    key, entry = stored
    chart_cache.fdb.delete_chart_cache(key)
    pending = Future()
    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle", lambda *args, **kwargs: pending)
    completion = chart_cache.fdb.set_chart_cache(key, entry)
    assert pending.cancel()
    assert completion.cancelled()
    assert chart_cache.fdb.get_chart_cache(key) is None


def test_marker_removal_failure_stays_unavailable_until_a_successful_write(stored, monkeypatch):
    key, entry = stored
    chart_cache.fdb.delete_chart_cache(key)
    marker = chart_cache.fdb._chart_cache_path_for(key).with_suffix(".pkl.invalid")
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("controlled marker removal failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError, match="marker removal failure"):
            chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
        assert chart_cache.fdb.get_chart_cache(key) is None
    chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
    assert chart_cache.fdb.get_chart_cache(key) == entry


def test_service_recovers_quarantined_disk_after_verified_replacement(stored, monkeypatch):
    key, entry = stored
    with monkeypatch.context() as patch:
        patch.setattr(chart_cache.fdb, "delete_chart_cache", lambda *_: (_ for _ in ()).throw(OSError("marker unavailable")))
        chart_cache._delete_chart_cache_entry(key)
    assert chart_cache._get_chart_cache_entry(key) is None
    chart_cache._set_chart_cache_entry(key, {"t": [1], "c": [50.]}, True)
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        future.result(timeout=2)
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
        assert key not in chart_cache._failed_chart_cache_invalidations
    assert chart_cache._get_chart_cache_entry(key)["data"]["c"] == [50.]


def test_persisted_rejection_is_seen_by_a_separate_process(stored, monkeypatch):
    key, entry = stored
    deny_cache_delete(monkeypatch, key)
    chart_cache.fdb.delete_chart_cache(key)
    root = Path(__file__).resolve().parents[2]
    program = """
from pathlib import Path
import json, sys
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'src')]
import tests.conftest
from chanlun.persistence.file_db import FileCacheDB
storage = FileCacheDB.__new__(FileCacheDB)
storage.chart_cache_path = Path(sys.argv[2])
print(json.dumps({'rejected': storage.get_chart_cache(sys.argv[3]) is None}))
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
                             str(root), str(chart_cache.fdb.chart_cache_path), key],
                            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    assert json.loads(result.stdout)["rejected"] is True


def test_replacement_sync_failure_cannot_remove_persisted_rejection(stored, monkeypatch):
    key, entry = stored
    chart_cache.fdb.delete_chart_cache(key)

    def fail_sync(*_args):
        raise OSError("controlled flush failure")

    monkeypatch.setattr(file_db.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="flush failure"):
        chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
    assert chart_cache.fdb.get_chart_cache(key) is None
