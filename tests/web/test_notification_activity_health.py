"""Notification silence must not conceal failed workers or live startup retries."""
from unittest.mock import Mock
import time

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport
from tests.web.test_monitor_control_ownership import monitor as monitor


@pytest.fixture
def quiet(monitor, tmp_path):
    service, manual, live, state = monitor
    box = DingTalkOutbox(tmp_path / "activity-outbox.sqlite", transport=Transport(), charts=True)
    service._notifications = box
    service._thread = Mock()
    service._thread.is_alive.return_value = True
    box._thread = Mock()
    box._thread.is_alive.return_value = True
    box.charts._thread = Mock()
    box.charts._thread.is_alive.return_value = True
    service._last_tick_at = service.now().timestamp()
    service._last_cycle_at = time.monotonic()
    state.update(dingtalk_enabled=True, last_check_at=service.now().timestamp(), last_check_notifications=0)
    return service, box, state


@pytest.mark.parametrize("worker", ["delivery", "charts"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("paged", [False, True])
def test_stopped_notification_worker_is_not_reported_as_quiet_or_paused(quiet, worker, enabled, paged):
    service, box, state = quiet
    state["enabled"] = enabled
    target = box if worker == "delivery" else box.charts
    target._thread.is_alive.return_value = False
    result = service.snapshot(page_options={} if paged else None)
    activity = result["notification_activity"]
    assert activity["state"] == "unhealthy"
    assert "线程" in activity["message"]
    assert ("发送" if worker == "delivery" else "图表") in activity["message"]


def test_worker_recovery_clears_the_activity_error_without_a_new_signal(quiet):
    service, box, _state = quiet
    box._thread.is_alive.return_value = False
    assert service.snapshot()["notification_activity"]["state"] == "unhealthy"
    box._thread.is_alive.return_value = True
    assert service.snapshot()["notification_activity"]["state"] == "quiet"


def test_optional_absent_chart_worker_is_not_reported_as_failed(quiet):
    service, box, _state = quiet
    box.charts = None
    assert service.snapshot()["notification_activity"]["state"] == "quiet"


@pytest.mark.parametrize("paged", [False, True])
def test_current_live_start_failure_explains_notification_silence(quiet, paged):
    service, _box, state = quiet
    state["startup_errors"] = {"live": {"seed": state["seed"]["run_id"],
        "message": "实时检查暂时无法启动，将自动重试"}}
    result = service.snapshot(page_options={} if paged else None)
    assert result["notification_activity"]["state"] == "unhealthy"
    assert "实时检查暂时无法启动" in result["notification_activity"]["message"]


def test_daily_start_failure_does_not_claim_healthy_live_monitoring_is_down(quiet):
    service, _box, state = quiet
    state["startup_errors"] = {"daily": {"session": service.now().date().isoformat(),
        "message": "定时选股暂时无法启动，将自动重试"}}
    assert service.snapshot()["notification_activity"]["state"] == "quiet"


@pytest.mark.parametrize("change", ["replacement", "empty", "paused"])
def test_retired_live_start_failure_does_not_override_current_activity(quiet, change):
    service, _box, state = quiet
    state["startup_errors"] = {"live": {"seed": state["seed"]["run_id"],
        "message": "实时检查暂时无法启动，将自动重试"}}
    if change == "replacement":
        state["seed"]["run_id"] = "replacement"
        expected = "quiet"
    elif change == "empty":
        state["seed"].update(codes=[], symbols=[], screening_symbols=[])
        expected = "empty"
    else:
        state["enabled"] = False
        expected = "paused"
    assert service.snapshot()["notification_activity"]["state"] == expected


@pytest.mark.parametrize("queue", ["pending", "rendering"])
def test_existing_delivery_work_remains_visible_during_live_start_retry(quiet, monkeypatch, queue):
    service, box, state = quiet
    state["startup_errors"] = {"live": {"seed": state["seed"]["run_id"],
        "message": "实时检查暂时无法启动，将自动重试"}}
    delivery = {"configured": True, "pending": 1} if queue == "pending" else {
        "configured": True, "chart_generation": {"pending": 1}}
    monkeypatch.setattr(box, "snapshot", lambda: delivery)
    assert service.snapshot()["notification_activity"]["state"] == queue


def test_disabled_push_remains_explicit_when_a_sender_is_not_running(quiet):
    service, box, state = quiet
    state["dingtalk_enabled"] = False
    box._thread.is_alive.return_value = False
    assert service.snapshot()["notification_activity"]["state"] == "disabled"


def test_concrete_queue_failure_remains_visible_with_a_stopped_sender(quiet, monkeypatch):
    service, box, _state = quiet
    box._thread.is_alive.return_value = False
    monkeypatch.setattr(box, "snapshot", lambda: {"configured": True, "last_error": "通知队列存储暂不可用"})
    activity = service.snapshot()["notification_activity"]
    assert activity["state"] == "failed"
    assert "通知队列存储暂不可用" in activity["message"]
