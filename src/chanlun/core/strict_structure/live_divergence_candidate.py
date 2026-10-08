"""Causal U255 observations for still-unlocked center and trend departures.

L061:97-121 keeps a prospective divergence leg under observation and drops
that hypothesis if later strength defeats it. Current-snapshot MACD comparisons
and explicitly supplied snapshot histories remain nontrading evidence; neither
certifies the author's full recursive interval nest or a trading point.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chanlun.core.strict_structure.divergence import (
    center_consolidation_comparison_legs,
    compare_center_consolidation_divergence,
)
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import CenterState, StrictPointStatus, TrendKind
from chanlun.core.strict_structure.point_rules import build_approaching_point_id
from chanlun.core.strict_structure.signals import project_live_terminal_trend_comparisons
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    MacdStrengthUnavailable,
    center_departure_comparison_leg,
    center_entry_comparison_leg,
)


def _strength_relation(earlier, later, direction: str) -> dict:
    """Compare three observable MACD measures without inventing a threshold."""

    def relation(left, right, *, negative=False):
        if left is None or right is None:
            return "unavailable"
        if negative:
            left, right = -left, -right
        return "stronger" if right > left else "weaker" if right < left else "equal"

    return {
        "histogram_area": relation(earlier.histogram_area, later.histogram_area),
        "histogram_peak": relation(earlier.histogram_peak, later.histogram_peak,
                                   negative=direction == "down"),
        "dif_extreme": relation(earlier.dif_extreme, later.dif_extreme,
                                negative=direction == "down"),
    }


def _hypothesis_id(
    *, observation_kind: str, center_id: str, compare_leg_unit_ids,
    signal_market_start: datetime, direction: str, structural_level: int,
    source_kind: str, price_basis_revision: str,
) -> str:
    """Keep one proposed comparison stable while the open tail geometry changes."""

    return stable_structure_id(
        "u255-live-comparison-hypothesis-v1", observation_kind,
        center_id, tuple(compare_leg_unit_ids),
        int(signal_market_start.timestamp()), direction,
        structural_level, source_kind, price_basis_revision,
    )


def observe_live_center_divergence_candidate(level, *, as_of: datetime, strength) -> tuple[dict, ...]:
    """Keep the open departure, comparison basis and current MACD relation separate."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("U255 candidate observation requires an aware cutoff")
    if not level.units:
        return ()
    tail = level.units[-1]
    if tail.locked or tail.available_at > as_of or not (tail.forming or tail.formed_at is not None):
        return ()
    output = []
    for center in level.center_result.centers:
        if (center.state is not CenterState.ONGOING
                or center.pending_leave_unit is not None
                or center.established_at > as_of
                or center.available_at > as_of
                or center.structural_level != level.structural_level
                or center.source_kind is not tail.source_kind
                or center.price_basis_revision != tail.price_basis_revision):
            continue
        last = center.body_units[-1]
        if (last.market_end != tail.market_start
                or last.end_tick != tail.start_tick
                or last.direction == tail.direction):
            continue
        projected_tail = replace(
            tail, locked=True, confirmed_at=tail.available_at, forming=False,
        )
        try:
            projected_center = replace(
                center, pending_leave_unit=projected_tail,
                available_at=max(center.available_at, projected_tail.available_at),
            )
        except ValueError:
            # A tail still inside the core is not a departure hypothesis.
            continue
        projected_units = (*level.units[:-1], projected_tail)
        pair = center_consolidation_comparison_legs(projected_center, projected_units)
        if pair is None:
            continue
        earlier, later = pair
        base = {
            "rule_id": "U255",
            "rule_source_refs": ["L061:97-121", "L061:124-130"],
            "observation_kind": "one_center_open_departure",
            "candidate_id": stable_structure_id(
                "u255-open-departure-comparison-v1", center.center_id,
                tail.unit_id, tuple(unit.unit_id for unit in earlier.units),
                tuple(unit.unit_id for unit in later.units),
                tail.price_basis_revision,
            ),
            "hypothesis_id": _hypothesis_id(
                observation_kind="one_center_open_departure",
                center_id=center.center_id,
                compare_leg_unit_ids=(unit.unit_id for unit in earlier.units),
                signal_market_start=later.units[0].market_start,
                direction=tail.direction,
                structural_level=level.structural_level,
                source_kind=tail.source_kind.value,
                price_basis_revision=tail.price_basis_revision,
            ),
            "center_id": center.center_id,
            "structural_level": level.structural_level,
            "source_kind": tail.source_kind.value,
            "price_basis_revision": tail.price_basis_revision,
            "direction": tail.direction,
            "compare_leg_unit_ids": [unit.unit_id for unit in earlier.units],
            "signal_leg_unit_ids": [unit.unit_id for unit in later.units],
            "signal_leg_market_start": int(later.units[0].market_start.timestamp()),
            "open_tail_unit_id": tail.unit_id,
            "open_tail_market_end": int(tail.market_end.timestamp()),
            "as_of": int(as_of.timestamp()),
            "tail_locked_in_source": False,
            "projected_lock_only_for_comparison": True,
            "formal_divergence_certified": False,
            "full_recursive_interval_nesting_certified": False,
            "historical_candidate_transition_certified": False,
            "author_strength_refutation_certified": False,
            "formal_point_from_observation": False,
            "tradable": False,
        }
        if strength is None:
            output.append({**base, "status": "pending_native_strength",
                           "known_at": int(tail.available_at.timestamp()),
                           "price_extreme_confirmed": None,
                           "macd_strength_relation": None})
            continue
        try:
            evidence = compare_center_consolidation_divergence(
                projected_center, projected_units, strength,
                movement_start_unit_id=(
                    center.entry_unit.unit_id if center.entry_unit
                    else center.body_units[0].unit_id
                ),
            )
            first = strength.snapshot(earlier.measurement_unit)
            second = strength.snapshot(later.measurement_unit)
        except (FormalDivergenceUnavailable, MacdStrengthUnavailable, ValueError):
            output.append({**base, "status": "pending_comparable_strength",
                           "known_at": int(tail.available_at.timestamp()),
                           "price_extreme_confirmed": None,
                           "macd_strength_relation": None})
            continue
        if evidence is None:
            continue
        known = max(tail.available_at, evidence.available_at,
                    first.available_at, second.available_at)
        if known > as_of:
            output.append({**base, "status": "pending_causal_strength_clock",
                           "known_at": int(tail.available_at.timestamp()),
                           "price_extreme_confirmed": None,
                           "macd_strength_relation": None})
            continue
        relation = _strength_relation(first, second, tail.direction)
        all_known = all(value != "unavailable" for value in relation.values())
        stronger_on_all = all_known and all(value != "weaker" for value in relation.values())
        some_stronger = any(value == "stronger" for value in relation.values())
        status = (
            "projected_new_extreme_missing" if not evidence.price_extreme_confirmed
            else "temporary_weakening_candidate" if evidence.is_divergent
            else "macd_strength_exceeds_reference" if stronger_on_all and some_stronger
            else "current_strength_test_not_divergent"
        )
        output.append({
            **base, "status": status, "known_at": int(known.timestamp()),
            "price_extreme_confirmed": evidence.price_extreme_confirmed,
            "macd_strength_relation": relation,
            "temporary_divergence_metrics": {
                "histogram_area_decayed": evidence.histogram_area_decayed,
                "histogram_peak_decayed": evidence.histogram_peak_decayed,
                "dif_extreme_decayed": evidence.dif_extreme_decayed,
            },
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["center_id"], item["candidate_id"],
    )))


