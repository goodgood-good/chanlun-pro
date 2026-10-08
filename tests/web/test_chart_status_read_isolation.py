"""Chart status and retention reads must not wait for a writer reservation."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from cl_app.services import notification_charts
from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_key
from tests.web.test_notification_charts import ImageTransport, image_event


READERS = ["snapshot", "archive_snapshot", "archive_referenced_runs"]


@pytest.fixture
def charts(tmp_path):
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", transport=ImageTransport(), now=lambda: 1000, charts=True)
    event = image_event()
    with box.charts._db() as db:
        db.execute("INSERT INTO jobs(id,event,status,created) VALUES (?,?,'pending',1000)",
                   (signal_key(event), json.dumps(event)))
    return box.charts


@pytest.mark.parametrize("method", READERS)
def test_current_chart_status_reads_committed_data_while_a_writer_is_active(charts, method):
    read = getattr(charts, method)
    expected = read()
    writer = sqlite3.connect(charts.root / "chart_jobs.sqlite3")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE settings SET v=v+1 WHERE k='archive_enabled_at'")
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(read)
        try:
            assert reading.result(timeout=1) == expected
        finally:
            writer.rollback()
            writer.close()
    assert read() == expected


@pytest.mark.parametrize("method", READERS)
def test_exclusive_contention_has_a_bounded_wait_and_recovers(charts, method):
    read = getattr(charts, method)
    expected = read()
    writer = sqlite3.connect(charts.root / "chart_jobs.sqlite3")
    writer.execute("BEGIN EXCLUSIVE")
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(read)
        try:
            if method == "archive_referenced_runs":
                with pytest.raises(sqlite3.Error):
                    reading.result(timeout=1)
            else:
                result = reading.result(timeout=1)
                assert result.get("error") or result.get("last_error")
        finally:
            writer.rollback()
            writer.close()
    assert read() == expected


@pytest.mark.parametrize("method", READERS)
def test_current_chart_reads_do_not_request_write_permission(charts, method, monkeypatch):
    read = getattr(charts, method)
    expected = read()
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

    monkeypatch.setattr(notification_charts.sqlite3, "connect", connect)
    assert read() == expected
    assert writes == []


@pytest.mark.parametrize("method", READERS)
def test_reading_an_unused_chart_service_does_not_create_storage(tmp_path, method):
    box = DingTalkOutbox(tmp_path / "new # charts" / "outbox.sqlite", charts=True)
    getattr(box.charts, method)()
    assert not box.charts.root.exists()


def test_legacy_schema_upgrade_does_not_wait_indefinitely_and_recovers(tmp_path):
    path = tmp_path / "chart_jobs.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT,created REAL,"
               "attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT DEFAULT '')")
    db.execute("INSERT INTO jobs(id,event,status,created,error) VALUES ('old','{}','failed',1000,'legacy error')")
    db.commit()
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: 1010, charts=True)
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE jobs SET attempts=1 WHERE id='old'")
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(box.charts.snapshot)
        try:
            assert reading.result(timeout=1)["last_error"]
        finally:
            db.rollback()
            db.close()
    assert box.charts.snapshot()["last_error"] == "legacy error"
