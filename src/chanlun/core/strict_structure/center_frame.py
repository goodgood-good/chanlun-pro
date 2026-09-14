"""Read formal center roles without constructing alternative partitions."""

from __future__ import annotations

from chanlun.core.strict_structure.models import ConstituentUnit


def center_frame_leave(center) -> ConstituentUnit | None:
    """Use the current departure while retaining initial establishment evidence.

    A failed departure stays in lifecycle history. Once its return is folded
    into the center, it cannot cut off later confirmed extensions merely
    because the eventual departure has the opposite direction.
    """
    core = center.initial_units
    candidates = (
        center.lifecycle_leave_unit,
        getattr(center, "establishment_leave_unit", None),
        *getattr(center, "failed_departure_units", ()),
    )
    valid = [u for u in candidates if u is not None
             and u.unit_id not in {c.unit_id for c in core}
             and u.market_start >= core[-1].market_end
             and max(u.low_tick, center.zd_tick) <= min(u.high_tick, center.zg_tick)
             and (u.end_tick > center.zg_tick if u.direction == "up" else u.end_tick < center.zd_tick)]
    return next(iter(valid), None)


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
