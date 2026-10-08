"""Scheduling, durable deduplication, pool handoff and authenticated controls."""

from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services.signal_monitor import SignalMonitor, _signal
from cl_app.services.screening import read_state_json, validate_settings
from chanlun.screening.rules import CN, trading_context
from chanlun.screening.markets import closed_bar_cutoff


class Jobs:
    def __init__(self, root):
        self.root = root
        self.state = {"status": "idle"}
        self.started = []
        self.rows = []
        self.consolidation = []
        self.errors = []
        self.committed = None

    def status(self):
        return deepcopy(self.state)

    def results(self, *, background=False):
        return {**self.status(), "selected": deepcopy(self.rows), "observations": [], "errors": self.errors,
                "consolidation_alerts": deepcopy(self.consolidation)}

    def committed_results(self, after_completed=0, *, limit=16):
        symbols = self.committed if self.committed is not None else self.state["settings"].get("symbols", [])
        selected = symbols[after_completed:after_completed + limit]
        identities = {(s.get("market", "a"), s["code"]) for s in selected}
        result = self.results()
        result["selected"] = [row for row in result["selected"] if (row.get("market", "a"), row["code"]) in identities]
        result["errors"] = [row for row in result["errors"] if (row.get("market", "a"), row["code"]) in identities]
        result["consolidation_alerts"] = [row for row in result["consolidation_alerts"]
                                          if (row.get("market", "a"), row["code"]) in identities]
        return {**result, "processed_symbols": deepcopy(selected), "completed": len(symbols),
                "results_through": after_completed + len(selected), "verified_periods": [
                    {"market": symbol.get("market", "a"), "code": symbol["code"], "frequency": frequency,
                     "consolidation_checked": True,
                     "source_closed_at": self.state.get("cutoffs", {}).get(frequency,
                         max(self.state.get("cutoffs", {}).values(), default=100))}
                    for symbol in selected for frequency in self.state["settings"].get("frequencies", [])
                ]}

    def start(self, settings, *, external_notifications=True, monitor_periods=None, holding_snapshot=None):
        self.runtime_periods = monitor_periods
        self.holding_snapshot = holding_snapshot
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
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [])
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


def test_multi_period_center_alert_monitor_uses_the_shortest_selected_clock(setup):
    service, manual, live, clock = setup
    settings = validate_settings({
        "scope": "all_a_watchlist",
        "strategy": "direct_structural",
        "frequencies": ["1m", "5m", "30m"],
    })
    manual.start(settings)
    manual.rows = [signal()]
    manual.finish(trading_context(clock[0], "5m", 1, 1)["cutoff"])
    service.tick()
    expected = trading_context(clock[0], "1m", 1, 1)["cutoff"]
    assert live.started[0]["frequencies"] == ["1m", "5m", "30m"]
    assert service.snapshot()["active_cutoffs"]["a:SH.600088"] == expected


