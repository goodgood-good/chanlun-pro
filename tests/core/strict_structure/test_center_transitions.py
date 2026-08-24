from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    ongoing_center,
    ongoing_down_center,
    unit,
)


def _ongoing_up_center():
    return ongoing_center()


def _ongoing_down_center():
    return ongoing_down_center()


def test_locked_return_into_core_extends_without_moving_core():
    value = _ongoing_up_center()
    ret = unit(5, "down", 130, 110)
    updated, event = advance_center(value, ret)
    assert updated.state is CenterState.ONGOING
    assert updated.pending_leave_unit is None
    assert updated.failed_departure_units == (value.pending_leave_unit,)
    assert updated.extension_units == value.extension_units + (ret,)
    assert updated.body_units == value.body_units + (ret,)
    assert updated.center_id == value.center_id
    assert (updated.zd_tick, updated.zg_tick) == (105, 115)
    assert updated.body_revision == value.body_revision + 1
    assert event.kind is CenterEventKind.EXTENDED


def test_each_extension_event_has_distinct_trigger_identity():
    value = _ongoing_up_center()
    first, first_event = advance_center(value, unit(5, "down", 130, 110))
    second, second_event = advance_center(first, unit(6, "up", 110, 112))

    assert first_event.kind is second_event.kind is CenterEventKind.EXTENDED
    assert first_event.market_time != second_event.market_time
    assert first_event.event_id != second_event.event_id
    assert second.body_revision > first.body_revision


def test_locked_return_outside_completes_center():
    value = _ongoing_up_center()
    ret = unit(5, "down", 130, 120)
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.pending_leave_unit is None
    assert completed.completion_leave_unit is value.pending_leave_unit
    assert completed.completion_return_unit is ret
    assert ret not in completed.body_units
    assert completed.completed_at == ret.confirmed_at
    assert event.kind is CenterEventKind.COMPLETED_UP
    assert event.available_at == ret.available_at


def test_return_touching_zg_is_completion_not_extension():
    value = _ongoing_up_center()
    ret = unit(5, "down", 130, value.zg_tick)
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_return_unit.low_tick == value.zg_tick
    assert event.kind is CenterEventKind.COMPLETED_UP


def test_down_leave_and_outside_return_complete_down_center():
    value = _ongoing_down_center()
    ret = unit(5, "up", 80, 90)
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_direction == "down"
    assert completed.completion_leave_unit is value.pending_leave_unit
    assert completed.completion_return_unit is ret
    assert event.kind is CenterEventKind.COMPLETED_DOWN


def test_return_extension_then_new_leave_keeps_core_and_emits_watch():
    value = _ongoing_up_center()
    entered = unit(5, "down", 130, 110)
    extended, first_event = advance_center(value, entered)
    leave = unit(6, "up", 110, 135)
    pending, watch = advance_center(extended, leave)

    assert first_event.kind is CenterEventKind.EXTENDED
    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is leave
    assert pending.failed_departure_units == (value.pending_leave_unit,)
    assert pending.extension_units == value.extension_units + (entered,)
    assert leave not in pending.body_units
    assert pending.body_revision == value.body_revision + 1
    assert pending.center_id == value.center_id
    assert (pending.zd_tick, pending.zg_tick) == (105, 115)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP

    ret = unit(7, "down", 135, 120)
    completed, completion = advance_center(pending, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is leave
    assert completed.completion_return_unit is ret
    assert completion.kind is CenterEventKind.COMPLETED_UP


def test_pending_leave_availability_cannot_be_hidden_by_older_snapshot_time():
    value = _ongoing_up_center()
    entered = unit(5, "down", 130, 110)
    extended, _event = advance_center(value, entered)
    late = extended.available_at + timedelta(days=1)
    leave = replace(
        unit(6, "up", 110, 135),
        confirmed_at=late,
        available_at=late,
    )
    pending, _watch = advance_center(extended, leave)

    assert pending.available_at == late
    with pytest.raises(
        ValueError,
        match="center availability must cover pending leave evidence",
    ):
        replace(pending, available_at=extended.available_at)


def test_opposite_down_crossing_reuses_return_as_new_leave():
    value = _ongoing_up_center()
    crossed = unit(5, "down", 130, 95)
    pending, event = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is crossed
    assert pending.extension_units == value.extension_units
    assert pending.failed_departure_units == (value.pending_leave_unit,)
    assert crossed.direction != value.pending_leave_unit.direction
    assert event.kind is CenterEventKind.BREAKOUT_WATCH_DOWN

    completed, completion = advance_center(
        pending,
        unit(6, "up", 95, 100),
    )
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is crossed
    assert completion.kind is CenterEventKind.COMPLETED_DOWN


def test_opposite_up_crossing_reuses_return_as_new_leave():
    value = _ongoing_down_center()
    crossed = unit(5, "up", 80, 115)
    pending, event = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is crossed
    assert pending.extension_units == value.extension_units
    assert pending.failed_departure_units == (value.pending_leave_unit,)
    assert crossed.direction != value.pending_leave_unit.direction
    assert event.kind is CenterEventKind.BREAKOUT_WATCH_UP

    completed, completion = advance_center(
        pending,
        unit(6, "down", 115, 110),
    )
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is crossed
    assert completion.kind is CenterEventKind.COMPLETED_UP


def test_transition_rejects_unlocked_cross_context_and_duplicate_evidence():
    value = _ongoing_up_center()
    unlocked = unit(5, "down", 130, 120, locked=False)
    with pytest.raises(ValueError, match="formal center transition must be locked"):
        advance_center(value, unlocked)

    rebased = replace(
        unit(5, "down", 130, 120),
        price_basis_revision="post-action",
    )
    with pytest.raises(ValueError, match="transition price basis mismatch"):
        advance_center(value, rebased)

    wrong_level = replace(unit(5, "down", 130, 120), structural_level=1)
    with pytest.raises(ValueError, match="transition level/source mismatch"):
        advance_center(value, wrong_level)

    duplicate = replace(
        unit(5, "down", 130, 120),
        unit_id=value.core_units[0].unit_id,
    )
    with pytest.raises(ValueError, match="unit id already belongs to center"):
        advance_center(value, duplicate)


def test_completed_center_rejects_further_transition():
    value = _ongoing_up_center()
    completed, _event = advance_center(value, unit(5, "down", 130, 120))
    with pytest.raises(ValueError, match="completed center cannot transition"):
        advance_center(completed, unit(6, "up", 120, 140))


def test_transition_event_retains_price_basis_revision():
    value = _ongoing_up_center()
    _completed, event = advance_center(value, unit(5, "down", 130, 120))
    assert event.price_basis_revision == TEST_PRICE_BASIS
