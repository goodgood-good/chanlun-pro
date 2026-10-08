"""An unrelated successful delivery is not a receipt for the latest signals."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
import time
from unittest.mock import Mock

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_key
from tests.web.test_dingtalk_notifications import event
from tests.web.test_monitor_control_ownership import monitor as monitor
from tests.web.test_monitor_recovery import scenario as scenario
from tests.web.test_notification_activity_health import quiet as quiet


def prepare(quiet, count=1):
    service, box, state = quiet
    stamp = service.now().timestamp()
    box.now = lambda: stamp + 10
    events = [event("confirmed", identity=f"signal-{i}", stamp=stamp) for i in range(count)]
    state.update(last_check_at=stamp, last_signal_event_at=stamp,
                 last_check_notifications=count, events=deepcopy(events),
                 last_check_event_refs=[[signal_key(e), e["recorded_at"]] for e in events])
    return service, box, state, events


def receipt(box, identity, events, *, kind="signals", status="sent", stamp=None):
    stamp = box.now() if stamp is None else stamp
    with box._connect() as db:
        db.execute("INSERT INTO outbox(id,kind,content,status,created,expires,sent) VALUES (?,?,?,?,?,?,?)",
                   (identity, kind, "isolated receipt", status, stamp - 1, stamp + 1800,
                    stamp if status == "sent" else None))
        db.execute("INSERT INTO signal_payloads VALUES (?,?)", (identity, json.dumps(events)))


@pytest.mark.parametrize("kind", ["screening", "chart", "other_signal", "old_occurrence", "other_stage"])
@pytest.mark.parametrize("paged", [False, True])
def test_later_unrelated_success_does_not_claim_current_event_was_sent(quiet, kind, paged):
    service, box, _state, events = prepare(quiet)
    payload = deepcopy(events)
    if kind == "other_signal":
        payload[0]["id"] = "different"
    elif kind == "old_occurrence":
        payload[0]["recorded_at"] -= 20
    elif kind == "other_stage":
        payload[0]["stage"] = "warning"
    receipt(box, "unrelated", payload, kind=kind if kind in {"screening", "chart"} else "signals")
    # The broad last-sent clock is still useful history, but is not proof.
    activity = service.snapshot(page_options={} if paged else None)["notification_activity"]
    assert activity["state"] == "recorded"


def test_partial_send_with_retired_member_is_reported_as_partial(quiet):
    service, box, _state, events = prepare(quiet, 2)
    receipt(box, "delivered", events[:1])
    receipt(box, "retired", events[1:], status="expired")
    activity = service.snapshot()["notification_activity"]
    assert activity["state"] == "partial"
    assert "1 / 2" in activity["message"]


@pytest.mark.parametrize("legacy", [False, True])
def test_all_matching_receipts_survive_restart_and_history_pagination(quiet, legacy):
    service, box, state, events = prepare(quiet, 2)
    receipt(box, "first", events[:1])
    receipt(box, "second", events[1:])
    for i in range(35):
        receipt(box, f"summary-{i}", [], kind="screening", stamp=box.now() + i + 1)
    if legacy:
        state.pop("last_check_event_refs")
    service._save()
    service._state = None
    restored = DingTalkOutbox(box.path, transport=box.transport, now=box.now)
    restored._thread = box._thread
    service._notifications = restored
    result = service.snapshot(page_options={})
    assert result["notification_activity"]["state"] == "sent"
    assert "last_check_event_refs" not in result


def test_incomplete_legacy_history_does_not_claim_all_events_sent(quiet):
    service, box, state, events = prepare(quiet, 2)
    state.pop("last_check_event_refs")
    state["events"] = events[:1]
    receipt(box, "first", events[:1])
    assert service.snapshot()["notification_activity"]["state"] == "recorded"


def test_saved_batch_references_work_after_event_history_is_trimmed(quiet):
    service, box, state, events = prepare(quiet, 2)
    state["events"] = []
    receipt(box, "all", events)
    assert service.snapshot()["notification_activity"]["state"] == "sent"


def test_receipt_read_failure_is_not_claimed_as_sent(quiet, monkeypatch):
    service, box, _state, events = prepare(quiet)
    receipt(box, "all", events)
    monkeypatch.setattr(box, "signal_receipts", lambda refs: {"available": False, "sent": None, "expected": len(refs)}, raising=False)
    activity = service.snapshot()["notification_activity"]
    assert activity["state"] == "recorded"
    assert "无法核对" in activity["message"]


def test_status_captures_event_batch_before_slow_delivery_read(quiet, monkeypatch):
    service, box, state, events = prepare(quiet)
    receipt(box, "old-batch", events)
    original = box.snapshot
    entered, release = threading.Event(), threading.Event()

    def slow_snapshot():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(box, "snapshot", slow_snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(service.snapshot)
        try:
            assert entered.wait(2)
            with service._lock:
                state.update(last_check_at=state["last_check_at"] + 20,
                             last_check_notifications=0, last_check_event_refs=[])
        finally:
            release.set()
        result = work.result(timeout=2)
    assert result["notification_activity"]["state"] == "sent"
    assert result["notification_activity"]["last_check_at"] == result["last_check_at"]


def test_receipt_lookup_reads_committed_rows_while_writer_is_reserved(quiet):
    _service, box, state, events = prepare(quiet)
    receipt(box, "sent", events)
    writer = sqlite3.connect(box.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE outbox SET status='expired'")
        result = box.signal_receipts(state["last_check_event_refs"])
        assert result["available"] and result["sent"] == 1
    finally:
        writer.close()


def test_malformed_unrelated_payload_does_not_hide_valid_receipt(quiet):
    _service, box, state, events = prepare(quiet)
    receipt(box, "sent", events)
    for i, payload in enumerate(("not json", "{}", "[null]", '[{"recorded_at":1}]')):
        receipt(box, f"bad-{i}", [])
        with box._connect() as db:
            db.execute("UPDATE signal_payloads SET events=? WHERE outbox_id=?", (payload, f"bad-{i}"))
    result = box.signal_receipts(state["last_check_event_refs"])
    assert result["available"] and result["sent"] == 1


def test_receipts_remain_unavailable_during_exclusive_lock_then_recover(quiet):
    _service, box, state, events = prepare(quiet)
    receipt(box, "sent", events)
    writer = sqlite3.connect(box.path)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert box.signal_receipts(state["last_check_event_refs"])["available"] is False
    finally:
        writer.close()
    assert box.signal_receipts(state["last_check_event_refs"])["sent"] == 1


def test_new_consolidation_cycle_does_not_reuse_old_cycle_receipt(quiet):
    _service, box, _state, events = prepare(quiet)
    old = {**events[0], "signal_type": "consolidation_divergence", "lifecycle_cycle": 0}
    new = {**old, "lifecycle_cycle": 1}
    receipt(box, "old-cycle", [old])
    assert box.signal_receipts([[signal_key(new), new["recorded_at"]]])["sent"] == 0


def test_ingestion_persists_complete_receipt_batch_beyond_history_limit(scenario):
    service, jobs, box, _transport, _clock, restart = scenario
    template = jobs.rows[0]
    jobs.rows = [{**deepcopy(template), "point": {**template["point"], "point_id": f"point-{i}"}}
                 for i in range(501)]
    service._consume_monitor()
    state = service._load()
    assert len(state["events"]) == 500
    assert state["last_check_notifications"] == len(state["last_check_event_refs"]) == 501
    restored = restart()
    assert restored._load()["last_check_event_refs"] == state["last_check_event_refs"]
    with box._connect() as db:
        db.execute("UPDATE outbox SET status='sent',sent=?", (box.now(),))
    restored._thread = Mock()
    restored._thread.is_alive.return_value = True
    box._thread = Mock()
    box._thread.is_alive.return_value = True
    restored._last_tick_at = restored.now().timestamp()
    restored._last_cycle_at = time.monotonic()
    assert restored.snapshot()["notification_activity"]["state"] == "sent"


def test_quiet_status_does_not_read_receipts(quiet, monkeypatch):
    service, box, _state = quiet
    def forbidden(_refs):
        raise AssertionError("quiet polling should not scan receipts")
    monkeypatch.setattr(box, "signal_receipts", forbidden, raising=False)
    assert service.snapshot()["notification_activity"]["state"] == "quiet"


def test_receipt_storage_probe_failure_is_reported(quiet, monkeypatch):
    _service, box, state, events = prepare(quiet)
    receipt(box, "sent", events)
    original = type(box.path).is_file
    def unavailable(path):
        if path == box.path:
            raise PermissionError("controlled receipt storage failure")
        return original(path)
    monkeypatch.setattr(type(box.path), "is_file", unavailable)
    assert box.signal_receipts(state["last_check_event_refs"])["available"] is False
