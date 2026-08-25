from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
)
from chanlun.core.strict_structure.identity import build_trend_id
from chanlun.core.strict_structure.models import (
    CenterEvidence,
    CenterState,
    SourceKind,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.trend_assembler import (
    _build,
    _geometric_movement_slices,
    assemble_trend_types,
    normalize_trend_assembly,
)
from chanlun.core.strict_structure.strength import (
    StrengthSnapshot,
    center_departure_comparison_leg,
    center_entry_comparison_leg,
)
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit
from tests.core.strict_structure.helpers import (
    ongoing_center,
    unit,
    valid_five_up_exit,
)


def three_center_fixture():
    """Three explicit physical entry/ABC/leave/return lifecycles.

    The first two centers are separated upward; the third overlaps the second
    and therefore locks the preceding up-trend at an upgrade boundary.  Each
    center owns an external entry, exactly three middle core units, an
    establishment leave, and its first outside return.  Consecutive centers
    share the preceding leave as entry and the return as the first core unit.
    """

    values = (
        # Center 1: entry u-0, core u-1..u-3, leave u-4, return u-5.
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 90),
        unit(2, "up", 90, 110),
        unit(3, "down", 110, 100),
        unit(4, "up", 100, 140),
        # Center 2: entry u-4, core u-5..u-7, leave u-8, return u-9.
        unit(5, "down", 140, 125),
        unit(6, "up", 125, 135),
        unit(7, "down", 135, 130),
        unit(8, "up", 130, 160),
        # Center 3: entry u-8, core u-9..u-11, leave u-12, return u-13.
        unit(9, "down", 160, 135),
        unit(10, "up", 135, 170),
        unit(11, "down", 170, 140),
        unit(12, "up", 140, 180),
        unit(13, "down", 180, 165),
    )
    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    assert len(centers) == 3
    first, second, third = centers
    return values, first, second, third


def sh513100_manual_tail_fixture():
    """Normalized L0 segment tail covered by the saved SH.513100 drawings."""

    specs = (
        ("up", 1667, 1841),
        ("down", 1841, 1788),
        ("up", 1788, 1912),
        ("down", 1912, 1875),
        ("up", 1875, 2126),
        ("down", 2126, 2026),
        ("up", 2026, 2270),
        ("down", 2270, 2150),
        ("up", 2150, 2386),
        ("down", 2386, 2318),
        ("up", 2318, 2577),
        ("down", 2577, 2113),
        ("up", 2113, 2203),
        ("down", 2203, 2092),
        ("up", 2092, 2383),
        ("down", 2383, 2240),
        ("up", 2240, 2311),
        ("down", 2311, 2133),
        ("up", 2133, 2232),
        ("down", 2232, 2140),
        ("up", 2140, 2189),
        ("down", 2189, 2136),
        ("up", 2136, 2192),
        ("down", 2192, 2035),
        ("up", 2035, 2292),
        ("down", 2292, 2203),
        ("up", 2203, 2274),
        ("down", 2274, 2166),
    )
    values = tuple(unit(index, *spec) for index, spec in enumerate(specs))
    return tuple(
        value
        if index < 23
        else replace(
            value,
            locked=False,
            confirmed_at=None,
            formed_at=value.available_at,
        )
        for index, value in enumerate(values)
    )


