from __future__ import annotations

from chanlun.core.strict_structure.center_machine import validate_unit_sequence
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.identity import build_trend_id, stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterRelation,
    CenterState,
    DecompositionBoundaryEvidence,
    TrendAssemblyResult,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.strength import (
    MacdStrengthUnavailable,
    compare_terminal_trend_divergence,
)


def _validate_center_references(
    centers, units, structural_level, oscillatory_ids=frozenset()
):
    if not units:
        raise ValueError("center references require source units")
    source_kind = centers[0].source_kind
    validate_unit_sequence(units, structural_level, source_kind, oscillatory_ids)
    index = {item.unit_id: offset for offset, item in enumerate(units)}
    if len(index) != len(units):
        raise ValueError("unit ids must be unique")
    if len({center.center_id for center in centers}) != len(centers):
        raise ValueError("center ids must be unique")

    previous_start = -1
    previous_completion_leave_index = None
    for center in centers:
        if (
            center.structural_level != structural_level
            or center.source_kind is not source_kind
        ):
            raise ValueError("center level/source mismatch")
        if center.price_basis_revision != units[0].price_basis_revision:
            raise ValueError("center price basis mismatch")
        try:
            body_indexes = tuple(index[item.unit_id] for item in center.body_units)
        except KeyError as exc:
            raise ValueError("center references a missing unit") from exc
        entry_index = index.get(center.entry_unit.unit_id)
        if entry_index is None or units[entry_index] != center.entry_unit:
            raise ValueError("center references missing or changed entry evidence")
        if entry_index + 1 != body_indexes[0]:
            raise ValueError("external entry must immediately precede center body")
        if body_indexes != tuple(range(body_indexes[0], body_indexes[-1] + 1)):
            raise ValueError("center body must be a contiguous unit slice")
        for offset, item in zip(body_indexes, center.body_units):
            if units[offset] != item:
                raise ValueError("center unit evidence changed")

        start = entry_index
        if start <= previous_start:
            raise ValueError("centers must be strictly ordered in the unit stream")
        if (
            previous_completion_leave_index is not None
            and start < previous_completion_leave_index
        ):
            raise ValueError(
                "next center cannot precede the previous completion leave"
            )

        return_index = None
        completion_leave_index = None
        if center.state is CenterState.COMPLETED:
            leave = center.completion_leave_unit
            ret = center.completion_return_unit
            if leave is None or ret is None:
                raise ValueError("completed center requires completion ownership")
            leave_index = index.get(leave.unit_id)
            if leave_index != body_indexes[-1] + 1 or units[leave_index] != leave:
                raise ValueError(
                    "external completion leave must immediately follow center body"
                )
            completion_leave_index = leave_index
            return_index = index.get(ret.unit_id)
            if return_index != leave_index + 1:
                raise ValueError("completion return must immediately follow leave")
            if units[return_index] != ret:
                raise ValueError("completion return evidence changed")
        elif center.pending_leave_unit is not None:
            leave = center.pending_leave_unit
            leave_index = index.get(leave.unit_id)
            if leave_index != body_indexes[-1] + 1 or units[leave_index] != leave:
                raise ValueError(
                    "external pending leave must immediately follow center body"
                )
        elif (
            center.completion_leave_unit is not None
            or center.completion_return_unit is not None
            or center.completed_at is not None
        ):
            raise ValueError("ongoing center cannot expose completion ownership")

        previous_start = start
        previous_completion_leave_index = completion_leave_index
    return index


def _constituent_units(group, units, index, group_start, *, end_index=None):
    tail = group[-1].completion_leave_unit or group[-1].body_units[-1]
    end = index[tail.unit_id] if end_index is None else end_index
    if end < group_start:
        raise ValueError("trend unit slice is inverted")
    return units[group_start : end + 1]


