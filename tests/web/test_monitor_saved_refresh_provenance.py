"""Re-reading a saved screening cannot replace the live worker's proof."""

from copy import deepcopy
from datetime import timedelta

import pytest

from tests.web.test_signal_monitor import setup as setup, _seed, signal


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("saved_offset", [-300, 0, 300])
def test_saved_reader_upgrade_preserves_live_provenance_until_recheck(
    setup, historical, saved_offset
):
    row = signal()
    row["point"]["evidence_scope"] = "native_center_signal"
    service, manual, live, clock, cutoff = _seed(setup, [row])
    service.tick()
    live_row = {
        **deepcopy(row),
        "source_closed_at": cutoff,
        "evidence": {"input_fingerprint": "a" * 64},
    }
    live.rows = [live_row]
    live.state.update(source_revision="code", current_source_revision="code")
    live.finish(cutoff)
    service._consume_monitor()
    original = deepcopy(next(iter(service._load()["signals"].values())))
    identity = original["id"]
    original["notification_timeline"] = {
        "detected_at": int(clock[0].timestamp()),
        "source_closed_at": cutoff,
    }
    service._load()["signals"][identity] = deepcopy(original)
    assert original["evidence_run_id"] == live.state["run_id"]

    saved = {
        **deepcopy(row),
        "source_closed_at": cutoff + saved_offset,
        "evidence": {"input_fingerprint": "b" * 64},
        "latest_close": 999,
    }
    manual.rows = [saved]
    manual.state.update(
        source_current=not historical,
        frozen_native_review=historical,
        result_reader_revision="new-reader",
    )
    clock[0] += timedelta(seconds=10)
    service.tick()
    state = service._load()
    assert identity not in state["signals"]
    assert state["unverified_signals"][identity] == original
    valid, pending = service._validate_notification_events([original])
    assert valid == [] and pending == [original]

    live.rows = [{**live_row, "evidence": {"input_fingerprint": "c" * 64}}]
    live.state.update(source_revision="code", current_source_revision="code")
    live.finish(cutoff)
    service._consume_monitor()
    refreshed = service._load()["signals"][identity]
    assert (
        refreshed["evidence_run_id"]
        == live.state["run_id"]
        != original["evidence_run_id"]
    )
    assert refreshed["evidence"]["input_fingerprint"] == "c" * 64
    assert refreshed["first_detected_at"] == original["first_detected_at"]
    assert identity not in service._load()["unverified_signals"]
