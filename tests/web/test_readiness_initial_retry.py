"""A brief metadata/quote startup race must not impose the steady-state interval."""

import threading
import time
from types import SimpleNamespace

import pytest

from cl_app.services.readiness import ReadinessRegistry, start_ticks_warmup


@pytest.mark.parametrize("failure", ["metadata_not_ready", "empty_quote"])
def test_initial_transient_failure_recovers_before_the_default_thirty_second_interval(
    failure,
):
    registry = ReadinessRegistry()
    first = threading.Event()
    recovered = threading.Event()
    attempts = []

    def probe(_market):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            first.set()
            if failure == "metadata_not_ready":
                raise RuntimeError("default market code is not ready")
            return {}
        recovered.set()
        return {"SH.000001": {"last": 3000}}

    handle = start_ticks_warmup(registry, probe)
    try:
        assert first.wait(1)
        assert not registry.ticks_snapshot("a")["ready"]
        assert recovered.wait(2.5), (
            "a dependency that recovered immediately still waited thirty seconds"
        )
        deadline = time.monotonic() + 0.5
        while not registry.ticks_snapshot("a")["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert registry.ticks_snapshot("a")["ready"]
        assert len(attempts) == 2
    finally:
        handle.stop()
        handle.join(0.5)
    assert not handle.is_alive()


def test_success_after_startup_failure_returns_to_the_normal_refresh_interval():
    registry = ReadinessRegistry()
    refreshed = threading.Event()
    attempts = []

    def probe(_market):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return {}
        if len(attempts) == 3:
            refreshed.set()
        return {"SH.000001": {"last": 3000}}

    handle = start_ticks_warmup(
        registry, probe, retry_seconds=0.3, initial_retry_seconds=0.01
    )
    try:
        assert refreshed.wait(1.5)
        assert attempts[2] - attempts[1] >= 0.25
        assert registry.ticks_snapshot("a")["ready"]
    finally:
        handle.stop()
        handle.join(0.5)


def test_persistent_failure_backs_off_to_a_bounded_interval_without_becoming_ready(monkeypatch):
    from cl_app.services import readiness

    registry = ReadinessRegistry()
    reached = threading.Event()
    attempts = []
    delays = []
    stop = threading.Event()

    class StopEvent:
        def is_set(self):
            return stop.is_set()

        def set(self):
            stop.set()

        def wait(self, timeout):
            # Verify requested backoff, independent of the Windows monotonic
            # clock's 15.625 ms granularity for these deliberately short delays.
            delays.append(timeout)
            if len(delays) == 8:
                stop.set()
                reached.set()
            return stop.is_set()

    created = []

    def event_factory():
        if not created:
            created.append(True)
            return StopEvent()
        return threading.Event()

    # Replace this module's factory, never threading.Event process-wide.
    monkeypatch.setattr(readiness, 'threading', SimpleNamespace(Event=event_factory, Thread=threading.Thread))

    def probe(_market):
        attempts.append(_market)
        return {}

    handle = start_ticks_warmup(
        registry, probe, retry_seconds=0.04, initial_retry_seconds=0.01
    )
    try:
        assert reached.wait(5), "the retry worker did not complete its attempts"
        assert delays == pytest.approx([0.01, 0.02, 0.04, 0.04, 0.04, 0.04, 0.04, 0.04])
        assert len(attempts) == 8
        assert not registry.ticks_snapshot("a")["ready"]
    finally:
        handle.stop()
        handle.join(0.5)
