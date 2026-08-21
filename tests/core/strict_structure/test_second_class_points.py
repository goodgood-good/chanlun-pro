from __future__ import annotations

from dataclasses import replace

import chanlun.core.strict_structure.signals as signal_module
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    SourceKind,
    StrictLevelResult,
    StrictPointVariant,
    StrictStructureResult,
    build_strict_point_id,
)
from chanlun.core.strict_structure.strength import FormalDivergenceUnavailable
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.test_first_class_points import (
    StrengthTable,
    UP_VALUES,
    engine_for,
    structure_from_values,
)


STRICT_VALUES = UP_VALUES + (
    ("down", 175, 152),
    ("up", 152, 165),
)

TOUCH_VALUES = STRICT_VALUES[:-1] + (("up", 152, 175),)

WEAK_VALUES = STRICT_VALUES[:-1] + (("up", 152, 185),)

INVALID_THEN_LATER_VALUES = WEAK_VALUES + (
    ("down", 185, 160),
    ("up", 160, 168),
)


def strengths(direction, *, weak=False, invalid=False):
    if direction == "up":
        values = {
            ("u-8", "u-9", "u-10"): (120, 6, 3),
            ("u-16", "u-17", "u-18"): (100, 5, 2),
        }
        if weak:
            values.update({"u-18": (120, 6, 3), "u-20": (80, 3, 1)})
        if invalid:
            values.update({"u-18": (120, 6, 3), "u-20": (130, 7, 4)})
    else:
        values = {
            ("u-8", "u-9", "u-10"): (120, -6, -3),
            ("u-16", "u-17", "u-18"): (100, -5, -2),
        }
        if weak:
            values.update({"u-18": (120, -6, -3), "u-20": (80, -3, -1)})
        if invalid:
            values.update({"u-18": (120, -6, -3), "u-20": (130, -7, -4)})
    return StrengthTable(values)


def strict_engine(direction="down", values=STRICT_VALUES, **strength_options):
    strength = strengths(direction, **strength_options)
    structure, _assembly = structure_from_values(
        values=values,
        direction=direction,
        strength=strength,
    )
    return engine_for(structure, strength)


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
    rejected_parent = only_point(rejected.first_class_points())
    assert rejected.second_class_points((rejected_parent,)) == ()

    accepted = strict_engine(direction="down", values=WEAK_VALUES, weak=True)
    parent = only_point(accepted.first_class_points())
    point = only_point(accepted.second_class_points((parent,)))
    pullback = accepted.structure.levels[0].units[20]
    assert point.variant is StrictPointVariant.WEAK_DIVERGENCE
    assert point.divergence.comparison_width == 1
    assert point.invalidation_tick == pullback.low_tick
    assert point.available_at == max(
        pullback.available_at,
        point.divergence.available_at,
    )


def test_weak_second_class_skips_formal_divergence_that_is_not_yet_available(
    monkeypatch,
):
    engine = strict_engine(direction="down", values=WEAK_VALUES, weak=True)
    parent = only_point(engine.first_class_points())

    def unavailable(*_args, **_kwargs):
        raise FormalDivergenceUnavailable("comparison leg is not locked")

    monkeypatch.setattr(signal_module, "compare_divergence", unavailable)

    assert engine.second_class_points((parent,)) == ()


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
    unlocked = replace(level.units[20], locked=False, confirmed_at=None)
    projected_level = replace(
        level,
        units=level.units[:20] + (unlocked,),
        center_result=replace(level.center_result, locked_unit_count=20),
    )
    projected = replace(engine.structure, levels=(projected_level,))
    projected_engine = engine_for(projected, strengths("down"))
    assert projected_engine.second_class_points((first,)) == ()


