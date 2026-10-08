"""Confirmed consolidation MACD notifications under the native-segment policy.

L024:334-379 distinguishes swings inside a center from departures beyond it;
L027:37-43 distinguishes a local consolidation comparison from trend divergence.
Using locked segments as members is the user's operational policy, rather than
an assertion that the retired recursive movement partition has been proved.
"""

from __future__ import annotations

from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.divergence import center_consolidation_comparison_legs
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import CenterRelation
from chanlun.core.strict_structure.native_center_signals import _center_sources_known
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable, MacdStrengthUnavailable, _comparison_leg, compare_comparison_legs,
)


NATIVE_CONSOLIDATION_RULE_BASIS = "user_declared_native_consolidation_macd_v1"


def build_native_consolidation_alerts(units, centers, strength, *, as_of):
    """Compare completed same-direction legs around one confirmed native core."""
    if strength is None:
        return []
    values = tuple(units)
    index = {unit.unit_id: offset for offset, unit in enumerate(values)}
    if len(index) != len(values):
        raise ValueError("consolidation source identities must be unique")
    by_id = {unit.unit_id: unit for unit in values}
    visible = tuple(center for center in centers if center.available_at <= as_of)
    output = {}
    for center_offset, center in enumerate(visible):
        if not _center_sources_known(center, by_id, as_of):
            continue
        pairs = []
        try:
            pair = center_consolidation_comparison_legs(center, values)
            if pair is not None:
                previous = visible[center_offset - 1] if center_offset else None
                relation = classify_center_relation(previous, center) if previous is not None else None
                trend_direction = {CenterRelation.UP_TREND: "up", CenterRelation.DOWN_TREND: "down"}.get(relation)
                if pair[1].terminal_unit.direction != trend_direction:
                    pairs.append(("entry_departure", *pair))
        except (TypeError, ValueError):
            pass
        # A -> opposite bridge -> C inside the same center is also relevant.
        # It does not need C to exceed A or break the core (L024:349-352).
        owned = {unit.unit_id for unit in (*center.body_units, *center.establishment_units,
                                          *center.failed_departure_units)}
        if center.pending_leave_unit is not None:
            owned.add(center.pending_leave_unit.unit_id)
        for offset in range(index[center.initial_units[0].unit_id], len(values) - 2):
            a, b, c = values[offset:offset + 3]
            if not {a.unit_id, b.unit_id, c.unit_id} <= owned:
                continue
            if any(not unit.locked or unit.confirmed_at is None or unit.available_at > as_of
                   for unit in (a, b, c)):
                continue
            if (a.direction != c.direction or b.direction == a.direction
                    or any(left.market_end != right.market_start or left.end_tick != right.start_tick
                           for left, right in ((a, b), (b, c)))):
                continue
            if not all(max(unit.low_tick, center.zd_tick) <= min(unit.high_tick, center.zg_tick)
                       for unit in (a, b, c)):
                continue
            pairs.append(("center_oscillation", _comparison_leg((a,)), _comparison_leg((c,))))
        for form, earlier, later in pairs:
            try:
                evidence = compare_comparison_legs(earlier, later, strength, kind="consolidation")
                area_a = strength.snapshot(earlier.measurement_unit).histogram_area
                area_c = strength.snapshot(later.measurement_unit).histogram_area
            except (FormalDivergenceUnavailable, MacdStrengthUnavailable, TypeError, ValueError):
                continue
            if not evidence.histogram_area_decayed or area_a is None or area_c is None:
                continue
            first = index[earlier.units[0].unit_id]
            end = index[later.terminal_unit.unit_id]
            chain = values[first:end + 1]
            if any(not unit.locked or unit.confirmed_at is None or unit.available_at > as_of
                   for unit in chain):
                continue
            if any(left.market_end != right.market_start or left.end_tick != right.start_tick
                   or left.direction == right.direction for left, right in zip(chain, chain[1:])):
                continue
            known_at = max(evidence.available_at, *(unit.available_at for unit in center.establishment_units),
                           *(unit.available_at for unit in chain))
            if known_at > as_of:
                continue
            direction = later.terminal_unit.direction
            alert_id = stable_structure_id(NATIVE_CONSOLIDATION_RULE_BASIS, center.price_basis_revision,
                                           center.center_id, form, evidence.compare_leg_unit_ids,
                                           evidence.signal_leg_unit_ids)
            signal = later.measurement_unit
            from .native_consolidation_preview import hypothesis_id
            start_at = int(later.units[0].market_start.timestamp())
            breaks_core = signal.high_tick > center.zg_tick if direction == "up" else signal.low_tick < center.zd_tick
            output[alert_id] = {
                "alert_id": alert_id, "signal_type": "consolidation_divergence", "status": "confirmed",
                "hypothesis_id": hypothesis_id(center.center_id, form, evidence.compare_leg_unit_ids,
                                               start_at, direction, center.price_basis_revision),
                "signal_start_at": start_at,
                "rule_basis": NATIVE_CONSOLIDATION_RULE_BASIS, "member_basis": "native_locked_segment",
                "structural_level": 0, "source_kind": "segment", "direction": direction,
                "price_basis_revision": center.price_basis_revision, "center_id": center.center_id,
                "comparison_form": form, "core_position": "breakout" if breaks_core else "inside",
                "center_zd_tick": center.zd_tick, "center_zg_tick": center.zg_tick,
                "compare_leg_unit_ids": list(evidence.compare_leg_unit_ids),
                "signal_leg_unit_ids": list(evidence.signal_leg_unit_ids),
                "source_unit_ids": [unit.unit_id for unit in chain],
                "price_anchor_unit_id": evidence.price_anchor_unit_id,
                "anchor_at": int(evidence.anchor_at.timestamp()), "anchor_tick": evidence.anchor_tick,
                "confirmed_at": int(known_at.timestamp()), "available_at": int(known_at.timestamp()),
                "macd_area_a": area_a, "macd_area_c": area_c, "macd_area_ratio": area_c / area_a,
                "price_extreme_confirmed": evidence.price_extreme_confirmed,
            }
    return sorted(output.values(), key=lambda alert: (alert["available_at"], alert["alert_id"]))[-200:]
