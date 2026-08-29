from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.cl_utils.strict_chart import (
    _ordered_current_trend_payloads,
    active_center_projection_to_chart_dict,
    aware_datetime_to_epoch_seconds,
    build_strict_structure_snapshot,
    center_observation_to_chart_dict,
    strict_center_preview_to_chart_dict,
    strict_center_to_chart_dict,
    strict_point_to_chart_dict,
    strict_trend_to_chart_dict,
)
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
    establish_center_preview,
    forming_preview,
)
from chanlun.core.strict_structure.identity import (
    build_trend_id,
    build_strict_evidence_revision,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    CenterPreviewState,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
    TrendCenter,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.point_rules import build_approaching_point_id
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_current_confirmed_points,
)
from chanlun.decision_support.trading_system.operational_point_graph import (
    resolve_current_operational_point_graph,
)
from tests.core.strict_structure.helpers import (
    completed_up_center,
    engine_for,
    ongoing_center,
)
from tests.trading_system.strict_helpers import strict_evidence_result, strict_point


CN = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
PRICE_BASIS = "test-raw"
QUANTUM = Decimal("0.01")


def test_current_trend_payload_order_is_causal_not_id_sorted() -> None:
    first = {
        "trend_id": "z-earlier-market-movement",
        "available_at": 200,
        "direction": "down",
        "points": [
            {"time": 100, "price_tick": 120},
            {"time": 150, "price_tick": 100},
        ],
    }
    second = {
        "trend_id": "a-later-market-movement",
        "available_at": 200,
        "direction": "up",
        "points": [
            {"time": 150, "price_tick": 100},
            {"time": 190, "price_tick": 130},
        ],
    }

    assert _ordered_current_trend_payloads((first, second)) == [first, second]


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


def _trend(center: TrendCenter) -> TrendType:
    units = tuple(
        sorted(
            (
                *center.body_units,
                *center.failed_departure_units,
                *center.supersession_bridge_units,
            ),
            key=lambda item: (item.market_start, item.market_end, item.unit_id),
        )
    )
    direction = "up" if units[-1].end_tick > units[0].start_tick else "down"
    return TrendType(
        trend_id=build_trend_id(
            price_basis_revision=PRICE_BASIS,
            structural_level=0,
            center_ids=(center.center_id,),
            constituent_unit_ids=tuple(item.unit_id for item in units),
            direction=direction,
        ),
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        kind=TrendKind.CONSOLIDATION,
        direction=direction,
        state=TrendState.FORMING,
        centers=(center,),
        constituent_units=units,
        start_tick=units[0].start_tick,
        end_tick=units[-1].end_tick,
        low_tick=min(item.low_tick for item in units),
        high_tick=max(item.high_tick for item in units),
        market_start=units[0].market_start,
        market_end=units[-1].market_end,
        confirmed_at=None,
        available_at=max(
            center.available_at,
            *(item.available_at for item in units),
        ),
    )


def _forming_preview_fixture():
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 130, locked=False),
    )
    preview = establish_center_preview(units, 0, SourceKind.SEGMENT)
    assert preview is not None
    return preview, units