def test_formed_unlocked_pullback_emits_operational_second_class_preview():
    for direction, expected in (("down", "2buy"), ("up", "2sell")):
        engine = strict_engine(direction=direction)
        level = engine.structure.levels[0]
        unlocked = replace(
            level.units[20],
            locked=False,
            confirmed_at=None,
            formed_at=level.units[20].available_at,
        )
        projected_level = replace(
            level,
            units=level.units[:20] + (unlocked,),
            center_result=replace(level.center_result, locked_unit_count=20),
        )
        projected = replace(engine.structure, levels=(projected_level,))

        points = engine_for(projected, strengths(direction)).approaching_points(
            unlocked.available_at
        )
        point = only_point(
            point
            for point in points
            if point.anchor_unit_id == unlocked.unit_id
        )
        assert point.point_type == expected
        assert point.missing_conditions == ("terminal_unit_audit_lock",)
        assert "projected_geometric_structure" in point.evidence_codes


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
        strength=strengths("down", invalid=True),
    )
    engine = engine_for(extended_structure, strengths("down", invalid=True))
    assert engine.second_class_points((first,)) == ()


def promote_structure_to_level_one(structure):
    original = structure.levels[0]
    units = {item.unit_id: replace(item, structural_level=1) for item in original.units}
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
            initial_units=tuple(units[item.unit_id] for item in value.initial_units),
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
            constituent_units=tuple(
                units[item.unit_id] for item in trend.constituent_units
            ),
        )
        for trend in original.trend_types
    )
    promoted_completed = tuple(
        replace(
            trend,
            structural_level=1,
            centers=tuple(centers[value.center_id] for value in trend.centers),
            constituent_units=tuple(
                units[item.unit_id] for item in trend.constituent_units
            ),
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
        centers=tuple(
            centers[value.center_id] for value in original.center_result.centers
        ),
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
        schema=structure.schema,
        price_basis_revision=structure.price_basis_revision,
        levels=(level_zero, level_one),
    )


def test_lower_level_first_point_independently_emits_higher_level_second_buy():
    lower_anchor = unit(6, "down", 110, 90)
    signal = replace(
        unit(
            6,
            "down",
            110,
            90,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit_id="l1-signal",
        child_ids=(lower_anchor.unit_id,),
    )
    rebound = replace(
        unit(
            7,
            "up",
            90,
            120,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit_id="l1-rebound",
    )
    pullback = replace(
        unit(
            13,
            "down",
            120,
            105,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit_id="l1-pullback",
    )

    lower = confirmed_point(point_type="1buy")
    lower = replace(
        lower,
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type="1buy",
            structural_level=0,
            anchor_unit_id=lower_anchor.unit_id,
            center_id=None,
            parent_point_id=None,
        ),
        anchor_unit_id=lower_anchor.unit_id,
        anchor_at=signal.market_end,
        confirmed_at=signal.confirmed_at,
        available_at=signal.available_at,
        anchor_tick=signal.end_tick,
        invalidation_tick=signal.end_tick,
        divergence=replace(
            lower.divergence,
            anchor_at=signal.market_end,
            anchor_tick=signal.end_tick,
            confirmed_at=signal.market_end,
        ),
    )

    def empty_centers(level, locked_count):
        return CenterLevelResult(
            structural_level=level,
            price_basis_revision=TEST_PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=locked_count,
            replay_from=0,
        )

    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                0,
                (lower_anchor,),
                empty_centers(0, 1),
                (),
                (),
            ),
            StrictLevelResult(
                1,
                (signal, rebound, pullback),
                empty_centers(1, 3),
                (),
                (),
            ),
        ),
    )

    points = engine_for(structure, None).second_class_points((lower,))
    promoted = tuple(point for point in points if point.structural_level == 1)
    assert len(promoted) == 1
    point = promoted[0]
    assert point.point_type == "2buy"
    assert point.parent_point_id == lower.point_id
    assert "small_to_large_reversal" in point.evidence_codes
    assert point.related_point_ids == (lower.point_id,)