def test_sh513100_manual_5m_tail_is_partitioned_at_saved_boundaries() -> None:
    values = sh513100_manual_tail_fixture()

    slices = _geometric_movement_slices(values, 0)
    assert tuple(
        (
            values.index(movement[0]),
            values.index(movement[-1]),
            tuple(values.index(item) for item in witness),
        )
        for movement, witness in slices
    ) == (
        (0, 10, (11, 12, 13)),
        (11, 13, (14, 15, 16)),
        (14, 16, (17, 18, 19)),
        (17, 23, (24, 25, 26)),
    )

    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    result = assemble_trend_types(centers, values, 0)
    assert tuple(
        (values.index(trend.constituent_units[0]), values.index(trend.terminal_unit))
        for trend in result.current_trends
    ) == ((0, 10), (11, 13), (14, 16), (17, 23))
    assert tuple(trend.state for trend in result.current_trends) == (
        TrendState.LOCKED,
        TrendState.LOCKED,
        TrendState.LOCKED,
        TrendState.FORMING,
    )
    assert tuple(len(trend.centers) for trend in result.current_trends) == (0, 0, 0, 1)
    assert result.pending_movements[0].constituent_units == values[24:]

    locked_ids = set()
    for size in range(6, len(values) + 1):
        prefix = values[:size]
        prefix_centers = calculate_centers(
            prefix,
            0,
            SourceKind.SEGMENT,
        ).centers
        prefix_result = assemble_trend_types(prefix_centers, prefix, 0)
        current_locked_ids = {
            trend.trend_id
            for trend in prefix_result.current_trends
            if trend.state is TrendState.LOCKED
        }
        assert locked_ids.issubset(current_locked_ids)
        locked_ids.update(current_locked_ids)


def test_geometric_successor_locks_completed_predecessor_without_chain_gap() -> None:
    points = (100, 122, 91, 115, 111, 120, 85, 103, 94, 128, 107, 120, 100, 115, 105)
    values = tuple(
        unit(
            index,
            "up" if index % 2 == 0 else "down",
            points[index],
            points[index + 1],
        )
        for index in range(len(points) - 1)
    )

    structure = StrictRecursiveEngine(max_levels=1).calculate(values)
    trends = structure.levels[0].trend_types

    assert tuple(trend.state for trend in trends) == (
        TrendState.LOCKED,
        TrendState.LOCKED,
        TrendState.FORMING,
    )
    assert tuple(
        (values.index(trend.constituent_units[0]), values.index(trend.terminal_unit))
        for trend in trends
    ) == ((0, 5), (6, 8), (9, 13))
    assert all(
        previous.end_tick == current.start_tick
        and previous.market_end <= current.market_start
        for previous, current in zip(trends, trends[1:])
    )


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


def test_locked_trend_requires_structurally_closed_center():
    with pytest.raises(
        ValueError,
        match="completed trend requires structurally closed centers",
    ):
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


def test_completed_center_promotes_confirmed_centerless_tail_movements():
    values, first, _second, _third = three_center_fixture()
    source = (
        *values[:6],
        unit(6, "up", 125, 155),
        unit(7, "down", 155, 130),
        unit(8, "up", 130, 160),
        unit(9, "down", 160, 145),
        unit(10, "up", 145, 155),
        unit(11, "down", 155, 135),
        unit(12, "up", 135, 150),
        unit(13, "down", 150, 140),
    )

    result = assemble_trend_types((first,), source, 0)

    assert tuple(item.direction for item in result.current_trends) == (
        "up",
        "down",
        "up",
    )
    assert all(item.state is TrendState.LOCKED for item in result.current_trends)
    assert result.current_trends[0].centers == (first,)
    assert result.current_trends[1].constituent_units == source[5:8]
    assert result.current_trends[2].constituent_units == source[8:11]
    assert len(result.pending_movements) == 1
    assert result.pending_movements[0].constituent_units == source[11:]


def test_completed_movement_absorbs_unresolved_same_direction_tail() -> None:
    values, first, _second, _third = three_center_fixture()
    source = (
        *values[:6],
        unit(6, "up", 125, 155),
        unit(7, "down", 155, 145),
    )

    result = assemble_trend_types((first,), source, 0)

    assert len(result.current_trends) == 1
    (current,) = result.current_trends
    assert current.direction == "up"
    assert current.state is TrendState.FORMING
    assert current.constituent_units == source
    assert current.centers == (first,)
    assert result.completed_trends
    assert result.pending_movements == ()