def _group_is_complete(group, constituent_units):
    return (
        all(center.state is CenterState.COMPLETED for center in group)
        and all(
            center.completion_leave_unit is not None
            and center.completion_return_unit is not None
            and center.completion_leave_unit.locked
            and center.completion_return_unit.locked
            for center in group
        )
        and bool(constituent_units)
        and all(item.locked for item in constituent_units)
    )


def _direction(constituent_units):
    start = constituent_units[0].start_tick
    end = constituent_units[-1].end_tick
    if end != start:
        return "up" if end > start else "down"
    return constituent_units[-1].direction


def _completion_times(group, constituent_units):
    confirmations = tuple(center.completed_at for center in group)
    if any(value is None for value in confirmations):
        raise ValueError("complete group requires completion confirmations")
    confirmed_at = max(confirmations)
    available_at = max(
        tuple(center.available_at for center in group)
        + tuple(item.available_at for item in constituent_units)
    )
    return confirmed_at, available_at


def _build(
    group,
    constituent_units,
    structural_level,
    state,
    confirmed_at,
    available_at,
    terminal_divergence=None,
):
    all_units = tuple(constituent_units)
    start = all_units[0]
    tail = all_units[-1]
    direction = _direction(all_units)
    kind = TrendKind.TREND if len(group) >= 2 else TrendKind.CONSOLIDATION
    return TrendType(
        trend_id=build_trend_id(
            price_basis_revision=start.price_basis_revision,
            structural_level=structural_level,
            center_ids=tuple(center.center_id for center in group),
            constituent_unit_ids=tuple(item.unit_id for item in all_units),
            direction=direction,
        ),
        structural_level=structural_level,
        price_basis_revision=start.price_basis_revision,
        kind=kind,
        direction=direction,
        state=state,
        centers=tuple(group),
        constituent_units=all_units,
        start_tick=start.start_tick,
        end_tick=tail.end_tick,
        low_tick=min(item.low_tick for item in all_units),
        high_tick=max(item.high_tick for item in all_units),
        market_start=start.market_start,
        market_end=tail.market_end,
        confirmed_at=confirmed_at,
        available_at=available_at,
        terminal_divergence=terminal_divergence,
    )


def _confirmed_divergence_boundary(
    group,
    source_units,
    index,
    group_start,
    structural_level,
    strength,
):
    """Return a complete-c divergence boundary from confirmed evidence only."""

    if strength is None or len(group) < 2:
        return None
    try:
        compared = compare_terminal_trend_divergence(
            group,
            source_units,
            strength,
            trend_start_unit_id=source_units[group_start].unit_id,
        )
    except MacdStrengthUnavailable:
        return None
    if compared is None:
        return None
    divergence, terminal_leg = compared
    if not divergence.is_divergent:
        return None
    terminal_component_id = terminal_leg.child_ids[-1]
    end_index = index.get(terminal_component_id)
    if end_index is None:
        raise ValueError("terminal c references a missing source unit")
    constituent_units = _constituent_units(
        group,
        source_units,
        index,
        group_start,
        end_index=end_index,
    )
    if not _group_is_complete(group, constituent_units):
        return None
    if _direction(constituent_units) != divergence.direction:
        return None
    confirmed_at, available_at = _completion_times(group, constituent_units)
    confirmed_at = max(confirmed_at, divergence.confirmed_at)
    available_at = max(available_at, divergence.available_at)
    complete = _build(
        group,
        constituent_units,
        structural_level,
        TrendState.COMPLETE,
        confirmed_at,
        available_at,
        terminal_divergence=divergence,
    )
    locked = _build(
        group,
        constituent_units,
        structural_level,
        TrendState.LOCKED,
        confirmed_at,
        available_at,
        terminal_divergence=divergence,
    )
    boundary = DecompositionBoundaryEvidence(
        boundary_id=stable_structure_id(
            "chanlun-decomposition-boundary/v1",
            divergence.price_basis_revision,
            "same_level",
            "trend_divergence",
            divergence.structural_level,
            divergence.source_kind.value,
            locked.trend_id,
            divergence.divergence_id,
        ),
        decomposition_mode="same_level",
        boundary_kind="trend_divergence",
        structural_level=divergence.structural_level,
        source_kind=divergence.source_kind,
        price_basis_revision=divergence.price_basis_revision,
        left_trend_id=locked.trend_id,
        # Divergence strength is attached to a distinct complete-c measurement
        # identity; the decomposition endpoint remains the raw terminal unit.
        anchor_unit_id=terminal_component_id,
        anchor_at=divergence.anchor_at,
        anchor_tick=divergence.anchor_tick,
        confirmed_at=divergence.confirmed_at,
        available_at=divergence.available_at,
        divergence=divergence,
    )
    return complete, locked, boundary, end_index


