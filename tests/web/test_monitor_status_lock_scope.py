"""Job status I/O must not hold the monitor state lock or defeat controls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import threading

import pytest

from chanlun.screening.rules import CN
from tests.web.test_monitor_control_ownership import monitor as monitor


def schedule(service, manual, kind):
    if kind == "daily":
        now = datetime(2026, 9, 30, 15, 11, tzinfo=CN)
        return lambda: service._schedule_daily(now, int(now.replace(hour=15, minute=0).timestamp()), {"status": "idle"})
    return lambda: service._schedule_monitor(service.now(), 300)


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_blocked_job_status_leaves_monitor_state_readable(monitor, kind):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        assert release.wait(5)
        return {"status": "idle"}

    live.status.side_effect = status
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            acquired = service._lock.acquire(timeout=0.15)
            if acquired:
                service._lock.release()
            assert acquired, "status I/O blocked all monitor state access"
        finally:
            release.set()
        reading.result(timeout=2)


@pytest.mark.parametrize("kind", ["daily", "live"])
@pytest.mark.parametrize("control", ["pause", "settings", "resume"])
def test_control_can_finish_while_status_is_slow_and_retires_old_dispatch(monitor, kind, control):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        assert release.wait(5)
        return {"status": "idle"}

    def change():
        if control == "settings":
            return service.enable({"interval_seconds": 120})
        service.pause()
        if control == "resume":
            return service.enable({})

    live.status.side_effect = status
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            changing = pool.submit(change)
            # Assert completion while status remains blocked, allowing time for
            # durable state writes and thread scheduling on a loaded machine.
            changing.result(timeout=2)
        finally:
            release.set()
        reading.result(timeout=2)
    manual.start.assert_not_called()
    live.start.assert_not_called()


@pytest.mark.parametrize("kind,change", [("daily", "run"), ("live", "run"),
                                         ("daily", "phase"), ("live", "processed"), ("live", "seed")])
def test_new_run_ownership_invalidates_a_stale_idle_status(monitor, kind, change):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        assert release.wait(5)
        return {"status": "idle"}

    live.status.side_effect = status
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            acquired = service._lock.acquire(timeout=0.15)
            assert acquired, "cannot record new ownership while the old status read is active"
            try:
                if change == "run":
                    state["active_monitor_run"] = "a" * 32
                elif change == "phase":
                    state["daily"]["afternoon"] = {"run_id": "b" * 32, "status": "started", "attempts": 1}
                elif change == "processed":
                    state["processed_run"] = "b" * 32
                else:
                    state["seed"]["run_id"] = "new-pool"
                expected = deepcopy(state)
            finally:
                service._lock.release()
        finally:
            release.set()
        reading.result(timeout=2)
    assert state == expected
    manual.start.assert_not_called()
    live.start.assert_not_called()
