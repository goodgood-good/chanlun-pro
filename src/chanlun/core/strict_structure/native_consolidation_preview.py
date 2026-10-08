"""Revisable holdings warnings, never completed segments or buy/sell points.

Local sources: D:/缠论/chanlun_lesson_corpus/L061_*:97-121 (hypothesis and
refutation), L024_*:310-319 (conditional area estimate), 334-379 (inside /
departure comparisons). The two shrinking closed bars and three-candle local
turn below are engineering gates, not an author's exact fractal definition.
"""
from __future__ import annotations

import math

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import CenterRelation, CenterState
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.native_center_signals import _center_sources_known
from chanlun.core.strict_structure.strength import center_entry_comparison_leg, _comparison_leg

PREVIEW_RULE = "native_consolidation_early_v1"
PREVIEW_AUDIT = ("cold_rebuild", "closed_bar_replay")


def hypothesis_id(center_id, form, earlier_ids, start_at, direction, basis):
    # Endpoint, MACD values and confirmation clocks deliberately do not own identity.
    return stable_structure_id(PREVIEW_RULE, center_id, form, tuple(earlier_ids),
                               start_at, direction, basis)


def _connected(values):
    return all(a.market_end == b.market_start and a.end_tick == b.start_tick
               and a.direction != b.direction for a, b in zip(values, values[1:]))