def _evidence(
    *,
    observation: TrendCenter | None = None,
    formal_centers: tuple[TrendCenter, ...] | None = None,
    confirmed_points=(),
    approaching_points=None,
    divergences=(),
    previews=(),
    level_units=None,
    level_count: int = 1,
    source_frequency: str = "5m",
) -> StrictEvidenceResult:
    formal_center = _center(extension=True)
    selected_centers = (
        (formal_center,) if formal_centers is None else tuple(formal_centers)
    )
    if level_units is None:
        units_by_id = {}
        for center in selected_centers:
            lifecycle = (
                *(() if center.entry_unit is None else (center.entry_unit,)),
                *center.establishment_units,
                *center.body_units,
                *center.failed_departure_units,
                *center.supersession_bridge_units,
                *(
                    ()
                    if center.pending_leave_unit is None
                    else (center.pending_leave_unit,)
                ),
                *(
                    ()
                    if center.completion_leave_unit is None
                    else (center.completion_leave_unit,)
                ),
                *(
                    ()
                    if center.completion_return_unit is None
                    else (center.completion_return_unit,)
                ),
            )
            for item in lifecycle:
                previous = units_by_id.setdefault(item.unit_id, item)
                assert previous == item
        selected_units = tuple(
            sorted(
                units_by_id.values(),
                key=lambda item: (item.market_start, item.unit_id),
            )
        )
    else:
        selected_units = tuple(level_units)
    center_result = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=selected_centers,
        previews=tuple(previews),
        events=(),
        locked_unit_count=sum(1 for item in selected_units if item.locked),
        replay_from=0,
    )
    first_level = StrictLevelResult(
        structural_level=0,
        units=selected_units,
        center_result=center_result,
        trend_types=(
            (_trend(formal_center),) if selected_centers == (formal_center,) else ()
        ),
        completed_trends=(),
    )
    levels = [first_level]
    for structural_level in range(1, level_count):
        empty_centers = CenterLevelResult(
            structural_level=structural_level,
            price_basis_revision=PRICE_BASIS,
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=0,
            replay_from=0,
        )
        levels.append(
            StrictLevelResult(
                structural_level=structural_level,
                units=(),
                center_result=empty_centers,
                trend_types=(),
                completed_trends=(),
            )
        )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=PRICE_BASIS,
        levels=tuple(levels),
    )
    if approaching_points is None:
        approaching_points = StrictSignalEngine(
            structure=structure,
            price_quantum=QUANTUM,
        ).approaching_points(BASE + timedelta(hours=6))
    observation_result = CenterLevelResult(
        structural_level=0,
        price_basis_revision=PRICE_BASIS,
        centers=() if observation is None else (observation,),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    revision = build_strict_evidence_revision(
        symbol="SH.600519",
        source_frequency=source_frequency,
        price_basis_revision=PRICE_BASIS,
        strict_config_revision="strict-config",
        structure=structure,
        confirmed_points=confirmed_points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol="SH.600519",
        source_frequency=source_frequency,
        source_closed_at=BASE + timedelta(hours=6),
        price_basis_revision=PRICE_BASIS,
        structure_price_quantum=QUANTUM,
        strict_config_revision="strict-config",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observation_result,
        confirmed_points=tuple(confirmed_points),
        approaching_points=tuple(approaching_points),
        divergences=tuple(divergences),
    )


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


def test_snapshot_exposes_first_components_and_leave_for_ui_audit() -> None:
    center = completed_up_center()
    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(center,),
            confirmed_points=engine_for(center).third_class_points(),
        ),
        interval="5m",
    )
    payload = snapshot["levels"][0]["centers"][0]

    assert payload["entering_segment"]["unit_id"] == center.entry_unit.unit_id
    assert payload["entry_role"] == "external_entry"
    assert payload["middle_three_components"][0]["start_price"] == float(
        QUANTUM * center.initial_units[0].start_tick
    )
    assert payload["leaving_segment"]["unit_id"] == (
        center.completion_leave_unit.unit_id
    )
    assert payload["leaving_segment"]["direction"] == "up"


def test_center_render_id_changes_on_body_revision_or_state_only() -> None:
    established = strict_center_to_chart_dict(_center())
    extended = strict_center_to_chart_dict(_center(extension=True))

    assert established["center_id"] == extended["center_id"]
    assert established["render_id"] != extended["render_id"]


def test_stroke_observation_is_explicitly_non_tradable() -> None:
    center = _center(source_kind=SourceKind.STROKE_OBSERVATION)
    payload = center_observation_to_chart_dict(center)

    assert payload["source_kind"] == "stroke_observation"
    assert payload["tradable"] is False
    assert payload["render_kind"] == "center_observation"
    assert payload["points"][0]["time"] == int(
        center.core_units[0].market_start.timestamp()
    )


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


def test_active_projection_keeps_entry_and_leave_outside_the_box() -> None:
    center = _center()
    source_closed_at = BASE + timedelta(hours=6)

    body = strict_center_to_chart_dict(center)
    projection = active_center_projection_to_chart_dict(
        center,
        source_closed_at,
    )

    assert body["points"][1]["time"] == int(
        center.body_units[-1].market_end.timestamp()
    )
    assert projection["render_kind"] == "center_projection"
    assert projection["tradable"] is False
    assert projection["entry_role"] == "external_entry"
    assert projection["overlap_component_count"] == 5
    assert projection["establishment_segment_ids"] == [
        item.unit_id for item in center.establishment_units
    ]
    assert projection["establishment_component_count"] == 5
    assert projection["establishment_leave_unit_id"] == (
        center.establishment_leave_unit.unit_id
    )
    assert projection["initial_exit_unit_id"] == center.initial_exit_unit.unit_id
    assert projection["pending_leave_unit_id"] == (center.pending_leave_unit.unit_id)
    assert projection["completion_phase"] == "AWAITING_SAME_LEVEL_RETURN"
    assert projection["expected_completion_point_type"] == "3buy"
    assert projection["body_unit_ids"] == [item.unit_id for item in center.body_units]
    assert projection["points"][0]["time"] == int(
        center.core_units[0].market_start.timestamp()
    )
    assert projection["points"][1]["time"] == body["points"][1]["time"]
    assert projection["points"][1]["time"] < int(source_closed_at.timestamp())
    assert projection["core"] == body["core"]


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