def observe_projected_trend_first_class_candidates(
    level, approaching_points, *, as_of: datetime,
) -> tuple[dict, ...]:
    """Expose the live trend comparison already proved by the strict point engine.

    This accepts only the lightweight unlocked-tail projection. In particular,
    a level-zero geometric projection with a different missing condition is
    not silently recast as the same candidate. The original divergence and
    point remain temporary; this observation adds no formal trading signal.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("U255 candidate observation requires an aware cutoff")
    units = {unit.unit_id: unit for unit in level.units}
    centers = {center.center_id: center for center in level.center_result.centers}
    output = []
    for point in approaching_points:
        if (
            point.structural_level != level.structural_level
            or point.point_type not in {"1buy", "1sell"}
            or point.status is not StrictPointStatus.APPROACHING
            or point.missing_conditions != ("terminal_unit_locked",)
            or "temporary_trend_divergence" not in point.evidence_codes
            or point.available_at > as_of
        ):
            continue
        tail = units.get(point.anchor_unit_id)
        divergence = point.divergence
        center = centers.get(point.center_id)
        if (
            tail is None or tail.locked or tail.available_at > as_of
            or not (tail.forming or tail.formed_at is not None)
            or tail.source_kind is not point.source_kind
            or tail.price_basis_revision != point.price_basis_revision
            or divergence is None or divergence.kind != "trend"
            or not divergence.is_divergent
            or divergence.available_at > as_of
            or divergence.signal_unit_id != tail.unit_id
            or divergence.signal_leg_unit_ids[-1] != tail.unit_id
            or divergence.anchor_at != point.anchor_at
            or divergence.anchor_tick != point.anchor_tick
            or divergence.price_anchor_unit_id != tail.unit_id
            or divergence.comparison_center_id != point.center_id
            or center is None or center.available_at > as_of
            or center.source_kind is not tail.source_kind
            or center.price_basis_revision != tail.price_basis_revision
            or any(unit_id not in units for unit_id in (
                *divergence.compare_leg_unit_ids,
                *divergence.signal_leg_unit_ids,
            ))
            or any(not units[unit_id].locked for unit_id in (
                *divergence.compare_leg_unit_ids,
                *divergence.signal_leg_unit_ids[:-1],
            ))
        ):
            continue
        matching_trend = next((
            trend for trend in level.trend_types
            if trend.kind is TrendKind.TREND
            and trend.terminal_divergence is None
            and trend.centers[-1] == center
            and trend.direction == divergence.direction
            and trend.constituent_units[0].source_kind is tail.source_kind
            and trend.price_basis_revision == tail.price_basis_revision
            and trend.available_at <= as_of
        ), None)
        if matching_trend is None:
            continue
        output.append({
            "rule_id": "U255",
            "rule_source_refs": ["L061:97-121", "L061:124-130"],
            "observation_kind": "projected_trend_first_class",
            "candidate_id": stable_structure_id(
                "u255-projected-trend-first-class-v1", point.point_id,
                divergence.divergence_id, tail.price_basis_revision,
            ),
            "hypothesis_id": _hypothesis_id(
                observation_kind="projected_trend_first_class",
                center_id=center.center_id,
                compare_leg_unit_ids=divergence.compare_leg_unit_ids,
                signal_market_start=units[
                    divergence.signal_leg_unit_ids[0]
                ].market_start,
                direction=divergence.direction,
                structural_level=level.structural_level,
                source_kind=tail.source_kind.value,
                price_basis_revision=tail.price_basis_revision,
            ),
            "status": "temporary_trend_weakening_candidate",
            "source_point_id": point.point_id,
            "projected_divergence_id": divergence.divergence_id,
            "trend_id": matching_trend.trend_id,
            "center_id": center.center_id,
            "structural_level": level.structural_level,
            "source_kind": tail.source_kind.value,
            "price_basis_revision": tail.price_basis_revision,
            "direction": divergence.direction,
            "compare_leg_unit_ids": list(divergence.compare_leg_unit_ids),
            "signal_leg_unit_ids": list(divergence.signal_leg_unit_ids),
            "signal_leg_market_start": int(units[
                divergence.signal_leg_unit_ids[0]
            ].market_start.timestamp()),
            "open_tail_unit_id": tail.unit_id,
            "open_tail_market_end": int(tail.market_end.timestamp()),
            "as_of": int(as_of.timestamp()),
            "known_at": int(max(
                point.available_at, divergence.available_at, tail.available_at,
            ).timestamp()),
            "tail_locked_in_source": False,
            "projected_lock_only_for_comparison": True,
            "price_extreme_confirmed": divergence.price_extreme_confirmed,
            "temporary_divergence_metrics": {
                "histogram_area_decayed": divergence.histogram_area_decayed,
                "histogram_peak_decayed": divergence.histogram_peak_decayed,
                "dif_extreme_decayed": divergence.dif_extreme_decayed,
            },
            "formal_divergence_certified": False,
            "full_recursive_interval_nesting_certified": False,
            "historical_candidate_transition_certified": False,
            "author_strength_refutation_certified": False,
            "formal_point_from_observation": False,
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["center_id"], item["candidate_id"],
    )))


def observe_live_trend_divergence_comparisons(
    level, approaching_points, *, as_of: datetime, strength,
) -> tuple[dict, ...]:
    """Observe both outcomes of the exact projected terminal trend comparison.

    The strict signal engine remains the sole source of a temporary first-class
    point. An absent point can mean stronger MACD, no new price extreme, or a
    previous signal-leg extreme; these outcomes must not be conflated.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("U255 trend comparison requires an aware cutoff")
    positive = {
        (state["projected_divergence_id"], state["open_tail_unit_id"]): state
        for state in observe_projected_trend_first_class_candidates(
            level, approaching_points, as_of=as_of,
        )
    }
    if strength is None:
        return tuple(positive.values())
    active = level.units[level.center_result.locked_unit_count:]
    if not active:
        return ()
    carriers = (
        (active[-1:] if active[-1].forming else ())
        if level.structural_level == 0 else active
    )
    output = []
    matched_positive_keys = set()
    for tail in carriers:
        if (
            tail.locked or tail.available_at > as_of
            or not (tail.forming or tail.formed_at is not None)
        ):
            continue
        for comparison in project_live_terminal_trend_comparisons(
            level, tail, strength,
        ):
            trend = comparison.trend
            center = comparison.center
            divergence = comparison.divergence
            if (
                comparison.signal_unit.unit_id != tail.unit_id
                or divergence.signal_unit_id != tail.unit_id
                or trend.available_at > as_of
                or center.available_at > as_of
                or center.source_kind is not tail.source_kind
                or center.price_basis_revision != tail.price_basis_revision
                or center not in level.center_result.centers
            ):
                continue
            entry = center_entry_comparison_leg(
                comparison.projected_center, comparison.projected_units,
                not_before_unit_id=trend.constituent_units[0].unit_id,
            )
            departure = center_departure_comparison_leg(
                comparison.projected_center, comparison.projected_units,
                width=3,
            )
            if entry is None or departure is None:
                continue
            if (
                tuple(unit.unit_id for unit in entry.units)
                != divergence.compare_leg_unit_ids
                or tuple(unit.unit_id for unit in departure.units)
                != divergence.signal_leg_unit_ids
            ):
                raise ValueError("projected trend comparison leg drifted")
            try:
                first = strength.snapshot(entry.measurement_unit)
                second = strength.snapshot(departure.measurement_unit)
            except (MacdStrengthUnavailable, ValueError):
                continue
            point_state = positive.get((divergence.divergence_id, tail.unit_id))
            if point_state is not None:
                matched_positive_keys.add((divergence.divergence_id, tail.unit_id))
            candidate_point_id = build_approaching_point_id(
                price_basis_revision=trend.price_basis_revision,
                point_type="1buy" if divergence.direction == "down" else "1sell",
                structural_level=trend.structural_level,
                anchor_unit_id=tail.unit_id,
                center_id=center.center_id,
                parent_point_id=None,
            )
            candidate_id = stable_structure_id(
                "u255-projected-trend-first-class-v1", candidate_point_id,
                divergence.divergence_id, tail.price_basis_revision,
            )
            hypothesis_id = _hypothesis_id(
                observation_kind="projected_trend_first_class",
                center_id=center.center_id,
                compare_leg_unit_ids=divergence.compare_leg_unit_ids,
                signal_market_start=departure.units[0].market_start,
                direction=divergence.direction,
                structural_level=level.structural_level,
                source_kind=tail.source_kind.value,
                price_basis_revision=tail.price_basis_revision,
            )
            if point_state is not None and (
                point_state["source_point_id"] != candidate_point_id
                or point_state["candidate_id"] != candidate_id
                or point_state["hypothesis_id"] != hypothesis_id
            ):
                raise ValueError("temporary first point and trend comparison disagree")
            known = max(
                trend.available_at, center.available_at, tail.available_at,
                divergence.available_at, first.available_at, second.available_at,
            )
            base = {
                "rule_id": "U255",
                "rule_source_refs": ["L061:97-121", "L061:124-130"],
                "observation_kind": "projected_trend_first_class",
                "candidate_id": candidate_id,
                "hypothesis_id": hypothesis_id,
                "source_point_id": (
                    None if point_state is None else point_state["source_point_id"]
                ),
                "projected_divergence_id": divergence.divergence_id,
                "trend_id": trend.trend_id,
                "center_id": center.center_id,
                "structural_level": level.structural_level,
                "source_kind": tail.source_kind.value,
                "price_basis_revision": tail.price_basis_revision,
                "direction": divergence.direction,
                "compare_leg_unit_ids": list(divergence.compare_leg_unit_ids),
                "signal_leg_unit_ids": list(divergence.signal_leg_unit_ids),
                "signal_leg_market_start": int(departure.units[0].market_start.timestamp()),
                "open_tail_unit_id": tail.unit_id,
                "open_tail_market_end": int(tail.market_end.timestamp()),
                "as_of": int(as_of.timestamp()),
                "tail_locked_in_source": False,
                "projected_lock_only_for_comparison": True,
                "price_extreme_confirmed": divergence.price_extreme_confirmed,
                "temporary_divergence_metrics": {
                    "histogram_area_decayed": divergence.histogram_area_decayed,
                    "histogram_peak_decayed": divergence.histogram_peak_decayed,
                    "dif_extreme_decayed": divergence.dif_extreme_decayed,
                },
                "formal_divergence_certified": False,
                "full_recursive_interval_nesting_certified": False,
                "historical_candidate_transition_certified": False,
                "author_strength_refutation_certified": False,
                "formal_point_from_observation": False,
                "tradable": False,
            }
            if known > as_of:
                output.append({
                    **base, "status": "pending_causal_strength_clock",
                    "known_at": int(tail.available_at.timestamp()),
                    "macd_strength_relation": None,
                })
                continue
            relation = _strength_relation(first, second, tail.direction)
            values = tuple(relation.values())
            all_nonweaker = all(value in {"equal", "stronger"} for value in values)
            tail_owns_extreme = (
                divergence.price_anchor_unit_id == tail.unit_id
                and divergence.anchor_at == tail.price_anchor_time(
                    "buy" if divergence.direction == "down" else "sell"
                )
                and divergence.anchor_tick == (
                    tail.low_tick if divergence.direction == "down"
                    else tail.high_tick
                )
            )
            status = (
                "projected_new_extreme_missing"
                if not divergence.price_extreme_confirmed
                else "signal_leg_extreme_precedes_tail"
                if not tail_owns_extreme
                else "temporary_trend_weakening_candidate"
                if point_state is not None
                else "projected_weakening_without_current_first_point"
                if divergence.is_divergent
                else "macd_strength_exceeds_reference"
                if all_nonweaker and "stronger" in values
                else "current_strength_test_not_divergent"
            )
            output.append({
                **base, "status": status, "known_at": int(known.timestamp()),
                "macd_strength_relation": relation,
                "tail_owns_signal_leg_price_extreme": tail_owns_extreme,
            })
    output.extend(
        {**state, "macd_strength_relation": None,
         "comparison_recomputed_in_chart": False}
        for key, state in positive.items() if key not in matched_positive_keys
    )
    return tuple(sorted(output, key=lambda item: (
        item["structural_level"], item["center_id"], item["candidate_id"],
    )))


