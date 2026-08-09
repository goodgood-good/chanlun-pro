from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
)
from chanlun.core.strict_structure.identity import build_trend_id
from chanlun.core.strict_structure.models import (
    SourceKind,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.strength import (
    StrengthSnapshot,
    completed_center_departure_leg,
)
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit
from tests.core.strict_structure.helpers import ongoing_center, unit


def three_center_fixture():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 140),
        unit(5, "down", 140, 125),
        unit(6, "up", 125, 175),
        unit(7, "down", 175, 155),
        unit(8, "up", 155, 175),
        unit(9, "down", 175, 160),
        unit(10, "up", 160, 190),
        unit(11, "down", 190, 177),
        unit(12, "up", 177, 200),
        unit(13, "down", 200, 130),
        unit(14, "up", 130, 150),
        unit(15, "down", 150, 120),
        unit(16, "up", 120, 145),
        unit(17, "down", 145, 100),
        unit(18, "up", 100, 120),
    )
    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    assert len(centers) == 3
    first, second, third = centers
    return values, first, second, third


def _trend_for(center, *, state, constituent_units=None):
    values = center.body_units if constituent_units is None else constituent_units
    direction = "up" if values[-1].end_tick > values[0].start_tick else "down"
    confirmed_at = (
        None
        if state is TrendState.FORMING
        else center.completed_at or center.established_at
    )
    return TrendType(
        trend_id=build_trend_id(
            price_basis_revision=center.price_basis_revision,
            structural_level=center.structural_level,
            center_ids=(center.center_id,),
            constituent_unit_ids=tuple(item.unit_id for item in values),
            direction=direction,
        ),
        structural_level=center.structural_level,
        price_basis_revision=center.price_basis_revision,
        kind=TrendKind.CONSOLIDATION,
        direction=direction,
        state=state,
        centers=(center,),
        constituent_units=values,
        start_tick=values[0].start_tick,
        end_tick=values[-1].end_tick,
        low_tick=min(item.low_tick for item in values),
        high_tick=max(item.high_tick for item in values),
        market_start=values[0].market_start,
        market_end=values[-1].market_end,
        confirmed_at=confirmed_at,
        available_at=max(
            center.available_at,
            *(item.available_at for item in values),
        ),
    )


def test_locked_trend_requires_completed_center():
    with pytest.raises(ValueError, match="completed trend requires completed centers"):
        _trend_for(ongoing_center(), state=TrendState.LOCKED)


