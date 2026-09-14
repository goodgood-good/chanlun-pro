"""Price references derived from known structure, separate from point evidence.

See docs/point_exit_prices.md for local original-text sources and the limits of
each derivation. These are observations, not orders or promised fill prices.
"""
from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal, InvalidOperation


EXIT_PRICE_VERSION = "structural-exit-references-v1"
_CORPUS = "D:/缠论/chanlun_lesson_corpus/"
EXIT_PRICE_SOURCES = {
    "L20": {"title": "第20课：三类买卖点与盘整退出", "path": _CORPUS +
            "L020_缠中说禅走势中枢级别扩张及第三类买卖点.md", "lines": "427–469"},
    "L22": {"title": "第22课回复：买卖点与止损", "path": _CORPUS +
            "L022_将8亿的大米装到5个庄家的肚里。(2007-01-11.md", "lines": "1723–1732"},
    "L29": {"title": "第29课：背驰后末中枢的回抽观察", "path": _CORPUS +
            "L029_转折的力度与级别(2007-02-09150808).md", "lines": "190–268"},
    "L45": {"title": "第45课：按操作级别等待反向买卖点", "path": _CORPUS +
            "L045_持股与持币,两种最基本的操作。(2007-04-12.md", "lines": "37–64，157–184"},
    "L53": {"title": "第53课：二类点及同级别操作", "path": _CORPUS +
            "L053_三类买卖点的再分辨.md", "lines": "235–298"},
}


def _group(point):
    return (point.get("structural_level"), point.get("source_kind"), point.get("price_basis_revision"))


def _price_reference(tick, quantum, **fields):
    valid = type(tick) is int and tick > 0 and quantum is not None
    return {"price_tick": tick if valid else None,
            "price": float(tick * quantum) if valid else None, **fields}


def _reverse_index(points):
    """Suffix minima answer the first *available* reverse point in O(log n)."""
    groups = {}
    for point in points:
        if (point.get("status") == "confirmed" and type(point.get("confirmed_at")) is int
                and point["confirmed_at"] <= point["available_at"]):
            groups.setdefault((*_group(point), point["side"]), []).append(point)
    index = {}
    for key, values in groups.items():
        values.sort(key=lambda p: (p["anchor_at"], p["available_at"], p["point_id"]))
        earliest = [None] * len(values)
        best = None
        for i in range(len(values) - 1, -1, -1):
            item = values[i]
            if best is None or (item["available_at"], item["anchor_at"], item["point_id"]) < (
                    best["available_at"], best["anchor_at"], best["point_id"]):
                best = item
            earliest[i] = best
        index[key] = ([p["anchor_at"] for p in values], earliest)
    return index


def _first_observation(point, center):
    """Freeze the terminal-center envelope to evidence available at this point.

    New snapshots carry unit availability. Older snapshots may use the saved
    envelope only if the *whole* center was already known at point availability.
    Never take a later center extension and write it back into an earlier target.
    """
    divergence = point.get("divergence") or {}
    if (not center or _group(center) != _group(point)
            or divergence.get("kind") != "trend"
            or divergence.get("structural_level") != point.get("structural_level")
            or center.get("established_at", float("inf")) > point["available_at"]):
        return None
    units = {u["unit_id"]: u for u in [*center.get("establishment_segments", ()),
                                       *center.get("overlap_components", ())]}
    body_ids = center.get("body_unit_ids", ())
    if body_ids and all(uid in units and type(units[uid].get("available_at")) is int for uid in body_ids):
        known = [units[uid] for uid in body_ids
                 if units[uid]["available_at"] <= point["available_at"]
                 and units[uid]["end_time"] <= point["anchor_at"] and units[uid].get("locked") is True]
        if not set(center.get("core_unit_ids", ())) <= {u["unit_id"] for u in known}:
            return None
        ticks = [u.get("low_tick" if point["side"] == "buy" else "high_tick") for u in known]
        if not ticks or any(type(t) is not int for t in ticks):
            return None
        return min(ticks) if point["side"] == "buy" else max(ticks)
    if center.get("available_at", float("inf")) <= point["available_at"]:
        return center.get("envelope", {}).get("dd_tick" if point["side"] == "buy" else "gg_tick")
    return None


