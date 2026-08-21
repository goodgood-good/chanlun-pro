from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import chanlun.core.strict_structure.signals as signal_module
from chanlun.core.strict_structure.models import (
    StrictLevelResult,
    StrictStructureResult,
    SourceKind,
    TrendKind,
    TrendState,
)
from chanlun.core.strict_structure.recursive_engine import (
    calculate_level_with_divergence_boundaries,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    StrengthSnapshot,
    center_departure_comparison_leg,
    center_entry_comparison_leg,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


UP_VALUES = (
    ("up", 90, 120),
    ("down", 120, 100),
    ("up", 100, 115),
    ("down", 115, 105),
    ("up", 105, 115),
    ("down", 115, 105),
    ("up", 105, 130),
    ("down", 130, 120),
    ("up", 120, 131),
    ("down", 131, 120),
    ("up", 120, 150),
    ("down", 150, 135),
    ("up", 135, 155),
    ("down", 155, 138),
    ("up", 138, 145),
    ("down", 145, 138),
    ("up", 138, 170),
    ("down", 170, 150),
    ("up", 150, 175),
)

EXTENDED_UP_VALUES = (
    *UP_VALUES,
    ("down", 175, 152),
    ("up", 152, 180),
    ("down", 180, 172),
    ("up", 172, 177),
    ("down", 177, 174),
    ("up", 174, 177),
    ("down", 177, 174),
    ("up", 174, 200),
    ("down", 200, 180),
)

NO_NEW_HIGH_VALUES = (
    *UP_VALUES[:12],
    ("up", 135, 145),
    ("down", 145, 138),
    ("up", 138, 145),
    ("down", 145, 138),
    ("up", 138, 148),
    ("down", 148, 146),
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


def structure_from_values(values=UP_VALUES, *, direction="up", strength=None):
    units = make_units(values, direction)
    center_result, assembly = calculate_level_with_divergence_boundaries(
        units,
        0,
        SourceKind.SEGMENT,
        strength=strength,
    )
    level = StrictLevelResult(
        structural_level=0,
        units=units,
        center_result=center_result,
        trend_types=assembly.current_trends,
        completed_trends=assembly.completed_trends,
        decomposition_boundaries=assembly.decomposition_boundaries,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(level,),
    )
    return structure, assembly


class StrengthTable:
    def __init__(self, values):
        self.values = values

    def snapshot(self, value):
        key = tuple(value.child_ids) if len(value.child_ids) == 3 else value.unit_id
        area, peak, dif = self.values[key]
        return StrengthSnapshot(
            unit_id=value.unit_id,
            direction=value.direction,
            histogram_area=area,
            histogram_peak=peak,
            dif_extreme=dif,
            source="macd",
            available_at=value.available_at,
        )


def divergent_strength(direction, *, extended=False):
    if direction == "up":
        values = {
            ("u-8", "u-9", "u-10"): (100, 5, 2),
            ("u-16", "u-17", "u-18"): (80, 3, 1),
        }
    else:
        values = {
            ("u-8", "u-9", "u-10"): (100, -5, -2),
            ("u-16", "u-17", "u-18"): (80, -3, -1),
        }
    return StrengthTable(values)


def consolidation_divergent_strength(direction):
    sign = 1 if direction == "up" else -1
    return StrengthTable(
        {
            "u-0": (100, sign * 5, sign * 2),
            "u-6": (80, sign * 3, sign * 1),
        }
    )


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
        trend for trend in assembly.completed_trends if trend.kind is TrendKind.TREND
    )
    assert matches
    divergent = tuple(
        trend for trend in matches if trend.terminal_divergence is not None
    )
    return max(divergent or matches, key=lambda trend: len(trend.centers))


def test_down_trend_terminal_divergence_emits_one_buy():
    strength = divergent_strength("down")
    structure, assembly = structure_from_values(
        direction="down",
        strength=strength,
    )
    trend = target_trend(assembly)
    point = only_point(engine_for(structure, strength).first_class_points())
    assert point.point_type == "1buy"
    assert point.divergence.kind == "trend"
    assert point.anchor_unit_id == trend.terminal_unit.unit_id
    assert point.invalidation_tick == trend.terminal_unit.low_tick


def test_up_trend_terminal_divergence_emits_one_sell():
    strength = divergent_strength("up")
    structure, assembly = structure_from_values(
        direction="up",
        strength=strength,
    )
    trend = target_trend(assembly)
    point = only_point(engine_for(structure, strength).first_class_points())
    assert point.point_type == "1sell"
    assert point.invalidation_tick == trend.terminal_unit.high_tick


def test_post_assembly_strength_cannot_retroactively_create_formal_first_class():
    structure, _assembly = structure_from_values(direction="up")
    assert engine_for(structure, divergent_strength("up")).first_class_points() == ()


def test_approaching_first_waits_for_unlocked_third_departure_segment():
    strength = divergent_strength("up")
    units = list(make_units(UP_VALUES, "up"))
    units[-1] = replace(
        units[-1],
        locked=False,
        confirmed_at=None,
        formed_at=units[-1].available_at,
    )
    center_result, assembly = calculate_level_with_divergence_boundaries(
        tuple(units),
        0,
        SourceKind.SEGMENT,
        strength=strength,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=tuple(units),
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=assembly.completed_trends,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )

    point = only_point(
        engine_for(structure, strength).approaching_points(units[-1].available_at)
    )
    assert point.point_type == "1sell"
    assert point.anchor_unit_id == units[-1].unit_id
    assert point.missing_conditions == ("terminal_unit_audit_lock",)
    assert "projected_geometric_structure" in point.evidence_codes
    assert "width_matched_entry_departure_legs" in point.evidence_codes
    assert "comparison_leg_width_3" in point.evidence_codes
    assert "macd_histogram_area_decay" in point.evidence_codes


def test_approaching_first_skips_formal_divergence_that_is_not_yet_available(
    monkeypatch,
):
    strength = divergent_strength("up")
    units = list(make_units(UP_VALUES, "up"))
    units[-1] = replace(
        units[-1],
        locked=False,
        confirmed_at=None,
        formed_at=units[-1].available_at,
    )
    center_result, assembly = calculate_level_with_divergence_boundaries(
        tuple(units),
        0,
        SourceKind.SEGMENT,
        strength=strength,
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=tuple(units),
                center_result=center_result,
                trend_types=assembly.current_trends,
                completed_trends=assembly.completed_trends,
                decomposition_boundaries=assembly.decomposition_boundaries,
            ),
        ),
    )

    def unavailable(*_args, **_kwargs):
        raise FormalDivergenceUnavailable("comparison leg is not locked")

    monkeypatch.setattr(
        signal_module,
        "compare_terminal_trend_divergence",
        unavailable,
    )

    assert (
        engine_for(structure, strength)._approaching_first_class(
            structure.levels[0],
            units[-1],
        )
        is None
    )


