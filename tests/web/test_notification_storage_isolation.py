from contextlib import contextmanager
import sqlite3
from unittest.mock import Mock
from types import SimpleNamespace

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services import signal_monitor, dingtalk_notifications
from tests.web.test_monitor_recovery import scenario as scenario
from tests.web.test_signal_monitor import Jobs
from tests.web.test_dingtalk_notifications import Transport, event


@contextmanager
def locked_store():
    raise sqlite3.OperationalError("isolated database is locked")
    yield


def test_queue_storage_failure_does_not_stop_committed_results_or_next_schedule(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, _ = scenario
    monkeypatch.setattr(
        signal_monitor, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    service.screening = Jobs(service.root / "manual")
    service._thread = Mock()
    service._thread.is_alive.return_value = True
    service._last_tick_at = clock[0]
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    monkeypatch.setattr(service, "_schedule_daily", lambda *a: None)
    schedule = Mock()
    monkeypatch.setattr(service, "_schedule_monitor", schedule)
    with monkeypatch.context() as fault:
        fault.setattr(box, "_connect", locked_store)
        service.tick()
        assert service._load()["processed_run"] == jobs.state["run_id"]
        assert len(service._load()["events"]) == 1
        schedule.assert_called_once()
        status = service.snapshot()
        assert (
            status["dingtalk"]["last_error"]
            and status["notification_activity"]["state"] == "failed"
        )
        clock[0] += 6
        service.tick()
        assert schedule.call_count == 2, (
            "repeated queue errors must not stop scheduling"
        )
    clock[0] += 6
    service.tick()
    box.deliver_one()
    assert len(transport.messages) == 1
    clock[0] += 5
    service.tick()
    box.deliver_one()
    assert len(transport.messages) == 1 and not box.snapshot()["last_error"]


def test_unreadable_outbox_still_has_an_explicit_status_and_recovers(
    tmp_path, monkeypatch
):
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=Transport(), now=lambda: 1000
    )
    box.signal_events([event("confirmed")], since=0)
    with monkeypatch.context() as fault:
        fault.setattr(box, "_read_connect", locked_store)
        status = box.snapshot()
        assert status["storage_available"] is False and status["last_error"]
    assert box.snapshot()["storage_available"] is True
    assert box.snapshot()["pending"] == 1


def test_schema_initialization_failure_closes_the_database_connection(
    tmp_path, monkeypatch
):
    connection = Mock()
    connection.executescript.side_effect = sqlite3.OperationalError(
        "locked during initialization"
    )
    monkeypatch.setattr(
        dingtalk_notifications.sqlite3, "connect", lambda *a, **k: connection
    )
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", transport=Transport())
    import pytest

    with pytest.raises(sqlite3.OperationalError):
        with box._connect():
            pass
    connection.close.assert_called_once()


def test_signal_that_exited_during_queue_outage_is_not_delivered_on_recovery(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, _ = scenario
    monkeypatch.setattr(
        signal_monitor, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    with monkeypatch.context() as fault:
        fault.setattr(box, "_connect", locked_store)
        service._consume_monitor()
        jobs.start(jobs.state["settings"])
        jobs.finish(300)
        jobs.state["source_revision"] = "test"
        jobs.rows = []
        service._load()["active_monitor_run"] = jobs.state["run_id"]
        service._load()["monitor_cursor"] = {}
        clock[0] += 6
        service._consume_monitor()
        assert not service._load()["signals"]
    clock[0] += 6
    service._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert not transport.messages and box.snapshot()["pending"] == 0


def test_real_sqlite_writer_lock_reports_unavailable_then_recovers(
    tmp_path, monkeypatch
):
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=Transport(), now=lambda: 1000
    )
    box.signal_events([event("confirmed")], since=0)
    connect = sqlite3.connect

    def bounded_connect(*args, **kwargs):
        return connect(*args, **{**kwargs, "timeout": 0.02})

    with connect(box.path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        try:
            with monkeypatch.context() as patch:
                patch.setattr(
                    dingtalk_notifications.sqlite3, "connect", bounded_connect
                )
                status = box.snapshot()
                assert (
                    status["storage_available"] is False and status["pending"] is None
                )
        finally:
            writer.rollback()
    box.deliver_one()
    assert (
        len(box.transport.messages) == 1 and box.snapshot()["storage_available"] is True
    )
