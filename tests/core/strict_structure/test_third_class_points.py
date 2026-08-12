from dataclasses import replace
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.identity import build_center_id
from chanlun.core.strict_structure.center_machine import close_center_at_divergence
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    DivergenceEvidence,
    SourceKind,
    StrictPointStatus,
    StrictPointVariant,
)
from chanlun.core.strict_structure.signals import center_ordinals
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    only_point,
    ongoing_center,
    unit,
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


def test_later_divergence_boundary_cannot_rewrite_confirmed_third_point():
    completed = completed_up_center(return_low_tick=120, zg_tick=115)
    engine = engine_for(completed)
    before = only_point(engine.third_class_points())
    leave = completed.completion_leave_unit
    ret = completed.completion_return_unit
    assert leave is not None and ret is not None
    terminal = unit(6, "up", ret.end_tick, ret.end_tick + 30)
    compare_ids = tuple(item.unit_id for item in completed.body_units[:3])
    signal_ids = (leave.unit_id, ret.unit_id, terminal.unit_id)
    divergence = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            completed.price_basis_revision,
            completed.structural_level,
            completed.source_kind.value,
            "consolidation",
            "up",
            compare_ids,
            signal_ids,
        ),
        structural_level=completed.structural_level,
        source_kind=completed.source_kind,
        price_basis_revision=completed.price_basis_revision,
        kind="consolidation",
        direction="up",
        compare_unit_id=compare_ids[-1],
        signal_unit_id=terminal.unit_id,
        anchor_at=terminal.market_end,
        anchor_tick=terminal.high_tick,
        confirmed_at=terminal.confirmed_at,
        available_at=terminal.available_at,
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=False,
        dif_extreme_decayed=False,
        strength_source="macd",
        compare_leg_unit_ids=compare_ids,
        signal_leg_unit_ids=signal_ids,
    )
    closed = close_center_at_divergence(completed, divergence)
    after = engine._third_class_point(closed, direction="up", ordinal=1)

    assert closed.available_at > completed.available_at
    assert after is not None
    assert after == before


def test_ongoing_center_never_emits_formal_third_class_point():
    assert engine_for(ongoing_center()).third_class_points() == ()


def test_third_class_pass_does_not_emit_first_or_second_class():
    points = engine_for(
        completed_up_center(0),
        completed_down_center(10),
    ).third_class_points()
    assert {point.point_type for point in points} == {"3buy", "3sell"}
    assert len(points) == 2


def test_center_ordinal_restarts_after_divergence_decomposition_boundary():
    first = completed_up_center(0, zd_tick=105, zg_tick=115)
    terminal = completed_up_center(10, zd_tick=160, zg_tick=170)
    post_boundary = completed_up_center(20, zd_tick=215, zg_tick=225)

    ordinals = center_ordinals(
        (first, terminal, post_boundary),
        (SimpleNamespace(terminal_center_id=terminal.center_id),),
    )

    assert ordinals[(first.center_id, "up")] == 1
    assert ordinals[(terminal.center_id, "up")] == 2
    assert ordinals[(post_boundary.center_id, "up")] == 1


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
        center_id=build_center_id(
            price_basis_revision=formal.price_basis_revision,
            structural_level=formal.structural_level,
            source_kind=SourceKind.STROKE_OBSERVATION.value,
            entry_unit_id=formal.entry_unit.unit_id,
            initial_unit_ids=tuple(item.unit_id for item in formal.initial_units),
            establishment_unit_id=formal.establishment_unit.unit_id,
            zd_tick=formal.zd_tick,
            zg_tick=formal.zg_tick,
        ),
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
    with pytest.raises(ValueError, match="canonical recursive source"):
        engine_for(observation)


def test_formal_center_model_rejects_unlocked_completion_return():
    formal = completed_up_center()
    unlocked = replace(
        formal.completion_return_unit,
        locked=False,
        confirmed_at=None,
    )
    with pytest.raises(ValueError, match="completion evidence must be locked"):
        replace(formal, completion_return_unit=unlocked)
