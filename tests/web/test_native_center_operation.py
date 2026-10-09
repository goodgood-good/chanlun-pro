"""End-to-end regression for the selected 7/23 operational center."""
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from chanlun.screening.runner import snapshot_for
from chanlun.screening.rules import _native_center_signal_source_valid, native_consolidation_source_valid


def stamp(value):
    return int(pd.Timestamp(value, tz="Asia/Shanghai").timestamp())


@pytest.fixture(scope="module")
def frame():
    return pd.read_parquet(Path("tests/fixtures/SH.513100_5m_center_20261009.parquet"))


@pytest.fixture(scope="module")
def snapshot(frame):
    return snapshot_for(frame, "SH.513100", "5m", include_consolidation=True)


def selected(snapshot):
    record = next((r for r in snapshot["center_view_selections"]
                   if (r["original_zd_tick"], r["original_zg_tick"]) == (2150, 2270)), None)
    if record is None:
        return None, None, None
    center = next(c for c in snapshot["levels"][0]["centers"] if c["center_id"] == record["selected_center_id"])
    point = next(p for p in snapshot["levels"][0]["points"] if p["center_id"] == center["center_id"] and p["point_type"] == "3sell")
    return record, center, point


def valid(snapshot, point):
    centers = {c["center_id"]: c for c in snapshot["levels"][0]["centers"]}
    return _native_center_signal_source_valid(snapshot, point, centers, snapshot["screening_segments"])


def test_selected_center_ends_and_subsequent_prices_belong_to_distinct_centers(snapshot):
    record, center, point = selected(snapshot)
    assert (center["core"]["zd_tick"], center["core"]["zg_tick"]) == (2154, 2232)
    assert center["state"] == "completed" and center["third_class_confirmed"]
    assert center["points"][-1]["time"] == stamp("2026-07-10 09:50")
    assert center["completion_return_segment"]["high_tick"] == 2153
    assert point["anchor_at"] == stamp("2026-07-23 13:35")
    assert point["anchor_tick"] == 2141
    assert point["price_anchor_basis"] == "return_endpoint"
    assert point["available_at"] == point["confirmed_at"] == record["available_at"] == stamp("2026-07-28 13:55")
    assert all(c["center_id"] != record["original_center_id"] for c in snapshot["levels"][0]["centers"])
    later = snapshot["levels"][0]["centers"][-1]
    assert later["center_id"] != center["center_id"]
    assert (later["core"]["zd_tick"], later["core"]["zg_tick"]) == (2191, 2244)
    assert valid(snapshot, point)
    assert all(valid(snapshot, p) for p in snapshot["levels"][0]["points"])
    assert all(native_consolidation_source_valid(snapshot, a) for a in snapshot["native_consolidation_alerts"])


def test_every_published_center_and_preview_has_five_contiguous_overlapping_roles(snapshot):
    positions = {u["unit_id"]: i for i, u in enumerate(snapshot["screening_segments"])}
    level = snapshot["levels"][0]
    for center in (*level["centers"], *level["center_previews"]):
        roles = center["establishment_segments"]
        assert center["formation_rule"] == "five_role"
        assert len(roles) == len({u["unit_id"] for u in roles}) == 5
        assert [u["unit_id"] for u in roles] == [center["entry_unit_id"], *center["core_unit_ids"], center["establishment_leave_unit_id"]]
        assert all(positions[b["unit_id"]] == positions[a["unit_id"]] + 1 for a, b in zip(roles, roles[1:]))
        assert max(u["low_tick"] for u in roles) <= min(u["high_tick"] for u in roles)
        assert (center["core"]["zd_tick"], center["core"]["zg_tick"]) == (
            max(u["low_tick"] for u in roles[1:4]), min(u["high_tick"] for u in roles[1:4]))
        if center["render_kind"] == "formal_center":
            assert all(u["locked"] for u in roles)
    assert all((c["core"]["zd_tick"], c["core"]["zg_tick"]) != (2094, 2141) for c in level["centers"])


@pytest.mark.parametrize("cutoff, exists", [
    ("2026-07-23 13:50", False), ("2026-07-28 13:50", False),
    ("2026-07-28 13:55", True), ("2026-08-31 15:00", True),
])
def test_historical_prefix_never_backdates_the_confirmed_three_sell(frame, snapshot, cutoff, exists):
    prefix = frame.loc[frame.date <= pd.Timestamp(cutoff, tz="Asia/Shanghai")].copy()
    replay = snapshot_for(prefix, "SH.513100", "5m")
    record, center, point = selected(replay)
    if not exists:
        assert record is None
        return
    expected_record, expected_center, expected_point = selected(snapshot)
    assert record == expected_record
    assert center == expected_center
    assert point == expected_point


@pytest.mark.parametrize("fault", ["missing_selection", "early_selection", "missing_connection",
                                   "endpoint_price", "unknown_anchor", "return_reenters"])
def test_saved_three_sell_rejects_changed_selection_or_return_evidence(snapshot, fault):
    damaged = deepcopy(snapshot)
    record, center, point = selected(damaged)
    if fault == "missing_selection":
        damaged["center_view_selections"].remove(record)
    elif fault == "early_selection":
        record["available_at"] = center["view_available_at"] = stamp("2026-07-23 13:50")
        point["view_available_at"] = record["available_at"]
    elif fault == "missing_connection":
        record["connection_unit_ids"] = []
    elif fault == "endpoint_price":
        point["anchor_tick"] = 2153
    elif fault == "unknown_anchor":
        point["price_anchor_basis"] = "source_extreme"
        point["anchor_tick"] = 2153
    else:
        ret = next(u for u in damaged["screening_segments"] if u["unit_id"] == record["return_unit_id"])
        ret["high_tick"] = 2155
    assert not valid(damaged, point)