def test_missing_period_proof_cannot_produce_a_false_exit_and_recovers_without_rediscovery(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist", "frequencies": ["5m", "30m"]}))
    slower = {**signal(), "frequency": "30m"}
    manual.rows = [signal(), slower]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    live.rows = [signal(), slower]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 2
    original = live.committed_results

    def missing_period(*args):
        result = original(*args)
        result["verified_periods"] = [period for period in result["verified_periods"] if period["frequency"] == "5m"]
        return result

    service.check_now()
    service.tick()
    live.committed_results = missing_period
    live.rows = []
    live.finish(cutoff)
    service.tick()
    exits = [event for event in service.snapshot()["events"] if event["change"] == "left"]
    assert [event["frequency"] for event in exits] == ["5m"]
    assert any(row["frequency"] == "30m" for row in service._load()["unverified_signals"].values())
    assert any(error.get("reasons") == ["PERIOD_CHECK_MISSING"] for error in service.snapshot()["errors"])
    service.check_now()
    service.tick()
    live.committed_results = original
    live.rows = [slower]
    live.finish(cutoff)
    service.tick()
    assert [row["frequency"] for row in service.snapshot()["signals"]] == ["30m"]
    assert len([event for event in service.snapshot()["events"] if event["change"] == "discovered"]) == 2


def test_dispatch_requires_a_current_successful_period_proof_and_a_live_signal_identity(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [{**signal(), "source_closed_at": cutoff}]
    live.finish(cutoff)
    service.tick()
    event = next(row for row in service.snapshot()["events"] if row["change"] == "discovered")
    valid, pending = service._validate_notification_events([event])
    assert len(valid) == 1 and pending == []
    checks = service._load()["selection_checks"]
    checks["a:SH.600088:5m"]["error"] = True
    assert service._validate_notification_events([event]) == ([], [event])
    checks["a:SH.600088:5m"]["error"] = False
    checks["a:SH.600088:5m"]["identities"] = []
    assert service._validate_notification_events([event]) == ([], [])
    checks["a:SH.600088:5m"]["identities"] = [event["id"]]
    clock[0] = clock[0].replace(hour=15, minute=1)
    assert service._validate_notification_events([event]) == ([], [event])
    service._load()["signals"].clear()
    assert service._validate_notification_events([event]) == ([], [])


def test_dispatch_rechecks_holding_membership_after_enqueue_and_defers_unknown_membership(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    holdings = [held]
    failed = [False]

    def source():
        if failed[0]:
            raise OSError("holding database unavailable")
        return list(holdings)

    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", source)
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.finish(cutoff)
    service.tick()
    clock[0] = clock[0].replace(hour=15, minute=1)
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.consolidation = [consolidation(held["code"], cutoff)]
    live.finish(cutoff)
    service._consume_monitor()
    event = next(row for row in service.snapshot()["events"] if row.get("signal_type") == "consolidation_divergence")
    assert event["external_notifications"] is True
    assert len(service._validate_notification_events([event])[0]) == 1
    failed[0] = True
    assert service._validate_notification_events([event]) == ([], [event])
    failed[0] = False
    holdings.clear()
    # No monitor tick has run yet; dispatch still sees the freshly removed holding.
    assert service._validate_notification_events([event]) == ([], [])


def test_reader_update_rechecks_closed_pool_without_rolling_live_data_back_to_manual_snapshot(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    manual.state["result_reader_revision"] = "reader-1"
    service.tick()
    live.rows = [{**signal(), "source_closed_at": cutoff}]
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 1
    manual.state["result_reader_revision"] = "reader-2"
    service.tick()
    assert len(live.started) == 2
    assert service.snapshot()["signals"] == []
    assert next(iter(service._load()["unverified_signals"].values()))["source_closed_at"] == cutoff
    service.tick()
    assert len(live.started) == 2
    live.finish(cutoff)
    service.tick()
    assert service.snapshot()["signals"][0]["source_closed_at"] == cutoff


def test_new_screening_frequency_removes_old_unmonitored_period_without_claiming_an_exit(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    assert service.snapshot()["signals"][0]["frequency"] == "5m"
    manual.start(validate_settings({"scope": "all_a_watchlist", "frequencies": ["30m"]}))
    manual.rows = [{**signal(), "frequency": "30m"}]
    manual.finish(cutoff)
    service.tick()
    assert not service.snapshot()["signals"]
    assert not any(key.endswith(":5m") for key in service._load()["selection_checks"])
    assert not any(event["change"] == "left" for event in service.snapshot()["events"])


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


def test_one_period_failure_does_not_hide_a_successful_period_signal_exit(setup):
    service, manual, live, clock = setup
    settings = validate_settings({"scope": "all_a_watchlist", "frequencies": ["1m", "5m"]})
    minute_signal = {**signal(), "frequency": "1m"}
    manual.start(settings)
    manual.rows = [minute_signal, signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    live.rows = [minute_signal, signal()]
    live.finish(cutoff)
    service.tick()
    service.check_now()
    service.tick()
    live.rows = []
    live.errors = [{"code": "SH.600088", "market": "a", "frequency": "5m",
                    "reasons": ["STALE_DATA"]}]
    live.finish(cutoff)
    service.tick()
    exits = [event for event in service.snapshot()["events"] if event["change"] == "left"]
    assert len(exits) == 1
    assert exits[0]["frequency"] == "1m"
    assert not service.snapshot()["signals"]


def test_changed_algorithm_rechecks_a_closed_clock_once_without_new_market_bars(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 1
    live.state.update(source_current=False, source_revision="before-change")
    service.tick()
    assert len(live.started) == 2
    service.tick()
    assert len(live.started) == 2
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 2
    assert len(service.snapshot()["events"]) == 1


def test_monitor_snapshot_hides_old_algorithm_signals_until_each_symbol_is_rechecked(setup, monkeypatch):
    rows = [signal(), signal("SZ.000001")]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    live.rows = rows
    live.state.update(source_revision="old-code", current_source_revision="old-code")
    live.finish(cutoff)
    service._consume_monitor()
    assert len(service.snapshot()["signals"]) == 2
    before = deepcopy(service._load())

    live.state.update(source_current=False, current_source_revision="new-code")
    # Opening the page before the scheduler runs must not publish old-code
    # signals as current; hiding them must not create exits or mutate history.
    assert service.snapshot()["signals"] == []
    assert service._load() == before

    restarted = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    assert restarted.snapshot()["signals"] == []
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 1)
    restarted._schedule_monitor(clock[0], cutoff)
    for expected in (1, 2):
        code = live.started[-1]["symbols"][0]["code"]
        live.rows = [row for row in rows if row["code"] == code]
        live.state.update(source_revision="new-code", current_source_revision="new-code")
        live.finish(cutoff)
        restarted._consume_monitor()
        snapshot = restarted.snapshot()
        assert len(snapshot["signals"]) == expected
        assert len(snapshot["events"]) == 2
        assert not any(event["change"] == "left" for event in snapshot["events"])
        if expected == 1:
            restarted._schedule_monitor(clock[0], cutoff)


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


def test_committed_backlog_wakes_the_next_tick_without_waiting_for_new_prices(setup):
    rows = [signal(f"SH.{600000 + i}") for i in range(20)]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    live.rows = rows
    live.finish(cutoff)
    service._wake.clear()
    service._consume_monitor()
    assert len(service.snapshot()["signals"]) == 16
    assert service._wake.is_set()
    service._wake.clear()
    service._consume_monitor()
    assert len(service.snapshot()["signals"]) == 20
    assert not service._wake.is_set()
    service._consume_monitor()
    assert not service._wake.is_set()
    assert len(service.snapshot()["events"]) == 20


def test_small_monitor_batch_does_not_spawn_workers_without_symbols(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal(), signal("SZ.000001")])
    service.tick()
    assert len(live.started[0]["symbols"]) == 2
    assert live.started[0]["workers"] == 2


def test_only_manual_screening_prewarms_a_full_page_result(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cl_app.services.screening import ScreeningManager

    started = []
    monkeypatch.setattr("cl_app.services.screening.subprocess.Popen", lambda *a, **kw: Mock(pid=1234))
    monkeypatch.setattr("cl_app.services.screening_worker_runtime.process_identity",
                        lambda pid: {"alive": True, "birth": "fixture-owner"})
    monkeypatch.setattr("cl_app.services.screening.threading.Thread", lambda **kw:
                        SimpleNamespace(start=lambda: started.append(kw["args"][0])))
    manual = ScreeningManager(tmp_path / "manual")
    live = SignalMonitor(tmp_path / "live").monitor
    settings = {"scope": "symbols", "symbols": [{"code": "SH.600088", "market": "a", "name": "example"}]}
    manual.start(settings)
    live.start(settings)
    # The monitor consumes committed prefixes; a second full proof reader
    # competes with that path and has no page that needs its result cache.
    assert len(started) == 1
    assert started[0].parent == manual.root


def test_bounded_batches_prioritize_watchlist_then_rotate_all_pending_symbols(setup, monkeypatch):
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


def test_full_screening_keeps_the_previous_pool_live_with_bounded_workers(setup, monkeypatch):
    rows = [signal(f"SH.{600000 + i}") for i in range(30)]
    holding = {"market": "a", "code": rows[-1]["code"], "name": "holding", "origin": "holding"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [holding])
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    old_seed = service.snapshot()["seed"]["run_id"]
    live.rows = rows
    live.finish(cutoff)
    service.tick()
    service.tick()  # Consume both committed chunks of the initial 30-symbol batch.
    assert len(live.started) == 1

    manual.start(validate_settings({"scope": "all_a_watchlist", "workers": 12}))
    manual.rows = [signal("SZ.000001")]  # Partial full-scan output is not a new pool.
    service.check_now()
    service.tick()
    assert len(live.started) == 2
    assert live.started[-1]["workers"] == 2
    assert len(live.started[-1]["symbols"]) == 16
    assert live.started[-1]["symbols"][0] == holding
    assert service.snapshot()["seed"]["run_id"] == old_seed
    assert service.snapshot()["pending_symbols_count"] == 14
    assert all(s["code"] != "SZ.000001" for s in live.started[-1]["symbols"])
    service.tick()
    assert len(live.started) == 2  # Never overlap two monitor jobs.

    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 3
    assert len(live.started[-1]["symbols"]) == 14
    assert live.started[-1]["workers"] == 2
    live.finish(cutoff)
    service.tick()
    assert service.snapshot()["pending_symbols_count"] == 0
    assert len(live.started) == 3
    assert {s["code"] for job in live.started[1:] for s in job["symbols"]} == {r["code"] for r in rows}

    manual.rows = rows
    manual.finish(cutoff)
    service.tick()
    assert len(live.started) == 4
    assert len(live.started[-1]["symbols"]) == 30
    assert live.started[-1]["workers"] == 6
    assert service.snapshot()["seed"]["run_id"] == manual.state["run_id"]


def test_holdings_are_monitored_while_the_first_full_screening_is_still_running(setup, monkeypatch):
    service, manual, live, clock = setup
    holding = {"market": "a", "code": "SH.600088", "name": "holding", "origin": "holding"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [holding])
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal("SZ.000001")]
    service.tick()
    assert live.started[0]["symbols"] == [holding]
    assert live.started[0]["workers"] == 1
    assert service.snapshot()["seed"]["screening_symbols"] == []


def test_scheduled_full_scan_limits_monitor_workers_in_the_same_scheduler_tick(setup):
    rows = [signal(f"SH.{600000 + i}") for i in range(20)]
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    live.rows = rows
    live.finish(cutoff)
    service.tick()
    service.tick()
    assert len(manual.started) == len(live.started) == 1
    clock[0] = clock[0].replace(hour=15, minute=11)
    service.tick()
    assert len(manual.started) == len(live.started) == 2
    assert manual.state["status"] == "running"
    assert live.started[-1]["workers"] == 2
    assert len(live.started[-1]["symbols"]) == 16


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


def _stub_external_cutoff(monkeypatch, provider):
    monkeypatch.setattr("cl_app.services.signal_monitor.closed_bar_cutoff", lambda market, *args:
                        closed_bar_cutoff(market, *args) if market == "a" else provider(market, *args))


def test_watchlist_is_monitored_even_when_it_has_no_screening_signal(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    _stub_external_cutoff(monkeypatch, lambda *args: cutoff + 300)
    service.tick()
    assert live.started[0]["symbols"] == [_us_watch()]
    assert service.snapshot()["pool_markets"] == {"us": 1}
    assert service.snapshot()["seed"]["watchlist_count"] == 1


def test_watchlist_can_start_before_a_completed_screening_run(setup, monkeypatch):
    service, manual, live, clock = setup
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    _stub_external_cutoff(monkeypatch, lambda *args: 12300)
    service.tick()
    assert not manual.started
    assert live.started[0]["symbols"] == [_us_watch()]


def test_only_changed_market_runs_and_other_market_signals_survive(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    us_close = [cutoff]  # Both initial clocks must already be closed at 14:59.
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    _stub_external_cutoff(monkeypatch, lambda *args: us_close[0])
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
    live.rows = [{**us_signal,'source_closed_at':us_close[0]}]
    live.finish(us_close[0])
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
        return cutoff + 300

    _stub_external_cutoff(monkeypatch, context)
    service.tick()
    assert live.started[0]["symbols"] == [_us_watch()]
    assert service.snapshot()["errors"][0]["code"] == "FE.USDCNY"
    assert "交易时段" in service.snapshot()["last_error"]


def test_calendar_failure_hides_current_signals_and_recovery_rechecks_the_same_clock(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])
    failing = [False]

    def external_cutoff(*_args):
        if failing[0]:
            raise ValueError("SESSION_CALENDAR_UNAVAILABLE")
        return cutoff

    _stub_external_cutoff(monkeypatch, external_cutoff)
    service.tick()
    us_signal = {**signal("TSLA.US"), "market": "us"}
    live.rows = [signal(), us_signal]
    live.state["source_revision"] = "current-code"
    live.finish(cutoff)
    committed = live.committed_results
    live.committed_results = lambda *args: {**committed(*args), "verified_periods": [
        {**symbol, "frequency": "5m", "source_closed_at": cutoff} for symbol in live.started[-1]["symbols"]]}
    service.tick()
    assert {row["market"] for row in service.snapshot()["signals"]} == {"a", "us"}
    failing[0] = True
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], cutoff)
    assert {row["market"] for row in service.snapshot()["signals"]} == {"a"}
    failed = service.selection_overlay(manual.state["run_id"], "current-code")["us:TSLA.US:5m"]
    assert failed["error"] is True
    assert failed["identities"]
    assert not any(event["change"] == "left" for event in service.snapshot()["events"])
    failing[0] = False
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], cutoff)
    assert live.started[-1]["symbols"] == [_us_watch()]
    live.rows = [us_signal]
    live.state["source_revision"] = "current-code"
    live.finish(cutoff)
    service._consume_monitor()
    assert {row["market"] for row in service.snapshot()["signals"]} == {"a", "us"}
    assert service.selection_overlay(manual.state["run_id"], "current-code")["us:TSLA.US:5m"]["error"] is False
    assert len([event for event in service.snapshot()["events"] if event["change"] == "discovered"]) == 2


def test_calendar_recovery_preserves_pending_symbols_beyond_one_batch(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal(), signal("SZ.000001")])
    service.tick()
    live.rows = [signal(), signal("SZ.000001")]
    live.finish(cutoff)
    service.tick()
    original_cutoff = closed_bar_cutoff
    failing = [True]

    def calendar(market, code, *args):
        if failing[0]:
            raise ValueError("calendar offline")
        return original_cutoff(market, code, *args)

    monkeypatch.setattr("cl_app.services.signal_monitor.closed_bar_cutoff", calendar)
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 1)
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], None)
    assert len(service.snapshot()["schedule_errors"]) == 2
    failing[0] = False
    service._schedule_monitor(clock[0], cutoff)
    first = live.started[-1]["symbols"][0]["code"]
    assert len(service.snapshot()["force_pending_symbols"]) == 1
    live.rows = [signal(first)]
    live.finish(cutoff)
    service._consume_monitor()
    clock[0] += timedelta(seconds=60)
    service._schedule_monitor(clock[0], cutoff)
    assert live.started[-1]["symbols"][0]["code"] != first
    assert service.snapshot()["force_pending_symbols"] == []


