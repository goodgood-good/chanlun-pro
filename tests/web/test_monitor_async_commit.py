"""The controller yields while frozen proofs are read, retaining control guards."""

import threading
import time
import pytest
from tests.web.test_signal_monitor import setup as setup, _seed, signal


@pytest.mark.parametrize("disable", [False, True])
def test_background_committed_read_yields_and_cannot_publish_after_disable(
    setup, monkeypatch, disable
):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    live.background_committed_reads = True
    original = live.committed_results
    entered, release = threading.Event(), threading.Event()

    def read(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(live, "committed_results", read)
    try:
        began = time.monotonic()
        service._consume_monitor()
        assert time.monotonic() - began < 0.5 and entered.wait(1)
        assert not service._load().get("monitor_cursor", {}).get("completed")
        if disable:
            service._load()["enabled"] = False
            service._save()
    finally:
        release.set()
    for _ in range(100):
        service._consume_monitor()
        if (
            service._load().get("monitor_cursor", {}).get("completed")
            or service._load().get("processed_run") == live.state["run_id"]
        ):
            break
        time.sleep(0.01)
    if disable:
        assert not service._load().get("monitor_cursor", {}).get("completed")
    else:
        assert service._load()["monitor_cursor"]["completed"] == 1
