"""Reinterpreting already checked candles must not replay historical alerts."""

from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_signal_monitor import setup as setup, _seed, signal


@pytest.mark.parametrize(
    "advance,new_revision,expected",
    [(0, "new", False), (300, "new", True), (0, "old", True)],
)
@pytest.mark.parametrize("failed_first", [False, True])
def test_algorithm_reclassification_is_quiet_but_new_market_confirmation_is_not(
    setup, monkeypatch, advance, new_revision, expected, failed_first
):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    first = signal()
    first["source_closed_at"] = cutoff
    first["point"]["available_at"] = cutoff - 300
    live.rows = [first]
    live.state.update(source_revision="old", current_source_revision="old")
    live.finish(cutoff)
    service._consume_monitor()
    key = "a:SH.600088:5m"
    assert service._load()["selection_checks"][key]["revision"] == "old"

    live.start(live.started[-1])
    clock[0] += timedelta(seconds=advance)
    state = service._load()
    state.update(
        active_monitor_run=live.state["run_id"],
        active_consumed_symbols=[],
        monitor_cursor={},
    )
    if failed_first:
        service._mark_selection_failures(
            ["a:SH.600088"],
            int(clock[0].timestamp()),
            new_revision,
            cutoffs={"a:SH.600088": cutoff + advance},
        )
    service._save()
    if failed_first:
        service = SignalMonitor(
            service.root, screening=manual, monitor=live, now=lambda: clock[0]
        )
        monkeypatch.setattr(service, "start_runtime", lambda: None)
    changed = deepcopy(first)
    changed["source_closed_at"] = cutoff + advance
    changed["point"].update(point_id="reclassified", available_at=cutoff + advance)
    live.rows = [changed]
    live.state.update(
        source_revision=new_revision, current_source_revision=new_revision
    )
    live.finish(cutoff + advance)
    service._consume_monitor()
    events = [
        e for e in service._load()["events"] if e.get("point_id") == "reclassified"
    ]
    assert len(events) == 1
    assert events[0].get("external_notifications", True) is expected
    assert any(
        p["point_id"] == "reclassified" for p in service._load()["signals"].values()
    )
    assert service._load()["selection_checks"][key]["revision"] == new_revision
    assert "notification_baseline" not in service._load()["selection_checks"][key]
