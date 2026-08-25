from __future__ import annotations

from dataclasses import replace

from chanlun.core.strict_structure.center_machine import (
    close_center_at_divergence,
    validate_unit_sequence,
)
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.divergence import (
    compare_center_consolidation_divergence,
)
from chanlun.core.strict_structure.identity import build_trend_id, stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterRelation,
    CenterState,
    DecompositionBoundaryEvidence,
    PendingMovementPartition,
    PendingMovementRole,
    SourceKind,
    TrendAssemblyResult,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.strength import (
    FormalDivergenceUnavailable,
    MacdStrengthUnavailable,
    compare_terminal_trend_divergence,
)


class IncompatibleDecompositionBoundaryError(ValueError):
    """A divergence cannot close the canonical alternating movement."""

    def __init__(self, message: str, anchor_unit_id: str) -> None:
        super().__init__(message)
        self.anchor_unit_id = anchor_unit_id


def _classify_trend_kind(group) -> TrendKind:
    """Classify a center sequence by its actual same-direction relation."""

    centers = tuple(group)
    if len(centers) < 2:
        return TrendKind.CONSOLIDATION
    relations = tuple(
        classify_center_relation(previous, current)
        for previous, current in zip(centers, centers[1:])
    )
    if all(relation is CenterRelation.UP_TREND for relation in relations) or all(
        relation is CenterRelation.DOWN_TREND for relation in relations
    ):
        return TrendKind.TREND
    return TrendKind.CONSOLIDATION


def _validate_center_references(
    centers, units, structural_level, oscillatory_ids=frozenset()
):
    centers = tuple(centers)
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
            failed_indexes = tuple(
                index[item.unit_id] for item in center.failed_departure_units
            )
        except KeyError as exc:
            raise ValueError("center references a missing unit") from exc
        if center.entry_unit is None:
            entry_index = None
        else:
            entry_index = index.get(center.entry_unit.unit_id)
            if entry_index is None or units[entry_index] != center.entry_unit:
                raise ValueError("center references missing or changed entry evidence")
            if entry_index + 1 != body_indexes[0]:
                raise ValueError("external entry must immediately precede center body")
        for offset, item in zip(body_indexes, center.body_units):
            if units[offset] != item:
                raise ValueError("center unit evidence changed")
        for offset, item in zip(failed_indexes, center.failed_departure_units):
            if units[offset] != item:
                raise ValueError("center failed departure evidence changed")
        history_indexes = tuple(sorted((*body_indexes, *failed_indexes)))
        if history_indexes != tuple(range(history_indexes[0], history_indexes[-1] + 1)):
            raise ValueError(
                "center body and failed departures must own a contiguous history"
            )
        history_end_index = history_indexes[-1]
        try:
            bridge_indexes = tuple(
                index[item.unit_id] for item in center.supersession_bridge_units
            )
        except KeyError as exc:
            raise ValueError("center supersession bridge is missing") from exc
        if bridge_indexes and bridge_indexes != tuple(
            range(
                history_end_index + 1,
                history_end_index + 1 + len(bridge_indexes),
            )
        ):
            raise ValueError("supersession bridge must follow center body")
        for offset, item in zip(
            bridge_indexes,
            center.supersession_bridge_units,
        ):
            if units[offset] != item:
                raise ValueError("center supersession bridge changed")

        start = body_indexes[0] if entry_index is None else entry_index
        if start <= previous_start:
            raise ValueError("centers must be strictly ordered in the unit stream")
        if (
            previous_completion_leave_index is not None
            and start < previous_completion_leave_index
        ):
            raise ValueError("next center cannot precede the previous completion leave")

        return_index = None
        completion_leave_index = None
        if center.state is CenterState.COMPLETED:
            leave = center.completion_leave_unit
            ret = center.completion_return_unit
            if leave is None or ret is None:
                raise ValueError("completed center requires completion ownership")
            leave_index = index.get(leave.unit_id)
            if leave_index != history_end_index + 1 or units[leave_index] != leave:
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
            if leave_index != history_end_index + 1 or units[leave_index] != leave:
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
    for position, center in enumerate(centers):
        if center.state is not CenterState.SUPERSEDED:
            continue
        if position + 1 >= len(centers):
            raise ValueError("superseded center is missing its successor")
        successor = centers[position + 1]
        if center.superseded_by_center_id != successor.center_id:
            raise ValueError("superseded center references the wrong successor")
        if center.superseded_at != successor.established_at:
            raise ValueError("supersession must wait for successor establishment")
    return index


