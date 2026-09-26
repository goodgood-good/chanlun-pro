from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import replace
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.cl_utils.strict_chart import (
    _center_payload,
    aware_datetime_to_epoch_seconds,
    strict_center_preview_to_chart_dict,
    strict_center_to_chart_dict,
)
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
    establish_center_preview,
)
from chanlun.core.strict_structure.models import CenterEvidence, CenterState, ConstituentUnit, SourceKind, TrendCenter
from tests.core.strict_structure.helpers import completed_up_center, unit


CN = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
PRICE_BASIS = "test-raw"
QUANTUM = Decimal("0.01")


def test_point_and_divergence_geometry_changes_invalidate_chart_render_identity():
    from chanlun.cl_utils.strict_chart import strict_point_to_chart_dict, strict_divergence_to_chart_dict
    from tests.core.strict_structure.test_first_class_points import structure_from_values, divergent_strength, engine_for
    strength = divergent_strength("down")
    structure, _ = structure_from_values(direction="down", strength=strength)
    point = engine_for(structure, strength).first_class_points()[0]
    divergence = replace(point.divergence, anchor_at=point.anchor_at-timedelta(seconds=1))
    moved = replace(point, anchor_at=divergence.anchor_at, divergence=divergence)
    assert moved.point_id == point.point_id
    assert strict_point_to_chart_dict(moved)["render_id"] != strict_point_to_chart_dict(point)["render_id"]
    assert strict_divergence_to_chart_dict(divergence)["render_id"] != strict_divergence_to_chart_dict(point.divergence)["render_id"]




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


def test_boundary_third_buy_exports_non_crossing_end_rule() -> None:
    center = completed_up_center(return_low_tick=115, zg_tick=115)

    payload = strict_center_to_chart_dict(center)

    assert payload["third_class_confirmed"]
    assert payload["center_end_rule"] == (
        "independent_completed_lower_leave_and_first_non_crossing_return"
    )
    assert payload["completion_return_segment"]["low_tick"] == payload["core"]["zg_tick"]


def test_one_price_recursive_center_publishes_unresolved_C46_policy() -> None:
    source = SourceKind.TREND_TYPE
    units = (
        unit(0, "up", 100, 120, source_kind=source, structural_level=1),
        unit(1, "down", 120, 90, source_kind=source, structural_level=1),
        unit(2, "up", 90, 100, source_kind=source, structural_level=1),
    )
    center = establish_center(units, 1, source)
    assert center is not None and center.zd_tick == center.zg_tick == 100

    evidence = CenterEvidence.from_center(center)
    assert evidence.runtime_overlap_policy == "recursive_closed_interval_contact"
    assert evidence.unresolved_source_difference_ids == ("C46-01",)

    payload = strict_center_to_chart_dict(center)
    assert payload["runtime_overlap_policy"] == "recursive_closed_interval_contact"
    assert payload["unresolved_source_difference_ids"] == ["C46-01"]
    assert payload["overlap_component_count"] == 3
    assert payload["higher_center_member_trend_ids"] == [part.unit_id for part in units]
    assert payload["higher_center_member_confirmed_at"] == [
        int(part.confirmed_at.timestamp()) for part in units
    ]
    assert payload["higher_center_formal_at"] >= payload["higher_center_member_confirmed_at"][-1]
    assert payload["higher_center_formal_member_gate"] == "three_locked_lower_types"
    assert [part["unit_id"] for part in payload["overlap_components"]] == [
        part.unit_id for part in units
    ]

    preview_units = (*units[:2], replace(units[2], locked=False, confirmed_at=None))
    preview = establish_center_preview(preview_units, 1, source)
    assert preview is not None
    candidate = strict_center_preview_to_chart_dict(
        preview, {part.unit_id: part for part in preview_units}
    )
    assert candidate is not None
    assert candidate["runtime_overlap_policy"] == "recursive_closed_interval_contact"
    assert candidate["unresolved_source_difference_ids"] == ["C46-01"]

    physical = strict_center_to_chart_dict(_center())
    assert physical["runtime_overlap_policy"] == "physical_positive_width"
    assert physical["unresolved_source_difference_ids"] == []

    positive_units = (
        unit(3, "up", 100, 120, source_kind=source, structural_level=1),
        unit(4, "down", 120, 100, source_kind=source, structural_level=1),
        unit(5, "up", 100, 115, source_kind=source, structural_level=1),
    )
    positive_center = establish_center(positive_units, 1, source)
    assert positive_center is not None and positive_center.zd_tick < positive_center.zg_tick
    assert strict_center_to_chart_dict(positive_center)["unresolved_source_difference_ids"] == []


