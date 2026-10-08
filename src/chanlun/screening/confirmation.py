"""Current selection lifetime, separate from immutable point confirmation."""
from __future__ import annotations

from decimal import Decimal

from chanlun.core.strict_structure.unit_adapter import _tick


def is_confirmed(point):
    return (point.get("status") == "confirmed" and point.get("confirmed_at") is not None
            and not point.get("missing_conditions"))


def confirmation_reasons(point, catalog):
    if not is_confirmed(point):
        return []
    status = catalog.get(point.get("point_id"), {}).get("state")
    if status == "completed":
        return ["CONFIRMING_SEGMENT_COMPLETED"]
    if status == "unresolved":
        return ["CONFIRMING_SEGMENT_UNRESOLVED"]
    return [] if status == "in_progress" else ["CONFIRMING_SEGMENT_MISSING"]


def native_selection_lifetime_reasons(point, points, catalog, source_valid):
    """Current opportunity window; immutable historical point facts stay intact."""
    reasons = (["CONFIRMING_SEGMENT_COMPLETED"]
               if catalog.get(point.get("point_id"), {}).get("state") == "completed" else [])
    if point.get("point_type") in {"1buy", "1sell"}:
        child_kind = "2buy" if point["point_type"] == "1buy" else "2sell"
        if any(child.get("parent_point_id") == point["point_id"]
               and child.get("point_type") == child_kind and is_confirmed(child)
               and source_valid.get(child.get("point_id")) is True
               and child.get("structural_level") == point.get("structural_level")
               and child.get("price_basis_revision") == point.get("price_basis_revision")
               and child.get("anchor_at", -1) > point["anchor_at"]
               and child.get("available_at", -1) >= point["available_at"]
               for child in points):
            reasons.append("FIRST_ADVANCED_TO_SECOND")
    return reasons


def consolidation_lifetime_reasons(snapshot, alert):
    """A confirmed comparison expires when a subsequent segment has completed.

    This controls the current notification window, not the historical truth of
    the MACD comparison. A later center lock cannot renew an older comparison.
    """
    if alert.get("status") != "confirmed":
        return []
    terminal = (alert.get("signal_leg_unit_ids") or [None])[-1]
    units = snapshot.get("screening_segments") or []
    index = next((i for i, unit in enumerate(units) if unit.get("unit_id") == terminal), None)
    if index is None:
        return ["CONSOLIDATION_SIGNAL_SEGMENT_MISSING"]
    cutoff = snapshot.get("source_closed_at")
    if any(unit.get("locked") is True and type(unit.get("available_at")) is int
           and type(cutoff) is int and unit["available_at"] <= cutoff for unit in units[index + 1:]):
        return ["CONSOLIDATION_FOLLOWING_SEGMENT_COMPLETED"]
    return []


