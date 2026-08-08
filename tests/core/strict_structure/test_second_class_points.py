from __future__ import annotations

from dataclasses import replace

from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    StrictLevelResult,
    StrictPointVariant,
    StrictStructureResult,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.test_first_class_points import (
    StrengthTable,
    UP_VALUES,
    engine_for,
    structure_from_values,
)


STRICT_VALUES = UP_VALUES

TOUCH_VALUES = STRICT_VALUES[:-1] + (("up", 150, 170),)

WEAK_VALUES = STRICT_VALUES[:-1] + (("up", 150, 175),)

INVALID_THEN_LATER_VALUES = WEAK_VALUES + (
    ("down", 175, 160),
    ("up", 160, 168),
)


def strengths(direction, *, weak=False, invalid=False):
    if direction == "up":
        values = {
            "u-6": (50, 2, 1),
            "u-10": (120, 6, 3),
            "u-12": (120, 6, 3),
            "u-16": (100, 5, 2),
        }
        if weak:
            values["u-18"] = (80, 3, 1)
        if invalid:
            values["u-18"] = (110, 6, 3)
    else:
        values = {
            "u-6": (50, -2, -1),
            "u-10": (120, -6, -3),
            "u-12": (120, -6, -3),
            "u-16": (100, -5, -2),
        }
        if weak:
            values["u-18"] = (80, -3, -1)
        if invalid:
            values["u-18"] = (110, -6, -3)
    return StrengthTable(values)


def strict_engine(direction="down", values=STRICT_VALUES, **strength_options):
    structure, _assembly = structure_from_values(values=values, direction=direction)
    return engine_for(structure, strengths(direction, **strength_options))


def only_point(points):
    values = tuple(points)
    assert len(values) == 1
    return values[0]


def test_first_complete_rebound_and_pullback_not_breaking_low_emits_strict_two_buy():
    engine = strict_engine(direction="down")
    first = only_point(engine.first_class_points())
    point = only_point(engine.second_class_points((first,)))
    assert point.point_type == "2buy"
    assert point.variant is StrictPointVariant.STRICT
    assert point.parent_point_id == first.point_id
    assert point.invalidation_tick == first.anchor_tick


def test_new_low_pullback_requires_weak_divergence():
    rejected = strict_engine(direction="down", values=WEAK_VALUES, invalid=True)
    first = only_point(rejected.first_class_points())
    assert rejected.second_class_points((first,)) == ()

    accepted = strict_engine(direction="down", values=WEAK_VALUES, weak=True)
    first = only_point(accepted.first_class_points())
    point = only_point(accepted.second_class_points((first,)))
    pullback = accepted.structure.levels[0].units[18]
    assert point.variant is StrictPointVariant.WEAK_DIVERGENCE
    assert point.invalidation_tick == pullback.low_tick
    assert point.available_at == max(
        pullback.available_at,
        point.divergence.available_at,
    )


def test_two_sell_is_symmetric():
    engine = strict_engine(direction="up")
    first = only_point(engine.first_class_points())
    point = only_point(engine.second_class_points((first,)))
    assert point.point_type == "2sell"
    assert point.variant is StrictPointVariant.STRICT


def test_unlocked_pullback_remains_non_confirmed():
    engine = strict_engine(direction="down")
    first = only_point(engine.first_class_points())
    level = engine.structure.levels[0]
    unlocked = replace(level.units[18], locked=False, confirmed_at=None)
    projected_level = replace(level, units=level.units[:18] + (unlocked,))
    projected = replace(engine.structure, levels=(projected_level,))
    projected_engine = engine_for(projected, strengths("down"))
    assert projected_engine.second_class_points((first,)) == ()


def test_pullback_touching_first_point_extreme_is_strict_second_class():
    buy_engine = strict_engine(direction="down", values=TOUCH_VALUES)
    buy_first = only_point(buy_engine.first_class_points())
    buy = only_point(buy_engine.second_class_points((buy_first,)))

    sell_engine = strict_engine(direction="up", values=TOUCH_VALUES)
    sell_first = only_point(sell_engine.first_class_points())
    sell = only_point(sell_engine.second_class_points((sell_first,)))
    assert buy.point_type == "2buy"
    assert sell.point_type == "2sell"
    assert buy.variant is sell.variant is StrictPointVariant.STRICT


