from datetime import datetime, timedelta
from copy import deepcopy

import pytest

from cl_app.services.signal_monitor import SignalMonitor, _signal
from chanlun.screening.rules import CN
from tests.web.test_signal_monitor import Jobs, signal
from tests.web.test_screening_workbench import fixture_run as fixture_run


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 7, 1, 0, tzinfo=CN)]
    closed = {
        "a": int(clock[0].timestamp()) - 86400,
        "us": int(clock[0].timestamp()) - 60,
    }
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff",
        lambda market, *args: closed[market],
    )
    manual, live = Jobs(tmp_path / "manual"), Jobs(tmp_path / "runs")
    service = SignalMonitor(
        tmp_path, screening=manual, monitor=live, now=lambda: clock[0]
    )
    stocks = [
        {"market": "a", "code": f"SH.{600000 + i}", "name": str(i)} for i in range(20)
    ]
    stocks.append(
        {"market": "us", "code": "LIVE.US", "name": "live", "origin": "watchlist"}
    )
    state = service._load()
    state.update(
        enabled=True,
        force=True,
        holding_symbols=["a:SH.600000"],
        seed={
            "run_id": "seed",
            "symbols": stocks,
            "codes": [f"{s['market']}:{s['code']}" for s in stocks],
            "settings": {"frequencies": ["5m"], "point_types": ["3buy"], "workers": 4},
        },
        attempted_cutoffs={
            f"{s['market']}:{s['code']}": closed[s["market"]] for s in stocks
        },
    )
    service._save()
    return service, manual, live, clock, closed


def finish(service, live, cutoff, *, cancelled=False, committed=None):
    if committed is not None:
        live.committed = committed
    live.finish(cutoff)
    live.state["completed"] = len(
        live.committed
        if live.committed is not None
        else live.state["settings"]["symbols"]
    )
    if cancelled:
        live.state["status"] = "cancelled"
    service._consume_monitor()


def background(mixed):
    service, _, live, clock, closed = mixed
    service._schedule_monitor(clock[0], None)
    finish(service, live, closed["us"])
    service._schedule_monitor(clock[0], None)
    return service, live, clock, closed


def test_live_clock_is_checked_before_closed_market_rebuild_even_if_closed_stock_is_held(
    mixed,
):
    service, _, live, clock, _ = mixed
    service._schedule_monitor(clock[0], None)
    assert [s["code"] for s in live.started[0]["symbols"]] == ["LIVE.US"]
    assert service._load()["pending_symbols_count"] == 20
    assert len(service._load()["force_pending_symbols"]) == 20


def test_background_batches_are_small_while_another_market_has_live_bars(mixed):
    service, live, _clock, _closed = background(mixed)
    assert 0 < len(live.started[-1]["symbols"]) <= 8
    assert service._load()["active_monitor_mode"] == "background"
    assert service._load()["pending_symbols_count"] > 0


def test_new_bar_yields_background_then_requeues_unfinished_symbols_without_false_failures(
    mixed,
):
    mixed[0]._load()["settings"]["interval_seconds"] = 600
    service, live, clock, closed = background(mixed)
    active = deepcopy(live.started[-1]["symbols"])
    untouched = _signal({**signal(active[-1]["code"]), "source_closed_at": closed["a"]})
    service._load()["signals"] = {untouched["id"]: untouched}
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    assert clock[0].timestamp() < service._load()["next_check_at"]
    service._schedule_monitor(clock[0], None)
    assert live.state.get("cancel_requested") is True
    assert len(live.started) == 2, (
        "must not overlap the still-running background worker"
    )
    finish(service, live, closed["a"], cancelled=True, committed=active[:2])
    state = service._load()
    assert not state["retry_symbols"] and not state["unverified_signals"]
    assert untouched["id"] in state["signals"]
    assert {f"a:{s['code']}" for s in active[2:]}.issubset(
        state["force_pending_symbols"]
    )
    service._schedule_monitor(clock[0], None)
    assert [s["code"] for s in live.started[-1]["symbols"]] == ["LIVE.US"]


