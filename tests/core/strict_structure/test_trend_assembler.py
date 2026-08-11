from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
)
from chanlun.core.strict_structure.identity import build_trend_id
from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.strength import (
    StrengthSnapshot,
    center_departure_comparison_leg,
    center_entry_comparison_leg,
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
    ongoing_centers = calculate_centers(values[:18], 0, SourceKind.SEGMENT).centers
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
    first_ids = [item.unit_id for item in result.current_trends[0].constituent_units]
    second_ids = [item.unit_id for item in result.current_trends[1].constituent_units]
    assert first_ids == [item.unit_id for item in values[:5]]
    assert second_ids == [item.unit_id for item in values[5:9]]
    assert (first_ids + second_ids).count(values[4].unit_id) == 1


def test_ongoing_center_produces_forming_trend_without_confirmation():
    center = ongoing_center()
    evidence = (
        (center.entry_unit,)
        + center.body_units
        + ((center.pending_leave_unit,) if center.pending_leave_unit else ())
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
    assert item.child_ids == tuple(value.unit_id for value in locked.constituent_units)


def test_trend_identity_cannot_be_detached_from_its_evidence():
    values, first, _second, _third = three_center_fixture()
    trend = assemble_trend_types((first,), values[:6], 0).current_trends[0]
    with pytest.raises(ValueError, match="immutable trend evidence"):
        replace(trend, trend_id="forged-trend-id")


class BoundaryStrength:
    def snapshot(self, value):
        key = tuple(value.child_ids) if len(value.child_ids) == 3 else value.unit_id
        area, peak, dif = {
            ("u-4", "u-5", "u-6"): (100, 5, 2),
            ("u-10", "u-11", "u-12"): (50, 6, 3),
        }[key]
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd",
            available_at=value.available_at,
        )


def test_three_unit_entry_waits_for_matching_three_unit_departure_boundary():
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
    assert locked.centers[0] is first
    assert locked.centers[-1].center_id == second.center_id
    assert locked.centers[-1].state is CenterState.DIVERGENCE_CLOSED
    assert locked.centers[-1].pending_leave_unit is None
    assert locked.centers[-1].completion_leave_unit is values[10]
    assert locked.centers[-1].completion_return_unit is values[11]
    assert locked.terminal_unit is values[12]
    assert locked.terminal_divergence is boundary.divergence
    assert boundary.anchor_at == values[12].market_end
    assert boundary.anchor_unit_id == values[12].unit_id
    assert boundary.divergence.compare_unit_id == values[6].unit_id
    assert boundary.divergence.signal_unit_id == boundary.anchor_unit_id
    assert boundary.divergence.compare_leg_unit_ids == tuple(
        item.unit_id for item in values[4:7]
    )
    assert boundary.divergence.signal_leg_unit_ids == tuple(
        item.unit_id for item in values[10:13]
    )
    assert boundary.divergence.comparison_width == 3
    assert boundary.divergence.histogram_area_decayed is True
    assert boundary.divergence.histogram_peak_decayed is False
    assert boundary.divergence.dif_extreme_decayed is False
    assert boundary.available_at >= boundary.anchor_at
    assert boundary.left_trend_id == locked.trend_id
    assert boundary.terminal_center_id == locked.centers[-1].center_id
    assert locked.centers[-1].boundary_divergence_id == (
        boundary.divergence.divergence_id
    )
    assert tail.constituent_units[0] is values[13]


def test_three_unit_entry_selects_matching_complete_departure_leg():
    values, _first, second, _third = three_center_fixture()

    entry = center_entry_comparison_leg(second, values)
    assert entry is not None
    leg = center_departure_comparison_leg(second, values, width=entry.width)

    assert tuple(item.unit_id for item in entry.units) == ("u-4", "u-5", "u-6")
    assert leg is not None
    assert tuple(item.unit_id for item in leg.units) == ("u-10", "u-11", "u-12")
    assert leg.terminal_unit is values[12]

    prefix_centers = calculate_centers(values[:11], 0, SourceKind.SEGMENT).centers
    assert prefix_centers[-1].state is CenterState.ONGOING
    assert prefix_centers[-1].pending_leave_unit is values[10]
    assert (
        center_departure_comparison_leg(
            prefix_centers[-1], values[:11], width=entry.width
        )
        is None
    )


def test_entry_first_leg_touching_center_falls_back_to_one_unit_comparison():
    specs = (
        ("up", 90, 120),
        ("down", 120, 100),
        ("up", 100, 115),
        ("down", 115, 105),
        ("up", 105, 130),
        ("down", 130, 110),
        ("up", 110, 140),
        ("down", 140, 125),
        ("up", 125, 145),
        ("down", 145, 130),
        ("up", 130, 150),
    )
    values = tuple(unit(index, *spec) for index, spec in enumerate(specs))
    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    assert len(centers) == 2
    center = centers[-1]

    # Candidate E1/E2/E3 is u-4/u-5/u-6.  E1.high == ZD, so E1 touches
    # the center interval and is not an external incoming leg.
    assert values[4].high_tick == center.zd_tick
    entry = center_entry_comparison_leg(center, values)
    assert entry is not None
    assert entry.width == 1
    assert entry.units == (values[6],)

    departure = center_departure_comparison_leg(center, values, width=entry.width)
    assert departure is not None
    assert departure.width == 1
    assert departure.units == (values[10],)


def test_three_unit_divergence_boundary_appears_only_when_terminal_locks():
    values, _first, _second, _third = three_center_fixture()
    before_centers = calculate_centers(values[:12], 0, SourceKind.SEGMENT).centers
    signal_centers = calculate_centers(values[:13], 0, SourceKind.SEGMENT).centers

    before = assemble_trend_types(
        before_centers,
        values[:12],
        0,
        strength=BoundaryStrength(),
    )
    at_signal = assemble_trend_types(
        signal_centers,
        values[:13],
        0,
        strength=BoundaryStrength(),
    )

    assert before.decomposition_boundaries == ()
    assert before.current_trends[-1].state is TrendState.COMPLETE
    assert len(at_signal.decomposition_boundaries) == 1
    boundary = at_signal.decomposition_boundaries[0]
    locked = at_signal.current_trends[-1]
    assert boundary.anchor_unit_id == "u-12"
    assert locked.state is TrendState.LOCKED
    assert locked.centers[-1].state is CenterState.DIVERGENCE_CLOSED
    assert locked.centers[-1].completion_leave_unit is values[10]
    assert locked.centers[-1].completion_return_unit is values[11]
    assert boundary.terminal_center_id == locked.centers[-1].center_id


class PeakDifOnlyDecayStrength:
    def snapshot(self, value):
        key = tuple(value.child_ids) if len(value.child_ids) == 3 else value.unit_id
        area, peak, dif = {
            ("u-4", "u-5", "u-6"): (100, 5, 2),
            ("u-10", "u-11", "u-12"): (110, 3, 1),
        }[key]
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd",
            available_at=value.available_at,
        )


