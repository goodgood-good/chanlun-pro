from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterEvidence,
    ConstituentUnit,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    BASE,
    TEST_PRICE_BASIS,
    completed_up_center,
    ongoing_center,
    unit,
    valid_three_center_seed,
)


def test_constituent_unit_rejects_inverted_range():
    with pytest.raises(ValueError, match="low_tick must be <= high_tick"):
        ConstituentUnit(
            unit_id="bad",
            structural_level=0,
            source_kind="segment",
            price_basis_revision="test-raw-v1",
            direction="up",
            start_tick=100,
            end_tick=110,
            low_tick=120,
            high_tick=110,
            market_start=BASE,
            market_end=BASE,
            confirmed_at=BASE,
            available_at=BASE,
            locked=True,
            child_ids=(),
        )


def test_constituent_unit_is_immutable():
    value = unit(0, "up", 90, 110)
    with pytest.raises(FrozenInstanceError):
        value.end_tick = 120


def test_constituent_unit_requires_directional_endpoint_progression():
    with pytest.raises(ValueError, match="up unit must not end below start"):
        replace(unit(0, "down", 110, 90), direction="up")
    with pytest.raises(ValueError, match="down unit must not end above start"):
        replace(unit(0, "up", 90, 110), direction="down")


def test_constituent_unit_lock_and_confirmation_are_bijective():
    with pytest.raises(ValueError, match="locked and confirmed_at must agree"):
        replace(unit(0, "up", 90, 110), confirmed_at=None)
    with pytest.raises(ValueError, match="locked and confirmed_at must agree"):
        replace(unit(0, "up", 90, 110, locked=False), confirmed_at=BASE)


def test_constituent_unit_requires_price_basis_revision():
    with pytest.raises(ValueError, match="price_basis_revision is required"):
        replace(unit(0, "up", 90, 110), price_basis_revision="")


def test_constituent_unit_requires_integer_structure_ticks():
    with pytest.raises(TypeError, match="ticks must be integers"):
        replace(unit(0, "up", 90, 110), end_tick=110.5)
    with pytest.raises(TypeError, match="ticks must be integers"):
        replace(unit(0, "up", 90, 110), low_tick=True)


def test_constituent_unit_enforces_market_confirmation_and_availability_order():
    value = unit(0, "up", 90, 110)
    with pytest.raises(ValueError, match="market_end must not precede market_start"):
        replace(value, market_end=value.market_start - timedelta(minutes=1))
    with pytest.raises(ValueError, match="confirmed_at must not precede market_end"):
        replace(value, confirmed_at=value.market_end - timedelta(minutes=1))
    with pytest.raises(ValueError, match="available_at must not precede confirmed_at"):
        replace(value, available_at=value.confirmed_at - timedelta(seconds=1))


def test_constituent_unit_normalizes_enum_and_child_ids_to_immutable_values():
    value = replace(
        unit(0, "up", 90, 110),
        source_kind="segment",
        child_ids=["child-1", "child-2"],
    )
    assert value.source_kind is SourceKind.SEGMENT
    assert value.child_ids == ("child-1", "child-2")


def test_stable_structure_id_is_deterministic_and_namespaced():
    first = stable_structure_id(
        "unit", SourceKind.SEGMENT, BASE, ("a", 1), "test-raw-v1"
    )
    second = stable_structure_id(
        "unit", "segment", BASE, ("a", 1), "test-raw-v1"
    )
    assert first == second
    assert first != stable_structure_id(
        "center", SourceKind.SEGMENT, BASE, ("a", 1), "test-raw-v1"
    )
    assert len(first) == 64


def test_trend_center_rejects_unlocked_initial_or_body_unit():
    value = ongoing_center()
    changed = replace(value.initial_units[2], locked=False, confirmed_at=None)
    initial = value.initial_units[:2] + (changed,)
    with pytest.raises(ValueError, match="formal center body units must be locked"):
        replace(
            value,
            initial_units=initial,
            body_units=initial + value.extension_units,
        )


def test_trend_center_requires_exactly_three_initial_units():
    value = ongoing_center()
    initial = value.initial_units[:2]
    with pytest.raises(ValueError, match="exactly three units"):
        replace(value, initial_units=initial, body_units=initial)


def test_trend_center_rejects_body_not_equal_to_initial_plus_extensions():
    value = ongoing_center()
    with pytest.raises(ValueError, match="initial plus extension units"):
        replace(value, body_units=value.body_units[:-1])


def test_ongoing_center_pending_leave_must_be_final_body_unit():
    value = ongoing_center()
    with pytest.raises(ValueError, match="pending leave must be the final body unit"):
        replace(value, pending_leave_unit=value.entry_unit)