def test_single_completed_center_owns_body_and_excludes_completion_return():
    values, first, _second, _third = three_center_fixture()
    result = assemble_trend_types((first,), values[:6], 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.COMPLETE
    assert trend.kind is TrendKind.CONSOLIDATION
    assert trend.constituent_units == values[:5]
    assert first.body_units == values[1:4]
    assert trend.terminal_unit is first.completion_leave_unit
    assert first.completion_return_unit not in trend.constituent_units


def test_two_separated_centers_form_complete_uptrend_with_internal_return():
    values, first, second, _third = three_center_fixture()
    result = assemble_trend_types((first, second), values[:12], 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.COMPLETE
    assert trend.kind is TrendKind.TREND
    assert trend.direction == "up"
    assert trend.centers == (first, second)
    assert trend.constituent_units == values[:11]
    assert first.completion_return_unit in trend.constituent_units
    assert second.completion_return_unit not in trend.constituent_units
    assert trend.terminal_unit is second.completion_leave_unit


def test_upgrade_boundary_locks_previous_trend_and_starts_at_completion_return():
    values, first, second, third = three_center_fixture()
    result = assemble_trend_types((first, second, third), values, 0)
    assert len(result.current_trends) == 2
    locked, tail = result.current_trends
    assert locked.state is TrendState.LOCKED
    assert locked.centers == (first, second)
    assert locked.constituent_units == values[:11]
    assert locked.terminal_unit is second.completion_leave_unit
    assert tail.state is TrendState.COMPLETE
    assert tail.centers == (third,)
    assert tail.constituent_units == values[11:18]
    assert tail.constituent_units[0] is second.completion_return_unit
    assert third.completion_return_unit not in tail.constituent_units


def test_ongoing_boundary_does_not_lock_a_stable_completed_trend():
    values, first, second, _third = three_center_fixture()
    ongoing_centers = calculate_centers(
        values[:18], 0, SourceKind.SEGMENT
    ).centers
    assert len(ongoing_centers) == 3
    ongoing_third = ongoing_centers[-1]

    result = assemble_trend_types(
        (first, second, ongoing_third),
        values[:18],
        0,
    )

    stable, forming = result.current_trends
    assert stable.state is TrendState.COMPLETE
    assert stable.centers == (first, second)
    assert forming.state is TrendState.FORMING
    assert forming.centers == (ongoing_third,)
    assert not any(trend.state is TrendState.LOCKED for trend in result.current_trends)


def test_non_center_bridge_units_are_preserved_exactly_once():
    values, first, second, _third = three_center_fixture()
    trend = assemble_trend_types((first, second), values[:12], 0).current_trends[0]
    bridge = values[5:7]
    assert all(item in trend.constituent_units for item in bridge)
    assert len({item.unit_id for item in trend.constituent_units}) == len(
        trend.constituent_units
    )


def test_trend_assembler_keeps_return_and_bridge_exactly_once_between_centers():
    values, first, second, _third = three_center_fixture()
    values = values[:12]
    centers = (first, second)

    result = assemble_trend_types(centers, values, 0)

    assert len(centers) == 2
    assert result.current_trends
    assert len(result.current_trends) == 1
    trend = result.current_trends[0]
    ids = [item.unit_id for item in trend.constituent_units]
    shared = centers[0].completion_return_unit
    assert shared is not None
    assert ids.count(shared.unit_id) == 1
    assert centers[1].entry_unit.market_start >= shared.market_start
    assert len(ids) == len(set(ids))


def test_trend_assembler_accepts_leave_shared_as_next_center_entry():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 130),
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 132),
    )
    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers

    result = assemble_trend_types(centers, values, 0)

    assert len(centers) == 2
    assert centers[1].entry_unit is centers[0].completion_leave_unit
    assert len(result.current_trends) == 2
    first_ids = [
        item.unit_id for item in result.current_trends[0].constituent_units
    ]
    second_ids = [
        item.unit_id for item in result.current_trends[1].constituent_units
    ]
    assert first_ids == [item.unit_id for item in values[:5]]
    assert second_ids == [item.unit_id for item in values[5:9]]
    assert (first_ids + second_ids).count(values[4].unit_id) == 1


def test_ongoing_center_produces_forming_trend_without_confirmation():
    center = ongoing_center()
    evidence = (center.entry_unit,) + center.body_units + (
        (center.pending_leave_unit,) if center.pending_leave_unit else ()
    )
    result = assemble_trend_types((center,), evidence, 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.FORMING
    assert trend.confirmed_at is None
    assert result.completed_trends == ()


def test_center_reference_validation_rejects_missing_or_changed_evidence():
    values, first, _second, _third = three_center_fixture()
    with pytest.raises(ValueError, match="completion return"):
        assemble_trend_types((first,), values[:5], 0)
    changed = values[:4] + (replace(values[4], unit_id="changed"),) + values[5:6]
    with pytest.raises(ValueError, match="completion leave"):
        assemble_trend_types((first,), changed, 0)


def test_forming_and_complete_trends_cannot_become_recursive_units():
    values, first, _second, _third = three_center_fixture()
    complete = assemble_trend_types((first,), values[:6], 0).current_trends[0]
    forming = _trend_for(ongoing_center(), state=TrendState.FORMING)
    for trend in (complete, forming):
        with pytest.raises(ValueError, match="only locked trend types can recurse"):
            trend_type_to_unit(trend)


def test_locked_trend_becomes_next_level_unit_with_owned_children():
    values, first, second, third = three_center_fixture()
    locked = assemble_trend_types(
        (first, second, third),
        values,
        0,
    ).current_trends[0]
    item = trend_type_to_unit(locked)
    assert item.structural_level == 1
    assert item.source_kind is SourceKind.TREND_TYPE
    assert item.locked
    assert item.unit_id == locked.trend_id
    assert item.child_ids == tuple(
        value.unit_id for value in locked.constituent_units
    )


def test_trend_identity_cannot_be_detached_from_its_evidence():
    values, first, _second, _third = three_center_fixture()
    trend = assemble_trend_types((first,), values[:6], 0).current_trends[0]
    with pytest.raises(ValueError, match="immutable trend evidence"):
        replace(trend, trend_id="forged-trend-id")


class BoundaryStrength:
    def snapshot(self, value):
        key = value.child_ids[-1] if value.child_ids else value.unit_id
        area, peak, dif = {
            "u-6": (100, 5, 2),
            "u-12": (50, 3, 1),
        }[key]
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd_htf",
            available_at=value.available_at,
        )