def test_peak_or_dif_decay_can_confirm_without_histogram_area_decay():
    values, _first, _second, _third = three_center_fixture()
    centers = calculate_centers(values[:13], 0, SourceKind.SEGMENT).centers

    result = assemble_trend_types(
        centers,
        values[:13],
        0,
        strength=PeakDifOnlyDecayStrength(),
    )

    assert len(result.decomposition_boundaries) == 1
    evidence = result.decomposition_boundaries[0].divergence
    assert evidence.histogram_area_decayed is False
    assert evidence.histogram_peak_decayed is True
    assert evidence.dif_extreme_decayed is True
    assert evidence.is_divergent is True


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
            source="macd",
            available_at=value.available_at,
        )


def test_boundary_replay_keeps_centers_and_locked_trends_on_one_side():
    specs = (
        ("up", 100, 115),
        ("down", 115, 80),
        ("up", 80, 118),
        ("down", 118, 88),
        ("up", 88, 94),
        ("down", 94, 66),
        ("up", 66, 74),
        ("down", 74, 44),
        ("up", 44, 56),
        ("down", 56, 30),
        ("up", 30, 45),
        ("down", 45, 12),
        ("up", 12, 27),
        ("down", 27, 12),
        ("up", 12, 17),
        ("down", 17, -16),
        ("up", -16, -1),
        ("down", -1, -36),
        ("up", -36, -31),
        ("down", -31, -64),
        ("up", -64, -38),
        ("down", -38, -64),
        ("up", -64, -41),
        ("down", -41, -78),
        ("up", -78, -56),
        ("down", -56, -87),
        ("up", -87, -73),
        ("down", -73, -107),
        ("up", -107, -82),
        ("down", -82, -95),
        ("up", -95, -76),
        ("down", -76, -104),
        ("up", -104, -70),
        ("down", -70, -108),
        ("up", -108, -97),
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
        "u-17",
    )
    boundary = level.decomposition_boundaries[0]
    terminal_centers = tuple(
        center
        for center in level.center_result.centers
        if center.center_id == boundary.terminal_center_id
    )
    assert len(terminal_centers) == 1
    terminal_center = terminal_centers[0]
    assert terminal_center.state is CenterState.DIVERGENCE_CLOSED
    assert terminal_center.pending_leave_unit is None
    assert terminal_center.completion_leave_unit is not None
    assert terminal_center.completion_leave_unit.unit_id == (
        boundary.divergence.signal_leg_unit_ids[0]
    )
    assert terminal_center.boundary_anchor_unit_id == boundary.anchor_unit_id
    assert terminal_center.boundary_divergence_id == (boundary.divergence.divergence_id)
    locked = tuple(trend for trend in level.trend_types if trend.locked)
    boundary_trend = next(
        trend for trend in locked if trend.trend_id == boundary.left_trend_id
    )
    assert boundary_trend.centers[-1] == terminal_center
    assert boundary_trend.terminal_unit.unit_id == boundary.anchor_unit_id
    assert boundary_trend.terminal_divergence == boundary.divergence
    assert all(
        previous.end_tick == current.start_tick
        for previous, current in zip(locked, locked[1:])
    )
    index = {item.unit_id: offset for offset, item in enumerate(level.units)}
    boundary_index = index[boundary.anchor_unit_id]
    for center in level.center_result.centers:
        evidence = [center.entry_unit, *center.body_units]
        if center.completion_leave_unit is not None:
            evidence.append(center.completion_leave_unit)
        if center.completion_return_unit is not None:
            evidence.append(center.completion_return_unit)
        offsets = tuple(index[item.unit_id] for item in evidence)
        assert not min(offsets) <= boundary_index < max(offsets)


