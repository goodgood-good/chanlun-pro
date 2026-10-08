"""Fault boundaries and concurrent controls, using an isolated outbox/transport."""

from copy import deepcopy
from datetime import datetime, timezone
import threading

import pytest

from cl_app.services.signal_monitor import SignalMonitor, _signal
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport
from tests.web.test_holding_risk import reference, frame, snapshot
from tests.web.test_signal_monitor import Jobs, signal


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path / "runs")
    stock = {"market": "a", "code": "SH.600000", "name": "fixture"}
    settings = {"symbols": [stock], "frequencies": ["5m"], "point_types": ["3buy"]}
    jobs.start(settings)
    jobs.finish(300)
    jobs.state["source_revision"] = "test"
    jobs.rows = [{**signal("SH.600000"), "source_closed_at": 300}]
    clock = [310.0]
    transport = Transport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=transport, now=lambda: clock[0]
    )

    def monitor():
        service = SignalMonitor(
            tmp_path,
            screening=jobs,
            monitor=jobs,
            notifications=box,
            now=lambda: datetime.fromtimestamp(clock[0], timezone.utc),
        )
        box.signal_validator = service._validate_notification_events
        return service

    service = monitor()
    state = service._load()
    state.update(
        enabled=True,
        dingtalk_enabled=True,
        notification_since=0,
        holding_source_current=True,
        active_seed="seed",
        active_monitor_run=jobs.state["run_id"],
        active_symbols=["a:SH.600000"],
        active_cutoffs={"a:SH.600000": 300},
        seed={"run_id": "seed", "codes": ["a:SH.600000"], "settings": settings},
    )
    service._save()
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff", lambda *args: 300
    )
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.holding_symbols", lambda: [stock]
    )
    return service, jobs, box, transport, clock, monitor


