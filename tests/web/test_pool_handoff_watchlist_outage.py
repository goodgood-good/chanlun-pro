"""A daily pool replacement must not erase an unreadable watchlist/holding source."""

import pytest

from tests.web.test_signal_monitor import setup as setup, signal
from tests.web.test_monitor_pool_handoff_reuse import checked_pool, finish_replacement


@pytest.mark.parametrize("membership", ["watchlist", "holding", "watchlist_overlap"])
def test_saved_independent_member_survives_source_outage_during_pool_handoff(
    setup, monkeypatch, membership
):
    service, manual, live, _, cutoff = checked_pool(setup)
    row = {"market": "us", "code": "TSLA.US", "name": "Tesla", "origin": "watchlist"}
    if membership == "watchlist_overlap":
        row = {
            "market": "a",
            "code": "SH.600088",
            "name": "watched",
            "origin": "watchlist",
        }
    key = f"{row['market']}:{row['code']}"
    source = "cl_app.services.signal_monitor." + (
        "holding_symbols" if membership == "holding" else "watchlist_symbols"
    )
    monkeypatch.setattr(source, lambda: [row])
    service._sync_watchlist()
    old_next = service._load()["next_check_at"] = 9999999999
    finish_replacement(manual, manual.state["settings"], cutoff, [signal("SZ.000001")])

    def unavailable():
        raise OSError("local group database unavailable")

    monkeypatch.setattr(source, unavailable)
    service.tick()
    state = service._load()
    assert set(state["seed"]["codes"]) == {"a:SZ.000001", key}
    assert state["watchlist_error"]
    assert state["next_check_at"] < old_next
    assert (
        state["seed"]["holding_count" if membership == "holding" else "watchlist_count"]
        == 1
    )
    assert any(symbol["code"] == row["code"] for symbol in state["seed"]["symbols"])
    assert any(symbol["code"] == "SZ.000001" for symbol in live.started[-1]["symbols"])
    monkeypatch.setattr(source, lambda: [])
    service._sync_watchlist()
    assert state["seed"]["codes"] == ["a:SZ.000001"]
