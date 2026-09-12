"""Serialize the native segment centers of one displayed interval."""

from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.center_frame import (
    center_frame_evidence,
    center_frame_leave,
)
from chanlun.core.strict_structure.models import (
    ConstituentUnit,
    SourceKind,
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
    if center.zd_tick >= center.zg_tick:
        raise ValueError("formal chart center violates source overlap contract")
    leaving_unit = center_frame_leave(center)
    from chanlun.core.strict_structure.center_frame import (
        directional_frame_end,
        directional_ownership_pending,
    )

    frame_end = directional_frame_end(center)
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
        "render_id": f"{center.center_id}@{center.body_revision}{failed_revision}@{center.state.value}",
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "formation_rule": center.formation_rule,
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
            "end_role": "directional_leave_start_ownership_pending"
            if directional_ownership_pending(center)
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
        "minimum_lifecycle_role_count": 5,
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
        "boundary_divergence_id": None,
        "boundary_anchor_unit_id": None,
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


def build_center_snapshot(cd, *, interval: str, display_bar_closed_at: tuple[int, ...]):
    if interval != cd.get_frequency():
        raise ValueError("native center interval must match its source bars")
    cutoff = aware_datetime_to_epoch_seconds(cd._strict_as_of())
    if (
        not display_bar_closed_at
        or display_bar_closed_at[-1] != cutoff
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
    result = cd.get_native_centers()
    centers = [
        _with_prices(strict_center_to_chart_dict(center), quantum)
        for center in result.centers
        if center.available_at <= cd._strict_as_of()
    ]
    revision = stable_structure_id(
        "native-center-snapshot-v2",
        cd.get_code(),
        interval,
        cd._strict_config_revision(),
        cutoff,
        centers,
    )
    return {
        "schema": CHART_STRUCTURE_SCHEMA,
        "analysis_scope": "native_centers",
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
        "levels": [
            {
                "structural_level": 0,
                "label": interval,
                "origin": "native_segments",
                "centers": centers,
            }
        ],
    }


__all__ = [
    "CHART_STRUCTURE_SCHEMA",
    "CHART_CENTER_SCHEMA",
    "build_center_snapshot",
    "strict_center_to_chart_dict",
]
