"""Entry/body/leave ownership must remain mutually exclusive."""

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import CenterEventKind, CenterState, SourceKind
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def test_entry_and_leave_are_external_to_extended_segment_body() -> None:
    values = valid_five_up_exit()
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    assert center.pending_leave_unit is values[4]

    reentry = unit(5, "down", values[4].end_tick, 110)
    center, event = advance_center(center, reentry)
    assert event.kind is CenterEventKind.EXTENDED

    leave = unit(6, "up", 110, 145)
    leaving, event = advance_center(center, leave)

    assert leaving.state is CenterState.ONGOING
    assert leaving.entry_unit is values[0]
    assert leaving.core_units == values[1:4]
    assert leaving.initial_units == values[1:4]
    assert leaving.body_units == values[1:4] + (values[4], reentry)
    assert leaving.initial_exit_unit is values[4]
    assert leaving.pending_leave_unit is leave
    assert leaving.entry_unit not in leaving.body_units
    assert leave not in leaving.body_units
    assert leaving.entry_unit.direction == leave.direction
    assert event.kind is CenterEventKind.BREAKOUT_WATCH_UP


def test_same_direction_successor_is_rejected_before_leave_classification() -> None:
    entry = unit(-1, "down", 130, 90, source_kind=SourceKind.TREND_TYPE)
    body = (
        unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE),
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(2, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
    )
    center = establish_center(
        body, 0, SourceKind.TREND_TYPE, entry_unit=entry
    )
    assert center is not None
    opposite = unit(3, "up", 115, 140, source_kind=SourceKind.TREND_TYPE)

    with pytest.raises(ValueError, match="transition must alternate"):
        advance_center(center, opposite)
