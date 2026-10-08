"""Center-specific observations wait for all three initial member locks."""

from datetime import timedelta
from pathlib import Path

import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_frame import initial_center_known_at
from chanlun.core.strict_structure.rule_state import (
    center_macd_phase_rule_states,
    center_oscillation_monitor_state,
    nested_change_watch_rule_states,
)


def test_recorded_parent_center_auxiliary_states_wait_for_third_member():
    # L018:31-55 requires three completed lower types.  In this recorded
    # source the parent's market establishment precedes its third type lock.
    path = Path(__file__).parents[2] / "fixtures/segment_origin/SZ.300867_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SZ.300867", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    lower, parent = runtime.cd.get_strict_evidence().structure.levels[:2]
    center, = parent.center_result.centers
    initial_known = initial_center_known_at(center)
    assert center.established_at < initial_known <= center.available_at
    assert initial_known == max(unit.available_at for unit in center.initial_units)

    for cutoff in (center.established_at, initial_known - timedelta(microseconds=1)):
        assert center_macd_phase_rule_states(parent, cutoff, None) == ()
        assert nested_change_watch_rule_states(parent, lower, cutoff) == ()
        pending = center_oscillation_monitor_state(center, cutoff)
        assert pending["status"] == "pending_center_establishment"
        assert pending["center_id"] is None
        assert pending["center_initial_known_at"] is None
        assert pending["z_known_at"] is None
        assert pending["observations"] == []

    phase, = center_macd_phase_rule_states(parent, initial_known, None)
    assert phase["center_id"] == center.center_id
    assert phase["center_initial_known_at"] == initial_known
    z_state = center_oscillation_monitor_state(center, initial_known)
    assert z_state["z_known_at"] == initial_known
    assert len(z_state["observations"]) >= 3


def test_recorded_boundary_contacts_do_not_fabricate_the_old_parent_center():
    path = Path(__file__).parents[2] / "fixtures/segment_clock/SZ.000766_1m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(market="a", code="SZ.000766", frequency="1m",
                                    frame=frame, last_bar_closed=True)
    lower, parent = runtime.cd.get_strict_evidence().structure.levels[:2]
    assert not parent.center_result.centers
    assert any(unit.high_tick == center.zd_tick or unit.low_tick == center.zg_tick
               for center in lower.center_result.centers for unit in center.extension_units)