def test_center_departure_is_not_constrained_by_a_fictitious_external_entry():
    seed = valid_three_center_seed()
    value = establish_center(seed, 0, SourceKind.SEGMENT)
    assert value is not None
    downward_leave = unit(3, "down", seed[-1].end_tick, 95)

    pending, _event = advance_center(value, downward_leave)

    assert pending.pending_leave_unit is downward_leave
    assert pending.entry_unit.direction == "up"
    assert pending.pending_leave_unit.direction == "down"


def test_completed_center_requires_atomic_locked_leave_return_and_timestamp():
    value = completed_up_center()
    assert value.completed_at == value.completion_return_unit.confirmed_at
    with pytest.raises(ValueError, match="requires leave, return and completed_at"):
        replace(value, completion_return_unit=None)
    with pytest.raises(ValueError, match="requires leave, return and completed_at"):
        replace(value, completed_at=None)


def test_completion_return_is_confirmation_evidence_not_center_body():
    value = completed_up_center()
    with pytest.raises(ValueError, match="completion return must not enter center body"):
        replace(value, completion_return_unit=value.entry_unit)


def test_center_rejects_body_level_or_source_mismatch():
    value = ongoing_center()
    bad = tuple(replace(item, structural_level=1) for item in value.initial_units)
    with pytest.raises(ValueError, match="center body level/source mismatch"):
        replace(
            value,
            initial_units=bad,
            body_units=bad + value.extension_units,
        )


def test_center_rejects_mixed_price_basis():
    value = ongoing_center()
    bad_last = replace(
        value.initial_units[-1],
        price_basis_revision="post-action-v2",
    )
    initial = value.initial_units[:-1] + (bad_last,)
    with pytest.raises(ValueError, match="center body price basis mismatch"):
        replace(
            value,
            initial_units=initial,
            body_units=initial + value.extension_units,
        )


def test_center_rejects_disconnected_or_non_alternating_body():
    value = ongoing_center()
    disconnected = replace(
        value.initial_units[1],
        start_tick=value.initial_units[1].start_tick - 1,
    )
    initial = (value.initial_units[0], disconnected) + value.initial_units[2:]
    with pytest.raises(ValueError, match="center body prices must connect"):
        replace(
            value,
            initial_units=initial,
            body_units=initial + value.extension_units,
        )

    non_alternating = replace(
        value.initial_units[1],
        direction="up",
        start_tick=value.initial_units[1].end_tick,
        end_tick=value.initial_units[1].start_tick,
    )
    initial = (value.initial_units[0], non_alternating) + value.initial_units[2:]
    with pytest.raises(ValueError, match="center body directions must alternate"):
        replace(
            value,
            initial_units=initial,
            body_units=initial + value.extension_units,
        )


def test_center_rejects_forged_core_or_envelope_fields():
    value = ongoing_center()
    with pytest.raises(ValueError, match="first-three intersection"):
        replace(value, zg_tick=value.zg_tick + 1)
    with pytest.raises(ValueError, match="body envelope"):
        replace(value, gg_tick=value.gg_tick + 1)


def test_center_core_body_time_uses_first_component_and_excludes_departure():
    ongoing = ongoing_center()
    completed = completed_up_center()

    assert (
        ongoing.core_body_start_market_time
        == ongoing.core_units[0].market_start
    )
    assert (
        ongoing.core_body_end_market_time
        == ongoing.pending_leave_unit.market_start
    )
    assert (
        completed.core_body_end_market_time
        == completed.completion_leave_unit.market_start
    )


def test_center_core_body_end_advances_only_after_an_accepted_reentry():
    initial = ongoing_center()
    reentry = unit(
        5,
        "down",
        initial.body_units[-1].end_tick,
        initial.zd_tick + 5,
    )
    extended, _event = advance_center(initial, reentry)

    assert extended.pending_leave_unit is None
    assert extended.core_body_end_market_time == reentry.market_end

    next_leave = unit(
        6,
        "up",
        reentry.end_tick,
        initial.zg_tick + 15,
    )
    leaving, _event = advance_center(extended, next_leave)

    assert leaving.pending_leave_unit is next_leave
    assert leaving.core_body_end_market_time == next_leave.market_start


def test_center_evidence_preserves_v4_first_three_and_excludes_return():
    value = completed_up_center()
    evidence = CenterEvidence.from_center(value)
    assert evidence.schema_version == "chanlun-center/v4"
    assert evidence.price_basis_revision == TEST_PRICE_BASIS
    assert evidence.initial_unit_ids == tuple(
        item.unit_id for item in value.initial_units
    )
    assert evidence.entry_unit_id == value.entry_unit.unit_id
    assert evidence.core_unit_ids == tuple(item.unit_id for item in value.core_units)
    assert evidence.initial_exit_unit_id == value.initial_exit_unit.unit_id
    assert evidence.completion_leave_unit_id in evidence.body_unit_ids
    assert evidence.completion_return_unit_id not in evidence.body_unit_ids
    assert evidence.completed_at == value.completed_at
    assert evidence.tradable is True