def _constituent_units(group, units, index, group_start, *, end_index=None):
    terminal = group[-1]
    tail = terminal.completion_leave_unit
    if tail is None and terminal.supersession_bridge_units:
        tail = terminal.supersession_bridge_units[-1]
    if tail is None:
        # A physical center's independent establishment leave is part of the
        # immutable evidence that makes the formal center (and therefore its
        # containing TrendType) exist.  Formal ownership must include it even
        # while the center is still ongoing; only later, unconfirmed tail
        # units remain available to a pending movement partition.
        establishment_leave = terminal.establishment_leave_unit
        tail = max(
            (
                *terminal.body_units,
                *terminal.failed_departure_units,
                *(() if establishment_leave is None else (establishment_leave,)),
            ),
            key=lambda item: (item.market_start, item.market_end, item.unit_id),
        )
    end = index[tail.unit_id] if end_index is None else end_index
    if end < group_start:
        raise ValueError("trend unit slice is inverted")
    return units[group_start : end + 1]


def _group_is_complete(group, constituent_units):
    return (
        all(center.structurally_closed for center in group)
        and all(
            (
                center.state is CenterState.SUPERSEDED
                and center.superseded_by_center_id is not None
                and center.superseded_at is not None
            )
            or (
                center.state is CenterState.COMPLETED
                and center.completion_leave_unit is not None
                and center.completion_return_unit is not None
                and center.completion_leave_unit.locked
                and center.completion_return_unit.locked
            )
            for center in group
        )
        and bool(constituent_units)
        and all(item.locked for item in constituent_units)
    )


def _group_is_divergence_complete(group, constituent_units, divergence):
    terminal_center = group[-1]
    leave = terminal_center.lifecycle_leave_unit
    unit_ids = tuple(item.unit_id for item in constituent_units)
    signal_ids = divergence.signal_leg_unit_ids
    if len(signal_ids) > len(unit_ids):
        return False
    return (
        len(group) >= 1
        and all(center.structurally_closed for center in group[:-1])
        and terminal_center.state is CenterState.DIVERGENCE_CLOSED
        and terminal_center.boundary_divergence_id == divergence.divergence_id
        and terminal_center.boundary_anchor_unit_id == divergence.signal_unit_id
        and leave is not None
        and leave.unit_id == signal_ids[0]
        and bool(constituent_units)
        and unit_ids[-len(signal_ids) :] == signal_ids
        and constituent_units[-1].unit_id == divergence.signal_unit_id
        and all(item.locked for item in constituent_units)
    )


def _direction(constituent_units):
    start = constituent_units[0].start_tick
    end = constituent_units[-1].end_tick
    if end != start:
        return "up" if end > start else "down"
    return constituent_units[-1].direction


def _reversal_witness_after(source_units, terminal_index, *, locked_only):
    terminal = source_units[terminal_index]
    witness = source_units[terminal_index + 1 : terminal_index + 4]
    if len(witness) != 3 or (locked_only and any(not item.locked for item in witness)):
        return None
    first, middle, third = witness
    opposite = "down" if terminal.direction == "up" else "up"
    if (
        first.direction != opposite
        or middle.direction != terminal.direction
        or third.direction != opposite
    ):
        return None
    if max(first.low_tick, third.low_tick) >= min(
        first.high_tick,
        third.high_tick,
    ):
        return None
    extends = (
        middle.high_tick > terminal.high_tick
        if terminal.direction == "up"
        else middle.low_tick < terminal.low_tick
    )
    return None if extends else witness


def _single_unit_reversal_witness(source_units, index, signal):
    """Return the locked three-unit reversal that confirms a one-unit exit.

    A one-unit consolidation departure only records the first directional
    extreme. It is not a stable movement boundary until the following
    ``opposite -> same -> opposite`` sequence overlaps on its two outside
    units and the middle unit fails to extend that extreme. A three-unit
    departure already contains this second-test shape and needs no extra
    witness.
    """

    signal_index = index.get(signal.unit_id)
    if signal_index is None:
        raise ValueError("single-unit departure signal is missing from source units")
    return _reversal_witness_after(
        source_units,
        signal_index,
        locked_only=True,
    )


def _geometric_movement_slices(source_units, start_index):
    """Partition an alternating segment suffix at confirmed reversal shapes."""

    values = tuple(source_units)
    if not 0 <= start_index < len(values):
        raise ValueError("geometric movement start is outside source units")
    output = []
    movement_start = start_index
    while movement_start + 5 < len(values):
        found = None
        for terminal_index in range(movement_start + 2, len(values) - 3, 2):
            if values[terminal_index].direction != values[movement_start].direction:
                raise ValueError("geometric movement requires alternating segments")
            start_tick = values[movement_start].start_tick
            end_tick = values[terminal_index].end_tick
            moves_in_segment_direction = (
                end_tick > start_tick
                if values[movement_start].direction == "up"
                else end_tick < start_tick
            )
            if not moves_in_segment_direction:
                continue
            witness = _reversal_witness_after(
                values,
                terminal_index,
                locked_only=False,
            )
            if witness is not None:
                found = (
                    values[movement_start : terminal_index + 1],
                    witness,
                )
                break
        if found is None:
            break
        output.append(found)
        movement_start += len(found[0])
    return tuple(output)


