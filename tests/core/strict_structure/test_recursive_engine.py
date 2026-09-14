from dataclasses import replace
from decimal import Decimal
import random

import pytest

from chanlun.core.strict_structure.divergence import (
    center_consolidation_comparison_legs,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
    TrendState,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import (
    unit,
    valid_five_up_exit,
)


def _two_completed_centers():
    return valid_five_up_exit() + (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 140),
        unit(7, "down", 140, 125),
        unit(8, "up", 125, 135),
        unit(9, "down", 135, 132),
    )


@pytest.mark.parametrize("mirrored", [False, True])
def test_physical_supersession_bridge_belongs_to_successor_without_losing_center_body(mirrored):
    # The internal pen low on u-19 revisits the core although its endpoint
    # stays above it. A later successor proves the boundary; u-20 is its
    # opposite entry bridge, not a missing part of the preceding center body.
    prices = [292, 377, 286, 305, 293, 324, 301, 326, 318, 343, 303, 324,
              312, 323, 267, 288, 270, 284, 276, 297, 285, 294, 282, 326, 288, 320]
    values = tuple(unit(i, "up" if b > a else "down", a, b)
                   for i, (a, b) in enumerate(zip(prices, prices[1:])))
    values = (*values[:19], replace(values[19], low_tick=281), *values[20:])
    if mirrored:
        values = tuple(replace(u, start_tick=700-u.start_tick, end_tick=700-u.end_tick,
                               low_tick=700-u.high_tick, high_tick=700-u.low_tick,
                               direction="down" if u.direction == "up" else "up") for u in values)
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    superseded = next(c for c in result.centers if c.state is CenterState.SUPERSEDED)
    assert superseded.supersession_bridge_units == (values[20],)
    assembly = assemble_trend_types(result.centers, values, 0)
    predecessor = next(t for t in assembly.current_trends if superseded in t.centers)
    successor = assembly.current_trends[assembly.current_trends.index(predecessor) + 1]
    assert predecessor.constituent_units[-1] == values[19]
    assert successor.constituent_units[0] == values[20]
    assert all(u in predecessor.constituent_units
               for u in (*superseded.body_units, *superseded.failed_departure_units))
    ids = [u.unit_id for t in assembly.current_trends for u in t.constituent_units]
    assert len(ids) == len(set(ids))
    # Only the single proven bridge may be external; real center-body units
    # remain mandatory even after a successor has appeared.
    with pytest.raises(ValueError, match="every center body and bridge"):
        replace(predecessor, constituent_units=predecessor.constituent_units[:-2],
                end_tick=predecessor.constituent_units[-3].end_tick)


def _recursive_depth_fixture():
    generator = random.Random(1)
    price = 100
    direction = "up"
    values = []
    for index in range(103):
        step = generator.randint(3, 45)
        end = price + step if direction == "up" else max(1, price - step)
        values.append(unit(index, direction, price, end))
        price = end
        direction = "down" if direction == "up" else "up"
    return tuple(values)


def _superseded_then_completed_center():
    return tuple(
        replace(item, source_kind=SourceKind.TREND_TYPE, structural_level=1)
        for item in (
            unit(0, "up", 100, 130),
            unit(1, "down", 130, 110),
            unit(2, "up", 110, 150),
            unit(3, "down", 150, 140),
            unit(4, "up", 140, 160),
            unit(5, "down", 160, 145),
            unit(6, "up", 145, 170),
            unit(7, "down", 170, 155),
        )
    )


def _sliding_superseded_center():
    return tuple(
        replace(item, source_kind=SourceKind.TREND_TYPE, structural_level=1)
        for item in (
            unit(0, "up", 100, 130),
            unit(1, "down", 130, 110),
            unit(2, "up", 110, 150),
            unit(3, "down", 150, 140),
            unit(4, "up", 140, 200),
            unit(5, "down", 200, 180),
            unit(6, "up", 180, 190),
        )
    )


def _recursive_trend_structure(values):
    values = tuple(values)
    centers = calculate_centers(values, 1, SourceKind.TREND_TYPE)
    assembly = assemble_trend_types(centers.centers, values, 1)
    empty = StrictLevelResult(
        structural_level=0,
        units=(),
        center_result=CenterLevelResult(
            structural_level=0,
            price_basis_revision=values[0].price_basis_revision,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        ),
        trend_types=(),
        completed_trends=(),
    )
    recursive = StrictLevelResult(
        structural_level=1,
        units=values,
        center_result=centers,
        trend_types=assembly.current_trends,
        completed_trends=assembly.completed_trends,
    )
    return StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=values[0].price_basis_revision,
        levels=(empty, recursive),
    )


def test_five_locked_physical_roles_form_level_zero_with_schema() -> None:
    result = StrictRecursiveEngine(max_levels=4).calculate(valid_five_up_exit())

    assert result.schema == "chanlun-structure"
    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.structural_level == 0
    assert len(level.center_result.centers) == 1
    assert level.center_result.centers[0].entry_unit is level.units[0]
    assert level.center_result.centers[0].establishment_leave_unit is level.units[4]
    assert all(item.source_kind is SourceKind.SEGMENT for item in level.units)


