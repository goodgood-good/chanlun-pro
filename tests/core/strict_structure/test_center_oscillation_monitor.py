"""L092's Z/Zn midpoint monitor remains auxiliary to center lifecycle proof."""

from pathlib import Path

import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.rule_state import center_oscillation_monitor_state
from tests.core.strict_structure.helpers import ongoing_center


def test_z_and_completed_child_midpoints_keep_their_causal_clocks():
    # L092:28-43 defines Z/Zn; L092:61-85 denies that a crossed midpoint
    # alone confirms a third point or a higher center.
    center = ongoing_center(zd_tick=105, zg_tick=115)
    early = center_oscillation_monitor_state(center, center.established_at)
    full = center_oscillation_monitor_state(center, center.available_at)
    assert early["z_twice_tick"] == full["z_twice_tick"] == 220
    assert early["z_known_at"] == center.established_at
    assert len(early["observations"]) == 4
    assert len(full["observations"]) == 4
    for item in full["observations"]:
        assert item["zn_twice_tick"] == item["range_low_tick"] + item["range_high_tick"]
        assert item["known_at"] >= max(center.established_at, item["market_at"])
    assert full["observations"][-1]["boundary_cross"] == "above_B"
    assert full["boundary_cross_count"] >= 1
    assert not center.third_class_confirmed
    assert full["third_point_status"] == "independent_complete_leave_and_return_required"
    assert full["higher_center_status"] == "independent_three_completed_child_types_required"
    assert full["tradable"] is False


def test_recorded_long_center_requires_zn_leg_grouping_review():
    # L092:52-58 caps its Zn discussion at nine values. A software center
    # lifecycle with more child records cannot be declared that exact sequence.
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures" / "segment_clock" / "SH.600658_5m.parquet"
    )
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600658", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    level = runtime.cd.get_strict_evidence().structure.levels[0]
    center = next(item for item in level.center_result.centers
                  if len(center_oscillation_monitor_state(item, runtime.cd._strict_as_of())["observations"]) > 9)
    state = center_oscillation_monitor_state(center, runtime.cd._strict_as_of())
    assert len(state["observations"]) == 12
    assert state["status"] == "source_leg_grouping_and_upgrade_review_required"
    assert state["author_zn_sequence_certified"] is False
    assert state["higher_center_status"] == "independent_three_completed_child_types_required"
    assert state["tradable"] is False
