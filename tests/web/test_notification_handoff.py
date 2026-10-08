"""Durable handoff is independent of the monitor's bounded display history."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import sqlite3
from unittest.mock import Mock

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from cl_app.services import signal_monitor
from cl_app.services.notification_handoff import retain
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_monitor_recovery import scenario as scenario
from tests.web.test_signal_monitor import Jobs
from tests.web.test_dingtalk_notifications import Transport, event


def test_outage_restart_keeps_all_notifications_beyond_the_500_event_page_history(
    scenario, monkeypatch
):
    service, jobs, box, _transport, clock, restart = scenario
    template = jobs.rows[0]
    jobs.rows = [
        {**deepcopy(template), "point": {**template["point"], "point_id": f"point-{i}"}}
        for i in range(501)
    ]
    with monkeypatch.context() as fault:
        fault.setattr(
            box,
            "signal_events",
            Mock(side_effect=sqlite3.OperationalError("isolated outage")),
        )
        service._consume_monitor()
    assert len(service._load()["events"]) == 500
    visible = service.snapshot()
    assert visible["dingtalk"]["awaiting_enqueue"] == 501
    assert "pending_notifications" not in visible
    recovered = restart()
    recovered._publish_notifications({"status": "idle"})
    with box._connect() as db:
        queued = [
            e
            for row in db.execute("SELECT events FROM signal_payloads")
            for e in json.loads(row[0])
        ]
    assert len({e["id"] for e in queued}) == 501
    assert not recovered._load()["pending_notifications"]
    recovered._publish_notifications({"status": "idle"})
    with box._connect() as db:
        repeated = [
            e
            for row in db.execute("SELECT events FROM signal_payloads")
            for e in json.loads(row[0])
        ]
    assert len(repeated) == 501


def test_crash_after_durable_enqueue_before_ack_retries_without_duplicate_dispatch(
    scenario, monkeypatch
):
    service, _jobs, box, transport, clock, restart = scenario
    save = service._save
    calls = []

    def crash_on_ack():
        calls.append(1)
        if len(calls) == 2:
            raise SystemExit("isolated crash before handoff acknowledgment")
        return save()

    with monkeypatch.context() as fault:
        fault.setattr(service, "_save", crash_on_ack)
        with pytest.raises(SystemExit):
            service._consume_monitor()
    assert box.snapshot()["pending"] == 1
    recovered = restart()
    assert len(recovered._load()["pending_notifications"]) == 1
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    clock[0] += 5
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert (
        len(transport.messages) == 1 and not recovered._load()["pending_notifications"]
    )


def test_notification_switch_retires_unqueued_events_instead_of_replaying_them(
    scenario, monkeypatch
):
    service, _jobs, box, transport, clock, _ = scenario
    with monkeypatch.context() as fault:
        fault.setattr(
            box,
            "signal_events",
            Mock(side_effect=sqlite3.OperationalError("isolated outage")),
        )
        service._consume_monitor()
    assert service._load()["pending_notifications"]
    monkeypatch.setattr(service, "start_runtime", lambda: None)
    service.configure_notifications({"enabled": False})
    assert not service._load()["pending_notifications"]
    clock[0] += 6
    service.configure_notifications({"enabled": True})
    service._enqueue_pending_notifications()
    box.deliver_one()
    assert not transport.messages and not service._load()["pending_notifications"]


def test_expired_handoff_is_removed_without_creating_a_delivery(scenario, monkeypatch):
    service, _jobs, box, transport, clock, restart = scenario
    with monkeypatch.context() as fault:
        fault.setattr(
            box,
            "signal_events",
            Mock(side_effect=sqlite3.OperationalError("isolated outage")),
        )
        service._consume_monitor()
    clock[0] += 1801
    recovered = restart()
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert not transport.messages and not recovered._load()["pending_notifications"]
    assert not box.snapshot()["pending"]


def test_unknown_holding_membership_defers_handoff_without_losing_the_event(scenario):
    service, _jobs, box, _transport, clock, _ = scenario
    pending = {
        **event("warning", stamp=clock[0]),
        "code": "SH.600000",
        "signal_type": "consolidation_divergence",
        "point_type": "consolidation_up",
    }
    state = service._load()
    state.update(
        pending_notifications={"old_key": pending},
        holding_source_current=False,
        holding_symbols=["a:SH.600000"],
    )
    service._save()
    service._enqueue_pending_notifications()
    assert (
        len(service._load()["pending_notifications"]) == 1
        and box.snapshot()["pending"] == 0
    )
    state["holding_source_current"] = True
    service._enqueue_pending_notifications()
    assert (
        not service._load()["pending_notifications"] and box.snapshot()["pending"] == 1
    )


def test_legacy_state_migrates_recent_events_once_and_keeps_outbox_deduplication(
    scenario,
):
    service, _jobs, box, transport, clock, restart = scenario
    service._consume_monitor()
    state = service._load()
    state.pop("pending_notifications")
    service._save()
    recovered = restart()
    assert recovered._load()["pending_notifications"]
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    clock[0] += 5
    recovered = restart()
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert (
        len(transport.messages) == 1 and not recovered._load()["pending_notifications"]
    )


def test_acknowledgment_does_not_remove_an_event_added_during_enqueue(
    scenario, monkeypatch
):
    service, _jobs, box, _transport, clock, _ = scenario
    original = box.signal_events
    newer = {
        **event("confirmed", identity="newer", stamp=clock[0]),
        "code": "SH.600000",
    }

    def enqueue_and_add(events, **kwargs):
        original(events, **kwargs)
        with service._lock:
            retain(service._load(), clock[0], [newer])
            service._save()

    with monkeypatch.context() as race:
        race.setattr(box, "signal_events", enqueue_and_add)
        service._consume_monitor()
    assert [e["id"] for e in service._load()["pending_notifications"].values()] == [
        "newer"
    ]
    service._enqueue_pending_notifications()
    assert not service._load()["pending_notifications"]
    with box._connect() as db:
        queued = [
            e
            for row in db.execute("SELECT events FROM signal_payloads")
            for e in json.loads(row[0])
        ]
    assert len(queued) == 2 and sum(e["id"] == "newer" for e in queued) == 1


def test_ack_write_failure_recovers_from_durable_state_without_losing_or_repeating(
    scenario, monkeypatch
):
    service, _jobs, box, transport, clock, restart = scenario
    original = signal_monitor.write_json
    writes = []

    def fail_ack(*args, **kwargs):
        writes.append(1)
        if len(writes) == 3:
            raise OSError("isolated acknowledgment write failure")
        return original(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(signal_monitor, "write_json", fail_ack)
        service._consume_monitor()
    recovered = restart()
    assert len(recovered._load()["pending_notifications"]) == 1
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    clock[0] += 5
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert (
        not recovered._load()["pending_notifications"] and len(transport.messages) == 1
    )


def test_unqueued_frozen_evidence_is_not_pruned_when_the_signal_gets_a_newer_snapshot(
    tmp_path,
):
    clock = [1000.0]
    jobs = Jobs(tmp_path / "runs")
    jobs.root.mkdir()
    paths = []
    for i in range(6):
        path = jobs.root / f"{i:032x}"
        path.mkdir()
        (path / "status.json").write_text(
            json.dumps({"run_id": path.name, "status": "completed"}), encoding="utf-8"
        )
        os.utime(path, (100 + i, 100 + i))
        paths.append(path)
    box = DingTalkOutbox(
        tmp_path / "new-outbox.sqlite", transport=Transport(), now=lambda: clock[0]
    )
    service = SignalMonitor(
        tmp_path,
        screening=jobs,
        monitor=jobs,
        notifications=box,
        now=lambda: datetime.fromtimestamp(clock[0], timezone.utc),
    )
    pending = {
        **event("invalidated"),
        "signal_type": "consolidation_divergence",
        "point_type": "consolidation_up",
        "evidence": {"status": "complete"},
        "evidence_run_id": paths[0].name,
        "invalidation_reference": {"evidence_run_id": paths[1].name},
    }
    state = service._load()
    state.update(
        dingtalk_enabled=True,
        notification_since=0,
        pending_notifications={"pending": pending},
    )
    state["signals"] = {
        "current": {
            **pending,
            "evidence_run_id": paths[5].name,
            "invalidation_reference": None,
        }
    }
    service._save()
    service._prune_runs()
    assert paths[0].exists() and paths[1].exists(), (
        "both image sources must survive until handoff"
    )
    assert not paths[2].exists()
    clock[0] += 1801
    service._publish_notifications({"status": "idle"})
    service._prune_runs()
    assert not paths[0].exists() and not paths[1].exists()