def test_four_physical_roles_expose_forming_core_without_formal_center() -> None:
    result = StrictRecursiveEngine(max_levels=8).calculate(valid_five_up_exit()[:4])

    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.center_result.centers == ()
    assert len(level.center_result.previews) == 1
    assert level.center_result.previews[0].establishment_leave_unit_id is None
    assert level.trend_types == ()
    assert level.completed_trends == ()


def test_forming_trend_does_not_recurse_as_locked_higher_level_input() -> None:
    result = StrictRecursiveEngine(max_levels=8).calculate(valid_five_up_exit())

    assert len(result.levels) == 1
    level = result.levels[0]
    assert len(level.center_result.centers) == 1
    assert level.center_result.centers[0].pending_leave_unit is level.units[4]
    assert not any(trend.locked for trend in level.trend_types)


def test_unlocked_fifth_role_is_preview_without_a_formal_physical_center() -> None:
    values = valid_five_up_exit()
    values = values[:4] + (replace(values[4], locked=False, confirmed_at=None),)

    result = StrictRecursiveEngine(max_levels=8).calculate(values)

    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.center_result.locked_unit_count == 4
    assert level.center_result.centers == ()
    assert level.center_result.previews


def test_recursion_rejects_mixed_price_basis() -> None:
    values = valid_five_up_exit()
    mixed = values[:-1] + (replace(values[-1], price_basis_revision="another-basis"),)

    with pytest.raises(ValueError, match="cannot cross price basis"):
        StrictRecursiveEngine().calculate(mixed)


def test_empty_recursion_requires_explicit_basis_and_has_no_levels() -> None:
    with pytest.raises(ValueError, match="empty strict recursion requires price basis"):
        StrictRecursiveEngine().calculate(())

    result = StrictRecursiveEngine().calculate(
        (),
        price_basis_revision="test-raw",
    )
    assert result.schema == "chanlun-structure"
    assert result.levels == ()
    assert result.price_basis_revision == "test-raw"


def test_recursive_engine_rejects_invalid_level_limit() -> None:
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_levels must be >= 1"):
            StrictRecursiveEngine(max_levels=invalid)


def test_max_levels_is_a_hard_structural_depth_cap() -> None:
    values = _recursive_depth_fixture()

    capped = StrictRecursiveEngine(max_levels=1).calculate(values)
    recursive = StrictRecursiveEngine(max_levels=8).calculate(values)

    assert len(capped.levels) == 1
    assert len(recursive.levels) == 2
    assert recursive.levels[1].structural_level == 1
    assert all(
        unit_value.source_kind is SourceKind.TREND_TYPE
        for unit_value in recursive.levels[1].units
    )
    assert all(unit_value.child_ids for unit_value in recursive.levels[1].units)


def test_superseded_center_closes_trend_without_third_class_evidence() -> None:
    values = _superseded_then_completed_center()

    result = _recursive_trend_structure(values[:6])
    level = result.levels[1]

    first, second = level.center_result.centers
    assert first.state is CenterState.SUPERSEDED
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert second.state is CenterState.ONGOING
    assert [trend.state for trend in level.trend_types] == [
        TrendState.COMPLETE,
        TrendState.FORMING,
    ]
    assert level.trend_types[0].centers == (first,)
    assert level.trend_types[1].centers == (second,)
    assert center_consolidation_comparison_legs(first, level.units) is None
    assert (
        StrictSignalEngine(
            structure=result,
            price_quantum=Decimal("0.01"),
        ).third_class_points()
        == ()
    )


def test_supersession_bridge_keeps_recursive_trend_units_connected() -> None:
    result = _recursive_trend_structure(_sliding_superseded_center())
    level = result.levels[1]
    first, successor = level.center_result.centers
    assert len(level.trend_types) == 1
    (trend,) = level.trend_types

    assert first.supersession_bridge_units == (level.units[3],)
    assert successor.entry_unit is level.units[3]
    assert trend.state is TrendState.FORMING
    assert trend.centers == (first, successor)
    assert trend.constituent_units == level.units


def test_real_fourth_and_fifth_units_complete_only_the_successor_center() -> None:
    values = _superseded_then_completed_center()

    result = _recursive_trend_structure(values)
    level = result.levels[1]

    first, second = level.center_result.centers
    assert first.state is CenterState.SUPERSEDED
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert second.state is CenterState.COMPLETED
    assert second.completion_leave_unit is values[6]
    assert second.completion_return_unit is values[7]
    assert len(level.trend_types) == 1
    (trend,) = level.trend_types
    assert trend.state is TrendState.COMPLETE
    assert trend.centers == (first, second)
    assert trend.constituent_units == values[:7]
    assert level.pending_movements[0].constituent_units == values[7:]


def test_completed_physical_centers_with_full_roles_are_owned_by_trends() -> None:
    result = StrictRecursiveEngine(max_levels=1).calculate(_two_completed_centers())
    level = result.levels[0]

    assert len(level.center_result.centers) == 2
    assert all(
        center.state is CenterState.COMPLETED for center in level.center_result.centers
    )
    owned_centers = tuple(
        center for trend in level.trend_types for center in trend.centers
    )
    assert owned_centers == level.center_result.centers
    assert all(
        center.entry_unit is not None and center.establishment_leave_unit is not None
        for center in level.center_result.centers
    )
