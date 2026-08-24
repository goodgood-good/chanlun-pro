from dataclasses import replace
from decimal import Decimal

import pytest

from chanlun.core.strict_structure.divergence import (
    center_consolidation_comparison_legs,
)
from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    TrendState,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
from tests.core.strict_structure.helpers import unit, valid_up_center_lifecycle


def _two_completed_centers():
    return valid_up_center_lifecycle() + (
        unit(5, "up", 120, 140),
        unit(6, "down", 140, 125),
        unit(7, "up", 125, 145),
        unit(8, "down", 145, 135),
    )


def _three_completed_centers():
    return _two_completed_centers() + (
        unit(9, "up", 135, 160),
        unit(10, "down", 160, 140),
        unit(11, "up", 140, 170),
        unit(12, "down", 170, 155),
    )


def _superseded_then_completed_center():
    return (
        unit(0, "up", 100, 130),
        unit(1, "down", 130, 110),
        unit(2, "up", 110, 150),
        unit(3, "down", 150, 140),
        unit(4, "up", 140, 160),
        unit(5, "down", 160, 145),
        unit(6, "up", 145, 170),
        unit(7, "down", 170, 155),
    )


def _sliding_superseded_center():
    return (
        unit(0, "up", 100, 130),
        unit(1, "down", 130, 110),
        unit(2, "up", 110, 150),
        unit(3, "down", 150, 140),
        unit(4, "up", 140, 200),
        unit(5, "down", 200, 180),
        unit(6, "up", 180, 190),
    )


def test_three_locked_segments_form_level_zero_with_schema() -> None:
    result = StrictRecursiveEngine(max_levels=4).calculate(
        valid_up_center_lifecycle()[:3]
    )

    assert result.schema == "chanlun-structure"
    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.structural_level == 0
    assert len(level.center_result.centers) == 1
    assert level.center_result.centers[0].entry_unit is None
    assert all(item.source_kind is SourceKind.SEGMENT for item in level.units)


def test_fewer_than_three_units_do_not_expose_structure_level() -> None:
    result = StrictRecursiveEngine(max_levels=8).calculate(
        valid_up_center_lifecycle()[:2]
    )

    assert result.levels == ()


def test_forming_trend_does_not_recurse_as_locked_higher_level_input() -> None:
    result = StrictRecursiveEngine(max_levels=8).calculate(
        valid_up_center_lifecycle()[:4]
    )

    assert len(result.levels) == 1
    level = result.levels[0]
    assert len(level.center_result.centers) == 1
    assert level.center_result.centers[0].pending_leave_unit is level.units[3]
    assert not any(trend.locked for trend in level.trend_types)


def test_unlocked_fourth_segment_is_preview_over_formal_three_unit_center() -> None:
    values = valid_up_center_lifecycle()
    values = values[:3] + (
        replace(values[3], locked=False, confirmed_at=None),
    )

    result = StrictRecursiveEngine(max_levels=8).calculate(values)

    assert len(result.levels) == 1
    level = result.levels[0]
    assert level.center_result.locked_unit_count == 3
    assert len(level.center_result.centers) == 1
    assert level.center_result.centers[0].initial_units == values[:3]
    assert level.center_result.previews


def test_recursion_rejects_mixed_price_basis() -> None:
    values = valid_up_center_lifecycle()
    mixed = values[:-1] + (
        replace(values[-1], price_basis_revision="another-basis"),
    )

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
    values = _three_completed_centers()

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

    result = StrictRecursiveEngine(max_levels=3).calculate(values[:6])
    level = result.levels[0]

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
    assert StrictSignalEngine(
        structure=result,
        price_quantum=Decimal("0.01"),
    ).third_class_points() == ()


def test_supersession_bridge_keeps_recursive_trend_units_connected() -> None:
    result = StrictRecursiveEngine(max_levels=3).calculate(
        _sliding_superseded_center()
    )
    level = result.levels[0]
    first, successor = level.center_result.centers
    left, right = level.trend_types

    assert first.supersession_bridge_units == (level.units[3],)
    assert successor.entry_unit is level.units[3]
    assert left.constituent_units == level.units[:4]
    assert right.constituent_units == level.units[4:7]
    assert left.end_tick == right.start_tick


def test_real_fourth_and_fifth_units_complete_only_the_successor_center() -> None:
    values = _superseded_then_completed_center()

    result = StrictRecursiveEngine(max_levels=3).calculate(values)
    level = result.levels[0]

    first, second = level.center_result.centers
    assert first.state is CenterState.SUPERSEDED
    assert first.completion_leave_unit is None
    assert first.completion_return_unit is None
    assert second.state is CenterState.COMPLETED
    assert second.completion_leave_unit is values[6]
    assert second.completion_return_unit is values[7]
    assert [trend.state for trend in level.trend_types] == [
        TrendState.LOCKED,
        TrendState.COMPLETE,
    ]
    upgrade = collect_recursive_upgrade_evidence(result)
    assert len(upgrade) == 1
    assert upgrade[0].kind is UpgradeEvidenceKind.CENTER_EXPANSION
    assert upgrade[0].source_center_ids == (first.center_id, second.center_id)


def test_completed_centers_are_owned_by_trends_without_entry_role_assumption() -> None:
    result = StrictRecursiveEngine(max_levels=1).calculate(
        _two_completed_centers()
    )
    level = result.levels[0]

    assert len(level.center_result.centers) == 2
    assert all(
        center.state is CenterState.COMPLETED
        for center in level.center_result.centers
    )
    owned_centers = tuple(
        center for trend in level.trend_types for center in trend.centers
    )
    assert owned_centers == level.center_result.centers
    assert level.center_result.centers[0].entry_unit is None
