from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
)
from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterEventKind,
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
    TrendKind,
)
from chanlun.core.strict_structure.recursive_engine import (
    calculate_level_with_divergence_boundaries,
)
from chanlun.core.strict_structure.strength import (
    StrengthSnapshot,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    ongoing_center,
    structure_for,
    unit,
)
from tests.core.strict_structure.test_first_class_points import UP_VALUES
from tests.core.strict_structure.test_trend_assembler import three_center_fixture


RECURSIVE_TREND_VALUES = UP_VALUES[:-6] + (
    ("down", 155, 138),
    ("up", 138, 145),
    ("down", 145, 138),
    ("up", 138, 170),
    ("down", 170, 150),
    ("up", 150, 175),
)


def completed_consolidation_fixture(level=0):
    value = ongoing_center(structural_level=level)
    assert value.pending_leave_unit is not None
    entered = unit(5, "down", 130, 110, structural_level=level)
    value, _ = advance_center(value, entered)
    later = unit(6, "up", 110, 135, structural_level=level)
    value, watch = advance_center(value, later)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    outside_return = unit(7, "down", 135, 120, structural_level=level)
    value, _ = advance_center(value, outside_return)
    assert value.state is CenterState.COMPLETED
    return structure_for(value), value.entry_unit, later


def completed_trend_fixture(level=1, *, decayed=True):
    source_kind = SourceKind.TREND_TYPE
    values = tuple(
        unit(
            index,
            direction,
            start,
            end,
            structural_level=level,
            source_kind=source_kind,
        )
        for index, (direction, start, end) in enumerate(RECURSIVE_TREND_VALUES)
    )[7:]
    strength = FixedStrength(
        {
            ("u-8", "u-9", "u-10"): (10.0, 5.0, 4.0),
            ("u-16", "u-17", "u-18"): (
                (5.0, 2.0, 2.0) if decayed else (12.0, 6.0, 5.0)
            ),
        }
    )
    center_result, assembly = calculate_level_with_divergence_boundaries(
        values,
        level,
        source_kind,
        strength=strength,
    )
    trend = max(
        (item for item in assembly.completed_trends if item.kind is TrendKind.TREND),
        key=lambda item: len(item.constituent_units),
    )
    divergence = trend.terminal_divergence
    by_id = {item.unit_id: item for item in values}
    earlier = by_id["u-10" if divergence is None else divergence.compare_unit_id]
    later = by_id["u-18" if divergence is None else divergence.signal_unit_id]
    empty_levels = tuple(
        StrictLevelResult(
            structural_level=empty_level,
            units=(),
            center_result=CenterLevelResult(
                structural_level=empty_level,
                price_basis_revision=TEST_PRICE_BASIS,
                centers=(),
                previews=(),
                events=(),
                locked_unit_count=0,
                replay_from=0,
            ),
            trend_types=(),
            completed_trends=(),
        )
        for empty_level in range(level)
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            *empty_levels,
            StrictLevelResult(
                structural_level=level,
                units=values,
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=assembly.completed_trends,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )
    return (
        structure,
        earlier,
        later,
    )


class FixedStrength:
    def __init__(self, values):
        self.values = values

    def snapshot(self, item):
        key = tuple(item.child_ids) if len(item.child_ids) == 3 else item.unit_id
        area, peak, dif = self.values.get(key, (10.0, 5.0, 4.0))
        return StrengthSnapshot(
            unit_id=item.unit_id,
            direction=item.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd",
            available_at=item.available_at,
        )


def only(values):
    result = tuple(values)
    assert len(result) == 1
    return result[0]


def test_completed_center_without_formal_boundary_does_not_enter_ledger():
    structure, _earlier, _later = completed_consolidation_fixture(level=0)

    assert collect_formal_divergence_ledger(structure) == ()


def test_consolidation_uses_matching_three_unit_entry_and_departure_legs():
    values = three_center_fixture()[0][:11]
    scanned = calculate_centers(values, 0, SourceKind.SEGMENT)
    center = scanned.centers[1]
    provider = FixedStrength(
        {
            ("u-2", "u-3", "u-4"): (10.0, 5.0, 4.0),
            ("u-8", "u-9", "u-10"): (6.0, 3.0, 2.0),
        }
    )
    assembly = assemble_trend_types(
        (center,),
        values,
        0,
        strength=provider,
        group_start_unit_id="u-2",
    )
    closed_center = assembly.current_trends[0].centers[0]
    center_result = replace(
        scanned,
        centers=(closed_center,),
        events=(),
    )
    completed = tuple(
        trend
        for trend in assembly.completed_trends
        if trend.terminal_divergence is not None
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=values,
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=completed,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )

    item = only(collect_formal_divergence_ledger(structure))

    assert item.kind == "consolidation"
    assert item.comparison_width == 3
    assert item.compare_leg_unit_ids == ("u-2", "u-3", "u-4")
    assert item.signal_leg_unit_ids == ("u-8", "u-9", "u-10")


def test_completed_trend_emits_trend_divergence_at_recursive_level():
    structure, _earlier, _later = completed_trend_fixture(level=2)
    item = only(collect_formal_divergence_ledger(structure))
    assert item.kind == "trend"
    assert item.structural_level == 2


def test_non_divergent_comparison_is_not_formal_evidence():
    structure, _earlier, _later = completed_trend_fixture(level=1, decayed=False)
    assert collect_formal_divergence_ledger(structure) == ()


def test_same_direction_subtrend_divergence_is_not_a_formal_boundary() -> None:
    values = tuple(
        unit(
            index,
            direction,
            start,
            end,
            structural_level=1,
            source_kind=SourceKind.TREND_TYPE,
        )
        for index, (direction, start, end) in enumerate(RECURSIVE_TREND_VALUES)
    )
    strength = FixedStrength(
        {
            ("u-8", "u-9", "u-10"): (10.0, 5.0, 4.0),
            ("u-16", "u-17", "u-18"): (5.0, 2.0, 2.0),
        }
    )

    _center_result, assembly = calculate_level_with_divergence_boundaries(
        values,
        1,
        SourceKind.TREND_TYPE,
        strength=strength,
    )

    assert assembly.decomposition_boundaries == ()
    assert tuple(trend.direction for trend in assembly.current_trends) == ("up",)
    assert assembly.current_trends[0].kind is TrendKind.CONSOLIDATION
    assert all(
        trend.terminal_divergence is None
        for trend in (*assembly.current_trends, *assembly.completed_trends)
    )


def test_legacy_oscillatory_metadata_cannot_admit_non_alternating_history() -> None:
    """Old oscillatory metadata cannot bypass the directional source contract."""

    geometry = (
        ("up", 30335, 32810, 29638, 32810),
        ("down", 32810, 23651, 23651, 41261),
        ("up", 23651, 25347, 22388, 25347),
        ("up", 25347, 28055, 24003, 28055),
        ("down", 28055, 24567, 24567, 28055),
        ("down", 24567, 21776, 21776, 26224),
        ("up", 21776, 24100, 19885, 24280),
        ("up", 24100, 28930, 22350, 28930),
    )
    values = tuple(
        replace(
            unit(
                index,
                direction,
                start_tick,
                end_tick,
                locked=index < 7,
                source_kind=SourceKind.TREND_TYPE,
                structural_level=1,
            ),
            low_tick=low_tick,
            high_tick=high_tick,
        )
        for index, (
            direction,
            start_tick,
            end_tick,
            low_tick,
            high_tick,
        ) in enumerate(geometry)
    )
    strength = FixedStrength(
        {
            "u-1": (371.05, -6.59, -6.85),
            "u-5": (103.48, -5.29, -5.59),
        }
    )
    oscillatory_ids = frozenset({"u-0", "u-2", "u-3", "u-4", "u-5", "u-6", "u-7"})

    with pytest.raises(ValueError, match="unit directions must alternate"):
        calculate_level_with_divergence_boundaries(
            values,
            1,
            SourceKind.TREND_TYPE,
            oscillatory_ids,
            strength=strength,
        )
