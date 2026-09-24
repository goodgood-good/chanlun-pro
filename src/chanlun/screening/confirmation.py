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


def confirmation_catalog(snapshot):
    """Follow the structural anchor's immediate reverse segment, never a later rally.

    A confirmed point already requires its anchor/return segment to be locked.
    Its selection window ends when the *next* segment locks. Price anchors may
    lie inside a segment or an earlier leg, so fresh evidence uses unit ids.
    Legacy evidence is accepted only when its saved geometry identifies the
    carrier unambiguously; chart line style alone is not completion evidence.
    """
    native = next((level for level in snapshot.get("levels", ())
                   if level.get("structural_level") == 0), {})
    points = [p for p in native.get("points", ()) if is_confirmed(p)]
    if not points:
        return {}
    cutoff = snapshot.get("source_closed_at")
    units = snapshot.get("screening_segments")
    legacy = units is None
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
                or point.get("source_kind") != "segment" or point.get("structural_level") != 0):
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
        construction = snapshot.get("segment_construction") or {}
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
                    or not segment["locked"] and locked_at is not None):
                continue
        record.update(state="completed" if segment["locked"] else "in_progress", segment=dict(segment))
    return result