def test_snapshot_projects_only_the_latest_ongoing_center() -> None:
    stale_ongoing = ongoing_center(20)
    latest_completed = completed_up_center(40)
    completion_points = engine_for(latest_completed).third_class_points()
    with pytest.raises(ValueError, match="only the terminal center may remain ongoing"):
        _evidence(
            formal_centers=(stale_ongoing, latest_completed),
            confirmed_points=completion_points,
        )

    completed_snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(latest_completed,),
            confirmed_points=completion_points,
        ),
        interval="5m",
    )

    assert completed_snapshot["levels"][0]["center_projections"] == []

    # Keep the synthetic level causally connected: the completed center's
    # return ends at 120 and the next center's entry starts at 120.
    latest_ongoing = ongoing_center(46, zd_tick=135, zg_tick=145)
    ongoing_snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(latest_completed, latest_ongoing),
            confirmed_points=completion_points,
        ),
        interval="5m",
    )

    assert [
        item["center_id"]
        for item in ongoing_snapshot["levels"][0]["center_projections"]
    ] == [latest_ongoing.center_id]


def test_snapshot_serializes_unlocked_tail_as_non_tradable_center_preview() -> None:
    preview, units = _forming_preview_fixture()
    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    payload = snapshot["levels"][0]["center_previews"][0]
    assert snapshot["levels"][0]["centers"] == []
    assert snapshot["levels"][0]["current_trends"] == []
    assert snapshot["levels"][0]["confirmed_points"] == []
    assert snapshot["levels"][0]["divergences"] == []
    assert snapshot["schema"] == "chanlun-chart-structure"
    assert snapshot["formal_direction"] == {
        "direction": "neutral",
        "structural_level": None,
        "trend_id": None,
        "support_point_id": None,
        "reason_codes": ["current_suffix_has_no_formal_trend"],
    }
    assert payload["schema"] == "chanlun-chart-center"
    assert payload["render_kind"] == "center_preview"
    assert payload["state"] == "forming"
    assert payload["tradable"] is False
    assert payload["completion_phase"] == "AWAITING_SAME_LEVEL_RETURN"
    assert payload["expected_completion_point_type"] == "3buy"
    assert payload["core"] == {
        "zd_tick": 105,
        "zg_tick": 115,
        "zd_price": 1.05,
        "zg_price": 1.15,
    }
    assert payload["entry_unit_id"] == units[0].unit_id
    assert payload["initial_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert payload["body_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert payload["initial_exit_unit_id"] == units[4].unit_id
    assert payload["pending_leave_unit_id"] == units[4].unit_id
    assert payload["completion_leave_unit_id"] is None
    assert payload["points"][0]["time"] == int(units[1].market_start.timestamp())
    assert payload["points"][1]["time"] == int(units[3].market_end.timestamp())


def test_snapshot_draws_partial_four_line_center_as_non_tradable_preview() -> None:
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105, locked=False),
    )
    preview = forming_preview(units[1:], 0, SourceKind.SEGMENT, entry_unit=units[0])
    assert preview is not None

    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    payload = snapshot["levels"][0]["center_previews"][0]
    assert payload["state"] == "forming"
    assert payload["tradable"] is False
    assert payload["entry_unit_id"] == units[0].unit_id
    assert payload["core_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert payload["establishment_segment_ids"] == [item.unit_id for item in units]
    assert payload["establishment_component_count"] == 4
    assert payload["overlap_component_count"] == 4
    assert payload["establishment_leave_unit_id"] is None
    assert payload["initial_exit_unit_id"] is None
    assert payload["lifecycle_role_count"] == 4
    assert payload["minimum_lifecycle_role_count"] == 5
    assert payload["established_market_time"] is None
    assert payload["established_at"] is None


def test_physical_preview_establishment_time_waits_for_locked_fifth_role() -> None:
    locked_units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 130),
    )
    unlocked_units = (
        *locked_units[:-1],
        replace(
            locked_units[-1],
            locked=False,
            confirmed_at=None,
        ),
    )
    preview = establish_center_preview(
        unlocked_units,
        0,
        SourceKind.SEGMENT,
    )
    assert preview is not None
    assert preview.establishment_leave_unit_id == locked_units[-1].unit_id

    unlocked = strict_center_preview_to_chart_dict(
        preview,
        unlocked_units,
        BASE + timedelta(hours=1),
    )
    locked = strict_center_preview_to_chart_dict(
        preview,
        locked_units,
        BASE + timedelta(hours=1),
    )

    assert unlocked["established_market_time"] is None
    assert unlocked["established_at"] is None
    assert locked["established_market_time"] == aware_datetime_to_epoch_seconds(
        locked_units[-1].market_end
    )
    assert locked["established_at"] == aware_datetime_to_epoch_seconds(
        locked_units[-1].confirmed_at
    )


