"""U175 formation cannot be inferred from missing formal trend divergence."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.rule_state import trend_formation_rule_states
from chanlun.core.strict_structure.strength import (
    MacdStrengthProvider,
    compare_divergence,
)


CN = ZoneInfo("Asia/Shanghai")
FIXTURES = Path(__file__).parents[2] / "fixtures"


@pytest.mark.parametrize("fixture, code, frequency, day, new_extreme, locally_divergent, expected_status", [
    ("segment_origin/SH.688377_5m.parquet", "SH.688377", "5m",
     "2026-01-15", True, True, "not_formed_local_consolidation_divergence"),
    ("SZ.002299_1m.parquet", "SZ.002299", "1m",
     "2025-07-03", True, False, "formation_supported_by_local_nondivergence"),
    ("SH.600519_5m.parquet", "SH.600519", "5m",
     "2025-07-30", False, False, "not_formed_no_new_extreme"),
])
def test_first_center_third_buy_requires_positive_strength_proof_for_formation(
    fixture, code, frequency, day, new_extreme, locally_divergent, expected_status,
):
    # L107:166-187 names third buy plus non-divergent upward movement;
    # L024:16-34 makes MACD auxiliary, and L037:52-103 reserves strict trend
    # divergence for a completed same-grade trend with two centers.
    frame = pd.read_parquet(FIXTURES / fixture)
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    runtime = build_strict_chart_cd(
        market="a", code=code, frequency=frequency,
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    evidence = runtime.cd.get_strict_evidence()
    level = evidence.structure.levels[0]
    point, = (
        item for item in evidence.confirmed_points
        if item.structural_level == 0
        and item.point_type == "3buy"
        and item.center_ordinal == 1
        and item.available_at.astimezone(CN).date().isoformat() == day
    )
    center = next(
        item for item in level.center_result.centers
        if item.center_id == point.center_id
    )
    positions = {unit.unit_id: offset for offset, unit in enumerate(level.units)}
    following = level.units[positions[center.completion_return_unit.unit_id] + 1]
    assert following.locked and following.direction == "up"
    assert following.market_start == center.completion_return_unit.market_end
    assert not any(
        divergence.kind == "trend"
        and divergence.signal_unit_id == following.unit_id
        for divergence in evidence.divergences
    )
    provider = MacdStrengthProvider(runtime.cd)
    comparison = compare_divergence(
        center.completion_leave_unit, following,
        provider, kind="consolidation",
    )
    assert comparison.price_extreme_confirmed is new_extreme
    assert comparison.is_divergent is locally_divergent
    state, = (
        item for item in trend_formation_rule_states(
            level, evidence.confirmed_points, runtime.cd._strict_as_of(), provider
        ) if item["third_buy_point_id"] == point.point_id
    )
    assert state["status"] == expected_status
    assert state["comparison_id"] == comparison.divergence_id
    assert state["completed_trend_status"] == "independent_not_inferred_from_formation"
    assert state["tradable"] is False
    no_strength, = (
        item for item in trend_formation_rule_states(
            level, evidence.confirmed_points, runtime.cd._strict_as_of(), None
        ) if item["third_buy_point_id"] == point.point_id
    )
    assert no_strength["status"] == "pending_strength_source"
    assert no_strength["formation_known_at"] is None
    if expected_status != "formation_supported_by_local_nondivergence":
        assert state["formation_market_at"] is None
        assert state["formation_known_at"] is None
    else:
        assert state["formation_market_at"] == following.market_end
        assert state["formation_known_at"] == max(
            point.available_at, following.available_at, comparison.available_at
        )
