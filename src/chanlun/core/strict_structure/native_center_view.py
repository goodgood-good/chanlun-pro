"""Causal operational decomposition of native segment centers.

The frozen first-core scan remains the comparison view. Every formal center
requires five consecutive overlapping roles: entry, three core members, exit.
A narrower five-role seed may become the operational view only after its own
same-level departure/return completes, before the original center closes.
This is an explicit engineering selection policy, not a unique source rule.
Local source discussion of legitimate alternative decompositions:
D:/缠论/chanlun_lesson_corpus/L054_一个具体走势的分析(2007-05-24013731).md
author lines 181-238 (pages 1600-1601). Actual segment ranges are preserved;
L078_继续说线段的划分(2007-09-06222831).md, lines 280-313.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chanlun.core.strict_structure.center_machine import (
    _event,
    _GEOMETRY_STOP_ERRORS,
    advance_center,
    calculate_centers,
    establish_center,
)
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterLevelResult,
    CenterState,
    ConstituentUnit,
    SourceKind,
    TrendCenter,
)

POLICY = "completed_inner_five_role_view_v1"


def _closed_at(center: TrendCenter) -> datetime | None:
    if center.state is CenterState.COMPLETED:
        return center.completed_at
    if center.state is CenterState.SUPERSEDED:
        return center.superseded_at
    return None


def _completed_inner_candidate(original, units, positions, inherited_at=None):
    """Find the first causally available alternative, including old closed views."""
    closed_at = _closed_at(original)
    first = max(positions[u.unit_id] for u in original.initial_units)
    original_core_ids = {u.unit_id for u in original.initial_units}
    best = None
    for start in range(first, len(units) - 4):
        roles = units[start:start + 5]
        if not all(u.locked for u in roles):
            break
        if original_core_ids.intersection(u.unit_id for u in roles[1:4]):
            continue
        seed = establish_center(roles, original.structural_level, SourceKind.SEGMENT)
        if seed is None or not (
            original.zd_tick < seed.zd_tick <= seed.zg_tick < original.zg_tick
        ):
            continue
        # Later seeds cannot win a tie with an already selected earlier seed.
        # Avoid replaying long suffixes whose evidence is already too late.
        clocks = (closed_at, None if best is None else best[0])
        limit = min((clock for clock in clocks if clock is not None), default=None)
        if limit is not None and seed.available_at >= limit:
            continue
        candidate = seed
        for unit in units[start + 5:]:
            if not unit.locked or (limit is not None and unit.available_at >= limit):
                break
            try:
                candidate, _ = advance_center(candidate, unit)
            except ValueError as exc:
                if str(exc) not in _GEOMETRY_STOP_ERRORS:
                    raise
                break
            if candidate.state is CenterState.COMPLETED:
                break
        if not candidate.third_class_confirmed:
            continue
        known_at = max(candidate.available_at, original.established_at)
        if inherited_at is not None:
            known_at = max(known_at, inherited_at)
        if closed_at is not None and known_at >= closed_at:
            continue
        choice = (known_at, start, candidate.center_id, candidate)
        if best is None or choice[:3] < best[:3]:
            best = choice
    return best


def _stamp(center, known_at, view_id):
    if known_at is None:
        return center
    existing = center.view_available_at
    if existing is not None and existing > known_at:
        return center
    return replace(center, view_available_at=known_at, view_id=view_id,
                   available_at=max(center.available_at, known_at))


def _retarget_predecessor(previous, successor, units, positions):
    """Retain a superseded predecessor and reconnect its bridge to the new seed."""
    owned = (*previous.body_units, *previous.failed_departure_units, *previous.excursion_units)
    start = max(positions[u.unit_id] for u in owned) + 1
    stop = positions[successor.initial_units[0].unit_id]
    bridge = units[start:stop]
    # Keep the established exit in its lifecycle role throughout the update.
    # Reopening then clearing the old bridge can temporarily orphan that exit.
    updated = replace(previous, superseded_by_center_id=successor.center_id,
                      superseded_at=successor.established_at,
                      supersession_bridge_units=bridge,
                      available_at=max(previous.available_at, successor.available_at,
                                       *(u.available_at for u in bridge)))
    return updated, _event(updated, CenterEventKind.SUPERSEDED,
                           successor.established_market_time, updated.available_at)


def calculate_native_center_views(
    units: tuple[ConstituentUnit, ...], structural_level: int = 0,
) -> CenterLevelResult:
    """Calculate one causal operational view and retain its selection evidence."""
    values = tuple(units)
    positions = {unit.unit_id: offset for offset, unit in enumerate(values)}

    def scan(start=0, inherited_at=None, inherited_view_id=None):
        baseline = calculate_centers(values[start:], structural_level, SourceKind.SEGMENT)
        for offset, original in enumerate(baseline.centers):
            choice = _completed_inner_candidate(original, values, positions, inherited_at)
            if choice is None:
                continue
            known_at, seed_offset, _, candidate = choice
            leave, ret = candidate.completion_leave_unit, candidate.completion_return_unit
            view_id = stable_structure_id(
                POLICY, original.price_basis_revision, structural_level,
                original.center_id, candidate.center_id, leave.unit_id, ret.unit_id,
            )
            selected = _stamp(candidate, known_at, view_id)
            # The completed leave serves as the next five-role window's entry.
            resume = positions[ret.unit_id] - 1
            if resume <= start:
                raise ValueError("operational selection must advance its source cursor")
            following = scan(resume, known_at, view_id)
            prefix = [_stamp(c, inherited_at, inherited_view_id) for c in baseline.centers[:offset]]
            prefix_ids = {c.center_id for c in prefix}
            prefix_events = [e for e in baseline.events if e.center_id in prefix_ids]
            if inherited_at is not None:
                prefix_events = [replace(e, available_at=max(e.available_at, inherited_at)) for e in prefix_events]
            if prefix and prefix[-1].state is CenterState.SUPERSEDED:
                predecessor, supersession = _retarget_predecessor(prefix[-1], selected, values, positions)
                prefix[-1] = _stamp(predecessor, known_at, view_id)
                prefix_events = [e for e in prefix_events if not (
                    e.center_id == predecessor.center_id and e.kind is CenterEventKind.SUPERSEDED)]
                prefix_events = [replace(e, available_at=max(e.available_at, known_at))
                                 if e.center_id == predecessor.center_id else e
                                 for e in prefix_events]
                prefix_events.append(replace(supersession, available_at=max(supersession.available_at, known_at)))
            chosen_events = (
                _event(selected, CenterEventKind.ESTABLISHED, selected.established_market_time, known_at),
                _event(selected,
                       CenterEventKind.COMPLETED_UP if leave.direction == "up" else CenterEventKind.COMPLETED_DOWN,
                       ret.market_end, known_at, leave=leave, ret=ret),
            )
            record = {
                "policy": POLICY, "view_id": view_id,
                "formation_rule": "five_role",
                "original_center_id": original.center_id,
                "original_core_unit_ids": tuple(u.unit_id for u in original.initial_units),
                "original_entry_unit_id": original.entry_unit.unit_id,
                "original_establishment_leave_unit_id": original.establishment_leave_unit.unit_id,
                "original_establishment_unit_ids": tuple(u.unit_id for u in original.establishment_units),
                "original_zd_tick": original.zd_tick, "original_zg_tick": original.zg_tick,
                "selected_center_id": selected.center_id,
                "selected_core_unit_ids": tuple(u.unit_id for u in selected.initial_units),
                "selected_entry_unit_id": selected.entry_unit.unit_id,
                "selected_establishment_leave_unit_id": selected.establishment_leave_unit.unit_id,
                "selected_establishment_unit_ids": tuple(u.unit_id for u in selected.establishment_units),
                "selected_five_overlap_low_tick": max(u.low_tick for u in selected.establishment_units),
                "selected_five_overlap_high_tick": min(u.high_tick for u in selected.establishment_units),
                "shared_original_entry_unit_ids": tuple(u.unit_id for u in original.initial_units
                                                        if u.unit_id == selected.entry_unit.unit_id),
                "leave_unit_id": leave.unit_id, "return_unit_id": ret.unit_id,
                "connection_unit_ids": tuple(u.unit_id for u in values[
                    max(positions[u.unit_id] for u in original.initial_units):seed_offset + 1]),
                "available_at": int(known_at.timestamp()),
            }
            return CenterLevelResult(
                structural_level=structural_level,
                price_basis_revision=baseline.price_basis_revision,
                centers=(*prefix, selected, *following.centers), previews=following.previews,
                events=(*prefix_events, *chosen_events, *following.events),
                locked_unit_count=baseline.locked_unit_count,
                replay_from=(resume - start) + following.replay_from,
                unprocessed_unit_ids=following.unprocessed_unit_ids,
                view_selections=(record, *following.view_selections),
            )
        if inherited_at is None:
            return baseline
        return replace(
            baseline,
            centers=tuple(_stamp(c, inherited_at, inherited_view_id) for c in baseline.centers),
            previews=tuple(replace(p, available_at=max(p.available_at, inherited_at)) for p in baseline.previews),
            events=tuple(replace(e, available_at=max(e.available_at, inherited_at)) for e in baseline.events),
        )

    return scan()