def test_approaching_consolidation_first_class_is_symmetric():
    for direction, expected_type in (("up", "1sell"), ("down", "1buy")):
        strength = consolidation_divergent_strength(direction)
        units = list(make_units(UP_VALUES[:7], direction))
        units[-1] = replace(
            units[-1],
            locked=False,
            confirmed_at=None,
            formed_at=units[-1].available_at,
        )
        center_result, assembly = calculate_level_with_divergence_boundaries(
            tuple(units),
            0,
            SourceKind.SEGMENT,
            strength=strength,
        )
        structure = StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=TEST_PRICE_BASIS,
            levels=(
                StrictLevelResult(
                    structural_level=0,
                    units=tuple(units),
                    center_result=center_result,
                    trend_types=assembly.current_trends,
                    completed_trends=assembly.completed_trends,
                    decomposition_boundaries=assembly.decomposition_boundaries,
                ),
            ),
        )

        point = only_point(
            engine_for(structure, strength).approaching_points(
                units[-1].available_at
            )
        )
        assert point.point_type == expected_type
        assert point.anchor_unit_id == "u-6"
        assert point.divergence.kind == "consolidation"
        assert "formal_consolidation_movement" in point.evidence_codes
        assert "projected_geometric_structure" in point.evidence_codes
        assert point.missing_conditions == ("terminal_unit_audit_lock",)


def test_uncompleted_single_center_does_not_emit_first_class():
    structure, _assembly = structure_from_values(values=UP_VALUES[:5])
    assert engine_for(structure, StrengthTable({})).first_class_points() == ()


def test_single_center_consolidation_divergence_emits_symmetric_first_class():
    for direction, expected_type in (("up", "1sell"), ("down", "1buy")):
        strength = consolidation_divergent_strength(direction)
        structure, assembly = structure_from_values(
            values=UP_VALUES[:7],
            direction=direction,
            strength=strength,
        )

        point = only_point(engine_for(structure, strength).first_class_points())
        boundary = assembly.decomposition_boundaries[0]
        trend = assembly.completed_trends[0]
        assert point.point_type == expected_type
        assert point.divergence.kind == "consolidation"
        assert point.anchor_unit_id == "u-6"
        assert point.available_at == structure.levels[0].units[-1].available_at
        assert boundary.boundary_kind == "consolidation_divergence"
        assert boundary.anchor_unit_id == point.anchor_unit_id
        assert trend.kind is TrendKind.CONSOLIDATION
        assert trend.terminal_divergence == point.divergence


