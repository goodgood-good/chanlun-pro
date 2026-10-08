"""Status reads are bounded, coherent, and independent of queue writes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3

import pytest

from cl_app.services import dingtalk_notifications
from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_key
from tests.web.test_dingtalk_notifications import Transport, event


READERS = ["snapshot", "has", "referenced_runs", "latency_summary"]


def read(box, method):
    return box.has(signal_key(event("confirmed"))) if method == "has" else getattr(box, method)()


@pytest.fixture
def box(tmp_path):
    outbox = DingTalkOutbox(tmp_path / "outbox # status.sqlite", transport=Transport(), now=lambda: 1000)
    outbox.signal_events([{**event("confirmed"), "evidence_run_id": "a" * 32}], since=0)
    return outbox


@pytest.mark.parametrize("method", READERS)
def test_readers_keep_committed_data_during_an_active_writer(box, method):
    expected = read(box, method)
    with closing(sqlite3.connect(box.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE outbox SET status='sent',sent=1000")
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(read, box, method)
            try:
                assert reading.result(timeout=1) == expected
            finally:
                writer.rollback()
    assert read(box, method) == expected


@pytest.mark.parametrize("method", READERS)
def test_exclusive_lock_returns_bounded_failure_and_then_recovers(box, method):
    expected = read(box, method)
    with closing(sqlite3.connect(box.path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(read, box, method)
            try:
                if method in {"has", "referenced_runs"}:
                    with pytest.raises(sqlite3.Error):
                        reading.result(timeout=1)
                else:
                    result = reading.result(timeout=1)
                    assert result.get("last_error") or result.get("error")
            finally:
                writer.rollback()
    assert read(box, method) == expected


def deny_writes(monkeypatch):
    original = sqlite3.connect
    writes = []
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_ALTER_TABLE}

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        def authorize(action, first, second, database, trigger):
            if action in denied:
                writes.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db
    monkeypatch.setattr(dingtalk_notifications.sqlite3, "connect", connect)
    return writes


@pytest.mark.parametrize("method", READERS)
def test_modern_status_reads_need_no_write_permissions(box, method, monkeypatch):
    expected = read(box, method)
    writes = deny_writes(monkeypatch)
    assert read(box, method) == expected
    assert not writes


@pytest.mark.parametrize("method", READERS)
def test_unused_service_status_does_not_create_storage(tmp_path, method):
    box = DingTalkOutbox(tmp_path / "absent" / "outbox.sqlite", transport=Transport())
    read(box, method)
    assert not box.path.parent.exists()


def test_snapshot_counts_and_history_share_one_committed_view(box, monkeypatch):
    with closing(sqlite3.connect(box.path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
    before = box.snapshot()
    connect = sqlite3.connect
    committed = []

    class InterleavedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT status,COUNT(*) FROM outbox WHERE kind!='chart'") and not committed:
                with closing(connect(box.path)) as writer:
                    writer.execute("UPDATE outbox SET status='sent',sent=1000")
                    writer.commit()
                committed.append(True)
            return cursor

    monkeypatch.setattr(dingtalk_notifications.sqlite3, "connect",
                        lambda *a, **k: connect(*a, **k, factory=InterleavedConnection))
    during = box.snapshot()
    assert committed
    assert during == before, "status totals and history describe different commits"
    after = box.snapshot()
    assert after["pending"] == 0 and after["sent"] == 1 and after["history"][0]["status"] == "sent"


@pytest.mark.parametrize("method", READERS)
def test_legacy_upgrade_wait_is_bounded_and_preserves_deadlines(tmp_path, method):
    path = tmp_path / "old.sqlite"
    payload = [{**event("confirmed"), "evidence_run_id": "a" * 32}]
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY,kind TEXT,content TEXT,status TEXT,created REAL,"
                   "expires REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,sent REAL,error TEXT DEFAULT '')")
        db.execute("CREATE TABLE signal_payloads(outbox_id TEXT PRIMARY KEY, events TEXT NOT NULL)")
        db.execute("CREATE TABLE markers(id TEXT PRIMARY KEY, created REAL NOT NULL)")
        db.execute("INSERT INTO markers VALUES (?,1000)", (signal_key(payload[0]),))
        db.execute("INSERT INTO outbox(id,kind,content,status,created,expires) VALUES ('old','signals','old','pending',1000,1100)")
        db.execute("INSERT INTO signal_payloads VALUES ('old',?)", (json.dumps(payload),))
        db.commit()
    box = DingTalkOutbox(path, transport=Transport(), now=lambda: 1000)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(read, box, method)
            try:
                if method in {"has", "referenced_runs"}:
                    with pytest.raises(sqlite3.Error):
                        reading.result(timeout=1)
                else:
                    value = reading.result(timeout=1)
                    assert value.get("error") or value.get("last_error")
            finally:
                writer.rollback()
    read(box, method)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT expires FROM outbox WHERE id='old'").fetchone()[0] == 2800
        assert "send_started" in {r[1] for r in db.execute("PRAGMA table_info(outbox)")}
    assert box.has(signal_key(payload[0]))


def test_chart_recovery_source_read_needs_no_write_permissions(box, monkeypatch):
    from cl_app.services.notification_charts import NotificationCharts
    charts = NotificationCharts(box)
    with box._connect() as db:
        db.execute("UPDATE outbox SET status='sent',sent=1000")
    recovered = []
    monkeypatch.setattr(charts, "enqueue", lambda events, stamp: recovered.extend(events))
    writes = deny_writes(monkeypatch)
    charts.recover()
    assert len(recovered) == 1 and recovered[0]["id"] == "point"
    assert not writes
