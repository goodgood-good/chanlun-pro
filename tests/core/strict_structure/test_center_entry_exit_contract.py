"""进入、核心本体和离开角色保持互斥。"""

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import CenterEventKind, CenterState, SourceKind
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def test_entry_and_departure_are_external_to_extended_center_body() -> None:
    values = valid_five_up_exit()
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None

    reentry = unit(5, "down", values[4].end_tick, 110)
    extended, event = advance_center(center, reentry)
    assert event.kind is CenterEventKind.EXTENDED

    next_leave = unit(6, "up", reentry.end_tick, 145)
    leaving_again, event = advance_center(extended, next_leave)

    assert leaving_again.state is CenterState.ONGOING
    assert leaving_again.entry_unit is values[0]
    assert leaving_again.core_units == values[1:4]
    assert leaving_again.initial_units == values[1:4]
    assert leaving_again.body_units == values[1:4] + (reentry,)
    assert leaving_again.failed_departure_units == (values[4],)
    assert leaving_again.pending_leave_unit is next_leave
    assert values[0] not in leaving_again.body_units
    assert values[4] not in leaving_again.body_units
    assert next_leave not in leaving_again.body_units
    assert event.kind is CenterEventKind.BREAKOUT_WATCH_UP


def test_same_direction_successor_is_rejected_before_leave_classification() -> None:
    entry = unit(-1, "down", 130, 90, source_kind=SourceKind.TREND_TYPE)
    body = (
        unit(0, "up", 90, 120, source_kind=SourceKind.TREND_TYPE),
        unit(1, "down", 120, 100, source_kind=SourceKind.TREND_TYPE),
        unit(2, "up", 100, 115, source_kind=SourceKind.TREND_TYPE),
    )
    center = establish_center(
        body,
        0,
        SourceKind.TREND_TYPE,
        entry_unit=entry,
    )
    assert center is not None
    same_direction = unit(
        3,
        "up",
        115,
        140,
        source_kind=SourceKind.TREND_TYPE,
    )

    with pytest.raises(ValueError, match="transition must alternate"):
        advance_center(center, same_direction)
