from dataclasses import replace
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    SourceKind,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


THREE_BUY_VALUES = (
    ("up", 90, 120),
    ("down", 120, 100),
    ("up", 100, 115),
    ("down", 115, 105),
    ("up", 105, 115),
    ("down", 115, 105),
    ("up", 105, 130),
    ("down", 130, 120),
)


def make_units(values):
    return tuple(
        unit(index, direction, start, end)
        for index, (direction, start, end) in enumerate(values)
    )


def structure_for_units(units):
    center_result = calculate_centers(units, 0, SourceKind.SEGMENT)
    assembly = assemble_trend_types(center_result.centers, units, 0)
    level = StrictLevelResult(
        structural_level=0,
        units=units,
        center_result=center_result,
        trend_types=assembly.current_trends,
        completed_trends=assembly.completed_trends,
    )
    return StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(level,),
    )


def projected_structure(values):
    units = list(make_units(values))
    units[-1] = replace(units[-1], locked=False, confirmed_at=None)
    return structure_for_units(tuple(units))


def engine_for(structure):
    return StrictSignalEngine(
        structure=structure,
        price_quantum=Decimal("0.01"),
    )


def only_point(points):
    values = tuple(points)
    assert len(values) == 1
    return values[0]


def test_locked_up_leave_and_unlocked_return_holding_zg_approaches_three_buy():
    structure = projected_structure(THREE_BUY_VALUES)
    tail = structure.levels[0].units[-1]
    point = only_point(engine_for(structure).approaching_points(tail.available_at))
    assert point.point_type == "3buy"
    assert point.anchor_unit_id == tail.unit_id
    assert point.status is StrictPointStatus.APPROACHING


def test_unlocked_return_entering_center_core_is_not_approaching_three_buy():
    values = THREE_BUY_VALUES[:-1] + (("down", 130, 110),)
    structure = projected_structure(values)
    tail = structure.levels[0].units[-1]
    assert engine_for(structure).approaching_points(tail.available_at) == ()


def test_approaching_point_never_enters_formal_third_class_set():
    structure = projected_structure(THREE_BUY_VALUES)
    tail = structure.levels[0].units[-1]
    engine = engine_for(structure)
    approaching = only_point(engine.approaching_points(tail.available_at))
    assert approaching.confirmed_at is None
    assert "terminal_unit_locked" in approaching.missing_conditions
    assert approaching.point_id not in {
        point.point_id for point in engine.third_class_points()
    }


def test_approaching_three_buy_becomes_distinct_confirmed_point_when_tail_locks():
    projected = projected_structure(THREE_BUY_VALUES)
    tail = projected.levels[0].units[-1]
    candidate = only_point(
        engine_for(projected).approaching_points(tail.available_at)
    )

    confirmed_structure = structure_for_units(make_units(THREE_BUY_VALUES))
    confirmed = only_point(engine_for(confirmed_structure).third_class_points())
    assert confirmed.anchor_unit_id == candidate.anchor_unit_id
    assert confirmed.point_id != candidate.point_id
    assert confirmed.status is StrictPointStatus.CONFIRMED


def test_approaching_three_buy_disappears_when_live_return_enters_core():
    projected = projected_structure(THREE_BUY_VALUES)
    first_tail = projected.levels[0].units[-1]
    assert engine_for(projected).approaching_points(first_tail.available_at)

    entered_values = THREE_BUY_VALUES[:-1] + (("down", 130, 110),)
    entered = projected_structure(entered_values)
    entered_tail = entered.levels[0].units[-1]
    assert engine_for(entered).approaching_points(entered_tail.available_at) == ()
