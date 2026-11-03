"""A stale async prefix must promptly drain newer committed records, not borrow proof."""
from copy import deepcopy
import threading

import pytest

from tests.web.test_monitor_incremental_priority import monitor as monitor


def active_prefix(monitor):
    service, _, live, clock = monitor
    state = service._load()
    stocks = [{"market": "a", "code": f"SH.{600100 + i}", "name": "sample"}
              for i in range(64)]
    state["seed"].update(symbols=stocks, screening_symbols=stocks,
                         codes=["a:" + stock["code"] for stock in stocks])
    state["seed"]["settings"].update(symbols=stocks, frequencies=["5m"])
    state["dingtalk_enabled"] = False
    service._schedule_monitor(clock[0], None)
    assert len(live.state["settings"]["symbols"]) == 64
    live.state.update(completed=16, cutoffs={"5m": int(clock[0].timestamp()) // 300 * 300})
    live.committed = live.state["settings"]["symbols"][:16]
    prefix = live.committed_results()
    assert prefix["completed"] == prefix["results_through"] == 16
    live.background_committed_reads = True
    return service, live, prefix


@pytest.mark.parametrize("committed,wake", [(64, True), (16, False)])
def test_older_async_prefix_wakes_only_when_same_run_has_more_committed(monitor, committed, wake):
    service, live, prefix = active_prefix(monitor)
    entered, release = threading.Event(), threading.Event()

    def delayed(_after):
        entered.set()
        assert release.wait(timeout=3)
        return deepcopy(prefix)

    live.committed_results = delayed
    try:
        service._consume_monitor()
        assert entered.wait(timeout=1)
        # The proof remains the earlier 16-record prefix while the actual
        # manager has committed more work before its next status poll.
        live.state["completed"] = committed
        if committed == 64:
            live.state["status"] = "completed"
        release.set()
        service._monitor_result_reader._future.result(timeout=3)
        service._wake.clear()  # Ignore the reader's completion callback.
        service._consume_monitor()
        assert service._wake.is_set() is wake
        state = service._load()
        assert state["monitor_cursor"]["completed"] == 16
        assert state.get("processed_run") != prefix["run_id"]
        assert len(state["selection_checks"]) == 16
    finally:
        release.set()
        service._monitor_result_reader.close()


@pytest.mark.parametrize("mismatch", ["run_id", "source_revision", "current_source_revision"])
def test_unrelated_status_count_never_wakes_an_older_prefix(monitor, monkeypatch, mismatch):
    service, live, prefix = active_prefix(monitor)
    live.state.update(completed=64)
    live.state[mismatch] = "other"
    # Fault-inject an old completed reader bundle: even if one reaches the
    # consumption boundary, an unrelated manager count cannot extend it.
    monkeypatch.setattr(service._monitor_result_reader, "read", lambda *_: (deepcopy(prefix), {}, {}))
    service._wake.clear()
    service._consume_monitor()
    assert not service._wake.is_set()
    assert service._load()["monitor_cursor"]["completed"] == 16


def test_empty_prefix_never_spins_against_a_newer_committed_count(monitor, monkeypatch):
    service, live, prefix = active_prefix(monitor)
    live.state["completed"] = 64
    prefix.update(results_through=0, processed_symbols=[], verified_periods=[])
    monkeypatch.setattr(service._monitor_result_reader, "read", lambda *_: (prefix, {}, {}))
    service._wake.clear()
    service._consume_monitor()
    assert not service._wake.is_set()
    assert service._load()["monitor_cursor"]["completed"] == 0
