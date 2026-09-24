"""Serialize chart centers, stroke observations, points and divergence evidence."""

from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.cl_utils.point_exits import EXIT_PRICE_VERSION, with_point_exit_plans
from chanlun.core.strict_structure.center_frame import (
    center_frame_evidence,
    center_frame_leave,
    directional_ownership_pending,
)
from chanlun.core.strict_structure.models import (
    CenterPreview,
    CenterPreviewState,
    CenterState,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
)

CHART_STRUCTURE_SCHEMA = "chanlun-chart-structure"
CHART_CENTER_SCHEMA = "chanlun-chart-center"
COMPLETED_CENTER_DISPLAY_POLICY = "directional_completed_core_and_frame_v2"


def _unique_units_in_time_order(
    units: Iterable[ConstituentUnit],
) -> tuple[ConstituentUnit, ...]:
    """按市场时间返回不重复的中心角色证据。"""
    by_id: dict[str, ConstituentUnit] = {}
    for unit in units:
        by_id.setdefault(unit.unit_id, unit)
    return tuple(
        sorted(
            by_id.values(),
            key=lambda unit: (unit.market_start, unit.market_end, unit.unit_id),
        )
    )


def _center_overlap_units(center: TrendCenter) -> tuple[ConstituentUnit, ...]:
    """返回与冻结核心存在正宽重叠的全部中心生命周期角色。"""
    candidates = _unique_units_in_time_order(
        (
            *((center.entry_unit,) if center.entry_unit is not None else ()),
            *center.body_units,
            *(
                ()
                if center.establishment_leave_unit is None
                else (center.establishment_leave_unit,)
            ),
            *center.failed_departure_units,
            *(
                ()
                if center.lifecycle_leave_unit is None
                else (center.lifecycle_leave_unit,)
            ),
        )
    )
    return tuple(
        (
            item
            for item in candidates
            if max(item.low_tick, center.zd_tick) < min(item.high_tick, center.zg_tick)
        )
    )


