"""Age cleanup must not delete a concurrently published chart."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from chanlun.persistence.file_db import FileCacheDB
from chanlun.persistence import file_db as file_db_module


@pytest.fixture
def aged(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.chart_cache_path = tmp_path
    storage.chart_cache_max_age_seconds = 60
    path = storage._chart_cache_path_for("aged")
    storage.set_chart_cache("aged", {"version": 1}).result(timeout=2)
    stamp = time.time() - 120
    os.utime(path, (stamp, stamp))
    return storage, path


def test_refresh_after_first_age_check_is_retained(aged, monkeypatch):
    storage, path = aged
    original = Path.stat
    replaced = []

    def stat(target, *args, **kwargs):
        value = original(target, *args, **kwargs)
        if target == path and not replaced:
            replaced.append(True)
            storage.set_chart_cache("aged", {"version": 2}).result(timeout=2)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        storage.clear_old_chart_cache()
    assert replaced == [True]
    assert storage.get_chart_cache("aged") == {"version": 2}


def test_publication_waits_for_same_file_cleanup_but_other_keys_can_write(aged, monkeypatch):
    storage, path = aged
    deleting, release = threading.Event(), threading.Event()
    original = Path.unlink

    def unlink(target, *args, **kwargs):
        if target == path:
            deleting.set()
            assert release.wait(5)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with ThreadPoolExecutor(max_workers=1) as pool:
        cleanup = pool.submit(storage.clear_old_chart_cache)
        try:
            assert deleting.wait(2)
            replacement = storage.set_chart_cache("aged", {"version": 3})
            storage.set_chart_cache("other", {"version": 10}).result(timeout=2)
            assert not replacement.done(), "publication overtook cleanup of the same file"
            assert storage.get_chart_cache("other") == {"version": 10}
        finally:
            release.set()
        cleanup.result(timeout=2)
    replacement.result(timeout=2)
    assert storage.get_chart_cache("aged") == {"version": 3}


def test_cleanup_skips_a_file_with_an_active_writer(aged, monkeypatch):
    storage, path = aged
    entered, release = threading.Event(), threading.Event()
    original = storage._atomic_write_pickle_blocking

    def write(target, obj, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(target, obj, **kwargs)

    monkeypatch.setattr(storage, "_atomic_write_pickle_blocking", write)
    future = storage.set_chart_cache("aged", {"version": 4})
    try:
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            cleanup = pool.submit(storage.clear_old_chart_cache)
            cleanup.result(timeout=0.5)
        assert path.is_file(), "cleanup removed a cache while its writer was active"
    finally:
        release.set()
    future.result(timeout=2)
    assert storage.get_chart_cache("aged") == {"version": 4}


def test_cleanup_removes_only_expired_data_files(aged):
    storage, path = aged
    storage.set_chart_cache("fresh", {"version": 5}).result(timeout=2)
    marker = path.with_suffix(".pkl.invalid")
    marker.write_bytes(b"durable invalidation")
    storage.clear_old_chart_cache()
    assert not path.exists()
    assert marker.read_bytes() == b"durable invalidation"
    assert storage.get_chart_cache("fresh") == {"version": 5}


def test_another_process_cannot_publish_inside_a_cleanup_delete(aged, monkeypatch):
    storage, path = aged
    deleting, release = threading.Event(), threading.Event()
    original = Path.unlink
    ready = path.parent / "child-ready"
    done = path.parent / "child-done"

    def unlink(target, *args, **kwargs):
        if target == path:
            deleting.set()
            assert release.wait(10)
        return original(target, *args, **kwargs)

    program = """
from pathlib import Path
import sys
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / 'src')]
import tests.conftest
from chanlun.persistence.file_db import FileCacheDB
storage = FileCacheDB.__new__(FileCacheDB)
storage.chart_cache_path = Path(sys.argv[2])
(storage.chart_cache_path / 'child-ready').write_text('ready')
storage.set_chart_cache('aged', {'version': 8}).result(timeout=10)
(storage.chart_cache_path / 'child-done').write_text('done')
"""
    monkeypatch.setattr(Path, "unlink", unlink)
    process = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        cleanup = pool.submit(storage.clear_old_chart_cache)
        try:
            assert deleting.wait(2)
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", program,
                 str(Path(__file__).resolve().parents[2]), str(path.parent)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.is_file()
            deadline = time.monotonic() + 0.15
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not done.exists(), "another process published before cleanup released the file"
            assert process.poll() is None
        finally:
            release.set()
            if process is not None:
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0, stderr
        cleanup.result(timeout=2)
    assert storage.get_chart_cache("aged") == {"version": 8}


def test_publication_failure_releases_the_cleanup_lock(aged, monkeypatch):
    storage, path = aged

    def fail(*args, **kwargs):
        raise OSError("controlled publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_atomic_write_pickle_blocking", fail)
        with pytest.raises(OSError, match="publication failure"):
            storage.set_chart_cache("aged", {"version": 9}).result(timeout=2)
    storage.clear_old_chart_cache()
    assert not path.exists()
    storage.set_chart_cache("aged", {"version": 10}).result(timeout=2)
    assert storage.get_chart_cache("aged") == {"version": 10}


def test_relative_publication_lock_stays_bound_to_submission_directory(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    submitted = []

    class Deferred:
        def submit(self, function, *args):
            submitted.append((function, args))

    storage = FileCacheDB.__new__(FileCacheDB)
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITE_EXECUTOR", Deferred())
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITES_CLOSED", False)
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITES_ACCEPTING", True)
    monkeypatch.chdir(first)
    future = storage._atomic_write_pickle(Path("cache.pkl"), {"version": 11},
                                         publication_lock=Path("cache.pkl.lock"))
    monkeypatch.chdir(second)
    for function, args in submitted:
        function(*args)
    future.result(timeout=2)
    assert (first / "cache.pkl").is_file() and (first / "cache.pkl.lock").is_file()
    assert not (second / "cache.pkl").exists() and not (second / "cache.pkl.lock").exists()
