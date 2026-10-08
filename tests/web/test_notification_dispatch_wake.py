"""Wake at the remaining queue/cooldown deadline without relaxing send limits."""

from contextlib import contextmanager
import threading

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport, event


def capture_next_wait(box, *, enabled=True):
    done = threading.Event()
    waits = []

    class Wake:
        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout):
            waits.append(timeout)
            box._stop.set()
            done.set()

    box._wake = Wake()
    box.start(lambda: enabled)
    try:
        assert done.wait(1)
    finally:
        box.stop()
    assert not box._thread.is_alive()
    return waits[0]


@pytest.mark.parametrize("processing_delay", [0.0, 0.2])
def test_chart_or_signal_arriving_near_cooldown_end_does_not_wait_another_full_poll(
    tmp_path,
    processing_delay,
):
    clock, transport = [1000.0], Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.signal_events([event("confirmed", identity="first")], since=0)
    box.deliver_one()
    clock[0] = 1003.75
    box.signal_events([event("confirmed", identity="second", stamp=clock[0])], since=0)

    @contextmanager
    def slow_cycle(_phase):
        yield
        clock[0] += processing_delay

    box.progress.cycle = slow_cycle
    assert capture_next_wait(box) == pytest.approx(0.25 - processing_delay)
    assert len(transport.messages) == 1


def test_deferred_signal_wakes_at_its_retry_time(tmp_path):
    clock, transport = [1000.0], Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.signal_events([event("confirmed")], since=0)
    with box._connect() as db:
        db.execute("UPDATE outbox SET status='retry',next_try=1005")
    clock[0] = 1004.8
    assert capture_next_wait(box) == pytest.approx(0.2)
    assert not transport.messages


def test_send_still_waits_for_the_full_four_second_limit(tmp_path):
    clock, transport = [1000.0], Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.signal_events([event("confirmed", identity="first")], since=0)
    box.deliver_one()
    box.signal_events([event("confirmed", identity="second")], since=0)
    clock[0] = 1003.999
    box.deliver_one()
    assert len(transport.messages) == 1
    clock[0] = 1004.0
    box.deliver_one()
    assert len(transport.messages) == 2


def test_recent_persisted_send_lease_keeps_its_remaining_cooldown(tmp_path):
    clock, transport = [1000.0], Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )
    box.signal_events([event("confirmed", identity="interrupted")], since=0)
    with box._connect() as db:
        db.execute("UPDATE outbox SET status='sending',next_try=1030")
    clock[0] = 1003.75
    box.signal_events([event("confirmed", identity="new", stamp=clock[0])], since=0)
    assert capture_next_wait(box) == pytest.approx(0.25)
    assert not transport.messages


@pytest.mark.parametrize("enabled", [False, True])
def test_idle_or_disabled_worker_keeps_its_normal_idle_wait(tmp_path, enabled):
    transport = Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: 1000.0
    )
    if not enabled:
        box.signal_events([event("confirmed")], since=0)
    assert capture_next_wait(box, enabled=enabled) == 4
    assert not transport.messages