def test_snapshot_serializes_pending_movements_separately_from_formal_trends() -> None:
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 125),
        _unit(3, "down", 125, 105),
    )
    snapshot = build_strict_structure_snapshot(
        _evidence(formal_centers=(), level_units=units),
        interval="5m",
    )

    level = snapshot["levels"][0]
    assert level["current_trends"] == []
    assert len(level["pending_movements"]) == 1
    pending = level["pending_movements"][0]
    assert pending["render_kind"] == "pending_movement"
    assert pending["state"] == "pending"
    assert pending["classification"] == "unresolved"
    assert pending["role"] == "entire_stream"
    assert pending["constituent_unit_ids"] == [item.unit_id for item in units]
    assert pending["boundary_policy"] == "exclusive_unit_ownership"
    assert pending["tradable"] is False
    assert pending["recursive_eligible"] is False
    assert pending["divergence_eligible"] is False


def test_snapshot_serializes_multiple_unlocked_preview_units() -> None:
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115, locked=False),
        _unit(3, "down", 115, 105, locked=False),
        _unit(4, "up", 105, 130, locked=False),
    )
    preview = establish_center_preview(units, 0, SourceKind.SEGMENT)
    assert preview is not None

    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    payload = snapshot["levels"][0]["center_previews"][0]
    assert payload["entry_unit_id"] == units[0].unit_id
    assert payload["initial_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert payload["body_unit_ids"] == [item.unit_id for item in units[1:4]]
    assert payload["pending_leave_unit_id"] == units[4].unit_id


def test_snapshot_keeps_active_core_until_adjacent_five_roles_exist() -> None:
    units = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 130),
        _unit(5, "down", 130, 110),
        _unit(6, "up", 110, 114),
        _unit(7, "down", 114, 106, locked=False),
        _unit(8, "up", 106, 128, locked=False),
        _unit(9, "down", 128, 112, locked=False),
    )
    result = calculate_centers(units, 0, SourceKind.SEGMENT)
    assert not any(
        item.unit_ids == tuple(unit.unit_id for unit in units[3:])
        for item in result.previews
    )
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.entry_unit_id == units[0].unit_id
    expected_body = tuple(units[index].unit_id for index in (1, 2, 3, 5, 6, 7, 9))
    expected_failed = (units[4].unit_id, units[8].unit_id)
    assert preview.unit_ids == expected_body
    assert preview.failed_departure_unit_ids == expected_failed

    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=result.centers,
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    payload = snapshot["levels"][0]["center_previews"][0]
    assert payload["core"]["zd_tick"] == 105
    assert payload["core"]["zg_tick"] == 115
    assert payload["entry_unit_id"] == units[0].unit_id
    assert payload["body_unit_ids"] == list(expected_body)
    assert payload["failed_departure_unit_ids"] == list(expected_failed)