def test_no_new_bar_does_not_cancel_work_and_background_eventually_drains(mixed):
    service, live, clock, closed = background(mixed)
    checked = set()
    for _ in range(8):
        if live.state["status"] == "running":
            service._schedule_monitor(clock[0], None)
            assert not live.state.get("cancel_requested")
            checked.update(s["code"] for s in live.started[-1]["symbols"])
            finish(service, live, closed["a"])
        service._schedule_monitor(clock[0], None)
        if live.state["status"] == "completed":
            break
    assert checked == {f"SH.{600000 + i}" for i in range(20)}
    assert service._load()["pending_symbols_count"] == 0
    assert not service._load()["force_pending_symbols"]


def test_restart_reissues_a_durable_yield_intent_and_keeps_unfinished_work(
    mixed, monkeypatch
):
    service, manual, live, clock, closed = mixed
    service, live, clock, closed = background(mixed)
    unfinished = {f"{s['market']}:{s['code']}" for s in live.started[-1]["symbols"]}
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    with monkeypatch.context() as fault:
        fault.setattr(
            live,
            "cancel",
            lambda: (_ for _ in ()).throw(OSError("isolated flag write failure")),
        )
        with pytest.raises(OSError):
            service._schedule_monitor(clock[0], None)
    restored = SignalMonitor(
        service.root, screening=manual, monitor=live, now=lambda: clock[0]
    )
    assert restored._load()["yielding_monitor_run"] == live.state["run_id"]
    restored._schedule_monitor(clock[0], None)
    assert live.state.get("cancel_requested")
    finish(restored, live, closed["a"], cancelled=True, committed=[])
    assert unfinished.issubset(restored._load()["force_pending_symbols"])
    restored._schedule_monitor(clock[0], None)
    assert [s["code"] for s in live.started[-1]["symbols"]] == ["LIVE.US"]


def test_a_naturally_completed_batch_wins_a_racing_yield_request(mixed):
    service, live, clock, closed = background(mixed)
    completed = {f"{s['market']}:{s['code']}" for s in live.started[-1]["symbols"]}
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    service._schedule_monitor(clock[0], None)
    finish(service, live, closed["a"])
    assert not (completed & set(service._load()["force_pending_symbols"]))
    assert service._load().get("yielding_monitor_run") is None
    assert not service._load()["retry_symbols"]


def test_recent_live_work_is_not_cancelled_when_another_bar_closes(mixed):
    service, _, live, clock, closed = mixed
    service._schedule_monitor(clock[0], None)
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    service._schedule_monitor(clock[0], None)
    assert not live.state.get("cancel_requested") and len(live.started) == 1


def test_unowned_cancellation_still_reports_unchecked_work_for_retry(mixed):
    service, live, _clock, closed = background(mixed)
    active = {f"{s['market']}:{s['code']}" for s in live.started[-1]["symbols"]}
    finish(service, live, closed["a"], cancelled=True, committed=[])
    assert set(service._load()["retry_symbols"]) == active


def test_yielded_completed_prefix_is_fully_consumed_before_replacement(mixed):
    service, _, live, clock, closed = mixed
    # No recent clocks initially: a large closed-market batch is permitted.
    closed["us"] = closed["a"]
    service._load()["attempted_cutoffs"]["us:LIVE.US"] = closed["us"]
    service._schedule_monitor(clock[0], None)
    assert len(live.started[-1]["symbols"]) == 21
    active = live.started[-1]["symbols"]
    closed["us"] = int(clock[0].timestamp())
    service._schedule_monitor(clock[0], None)
    finish(service, live, closed["a"], cancelled=True, committed=active[:20])
    assert service._load()["monitor_cursor"]["completed"] == 16
    service._schedule_monitor(clock[0], None)
    assert len(live.started) == 1
    service._consume_monitor()
    assert service._load()["monitor_cursor"]["completed"] == 20
    service._schedule_monitor(clock[0], None)
    assert [s["code"] for s in live.started[-1]["symbols"]] == ["LIVE.US"]


