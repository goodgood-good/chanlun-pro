"""Serialize chart centers, stroke observations, points and divergence evidence."""

from __future__ import annotations
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable
from zoneinfo import ZoneInfo
from chanlun.exchange.trading_session import official_trading_session_evidence
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.same_level_decomposition import (
    fixed_three_member_partition_payload,
    l038_six_member_split_payload,
    observe_l038_six_member_split,
    review_fixed_three_member_partition,
)
from chanlun.core.strict_structure.general_decomposition import (
    GapBarEvidence,
    associative_view_payload,
    discover_entry_anchored_associations,
    general_center_connector_observation_payload,
    observe_explicit_general_center_connector,
    materialize_declared_upgrade_child_path,
    declared_upgrade_child_path_payload,
    replay_declared_upgrade_parent_geometry,
)
from chanlun.core.strict_structure.unit_adapter import _tick
from chanlun.core.strict_structure.operational_risk import macd_zero_axis_operational_state
from chanlun.core.strict_structure.gap_strength import observe_u297_gap_strength
from chanlun.core.strict_structure.physical_stroke_lineage import (
    observe_class_formal_component_candidates,
    observe_physical_segment_stroke_lineage,
)
from chanlun.core.strict_structure.nine_segment_grade_watch import (
    observe_u180_nine_segment_grade_watches,
)
from chanlun.core.strict_structure.macd_cross_frequency import observe_u293_macd_cross_frequency
from chanlun.core.strict_structure.nested_divergence_lineage import (
    observe_completed_nested_divergence_lineage,
    observe_nested_candidate_history_lineage,
)
from chanlun.core.strict_structure.live_divergence_candidate import (
    observe_live_center_divergence_candidate,
    observe_live_trend_divergence_comparisons,
)
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
    StrictLevelResult,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
    center_overlap_interpretation,
)
from chanlun.core.strict_structure.rule_state import (
    RULE_STATE_VERSION,
    center_first_return_grade_watch_states,
    third_point_entry_precision_rule_states,
    center_pair_rule_state,
    center_oscillation_monitor_state,
    center_oscillation_operation_rule_states,
    center_macd_phase_rule_states,
    nested_change_watch_rule_states,
    second_buy_followup_observations,
    small_to_large_point_rule_state,
    trend_formation_rule_states,
    transition_rule_state,
)
from chanlun.core.strict_structure.strength import MacdStrengthProvider
from chanlun.core.strict_structure.native_center_signals import build_native_center_signals

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
    """Return lifecycle roles under the center's declared source overlap policy."""
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
            if (
                max(item.low_tick, center.zd_tick)
                <= min(item.high_tick, center.zg_tick)
                if center.source_kind is SourceKind.TREND_TYPE
                or center.formation_rule == "three_core"
                or center.zd_tick == center.zg_tick
                else max(item.low_tick, center.zd_tick)
                < min(item.high_tick, center.zg_tick)
            )
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
    if center.formation_rule == "three_core":
        leave = center_frame_leave(center)
        frame = {
            **frame,
            "core_formed": True,
            "frame_qualified": leave is not None,
            "frame_missing_conditions": (() if leave is not None else ("leave_unavailable",)),
            "frame_leave_unit_id": None if leave is None else leave.unit_id,
            "available_at": max(
                center.established_at,
                *(unit.available_at for unit in center.initial_units),
                *((leave.available_at,) if leave is not None else ()),
            ),
        }
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
        "center_end_rule": "independent_completed_lower_leave_and_first_disjoint_return",
    }


