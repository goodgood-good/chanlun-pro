"""Recorded stale candidates must leave the opportunity window, not the chart."""

from pathlib import Path
from copy import deepcopy

import pandas as pd

from chanlun.screening import runner, rules
from chanlun.screening.confirmation import confirmation_catalog, native_selection_lifetime_reasons


def test_sz_002611_confirmed_successor_closes_first_and_second_buy_window():
    frame = pd.read_parquet(Path("tests/fixtures/native_timeliness/SZ.002611_5m.parquet"))
    snapshot = runner.snapshot_for(frame, "SZ.002611", "5m")
    points = deepcopy(snapshot["levels"][0]["points"])
    context = rules.trading_context(frame.date.iloc[-1], "5m", 20, 60)
    result = rules.audit_native_center_signals(snapshot, frame, context, max_anchor_gain_pct=100)
    following = confirmation_catalog(snapshot)
    for kind in ("1buy", "2buy"):
        old = next(point for point in points if point["point_type"] == kind)
        assert following[old["point_id"]]["state"] == "completed"
        assert old["point_id"] not in {candidate["point"]["point_id"] for candidate in result["selected"]}
        rejected = next(candidate for candidate in result["rejected"] if candidate["point"]["point_id"] == old["point_id"])
        assert "CONFIRMING_SEGMENT_COMPLETED" in rejected["reasons"]
    assert snapshot["levels"][0]["points"] == points
    assert not result["observations"]


def test_only_a_completed_successor_ends_the_window_and_child_progression_never_revives_first():
    first = {"point_id": "first", "point_type": "1buy", "anchor_at": 10, "available_at": 20,
             "structural_level": 0, "price_basis_revision": "basis"}
    child = {**first, "point_id": "second", "point_type": "2buy", "parent_point_id": "first",
             "anchor_at": 15, "available_at": 20, "status": "confirmed", "confirmed_at": 20,
             "missing_conditions": []}
    assert native_selection_lifetime_reasons(first, [first], {"first": {"state": "in_progress"}}, {}) == []
    assert native_selection_lifetime_reasons(first, [first], {"first": {"state": "completed"}}, {}) == ["CONFIRMING_SEGMENT_COMPLETED"]
    assert native_selection_lifetime_reasons(first, [first, child], {"first": {"state": "in_progress"}}, {"second": True}) == ["FIRST_ADVANCED_TO_SECOND"]
    assert native_selection_lifetime_reasons(first, [first, child], {}, {"second": False}) == []
