"""Scheduling, durable deduplication, pool handoff and authenticated controls."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services.signal_monitor import SignalMonitor, _signal
from cl_app.services.screening import read_state_json, validate_settings
from chanlun.screening.rules import CN, trading_context


class Jobs:
    def __init__(self, root):
        self.root = root
        self.state = {"status": "idle"}
        self.started = []
        self.rows = []
        self.errors = []
        self.committed = None

    def status(self):
        return deepcopy(self.state)

    def results(self):
        return {**self.status(), "selected": deepcopy(self.rows), "observations": [], "errors": self.errors}

    def committed_results(self, after_completed=0, *, limit=16):
        symbols = self.committed if self.committed is not None else self.state["settings"].get("symbols", [])
        selected = symbols[after_completed:after_completed + limit]
        identities = {(s.get("market", "a"), s["code"]) for s in selected}
        result = self.results()
        result["selected"] = [row for row in result["selected"] if (row.get("market", "a"), row["code"]) in identities]
        result["errors"] = [row for row in result["errors"] if (row.get("market", "a"), row["code"]) in identities]
        return {**result, "processed_symbols": deepcopy(selected), "completed": len(symbols),
                "results_through": after_completed + len(selected)}

    def start(self, settings, *, external_notifications=True):
        self.started.append(deepcopy(settings))
        self.state = {"status": "running", "run_id": str(len(self.started)).zfill(32), "settings": settings, "completed": 0}
        self.committed = None
        if not external_notifications:
            self.state["external_notifications"] = False
        return self.status()

    def cancel(self):
        self.state["cancel_requested"] = True
        return self.status()

    def finish(self, cutoff):
        self.state.update(status="completed", source_current=True, cutoffs={"5m": cutoff, "1m": cutoff},
                          completed=len(self.state["settings"].get("symbols", [])))


def signal(code="SH.600088", stage="confirmed"):
    return {"code": code, "market": "a", "name": "样例", "frequency": "5m", "source_closed_at": 100,
            "point": {"point_id": "point", "point_type": "3buy", "center_ordinal": 1, "status": stage, "available_at": 100}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [])
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
    assert service.snapshot()["daily"]["afternoon"]["status"] == "completed"


def test_morning_and_afternoon_scans_use_distinct_completed_bars_once_per_session(setup):
    service, manual, live, clock = setup
    clock[0] = clock[0].replace(hour=11, minute=30)
    service.tick()
    assert not manual.started
    clock[0] = clock[0].replace(minute=35)
    morning_cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    service.tick()
    assert len(manual.started) == 1
    assert service.snapshot()["daily"]["morning"]["status"] == "started"
    manual.rows = [signal()]
    manual.finish(morning_cutoff)
    service.tick()
    assert service.snapshot()["daily"]["morning"]["status"] == "completed"
    clock[0] = clock[0].replace(hour=13, minute=5)
    service.tick()
    assert len(manual.started) == 1
    clock[0] = clock[0].replace(hour=15, minute=11)
    afternoon_cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.finish(morning_cutoff)
    service.tick()
    assert len(manual.started) == 2
    assert service.snapshot()["daily"]["afternoon"]["status"] == "started"
    assert morning_cutoff != afternoon_cutoff
    manual.finish(afternoon_cutoff)
    service.tick()
    assert service.snapshot()["daily"]["afternoon"]["status"] == "completed"
    service.tick()
    assert len(manual.started) == 2


def test_morning_scan_waits_for_11_30_cutoff_after_late_service_start(setup):
    service, manual, _, clock = setup
    clock[0] = clock[0].replace(hour=12, minute=5)
    service.tick()
    assert len(manual.started) == 1
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    assert datetime.fromtimestamp(cutoff, CN).strftime("%H:%M") == "11:30"
    assert service.snapshot()["daily"]["morning"]["status"] == "started"


def test_late_afternoon_scan_is_recorded_completed_after_midnight(setup):
    service, manual, _, clock = setup
    clock[0] = clock[0].replace(hour=15, minute=11)
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    clock[0] = clock[0].replace(day=18, hour=0, minute=30)
    service.tick()
    assert service.snapshot()["daily"]["session"] == "2026-09-17"
    assert service.snapshot()["today"] == "2026-09-18"
    assert service.snapshot()["daily"]["afternoon"]["status"] == "completed"


def test_legacy_afternoon_record_is_migrated_without_duplicate_scan(setup):
    service, manual, _, clock = setup
    clock[0] = clock[0].replace(hour=15, minute=11)
    state = service._load()
    state["daily"] = {"session": "2026-09-17", "run_id": "old", "status": "completed",
                      "completed_session": "2026-09-17", "attempts": 1}
    service._save()
    restarted = SignalMonitor(service.root, screening=manual, monitor=service.monitor, now=lambda: clock[0])
    restarted.tick()
    assert restarted.snapshot()["daily"]["afternoon"]["status"] == "completed"
    assert not manual.started


def test_monitor_recovers_enabled_settings_after_corrupt_state_file(setup):
    service, manual, live, clock = setup
    state = service._load()
    state["dingtalk_enabled"] = True
    service._save()
    (service.root / "state.json").write_bytes(b"\x00" * 1024)
    restarted = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    recovered = restarted._load()
    assert recovered["enabled"] is True
    assert recovered["dingtalk_enabled"] is True
    assert recovered["settings"]["morning_after_close"] == "11:35"
    assert read_state_json(service.root / "state.json")["enabled"] is True
    assert len(list(service.root.glob("state.corrupt.*.json"))) == 1


def test_changing_daily_parallelism_keeps_live_notifications_enabled(setup):
    service, _, _, _ = setup
    service._load()["dingtalk_enabled"] = True
    service._save()
    service.enable({"daily_scope": "all_a_watchlist", "screening_workers": 8})
    configured = service.enable({"daily_scope": "all_a_watchlist", "screening_workers": 12,
                                 "morning_after_close": "11:35", "after_close": "15:10"})
    assert configured["enabled"] is True
    assert configured["dingtalk_enabled"] is True
    assert configured["settings"]["screening"]["workers"] == 12
    with pytest.raises(ValueError, match="workers"):
        service.enable({"screening_workers": 13})


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


def test_quiet_screening_does_not_disable_live_notifications_after_restart(setup, monkeypatch):
    service, manual, live, clock = setup
    notifications = Mock()
    notifications.snapshot.return_value = {}
    notifications.has.return_value = False
    service._notifications = notifications
    with service._lock:
        service._load().update(dingtalk_enabled=True, notification_since=0)
        service._save()
    manual.start(validate_settings({"scope": "all_a_watchlist"}), external_notifications=False)
    manual.rows = [signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    manual.state["finished_at"] = clock[0].timestamp()
    service.tick()
    assert service._load()["seed"].get("external_notifications", True) is True
    assert live.state.get("external_notifications", True) is True
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    assert service._load()["dingtalk_enabled"] is True
    assert service._load()["events"][0].get("external_notifications", True) is True
    notifications.screening_completed.assert_not_called()
    assert any(call.args[0] for call in notifications.signal_events.call_args_list)
    restarted = SignalMonitor(service.root, screening=manual, monitor=live,
                              now=lambda: clock[0], notifications=notifications)
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    restarted.tick()
    notifications.screening_completed.assert_not_called()
    assert len(restarted.snapshot()["events"]) == 1


def test_running_monitor_delivers_committed_signals_and_keeps_unchecked_symbols_on_restart(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal(), signal("SZ.000001")])
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service._notifications = notifications
    service._load().update(dingtalk_enabled=True, notification_since=0)
    service.tick()
    old = _signal(signal("SZ.000001"))
    service._load()["signals"] = {old["id"]: old}
    service._save()
    symbols = live.started[0]["symbols"]
    live.rows = [signal()]
    live.committed = symbols[:1]
    # An old in-flight run may still carry the inherited quiet flag.
    live.state.update(completed=1, source_current=True, external_notifications=False)
    service.tick()
    snapshot = service.snapshot()
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["change"] == "discovered"
    assert any(call.args[0] for call in notifications.signal_events.call_args_list)
    assert {s["code"] for s in snapshot["signals"]} == {"SH.600088", "SZ.000001"}
    assert set(snapshot["checked_cutoffs"]) == {"a:SH.600088"}
    assert snapshot.get("processed_run") != live.state["run_id"]
    restarted = SignalMonitor(service.root, screening=manual, monitor=live,
                              now=lambda: clock[0], notifications=notifications)
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    restarted.tick()
    assert len(restarted.snapshot()["events"]) == 1
    live.committed = symbols
    live.finish(cutoff)
    restarted.tick()
    snapshot = restarted.snapshot()
    assert len(snapshot["events"]) == 2
    assert snapshot["events"][0]["code"] == "SZ.000001"
    assert snapshot["events"][0]["change"] == "left"
    assert snapshot["processed_run"] == live.state["run_id"]


def test_completed_batch_is_fully_consumed_before_next_run_can_replace_it(setup):
    rows = [signal(f"SH.{600000 + i}") for i in range(20)]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    live.rows = rows
    live.finish(cutoff)
    clock[0] = clock[0].replace(hour=15, minute=1)
    service.tick()
    assert len(service.snapshot()["signals"]) == 16
    assert len(live.started) == 1
    assert service.snapshot().get("processed_run") != live.state["run_id"]
    service.tick()
    assert len(service.snapshot()["signals"]) == 20
    assert len(live.started) == 2


def test_bounded_batches_prioritize_watchlist_then_rotate_all_pending_symbols(setup, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 2)
    rows = [signal(f"SH.{600000 + i}") for i in range(5)]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    watch = {"market": "a", "code": "SH.600004", "name": "watch", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [watch])
    service.tick()
    assert live.started[0]["symbols"][0] == watch
    assert len(live.started[0]["symbols"]) == 2
    assert service.snapshot()["pending_symbols_count"] == 3
    for _ in range(3):
        live.finish(cutoff)
        clock[0] += timedelta(seconds=61)
        service.tick()
    checked = {s["code"] for request in live.started for s in request["symbols"]}
    assert checked == {r["code"] for r in rows}
    assert all(len(request["symbols"]) <= 2 for request in live.started)


def test_legacy_quiet_history_is_not_replayed_when_live_policy_is_repaired(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    state = service._load()
    state["seed"]["external_notifications"] = False
    state.update(dingtalk_enabled=True, notification_since=0)
    state["events"] = [{**_signal(signal()), "external_notifications": False,
                        "recorded_at": int(clock[0].timestamp()), "change": "discovered"}]
    service._save()
    notifications = Mock()
    notifications.snapshot.return_value = {}
    restarted = SignalMonitor(service.root, screening=manual, monitor=live,
                              now=lambda: clock[0], notifications=notifications)
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    restarted.tick()
    assert "external_notifications" not in restarted.snapshot()["seed"]
    assert all(not call.args[0] for call in notifications.signal_events.call_args_list)


def test_force_check_keeps_remaining_symbols_across_bounded_batches(setup, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 2)
    rows = [signal(f"SH.{600000 + i}") for i in range(3)]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    clock[0] = clock[0].replace(hour=11, minute=31)  # No new bars before the noon scan.
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    service.tick()
    live.finish(cutoff)
    clock[0] += timedelta(seconds=61)
    service.tick()
    live.finish(cutoff)
    service.tick()
    service.check_now()
    service.tick()
    before = len(live.started)
    assert service.snapshot()["force_pending_symbols"]
    live.finish(cutoff)
    clock[0] += timedelta(seconds=61)
    service.tick()
    assert len(live.started) == before + 1
    assert not service.snapshot()["force_pending_symbols"]
    assert {s["code"] for request in live.started[-2:] for s in request["symbols"]} == {r["code"] for r in rows}


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


def _us_watch():
    return {"market": "us", "code": "TSLA.US", "name": "Tesla", "origin": "watchlist"}


def _seed(setup, rows):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = rows
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    return service, manual, live, clock, cutoff


def test_watchlist_is_monitored_even_when_it_has_no_screening_signal(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": cutoff + 300})
    service.tick()
    assert live.started[0]["symbols"] == [_us_watch()]
    assert service.snapshot()["pool_markets"] == {"us": 1}
    assert service.snapshot()["seed"]["watchlist_count"] == 1


def test_watchlist_can_start_before_a_completed_screening_run(setup, monkeypatch):
    service, manual, live, clock = setup
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": 12300})
    service.tick()
    assert not manual.started
    assert live.started[0]["symbols"] == [_us_watch()]


def test_only_changed_market_runs_and_other_market_signals_survive(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    us_close = [cutoff + 300]
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": us_close[0]})
    service.tick()
    us_signal = {**signal("TSLA.US"), "market": "us"}
    live.rows = [signal(), us_signal]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 2
    assert len(live.started) == 1
    us_close[0] += 300
    clock[0] = clock[0].replace(hour=15, minute=1)
    # Pin A-share closure to the already checked minute while US advances.
    service._schedule_monitor(clock[0], cutoff)
    assert live.started[-1]["symbols"] == [_us_watch()]
    live.rows = [us_signal]
    live.finish(cutoff)
    service._consume_monitor()
    snapshot = service.snapshot()
    assert {s["market"] for s in snapshot["signals"]} == {"a", "us"}
    assert not any(e["change"] == "left" for e in snapshot["events"])
    assert snapshot["last_cutoffs"]["5m"] == us_close[0]
    assert snapshot["last_cutoffs_by_market"] == {"a": cutoff, "us": us_close[0]}


def test_bad_watchlist_calendar_does_not_block_supported_markets(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [])
    unsupported = {"market": "fx", "code": "FE.USDCNY", "name": "FX", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [unsupported, _us_watch()])

    def context(market, *_args):
        if market == "fx":
            raise ValueError("SESSION_CALENDAR_UNAVAILABLE: fx")
        return {"cutoff": cutoff + 300}

    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", context)
    service.tick()
    assert live.started[0]["symbols"] == [_us_watch()]
    assert service.snapshot()["errors"][0]["code"] == "FE.USDCNY"
    assert "交易时段" in service.snapshot()["last_error"]


def test_existing_closed_market_is_not_rescanned_when_watchlist_is_added(setup, monkeypatch):
    import json
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    state = service._load()
    state.pop("attempted_cutoffs")
    state.pop("checked_cutoffs")
    state["seed"].pop("screening_symbols")
    state["last_attempt_key"] = state["seed"]["run_id"] + json.dumps({"a": cutoff})
    service._save()
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": cutoff + 300})
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, "start_runtime", lambda: None)
    restored.tick()
    assert live.started[-1]["symbols"] == [_us_watch()]
    assert len(live.started) == 2


def test_removed_watchlist_member_cannot_publish_an_inflight_signal(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    watch = [_us_watch()]
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: list(watch))
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": cutoff + 300})
    service.tick()
    watch.clear()
    live.rows = [signal(), {**signal("TSLA.US"), "market": "us"}]
    live.finish(cutoff)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["pool_markets"] == {"a": 1}
    assert all(s["code"] != "TSLA.US" for s in snapshot["signals"])
    assert all(e["code"] != "TSLA.US" for e in snapshot["events"])


def test_new_screening_pool_preserves_watchlist_signals_until_their_next_check(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": cutoff + 300})
    service.tick()
    live.rows = [signal(), {**signal("TSLA.US"), "market": "us"}]
    live.finish(cutoff)
    service.tick()
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal("SZ.000001")]
    manual.finish(cutoff)
    service.tick()
    assert {s["code"] for s in service.snapshot()["signals"]} == {"TSLA.US"}
    assert {s["code"] for s in live.started[-1]["symbols"]} == {"TSLA.US", "SZ.000001"}


def test_new_watchlist_signal_reaches_outbox_once_without_a_screening_candidate(setup, monkeypatch):
    from cl_app.services.dingtalk_notifications import DingTalkOutbox
    service, manual, live, clock, cutoff = _seed(setup, [])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    monkeypatch.setattr("cl_app.services.signal_monitor.market_context", lambda *a: {"cutoff": cutoff + 300})
    transport = Mock()
    transport.configuration.return_value = {"configured": True}
    outbox = DingTalkOutbox(service.root / "test_outbox.sqlite3", transport=transport,
                           now=lambda: clock[0].timestamp())
    service._notifications = outbox
    service._load().update(dingtalk_enabled=True, notification_since=clock[0].timestamp())
    service.tick()
    live.rows = [{**signal("TSLA.US"), "market": "us"}]
    live.finish(cutoff)
    service.tick()
    assert outbox.snapshot()["pending"] == 1
    outbox.deliver_one()
    assert "TSLA.US" in transport.send.call_args.args[0]
    service.check_now()
    service.tick()
    live.finish(cutoff)
    service.tick()
    outbox.deliver_one()
    assert transport.send.call_count == 1
    assert outbox.snapshot()["pending"] == 0


def test_main_confirmation_transition_is_detected_while_selection_still_waits(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    waiting = {**signal(stage="approaching"), "selection_status": "approaching",
               "nested_confirmation": {"state": "waiting", "frequency": "1m"}}
    live.rows = [waiting]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["events"]) == 1
    service.check_now()
    service.tick()
    live.rows = [{**waiting, "point": {**waiting["point"], "status": "confirmed", "confirmed_at": 100}}]
    live.finish(cutoff)
    service.tick()
    events = service.snapshot()["events"]
    assert len(events) == 2
    assert events[0]["stage"] == "approaching"
    assert events[0]["point_status"] == "confirmed"
    assert events[0]["lower_confirmation_state"] == "waiting"
    assert events[0]["change"] == "changed"


def test_legacy_confirmed_status_does_not_resend_only_for_new_metadata(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    for previous in service._load()["known"].values():
        previous.pop("notification_state", None)
    service.check_now()
    service.tick()
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["events"]) == 1


def test_policy_update_refreshes_existing_pool_without_rechecking_unchanged_bars(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    previous_cutoffs = dict(service._load()["attempted_cutoffs"])
    manual.state["selection_policy_version"] = "first-up-center-v1"
    service.tick()
    assert service.snapshot()["seed"]["selection_policy_version"] == "first-up-center-v1"
    assert service._load()["attempted_cutoffs"] == previous_cutoffs
    assert len(live.started) == 1
    assert len(service.snapshot()["signals"]) == 1
