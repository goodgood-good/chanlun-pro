"""Exact completed parent/child divergence lineage, without a trade signal.

L061:97-166 describes recursive *candidate* comparison and possible later
refutation. This observer certifies only a completed lower divergence inside a
completed parent's exact signal member. It never calls that the full interval
nest, a turning-point order, or a live candidate/refutation certificate.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.live_divergence_candidate import (
    summarize_u255_candidate_history,
)
from chanlun.core.strict_structure.models import SourceKind


def observe_completed_nested_divergence_lineage(
    levels,
    divergences,
    *,
    as_of: datetime,
) -> tuple[dict, ...]:
    """Return causal, exact one-grade links from frozen formal source objects."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("nested divergence observation requires an aware cutoff")
    source = tuple(levels)
    source_levels = {level.structural_level: level for level in source}
    if len(source_levels) != len(source):
        raise ValueError("nested divergence levels must be unique")
    ledger = tuple(divergences)
    if len({item.divergence_id for item in ledger}) != len(ledger):
        raise ValueError("nested divergence ids must be unique")
    output = []
    for parent in ledger:
        if (parent.structural_level < 1 or parent.available_at > as_of
                or not parent.is_divergent):
            continue
        grade = parent.structural_level
        higher = source_levels.get(grade)
        lower = source_levels.get(grade - 1)
        if higher is None or lower is None:
            raise ValueError("completed higher divergence lacks adjacent source levels")
        if parent.source_kind is not SourceKind.TREND_TYPE:
            raise ValueError("higher divergence requires completed lower trend-type units")
        parent_units = {unit.unit_id: unit for unit in higher.units}
        lower_trends = {}
        for trend in (*lower.trend_types, *lower.completed_trends):
            prior = lower_trends.setdefault(trend.trend_id, trend)
            if prior is not trend and (
                prior.market_start != trend.market_start
                or prior.market_end != trend.market_end
                or prior.start_tick != trend.start_tick
                or prior.end_tick != trend.end_tick
                or prior.price_basis_revision != trend.price_basis_revision
                or tuple(unit.unit_id for unit in prior.constituent_units)
                   != tuple(unit.unit_id for unit in trend.constituent_units)
                or prior.terminal_divergence != trend.terminal_divergence
            ):
                raise ValueError("one lower trend id has conflicting source lineage")
        missing = []
        exact_children = []
        for unit_id in parent.signal_leg_unit_ids:
            unit = parent_units.get(unit_id)
            trend = lower_trends.get(unit_id)
            if unit is None or trend is None or (
                trend.market_start != unit.market_start
                or trend.market_end != unit.market_end
                or trend.start_tick != unit.start_tick
                or trend.end_tick != unit.end_tick
                or trend.direction != unit.direction
                or trend.price_basis_revision != parent.price_basis_revision
                or unit.price_basis_revision != parent.price_basis_revision
                or not trend.complete
                or trend.available_at > as_of
            ):
                missing.append(unit_id)
                continue
            exact_children.append(trend)
        links = []
        late_links = []
        for child in exact_children:
            child_source_ids = {unit.unit_id for unit in child.constituent_units}
            if not child_source_ids:
                continue
            for inner in ledger:
                if (
                    inner.structural_level != grade - 1
                    or not inner.is_divergent
                    or inner.direction != parent.direction
                    or inner.source_kind is not child.constituent_units[0].source_kind
                    or inner.available_at > parent.available_at
                    or inner.available_at > as_of
                    or inner.price_basis_revision != parent.price_basis_revision
                    or not set((*inner.compare_leg_unit_ids,
                                *inner.signal_leg_unit_ids)) <= child_source_ids
                    or not child.market_start <= inner.anchor_at <= child.market_end
                ):
                    continue
                observation = {
                    "child_trend_id": child.trend_id,
                    "child_divergence_id": inner.divergence_id,
                    "child_grade": inner.structural_level,
                    "child_kind": inner.kind,
                    "child_direction": inner.direction,
                    "child_anchor_at": int(inner.anchor_at.timestamp()),
                    "child_known_at": int(inner.available_at.timestamp()),
                    "child_compare_unit_ids": list(inner.compare_leg_unit_ids),
                    "child_signal_unit_ids": list(inner.signal_leg_unit_ids),
                }
                if inner.anchor_at > parent.anchor_at:
                    late_links.append(observation)
                else:
                    links.append(observation)
        links.sort(key=lambda item: (
            item["child_known_at"], item["child_divergence_id"],
            item["child_trend_id"],
        ))
        late_links.sort(key=lambda item: (
            item["child_known_at"], item["child_divergence_id"],
            item["child_trend_id"],
        ))
        status = (
            "pending_exact_child_type_lineage" if missing else
            "exact_completed_child_divergence_facts" if links else
            "child_divergence_after_parent_anchor_only" if late_links else
            "no_completed_child_divergence_in_parent_signal_members"
        )
        output.append({
            "rule_id": "U255",
            "rule_source_refs": ["L061:97-121", "L061:124-166"],
            "status": status,
            "parent_divergence_id": parent.divergence_id,
            "parent_grade": grade,
            "parent_kind": parent.kind,
            "parent_direction": parent.direction,
            "parent_signal_unit_ids": list(parent.signal_leg_unit_ids),
            "parent_anchor_at": int(parent.anchor_at.timestamp()),
            "parent_known_at": int(parent.available_at.timestamp()),
            "missing_child_type_ids": missing,
            "exact_child_divergences": links,
            "late_child_divergences": late_links,
            "known_at": int(parent.available_at.timestamp()),
            "source_scope": "completed_formal_divergence_exact_signal_member_only",
            "full_recursive_interval_nesting_certified": False,
            "live_candidate_refutation_certified": False,
            "formal_point_from_this_observation": False,
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["parent_known_at"], item["parent_grade"],
        item["parent_divergence_id"],
    )))


