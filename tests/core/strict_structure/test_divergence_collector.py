from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.divergence import collect_strict_divergences
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
    SourceKind,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.strength import StrengthSnapshot
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    ongoing_center,
    structure_for,
    unit,
)


def completed_consolidation_fixture(level=0):
    value = ongoing_center(structural_level=level)
    earlier = value.initial_exit_unit
    entered = unit(5, "down", 130, 110, structural_level=level)
    value, _ = advance_center(value, entered)
    later = unit(6, "up", 110, 135, structural_level=level)
    value, watch = advance_center(value, later)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP
    outside_return = unit(7, "down", 135, 120, structural_level=level)
    value, _ = advance_center(value, outside_return)
    assert value.state is CenterState.COMPLETED
    return structure_for(value), earlier, later


def completed_trend_fixture(level=0):
    values = (
        unit(0, "up", 90, 120, structural_level=level),
        unit(1, "down", 120, 100, structural_level=level),
        unit(2, "up", 100, 115, structural_level=level),
        unit(3, "down", 115, 105, structural_level=level),
        unit(4, "up", 105, 130, structural_level=level),
        unit(5, "down", 130, 120, structural_level=level),
        unit(6, "up", 120, 140, structural_level=level),
        unit(7, "down", 140, 135, structural_level=level),
        unit(8, "up", 135, 160, structural_level=level),
        unit(9, "down", 160, 140, structural_level=level),
        unit(10, "up", 140, 155, structural_level=level),
        unit(11, "down", 155, 145, structural_level=level),
        unit(12, "up", 145, 170, structural_level=level),
        unit(13, "down", 170, 160, structural_level=level),
    )
    first = establish_center(values[0:5], level, SourceKind.SEGMENT)
    second = establish_center(values[8:13], level, SourceKind.SEGMENT)
    assert first is not None and second is not None
    first, _ = advance_center(first, values[5])
    second, _ = advance_center(second, values[13])
    owned = values[:13]
    trend = TrendType(
        trend_id=f"trend-{level}",
        structural_level=level,
        price_basis_revision=TEST_PRICE_BASIS,
        kind=TrendKind.TREND,
        direction="up",
        state=TrendState.COMPLETE,
        centers=(first, second),
        constituent_units=owned,
        start_tick=owned[0].start_tick,
        end_tick=owned[-1].end_tick,
        low_tick=min(item.low_tick for item in owned),
        high_tick=max(item.high_tick for item in owned),
        market_start=owned[0].market_start,
        market_end=owned[-1].market_end,
        confirmed_at=second.completed_at,
        available_at=second.available_at,
    )
    return (
        structure_for(first, second, completed_trends=(trend,)),
        values[6],
        second.completion_leave_unit,
    )


class FixedStrength:
    def __init__(self, values):
        self.values = values

    def snapshot(self, item):
        area, peak, dif = self.values[item.unit_id]
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
    structure, earlier, later = completed_trend_fixture(level=1)
    assert (
        collect_strict_divergences(
            structure,
            strength_pair(earlier, later, decayed=False),
        )
        == ()
    )
