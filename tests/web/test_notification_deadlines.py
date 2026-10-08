"""Deadlines belong to individual signals and hold at actual dispatch time."""

import json
from unittest.mock import Mock

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import event
from tests.web.test_notification_charts import ImageTransport, image_event, PNG
from tests.web.test_consolidation_messages import alert


@pytest.mark.parametrize("kind", ["signals", "chart", "combined"])
def test_signal_expiring_during_validation_is_not_sent(tmp_path, kind):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        charts=kind == "combined",
        signal_validator=lambda rows: (rows, []),
    )
    if kind == "signals":
        box.signal_events([event("confirmed")], since=0)
    elif kind == "chart":
        box.enqueue_chart(image_event(), "https://example.test/frozen.png")
    else:
        box.charts.renderer = lambda *_: PNG
        box.charts.check_public = False
        box.signal_events([alert()], since=0)
        box.charts.process_one()
    clock[0] = 2799

    def slow_validation(rows):
        clock[0] = 2801
        return rows, []

    box.signal_validator = slow_validation
    box.deliver_one()
    assert not transport.messages and not transport.images
    assert box.snapshot()["history"][0]["status"] == "expired"


def test_newer_member_is_not_expired_with_an_older_member_of_the_same_batch(tmp_path):
    clock = [2700.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    old = {**event("confirmed", identity="old", stamp=1000), "name": "old_member"}
    new = {**event("confirmed", identity="new", stamp=2700), "name": "new_member"}
    box.signal_events([old, new], since=0)
    clock[0] = 2801
    box.deliver_one()
    assert len(transport.messages) == 1
    assert (
        "new_member" in transport.messages[0]
        and "old_member" not in transport.messages[0]
    )
    with box._connect() as db:
        sent = json.loads(
            db.execute("SELECT events FROM signal_payloads").fetchone()[0]
        )
    assert [row["id"] for row in sent] == ["new"]


def test_split_pending_member_keeps_its_own_deadline_across_restart(tmp_path):
    clock = [2700.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (
            [row for row in rows if row["id"] == "old"],
            [row for row in rows if row["id"] == "new"],
        ),
    )
    rows = [
        {**event("confirmed", identity="old", stamp=1000), "name": "old_member"},
        {**event("confirmed", identity="new", stamp=2700), "name": "new_member"},
    ]
    box.signal_events(rows, since=0)
    box.deliver_one()
    assert len(transport.messages) == 1 and "old_member" in transport.messages[0]
    clock[0] = 2801
    restarted = DingTalkOutbox(
        box.path,
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda remaining: (remaining, []),
    )
    restarted.deliver_one()
    assert len(transport.messages) == 2 and "new_member" in transport.messages[1]
    assert "old_member" not in transport.messages[1]
    restarted.signal_events(rows, since=0)
    clock[0] += 5
    restarted.deliver_one()
    assert len(transport.messages) == 2


def test_mixed_batch_filters_a_member_expiring_during_validation(tmp_path):
    clock = [2799.0]
    transport = ImageTransport()

    def slow_validation(rows):
        clock[0] = 2801
        return rows, []

    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=slow_validation,
    )
    box.signal_events(
        [
            {**event("confirmed", identity="old", stamp=1000), "name": "old_member"},
            {**event("confirmed", identity="new", stamp=2700), "name": "new_member"},
        ],
        since=0,
    )
    box.deliver_one()
    assert len(transport.messages) == 1
    assert (
        "new_member" in transport.messages[0]
        and "old_member" not in transport.messages[0]
    )