def test_small_first_point_can_promote_across_multiple_levels():
    lower_anchor = unit(6, "down", 110, 90)
    bridge = replace(
        unit(
            6,
            "down",
            110,
            90,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit_id="l1-bridge",
        child_ids=(lower_anchor.unit_id,),
    )
    recursive_units = (
        unit(
            7,
            "up",
            90,
            120,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            8,
            "down",
            120,
            100,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            9,
            "up",
            100,
            115,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            10,
            "down",
            115,
            105,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            11,
            "up",
            105,
            130,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
        unit(
            12,
            "down",
            130,
            115,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=1,
        ),
    )
    reverse_center = establish_center(
        recursive_units[1:4],
        1,
        SourceKind.TREND_TYPE,
        entry_unit=recursive_units[0],
    )
    assert reverse_center is not None
    reverse_center, _ = advance_center(reverse_center, recursive_units[4])
    reverse_center, _ = advance_center(reverse_center, recursive_units[5])
    reverse_return = reverse_center.completion_return_unit
    assert reverse_return is not None
    reverse_units = recursive_units
    signal = replace(
        unit(
            6,
            "down",
            110,
            90,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=2,
        ),
        unit_id="l2-signal",
        child_ids=(bridge.unit_id,),
    )
    rebound = replace(
        unit(
            7,
            "up",
            90,
            115,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=2,
        ),
        unit_id="l2-rebound",
        market_end=reverse_return.market_end,
        confirmed_at=reverse_return.confirmed_at,
        available_at=reverse_return.available_at,
        child_ids=tuple(item.unit_id for item in reverse_units),
    )
    pullback = replace(
        unit(
            13,
            "down",
            115,
            105,
            source_kind=SourceKind.TREND_TYPE,
            structural_level=2,
        ),
        unit_id="l2-pullback",
    )
    parent = confirmed_point(point_type="1buy")
    parent = replace(
        parent,
        point_id=build_strict_point_id(
            price_basis_revision=TEST_PRICE_BASIS,
            point_type="1buy",
            structural_level=0,
            anchor_unit_id=lower_anchor.unit_id,
            center_id=None,
            parent_point_id=None,
        ),
        anchor_unit_id=lower_anchor.unit_id,
        anchor_at=signal.market_end,
        confirmed_at=signal.confirmed_at,
        available_at=signal.available_at,
        anchor_tick=signal.end_tick,
        invalidation_tick=signal.end_tick,
        divergence=replace(
            parent.divergence,
            anchor_at=signal.market_end,
            anchor_tick=signal.end_tick,
            confirmed_at=signal.market_end,
        ),
    )

    def empty_centers(level, locked_count):
        return CenterLevelResult(
            structural_level=level,
            price_basis_revision=TEST_PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=locked_count,
            replay_from=0,
        )

    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                0,
                (lower_anchor,),
                empty_centers(0, 1),
                (),
                (),
            ),
            StrictLevelResult(
                1,
                (bridge, *reverse_units),
                CenterLevelResult(
                    structural_level=1,
                    price_basis_revision=TEST_PRICE_BASIS,
                    centers=(reverse_center,),
                    previews=(),
                    events=(),
                    locked_unit_count=1 + len(reverse_units),
                    replay_from=0,
                ),
                (),
                (),
            ),
            StrictLevelResult(
                2,
                (signal, rebound, pullback),
                empty_centers(2, 3),
                (),
                (),
            ),
        ),
    )
    points = engine_for(structure, None).second_class_points((parent,))
    promoted = tuple(point for point in points if point.structural_level == 2)
    assert len(promoted) == 1
    assert promoted[0].parent_point_id == parent.point_id
    assert promoted[0].related_point_ids == (parent.point_id,)

    false_extreme = replace(signal, low_tick=80)
    rejected = replace(
        structure,
        levels=(
            structure.levels[0],
            structure.levels[1],
            StrictLevelResult(
                2,
                (false_extreme, rebound, pullback),
                empty_centers(2, 3),
                (),
                (),
            ),
        ),
    )
    assert not tuple(
        point
        for point in engine_for(rejected, None).second_class_points((parent,))
        if point.structural_level == 2
    )