def test_one_price_physical_center_keeps_C46_marker_through_chart_output() -> None:
    values = (
        _unit(0, "down", 130, 120),
        replace(_unit(1, "up", 120, 120), high_tick=130),
        _unit(2, "down", 120, 100),
        _unit(3, "up", 100, 120),
        _unit(4, "down", 120, 110),
    )
    unfinished = values[:-1] + (replace(values[-1], locked=False, confirmed_at=None),)
    preview = establish_center_preview(unfinished, 0, SourceKind.SEGMENT)
    assert preview is not None
    candidate = strict_center_preview_to_chart_dict(
        preview, {part.unit_id: part for part in unfinished}
    )
    assert candidate is not None
    assert candidate["runtime_overlap_policy"] == "physical_closed_interval_contact"
    assert candidate["unresolved_source_difference_ids"] == ["C46-01"]

    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    completed, _event = advance_center(center, _unit(5, "up", 110, 115))
    payload = strict_center_to_chart_dict(completed)
    assert payload["core"] == {"zd_tick": 120, "zg_tick": 120}
    assert payload["runtime_overlap_policy"] == "physical_closed_interval_contact"
    assert payload["unresolved_source_difference_ids"] == ["C46-01"]
    assert payload["overlap_component_count"] >= 5


def test_one_price_stroke_observation_counts_its_five_closed_overlap_roles() -> None:
    source = SourceKind.STROKE_OBSERVATION
    values = (
        _unit(0, "down", 130, 120, source_kind=source),
        replace(_unit(1, "up", 120, 120, source_kind=source), high_tick=130),
        _unit(2, "down", 120, 100, source_kind=source),
        _unit(3, "up", 100, 120, source_kind=source),
        _unit(4, "down", 120, 110, source_kind=source),
    )
    center = establish_center(values, 0, source)
    assert center is not None and center.zd_tick == center.zg_tick == 120
    payload = _center_payload(center, render_kind="center_observation", tradable=False)
    assert payload["runtime_overlap_policy"] == "physical_closed_interval_contact"
    assert payload["overlap_component_count"] >= 5


def test_small_to_large_second_point_does_not_claim_larger_turn_confirmation() -> None:
    from chanlun.cl_utils.strict_chart import strict_point_to_chart_dict
    from tests.core.strict_structure.signal_helpers import confirmed_point

    second = confirmed_point(point_type="2buy")
    candidate = replace(
        second,
        evidence_codes=("formal_structure", "small_to_large_reversal"),
        small_to_large_carrier_unit_ids=("depart", "return", second.anchor_unit_id),
    )

    assert strict_point_to_chart_dict(candidate)["large_turn_certificate"] == (
        "operating_second_class_not_larger_turn_certificate"
    )


def test_higher_center_is_linked_only_through_three_exact_lower_trend_members() -> None:
    from chanlun.cl_utils.strict_chart import _attach_parent_center_rule_evidence

    pair = {
        "previous_center_id": "old", "current_center_id": "new",
        "higher_center_ids": [],
        "higher_center_formal": "requires_independent_parent_three_members",
        "higher_center_formal_at": None,
    }
    unrelated = {
        "previous_center_id": "old", "current_center_id": "other",
        "higher_center_ids": [],
        "higher_center_formal": "requires_independent_parent_three_members",
        "higher_center_formal_at": None,
    }
    levels = [
        {"trend_types": [
            {"trend_id": "a", "center_ids": ["old"]},
            {"trend_id": "b", "center_ids": []},
            {"trend_id": "c", "center_ids": ["new"]},
        ], "center_pair_rule_states": [pair, unrelated]},
        {"centers": [{
            "center_id": "parent", "higher_center_member_trend_ids": ["a", "b", "c"],
            "higher_center_formal_at": 123,
        }]},
    ]

    _attach_parent_center_rule_evidence(levels)

    assert pair["higher_center_ids"] == ["parent"]
    assert pair["higher_center_formal"] == "confirmed_by_parent_three_members"
    assert pair["higher_center_formal_at"] == 123
    assert unrelated["higher_center_ids"] == []
    assert unrelated["higher_center_formal_at"] is None


@pytest.mark.parametrize('state,phase', [
    (CenterState.DIVERGENCE_CLOSED, 'CLOSED_AT_DIVERGENCE'),
    (CenterState.SUPERSEDED, 'SUPERSEDED_BY_SUCCESSOR'),
])
def test_frozen_center_without_a_third_point_does_not_wait_for_a_future_return(state, phase):
    center = _center()
    if state is CenterState.DIVERGENCE_CLOSED:
        center = replace(center, state=state, boundary_divergence_id='confirmed-divergence',
                         boundary_anchor_unit_id=center.pending_leave_unit.unit_id)
    else:
        center = replace(center, state=state, pending_leave_unit=None,
                         superseded_by_center_id='successor', superseded_at=center.available_at,
                         supersession_bridge_units=(center.pending_leave_unit,))
    payload = strict_center_to_chart_dict(center)
    assert payload['completion_phase'] == phase
    assert payload['expected_completion_point_type'] is None
    assert payload['completion_point_type'] is None
    assert not payload['third_class_confirmed'] and not payload['center_ended']


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
