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
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
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


def test_fewer_than_five_physical_roles_do_not_expose_structure_level() -> None:
    result = StrictRecursiveEngine(max_levels=8).calculate(valid_five_up_exit()[:4])

    assert result.levels == ()


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
    upgrade = collect_recursive_upgrade_evidence(result)
    assert len(upgrade) == 1
    assert upgrade[0].kind is UpgradeEvidenceKind.CENTER_EXPANSION
    assert upgrade[0].source_center_ids == (first.center_id, second.center_id)


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