def test_snapshot_serializes_provisional_third_sell_completion() -> None:
    units = (
        _unit(0, "down", 140, 110),
        _unit(1, "up", 110, 130),
        _unit(2, "down", 130, 115),
        _unit(3, "up", 115, 125),
        _unit(4, "down", 125, 90, locked=False),
        _unit(5, "up", 90, 100, locked=False),
    )
    result = calculate_centers(units, 0, SourceKind.SEGMENT)
    assert result.centers == ()
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.state is CenterPreviewState.COMPLETED

    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    payload = snapshot["levels"][0]["center_previews"][0]
    assert payload["center_id"] == preview.formal_center_id
    assert payload["preview_id"] != payload["center_id"]
    assert payload["state"] == "completed"
    assert payload["tradable"] is False
    assert payload["completion_phase"] == "GEOMETRIC_THIRD_CLASS_POINT"
    assert payload["completion_point_type"] == "3sell"
    assert payload["expected_completion_point_type"] == "3sell"
    assert payload["completion_point_status"] == "provisional"
    assert payload["completion_direction"] == "down"
    assert payload["completion_leave_unit_id"] == units[4].unit_id
    assert payload["completion_return_unit_id"] == units[5].unit_id
    assert payload["completed_at"] is None
    assert payload["points"][0]["time"] == int(units[1].market_start.timestamp())
    assert payload["points"][1]["time"] == int(units[3].market_end.timestamp())


def test_operational_third_point_updates_the_same_center_explanation() -> None:
    raw_units = (
        _unit(0, "down", 140, 110),
        _unit(1, "up", 110, 130),
        _unit(2, "down", 130, 115),
        _unit(3, "up", 115, 125),
        _unit(4, "down", 125, 90, locked=False),
        _unit(5, "up", 90, 100, locked=False),
    )
    result = calculate_centers(raw_units, 0, SourceKind.SEGMENT)
    preview = result.previews[0]
    units = (
        *raw_units[:4],
        replace(raw_units[4], formed_at=raw_units[4].available_at),
        replace(raw_units[5], formed_at=raw_units[5].available_at),
        replace(
            _unit(6, "down", 100, 80, locked=False),
            forming=True,
        ),
    )
    source = strict_point(
        "3sell",
        status=StrictPointStatus.APPROACHING,
        available_at=units[5].available_at,
    )
    point = replace(
        source,
        point_id=build_approaching_point_id(
            price_basis_revision=PRICE_BASIS,
            point_type="3sell",
            structural_level=0,
            anchor_unit_id=units[5].unit_id,
            center_id=preview.formal_center_id,
            parent_point_id=None,
        ),
        anchor_unit_id=units[5].unit_id,
        anchor_at=units[5].market_end,
        anchor_tick=units[5].high_tick,
        invalidation_tick=preview.zd_tick,
        center_id=preview.formal_center_id,
        center_zd_tick=preview.zd_tick,
        center_zg_tick=preview.zg_tick,
        center_ordinal=1,
        evidence_codes=("projected_geometric_structure",),
        missing_conditions=("terminal_unit_locked",),
    )

    snapshot = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
            approaching_points=(point,),
        ),
        interval="5m",
    )

    level = snapshot["levels"][0]
    center_payload = level["center_previews"][0]
    point_payload = level["confirmed_points"][0]
    assert center_payload["completion_phase"] == "OPERATIONAL_THIRD_CLASS_POINT"
    assert center_payload["completion_point_status"] == "confirmed"
    assert center_payload["operational_confirmation"] is True
    assert center_payload["audit_lock_state"] == "pending"
    assert center_payload["tradable"] is True
    assert point_payload["point_type"] == "3sell"
    assert point_payload["formation_state"] == "confirmed"
    assert point_payload["lock_state"] == "pending"


def test_completed_preview_serializer_rejects_return_that_crosses_core() -> None:
    units = (
        _unit(0, "down", 140, 110),
        _unit(1, "up", 110, 130),
        _unit(2, "down", 130, 115),
        _unit(3, "up", 115, 125),
        _unit(4, "down", 125, 90, locked=False),
        _unit(5, "up", 90, 100, locked=False),
    )
    result = calculate_centers(units, 0, SourceKind.SEGMENT)
    preview = result.previews[0]
    crossing_return = replace(units[5], end_tick=120, high_tick=120)

    with pytest.raises(ValueError, match="return must stay outside"):
        strict_center_preview_to_chart_dict(
            preview,
            units[:5] + (crossing_return,),
            crossing_return.market_end,
        )


def test_shifted_forming_preview_cannot_replace_active_ongoing_center() -> None:
    preview, units = _forming_preview_fixture()
    with pytest.raises(
        ValueError,
        match="shifted forming preview cannot displace an unresolved active-center",
    ):
        _evidence(
            formal_centers=(ongoing_center(20),),
            previews=(preview,),
            level_units=units,
        )