def test_bound_shorter_period_can_yield_a_background_batch(mixed, monkeypatch):
    service, live, clock, closed = background(mixed)
    service._load()["seed"]["settings"]["frequencies"] = ["30m"]
    service._load()["holding_risk_plans"] = {
        "us:LIVE.US:1m": {
            "market": "us",
            "code": "LIVE.US",
            "frequency": "1m",
            "active": True,
        }
    }
    called = []

    def cutoff(market, code, now, frequency):
        called.append((market, frequency))
        return (
            int(now.timestamp())
            if market == "us" and frequency == "1m"
            else closed[market]
        )

    monkeypatch.setattr("cl_app.services.signal_monitor.closed_bar_cutoff", cutoff)
    service._schedule_monitor(clock[0], None)
    assert ("us", "1m") in called and live.state.get("cancel_requested")


def test_frequent_new_bars_cannot_restart_background_work_forever_without_progress(
    mixed,
):
    service, live, clock, closed = background(mixed)
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    service._schedule_monitor(clock[0], None)
    finish(service, live, closed["a"], cancelled=True, committed=[])
    assert service._load()["background_empty_yields"] == 1
    service._schedule_monitor(clock[0], None)
    finish(service, live, closed["us"])
    service._schedule_monitor(clock[0], None)
    assert service._load()["active_monitor_mode"] == "background"
    clock[0] += timedelta(minutes=5)
    closed["us"] += 300
    service._schedule_monitor(clock[0], None)
    assert not live.state.get("cancel_requested")
    live.committed = live.started[-1]["symbols"][:1]
    live.state.update(completed=1, source_current=True)
    service._consume_monitor()
    service._schedule_monitor(clock[0], None)
    assert live.state.get("cancel_requested")
    finish(service, live, closed["a"], cancelled=True, committed=live.committed)
    assert service._load()["background_empty_yields"] == 0


def test_real_result_reader_consumes_cancelled_committed_evidence_before_requeue(
    fixture_run, tmp_path
):
    import json
    from chanlun.screening.runner import write_json
    from cl_app.services.dingtalk_notifications import DingTalkOutbox
    from tests.web.test_dingtalk_notifications import Transport

    manager, run_id, _ = fixture_run
    path = manager.root / run_id / "status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    write_json(path, {**status, "status": "cancelled"})
    records = manager.root / run_id / "results.jsonl"
    record = json.loads(records.read_text(encoding="utf-8"))
    record["rows"][0]["source_closed_at"] = 130
    records.write_text(json.dumps(record) + "\n", encoding="utf-8")
    service = SignalMonitor(
        tmp_path / "monitor-state",
        screening=Jobs(tmp_path / "manual"),
        monitor=manager,
        notifications=DingTalkOutbox(tmp_path / "outbox.sqlite", transport=Transport()),
    )
    state = service._load()
    state.update(
        enabled=True,
        active_seed="seed",
        active_monitor_run=run_id,
        yielding_monitor_run=run_id,
        active_monitor_mode="background",
        active_symbols=["a:SH.600088", "a:SH.600001"],
        active_cutoffs={"a:SH.600088": 130, "a:SH.600001": 130},
        seed={
            "run_id": "seed",
            "codes": ["a:SH.600088", "a:SH.600001"],
            "settings": {"frequencies": ["5m"], "point_types": ["3buy"]},
        },
    )
    untouched = _signal(signal("SH.600001"))
    state["signals"] = {untouched["id"]: untouched}
    service._save()
    service._consume_monitor()
    assert (
        state["processed_run"] == run_id and state["monitor_cursor"]["completed"] == 1
    )
    assert state["force_pending_symbols"] == ["a:SH.600001"]
    assert {row["code"] for row in state["signals"].values()} == {
        "SH.600088",
        "SH.600001",
    }
    assert not state["retry_symbols"] and not state["unverified_signals"]
