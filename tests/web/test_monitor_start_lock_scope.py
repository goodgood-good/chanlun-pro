"""Worker startup I/O must not hold state access or lose newer check requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from cl_app.services import holding_risk, signal_monitor
from tests.web.test_monitor_control_ownership import monitor as monitor
from tests.web.test_monitor_status_lock_scope import schedule


def slow_start(manager, monkeypatch, *, failed=False):
    entered, release = threading.Event(), threading.Event()
    launched = deepcopy(manager.start.return_value)

    def start(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        if failed:
            raise OSError("controlled startup failure")
        return launched

    monkeypatch.setattr(manager.start, "side_effect", start)
    return entered, release


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_worker_startup_leaves_monitor_state_readable(monitor, monkeypatch, kind):
    service, manual, live, _state = monitor
    entered, release = slow_start(manual if kind == "daily" else live, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            readable = service._lock.acquire(timeout=0.2)
            if readable:
                service._lock.release()
            assert readable, "worker startup held the shared state lock"
        finally:
            release.set()
        starting.result(timeout=2)


@pytest.mark.parametrize("already_forced", [False, True])
def test_check_requested_during_startup_is_committed_and_not_consumed_by_old_plan(monitor, monkeypatch, already_forced):
    service, manual, live, state = monitor
    state["force"] = already_forced
    entered, release = slow_start(live, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(schedule(service, manual, "live"))
        try:
            assert entered.wait(2)
            pool.submit(service.check_now).result(timeout=0.3)
            assert state["force"] is True
        finally:
            release.set()
        starting.result(timeout=2)
    assert state["active_monitor_run"] == live.start.return_value["run_id"]
    assert state["force"] is True, "startup acknowledged a newer full-check request"


@pytest.mark.parametrize("already_pending", [False, True])
def test_new_binding_check_survives_startup_even_for_an_already_queued_symbol(monitor, monkeypatch, already_pending):
    service, manual, live, state = monitor
    symbol = {"market": "a", "code": "SH.600000", "frequency": "5m"}
    key = "a:SH.600000"
    slot = key + ":5m"
    choice = {**symbol, "signal_id": "new-choice"}
    record = {**symbol, "source_closed_at": 300, "evidence_run_id": "a" * 32}
    state.update(holding_symbols=[key], holding_source_current=True,
                 force_pending_symbols=[key] if already_pending else [])
    state["holding_evidence"][slot] = record
    monkeypatch.setattr(service, "holding_risks", lambda: {"holdings": [{"choices": [choice]}]})
    monkeypatch.setattr(signal_monitor, "holding_symbols", lambda: [symbol])
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: 300)
    plan = {**choice, "binding_id": "new-binding", "active": True}
    monkeypatch.setattr(holding_risk, "bind", lambda *args: deepcopy(plan))
    entered, release = slow_start(live, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(schedule(service, manual, "live"))
        try:
            assert entered.wait(2)
            binding = pool.submit(service.configure_holding_risk,
                                  {"action": "bind", "signal_id": choice["signal_id"], "source_closed_at": 300})
            binding.result(timeout=0.3)
        finally:
            release.set()
        starting.result(timeout=2)
    assert state["holding_risk_plans"][slot]["binding_id"] == "new-binding"
    assert key in state["force_pending_symbols"], "startup consumed a newer binding check"
    assert state["pending_symbols_count"] >= 1


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_start_failure_keeps_pending_checks_and_releases_control_serialization(monitor, monkeypatch, kind):
    service, manual, live, state = monitor
    state.update(force=True, force_pending_symbols=["a:SH.600000"])
    manager = manual if kind == "daily" else live
    manager.start.side_effect = OSError("controlled startup failure")
    before = deepcopy(state["attempted_cutoffs"])
    with pytest.raises(signal_monitor.TaskStartError, match="startup failure") as failure:
        schedule(service, manual, kind)()
    assert isinstance(failure.value.__cause__, OSError)
    assert state["force"] is True and state["force_pending_symbols"] == ["a:SH.600000"]
    assert state["attempted_cutoffs"] == before
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(service.enable, {}).result(timeout=0.5)


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_enabling_new_settings_waits_until_startup_is_owned(monitor, monkeypatch, kind):
    service, manual, live, state = monitor
    manager = manual if kind == "daily" else live
    entered, release = slow_start(manager, monkeypatch)
    enabled = threading.Event()
    monkeypatch.setattr(service, "start_runtime", enabled.set)
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            changing = pool.submit(service.enable, {"interval_seconds": 120})
            assert not enabled.wait(0.2)
        finally:
            release.set()
        starting.result(timeout=2)
        changing.result(timeout=2)
    assert state["settings"]["interval_seconds"] == 120
    if kind == "live":
        assert state["active_monitor_run"] == manager.start.return_value["run_id"]


def test_new_check_survives_a_concurrent_start_failure(monitor, monkeypatch):
    service, manual, live, state = monitor
    state["force"] = False
    entered, release = slow_start(live, monkeypatch, failed=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(schedule(service, manual, "live"))
        try:
            assert entered.wait(2)
            pool.submit(service.check_now).result(timeout=0.3)
        finally:
            release.set()
        with pytest.raises(signal_monitor.TaskStartError) as failure:
            starting.result(timeout=2)
        assert isinstance(failure.value.__cause__, OSError)
    assert state["force"] is True


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_pause_observes_the_started_run_before_canceling(monitor, monkeypatch, kind):
    service, manual, live, state = monitor
    manager = manual if kind == "daily" else live
    entered, release = slow_start(manager, monkeypatch)
    canceled = threading.Event()

    def cancel():
        if kind == "live":
            assert state["active_monitor_run"] == manager.start.return_value["run_id"]
        else:
            assert state["daily"]["afternoon"]["run_id"] == manager.start.return_value["run_id"]
        canceled.set()

    live.cancel.side_effect = cancel
    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            pausing = pool.submit(service.pause)
            assert not canceled.wait(0.2)
        finally:
            release.set()
        starting.result(timeout=2)
        pausing.result(timeout=2)
    assert canceled.is_set() and state["enabled"] is False


def test_dispatch_preserves_existing_calendar_failure_scheduling(monitor, monkeypatch):
    service, manual, live, state = monitor
    blocked = {"market": "us", "code": "BLOCKED.US", "name": "blocked"}
    state["seed"]["symbols"].append(blocked)
    state["seed"]["codes"].append("us:BLOCKED.US")
    state.update(force=True, force_pending_symbols=["us:BLOCKED.US"])

    def cutoff(*args):
        raise OSError("controlled calendar unavailable")

    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", cutoff)
    schedule(service, manual, "live")()
    assert [row["code"] for row in live.start.call_args.args[0]["symbols"]] == ["SH.600000"]
    assert state["force_pending_symbols"] == []
    assert state["pending_symbols_count"] == 0
    assert state["schedule_errors"][0]["code"] == "BLOCKED.US"


def test_two_schedulers_do_not_launch_overlapping_monitor_batches(monitor):
    service, manual, live, _state = monitor
    entered, release = threading.Event(), threading.Event()
    launched = deepcopy(live.start.return_value)

    def start(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        live.status.return_value = launched
        return launched

    live.start.side_effect = start
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(schedule(service, manual, "live"))
        try:
            assert entered.wait(2)
            second = pool.submit(schedule(service, manual, "live"))
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert live.start.call_count == 1


@pytest.mark.parametrize("kind", ["daily", "live"])
def test_shutdown_during_startup_still_records_the_launched_run(monitor, monkeypatch, kind):
    import json

    service, manual, live, _state = monitor
    manager = manual if kind == "daily" else live
    entered, release = slow_start(manager, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(schedule(service, manual, kind))
        try:
            assert entered.wait(2)
            service.stop_runtime()
        finally:
            release.set()
        starting.result(timeout=2)
    persisted = json.loads((service.root / "state.json").read_text(encoding="utf-8"))
    run_id = persisted["active_monitor_run"] if kind == "live" else persisted["daily"]["afternoon"]["run_id"]
    assert run_id == manager.start.return_value["run_id"]
