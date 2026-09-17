"""Scheduling, durable deduplication, pool handoff and authenticated controls."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services.signal_monitor import SignalMonitor
from cl_app.services.screening import validate_settings
from chanlun.screening.rules import CN, trading_context


class Jobs:
    def __init__(self, root):
        self.root = root
        self.state = {"status": "idle"}
        self.started = []
        self.rows = []
        self.errors = []

    def status(self):
        return deepcopy(self.state)

    def results(self):
        return {**self.status(), "selected": deepcopy(self.rows), "observations": [], "errors": self.errors}

    def start(self, settings):
        self.started.append(deepcopy(settings))
        self.state = {"status": "running", "run_id": str(len(self.started)).zfill(32), "settings": settings}
        return self.status()

    def cancel(self):
        self.state["cancel_requested"] = True
        return self.status()

    def finish(self, cutoff):
        self.state.update(status="completed", source_current=True, cutoffs={"5m": cutoff, "1m": cutoff})


def signal(code="SH.600088", stage="confirmed"):
    return {"code": code, "market": "a", "name": "样例", "frequency": "5m", "source_closed_at": 100,
            "point": {"point_id": "point", "point_type": "3buy", "status": stage, "available_at": 100}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 17, 14, 59, tzinfo=CN)]
    manual, live = Jobs(tmp_path / "manual"), Jobs(tmp_path / "runs")
    service = SignalMonitor(tmp_path, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(service, "start_runtime", lambda: None)
    service.enable({"daily_scope": "all_a_watchlist"})
    return service, manual, live, clock


def test_daily_screening_waits_for_close_and_hands_only_completed_pool_to_monitor(setup):
    service, manual, live, clock = setup
    service.tick()
    assert not manual.started and not live.started
    clock[0] = clock[0].replace(hour=15, minute=11)
    service.tick()
    assert manual.started[0]["scope"] == "all_a_watchlist"
    manual.rows = [signal()]
    service.tick()
    assert not live.started  # An appended partial result is never a seed.
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    assert len(manual.started) == len(live.started) == 1
    assert live.started[0]["symbols"] == [{"market": "a", "code": "SH.600088", "name": "样例"}]
    service.tick()
    assert len(manual.started) == len(live.started) == 1
    assert service.snapshot()["daily"]["completed_session"] == "2026-09-17"


def test_monitor_deduplicates_after_restart_and_waits_for_new_closed_bar(setup, monkeypatch):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["events"]) == 1
    restarted = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    restarted.tick()
    assert len(restarted.snapshot()["events"]) == 1
    assert len(live.started) == 1
    restarted.check_now()
    restarted.tick()
    assert len(live.started) == 2
    live.finish(cutoff)
    restarted.tick()
    assert len(restarted.snapshot()["events"]) == 1
    restarted.pause()
    clock[0] = clock[0].replace(hour=16)
    restarted.tick()
    assert len(live.started) == 2
    assert not restarted.snapshot()["enabled"]


def test_data_failure_does_not_emit_a_signal_exit(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    service.check_now()
    service.tick()
    live.rows = []
    live.errors = [{"code": "SH.600088", "market": "a", "reasons": ["STALE_DATA"]}]
    live.finish(cutoff)
    service.tick()
    state = service.snapshot()
    assert len(state["events"]) == 1
    assert not state["signals"]
    assert state["errors"] and state["last_error"]


def test_weekend_does_not_trigger_a_full_market_scan(setup):
    service, manual, _, clock = setup
    clock[0] = datetime(2026, 9, 19, 16, 0, tzinfo=CN)
    service.tick()
    assert not manual.started


def test_monitor_errors_keep_last_good_pool_and_do_not_run_overlapping_jobs(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal()]
    manual.finish(trading_context(clock[0], "5m", 1, 1)["cutoff"])
    service.tick()
    first_pool = service.snapshot()["seed"]
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal("SZ.000001")]
    service.tick()
    assert service.snapshot()["seed"] == first_pool
    assert len(live.started) == 1


def test_routes_require_login_and_csrf_and_reads_do_not_launch_jobs(monkeypatch):
    fake = Mock()
    fake.snapshot.return_value = {"enabled": False}
    monkeypatch.setattr("cl_app.blueprints.monitor.service", fake)
    app = create_app(test_config={"TESTING": True, "VALIDATE_WEB_SECURITY": False})
    client = app.test_client()
    assert client.get("/monitor").status_code == 302
    assert client.get("/monitor/status").status_code == 302
    app.config["LOGIN_DISABLED"] = True
    assert client.get("/monitor/status").get_json() == {"enabled": False}
    assert client.get("/monitor/start").status_code == 405
    assert client.post("/monitor/start", json={}).status_code == 400
    fake.enable.assert_not_called()
    app.config["WTF_CSRF_ENABLED"] = False
    fake.enable.return_value = {"enabled": True}
    assert client.post("/monitor/start", json={}).get_json()["enabled"]


def test_trusted_monitor_limit_does_not_raise_public_manual_limit():
    body = {"scope": "symbols", "symbols": [{"market": "a", "code": f"SH.{600000+i}"} for i in range(101)]}
    with pytest.raises(ValueError):
        validate_settings(body)
    assert len(validate_settings(body, max_codes=10000)["symbols"]) == 101