def aware_datetime_to_epoch_seconds(value: datetime) -> int:
    """把带时区时间精确转换为 UTC Unix 秒坐标。"""
    if not isinstance(value, datetime):
        raise TypeError("chart time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("chart time must be timezone-aware")
    if value.microsecond:
        raise ValueError("chart time must have whole-second precision")
    return int(value.astimezone(timezone.utc).timestamp())


def _optional_epoch(value: datetime | None) -> int | None:
    return None if value is None else aware_datetime_to_epoch_seconds(value)


def _unit_audit_payload(unit: ConstituentUnit) -> dict[str, object]:
    """序列化一个严格单元，供中枢进入、离开证据审计展示。"""
    if not isinstance(unit, ConstituentUnit):
        raise TypeError("center audit unit must be a ConstituentUnit")
    return {
        "unit_id": unit.unit_id,
        "direction": unit.direction,
        "start_time": aware_datetime_to_epoch_seconds(unit.market_start),
        "end_time": aware_datetime_to_epoch_seconds(unit.market_end),
        "start_tick": unit.start_tick,
        "end_tick": unit.end_tick,
        "low_tick": unit.low_tick,
        "high_tick": unit.high_tick,
        "locked": unit.locked,
        "forming": unit.forming,
        "confirmed_at": _optional_epoch(unit.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(unit.available_at),
    }


def _center_lifecycle_payload(
    *,
    pending_leave: ConstituentUnit | None,
    completion_leave: ConstituentUnit | None,
    completed: bool,
    provisional: bool = False,
    observation: bool = False,
    departure_direction: str | None = None,
    independent_leave: bool = True,
    closed_state: CenterState | None = None,
) -> dict[str, object]:
    """序列化唯一的同级离开、回返生命周期契约。"""
    leave = completion_leave if completed else pending_leave
    expected_point_type = (
        None
        if leave is None
        else "3buy"
        if (departure_direction or leave.direction) == "up"
        else "3sell"
    )
    if observation:
        phase = "NON_TRADABLE_OBSERVATION"
        point_type = None
        point_status = None
    elif not independent_leave:
        phase = "SHARED_CORE_EXIT_INTERPRETATION"
        point_type = None
        expected_point_type = None
        point_status = "interpretation_only"
    elif completed and provisional:
        phase = "GEOMETRIC_THIRD_CLASS_POINT"
        point_type = expected_point_type
        point_status = "provisional"
    elif completed:
        phase = "FORMAL_THIRD_CLASS_POINT"
        point_type = expected_point_type
        point_status = "confirmed"
    elif closed_state in (CenterState.DIVERGENCE_CLOSED, CenterState.SUPERSEDED):
        phase = ("CLOSED_AT_DIVERGENCE" if closed_state is CenterState.DIVERGENCE_CLOSED
                 else "SUPERSEDED_BY_SUCCESSOR")
        point_type = None
        expected_point_type = None
        point_status = None
    elif pending_leave is not None:
        phase = "AWAITING_SAME_LEVEL_RETURN"
        point_type = None
        point_status = None
    else:
        phase = "AWAITING_SAME_LEVEL_DEPARTURE"
        point_type = None
        point_status = None
    return {
        "completion_phase": phase,
        "completion_point_type": point_type,
        "expected_completion_point_type": expected_point_type,
        "completion_point_status": point_status,
    }


def _center_frame_payload(center: TrendCenter) -> dict[str, object]:
    frame = center_frame_evidence(center)
    return {
        "third_class_confirmation_rule": "independent_leave_after_three_core_children",
        "minimum_third_class_role_count": 5,
        "third_class_confirmed": center.third_class_confirmed,
        "core_formed": frame["core_formed"],
        "frame_qualified": frame["frame_qualified"],
        "frame_missing_conditions": list(frame["frame_missing_conditions"]),
        "frame_leave_unit_id": frame["frame_leave_unit_id"],
        "frame_available_at": aware_datetime_to_epoch_seconds(frame["available_at"]),
        "center_ended": center.third_class_confirmed,
        "center_end_available_at": _optional_epoch(
            center.completion_available_at if center.third_class_confirmed else None
        ),
        "center_end_rule": "independent_completed_lower_leave_and_first_outside_return",
    }


def _center_payload(
    center: TrendCenter, *, render_kind: str, tradable: bool
) -> dict[str, object]:
    if not isinstance(center, TrendCenter):
        raise TypeError("center must be a TrendCenter")
    if not center.has_minimum_physical_roles:
        raise ValueError("chart center lacks its declared formation evidence")
    recursive = center.source_kind is SourceKind.TREND_TYPE
    if center.zd_tick > center.zg_tick or (not recursive and center.zd_tick == center.zg_tick):
        raise ValueError("formal chart center violates source overlap contract")
    leaving_unit = center_frame_leave(center)
    frame_end = center.display_range_end_market_time
    establishment_units = center.establishment_units
    overlap_units = _center_overlap_units(center)
    failed_revision = (
        ""
        if not center.failed_departure_units
        else f"@failed{len(center.failed_departure_units)}"
    )
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": render_kind,
        "center_id": center.center_id,
        "price_basis_revision": center.price_basis_revision,
        "render_id": f"{center.center_id}@{center.body_revision}{failed_revision}@{center.state.value}",
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "formation_rule": "recursive_three" if recursive else center.formation_rule,
        "boundary_contact": center.boundary_contact,
        "state": center.state.value,
        "tradable": bool(tradable and center.third_class_confirmed),
        **_center_frame_payload(center),
        **_center_lifecycle_payload(
            pending_leave=center.pending_leave_unit,
            completion_leave=center.completion_leave_unit,
            completed=center.physically_completed,
            observation=center.source_kind is SourceKind.STROKE_OBSERVATION,
            departure_direction=center.departure_direction(center.lifecycle_leave_unit)
            if center.lifecycle_leave_unit
            else None,
            independent_leave=center.lifecycle_leave_unit is None
            or center.has_independent_third_class_leave(center.lifecycle_leave_unit),
            closed_state=center.state,
        ),
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(
                    center.display_range_start_market_time
                ),
                "price_tick": center.zg_tick,
            },
            {
                "time": aware_datetime_to_epoch_seconds(frame_end),
                "price_tick": center.zd_tick,
            },
        ],
        "display_range": {
            "start_role": "middle_three_first_start",
            "end_role": "external_leave_start"
            if center.lifecycle_leave_unit is not None
            and center.lifecycle_leave_unit != center.establishment_leave_unit
            else "body_tail_end"
            if center.extension_units
            else "middle_three_last_end",
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        "directional_ownership_pending": directional_ownership_pending(center),
        "lifecycle_range_end": aware_datetime_to_epoch_seconds(
            center.display_range_end_market_time
        ),
        "frame_body_unit_ids": [
            u.unit_id
            for u in sorted(
                (*center.body_units, *center.failed_departure_units),
                key=lambda unit: unit.market_start,
            )
            if u.market_end <= frame_end
        ],
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "envelope": {"dd_tick": center.dd_tick, "gg_tick": center.gg_tick},
        "entry_unit_id": None
        if center.entry_unit is None
        else center.entry_unit.unit_id,
        "entry_role": None if center.entry_unit is None else "external_entry",
        "lifecycle_role_count": center.lifecycle_role_count,
        "minimum_lifecycle_role_count": 3 if recursive else 5,
        "core_component_count": 3,
        "overlap_component_count": len(overlap_units),
        "establishment_component_count": len(establishment_units),
        "establishment_segment_ids": [unit.unit_id for unit in establishment_units],
        "middle_three_component_ids": [unit.unit_id for unit in center.core_units],
        "core_unit_ids": [unit.unit_id for unit in center.core_units],
        "establishment_leave_unit_id": None
        if center.establishment_leave_unit is None
        else center.establishment_leave_unit.unit_id,
        "initial_exit_unit_id": None
        if center.establishment_leave_unit is None
        else center.establishment_leave_unit.unit_id,
        "initial_unit_ids": [unit.unit_id for unit in center.initial_units],
        "body_unit_ids": [unit.unit_id for unit in center.body_units],
        "extension_unit_ids": [unit.unit_id for unit in center.extension_units],
        "failed_departure_unit_ids": [
            unit.unit_id for unit in center.failed_departure_units
        ],
        "pending_leave_unit_id": None
        if center.pending_leave_unit is None
        else center.pending_leave_unit.unit_id,
        "completion_leave_unit_id": None
        if center.completion_leave_unit is None
        else center.completion_leave_unit.unit_id,
        "completion_return_unit_id": None
        if center.completion_return_unit is None
        else center.completion_return_unit.unit_id,
        "completion_direction": center.completion_direction,
        "boundary_divergence_id": center.boundary_divergence_id,
        "boundary_anchor_unit_id": center.boundary_anchor_unit_id,
        "superseded_by_center_id": center.superseded_by_center_id,
        "superseded_at": None
        if center.superseded_at is None
        else aware_datetime_to_epoch_seconds(center.superseded_at),
        "supersession_bridge_unit_ids": [
            item.unit_id for item in center.supersession_bridge_units
        ],
        "supersession_bridge_units": [
            _unit_audit_payload(item) for item in center.supersession_bridge_units
        ],
        "entering_segment": None
        if center.entry_unit is None
        else _unit_audit_payload(center.entry_unit),
        "middle_three_components": [
            _unit_audit_payload(unit) for unit in center.core_units
        ],
        "overlap_components": [_unit_audit_payload(unit) for unit in overlap_units],
        "establishment_segments": [
            _unit_audit_payload(unit) for unit in establishment_units
        ],
        "leaving_segment": None
        if leaving_unit is None
        else _unit_audit_payload(leaving_unit),
        "lifecycle_leaving_segment": None
        if center.lifecycle_leave_unit is None
        else _unit_audit_payload(center.lifecycle_leave_unit),
        "completion_return_segment": None
        if center.completion_return_unit is None
        else _unit_audit_payload(center.completion_return_unit),
        "established_market_time": aware_datetime_to_epoch_seconds(
            center.established_market_time
        ),
        "established_at": aware_datetime_to_epoch_seconds(center.established_at),
        "completed_at": _optional_epoch(center.completed_at),
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
    }


