"""Leaving an opportunity preserves its known origin and records the real exit."""
from copy import deepcopy
from datetime import timedelta

from tests.web.test_signal_monitor import setup as setup, _seed, signal


def test_exit_and_reentry_preserve_first_detection_and_use_the_actual_transition_time(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service._consume_monitor()
    first = deepcopy(next(iter(service._load()["signals"].values())))
    identity = first["id"]
    first_time = first["first_detected_at"]
    assert first_time == int(clock[0].timestamp())

    clock[0] += timedelta(seconds=10)
    service.check_now()
    service.tick()
    live.rows = []
    live.finish(cutoff)
    service._consume_monitor()
    exit_time = int(clock[0].timestamp())
    history = service._load()["known"][identity]
    assert history["stage"] == "left" and history["first_detected_at"] == first_time
    assert history["state_changed_at"] == exit_time
    assert "notification_state" not in history
    left = next(e for e in service._load()["events"] if e["id"] == identity and e["stage"] == "left")
    assert left["first_detected_at"] == first_time and left["state_changed_at"] == exit_time

    clock[0] += timedelta(seconds=10)
    service.check_now()
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service._consume_monitor()
    current = service._load()["signals"][identity]
    assert current["first_detected_at"] == first_time
    assert current["state_changed_at"] == int(clock[0].timestamp())
    latest = service._load()["events"][0]
    assert latest["id"] == identity and latest["change"] == "changed"