def _center_payload(
    center: TrendCenter, *, render_kind: str, tradable: bool
) -> dict[str, object]:
    if not isinstance(center, TrendCenter):
        raise TypeError("center must be a TrendCenter")
    if not center.has_minimum_physical_roles:
        raise ValueError("chart center lacks its declared formation evidence")
    recursive = center.source_kind is SourceKind.TREND_TYPE
    three_seed = recursive or center.formation_rule == "three_core"
    overlap_policy, source_differences = center_overlap_interpretation(
        center.source_kind, center.zd_tick, center.zg_tick
    )
    if center.zd_tick > center.zg_tick:
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
        "runtime_overlap_policy": overlap_policy,
        "unresolved_source_difference_ids": list(source_differences),
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
            "start_role": (
                "first_three_start" if center.formation_rule == "three_core"
                else "middle_three_first_start"
            ),
            "end_role": "external_leave_start"
            if center.lifecycle_leave_unit is not None
            and center.lifecycle_leave_unit != center.establishment_leave_unit
            else "body_tail_end"
            if center.extension_units
            else "middle_three_last_end",
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": (
                "first_three_intersection" if center.formation_rule == "three_core"
                else "middle_three_intersection"
            ),
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
        "minimum_lifecycle_role_count": 3 if three_seed else 5,
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
        "higher_center_member_trend_ids": (
            [unit.unit_id for unit in center.initial_units] if recursive else []
        ),
        "higher_center_member_confirmed_at": (
            [_optional_epoch(unit.confirmed_at) for unit in center.initial_units]
            if recursive else []
        ),
        "higher_center_formal_at": (
            aware_datetime_to_epoch_seconds(center.established_at)
            if recursive else None
        ),
        "higher_center_formal_member_gate": (
            "three_locked_lower_types" if recursive else "not_recursive_center"
        ),
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

    Source-specific seeds keep five-role and selected three-core previews
    separate. A projection of an existing center only draws its extension.
    """
    if (len(preview.unit_ids) < 3 or preview.zd_tick is None
            or preview.zg_tick is None or preview.state is CenterPreviewState.TOUCH_ONLY):
        return None
    recursive = (
        preview.source_kind is SourceKind.TREND_TYPE
        or preview.formation_rule == "three_core"
    )
    overlap_policy, source_differences = center_overlap_interpretation(
        preview.source_kind, preview.zd_tick, preview.zg_tick
    )
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
        "runtime_overlap_policy": overlap_policy,
        "unresolved_source_difference_ids": list(source_differences),
        "formation_rule": (
            "three_core" if preview.formation_rule == "three_core"
            else "recursive_three" if recursive else "five_role"
        ),
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
        "comparison_center_id": divergence.comparison_center_id,
        "comparison_center_established_at": _optional_epoch(
            divergence.comparison_center_established_at
        ),
        "comparison_center_known_at": _optional_epoch(
            divergence.comparison_center_known_at
        ),
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
        "large_turn_certificate": small_to_large_point_rule_state(point),
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


def _trend_type_rule_payload(trend) -> dict[str, object]:
    formal_type_qualified = bool(trend.centers)
    return {
        "trend_id": trend.trend_id,
        "kind": trend.kind.value if formal_type_qualified else "geometric_movement",
        "formal_type_qualified": formal_type_qualified,
        "direction": trend.trend_direction,
        "trend_direction": trend.trend_direction,
        "movement_orientation": trend.movement_orientation,
        "state": trend.state.value,
        "structural_level": trend.structural_level,
        "center_ids": [center.center_id for center in trend.centers],
        "constituent_unit_ids": [unit.unit_id for unit in trend.constituent_units],
        "market_start": aware_datetime_to_epoch_seconds(trend.market_start),
        "market_end": aware_datetime_to_epoch_seconds(trend.market_end),
        "confirmed_at": _optional_epoch(trend.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(trend.available_at),
        "terminal_divergence_id": (
            trend.terminal_divergence.divergence_id
            if trend.terminal_divergence is not None else None
        ),
    }


def _formal_trend_chart_payload(trend) -> dict[str, object]:
    """Draw a type with an established center using its audited endpoints."""
    if not trend.centers:
        raise ValueError("chart trend requires a formal center")
    return {
        "schema": "chanlun-chart-trend",
        "render_kind": "strict_trend",
        "trend_id": trend.trend_id,
        "render_id": f"{trend.trend_id}@{trend.state.value}@{trend.terminal_unit.unit_id}",
        "price_basis_revision": trend.price_basis_revision,
        "structural_level": trend.structural_level,
        "source_kind": trend.terminal_unit.source_kind.value,
        "kind": trend.kind.value,
        "direction": trend.trend_direction,
        "movement_orientation": trend.movement_orientation,
        "state": trend.state.value,
        "tradable": False,
        "center_ids": [center.center_id for center in trend.centers],
        "constituent_unit_ids": [unit.unit_id for unit in trend.constituent_units],
        "points": [
            {"time": aware_datetime_to_epoch_seconds(trend.market_start),
             "price_tick": trend.start_tick},
            {"time": aware_datetime_to_epoch_seconds(trend.market_end),
             "price_tick": trend.end_tick},
        ],
        "range": {"low_tick": trend.low_tick, "high_tick": trend.high_tick},
        "confirmed_at": _optional_epoch(trend.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(trend.available_at),
    }


def _trend_partition_review(level, visible_centers, visible_trends, as_of) -> dict[str, object]:
    """Separate local types from a proven whole-window same-level partition."""

    source_units = tuple(unit for unit in level.units if unit.available_at <= as_of)
    owned_ids = {
        unit.unit_id for trend in visible_trends
        for unit in trend.constituent_units if unit.available_at <= as_of
    }
    unowned_ids = [unit.unit_id for unit in source_units if unit.unit_id not in owned_ids]
    formal_count = sum(bool(trend.centers) for trend in visible_trends)
    geometric_count = len(visible_trends) - formal_count
    first_formal = next((trend for trend in visible_trends if trend.centers), None)
    first_formal_offset = (
        next(
            index for index, unit in enumerate(source_units)
            if unit.unit_id == first_formal.constituent_units[0].unit_id
        )
        if first_formal is not None else None
    )
    leading_unowned_ids = (
        [
            unit.unit_id for unit in source_units[:first_formal_offset]
            if unit.unit_id not in owned_ids
        ]
        if first_formal_offset is not None else []
    )
    first_formal_connection = (
        "no_formal_type" if first_formal_offset is None else
        "starts_at_recorded_source" if first_formal_offset == 0 else
        "unowned_prefix_before_first_formal_type" if leading_unowned_ids else
        "preceded_by_nonformal_movement"
    )
    first_center = visible_centers[0] if visible_centers else None
    left_edge_candidate = bool(
        source_units and first_center is not None
        and first_center.entry_unit is None
        and first_center.body_units[0].unit_id == source_units[0].unit_id
    )
    status = (
        "no_visible_source_units" if not source_units else
        "formal_types_with_open_left_partition" if formal_count and first_formal_offset else
        "formal_types_with_pending_source" if formal_count and unowned_ids else
        "formal_types_cover_visible_source" if formal_count else
        "geometric_movements_only" if geometric_count else
        "pending_connected_type_partition" if visible_centers else
        "no_formal_center_or_type"
    )
    return {
        "status": status,
        "source_unit_count": len(source_units),
        "formal_center_count": len(visible_centers),
        "formal_type_count": formal_count,
        "geometric_movement_count": geometric_count,
        "unowned_source_unit_ids": unowned_ids,
        "unowned_leading_source_unit_ids": leading_unowned_ids,
        "first_formal_type_connection": first_formal_connection,
        "open_left_boundary_candidate": left_edge_candidate,
        "formal_type_absence_proves_market_has_no_type": False,
        "tradable": False,
    }


def _l038_six_member_split_observations(level, as_of) -> list[dict[str, object]]:
    """Keep bounded fixed same-level splits apart from lifecycle centers."""

    if level.structural_level < 1:
        return []
    source = tuple(level.units)
    output = []
    for offset in range(max(0, len(source) - 5)):
        window = source[offset : offset + 6]
        if any(
            not unit.locked or unit.available_at > as_of or not unit.child_ids
            for unit in window
        ):
            continue
        observed = observe_l038_six_member_split(window, as_of=as_of)
        if observed is not None:
            output.append(l038_six_member_split_payload(observed))
    return output


def _declared_30m_fixed_same_level_review(
    lower_types, source_units, as_of, basis,
    pending_lower_type_ids=frozenset(),
) -> dict:
    """Apply the user's 30m operating view to completed 5m-type source units.

    The live recursive grade is kept separate from a physical bar frequency.
    This view is selected by the explicit 5m-lower/30m-operating declaration;
    its local triples do not overwrite the existing center lifecycle or points.
    """

    source = tuple(source_units)
    visible_prefix = []
    for unit in source:
        if unit.available_at > as_of:
            break
        visible_prefix.append(unit.unit_id)
    view_id = stable_structure_id(
        "chanlun-declared-30m-fixed-same-level-v1", basis,
        tuple(visible_prefix),
    )
    if source:
        pending_lower_type_ids = frozenset(pending_lower_type_ids) | frozenset(
            trend.trend_id
            for trend in lower_types
            if trend.kind.value == "consolidation" and len(trend.centers) > 1
        )
        payload = fixed_three_member_partition_payload(
            review_fixed_three_member_partition(
                source, view_id=view_id,
                operating_type_label="30m", as_of=as_of,
                pending_lower_type_ids=pending_lower_type_ids.intersection(
                    unit.unit_id for unit in source
                ),
            )
        )
    else:
        payload = {
            "schema": "chanlun-fixed-three-member-partition-review-v1",
            "rule_source_refs": ["L018:31-55", "L038:81-94", "L038:175-196"],
            "policy_basis": "declared_fixed_same_level_implementation",
            "view_id": view_id,
            "operating_type_label": "30m",
            "target_structural_level": 1,
            "lower_trend_structural_level": 0,
            "source_unit_ids": [],
            "status": "awaiting_three_completed_lower_types",
            "as_of": int(as_of.timestamp()),
            "candidates": [],
            "post_gap_center_candidates": [],
            "pending_source_unit_ids": [
                trend.trend_id
                for trend in lower_types
                if trend.locked and trend.available_at <= as_of
            ],
            "unavailable_future_count": 0,
            "whole_path_partition_certified": False,
            "formal_point_from_review": False,
            "tradable": False,
        }
    payload.update({
        "source_frequency": "5m",
        "target_operating_type": "30m",
        "lower_construction_policy": "center_extension_and_connected_trend_types",
        "formal_30m_type_issued": False,
    })
    return payload


def _declared_upgrade_review_payload(trend, parent_level, depth, as_of) -> dict:
    view_id = stable_structure_id(
        "chanlun-declared-upgrade-split-view-v1",
        trend.price_basis_revision, depth, trend.trend_id,
    )
    path = materialize_declared_upgrade_child_path(
        trend, view_id=view_id, as_of=as_of,
    )
    payload = declared_upgrade_child_path_payload(path)
    payload["parent_geometry_replay"] = (
        None if parent_level is None else
        replay_declared_upgrade_parent_geometry(path, parent_level, as_of=as_of)
    )
    return payload


def _decomposition_boundary_rule_payload(boundary) -> dict[str, object]:
    market_boundary = aware_datetime_to_epoch_seconds(boundary.anchor_at)
    return {
        "boundary_id": boundary.boundary_id,
        "view_id": boundary.decomposition_mode,
        "boundary_kind": boundary.boundary_kind,
        "structural_level": boundary.structural_level,
        "left_trend_id": boundary.left_trend_id,
        "terminal_center_id": boundary.terminal_center_id,
        "divergence_id": boundary.divergence.divergence_id,
        "shared_market_boundary_at": market_boundary,
        "previous_type_end_at": market_boundary,
        "next_type_start_at": market_boundary,
        "boundary_confirmed_at": aware_datetime_to_epoch_seconds(boundary.confirmed_at),
        "boundary_available_at": aware_datetime_to_epoch_seconds(boundary.available_at),
        "policy_basis": "same_grade_shared_divergence_point_P132",
    }


def _center_pair_rule_payload(previous: TrendCenter, current: TrendCenter) -> dict[str, object]:
    state = center_pair_rule_state(previous, current)
    state["previous_center_initial_known_at"] = _optional_epoch(
        state["previous_center_initial_known_at"]
    )
    state["current_center_initial_known_at"] = _optional_epoch(
        state["current_center_initial_known_at"]
    )
    state["touch_known_at"] = _optional_epoch(state["touch_known_at"])
    state["touch_witness_known_at"] = _optional_epoch(
        state["touch_witness_known_at"]
    )
    state["strict_relation_known_at"] = _optional_epoch(state["strict_relation_known_at"])
    return state


def _transition_rule_payload(level, confirmed_points, cutoff) -> dict[str, object]:
    state = transition_rule_state(level, tuple(confirmed_points), cutoff)
    for key in (
        "previous_type_end_at", "next_type_start_at", "boundary_confirmed_at",
        "boundary_available_at", "transition_exit_at", "transition_exit_confirmed_at",
        "return_to_last_center_at", "return_to_last_center_known_at",
    ):
        if key in state:
            state[key] = _optional_epoch(state[key])
    return state


def _attach_parent_center_rule_evidence(levels: list[dict]) -> None:
    """Link a pair only through three exact, locked parent member trend IDs."""

    for lower, parent in zip(levels, levels[1:]):
        lower_trends = {trend["trend_id"]: trend for trend in lower["trend_types"]}
        for higher_center in parent["centers"]:
            member_ids = higher_center["higher_center_member_trend_ids"]
            if len(member_ids) != 3 or any(key not in lower_trends for key in member_ids):
                continue
            represented_centers = {
                center_id
                for key in member_ids
                for center_id in lower_trends[key]["center_ids"]
            }
            for pair in lower["center_pair_rule_states"]:
                if {
                    pair["previous_center_id"], pair["current_center_id"]
                } <= represented_centers:
                    pair["higher_center_ids"].append(higher_center["center_id"])
                    pair["higher_center_formal"] = "confirmed_by_parent_three_members"
                    pair["higher_center_formal_at"] = higher_center["higher_center_formal_at"]


def _active_associative_hypotheses(level, as_of: datetime) -> list[dict]:
    """Keep source-bounded general-view geometry separate from formal output."""

    if level.structural_level != 0:
        return []
    locked = tuple(
        unit for unit in level.units
        if unit.locked and unit.available_at <= as_of
    )
    if len(locked) < 7:
        return []
    positions = {unit.unit_id: index for index, unit in enumerate(locked)}
    visible_trends = tuple(
        trend for trend in getattr(level, "trend_types", ())
        if trend.available_at <= as_of
        and trend.constituent_units[0].unit_id in positions
    )
    anchor_id = (
        visible_trends[-1].constituent_units[0].unit_id
        if visible_trends else locked[0].unit_id
    )
    source = locked[positions[anchor_id] :]
    if len(source) < 7:
        return []
    return [
        {**associative_view_payload(view),
         "schema": "chanlun-entry-anchored-associative-hypothesis-v1",
         "evidence_scope": "entry_anchored_general_geometry_hypothesis",
         "anchor_basis": (
             "latest_visible_same_level_type_start"
             if visible_trends else "first_locked_segment"
         ),
         "anchor_unit_id": anchor_id}
        for view in discover_entry_anchored_associations(
            source, anchor_unit_id=anchor_id, as_of=as_of
        )
    ]


def _direct_chart_gap_connectors(
    centers: tuple[TrendCenter, ...], raw_bars: tuple,
    display_bar_closed_at: tuple[int, ...], *, symbol: str,
    market: str, interval: str, basis: str, quantum: Decimal, as_of: datetime,
) -> tuple[list[dict], dict]:
    """Observe only direct center-boundary gaps in the full chart bar window."""

    def review(status, candidates=0, verified=0, invalid=0, observed=0):
        return {
            "status": status,
            "candidate_boundary_pairs": candidates,
            "calendar_verified_pairs": verified,
            "calendar_unverified_pairs": candidates - verified,
            "invalid_bar_geometry_pairs": invalid,
            "direct_gap_observations": observed,
            "tradable": False,
        }

    if (len(raw_bars) != len(display_bar_closed_at)
            or any(aware_datetime_to_epoch_seconds(bar.date) != stamp
                   for bar, stamp in zip(raw_bars, display_bar_closed_at))):
        return [], review("source_window_mismatch")
    positions = {bar.date: index for index, bar in enumerate(raw_bars)}
    if len(positions) != len(raw_bars):
        return [], review("source_window_mismatch")
    output = []
    candidates = verified = invalid = 0
    for previous, current in zip(centers, centers[1:]):
        if not previous.physically_completed:
            continue
        left = (previous.completion_return_unit
                or (previous.supersession_bridge_units[-1]
                    if previous.supersession_bridge_units else previous.body_units[-1]))
        right = current.entry_unit or current.body_units[0]
        first = positions.get(left.market_end)
        second = positions.get(right.market_start)
        if first is None or second != first + 1:
            continue
        candidates += 1
        calendar_ids = _official_adjacent_a_share_closes(
            left.market_end, right.market_start,
            market=market, interval=interval, as_of=as_of,
        )
        if calendar_ids is None:
            continue
        verified += 1
        try:
            pair = tuple(
                GapBarEvidence(
                    bar_id=stable_structure_id(
                        "chart-closed-gap-bar-v1", symbol, interval, basis,
                        index, int(bar.date.timestamp()),
                        _tick(bar.l, quantum), _tick(bar.h, quantum),
                    ),
                    closed_at=bar.date, low_tick=_tick(bar.l, quantum),
                    high_tick=_tick(bar.h, quantum),
                    price_basis_revision=basis,
                )
                for index, bar in ((first, raw_bars[first]), (second, raw_bars[second]))
            )
        except (AttributeError, ArithmeticError, TypeError, ValueError):
            invalid += 1
            continue
        try:
            observation = observe_explicit_general_center_connector(
                previous, current, as_of=as_of,
                market_source_ref=stable_structure_id(
                    "chart-center-gap-source-v1", symbol, interval, basis,
                    len(raw_bars), pair[0].bar_id, pair[1].bar_id,
                    calendar_ids,
                ),
                source_bars=pair, gap_after_bar_index=0,
            )
        except ValueError:
            continue
        output.append({
            **general_center_connector_observation_payload(observation),
            "source_adjacency_scope": "adjacent_in_matching_chart_source_bars",
            "source_bar_count": len(raw_bars),
            "calendar_adjacency_scope": "official_a_share_next_closed_bar",
            "calendar_evidence_ids": list(calendar_ids),
            "u297_gap_strength_state": observe_u297_gap_strength(
                pair[0], pair[1], physical_frequency=interval, as_of=as_of,
            ),
        })
    status = (
        "direct_gap_observed" if output else
        "no_direct_boundary_pair" if not candidates else
        "calendar_adjacency_not_proven" if not verified else
        "bar_geometry_invalid" if invalid == verified else
        "verified_no_direct_gap"
    )
    return output, review(status, candidates, verified, invalid, len(output))


def _official_adjacent_a_share_closes(
    before: datetime, after: datetime, *,
    market: str, interval: str, as_of: datetime,
) -> tuple[str, ...] | None:
    """Require a source pair to be adjacent in pinned A-share sessions."""

    if (market != "a" or interval not in {"1m", "5m", "30m"}
            or before.tzinfo is None or after.tzinfo is None):
        return None
    cn = ZoneInfo("Asia/Shanghai")
    earlier, later = before.astimezone(cn), after.astimezone(cn)
    if earlier >= later or later.year > earlier.year + 1:
        return None
    try:
        first = official_trading_session_evidence(
            session=earlier.date(), observed_at=as_of,
        )
        second = (first if earlier.year == later.year else
                  official_trading_session_evidence(
                      session=later.date(), observed_at=as_of,
                  ))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (first is None or second is None
            or first["classification"] != "TRADING_SESSION"
            or second["classification"] != "TRADING_SESSION"):
        return None
    step = int(interval[:-1])

    def closes(day):
        return tuple(
            datetime.combine(day, time(hour, minute), cn) + timedelta(minutes=offset)
            for hour, minute in ((9, 30), (13, 0))
            for offset in range(step, 121, step)
        )

    if earlier.date() == later.date():
        ordered = closes(earlier.date())
        if (earlier, later) not in tuple(zip(ordered, ordered[1:])):
            return None
    else:
        if earlier != closes(earlier.date())[-1] or later != closes(later.date())[0]:
            return None
        days = tuple(first["calendar_document"]["trading_days"])
        following = next((day for day in days if day > earlier.date().isoformat()), None)
        if following is None and earlier.year != later.year:
            following = next(iter(second["calendar_document"]["trading_days"]), None)
        if following != later.date().isoformat():
            return None
    return tuple(dict.fromkeys((first["content_sha256"], second["content_sha256"])))


def build_native_center_snapshot(
    cd, *, interval: str, display_bar_closed_at: tuple[int, ...],
) -> dict[str, object]:
    """Publish only centers calculated from this chart's own K-line period.

    The retired recursive movement partition does not run in the live chart.
    Point labels use the separately declared native-center signal policy.
    """

    if interval != cd.get_frequency():
        raise ValueError("native center interval must match its source bars")
    bars = tuple(cd.get_src_klines())
    if not bars or not display_bar_closed_at:
        raise ValueError("native center snapshot requires source bars")
    display_end = aware_datetime_to_epoch_seconds(bars[-1].date)
    if (display_bar_closed_at[-1] != display_end
            or any(left >= right for left, right in zip(
                display_bar_closed_at, display_bar_closed_at[1:]
            ))):
        raise ValueError("display bars must be ordered and end at the source cutoff")

    as_of = cd._strict_as_of()
    cutoff = aware_datetime_to_epoch_seconds(as_of)
    quantum = cd._strict_price_quantum()
    center_result = cd.get_native_centers()
    centers = []
    for center in center_result.centers:
        if center.available_at > as_of:
            continue
        item = strict_center_to_chart_dict(center)
        item["tradable"] = False
        centers.append(_with_prices(item, quantum))
    segment_units = tuple(cd.get_segment_units())
    units = {unit.unit_id: unit for unit in segment_units}
    center_by_id = {center["center_id"]: center for center in centers}
    previews = [
        _with_prices(item, quantum)
        for preview in center_result.previews
        if preview.available_at <= as_of
        if (item := strict_center_preview_to_chart_dict(
            preview, units, center_by_id.get(preview.formal_center_id)
        )) is not None
    ]
    try:
        strength = MacdStrengthProvider(cd)
    except (AttributeError, TypeError, ValueError):
        strength = None
    alerts = [
        _with_prices(item, quantum)
        for item in build_native_center_signals(
            segment_units, tuple(center_result.centers), strength, as_of=as_of,
        )
    ]
    stroke_construction = (
        cd.get_stroke_construction_state()
        if hasattr(cd, "get_stroke_construction_state") else {}
    )
    segment_construction = (
        cd.get_segment_construction_state()
        if hasattr(cd, "get_segment_construction_state") else {}
    )
    unresolved_segment_ranges = []
    tail = segment_construction.get("tail")
    if segment_construction.get("status") == "unresolved" and tail:
        region_id = stable_structure_id(
            "unresolved-segment-tail-v1", cd.get_code(), interval,
            cd._strict_price_basis_revision(), tail["start_time"], tail["start_price"],
        )
        unresolved_segment_ranges.append({
            "render_kind": "segment_unresolved_range",
            "region_id": region_id,
            "render_id": stable_structure_id(region_id, tail),
            "structural_level": 0,
            "price_basis_revision": cd._strict_price_basis_revision(),
            "available_at": cutoff,
            "state": "unresolved",
            "tradable": False,
            "direction": tail["direction"],
            "reason": tail["reason"],
            "pen_count": tail["pen_count"],
            "confirmed_pen_count": tail["confirmed_pen_count"],
            "points": [
                {"time": tail["start_time"], "price": tail["high"]},
                {"time": tail["observed_through"], "price": tail["low"]},
            ],
        })
    basis = cd._strict_price_basis_revision()
    revision = stable_structure_id(
        "native-center-only-v1", cd.get_code(), interval, basis, cutoff,
        centers, previews, alerts, bool(stroke_construction.get("unresolved_regions")),
        segment_construction,
        unresolved_segment_ranges,
        EXIT_PRICE_VERSION,
    )
    return with_point_exit_plans({
        "schema": CHART_STRUCTURE_SCHEMA,
        "analysis_scope": "native_centers",
        "native_member_policy": {
            "source_frequency": interval,
            "member_basis": "native_locked_segment",
            "role": "operational_subordinate_member",
            "recursive_type_proven": False,
        },
        "symbol": cd.get_code(),
        "source_frequency": interval,
        "display_frequency": interval,
        "source_closed_at": cutoff,
        "price_basis_revision": basis,
        "structure_price_quantum": _canonical_quantum(quantum),
        "strict_config_revision": cd._strict_config_revision(),
        "structure_revision": revision,
        "snapshot_revision": revision,
        "render_revision": revision,
        "declared_30m_fixed_view": None,
        "levels": [{
            "structural_level": 0,
            "label": interval,
            "origin": "native_segments",
            "centers": centers,
            "center_previews": previews,
            "trend_types": [],
            "trend_shapes": [],
            "points": alerts,
            "divergences": [],
        }],
        "stroke_center_observations": [],
        "conditional_centers": [],
        "unresolved_segment_ranges": unresolved_segment_ranges,
        "stroke_connection_pending": bool(stroke_construction.get("unresolved_regions")),
        "segment_construction": segment_construction,
        "point_exit_plans": {},
        "formal_point_issued": bool(alerts),
        "tradable": bool(alerts),
    }, native_source_valid={point["point_id"]: True for point in alerts})


def build_center_snapshot(cd, *, interval: str, display_bar_closed_at: tuple[int, ...],
                          declared_minimum_frequency: str | None = None,
                          bar_time_label: str = "end"):
    if interval != cd.get_frequency():
        raise ValueError("native center interval must match its source bars")
    as_of = cd._strict_as_of()
    cutoff = aware_datetime_to_epoch_seconds(as_of)
    # Display geometry keeps the provider's labels. For start-labelled bars,
    # the completed input's causal cutoff is one interval after its last label.
    raw_bars = tuple(cd.get_src_klines())
    display_end = aware_datetime_to_epoch_seconds(raw_bars[-1].date)
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
    try:
        macd = cd.get_idx()["macd"]
        zero_axis_state = macd_zero_axis_operational_state(
            tuple(bar.date for bar in raw_bars), macd["dif"], macd["dea"],
            physical_frequency=interval, as_of=as_of,
            closed_through=getattr(cd, "_stroke_closed_through", None),
            bar_known_times=tuple(
                getattr(cd, "_stroke_close_times", {}).get(bar.date)
                for bar in raw_bars
            ),
            declared_minimum_frequency=declared_minimum_frequency,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        zero_axis_state = macd_zero_axis_operational_state(
            (), (), (), physical_frequency=interval, as_of=as_of,
            closed_through=None,
            declared_minimum_frequency=declared_minimum_frequency,
        )
    zero_axis_state = {
        **zero_axis_state,
        "bar_market_at": _optional_epoch(zero_axis_state["bar_market_at"]),
        "known_at": _optional_epoch(zero_axis_state["known_at"]),
        "price_basis_revision": cd._strict_price_basis_revision(),
    }
    cross_frequency_state = None
    if interval == "5m":
        try:
            cross_frequency_state = observe_u293_macd_cross_frequency(
                tuple(bar.date for bar in raw_bars),
                tuple(getattr(cd, "_stroke_close_times", {}).get(bar.date)
                      for bar in raw_bars),
                tuple(bar.c for bar in raw_bars),
                cd.get_idx()["macd"]["hist"],
                physical_frequency=interval,
                market=getattr(cd, "market", "unknown"),
                bar_time_label=bar_time_label,
                as_of=as_of,
                closed_through=getattr(cd, "_stroke_closed_through", None),
                price_basis_revision=cd._strict_price_basis_revision(),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            cross_frequency_state = observe_u293_macd_cross_frequency(
                (), (), (), (), physical_frequency=interval,
                market=getattr(cd, "market", "unknown"),
                bar_time_label=bar_time_label, as_of=as_of, closed_through=None,
                price_basis_revision=cd._strict_price_basis_revision(),
            )
    try:
        formation_strength = MacdStrengthProvider(cd)
    except (AttributeError, TypeError, ValueError):
        # The local formation comparison is optional diagnostic evidence;
        # a structure-only replay must remain drawable without MACD.
        formation_strength = None
    levels = []
    source_levels = {level.structural_level: level for level in evidence.structure.levels}
    associative_hypotheses = []
    for level in evidence.structure.levels:
        depth = level.structural_level
        if depth == 0:
            associative_hypotheses = _active_associative_hypotheses(level, as_of)
        visible_centers = tuple(
            center for center in level.center_result.centers
            if center.available_at <= as_of
        )
        centers = [
            strict_center_to_chart_dict(center)
            for center in visible_centers
        ]
        direct_gap_connectors, direct_gap_review = _direct_chart_gap_connectors(
            visible_centers, raw_bars, display_bar_closed_at,
            symbol=cd.get_code(), market=getattr(cd, "market", "unknown"), interval=interval,
            basis=cd._strict_price_basis_revision(), quantum=quantum,
            as_of=as_of,
        )
        by_id = {center["center_id"]: center for center in centers}
        units = {unit.unit_id: unit for unit in level.units}
        previews = [item for preview in level.center_result.previews
                    if preview.available_at <= as_of
                    if (item := strict_center_preview_to_chart_dict(
                        preview, units, by_id.get(preview.formal_center_id))) is not None]
        visible_trends = tuple(
            trend for trend in getattr(level, "trend_types", ())
            if trend.available_at <= as_of
        )
        formal_chart_trends = {
            trend.trend_id: trend
            for trend in (*getattr(level, "completed_trends", ()), *visible_trends)
            if trend.available_at <= as_of and trend.centers
        }
        visible_boundaries = tuple(
            boundary for boundary in getattr(level, "decomposition_boundaries", ())
            if boundary.available_at <= as_of
        )
        formation_states = [
            {
                **state,
                "following_up_market_end": _optional_epoch(
                    state["following_up_market_end"]
                ),
                "following_up_available_at": _optional_epoch(
                    state["following_up_available_at"]
                ),
                "formation_market_at": _optional_epoch(state["formation_market_at"]),
                "formation_known_at": _optional_epoch(state["formation_known_at"]),
            }
            for state in trend_formation_rule_states(
                level, evidence.confirmed_points, as_of, formation_strength
            )
        ]
        second_buy_followups = [
            {
                **state,
                "first_up_available_at": _optional_epoch(
                    state["first_up_available_at"]
                ),
                "following_up_available_at": _optional_epoch(
                    state["following_up_available_at"]
                ),
                "branch_market_at": _optional_epoch(state["branch_market_at"]),
                "branch_known_at": _optional_epoch(state["branch_known_at"]),
            }
            for state in second_buy_followup_observations(
                level, evidence.confirmed_points, as_of
            )
        ]
        oscillation_monitors = [
            {
                **state,
                "z_known_at": _optional_epoch(state["z_known_at"]),
                "center_initial_known_at": _optional_epoch(
                    state["center_initial_known_at"]
                ),
                "observations": [
                    {
                        **item,
                        "market_at": _optional_epoch(item["market_at"]),
                        "known_at": _optional_epoch(item["known_at"]),
                    }
                    for item in state["observations"]
                ],
            }
            for center in visible_centers
            for state in (center_oscillation_monitor_state(center, as_of),)
        ]
        oscillation_operation_states = [
            {
                **state,
                "center_established_at": _optional_epoch(state["center_established_at"]),
                "lower_divergence_observations": [
                    {
                        **item,
                        "market_at": _optional_epoch(item["market_at"]),
                        "known_at": _optional_epoch(item["known_at"]),
                    }
                    for item in state["lower_divergence_observations"]
                ],
                "formal_third_points": [
                    {
                        **item,
                        "market_at": _optional_epoch(item["market_at"]),
                        "known_at": _optional_epoch(item["known_at"]),
                    }
                    for item in state["formal_third_points"]
                ],
            }
            for state in (
                center_oscillation_operation_rule_states(
                    level, source_levels[depth - 1],
                    tuple((*evidence.confirmed_points, *evidence.approaching_points)),
                    as_of,
                ) if depth > 0 and depth - 1 in source_levels else ()
            )
        ]
        nested_change_states = [
            {
                **state,
                "parent_center_initial_known_at": _optional_epoch(
                    state["parent_center_initial_known_at"]
                ),
                "condition_market_at": _optional_epoch(state["condition_market_at"]),
                "condition_known_at": _optional_epoch(state["condition_known_at"]),
            }
            for state in (
                nested_change_watch_rule_states(level, source_levels[depth - 1], as_of)
                if depth > 0 and depth - 1 in source_levels else ()
            )
        ]
        return_grade_states = [
            {
                **state,
                "first_lower_return_market_at": _optional_epoch(
                    state["first_lower_return_market_at"]
                ),
                "first_lower_return_known_at": _optional_epoch(
                    state["first_lower_return_known_at"]
                ),
                "qualified_child_return_market_at": _optional_epoch(
                    state["qualified_child_return_market_at"]
                ),
                "qualified_child_return_known_at": _optional_epoch(
                    state["qualified_child_return_known_at"]
                ),
            }
            for state in (
                center_first_return_grade_watch_states(
                    level, source_levels[depth - 1], as_of
                )
                if depth > 0 and depth - 1 in source_levels else ()
            )
        ]
        third_precision_states = [
            {
                **state,
                "lower_return_member_ids": list(state["lower_return_member_ids"]),
                "parent_center_established_at": _optional_epoch(
                    state["parent_center_established_at"]
                ),
                "parent_center_initial_known_at": _optional_epoch(
                    state["parent_center_initial_known_at"]
                ),
                "double_subsub_return_known_at": _optional_epoch(
                    state["double_subsub_return_known_at"]
                ),
                "lower_divergence_known_at": _optional_epoch(
                    state["lower_divergence_known_at"]
                ),
                "precision_market_at": _optional_epoch(state["precision_market_at"]),
                "precision_known_at": _optional_epoch(state["precision_known_at"]),
                "formal_third_point_available_at": _optional_epoch(
                    state["formal_third_point_available_at"]
                ),
            }
            for state in (
                third_point_entry_precision_rule_states(
                    level, source_levels[depth - 1],
                    tuple((*evidence.confirmed_points, *evidence.approaching_points)),
                    as_of,
                )
                if depth > 0 and depth - 1 in source_levels else ()
            )
        ]
        macd_phase_states = [
            {
                **state,
                "center_initial_known_at": _optional_epoch(
                    state["center_initial_known_at"]
                ),
                "center_context_known_at": _optional_epoch(
                    state["center_context_known_at"]
                ),
                "compare_leg_unit_ids": list(state["compare_leg_unit_ids"]),
                "signal_leg_unit_ids": list(state["signal_leg_unit_ids"]),
                "phase": (
                    None if state["phase"] is None else {
                        **state["phase"],
                        "market_at": _optional_epoch(state["phase"]["market_at"]),
                        "known_at": _optional_epoch(state["phase"]["known_at"]),
                    }
                ),
            }
            for state in center_macd_phase_rule_states(
                level, as_of, formation_strength
            )
        ]
        levels.append({
            "structural_level": depth,
            "label": interval if depth == 0 else f"{interval}/L{depth}",
            "origin": "native_segments" if depth == 0 else "completed_lower_structures",
            "centers": [_with_prices(center, quantum) for center in centers],
            "center_previews": [_with_prices(preview, quantum) for preview in previews],
            "trend_types": [_trend_type_rule_payload(trend) for trend in visible_trends],
            "trend_shapes": [
                _with_prices(_formal_trend_chart_payload(trend), quantum)
                for trend in formal_chart_trends.values()
            ],
            "trend_partition_review": _trend_partition_review(
                level, visible_centers, visible_trends, as_of
            ),
            "declared_upgrade_split_reviews": [
                _declared_upgrade_review_payload(
                    trend, source_levels.get(depth + 1), depth, as_of,
                )
                for trend in visible_trends
                if trend.kind.value == "consolidation" and len(trend.centers) > 1
            ],
            "l038_fixed_same_level_split_observations": (
                _l038_six_member_split_observations(level, as_of)
            ),
            "completed_trend_ids": [
                trend.trend_id for trend in getattr(level, "completed_trends", ())
                if trend.available_at <= as_of and trend.centers
            ],
            "decomposition_boundaries": [
                _decomposition_boundary_rule_payload(boundary)
                for boundary in visible_boundaries
            ],
            "center_pair_rule_states": [
                _center_pair_rule_payload(previous, current)
                for previous, current in zip(visible_centers, visible_centers[1:])
            ],
            "general_center_connector_observations": direct_gap_connectors,
            "general_center_connector_review": direct_gap_review,
            "center_return_grade_watch_states": return_grade_states,
            "third_point_entry_precision_rule_states": third_precision_states,
            "center_oscillation_monitors": oscillation_monitors,
            "center_oscillation_operation_rule_states": oscillation_operation_states,
            "nested_change_watch_rule_states": nested_change_states,
            "center_macd_phase_rule_states": macd_phase_states,
            "transition_rule_state": _transition_rule_payload(
                level, evidence.confirmed_points, as_of
            ),
            "trend_formation_rule_states": formation_states,
            "second_buy_followup_observations": second_buy_followups,
            "points": [
                _with_prices(strict_point_to_chart_dict(point), quantum)
                for point in (*evidence.confirmed_points, *evidence.approaching_points)
                if point.structural_level == depth and point.available_at <= as_of
            ],
            "divergences": [
                {
                    **_with_prices(strict_divergence_to_chart_dict(divergence), quantum),
                    "evidence_scope": "formal_structural_ledger",
                    "indicator_role": "auxiliary_strength_comparison",
                    "metric_policy": "price_extreme_and_any_recorded_decay",
                }
                for divergence in evidence.divergences
                if divergence.structural_level == depth and divergence.available_at <= as_of
            ],
        })
    if not levels:
        levels.append({"structural_level": 0, "label": interval, "origin": "native_segments",
                       "centers": [], "center_previews": [], "trend_types": [],
                       "trend_shapes": [],
                       "trend_partition_review": {
                           "status": "no_visible_source_units",
                           "source_unit_count": 0,
                           "formal_center_count": 0,
                           "formal_type_count": 0,
                           "geometric_movement_count": 0,
                           "unowned_source_unit_ids": [],
                           "unowned_leading_source_unit_ids": [],
                           "first_formal_type_connection": "no_formal_type",
                           "open_left_boundary_candidate": False,
                           "formal_type_absence_proves_market_has_no_type": False,
                           "tradable": False,
                       },
                       "declared_upgrade_split_reviews": [],
                       "l038_fixed_same_level_split_observations": [],
                       "completed_trend_ids": [], "decomposition_boundaries": [],
                        "center_pair_rule_states": [],
                        "general_center_connector_observations": [],
                        "general_center_connector_review": {
                            "status": "no_formal_center_level",
                            "candidate_boundary_pairs": 0,
                            "calendar_verified_pairs": 0,
                            "calendar_unverified_pairs": 0,
                            "invalid_bar_geometry_pairs": 0,
                            "direct_gap_observations": 0,
                            "tradable": False,
                        },
                        "center_return_grade_watch_states": [],
                        "third_point_entry_precision_rule_states": [],
                       "center_oscillation_monitors": [],
                       "center_oscillation_operation_rule_states": [],
                       "nested_change_watch_rule_states": [],
                       "center_macd_phase_rule_states": [],
                       "transition_rule_state": {"status": "not_started", "boll_hint": "unavailable"},
                       "trend_formation_rule_states": [],
                       "second_buy_followup_observations": [],
                       "points": [], "divergences": []})
    _attach_parent_center_rule_evidence(levels)
    observations = [
        _with_prices(_center_payload(center, render_kind="center_observation", tradable=False), quantum)
        for center in evidence.stroke_center_observations.centers
        if center.available_at <= cd._strict_as_of()
    ]
    physical_lineage_api_ready = all(callable(getattr(cd, name, None)) for name in (
        "get_xds", "get_segment_units", "get_contiguous_bis",
        "get_stroke_observation_units",
    ))
    physical_lineage_sources = (
        cd.get_xds(), cd.get_segment_units(),
        cd.get_contiguous_bis(), cd.get_stroke_observation_units(),
    ) if physical_lineage_api_ready else ((), (), (), ())
    raw_physical_memberships = observe_physical_segment_stroke_lineage(
        *physical_lineage_sources
    )
    physical_stroke_lineage = [
        {
            **item,
            "stroke_unit_ids": list(item["stroke_unit_ids"]),
            "segment_available_at": aware_datetime_to_epoch_seconds(
                item["segment_available_at"]
            ),
            "latest_stroke_available_at": _optional_epoch(
                item["latest_stroke_available_at"]
            ),
        }
        for item in raw_physical_memberships
    ]
    physical_lineage_status = (
        "source_api_unavailable" if not physical_lineage_api_ready else
        "pending_raw_stroke_indices" if any(
            item["status"] != "exact_raw_stroke_membership"
            for item in physical_stroke_lineage
        ) else "exact_raw_source_membership"
    )
    physical_formal_centers = (
        evidence.structure.levels[0].center_result.centers
        if evidence.structure.levels else ()
    )
    u194_observation = observe_class_formal_component_candidates(
        evidence.stroke_center_observations.centers,
        physical_formal_centers,
        raw_physical_memberships,
        as_of=as_of,
    )
    u194_component_pairs = [
        {
            **pair,
            "class_core_pen_unit_ids": list(pair["class_core_pen_unit_ids"]),
            "class_body_pen_unit_ids": list(pair["class_body_pen_unit_ids"]),
            "formal_body_segment_unit_ids": list(pair["formal_body_segment_unit_ids"]),
            "class_core_zone": list(pair["class_core_zone"]),
            "formal_core_zone": list(pair["formal_core_zone"]),
            "class_available_at": aware_datetime_to_epoch_seconds(pair["class_available_at"]),
            "formal_available_at": aware_datetime_to_epoch_seconds(pair["formal_available_at"]),
        }
        for pair in u194_observation["pairs"]
    ]
    u194_component_review = {
        **{key: value for key, value in u194_observation.items() if key != "pairs"},
        "as_of": cutoff,
        "formal_center_ids_missing_segment_membership": list(
            u194_observation["formal_center_ids_missing_segment_membership"]
        ),
        "physical_lineage_status": physical_lineage_status,
    }
    strict_levels = evidence.structure.levels
    u180_source_ready = bool(strict_levels) and isinstance(
        strict_levels[0], StrictLevelResult
    )
    u180_watches = (
        observe_u180_nine_segment_grade_watches(
            strict_levels[0],
            strict_levels[1] if len(strict_levels) > 1 else None,
            as_of=as_of,
        )
        if u180_source_ready else ()
    )
    u180_watch_payload = [
        {
            **watch,
            "related_rule_ids": list(watch["related_rule_ids"]),
            "source_refs": list(watch["source_refs"]),
            "segment_unit_ids": list(watch["segment_unit_ids"]),
            "higher_level_visible_center_ids": list(watch["higher_level_visible_center_ids"]),
            "higher_level_member_reviews": [
                {
                    **item,
                    "parent_established_at": aware_datetime_to_epoch_seconds(
                        item["parent_established_at"]
                    ),
                    "parent_grade_source_known_at": _optional_epoch(
                        item["parent_grade_source_known_at"]
                    ),
                    "parent_member_type_unit_ids": list(item["parent_member_type_unit_ids"]),
                    "parent_member_segment_groups": [
                        list(group) for group in item["parent_member_segment_groups"]
                    ],
                    "shared_first_nine_segment_unit_ids": list(
                        item["shared_first_nine_segment_unit_ids"]
                    ),
                }
                for item in watch["higher_level_member_reviews"]
            ],
            "higher_level_center_ids_with_shared_nine_segments": list(
                watch["higher_level_center_ids_with_shared_nine_segments"]
            ),
            "nine_segment_source_contained_formal_parent_center_ids": list(
                watch["nine_segment_source_contained_formal_parent_center_ids"]
            ),
            "all_completed_watch_segments_in_formal_parent_center_ids": list(
                watch["all_completed_watch_segments_in_formal_parent_center_ids"]
            ),
            "ninth_segment_market_at": aware_datetime_to_epoch_seconds(
                watch["ninth_segment_market_at"]
            ),
            "review_known_at": aware_datetime_to_epoch_seconds(watch["review_known_at"]),
            "nine_segment_review_known_at": aware_datetime_to_epoch_seconds(
                watch["nine_segment_review_known_at"]
            ),
            "higher_grade_source_link_known_at": _optional_epoch(
                watch["higher_grade_source_link_known_at"]
            ),
        }
        for watch in u180_watches
    ]
    u180_watch_review = {
        "rule_id": "U180",
        "status": (
            "source_api_unavailable" if not u180_source_ready else
            "review_candidates" if u180_watches else "no_nine_segment_lifecycle_witness"
        ),
        "source_scope": "one_formal_center_body_plus_failed_departures",
        "candidate_count": len(u180_watch_payload),
        "source_linked_parent_center_count": sum(
            len(watch["higher_level_center_ids_with_shared_nine_segments"])
            for watch in u180_watch_payload
        ),
        "nine_segment_source_contained_parent_center_count": sum(
            len(watch["nine_segment_source_contained_formal_parent_center_ids"])
            for watch in u180_watch_payload
        ),
        "all_completed_watch_source_contained_parent_center_count": sum(
            len(watch["all_completed_watch_segments_in_formal_parent_center_ids"])
            for watch in u180_watch_payload
        ),
        "author_exact_nine_segment_pattern_certified": False,
        "higher_center_membership_certified_by_watch": False,
        "formal_third_point_identity_changed": False,
        "tradable": False,
    }
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
    completed_nested_divergences = list(observe_completed_nested_divergence_lineage(
        evidence.structure.levels, getattr(evidence, "divergences", ()), as_of=as_of,
    ))
    live_divergence_candidates = [
        state for level in evidence.structure.levels
        for state in observe_live_center_divergence_candidate(
            level, as_of=as_of, strength=formation_strength,
        )
    ]
    live_divergence_candidates.extend(
        state for level in evidence.structure.levels
        for state in observe_live_trend_divergence_comparisons(
            level, evidence.approaching_points, as_of=as_of,
            strength=formation_strength,
        )
    )
    live_divergence_candidates.sort(key=lambda state: (
        state["structural_level"], state["center_id"], state["candidate_id"],
    ))
    nested_candidate_lineage = list(observe_nested_candidate_history_lineage(
        evidence.structure.levels, getattr(evidence, "divergences", ()),
        ((cutoff, tuple(live_divergence_candidates)),), as_of=as_of,
    ))
    revision = stable_structure_id(
        "chart-analysis-snapshot-v45-restored-physical-centers",
        EXIT_PRICE_VERSION,
        cd.get_code(),
        interval,
        cd._strict_config_revision(),
        cutoff,
        levels, observations, physical_stroke_lineage, physical_lineage_status,
        u194_component_pairs, u194_component_review,
        u180_watch_payload, u180_watch_review,
        connection_pending, conditional, segment_construction,
        associative_hypotheses,
        zero_axis_state,
        cross_frequency_state, completed_nested_divergences,
        live_divergence_candidates, nested_candidate_lineage,
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
        "rule_state_version": RULE_STATE_VERSION,
        "structure_revision": revision,
        "snapshot_revision": revision,
        "render_revision": revision,
        "levels": levels,
        "declared_30m_fixed_view": None,
        "general_associative_hypotheses": associative_hypotheses,
        "macd_zero_axis_operational_state": zero_axis_state,
        "macd_cross_frequency_u293_state": cross_frequency_state,
        "completed_nested_divergence_observations": completed_nested_divergences,
        "u255_live_departure_candidate_states": live_divergence_candidates,
        "nested_candidate_history_lineage_observations": nested_candidate_lineage,
        "stroke_center_observations": observations,
        "physical_segment_stroke_lineage": physical_stroke_lineage,
        "physical_segment_stroke_lineage_status": physical_lineage_status,
        "u194_class_formal_component_candidates": u194_component_pairs,
        "u194_class_formal_component_review": u194_component_review,
        "u180_nine_segment_grade_watch_states": u180_watch_payload,
        "u180_nine_segment_grade_watch_review": u180_watch_review,
        "stroke_connection_pending": connection_pending,
        "conditional_centers": conditional,
        "segment_construction": segment_construction,
        "unresolved_segment_ranges": unresolved_segment_ranges,
    })


__all__ = [
    "CHART_STRUCTURE_SCHEMA",
    "CHART_CENTER_SCHEMA",
    "build_center_snapshot",
    "build_native_center_snapshot",
    "strict_center_to_chart_dict",
]
