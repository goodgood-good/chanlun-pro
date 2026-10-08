from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, close_center_at_divergence, establish_center
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterPreview,
    CenterPreviewState,
    DivergenceEvidence,
    SourceKind,
    StrictPointStatus,
    StrictPointVariant,
)
from chanlun.core.strict_structure.point_rules import preview_center_ordinal
from chanlun.core.strict_structure.signals import center_ordinals
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    only_point,
    ongoing_center,
    ongoing_down_center,
    unit,
    valid_five_up_exit,
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


def test_return_touching_core_boundary_does_not_confirm_three_buy():
    center = ongoing_center(zg_tick=115)
    center, _ = advance_center(center, unit(5, "down", 130, 115))
    assert not center.third_class_confirmed
    assert not engine_for(center).third_class_points()


def test_l081_return_endpoint_outside_but_internal_dip_inside_is_not_three_buy():
    # L081:1579-1591 explicitly rejects a third buy when the lower-level
    # return dips into the old center even though its endpoint stays outside.
    center = ongoing_center(zg_tick=115)
    return_unit = unit(5, "down", 130, 116)
    return_unit = replace(
        return_unit,
        low_tick=114,
        low_market_time=return_unit.market_start + timedelta(minutes=2),
    )
    center, _ = advance_center(center, return_unit)
    assert not center.third_class_confirmed
    assert not engine_for(center).third_class_points()


def test_return_touching_core_boundary_does_not_confirm_three_sell():
    center = ongoing_down_center(zd_tick=95)
    center, _ = advance_center(center, unit(5, "up", 80, 95))
    assert not center.third_class_confirmed
    assert not engine_for(center).third_class_points()


def test_completed_down_center_emits_symmetric_three_sell():
    completed = completed_down_center(return_high_tick=94, zd_tick=95)
    point = only_point(engine_for(completed).third_class_points())
    assert point.point_type == "3sell"
    assert point.variant is StrictPointVariant.STANDARD
    assert point.anchor_unit_id == completed.completion_return_unit.unit_id
    assert point.confirmed_at == completed.completed_at


@pytest.mark.parametrize("direction", ["up", "down"])
def test_third_point_uses_internal_return_extreme_time(direction):
    center = completed_up_center() if direction == "up" else completed_down_center()
    ret = center.completion_return_unit
    moment = ret.market_end - timedelta(minutes=2)
    side = "buy" if direction == "up" else "sell"
    ret = replace(ret, **(
        {"end_tick":ret.low_tick + 2, "low_market_time":moment}
        if side == "buy" else {"end_tick":ret.high_tick - 2, "high_market_time":moment}
    ))
    center = replace(center, completion_return_unit=ret)
    point = only_point(engine_for(center).third_class_points())
    assert point.anchor_at == moment < ret.market_end
    assert point.anchor_tick == (ret.low_tick if side == "buy" else ret.high_tick)
    assert point.confirmed_at == center.completed_at
    assert point.available_at == center.completion_available_at


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
        comparison_center_id=completed.center_id,
        comparison_center_established_at=completed.established_at,
        comparison_center_known_at=max(
            item.available_at for item in completed.establishment_units
        ),
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


def test_center_ordinal_tracks_rising_cores_when_envelopes_still_overlap():
    first = completed_up_center(0, zd_tick=100, zg_tick=110)
    second = completed_up_center(10, zd_tick=116, zg_tick=126)
    third = completed_up_center(20, zd_tick=132, zg_tick=142)

    # 601298 一类的走势里，中枢核心持续抬高，但组成单元的外围仍会重叠。
    assert second.dd_tick <= first.gg_tick
    assert third.dd_tick <= second.gg_tick

    ordinals = center_ordinals((first, second, third))

    assert ordinals[(first.center_id, "up")] == 1
    assert ordinals[(second.center_id, "up")] == 2
    assert ordinals[(third.center_id, "up")] == 3


def test_center_ordinal_tracks_falling_cores_when_envelopes_still_overlap():
    first = completed_down_center(0, zd_tick=122, zg_tick=132)
    second = completed_down_center(10, zd_tick=111, zg_tick=121)
    third = completed_down_center(20, zd_tick=100, zg_tick=110)

    assert second.gg_tick >= first.dd_tick
    assert third.gg_tick >= second.dd_tick

    ordinals = center_ordinals((first, second, third))

    assert ordinals[(first.center_id, "down")] == 1
    assert ordinals[(second.center_id, "down")] == 2
    assert ordinals[(third.center_id, "down")] == 3


def test_completed_preview_continues_ordinal_by_rising_core():
    first = completed_up_center(0, zd_tick=100, zg_tick=110)
    second = completed_up_center(10, zd_tick=116, zg_tick=126)
    third = completed_up_center(20, zd_tick=132, zg_tick=142)
    entry = unit(29, "down", 138, 127)
    body = (
        unit(30, "up", 127, 148),
        unit(31, "down", 148, 143),
        unit(32, "up", 143, 148),
    )
    leave = unit(33, "up", 148, 163)
    return_unit = unit(34, "down", 163, 150)
    preview = CenterPreview(
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_basis_revision=first.price_basis_revision,
        entry_unit_id=None,
        unit_ids=tuple(item.unit_id for item in body),
        state=CenterPreviewState.COMPLETED,
        zd_tick=143,
        zg_tick=148,
        available_at=return_unit.available_at,
        completion_leave_unit_id=leave.unit_id,
        completion_return_unit_id=return_unit.unit_id,
        establishment_leave_unit_id=None,
        formation_rule="three_core",
    )
    level = SimpleNamespace(
        center_result=SimpleNamespace(centers=(first, second, third)),
        decomposition_boundaries=(),
        units=(entry, *body, leave, return_unit),
    )

    assert min(item.low_tick for item in body) <= third.gg_tick
    assert preview_center_ordinal(level, preview, direction="up") == 4


def test_stroke_observation_center_is_never_formal_third_class_point():
    source = SourceKind.STROKE_OBSERVATION
    values = tuple(
        replace(item, source_kind=source) for item in valid_five_up_exit()
    )
    observation = establish_center(values, 0, source)
    assert observation is not None
    observation, _event = advance_center(
        observation, unit(5, "down", 130, 120, source_kind=source)
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
