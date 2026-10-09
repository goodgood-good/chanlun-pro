"""Initial five roles are immutable; final lifecycle departures are separate."""
from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.identity import build_center_id
from chanlun.core.strict_structure.models import CenterState, SourceKind
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def mirrored(value, mirror):
    if not mirror:
        return value
    return replace(value, direction="down" if value.direction == "up" else "up",
                   start_tick=300-value.start_tick, end_tick=300-value.end_tick,
                   low_tick=300-value.high_tick, high_tick=300-value.low_tick)


def changed_establishment(center, leave):
    # Supply a matching identity and maturity clock: rejection must come from
    # the actual five-role contract, not a stale center ID or timestamp.
    identity = build_center_id(
        price_basis_revision=center.price_basis_revision,
        structural_level=center.structural_level, source_kind=center.source_kind.value,
        entry_unit_id=center.entry_unit.unit_id,
        initial_unit_ids=tuple(u.unit_id for u in center.initial_units),
        establishment_leave_unit_id=leave.unit_id,
        zd_tick=center.zd_tick, zg_tick=center.zg_tick,
        formation_rule=center.formation_rule,
    )
    return replace(center, establishment_leave_unit=leave, center_id=identity,
                   established_market_time=leave.market_end,
                   established_at=leave.confirmed_at)


@pytest.mark.parametrize("mirror", [False, True])
def test_opposite_final_departure_cannot_replace_initial_fifth_role(mirror):
    seed = tuple(mirrored(u, mirror) for u in valid_five_up_exit())
    center = establish_center(seed, 0, SourceKind.SEGMENT)
    leave = mirrored(unit(5, "down", 130, 90), mirror)
    ret = mirrored(unit(6, "up", 90, 100), mirror)
    center, _ = advance_center(center, leave)
    center, _ = advance_center(center, ret)
    assert center.state is CenterState.COMPLETED
    assert center.establishment_units == seed
    assert center.completion_leave_unit == leave
    assert center.entry_unit.direction != leave.direction
    assert center.third_class_confirmed
    # Before this fix the connected history [0..6] allowed a false seed
    # [0,1,2,3,5], whose entry and purported initial departure opposed.
    with pytest.raises(ValueError, match="establishment entry and leave must share direction"):
        changed_establishment(center, leave)


@pytest.mark.parametrize("mirror", [False, True])
def test_later_same_direction_departure_cannot_skip_initial_roles(mirror):
    seed = tuple(mirrored(u, mirror) for u in valid_five_up_exit())
    center = establish_center(seed, 0, SourceKind.SEGMENT)
    failed_return = mirrored(unit(5, "down", 130, 105), mirror)
    leave = mirrored(unit(6, "up", 105, 140), mirror)
    ret = mirrored(unit(7, "down", 140, 130), mirror)
    for value in (failed_return, leave, ret):
        center, _ = advance_center(center, value)
    assert center.state is CenterState.COMPLETED
    assert center.entry_unit.direction == leave.direction
    assert center.initial_units[-1].end_tick == leave.start_tick
    assert center.establishment_units == seed
    # Matching direction and price alone cannot certify the seed: replacing
    # role4 with role6 skips two physical units while keeping a valid history.
    with pytest.raises(ValueError, match="establishment role market endpoints must connect"):
        changed_establishment(center, leave)