def strict_center_to_chart_dict(center: TrendCenter) -> dict[str, object]:
    """在不改变来源几何的前提下序列化正式中枢。"""
    if center.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal serializer rejects stroke observation")
    if not center.tradable:
        raise ValueError("formal chart center must be tradable")
    return _center_payload(center, render_kind="formal_center", tradable=True)


def strict_center_preview_to_chart_dict(
    preview: CenterPreview,
    units: dict[str, ConstituentUnit],
    owner: dict | None = None,
) -> dict | None:
    """Publish the selected unfinished tail without changing confirmed geometry.

    An entry and three overlapping core children are enough to draw a forming
    frame. Its independent exit and locked evidence remain mandatory for formal
    establishment. A projection of an existing center only draws its extension.
    """
    if (len(preview.unit_ids) < 3 or preview.zd_tick is None
            or preview.zg_tick is None or preview.state is CenterPreviewState.TOUCH_ONLY):
        return None
    recursive = preview.source_kind is SourceKind.TREND_TYPE
    body = tuple(units[key] for key in preview.unit_ids)
    core = body[:3]
    entry = units.get(preview.entry_unit_id)
    initial_exit = units.get(preview.establishment_leave_unit_id)
    pending = units.get(preview.pending_leave_unit_id)
    completion = units.get(preview.completion_leave_unit_id)
    leave = completion or pending
    failed = tuple(units[key] for key in preview.failed_departure_unit_ids)
    establishment = core if recursive else tuple(
        value for value in (entry, *core, initial_exit) if value is not None
    )
    frame_leave = leave or initial_exit or next(iter(failed), None)
    end = leave.market_start if leave is not None else body[-1].market_end
    start_time = aware_datetime_to_epoch_seconds(core[0].market_start)
    end_time = aware_datetime_to_epoch_seconds(end)
    status = ("awaiting_leave" if not recursive and initial_exit is None
              else "awaiting_completion_confirmation" if completion is not None
              else "extending" if owner is not None
              else "awaiting_segment_confirmation")
    center_id = preview.formal_center_id or stable_structure_id(
        "chart-forming-core", preview.price_basis_revision, preview.structural_level,
        preview.entry_unit_id, preview.unit_ids[:3], preview.zd_tick, preview.zg_tick,
    )
    if owner is not None:
        start_time = max(start_time, owner["points"][-1]["time"])
    result = {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "center_preview",
        "center_id": center_id,
        "owner_center_id": None if owner is None else owner["center_id"],
        "draw_geometry": end_time > start_time,
        "price_basis_revision": preview.price_basis_revision,
        "structural_level": preview.structural_level,
        "source_kind": preview.source_kind.value,
        "formation_rule": "recursive_three" if recursive else "five_role",
        "state": "forming",
        "geometry_state": preview.state.value,
        "preview_status": status,
        "tradable": False,
        "third_class_confirmed": False,
        "center_ended": False,
        "center_end_available_at": None,
        "core_formed": True,
        "frame_qualified": recursive or frame_leave is not None,
        "frame_missing_conditions": [] if recursive or frame_leave is not None else ["leave_unavailable"],
        "frame_leave_unit_id": None if frame_leave is None else frame_leave.unit_id,
        "core": {"zd_tick": preview.zd_tick, "zg_tick": preview.zg_tick},
        "points": [{"time": start_time, "price_tick": preview.zg_tick},
                   {"time": max(start_time, end_time), "price_tick": preview.zd_tick}],
        "entry_unit_id": preview.entry_unit_id,
        "establishment_leave_unit_id": preview.establishment_leave_unit_id,
        "initial_exit_unit_id": preview.establishment_leave_unit_id,
        "core_component_count": 3,
        "core_unit_ids": list(preview.unit_ids[:3]),
        "initial_unit_ids": list(preview.unit_ids[:3]),
        "body_unit_ids": list(preview.unit_ids),
        "failed_departure_unit_ids": list(preview.failed_departure_unit_ids),
        "establishment_segment_ids": [value.unit_id for value in establishment],
        "establishment_component_count": len(establishment),
        "overlap_component_count": len(establishment),
        "lifecycle_role_count": len(establishment),
        "minimum_lifecycle_role_count": 3 if recursive else 5,
        "entering_segment": None if entry is None else _unit_audit_payload(entry),
        "middle_three_components": [_unit_audit_payload(value) for value in core],
        "leaving_segment": None if frame_leave is None else _unit_audit_payload(frame_leave),
        "available_at": aware_datetime_to_epoch_seconds(preview.available_at),
    }
    result["render_id"] = stable_structure_id("chart-center-preview", result)
    return result


