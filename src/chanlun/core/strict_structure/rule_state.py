"""Causal runtime states for the bounded, chart-derived Chan rule decisions.

These states separate an observed relationship from its formal completion.
They do not turn diagram annotations or indicator hints into source rules.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.center_frame import initial_center_known_at
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.divergence import center_consolidation_comparison_legs
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    MacdStrengthUnavailable,
    compare_divergence,
)
from chanlun.core.strict_structure.models import (
    CenterRelation,
    DecompositionBoundaryEvidence,
    SourceKind,
    StrictLevelResult,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
    TrendKind,
    TrendState,
)


RULE_STATE_VERSION = "chanlun-local-source-conditioned-v20"


def center_pair_rule_state(previous: TrendCenter, current: TrendCenter) -> dict:
    """Keep same-grade touch, strict relation, and parent formation separate.

    A completed source unit can show that the later center's price path met
    the earlier center's envelope.  Its availability is the first safe time
    for that witness; the intramovement market touch time is not known here.
    """

    relation = classify_center_relation(previous, current)
    previous_center_known_at = initial_center_known_at(previous)
    current_center_known_at = initial_center_known_at(current)
    touch_observed = max(previous.dd_tick, current.dd_tick) <= min(
        previous.gg_tick, current.gg_tick
    )
    touch_witness = next((
        unit for unit in current.body_units
        if max(unit.low_tick, previous.dd_tick)
        <= min(unit.high_tick, previous.gg_tick)
    ), None) if touch_observed else None
    touch_known_at = (
        max(
            previous_center_known_at,
            # The previous envelope includes later lifecycle extremes; its
            # complete source must be available before a full-envelope touch.
            previous.available_at,
            current_center_known_at,
            touch_witness.available_at if touch_witness is not None
            else current.available_at,
        )
        if touch_observed else None
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
        "previous_center_initial_known_at": previous_center_known_at,
        "current_center_initial_known_at": current_center_known_at,
        "structural_level": previous.structural_level,
        "geometry_relation": relation.value,
        "touch_observed": touch_observed,
        "touch_known_at": touch_known_at,
        "touch_witness_unit_id": (
            touch_witness.unit_id if touch_witness is not None else None
        ),
        "touch_witness_known_at": (
            touch_witness.available_at if touch_witness is not None else None
        ),
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


def center_first_return_grade_watch_states(
    parent_level: StrictLevelResult,
    lower_level: StrictLevelResult,
    as_of: datetime,
) -> tuple[dict, ...]:
    """Observe L025's first low-grade return separately from a child-type return.

    The first locked lower source unit returning toward an established center
    can touch its oscillation envelope before any full lower trend type exists.
    This observation never creates a higher center. A grade-qualified return
    must be the parent's actual completion-return unit and resolve to an exact
    locked lower trend type; even then, the higher center needs its own three
    complete members. This is a bounded source-ledger observation rather than
    a universal numeric upgrade formula.
    """

    if lower_level.structural_level + 1 != parent_level.structural_level:
        raise ValueError("first-return watch requires adjacent recursive levels")
    if not all(
        center.source_kind is SourceKind.TREND_TYPE
        for center in parent_level.center_result.centers
    ):
        raise ValueError("first-return watch requires recursive trend-type centers")
    visible_lower_units = tuple(
        unit for unit in lower_level.units
        if unit.locked and unit.available_at <= as_of
    )
    locked_trends = {
        trend.trend_id: trend for trend in lower_level.trend_types
        if trend.state is TrendState.LOCKED and trend.available_at <= as_of
    }
    output = []
    for center in parent_level.center_result.centers:
        if center.established_at > as_of or center.available_at > as_of:
            continue
        window_end = (
            center.completion_return_unit.market_end
            if center.completion_return_unit is not None
            else center.superseded_at
            if center.superseded_at is not None else as_of
        )
        lower_units = tuple(
            unit for unit in visible_lower_units
            if unit.market_start >= center.last_touch_market_time
            and unit.market_end <= window_end
        )
        if any(
            unit.price_basis_revision != center.price_basis_revision
            for unit in lower_units
        ):
            raise ValueError("lower return and parent center price bases differ")
        leave = center.completion_leave_unit or center.pending_leave_unit
        desired_direction = (
            None if leave is None else center.departure_direction(leave)
        )
        start_at = (
            center.last_touch_market_time if leave is None
            else max(center.last_touch_market_time, leave.market_start)
        )
        breakout = None
        breakout_index = None
        for index, unit in enumerate(lower_units):
            if unit.market_start < start_at:
                continue
            if (
                unit.direction == "up"
                and (desired_direction in (None, "up"))
                and unit.high_tick > center.gg_tick
                and unit.end_tick > center.gg_tick
            ) or (
                unit.direction == "down"
                and (desired_direction in (None, "down"))
                and unit.low_tick < center.dd_tick
                and unit.end_tick < center.dd_tick
            ):
                breakout = unit
                breakout_index = index
                break
        direction = desired_direction if breakout is None else breakout.direction
        first_return = None
        if breakout_index is not None:
            for unit in lower_units[breakout_index + 1:]:
                if unit.market_start < breakout.market_end:
                    continue
                if unit.direction != direction:
                    first_return = unit
                    break

        def zone(unit):
            if unit is None or direction is None:
                return None
            if direction == "up":
                return (
                    "core" if unit.low_tick < center.zg_tick
                    else "core_boundary_contact" if unit.low_tick == center.zg_tick
                    else "outer_band" if unit.low_tick <= center.gg_tick
                    else "outside_envelope"
                )
            return (
                "core" if unit.high_tick > center.zd_tick
                else "core_boundary_contact" if unit.high_tick == center.zd_tick
                else "outer_band" if unit.high_tick >= center.dd_tick
                else "outside_envelope"
            )

        lower_zone = zone(first_return)
        qualified = center.completion_return_unit
        qualified_trend = (
            None if qualified is None else locked_trends.get(qualified.unit_id)
        )
        if (
            qualified_trend is not None
            and qualified_trend.price_basis_revision != center.price_basis_revision
        ):
            raise ValueError("qualified return and parent center price bases differ")
        qualified_zone = zone(qualified) if qualified_trend is not None else None
        if qualified_zone == "outer_band":
            status = "qualified_child_return_touches_outer_band_parent_pending"
        elif qualified_zone == "core_boundary_contact":
            status = "qualified_child_return_touches_core_boundary_parent_pending"
        elif qualified_zone == "core":
            status = "qualified_child_return_reenters_core_parent_pending"
        elif qualified_zone == "outside_envelope":
            status = "qualified_child_return_outside_envelope"
        elif qualified is not None:
            status = "pending_exact_locked_child_return_source"
        elif lower_zone == "outer_band":
            status = "lower_grade_outer_touch_only"
        elif lower_zone == "core_boundary_contact":
            status = "lower_grade_core_boundary_contact_only"
        elif lower_zone == "core":
            status = "lower_grade_core_touch_only"
        elif first_return is not None:
            status = "lower_grade_return_outside_envelope"
        elif breakout is not None:
            status = "pending_first_lower_return"
        else:
            status = "pending_lower_grade_breakout"
        output.append({
            "center_id": center.center_id,
            "structural_level": center.structural_level,
            "required_child_trend_level": lower_level.structural_level,
            "lower_source_kind": (
                None if not lower_units else lower_units[0].source_kind.value
            ),
            "breakout_direction": direction,
            "lower_breakout_unit_id": (
                None if breakout is None else breakout.unit_id
            ),
            "first_lower_return_unit_id": (
                None if first_return is None else first_return.unit_id
            ),
            "first_lower_return_touch_zone": lower_zone,
            "first_lower_return_market_at": (
                None if first_return is None else first_return.extreme_market_time(
                    "low" if direction == "up" else "high"
                )
            ),
            "first_lower_return_known_at": (
                None if first_return is None else max(
                    center.available_at, breakout.available_at,
                    first_return.available_at,
                )
            ),
            "qualified_child_return_trend_id": (
                None if qualified_trend is None else qualified_trend.trend_id
            ),
            "qualified_child_return_touch_zone": qualified_zone,
            "qualified_child_return_market_at": (
                None if qualified_trend is None else qualified.extreme_market_time(
                    "low" if direction == "up" else "high"
                )
            ),
            "qualified_child_return_known_at": (
                None if qualified_trend is None else max(
                    center.available_at, qualified_trend.available_at,
                )
            ),
            "status": status,
            "higher_center_formal": "independent_three_completed_child_types_required",
            "author_condition_scope": "L025_808_823_first_return_grade_example",
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: item["center_id"]))


def third_point_entry_precision_rule_states(
    parent_level: StrictLevelResult,
    lower_level: StrictLevelResult,
    points: tuple[StrictPointEvidence, ...],
    as_of: datetime,
) -> tuple[dict, ...]:
    """Observe L025's sub-subgrade third-point entry timing separately.

    L038/L041 distinguish two lower-lower return legs within *one* complete
    child return from two complete child returns.  A locked/complete exact
    child trend with an anchor-bearing second directional lower unit is a
    bounded double-return observation. An exact confirmed lower first point
    at the return extreme is a separate divergence branch. Neither branch
    changes the parent's formal third-point identity or issues a trade.
    """

    if lower_level.structural_level + 1 != parent_level.structural_level:
        raise ValueError("third-point precision requires adjacent recursive levels")
    centers = {center.center_id: center for center in parent_level.center_result.centers}
    lower_trends = {}
    state_rank = {
        TrendState.FORMING: 0,
        TrendState.COMPLETE: 1,
        TrendState.LOCKED: 2,
    }
    for trend in (
        *lower_level.trend_types,
        *getattr(lower_level, "completed_trends", ()),
    ):
        if trend.available_at > as_of:
            continue
        previous = lower_trends.get(trend.trend_id)
        if previous is None or state_rank[trend.state] > state_rank[previous.state]:
            lower_trends[trend.trend_id] = trend
    lower_first_points = tuple(
        point for point in points
        if point.structural_level == lower_level.structural_level
        and point.point_type in {"1buy", "1sell"}
        and point.status is StrictPointStatus.CONFIRMED
        and point.available_at <= as_of
        and point.divergence is not None
        and point.divergence.kind == "trend"
    )
    output = []
    for point in points:
        if (
            point.structural_level != parent_level.structural_level
            or point.point_type not in {"3buy", "3sell"}
            or point.available_at > as_of
        ):
            continue
        center = centers.get(point.center_id)
        leave = (
            None if center is None else
            center.completion_leave_unit or center.pending_leave_unit
        )
        lower_trend = lower_trends.get(point.anchor_unit_id)
        expected_return_direction = "down" if point.point_type == "3buy" else "up"
        side = "low" if point.point_type == "3buy" else "high"
        source_matches = bool(
            lower_trend is not None
            and lower_trend.price_basis_revision == point.price_basis_revision
            and lower_trend.direction == expected_return_direction
            and lower_trend.centers
            and lower_trend.market_start <= point.anchor_at <= lower_trend.market_end
            and (
                (lower_trend.low_tick == point.anchor_tick)
                if side == "low" else
                (lower_trend.high_tick == point.anchor_tick)
            )
        )
        complete_lower_return = bool(
            source_matches
            and lower_trend.state in {TrendState.COMPLETE, TrendState.LOCKED}
            and all(
                member.locked and member.available_at <= as_of
                for member in lower_trend.constituent_units
            )
        )
        same_direction_members = (
            tuple(
                member for member in lower_trend.constituent_units
                if member.direction == expected_return_direction
                and member.available_at <= as_of
            )
            if source_matches else ()
        )
        anchor_members = tuple(
            member for member in same_direction_members
            if getattr(member, side + "_tick") == point.anchor_tick
            and member.extreme_market_time(side) == point.anchor_at
        )
        anchor_member = anchor_members[-1] if anchor_members else None
        same_direction_before_anchor = (
            tuple(
                member for member in same_direction_members
                if member.market_end <= anchor_member.market_end
            )
            if anchor_member is not None else ()
        )
        double_return = bool(
            complete_lower_return
            and anchor_member is not None
            and len(same_direction_before_anchor) >= 2
        )
        double_known_at = (
            max(
                lower_trend.available_at,
                *(member.available_at for member in same_direction_before_anchor),
            ) if double_return else None
        )
        lower_divergences = (
            tuple(
                candidate for candidate in lower_first_points
                if candidate.point_type == (
                    "1buy" if point.point_type == "3buy" else "1sell"
                )
                and candidate.price_basis_revision == point.price_basis_revision
                and candidate.anchor_tick == point.anchor_tick
                and candidate.anchor_at == point.anchor_at
                and candidate.anchor_unit_id in {
                    member.unit_id for member in lower_trend.constituent_units
                }
                and lower_trend.market_start <= candidate.anchor_at <= lower_trend.market_end
            )
            if source_matches else ()
        )
        lower_divergence = (
            min(lower_divergences, key=lambda item: (item.available_at, item.point_id))
            if lower_divergences else None
        )
        center_known_at = initial_center_known_at(center) if center is not None else None
        parent_source_ready = bool(
            center_known_at is not None and center_known_at <= as_of
            and leave is not None and leave.locked and leave.available_at <= as_of
        )
        alternatives = []
        if parent_source_ready and double_known_at is not None:
            alternatives.append((
                max(center_known_at, leave.available_at, double_known_at),
                None if anchor_member is None else anchor_member.extreme_market_time(side),
                "double_subsub_return",
            ))
        if parent_source_ready and lower_divergence is not None:
            alternatives.append((
                max(center_known_at, leave.available_at,
                    lower_divergence.available_at),
                lower_divergence.anchor_at,
                "lower_trend_divergence",
            ))
        chosen = min(alternatives, key=lambda item: (item[0], item[2])) if alternatives else None
        if not parent_source_ready:
            status = "pending_parent_center_or_leave"
        elif not source_matches:
            status = "pending_exact_lower_return_type"
        elif chosen is not None:
            status = "bounded_precision_condition_observed"
        elif not complete_lower_return:
            status = "pending_lower_return_completion"
        else:
            status = "pending_subsub_divergence_or_double_return"
        output.append({
            "point_id": point.point_id,
            "point_type": point.point_type,
            "point_status": point.status.value,
            "structural_level": point.structural_level,
            "parent_center_id": point.center_id,
            "parent_center_established_at": (
                None if center is None else center.established_at
            ),
            "parent_center_initial_known_at": (
                center_known_at if parent_source_ready else None
            ),
            "parent_leave_unit_id": None if leave is None else leave.unit_id,
            "lower_return_trend_id": (
                lower_trend.trend_id if source_matches else None
            ),
            "lower_return_type_state": (
                lower_trend.state.value if source_matches else None
            ),
            "lower_return_member_ids": (
                tuple(member.unit_id for member in lower_trend.constituent_units)
                if source_matches else ()
            ),
            "same_direction_lower_member_count_to_anchor": len(
                same_direction_before_anchor
            ),
            "anchor_lower_member_id": (
                None if anchor_member is None else anchor_member.unit_id
            ),
            "double_subsub_return_observed": double_return,
            "double_subsub_return_known_at": double_known_at,
            "lower_divergence_point_id": (
                None if lower_divergence is None else lower_divergence.point_id
            ),
            "lower_divergence_known_at": (
                None if lower_divergence is None else lower_divergence.available_at
            ),
            "precision_branch": None if chosen is None else chosen[2],
            "precision_market_at": None if chosen is None else chosen[1],
            "precision_known_at": None if chosen is None else chosen[0],
            "author_entry_condition_supported": chosen is not None,
            "formal_third_point_available_at": point.available_at,
            "formal_third_point_independent": True,
            "status": status,
            "source_scope": "L025_3157_L038_904_L041_817_bounded_entry",
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["point_id"]
    )))


def center_oscillation_monitor_state(center: TrendCenter, as_of: datetime) -> dict:
    """L092 Z/Zn geometry, separated from center and third-point certificates.

    Completed child units recorded in this center's lifecycle form a bounded
    program observation.  The author gives the midpoint comparison but not an
    exact mapping from every software lifecycle role to each oscillation leg.
    In particular, a midpoint outside A/B never confirms a third point.
    """

    center_known_at = initial_center_known_at(center)
    established = center_known_at <= as_of
    z_twice_tick = center.zd_tick + center.zg_tick
    source = {}
    for role, units in (
        ("center_body", center.body_units),
        ("failed_departure", center.failed_departure_units),
        ("establishment_leave", () if center.establishment_leave_unit is None
         else (center.establishment_leave_unit,)),
        ("pending_leave", () if center.pending_leave_unit is None
         else (center.pending_leave_unit,)),
        ("completion_leave", () if center.completion_leave_unit is None
         else (center.completion_leave_unit,)),
        ("completion_return", () if center.completion_return_unit is None
         else (center.completion_return_unit,)),
    ):
        for unit in units:
            source.setdefault(unit.unit_id, (unit, role))
    observations = []
    if established:
        for unit, role in sorted(source.values(), key=lambda item: (
            item[0].market_start, item[0].market_end, item[0].unit_id
        )):
            if not unit.locked or unit.available_at > as_of:
                continue
            zn_twice_tick = unit.low_tick + unit.high_tick
            observations.append({
                "unit_id": unit.unit_id,
                "source_role": role,
                "range_low_tick": unit.low_tick,
                "range_high_tick": unit.high_tick,
                "zn_twice_tick": zn_twice_tick,
                "relation_to_z": (
                    "above" if zn_twice_tick > z_twice_tick
                    else "below" if zn_twice_tick < z_twice_tick else "equal"
                ),
                "boundary_cross": (
                    "above_B" if zn_twice_tick > 2 * center.zg_tick
                    else "below_A" if zn_twice_tick < 2 * center.zd_tick
                    else "none"
                ),
                "market_at": unit.market_end,
                "known_at": max(center_known_at, unit.available_at),
            })
    beyond_nine = len(observations) > 9
    return {
        "center_id": center.center_id if established else None,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "status": (
            "source_leg_grouping_and_upgrade_review_required" if beyond_nine
            else "auxiliary_observation" if established
            else "pending_center_establishment"
        ),
        "a_tick": center.zd_tick if established else None,
        "b_tick": center.zg_tick if established else None,
        "z_twice_tick": z_twice_tick if established else None,
        "z_known_at": center_known_at if established else None,
        "center_initial_known_at": center_known_at if established else None,
        "observations": observations,
        "boundary_cross_count": sum(
            item["boundary_cross"] != "none" for item in observations
        ),
        "more_than_nine_observations_hint": beyond_nine,
        "author_zn_sequence_certified": False,
        "source_policy": "recorded_completed_center_lifecycle_child_units",
        "third_point_status": "independent_complete_leave_and_return_required",
        "higher_center_status": "independent_three_completed_child_types_required",
        "tradable": False,
    }


def center_oscillation_operation_rule_states(
    parent_level: StrictLevelResult,
    lower_level: StrictLevelResult,
    points: tuple[StrictPointEvidence, ...],
    as_of: datetime,
) -> tuple[dict, ...]:
    """Bounded U294 source facts, with no automatic exit, re-entry, or size.

    An exact locked lower trend type must be a *later* member of an already
    established recursive center. Its terminal divergence can then be shown
    beside that center. The first three parent members only establish the
    center; their prior divergences are not backfilled as an oscillation trade.
    This does not certify the author's undefined "high" / "downward re-entry"
    thresholds or the sub-subgrade branch.
    """

    if lower_level.structural_level + 1 != parent_level.structural_level:
        raise ValueError("U294 observation requires adjacent recursive levels")
    trends = {
        trend.trend_id: trend for trend in lower_level.trend_types
        if trend.state is TrendState.LOCKED and trend.available_at <= as_of
    }
    output = []
    for center in parent_level.center_result.centers:
        if center.established_at > as_of or center.available_at > as_of:
            continue
        if center.source_kind is not SourceKind.TREND_TYPE:
            raise ValueError("U294 recursive center requires lower trend-type members")
        initial_ids = {member.unit_id for member in center.initial_units}
        members = {}
        for role, values in (
            ("center_body", center.body_units),
            ("failed_departure", center.failed_departure_units),
            ("pending_leave", () if center.pending_leave_unit is None
             else (center.pending_leave_unit,)),
            ("completion_leave", () if center.completion_leave_unit is None
             else (center.completion_leave_unit,)),
            ("completion_return", () if center.completion_return_unit is None
             else (center.completion_return_unit,)),
        ):
            for member in values:
                if member.unit_id not in initial_ids and member.available_at <= as_of:
                    members.setdefault(member.unit_id, (member, role))
        observations, missing = [], []
        for member, role in sorted(members.values(), key=lambda pair: (
            pair[0].market_start, pair[0].unit_id
        )):
            trend = trends.get(member.unit_id)
            if trend is None or (
                trend.market_start != member.market_start
                or trend.market_end != member.market_end
                or trend.start_tick != member.start_tick
                or trend.end_tick != member.end_tick
                or trend.price_basis_revision != center.price_basis_revision
                or member.price_basis_revision != center.price_basis_revision
            ):
                missing.append(member.unit_id)
                continue
            divergence = trend.terminal_divergence
            if divergence is None or divergence.available_at > as_of:
                continue
            observations.append({
                "child_trend_id": trend.trend_id,
                "parent_member_role": role,
                "divergence_id": divergence.divergence_id,
                "divergence_kind": divergence.kind,
                "direction": divergence.direction,
                "signal_unit_id": divergence.signal_unit_id,
                "market_at": divergence.anchor_at,
                "known_at": max(center.established_at, member.available_at,
                                trend.available_at, divergence.available_at),
                "anchor_tick": divergence.anchor_tick,
                "anchor_core_relation": (
                    "above" if divergence.anchor_tick > center.zg_tick else
                    "below" if divergence.anchor_tick < center.zd_tick else
                    "in_or_on_core"
                ),
            })
        third_points = tuple(sorted((
            point for point in points
            if point.point_type in {"3buy", "3sell"}
            and point.status is StrictPointStatus.CONFIRMED
            and point.center_id == center.center_id
            and point.structural_level == center.structural_level
            and point.price_basis_revision == center.price_basis_revision
            and point.available_at <= as_of
        ), key=lambda item: (item.available_at, item.point_id)))
        live_center = center.state.value == "ongoing"
        output.append({
            "rule_id": "U294",
            "rule_source_refs": ["L105:208-226", "L105:3673-3691"],
            "center_id": center.center_id,
            "structural_level": center.structural_level,
            "lower_structural_level": lower_level.structural_level,
            "center_state": center.state.value,
            "operation_scope": (
                "ongoing_center_observation" if live_center
                else "historical_center_audit"
            ),
            "center_established_at": center.established_at,
            "status": (
                "pending_exact_lower_member_lineage" if missing else
                "exact_lower_divergence_facts_observed" if observations and live_center else
                "historical_exact_lower_divergence_facts" if observations else
                "pending_later_lower_divergence" if live_center else
                "historical_no_later_lower_divergence"
            ),
            "initial_member_divergences_excluded": True,
            "exact_later_member_count": len(members) - len(missing),
            "missing_child_trend_ids": missing,
            "lower_divergence_observations": observations,
            "upward_divergence_fact_count": sum(
                item["direction"] == "up" for item in observations
            ),
            "downward_divergence_fact_count": sum(
                item["direction"] == "down" for item in observations
            ),
            "formal_third_points": [
                {"point_id": point.point_id, "point_type": point.point_type,
                 "market_at": point.anchor_at, "known_at": point.available_at}
                for point in third_points
            ],
            "subsubgrade_lineage_status": "not_mapped",
            "key_level_break_status": "requires_declared_case_level",
            "downward_reentry_status": "author_threshold_not_specified",
            "position_size_status": "not_programmed",
            "author_operation_certified": False,
            "formal_third_point_independent": True,
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["center_id"]
    )))


def center_macd_phase_rule_states(
    level: StrictLevelResult,
    as_of: datetime,
    strength,
) -> tuple[dict, ...]:
    """Observe L025 zero-axis chronology without choosing a numeric vicinity.

    The source defines one bounded negative and positive example, not a
    universal DIF threshold. Formal consolidation divergence and trade points
    remain governed by their separate structural ledger.
    """

    output = []
    for center in level.center_result.centers:
        center_known_at = initial_center_known_at(center)
        if center_known_at > as_of:
            continue
        pair = center_consolidation_comparison_legs(center, level.units)
        status = "pending_comparison_legs"
        phase = None
        compare_ids = ()
        signal_ids = ()
        if pair is not None:
            earlier, later = pair
            compare_ids = tuple(
                unit.unit_id for unit in earlier.units
                if unit.available_at <= as_of
            )
            signal_ids = tuple(
                unit.unit_id for unit in later.units
                if unit.available_at <= as_of
            )
            if any(unit.available_at > as_of for unit in earlier.units):
                status = "pending_first_leg_completion"
            elif any(unit.available_at > as_of for unit in later.units):
                status = "pending_second_leg_completion"
            elif strength is None or not hasattr(strength, "zero_axis_phase_observation"):
                status = "pending_native_macd_phase_source"
            else:
                try:
                    phase = strength.zero_axis_phase_observation(
                        earlier.measurement_unit, later.measurement_unit
                    )
                    status = phase["status"]
                except (MacdStrengthUnavailable, ValueError):
                    status = "pending_native_macd_phase_source"
        output.append({
            "center_id": center.center_id,
            "structural_level": level.structural_level,
            "center_initial_known_at": center_known_at,
            "center_context_known_at": (
                max(center_known_at, phase["known_at"])
                if phase is not None else None
            ),
            "status": status,
            "compare_leg_unit_ids": compare_ids,
            "signal_leg_unit_ids": signal_ids,
            "phase": phase,
            "author_near_zero_threshold_status": "not_specified",
            "formal_divergence_status": "independent_structural_ledger",
            "source_scope": "L025_3424_3430_bounded_negative_case",
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["center_id"]
    )))


def nested_change_watch_rule_states(
    parent_level: StrictLevelResult,
    lower_level: StrictLevelResult,
    as_of: datetime,
) -> tuple[dict, ...]:
    """Bounded L092 nested trend/center geometry, with Zn proof still pending.

    A parent center member must be an exact locked lower trend type.  Its last
    lower center and the *next* locked lower type are checked through the same
    source chain.  Even the supported structural branch is not the author's
    complete change-of-oscillation certificate while Zn leg mapping is open.
    """

    if lower_level.structural_level + 1 != parent_level.structural_level:
        raise ValueError("nested watch requires adjacent recursive levels")
    trends = {
        trend.trend_id: trend for trend in lower_level.trend_types
        if trend.state is TrendState.LOCKED and trend.available_at <= as_of
    }
    units = tuple(
        unit for unit in parent_level.units if unit.available_at <= as_of
    )
    positions = {unit.unit_id: index for index, unit in enumerate(units)}
    output = []
    for center in parent_level.center_result.centers:
        center_known_at = initial_center_known_at(center)
        if center_known_at > as_of:
            continue
        lifecycle = {}
        for role, members in (
            ("center_body", center.body_units),
            ("failed_departure", center.failed_departure_units),
            ("pending_leave", () if center.pending_leave_unit is None
             else (center.pending_leave_unit,)),
            ("completion_leave", () if center.completion_leave_unit is None
             else (center.completion_leave_unit,)),
            ("completion_return", () if center.completion_return_unit is None
             else (center.completion_return_unit,)),
        ):
            for member in members:
                if member.available_at <= as_of:
                    lifecycle.setdefault(member.unit_id, role)
        for unit_id, role in lifecycle.items():
            trend = trends.get(unit_id)
            if (
                trend is None or trend.kind is not TrendKind.TREND
                or not trend.centers or unit_id not in positions
            ):
                continue
            last = trend.centers[-1]
            core_relation = (
                "above" if last.zd_tick > center.zg_tick
                else "below" if last.zg_tick < center.zd_tick else "overlap"
            )
            envelope_relation = (
                "above" if last.dd_tick > center.zg_tick
                else "below" if last.gg_tick < center.zd_tick else "overlap"
            )
            offset = positions[unit_id]
            next_unit = units[offset + 1] if offset + 1 < len(units) else None
            next_trend = trends.get(next_unit.unit_id) if next_unit is not None else None
            if last.available_at > as_of:
                state = "pending_last_child_center"
            elif core_relation == "overlap":
                state = "last_child_center_overlaps_parent"
            elif envelope_relation == "overlap":
                state = "outside_core_envelope_overlap_needs_scope_review"
            elif next_unit is None:
                state = "pending_next_child_type"
            elif (
                next_unit.market_start != trend.market_end
                or next_unit.start_tick != trend.end_tick
            ):
                state = "next_child_source_not_connected"
            elif next_trend is None:
                state = "pending_next_child_completion"
            elif not last.zd_tick <= next_trend.end_tick <= last.zg_tick:
                state = "next_child_ends_outside_last_child_center"
            elif next_unit.unit_id not in lifecycle:
                state = "next_child_outside_parent_lifecycle_needs_scope_review"
            else:
                state = "nested_structure_supported_zn_mapping_pending"
            structural_support = state == "nested_structure_supported_zn_mapping_pending"
            output.append({
                "parent_center_id": center.center_id,
                "parent_center_initial_known_at": center_known_at,
                "structural_level": parent_level.structural_level,
                "child_trend_id": trend.trend_id,
                "child_member_role": role,
                "last_child_center_id": last.center_id,
                "last_child_core_relation": core_relation,
                "last_child_envelope_relation": envelope_relation,
                "next_child_trend_id": (
                    None if next_trend is None else next_trend.trend_id
                ),
                "next_child_member_role": (
                    None if next_unit is None else lifecycle.get(next_unit.unit_id)
                ),
                "next_child_end_tick": (
                    None if next_trend is None else next_trend.end_tick
                ),
                "status": state,
                "condition_market_at": (
                    next_trend.market_end if structural_support else None
                ),
                "condition_known_at": (
                    max(center_known_at, trend.available_at,
                        last.available_at, next_trend.available_at)
                    if structural_support else None
                ),
                "price_completion_policy": "next_locked_child_end_in_last_child_core",
                "author_zn_source_status": "mapping_pending",
                "formal_change_status": "independent_not_inferred",
                "tradable": False,
            })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["parent_center_id"],
        item["child_trend_id"]
    )))


def trend_formation_rule_states(
    level: StrictLevelResult,
    points: tuple[StrictPointEvidence, ...],
    as_of: datetime,
    strength,
) -> tuple[dict, ...]:
    """Bounded U175 watch: a first-center third buy needs positive up-leg proof.

    The one-unit local consolidation comparison is an explicit implementation
    choice based on L049/L061. MACD is auxiliary; even supported formation is
    kept separate from a completed two-center trend and from trade signals.
    """

    centers = {center.center_id: center for center in level.center_result.centers}
    units = tuple(level.units)
    positions = {unit.unit_id: index for index, unit in enumerate(units)}
    output = []
    for point in points:
        if (
            point.structural_level != level.structural_level
            or point.point_type != "3buy"
            or point.center_ordinal != 1
            or point.status is not StrictPointStatus.CONFIRMED
            or point.available_at > as_of
        ):
            continue
        center = centers.get(point.center_id)
        if center is None or center.available_at > as_of:
            continue
        leave = center.completion_leave_unit
        ret = center.completion_return_unit
        if (
            leave is None or ret is None
            or center.completion_direction != "up"
            or ret.unit_id != point.anchor_unit_id
        ):
            continue
        offset = positions.get(ret.unit_id)
        following = None if offset is None or offset + 1 >= len(units) else units[offset + 1]
        state = "pending_following_up_unit"
        comparison = None
        if following is not None:
            if (
                following.direction != "up"
                or following.market_start != ret.market_end
                or following.start_tick != ret.end_tick
            ):
                state = "following_up_unit_not_connected"
            elif not following.locked or following.available_at > as_of:
                state = "pending_following_up_completion"
            elif strength is None:
                state = "pending_strength_source"
            else:
                try:
                    earlier_strength = strength.snapshot(leave)
                    later_strength = strength.snapshot(following)
                    if any(value is None for value in (
                        earlier_strength.histogram_area,
                        earlier_strength.histogram_peak,
                        later_strength.histogram_area,
                        later_strength.histogram_peak,
                    )):
                        state = "pending_complete_strength_metrics"
                    else:
                        comparison = compare_divergence(
                            leave, following, strength, kind="consolidation"
                        )
                        state = (
                            "not_formed_no_new_extreme"
                            if not comparison.price_extreme_confirmed
                            else "not_formed_local_consolidation_divergence"
                            if comparison.is_divergent
                            else "formation_supported_by_local_nondivergence"
                        )
                except (MacdStrengthUnavailable, FormalDivergenceUnavailable):
                    state = "pending_strength_source"
        known_at = (
            max(point.available_at, following.available_at, comparison.available_at)
            if comparison is not None else None
        )
        output.append({
            "third_buy_point_id": point.point_id,
            "center_id": center.center_id,
            "structural_level": level.structural_level,
            "source_kind": point.source_kind.value,
            "status": state,
            "leave_unit_id": leave.unit_id,
            "return_unit_id": ret.unit_id,
            "following_up_unit_id": None if following is None else following.unit_id,
            "following_up_market_end": None if following is None else following.market_end,
            "following_up_available_at": None if following is None else following.available_at,
            "comparison_id": None if comparison is None else comparison.divergence_id,
            "comparison_kind": None if comparison is None else "local_consolidation_auxiliary",
            "price_extreme_confirmed": (
                None if comparison is None else comparison.price_extreme_confirmed
            ),
            "locally_divergent": None if comparison is None else comparison.is_divergent,
            "formation_market_at": (
                following.market_end
                if state == "formation_supported_by_local_nondivergence" else None
            ),
            "formation_known_at": (
                known_at if state == "formation_supported_by_local_nondivergence" else None
            ),
            "formation_clock_policy": "completed_following_up_unit_end",
            "completed_trend_status": "independent_not_inferred_from_formation",
            "tradable": False,
            "policy_basis": "L049_L061_local_strength_implementation_U175",
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["third_buy_point_id"]
    )))


def second_buy_followup_observations(
    level: StrictLevelResult,
    points: tuple[StrictPointEvidence, ...],
    as_of: datetime,
) -> tuple[dict, ...]:
    """Measure the next child high in L086's bounded first/second-buy example.

    The author's exit/hold example is not a new formal second/third point or a
    general trading signal.  Only locked, causally available units settle the
    comparison; a later third buy needs its own independent point certificate.
    """

    parents = {point.point_id: point for point in points}
    units = tuple(level.units)
    positions = {unit.unit_id: index for index, unit in enumerate(units)}
    output = []
    for point in points:
        if (
            point.point_type != "2buy"
            or point.structural_level != level.structural_level
            or point.status is not StrictPointStatus.CONFIRMED
            or point.available_at > as_of
        ):
            continue
        parent = parents.get(point.parent_point_id)
        if (
            parent is None or parent.point_type != "1buy"
            or parent.status is not StrictPointStatus.CONFIRMED
            or parent.structural_level != level.structural_level
            or parent.source_kind is not point.source_kind
            or parent.price_basis_revision != point.price_basis_revision
        ):
            continue
        offset = positions.get(point.anchor_unit_id)
        if offset is None or offset < 1:
            continue
        first_up, pullback = units[offset - 1:offset + 1]
        following = units[offset + 1] if offset + 1 < len(units) else None
        if (
            first_up.direction != "up" or pullback.direction != "down"
            or first_up.market_end != pullback.market_start
            or first_up.end_tick != pullback.start_tick
            or not first_up.locked or not pullback.locked
        ):
            state = "source_sequence_invalid"
        elif following is None:
            state = "pending_next_child_up"
        elif (
            following.direction != "up"
            or following.market_start != pullback.market_end
            or following.start_tick != pullback.end_tick
        ):
            state = "next_child_not_connected"
        elif not following.locked or following.available_at > as_of:
            state = "pending_next_child_completion"
        else:
            state = (
                "first_up_high_broken_watch_independent_third_buy"
                if following.high_tick > first_up.high_tick
                else "first_up_high_unbroken_in_bounded_case"
            )
        decided = state in {
            "first_up_high_broken_watch_independent_third_buy",
            "first_up_high_unbroken_in_bounded_case",
        }
        output.append({
            "first_buy_point_id": parent.point_id,
            "second_buy_point_id": point.point_id,
            "structural_level": level.structural_level,
            "status": state,
            "first_up_unit_id": first_up.unit_id,
            "first_up_high_tick": first_up.high_tick,
            "first_up_available_at": first_up.available_at,
            "pullback_unit_id": pullback.unit_id,
            "following_up_unit_id": None if following is None else following.unit_id,
            "following_up_high_tick": None if following is None else following.high_tick,
            "following_up_available_at": None if following is None else following.available_at,
            "branch_market_at": following.market_end if decided else None,
            "branch_known_at": max(point.available_at, following.available_at) if decided else None,
            "third_buy_status": "independent_point_check_required",
            "author_case_scope": "L086_304_322_one_minute_downtrend_example",
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["second_buy_point_id"]
    )))


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
    left_trend = next(
        (
            trend for trend in getattr(level, "trend_types", ())
            if trend.trend_id == getattr(boundary, "left_trend_id", None)
        ),
        None,
    )
    left_owned_unit_ids = (
        {unit.unit_id for unit in left_trend.constituent_units}
        if left_trend is not None else set()
    )
    first_new_unit = next(
        (
            unit for unit in level.units
            if unit.locked
            and unit.market_start >= boundary.anchor_at
            and unit.unit_id not in left_owned_unit_ids
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
            and unit.locked
            and unit.market_start >= boundary.anchor_at
            and unit.unit_id not in left_owned_unit_ids
            and unit.available_at <= as_of
            and max(unit.low_tick, old_center.zd_tick)
            <= min(unit.high_tick, old_center.zg_tick)
        ),
        None,
    )
    post_boundary_center_ids = {
        center.center_id
        for center in getattr(
            getattr(level, "center_result", None), "centers", ()
        )
        if center.body_start_market_time >= boundary.anchor_at
        and center.established_at <= as_of
        and center.available_at <= as_of
    }
    transition_center_ids = post_boundary_center_ids | {
        boundary.terminal_center_id
    }
    third_points = tuple(sorted((
        point for point in points
        if point.structural_level == level.structural_level
        and point.status is StrictPointStatus.CONFIRMED
        and point.point_type in ("3buy", "3sell")
        and point.center_id in transition_center_ids
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
        "eligible_transition_center_ids": tuple(sorted(transition_center_ids)),
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
