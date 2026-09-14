"""Bind a 1m reversal to the actual terminal interval of one 5m signal."""
from __future__ import annotations

from decimal import Decimal

from .cache import REPLAY_CHECKS
from .confirmation import is_confirmed

STRATEGY = "5m_with_1m_confirmation"
MAIN_FREQUENCY = "5m"
CONFIRMATION_FREQUENCY = "1m"
PENDING = "LOWER_CONFIRMATION_PENDING"


def main_interval(snapshot, point):
    """Market interval and exact five-minute candle containing the turning price.

    Close-labelled 5m endpoints represent a five-minute window, not an exact
    one-minute turning time. Expand its left boundary, never its right one.
    """
    if (snapshot.get("source_frequency") != MAIN_FREQUENCY
            or point.get("source_kind") != "segment" or point.get("structural_level") != 0):
        raise ValueError("区间套主信号必须来自 5m 原生线段")
    units = snapshot.get("screening_segments", [])
    indices = {u["unit_id"]: i for i, u in enumerate(units)}
    first = point["point_type"] in {"1buy", "1sell"}
    identifiers = ((point.get("divergence") or {}).get("signal_leg_unit_ids", []) if first
                   else [point.get("anchor_unit_id")])
    positions = [indices.get(identifier) for identifier in identifiers]
    if (not positions or any(i is None for i in positions)
            or positions != list(range(positions[0], positions[0] + len(positions)))):
        raise ValueError("缺少 5m 离开或首次回试区间的连续线段证据")
    selected = [units[i] for i in positions]
    anchor = point["anchor_at"]
    start = selected[0]["start_time"] - 300
    if not start < anchor <= selected[-1]["end_time"]:
        raise ValueError("5m 价格拐点不在对应的离开或回试区间内")
    return {"kind": "terminal_departure" if first else "first_retest",
            "unit_ids": identifiers, "start_after": start, "end_at": anchor,
            "pivot_after": anchor - 300, "pivot_through": anchor,
            "anchor_tick": point["anchor_tick"]}


def matching_confirmations(main_snapshot, point, lower_snapshot, candidates):
    """Match terminal trend divergence or a same-side point in the 5m retest."""
    interval = main_interval(main_snapshot, point)
    if (lower_snapshot.get("symbol") != main_snapshot.get("symbol")
            or lower_snapshot.get("source_frequency") != CONFIRMATION_FREQUENCY
            or lower_snapshot.get("source_closed_at") != main_snapshot.get("source_closed_at")
            or lower_snapshot.get("price_basis_revision") != main_snapshot.get("price_basis_revision")
            or Decimal(str(lower_snapshot.get("structure_price_quantum")))
            != Decimal(str(main_snapshot.get("structure_price_quantum")))):
        raise ValueError("5m 与 1m 的标的、截止时间或价格基准不一致")
    if (type(lower_snapshot.get("source_started_at")) is not int
            or lower_snapshot["source_started_at"] > interval["start_after"] + 60):
        raise ValueError("1m 历史未覆盖完整的 5m 离开或回试区间，不能判断区间套")
    matches = []
    for candidate in candidates:
        lower = candidate["point"]
        if (lower.get("source_kind") != "segment" or lower.get("structural_level") != 0
                or lower.get("side") != point["side"]
                or lower.get("price_basis_revision") != main_snapshot["price_basis_revision"]
                or lower.get("point_type") not in {"1" + point["side"], "2" + point["side"], "3" + point["side"]}
                or not interval["start_after"] < lower["anchor_at"] <= interval["end_at"]
                or not lower["anchor_at"] <= lower["available_at"] <= lower_snapshot["source_closed_at"]):
            continue
        if (point["point_type"].startswith("1")
                and (lower.get("point_type") != "1" + point["side"]
                     or (lower.get("divergence") or {}).get("kind") != "trend"
                     or not interval["pivot_after"] < lower["anchor_at"] <= interval["pivot_through"]
                     or lower["anchor_tick"] != point["anchor_tick"]
                     or type(lower.get("dependency_from")) is not int
                     or lower["dependency_from"] <= interval["start_after"])):
            continue
        matches.append(candidate)
    # One reproducible association per finished main point. Waiting points do
    # not displace an already confirmed lower reversal in the target interval.
    matches.sort(key=lambda item: (not is_confirmed(item["point"]),
                                  item["point"]["available_at"], item["point"]["point_type"],
                                  item["point"]["point_id"]))
    return interval, matches


def attach_confirmation(candidate, interval, lower):
    """Keep immutable chart points separate from the combined selection state."""
    point = candidate["point"]
    lower_confirmed = lower is not None and is_confirmed(lower["point"])
    missing = list(point.get("missing_conditions", []))
    if not lower_confirmed:
        missing.append("lower_1m_confirmation")
    ready = is_confirmed(point) and lower_confirmed
    return {**candidate,
            "selection_status": "confirmed" if ready else "approaching",
            "selection_available_at": max(point["available_at"], lower["point"]["available_at"] if lower else 0),
            "selection_missing_conditions": missing,
            "reasons": list(dict.fromkeys([*candidate.get("reasons", []), *([] if lower_confirmed else [PENDING])])),
            "nested_confirmation": {"strategy": STRATEGY, "frequency": CONFIRMATION_FREQUENCY,
                                    "state": "confirmed" if lower_confirmed else "waiting",
                                    "interval": interval, "point": lower["point"] if lower else None,
                                    "audit": dict(lower.get("audit", {})) if lower else {}}}


def is_nested_observation(item):
    return (item.get("nested_confirmation", {}).get("strategy") == STRATEGY
            and item.get("point", {}).get("source_kind") == "segment"
            and item.get("point", {}).get("structural_level") == 0
            and item.get("selection_status") == "approaching"
            and bool(item.get("selection_missing_conditions"))
            and bool(item.get("reasons"))
            and set(item["reasons"]) <= {"NOT_CONFIRMED", PENDING})


def saved_confirmation_valid(candidate, main_snapshot, lower_snapshot):
    """Recheck the saved association; neither polling nor charts run analysis."""
    record = candidate.get("nested_confirmation", {})
    if record.get("strategy") != STRATEGY:
        return False
    try:
        from .rules import point_semantic_reasons
        point_catalog = {p["point_id"]: p for level in lower_snapshot["levels"] for p in level.get("points", [])}
        center_catalog = {c["center_id"]: c for level in lower_snapshot["levels"] for c in level.get("centers", [])}
        interval, matches = matching_confirmations(
            main_snapshot, candidate["point"], lower_snapshot,
            lower_snapshot.get("screening_interval_signals", []))
        lower = matches[0] if matches else None
        if lower is not None and (point_catalog.get(lower["point"]["point_id"]) != lower["point"]
                                  or point_semantic_reasons(lower["point"], point_catalog, center_catalog)):
            return False
        if lower is not None and is_confirmed(lower["point"]):
            if not all(lower.get("audit", {}).get(k) is True for k in REPLAY_CHECKS):
                return False
        expected = attach_confirmation({"point": candidate["point"]}, interval, lower)
        return all(candidate.get(k) == expected[k] for k in (
            "nested_confirmation", "selection_status", "selection_available_at", "selection_missing_conditions"))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