def build_point_exit_plans(snapshot):
    """Compute cheap, causal presentation references; do not change point IDs."""
    cutoff = snapshot.get("source_closed_at")
    if type(cutoff) is not int:
        return {}
    try:
        quantum = Decimal(str(snapshot["structure_price_quantum"]))
        if not quantum.is_finite() or quantum <= 0:
            quantum = None
    except (KeyError, ValueError, InvalidOperation):
        quantum = None
    points = [p for level in snapshot.get("levels", ()) for p in level.get("points", ())
              if p.get("point_type") in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
              and p.get("side") == ("buy" if p["point_type"].endswith("buy") else "sell")
              and type(p.get("anchor_at")) is int and type(p.get("available_at")) is int
              and p["anchor_at"] <= p["available_at"] <= cutoff
              and p.get("price_basis_revision") == snapshot.get("price_basis_revision")]
    centers = {c["center_id"]: c for level in snapshot.get("levels", ()) for c in level.get("centers", ())}
    reverse_index = _reverse_index(points)
    plans = {}
    for point in points:
        buy = point["side"] == "buy"
        kind = point["point_type"][0]
        confirmed = point.get("status") == "confirmed"
        anchor = point.get("anchor_tick")
        stop = point.get("invalidation_tick")
        valid_stop = type(stop) is int and type(anchor) is int and (stop <= anchor if buy else stop >= anchor)
        stop_kind = "center_edge" if kind == "3" else (
            "parent_extreme" if kind == "2" and point.get("variant") == "strict" else "turn_extreme")
        comparator = ("lte" if buy else "gte") if kind == "3" else ("lt" if buy else "gt")
        # All numeric stops are an engineering interpretation of invalidation,
        # not an original-text fixed stop order or an instruction to short A shares.
        stop_loss = _price_reference(stop if valid_stop else None, quantum,
            status="reference" if confirmed else "provisional", basis=stop_kind, trigger=comparator,
            derivation="system_structural_invalidation", sources=["L22", "L20" if kind == "3" else "L53"])
        if stop_loss["price"] is None:
            stop_loss["status"] = "unavailable"
        take_profit = _price_reference(None, quantum, status="waiting", basis="opposite_point", sources=["L45", "L53"])
        observation = None
        if kind == "1":
            tick = _first_observation(point, centers.get(point.get("center_id")))
            if type(tick) is int and type(anchor) is int and (tick > anchor if buy else tick < anchor):
                observation = _price_reference(tick, quantum,
                    status="reference" if confirmed else "provisional", basis="terminal_dd" if buy else "terminal_gg",
                    available_at=point["available_at"], sources=["L29"], derivation="structural_observation")
                take_profit = dict(observation)
        exit_point = None
        if confirmed:
            times, earliest = reverse_index.get((*_group(point), "sell" if buy else "buy"), ([], []))
            # A pivot that pre-dates recognition of the entry is not a later exit.
            i = bisect_right(times, point["available_at"])
            if i < len(times):
                other = earliest[i]
                exit_point = {"point_id": other["point_id"], "point_type": other["point_type"],
                              "anchor_at": other["anchor_at"], "available_at": other["available_at"]}
                take_profit = _price_reference(other.get("anchor_tick"), quantum, status="exit_confirmed",
                    basis="opposite_point", sources=["L45"], **exit_point)
        plans[point["point_id"]] = {
            "version": EXIT_PRICE_VERSION, "point_id": point["point_id"],
            "source_frequency": snapshot.get("source_frequency"), "structural_level": point.get("structural_level"),
            "side": point["side"], "point_status": point.get("status"),
            "as_of": cutoff, "known_at": point["available_at"],
            "stop_loss": stop_loss, "take_profit": take_profit, "profit_observation": observation,
        }
    return plans


def with_point_exit_plans(snapshot):
    """Enrich a live or verified saved snapshot without mutating frozen evidence."""
    return {**snapshot, "point_exit_version": EXIT_PRICE_VERSION,
            "point_exit_sources": EXIT_PRICE_SOURCES, "point_exit_plans": build_point_exit_plans(snapshot)}
