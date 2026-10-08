"""The chart writer owns a disk operation until the actual file write ends."""

from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest

from chanlun.persistence import file_db
from cl_app.services import chart_cache


@pytest.fixture
def writer(monkeypatch):
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
        if not future.cancelled():
            future.result(timeout=3)
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    file_db.shutdown_pickle_writes(wait=True)
    file_db.start_pickle_writes()
    chart_cache.start_chart_cache_runtime()


def slow_file_write(monkeypatch, writer, slow_key="slow"):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    writer.append(release)
    original = chart_cache.fdb._atomic_write_pickle_blocking

    def write(path, obj, **kwargs):
        if path == chart_cache.fdb._chart_cache_path_for(slow_key):
            entered.set()
            assert release.wait(5), "test did not release the disk write"
        try:
            return original(path, obj, **kwargs)
        finally:
            if path == chart_cache.fdb._chart_cache_path_for(slow_key):
                finished.set()

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    return entered, release, finished


def test_file_cache_returns_actual_completion_future(writer, monkeypatch):
    entered, release, finished = slow_file_write(monkeypatch, writer)
    completion = chart_cache.fdb.set_chart_cache("slow", {"value": 1})
    try:
        assert entered.wait(2)
        assert isinstance(completion, Future)
        assert not completion.done()
    finally:
        release.set()
        assert finished.wait(2)
    completion.result(timeout=2)
    assert chart_cache.fdb.get_chart_cache("slow") == {"value": 1}


def test_delete_waits_for_nested_disk_write_and_prevents_resurrection(writer, monkeypatch):
    entered, release, finished = slow_file_write(monkeypatch, writer)
    deleting = threading.Event()
    real_delete = chart_cache.fdb.delete_chart_cache

    def observed_delete(key):
        deleting.set()
        return real_delete(key)

    monkeypatch.setattr(chart_cache.fdb, "delete_chart_cache", observed_delete)
    chart_cache._persist_chart_cache_async("slow", {"value": "invalidated"})
    assert entered.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deletion = pool.submit(chart_cache._delete_chart_cache_entry, "slow")
        try:
            assert not deleting.wait(0.15), "delete ran before the actual write completed"
        finally:
            release.set()
        deletion.result(timeout=2)
    assert finished.is_set()
    assert chart_cache.fdb.get_chart_cache("slow") is None


def test_shutdown_wait_true_drains_actual_file_write(writer, monkeypatch):
    entered, release, _ = slow_file_write(monkeypatch, writer)
    chart_cache._persist_chart_cache_async("slow", {"value": 2})
    assert entered.wait(2)
    stopped = threading.Event()

    def shutdown():
        result = chart_cache.shutdown_chart_cache_runtime(wait=True)
        stopped.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        shutdown_result = pool.submit(shutdown)
        try:
            assert not stopped.wait(0.15), "writer shutdown returned before disk completion"
        finally:
            release.set()
        assert shutdown_result.result(timeout=2) is True
    assert chart_cache.fdb.get_chart_cache("slow") == {"value": 2}


def test_shutdown_without_wait_reports_active_write_and_blocks_restart(writer, monkeypatch):
    entered, release, _ = slow_file_write(monkeypatch, writer)
    chart_cache._persist_chart_cache_async("slow", {"value": 3})
    assert entered.wait(2)
    try:
        assert chart_cache.shutdown_chart_cache_runtime(wait=False) is False
        with pytest.raises(RuntimeError, match="active writes"):
            chart_cache.start_chart_cache_runtime()
    finally:
        release.set()


def test_slow_disk_write_keeps_its_capacity_slot_until_completion(writer, monkeypatch):
    monkeypatch.setattr(chart_cache, "_chart_cache_disk_slots", threading.BoundedSemaphore(1))
    entered, release, _ = slow_file_write(monkeypatch, writer)
    chart_cache._persist_chart_cache_async("slow", {"value": 4})
    assert entered.wait(2)
    chart_cache._persist_chart_cache_async("over-capacity", {"value": 5})
    release.set()
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    assert chart_cache.fdb.get_chart_cache("slow") == {"value": 4}
    assert chart_cache.fdb.get_chart_cache("over-capacity") is None


def test_other_chart_can_finish_while_one_file_write_is_blocked(writer, monkeypatch):
    entered, release, _ = slow_file_write(monkeypatch, writer)
    fast_written = threading.Event()
    original = chart_cache.fdb._atomic_write_pickle_blocking

    def write(path, obj, **kwargs):
        result = original(path, obj, **kwargs)
        if path == chart_cache.fdb._chart_cache_path_for("fast"):
            fast_written.set()
        return result

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    chart_cache._persist_chart_cache_async("slow", {"value": 6})
    assert entered.wait(2)
    try:
        chart_cache._persist_chart_cache_async("fast", {"value": 7})
        assert fast_written.wait(2), "an unrelated chart was blocked by the slow file"
        assert chart_cache.fdb.get_chart_cache("fast") == {"value": 7}
    finally:
        release.set()


def test_older_dispatch_cannot_overwrite_newer_same_key_write(writer, monkeypatch):
    entered, release, newer_finished = threading.Event(), threading.Event(), threading.Event()
    writer.append(release)
    original = chart_cache.fdb.set_chart_cache

    def delayed_dispatch(key, entry):
        if entry["version"] == 1:
            entered.set()
            assert release.wait(5)
        completion = original(key, entry)
        completion.result(timeout=2)
        if entry["version"] == 2:
            newer_finished.set()
        return completion

    monkeypatch.setattr(chart_cache.fdb, "set_chart_cache", delayed_dispatch)
    chart_cache._persist_chart_cache_async("ordered", {"version": 1})
    assert entered.wait(2)
    try:
        chart_cache._persist_chart_cache_async("ordered", {"version": 2})
        assert not newer_finished.wait(0.15), "newer write overtook an active same-key writer"
    finally:
        release.set()
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        future.result(timeout=3)
    assert chart_cache.fdb.get_chart_cache("ordered") == {"version": 2}


