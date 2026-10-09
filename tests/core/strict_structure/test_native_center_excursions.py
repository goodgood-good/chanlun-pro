"""Regression for SH.513100 5m: an internal contact ends outside the core."""
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import build_native_center_snapshot
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import calculate_three_core_segment_centers
from chanlun.core.strict_structure.models import CenterState
from chanlun.core.strict_structure.native_center_signals import build_native_center_signals
from tests.core.strict_structure.helpers import unit


def excursion_sequence(mirror=False):
    prices = [120, 100, 115, 105, 130, 83, 84, 75, 118, 110]
    units = [unit(i, "up" if end > start else "down", start, end)
             for i, (start, end) in enumerate(zip(prices, prices[1:]))]
    units[5] = replace(units[5], high_tick=106)
    if mirror:
        units = [replace(value, direction="down" if value.direction == "up" else "up",
                         start_tick=300-value.start_tick, end_tick=300-value.end_tick,
                         low_tick=300-value.high_tick, high_tick=300-value.low_tick) for value in units]
    return tuple(units)


@pytest.mark.parametrize("mirror", [False, True])
def test_internal_contact_and_outside_connector_resume_without_false_third_point(mirror):
    units = excursion_sequence(mirror)
    for count in range(3, len(units) + 1):
        result = calculate_three_core_segment_centers(units[:count], 0)
        assert not result.unprocessed_unit_ids
        center, = result.centers
        assert center.state is CenterState.ONGOING
        assert not center.third_class_confirmed
        assert center.initial_units == units[:3]
        assert center.established_at == units[2].confirmed_at
        if count >= 7:
            assert center.excursion_units == (units[6],)
            assert units[6] not in center.body_units
        assert build_native_center_signals(units[:count], result.signal_centers, None,
                                          as_of=units[count-1].available_at) == []
    assert center.display_range_end_market_time == units[-1].market_end


def test_unlocked_tail_projects_past_the_outside_connector_without_confirming_it():
    units = excursion_sequence()
    live = (*units[:7], replace(units[7], locked=False, confirmed_at=None))
    result = calculate_three_core_segment_centers(live, 0)
    center, = result.centers
    preview, = result.previews
    assert preview.formal_center_id == center.center_id
    assert preview.excursion_unit_ids == (units[6].unit_id,)
    assert preview.pending_leave_unit_id == units[7].unit_id
    assert center.pending_leave_unit is None
    assert not center.third_class_confirmed


def test_unresolved_suffix_is_explicit_until_a_connected_successor_is_available():
    units = (*excursion_sequence()[:7], unit(7, "up", 75, 98))
    result = calculate_three_core_segment_centers(units, 0)
    assert result.unprocessed_unit_ids == (units[-1].unit_id,)
    assert result.signal_centers == ()
    completed = calculate_three_core_segment_centers(
        (*units, unit(8, "down", 98, 90), unit(9, "up", 90, 96)), 0,
    )
    assert not completed.unprocessed_unit_ids
    previous, successor = completed.centers
    assert previous.state is CenterState.SUPERSEDED
    assert not previous.third_class_confirmed
    assert previous.excursion_units[-1].market_end == successor.initial_units[0].market_start


@pytest.fixture(scope="module")
def recorded_frame():
    return pd.read_parquet(Path(__file__).parents[2] / "fixtures" / "SH.513100_5m_center_20261009.parquet")


def test_saved_center_cannot_drop_its_outside_connector_or_claim_an_incomplete_scan(recorded_frame):
    from chanlun.screening.runner import snapshot_for
    from chanlun.screening.rules import _native_center_body_source_valid

    snapshot = snapshot_for(recorded_frame, "SH.513100", "5m", include_consolidation=True)
    center = next(c for c in snapshot["levels"][0]["centers"] if c["excursion_unit_ids"])
    units = snapshot["screening_segments"]
    by_id = {u["unit_id"]: u for u in units}
    positions = {u["unit_id"]: i for i, u in enumerate(units)}
    def valid(value):
        return _native_center_body_source_valid(snapshot, value, by_id, positions, snapshot["source_closed_at"])
    assert valid(center)
    assert not valid({**center, "excursion_unit_ids": [], "excursion_segments": []})
    assert not valid({**center, "processing_complete": False})


@pytest.mark.parametrize("cutoff, expected_end", [
    ("2026-07-28 13:55", "2026-07-23 13:35"),
    ("2026-08-07 10:40", "2026-07-23 13:35"),
    ("2026-08-31 13:40", "2026-08-25 09:40"),
    ("2026-09-22 11:25", "2026-09-15 13:25"),
    ("2026-10-09 15:00", "2026-09-15 13:25"),
])
def test_frozen_first_core_view_keeps_real_contact_evidence(recorded_frame, cutoff, expected_end, monkeypatch):
    frame = recorded_frame[recorded_frame.date <= pd.Timestamp(cutoff, tz="Asia/Shanghai")].copy()
    cd = build_strict_chart_cd(market="a", code="SH.513100", frequency="5m", frame=frame,
                               last_bar_closed=True).cd
    result = calculate_three_core_segment_centers(tuple(cd.get_segment_units()), 0)
    # The comparison view remains auditable even when production chooses an
    # independently completed inner view. Its original core is never moved.
    monkeypatch.setattr(cd, "get_native_centers", lambda: result)
    center = result.centers[-1]
    assert not result.unprocessed_unit_ids
    assert center.formation_rule == "three_core"
    assert (center.zd_tick, center.zg_tick) == (2150, 2270)
    assert center.display_range_end_market_time == pd.Timestamp(expected_end, tz="Asia/Shanghai")
    snapshot = build_native_center_snapshot(cd, interval="5m",
                                           display_bar_closed_at=tuple(int(t.timestamp()) for t in frame.date))
    assert snapshot["center_processing"]["complete"] is True
    assert snapshot["levels"][0]["centers"][-1]["processing_complete"] is True
    assert not center.third_class_confirmed
    if cutoff >= "2026-08-07":
        excursion, = center.excursion_units
        assert (excursion.low_tick, excursion.high_tick) == (2035, 2141)
        assert excursion.market_start == pd.Timestamp("2026-07-23 13:35", tz="Asia/Shanghai")
    if cutoff >= "2026-09-22":
        preview = snapshot["levels"][0]["center_previews"][-1]
        assert preview["owner_center_id"] == center.center_id
        assert [role["unit_id"] for role in preview["establishment_segments"]] == preview["core_unit_ids"]