def test_stale_worker_version_suspends_its_earlier_consumed_signals(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal(), signal("SZ.000001")])
    service.tick()
    live.rows = [signal()]
    live.committed = live.started[0]["symbols"][:1]
    live.state.update(completed=1, source_current=True, source_revision="old")
    service._consume_monitor()
    assert service.snapshot()["signals"]
    live.committed = live.started[0]["symbols"]
    live.state.update(completed=2, source_current=False, current_source_revision="new")
    service._consume_monitor()
    assert service.snapshot()["signals"] == []
    assert service._load()["unverified_signals"]
    checks = service.selection_overlay(manual.state["run_id"], "new")
    assert len(checks) == 2 and all(check["error"] for check in checks.values())
    assert not any(event["change"] == "left" for event in service.snapshot()["events"])


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
    _stub_external_cutoff(monkeypatch, lambda *args: cutoff + 300)
    restored = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restored, "start_runtime", lambda: None)
    restored.tick()
    assert live.started[-1]["symbols"] == [_us_watch()]
    assert len(live.started) == 2


def test_removed_watchlist_member_cannot_publish_an_inflight_signal(setup, monkeypatch):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    watch = [_us_watch()]
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: list(watch))
    _stub_external_cutoff(monkeypatch, lambda *args: cutoff + 300)
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
    _stub_external_cutoff(monkeypatch, lambda *args: cutoff)
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
    _stub_external_cutoff(monkeypatch, lambda *args: cutoff + 300)
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


