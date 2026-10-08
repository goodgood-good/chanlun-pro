"""Daily status reflects verified sessions, actual phases and pause state."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cl_app.services import monitor_schedule
from tests.web.test_signal_monitor import setup as setup


def state(day, *, enabled=True, **phases):
    return {
        "enabled": enabled,
        "settings": {"morning_after_close": "11:35", "after_close": "15:10"},
        "daily": {"session": day, **phases},
    }


def describe(day="2026-09-17", **phases):
    now = datetime.fromisoformat(day + "T08:00:00+08:00")
    return monitor_schedule.describe(state(day, **phases), {"status": "idle"}, now)


def test_non_trading_day_does_not_claim_a_daily_run_is_waiting():
    result = describe("2026-10-07")
    assert result["status"] == "non_trading_day"
    assert result["next_trading_session"] == "2026-10-08"
    assert "待运行" not in result["detail"]


def test_preopen_trading_day_is_not_mistaken_for_a_holiday():
    result = describe("2026-10-08")
    assert result["status"] == "waiting"
    assert result["calendar_status"] == "trading"


def test_only_afternoon_completion_is_not_two_completed_runs():
    result = describe(afternoon={"status": "completed"})
    assert result["completed_phases"] == 1
    assert result["title"] == "今日已完成 1 / 2 轮"
    assert "上午 11:35：未运行" in result["detail"]


def test_both_completed_phases_are_required_for_the_completed_title():
    assert (
        describe(morning={"status": "completed"}, afternoon={"status": "completed"})[
            "status"
        ]
        == "completed"
    )


def test_morning_failure_is_not_hidden_by_afternoon_completion():
    result = describe(
        morning={"status": "failed", "attempts": 3}, afternoon={"status": "completed"}
    )
    assert result["status"] == "attention"
    assert "已达重试上限" in result["detail"]
    assert "失败，待重试" not in result["detail"]


def test_paused_automation_does_not_claim_it_will_run_today():
    result = describe(enabled=False)
    assert result["status"] == "paused"
    assert "待运行" not in result["detail"]


@pytest.mark.parametrize("unavailable", [None, OSError("calendar unavailable")])
def test_unavailable_calendar_does_not_guess_a_trading_day(monkeypatch, unavailable):
    def evidence(**_):
        if isinstance(unavailable, Exception):
            raise unavailable
        return unavailable

    monkeypatch.setattr(monitor_schedule, "official_trading_session_evidence", evidence)
    assert describe()["status"] == "calendar_unavailable"


def test_a_manual_scan_can_be_running_on_a_closed_day():
    now = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = monitor_schedule.describe(
        state("2026-10-07"), {"status": "running", "completed": 3, "total": 10}, now
    )
    assert result["status"] == "running" and "3 / 10" in result["title"]


def test_full_and_paged_snapshots_share_the_daily_status_without_starting_work(setup):
    service, manual, live, clock = setup
    clock[0] = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    full = service.snapshot()
    paged = service.snapshot(page_options={})
    assert full["daily_schedule"] == paged["daily_schedule"]
    assert full["daily_schedule"]["status"] == "non_trading_day"
    assert not manual.started and not live.started
