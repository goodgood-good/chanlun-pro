from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


def _ongoing_up_center():
    value = establish_center(valid_five_up_exit(), 0, SourceKind.SEGMENT)
    assert value is not None
    return value


def _ongoing_down_center():
    initial = (
        unit(0, "down", 120, 90),
        unit(1, "up", 90, 110),
        unit(2, "down", 110, 95),
        unit(3, "up", 95, 105),
        unit(4, "down", 105, 80),
    )
    value = establish_center(initial, 0, SourceKind.SEGMENT)
    assert value is not None
    assert (value.zd_tick, value.zg_tick) == (95, 105)
    return value


def test_locked_return_into_core_extends_without_moving_core():
    value = _ongoing_up_center()
    ret = unit(5, "down", 130, 110)
    updated, event = advance_center(value, ret)
    assert updated.state is CenterState.ONGOING
    assert updated.pending_leave_unit is None
    assert updated.extension_units == (ret,)
    assert updated.body_units == value.body_units + (ret,)
    assert updated.center_id == value.center_id
    assert (updated.zd_tick, updated.zg_tick) == (105, 115)
    assert updated.body_revision == 1
    assert event.kind is CenterEventKind.EXTENDED


def test_locked_return_outside_completes_center():
    value = _ongoing_up_center()
    ret = unit(5, "down", 130, 120)
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.pending_leave_unit is None
    assert completed.completion_leave_unit is value.initial_exit_unit
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
    assert completed.completion_leave_unit is value.initial_exit_unit
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
    assert pending.extension_units == (entered, leave)
    assert pending.body_revision == 2
    assert pending.center_id == value.center_id
    assert (pending.zd_tick, pending.zg_tick) == (105, 115)
    assert watch.kind is CenterEventKind.BREAKOUT_WATCH_UP

    ret = unit(7, "down", 135, 120)
    completed, completion = advance_center(pending, ret)
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit is leave
    assert completed.completion_return_unit is ret
    assert completion.kind is CenterEventKind.COMPLETED_UP


def test_return_crossing_core_is_not_an_opposite_down_leave():
    value = _ongoing_up_center()
    crossed = unit(5, "down", 130, 95)
    pending, event = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is None
    assert pending.extension_units == (crossed,)
    assert event.kind is CenterEventKind.EXTENDED

    ret = unit(6, "up", 95, 100)
    with pytest.raises(ValueError, match="ongoing center unit must re-enter"):
        advance_center(pending, ret)


def test_return_crossing_core_is_not_an_opposite_up_leave():
    value = _ongoing_down_center()
    crossed = unit(5, "up", 80, 115)
    pending, event = advance_center(value, crossed)

    assert pending.state is CenterState.ONGOING
    assert pending.pending_leave_unit is None
    assert pending.extension_units == (crossed,)
    assert event.kind is CenterEventKind.EXTENDED

    ret = unit(6, "down", 115, 110)
    with pytest.raises(ValueError, match="ongoing center unit must re-enter"):
        advance_center(pending, ret)


def test_transition_rejects_unlocked_cross_context_and_duplicate_evidence():
    value = _ongoing_up_center()
    unlocked = unit(5, "down", 130, 120, locked=False)
    with pytest.raises(ValueError, match="formal center transition must be locked"):
        advance_center(value, unlocked)

    rebased = replace(
        unit(5, "down", 130, 120),
        price_basis_revision="post-action-v2",
    )
    with pytest.raises(ValueError, match="transition price basis mismatch"):
        advance_center(value, rebased)

    wrong_level = replace(unit(5, "down", 130, 120), structural_level=1)
    with pytest.raises(ValueError, match="transition level/source mismatch"):
        advance_center(value, wrong_level)

    duplicate = replace(unit(5, "down", 130, 120), unit_id=value.entry_unit.unit_id)
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
