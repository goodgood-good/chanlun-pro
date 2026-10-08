"""U294 records exact later child divergence without issuing a trade."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.models import (
    CenterState, SourceKind, StrictPointStatus, TrendState,
)
from chanlun.core.strict_structure.rule_state import (
    center_oscillation_operation_rule_states,
)


BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


def at(index):
    return BASE + timedelta(minutes=index * 5)


def fixture():
    def unit(name, index, start, end):
        return SimpleNamespace(
            unit_id=name, market_start=at(index), market_end=at(index + 1),
            start_tick=start, end_tick=end, available_at=at(index + 1),
            price_basis_revision="raw",
        )

    initial = (unit("a", 0, 80, 120), unit("b", 1, 120, 90),
               unit("c", 2, 90, 110))
    up = unit("upper", 3, 110, 130)
    down = unit("lower", 4, 130, 100)

    def trend(member, kind, direction, anchor):
        divergence = SimpleNamespace(
            divergence_id="div-" + member.unit_id, kind=kind,
            direction=direction, signal_unit_id="signal-" + member.unit_id,
            anchor_at=member.market_end, anchor_tick=anchor,
            available_at=member.available_at,
        )
        return SimpleNamespace(
            trend_id=member.unit_id, state=TrendState.LOCKED,
            market_start=member.market_start, market_end=member.market_end,
            start_tick=member.start_tick, end_tick=member.end_tick,
            price_basis_revision="raw", available_at=member.available_at,
            terminal_divergence=divergence,
        )

    center = SimpleNamespace(
        center_id="parent", structural_level=1, source_kind=SourceKind.TREND_TYPE,
        price_basis_revision="raw", state=CenterState.ONGOING,
        zd_tick=90, zg_tick=110, established_at=at(3), available_at=at(5),
        initial_units=initial, body_units=(*initial, up, down),
        failed_departure_units=(), pending_leave_unit=None,
        completion_leave_unit=None, completion_return_unit=None,
    )
    parent = SimpleNamespace(
        structural_level=1, center_result=SimpleNamespace(centers=(center,)),
    )
    lower = SimpleNamespace(
        structural_level=0,
        trend_types=(trend(initial[-1], "trend", "up", 110),
                     trend(up, "consolidation", "up", 130),
                     trend(down, "trend", "down", 100)),
    )
    point = SimpleNamespace(
        point_id="independent-third", point_type="3buy",
        status=StrictPointStatus.CONFIRMED, center_id="parent",
        structural_level=1, price_basis_revision="raw",
        anchor_at=at(5), available_at=at(5),
    )
    return parent, lower, (point,)


def test_later_exact_child_divergences_are_separate_from_formal_third_point():
    parent, lower, points = fixture()
    state, = center_oscillation_operation_rule_states(parent, lower, points, at(5))
    assert state["status"] == "exact_lower_divergence_facts_observed"
    assert [item["child_trend_id"] for item in state["lower_divergence_observations"]] == [
        "upper", "lower",
    ]
    assert state["initial_member_divergences_excluded"] is True
    assert state["upward_divergence_fact_count"] == 1
    assert state["downward_divergence_fact_count"] == 1
    assert state["lower_divergence_observations"][0]["anchor_core_relation"] == "above"
    assert state["formal_third_points"][0]["point_id"] == "independent-third"
    assert state["formal_third_point_independent"] is True
    assert state["author_operation_certified"] is False
    assert state["position_size_status"] == "not_programmed"
    assert state["tradable"] is False
    assert all(item["known_at"] <= at(5) for item in state["lower_divergence_observations"])


def test_missing_exact_child_source_stays_pending_without_borrowing_a_other_trend():
    parent, lower, points = fixture()
    lower.trend_types = lower.trend_types[:-1]
    state, = center_oscillation_operation_rule_states(parent, lower, points, at(5))
    assert state["status"] == "pending_exact_lower_member_lineage"
    assert state["missing_child_trend_ids"] == ["lower"]
    assert [item["child_trend_id"] for item in state["lower_divergence_observations"]] == ["upper"]
    assert state["author_operation_certified"] is False


def test_future_member_and_third_point_are_not_backfilled_into_earlier_cutoff():
    parent, lower, points = fixture()
    center = parent.center_result.centers[0]
    center.available_at = at(4)
    state, = center_oscillation_operation_rule_states(parent, lower, points, at(4))
    assert [item["child_trend_id"] for item in state["lower_divergence_observations"]] == ["upper"]
    assert state["formal_third_points"] == []
    assert state["upward_divergence_fact_count"] == 1
    assert state["downward_divergence_fact_count"] == 0


def test_adjacent_recursive_grade_is_required():
    parent, lower, points = fixture()
    lower.structural_level = 2
    with pytest.raises(ValueError, match="adjacent recursive levels"):
        center_oscillation_operation_rule_states(parent, lower, points, at(5))


def test_finished_center_keeps_history_without_presenting_a_live_watch():
    parent, lower, points = fixture()
    parent.center_result.centers[0].state = CenterState.COMPLETED
    state, = center_oscillation_operation_rule_states(parent, lower, points, at(5))
    assert state["status"] == "historical_exact_lower_divergence_facts"
    assert state["operation_scope"] == "historical_center_audit"
    assert state["author_operation_certified"] is False
