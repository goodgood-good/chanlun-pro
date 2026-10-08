"""Metadata validation must not block publication or mutate replacement readers."""
from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest

from cl_app.services import screening_progress as progress
from tests.web.test_screening_results_cache import (
    cached_run as cached_run, _running, _append_error, seed_run,
)


def seeded(manager):
    _running(manager)
    assert manager.results()["results_through"] == 1
    return manager._progressive_results


def block_poll(monkeypatch, *, outcome=None):
    entered, release = threading.Event(), threading.Event()
    original = progress._unchanged

    def check(*args):
        if threading.current_thread().name.startswith("slow-poll") and not entered.is_set():
            entered.set()
            assert release.wait(5)
            if outcome is not None:
                return outcome
        return original(*args)

    monkeypatch.setattr(progress, "_unchanged", check)
    return entered, release


def test_slow_metadata_validation_does_not_hold_publication_lock(cached_run, monkeypatch):
    manager = cached_run
    reader = seeded(manager)
    entered, release = block_poll(monkeypatch)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-poll") as pool:
        polling = pool.submit(manager.results, background=True)
        try:
            assert entered.wait(2)
            acquired = reader.lock.acquire(timeout=0.2)
            if acquired:
                reader.lock.release()
            assert acquired, "metadata I/O held the result publication lock"
        finally:
            release.set()
        polling.result(timeout=2)


def test_completed_chunk_can_publish_while_another_poll_checks_metadata(cached_run, monkeypatch):
    manager = cached_run
    reader = seeded(manager)
    entered_read, release_read = threading.Event(), threading.Event()
    original = reader.read

    def read(*args, **kwargs):
        if kwargs.get("after_completed") == 1:
            entered_read.set()
            assert release_read.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "read", read)
    _append_error(manager, "SZ.000001")
    _running(manager, completed=2)
    manager.results(background=True)
    assert entered_read.wait(2)
    future = reader.future
    entered, release = block_poll(monkeypatch)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-poll") as pool:
        polling = pool.submit(manager.results, background=True)
        try:
            assert entered.wait(2)
            release_read.set()
            future.result(timeout=1)
        finally:
            release_read.set()
            release.set()
        polling.result(timeout=2)
    assert manager.results()["results_through"] == 2


@pytest.mark.parametrize("old_validation", [True, False])
def test_new_run_is_not_blocked_or_replaced_by_late_old_validation(cached_run, monkeypatch, old_validation):
    manager = cached_run
    reader = seeded(manager)
    entered, release = block_poll(monkeypatch, outcome=old_validation)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-poll") as old_pool:
        old = old_pool.submit(manager.results, background=True)
        try:
            assert entered.wait(2)
            seed_run(manager, "b" * 32, "SH.600002")
            _running(manager)
            with ThreadPoolExecutor(max_workers=1) as new_pool:
                new = new_pool.submit(manager.results, background=True)
                try:
                    result = new.result(timeout=1)
                    assert result["run_id"] == "b" * 32
                    assert all(row["code"] == "SH.600002" for row in result["errors"])
                finally:
                    release.set()
            old.result(timeout=2)
        finally:
            release.set()
    result = manager.results()
    assert result["run_id"] == "b" * 32
    assert [row["code"] for row in result["errors"]] == ["SH.600002"]
    assert reader.context[0].endswith("b" * 32)


def test_completion_during_validation_does_not_wait_on_a_cleared_future(cached_run, monkeypatch):
    manager = cached_run
    reader = seeded(manager)
    pending = reader.future = Future()
    original = progress._unchanged

    def check(*args):
        pending.set_result(None)
        return original(*args)

    monkeypatch.setattr(progress, "_unchanged", check)
    result = manager.results()
    assert result["results_through"] == 1 and not result["results_loading"]


def test_failure_from_retired_reader_does_not_fail_the_new_run(cached_run, monkeypatch):
    manager = cached_run
    reader = seeded(manager)
    entered, release = threading.Event(), threading.Event()
    original = reader.read

    def read(directory, *args, **kwargs):
        if directory.name == "a" * 32 and kwargs.get("after_completed"):
            entered.set()
            assert release.wait(5)
            raise ValueError("controlled retired reader failure")
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(reader, "read", read)
    _append_error(manager, "SZ.000001")
    _running(manager, completed=2)
    manager.results(background=True)
    assert entered.wait(2)
    seed_run(manager, "b" * 32, "SH.600002")
    _running(manager)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(manager.results)
        try:
            # A background request registers the replacement while the old
            # worker remains blocked, avoiding a timing assumption here.
            manager.results(background=True)
            release.set()
            result = reading.result(timeout=2)
        finally:
            release.set()
    assert result["run_id"] == "b" * 32
    assert [row["code"] for row in result["errors"]] == ["SH.600002"]


def test_reader_thread_start_failure_does_not_leave_a_permanent_pending_future(cached_run, monkeypatch):
    manager = cached_run
    _running(manager)
    with monkeypatch.context() as fault:
        fault.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("thread unavailable")))
        with pytest.raises(RuntimeError, match="thread unavailable"):
            manager.results(background=True)
    first = manager.results(background=True)
    future = manager._progressive_results.future
    if first["results_loading"]:
        future.result(timeout=1)
    assert manager.results()["results_through"] == 1


def test_late_poll_cannot_regress_a_newer_committed_prefix(cached_run, monkeypatch):
    manager = cached_run
    reader = seeded(manager)
    entered, release = block_poll(monkeypatch)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-poll") as old_pool:
        old = old_pool.submit(manager.results, background=True)
        try:
            assert entered.wait(2)
            _append_error(manager, "SZ.000001")
            _running(manager, completed=2)
            with ThreadPoolExecutor(max_workers=1) as new_pool:
                new = new_pool.submit(manager.results, background=True)
                try:
                    assert new.result(timeout=1)["completed"] == 2
                finally:
                    release.set()
            old.result(timeout=2)
        finally:
            release.set()
    result = manager.results()
    assert result["results_through"] == 2 and reader.requested_completed == 2
    assert [row["code"] for row in result["errors"]] == ["SH.600000", "SZ.000001"]
