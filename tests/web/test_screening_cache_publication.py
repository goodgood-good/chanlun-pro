"""Late progressive cache writes must not replace newer result ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.web.test_screening_results_cache import cached_run as cached_run, _running, seed_run


def finishing(manager, monkeypatch):
    _running(manager)
    manager.results()
    _running(manager, status="completed")
    entered, release = threading.Event(), threading.Event()
    original = manager._save_results_cache

    def save(directory, *args):
        if directory.name == "a" * 32 and threading.current_thread().name.startswith("old-cache"):
            entered.set()
            assert release.wait(5)
        return original(directory, *args)

    monkeypatch.setattr(manager, "_save_results_cache", save)
    return entered, release


@pytest.mark.parametrize("new_finished", [False, True])
def test_old_progressive_save_cannot_replace_new_full_reader(cached_run, monkeypatch, new_finished):
    manager = cached_run
    entered, release = finishing(manager, monkeypatch)
    entered_read, release_read = threading.Event(), threading.Event()
    original = manager._read_results

    def read(directory, *args, **kwargs):
        if directory.name == "b" * 32:
            entered_read.set()
            assert release_read.wait(5)
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-cache") as pool:
        old = pool.submit(manager.results)
        future = None
        try:
            assert entered.wait(2)
            seed_run(manager, "b" * 32, "SH.600002")
            manager.results(background=True)
            assert entered_read.wait(2)
            future = manager._results_future
            key = manager._results_key
            if new_finished:
                release_read.set()
                future.result(timeout=2)
            release.set()
            old.result(timeout=2)
            assert manager._results_future is future, "old cache completion replaced the new reader"
            assert manager._results_key == key
        finally:
            release.set()
            release_read.set()
            if future is not None:
                future.result(timeout=2)
            old.result(timeout=2)
    assert manager.results()["errors"][0]["code"] == "SH.600002"


def test_late_cache_completion_does_not_spawn_a_duplicate_full_read(cached_run, monkeypatch):
    manager = cached_run
    entered, release = finishing(manager, monkeypatch)
    entered_read, release_read = threading.Event(), threading.Event()
    original = manager._read_results
    reads = []

    def read(directory, *args, **kwargs):
        if directory.name == "b" * 32:
            reads.append(directory.name)
            entered_read.set()
            assert release_read.wait(5)
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(manager, "_read_results", read)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-cache") as pool:
        old = pool.submit(manager.results)
        futures = []
        try:
            assert entered.wait(2)
            seed_run(manager, "b" * 32, "SH.600002")
            manager.results(background=True)
            assert entered_read.wait(2)
            futures.append(manager._results_future)
            release.set()
            old.result(timeout=2)
            manager.results(background=True)
            futures.append(manager._results_future)
            assert futures[0] is futures[1], "another poll launched a second reader for the same run"
        finally:
            release.set()
            release_read.set()
            for future in futures:
                future.result(timeout=2)
            old.result(timeout=2)
    assert reads == ["b" * 32]


def test_old_save_does_not_replace_new_progressive_persistence_marker(cached_run, monkeypatch):
    manager = cached_run
    entered, release = finishing(manager, monkeypatch)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-cache") as pool:
        old = pool.submit(manager.results)
        try:
            assert entered.wait(2)
            seed_run(manager, "b" * 32, "SH.600002")
            _running(manager)
            manager.results()
            _running(manager, status="completed")
            manager.results()
            key = manager._results_key
            marker = manager._progressive_results.persisted_key
            release.set()
            old.result(timeout=2)
            assert manager._progressive_results.persisted_key == marker
            assert manager._results_key == key
        finally:
            release.set()
            old.result(timeout=2)
    assert manager.results()["errors"][0]["code"] == "SH.600002"


def test_old_save_cannot_mark_a_changed_snapshot_of_the_same_run_as_persisted(cached_run, monkeypatch):
    manager = cached_run
    entered, release = finishing(manager, monkeypatch)
    path = manager._directory() / "results.jsonl"
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-cache") as pool:
        old = pool.submit(manager.results)
        try:
            assert entered.wait(2)
            path.write_bytes(path.read_bytes().replace(b"SH.600000", b"SZ.000001"))
            _running(manager)
            current = manager.results()
            assert current["errors"][0]["code"] == "SZ.000001"
            assert manager._progressive_results.persisted_key is None
            release.set()
            old.result(timeout=2)
            assert manager._progressive_results.persisted_key is None
        finally:
            release.set()
            old.result(timeout=2)
    _running(manager, status="completed")
    assert manager.results()["errors"][0]["code"] == "SZ.000001"


def test_empty_completed_progressive_result_is_reused_without_repeated_cache_writes(cached_run, monkeypatch):
    manager = cached_run
    _running(manager, completed=0)
    manager.results()
    _running(manager, completed=0, status="completed")
    saved = []
    original = manager._save_results_cache

    def save(*args):
        saved.append(True)
        return original(*args)

    monkeypatch.setattr(manager, "_save_results_cache", save)
    first = manager.results()
    assert first["processed_symbols"] == [] and not first["results_loading"]
    for _ in range(3):
        assert manager.results()["processed_symbols"] == []
    assert saved == [True]
