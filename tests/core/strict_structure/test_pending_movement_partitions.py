from __future__ import annotations

from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import establish_center
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    PendingMovementRole,
    SourceKind,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def _unit_ids(values) -> tuple[str, ...]:
    return tuple(item.unit_id for item in values)


def _replace_pending_identity(pending, **changes):
    values = {
        "role": pending.role,
        "constituent_units": pending.constituent_units,
        "left_trend_id": pending.left_trend_id,
        "right_trend_id": pending.right_trend_id,
        "left_boundary_unit_id": pending.left_boundary_unit_id,
        "right_boundary_unit_id": pending.right_boundary_unit_id,
        **changes,
    }
    partition_id = "sha256:" + stable_structure_id(
        "chanlun-pending-movement",
        pending.price_basis_revision,
        pending.structural_level,
        pending.source_kind.value,
        values["role"].value,
        tuple(item.unit_id for item in values["constituent_units"]),
        values["left_trend_id"],
        values["right_trend_id"],
        values["left_boundary_unit_id"],
        values["right_boundary_unit_id"],
    )
    return replace(pending, partition_id=partition_id, **changes)


def test_centerless_stream_is_one_non_formal_pending_movement() -> None:
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 125),
        unit(3, "down", 125, 105),
    )

    result = assemble_trend_types((), values, 0)

    assert result.current_trends == ()
    assert result.completed_trends == ()
    assert len(result.pending_movements) == 1
    pending = result.pending_movements[0]
    assert pending.role is PendingMovementRole.ENTIRE_STREAM
    assert pending.constituent_units == values
    assert pending.left_trend_id is None
    assert pending.right_trend_id is None
    assert pending.state == "pending"
    assert pending.classification == "unresolved"
    assert pending.tradable is False
    assert pending.recursive_eligible is False
    assert pending.divergence_eligible is False


def test_single_unit_pending_movement_uses_its_own_availability() -> None:
    value = unit(0, "up", 90, 120)

    pending = assemble_trend_types((), (value,), 0).pending_movements

    assert len(pending) == 1
    assert pending[0].constituent_units == (value,)
    assert pending[0].available_at == value.available_at


def test_terminal_unresolved_suffix_has_exclusive_unit_ownership() -> None:
    center_units = valid_five_up_exit()
    center = establish_center(center_units, 0, SourceKind.SEGMENT)
    assert center is not None
    tail = (
        unit(5, "down", center_units[-1].end_tick, 120),
        unit(6, "up", 120, 128),
    )
    values = (*center_units, *tail)

    result = assemble_trend_types((center,), values, 0)

    assert len(result.current_trends) == 1
    assert len(result.pending_movements) == 1
    trend = result.current_trends[0]
    pending = result.pending_movements[0]
    assert pending.role is PendingMovementRole.SUFFIX
    assert pending.constituent_units == tail
    assert pending.left_trend_id == trend.trend_id
    assert pending.right_trend_id is None
    assert pending.left_boundary_unit_id == trend.terminal_unit.unit_id
    assert pending.right_boundary_unit_id is None

    formal_ids = set(_unit_ids(trend.constituent_units))
    pending_ids = set(_unit_ids(pending.constituent_units))
    assert formal_ids.isdisjoint(pending_ids)
    assert formal_ids | pending_ids == set(_unit_ids(values))


def test_pending_boundary_must_be_owned_by_the_referenced_formal_trend() -> None:
    center_units = valid_five_up_exit()
    center = establish_center(center_units, 0, SourceKind.SEGMENT)
    assert center is not None
    tail = (
        unit(5, "down", center_units[-1].end_tick, 120),
        unit(6, "up", 120, 128),
    )
    result = assemble_trend_types((center,), (*center_units, *tail), 0)
    trend = result.current_trends[0]
    pending = result.pending_movements[0]
    forged = _replace_pending_identity(
        pending,
        left_boundary_unit_id=trend.constituent_units[0].unit_id,
    )

    with pytest.raises(ValueError, match="left boundary does not match"):
        replace(result, pending_movements=(forged,))


def test_pending_identity_and_availability_are_deterministic() -> None:
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 125),
    )

    first = assemble_trend_types((), values, 0).pending_movements
    second = assemble_trend_types((), values, 0).pending_movements

    assert first == second
    assert first[0].available_at == max(item.available_at for item in values)
    assert first[0].partition_id.startswith("sha256:")
