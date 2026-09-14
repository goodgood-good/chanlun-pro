from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import chanlun.core.strict_structure.signals as signal_module
from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.center_machine import calculate_centers
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
    compare_terminal_trend_divergence,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
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
    ("down", 155, 151),
    ("up", 151, 154),
    ("down", 154, 152),
    ("up", 152, 170),
    ("down", 170, 160),
    ("up", 160, 175),
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

# A compact, connected sequence with two separated centers. The entry into
# the last center is one unit; its complete departure contains the outside
# return and a further directional unit. No symbol-dependent rule is needed.
SHORT_ENTRY_UP_VALUES = (
    ("up", 78, 93), ("down", 93, 74), ("up", 74, 104),
    ("down", 104, 83), ("up", 83, 185), ("down", 185, 143),
    ("up", 143, 172), ("down", 172, 151), ("up", 151, 167),
    ("down", 167, 151), ("up", 151, 159), ("down", 159, 151),
    ("up", 151, 182), ("down", 182, 156), ("up", 156, 237),
    ("down", 237, 204), ("up", 204, 258),
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
            ("u-10", "u-11", "u-12"): (100, 5, 2),
            ("u-16", "u-17", "u-18"): (80, 3, 1),
        }
    else:
        values = {
            ("u-10", "u-11", "u-12"): (100, -5, -2),
            ("u-16", "u-17", "u-18"): (80, -3, -1),
        }
    return StrengthTable(values)


def consolidation_divergent_strength(direction):
    sign = 1 if direction == "up" else -1
    return StrengthTable(
        {
            ("u-4", "u-5", "u-6"): (100, sign * 5, sign * 2),
            ("u-10", "u-11", "u-12"): (80, sign * 3, sign * 1),
        }
    )


def engine_for(structure, strength):
    return StrictSignalEngine(
        structure=structure,
        strength=strength,
        price_quantum=Decimal("0.01"),
    )


def consolidation_structure(
    direction,
    *,
    terminal_locked=True,
    include_second_class_tail=False,
):
    """Isolate the second scanned center as a later consolidation movement.

    Both physical centers have all five establishment roles.  For the selected
    second center, u-6 is the physical entry, u-7/u-8/u-9 are the middle core,
    and u-10 is the establishment leave.  The completed movement u-4/u-5/u-6
    and departure movement u-10/u-11/u-12 are the divergence comparison legs.
    """

    end = 15 if include_second_class_tail else 13
    units = list(make_units(UP_VALUES[:end], direction))
    if not terminal_locked:
        if include_second_class_tail:
            raise ValueError("an unlocked terminal cannot precede a locked tail")
        units[-1] = replace(
            units[-1],
            locked=False,
            confirmed_at=None,
            formed_at=units[-1].available_at,
        )
    source_units = tuple(units)
    scanned = calculate_centers(source_units, 0, SourceKind.SEGMENT)
    center = scanned.centers[1]
    strength = consolidation_divergent_strength(direction)
    assembly = assemble_trend_types(
        (center,),
        source_units,
        0,
        strength=strength,
        group_start_unit_id="u-4",
    )

    if terminal_locked:
        retained_center = assembly.current_trends[0].centers[0]
        completed_trends = tuple(
            trend
            for trend in assembly.completed_trends
            if trend.terminal_divergence is not None
        )
    else:
        retained_center = center
        completed_trends = assembly.completed_trends
    center_result = replace(
        scanned,
        centers=(retained_center,),
        events=(),
    )
    level = StrictLevelResult(
        structural_level=0,
        units=source_units,
        center_result=center_result,
        trend_types=assembly.current_trends,
        completed_trends=completed_trends,
        decomposition_boundaries=assembly.decomposition_boundaries,
    )
    return (
        StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=TEST_PRICE_BASIS,
            levels=(level,),
        ),
        assembly,
        strength,
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
    assert "complete_entry_departure_legs" in point.evidence_codes
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