def observe_nested_candidate_history_lineage(
    levels,
    divergences,
    snapshot_rows,
    *,
    as_of: datetime,
) -> tuple[dict, ...]:
    """Bind supplied lower U255 snapshots to exact parent signal members.

    L061:97-121,151-166 motivates the comparison and turning-point order.
    Snapshot rows remain caller-supplied; a linked candidate is not a complete
    closed-bar interval replay, author-level refutation, or a trading point.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("nested candidate lineage requires an aware cutoff")
    cutoff_epoch = int(as_of.timestamp())
    source = tuple(levels)
    snapshots = tuple(
        (cutoff, tuple(states)) for cutoff, states in snapshot_rows
    )
    # Validate every supplied version before associating it with a parent.
    summarize_u255_candidate_history(snapshots)
    if any(cutoff > cutoff_epoch for cutoff, _ in snapshots):
        raise ValueError("nested candidate history cannot use future snapshots")
    source_levels = {level.structural_level: level for level in source}
    parent_facts = observe_completed_nested_divergence_lineage(
        source, divergences, as_of=as_of,
    )
    output = []
    for parent in parent_facts:
        lower = source_levels[parent["parent_grade"] - 1]
        child_by_id = {
            trend.trend_id: trend
            for trend in (*lower.completed_trends, *lower.trend_types)
        }
        children = tuple(
            child_by_id[unit_id]
            for unit_id in parent["parent_signal_unit_ids"]
            if unit_id not in parent["missing_child_type_ids"]
        )
        parent_rows = tuple(
            (cutoff, states) for cutoff, states in snapshots
            if cutoff <= parent["parent_known_at"]
        )
        links = []
        late_candidate_ids = set()
        for child in children:
            member_ids = {unit.unit_id for unit in child.constituent_units}
            center_ids = {center.center_id for center in child.centers}
            child_start = int(child.market_start.timestamp())
            child_end = int(child.market_end.timestamp())
            scoped_rows = []
            exact_versions: dict[str, bool] = {}
            for cutoff, states in parent_rows:
                scoped = []
                for state in states:
                    if (
                        state["structural_level"] != parent["parent_grade"] - 1
                        or state["source_kind"]
                        != child.constituent_units[0].source_kind.value
                        or state["price_basis_revision"]
                        != child.price_basis_revision
                        or state["direction"] != parent["parent_direction"]
                        or state["center_id"] not in center_ids
                        or not set(state["compare_leg_unit_ids"]) <= member_ids
                        or not child_start <= state["signal_leg_market_start"]
                        <= state["open_tail_market_end"] <= child_end
                    ):
                        continue
                    if state["open_tail_market_end"] > parent["parent_anchor_at"]:
                        late_candidate_ids.add(state["candidate_id"])
                        continue
                    scoped.append(state)
                    hypothesis_id = state["hypothesis_id"]
                    version_exact = set(state["signal_leg_unit_ids"]) <= member_ids
                    exact_versions[hypothesis_id] = (
                        exact_versions.get(hypothesis_id, True) and version_exact
                    )
                scoped_rows.append((cutoff, tuple(scoped)))
            for history in summarize_u255_candidate_history(scoped_rows):
                weakening_known = history["first_weakening_known_at"]
                turn_knowledge = (
                    "no_weakening_observed" if weakening_known is None else
                    "known_at_or_before_parent_turn"
                    if weakening_known <= parent["parent_anchor_at"] else
                    "known_after_parent_turn"
                )
                links.append({
                    "child_trend_id": child.trend_id,
                    "child_grade": parent["parent_grade"] - 1,
                    "source_lineage_scope": (
                        "exact_final_child_source_unit_ids"
                        if exact_versions[history["hypothesis_id"]]
                        else "same_center_and_contained_tail_clock_only"
                    ),
                    "turn_anchor_knowledge": turn_knowledge,
                    "candidate_history": history,
                })
        links.sort(key=lambda item: (
            item["candidate_history"]["first_weakening_known_at"] or 0,
            item["child_trend_id"],
            item["candidate_history"]["hypothesis_id"],
        ))
        exact_count = sum(
            item["source_lineage_scope"] == "exact_final_child_source_unit_ids"
            for item in links
        )
        turn_time_count = sum(
            item["turn_anchor_knowledge"] == "known_at_or_before_parent_turn"
            for item in links
        )
        post_turn_count = sum(
            item["turn_anchor_knowledge"] == "known_after_parent_turn"
            for item in links
        )
        status = (
            "pending_exact_child_type_lineage"
            if parent["missing_child_type_ids"] else
            "candidate_history_known_after_parent_turn_only"
            if links and turn_time_count == 0 and post_turn_count else
            "candidate_history_without_turn_time_weakening"
            if links and turn_time_count == 0 else
            "exact_candidate_history_linked_to_parent_signal_member"
            if links and exact_count == len(links) else
            "mixed_exact_and_temporal_candidate_histories"
            if exact_count else
            "candidate_history_temporally_associated_only"
            if links else
            "candidate_after_parent_anchor_only"
            if late_candidate_ids else
            "no_candidate_snapshots_supplied"
            if not parent_rows else
            "no_linked_candidate_in_supplied_snapshots"
        )
        output.append({
            "rule_id": "U255",
            "rule_source_refs": ["L061:97-121", "L061:151-166"],
            "status": status,
            "parent_divergence_id": parent["parent_divergence_id"],
            "parent_grade": parent["parent_grade"],
            "parent_anchor_at": parent["parent_anchor_at"],
            "parent_known_at": parent["parent_known_at"],
            "parent_signal_unit_ids": parent["parent_signal_unit_ids"],
            "missing_child_type_ids": parent["missing_child_type_ids"],
            "sampled_cutoff_count": len(parent_rows),
            "linked_candidate_histories": links,
            "exact_candidate_history_count": exact_count,
            "temporal_only_candidate_history_count": len(links) - exact_count,
            "turn_time_weakening_history_count": turn_time_count,
            "post_turn_known_weakening_history_count": post_turn_count,
            "late_candidate_version_ids": sorted(late_candidate_ids),
            "known_at": parent["parent_known_at"],
            "source_scope": "caller_supplied_cutoff_snapshots_with_child_lineage",
            "full_child_interval_replay_certified": False,
            "author_strength_refutation_certified": False,
            "full_recursive_interval_nesting_certified": False,
            "formal_point_from_this_observation": False,
            "tradable": False,
        })
    return tuple(output)
