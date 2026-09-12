from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.cl_utils.strict_chart import aware_datetime_to_epoch_seconds, strict_center_to_chart_dict
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind, TrendCenter
from tests.core.strict_structure.helpers import completed_up_center


CN = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
PRICE_BASIS = "test-raw"
QUANTUM = Decimal("0.01")




def _unit(
    index: int,
    direction: str,
    start_tick: int,
    end_tick: int,
    *,
    source_kind: SourceKind = SourceKind.SEGMENT,
    locked: bool = True,
) -> ConstituentUnit:
    market_start = BASE + timedelta(minutes=index * 5)
    market_end = market_start + timedelta(minutes=5)
    return ConstituentUnit(
        unit_id=f"{source_kind.value}-u-{index}",
        structural_level=0,
        source_kind=source_kind,
        price_basis_revision=PRICE_BASIS,
        direction=direction,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=market_start,
        market_end=market_end,
        confirmed_at=market_end + timedelta(minutes=5) if locked else None,
        available_at=market_end + timedelta(minutes=5),
        locked=locked,
        child_ids=(),
    )


def _center(
    *,
    source_kind: SourceKind = SourceKind.SEGMENT,
    extension: bool = False,
) -> TrendCenter:
    initial = (
        _unit(0, "up", 90, 120, source_kind=source_kind),
        _unit(1, "down", 120, 100, source_kind=source_kind),
        _unit(2, "up", 100, 115, source_kind=source_kind),
        _unit(3, "down", 115, 105, source_kind=source_kind),
        _unit(4, "up", 105, 130, source_kind=source_kind),
    )
    value = establish_center(initial, 0, source_kind)
    assert value is not None
    if extension:
        # The observed departure fails when u5 returns to the core.  The
        # disproved u4 remains external history; only u5 extends the body.
        value, _event = advance_center(
            value,
            _unit(5, "down", 130, 110, source_kind=source_kind),
        )
    return value




def test_formal_center_rectangle_uses_core_not_envelope() -> None:
    payload = strict_center_to_chart_dict(_center(extension=True))

    assert [point["price_tick"] for point in payload["points"]] == [115, 105]
    assert payload["envelope"] == {"dd_tick": 100, "gg_tick": 130}


def test_first_three_locked_units_never_render_a_formal_physical_center() -> None:
    units = (
        _unit(0, "down", 120, 100),
        _unit(1, "up", 100, 115),
        _unit(2, "down", 115, 105),
    )
    center = establish_center(units, 0, SourceKind.SEGMENT)
    assert center is None


def test_fifth_leave_establishes_center_and_sixth_return_completes_lifecycle() -> None:
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 130),
        _unit(5, "down", 130, 120),
    )
    center = establish_center(units[:5], 0, SourceKind.SEGMENT)
    assert center is not None

    leaving_payload = strict_center_to_chart_dict(center)
    assert leaving_payload["pending_leave_unit_id"] == units[4].unit_id
    assert leaving_payload["body_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert leaving_payload["establishment_component_count"] == 5

    completed, _event = advance_center(center, units[5])
    completed_payload = strict_center_to_chart_dict(completed)
    assert completed_payload["pending_leave_unit_id"] is None
    assert completed_payload["completion_leave_unit_id"] == units[4].unit_id
    assert completed_payload["completion_return_unit_id"] == units[5].unit_id
    assert completed_payload["extension_unit_ids"] == []
    assert completed_payload["overlap_component_count"] == 5
    assert completed_payload["establishment_component_count"] == 5


def test_failed_departure_reentry_increases_actual_overlap_evidence_count() -> None:
    center = _center(extension=True)

    payload = strict_center_to_chart_dict(center)

    assert payload["establishment_component_count"] == 5
    assert payload["overlap_component_count"] == 6
    assert [item["unit_id"] for item in payload["overlap_components"]] == [
        item.unit_id for item in (*center.establishment_units, *center.extension_units)
    ]


def test_center_payload_exposes_five_roles_and_separate_completion_return() -> None:
    center = completed_up_center()
    payload = strict_center_to_chart_dict(center)

    assert payload["schema"] == "chanlun-chart-center"
    assert payload["state"] == "completed"
    assert payload["completion_phase"] == "FORMAL_THIRD_CLASS_POINT"
    assert payload["completion_point_type"] == "3buy"
    assert payload["expected_completion_point_type"] == "3buy"
    assert payload["completion_point_status"] == "confirmed"
    assert payload["entry_unit_id"] == center.entry_unit.unit_id
    assert payload["core_unit_ids"] == [item.unit_id for item in center.core_units]
    assert payload["establishment_leave_unit_id"] == (
        center.establishment_leave_unit.unit_id
    )
    assert payload["initial_exit_unit_id"] == center.initial_exit_unit.unit_id
    assert payload["completion_leave_unit_id"] == center.completion_leave_unit.unit_id
    assert payload["completion_return_unit_id"] == center.completion_return_unit.unit_id
    assert payload["entering_segment"]["unit_id"] == center.entry_unit.unit_id
    assert payload["entry_role"] == "external_entry"
    assert payload["minimum_lifecycle_role_count"] == 5
    assert payload["overlap_component_count"] == 5
    assert payload["establishment_component_count"] == 5
    assert payload["middle_three_component_ids"] == [
        item.unit_id for item in center.core_units
    ]
    assert payload["leaving_segment"]["unit_id"] == (
        center.completion_leave_unit.unit_id
    )
    assert payload["leaving_segment"]["direction"] == "up"
    assert payload["completion_return_segment"]["unit_id"] == (
        center.completion_return_unit.unit_id
    )
    assert payload["establishment_segment_ids"] == [
        item.unit_id for item in center.establishment_units
    ]
    assert (
        payload["points"][0]["time"]
        == payload["middle_three_components"][0]["start_time"]
    )
    assert (
        payload["points"][1]["time"]
        == payload["middle_three_components"][-1]["end_time"]
    )
    assert payload["display_range"]["includes_entry"] is False
    assert payload["display_range"]["includes_leave"] is False


def test_fifth_segment_inside_core_never_reaches_chart_serializer() -> None:
    values = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 110),
    )
    assert establish_center(values, 0, SourceKind.SEGMENT) is None




def test_center_render_id_changes_on_body_revision_or_state_only() -> None:
    established = strict_center_to_chart_dict(_center())
    extended = strict_center_to_chart_dict(_center(extension=True))

    assert established["center_id"] == extended["center_id"]
    assert established["render_id"] != extended["render_id"]




def test_formal_serializer_rejects_stroke_observation() -> None:
    with pytest.raises(ValueError, match="formal serializer rejects"):
        strict_center_to_chart_dict(_center(source_kind=SourceKind.STROKE_OBSERVATION))


def test_chart_times_are_utc_epoch_seconds_and_reject_naive_datetime() -> None:
    center = _center()
    payload = strict_center_to_chart_dict(center)

    assert payload["points"][0]["time"] == int(
        center.core_units[0].market_start.timestamp()
    )
    assert payload["points"][1]["time"] == int(
        center.body_units[-1].market_end.timestamp()
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        aware_datetime_to_epoch_seconds(
            center.core_body_start_market_time.replace(tzinfo=None)
        )




def test_completed_center_range_excludes_leave_and_completion_return() -> None:
    center = completed_up_center()
    payload = strict_center_to_chart_dict(center)

    assert payload["points"][1]["time"] == int(
        center.completion_leave_unit.market_start.timestamp()
    )
    assert payload["points"][1]["time"] < int(
        center.completion_return_unit.market_end.timestamp()
    )
    assert payload["completed_at"] == int(center.completed_at.timestamp())