def test_confirmed_complete_c_divergence_is_an_immediate_trend_boundary():
    values, first, second, third = three_center_fixture()

    result = assemble_trend_types(
        (first, second, third),
        values,
        0,
        strength=BoundaryStrength(),
    )

    assert len(result.decomposition_boundaries) == 1
    locked, tail = result.current_trends
    boundary = result.decomposition_boundaries[0]
    assert locked.state is TrendState.LOCKED
    assert locked.centers == (first, second)
    assert locked.terminal_unit is values[12]
    assert locked.terminal_divergence is boundary.divergence
    assert boundary.anchor_at == values[12].market_end
    assert boundary.anchor_unit_id == values[12].unit_id
    assert boundary.divergence.signal_unit_id != boundary.anchor_unit_id
    assert boundary.available_at >= boundary.anchor_at
    assert boundary.left_trend_id == locked.trend_id
    assert tail.constituent_units[0] is values[13]


def test_complete_c_strength_window_covers_leave_return_and_terminal_signal():
    values, _first, second, _third = three_center_fixture()

    leg = completed_center_departure_leg(second, values)

    assert leg is not None
    assert leg.unit_id != values[12].unit_id
    assert leg.market_start == second.completion_leave_unit.market_start
    assert leg.market_end == values[12].market_end
    assert leg.child_ids == ("u-10", "u-11", "u-12")


def test_divergence_boundary_does_not_exist_before_terminal_c_is_confirmed():
    values, first, second, _third = three_center_fixture()

    before = assemble_trend_types(
        (first, second),
        values[:12],
        0,
        strength=BoundaryStrength(),
    )
    after = assemble_trend_types(
        (first, second),
        values[:13],
        0,
        strength=BoundaryStrength(),
    )

    assert before.decomposition_boundaries == ()
    assert before.current_trends[-1].state is TrendState.COMPLETE
    assert len(after.decomposition_boundaries) == 1
    assert after.current_trends[-1].state is TrendState.LOCKED


class CausalDecayStrength:
    def snapshot(self, value):
        magnitude = max(1.0, 100_000_000.0 - value.market_end.timestamp() / 300)
        signed = magnitude if value.direction == "up" else -magnitude
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=magnitude,
            histogram_peak=signed,
            dif_extreme=signed,
            source="macd_native",
            available_at=value.available_at,
        )