def test_policy_update_rechecks_existing_signals_even_when_bars_are_unchanged(setup):
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
    assert len(live.started) == 2
    assert service.snapshot()["signals"] == []
    assert len(service._load()["unverified_signals"]) == 1
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 1


def _closed_seed(setup, rows):
    setup[3][0] = datetime(2026, 9, 19, 12, tzinfo=CN)
    service, manual, live, clock, cutoff = _seed(setup, rows)
    service.tick()
    return service, manual, live, clock, cutoff


@pytest.mark.parametrize("restored", [True, False])
def test_failed_evidence_recovers_without_duplicate_discovery_and_resolves_real_exit(setup, restored):
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal()])
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    service.check_now()
    service.tick()
    live.rows = []
    live.errors = [{"code": "SH.600088", "market": "a", "frequency": "5m", "reasons": ["STALE_DATA"]}]
    live.finish(cutoff)
    service.tick()
    snapshot = service.snapshot()
    assert not snapshot["signals"]
    assert "unverified_signals" not in snapshot
    assert len(snapshot["events"]) == 1
    assert snapshot["retry_symbols"]["a:SH.600088"]["retry_at"] == clock[0].timestamp() + 60
    clock[0] += timedelta(seconds=59)
    service.tick()
    assert len(live.started) == 2
    clock[0] += timedelta(seconds=1)
    service.tick()
    assert len(live.started) == 3
    live.errors = []
    live.rows = [signal()] if restored else []
    live.finish(cutoff)
    service.tick()
    snapshot = service.snapshot()
    assert not snapshot["retry_symbols"] and not snapshot["errors"]
    assert bool(snapshot["signals"]) is restored
    assert [event["change"] for event in snapshot["events"]] == (["discovered"] if restored else ["left", "discovered"])
    service.tick()
    assert len(live.started) == 3
    assert not service._load()["unverified_signals"]


def test_same_clock_failures_stop_after_three_attempts_and_new_bar_resets_budget(setup):
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal()])
    live.errors = [{"code": "SH.600088", "market": "a", "reasons": ["STALE_DATA"]}]
    for failures, delay in ((1, 60), (2, 120), (3, None)):
        live.finish(cutoff)
        service.tick()
        retry = service.snapshot()["retry_symbols"]["a:SH.600088"]
        assert retry["failures"] == failures
        assert len(live.started) == failures
        if delay is not None:
            clock[0] += timedelta(seconds=delay - 1)
            service.tick()
            assert len(live.started) == failures
            clock[0] += timedelta(seconds=1)
            service.tick()
            assert len(live.started) == failures + 1
        else:
            assert retry["retry_at"] is None
    clock[0] += timedelta(hours=2)
    service.tick()
    assert len(live.started) == 3
    clock[0] = datetime(2026, 9, 21, 9, 36, tzinfo=CN)
    service.tick()
    assert len(live.started) == 4
    assert not service.snapshot()["retry_symbols"]
    assert service.snapshot()["active_cutoffs"]["a:SH.600088"] > cutoff


def test_retry_survives_restart_and_manual_check_can_reset_an_exhausted_budget(setup, monkeypatch):
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal()])
    live.errors = [{"code": "SH.600088", "market": "a", "reasons": ["ENGINE_ERROR"]}]
    live.finish(cutoff)
    service.tick()
    clock[0] += timedelta(seconds=60)
    restarted = SignalMonitor(service.root, screening=manual, monitor=live, now=lambda: clock[0])
    monkeypatch.setattr(restarted, "start_runtime", lambda: None)
    restarted.tick()
    assert len(live.started) == 2
    live.finish(cutoff)
    restarted.tick()
    clock[0] += timedelta(seconds=120)
    restarted.tick()
    live.finish(cutoff)
    restarted.tick()
    assert restarted.snapshot()["retry_symbols"]["a:SH.600088"]["retry_at"] is None
    restarted.check_now()
    restarted.tick()
    assert len(live.started) == 4
    assert not restarted.snapshot()["retry_symbols"]