def partition_pending_movements(
    current_trends,
    units,
    structural_level: int,
) -> tuple[PendingMovementPartition, ...]:
    """把未被当前正式走势拥有的单元切成连续、独占的待定分区。

    正式走势始终优先拥有其 ``constituent_units``。待定分区只取得剩余单元；
    相邻正式走势通过 trend/unit 引用表达边界，不复用边界单元。
    """

    trends = tuple(current_trends)
    values = tuple(units)
    if type(structural_level) is not int or structural_level < 0:
        raise ValueError("pending movement structural_level must be non-negative")
    if not values:
        if trends:
            raise ValueError("formal trends require source units")
        return ()
    if len({item.unit_id for item in values}) != len(values):
        raise ValueError("pending movement source unit ids must be unique")
    source_kind = values[0].source_kind
    price_basis_revision = values[0].price_basis_revision
    if any(
        item.structural_level != structural_level
        or item.source_kind is not source_kind
        or item.price_basis_revision != price_basis_revision
        for item in values
    ):
        raise ValueError("pending movement source unit context mismatch")
    for previous, current in zip(values, values[1:]):
        if previous.end_tick != current.start_tick:
            raise ValueError("pending movement source units must connect")
        if current.market_start < previous.market_end:
            raise ValueError("pending movement source intervals must not overlap")

    positions = {item.unit_id: offset for offset, item in enumerate(values)}
    owners: list[str | None] = [None] * len(values)
    trend_by_id = {}
    for trend in trends:
        previous = trend_by_id.setdefault(trend.trend_id, trend)
        if previous != trend:
            raise ValueError("current trend id maps to conflicting evidence")
        if (
            trend.structural_level != structural_level
            or trend.price_basis_revision != price_basis_revision
            or trend.constituent_units[0].source_kind is not source_kind
        ):
            raise ValueError("pending movement formal trend context mismatch")
        try:
            offsets = tuple(positions[item.unit_id] for item in trend.constituent_units)
        except KeyError as exc:
            raise ValueError("formal trend unit is absent from source stream") from exc
        if offsets != tuple(range(offsets[0], offsets[0] + len(offsets))):
            raise ValueError("formal trend must own one contiguous source slice")
        if tuple(values[offset] for offset in offsets) != trend.constituent_units:
            raise ValueError("formal trend source evidence changed")
        for offset in offsets:
            if owners[offset] is not None:
                raise ValueError("current formal trends cannot share source units")
            owners[offset] = trend.trend_id

    output = []
    start = 0
    while start < len(values):
        if owners[start] is not None:
            start += 1
            continue
        end = start + 1
        while end < len(values) and owners[end] is None:
            end += 1
        constituent_units = values[start:end]
        left_trend_id = owners[start - 1] if start > 0 else None
        right_trend_id = owners[end] if end < len(values) else None
        left_boundary_unit_id = (
            values[start - 1].unit_id if left_trend_id is not None else None
        )
        right_boundary_unit_id = (
            values[end].unit_id if right_trend_id is not None else None
        )
        role = (
            PendingMovementRole.ENTIRE_STREAM
            if left_trend_id is None and right_trend_id is None
            else PendingMovementRole.PREFIX
            if left_trend_id is None
            else PendingMovementRole.SUFFIX
            if right_trend_id is None
            else PendingMovementRole.BRIDGE
        )
        adjacent_trends = tuple(
            trend_by_id[item]
            for item in (left_trend_id, right_trend_id)
            if item is not None
        )
        availability = tuple(item.available_at for item in constituent_units) + tuple(
            trend.available_at for trend in adjacent_trends
        )
        available_at = max(availability)
        direction = _direction(constituent_units)
        output.append(
            PendingMovementPartition(
                partition_id=(
                    "sha256:"
                    + stable_structure_id(
                        "chanlun-pending-movement",
                        price_basis_revision,
                        structural_level,
                        source_kind.value,
                        role.value,
                        tuple(item.unit_id for item in constituent_units),
                        left_trend_id,
                        right_trend_id,
                        left_boundary_unit_id,
                        right_boundary_unit_id,
                    )
                ),
                structural_level=structural_level,
                source_kind=source_kind,
                price_basis_revision=price_basis_revision,
                role=role,
                direction=direction,
                constituent_units=constituent_units,
                left_trend_id=left_trend_id,
                right_trend_id=right_trend_id,
                left_boundary_unit_id=left_boundary_unit_id,
                right_boundary_unit_id=right_boundary_unit_id,
                available_at=available_at,
            )
        )
        start = end

    formal_ids = {item.unit_id for trend in trends for item in trend.constituent_units}
    pending_ids = {
        item.unit_id for partition in output for item in partition.constituent_units
    }
    if formal_ids & pending_ids or formal_ids | pending_ids != set(positions):
        raise ValueError("formal and pending movement ownership must cover units once")
    return tuple(output)