def test_center_preview_changes_render_revision_not_formal_revision() -> None:
    preview, units = _forming_preview_fixture()
    before = build_strict_structure_snapshot(
        _evidence(formal_centers=(), level_units=units),
        interval="5m",
    )
    after = build_strict_structure_snapshot(
        _evidence(
            formal_centers=(),
            previews=(preview,),
            level_units=units,
        ),
        interval="5m",
    )

    assert before["structure_revision"] == after["structure_revision"]
    assert before["snapshot_revision"] == after["snapshot_revision"]
    assert before["render_revision"] != after["render_revision"]


def test_trend_and_point_serializers_preserve_strict_identity() -> None:
    center = _center(extension=True)
    source_trend = _trend(center)
    trend = strict_trend_to_chart_dict(source_trend)
    source_point = strict_point("3buy")
    point = strict_point_to_chart_dict(source_point)

    assert trend["trend_id"] == source_trend.trend_id
    assert trend["render_id"].startswith(f"{source_trend.trend_id}@forming@")
    assert trend["geometric_direction"] == source_trend.direction
    assert trend["semantic_direction"] is None
    assert trend["direction_status"] == "consolidation"
    assert trend["formal_direction_confirmed"] is False
    assert trend["direction_reason_codes"] == []
    assert trend["constituent_unit_count"] == len(source_trend.constituent_units)
    assert trend["first_unit_direction"] == source_trend.direction
    assert trend["terminal_unit_direction"] == source_trend.direction
    assert trend["direction_aligned"] is True
    assert point["point_id"] == source_point.point_id
    assert point["point_type"] == "3buy"
    assert point["status"] == "confirmed"
    assert point["formation_state"] == "confirmed"
    assert point["lock_state"] == "locked"
    assert point["contains_forming_segment"] is False
    assert point["contains_unlocked_segment"] is False
    assert point["center_ordinal"] == 1


def test_formed_chart_point_declares_geometry_and_lock_state() -> None:
    source = replace(
        strict_point("3sell", status=StrictPointStatus.APPROACHING),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
        missing_conditions=(
            "unfinished_segment_lock",
            "formal_center_confirmation",
        ),
    )

    point = strict_point_to_chart_dict(source)

    assert point["status"] == "approaching"
    assert point["formation_state"] == "geometry_ready"
    assert point["lock_state"] == "pending"
    assert point["contains_forming_segment"] is False
    assert point["contains_unlocked_segment"] is True
    assert point["tradable"] is False


def test_snapshot_uses_same_latest_completed_operational_confirmation_as_selection() -> (
    None
):
    target = strict_point(
        "1buy",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=20),
    )
    forming_tail = strict_point(
        "1sell",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=30),
    )
    evidence = strict_evidence_result(
        code="SZ.000061",
        source_frequency="5m",
        source_closed_at=BASE + timedelta(hours=5, minutes=30),
        approaching_points=(target, forming_tail),
    )

    snapshot = build_strict_structure_snapshot(evidence, interval="5m")
    selected = extract_current_confirmed_points(
        evidence,
        code=evidence.symbol,
        source_frequency=evidence.source_frequency,
        as_of=evidence.source_closed_at,
    )
    level = snapshot["levels"][0]
    promoted = next(
        point for point in level["confirmed_points"] if point["point_type"] == "1buy"
    )
    still_forming = next(
        point for point in level["approaching_points"] if point["point_type"] == "1sell"
    )

    assert all(
        point["point_id"] != target.point_id for point in level["approaching_points"]
    )
    assert promoted["status"] == "confirmed"
    assert promoted["strict_status"] == "approaching"
    assert promoted["operational_confirmation"] is True
    assert promoted["confirmation_basis"] == "latest_completed_geometry"
    assert promoted["formation_state"] == "confirmed"
    assert promoted["lock_state"] == "pending"
    assert promoted["contains_forming_segment"] is False
    assert promoted["contains_unlocked_segment"] is True
    assert promoted["terminal_segment_role"] == "latest_completed"
    assert promoted["terminal_segment_state"] == "formed"
    assert promoted["confirmed_at"] == aware_datetime_to_epoch_seconds(
        target.available_at
    )
    assert promoted["confirmed_at"] == aware_datetime_to_epoch_seconds(
        next(point for point in selected if point.point_type == "1buy").confirmed_at
    )
    assert promoted["tradable"] is True

    assert still_forming["status"] == "approaching"
    assert still_forming["formation_state"] == "forming"
    assert still_forming["terminal_segment_role"] == "latest_unfinished"
    assert still_forming["tradable"] is False


