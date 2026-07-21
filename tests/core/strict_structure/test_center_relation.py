from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import CenterRelation, SourceKind
from tests.core.strict_structure.helpers import unit


def relation_center(
    center_id,
    unit_offset,
    zd,
    zg,
    dd,
    gg,
    *,
    structural_level=0,
    source_kind=SourceKind.SEGMENT,
    price_basis_revision="test-raw-v1",
):
    initial = tuple(
        replace(item, price_basis_revision=price_basis_revision)
        for item in (
            unit(
                unit_offset,
                "up",
                dd,
                gg,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                unit_offset + 1,
                "down",
                gg,
                zd,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                unit_offset + 2,
                "up",
                zd,
                zg,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                unit_offset + 3,
                "down",
                zg,
                zd,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
            unit(
                unit_offset + 4,
                "up",
                zd,
                gg,
                structural_level=structural_level,
                source_kind=source_kind,
            ),
        )
    )
    value = establish_center(initial, structural_level, source_kind)
    assert value is not None
    assert (value.zd_tick, value.zg_tick) == (zd, zg)
    assert (value.dd_tick, value.gg_tick) == (dd, gg)
    return replace(value, center_id=center_id)


def test_core_overlap_after_previous_center_is_frozen_is_upgrade():
    assert classify_center_relation(
        relation_center("a", 0, 100, 110, 90, 120),
        relation_center("b", 10, 105, 115, 95, 125),
    ) is CenterRelation.UPGRADE


def test_full_wave_separation_is_uptrend():
    assert classify_center_relation(
        relation_center("a", 0, 100, 110, 90, 120),
        relation_center("b", 10, 131, 140, 130, 150),
    ) is CenterRelation.UP_TREND


def test_full_wave_separation_is_downtrend():
    assert classify_center_relation(
        relation_center("a", 0, 130, 140, 120, 150),
        relation_center("b", 10, 90, 100, 80, 110),
    ) is CenterRelation.DOWN_TREND


def test_separated_cores_with_touching_envelopes_are_upgrade():
    assert classify_center_relation(
        relation_center("a", 0, 100, 110, 90, 120),
        relation_center("b", 10, 121, 130, 120, 140),
    ) is CenterRelation.UPGRADE


def test_cores_touching_at_one_tick_are_upgrade():
    assert classify_center_relation(
        relation_center("a", 0, 100, 110, 90, 120),
        relation_center("b", 10, 110, 120, 100, 130),
    ) is CenterRelation.UPGRADE


def test_extension_changes_envelope_revision_but_never_fixed_core_or_identity():
    value = relation_center("a", 0, 100, 110, 90, 120)
    entered = replace(unit(5, "down", 120, 105), low_tick=80)
    updated, _event = advance_center(value, entered)
    assert updated.center_id == value.center_id
    assert (updated.zd_tick, updated.zg_tick) == (100, 110)
    assert (updated.dd_tick, updated.gg_tick) == (80, 120)
    assert updated.body_revision == 1


def test_relation_rejects_cross_basis_or_cross_level_centers():
    previous = relation_center("a", 0, 100, 110, 90, 120)
    rebased = relation_center(
        "b",
        10,
        131,
        140,
        130,
        150,
        price_basis_revision="post-action-v2",
    )
    with pytest.raises(ValueError, match="same price basis"):
        classify_center_relation(previous, rebased)

    higher = relation_center(
        "higher",
        10,
        131,
        140,
        130,
        150,
        structural_level=1,
    )
    with pytest.raises(ValueError, match="same level and source"):
        classify_center_relation(previous, higher)


def test_relation_rejects_duplicate_or_non_ordered_centers():
    previous = relation_center("a", 10, 100, 110, 90, 120)
    duplicate = replace(previous)
    with pytest.raises(ValueError, match="distinct identities"):
        classify_center_relation(previous, duplicate)

    earlier = relation_center("b", 0, 131, 140, 130, 150)
    with pytest.raises(ValueError, match="strictly time ordered"):
        classify_center_relation(previous, earlier)
