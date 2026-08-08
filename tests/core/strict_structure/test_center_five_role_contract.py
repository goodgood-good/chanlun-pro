"""The physical center uses one immutable five-segment establishment window.

The user-approved physical contract is::

    entry + core A/B/C + leave

All five positively overlap the A/B/C core. Only A/B/C define ``ZD/ZG`` and
the rectangle's initial time span.
"""

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.models import CenterState, SourceKind
from tests.core.strict_structure.helpers import unit


def _completed_five_role_center():
    values = (
        unit(0, "up", 90, 120),       # external entry
        unit(1, "down", 120, 100),    # core A
        unit(2, "up", 100, 115),      # core B
        unit(3, "down", 115, 105),    # core C
        unit(4, "up", 105, 130),      # external leave
        unit(5, "down", 130, 120),    # first outside return
    )
    center = establish_center(values[:5], 0, SourceKind.SEGMENT)
    assert center is not None
    completed, _ = advance_center(center, values[5])
    return values, completed


def test_entry_core_and_leave_are_five_distinct_roles() -> None:
    values, center = _completed_five_role_center()

    assert center.state is CenterState.COMPLETED
    assert center.entry_unit is values[0]
    assert center.initial_units == values[1:4]
    assert center.core_units == values[1:4]
    assert center.body_units == values[1:4]
    assert center.completion_leave_unit is values[4]
    assert center.completion_return_unit is values[5]
    assert center.lifecycle_role_count == 5
    assert center.has_minimum_physical_roles is True
    assert center.entry_unit not in center.body_units
    assert center.completion_leave_unit not in center.body_units
    assert (center.zd_tick, center.zg_tick) == (105, 115)


def test_chart_rectangle_excludes_entry_and_leave_and_uses_middle_three() -> None:
    values, center = _completed_five_role_center()
    payload = strict_center_to_chart_dict(center)

    assert payload["points"][0]["time"] == int(values[1].market_start.timestamp())
    assert payload["points"][1]["time"] == int(values[3].market_end.timestamp())
    assert payload["entering_segment"]["start_time"] < payload["points"][0]["time"]
    assert payload["leaving_segment"]["end_time"] > payload["points"][1]["time"]
    assert payload["display_range"] == {
        "start_role": "middle_three_first_start",
        "end_role": "middle_three_last_end",
        "includes_entry": False,
        "includes_leave": False,
        "price_core_source": "middle_three_intersection",
    }
    assert payload["entry_unit_id"] not in payload["body_unit_ids"]
    assert payload["completion_leave_unit_id"] not in payload["body_unit_ids"]
    assert payload["core_unit_ids"] == payload["body_unit_ids"]
    assert payload["overlap_component_count"] == 5
    assert payload["establishment_component_count"] == 5
    assert payload["lifecycle_role_count"] == 5


def test_four_roles_are_internal_evidence_but_not_a_physical_output() -> None:
    entry_and_core = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
    )

    internal = establish_center(entry_and_core, 0, SourceKind.SEGMENT)
    assert internal is None
    assert calculate_centers(
        entry_and_core, 0, SourceKind.SEGMENT
    ).centers == ()