def test_two_separated_centers_form_complete_uptrend_with_internal_return():
    values, first, second, _third = three_center_fixture()
    result = assemble_trend_types((first, second), values[:10], 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.COMPLETE
    assert trend.kind is TrendKind.TREND
    assert trend.direction == "up"
    assert trend.centers == (first, second)
    assert trend.constituent_units == values[:9]
    assert first.completion_return_unit in trend.constituent_units
    assert second.completion_return_unit not in trend.constituent_units
    assert trend.terminal_unit is second.completion_leave_unit


def test_same_direction_upgrade_boundary_is_combined_into_one_movement():
    values, first, second, third = three_center_fixture()
    result = assemble_trend_types((first, second, third), values, 0)
    assert len(result.current_trends) == 1
    (trend,) = result.current_trends
    assert trend.direction == "up"
    assert trend.kind is TrendKind.CONSOLIDATION
    assert trend.state is TrendState.COMPLETE
    assert trend.centers == (first, second, third)
    assert trend.constituent_units == values[:13]
    assert second.completion_return_unit in trend.constituent_units
    assert third.completion_return_unit not in trend.constituent_units
    assert result.pending_movements[0].constituent_units == values[13:]


def test_same_direction_merge_absorbs_completion_return_as_internal_unit():
    values, first, _second, _third = three_center_fixture()
    source_units = (
        *values[:6],
        unit(6, "up", 125, 150),
        unit(7, "down", 150, 135),
        unit(8, "up", 135, 160),
        unit(9, "down", 160, 145),
        unit(10, "up", 145, 155),
        unit(11, "down", 155, 150),
    )
    prefix_units = source_units[:5]
    prefix = _build(
        (first,),
        prefix_units,
        0,
        TrendState.LOCKED,
        first.completed_at,
        max(first.available_at, *(item.available_at for item in prefix_units)),
    )
    successor_units = source_units[5:9]
    successor_witness = source_units[9:12]
    successor_confirmation = max(
        item.confirmed_at for item in (*successor_units, *successor_witness)
    )
    successor = _build(
        (),
        successor_units,
        0,
        TrendState.LOCKED,
        successor_confirmation,
        max(item.available_at for item in (*successor_units, *successor_witness)),
        completion_witness_units=successor_witness,
    )

    result = normalize_trend_assembly(
        current_trends=(prefix, successor),
        completed_trends=(),
        decomposition_boundaries=(),
        source_units=source_units,
        structural_level=0,
    )

    assert len(result.current_trends) == 1
    (merged,) = result.current_trends
    assert merged.direction == "up"
    assert merged.centers == (first,)
    assert first.completion_return_unit in merged.constituent_units
    assert merged.terminal_unit is source_units[8]


def test_same_direction_ongoing_center_extends_forming_movement():
    values, first, second, _third = three_center_fixture()
    ongoing_centers = calculate_centers(values[:13], 0, SourceKind.SEGMENT).centers
    assert len(ongoing_centers) == 3
    ongoing_third = ongoing_centers[-1]

    result = assemble_trend_types(
        (first, second, ongoing_third),
        values[:13],
        0,
    )

    assert len(result.current_trends) == 1
    (forming,) = result.current_trends
    assert forming.direction == "up"
    assert forming.state is TrendState.FORMING
    assert forming.centers == (first, second, ongoing_third)
    assert forming.constituent_units == values[:13]
    assert any(
        trend.state is TrendState.COMPLETE and trend.centers == (first, second)
        for trend in result.completed_trends
    )
    assert not any(trend.state is TrendState.LOCKED for trend in result.current_trends)


def test_non_center_bridge_units_are_preserved_exactly_once():
    values, first, second, _third = three_center_fixture()
    trend = assemble_trend_types((first, second), values[:10], 0).current_trends[0]
    shared_return = first.completion_return_unit
    assert shared_return is not None
    assert shared_return in trend.constituent_units
    assert len({item.unit_id for item in trend.constituent_units}) == len(
        trend.constituent_units
    )


def test_trend_assembler_keeps_return_and_bridge_exactly_once_between_centers():
    values, first, second, _third = three_center_fixture()
    values = values[:10]
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
    assert centers[1].body_units[0] is shared
    assert centers[1].entry_unit is centers[0].completion_leave_unit
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
    assert len(result.current_trends) == 1
    (trend,) = result.current_trends
    trend_ids = [item.unit_id for item in trend.constituent_units]
    assert trend.direction == "up"
    assert trend_ids == [item.unit_id for item in values[:9]]
    assert trend_ids.count(values[4].unit_id) == 1
    assert result.pending_movements[0].constituent_units == values[9:]


def test_ongoing_center_produces_forming_trend_without_confirmation():
    center = ongoing_center()
    evidence = (
        (() if center.entry_unit is None else (center.entry_unit,))
        + center.body_units
        + ((center.pending_leave_unit,) if center.pending_leave_unit else ())
    )
    result = assemble_trend_types((center,), evidence, 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.FORMING
    assert trend.confirmed_at is None
    assert result.completed_trends == ()


def test_failed_establishment_leave_remains_in_trend_and_center_evidence():
    establishment = valid_five_up_exit()
    values = (
        *establishment,
        unit(5, "down", establishment[4].end_tick, 110),
    )
    center_result = calculate_centers(values, 0, SourceKind.SEGMENT)
    center = center_result.centers[0]
    assembly = assemble_trend_types(center_result.centers, values, 0)
    trend = assembly.current_trends[0]
    evidence = CenterEvidence.from_center(center)
    failed_id = establishment[4].unit_id

    assert trend.constituent_units == values
    assert tuple(item.unit_id for item in trend.constituent_units).count(failed_id) == 1
    assert evidence.failed_departure_unit_ids == (failed_id,)
    assert assembly.completed_trends == ()
    assert assembly.pending_movements == ()


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
    values = sh513100_manual_tail_fixture()
    centers = calculate_centers(values, 0, SourceKind.SEGMENT).centers
    locked = assemble_trend_types(centers, values, 0).current_trends[0]
    assert locked.state is TrendState.LOCKED
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
            ("u-2", "u-3", "u-4"): (100, 5, 2),
            ("u-8", "u-9", "u-10"): (50, 6, 3),
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


def test_same_direction_continuation_recomposes_current_chain_and_preserves_snapshot():
    values, first, second, _third = three_center_fixture()
    boundary_result = assemble_trend_types(
        (first, second),
        values[:11],
        0,
        strength=BoundaryStrength(),
    )
    (boundary_trend,) = boundary_result.current_trends
    (boundary,) = boundary_result.decomposition_boundaries
    suffix_units = (
        unit(11, "down", 170, 150),
        unit(12, "up", 150, 180),
        unit(13, "down", 180, 160),
        unit(14, "up", 160, 190),
        unit(15, "down", 190, 170),
        unit(16, "up", 170, 185),
        unit(17, "down", 185, 175),
        unit(18, "up", 175, 200),
        unit(19, "down", 200, 180),
        unit(20, "up", 180, 195),
        unit(21, "down", 195, 185),
        unit(22, "up", 185, 192),
    )

    def geometric_trend(constituents, witnesses):
        confirmation = max(item.confirmed_at for item in (*constituents, *witnesses))
        return _build(
            (),
            constituents,
            0,
            TrendState.LOCKED,
            confirmation,
            max(item.available_at for item in (*constituents, *witnesses)),
            completion_witness_units=witnesses,
        )

    local_up = geometric_trend(suffix_units[:4], suffix_units[4:7])
    local_down = geometric_trend(suffix_units[4:9], suffix_units[9:12])
    result = normalize_trend_assembly(
        current_trends=(boundary_trend, local_up, local_down),
        completed_trends=boundary_result.completed_trends,
        decomposition_boundaries=(boundary,),
        source_units=(*values[:11], *suffix_units),
        structural_level=0,
    )

    assert tuple(item.direction for item in result.current_trends) == ("up", "down")
    recomposed, successor = result.current_trends
    assert recomposed.constituent_units == (
        *boundary_trend.constituent_units,
        *suffix_units[:4],
    )
    assert successor == local_down
    assert result.decomposition_boundaries == ()
    assert boundary_result.completed_trends[0] in result.completed_trends
    assert len(result.pending_movements) == 1
    assert result.pending_movements[0].constituent_units == suffix_units[9:]


def test_three_unit_entry_waits_for_matching_three_unit_departure_boundary():
    values, first, second, third = three_center_fixture()

    result = assemble_trend_types(
        (first, second, third),
        values,
        0,
        strength=BoundaryStrength(),
    )

    assert len(result.decomposition_boundaries) == 1
    (locked,) = result.current_trends
    boundary = result.decomposition_boundaries[0]
    assert locked.state is TrendState.LOCKED
    assert locked.centers[0] is first
    assert locked.centers[-1].center_id == second.center_id
    assert locked.centers[-1].state is CenterState.DIVERGENCE_CLOSED
    assert locked.centers[-1].pending_leave_unit is None
    assert locked.centers[-1].completion_leave_unit is values[8]
    assert locked.centers[-1].completion_return_unit is values[9]
    assert locked.terminal_unit is values[10]
    assert locked.terminal_divergence is boundary.divergence
    assert boundary.anchor_at == values[10].market_end
    assert boundary.anchor_unit_id == values[10].unit_id
    assert boundary.divergence.compare_unit_id == values[4].unit_id
    assert boundary.divergence.signal_unit_id == boundary.anchor_unit_id
    assert boundary.divergence.compare_leg_unit_ids == tuple(
        item.unit_id for item in values[2:5]
    )
    assert boundary.divergence.signal_leg_unit_ids == tuple(
        item.unit_id for item in values[8:11]
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


def test_three_unit_departure_uses_the_whole_leg_extreme() -> None:
    values, first, second, _third = three_center_fixture()
    # u-8 makes the signal leg's new high; u-10 is the weaker second test that
    # anchors the reversal.  The terminal unit therefore need not exceed u-8.
    weaker_terminal = replace(values[10], end_tick=155, high_tick=155)
    source = (*values[:10], weaker_terminal)

    result = assemble_trend_types(
        (first, second),
        source,
        0,
        strength=BoundaryStrength(),
    )

    assert len(result.decomposition_boundaries) == 1
    boundary = result.decomposition_boundaries[0]
    assert boundary.anchor_unit_id == weaker_terminal.unit_id
    assert boundary.divergence.signal_leg_unit_ids == ("u-8", "u-9", "u-10")
    assert weaker_terminal.high_tick < source[8].high_tick
    assert boundary.divergence.price_extreme_confirmed is True


def test_three_unit_entry_selects_matching_complete_departure_leg():
    values, _first, second, _third = three_center_fixture()

    entry = center_entry_comparison_leg(second, values)
    assert entry is not None
    leg = center_departure_comparison_leg(second, values, width=entry.width)

    assert tuple(item.unit_id for item in entry.units) == ("u-2", "u-3", "u-4")
    assert leg is not None
    assert tuple(item.unit_id for item in leg.units) == ("u-8", "u-9", "u-10")
    assert leg.terminal_unit is values[10]

    prefix_centers = calculate_centers(values[:9], 0, SourceKind.SEGMENT).centers
    assert prefix_centers[-1].state is CenterState.ONGOING
    assert prefix_centers[-1].pending_leave_unit is values[8]
    assert (
        center_departure_comparison_leg(
            prefix_centers[-1], values[:9], width=entry.width
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

    # 候选 E1/E2/E3 为 u-4/u-5/u-6。E1.high == ZD，说明 E1 接触
    # 中枢闭区间，不能作为中枢外部的进入第一段。
    assert values[4].high_tick == center.zd_tick
    entry = center_entry_comparison_leg(center, values)
    assert entry is not None
    assert entry.width == 1
    assert entry.units == (values[6],)

    departure = center_departure_comparison_leg(center, values, width=entry.width)
    assert departure is not None
    assert departure.width == 1
    assert departure.units == (values[10],)


def test_one_unit_consolidation_exit_waits_for_non_extending_reversal() -> None:
    base_specs = (
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
    base = tuple(unit(index, *spec) for index, spec in enumerate(base_specs))
    center = calculate_centers(base, 0, SourceKind.SEGMENT).centers[-1]

    class OneUnitDecayStrength:
        def snapshot(self, value):
            area, peak, dif = {
                "u-6": (100, 5, 2),
                "u-10": (50, 3, 1),
            }[value.unit_id]
            return StrengthSnapshot(
                unit_id=value.unit_id,
                direction=value.direction,
                histogram_area=area,
                histogram_peak=peak,
                dif_extreme=dif,
                source="macd",
                available_at=value.available_at,
            )

    extending = (
        *base,
        unit(11, "down", 150, 140),
        unit(12, "up", 140, 160),
        unit(13, "down", 160, 145),
    )
    rejected = assemble_trend_types(
        (center,),
        extending,
        0,
        strength=OneUnitDecayStrength(),
        group_start_unit_id="u-6",
    )
    assert rejected.decomposition_boundaries == ()

    confirmed = (
        *base,
        unit(11, "down", 150, 140),
        unit(12, "up", 140, 148),
        unit(13, "down", 148, 142),
    )
    accepted = assemble_trend_types(
        (center,),
        confirmed,
        0,
        strength=OneUnitDecayStrength(),
        group_start_unit_id="u-6",
    )
    assert len(accepted.decomposition_boundaries) == 1
    boundary = accepted.decomposition_boundaries[0]
    assert boundary.anchor_unit_id == "u-10"
    assert boundary.available_at == confirmed[-1].available_at


def test_three_unit_divergence_boundary_appears_only_when_terminal_locks():
    values, _first, _second, _third = three_center_fixture()
    before_centers = calculate_centers(values[:10], 0, SourceKind.SEGMENT).centers
    signal_centers = calculate_centers(values[:11], 0, SourceKind.SEGMENT).centers

    before = assemble_trend_types(
        before_centers,
        values[:10],
        0,
        strength=BoundaryStrength(),
    )
    at_signal = assemble_trend_types(
        signal_centers,
        values[:11],
        0,
        strength=BoundaryStrength(),
    )

    assert before.decomposition_boundaries == ()
    assert before.current_trends[-1].state is TrendState.COMPLETE
    assert len(at_signal.decomposition_boundaries) == 1
    boundary = at_signal.decomposition_boundaries[0]
    locked = at_signal.current_trends[-1]
    assert boundary.anchor_unit_id == "u-10"
    assert locked.state is TrendState.LOCKED
    assert locked.centers[-1].state is CenterState.DIVERGENCE_CLOSED
    assert locked.centers[-1].completion_leave_unit is values[8]
    assert locked.centers[-1].completion_return_unit is values[9]
    assert boundary.terminal_center_id == locked.centers[-1].center_id


def test_superseded_prior_center_does_not_confirm_divergence_boundary() -> None:
    values, _first, _second, _third = three_center_fixture()
    first, terminal = calculate_centers(
        values[:11],
        0,
        SourceKind.SEGMENT,
    ).centers
    bridge = values[4]
    superseded = replace(
        first,
        state=CenterState.SUPERSEDED,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        superseded_by_center_id=terminal.center_id,
        superseded_at=terminal.established_at,
        supersession_bridge_units=(bridge,),
        available_at=max(
            first.available_at,
            terminal.established_at,
            bridge.available_at,
        ),
    )

    result = assemble_trend_types(
        (superseded, terminal),
        values[:11],
        0,
        strength=BoundaryStrength(),
    )

    assert result.decomposition_boundaries == ()
    (trend,) = result.current_trends
    assert trend.state is TrendState.COMPLETE
    assert trend.centers == (superseded, terminal)
    assert trend.terminal_divergence is None


def test_unlocked_terminal_divergence_leg_remains_a_forming_trend() -> None:
    values, _first, _second, _third = three_center_fixture()
    unlocked_values = (
        *values[:10],
        replace(
            values[10],
            locked=False,
            confirmed_at=None,
            formed_at=values[10].available_at,
        ),
    )
    centers = calculate_centers(
        unlocked_values,
        0,
        SourceKind.SEGMENT,
    ).centers

    result = assemble_trend_types(
        centers,
        unlocked_values,
        0,
        strength=BoundaryStrength(),
    )

    assert result.decomposition_boundaries == ()
    assert result.current_trends[-1].terminal_divergence is None


class PeakDifOnlyDecayStrength:
    def snapshot(self, value):
        key = tuple(value.child_ids) if len(value.child_ids) == 3 else value.unit_id
        area, peak, dif = {
            ("u-2", "u-3", "u-4"): (100, 5, 2),
            ("u-8", "u-9", "u-10"): (110, 3, 1),
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
    centers = calculate_centers(values[:11], 0, SourceKind.SEGMENT).centers

    result = assemble_trend_types(
        centers,
        values[:11],
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
    values, _first, _second, _third = three_center_fixture()
    strength = BoundaryStrength()
    seen = {}
    latest = None
    for size in range(3, len(values) + 1):
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
        "u-10",
    )
    locked = tuple(trend for trend in level.trend_types if trend.locked)
    assert all(
        previous.end_tick == current.start_tick
        for previous, current in zip(locked, locked[1:])
    )
    index = {item.unit_id: offset for offset, item in enumerate(level.units)}
    for boundary in level.decomposition_boundaries:
        terminal_centers = tuple(
            center
            for center in level.center_result.centers
            if center.center_id == boundary.terminal_center_id
        )
        assert len(terminal_centers) == 1
        terminal_center = terminal_centers[0]
        assert terminal_center.state is CenterState.DIVERGENCE_CLOSED
        assert boundary.divergence.comparison_width == 3
        assert terminal_center.pending_leave_unit is None
        assert terminal_center.completion_leave_unit is not None
        assert (
            terminal_center.completion_leave_unit.unit_id
            == (boundary.divergence.signal_leg_unit_ids[0])
        )
        assert terminal_center.boundary_anchor_unit_id == boundary.anchor_unit_id
        assert terminal_center.boundary_divergence_id == (
            boundary.divergence.divergence_id
        )
        boundary_trend = next(
            trend for trend in locked if trend.trend_id == boundary.left_trend_id
        )
        assert boundary_trend.centers[-1] == terminal_center
        assert boundary_trend.terminal_unit.unit_id == boundary.anchor_unit_id
        assert boundary_trend.terminal_divergence == boundary.divergence

        boundary_index = index[boundary.anchor_unit_id]
        for center in level.center_result.centers:
            evidence = [
                *(() if center.entry_unit is None else (center.entry_unit,)),
                *center.body_units,
            ]
            if center.completion_leave_unit is not None:
                evidence.append(center.completion_leave_unit)
            if center.completion_return_unit is not None:
                evidence.append(center.completion_return_unit)
            offsets = tuple(index[item.unit_id] for item in evidence)
            assert not min(offsets) <= boundary_index < max(offsets)


def test_raw_departure_without_whole_trend_new_extreme_is_not_trend_boundary():
    specs = tuple(
        (item.direction, item.start_tick, item.end_tick)
        for item in three_center_fixture()[0]
    )
    prefix = unit(0, "down", 200, 90)
    tail = tuple(
        unit(index + 1, direction, start, end)
        for index, (direction, start, end) in enumerate(specs)
    )
    values = (prefix, *tail)
    centers = calculate_centers(tail, 0, SourceKind.SEGMENT).centers

    result = assemble_trend_types(
        centers[:2],
        values,
        0,
        strength=CausalDecayStrength(),
        group_start_unit_id=prefix.unit_id,
    )

    entry = center_entry_comparison_leg(centers[1], values)
    assert entry is not None
    departure = center_departure_comparison_leg(
        centers[1],
        values,
        width=entry.width,
    )
    assert departure is not None
    assert departure.terminal_unit.high_tick > entry.terminal_unit.high_tick
    assert departure.terminal_unit.high_tick < prefix.high_tick
    assert result.decomposition_boundaries == ()
    assert all(trend.terminal_divergence is None for trend in result.current_trends)


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