@pytest.mark.parametrize(
    ("parent_type", "child_type", "tail_type"),
    (
        ("1buy", "2buy", "1sell"),
        ("1sell", "2sell", "1buy"),
    ),
)
def test_projected_second_class_promotes_its_parent_geometry_atomically(
    parent_type: str,
    child_type: str,
    tail_type: str,
) -> None:
    parent = strict_point(
        parent_type,
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=10),
    )
    raw_child = strict_point(
        child_type,
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=20),
    )
    child = replace(
        raw_child,
        point_id=build_approaching_point_id(
            price_basis_revision=raw_child.price_basis_revision,
            point_type=raw_child.point_type,
            structural_level=raw_child.structural_level,
            anchor_unit_id=raw_child.anchor_unit_id,
            center_id=raw_child.center_id,
            parent_point_id=parent.point_id,
        ),
        parent_point_id=parent.point_id,
        evidence_codes=(
            "formed_first_class_parent",
            "complete_adjacent_rebound",
            "complete_first_pullback",
            "prior_extreme_held",
            "projected_geometric_structure",
        ),
    )
    forming_tail = strict_point(
        tail_type,
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=30),
    )
    evidence = strict_evidence_result(
        code="SZ.000062",
        source_frequency="5m",
        source_closed_at=BASE + timedelta(hours=5, minutes=30),
        approaching_points=(parent, child, forming_tail),
    )

    snapshot = build_strict_structure_snapshot(evidence, interval="5m")
    level = snapshot["levels"][0]
    confirmed = {point["point_id"]: point for point in level["confirmed_points"]}

    assert parent.point_id in confirmed
    assert child.point_id in confirmed
    assert not {
        parent.point_id,
        child.point_id,
    }.intersection(point["point_id"] for point in level["approaching_points"])

    parent_payload = confirmed[parent.point_id]
    assert parent_payload["strict_status"] == "approaching"
    assert parent_payload["status"] == "confirmed"
    assert parent_payload["confirmation_basis"] == "dependency_chain_geometry"
    assert parent_payload["terminal_segment_role"] == "dependency_completed"
    assert parent_payload["lock_state"] == "pending"

    child_payload = confirmed[child.point_id]
    assert child_payload["confirmation_basis"] == "latest_completed_geometry"
    assert child_payload["parent_point_id"] == parent.point_id
    assert child_payload["operational_parent_point_ids"] == [parent.point_id]
    assert "operational_parent_geometry_confirmed" in child_payload["evidence_codes"]

    selected = extract_current_confirmed_points(
        evidence,
        code=evidence.symbol,
        source_frequency=evidence.source_frequency,
        as_of=evidence.source_closed_at,
    )
    assert [point.point_type for point in selected] == [child_type]
    assert "operational_parent_geometry_confirmed" in selected[0].evidence_codes


def test_projected_child_fails_closed_when_parent_geometry_is_not_complete() -> None:
    parent = strict_point(
        "1sell",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=10),
    )
    raw_child = strict_point(
        "2sell",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=20),
    )
    child = replace(
        raw_child,
        point_id=build_approaching_point_id(
            price_basis_revision=raw_child.price_basis_revision,
            point_type=raw_child.point_type,
            structural_level=raw_child.structural_level,
            anchor_unit_id=raw_child.anchor_unit_id,
            center_id=raw_child.center_id,
            parent_point_id=parent.point_id,
        ),
        parent_point_id=parent.point_id,
    )
    forming_tail = strict_point(
        "1buy",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=30),
    )
    evidence = strict_evidence_result(
        source_frequency="5m",
        source_closed_at=BASE + timedelta(hours=5, minutes=30),
        approaching_points=(parent, child, forming_tail),
    )
    level = evidence.structure.levels[0]
    incomplete_parent_units = tuple(
        replace(unit, formed_at=None) if unit.unit_id == parent.anchor_unit_id else unit
        for unit in level.units
    )
    incomplete_structure = replace(
        evidence.structure,
        levels=(replace(level, units=incomplete_parent_units),),
    )

    projections = resolve_current_operational_point_graph(
        incomplete_structure,
        confirmed_points=evidence.confirmed_points,
        approaching_points=evidence.approaching_points,
        source_frequency=evidence.source_frequency,
    )

    assert parent.point_id not in projections
    assert child.point_id not in projections


