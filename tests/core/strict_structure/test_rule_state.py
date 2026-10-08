"""Causal state checks for source-bounded implementation decisions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.center_machine import advance_center
from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.core.strict_structure.rule_state import (
    center_pair_rule_state,
    small_to_large_point_rule_state,
    transition_rule_state,
)
from tests.core.strict_structure.helpers import completed_up_center, ongoing_center, unit


def test_upgrade_relation_requires_two_completed_centers_and_parent_remains_separate():
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = completed_up_center(10, zd_tick=121, zg_tick=131)
    pending = center_pair_rule_state(previous, ongoing_center(10, zd_tick=121, zg_tick=131))
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


def test_first_touch_clock_survives_later_center_extension():
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = ongoing_center(10, zd_tick=111, zg_tick=121)
    before = center_pair_rule_state(previous, current)
    reentry = unit(15, "down", current.pending_leave_unit.end_tick, 117)
    extended, _event = advance_center(current, reentry)
    after = center_pair_rule_state(previous, extended)

    assert before["touch_observed"] and after["touch_observed"]
    assert before["touch_witness_unit_id"] == after["touch_witness_unit_id"]
    assert before["touch_witness_known_at"] <= before["touch_known_at"]
    assert before["touch_known_at"] == after["touch_known_at"]
    assert after["touch_known_at"] < extended.available_at


def test_full_previous_envelope_cannot_be_used_before_its_source_is_available():
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = ongoing_center(10, zd_tick=111, zg_tick=121)
    later_envelope_source = current.available_at + timedelta(minutes=5)
    previous = replace(previous, available_at=later_envelope_source)
    relation = center_pair_rule_state(previous, current)
    assert relation["touch_observed"]
    assert relation["touch_known_at"] == later_envelope_source


def test_separate_wave_ranges_have_no_touch_witness_or_clock():
    previous = completed_up_center(0, zd_tick=105, zg_tick=115)
    current = ongoing_center(10, zd_tick=200, zg_tick=210)

    state = center_pair_rule_state(previous, current)

    assert not state["touch_observed"]
    assert state["touch_known_at"] is None
    assert state["touch_witness_unit_id"] is None
    assert state["touch_witness_known_at"] is None


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
                               market_start=boundary_at,
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


def test_transition_exit_rejects_unrelated_center_third_points():
    base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    boundary_at = base + timedelta(minutes=10)
    known_at = base + timedelta(minutes=30)
    boundary = SimpleNamespace(
        anchor_at=boundary_at, confirmed_at=base + timedelta(minutes=12),
        available_at=base + timedelta(minutes=12), boundary_id="P",
        terminal_center_id="last-old-center",
    )
    earlier_center = SimpleNamespace(
        center_id="earlier-old-center", body_start_market_time=base,
        established_at=base + timedelta(minutes=5),
        available_at=base + timedelta(minutes=5),
    )
    new_center = SimpleNamespace(
        center_id="new-path-center",
        body_start_market_time=boundary_at,
        established_at=base + timedelta(minutes=20),
        available_at=base + timedelta(minutes=20),
    )
    level = SimpleNamespace(
        structural_level=0, decomposition_mode="same_level",
        decomposition_boundaries=(boundary,),
        units=(SimpleNamespace(
            unit_id="new-child", locked=True,
            market_start=boundary_at,
            market_end=base + timedelta(minutes=18),
            available_at=base + timedelta(minutes=20),
        ),),
        center_result=SimpleNamespace(centers=(earlier_center, new_center)),
    )

    def third(center_id):
        return SimpleNamespace(
            structural_level=0, status=StrictPointStatus.CONFIRMED,
            point_type="3sell", center_id=center_id,
            anchor_at=base + timedelta(minutes=25),
            confirmed_at=known_at, available_at=known_at,
            point_id=f"third-{center_id}",
        )

    earlier = transition_rule_state(level, (third("earlier-old-center"),), known_at)
    unknown = transition_rule_state(level, (third("unrelated"),), known_at)
    new_path = transition_rule_state(level, (third("new-path-center"),), known_at)

    assert earlier["status"] == unknown["status"] == "active"
    assert earlier["exit_point_id"] is None and unknown["exit_point_id"] is None
    assert new_path["status"] == "ended"
    assert new_path["exit_point_id"] == "third-new-path-center"
    assert new_path["eligible_transition_center_ids"] == (
        "last-old-center", "new-path-center",
    )


@pytest.mark.parametrize("old_starts_at_boundary", [False, True])
def test_old_type_unit_crossing_divergence_point_is_not_new_initial_structure(
    old_starts_at_boundary,
):
    base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    boundary_at = base + timedelta(minutes=10)
    known_at = base + timedelta(minutes=30)
    old = SimpleNamespace(
        unit_id="old-terminal", locked=True,
        market_start=boundary_at if old_starts_at_boundary else base + timedelta(minutes=5),
        market_end=base + timedelta(minutes=15),
        available_at=base + timedelta(minutes=20),
        low_tick=100, high_tick=120,
    )
    new = SimpleNamespace(
        unit_id="new-owned", locked=True,
        market_start=old.market_end,
        market_end=base + timedelta(minutes=20),
        available_at=base + timedelta(minutes=25),
        low_tick=100, high_tick=115,
    )
    boundary = SimpleNamespace(
        anchor_at=boundary_at, confirmed_at=base + timedelta(minutes=12),
        available_at=base + timedelta(minutes=12), boundary_id="P",
        boundary_kind="trend_divergence",
        terminal_center_id="old-center", left_trend_id="old-type",
    )
    old_type = SimpleNamespace(trend_id="old-type", constituent_units=(old,))
    old_center = SimpleNamespace(
        center_id="old-center", zd_tick=102, zg_tick=110,
        body_start_market_time=base,
        established_at=base + timedelta(minutes=5),
        available_at=base + timedelta(minutes=5),
    )
    point = SimpleNamespace(
        structural_level=0, status=StrictPointStatus.CONFIRMED,
        point_type="3sell", center_id="old-center",
        anchor_at=base + timedelta(minutes=25),
        confirmed_at=known_at, available_at=known_at,
        point_id="old-reference-third",
    )

    def level(units):
        return SimpleNamespace(
            structural_level=0, decomposition_mode="same_level",
            decomposition_boundaries=(boundary,), units=units,
            trend_types=(old_type,),
            center_result=SimpleNamespace(centers=(old_center,)),
        )

    old_only = transition_rule_state(level((old,)), (point,), known_at)
    with_new = transition_rule_state(level((old, new)), (point,), known_at)

    assert old_only["status"] == "active"
    assert old_only["new_initial_structure_unit_id"] is None
    assert old_only["natural_end_signal"] == "pending"
    assert with_new["status"] == "ended"
    assert with_new["new_initial_structure_unit_id"] == new.unit_id
    assert with_new["natural_end_signal"] == "confirmed_at_return"


def test_trend_return_to_last_center_confirms_signal_without_moving_boundary():
    base = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    boundary_at = base + timedelta(minutes=10)
    boundary = SimpleNamespace(
        anchor_at=boundary_at, confirmed_at=base + timedelta(minutes=12),
        available_at=base + timedelta(minutes=12), boundary_id="P",
        boundary_kind="trend_divergence", terminal_center_id="last-center",
    )
    old_center = SimpleNamespace(
        center_id="last-center", zd_tick=100, zg_tick=110,
        body_start_market_time=base,
        established_at=base + timedelta(minutes=5),
        available_at=base + timedelta(minutes=5),
    )
    ret = SimpleNamespace(
        unit_id="complete-return", locked=True,
        market_start=boundary_at,
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
