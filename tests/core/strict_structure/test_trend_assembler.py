from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
)
from chanlun.core.strict_structure.models import (
    SourceKind,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
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
    confirmed_at = (
        None
        if state is TrendState.FORMING
        else center.completed_at or center.established_at
    )
    return TrendType(
        trend_id=f"trend-{state.value}",
        structural_level=center.structural_level,
        price_basis_revision=center.price_basis_revision,
        kind=TrendKind.CONSOLIDATION,
        direction=(
            "up" if values[-1].end_tick > values[0].start_tick else "down"
        ),
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