def test_three_unit_entry_compares_three_unit_departure_for_first_class():
    up_units = (
        unit(0, "up", 80, 100),
        unit(1, "down", 100, 90),
        *valid_five_up_exit(2),
        unit(7, "down", 130, 120),
        unit(8, "up", 120, 140),
    )
    for direction, expected_type in (("up", "1sell"), ("down", "1buy")):
        units = (
            up_units
            if direction == "up"
            else tuple(
                unit(
                    index,
                    "up" if item.direction == "down" else "down",
                    300 - item.start_tick,
                    300 - item.end_tick,
                )
                for index, item in enumerate(up_units)
            )
        )
        sign = 1 if direction == "up" else -1
        strength = StrengthTable(
            {
                ("u-0", "u-1", "u-2"): (100, sign * 5, sign * 2),
                ("u-6", "u-7", "u-8"): (80, sign * 3, sign * 1),
            }
        )
        center_result, assembly = calculate_level_with_divergence_boundaries(
            units,
            0,
            SourceKind.SEGMENT,
            strength=strength,
        )
        structure = StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=TEST_PRICE_BASIS,
            levels=(
                StrictLevelResult(
                    structural_level=0,
                    units=units,
                    center_result=center_result,
                    trend_types=assembly.current_trends,
                    completed_trends=assembly.completed_trends,
                    decomposition_boundaries=assembly.decomposition_boundaries,
                ),
            ),
        )

        point = only_point(engine_for(structure, strength).first_class_points())
        assert point.point_type == expected_type
        assert point.divergence.comparison_width == 3
        assert point.divergence.compare_leg_unit_ids == ("u-0", "u-1", "u-2")
        assert point.divergence.signal_leg_unit_ids == ("u-6", "u-7", "u-8")


def test_consolidation_first_class_is_followed_by_symmetric_second_class():
    values = (*UP_VALUES[:8], ("up", 120, 129))
    for direction, first_type, second_type in (
        ("up", "1sell", "2sell"),
        ("down", "1buy", "2buy"),
    ):
        strength = consolidation_divergent_strength(direction)
        structure, _assembly = structure_from_values(
            values=values,
            direction=direction,
            strength=strength,
        )
        engine = engine_for(structure, strength)
        first = only_point(engine.first_class_points())
        second = only_point(engine.second_class_points((first,)))

        assert first.point_type == first_type
        assert second.point_type == second_type
        assert second.parent_point_id == first.point_id
        assert second.anchor_unit_id == "u-8"
        assert second.variant.value == "strict"


def test_departure_without_whole_trend_new_extreme_is_not_formal_divergence():
    strength = divergent_strength("up")
    structure, _assembly = structure_from_values(
        values=NO_NEW_HIGH_VALUES,
        strength=strength,
    )
    assert engine_for(structure, strength).first_class_points() == ()


def test_trend_comparison_uses_equal_three_unit_entry_and_departure_legs():
    _structure, assembly = structure_from_values()
    trend = target_trend(assembly)
    last_center = trend.centers[-1]
    entry = center_entry_comparison_leg(last_center, trend.constituent_units)
    assert entry is not None
    departure = center_departure_comparison_leg(
        last_center,
        make_units(UP_VALUES, "up"),
        width=entry.width,
    )
    assert tuple(item.unit_id for item in entry.units) == ("u-8", "u-9", "u-10")
    assert departure is not None
    assert tuple(item.unit_id for item in departure.units) == (
        "u-16",
        "u-17",
        "u-18",
    )


def test_forming_trend_is_observation_only_not_first_class():
    strength = divergent_strength("up")
    structure, assembly = structure_from_values()
    trend = next(
        item
        for item in assembly.completed_trends
        if item.kind is TrendKind.TREND and item.terminal_divergence is None
    )
    forming = replace(
        trend,
        state=TrendState.FORMING,
        confirmed_at=None,
        terminal_divergence=None,
    )
    level = replace(
        structure.levels[0],
        trend_types=(forming,),
        completed_trends=(),
        decomposition_boundaries=(),
    )
    projected = replace(structure, levels=(level,))
    assert engine_for(projected, strength).first_class_points() == ()


def test_first_class_available_at_uses_trend_completion():
    strength = divergent_strength("down")
    structure, assembly = structure_from_values(
        direction="down",
        strength=strength,
    )
    trend = target_trend(assembly)
    point = only_point(engine_for(structure, strength).first_class_points())
    assert point.available_at == max(
        trend.terminal_unit.available_at,
        trend.centers[-1].available_at,
        trend.available_at,
        point.divergence.available_at,
    )


def test_completed_first_point_survives_later_same_direction_trend():
    strength = divergent_strength("up", extended=True)
    prefix_structure, _ = structure_from_values(strength=strength)
    frozen = only_point(engine_for(prefix_structure, strength).first_class_points())
    structure, _assembly = structure_from_values(
        values=EXTENDED_UP_VALUES,
        strength=strength,
    )
    points = engine_for(
        structure,
        strength,
    ).first_class_points()
    assert points == (frozen,)