def test_non_trade_frequency_keeps_latest_completed_point_approaching() -> None:
    target = strict_point(
        "1buy",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=20),
    )
    forming_tail = strict_point(
        "1sell",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5, minutes=30),
    )
    evidence = strict_evidence_result(
        source_frequency="1m",
        source_closed_at=BASE + timedelta(hours=5, minutes=30),
        approaching_points=(target, forming_tail),
    )

    snapshot = build_strict_structure_snapshot(evidence, interval="1m")
    level = snapshot["levels"][0]

    assert level["confirmed_points"] == []
    assert {point["point_type"] for point in level["approaching_points"]} == {
        "1buy",
        "1sell",
    }


def test_snapshot_revision_is_deterministic_and_window_independent() -> None:
    evidence = _evidence()

    first = build_strict_structure_snapshot(evidence, interval="5m")
    second = build_strict_structure_snapshot(evidence, interval="5m")

    assert first == second
    assert first["structure_revision"] == evidence.structure_revision
    assert first["source_frequency"] == first["display_frequency"] == "5m"
    assert first["schema"] == "chanlun-chart-structure"


def test_atomic_evidence_rejects_an_unreferenced_recursive_divergence() -> None:
    item = DivergenceEvidence(
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            PRICE_BASIS,
            1,
            SourceKind.TREND_TYPE.value,
            "trend",
            "up",
            ("L1-earlier",),
            ("L1-later",),
        ),
        structural_level=1,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=PRICE_BASIS,
        kind="trend",
        direction="up",
        compare_unit_id="L1-earlier",
        signal_unit_id="L1-later",
        anchor_at=BASE + timedelta(minutes=30),
        anchor_tick=160,
        confirmed_at=BASE + timedelta(minutes=35),
        available_at=BASE + timedelta(minutes=35),
        price_extreme_confirmed=True,
        histogram_area_decayed=True,
        histogram_peak_decayed=True,
        dif_extreme_decayed=True,
        strength_source="macd",
    )
    with pytest.raises(
        ValueError,
        match="recursive level units must exactly replay prior locked trends",
    ):
        _evidence(
            divergences=(item,),
            level_count=2,
            source_frequency="1m",
        )


def test_observation_or_approaching_change_updates_render_not_decision_revision() -> (
    None
):
    observation = _center(source_kind=SourceKind.STROKE_OBSERVATION)
    formal_center = _center(extension=True)
    anchor = formal_center.body_units[-1]
    approaching = strict_point(
        "1buy",
        status=StrictPointStatus.APPROACHING,
        available_at=BASE + timedelta(hours=5),
    )
    divergence = replace(
        approaching.divergence,
        divergence_id=stable_structure_id(
            "chanlun-strict-divergence",
            PRICE_BASIS,
            0,
            SourceKind.SEGMENT.value,
            "trend",
            "down",
            approaching.divergence.compare_leg_unit_ids,
            (anchor.unit_id,),
        ),
        signal_unit_id=anchor.unit_id,
        signal_leg_unit_ids=(anchor.unit_id,),
        anchor_at=anchor.market_end,
        anchor_tick=anchor.low_tick,
        confirmed_at=anchor.confirmed_at,
        available_at=approaching.available_at,
    )
    approaching = replace(
        approaching,
        point_id=build_approaching_point_id(
            price_basis_revision=PRICE_BASIS,
            point_type=approaching.point_type,
            structural_level=0,
            anchor_unit_id=anchor.unit_id,
            center_id=None,
            parent_point_id=None,
        ),
        anchor_unit_id=anchor.unit_id,
        anchor_at=anchor.market_end,
        anchor_tick=anchor.low_tick,
        invalidation_tick=anchor.low_tick,
        center_id=None,
        center_zd_tick=None,
        center_zg_tick=None,
        center_ordinal=None,
        divergence=divergence,
    )
    before = build_strict_structure_snapshot(_evidence(), interval="5m")
    observation_after = build_strict_structure_snapshot(
        _evidence(observation=observation),
        interval="5m",
    )
    approaching_after = build_strict_structure_snapshot(
        _evidence(approaching_points=(approaching,)),
        interval="5m",
    )

    for after in (observation_after, approaching_after):
        assert before["structure_revision"] == after["structure_revision"]
        assert before["snapshot_revision"] == after["snapshot_revision"]
        assert before["render_revision"] != after["render_revision"]
