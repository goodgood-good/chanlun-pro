"""Causal state checks for source-bounded implementation decisions."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.core.strict_structure.rule_state import (
    center_pair_rule_state,
    small_to_large_point_rule_state,
    transition_rule_state,
)
from tests.core.strict_structure.helpers import completed_up_center, ongoing_center


def test_upgrade_relation_requires_two_completed_centers_and_parent_remains_separate():
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = completed_up_center(10, zd_tick=116, zg_tick=126)
    pending = center_pair_rule_state(previous, ongoing_center(10, zd_tick=116, zg_tick=126))
    confirmed = center_pair_rule_state(previous, current)

    assert pending["touch_observed"]
    assert pending["strict_expansion_relation"] == "pending_center_completion"
    assert confirmed["strict_expansion_relation"] == "confirmed_relation"
    assert confirmed["strict_relation_known_at"] >= current.completed_at
    assert confirmed["higher_center_formal"] == "requires_independent_parent_three_members"


@pytest.mark.parametrize("zd_tick,zg_tick", [(115, 125), (111, 121)])
def test_core_contact_or_overlap_remains_pending_recomposition(zd_tick, zg_tick):
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = completed_up_center(10, zd_tick=zd_tick, zg_tick=zg_tick)

    state = center_pair_rule_state(previous, current)

    assert state["geometry_relation"] == "recomposition_pending"
    assert state["strict_expansion_relation"] == "pending_recomposition"
    assert state["strict_relation_known_at"] is None
    assert state["higher_center_formal"] == "requires_independent_parent_three_members"


def test_transition_keeps_shared_endpoint_until_later_structural_third_point():
    base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    boundary_at = base + timedelta(minutes=10)
    child_available = base + timedelta(minutes=20)
    third_at = base + timedelta(minutes=25)
    third_available = base + timedelta(minutes=30)
    boundary = SimpleNamespace(
        anchor_at=boundary_at,
        confirmed_at=base + timedelta(minutes=15),
        available_at=base + timedelta(minutes=15),
        boundary_id="boundary-P", terminal_center_id="old-center",
    )
    level = SimpleNamespace(
        structural_level=0, decomposition_mode="same_level",
        decomposition_boundaries=(boundary,),
        units=(SimpleNamespace(unit_id="new-child", locked=True,
                               market_end=base + timedelta(minutes=19),
                               available_at=child_available),),
    )
    third = SimpleNamespace(
        structural_level=0, status=StrictPointStatus.CONFIRMED,
        point_type="3sell", center_id="old-center", anchor_at=third_at,
        confirmed_at=third_available, available_at=third_available,
        point_id="third-sell",
    )

    waiting = transition_rule_state(level, (third,), base + timedelta(minutes=24))
    completed = transition_rule_state(level, (third,), third_available)

    assert waiting["status"] == "active" and waiting["exit_point_id"] is None
    assert completed["status"] == "ended"
    assert completed["previous_type_end_at"] == completed["next_type_start_at"] == boundary_at
    assert completed["transition_exit_at"] == third_at
    assert completed["transition_exit_confirmed_at"] == third_available
    assert completed["boll_hint"] == "unavailable"


def test_trend_return_to_last_center_confirms_signal_without_moving_boundary():
    base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    boundary_at = base + timedelta(minutes=10)
    boundary = SimpleNamespace(
        anchor_at=boundary_at, confirmed_at=base + timedelta(minutes=12),
        available_at=base + timedelta(minutes=12), boundary_id="P",
        boundary_kind="trend_divergence", terminal_center_id="last-center",
    )
    old_center = SimpleNamespace(center_id="last-center", zd_tick=100, zg_tick=110)
    ret = SimpleNamespace(
        unit_id="complete-return", locked=True,
        market_end=base + timedelta(minutes=20),
        available_at=base + timedelta(minutes=25), low_tick=105, high_tick=120,
    )
    level = SimpleNamespace(
        structural_level=0, decomposition_mode="same_level",
        decomposition_boundaries=(boundary,), units=(ret,),
        center_result=SimpleNamespace(centers=(old_center,)),
    )

    before = transition_rule_state(level, (), base + timedelta(minutes=24))
    after = transition_rule_state(level, (), ret.available_at)

    assert before["natural_end_signal"] == "pending"
    assert after["natural_end_signal"] == "confirmed_at_return"
    assert after["return_to_last_center_known_at"] == ret.available_at
    assert after["previous_type_end_at"] == after["next_type_start_at"] == boundary_at


def test_small_to_large_second_class_marker_is_not_larger_turn_proof():
    point = SimpleNamespace(evidence_codes=("small_to_large_reversal",))
    ordinary = SimpleNamespace(evidence_codes=("formal_structure",))

    assert small_to_large_point_rule_state(point) == (
        "operating_second_class_not_larger_turn_certificate"
    )
    assert small_to_large_point_rule_state(ordinary) is None