def test_approaching_consolidation_does_not_become_a_first_class_point():
    for direction in ("up", "down"):
        structure, _assembly, strength = consolidation_structure(
            direction,
            terminal_locked=False,
        )
        level = structure.levels[0]
        terminal = level.units[-1]
        point = engine_for(structure, strength)._approaching_first_class(
            level,
            terminal,
        )

        assert point is None
        assert not engine_for(structure, strength).first_class_points()


def test_uncompleted_single_center_does_not_emit_first_class():
    structure, _assembly = structure_from_values(values=UP_VALUES[:5])
    assert engine_for(structure, StrengthTable({})).first_class_points() == ()


def test_later_divergence_witness_cannot_erase_an_already_confirmed_third_class():
    values = (("up", 90, 120), ("down", 120, 100), ("up", 100, 115),
              ("down", 115, 105), ("up", 105, 130), ("down", 130, 120),
              ("up", 120, 128), ("down", 128, 118))
    for direction, kind in (("up", "3buy"), ("down", "3sell")):
        sign = 1 if direction == "up" else -1
        strength = StrengthTable({"u-0": (100, sign*5, sign*2), "u-4": (50, sign*2, sign)})
        before, _ = structure_from_values(values=values[:6], direction=direction, strength=strength)
        after, _ = structure_from_values(values=values, direction=direction, strength=strength)
        point = only_point(engine_for(before, strength).third_class_points())
        assert point.point_type == kind
        assert point in engine_for(after, strength).third_class_points()
        center = next(c for c in after.levels[0].center_result.centers if c.center_id == point.center_id)
        assert center.physically_completed


def test_consolidation_divergence_remains_independent_of_first_class_points():
    for direction in ("up", "down"):
        structure, assembly, strength = consolidation_structure(direction)

        assert engine_for(structure, strength).first_class_points() == ()
        divergence = only_point(collect_formal_divergence_ledger(structure))
        boundary = assembly.decomposition_boundaries[0]
        trend = next(
            item
            for item in assembly.completed_trends
            if item.terminal_divergence is not None
        )
        assert divergence.kind == "consolidation"
        assert divergence.direction == direction
        assert divergence.signal_unit_id == "u-12"
        assert divergence.available_at == structure.levels[0].units[-1].available_at
        assert boundary.boundary_kind == "consolidation_divergence"
        assert boundary.anchor_unit_id == divergence.signal_unit_id
        assert trend.kind is TrendKind.CONSOLIDATION
        assert trend.terminal_divergence == divergence


def test_consolidation_observation_keeps_complete_comparable_legs():
    for direction in ("up", "down"):
        structure, _assembly, strength = consolidation_structure(direction)

        divergence = only_point(collect_formal_divergence_ledger(structure))
        assert divergence.comparison_width == 3
        assert divergence.compare_leg_unit_ids == ("u-4", "u-5", "u-6")
        assert divergence.signal_leg_unit_ids == (
            "u-10",
            "u-11",
            "u-12",
        )


def test_consolidation_does_not_automatically_produce_second_class_points():
    for direction in ("up", "down"):
        structure, _assembly, strength = consolidation_structure(
            direction,
            include_second_class_tail=True,
        )
        engine = engine_for(structure, strength)
        assert not engine.first_class_points()
        assert not engine.second_class_points()
        assert collect_formal_divergence_ledger(structure)


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
    assert tuple(item.unit_id for item in entry.units) == ("u-10", "u-11", "u-12")
    assert departure is not None
    assert tuple(item.unit_id for item in departure.units) == (
        "u-16",
        "u-17",
        "u-18",
    )