def test_exit_before_state_commit_never_exposes_an_uncommitted_notification(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, restart = scenario
    with monkeypatch.context() as patch:
        patch.setattr(
            service,
            "_save",
            lambda: (_ for _ in ()).throw(SystemExit("fault before commit")),
        )
        with pytest.raises(SystemExit):
            service._consume_monitor()
    assert box.snapshot()["pending"] == 0
    recovered = restart()
    recovered._consume_monitor()
    box.deliver_one()
    assert len(transport.messages) == 1


def test_exit_after_state_commit_recovers_enqueue_and_delivers_exactly_once(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, restart = scenario
    with monkeypatch.context() as patch:
        patch.setattr(
            box,
            "signal_events",
            lambda *a, **k: (_ for _ in ()).throw(SystemExit("fault before enqueue")),
        )
        with pytest.raises(SystemExit):
            service._consume_monitor()
    recovered = restart()
    assert len(recovered._load()["events"]) == 1
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    clock[0] += 5
    recovered._publish_notifications({"status": "idle"})
    box.deliver_one()
    assert len(transport.messages) == 1 and box.snapshot()["pending"] == 0


def test_legacy_queued_event_waits_for_unconsumed_state_after_restart(scenario):
    service, jobs, box, transport, clock, restart = scenario
    row = {
        **_signal(jobs.rows[0]),
        "recorded_at": clock[0],
        "run_id": jobs.state["run_id"],
        "change": "discovered",
    }
    box.signal_events([row], since=0)
    recovered = restart()
    box.deliver_one()
    assert box.snapshot()["pending"] == 1 and not transport.messages
    recovered._consume_monitor()
    clock[0] += 6
    box.deliver_one()
    assert len(transport.messages) == 1 and box.snapshot()["pending"] == 0


def test_disk_write_failure_discards_uncommitted_in_memory_events(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, restart = scenario
    service._load()["events"] = [{"id": "memory-only"}]
    with monkeypatch.context() as patch:
        patch.setattr(
            "cl_app.services.signal_monitor.write_json",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")),
        )
        with pytest.raises(OSError):
            service._save()
    assert service._load()["events"] == []


def install_risk(scenario):
    service, jobs, *_ = scenario
    state = service._load()
    state["holding_symbols"] = ["a:SH.600000"]
    state["holding_risk_plans"] = {"a:SH.600000:5m": reference()}
    committed = jobs.committed_results

    def result(*a, **k):
        value = committed(*a, **k)
        for p in value["verified_periods"]:
            p["evidence"] = {"fixture": True}
        return value

    jobs.committed_results = result
    return state


def test_reverse_replay_failure_does_not_block_stop_and_retries_are_bounded(
    scenario, monkeypatch
):
    service, jobs, box, transport, clock, _ = scenario
    state = install_risk(scenario)
    monkeypatch.setattr(
        "cl_app.services.holding_risk.source",
        lambda *a: (frame(), snapshot(), {}, {}, {}, {}),
    )
    monkeypatch.setattr(
        "cl_app.services.holding_risk.verify_opposites",
        lambda *a: (_ for _ in ()).throw(ValueError("bad reverse replay")),
    )
    for attempt in range(1, 4):
        if attempt > 1:
            jobs.start(jobs.state["settings"])
            jobs.finish(300)
            jobs.state["source_revision"] = "test"
            state["active_monitor_run"] = jobs.state["run_id"]
            state["monitor_cursor"] = {}
            clock[0] += 120
        service._consume_monitor()
        assert state["retry_symbols"]["a:SH.600000"]["failures"] == attempt
    plan = state["holding_risk_plans"]["a:SH.600000:5m"]
    assert plan["status"] == "stop_triggered" and plan["stop_triggered_at"] == 200
    assert (
        plan["reverse_check_pending"]
        and state["retry_symbols"]["a:SH.600000"]["retry_at"] is None
    )
    assert len([e for e in state["events"] if e["point_type"] == "risk_stop"]) == 1


def test_risk_replay_does_not_block_state_reads_and_unbinding_discards_inflight_result(
    scenario, monkeypatch
):
    service, jobs, *_ = scenario
    state = install_risk(scenario)
    started, release = threading.Event(), threading.Event()
    errors = []

    def source(*args):
        started.set()
        assert release.wait(5)
        return frame(), snapshot(), {}, {}, {}, {}

    monkeypatch.setattr("cl_app.services.holding_risk.source", source)
    monkeypatch.setattr(service, "holding_risks", lambda: {})

    def consume():
        try:
            service._consume_monitor()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert started.wait(5)
        assert service._lock.acquire(timeout=0.5)
        service._lock.release()
        service.configure_holding_risk({"action": "unbind", "binding_id": "b" * 32})
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert not state["holding_risk_plans"]["a:SH.600000:5m"]["active"]
    assert not any(e["point_type"] == "risk_stop" for e in state["events"])


def test_older_binding_request_cannot_overwrite_a_newer_binding(scenario, monkeypatch):
    service, jobs, *_ = scenario
    state = service._load()
    key = "a:SH.600000:5m"
    base = {"market": "a", "code": "SH.600000", "frequency": "5m"}
    state.update(
        holding_symbols=["a:SH.600000"],
        holding_evidence={key: {**base, "source_closed_at": 300}},
    )
    started, release = threading.Event(), threading.Event()
    errors = []

    def bind(_root, _record, identity, _now):
        if identity == "first":
            started.set()
            assert release.wait(5)
        return {**reference(), "signal_id": identity, "binding_id": identity}

    monkeypatch.setattr(
        service,
        "holding_risks",
        lambda: {
            "holdings": [
                {"choices": [{**base, "signal_id": s} for s in ("first", "second")]}
            ]
        },
    )
    monkeypatch.setattr("cl_app.services.holding_risk.bind", bind)

    def first():
        try:
            service.configure_holding_risk(
                {"action": "bind", "signal_id": "first", "source_closed_at": 300}
            )
        except ValueError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert started.wait(5)
        service.configure_holding_risk(
            {"action": "bind", "signal_id": "second", "source_closed_at": 300}
        )
        accepted = deepcopy(state["holding_risk_plans"][key])
    finally:
        release.set()
        thread.join(5)
    assert (
        state["holding_risk_plans"][key] == accepted
        and accepted["signal_id"] == "second"
    )
    assert len(errors) == 1 and "其他操作" in errors[0]
