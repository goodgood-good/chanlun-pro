from unittest.mock import Mock
import threading
import time

from tests.web.test_monitor_recovery import scenario as scenario


def test_error_record_failure_does_not_kill_the_loop_and_storage_can_recover(
    scenario, monkeypatch
):
    from cl_app.services import signal_monitor as module

    service, *_ = scenario
    original = module.write_json
    writes = []
    ticks = []

    def write(*args, **kwargs):
        writes.append(1)
        if len(writes) <= 2:
            raise OSError("temporary write failure")
        return original(*args, **kwargs)

    def tick():
        ticks.append(1)
        service._load()["recovered_marker"] = True
        service._save()
        service._stop.set()

    monkeypatch.setattr(module, "write_json", write)
    monkeypatch.setattr(service, "tick", tick)
    monkeypatch.setattr(service._wake, "wait", lambda _timeout: None)
    monkeypatch.setattr(service._input_retention, "schedule", Mock())
    service._loop()
    assert len(ticks) == 2 and service._last_tick_error is None
    assert service._last_tick_at is not None
    service._state = None
    assert service._load()["recovered_marker"] is True


def test_recovered_scheduler_error_is_cleared_without_erasing_another_diagnostic(
    scenario, monkeypatch
):
    service, *_ = scenario
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("temporary scheduler error")
        assert (
            service._load()["scheduler_error"]["message"] == "temporary scheduler error"
        )
        service._load()["last_error"] = "independent provider limitation"
        service._stop.set()

    monkeypatch.setattr(service, "tick", tick)
    monkeypatch.setattr(service._wake, "wait", lambda _: None)
    monkeypatch.setattr(service._input_retention, "schedule", Mock())
    service._loop()
    assert "scheduler_error" not in service._load()
    assert service._load()["last_error"] == "independent provider limitation"


def test_business_liveness_is_nonblocking_and_distinguishes_recovery_needs(scenario):
    service, _, box, *_ = scenario
    service._thread = Mock()
    service._thread.is_alive.return_value = True
    box._thread = Mock()
    box._thread.is_alive.return_value = True
    service._runtime_started_at = service._last_cycle_at = time.monotonic()
    service._last_tick_at = service.now().timestamp()
    result = []
    done = threading.Event()

    def read():
        result.append(service.runtime_health())
        done.set()

    with service._lock:
        probe = threading.Thread(target=read)
        probe.start()
        assert done.wait(1), "health probe must not block behind the monitor state lock"
    probe.join()
    assert result[0]["ready"] and not result[0]["recovery_recommended"]
    service._last_tick_error = "OSError"
    assert service.runtime_health()["status"] == "degraded"
    assert not service.runtime_health()["recovery_recommended"]
    service._last_tick_error = None
    service._last_cycle_at -= 181
    assert (
        service.runtime_health()["status"] == "stalled"
        and service.runtime_health()["recovery_recommended"]
    )
    service._last_cycle_at = time.monotonic()
    box._thread.is_alive.return_value = False
    assert (
        not service.runtime_health()["ready"]
        and service.runtime_health()["recovery_recommended"]
    )


def test_unrelated_failed_batch_does_not_hold_current_verified_notification(scenario):
    service, jobs, box, transport, *_ = scenario
    service._consume_monitor()
    event = service._load()["events"][0]
    jobs.state.update(
        status="interrupted",
        run_id="b" * 32,
        source_current=False,
        source_revision=None,
        current_source_revision="test",
    )
    valid, pending = service._validate_notification_events([event])
    assert len(valid) == 1 and not pending
    box.deliver_one()
    assert len(transport.messages) == 1
    jobs.state["current_source_revision"] = "a-new-algorithm"
    assert service._validate_notification_events([event]) == ([], [event])