@pytest.mark.parametrize("direction,expected_type", [("up", "1sell"), ("down", "1buy")])
def test_short_entry_and_complete_departure_preserve_first_and_third_points(direction, expected_type):
    from chanlun.core.strict_structure.evidence_assembler import StrictEvidenceAssembler, empty_stroke_center_observations
    sign = 1 if direction == "up" else -1
    strength = StrengthTable({"u-8": (100, sign * 5, sign * 2),
                              ("u-14", "u-15", "u-16"): (80, sign * 3, sign)})
    structure, assembly = structure_from_values(SHORT_ENTRY_UP_VALUES, direction=direction, strength=strength)
    evidence = StrictEvidenceAssembler(
        symbol="fixture", source_frequency="1m", source_closed_at=structure.levels[0].units[-1].available_at,
        price_basis_revision=TEST_PRICE_BASIS, structure_price_quantum=Decimal("0.01"),
        strict_config_revision="fixture", structure=structure, strength=strength,
    ).evidence(stroke_center_observations=empty_stroke_center_observations(TEST_PRICE_BASIS))
    first = only_point(p for p in evidence.confirmed_points if p.point_type == expected_type)
    assert first.divergence.compare_leg_unit_ids == ("u-8",)
    assert first.divergence.signal_leg_unit_ids == ("u-14", "u-15", "u-16")
    assert first.divergence.comparison_width == 1 and first.divergence.signal_width == 3
    # Closing a 1-to-3 divergence must not use the entry width to erase the
    # already confirmed third point on the departure's outside return.
    third = only_point(p for p in evidence.confirmed_points if p.center_id == first.center_id and p.point_type[0] == "3")
    assert third.point_type == ("3buy" if direction == "up" else "3sell")
    assert third.anchor_unit_id == "u-15"
    assert third.available_at <= first.available_at
    before, _ = structure_from_values(SHORT_ENTRY_UP_VALUES[:-1], direction=direction, strength=strength)
    assert not engine_for(before, strength).first_class_points()


def test_short_entry_does_not_allow_stronger_departure_to_become_divergence():
    strength = StrengthTable({"u-8": (100, 5, 2), ("u-14", "u-15", "u-16"): (120, 6, 3)})
    structure, _ = structure_from_values(SHORT_ENTRY_UP_VALUES, strength=strength)
    assert not engine_for(structure, strength).first_class_points()


@pytest.mark.parametrize("direction", ["up", "down"])
def test_projected_internal_second_discloses_pending_parent_and_passes_screening_semantics(direction):
    from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict, strict_point_to_chart_dict
    from chanlun.screening.rules import point_semantic_reasons
    values = (*UP_VALUES[:18], ("up", 160, 168))
    strength = divergent_strength(direction)
    units = list(make_units(values, direction))
    units[-1] = replace(units[-1], locked=False, confirmed_at=None, formed_at=units[-1].available_at)
    center_result, assembly = calculate_level_with_divergence_boundaries(
        tuple(units), 0, SourceKind.SEGMENT, strength=strength)
    level = StrictLevelResult(
        structural_level=0, units=tuple(units), center_result=center_result,
        trend_types=assembly.current_trends, completed_trends=assembly.completed_trends,
        decomposition_boundaries=assembly.decomposition_boundaries)
    structure = StrictStructureResult(schema="chanlun-structure", price_basis_revision=TEST_PRICE_BASIS, levels=(level,))
    engine = engine_for(structure, strength)
    waiting = engine.approaching_points(units[-1].available_at)
    second = only_point(p for p in waiting if p.point_type[0] == "2")
    assert "parent_first_class_confirmed" in second.missing_conditions
    assert "formed_first_class_parent" in second.evidence_codes
    assert "confirmed_first_class_parent" not in second.evidence_codes
    assert not any(p.point_type[0] in "12" for p in engine.confirmed_points())
    points = {p.point_id: strict_point_to_chart_dict(p) for p in waiting}
    centers = {c.center_id: strict_center_to_chart_dict(c) for c in center_result.centers}
    assert not point_semantic_reasons(points[second.point_id], points, centers)


def test_composite_extreme_keeps_real_price_time_and_late_confirmation_symmetrically():
    # The last test is lower than the first departure high (mirror: higher low).
    values = (*UP_VALUES[:18], ("up", 160, 168), ("down", 168, 162), ("up", 162, 166))
    for direction in ("up", "down"):
        strength = divergent_strength(direction)
        structure, assembly = structure_from_values(values, direction=direction, strength=strength)
        engine = engine_for(structure, strength)
        point = only_point(engine.first_class_points())
        assert point.anchor_unit_id == "u-18"
        assert point.price_anchor_unit_id in {"u-16", "u-17"}
        unit_by_id = {u.unit_id: u for u in structure.levels[0].units}
        actual = unit_by_id[point.price_anchor_unit_id]
        assert point.anchor_tick == (170 if direction == "up" else 130)
        assert point.anchor_at == actual.price_anchor_time(point.side)
        assert point.available_at >= unit_by_id["u-18"].available_at
        assert point.invalidation_tick == point.anchor_tick
        assert assembly.decomposition_boundaries[0].anchor_at == point.anchor_at
        # The actual first retest is u-18, not the later u-20. It can only be
        # confirmed once the composite first-class proof becomes available.
        second = only_point(engine.second_class_points((point,)))
        assert second.anchor_unit_id == "u-18"
        assert second.available_at == point.available_at
        assert second.confirmed_at >= point.confirmed_at
        assert "internal_first_retest" in second.evidence_codes


