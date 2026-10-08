"""Startup failures must back off without stopping independent live monitoring."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chanlun.screening.rules import CN
from cl_app.services import signal_monitor
from tests.web.test_monitor_control_ownership import monitor as monitor
from tests.web.test_monitor_status_lock_scope import schedule


@pytest.fixture
def launching(monitor, monkeypatch):
    service, manual, live, state = monitor
    timer = [0.0]
    monkeypatch.setattr(signal_monitor, "time", SimpleNamespace(monotonic=lambda: timer[0]))
    service.notifications.snapshot.return_value = {"configured": True}
    return service, manual, live, state, timer


def attempt(operation):
    try:
        operation()
    except Exception as error:
        cause = error.__cause__ or error
        assert isinstance(cause, (OSError, RuntimeError, ValueError))


@pytest.mark.parametrize("kind", ["daily", "live"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_repeated_start_failures_use_bounded_backoff(launching, kind, error_type):
    service, manual, live, state, timer = launching
    target = manual if kind == "daily" else live
    target.start.side_effect = error_type("controlled startup failure")
    operation = schedule(service, manual, kind)
    for stamp, expected in ((0, 1), (3, 1), (14, 1), (15, 2), (44, 2),
                            (45, 3), (104, 3), (105, 4), (164, 4), (165, 5)):
        timer[0] = stamp
        attempt(operation)
        assert target.start.call_count == expected
    assert not state["attempted_cutoffs"]


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_startup_recovers_after_three_failures_without_changing_settings(launching, kind):
    service, manual, live, state, timer = launching
    target = manual if kind == "daily" else live
    target.start.side_effect = OSError("controlled temporary start failure")
    operation = schedule(service, manual, kind)
    for stamp in (0, 15, 45):
        timer[0] = stamp
        attempt(operation)
    target.start.side_effect = None
    timer[0] = 104
    operation()
    assert target.start.call_count == 3
    timer[0] = 105
    operation()
    assert target.start.call_count == 4
    assert not service.snapshot()["last_error"]
    if kind == "live":
        assert state["active_monitor_run"] == target.start.return_value["run_id"]
    else:
        assert state["daily"]["afternoon"]["run_id"] == target.start.return_value["run_id"]


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_changed_settings_allow_a_fresh_start_attempt(launching, kind):
    service, manual, live, _state, timer = launching
    target = manual if kind == "daily" else live
    target.start.side_effect = OSError("controlled start failure")
    operation = schedule(service, manual, kind)
    attempt(operation)
    timer[0] = 1
    attempt(operation)
    assert target.start.call_count == 1
    service.enable({"interval_seconds": 120})
    target.start.side_effect = None
    operation()
    assert target.start.call_count == 2


def test_explicit_check_request_allows_a_fresh_live_start_attempt(launching):
    service, manual, live, _state, timer = launching
    live.start.side_effect = OSError("controlled start failure")
    operation = schedule(service, manual, "live")
    attempt(operation)
    timer[0] = 1
    attempt(operation)
    assert live.start.call_count == 1
    service.check_now()
    live.start.side_effect = None
    operation()
    assert live.start.call_count == 2


def configure_tick(service, monkeypatch):
    now = datetime(2026, 9, 30, 15, 11, tzinfo=CN)
    cutoff = int(now.replace(hour=15, minute=0).timestamp())
    monkeypatch.setattr(service, "now", lambda: now)
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: cutoff)
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)


def test_daily_start_failure_does_not_abort_live_schedule_or_hide_its_error(launching, monkeypatch):
    service, manual, live, state, timer = launching
    configure_tick(service, monkeypatch)
    manual.start.side_effect = OSError("controlled daily startup failure")
    attempt(service.tick)
    assert live.start.call_count == 1, "daily startup failure skipped the live scheduler"
    assert state["seed"]["codes"] == ["a:SH.600000"]
    assert "选股" in service.snapshot()["last_error"]
    timer[0] = 3
    service.tick()
    assert manual.start.call_count == 1
    assert "选股" in service.snapshot()["last_error"]


def test_startup_backoff_is_independent_for_daily_and_live_jobs(launching):
    service, manual, live, _state, timer = launching
    manual.start.side_effect = OSError("daily failure")
    live.start.side_effect = OSError("live failure")
    daily, realtime = schedule(service, manual, "daily"), schedule(service, manual, "live")
    attempt(daily)
    attempt(realtime)
    timer[0] = 1
    service.check_now()
    live.start.side_effect = None
    realtime()
    attempt(daily)
    assert live.start.call_count == 2 and manual.start.call_count == 1
    assert "选股" in service.snapshot()["last_error"]


def test_failure_to_persist_start_diagnostics_is_not_swallowed(launching, monkeypatch):
    service, manual, live, _state, _timer = launching
    configure_tick(service, monkeypatch)
    manual.start.side_effect = OSError("controlled startup failure")
    monkeypatch.setattr(service, "_save", Mock(side_effect=OSError("diagnostic persistence unavailable")))
    with pytest.raises(OSError, match="diagnostic persistence unavailable"):
        service.tick()
    live.start.assert_not_called()


def test_status_failure_outside_start_is_not_misclassified(launching):
    service, manual, _live, state, _timer = launching
    manual.status.side_effect = OSError("status probe failed")
    with pytest.raises(OSError, match="status probe failed"):
        service.tick()
    assert not state.get("startup_errors")


def test_status_reads_keep_start_failure_visible_without_retrying(launching):
    service, manual, live, _state, _timer = launching
    live.start.side_effect = OSError("controlled start failure")
    attempt(schedule(service, manual, "live"))
    for options in (None, {}):
        result = service.snapshot(page_options=options)
        assert "实时" in result["last_error"]
        assert "startup_errors" not in result
    assert live.start.call_count == 1


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_success_resets_the_start_failure_backoff(launching, kind):
    service, manual, live, state, timer = launching
    target = manual if kind == "daily" else live
    operation = schedule(service, manual, kind)
    target.start.side_effect = OSError("controlled first failure sequence")
    for stamp in (0, 15, 45):
        timer[0] = stamp
        attempt(operation)
    timer[0] = 105
    target.start.side_effect = None
    operation()
    if kind == "daily":
        state["daily"]["afternoon"].update(status="failed", retry_at=0)
    else:
        state["force"] = True
    timer[0] = 106
    target.start.side_effect = OSError("later independent startup failure")
    attempt(operation)
    assert state["startup_errors"][kind]["attempts"] == 1
    count = target.start.call_count
    timer[0] = 120
    attempt(operation)
    assert target.start.call_count == count
    timer[0] = 121
    attempt(operation)
    assert target.start.call_count == count + 1


@pytest.mark.parametrize("change", ["empty", "paused", "replacement"])
def test_retired_live_start_diagnostic_does_not_describe_the_current_pool(launching, change):
    service, manual, live, state, _timer = launching
    live.start.side_effect = OSError("controlled prior startup failure")
    attempt(schedule(service, manual, "live"))
    if change == "empty":
        state["seed"].update(codes=[], symbols=[], screening_symbols=[])
    elif change == "paused":
        state["enabled"] = False
    else:
        state["seed"]["run_id"] = "replacement"
    for options in (None, {}):
        assert "实时检查暂时无法启动" not in service.snapshot(page_options=options)["last_error"]
