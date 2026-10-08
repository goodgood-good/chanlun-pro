import json

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, image_event, PNG


@pytest.mark.parametrize("payload", ["{", "null", "{}", "[null]"])
def test_one_bad_recovery_payload_does_not_stop_a_later_valid_chart(tmp_path, payload):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    rendered = []
    box.charts.renderer = lambda row, _: rendered.append(row["id"]) or PNG
    box.charts.check_public = False
    with box._connect() as db:
        for identity, event_data in [
            ("broken", payload),
            ("healthy", json.dumps([image_event(identity="healthy")])),
        ]:
            box._insert(
                db, identity, "signals", "previously delivered text", 1000, 1800
            )
            db.execute(
                "UPDATE outbox SET status='sent',sent=1000 WHERE id=?", (identity,)
            )
            db.execute(
                "INSERT INTO signal_payloads VALUES (?,?)", (identity, event_data)
            )
    box.charts.recover()
    box.charts.process_one()
    assert rendered == ["healthy"]
    clock[0] += 5
    box.deliver_one()
    assert len(transport.images) == 1


def test_bad_chart_timestamp_does_not_terminate_processing_of_other_jobs(tmp_path):
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: 1000,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    rendered = []
    box.charts.renderer = lambda row, _: rendered.append(row["id"]) or PNG
    box.charts.check_public = False
    with box.charts._db() as db:
        db.execute(
            "INSERT INTO jobs(id,event,status,created) VALUES ('broken',?,'pending',999)",
            (json.dumps({**image_event(), "recorded_at": None}),),
        )
    box.charts.enqueue([image_event(identity="healthy")], 1000)
    box.charts.process_one()
    box.charts.process_one()
    assert rendered == ["healthy"]


def test_valid_events_in_a_partly_damaged_delivered_batch_still_get_their_charts(
    tmp_path,
):
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: 1000,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    rendered = []
    box.charts.renderer = lambda row, _: rendered.append(row["id"]) or PNG
    box.charts.check_public = False
    with box._connect() as db:
        box._insert(db, "partly-damaged", "signals", "delivered text", 1000, 1800)
        db.execute(
            "UPDATE outbox SET status='sent',sent=1000 WHERE id='partly-damaged'"
        )
        db.execute(
            "INSERT INTO signal_payloads VALUES (?,?)",
            ("partly-damaged", json.dumps([None, image_event(identity="healthy")])),
        )
    box.charts.recover()
    box.charts.process_one()
    assert rendered == ["healthy"]
    with box._connect() as db:
        state = db.execute(
            "SELECT status,error FROM outbox WHERE id='partly-damaged'"
        ).fetchone()
    assert state["status"] == "sent" and state["error"], (
        "keep the real text receipt and the image diagnostic"
    )
    box.charts.recover()
    box.charts.process_one()
    assert rendered == ["healthy"]
    assert box.charts.snapshot()["failed"] == 1, (
        "repeated reads cannot duplicate quarantine records"
    )


@pytest.mark.parametrize("damage", ["json", "timestamp", "identity"])
def test_corrupt_image_job_cannot_leave_a_combined_message_waiting_after_restart(
    tmp_path, damage
):
    from tests.web.test_consolidation_messages import alert

    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: 1000,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    row = alert()
    box.signal_events([row], since=0)
    with box.charts._db() as db:
        job = db.execute("SELECT event FROM jobs").fetchone()
        data = json.loads(job[0])
        if damage == "timestamp":
            data["recorded_at"] = None
        if damage == "identity":
            data["id"] = "wrong-event"
        packed = "{" if damage == "json" else json.dumps(data)
        db.execute("UPDATE jobs SET event=?", (packed,))
    box.charts.process_one()
    restored = DingTalkOutbox(
        box.path,
        transport=transport,
        now=lambda: 1000,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    restored.charts.recover()
    assert restored.snapshot()["pending"] == 0 and restored.snapshot()["failed"] == 1
    assert not transport.messages and not transport.images


def test_a_malformed_pending_combined_payload_is_failed_without_touching_the_next_event(
    tmp_path,
):
    from tests.web.test_consolidation_messages import alert

    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    box.charts.renderer = lambda *_: PNG
    box.charts.check_public = False
    box.signal_events([alert()], since=0)
    with box._connect() as db:
        db.execute("UPDATE signal_payloads SET events='[null]'")
    # A previously queued chart cannot resurrect its malformed parent message.
    box.charts.recover()
    box.charts.process_one()
    assert box.snapshot()["failed"] == 1 and box.snapshot()["pending"] == 0
    clock[0] += 5
    box.signal_events([{**alert(stamp=clock[0]), "id": "healthy"}], since=0)
    box.charts.recover()
    box.charts.process_one()
    box.deliver_one()
    assert len(transport.images) == 1