def strict_divergence_to_chart_dict(divergence: DivergenceEvidence) -> dict:
    if not isinstance(divergence, DivergenceEvidence):
        raise TypeError("divergence must be a DivergenceEvidence")
    return {
        "schema": "chanlun-chart-divergence",
        "render_kind": "strict_divergence",
        "render_id": stable_structure_id("chart-divergence-anchor", divergence.divergence_id,
                                         divergence.price_anchor_unit_id, divergence.anchor_at,
                                         divergence.anchor_tick, divergence.available_at),
        "divergence_id": divergence.divergence_id,
        "structural_level": divergence.structural_level,
        "source_kind": divergence.source_kind.value,
        "price_basis_revision": divergence.price_basis_revision,
        "kind": divergence.kind,
        "direction": divergence.direction,
        "compare_unit_id": divergence.compare_unit_id,
        "signal_unit_id": divergence.signal_unit_id,
        "comparison_width": divergence.comparison_width,
        "signal_width": divergence.signal_width,
        "compare_leg_unit_ids": list(divergence.compare_leg_unit_ids),
        "signal_leg_unit_ids": list(divergence.signal_leg_unit_ids),
        "price_anchor_unit_id": divergence.price_anchor_unit_id,
        "anchor_at": aware_datetime_to_epoch_seconds(divergence.anchor_at),
        "anchor_tick": divergence.anchor_tick,
        "confirmed_at": aware_datetime_to_epoch_seconds(divergence.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(divergence.available_at),
        "metrics": {
            name: getattr(divergence, name)
            for name in (
                "price_extreme_confirmed", "histogram_area_decayed",
                "histogram_peak_decayed", "dif_extreme_decayed", "strength_source",
                "is_divergent", "strength_decay_count",
            )
        },
        "points": [{"time": aware_datetime_to_epoch_seconds(divergence.anchor_at),
                    "price_tick": divergence.anchor_tick}],
    }


def strict_point_to_chart_dict(point: StrictPointEvidence) -> dict:
    if not isinstance(point, StrictPointEvidence):
        raise TypeError("point must be StrictPointEvidence")
    confirmed = point.status is StrictPointStatus.CONFIRMED
    revision = stable_structure_id(
        "chart-point-evidence", point.point_id, point.status.value,
        point.variant.value, point.available_at, point.evidence_codes,
        point.missing_conditions, point.related_point_ids,
        point.price_anchor_unit_id, point.anchor_at, point.anchor_tick, point.invalidation_tick,
    )
    return {
        "schema": "chanlun-chart-point",
        "render_kind": "point_confirmed" if confirmed else "point_approaching",
        "render_id": f"{point.point_id}@{revision}",
        "evidence_revision": revision,
        "point_id": point.point_id,
        "point_type": point.point_type,
        "side": point.side,
        "status": point.status.value,
        "strict_status": point.status.value,
        "variant": point.variant.value,
        "structural_level": point.structural_level,
        "source_kind": point.source_kind.value,
        "price_basis_revision": point.price_basis_revision,
        "anchor_unit_id": point.anchor_unit_id,
        "price_anchor_unit_id": point.price_anchor_unit_id,
        "anchor_at": aware_datetime_to_epoch_seconds(point.anchor_at),
        "anchor_tick": point.anchor_tick,
        "invalidation_tick": point.invalidation_tick,
        "confirmed_at": _optional_epoch(point.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(point.available_at),
        "center_id": point.center_id,
        "center_zd_tick": point.center_zd_tick,
        "center_zg_tick": point.center_zg_tick,
        "center_ordinal": point.center_ordinal,
        "parent_point_id": point.parent_point_id,
        "evidence_codes": list(point.evidence_codes),
        "missing_conditions": list(point.missing_conditions),
        "related_point_ids": list(point.related_point_ids),
        "small_to_large_carrier_unit_ids": list(point.small_to_large_carrier_unit_ids),
        "divergence": None if point.divergence is None else strict_divergence_to_chart_dict(point.divergence),
        "points": [{"time": aware_datetime_to_epoch_seconds(point.anchor_at),
                    "price_tick": point.anchor_tick}],
    }


def _canonical_quantum(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("structure_price_quantum must be a positive Decimal")
    return format(value.normalize(), "f")


def _tick_price(tick: int, quantum: Decimal) -> float:
    if type(tick) is not int:
        raise TypeError("price_tick must be an integer")
    value = Decimal(tick) * quantum
    if not value.is_finite():
        raise ValueError("chart price must be finite")
    return float(value)


def _with_prices(value: object, quantum: Decimal) -> object:
    if isinstance(value, list):
        return [_with_prices(item, quantum) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _with_prices(item, quantum) for (key, item) in value.items()}
    tick_price_names = {
        "price_tick": "price",
        "anchor_tick": "anchor_price",
        "invalidation_tick": "invalidation_price",
        "center_zd_tick": "center_zd_price",
        "center_zg_tick": "center_zg_price",
        "zd_tick": "zd_price",
        "zg_tick": "zg_price",
        "dd_tick": "dd_price",
        "gg_tick": "gg_price",
        "low_tick": "low_price",
        "high_tick": "high_price",
        "start_tick": "start_price",
        "end_tick": "end_price",
    }
    for tick_name, price_name in tick_price_names.items():
        tick = value.get(tick_name)
        if tick is not None:
            result[price_name] = _tick_price(tick, quantum)
    return result


def _conditional_center_payload(center, observation):
    """只有条件几何和作用范围；局部证明不冒充正式中枢/三买卖确认。"""
    payload = {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "conditional_center",
        "center_id": center.center_id,
        "observation_scope_id": observation.scope_id,
        "component_index": observation.component_index,
        "price_basis_revision": center.price_basis_revision,
        "structural_level": 0,
        "source_kind": "segment",
        "state": "selection_pending",
        "selection_pending": True,
        "tradable": False,
        "locked": False,
        "third_class_confirmed": False,
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "points": [
            {"time": aware_datetime_to_epoch_seconds(center.display_range_start_market_time), "price_tick": center.zg_tick},
            {"time": aware_datetime_to_epoch_seconds(center.display_range_end_market_time), "price_tick": center.zd_tick},
        ],
    }
    payload["render_id"] = stable_structure_id("conditional-center-v1", payload)
    return payload


def build_center_snapshot(cd, *, interval: str, display_bar_closed_at: tuple[int, ...]):
    if interval != cd.get_frequency():
        raise ValueError("native center interval must match its source bars")
    cutoff = aware_datetime_to_epoch_seconds(cd._strict_as_of())
    # Display geometry keeps the provider's labels. For start-labelled bars,
    # the completed input's causal cutoff is one interval after its last label.
    display_end = aware_datetime_to_epoch_seconds(cd.get_src_klines()[-1].date)
    if (
        not display_bar_closed_at
        or display_bar_closed_at[-1] != display_end
        or any(
            (
                left >= right
                for (left, right) in zip(
                    display_bar_closed_at, display_bar_closed_at[1:]
                )
            )
        )
    ):
        raise ValueError("display bars must be ordered and end at the source cutoff")
    quantum = cd._strict_price_quantum()
    evidence = cd.get_strict_evidence()
    levels = []
    for level in evidence.structure.levels:
        depth = level.structural_level
        centers = [
            strict_center_to_chart_dict(center)
            for center in level.center_result.centers
            if center.available_at <= cd._strict_as_of()
        ]
        by_id = {center["center_id"]: center for center in centers}
        units = {unit.unit_id: unit for unit in level.units}
        previews = [item for preview in level.center_result.previews
                    if preview.available_at <= cd._strict_as_of()
                    if (item := strict_center_preview_to_chart_dict(
                        preview, units, by_id.get(preview.formal_center_id))) is not None]
        levels.append({
            "structural_level": depth,
            "label": interval if depth == 0 else f"{interval}/L{depth}",
            "origin": "native_segments" if depth == 0 else "completed_lower_structures",
            "centers": [_with_prices(center, quantum) for center in centers],
            "center_previews": [_with_prices(preview, quantum) for preview in previews],
            "points": [
                _with_prices(strict_point_to_chart_dict(point), quantum)
                for point in (*evidence.confirmed_points, *evidence.approaching_points)
                if point.structural_level == depth and point.available_at <= cd._strict_as_of()
            ],
            "divergences": [
                _with_prices(strict_divergence_to_chart_dict(divergence), quantum)
                for divergence in evidence.divergences
                if divergence.structural_level == depth and divergence.available_at <= cd._strict_as_of()
            ],
        })
    if not levels:
        levels.append({"structural_level": 0, "label": interval, "origin": "native_segments",
                       "centers": [], "center_previews": [], "points": [], "divergences": []})
    observations = [
        _with_prices(_center_payload(center, render_kind="center_observation", tradable=False), quantum)
        for center in evidence.stroke_center_observations.centers
        if center.available_at <= cd._strict_as_of()
    ]
    construction = (cd.get_stroke_construction_state()
                    if hasattr(cd, "get_stroke_construction_state") else {})
    connection_pending = bool(construction.get("unresolved_regions"))
    conditional = [
        _with_prices(_conditional_center_payload(center, observation), quantum)
        for observation, result in (cd.get_conditional_centers() if hasattr(cd, "get_conditional_centers") else ())
        for center in result.centers if center.available_at <= cd._strict_as_of()
    ]
    segment_construction = (cd.get_segment_construction_state()
                            if hasattr(cd, "get_segment_construction_state") else {})
    tail = segment_construction.get("tail")
    unresolved_segment_ranges = []
    if segment_construction.get("status") == "unresolved" and tail:
        region_id = stable_structure_id("unresolved-segment-tail-v1", cd.get_code(), interval,
                                        cd._strict_price_basis_revision(), tail["start_time"], tail["start_price"])
        unresolved_segment_ranges.append({
            "render_kind": "segment_unresolved_range", "region_id": region_id,
            "render_id": stable_structure_id(region_id, tail),
            "structural_level": 0, "price_basis_revision": cd._strict_price_basis_revision(),
            "available_at": cutoff, "state": "unresolved", "tradable": False,
            "direction": tail["direction"], "reason": tail["reason"],
            "pen_count": tail["pen_count"], "confirmed_pen_count": tail["confirmed_pen_count"],
            "points": [{"time": tail["start_time"], "price": tail["high"]},
                       {"time": tail["observed_through"], "price": tail["low"]}],
        })
    revision = stable_structure_id(
        "chart-analysis-snapshot-v7",
        EXIT_PRICE_VERSION,
        cd.get_code(),
        interval,
        cd._strict_config_revision(),
        cutoff,
        levels, observations, connection_pending, conditional, segment_construction,
    )
    return with_point_exit_plans({
        "schema": CHART_STRUCTURE_SCHEMA,
        "analysis_scope": "centers_and_signals",
        "symbol": cd.get_code(),
        "source_frequency": interval,
        "display_frequency": interval,
        "source_closed_at": cutoff,
        "price_basis_revision": cd._strict_price_basis_revision(),
        "structure_price_quantum": _canonical_quantum(quantum),
        "strict_config_revision": cd._strict_config_revision(),
        "structure_revision": revision,
        "snapshot_revision": revision,
        "render_revision": revision,
        "levels": levels,
        "stroke_center_observations": observations,
        "stroke_connection_pending": connection_pending,
        "conditional_centers": conditional,
        "segment_construction": segment_construction,
        "unresolved_segment_ranges": unresolved_segment_ranges,
    })


__all__ = [
    "CHART_STRUCTURE_SCHEMA",
    "CHART_CENTER_SCHEMA",
    "build_center_snapshot",
    "strict_center_to_chart_dict",
]
