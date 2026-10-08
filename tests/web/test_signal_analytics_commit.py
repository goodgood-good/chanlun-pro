"""Analytics acknowledges a batch only after its transaction commits."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from cl_app.services.signal_analytics import SignalAnalytics
from tests.web.test_signal_analytics import event
from tests.web.test_monitor_recovery import scenario as scenario
from tests.web.test_signal_monitor import Jobs


def block_commit(store, monkeypatch):
    store.summary()
    original = store._db

    @contextmanager
    def short_timeout():
        with original() as db:
            db.execute("PRAGMA busy_timeout=20")
            yield db

    monkeypatch.setattr(store, "_db", short_timeout)
    reader = sqlite3.connect(store.path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM events").fetchall()
    return reader


def test_real_commit_failure_retries_the_same_event_batch(tmp_path, monkeypatch):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    rows = [event("event", "confirmed", 1001)]
    reader = block_commit(store, monkeypatch)
    try:
        store.record(rows)
        assert store.error
    finally:
        reader.rollback()
        reader.close()
    store.record(rows)
    assert store.summary()["events"] == 1
    assert not store.error


def test_failed_commit_preserves_last_acknowledged_signature_and_cache(tmp_path, monkeypatch):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    reader = block_commit(store, monkeypatch)
    cached, signature = store.cached, store.last_signature
    try:
        store.record([event("event", "confirmed", 1001)])
        assert store.error
        assert store.last_signature == signature
        assert store.cached is cached
    finally:
        reader.rollback()
        reader.close()


@pytest.mark.parametrize("updated", [{"change": "discovered"}, {"source_closed_at": 995}])
def test_changes_used_by_event_identity_are_not_skipped(tmp_path, updated):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    first = event("same", "confirmed", 1001, change="changed", source_closed_at=990)
    store.record([first])
    store.record([{**first, **updated}])
    result = store.summary()
    assert result["events"] == 2
    assert result["groups"][0]["samples"] == 1


def test_late_failure_cannot_overwrite_a_later_success_status(tmp_path, monkeypatch):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    released, finish = threading.Event(), threading.Event()
    actual_lock = threading.RLock()
    original = store._db

    class Gate:
        def __enter__(self):
            actual_lock.acquire()
            return self

        def __exit__(self, *args):
            actual_lock.release()
            if threading.current_thread().name.startswith("analytics-failing"):
                released.set()
                assert finish.wait(5)

    @contextmanager
    def database():
        if threading.current_thread().name.startswith("analytics-failing"):
            raise OSError("controlled first write failure")
        with original() as db:
            yield db

    monkeypatch.setattr(store, "lock", Gate())
    monkeypatch.setattr(store, "_db", database)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics-failing") as pool:
        failed = pool.submit(store.record, [event("old", "confirmed", 1001)])
        try:
            assert released.wait(2)
            store.record([event("new", "confirmed", 1002)])
        finally:
            finish.set()
        failed.result(timeout=2)
    assert store.summary()["events"] == 1
    assert not store.error, "the retired failure replaced a successful later status"


@pytest.mark.parametrize("invalid", [None, {"id": "missing-fields", "stage": "confirmed", "recorded_at": 1001}])
def test_malformed_analytics_input_reports_an_error_without_escaping(tmp_path, invalid):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    store.record([invalid])
    assert store.error
    store.record([event("valid", "confirmed", 1002)])
    assert store.summary()["events"] == 1 and not store.error


def test_incomplete_historical_statistics_cannot_stop_monitor_progress(scenario, monkeypatch):
    service, jobs, box, transport, clock, restart = scenario
    service.screening = Jobs(service.root / "manual")
    service._load()["events"] = [{"id": "legacy", "stage": "confirmed", "recorded_at": clock[0] + 1}]
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    monkeypatch.setattr(service, "_schedule_daily", lambda *args: None)
    scheduled = Mock()
    monkeypatch.setattr(service, "_schedule_monitor", scheduled)
    service.tick()
    assert service._load()["processed_run"] == jobs.state["run_id"]
    assert service.analytics.error
    scheduled.assert_called_once()
    assert box.snapshot()["pending"] == 1
