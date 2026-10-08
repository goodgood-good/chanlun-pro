"""A control change retires scheduler work that was reading an older context."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import threading
from unittest.mock import Mock

import pytest

from chanlun.screening.rules import CN
from cl_app.services import signal_monitor
from cl_app.services.screening import validate_settings


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    manual, live, notifications = Mock(), Mock(), Mock()
    manual.root, live.root = tmp_path / "manual", tmp_path / "runs"
    manual.status.return_value = {"status": "idle"}
    live.status.return_value = {"status": "idle"}
    manual.start.return_value = {"run_id": "b" * 32, "status": "running"}
    live.start.return_value = {"run_id": "c" * 32, "status": "running"}
    service = signal_monitor.SignalMonitor(tmp_path, screening=manual, monitor=live,
                                          notifications=notifications,
                                          now=lambda: datetime(2026, 9, 30, 10, 0, tzinfo=CN))
    state = service._load()
    symbol = {"market": "a", "code": "SH.600000", "name": "existing"}
    settings = validate_settings({"scope": "symbols", "symbols": [symbol], "frequencies": ["5m"]})
    state.update(enabled=True, holding_source_current=True, seed={
        "run_id": "previous", "codes": ["a:SH.600000"], "symbols": [symbol],
        "screening_symbols": [symbol], "settings": settings,
    })
    monkeypatch.setattr(service, "start_runtime", lambda: None)
    monkeypatch.setattr(service, "_control_snapshot", lambda _: {"enabled": service._load()["enabled"]})
    monkeypatch.setattr(signal_monitor, "holding_symbols", lambda: [])
    monkeypatch.setattr(service, "_publish_notifications", lambda *args: None)
    monkeypatch.setattr(service, "_consume_monitor", lambda: None)
    return service, manual, live, state


@pytest.mark.parametrize("control", ["pause", "settings", "resume"])
@pytest.mark.parametrize("failed", [False, True])
def test_late_watchlist_read_does_not_overwrite_a_new_control_context(monitor, monkeypatch, control, failed):
    service, manual, live, state = monitor
    state["signals"]["warning"] = {"signal_type": "consolidation_divergence", "market": "a", "code": "SH.600000", "frequency": "5m"}
    entered, release = threading.Event(), threading.Event()
    before_seed = deepcopy(state["seed"])

    def watch():
        entered.set()
        assert release.wait(5)
        if failed:
            raise OSError("controlled old watchlist failure")
        return [{"market": "a", "code": "SH.600001", "name": "old lookup"}]

    monkeypatch.setattr(signal_monitor, "watchlist_symbols", watch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(service._sync_watchlist)
        try:
            assert entered.wait(2)
            if control == "pause":
                service.pause()
            elif control == "settings":
                service.enable({"interval_seconds": 120})
            else:
                service.pause()
                service.enable({})
        finally:
            release.set()
        reading.result(timeout=2)
    assert state["seed"] == before_seed, "old lookup changed the current pool"
    assert state["holding_source_current"] is True
    assert "warning" in state["signals"] and not state.get("watchlist_error")
    if control != "pause":
        monkeypatch.setattr(signal_monitor, "watchlist_symbols", lambda: [{"market": "a", "code": "SH.600002", "name": "new lookup"}])
        service._sync_watchlist()
        assert "a:SH.600002" in state["seed"]["codes"]
        assert "a:SH.600001" not in state["seed"]["codes"]


@pytest.mark.parametrize("phase", ["status", "results"])
@pytest.mark.parametrize("control", ["pause", "settings", "resume"])
def test_control_during_screening_read_does_not_install_a_late_pool(monitor, monkeypatch, phase, control):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()
    before_seed = deepcopy(state["seed"])
    settings = deepcopy(before_seed["settings"])
    result = {"status": "completed", "source_current": True, "run_id": "new-pool",
              "settings": settings, "selected": [{"market": "a", "code": "SH.600003", "frequency": "5m"}],
              "errors": [], "verified_periods": [{"market": "a", "code": "SH.600003", "frequency": "5m", "source_closed_at": 300}]}

    def read(name):
        if name == phase:
            entered.set()
            assert release.wait(5)
        return deepcopy(result)

    manual.status.side_effect = lambda: read("status")
    monkeypatch.setattr(service, "_screening_results_for_monitor", lambda *_: read("results"))
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(service.tick)
        try:
            assert entered.wait(2)
            if control == "settings":
                service.enable({"interval_seconds": 120})
            else:
                service.pause()
                if control == "resume":
                    service.enable({})
        finally:
            release.set()
        tick.result(timeout=2)
    assert state["enabled"] is (control != "pause")
    assert state["seed"] == before_seed, "a completed read replaced the pool after pause"
    live.start.assert_not_called()


def test_pause_while_status_is_read_prevents_new_monitor_launch(monitor, monkeypatch):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        assert release.wait(5)
        return {"status": "idle"}

    manual.status.side_effect = status
    with ThreadPoolExecutor(max_workers=1) as pool:
        scheduling = pool.submit(service._schedule_monitor, service.now(), 300)
        try:
            assert entered.wait(2)
            service.pause()
        finally:
            release.set()
        scheduling.result(timeout=2)
    live.start.assert_not_called()


def test_pause_and_resume_while_status_is_read_retires_old_monitor_launch(monitor):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()

    def status():
        entered.set()
        assert release.wait(5)
        return {"status": "idle"}

    manual.status.side_effect = status
    with ThreadPoolExecutor(max_workers=1) as pool:
        scheduling = pool.submit(service._schedule_monitor, service.now(), 300)
        try:
            assert entered.wait(2)
            service.pause()
            service.enable({})
        finally:
            release.set()
        scheduling.result(timeout=2)
    assert state["enabled"] is True
    live.start.assert_not_called()


def test_pause_and_resume_before_daily_dispatch_retires_the_old_tick(monitor, monkeypatch):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()
    observed = datetime(2026, 9, 30, 15, 11, tzinfo=CN)
    monkeypatch.setattr(service, "now", lambda: observed)
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: int(observed.replace(hour=15, minute=0).timestamp()))
    original = service._schedule_daily

    def dispatch(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service, "_schedule_daily", dispatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(service.tick)
        try:
            assert entered.wait(2)
            service.pause()
            service.enable({})
        finally:
            release.set()
        tick.result(timeout=2)
    manual.start.assert_not_called()
    live.start.assert_not_called()


def test_old_calendar_failure_cannot_replace_new_control_state(monitor, monkeypatch):
    service, manual, live, state = monitor
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)

    def clock(*args):
        entered.set()
        assert release.wait(5)
        raise ValueError("old calendar read failure")

    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(service.tick)
        try:
            assert entered.wait(2)
            service.enable({"interval_seconds": 120})
        finally:
            release.set()
        tick.result(timeout=2)
    assert not state.get("daily_schedule_error") and not state.get("schedule_errors")
    manual.start.assert_not_called()
    live.start.assert_not_called()