def test_interrupted_worker_retries_only_symbols_without_committed_results(setup):
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal(), signal("SZ.000001")])
    live.rows = [signal()]
    live.committed = live.started[0]["symbols"][:1]
    live.state.update(completed=1, source_current=True)
    service.tick()
    assert len(service.snapshot()["signals"]) == 1
    live.state.update(status="interrupted", error="worker exited")
    service.tick()
    snapshot = service.snapshot()
    assert set(snapshot["retry_symbols"]) == {"a:SZ.000001"}
    assert {row["code"] for row in snapshot["signals"]} == {"SH.600088"}
    clock[0] += timedelta(seconds=60)
    service.tick()
    assert [symbol["code"] for symbol in live.started[-1]["symbols"]] == ["SZ.000001"]
    live.rows = [signal("SZ.000001")]
    live.finish(cutoff)
    service.tick()
    assert not service.snapshot()["retry_symbols"]
    assert len(service.snapshot()["events"]) == len(service.snapshot()["signals"]) == 2


def test_pending_batches_continue_without_idle_interval_or_overlapping_workers(setup, monkeypatch):
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 1)
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal(), signal("SZ.000001")])
    assert len(live.started) == 1
    assert service.snapshot()["pending_symbols_count"] == 1
    assert service.snapshot()["next_check_at"] > clock[0].timestamp()
    service.tick()
    assert len(live.started) == 1  # An active worker still owns the batch.
    live.rows = [signal(live.started[-1]["symbols"][0]["code"])]
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 2  # Same wall clock, no artificial 60 s gap.
    assert live.started[0]["symbols"] != live.started[1]["symbols"]
    assert service.snapshot()["pending_symbols_count"] == 0
    live.rows = [signal(live.started[-1]["symbols"][0]["code"])]
    live.finish(cutoff)
    service.tick()
    assert len(live.started) == 2
    service._load()["pending_symbols_count"] = 1
    service._schedule_monitor(clock[0], cutoff)
    assert len(live.started) == 2
    assert service.snapshot()["pending_symbols_count"] == 0


def test_unchecked_symbols_take_priority_over_a_failed_watchlist_retry(setup, monkeypatch):
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 1)
    watch = {"market": "a", "code": "SH.600088", "name": "watch", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [watch])
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal(), signal("SZ.000001")])
    assert live.started[0]["symbols"] == [watch]
    live.errors = [{"code": watch["code"], "market": "a", "reasons": ["STALE_DATA"]}]
    live.finish(cutoff)
    service.tick()
    clock[0] += timedelta(seconds=60)
    service.tick()
    assert [symbol["code"] for symbol in live.started[-1]["symbols"]] == ["SZ.000001"]
    live.errors = []
    live.finish(cutoff)
    service.tick()
    clock[0] += timedelta(seconds=60)
    service.tick()
    assert live.started[-1]["symbols"] == [watch]


def test_missing_run_source_hash_cannot_force_unlimited_rescans(setup):
    service, manual, live, clock, cutoff = _closed_seed(setup, [signal()])
    live.state.update(status="failed", source_current=False, current_source_revision="new-version", error="startup failed")
    service.tick()
    assert len(live.started) == 2  # One recheck for this target version.
    for failure, delay in ((1, 60), (2, 120), (3, None)):
        live.state.update(status="failed", source_current=False, current_source_revision="new-version", error="startup failed")
        service.tick()
        assert service.snapshot()["retry_symbols"]["a:SH.600088"]["failures"] == failure
        assert len(live.started) == failure + 1
        if delay:
            clock[0] += timedelta(seconds=delay)
            service.tick()
    clock[0] += timedelta(hours=2)
    service.tick()
    assert len(live.started) == 4
    live.state["current_source_revision"] = "next-version"
    service.tick()
    assert len(live.started) == 5


def test_version_recheck_drains_every_batch_once_at_a_closed_clock(setup, monkeypatch):
    monkeypatch.setattr("cl_app.services.signal_monitor.MONITOR_BATCH_SIZE", 1)
    rows = [signal(), signal("SZ.000001"), signal("SH.600002")]
    service, manual, live, clock, cutoff = _closed_seed(setup, rows)
    for index in range(3):
        live.rows = [row for row in rows if row["code"] == live.started[-1]["symbols"][0]["code"]]
        live.finish(cutoff)
        service.tick()
        if index < 2:
            clock[0] += timedelta(seconds=60)
            service.tick()
    assert len(live.started) == 3
    live.state.update(source_current=False, source_revision="old-version", current_source_revision="new-version")
    service.tick()
    for index in range(3):
        live.rows = [row for row in rows if row["code"] == live.started[-1]["symbols"][0]["code"]]
        live.finish(cutoff)
        service.tick()
        if index < 2:
            clock[0] += timedelta(seconds=60)
            service.tick()
    assert len(live.started) == 6
    assert {s["code"] for request in live.started[3:] for s in request["symbols"]} == {row["code"] for row in rows}
    assert not service.snapshot()["force_pending_symbols"]
    assert len(service.snapshot()["events"]) == 3


def test_a_calendar_failure_does_not_stop_a_supported_non_a_watchlist(setup, monkeypatch):
    service, manual, live, clock = setup
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [_us_watch()])

    def cutoff(market, *_args):
        if market == "a":
            raise ValueError("calendar coverage unavailable")
        return 12300

    monkeypatch.setattr("cl_app.services.signal_monitor.closed_bar_cutoff", cutoff)
    service.tick()
    assert not manual.started
    assert live.started[0]["symbols"] == [_us_watch()]
    assert service.snapshot()["daily_schedule_error"]