def _completion_times(group, constituent_units):
    confirmations = tuple(center.structural_closed_at for center in group)
    if any(value is None for value in confirmations):
        raise ValueError("complete group requires completion confirmations")
    confirmed_at = max(confirmations)
    available_at = max(
        tuple(center.available_at for center in group)
        + tuple(item.available_at for item in constituent_units)
    )
    return confirmed_at, available_at


def _next_group_start(
    terminal_center,
    successor_center,
    unit_index,
):
    """Return the first source unit owned by the successor trend group."""

    terminal_return = terminal_center.completion_return_unit
    if terminal_return is not None:
        return unit_index[terminal_return.unit_id]
    if (
        terminal_center.state is CenterState.SUPERSEDED
        and terminal_center.superseded_by_center_id == successor_center.center_id
    ):
        # The old core's third unit may be an optional comparison entry for
        # the successor, but it must never be reused as a fabricated leave or
        # as the successor trend's first constituent unit.
        return unit_index[successor_center.body_units[0].unit_id]
    raise ValueError("closed trend requires a causal successor boundary")


def _build(
    group,
    constituent_units,
    structural_level,
    state,
    confirmed_at,
    available_at,
    terminal_divergence=None,
    completion_witness_units=(),
):
    all_units = tuple(constituent_units)
    start = all_units[0]
    tail = all_units[-1]
    direction = _direction(all_units)
    kind = _classify_trend_kind(group)
    return TrendType(
        trend_id=build_trend_id(
            price_basis_revision=start.price_basis_revision,
            structural_level=structural_level,
            center_ids=tuple(center.center_id for center in group),
            constituent_unit_ids=tuple(item.unit_id for item in all_units),
            direction=direction,
            terminal_divergence_id=(
                None
                if terminal_divergence is None
                else terminal_divergence.divergence_id
            ),
            completion_witness_unit_ids=tuple(
                item.unit_id for item in completion_witness_units
            ),
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
        completion_witness_units=tuple(completion_witness_units),
    )


def _merge_trend_group(trends) -> TrendType:
    """Combine a contiguous same-level group into one canonical movement."""

    values = tuple(trends)
    if not values:
        raise ValueError("cannot merge an empty trend group")
    if len(values) == 1:
        return values[0]
    first = values[0]
    last = values[-1]
    for previous, current in zip(values, values[1:]):
        if (
            previous.structural_level != current.structural_level
            or previous.price_basis_revision != current.price_basis_revision
        ):
            raise ValueError("merged trends must share one structural context")
        if (
            previous.end_tick != current.start_tick
            or current.market_start < previous.market_end
        ):
            raise ValueError("merged trends must form one connected chain")

    constituent_units = tuple(
        unit for trend in values for unit in trend.constituent_units
    )
    if len({unit.unit_id for unit in constituent_units}) != len(constituent_units):
        raise ValueError("merged trends cannot reuse constituent units")
    centers = tuple(center for trend in values for center in trend.centers)
    if len({center.center_id for center in centers}) != len(centers):
        raise ValueError("merged trends cannot reuse centers")
    merged_kind = _classify_trend_kind(centers)
    if (
        last.terminal_divergence is not None
        and last.terminal_divergence.kind != merged_kind.value
    ):
        raise IncompatibleDecompositionBoundaryError(
            "terminal divergence kind does not match merged movement",
            last.terminal_divergence.signal_unit_id,
        )

    confirmed_at = None
    if last.state is not TrendState.FORMING:
        confirmations = tuple(
            trend.confirmed_at for trend in values if trend.confirmed_at is not None
        )
        if not confirmations:
            raise ValueError("completed merged trend requires confirmation")
        confirmed_at = max(confirmations)
    try:
        return _build(
            centers,
            constituent_units,
            first.structural_level,
            last.state,
            confirmed_at,
            max(trend.available_at for trend in values),
            terminal_divergence=last.terminal_divergence,
            completion_witness_units=last.completion_witness_units,
        )
    except ValueError as exc:
        if last.terminal_divergence is None:
            raise
        # The divergence was valid for the local raw group, but a soft
        # same-direction boundary has now extended that group to the left.
        # Direction, kind and whole-movement extreme requirements must all be
        # re-proven for the canonical movement.  If any of those immutable
        # evidence contracts fail, reject this candidate boundary and replay
        # without it; it cannot split the alternating movement ledger.
        raise IncompatibleDecompositionBoundaryError(
            "terminal divergence is incompatible with merged movement",
            last.terminal_divergence.signal_unit_id,
        ) from exc


def _rebind_decomposition_boundary(
    boundary: DecompositionBoundaryEvidence,
    trend: TrendType,
) -> DecompositionBoundaryEvidence:
    """Bind terminal divergence evidence to a left-extended movement."""

    divergence = boundary.divergence
    if (
        trend.state is not TrendState.LOCKED
        or trend.terminal_divergence != divergence
        or trend.centers[-1].center_id != boundary.terminal_center_id
        or trend.terminal_unit.unit_id != boundary.anchor_unit_id
        or trend.market_end != boundary.anchor_at
    ):
        raise IncompatibleDecompositionBoundaryError(
            "decomposition boundary cannot bind to merged trend",
            boundary.anchor_unit_id,
        )
    return replace(
        boundary,
        boundary_id=stable_structure_id(
            "chanlun-decomposition-boundary",
            divergence.price_basis_revision,
            "same_level",
            boundary.boundary_kind,
            divergence.structural_level,
            divergence.source_kind.value,
            trend.trend_id,
            boundary.terminal_center_id,
            divergence.divergence_id,
        ),
        left_trend_id=trend.trend_id,
    )


def normalize_trend_assembly(
    current_trends,
    completed_trends,
    decomposition_boundaries,
    source_units,
    structural_level: int,
) -> TrendAssemblyResult:
    """Publish only a connected up/down/up/down same-level movement chain.

    Soft boundaries between equal endpoint directions are combined.  A locked
    divergence boundary is immutable: units to its right remain pending until
    their combined endpoint direction is the required opposite direction.
    """

    raw_trends = tuple(current_trends)
    raw_completed = tuple(completed_trends)
    raw_boundaries = tuple(decomposition_boundaries)
    boundary_by_trend = {
        boundary.left_trend_id: boundary for boundary in raw_boundaries
    }
    if len(boundary_by_trend) != len(raw_boundaries):
        raise ValueError("a trend cannot own multiple decomposition boundaries")

    # Each item is [raw trend group, canonical trend, rebound boundary].
    emitted: list[list[object]] = []
    pending_group: list[TrendType] = []

    def group_boundary(group: tuple[TrendType, ...]):
        internal = tuple(
            trend.trend_id
            for trend in group[:-1]
            if trend.trend_id in boundary_by_trend
        )
        if internal:
            boundary = boundary_by_trend[internal[0]]
            raise IncompatibleDecompositionBoundaryError(
                "decomposition boundary cannot be hidden inside a movement",
                boundary.anchor_unit_id,
            )
        return boundary_by_trend.get(group[-1].trend_id)

    def emit(group_values: list[TrendType], trend: TrendType) -> None:
        group = tuple(group_values)
        boundary = group_boundary(group)
        rebound = (
            None
            if boundary is None
            else _rebind_decomposition_boundary(boundary, trend)
        )
        emitted.append([group, trend, rebound])

    for raw_trend in raw_trends:
        pending_group.append(raw_trend)
        while pending_group:
            previous = None if not emitted else emitted[-1][1]
            if previous is not None and not isinstance(previous, TrendType):
                raise TypeError("invalid canonical trend accumulator")
            previous_boundary = None if not emitted else emitted[-1][2]
            pending_direction = _direction(
                tuple(
                    unit for trend in pending_group for unit in trend.constituent_units
                )
            )
            if (
                previous is not None
                and previous_boundary is not None
                and pending_direction == previous.direction
            ):
                if any(trend.trend_id in boundary_by_trend for trend in pending_group):
                    boundary = next(
                        boundary_by_trend[trend.trend_id]
                        for trend in pending_group
                        if trend.trend_id in boundary_by_trend
                    )
                    raise IncompatibleDecompositionBoundaryError(
                        "same-direction successor cannot close a decomposition boundary",
                        boundary.anchor_unit_id,
                    )
                # The suffix has not reversed the immutable boundary movement
                # yet.  Do not try to promote its local completion witness to
                # whole-movement evidence; keep every unit pending and retry as
                # later raw movements extend the endpoint.
                break

            candidate = _merge_trend_group(tuple(pending_group))
            if previous is None:
                emit(pending_group, candidate)
                pending_group = []
                break

            if candidate.direction != previous.direction:
                emit(pending_group, candidate)
                pending_group = []
                break

            if previous_boundary is not None:
                if any(trend.trend_id in boundary_by_trend for trend in pending_group):
                    boundary = next(
                        boundary_by_trend[trend.trend_id]
                        for trend in pending_group
                        if trend.trend_id in boundary_by_trend
                    )
                    raise IncompatibleDecompositionBoundaryError(
                        "same-direction successor cannot close a decomposition boundary",
                        boundary.anchor_unit_id,
                    )
                # Do not manufacture another same-direction movement after an
                # immutable divergence boundary.  The accumulated suffix stays
                # unresolved and will be exposed by partition_pending_movements.
                break

            previous_group = emitted.pop()[0]
            if not isinstance(previous_group, tuple):
                raise TypeError("invalid canonical trend group")
            pending_group = [*previous_group, *pending_group]

    if pending_group and any(
        trend.trend_id in boundary_by_trend for trend in pending_group
    ):
        boundary = next(
            boundary_by_trend[trend.trend_id]
            for trend in pending_group
            if trend.trend_id in boundary_by_trend
        )
        raise IncompatibleDecompositionBoundaryError(
            "unresolved same-direction suffix cannot own a decomposition boundary",
            boundary.anchor_unit_id,
        )

    canonical_trends = tuple(item[1] for item in emitted)
    canonical_boundaries = tuple(
        sorted(
            (item[2] for item in emitted if item[2] is not None),
            key=lambda boundary: (boundary.available_at, boundary.boundary_id),
        )
    )

    # Completed snapshots are causal prefixes.  Retain only snapshots that are
    # prefixes of a currently valid canonical movement, then ensure every
    # complete canonical movement has one immutable COMPLETE snapshot.
    canonical_unit_ids = tuple(
        tuple(unit.unit_id for unit in trend.constituent_units)
        for trend in canonical_trends
    )

    def compatible_snapshot(snapshot: TrendType) -> bool:
        snapshot_ids = tuple(unit.unit_id for unit in snapshot.constituent_units)
        return any(
            snapshot.direction == trend.direction
            and len(snapshot_ids) <= len(unit_ids)
            and unit_ids[: len(snapshot_ids)] == snapshot_ids
            for trend, unit_ids in zip(canonical_trends, canonical_unit_ids)
        )

    canonical_completed = {
        trend.trend_id: trend for trend in raw_completed if compatible_snapshot(trend)
    }
    for trend in canonical_trends:
        if not trend.complete or trend.trend_id in canonical_completed:
            continue
        canonical_completed[trend.trend_id] = replace(
            trend,
            state=TrendState.COMPLETE,
        )

    return TrendAssemblyResult(
        current_trends=canonical_trends,
        completed_trends=tuple(
            sorted(
                canonical_completed.values(),
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        ),
        decomposition_boundaries=canonical_boundaries,
        pending_movements=partition_pending_movements(
            canonical_trends,
            tuple(source_units),
            structural_level,
        ),
    )


def _refine_forming_segment_tail(
    output,
    completed,
    source_units,
    index,
    structural_level,
):
    """Separate resolved centerless movements before the active tail center.

    A late center must not absorb several already-confirmed alternating
    movements merely because no earlier formal five-role center existed.
    Every geometrically completed lead-in movement that ends immediately
    before the center is an independent same-level movement type.
    """

    if (
        not output
        or not source_units
        or source_units[0].source_kind is not SourceKind.SEGMENT
    ):
        return
    tail = output[-1]
    if (
        tail.state is not TrendState.FORMING
        or tail.terminal_divergence is not None
        or len(tail.centers) != 1
    ):
        return
    center = tail.centers[0]
    center_start_unit = (
        center.body_units[0] if center.entry_unit is None else center.entry_unit
    )
    movement_start = index[tail.constituent_units[0].unit_id]
    center_start = index[center_start_unit.unit_id]
    if center_start - movement_start < 3:
        return

    slices = _geometric_movement_slices(source_units, movement_start)
    prefix_slices = tuple(
        item for item in slices if index[item[0][-1].unit_id] < center_start
    )
    if (
        not prefix_slices
        or index[prefix_slices[-1][0][-1].unit_id] != center_start - 1
        or any(
            not unit.locked
            for movement, witness in prefix_slices
            for unit in (*movement, *witness)
        )
    ):
        return

    refined = []
    for movement, witness in prefix_slices:
        confirmations = tuple(item.confirmed_at for item in (*movement, *witness))
        if any(value is None for value in confirmations):
            raise ValueError("locked geometric movement requires confirmations")
        confirmed_at = max(confirmations)
        available_at = max(item.available_at for item in (*movement, *witness))
        complete = _build(
            (),
            movement,
            structural_level,
            TrendState.COMPLETE,
            confirmed_at,
            available_at,
            completion_witness_units=witness,
        )
        locked = _build(
            (),
            movement,
            structural_level,
            TrendState.LOCKED,
            confirmed_at,
            available_at,
            completion_witness_units=witness,
        )
        previous = completed.setdefault(complete.trend_id, complete)
        if previous != complete:
            raise ValueError("geometric movement identity collision")
        refined.append(locked)

    # The first locked geometric movement is itself a causal successor
    # boundary.  Any earlier COMPLETE trend in the current partition must be
    # locked at that boundary as well; otherwise a later LOCKED trend would
    # jump over an unlocked predecessor and the recursive stream would no
    # longer be one continuous same-level chain.
    first_locked = refined[0]
    if first_locked.confirmed_at is None:
        raise ValueError("locked geometric movement requires confirmation")
    for offset, trend in enumerate(output[:-1]):
        if trend.state is not TrendState.COMPLETE:
            continue
        if trend.confirmed_at is None:
            raise ValueError("completed trend requires confirmation")
        output[offset] = replace(
            trend,
            state=TrendState.LOCKED,
            confirmed_at=max(trend.confirmed_at, first_locked.confirmed_at),
            available_at=max(trend.available_at, first_locked.available_at),
        )

    original_end = index[tail.constituent_units[-1].unit_id]
    center_slice = next(
        (
            item
            for item in slices
            if index[item[0][0].unit_id] == center_start
            and index[item[0][-1].unit_id] >= original_end
        ),
        None,
    )
    if center_slice is None:
        center_units = source_units[center_start : original_end + 1]
        center_witness = ()
    else:
        center_units, center_witness = center_slice
    available_at = max(
        center.available_at,
        *(item.available_at for item in (*center_units, *center_witness)),
    )
    refined.append(
        _build(
            (center,),
            center_units,
            structural_level,
            TrendState.FORMING,
            None,
            available_at,
            completion_witness_units=center_witness,
        )
    )
    output[-1:] = refined


def _confirmed_divergence_boundary(
    group,
    source_units,
    index,
    group_start,
    structural_level,
    strength,
):
    """返回趋势或盘整走势中已经确认的同宽背驰边界。"""

    if strength is None or not group:
        return None
    try:
        group_kind = _classify_trend_kind(group)
        if group_kind is TrendKind.TREND:
            compared = compare_terminal_trend_divergence(
                group,
                source_units,
                strength,
                trend_start_unit_id=source_units[group_start].unit_id,
            )
        else:
            divergence = compare_center_consolidation_divergence(
                group[-1],
                source_units,
                strength,
                movement_start_unit_id=source_units[group_start].unit_id,
            )
            if divergence is None:
                compared = None
            else:
                units_by_id = {item.unit_id: item for item in source_units}
                signal = units_by_id.get(divergence.signal_unit_id)
                if signal is None:
                    raise ValueError("盘整背驰的离开段末端不在同级别单元序列中")
                compared = (divergence, signal)
    except (FormalDivergenceUnavailable, MacdStrengthUnavailable, KeyError):
        # 稀疏回放或测试强度表可能只保存目标趋势的 MACD 切片；缺失的单中枢
        # 切片与仍含未锁定比较腿的实时前缀，都只能表示正式背驰尚不可用。
        return None
    if compared is None:
        return None
    divergence, signal = compared
    if not divergence.is_divergent:
        return None
    if len(group) == 1 and divergence.comparison_width == 1:
        reversal_witness = _single_unit_reversal_witness(
            source_units,
            index,
            signal,
        )
        if reversal_witness is None:
            return None
        witness_confirmations = tuple(item.confirmed_at for item in reversal_witness)
        if any(value is None for value in witness_confirmations):
            raise ValueError("locked reversal witness requires confirmations")
        divergence = replace(
            divergence,
            confirmed_at=max(divergence.confirmed_at, *witness_confirmations),
            available_at=max(
                divergence.available_at,
                *(item.available_at for item in reversal_witness),
            ),
        )
    # 一个较窄的中枢可能只在后续回返单元到来后，才取代此前更宽的进行中中枢。
    # 若再使用更早离开单元上的单段背驰关闭它，就会把“后来才知道的中枢几何”
    # 倒写到过去，且在硬边界分区中无法因果重放。此类比较只是不成立，不是整只
    # 标的的结构错误；正式边界必须不早于其终端中枢自身的可用时点。
    if divergence.available_at < group[-1].available_at:
        return None
    end_index = index.get(signal.unit_id)
    if end_index is None:
        raise ValueError("divergence signal references a missing source unit")
    closed_terminal = close_center_at_divergence(group[-1], divergence)
    closed_group = (*group[:-1], closed_terminal)
    constituent_units = _constituent_units(
        closed_group,
        source_units,
        index,
        group_start,
        end_index=end_index,
    )
    if not _group_is_divergence_complete(
        closed_group,
        constituent_units,
        divergence,
    ):
        return None
    if _direction(constituent_units) != divergence.direction:
        return None
    prior_completions = tuple(center.completed_at for center in closed_group[:-1])
    if any(value is None for value in prior_completions):
        # A superseded prior center is structurally closed but deliberately has
        # no fabricated third-class completion.  That is a valid live prefix,
        # not corrupt evidence; it simply cannot confirm a formal divergence
        # trend at this boundary yet.
        return None
    confirmed_at = max(
        *prior_completions,
        signal.confirmed_at,
        divergence.confirmed_at,
    )
    available_at = max(
        divergence.available_at,
        *(center.available_at for center in closed_group),
        *(item.available_at for item in constituent_units),
    )
    complete = _build(
        closed_group,
        constituent_units,
        structural_level,
        TrendState.COMPLETE,
        confirmed_at,
        available_at,
        terminal_divergence=divergence,
    )
    locked = _build(
        closed_group,
        constituent_units,
        structural_level,
        TrendState.LOCKED,
        confirmed_at,
        available_at,
        terminal_divergence=divergence,
    )
    boundary_kind = f"{divergence.kind}_divergence"
    boundary = DecompositionBoundaryEvidence(
        boundary_id=stable_structure_id(
            "chanlun-decomposition-boundary",
            divergence.price_basis_revision,
            "same_level",
            boundary_kind,
            divergence.structural_level,
            divergence.source_kind.value,
            locked.trend_id,
            closed_terminal.center_id,
            divergence.divergence_id,
        ),
        decomposition_mode="same_level",
        boundary_kind=boundary_kind,
        structural_level=divergence.structural_level,
        source_kind=divergence.source_kind,
        price_basis_revision=divergence.price_basis_revision,
        left_trend_id=locked.trend_id,
        terminal_center_id=closed_terminal.center_id,
        anchor_unit_id=signal.unit_id,
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
        return TrendAssemblyResult(
            current_trends=(),
            completed_trends=(),
            pending_movements=partition_pending_movements(
                (),
                source_units,
                structural_level,
            ),
        )
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
        0 if group_start_unit_id is None else index.get(group_start_unit_id, -1)
    )
    first_center_start = index[values[0].body_units[0].unit_id]
    if values[0].entry_unit is not None:
        first_center_start = index[values[0].entry_unit.unit_id]
    if not 0 <= group_start <= first_center_start:
        raise ValueError("trend group start must precede its first center")
    group_relation = None
    active_divergence_end = None

    def lock_completed_output(confirmed_at, available_at) -> None:
        """后续背驰边界成立时，锁定此前仅完成但尚未冻结的走势。"""

        for offset, trend in enumerate(output):
            if trend.state is not TrendState.COMPLETE:
                continue
            if trend.confirmed_at is None:
                raise ValueError("已完成走势缺少确认时间")
            output[offset] = replace(
                trend,
                state=TrendState.LOCKED,
                confirmed_at=max(trend.confirmed_at, confirmed_at),
                available_at=max(trend.available_at, available_at),
            )

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
                lock_completed_output(
                    boundary.confirmed_at,
                    boundary.available_at,
                )
                output.append(locked)
                previous_boundary = boundaries.setdefault(
                    boundary.boundary_id, boundary
                )
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
                # 该原始中枢窗口跨越了此前前缀已确认的边界。它不能撤销或跨越
                # 这个不可变同级别切分，应等待本体完全从边界右侧开始的首个中枢。
                continue
            group = [current]
            group_relation = None
            active_divergence_end = None
            record_complete(group, group_start)
            continue

        relation = classify_center_relation(group[-1], current)
        continues = relation in (
            CenterRelation.UP_TREND,
            CenterRelation.DOWN_TREND,
        ) and (group_relation is None or relation is group_relation)
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
        if not current.structurally_closed:
            # 实时中枢仍可能被后续生命周期推进。它可以开启形成中边界，但在自身
            # 中枢身份完成之前，不能不可逆地锁定前一段走势。
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
            # 前一中枢的完成回返是下一来源特定成立窗口最早可拥有的单元；这样既
            # 保持同级走势单元相邻，也不会重复使用上一离开段。
            group_start = _next_group_start(group[-1], current, index)
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
        group_start = _next_group_start(group[-1], current, index)
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
        lock_completed_output(
            boundary.confirmed_at,
            boundary.available_at,
        )
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
    _refine_forming_segment_tail(
        output,
        completed,
        source_units,
        index,
        structural_level,
    )
    return normalize_trend_assembly(
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
        source_units=source_units,
        structural_level=structural_level,
    )