@pytest.mark.parametrize("operation", ["replace", "invalidate"])
def test_delayed_snapshot_cannot_publish_after_a_newer_operation(writer, monkeypatch, operation):
    entered, release, fresh_written = threading.Event(), threading.Event(), threading.Event()
    writer.append(release)
    original_copy = chart_cache.copy.deepcopy
    original_write = chart_cache.fdb._atomic_write_pickle_blocking

    def copy_snapshot(value, *args, **kwargs):
        if isinstance(value, dict) and (value.get("data") or {}).get("c") == [1.]:
            entered.set()
            assert release.wait(5)
        return original_copy(value, *args, **kwargs)

    def write(path, obj, **kwargs):
        result = original_write(path, obj, **kwargs)
        if (obj.get("data") or {}).get("c") == [2.]:
            fresh_written.set()
        return result

    monkeypatch.setattr(chart_cache.copy, "deepcopy", copy_snapshot)
    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", write)
    key = "snapshot-" + operation
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(chart_cache._set_chart_cache_entry, key, {"t": [1], "c": [1.]}, True)
        try:
            assert entered.wait(2)
            if operation == "replace":
                chart_cache._set_chart_cache_entry(key, {"t": [1], "c": [2.]}, True)
                assert fresh_written.wait(2)
            else:
                chart_cache._delete_chart_cache_entry(key)
        finally:
            release.set()
        old.result(timeout=2)
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        future.result(timeout=3)
    disk = chart_cache.fdb.get_chart_cache(key)
    if operation == "replace":
        assert disk["data"]["c"] == [2.]
    else:
        assert disk is None
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)


def test_invalidated_inflight_cold_read_cannot_refill_ram(writer, monkeypatch):
    entry = chart_cache._build_chart_cache_entry({"t": [1], "c": [10.]}, is_full_snapshot=True)
    chart_cache.fdb.set_chart_cache("cold", entry).result(timeout=2)
    entered, release = threading.Event(), threading.Event()
    writer.append(release)
    original = chart_cache.fdb.get_chart_cache

    def old_read(key):
        value = original(key)
        entered.set()
        assert release.wait(5)
        return value

    monkeypatch.setattr(chart_cache.fdb, "get_chart_cache", old_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(chart_cache._get_chart_cache_entry, "cold")
        try:
            assert entered.wait(2)
            chart_cache._delete_chart_cache_entry("cold")
        finally:
            release.set()
        assert reading.result(timeout=2) is None
    assert chart_cache._get_chart_cache_entry_ram_only("cold") is None


def test_actual_disk_failure_is_visible_on_completion_future(writer, monkeypatch):
    def failed_write(*_args, **_kwargs):
        raise OSError("controlled disk failure")

    monkeypatch.setattr(chart_cache.fdb, "_atomic_write_pickle_blocking", failed_write)
    completion = chart_cache.fdb.set_chart_cache("failed", {"value": 8})
    with pytest.raises(OSError, match="controlled disk failure"):
        completion.result(timeout=2)


def test_reads_during_invalidation_do_not_restore_the_old_disk_snapshot(writer, monkeypatch):
    key = "deleting"
    entry = chart_cache._build_chart_cache_entry({"t": [1], "c": [10.]}, is_full_snapshot=True)
    chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
    entered, release, _ = slow_file_write(monkeypatch, writer, slow_key=key)
    ram_removed = threading.Event()
    original_pop = chart_cache.chart_data_cache.pop

    def pop(cache_key, *args):
        result = original_pop(cache_key, *args)
        if cache_key == key:
            ram_removed.set()
        return result

    monkeypatch.setattr(chart_cache.chart_data_cache, "pop", pop)
    chart_cache._persist_chart_cache_async(key, entry)
    assert entered.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deletion = pool.submit(chart_cache._delete_chart_cache_entry, key)
        try:
            assert ram_removed.wait(2)
            assert chart_cache._get_chart_cache_entry(key) is None
        finally:
            release.set()
        deletion.result(timeout=2)
    assert chart_cache._get_chart_cache_entry_ram_only(key) is None


def test_fresh_snapshot_published_during_delete_survives_in_ram_and_disk(writer, monkeypatch):
    key = "replace-during-delete"
    entry = chart_cache._build_chart_cache_entry({"t": [1], "c": [10.]}, is_full_snapshot=True)
    chart_cache.fdb.set_chart_cache(key, entry).result(timeout=2)
    deleting, release = threading.Event(), threading.Event()
    writer.append(release)
    original_delete = chart_cache.fdb.delete_chart_cache

    def delete(cache_key):
        deleting.set()
        assert release.wait(5)
        return original_delete(cache_key)

    monkeypatch.setattr(chart_cache.fdb, "delete_chart_cache", delete)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deletion = pool.submit(chart_cache._delete_chart_cache_entry, key)
        try:
            assert deleting.wait(2)
            chart_cache._set_chart_cache_entry(key, {"t": [1], "c": [20.]}, True)
            assert chart_cache._get_chart_cache_entry_ram_only(key)["data"]["c"] == [20.]
        finally:
            release.set()
        deletion.result(timeout=2)
    with chart_cache._chart_cache_disk_lock:
        pending = list(chart_cache._chart_cache_disk_futures)
    for future in pending:
        future.result(timeout=3)
    assert chart_cache.fdb.get_chart_cache(key)["data"]["c"] == [20.]
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(key, None)
