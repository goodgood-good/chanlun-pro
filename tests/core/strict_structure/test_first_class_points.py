from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    StrictLevelResult,
    StrictStructureResult,
    SourceKind,
    TrendKind,
    TrendState,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine, _comparison_unit
from chanlun.core.strict_structure.strength import StrengthSnapshot
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


UP_VALUES = (
    ("up", 90, 120),
    ("down", 120, 100),
    ("up", 100, 115),
    ("down", 115, 105),
    ("up", 105, 130),
    ("down", 130, 120),
    ("up", 120, 160),
    ("down", 160, 150),
    ("up", 150, 180),
    ("down", 180, 160),
    ("up", 160, 175),
    ("down", 175, 165),
    ("up", 165, 190),
    ("down", 190, 180),
    ("up", 180, 185),
)

EXTENDED_UP_VALUES = (
    *UP_VALUES[:14],
    ("up", 180, 260),
    ("down", 260, 200),
    ("up", 200, 240),
    ("down", 240, 220),
    ("up", 220, 235),
    ("down", 235, 225),
    ("up", 225, 280),
    ("down", 280, 240),
)

NO_NEW_HIGH_VALUES = (
    *UP_VALUES[:6],
    ("up", 120, 200),
    ("down", 200, 150),
    *UP_VALUES[8:],
)


def make_units(values, direction):
    if direction == "up":
        return tuple(
            unit(index, item_direction, start, end)
            for index, (item_direction, start, end) in enumerate(values)
        )
    return tuple(
        unit(
            index,
            "up" if item_direction == "down" else "down",
            300 - start,
            300 - end,
        )
        for index, (item_direction, start, end) in enumerate(values)
    )


def structure_from_values(values=UP_VALUES, *, direction="up"):
    units = make_units(values, direction)
    center_result = calculate_centers(units, 0, SourceKind.SEGMENT)
    assembly = assemble_trend_types(center_result.centers, units, 0)
    level = StrictLevelResult(
        structural_level=0,
        units=units,
        center_result=center_result,
        trend_types=assembly.current_trends,
        completed_trends=assembly.completed_trends,
    )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(level,),
    )
    return structure, assembly


class StrengthTable:
    def __init__(self, values):
        self.values = values

    def snapshot(self, value):
        area, peak, dif = self.values[value.unit_id]
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd_htf",
            available_at=value.available_at,
        )


def divergent_strength(direction, *, extended=False):
    if direction == "up":
        values = {"u-6": (100, 5, 2), "u-10": (80, 3, 1)}
        if extended:
            values.update({"u-14": (100, 5, 2), "u-20": (60, 2, 0.5)})
    else:
        values = {"u-6": (100, -5, -2), "u-10": (80, -3, -1)}
        if extended:
            values.update({"u-14": (100, -5, -2), "u-20": (60, -2, -0.5)})
    return StrengthTable(values)


def engine_for(structure, strength):
    return StrictSignalEngine(
        structure=structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )


def only_point(points):
    values = tuple(points)
    assert len(values) == 1
    return values[0]


def target_trend(assembly):
    matches = tuple(
        trend
        for trend in assembly.completed_trends
        if trend.kind is TrendKind.TREND and len(trend.centers) == 2
    )
    assert len(matches) == 1
    return matches[0]


def test_down_trend_terminal_divergence_emits_one_buy():
    structure, assembly = structure_from_values(direction="down")
    trend = target_trend(assembly)
    point = only_point(
        engine_for(structure, divergent_strength("down")).first_class_points()
    )
    assert point.point_type == "1buy"
    assert point.divergence.kind == "trend"
    assert point.anchor_unit_id == trend.terminal_unit.unit_id
    assert point.invalidation_tick == trend.terminal_unit.low_tick


def test_up_trend_terminal_divergence_emits_one_sell():
    structure, assembly = structure_from_values(direction="up")
    trend = target_trend(assembly)
    point = only_point(
        engine_for(structure, divergent_strength("up")).first_class_points()
    )
    assert point.point_type == "1sell"
    assert point.invalidation_tick == trend.terminal_unit.high_tick


def test_single_center_consolidation_does_not_emit_first_class():
    structure, _assembly = structure_from_values(values=UP_VALUES[:5])
    assert engine_for(structure, StrengthTable({})).first_class_points() == ()


def test_smaller_macd_without_new_price_extreme_is_not_divergence():
    structure, assembly = structure_from_values(values=NO_NEW_HIGH_VALUES)
    trend = tuple(
        item for item in assembly.completed_trends if item.kind is TrendKind.TREND
    )[0]
    comparison = _comparison_unit(trend, trend.terminal_unit)
    assert comparison.high_tick > trend.terminal_unit.high_tick
    strength = StrengthTable(
        {
            comparison.unit_id: (100, 5, 2),
            trend.terminal_unit.unit_id: (80, 3, 1),
        }
    )
    assert engine_for(structure, strength).first_class_points() == ()


def test_internal_center_body_unit_is_never_used_as_trend_comparison():
    _structure, assembly = structure_from_values()
    trend = target_trend(assembly)
    last_center = trend.centers[-1]
    body_only_projection = SimpleNamespace(
        centers=(last_center,),
        constituent_units=last_center.body_units,
    )
    assert (
        _comparison_unit(body_only_projection, last_center.completion_leave_unit)
        is None
    )


def test_forming_trend_is_observation_only_not_first_class():
    structure, assembly = structure_from_values()
    trend = target_trend(assembly)
    forming = replace(trend, state=TrendState.FORMING, confirmed_at=None)
    level = replace(
        structure.levels[0],
        trend_types=(forming,),
        completed_trends=(),
    )
    projected = replace(structure, levels=(level,))
    assert engine_for(projected, divergent_strength("up")).first_class_points() == ()


def test_first_class_available_at_uses_trend_completion():
    structure, assembly = structure_from_values(direction="down")
    trend = target_trend(assembly)
    point = only_point(
        engine_for(structure, divergent_strength("down")).first_class_points()
    )
    assert point.available_at == max(
        trend.terminal_unit.available_at,
        trend.centers[-1].available_at,
        trend.available_at,
        point.divergence.available_at,
    )


def test_completed_first_point_survives_later_same_direction_trend_extension():
    structure, assembly = structure_from_values(values=EXTENDED_UP_VALUES)
    snapshots = tuple(
        trend for trend in assembly.completed_trends if trend.kind is TrendKind.TREND
    )
    assert [len(trend.centers) for trend in snapshots] == [2, 3]
    points = engine_for(
        structure,
        divergent_strength("up", extended=True),
    ).first_class_points()
    assert [point.anchor_unit_id for point in points] == ["u-10", "u-20"]
    frozen = points[0]
    assert frozen.available_at == snapshots[0].available_at
    assert frozen.point_id != points[1].point_id
