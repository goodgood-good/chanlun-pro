import sqlite3
import threading

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.monitor_activity import describe
from tests.web.test_notification_charts import ImageTransport, image_event, PNG


def test_later_success_resolves_old_failure_banner_but_keeps_history(tmp_path):
    clock = [1000.0]
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: clock[0],
        charts=True,
    )

    def failed(*_):
        raise ValueError("isolated failure")

    box.charts.renderer = failed
    box.charts.check_public = False
    box.charts.enqueue([image_event()], clock[0])
    for _ in range(3):
        box.charts.process_one()
        clock[0] += 31
    assert box.charts.snapshot()["last_error"]
    box.charts.renderer = lambda *_: PNG
    box.charts.enqueue([image_event(stamp=clock[0], identity="new")], clock[0])
    box.charts.process_one()
    generation = box.charts.snapshot()
    assert generation["failed"] == 1 and generation["last_error"] == ""
    activity = describe(
        {
            "enabled": True,
            "dingtalk_enabled": True,
            "pool_size": 1,
            "last_check_at": clock[0],
            "last_check_notifications": 0,
        },
        {"configured": True, "chart_generation": generation},
        "running",
    )
    assert activity["state"] == "quiet"
    box.charts.enqueue([image_event(stamp=clock[0], identity="retry")], clock[0])
    box.charts.renderer = failed
    box.charts.process_one()
    assert box.charts.snapshot()["last_error"], (
        "an active retry is still a current problem"
    )


def test_idle_disabled_loop_cleans_expired_history_and_keeps_pending_jobs(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: clock[0],
        charts=True,
    )
    box.charts.enqueue(
        [image_event(identity="old"), image_event(identity="pending")], clock[0]
    )
    with box.charts._db() as db:
        db.execute(
            "UPDATE jobs SET status='failed',updated=1000,error='old failure' WHERE id LIKE '%old:%'"
        )
    clock[0] += 9 * 86400
    assert box.charts.snapshot()["last_error"] == ""
    cleaned = threading.Event()
    original = box.charts.cleanup

    def cleanup():
        original()
        cleaned.set()

    monkeypatch.setattr(box.charts, "cleanup", cleanup)
    try:
        box.charts.start(lambda: False)
        assert cleaned.wait(2)
        with box.charts._db() as db:
            states = [r[0] for r in db.execute("SELECT status FROM jobs")]
        assert states == ["pending"]
    finally:
        box.charts.stop()


def test_previous_job_schema_is_migrated_without_losing_records(tmp_path):
    with sqlite3.connect(tmp_path / "chart_jobs.sqlite3") as db:
        db.execute(
            "CREATE TABLE jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT,created REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT DEFAULT '')"
        )
        db.execute(
            "INSERT INTO jobs(id,event,status,created,error) VALUES ('old','{}','failed',1000,'legacy error')"
        )
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: 1010,
        charts=True,
    )
    assert box.charts.snapshot()["last_error"] == "legacy error"
    with box.charts._db() as db:
        assert "updated" in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