def test_render_expiring_before_publication_does_not_publish_or_enqueue_an_old_card(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    box.charts.check_public = False
    box.signal_events([alert()], since=0)
    clock[0] = 2799

    def slow_render(*_):
        clock[0] = 2801
        return PNG

    box.charts.renderer = slow_render
    publish = Mock(wraps=box.charts.store.publish)
    monkeypatch.setattr(box.charts.store, "publish", publish)
    box.charts.process_one()
    publish.assert_not_called()
    assert box.snapshot()["history"][0]["status"] == "expired"


def test_newer_structure_cannot_extend_the_original_notification_deadline(tmp_path):
    clock = [2799.0]
    transport = ImageTransport()

    def refresh(rows):
        clock[0] = 2800
        return [
            {**row, "recorded_at": 2800, "source_closed_at": 2800} for row in rows
        ], []

    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=refresh,
    )
    box.signal_events([event("confirmed", stamp=1000)], since=0)
    box.deliver_one()
    assert (
        not transport.messages and box.snapshot()["history"][0]["status"] == "expired"
    )


def test_final_dispatch_gate_prunes_events_expiring_after_validity_check(
    tmp_path, monkeypatch
):
    clock = [2799.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    box.signal_events(
        [
            {**event("confirmed", identity="old", stamp=1000), "name": "old_member"},
            {**event("confirmed", identity="new", stamp=2700), "name": "new_member"},
        ],
        since=0,
    )
    formatter = box._signal_content

    def delayed_format(rows, stamp):
        text = formatter(rows, stamp)
        clock[0] = 2801
        return text

    monkeypatch.setattr(box, "_signal_content", delayed_format)
    box.deliver_one()
    assert len(transport.messages) == 1
    assert (
        "new_member" in transport.messages[0]
        and "old_member" not in transport.messages[0]
    )


@pytest.mark.parametrize("status", ["pending", "retry", "expired"])
def test_legacy_batch_migration_preserves_current_members_without_reopening_terminal_work(
    tmp_path, status
):
    clock = [2700.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.signal_events(
        [
            {**event("confirmed", identity="old", stamp=1000), "name": "old_member"},
            {**event("confirmed", identity="new", stamp=2700), "name": "new_member"},
        ],
        since=0,
    )
    with box._connect() as db:
        db.execute("DELETE FROM delivery_settings WHERE key='per_signal_deadlines'")
        db.execute("UPDATE outbox SET status=?,expires=2800", (status,))
    clock[0] = 2801
    restarted = DingTalkOutbox(
        box.path,
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    restarted.deliver_one()
    if status == "expired":
        assert not transport.messages
    else:
        assert len(transport.messages) == 1
        assert (
            "new_member" in transport.messages[0]
            and "old_member" not in transport.messages[0]
        )


def test_request_started_before_deadline_keeps_successful_receipt_after_deadline(
    tmp_path, monkeypatch
):
    clock = [2799.0]
    transport = ImageTransport()
    send = transport.send

    def delayed_receipt(text):
        send(text)
        clock[0] = 2801

    monkeypatch.setattr(transport, "send", delayed_receipt)
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    box.signal_events([event("confirmed", stamp=1000)], since=0)
    box.deliver_one()
    assert (
        len(transport.messages) == 1
        and box.snapshot()["history"][0]["status"] == "sent"
    )


def test_slow_final_commit_cannot_send_expired_members_or_lose_newer_members(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    clock = [2799.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    box.signal_events(
        [
            {**event("confirmed", identity="old", stamp=1000), "name": "old_member"},
            {**event("confirmed", identity="new", stamp=2700), "name": "new_member"},
        ],
        since=0,
    )
    connect = box._connect
    delayed = []

    @contextmanager
    def blocked_commit():
        with connect() as db:
            yield db
            started = db.execute("SELECT send_started FROM outbox").fetchone()[0]
        if started is not None and not delayed:
            clock[0] = 2801
            delayed.append(True)

    monkeypatch.setattr(box, "_connect", blocked_commit)
    box.deliver_one()
    assert delayed and not transport.messages
    assert box.snapshot()["history"][0]["status"] == "retry"
    clock[0] += 4
    box.deliver_one()
    assert len(transport.messages) == 1
    assert (
        "new_member" in transport.messages[0]
        and "old_member" not in transport.messages[0]
    )
