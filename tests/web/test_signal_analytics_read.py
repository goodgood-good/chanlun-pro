"""Analytics summaries should read committed data without acquiring write access."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from cl_app.services import signal_analytics
from cl_app.services.signal_analytics import SignalAnalytics
from tests.web.test_signal_analytics import event


@pytest.mark.parametrize("mode", ["IMMEDIATE", "EXCLUSIVE"])
def test_summary_has_bounded_wait_when_another_connection_is_writing(tmp_path, mode):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    store.record([event("committed", "confirmed", 1001)])
    writer = sqlite3.connect(store.path)
    writer.execute("BEGIN " + mode)
    writer.execute("UPDATE settings SET v=v+1 WHERE k='since'")
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(store.summary)
        try:
            result = reading.result(timeout=0.5)
            if mode == "IMMEDIATE":
                assert result["events"] == 1 and result["since"] == 1000 and not result["error"]
            else:
                assert result["error"], "exclusive contention must remain an explicit read failure"
        finally:
            writer.rollback()
            writer.close()
    recovered = store.summary()
    assert recovered["events"] == 1 and not recovered["error"]


def test_existing_summary_works_when_database_writes_are_forbidden(tmp_path, monkeypatch):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    store.record([event("committed", "confirmed", 1001)])
    original = sqlite3.connect
    writes = []
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX}

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)

        def authorize(action, first, second, database, trigger):
            if action in denied:
                writes.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(signal_analytics.sqlite3, "connect", connect)
    result = store.summary()
    assert result["events"] == 1 and not result["error"]
    assert writes == []


def test_first_summary_initializes_an_empty_store_and_later_records_invalidate_it(tmp_path):
    store = SignalAnalytics(tmp_path / "new # store", now=lambda: 1000)
    first = store.summary()
    assert first["events"] == 0 and first["since"] == 1000 and not first["error"]
    store.record([event("later", "confirmed", 1001)])
    assert store.summary()["events"] == 1


def test_summary_queries_share_one_snapshot_during_concurrent_commits(tmp_path, monkeypatch):
    store = SignalAnalytics(tmp_path, now=lambda: 1000)
    store.record([event("committed", "confirmed", 1001)])
    original = sqlite3.connect
    writer = original(store.path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    committed = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)

        def trace(sql):
            if "SELECT COUNT(*) FROM events WHERE recorded" in sql and not committed:
                writer.execute("""INSERT INTO events
                    SELECT 'later', 'later', cycle, market, frequency, point_type,
                    signal_type, stage, change_kind, recorded, cutoff, delay_bars,
                    exit_reason, external, revision FROM events LIMIT 1""")
                writer.commit()
                committed.append(True)

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(signal_analytics.sqlite3, "connect", connect)
    try:
        first = store.summary()
        assert committed == [True]
        assert first["events"] == first["groups"][0]["samples"] == 1
        assert not first["error"]
        restarted = SignalAnalytics(tmp_path, now=lambda: 1000).summary()
        assert restarted["events"] == restarted["groups"][0]["samples"] == 2
    finally:
        writer.close()