def summarize_u255_candidate_history(snapshot_rows) -> tuple[dict, ...]:
    """Reconcile supplied causal snapshots; never infer an unobserved refutation.

    The caller supplies strictly increasing cutoff snapshots. The function
    checks each observation's hypothesis identity and clocks, preserves every
    version, and records only an actually observed MACD-proxy increase after
    weakening. A vanished observation or unchanged known-at clock is not a
    refutation, and no author-level or trade certificate is produced.
    """

    snapshots = tuple(snapshot_rows)
    grouped: dict[str, list[tuple[int, dict]]] = {}
    previous_cutoff = None
    for index, (cutoff, states) in enumerate(snapshots):
        if type(cutoff) is not int or cutoff < 0 or (
            previous_cutoff is not None and cutoff <= previous_cutoff
        ):
            raise ValueError("U255 snapshot cutoffs must strictly increase")
        previous_cutoff = cutoff
        seen_here = set()
        for state in states:
            if (
                state.get("rule_id") != "U255"
                or state.get("as_of") != cutoff
                or type(state.get("known_at")) is not int
                or type(state.get("signal_leg_market_start")) is not int
                or type(state.get("open_tail_market_end")) is not int
                or not isinstance(state.get("open_tail_unit_id"), str)
                or not state["open_tail_unit_id"]
                or not isinstance(state.get("candidate_id"), str)
                or not state["candidate_id"]
                or state["known_at"] > cutoff
                or state["known_at"] < state["signal_leg_market_start"]
                or not state["signal_leg_market_start"]
                <= state["open_tail_market_end"] <= state["known_at"]
                or not state.get("signal_leg_unit_ids")
                or state["signal_leg_unit_ids"][-1] != state.get("open_tail_unit_id")
                or state.get("tail_locked_in_source") is not False
                or state.get("projected_lock_only_for_comparison") is not True
                or state.get("formal_divergence_certified") is not False
                or state.get("author_strength_refutation_certified") is not False
                or state.get("full_recursive_interval_nesting_certified") is not False
                or state.get("formal_point_from_observation") is not False
                or state.get("tradable") is not False
            ):
                raise ValueError("U255 history contains noncausal or formal evidence")
            observation_kind = state.get("observation_kind")
            if observation_kind == "one_center_open_departure":
                expected_candidate = stable_structure_id(
                    "u255-open-departure-comparison-v1",
                    state["center_id"], state["open_tail_unit_id"],
                    tuple(state["compare_leg_unit_ids"]),
                    tuple(state["signal_leg_unit_ids"]),
                    state["price_basis_revision"],
                )
            elif observation_kind == "projected_trend_first_class":
                expected_point = build_approaching_point_id(
                    price_basis_revision=state["price_basis_revision"],
                    point_type="1buy" if state["direction"] == "down" else "1sell",
                    structural_level=state["structural_level"],
                    anchor_unit_id=state["open_tail_unit_id"],
                    center_id=state["center_id"],
                    parent_point_id=None,
                )
                if state.get("source_point_id") not in (None, expected_point):
                    raise ValueError("U255 history point does not match its tail")
                expected_candidate = stable_structure_id(
                    "u255-projected-trend-first-class-v1", expected_point,
                    state["projected_divergence_id"],
                    state["price_basis_revision"],
                )
            else:
                raise ValueError("U255 history has an unknown observation kind")
            if state.get("candidate_id") != expected_candidate:
                raise ValueError("U255 history candidate id mismatches its source")
            status = state.get("status")
            if status in {"temporary_weakening_candidate",
                          "temporary_trend_weakening_candidate"}:
                metrics = state.get("temporary_divergence_metrics") or {}
                if (
                    state.get("price_extreme_confirmed") is not True
                    or not any(metrics.get(key) is True for key in (
                        "histogram_area_decayed", "histogram_peak_decayed",
                        "dif_extreme_decayed",
                    ))
                ):
                    raise ValueError("U255 weakening status lacks its actual metrics")
            if status == "macd_strength_exceeds_reference":
                relations = state.get("macd_strength_relation") or {}
                values = tuple(relations.get(key) for key in (
                    "histogram_area", "histogram_peak", "dif_extreme",
                ))
                if (
                    state.get("observation_kind") not in {
                        "one_center_open_departure",
                        "projected_trend_first_class",
                    }
                    or state.get("price_extreme_confirmed") is not True
                    or not all(value in {"equal", "stronger"} for value in values)
                    or "stronger" not in values
                ):
                    raise ValueError("U255 proxy increase lacks its actual metrics")
            expected = stable_structure_id(
                "u255-live-comparison-hypothesis-v1",
                state["observation_kind"], state["center_id"],
                tuple(state["compare_leg_unit_ids"]),
                state["signal_leg_market_start"], state["direction"],
                state["structural_level"], state["source_kind"],
                state["price_basis_revision"],
            )
            hypothesis_id = state.get("hypothesis_id")
            if hypothesis_id != expected or hypothesis_id in seen_here:
                raise ValueError("U255 history has a mismatched or duplicate hypothesis")
            seen_here.add(hypothesis_id)
            grouped.setdefault(hypothesis_id, []).append((index, state))

    output = []
    candidate_statuses = {
        "temporary_weakening_candidate",
        "temporary_trend_weakening_candidate",
    }
    for hypothesis_id, records in grouped.items():
        first = records[0][1]
        if any(
            current["open_tail_market_end"] < previous["open_tail_market_end"]
            for (_, previous), (_, current) in zip(records, records[1:])
        ):
            raise ValueError("U255 hypothesis tail market clock moved backward")
        weakening = next((
            (offset, state) for offset, state in records
            if state["status"] in candidate_statuses
        ), None)
        proxy_increase = None
        if weakening is not None:
            first_index, first_candidate = weakening
            for offset, state in records:
                if (
                    offset > first_index
                    and state["status"] == "macd_strength_exceeds_reference"
                    and state["known_at"] > first_candidate["known_at"]
                    and {position for position, _ in records
                         if first_index <= position <= offset}
                    == set(range(first_index, offset + 1))
                ):
                    proxy_increase = state
                    break
        observation_gap_count = (
            records[-1][0] - records[0][0] + 1 - len(records)
        )
        output.append({
            "rule_id": "U255",
            "hypothesis_id": hypothesis_id,
            "observation_kind": first["observation_kind"],
            "center_id": first["center_id"],
            "structural_level": first["structural_level"],
            "source_kind": first["source_kind"],
            "price_basis_revision": first["price_basis_revision"],
            "direction": first["direction"],
            "compare_leg_unit_ids": list(first["compare_leg_unit_ids"]),
            "signal_leg_market_start": first["signal_leg_market_start"],
            "input_snapshot_count": len(snapshots),
            "observed_snapshot_count": len(records),
            "observation_gap_count": observation_gap_count,
            "candidate_version_ids": list(dict.fromkeys(
                state["candidate_id"] for _, state in records
            )),
            "status_timeline": [
                {"as_of": state["as_of"], "known_at": state["known_at"],
                 "status": state["status"], "candidate_id": state["candidate_id"]}
                for _, state in records
            ],
            "first_weakening_known_at": (
                None if weakening is None else weakening[1]["known_at"]
            ),
            "first_macd_proxy_exceeds_known_at": (
                None if proxy_increase is None else proxy_increase["known_at"]
            ),
            "time_ordered_macd_proxy_increase_observed": proxy_increase is not None,
            "author_strength_refutation_certified": False,
            "full_recursive_interval_nesting_certified": False,
            "formal_point_from_history": False,
            "tradable": False,
        })
    return tuple(sorted(output, key=lambda item: item["hypothesis_id"]))
