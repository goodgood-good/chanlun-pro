from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
)
from chanlun.core.strict_structure.divergence import collect_strict_divergences
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
    SourceKind,
    TrendKind,
)
from chanlun.core.strict_structure.strength import (
    StrengthSnapshot,
    completed_center_departure_leg,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import (
    ongoing_center,
    structure_for,
    unit,
)
from tests.core.strict_structure.test_first_class_points import UP_VALUES


def completed_consolidation_fixture(level=0):
    value = ongoing_center(structural_level=level)
    earlier = value.pending_leave_unit
    assert earlier is not None
    entered = unit(5, "down", 130, 110, structural_level=level)
    value, _ = advance_center(value, entered)
    later = unit(6, "up", 110, 135, structural_level=level)
    value, watch = advance_center(value, later)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    outside_return = unit(7, "down", 135, 120, structural_level=level)
    value, _ = advance_center(value, outside_return)
    assert value.state is CenterState.COMPLETED
    return structure_for(value), earlier, later


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
    center_result = calculate_centers(values, level, source_kind)
    strength = FixedStrength(
        {
            "u-12": (10.0, 5.0, 4.0),
            "u-18": (
                (5.0, 2.0, 2.0)
                if decayed
                else (12.0, 6.0, 5.0)
            ),
        }
    )
    assembly = assemble_trend_types(
        center_result.centers,
        values,
        level,
        strength=strength,
    )
    trend = max(
        (
            item
            for item in assembly.completed_trends
            if item.kind is TrendKind.TREND
        ),
        key=lambda item: len(item.constituent_units),
    )
    earlier = completed_center_departure_leg(trend.centers[-2], values)
    later = completed_center_departure_leg(trend.centers[-1], values)
    assert earlier is not None and later is not None
    return (
        structure_for(*trend.centers, completed_trends=(trend,)),
        earlier,
        later,
    )


class FixedStrength:
    def __init__(self, values):
        self.values = values

    def snapshot(self, item):
        key = (
            item.child_ids[-1]
            if item.child_ids and item.child_ids[-1] in self.values
            else item.unit_id
        )
        area, peak, dif = self.values[key]
        return StrengthSnapshot(
            unit_id=item.unit_id,
            direction=item.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd_native",
            available_at=item.available_at,
        )


def strength_pair(earlier, later, *, decayed):
    return FixedStrength(
        {
            earlier.unit_id: (10.0, 5.0, 4.0),
            later.unit_id: (
                (5.0, 2.0, 2.0)
                if decayed
                else (12.0, 6.0, 5.0)
            ),
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
