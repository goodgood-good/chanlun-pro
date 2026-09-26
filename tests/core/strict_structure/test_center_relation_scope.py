"""Theorem-two center inequalities require strictly separated frozen cores."""

from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers, establish_center
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import CenterRelation, CenterState, SourceKind, TrendState
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.unit_adapter import build_recursive_unit_stream
from tests.core.strict_structure.helpers import unit


def _center(first_index, legs):
    values = tuple(
        unit(first_index + offset, direction, start, end,
             source_kind=SourceKind.TREND_TYPE, structural_level=1)
        for offset, (direction, start, end) in enumerate(legs)
    )
    center = establish_center(values, 1, SourceKind.TREND_TYPE)
    assert center is not None
    return center


PREVIOUS = (
    ("up", 80, 120),
    ("down", 120, 100),
    ("up", 100, 120),
)


@pytest.mark.parametrize(
    "later,expected",
    [
        (
            (("up", 130, 140), ("down", 140, 135), ("up", 135, 150)),
            CenterRelation.UP_TREND,
        ),
        (
            (("down", 75, 55), ("up", 55, 70), ("down", 70, 60)),
            CenterRelation.DOWN_TREND,
        ),
        # Equal outer extrema belong to the higher-center relation side only
        # when the immutable cores themselves are strictly separated.
        (
            (("up", 120, 140), ("down", 140, 130), ("up", 130, 150)),
            CenterRelation.UPGRADE,
        ),
        (
            (("down", 80, 55), ("up", 55, 70), ("down", 70, 60)),
            CenterRelation.UPGRADE,
        ),
        (
            (("up", 110, 140), ("down", 140, 120), ("up", 120, 140)),
            CenterRelation.RECOMPOSITION_PENDING,
        ),
        (
            (("up", 110, 130), ("down", 130, 110), ("up", 110, 120)),
            CenterRelation.RECOMPOSITION_PENDING,
        ),
    ],
)
def test_theorem_two_relation_does_not_label_core_contact_as_upgrade(later, expected):
    previous = _center(0, PREVIOUS)
    current = _center(10, later)

    assert classify_center_relation(previous, current) is expected


def test_touching_successor_after_boundary_third_sell_waits_for_parent_members():
    source = SourceKind.TREND_TYPE
    prices = (
        ("up", 80, 120), ("down", 120, 100), ("up", 100, 120),
        ("down", 120, 80), ("up", 80, 90), ("down", 90, 80),
        ("up", 80, 100), ("down", 100, 60), ("up", 60, 70),
    )
    values = [
        unit(i, direction, start, end, source_kind=source, structural_level=1)
        for i, (direction, start, end) in enumerate(prices)
    ]
    values[4] = replace(values[4], high_tick=100)
    values[5] = replace(values[5], high_tick=100)
    values[8] = replace(values[8], high_tick=80)

    first = calculate_centers(tuple(values[:5]), 1, source).centers
    assert len(first) == 1 and first[0].state is CenterState.COMPLETED
    assert first[0].completion_return_unit.high_tick == first[0].zd_tick

    for length, successor_state in ((7, CenterState.ONGOING),
                                    (9, CenterState.COMPLETED)):
        prefix = tuple(values[:length])
        centers = calculate_centers(prefix, 1, source).centers
        assert len(centers) == 2 and centers[1].state is successor_state
        assert classify_center_relation(*centers) is CenterRelation.RECOMPOSITION_PENDING
        assembly = assemble_trend_types(centers, prefix, 1)
        assert all(not trend.locked for trend in assembly.current_trends)
        parent_units, _ = build_recursive_unit_stream(assembly.current_trends)
        assert all(not item.locked for item in parent_units)
        assert len(parent_units) < 3
        if length == 9:
            assert len(assembly.current_trends) == 1
            assert assembly.current_trends[0].state is TrendState.COMPLETE
