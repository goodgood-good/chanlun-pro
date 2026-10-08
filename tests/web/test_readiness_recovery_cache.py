"""Cache healthy readiness while promptly rechecking failed startup probes."""

from concurrent.futures import Future
from types import SimpleNamespace
import time

import pytest

from app import NativeReadinessRunner


class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future


@pytest.mark.parametrize("initially_ready", [False, True])
def test_recovered_startup_is_rechecked_without_shortening_healthy_cache(
    initially_ready,
):
    calls = []
    healthy = [initially_ready]

    def snapshot(kind, market):
        calls.append((kind, market))
        return (
            {"status": "ready" if healthy[0] else "not_ready"},
            200 if healthy[0] else 503,
        )

    runner = NativeReadinessRunner(
        SimpleNamespace(extensions={"health_snapshot": snapshot}), ImmediateExecutor()
    )
    first = runner.submit("a").result()
    # Age only this cached record; no global clock patch or real sleep is needed.
    runner._cached["a"] = (time.monotonic() - 1.1, first)
    healthy[0] = True
    result = runner.submit("a").result()
    assert result[0]["status"] == "ready"
    assert len(calls) == (1 if initially_ready else 2)
