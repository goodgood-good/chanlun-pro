"""Slow pause cleanup must leave state readable and finish before resume."""

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from tests.web.test_monitor_control_ownership import monitor as monitor


@pytest.mark.parametrize("phase", ["cancel", "release_workers"])
def test_slow_pause_cleanup_does_not_hold_state_lock(monitor, phase):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(5)

    getattr(live, phase).side_effect = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        pausing = pool.submit(service.pause)
        try:
            assert entered.wait(2)
            acquired = service._lock.acquire(timeout=0.15)
            if acquired:
                try:
                    assert state["enabled"] is False and state["force"] is False
                finally:
                    service._lock.release()
            assert acquired, "pause cleanup blocked monitor state access"
        finally:
            release.set()
        pausing.result(timeout=2)


@pytest.mark.parametrize("phase", ["cancel", "release_workers"])
def test_resume_waits_for_old_pause_cleanup_without_exposing_enabled_state(monitor, monkeypatch, phase):
    service, manual, live, state = monitor
    entered, release, resumed = threading.Event(), threading.Event(), threading.Event()
    order = []

    def slow():
        order.append("cleanup-start")
        entered.set()
        assert release.wait(5)
        order.append("cleanup-finish")

    def start():
        order.append("resume")
        resumed.set()

    getattr(live, phase).side_effect = slow
    monkeypatch.setattr(service, "start_runtime", start)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pausing = pool.submit(service.pause)
        try:
            assert entered.wait(2)
            enabling = pool.submit(service.enable, {})
            assert not resumed.wait(0.15), "old pause cleanup could cancel the resumed runtime"
            assert state["enabled"] is False
        finally:
            release.set()
        pausing.result(timeout=2)
        enabling.result(timeout=2)
    assert order == ["cleanup-start", "cleanup-finish", "resume"]
    assert state["enabled"] is True


@pytest.mark.parametrize("phase", ["cancel", "release_workers"])
def test_check_now_sees_paused_state_while_cleanup_is_waiting(monitor, phase):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(5)

    getattr(live, phase).side_effect = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        pausing = pool.submit(service.pause)
        try:
            assert entered.wait(2)
            checking = pool.submit(service.check_now)
            with pytest.raises(ValueError):
                checking.result(timeout=0.2)
        finally:
            release.set()
        pausing.result(timeout=2)
    assert state["force"] is False


@pytest.mark.parametrize("phase", ["cancel", "release_workers"])
def test_pause_cleanup_failure_does_not_strand_future_controls(monitor, phase):
    service, manual, live, state = monitor
    getattr(live, phase).side_effect = OSError("controlled pause cleanup failure")
    with pytest.raises(OSError, match="cleanup failure"):
        service.pause()
    assert state["enabled"] is False
    getattr(live, phase).side_effect = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(service.enable, {}).result(timeout=0.5)
    assert state["enabled"] is True
