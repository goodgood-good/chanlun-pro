from __future__ import annotations

from chanlun.core.strict_structure.center_machine import validate_unit_sequence
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterRelation,
    CenterState,
    TrendAssemblyResult,
    TrendKind,
    TrendState,
    TrendType,
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
    previous_return_index = None
    previous_leave_index = None
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
        if body_indexes != tuple(range(body_indexes[0], body_indexes[-1] + 1)):
            raise ValueError("center body must be a contiguous unit slice")
        for offset, item in zip(body_indexes, center.body_units):
            if units[offset] != item:
                raise ValueError("center unit evidence changed")

        start = body_indexes[0]
        if start <= previous_start:
            raise ValueError("centers must be strictly ordered in the unit stream")
        if previous_return_index is not None and start < previous_return_index:
            if start != previous_leave_index:
                raise ValueError(
                    "next center can only reuse the previous completion leave"
                )

        return_index = None
        if center.state is CenterState.COMPLETED:
            leave = center.completion_leave_unit
            ret = center.completion_return_unit
            if leave is None or ret is None:
                raise ValueError("completed center requires completion ownership")
            if leave is not center.body_units[-1]:
                raise ValueError("completion leave must be the final body unit")
            if units[body_indexes[-1]] != leave:
                raise ValueError("completion leave evidence changed")
            return_index = index.get(ret.unit_id)
            if return_index != body_indexes[-1] + 1:
                raise ValueError("completion return must immediately follow center body")
            if units[return_index] != ret:
                raise ValueError("completion return evidence changed")
        elif (
            center.completion_leave_unit is not None
            or center.completion_return_unit is not None
            or center.completed_at is not None
        ):
            raise ValueError("ongoing center cannot expose completion ownership")

        previous_start = start
        previous_return_index = return_index
        previous_leave_index = (
            None if return_index is None else return_index - 1
        )
    return index


def _constituent_units(group, units, index, group_start):
    tail = group[-1].completion_leave_unit or group[-1].body_units[-1]
    end = index[tail.unit_id]
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
):
    all_units = tuple(constituent_units)
    start = all_units[0]
    tail = all_units[-1]
    direction = _direction(all_units)
    kind = TrendKind.TREND if len(group) >= 2 else TrendKind.CONSOLIDATION
    return TrendType(
        trend_id=stable_structure_id(
            "chanlun-trend/v3",
            start.price_basis_revision,
            structural_level,
            tuple(center.center_id for center in group),
            tuple(item.unit_id for item in all_units),
            direction,
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
    )


def assemble_trend_types(
    centers, units, structural_level, oscillatory_ids=frozenset()
) -> TrendAssemblyResult:
    values = tuple(centers)
    source_units = tuple(units)
    if not values:
        return TrendAssemblyResult(current_trends=(), completed_trends=())
    index = _validate_center_references(
        values, source_units, structural_level, oscillatory_ids
    )
    output = []
    completed = {}
    group = [values[0]]
    group_start = index[values[0].initial_units[0].unit_id]
    group_relation = None

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
            # A center may reuse the prior center's completion leave as its
            # entry evidence.  The shared segment belongs to the preceding
            # trend type; start the next trend at the completion return so
            # recursive trend units remain adjacent instead of overlapping.
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
            max(item.available_at for item in current.initial_units),
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
    )