def test_first_retest_uses_shared_endpoint_but_not_an_unresolved_interior_reversal():
    values = (*UP_VALUES[:18], ("up", 160, 168), ("down", 168, 162), ("up", 162, 166))
    strength = divergent_strength("up")
    structure, _ = structure_from_values(values, direction="up", strength=strength)
    engine = engine_for(structure, strength)
    original = only_point(engine.first_class_points())
    # The high at u-16's end is also u-17's start. Either attribution must
    # still find the same actual first retest, u-18.
    shared = replace(original, divergence=replace(original.divergence, price_anchor_unit_id="u-17"))
    assert only_point(engine.second_class_points((shared,))).anchor_unit_id == "u-18"
    # A higher actual high inside the downward u-17 has no complete same-level
    # outward leg followed by a first retest. Do not silently move the origin
    # to u-16 or use u-20's later rebound as that first retest.
    units = list(structure.levels[0].units)
    internal_at = units[17].market_start + (units[17].market_end - units[17].market_start) / 2
    units[17] = replace(units[17], high_tick=180, high_market_time=internal_at)
    interior = replace(shared, anchor_tick=180, invalidation_tick=180, anchor_at=internal_at,
                       divergence=replace(shared.divergence, anchor_tick=180, anchor_at=internal_at))
    from types import SimpleNamespace
    # Test selection against the changed price path without reusing the old
    # path's center/trend proofs as if those also described this counterexample.
    assert engine._first_retest_units(SimpleNamespace(units=tuple(units)), interior) is None


def test_consolidation_uses_the_same_composite_extreme_rule(monkeypatch):
    import sys
    values = (*UP_VALUES[:12], ("up", 135, 145))
    monkeypatch.setattr(sys.modules[__name__], "UP_VALUES", values)
    for direction in ("up", "down"):
        structure, _, strength = consolidation_structure(direction)
        divergence = only_point(collect_formal_divergence_ledger(structure))
        assert divergence.kind == "consolidation"
        assert divergence.signal_unit_id == "u-12"
        assert divergence.price_anchor_unit_id in {"u-10", "u-11"}
        assert divergence.anchor_tick == 150
        assert divergence.available_at >= structure.levels[0].units[-1].available_at


def test_first_class_model_cannot_accept_consolidation_evidence():
    strength = divergent_strength("down")
    structure, _ = structure_from_values(direction="down", strength=strength)
    point = only_point(engine_for(structure, strength).first_class_points())
    consolidation, _, _ = consolidation_structure("down")
    divergence = only_point(collect_formal_divergence_ledger(consolidation))
    with pytest.raises(ValueError, match="盘整背驰不是一类点"):
        replace(point, divergence=divergence)


def test_trend_divergence_requires_terminal_center_third_class_departure():
    for direction in ("up", "down"):
        from chanlun.core.strict_structure.models import CenterState
        structure, assembly = structure_from_values(direction=direction)
        trend = target_trend(assembly)
        center = trend.centers[-1]
        pending = replace(center, state=CenterState.ONGOING,
                          pending_leave_unit=center.completion_leave_unit,
                          completion_leave_unit=None, completion_return_unit=None, completed_at=None)
        assert compare_terminal_trend_divergence(
            (*trend.centers[:-1], pending), structure.levels[0].units, StrengthTable({}),
            trend_start_unit_id=trend.constituent_units[0].unit_id,
        ) is None


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
