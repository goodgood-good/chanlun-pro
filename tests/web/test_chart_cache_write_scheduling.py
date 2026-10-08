"""A burst from one chart must not exhaust the shared disk writer."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from chanlun.persistence import file_db
from cl_app.services import chart_cache


@pytest.fixture
def runtime():
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    file_db.start_pickle_writes()
    chart_cache.start_chart_cache_runtime()
    releases = []
    yield releases
    for event in releases:
        event.set()
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        try:
            future.result(timeout=5)
        except Exception:
            pass
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    file_db.shutdown_pickle_writes(wait=True)
    file_db.start_pickle_writes()
    chart_cache.start_chart_cache_runtime()


def drain():
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        future.result(timeout=5)


def block_busy_chart(monkeypatch, runtime):
    entered, release, fast_done = threading.Event(), threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.fdb._atomic_write_pickle_blocking
    written = []

    def write(path, value, **kwargs):
        if path == chart_cache.fdb._chart_cache_path_for("busy") and value["version"] == 1:
            entered.set()
            assert release.wait(5)
        result = original(path, value, **kwargs)
        written.append((path.name, value["version"]))
        if path == chart_cache.fdb._chart_cache_path_for("fast"):
            fast_done.set()
        return result

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    chart_cache._persist_chart_cache_async("busy", {"version": 1})
    assert entered.wait(2)
    return release, fast_done, written


def test_one_busy_chart_does_not_starve_an_unrelated_chart(runtime, monkeypatch):
    release, fast_done, written = block_busy_chart(monkeypatch, runtime)
    try:
        for version in range(2, 41):
            chart_cache._persist_chart_cache_async("busy", {"version": version})
        chart_cache._persist_chart_cache_async("fast", {"version": 100})
        assert fast_done.wait(0.5), "same-key writes occupied all shared writer threads"
    finally:
        release.set()
    drain()
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 40}
    assert sorted(version for name, version in written if name == "busy.pkl") == [1, 40]


def test_busy_key_pending_updates_use_one_latest_slot(runtime, monkeypatch):
    release, fast_done, written = block_busy_chart(monkeypatch, runtime)
    try:
        for version in range(2, 9):
            chart_cache._persist_chart_cache_async("busy", {"version": version})
        with chart_cache._chart_cache_disk_lock:
            assert len(chart_cache._chart_cache_disk_futures) <= 2
    finally:
        release.set()
    drain()
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 8}


@pytest.mark.parametrize("reason", ["closed", "capacity"])
def test_rejected_write_never_copies_the_snapshot(runtime, monkeypatch, reason):
    if reason == "closed":
        chart_cache.shutdown_chart_cache_runtime(wait=True)
    else:
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", slots)
    copies = []
    original = chart_cache.copy.deepcopy
    monkeypatch.setattr(chart_cache.copy, "deepcopy", lambda value: copies.append(value) or original(value))
    chart_cache._persist_chart_cache_async("rejected", {"data": [1, 2, 3]})
    assert copies == []
    assert chart_cache.fdb.get_chart_cache("rejected") is None


def test_snapshot_copy_is_tracked_until_shutdown_finishes(runtime, monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.copy.deepcopy

    def copy(value):
        entered.set()
        assert release.wait(5)
        return original(value)

    def shutdown():
        result = chart_cache.shutdown_chart_cache_runtime(wait=True)
        stopped.set()
        return result

    monkeypatch.setattr(chart_cache.copy, "deepcopy", copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(chart_cache._persist_chart_cache_async, "copying", {"version": 1})
        try:
            assert entered.wait(2)
            stopping = pool.submit(shutdown)
            assert not stopped.wait(0.15), "shutdown ignored an in-progress snapshot copy"
        finally:
            release.set()
        copying.result(timeout=2)
        assert stopping.result(timeout=2) is True
    assert chart_cache.fdb.get_chart_cache("copying") is None


def test_nonblocking_shutdown_cannot_restart_over_an_old_snapshot_copy(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.copy.deepcopy

    def copy(value):
        entered.set()
        assert release.wait(5)
        return original(value)

    monkeypatch.setattr(chart_cache.copy, "deepcopy", copy)
    with ThreadPoolExecutor(max_workers=1) as pool:
        copying = pool.submit(chart_cache._persist_chart_cache_async, "copying", {"version": 2})
        try:
            assert entered.wait(2)
            assert chart_cache.shutdown_chart_cache_runtime(wait=False) is False
            with pytest.raises(RuntimeError, match="active writes"):
                chart_cache.start_chart_cache_runtime()
        finally:
            release.set()
        copying.result(timeout=2)
    assert chart_cache.shutdown_chart_cache_runtime(wait=True) is True
    chart_cache.start_chart_cache_runtime()
    assert chart_cache.fdb.get_chart_cache("copying") is None


def test_write_failure_still_allows_the_latest_pending_snapshot(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.fdb._atomic_write_pickle_blocking

    def write(path, value, **kwargs):
        if value["version"] == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("controlled first write failure")
        return original(path, value, **kwargs)

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    chart_cache._persist_chart_cache_async("recover", {"version": 1})
    assert entered.wait(2)
    try:
        chart_cache._persist_chart_cache_async("recover", {"version": 2})
    finally:
        release.set()
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        try:
            future.result(timeout=3)
        except OSError:
            pass
    assert chart_cache.fdb.get_chart_cache("recover") == {"version": 2}


def test_latest_snapshot_can_replace_a_waiting_one_at_capacity(runtime, monkeypatch):
    monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", threading.BoundedSemaphore(2))
    release, fast_done, written = block_busy_chart(monkeypatch, runtime)
    try:
        chart_cache._persist_chart_cache_async("busy", {"version": 2})
        chart_cache._persist_chart_cache_async("busy", {"version": 3})
    finally:
        release.set()
    drain()
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 3}


def test_copy_failure_releases_capacity_for_a_later_request(runtime, monkeypatch):
    monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", threading.BoundedSemaphore(1))
    original = chart_cache.copy.deepcopy

    def copy(value):
        if value["version"] == 1:
            raise ValueError("controlled copy failure")
        return original(value)

    monkeypatch.setattr(chart_cache.copy, "deepcopy", copy)
    chart_cache._persist_chart_cache_async("copy-failed", {"version": 1})
    chart_cache._persist_chart_cache_async("copy-failed", {"version": 2})
    drain()
    assert chart_cache.fdb.get_chart_cache("copy-failed") == {"version": 2}


def test_submit_failure_does_not_leave_the_key_or_capacity_reserved(runtime, monkeypatch):
    monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", threading.BoundedSemaphore(1))
    with monkeypatch.context() as patch:
        patch.setattr(chart_cache._chart_cache_disk_executor, "submit", lambda *args: (_ for _ in ()).throw(RuntimeError("controlled queue rejection")))
        chart_cache._persist_chart_cache_async("submit-failed", {"version": 1})
    chart_cache._persist_chart_cache_async("submit-failed", {"version": 2})
    drain()
    assert chart_cache.fdb.get_chart_cache("submit-failed") == {"version": 2}


def test_shutdown_discards_coalesced_pending_work_and_drains_active_write(runtime, monkeypatch):
    release, fast_done, written = block_busy_chart(monkeypatch, runtime)
    try:
        chart_cache._persist_chart_cache_async("busy", {"version": 2})
        assert chart_cache.shutdown_chart_cache_runtime(wait=False) is False
    finally:
        release.set()
    assert chart_cache.shutdown_chart_cache_runtime(wait=True) is True
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 1}
    chart_cache.start_chart_cache_runtime()
    chart_cache._persist_chart_cache_async("busy", {"version": 3})
    drain()
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 3}


def test_caller_mutation_after_submission_cannot_change_the_disk_snapshot(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.fdb._atomic_write_pickle_blocking

    def write(path, value, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(path, value, **kwargs)

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    value = {"nested": [10]}
    chart_cache._persist_chart_cache_async("immutable", value)
    try:
        assert entered.wait(2)
        value["nested"][0] = 99
    finally:
        release.set()
    drain()
    assert chart_cache.fdb.get_chart_cache("immutable") == {"nested": [10]}


def test_shutdown_releases_a_group_whose_driver_has_not_started(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    runtime.append(release)
    original = chart_cache.fdb._atomic_write_pickle_blocking
    started = []
    lock = threading.Lock()

    def write(path, value, **kwargs):
        with lock:
            started.append(path.name)
            if len(started) == 4:
                entered.set()
        assert release.wait(5)
        return original(path, value, **kwargs)

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    for index in range(4):
        chart_cache._persist_chart_cache_async("active-" + str(index), {"version": index})
    try:
        assert entered.wait(2)
        chart_cache._persist_chart_cache_async("queued", {"version": 5})
        assert chart_cache.shutdown_chart_cache_runtime(wait=False) is False
    finally:
        release.set()
    assert chart_cache.shutdown_chart_cache_runtime(wait=True) is True
    assert chart_cache.fdb.get_chart_cache("queued") is None
    chart_cache.start_chart_cache_runtime()
    chart_cache._persist_chart_cache_async("queued", {"version": 6})
    drain()
    assert chart_cache.fdb.get_chart_cache("queued") == {"version": 6}


def test_repeated_slot_transfers_do_not_overrelease_shared_capacity(runtime, monkeypatch):
    monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", threading.BoundedSemaphore(2))
    release, fast_done, written = block_busy_chart(monkeypatch, runtime)
    try:
        for version in range(2, 21):
            chart_cache._persist_chart_cache_async("busy", {"version": version})
        chart_cache._persist_chart_cache_async("fast", {"version": 100})
        assert not fast_done.wait(0.1), "coalescing accidentally created extra capacity"
    finally:
        release.set()
    drain()
    assert chart_cache.fdb.get_chart_cache("busy") == {"version": 20}
    assert chart_cache.fdb.get_chart_cache("fast") is None
    chart_cache._persist_chart_cache_async("fast", {"version": 101})
    drain()
    assert chart_cache.fdb.get_chart_cache("fast") == {"version": 101}
