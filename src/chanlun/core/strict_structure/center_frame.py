"""Read formal center roles without constructing alternative partitions."""

from __future__ import annotations

from chanlun.core.strict_structure.models import ConstituentUnit


def center_frame_leave(center) -> ConstituentUnit | None:
    """Preserve a directional frame's departure, or its opposite final exit.

    A return may disprove a departure's *ending*, but cannot erase the five
    roles already observed. Prefer the earliest departure in the entry's
    direction when one exists; a later opposite ending does not replace it.
    Failed departures are retained by the center machine for this purpose.
    Lessons 87/88 also permit an opposite final departure: the direction of
    entry defines the core's role, not the direction of every later exit.
    """
    core = center.initial_units
    expected = ("up" if tuple(u.direction for u in core) == ("down", "up", "down")
                else "down" if tuple(u.direction for u in core) == ("up", "down", "up")
                else None)
    candidates = (
        *getattr(center, "failed_departure_units", ()),
        getattr(center, "establishment_leave_unit", None),
        center.lifecycle_leave_unit,
    )
    valid = [u for u in candidates if u is not None
             and u.unit_id not in {c.unit_id for c in core}
             and u.market_start >= core[-1].market_end
             and max(u.low_tick, center.zd_tick) <= min(u.high_tick, center.zg_tick)
             and (u.end_tick > center.zg_tick if u.direction == "up" else u.end_tick < center.zd_tick)]
    directional = [u for u in valid if u.direction == expected]
    return min(directional or valid, key=lambda u: (u.market_start, u.available_at, u.unit_id), default=None)


def center_frame_evidence(center) -> dict:
    """Observe the five directional display roles without changing formation.

    A core child cannot double as the external departure. Native undirected
    consolidations remain valid cores even if no directional frame is present.
    This helper creates neither a third point nor a movement completion.
    """
    core = center.initial_units
    directions = tuple(unit.direction for unit in core)
    expected = ("up" if directions == ("down", "up", "down") else
                "down" if directions == ("up", "down", "up") else None)
    entry = center.entry_unit
    leave = center_frame_leave(center)
    missing = [] if expected is not None else ["core_direction_not_alternating"]
    for role, unit in (("entry", entry), ("leave", leave)):
        if unit is None:
            missing.append(f"{role}_unavailable")
        elif role == "entry" and expected is not None and unit.direction != expected:
            missing.append(f"{role}_direction_mismatch")
        elif max(unit.low_tick, center.zd_tick) > min(unit.high_tick, center.zg_tick):
            missing.append(f"{role}_does_not_intersect_core")
    return {
        "core_formed": True,
        "frame_qualified": not missing,
        "frame_missing_conditions": tuple(missing),
        "entry_unit_id": None if entry is None else entry.unit_id,
        "frame_leave_unit_id": None if leave is None else leave.unit_id,
        "available_at": max(unit.available_at for unit in (*core, *(
            unit for unit in (entry, leave) if unit is not None
        ))),
    }


def directional_ownership_pending(center):
    """An opposite escape ends the core, not necessarily the earlier movement."""
    leave = center_frame_leave(center)
    return bool(center.third_class_confirmed and center.entry_unit is not None and leave is not None
                and center.completion_direction != center.entry_unit.direction)


def directional_frame_end(center):
    """Separate the directional body from a later reversal observation.

    The first directional departure stays external when the later escape is
    opposite to the entering movement. Later touches remain in the lifecycle
    record while movement ownership is pending; they do not silently turn the
    original departure into a child of the displayed directional body.
    """
    if directional_ownership_pending(center):
        return center_frame_leave(center).market_start
    return center.display_range_end_market_time