def test_invalid_first_completed_pullback_cannot_be_skipped_for_later_pullback():
    parent_engine = strict_engine(direction="down", values=WEAK_VALUES, invalid=True)
    first = only_point(parent_engine.first_class_points())
    extended_structure, _assembly = structure_from_values(
        values=INVALID_THEN_LATER_VALUES,
        direction="down",
    )
    engine = engine_for(extended_structure, strengths("down", invalid=True))
    assert engine.second_class_points((first,)) == ()


def promote_structure_to_level_one(structure):
    original = structure.levels[0]
    units = {
        item.unit_id: replace(item, structural_level=1)
        for item in original.units
    }
    centers = {}
    for value in original.center_result.centers:
        centers[value.center_id] = replace(
            value,
            structural_level=1,
            entry_unit=units[value.entry_unit.unit_id],
            establishment_unit=(
                None
                if value.establishment_unit is None
                else units[value.establishment_unit.unit_id]
            ),
            establishment_leave_unit=(
                None
                if value.establishment_leave_unit is None
                else units[value.establishment_leave_unit.unit_id]
            ),
            initial_units=tuple(
                units[item.unit_id] for item in value.initial_units
            ),
            body_units=tuple(units[item.unit_id] for item in value.body_units),
            extension_units=tuple(
                units[item.unit_id] for item in value.extension_units
            ),
            pending_leave_unit=(
                None
                if value.pending_leave_unit is None
                else units[value.pending_leave_unit.unit_id]
            ),
            completion_leave_unit=(
                None
                if value.completion_leave_unit is None
                else units[value.completion_leave_unit.unit_id]
            ),
            completion_return_unit=(
                None
                if value.completion_return_unit is None
                else units[value.completion_return_unit.unit_id]
            ),
        )
    promoted_trends = tuple(
        replace(
            trend,
            structural_level=1,
            centers=tuple(centers[value.center_id] for value in trend.centers),
            constituent_units=tuple(units[item.unit_id] for item in trend.constituent_units),
        )
        for trend in original.trend_types
    )
    promoted_completed = tuple(
        replace(
            trend,
            structural_level=1,
            centers=tuple(centers[value.center_id] for value in trend.centers),
            constituent_units=tuple(units[item.unit_id] for item in trend.constituent_units),
        )
        for trend in original.completed_trends
    )
    empty_centers = CenterLevelResult(
        structural_level=0,
        price_basis_revision=structure.price_basis_revision,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    level_zero = StrictLevelResult(
        structural_level=0,
        units=(),
        center_result=empty_centers,
        trend_types=(),
        completed_trends=(),
    )
    promoted_center_result = CenterLevelResult(
        structural_level=1,
        price_basis_revision=structure.price_basis_revision,
        centers=tuple(centers[value.center_id] for value in original.center_result.centers),
        previews=(),
        events=(),
        locked_unit_count=original.center_result.locked_unit_count,
        replay_from=original.center_result.replay_from,
    )
    level_one = StrictLevelResult(
        structural_level=1,
        units=tuple(units[item.unit_id] for item in original.units),
        center_result=promoted_center_result,
        trend_types=promoted_trends,
        completed_trends=promoted_completed,
    )
    return StrictStructureResult(
        schema_version=structure.schema_version,
        price_basis_revision=structure.price_basis_revision,
        levels=(level_zero, level_one),
    )


def test_lower_level_first_point_enriches_but_does_not_duplicate_second_class():
    base, _assembly = structure_from_values(values=STRICT_VALUES, direction="down")
    promoted = promote_structure_to_level_one(base)
    engine = engine_for(promoted, strengths("down"))
    parent = only_point(engine.first_class_points())
    pullback = promoted.levels[1].units[18]
    lower = confirmed_point(point_type="1buy")
    lower = replace(
        lower,
        anchor_at=pullback.market_start,
        confirmed_at=pullback.market_start,
        available_at=pullback.market_start,
        divergence=replace(lower.divergence, available_at=pullback.market_start),
    )
    points = engine.second_class_points(
        (parent,),
        lower_level_first_points=(lower,),
    )
    assert len(points) == 1
    assert points[0].related_point_ids == (lower.point_id,)