def confirmation_catalog(snapshot, structural_level=0):
    """Follow the structural anchor's immediate reverse source unit.

    A confirmed point already requires its anchor/return unit to be locked.
    Its selection window ends when the next unit locks. Price anchors may
    lie inside a unit or an earlier leg, so fresh evidence uses unit ids.
    Legacy evidence is accepted only when its saved geometry identifies the
    carrier unambiguously; chart line style alone is not completion evidence.
    """
    native = next((level for level in snapshot.get("levels", ())
                   if level.get("structural_level", 0) == structural_level), {})
    points = [p for p in native.get("points", ()) if is_confirmed(p)]
    if not points:
        return {}
    cutoff = snapshot.get("source_closed_at")
    units = (snapshot.get("screening_segments") if structural_level == 0
             else snapshot.get("screening_units_by_level", {}).get(str(structural_level)))
    legacy = structural_level == 0 and units is None
    roles = {}
    if legacy:
        for center in native.get("centers", ()):
            for role in [*center.get("establishment_segments", ()),
                         center.get("lifecycle_leaving_segment"), center.get("completion_return_segment")]:
                if isinstance(role, dict) and role.get("unit_id"):
                    roles[role["unit_id"]] = role
        try:
            quantum = Decimal(str(snapshot["structure_price_quantum"]))
            units = []
            for line in snapshot.get("chart", {}).get("xds", ()):
                start, end = line["points"]
                units.append({"start_time": start["time"], "end_time": end["time"],
                              "start_tick": _tick(start["price"], quantum), "end_tick": _tick(end["price"], quantum),
                              "direction": "up" if end["price"] > start["price"] else "down",
                              "locked": line.get("locked"), "forming": line.get("state") == "forming"})
        except (KeyError, TypeError, ValueError, ArithmeticError):
            units = []
    if not isinstance(units, list) or any(not isinstance(u, dict) for u in units):
        return {}
    by_id = {}
    by_end = {}
    for index, unit in enumerate(units):
        by_id.setdefault(unit.get("unit_id"), []).append(index)
        by_end.setdefault((unit.get("end_time"), unit.get("end_tick")), []).append(index)
    result = {}
    for point in points:
        record = {"state": "unavailable", "source_closed_at": cutoff,
                  "anchor_unit_id": point.get("anchor_unit_id")}
        result[point["point_id"]] = record
        if (not isinstance(point.get("anchor_unit_id"), str) or not point["anchor_unit_id"]
                or point.get("side") not in {"buy", "sell"}
                or point.get("source_kind") != (
                    "segment" if structural_level == 0 else "trend_type"
                ) or point.get("structural_level") != structural_level):
            continue
        positions = by_id.get(point.get("anchor_unit_id"), []) if not legacy else []
        if legacy:
            role = roles.get(point.get("anchor_unit_id"))
            if role and role.get("end_time") is not None and role.get("end_tick") is not None:
                positions = by_end.get((role["end_time"], role["end_tick"]), [])
            elif (point.get("anchor_unit_id") and
                  point.get("price_anchor_unit_id", point["anchor_unit_id"]) == point["anchor_unit_id"]):
                positions = by_end.get((point.get("anchor_at"), point.get("anchor_tick")), [])
        if len(positions) != 1:
            continue
        anchor = units[positions[0]]
        construction = (
            snapshot.get("segment_construction") or {}
            if structural_level == 0 else {}
        )
        tail = construction.get("tail") or {}
        # Never let an older drawn projection override an explicit unresolved
        # interval belonging to this point's immediate completed anchor.
        if (construction.get("status") == "unresolved" and anchor.get("locked") is True
                and anchor.get("forming") is False
                and tail.get("start_time") == anchor.get("end_time")
                and tail.get("direction") == ("up" if point.get("side") == "buy" else "down")
                and type(cutoff) is int and tail.get("available_at") == cutoff):
            try:
                start_tick = _tick(tail["start_price"], Decimal(str(snapshot["structure_price_quantum"])))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if start_tick == anchor.get("end_tick"):
                record.update(state="unresolved", unresolved_tail=dict(tail))
                continue
        if positions[0] + 1 >= len(units):
            continue
        anchor, segment = units[positions[0]:positions[0] + 2]
        direction = "up" if point.get("side") == "buy" else "down"
        times = [anchor.get("start_time"), anchor.get("end_time"), segment.get("end_time")]
        if (type(cutoff) is not int or any(type(t) is not int for t in times)
                or not times[0] < times[1] < times[2] <= cutoff
                or anchor.get("locked") is not True or anchor.get("forming") is not False
                or segment.get("start_time") != anchor["end_time"]
                or segment.get("start_tick") != anchor.get("end_tick")
                or anchor.get("direction") == direction or segment.get("direction") != direction
                or type(segment.get("locked")) is not bool or type(segment.get("forming")) is not bool
                or (segment["locked"] and segment["forming"])
                or not anchor["end_time"] <= point["confirmed_at"] <= cutoff):
            continue
        if not legacy:
            locked_at = segment.get("confirmed_at")
            anchor_locked_at = anchor.get("confirmed_at")
            if (type(anchor_locked_at) is not int or not anchor["end_time"] <= anchor_locked_at <= point["confirmed_at"]
                    or segment["locked"] and (type(locked_at) is not int or not segment["end_time"] <= locked_at <= cutoff)
                    or not segment["locked"] and locked_at is not None
                    or any(
                        "available_at" in unit and (
                            type(unit["available_at"]) is not int
                            or unit["available_at"] > cutoff
                        ) for unit in (anchor, segment)
                    )):
                continue
        record.update(state="completed" if segment["locked"] else "in_progress", segment=dict(segment))
    return result
