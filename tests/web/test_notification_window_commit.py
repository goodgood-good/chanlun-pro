"""A canceled dispatch cannot create deferred work after a switch change."""

import json
import threading

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport, event


@pytest.mark.parametrize("pause_at", ["validation", "formatting"])
def test_switch_during_partial_dispatch_does_not_create_stale_retry_or_delay_new_signal(
    tmp_path, pause_at, monkeypatch
):
    clock = [1000.0]
    transport = Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    events = [event("confirmed", identity=name) for name in ("ready", "waiting")]
    box.signal_events(events, since=0)
    entered, resume = threading.Event(), threading.Event()
    failures = []

    def pause():
        entered.set()
        assert resume.wait(5), "test did not release the paused dispatch"

    def validate(rows):
        if pause_at == "validation":
            pause()
        return rows[:1], rows[1:]

    original_content = box._signal_content

    def format_content(rows, stamp):
        if pause_at == "formatting":
            pause()
        return original_content(rows, stamp)

    box.signal_validator = validate
    monkeypatch.setattr(box, "_signal_content", format_content)

    def deliver():
        try:
            box.deliver_one()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=deliver)
    worker.start()
    try:
        assert entered.wait(5), "dispatch never reached the selected boundary"
        clock[0] += 1
        box.begin_window(clock[0])
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive() and not failures
    monkeypatch.setattr(box, "_signal_content", original_content)
    assert box.snapshot()["pending"] == 0, "the old dispatch recreated a canceled retry"
    with box._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "expired"
        stored = json.loads(db.execute("SELECT events FROM signal_payloads").fetchone()[0])
    assert stored == events, "a canceled dispatch overwrote its historical payload"
    assert not transport.messages

    # A newly enabled window must not wait behind a stale split batch or lease.
    clock[0] += 0.1
    box.signal_validator = lambda rows: (rows, [])
    fresh = {**event("confirmed", identity="fresh", stamp=clock[0]), "name": "fresh"}
    box.signal_events([fresh], since=clock[0])
    box.deliver_one()
    assert len(transport.messages) == 1 and "fresh" in transport.messages[0]
    assert box.snapshot()["pending"] == 0


@pytest.mark.parametrize("verdict", ["valid", "pending", "removed"])
def test_switch_during_validation_preserves_the_canceled_receipt(tmp_path, verdict):
    clock = [1000.0]
    transport = Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    original = event("confirmed")
    box.signal_events([original], since=0)

    def validate(rows):
        clock[0] += 1
        box.begin_window(clock[0])
        updated = [{**rows[0], "latest_close": 15, "source_closed_at": clock[0]}]
        return (updated, []) if verdict == "valid" else ([], updated if verdict == "pending" else [])

    box.signal_validator = validate
    box.deliver_one()
    with box._connect() as db:
        receipt = dict(db.execute("SELECT status,content,expires,error FROM outbox").fetchone())
        stored = json.loads(db.execute("SELECT events FROM signal_payloads").fetchone()[0])
    assert receipt["status"] == "expired" and receipt["error"]
    assert stored == [original], "late validation changed the canceled event evidence"
    assert receipt["content"] == box._signal_content([original], 1000)
    assert box.snapshot()["pending"] == 0 and not transport.messages


@pytest.mark.parametrize("verdict", ["valid", "pending"])
def test_old_version_deferred_batch_is_retired_instead_of_retried_forever(tmp_path, verdict):
    clock = [1001.0]
    transport = Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.begin_window(clock[0])
    original = event("confirmed")
    # Older code could insert this after begin_window had canceled existing work.
    with box._connect() as db:
        box._insert(db, "old-deferred-batch", "signals", "old", 1000, 1800)
        db.execute("INSERT INTO signal_payloads VALUES (?,?)", ("old-deferred-batch", json.dumps([original])))
    box.signal_validator = lambda rows: (rows, []) if verdict == "valid" else ([], rows)
    box.deliver_one()
    assert box.snapshot()["pending"] == 0 and not transport.messages
    with box._connect() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "expired"
    assert box.deliver_one() is None
