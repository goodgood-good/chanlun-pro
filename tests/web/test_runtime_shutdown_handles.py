import threading
import time

import pytest

from cl_app.handlers import sse_stream
from cl_app.services import chart_cache, chart_revalidate, constants


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_metadata_loader_can_be_stopped_and_reopened():
    metadata = constants._LazyMarketDict(
        lambda _key, _market: ["ready"],
        markets=[("a", object())],
        fallback_factory=list,
    )
    assert metadata.shutdown(timeout=0.01) is True
    assert metadata["a"] == []
    metadata.start()
    assert metadata["a"] == ["ready"]


def test_revalidation_runtime_rejects_after_shutdown_and_can_reopen(monkeypatch):
    chart_revalidate._reset_revalidation_state_for_tests()
    assert chart_revalidate.shutdown_revalidation(wait=True) is True
    assert chart_revalidate.submit_revalidation("a", "X", "1m", {}, "K") is False

    monkeypatch.setattr(chart_revalidate, "_do_revalidate", lambda *_args: None)
    chart_revalidate.start_revalidation_runtime()
    assert chart_revalidate.submit_revalidation("a", "X", "1m", {}, "K") is True
    assert _wait_until(lambda: chart_revalidate.revalidation_status()["active"] == 0)


def test_sse_shutdown_wakes_connected_handlers():
    class Flag:
        def __init__(self):
            self.value = False

        def set(self):
            self.value = True

        def is_set(self):
            return self.value

    class Loop:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    client = type("Client", (), {"_closed": Flag()})()
    loop = Loop()
    sse_stream.get_hub()._subs["key"] = {
        "clients": {client},
        "client_ids": {client: "id"},
        "loop": loop,
    }

    assert sse_stream.shutdown_sse_runtime() is True
    assert loop.stopped is True
    assert client._closed.is_set() is True
    assert sse_stream.get_hub().active_keys() == []
    sse_stream.start_sse_runtime()

def test_sse_runtime_shutdown_and_restart_without_active_work():
    sse_stream._reset_sse_runtime_for_tests(max_pending=1)
    assert sse_stream.shutdown_sse_runtime() is True
    assert sse_stream.sse_runtime_status()["closed"] is True
    sse_stream.start_sse_runtime()
    assert sse_stream.sse_runtime_status()["closed"] is False


def test_sse_start_is_idempotent_with_active_work_on_open_runtime():
    marker = object()
    with sse_stream._runtime_lock:
        previous_closed = sse_stream._runtime_closed
        sse_stream._runtime_closed = False
        sse_stream._runtime_inflight.add(marker)
    try:
        sse_stream.start_sse_runtime()
        assert sse_stream.sse_runtime_status()["closed"] is False
    finally:
        with sse_stream._runtime_lock:
            sse_stream._runtime_inflight.discard(marker)
            sse_stream._runtime_closed = previous_closed


def test_sse_restart_still_rejects_active_work_after_shutdown():
    marker = object()
    with sse_stream._runtime_lock:
        previous_closed = sse_stream._runtime_closed
        sse_stream._runtime_closed = True
        sse_stream._runtime_inflight.add(marker)
    try:
        with pytest.raises(
            RuntimeError,
            match="cannot restart SSE runtime with active recomputes",
        ):
            sse_stream.start_sse_runtime()
    finally:
        with sse_stream._runtime_lock:
            sse_stream._runtime_inflight.discard(marker)
            sse_stream._runtime_closed = previous_closed


def test_chart_cache_writer_threads_are_daemon():
    completed = threading.Event()
    chart_cache.start_chart_cache_runtime()
    try:
        future = chart_cache._chart_cache_disk_executor.submit(completed.set)

        assert future.result(timeout=1) is None
        assert completed.is_set()
        assert all(
            thread.daemon
            for thread in chart_cache._chart_cache_disk_executor._threads
        )
    finally:
        chart_cache.shutdown_chart_cache_runtime(wait=False)

def test_chart_cache_writer_shutdown_and_restart(monkeypatch):
    written = threading.Event()
    monkeypatch.setattr(
        chart_cache.fdb,
        "set_chart_cache",
        lambda *_args: written.set(),
    )
    chart_cache.shutdown_chart_cache_runtime(wait=True)
    chart_cache.start_chart_cache_runtime()
    chart_cache._persist_chart_cache_async(
        "key",
        {"data": {"t": [1]}, "validated_at": 1},
    )
    assert written.wait(timeout=1)


def test_late_chart_cache_write_does_not_restart_runtime(monkeypatch):
    written = threading.Event()
    monkeypatch.setattr(
        chart_cache.fdb,
        "set_chart_cache",
        lambda *_args: written.set(),
    )
    chart_cache.shutdown_chart_cache_runtime(wait=False)

    chart_cache._persist_chart_cache_async(
        "late-key",
        {"data": {"t": [1]}, "validated_at": 1},
    )

    assert written.wait(timeout=0.1) is False
    assert chart_cache._chart_cache_disk_closed is True
    assert chart_cache._chart_cache_disk_executor is None
