from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
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
        unit(4, "up", 105, 130),
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 160),
        unit(7, "down", 160, 150),
        unit(8, "up", 150, 180),
        unit(9, "down", 180, 160),
        unit(10, "up", 160, 175),
        unit(11, "down", 175, 165),
        unit(12, "up", 165, 190),
        unit(13, "down", 190, 180),
        unit(14, "up", 180, 195),
        unit(15, "down", 195, 175),
        unit(16, "up", 175, 185),
        unit(17, "down", 185, 160),
        unit(18, "up", 160, 170),
    )
    first = establish_center(values[0:5], 0, SourceKind.SEGMENT)
    second = establish_center(values[8:13], 0, SourceKind.SEGMENT)
    third = establish_center(values[13:18], 0, SourceKind.SEGMENT)
    assert first is not None and second is not None and third is not None
    first, _ = advance_center(first, values[5])
    second, _ = advance_center(second, values[13])
    third, _ = advance_center(third, values[18])
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
    assert trend.constituent_units == first.body_units == values[:5]
    assert trend.terminal_unit is first.completion_leave_unit
    assert first.completion_return_unit not in trend.constituent_units


def test_two_separated_centers_form_complete_uptrend_with_internal_return():
    values, first, second, _third = three_center_fixture()
    result = assemble_trend_types((first, second), values[:14], 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.COMPLETE
    assert trend.kind is TrendKind.TREND
    assert trend.direction == "up"
    assert trend.centers == (first, second)
    assert trend.constituent_units == values[:13]
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
    assert locked.constituent_units == values[:13]
    assert locked.terminal_unit is second.completion_leave_unit
    assert tail.state is TrendState.COMPLETE
    assert tail.centers == (third,)
    assert tail.constituent_units == values[13:18]
    assert tail.constituent_units[0] is second.completion_return_unit
    assert third.completion_return_unit not in tail.constituent_units


def test_non_center_bridge_units_are_preserved_exactly_once():
    values, first, second, _third = three_center_fixture()
    trend = assemble_trend_types((first, second), values[:14], 0).current_trends[0]
    bridge = values[6:8]
    assert all(item in trend.constituent_units for item in bridge)
    assert len({item.unit_id for item in trend.constituent_units}) == len(
        trend.constituent_units
    )


def test_ongoing_center_produces_forming_trend_without_confirmation():
    center = ongoing_center()
    result = assemble_trend_types((center,), center.body_units, 0)
    trend = result.current_trends[0]
    assert trend.state is TrendState.FORMING
    assert trend.confirmed_at is None
    assert result.completed_trends == ()


def test_center_reference_validation_rejects_missing_or_changed_evidence():
    values, first, _second, _third = three_center_fixture()
    with pytest.raises(ValueError, match="completion return"):
        assemble_trend_types((first,), values[:5], 0)
    changed = values[:4] + (replace(values[4], unit_id="changed"),) + values[5:6]
    with pytest.raises(ValueError, match="missing unit"):
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
