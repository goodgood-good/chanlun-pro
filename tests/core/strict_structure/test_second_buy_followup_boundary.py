"""L086's bounded second-buy follow-up keeps its own causal high comparison."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.rule_state import second_buy_followup_observations


def test_recorded_second_buy_followup_has_a_causal_new_high_branch():
    # L086:304-322 compares the next completed child up with the first up leg.
    path = Path(__file__).parents[2] / "fixtures" / "segment_origin" / "SH.688377_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.688377", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    evidence = runtime.cd.get_strict_evidence()
    level = evidence.structure.levels[0]
    states = second_buy_followup_observations(
        level, evidence.confirmed_points, runtime.cd._strict_as_of()
    )
    state, = states
    assert state["status"] == "first_up_high_broken_watch_independent_third_buy"
    assert state["following_up_high_tick"] > state["first_up_high_tick"]
    assert state["branch_market_at"] < state["branch_known_at"]
    assert state["third_buy_status"] == "independent_point_check_required"
    assert state["tradable"] is False

    second = next(point for point in evidence.confirmed_points
                  if point.point_id == state["second_buy_point_id"])
    before, = second_buy_followup_observations(
        level, evidence.confirmed_points, second.available_at
    )
    assert before["status"] == "pending_next_child_completion"
    assert before["branch_market_at"] is None
    assert before["branch_known_at"] is None

    # A completed next child that fails to exceed the first up leg must take
    # the other author-bounded branch, without manufacturing a sell point.
    higher_reference = tuple(
        replace(unit, high_tick=state["following_up_high_tick"] + 1)
        if unit.unit_id == state["first_up_unit_id"] else unit
        for unit in level.units
    )
    counterexample_level = SimpleNamespace(
        structural_level=level.structural_level, units=higher_reference
    )
    no_break, = second_buy_followup_observations(
        counterexample_level, evidence.confirmed_points, runtime.cd._strict_as_of()
    )
    assert no_break["status"] == "first_up_high_unbroken_in_bounded_case"
    assert no_break["branch_known_at"] == state["branch_known_at"]
    assert no_break["tradable"] is False