def assemble_trend_types(
    centers,
    units,
    structural_level,
    oscillatory_ids=frozenset(),
    *,
    strength=None,
    ignored_boundary_anchor_ids: frozenset[str] = frozenset(),
    group_start_unit_id: str | None = None,
) -> TrendAssemblyResult:
    values = tuple(centers)
    source_units = tuple(units)
    if not values:
        return TrendAssemblyResult(current_trends=(), completed_trends=())
    index = _validate_center_references(
        values, source_units, structural_level, oscillatory_ids
    )
    ignored_boundaries = frozenset(ignored_boundary_anchor_ids)
    if not ignored_boundaries.issubset(index):
        raise ValueError("ignored boundary anchors must reference source units")
    output = []
    completed = {}
    boundaries = {}
    group = [values[0]]
    group_start = (
        index[values[0].entry_unit.unit_id]
        if group_start_unit_id is None
        else index.get(group_start_unit_id, -1)
    )
    if not 0 <= group_start <= index[values[0].entry_unit.unit_id]:
        raise ValueError("trend group start must precede its first center")
    group_relation = None
    active_divergence_end = None

    def record_complete(candidate_group, candidate_start):
        constituent_units = _constituent_units(
            candidate_group,
            source_units,
            index,
            candidate_start,
        )
        if not _group_is_complete(candidate_group, constituent_units):
            return None
        confirmed_at, available_at = _completion_times(
            candidate_group,
            constituent_units,
        )
        snapshot = _build(
            candidate_group,
            constituent_units,
            structural_level,
            TrendState.COMPLETE,
            confirmed_at,
            available_at,
        )
        previous = completed.setdefault(snapshot.trend_id, snapshot)
        if previous != snapshot:
            raise ValueError("completed trend snapshot identity collision")
        return snapshot

    record_complete(group, group_start)
    for current in values[1:]:
        if group:
            divergence_boundary = _confirmed_divergence_boundary(
                group,
                source_units,
                index,
                group_start,
                structural_level,
                strength,
            )
            if (
                divergence_boundary is not None
                and divergence_boundary[2].anchor_unit_id in ignored_boundaries
            ):
                divergence_boundary = None
            if divergence_boundary is not None:
                complete, locked, boundary, end_index = divergence_boundary
                previous = completed.setdefault(complete.trend_id, complete)
                if previous != complete:
                    raise ValueError("completed divergence trend identity collision")
                output.append(locked)
                previous_boundary = boundaries.setdefault(boundary.boundary_id, boundary)
                if previous_boundary != boundary:
                    raise ValueError("decomposition boundary identity collision")
                group_start = end_index + 1
                group = []
                group_relation = None
                active_divergence_end = end_index

        if not group:
            first_body_index = index[current.body_units[0].unit_id]
            if (
                active_divergence_end is not None
                and first_body_index <= active_divergence_end
            ):
                # This raw center window straddles a boundary that was already
                # confirmed on an earlier prefix.  It cannot revoke or cross
                # that immutable same-level split; wait for the first center
                # whose body starts wholly to its right.
                continue
            group = [current]
            group_relation = None
            active_divergence_end = None
            record_complete(group, group_start)
            continue

        relation = classify_center_relation(group[-1], current)
        continues = (
            relation in (CenterRelation.UP_TREND, CenterRelation.DOWN_TREND)
            and (group_relation is None or relation is group_relation)
        )
        if continues:
            group.append(current)
            group_relation = relation
            record_complete(group, group_start)
            continue

        constituent_units = _constituent_units(
            group,
            source_units,
            index,
            group_start,
        )
        if not _group_is_complete(group, constituent_units):
            raise ValueError("boundary cannot lock incomplete trend group")
        complete_confirmed_at, complete_available_at = _completion_times(
            group,
            constituent_units,
        )
        record_complete(group, group_start)
        if current.state is not CenterState.COMPLETED:
            # A live center can still be replaced by a later five-unit window.
            # It may start a forming boundary, but it cannot irreversibly lock
            # the preceding trend until its own center identity is completed.
            output.append(
                _build(
                    group,
                    constituent_units,
                    structural_level,
                    TrendState.COMPLETE,
                    complete_confirmed_at,
                    complete_available_at,
                )
            )
            terminal_return = group[-1].completion_return_unit
            if terminal_return is None:
                raise ValueError("complete trend requires terminal return")
            # The previous completion return is the earliest source unit of
            # the next five-component center and keeps recursive trend units
            # adjacent without reusing the prior departure.
            group_start = index[terminal_return.unit_id]
            group = [current]
            group_relation = None
            continue
        boundary_confirmed_at = max(
            complete_confirmed_at,
            current.established_at,
        )
        boundary_available_at = max(
            complete_available_at,
            current.available_at,
            max(item.available_at for item in current.establishment_units),
        )
        output.append(
            _build(
                group,
                constituent_units,
                structural_level,
                TrendState.LOCKED,
                boundary_confirmed_at,
                boundary_available_at,
            )
        )
        terminal_return = group[-1].completion_return_unit
        if terminal_return is None:
            raise ValueError("locked trend requires terminal completion return")
        group_start = index[terminal_return.unit_id]
        group = [current]
        group_relation = None
        record_complete(group, group_start)

    divergence_boundary = (
        None
        if not group
        else _confirmed_divergence_boundary(
            group,
            source_units,
            index,
            group_start,
            structural_level,
            strength,
        )
    )
    if (
        divergence_boundary is not None
        and divergence_boundary[2].anchor_unit_id in ignored_boundaries
    ):
        divergence_boundary = None
    if divergence_boundary is not None:
        complete, locked, boundary, _end_index = divergence_boundary
        previous = completed.setdefault(complete.trend_id, complete)
        if previous != complete:
            raise ValueError("completed divergence trend identity collision")
        output.append(locked)
        previous_boundary = boundaries.setdefault(boundary.boundary_id, boundary)
        if previous_boundary != boundary:
            raise ValueError("decomposition boundary identity collision")
    elif group:
        constituent_units = _constituent_units(
            group,
            source_units,
            index,
            group_start,
        )
        if _group_is_complete(group, constituent_units):
            tail_state = TrendState.COMPLETE
            tail_confirmed_at, tail_available_at = _completion_times(
                group,
                constituent_units,
            )
        else:
            tail_state = TrendState.FORMING
            tail_confirmed_at = None
            tail_available_at = max(
                tuple(center.available_at for center in group)
                + tuple(item.available_at for item in constituent_units)
            )
        output.append(
            _build(
                group,
                constituent_units,
                structural_level,
                tail_state,
                tail_confirmed_at,
                tail_available_at,
            )
        )
        record_complete(group, group_start)
    return TrendAssemblyResult(
        current_trends=tuple(output),
        completed_trends=tuple(
            sorted(
                completed.values(),
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        ),
        decomposition_boundaries=tuple(
            sorted(
                boundaries.values(),
                key=lambda item: (item.available_at, item.boundary_id),
            )
        ),
    )