def test_frozen_historical_screening_does_not_send_a_live_summary(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    notifications = Mock()
    notifications.has.return_value = False
    service._notifications = notifications
    service._load().update(dingtalk_enabled=True, notification_since=0, notification_follow_run=manual.state["run_id"])
    manual.state.update(source_current=False, frozen_native_review=True)
    service._publish_notifications(manual.status())
    notifications.screening_completed.assert_not_called()


def consolidation(code, cutoff, *, identity="pan", direction="up"):
    return {"market": "a", "code": code, "name": "held", "frequency": "5m", "source_closed_at": cutoff,
            "alert": {"alert_id": identity, "direction": direction, "anchor_price": 12.3,
                      "confirmed_at": cutoff, "available_at": cutoff, "center_id": "center",
                      "macd_area_a": 10., "macd_area_c": 5., "macd_area_ratio": .5,
                      "core_position": "breakout", "comparison_form": "entry_departure"}}


def _preview_service(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [held])
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service._notifications = notifications
    service._load().update(dingtalk_enabled=True, notification_since=0, consolidation_warning_enabled_at=0,
                           consolidation_enabled_at=0)
    service.tick()
    service._load()["holding_notification_since"]["a:SH.600088"] = 0
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    warning = consolidation(held["code"], cutoff)
    warning["alert"].update(status="warning", confirmed_at=None, hypothesis_id="same-open-leg")
    live.rows = [signal()]
    live.consolidation = [warning]
    live.finish(cutoff)
    service.tick()
    return service, live, cutoff, warning


def test_holding_warning_invalidation_reactivation_and_confirmation_share_one_identity(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    extra = lambda: [e for e in service.snapshot()["events"] if e.get("signal_type") == "consolidation_divergence"]
    first, = extra()
    assert first["stage"] == "warning" and first["external_notifications"]
    assert len(service._validate_notification_events([first])[0]) == 1
    def finish(rows, errors=()):
        service.check_now(); service.tick()
        live.consolidation = rows; live.errors = list(errors)
        live.finish(cutoff); service.tick()
    moved = deepcopy(warning)
    moved["alert"].update(alert_id="new-endpoint", anchor_price=12.2)
    finish([moved])
    assert len(extra()) == 1, 'endpoint movement alone must not repeat an alert'
    finish([])
    assert extra()[0]["stage"] == "invalidated" and extra()[0]["id"] == first["id"]
    assert len(service._validate_notification_events([extra()[0]])[0]) == 1
    assert service._validate_notification_events([first]) == ([], [])
    assert [e["point_type"] for e in service.snapshot()["signals"]] == ["3buy"]
    finish([moved])
    assert extra()[0]["stage"] == "warning" and extra()[0]["lifecycle_cycle"] == 1
    confirmed = deepcopy(moved)
    confirmed["alert"].update(status="confirmed", confirmed_at=cutoff, alert_id="final-endpoint")
    finish([confirmed])
    assert [e["stage"] for e in reversed(extra())] == ["warning", "invalidated", "warning", "confirmed"]
    assert len({e["id"] for e in extra()}) == 1
    assert len(service.snapshot()["signals"]) == 2


def test_invalidation_keeps_previous_proof_but_uses_new_check_evidence_for_its_chart(setup,monkeypatch):
    service,live,cutoff,warning=_preview_service(setup,monkeypatch)
    previous=next(v for v in service._load()['signals'].values() if v.get('stage')=='warning')
    previous['evidence']={'version':'previous'}
    original_run=previous['evidence_run_id']
    service.check_now();service.tick()
    committed=live.committed_results
    def results(*args,**kwargs):
        result=committed(*args,**kwargs)
        for row in result['verified_periods']:row['evidence']={'version':'current'}
        return result
    live.committed_results=results;live.consolidation=[];live.finish(cutoff);service._consume_monitor()
    event=next(v for v in service._load()['events'] if v.get('stage')=='invalidated')
    assert event['evidence']=={'version':'current'} and event['evidence_run_id']==live.state['run_id']
    assert event['invalidation_reference']['evidence']=={'version':'previous'}
    assert event['invalidation_reference']['evidence_run_id']==original_run


def test_existing_warning_sends_once_when_new_closed_data_crosses_enable_time(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    state = service._load()
    identity = next(k for k,v in state["signals"].items() if v.get("signal_type") == "consolidation_divergence")
    state["events"] = []
    state["known"][identity]["notification_authorized"] = False
    state["consolidation_warning_enabled_at"] = cutoff + 300
    clock = setup[3];clock[0] = clock[0].replace(hour=15,minute=1)
    fresh = cutoff + 300
    newer = deepcopy(warning)
    newer["source_closed_at"] = fresh;newer["alert"]["available_at"] = fresh
    service.tick();live.consolidation = [newer];live.finish(fresh);service.tick()
    events = [e for e in service.snapshot()["events"] if e.get("signal_type") == "consolidation_divergence"]
    assert len(events) == 1 and events[0]["external_notifications"] and events[0]["id"] == identity
    service.check_now();service.tick();live.finish(fresh);service.tick()
    assert len([e for e in service.snapshot()["events"] if e.get("signal_type") == "consolidation_divergence"]) == 1


def test_recovered_warning_requires_two_distinct_closed_checks_but_confirmation_is_immediate(setup,monkeypatch):
    service,live,cutoff,warning=_preview_service(setup,monkeypatch)
    def finish(rows,stamp):
        service.check_now();service.tick();live.consolidation=rows;live.finish(stamp);service.tick()
    finish([],cutoff)
    finish([warning],cutoff)
    row=next(r for r in service._load()['signals'].values() if r.get('signal_type')=='consolidation_divergence')
    assert row['warning_recovery_pending'] and row['warning_reactivated']
    assert service._load()['events'][0]['external_notifications'] is False
    before=len(service._load()['events']);finish([warning],cutoff)
    assert len(service._load()['events'])==before
    newer=deepcopy(warning);newer['source_closed_at']=cutoff+300;newer['alert']['available_at']=cutoff+300
    finish([newer],cutoff+300)
    assert service._load()['events'][0]['external_notifications'] is True
    assert service._load()['events'][0]['warning_reactivated']
    finish([],cutoff+300);finish([newer],cutoff+300)
    confirmed=deepcopy(newer);confirmed['alert'].update(status='confirmed',confirmed_at=cutoff+300)
    finish([confirmed],cutoff+300)
    event=service._load()['events'][0]
    assert event['stage']=='confirmed' and event['external_notifications'] is True


def test_pruning_retains_current_and_queued_notification_evidence(setup):
    import os,json
    service,_,live,_=setup
    live.root.mkdir(parents=True,exist_ok=True)
    paths=[]
    for i in range(6):
        path=live.root/f'{i:032x}';path.mkdir()
        (path/'status.json').write_text(json.dumps({'run_id':path.name,'status':'completed'}),encoding='utf-8')
        os.utime(path,(1000+i,1000+i));paths.append(path)
    service._load()['signals']={'active':{'evidence':{'status':'complete'},'evidence_run_id':paths[0].name}}
    service._notifications=Mock()
    service._notifications.referenced_runs.return_value={paths[1].name}
    service._prune_runs()
    assert paths[0].exists() and paths[1].exists() and not paths[2].exists()
    assert all(path.exists() for path in paths[3:])


def test_failed_holding_evidence_does_not_invalidate_a_warning(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    service.check_now(); service.tick()
    live.consolidation = []
    live.errors = [{"market": "a", "code": warning["code"], "frequency": "5m",
                    "signal_type": "consolidation_divergence", "reasons": ["CONSOLIDATION_EVIDENCE_MISSING"]}]
    live.finish(cutoff); service.tick()
    assert not any(e["stage"] == "invalidated" for e in service.snapshot()["events"])
    assert any(e["stage"] == "warning" for e in service._load()["unverified_signals"].values())


def test_missing_consolidation_check_never_becomes_a_negative_check(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    service.check_now(); service.tick()
    original = live.committed_results
    def missing(*args, **kwargs):
        result = original(*args, **kwargs)
        for period in result['verified_periods']:
            period.pop('consolidation_checked', None)
        return result
    live.committed_results = missing
    live.consolidation = []; live.errors = []; live.finish(cutoff); service.tick()
    assert not any(e['stage'] == 'invalidated' for e in service._load()['events'])
    assert any(e['stage'] == 'warning' for e in service._load()['unverified_signals'].values())
    assert any(e.get('reasons') == ['CONSOLIDATION_CHECK_MISSING'] for e in service._load()['errors'])
    assert any(e['point_type'] == '3buy' for e in service._load()['signals'].values())
    service.check_now(); service.tick()
    live.committed_results = original; live.consolidation = [warning]; live.finish(cutoff); service.tick()
    assert not service._load()['unverified_signals']
    assert len([e for e in service._load()['events'] if e.get('signal_type') == 'consolidation_divergence']) == 1


def test_confirmed_comparison_retains_the_warned_hypothesis_over_an_alternate_form(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    identity = next(s["id"] for s in service.snapshot()["signals"] if s.get("signal_type") == "consolidation_divergence")
    matched = deepcopy(warning)
    matched["alert"].update(status="confirmed", confirmed_at=cutoff, comparison_form="center_oscillation")
    alternate = deepcopy(matched)
    alternate["alert"].update(hypothesis_id="alternate-entry", alert_id="alternate", comparison_form="entry_departure")
    service.check_now(); service.tick()
    live.consolidation = [alternate, matched];live.finish(cutoff);service.tick()
    rows = [s for s in service.snapshot()["signals"] if s.get("signal_type") == "consolidation_divergence"]
    assert len(rows) == 1 and rows[0]["id"] == identity and rows[0]["stage"] == "confirmed"
    assert not any(e["stage"] == "invalidated" for e in service.snapshot()["events"])


def test_legacy_confirmed_holding_identity_migrates_without_another_notification(setup, monkeypatch):
    service, live, cutoff, warning = _preview_service(setup, monkeypatch)
    from cl_app.services.signal_monitor import _consolidation_signal
    old = deepcopy(warning)
    old["alert"].pop("hypothesis_id")
    old["alert"].update(status="confirmed", confirmed_at=cutoff)
    legacy = _consolidation_signal(old)
    state = service._load()
    state["signals"] = {legacy["id"]: legacy}
    state["unverified_signals"] = {}
    state["known"] = {legacy["id"]: {"stage": "confirmed", "updated_at": cutoff}}
    state["events"] = []
    newer = deepcopy(old); newer["alert"]["hypothesis_id"] = "same-open-leg"
    service.check_now(); service.tick()
    live.consolidation = [newer]; live.finish(cutoff); service.tick()
    assert not [e for e in service.snapshot()["events"] if e.get("signal_type") == "consolidation_divergence"]
    assert any(e.get("hypothesis_id") == "same-open-leg" for e in service.snapshot()["signals"])


def test_holdings_receive_additional_alerts_without_non_holding_leak_or_duplicate_delivery(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    other = {"market": "a", "code": "SZ.000001", "name": "watch", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [held])
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [held, other])
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service._notifications = notifications
    service._load().update(dingtalk_enabled=True, notification_since=0)
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.rows = [signal()]
    live.consolidation = [consolidation(held["code"], cutoff), consolidation(other["code"], cutoff)]
    live.finish(cutoff)
    service.tick()
    snapshot = service.snapshot()
    assert snapshot["seed"]["holding_count"] == 1
    assert len(snapshot["signals"]) == 2
    assert all(row["code"] == held["code"] for row in snapshot["signals"])
    historical = [event for event in snapshot["events"] if event.get("signal_type") == "consolidation_divergence"]
    assert historical and historical[0]["external_notifications"] is False
    clock[0] = clock[0].replace(hour=15, minute=1)
    service.tick()
    fresh_cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.consolidation = [consolidation(held["code"], fresh_cutoff, identity="new-pan")]
    live.finish(fresh_cutoff)
    service.tick()
    delivered = [event for call in notifications.signal_events.call_args_list for event in call.args[0]
                 if event.get("signal_type") == "consolidation_divergence"]
    assert delivered and all(event["id"] == delivered[0]["id"] for event in delivered)
    assert all(event.get("external_notifications") is True for event in delivered)
    count = len(service.snapshot()["events"])
    service.check_now()
    service.tick()
    live.finish(fresh_cutoff)
    service.tick()
    assert len(service.snapshot()["events"]) == count


def test_removing_a_holding_retains_its_buy_sell_signal_but_stops_extra_alerts(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    holdings = [held]
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: list(holdings))
    monkeypatch.setattr("cl_app.services.signal_monitor.watchlist_symbols", lambda: [held])
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.rows = [signal()]
    live.consolidation = [consolidation(held["code"], cutoff)]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 2
    holdings.clear()
    service.tick()
    assert service.snapshot()["holding_symbols"] == []
    assert len(service.snapshot()["signals"]) == 1
    assert service.snapshot()["signals"][0]["point_type"] == "3buy"
    service.check_now()
    service.tick()
    live.finish(cutoff)
    service.tick()
    assert all(row.get("signal_type") != "consolidation_divergence" for row in service.snapshot()["signals"])


def test_unavailable_holding_group_does_not_create_false_consolidation_exit_and_rechecks_recovery(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    failed = [False]

    def holdings():
        if failed[0]:
            raise OSError("group read failed")
        return [held]

    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", holdings)
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.rows = [signal()]
    live.consolidation = [consolidation(held["code"], cutoff)]
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 2
    service.check_now()
    service.tick()
    failed[0] = True
    live.finish(cutoff)
    service.tick()
    assert [row["point_type"] for row in service.snapshot()["signals"]] == ["3buy"]
    assert not any(row["change"] == "left" for row in service.snapshot()["events"])
    assert any(row.get("signal_type") == "consolidation_divergence" for row in service._load()["unverified_signals"].values())
    runs = len(live.started)
    failed[0] = False
    service.tick()
    assert len(live.started) == runs + 1
    live.finish(cutoff)
    service.tick()
    assert len(service.snapshot()["signals"]) == 2
    assert len([row for row in service.snapshot()["events"] if row["change"] == "discovered"]) == 2


@pytest.mark.parametrize("error_frequency", ["5m", None])
def test_extra_comparison_failure_does_not_block_a_successful_buy_sell_exit(setup, monkeypatch, error_frequency):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [held])
    service.tick()
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    live.rows = [signal()]
    live.consolidation = [consolidation(held["code"], cutoff)]
    live.finish(cutoff)
    service.tick()
    service.check_now()
    service.tick()
    live.rows = []
    live.consolidation = []
    live.errors = [{"market": "a", "code": held["code"], "frequency": error_frequency,
                    "signal_type": "consolidation_divergence", "reasons": ["CONSOLIDATION_EVIDENCE_MISSING"]}]
    live.finish(cutoff)
    service.tick()
    exits = [event for event in service.snapshot()["events"] if event["change"] == "left"]
    assert len(exits) == 1 and exits[0]["point_type"] == "3buy"
    assert not service.snapshot()["signals"]
    assert service._load()["unverified_signals"]


def test_holding_group_is_monitored_even_when_other_watchlists_are_excluded(setup, monkeypatch):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [held])
    service._load()["settings"]["daily_scope"] = "latest"
    service._load()["settings"]["screening"]["scope"] = "all_a"
    service.tick()
    assert live.started[0]["symbols"] == [held]
    assert service.snapshot()["holding_symbols"] == ["a:SH.600088"]


def test_verified_historical_pool_is_only_a_recheck_list_and_does_not_publish_saved_signals(setup):
    service, manual, live, clock = setup
    manual.start(validate_settings({"scope": "all_a_watchlist"}))
    manual.rows = [signal()]
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    manual.finish(cutoff)
    manual.state.update(source_current=False, frozen_native_review=True, result_reader_revision="reader-v2")
    service.tick()
    assert service.snapshot()["seed"]["historical_pool_requires_recheck"] is True
    assert live.started[0]["symbols"][0]["code"] == "SH.600088"
    assert service.snapshot()["signals"] == []


def test_selection_overlay_preserves_successful_evidence_through_failure_and_recovery(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.state["source_revision"] = "current-code"
    live.finish(cutoff)
    live.committed_results = lambda *_: {**live.results(), "processed_symbols": live.started[-1]["symbols"],
                                       "results_through": 1, "verified_periods": [
        {"market": "a", "code": "SH.600088", "frequency": "5m", "source_closed_at": cutoff}]}
    service.tick()
    good = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert good["identities"]
    assert service.selection_overlay(manual.state["run_id"], "old-code") == {}
    assert service.selection_overlay("other-run", "current-code") == {}
    service.check_now()
    service.tick()
    live.rows = []
    live.state["source_revision"] = "current-code"
    live.errors = [{"market": "a", "code": "SH.600088", "frequency": "5m", "reasons": ["STALE_DATA"]}]
    live.finish(cutoff)
    service.tick()
    failed = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert failed["error"] is True
    assert failed["identities"] == good["identities"]
    assert failed["checked_at"] == good["checked_at"]
    assert failed["failed_cutoff"] == cutoff
    service.check_now()
    service.tick()
    live.state["source_revision"] = "current-code"
    live.errors = []
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    recovered = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert recovered["error"] is False
    assert recovered["identities"] == good["identities"]
    assert len([event for event in service.snapshot()["events"] if event["change"] == "discovered"]) == 1


@pytest.mark.parametrize("worker_failed", [False, True])
def test_first_unsuccessful_monitor_check_suspends_candidates_without_exit_evidence(setup, worker_failed):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.state["source_revision"] = "current-code"
    if worker_failed:
        live.state.update(status="failed", error="worker exited")
    else:
        live.errors = [{"market": "a", "code": "SH.600088", "frequency": "5m", "reasons": ["STALE_DATA"]}]
        live.finish(cutoff)
    service.tick()
    failed = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert failed["error"] is True
    assert failed["failed_cutoff"] == cutoff
    assert "cutoff" not in failed and "identities" not in failed
    assert not any(event["change"] == "left" for event in service.snapshot()["events"])


def test_older_successful_period_does_not_undo_a_newer_candidate_exit(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    verified_cutoff = [cutoff]
    live.state["source_revision"] = "current-code"
    live.finish(cutoff)
    live.committed_results = lambda *_: {**live.results(), "processed_symbols": live.started[-1]["symbols"],
                                       "results_through": 1, "verified_periods": [
        {"market": "a", "code": "SH.600088", "frequency": "5m", "source_closed_at": verified_cutoff[0]}]}
    service.tick()
    exited = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert exited["identities"] == []
    service.check_now()
    service.tick()
    live.state["source_revision"] = "current-code"
    verified_cutoff[0] -= 300
    live.rows = [signal()]
    live.finish(cutoff)
    service.tick()
    latest = service.selection_overlay(manual.state["run_id"], "current-code")["a:SH.600088:5m"]
    assert latest["identities"] == exited["identities"]
    assert latest["cutoff"] == exited["cutoff"]
    assert latest["error"] is True
    assert service.snapshot()["signals"] == []
    assert service.snapshot()["errors"][0]["reasons"] == ["REGRESSED_SOURCE_CUTOFF"]
    assert not any(event["change"] == "discovered" for event in service.snapshot()["events"])
