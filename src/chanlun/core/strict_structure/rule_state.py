"""Causal runtime states for the bounded, chart-derived Chan rule decisions.

These states separate an observed relationship from its formal completion.
They do not turn diagram annotations or indicator hints into source rules.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import (
    CenterRelation,
    DecompositionBoundaryEvidence,
    StrictLevelResult,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
)


RULE_STATE_VERSION = "chanlun-local-source-conditioned-v4"


def center_pair_rule_state(previous: TrendCenter, current: TrendCenter) -> dict:
    """Keep same-grade touch, strict relation, and parent formation separate."""

    relation = classify_center_relation(previous, current)
    touch_observed = max(previous.dd_tick, current.dd_tick) <= min(
        previous.gg_tick, current.gg_tick
    )
    both_complete = previous.physically_completed and current.physically_completed
    if not both_complete:
        strict_expansion = "pending_center_completion"
    elif relation is CenterRelation.RECOMPOSITION_PENDING:
        strict_expansion = "pending_recomposition"
    elif relation is CenterRelation.UPGRADE:
        strict_expansion = "confirmed_relation"
    else:
        strict_expansion = "not_expansion"
    return {
        "previous_center_id": previous.center_id,
        "current_center_id": current.center_id,
        "structural_level": previous.structural_level,
        "geometry_relation": relation.value,
        "touch_observed": touch_observed,
        "touch_known_at": current.available_at if touch_observed else None,
        "both_centers_completed": both_complete,
        "strict_expansion_relation": strict_expansion,
        "strict_relation_known_at": (
            max(previous.completion_available_at, current.completion_available_at)
            if both_complete and relation is not CenterRelation.RECOMPOSITION_PENDING
            else None
        ),
        "higher_center_formal": "requires_independent_parent_three_members",
        "higher_center_ids": [],
        "higher_center_formal_at": None,
    }


def transition_rule_state(
    level: StrictLevelResult,
    points: tuple[StrictPointEvidence, ...],
    as_of: datetime,
) -> dict:
    """Emit a shared boundary and a separate structural transition exit.

    A confirmed third-class point can end this implementation's transition
    only after a new locked unit beyond the prior type's shared endpoint exists.
    BOLL is absent from strict structural inputs, so its hint stays unavailable.
    """

    boundaries: tuple[DecompositionBoundaryEvidence, ...] = tuple(
        boundary for boundary in getattr(level, "decomposition_boundaries", ())
        if boundary.available_at <= as_of
    )
    if not boundaries:
        return {"status": "not_started", "boll_hint": "unavailable"}
    boundary = max(boundaries, key=lambda item: (item.anchor_at, item.available_at))
    first_new_unit = next(
        (
            unit for unit in level.units
            if unit.locked and unit.market_end > boundary.anchor_at
            and unit.available_at <= as_of
        ),
        None,
    )
    old_center = next(
        (
            center for center in getattr(
                getattr(level, "center_result", None), "centers", ()
            )
            if center.center_id == boundary.terminal_center_id
        ),
        None,
    )
    return_unit = next(
        (
            unit for unit in level.units
            if getattr(boundary, "boundary_kind", None) == "trend_divergence"
            and old_center is not None
            and unit.locked and unit.market_end > boundary.anchor_at
            and unit.available_at <= as_of
            and max(unit.low_tick, old_center.zd_tick)
            <= min(unit.high_tick, old_center.zg_tick)
        ),
        None,
    )
    third_points = tuple(sorted((
        point for point in points
        if point.structural_level == level.structural_level
        and point.status is StrictPointStatus.CONFIRMED
        and point.point_type in ("3buy", "3sell")
        and point.center_id is not None
        and point.anchor_at > boundary.anchor_at
        and point.available_at <= as_of
    ), key=lambda item: (item.anchor_at, item.available_at, item.point_id)))
    exit_point = third_points[0] if first_new_unit is not None and third_points else None
    return {
        "status": "ended" if exit_point is not None else "active",
        "view_id": level.decomposition_mode,
        "structural_level": level.structural_level,
        "previous_type_end_at": boundary.anchor_at,
        "next_type_start_at": boundary.anchor_at,
        "boundary_confirmed_at": boundary.confirmed_at,
        "boundary_available_at": boundary.available_at,
        "boundary_id": boundary.boundary_id,
        "old_reference_center_id": boundary.terminal_center_id,
        "natural_end_signal": (
            "confirmed_at_return" if return_unit is not None else "pending"
        ),
        "return_to_last_center_at": (
            return_unit.market_end if return_unit is not None else None
        ),
        "return_to_last_center_known_at": (
            return_unit.available_at if return_unit is not None else None
        ),
        "new_initial_structure_unit_id": (
            first_new_unit.unit_id if first_new_unit is not None else None
        ),
        "exit_point_id": exit_point.point_id if exit_point is not None else None,
        "transition_exit_at": exit_point.anchor_at if exit_point is not None else None,
        "transition_exit_confirmed_at": (
            exit_point.confirmed_at if exit_point is not None else None
        ),
        "boll_hint": "unavailable",
        "new_type_final_completion": "independent",
        "policy_basis": "chart_derived_implementation_PY",
    }


def small_to_large_point_rule_state(point: StrictPointEvidence) -> str | None:
    """A second-class operating point is not proof of a larger reversal."""

    if "small_to_large_reversal" in point.evidence_codes:
        return "operating_second_class_not_larger_turn_certificate"
    return None
