"""Yielding background work must not hold the shared monitor state lock."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
from unittest.mock import Mock

import pytest

from tests.web.test_monitor_live_priority import mixed as mixed, background


def prepare(mixed):
    service, live, clock, closed = background(mixed)
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    return service, live, clock


def test_slow_background_cancel_leaves_state_readable(mixed, monkeypatch):
    service, live, clock = prepare(mixed)
    entered, release = threading.Event(), threading.Event()
    original = live.cancel

    def slow():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(live, "cancel", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        scheduling = pool.submit(service._schedule_monitor, clock[0], None)
        try:
            assert entered.wait(2)
            acquired = service._lock.acquire(timeout=0.2)
            if acquired:
                try:
                    state = service._load()
                    assert state["yielding_monitor_run"] == state["active_monitor_run"]
                finally:
                    service._lock.release()
            assert acquired, "background cancel held the shared state lock"
        finally:
            release.set()
        scheduling.result(timeout=2)
    assert live.state["cancel_requested"]


def test_check_now_can_commit_while_background_cancel_is_waiting(mixed, monkeypatch):
    service, live, clock = prepare(mixed)
    entered, release = threading.Event(), threading.Event()
    original = live.cancel

    def slow():
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(live, "cancel", slow)
    monkeypatch.setattr(service, "_control_snapshot", lambda _: {"force": service._load()["force"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduling = pool.submit(service._schedule_monitor, clock[0], None)
        try:
            assert entered.wait(2)
            assert pool.submit(service.check_now).result(timeout=0.3) == {"force": True}
        finally:
            release.set()
        scheduling.result(timeout=2)
    assert service._load()["force"] is True


def test_enable_finishes_after_old_background_cancellation(mixed, monkeypatch):
    service, live, clock = prepare(mixed)
    entered, release, enabled = threading.Event(), threading.Event(), threading.Event()
    order = []
    original = live.cancel

    def slow():
        order.append("cancel-start")
        entered.set()
        assert release.wait(5)
        original()
        order.append("cancel-finish")

    def start():
        order.append("enable")
        enabled.set()

    monkeypatch.setattr(live, "cancel", slow)
    monkeypatch.setattr(service, "start_runtime", start)
    monkeypatch.setattr(service, "_control_snapshot", lambda _: {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduling = pool.submit(service._schedule_monitor, clock[0], None)
        try:
            assert entered.wait(2)
            enabling = pool.submit(service.enable, {})
            assert not enabled.wait(0.2)
        finally:
            release.set()
        scheduling.result(timeout=2)
        enabling.result(timeout=2)
    assert order == ["cancel-start", "cancel-finish", "enable"]


@pytest.mark.parametrize("change", ["pool", "empty_pool", "run", "pause_resume"])
def test_retired_status_read_cannot_cancel_replacement_work(mixed, monkeypatch, change):
    service, live, clock = prepare(mixed)
    entered, release = threading.Event(), threading.Event()
    original = live.status
    cancel = Mock()

    def stale_status():
        result = original()
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(live, "status", stale_status)
    monkeypatch.setattr(live, "cancel", cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        scheduling = pool.submit(service._schedule_monitor, clock[0], None)
        try:
            assert entered.wait(2)
            with service._lock:
                state = service._load()
                if change == "pool":
                    state["seed"]["run_id"] = "replacement"
                elif change == "empty_pool":
                    state["seed"]["codes"] = []
                elif change == "run":
                    state["active_monitor_run"] = "replacement"
                else:
                    service._control_version += 2
        finally:
            release.set()
        scheduling.result(timeout=2)
    cancel.assert_not_called()
    assert not service._load().get("yielding_monitor_run")
