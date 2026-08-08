from dataclasses import replace

import pytest

from chanlun.core.strict_structure.models import (
    SourceKind,
    StrictPointStatus,
    StrictPointVariant,
)
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    only_point,
    ongoing_center,
)


def test_completed_up_center_emits_exactly_one_confirmed_three_buy():
    completed = completed_up_center(return_low_tick=120, zg_tick=115)
    points = engine_for(completed).third_class_points()
    point = only_point(points)
    assert completed.completion_direction == "up"
    assert point.point_type == "3buy"
    assert point.status is StrictPointStatus.CONFIRMED
    assert point.center_id == completed.center_id
    assert point.anchor_unit_id == completed.completion_return_unit.unit_id
    assert point.confirmed_at == completed.completed_at
    assert point.available_at == completed.available_at


def test_return_touching_core_boundary_is_confirmed_boundary_three_buy():
    completed = completed_up_center(return_low_tick=115, zg_tick=115)
    point = only_point(engine_for(completed).third_class_points())
    assert point.point_type == "3buy"
    assert point.variant is StrictPointVariant.BOUNDARY_TOUCH
    assert point.anchor_tick == point.invalidation_tick == 115


def test_completed_down_center_emits_symmetric_three_sell():
    completed = completed_down_center(return_high_tick=95, zd_tick=95)
    point = only_point(engine_for(completed).third_class_points())
    assert point.point_type == "3sell"
    assert point.variant is StrictPointVariant.BOUNDARY_TOUCH
    assert point.anchor_unit_id == completed.completion_return_unit.unit_id
    assert point.confirmed_at == completed.completed_at


def test_ongoing_center_never_emits_formal_third_class_point():
    assert engine_for(ongoing_center()).third_class_points() == ()


def test_third_class_pass_does_not_emit_first_or_second_class():
    points = engine_for(
        completed_up_center(0),
        completed_down_center(10),
    ).third_class_points()
    assert {point.point_type for point in points} == {"3buy", "3sell"}
    assert len(points) == 2


def test_stroke_observation_center_is_never_formal_third_class_point():
    formal = completed_up_center()
    converted = {
        item.unit_id: replace(item, source_kind=SourceKind.STROKE_OBSERVATION)
        for item in (
            formal.entry_unit,
            *formal.body_units,
            formal.completion_leave_unit,
            formal.completion_return_unit,
        )
    }
    observation = replace(
        formal,
            source_kind=SourceKind.STROKE_OBSERVATION,
            entry_unit=converted[formal.entry_unit.unit_id],
            establishment_unit=converted[formal.establishment_unit.unit_id],
            establishment_leave_unit=converted[
                formal.establishment_leave_unit.unit_id
            ],
        initial_units=tuple(
            converted[item.unit_id] for item in formal.initial_units
        ),
        body_units=tuple(converted[item.unit_id] for item in formal.body_units),
        extension_units=tuple(
            converted[item.unit_id] for item in formal.extension_units
        ),
        completion_leave_unit=converted[formal.completion_leave_unit.unit_id],
        completion_return_unit=converted[formal.completion_return_unit.unit_id],
    )
    assert engine_for(observation).third_class_points() == ()


def test_formal_center_model_rejects_unlocked_completion_return():
    formal = completed_up_center()
    unlocked = replace(
        formal.completion_return_unit,
        locked=False,
        confirmed_at=None,
    )
    with pytest.raises(ValueError, match="completion evidence must be locked"):
        replace(formal, completion_return_unit=unlocked)
