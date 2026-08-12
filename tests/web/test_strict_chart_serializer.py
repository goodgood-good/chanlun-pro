from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.cl_utils.strict_chart import (
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
from tests.core.strict_structure.helpers import (
    completed_up_center,
    engine_for,
    ongoing_center,
)
from tests.trading_system.strict_helpers import strict_point


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
        # 初始离开没有得到外侧回抽确认，u5 重新进入核心；这两段此时
        # 才折叠为同一个中枢的延伸，图框仍不会包含外部进入段。
        value, _event = advance_center(
            value,
            _unit(5, "down", 130, 110, source_kind=source_kind),
        )
    return value


def _trend(center: TrendCenter) -> TrendType:
    units = center.body_units
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
                center.entry_unit,
                *center.establishment_units,
                *center.body_units,
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


def test_center_payload_exposes_five_segment_and_middle_core_roles() -> None:
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
    assert payload["establishment_unit_id"] == center.establishment_unit.unit_id
    assert payload["initial_exit_unit_id"] == center.initial_exit_unit.unit_id
    assert payload["completion_leave_unit_id"] == center.completion_leave_unit.unit_id
    assert payload["completion_return_unit_id"] == center.completion_return_unit.unit_id
    assert payload["entering_segment"]["unit_id"] == center.entry_unit.unit_id
    assert payload["entry_role"] == "external_entry"
    assert payload["overlap_component_count"] == 5
    assert payload["first_three_component_ids"] == [
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
        == payload["first_three_components"][0]["start_time"]
    )
    assert (
        payload["points"][1]["time"]
        == payload["first_three_components"][-1]["end_time"]
    )
    assert payload["display_range"]["includes_entry"] is False
    assert payload["display_range"]["includes_leave"] is False


def test_center_payload_exposes_fifth_maturity_extension_without_leave() -> None:
    values = (
        _unit(0, "up", 90, 120),
        _unit(1, "down", 120, 100),
        _unit(2, "up", 100, 115),
        _unit(3, "down", 115, 105),
        _unit(4, "up", 105, 110),
    )
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None

    payload = strict_center_to_chart_dict(center)

    assert payload["establishment_unit_id"] == values[4].unit_id
    assert payload["initial_exit_unit_id"] is None
    assert payload["pending_leave_unit_id"] is None
    assert payload["leaving_segment"] is None
    assert payload["establishment_segment_ids"] == [item.unit_id for item in values]
    assert payload["extension_unit_ids"] == [values[4].unit_id]
    assert payload["body_unit_ids"] == [item.unit_id for item in values[1:]]
    assert payload["points"][1]["time"] == int(values[4].market_end.timestamp())


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
    assert payload["first_three_components"][0]["start_price"] == float(
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
    assert projection["overlap_component_count"] >= 5
    assert projection["establishment_segment_ids"] == [
        item.unit_id for item in center.establishment_units
    ]
    assert projection["establishment_unit_id"] == (center.establishment_unit.unit_id)
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

    latest_ongoing = ongoing_center(60)
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
    assert payload["establishment_unit_id"] is None
    assert payload["lifecycle_role_count"] == 4
    assert payload["minimum_lifecycle_role_count"] == 5
    assert payload["established_market_time"] is None
    assert payload["established_at"] is None


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
    assert preview.unit_ids == tuple(unit.unit_id for unit in units[1:])

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
    assert point["point_id"] == source_point.point_id
    assert point["point_type"] == "3buy"
    assert point["status"] == "confirmed"
    assert point["center_ordinal"] == 1


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