def build_native_consolidation_previews(units, centers, strength, frame, histogram, *, as_of, diagnostics=None):
    """Require a real unlocked tail, weak area, closed momentum and a local turn."""
    values = tuple(units)
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(schema="native-preview-checks-v1", source_closed_at=int(as_of.timestamp()),
                       evaluations={}, reason_code="COMPARISON_CHANGED",
                       price_basis_revision=values[-1].price_basis_revision if values else None,
                       segments=[{"start_at": int(u.market_start.timestamp()), "direction": u.direction,
                                  "locked": u.locked} for u in values[-8:]])

    def reject(code, **details):
        diagnostics.update(reason_code=code, details=details)
        return []

    if strength is None or len(values) < 3 or frame.empty:
        return reject("COMPARISON_UNAVAILABLE")
    tail = values[-1]
    diagnostics.update(tail_start_at=int(tail.market_start.timestamp()), direction=tail.direction)
    if tail.locked or tail.confirmed_at is not None or tail.available_at > as_of:
        return reject("OPEN_SEGMENT_CHANGED")
    bars = list(frame.itertuples(index=False))
    dates = [bar.date.to_pydatetime() for bar in bars]
    hist = [float(value) for value in histogram]
    if len(hist) != len(bars) or dates[-1] != as_of or not all(math.isfinite(v) for v in hist):
        raise ValueError("preview MACD and closed bars must share the exact cutoff")
    positions = {date: i for i, date in enumerate(dates)}
    pivot = positions.get(tail.market_end)
    if pivot is None or pivot < 1 or pivot + 1 >= len(bars):
        return reject("LOCAL_TURN_PENDING")
    left, middle, right = bars[pivot - 1:pivot + 2]
    up = tail.direction == "up"
    turn = (middle.high > max(left.high, right.high) if up
            else middle.low < min(left.low, right.low))
    if not turn:
        return reject("LOCAL_TURN_CHANGED")
    # A later price extreme invalidates this specific endpoint hypothesis.
    if (any(bar.high > middle.high for bar in bars[pivot + 1:]) if up
            else any(bar.low < middle.low for bar in bars[pivot + 1:])):
        return reject("PRICE_EXTREME_BROKEN", anchor=float(middle.high if up else middle.low),
                      latest_extreme=float(max(bar.high for bar in bars[pivot + 1:]) if up
                                           else min(bar.low for bar in bars[pivot + 1:])))
    sign = 1 if up else -1
    momentum = [max(0.0, sign * value) for value in hist]
    start = positions.get(tail.market_start)
    if start is None or len(bars) - start < 4:
        return reject("COMPARISON_UNAVAILABLE")
    weakening = next((i for i in range(len(hist) - 1, max(start + 1, pivot), -1)
                      if momentum[i - 2] > momentum[i - 1] > momentum[i]), None)
    if weakening is None:
        return reject("MOMENTUM_CONTRACTION_LOST", latest_momentum=momentum[-3:])
    if any(momentum[i] > momentum[weakening] for i in range(weakening + 1, len(hist))):
        return reject("MOMENTUM_RECOVERED", contracted_momentum=momentum[weakening],
                      latest_momentum=momentum[-1])
    by_id = {unit.unit_id: unit for unit in values}
    index = {unit.unit_id: i for i, unit in enumerate(values)}
    visible = tuple(c for c in centers if c.available_at <= as_of)
    output = []
    for offset, center in enumerate(visible):
        if not _center_sources_known(center, by_id, as_of):
            continue
        owned = {u.unit_id for u in (*center.body_units, *center.establishment_units,
                                    *center.failed_departure_units)}
        if center.pending_leave_unit is not None:
            owned.add(center.pending_leave_unit.unit_id)
        pairs = []
        a, b = values[-3:-1]
        overlaps = lambda u: max(u.low_tick, center.zd_tick) <= min(u.high_tick, center.zg_tick)
        if (center.state is CenterState.ONGOING and a.unit_id in owned and b.unit_id in owned
                and a.direction == tail.direction and _connected((a, b, tail))
                and all(overlaps(u) for u in (a, b, tail))):
            pairs.append(("center_oscillation", _comparison_leg((a,)), (tail,)))
        earlier = center_entry_comparison_leg(center, values)
        if earlier is not None and earlier.terminal_unit.direction == tail.direction:
            later = values[-earlier.width:]
            departure = (
                earlier.width == 1 and center.state is CenterState.ONGOING
                and values[-2].unit_id in owned
                and (tail.end_tick > center.zg_tick if up else tail.end_tick < center.zd_tick)
            ) or (
                earlier.width == 3 and later[0] == center.completion_leave_unit
                and later[1] == center.completion_return_unit
            )
            relation = classify_center_relation(visible[offset - 1], center) if offset else None
            trend_direction = {CenterRelation.UP_TREND: "up", CenterRelation.DOWN_TREND: "down"}.get(relation)
            if departure and tail.direction != trend_direction and _connected(later):
                pairs.append(("entry_departure", earlier, later))
        for form, earlier, later in pairs:
            first = index[earlier.units[0].unit_id]
            chain = values[first:]
            if (not _connected(chain) or any(not u.locked or u.confirmed_at is None
                                             or u.available_at > as_of for u in chain[:-1])
                    or earlier.measurement_unit.market_end >= later[0].market_start):
                continue
            c_start = positions.get(later[0].market_start)
            if c_start is None:
                continue
            area_a = strength.snapshot(earlier.measurement_unit).histogram_area
            area_c = sum(momentum[c_start:])
            a_ids = [u.unit_id for u in earlier.units]
            start_at = int(later[0].market_start.timestamp())
            identity = hypothesis_id(center.center_id, form, a_ids, start_at,
                                     tail.direction, tail.price_basis_revision)
            diagnostics["evaluations"][identity] = {
                "reason_code": "ACTIVE" if area_a and 0 < 2 * area_c < area_a else "AREA_MARGIN_LOST",
                "macd_area_a": area_a, "macd_area_c": area_c,
                "macd_area_ratio": area_c / area_a if area_a else None,
            }
            # Conservative provisional estimate; never equate this with a final area.
            if not area_a or not 0 < 2 * area_c < area_a:
                continue
            witness = bars[weakening - 2:weakening + 1]
            output.append({
                "alert_id": identity, "hypothesis_id": identity,
                "signal_type": "consolidation_divergence", "status": "warning",
                "rule_basis": PREVIEW_RULE, "member_basis": "native_unlocked_segment",
                "structural_level": 0, "source_kind": "segment", "direction": tail.direction,
                "price_basis_revision": tail.price_basis_revision, "center_id": center.center_id,
                "comparison_form": form,
                "core_position": "breakout" if (tail.end_tick > center.zg_tick if up
                                                  else tail.end_tick < center.zd_tick) else "inside",
                "center_zd_tick": center.zd_tick, "center_zg_tick": center.zg_tick,
                "compare_leg_unit_ids": a_ids, "signal_leg_unit_ids": [u.unit_id for u in later],
                "source_unit_ids": [u.unit_id for u in chain], "signal_start_at": start_at,
                "terminal_start_at": int(tail.market_start.timestamp()),
                "price_anchor_unit_id": tail.unit_id, "anchor_at": int(tail.market_end.timestamp()),
                "anchor_tick": tail.end_tick, "confirmed_at": None,
                "available_at": int(as_of.timestamp()), "measurement_end_at": int(as_of.timestamp()),
                "macd_area_a": area_a, "macd_area_c": area_c, "macd_area_ratio": area_c / area_a,
                "estimated_area_c": 2 * area_c, "estimate_factor": 2,
                "momentum_times": [int(bar.date.timestamp()) for bar in witness],
                "momentum_values": momentum[weakening - 2:weakening + 1],
                "turn_times": [int(bar.date.timestamp()) for bar in (left, middle, right)],
                "turn_prices": [float(bar.high if up else bar.low) for bar in (left, middle, right)],
                "price_extreme_confirmed": False,
            })
    return output
