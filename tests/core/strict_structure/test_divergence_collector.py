from dataclasses import replace

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.divergence import collect_strict_divergences
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
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    ongoing_center,
    structure_for,
    unit,
    valid_five_up_exit,
)
from tests.core.strict_structure.test_first_class_points import UP_VALUES


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


def completed_trend_fixture(level=0, *, decayed=True):
    source_kind = SourceKind.SEGMENT if level == 0 else SourceKind.TREND_TYPE
    values = tuple(
        unit(
            index,
            direction,
            start,
            end,
            structural_level=level,
            source_kind=source_kind,
        )
        for index, (direction, start, end) in enumerate(UP_VALUES)
    )
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
            source="macd_htf",
            available_at=item.available_at,
        )


def strength_pair(earlier, later, *, decayed):
    return FixedStrength(
        {
            earlier.unit_id: (10.0, 5.0, 4.0),
            later.unit_id: ((5.0, 2.0, 2.0) if decayed else (12.0, 6.0, 5.0)),
        }
    )


def only(values):
    result = tuple(values)
    assert len(result) == 1
    return result[0]


def test_completed_consolidation_emits_level_scoped_divergence_without_point_dependency():
    structure, earlier, later = completed_consolidation_fixture(level=0)
    values = collect_strict_divergences(
        structure,
        strength_pair(earlier, later, decayed=True),
    )
    item = only(values)
    assert item.kind == "consolidation"
    assert item.structural_level == 0
    assert item.compare_unit_id == earlier.unit_id
    assert item.signal_unit_id == later.unit_id
    assert item.confirmed_at == later.confirmed_at
    assert item.divergence_id


def test_consolidation_uses_matching_three_unit_entry_and_departure_legs():
    prefix = (
        unit(0, "up", 80, 100),
        unit(1, "down", 100, 90),
    )
    seed = valid_five_up_exit(2)
    center = establish_center(seed, 0, SourceKind.SEGMENT)
    assert center is not None
    return_unit = unit(7, "down", 130, 120)
    center, _event = advance_center(center, return_unit)
    terminal = unit(8, "up", 120, 140)
    values = (*prefix, *seed, return_unit, terminal)
    structure = structure_for(center)
    level = structure.levels[0]
    structure = replace(
        structure,
        levels=(
            replace(
                level,
                units=values,
                center_result=replace(
                    level.center_result,
                    locked_unit_count=len(values),
                ),
            ),
        ),
    )
    provider = FixedStrength(
        {
            ("u-0", "u-1", "u-2"): (10.0, 5.0, 4.0),
            ("u-6", "u-7", "u-8"): (6.0, 3.0, 2.0),
        }
    )

    item = only(collect_strict_divergences(structure, provider))

    assert item.kind == "consolidation"
    assert item.comparison_width == 3
    assert item.compare_leg_unit_ids == ("u-0", "u-1", "u-2")
    assert item.signal_leg_unit_ids == ("u-6", "u-7", "u-8")


def test_completed_trend_emits_trend_divergence_at_recursive_level():
    structure, earlier, later = completed_trend_fixture(level=2)
    item = only(
        collect_strict_divergences(
            structure,
            strength_pair(earlier, later, decayed=True),
        )
    )
    assert item.kind == "trend"
    assert item.structural_level == 2


def test_non_divergent_comparison_is_not_formal_evidence():
    structure, earlier, later = completed_trend_fixture(level=1, decayed=False)
    assert (
        collect_strict_divergences(
            structure,
            strength_pair(earlier, later, decayed=False),
        )
        == ()
    )