def test_boundary_replay_keeps_centers_and_locked_trends_on_one_side():
    specs = (
        ("up", 100, 115), ("down", 115, 80), ("up", 80, 118),
        ("down", 118, 88), ("up", 88, 94), ("down", 94, 66),
        ("up", 66, 74), ("down", 74, 44), ("up", 44, 56),
        ("down", 56, 30), ("up", 30, 45), ("down", 45, 12),
        ("up", 12, 27), ("down", 27, 12), ("up", 12, 17),
        ("down", 17, -16), ("up", -16, -1), ("down", -1, -36),
        ("up", -36, -31), ("down", -31, -64), ("up", -64, -38),
        ("down", -38, -64), ("up", -64, -41), ("down", -41, -78),
        ("up", -78, -56), ("down", -56, -87), ("up", -87, -73),
        ("down", -73, -107), ("up", -107, -82), ("down", -82, -95),
        ("up", -95, -76), ("down", -76, -104), ("up", -104, -70),
        ("down", -70, -108), ("up", -108, -97),
    )
    values = tuple(
        unit(index, direction, start, end)
        for index, (direction, start, end) in enumerate(specs)
    )
    strength = CausalDecayStrength()
    seen = {}
    latest = None
    for size in range(5, len(values) + 1):
        latest = StrictRecursiveEngine(max_levels=3).calculate(
            values[:size],
            strength=strength,
        )
        current = {
            boundary.boundary_id: boundary
            for level in latest.levels
            for boundary in level.decomposition_boundaries
        }
        assert set(seen).issubset(current)
        assert all(current[item_id] == item for item_id, item in seen.items())
        seen.update(current)

    assert latest is not None
    level = latest.levels[0]
    assert tuple(item.anchor_unit_id for item in level.decomposition_boundaries) == (
        "u-27",
    )
    locked = tuple(trend for trend in level.trend_types if trend.locked)
    assert all(
        previous.end_tick == current.start_tick
        for previous, current in zip(locked, locked[1:])
    )
    index = {item.unit_id: offset for offset, item in enumerate(level.units)}
    boundary_index = index["u-27"]
    for center in level.center_result.centers:
        evidence = [center.entry_unit, *center.body_units]
        if center.completion_leave_unit is not None:
            evidence.append(center.completion_leave_unit)
        if center.completion_return_unit is not None:
            evidence.append(center.completion_return_unit)
        offsets = tuple(index[item.unit_id] for item in evidence)
        assert not min(offsets) <= boundary_index < max(offsets)


def test_terminal_c_without_whole_trend_new_extreme_is_not_trend_boundary():
    specs = (
        ("up", 159, 172), ("down", 172, 148), ("up", 148, 176),
        ("down", 176, 155), ("up", 155, 203), ("down", 203, 196),
        ("up", 196, 228), ("down", 228, 208), ("up", 208, 216),
        ("down", 216, 208), ("up", 208, 239), ("down", 239, 200),
        ("up", 200, 240), ("down", 240, 207), ("up", 207, 233),
        ("down", 233, 223), ("up", 223, 239),
    )
    values = tuple(
        unit(index, direction, start, end)
        for index, (direction, start, end) in enumerate(specs)
    )

    result = StrictRecursiveEngine(max_levels=1).calculate(
        values,
        strength=CausalDecayStrength(),
    )

    assert result.levels[0].decomposition_boundaries == ()
    assert all(
        trend.terminal_divergence is None
        for trend in result.levels[0].completed_trends
    )


def test_terminal_extreme_uses_exact_group_start_before_first_center_entry():
    specs = (
        ("up", 100, 136), ("down", 136, 90), ("up", 90, 139),
        ("down", 139, 125), ("up", 125, 133), ("down", 133, 88),
        ("up", 88, 116), ("down", 116, 108), ("up", 108, 123),
        ("down", 123, 120), ("up", 120, 128), ("down", 128, 101),
        ("up", 101, 115), ("down", 115, 98), ("up", 98, 101),
        ("down", 101, 55), ("up", 55, 93), ("down", 93, 89),
        ("up", 89, 142), ("down", 142, 126), ("up", 126, 158),
        ("down", 158, 133), ("up", 133, 139), ("down", 139, 117),
        ("up", 117, 141), ("down", 141, 114), ("up", 114, 120),
        ("down", 120, 97), ("up", 97, 110), ("down", 110, 79),
        ("up", 79, 113), ("down", 113, 60), ("up", 60, 113),
        ("down", 113, 81), ("up", 81, 88), ("down", 88, 56),
    )
    values = tuple(
        unit(index, direction, start, end)
        for index, (direction, start, end) in enumerate(specs)
    )

    structure = StrictRecursiveEngine(max_levels=3).calculate(
        values,
        strength=CausalDecayStrength(),
    )

    assert all(
        boundary.anchor_unit_id != "u-35"
        for level in structure.levels
        for boundary in level.decomposition_boundaries
    )
