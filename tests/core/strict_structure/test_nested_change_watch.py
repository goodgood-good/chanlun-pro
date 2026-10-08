"""L092 nested child-trend geometry keeps Zn and formal change independent."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.models import TrendKind, TrendState
from chanlun.core.strict_structure.rule_state import nested_change_watch_rule_states


BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _at(index):
    return BASE + timedelta(minutes=index * 5)


def _bounded_source(*, child_zd=120, child_dd=120, next_end=125):
    # One exact locked lower trend is the third parent-center member. Its last
    # lower center lies wholly above the parent core; the next lower type is a
    # completed, connected pullback into that lower center.
    def unit(unit_id, index, start_tick, end_tick):
        return SimpleNamespace(
            unit_id=unit_id, market_start=_at(index), market_end=_at(index + 1),
            start_tick=start_tick, end_tick=end_tick, available_at=_at(index + 1),
        )

    first = unit("a", 0, 80, 110)
    second = unit("b", 1, 110, 90)
    third = unit("t0", 2, 90, 130)
    following = unit("t1", 3, 130, next_end)
    child_center = SimpleNamespace(
        center_id="child-last", zd_tick=child_zd, zg_tick=130,
        dd_tick=child_dd, gg_tick=130, available_at=_at(3),
    )
    child_trend = SimpleNamespace(
        trend_id="t0", kind=TrendKind.TREND, state=TrendState.LOCKED,
        centers=(child_center,), market_end=_at(3), end_tick=130,
        available_at=_at(3),
    )
    next_trend = SimpleNamespace(
        trend_id="t1", kind=TrendKind.CONSOLIDATION,
        state=TrendState.LOCKED, market_end=_at(4), end_tick=next_end,
        available_at=_at(4),
    )
    center = SimpleNamespace(
        center_id="parent", zd_tick=90, zg_tick=110, established_at=_at(3),
        body_units=(first, second, third), failed_departure_units=(),
        pending_leave_unit=following, completion_leave_unit=None,
        completion_return_unit=None,
    )
    parent = SimpleNamespace(
        structural_level=1, units=(first, second, third, following),
        center_result=SimpleNamespace(centers=(center,)),
    )
    lower = SimpleNamespace(structural_level=0, trend_types=(child_trend, next_trend))
    return parent, lower


def test_nested_completed_child_branch_is_supported_without_claiming_change():
    # L092:109-115 states the nested condition; the Zn leg mapping remains open.
    parent, lower = _bounded_source()
    state, = nested_change_watch_rule_states(parent, lower, _at(4))
    assert state["status"] == "nested_structure_supported_zn_mapping_pending"
    assert state["last_child_core_relation"] == "above"
    assert state["last_child_envelope_relation"] == "above"
    assert state["next_child_end_tick"] == 125
    assert state["condition_market_at"] == state["condition_known_at"] == _at(4)
    assert state["author_zn_source_status"] == "mapping_pending"
    assert state["formal_change_status"] == "independent_not_inferred"
    assert state["tradable"] is False

    before, = nested_change_watch_rule_states(parent, lower, _at(3))
    assert before["status"] == "pending_next_child_type"
    assert before["next_child_trend_id"] is None
    assert before["condition_known_at"] is None


def test_nested_branch_rejects_overlap_and_next_child_outside_range():
    parent, lower = _bounded_source(child_zd=100, child_dd=100)
    overlap, = nested_change_watch_rule_states(parent, lower, _at(4))
    assert overlap["status"] == "last_child_center_overlaps_parent"

    parent, lower = _bounded_source(child_zd=120, child_dd=105)
    uncertain, = nested_change_watch_rule_states(parent, lower, _at(4))
    assert uncertain["status"] == "outside_core_envelope_overlap_needs_scope_review"

    parent, lower = _bounded_source(next_end=119)
    outside, = nested_change_watch_rule_states(parent, lower, _at(4))
    assert outside["status"] == "next_child_ends_outside_last_child_center"
    assert outside["condition_known_at"] is None


def test_nested_center_is_not_visible_before_third_member_lock():
    parent, lower = _bounded_source()
    center, = parent.center_result.centers
    center.body_units[2].available_at = _at(5)
    assert nested_change_watch_rule_states(parent, lower, _at(4)) == ()


def test_recorded_parent_trends_do_not_borrow_u287_outside_condition():
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures" / "segment_origin" / "SH.688377_5m.parquet"
    )
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.688377", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    lower, parent = runtime.cd.get_strict_evidence().structure.levels[:2]
    states = nested_change_watch_rule_states(parent, lower, runtime.cd._strict_as_of())
    assert len(states) == 2
    assert all(item["status"] == "last_child_center_overlaps_parent" for item in states)
    assert all(item["condition_known_at"] is None and not item["tradable"] for item in states)
