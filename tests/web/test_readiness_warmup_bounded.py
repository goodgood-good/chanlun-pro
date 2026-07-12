import threading
import time
from types import SimpleNamespace

from cl_app.services.readiness import ReadinessRegistry, start_metadata_warmup, start_ticks_warmup


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_ticks_warmup_records_timeout_and_stop_does_not_wait_for_hung_probe():
    registry = ReadinessRegistry()
    entered = threading.Event()
    release = threading.Event()

    def probe(_market):
        entered.set()
        release.wait(timeout=2)
        return [{"code": "SH.000001"}]

    handle = start_ticks_warmup(
        registry,
        probe,
        market="a",
        retry_seconds=0.01,
        attempt_timeout=0.05,
    )
    try:
        assert entered.wait(timeout=1)
        assert _wait_until(lambda: registry.ticks_snapshot("a")["status"] == "error")
        snapshot = registry.ticks_snapshot("a")
        assert snapshot["ready"] is False
        assert snapshot["error"]["code"] == "warmup_timeout"

        handle.stop()
        handle.join(timeout=0.5)
        assert handle.is_alive() is False
    finally:
        release.set()


def test_ticks_warmup_retries_empty_result_then_records_success():
    registry = ReadinessRegistry()
    calls = []

    def probe(_market):
        calls.append(True)
        return [] if len(calls) == 1 else [{"code": "SH.000001"}]

    handle = start_ticks_warmup(
        registry,
        probe,
        market="a",
        retry_seconds=0.01,
        attempt_timeout=0.2,
    )
    try:
        assert _wait_until(
            lambda: registry.ticks_snapshot("a")["status"] == "ok"
            and len(calls) >= 3
        )
        assert handle.is_alive() is True
    finally:
        handle.stop()
        handle.join(timeout=0.5)
    assert handle.is_alive() is False


def test_tick_success_becomes_stale_without_a_fresh_probe():
    now = [0.0]
    registry = ReadinessRegistry(
        tick_success_ttl=30.0,
        clock=lambda: now[0],
    )
    registry.record_ticks_success("a")

    assert registry.ticks_snapshot("a")["ready"] is True
    now[0] = 30.0
    assert registry.ticks_snapshot("a") == {
        "required": True,
        "ready": False,
        "status": "stale",
        "error": None,
    }


def test_metadata_warmup_handle_can_stop_while_mapping_attempt_is_hung():
    entered = threading.Event()
    release = threading.Event()

    class BlockingMap:
        def __getitem__(self, _market):
            entered.set()
            release.wait(timeout=2)
            return ["ready"]

        def status(self, _market):
            return {"state": "unloaded", "ready": False}

    constants = SimpleNamespace(
        market_frequencys=BlockingMap(),
        market_default_codes=BlockingMap(),
    )
    handle = start_metadata_warmup(
        constants,
        "a",
        retry_seconds=0.01,
        attempt_timeout=0.05,
    )
    try:
        assert entered.wait(timeout=1)
        handle.stop()
        handle.join(timeout=0.5)
        assert handle.is_alive() is False
    finally:
        release.set()