def test_raw_departure_without_whole_trend_new_extreme_is_not_trend_boundary():
    specs = (
        ("up", 159, 250),
        ("down", 250, 148),
        ("up", 148, 176),
        ("down", 176, 155),
        ("up", 155, 203),
        ("down", 203, 196),
        ("up", 196, 228),
        ("down", 228, 208),
        ("up", 208, 216),
        ("down", 216, 208),
        ("up", 208, 239),
        ("down", 239, 200),
        ("up", 200, 240),
        ("down", 240, 207),
        ("up", 207, 233),
        ("down", 233, 223),
        ("up", 223, 239),
    )
    values = tuple(
        unit(index, direction, start, end)
        for index, (direction, start, end) in enumerate(specs)
    )

    result = StrictRecursiveEngine(max_levels=1).calculate(
        values,
        strength=CausalDecayStrength(),
    )

    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    earlier = centers[0].lifecycle_leave_unit
    later = centers[1].lifecycle_leave_unit
    assert earlier is values[4]
    assert later is values[14]
    assert later.high_tick > earlier.high_tick
    assert later.high_tick < values[0].high_tick
    assert result.levels[0].decomposition_boundaries == ()
    assert all(
        trend.terminal_divergence is None for trend in result.levels[0].completed_trends
    )


def test_terminal_extreme_uses_exact_group_start_before_first_center_entry():
    specs = (
        ("up", 100, 136),
        ("down", 136, 90),
        ("up", 90, 139),
        ("down", 139, 125),
        ("up", 125, 133),
        ("down", 133, 88),
        ("up", 88, 116),
        ("down", 116, 108),
        ("up", 108, 123),
        ("down", 123, 120),
        ("up", 120, 128),
        ("down", 128, 101),
        ("up", 101, 115),
        ("down", 115, 98),
        ("up", 98, 101),
        ("down", 101, 55),
        ("up", 55, 93),
        ("down", 93, 89),
        ("up", 89, 142),
        ("down", 142, 126),
        ("up", 126, 158),
        ("down", 158, 133),
        ("up", 133, 139),
        ("down", 139, 117),
        ("up", 117, 141),
        ("down", 141, 114),
        ("up", 114, 120),
        ("down", 120, 97),
        ("up", 97, 110),
        ("down", 110, 79),
        ("up", 79, 113),
        ("down", 113, 60),
        ("up", 60, 113),
        ("down", 113, 81),
        ("up", 81, 88),
        ("down", 88, 56),
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
