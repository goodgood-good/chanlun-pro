from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import (
    CenterEvent,
    CenterEventKind,
    CenterLevelResult,
    CenterPreview,
    CenterPreviewState,
    CenterRelation,
    CenterState,
    ConstituentUnit,
    SourceKind,
    TrendCenter,
)


_GEOMETRY_STOP_ERRORS = frozenset(
    {
        "ongoing center unit must re-enter the core",
        "return geometry is neither extension nor third-class completion",
    }
)


def _alternates(values: tuple[ConstituentUnit, ...]) -> bool:
    return all(
        previous.direction != current.direction
        for previous, current in zip(values, values[1:])
    )


def _positive_overlap(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    return max(item.low_tick, zd_tick) < min(item.high_tick, zg_tick)


def _outside_in_direction(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
) -> bool:
    return (
        item.end_tick > zg_tick
        if item.direction == "up"
        else item.end_tick < zd_tick
    )


def _core(initial_units: tuple[ConstituentUnit, ...]) -> tuple[int, int]:
    middle = initial_units[1:4]
    return (
        max(item.low_tick for item in middle),
        min(item.high_tick for item in middle),
    )


def _event(
    center: TrendCenter,
    kind: CenterEventKind,
    market_time: datetime,
    available_at: datetime,
    leave: ConstituentUnit | None = None,
    ret: ConstituentUnit | None = None,
) -> CenterEvent:
    return CenterEvent(
        event_id=stable_structure_id(
            "chanlun-center-event/v3",
            center.price_basis_revision,
            center.structural_level,
            center.source_kind.value,
            center.center_id,
            kind.value,
            None if leave is None else leave.unit_id,
            None if ret is None else ret.unit_id,
        ),
        kind=kind,
        center_id=center.center_id,
        price_basis_revision=center.price_basis_revision,
        market_time=market_time,
        available_at=available_at,
        leave_unit_id=None if leave is None else leave.unit_id,
        return_unit_id=None if ret is None else ret.unit_id,
    )


def _validate_seed_context(
    values: tuple[ConstituentUnit, ...],
    structural_level: int,
    source_kind: SourceKind,
) -> str:
    if not values:
        raise ValueError("center candidate cannot be empty")
    source_kind = SourceKind(source_kind)
    if any(
        item.structural_level != structural_level or item.source_kind is not source_kind
        for item in values
    ):
        raise ValueError("seed level/source mismatch")
    bases = {item.price_basis_revision for item in values}
    if len(bases) != 1:
        raise ValueError("seed price basis mismatch")
    if len({item.unit_id for item in values}) != len(values):
        raise ValueError("seed unit ids must be unique")
    for previous, current in zip(values, values[1:]):
        if previous.end_tick != current.start_tick:
            raise ValueError("seed prices must connect")
        if current.market_start < previous.market_end:
            raise ValueError("seed intervals must not overlap")
    return next(iter(bases))


def _new_ongoing_center(
    initial_units: tuple[
        ConstituentUnit,
        ConstituentUnit,
        ConstituentUnit,
        ConstituentUnit,
        ConstituentUnit,
    ],
    structural_level: int,
    source_kind: SourceKind,
    price_basis_revision: str,
    zd_tick: int,
    zg_tick: int,
) -> TrendCenter:
    center_id = stable_structure_id(
        "chanlun-center/v3",
        price_basis_revision,
        structural_level,
        source_kind.value,
        tuple(item.unit_id for item in initial_units),
        zd_tick,
        zg_tick,
    )
    pending_leave = (
        initial_units[4]
        if _outside_in_direction(initial_units[4], zd_tick, zg_tick)
        else None
    )
    return TrendCenter(
        center_id=center_id,
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        state=CenterState.ONGOING,
        initial_units=initial_units,
        body_units=initial_units,
        extension_units=(),
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        dd_tick=min(item.low_tick for item in initial_units),
        gg_tick=max(item.high_tick for item in initial_units),
        body_start_market_time=initial_units[0].market_start,
        established_market_time=initial_units[4].market_end,
        established_at=initial_units[4].confirmed_at,
        last_touch_market_time=initial_units[4].market_end,
        pending_leave_unit=pending_leave,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(item.available_at for item in initial_units),
        body_revision=0,
    )


def establish_center(
    initial_units,
    structural_level: int,
    source_kind: SourceKind,
) -> TrendCenter | None:
    values = tuple(initial_units)
    if len(values) != 5 or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    if any(not item.locked for item in values):
        return None
    zd_tick, zg_tick = _core(values)
    if zd_tick >= zg_tick:
        return None
    if not _positive_overlap(values[0], zd_tick, zg_tick):
        return None
    if not _positive_overlap(values[4], zd_tick, zg_tick):
        return None
    return _new_ongoing_center(
        values,
        structural_level,
        SourceKind(source_kind),
        price_basis_revision,
        zd_tick,
        zg_tick,
    )


def establish_center_preview(
    initial_units,
    structural_level: int,
    source_kind: SourceKind,
) -> CenterPreview | None:
    """Build a non-formal five-unit candidate containing provisional units.

    Segment calculation can expose more than one unlocked unit at the live
    edge.  Such a candidate is display evidence only, but every provisional
    unit may participate in the five-unit overlap test.
    """

    values = tuple(initial_units)
    if len(values) != 5 or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    unlocked_seen = False
    for item in values:
        if not item.locked:
            unlocked_seen = True
        elif unlocked_seen:
            return None
    if not unlocked_seen:
        return None
    zd_tick, zg_tick = _core(values)
    if zd_tick >= zg_tick:
        return None
    if not _positive_overlap(values[0], zd_tick, zg_tick):
        return None
    if not _positive_overlap(values[4], zd_tick, zg_tick):
        return None
    return CenterPreview(
        structural_level=structural_level,
        source_kind=SourceKind(source_kind),
        price_basis_revision=price_basis_revision,
        unit_ids=tuple(item.unit_id for item in values),
        state=CenterPreviewState.FORMING,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in values),
    )


def _establish_trend_linked_center_preview(
    initial_units,
    previous_center: TrendCenter,
    structural_level: int,
    source_kind: SourceKind,
) -> CenterPreview | None:
    """Build a guarded live preview above/below an active prior center.

    This is the five-segment shape seen at the live edge of SH.000001/1m:
    the candidate reuses the active center's penultimate body unit and pending
    leave, then adds three provisional units.  Its middle-three core is
    separated in the pending-leave direction, while the reused entry unit is
    still on the old-center side and therefore cannot overlap the new core.

    The exception is deliberately preview-only.  Locked formal centers keep
    the ordinary five-unit overlap contract and the provisional shape may
    repaint as its line segments lock.
    """

    values = tuple(initial_units)
    if SourceKind(source_kind) is not SourceKind.SEGMENT:
        return None
    if len(values) != 5 or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    unlocked_seen = False
    for item in values:
        if not item.locked:
            unlocked_seen = True
        elif unlocked_seen:
            return None
    if not unlocked_seen:
        return None
    if (
        previous_center.state is not CenterState.ONGOING
        or previous_center.structural_level != structural_level
        or previous_center.source_kind is not SourceKind(source_kind)
        or previous_center.price_basis_revision != price_basis_revision
        or previous_center.pending_leave_unit is None
        or len(previous_center.body_units) < 2
    ):
        return None
    previous_tail_ids = tuple(
        item.unit_id for item in previous_center.body_units[-2:]
    )
    if previous_tail_ids != tuple(item.unit_id for item in values[:2]):
        return None
    if values[1].unit_id != previous_center.pending_leave_unit.unit_id:
        return None

    zd_tick, zg_tick = _core(values)
    if zd_tick >= zg_tick:
        return None
    if values[0].direction != values[4].direction:
        return None
    if _positive_overlap(values[0], zd_tick, zg_tick):
        return None
    if not _positive_overlap(values[4], zd_tick, zg_tick):
        return None

    pending_direction = previous_center.pending_leave_unit.direction
    if pending_direction == "up":
        if zd_tick <= previous_center.zg_tick:
            return None
    elif zg_tick >= previous_center.zd_tick:
        return None

    return CenterPreview(
        structural_level=structural_level,
        source_kind=SourceKind(source_kind),
        price_basis_revision=price_basis_revision,
        unit_ids=tuple(item.unit_id for item in values),
        state=CenterPreviewState.FORMING,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in values),
    )


def _advance_center_preview_lifecycle(
    preview: CenterPreview,
    initial_units,
    following_units,
) -> CenterPreview | None:
    """Advance provisional center geometry without promoting it to formal evidence.

    Unlocked segments may establish, extend and geometrically complete a center for
    display.  The result deliberately remains a non-tradable ``CenterPreview``;
    formal centers and confirmed third-class points still require locked units.
    """

    body = list(initial_units)
    if len(body) < 5 or tuple(item.unit_id for item in body) != preview.unit_ids:
        raise ValueError("preview lifecycle seed mismatch")
    if preview.state is not CenterPreviewState.FORMING:
        raise ValueError("only a forming preview can advance")
    if preview.zd_tick is None or preview.zg_tick is None:
        raise ValueError("preview lifecycle requires a positive core")

    entry = body[0]
    pending = (
        body[-1]
        if body[-1].direction == entry.direction
        and _outside_in_direction(body[-1], preview.zd_tick, preview.zg_tick)
        else None
    )
    available_at = max(item.available_at for item in body)

    for item in following_units:
        previous = body[-1]
        if (
            item.structural_level != preview.structural_level
            or item.source_kind is not preview.source_kind
            or item.price_basis_revision != preview.price_basis_revision
        ):
            raise ValueError("preview transition level/source/basis mismatch")
        if item.unit_id in {value.unit_id for value in body}:
            raise ValueError("preview transition unit already belongs to body")
        if (
            item.direction == previous.direction
            or item.start_tick != previous.end_tick
            or item.market_start < previous.market_end
        ):
            raise ValueError("preview transition must be connected and alternating")
        available_at = max(available_at, item.available_at)

        if pending is not None:
            if _positive_overlap(item, preview.zd_tick, preview.zg_tick):
                body.append(item)
                pending = (
                    item
                    if item.direction == entry.direction
                    and _outside_in_direction(item, preview.zd_tick, preview.zg_tick)
                    else None
                )
                continue
            completes_up = (
                pending.direction == "up"
                and item.direction == "down"
                and item.low_tick >= preview.zg_tick
            )
            completes_down = (
                pending.direction == "down"
                and item.direction == "up"
                and item.high_tick <= preview.zd_tick
            )
            if completes_up or completes_down:
                return replace(
                    preview,
                    unit_ids=tuple(value.unit_id for value in body),
                    state=CenterPreviewState.COMPLETED,
                    available_at=available_at,
                    completion_return_unit_id=item.unit_id,
                )
            return None

        if not _positive_overlap(item, preview.zd_tick, preview.zg_tick):
            return None
        body.append(item)
        pending = (
            item
            if item.direction == entry.direction
            and _outside_in_direction(item, preview.zd_tick, preview.zg_tick)
            else None
        )

    return replace(
        preview,
        unit_ids=tuple(value.unit_id for value in body),
        available_at=available_at,
    )


def _project_ongoing_center_preview(
    center: TrendCenter,
    following_units,
) -> CenterPreview | None:
    """Project an already-formal ongoing center through provisional units."""

    if center.state is not CenterState.ONGOING:
        return None
    following = tuple(following_units)
    if not following or all(item.locked for item in following):
        return None
    seed = center.initial_units
    preview = CenterPreview(
        structural_level=center.structural_level,
        source_kind=center.source_kind,
        price_basis_revision=center.price_basis_revision,
        unit_ids=tuple(item.unit_id for item in seed),
        state=CenterPreviewState.FORMING,
        zd_tick=center.zd_tick,
        zg_tick=center.zg_tick,
        available_at=max(item.available_at for item in seed),
    )
    return _advance_center_preview_lifecycle(
        preview,
        seed,
        center.extension_units + following,
    )


def _validate_transition_unit(center: TrendCenter, item: ConstituentUnit) -> None:
    if (
        item.structural_level != center.structural_level
        or item.source_kind is not center.source_kind
    ):
        raise ValueError("transition level/source mismatch")
    if item.price_basis_revision != center.price_basis_revision:
        raise ValueError("transition price basis mismatch")
    if item.unit_id in {value.unit_id for value in center.body_units}:
        raise ValueError("transition unit id already belongs to center")
    if (
        center.completion_return_unit is not None
        and item.unit_id == center.completion_return_unit.unit_id
    ):
        raise ValueError("transition unit id already belongs to center")
    previous = center.body_units[-1]
    if item.start_tick != previous.end_tick:
        raise ValueError("center transition must connect")
    if item.direction == previous.direction:
        raise ValueError("center transition must alternate")
    if item.market_start < previous.market_end:
        raise ValueError("center transition intervals must not overlap")


def _append_body_unit(
    center: TrendCenter,
    item: ConstituentUnit,
    *,
    pending_leave: ConstituentUnit | None,
) -> tuple[TrendCenter, CenterEvent]:
    extension_units = center.extension_units + (item,)
    body_units = center.initial_units + extension_units
    updated = replace(
        center,
        body_units=body_units,
        extension_units=extension_units,
        dd_tick=min(value.low_tick for value in body_units),
        gg_tick=max(value.high_tick for value in body_units),
        last_touch_market_time=item.market_end,
        pending_leave_unit=pending_leave,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(center.available_at, item.available_at),
        body_revision=len(extension_units),
    )
    if pending_leave is None:
        kind = CenterEventKind.EXTENDED
    elif pending_leave.direction == "up":
        kind = CenterEventKind.BREAKOUT_WATCH_UP
    else:
        kind = CenterEventKind.BREAKOUT_WATCH_DOWN
    return updated, _event(
        updated,
        kind,
        item.market_end,
        updated.available_at,
        leave=pending_leave,
    )


def _append_extension_return(
    center: TrendCenter,
    item: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    pending_leave = (
        item
        if item.direction == center.entry_unit.direction
        and _outside_in_direction(item, center.zd_tick, center.zg_tick)
        else None
    )
    return _append_body_unit(center, item, pending_leave=pending_leave)


def _complete_center(
    center: TrendCenter,
    leave: ConstituentUnit,
    ret: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    direction = leave.direction
    updated = replace(
        center,
        state=CenterState.COMPLETED,
        pending_leave_unit=None,
        completion_leave_unit=leave,
        completion_return_unit=ret,
        completed_at=ret.confirmed_at,
        available_at=max(center.available_at, ret.available_at),
    )
    kind = (
        CenterEventKind.COMPLETED_UP
        if direction == "up"
        else CenterEventKind.COMPLETED_DOWN
    )
    return updated, _event(
        updated,
        kind,
        ret.market_end,
        updated.available_at,
        leave=leave,
        ret=ret,
    )


def advance_center(
    center: TrendCenter,
    item: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    if center.state is CenterState.COMPLETED:
        raise ValueError("completed center cannot transition")
    if not item.locked:
        raise ValueError("formal center transition must be locked")
    _validate_transition_unit(center, item)

    pending = center.pending_leave_unit
    if pending is not None:
        if _positive_overlap(item, center.zd_tick, center.zg_tick):
            return _append_extension_return(center, item)
        if (
            pending.direction == "up"
            and item.direction == "down"
            and item.low_tick >= center.zg_tick
        ):
            return _complete_center(center, pending, item)
        if (
            pending.direction == "down"
            and item.direction == "up"
            and item.high_tick <= center.zd_tick
        ):
            return _complete_center(center, pending, item)
        raise ValueError(
            "return geometry is neither extension nor third-class completion"
        )

    if not _positive_overlap(item, center.zd_tick, center.zg_tick):
        raise ValueError("ongoing center unit must re-enter the core")
    pending_leave = (
        item
        if item.direction == center.entry_unit.direction
        and _outside_in_direction(item, center.zd_tick, center.zg_tick)
        else None
    )
    return _append_body_unit(center, item, pending_leave=pending_leave)


def forming_preview(
    candidate,
    structural_level: int,
    source_kind: SourceKind,
) -> CenterPreview | None:
    values = tuple(candidate)
    if not 1 <= len(values) <= 5 or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    zd_tick = None
    zg_tick = None
    state = CenterPreviewState.FORMING
    if len(values) >= 4:
        zd_tick, zg_tick = _core(values)
        if zd_tick > zg_tick:
            return None
        if zd_tick == zg_tick:
            state = CenterPreviewState.TOUCH_ONLY
    return CenterPreview(
        structural_level=structural_level,
        source_kind=SourceKind(source_kind),
        price_basis_revision=price_basis_revision,
        unit_ids=tuple(item.unit_id for item in values),
        state=state,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in values),
    )


def validate_unit_sequence(
    values: tuple[ConstituentUnit, ...],
    structural_level: int,
    source_kind: SourceKind,
) -> None:
    source_kind = SourceKind(source_kind)
    if len({item.unit_id for item in values}) != len(values):
        raise ValueError("unit ids must be unique")
    bases = {item.price_basis_revision for item in values}
    if len(bases) > 1:
        raise ValueError("unit price basis mismatch")

    preview_seen = False
    previous = None
    for item in values:
        if (
            item.structural_level != structural_level
            or item.source_kind is not source_kind
        ):
            raise ValueError("unit level/source mismatch")
        if not item.locked:
            preview_seen = True
        elif preview_seen:
            raise ValueError("locked units must form a prefix")
        if previous is not None:
            if item.direction == previous.direction:
                raise ValueError("unit directions must alternate")
            if item.start_tick != previous.end_tick:
                raise ValueError("adjacent unit prices must connect")
            if item.market_start < previous.market_end:
                raise ValueError("unit market intervals must not overlap")
        previous = item


def _has_later_trend_candidate(
    previous_center: TrendCenter,
    formal: tuple[ConstituentUnit, ...],
    current_start: int,
    current_body_end: int,
    structural_level: int,
    source_kind: SourceKind,
) -> bool:
    """Return whether a later overlapping window preserves the trend leg.

    A too-early seed can absorb the connecting rise/fall into its envelope and
    turn two separated centers into an artificial upgrade.  Only prefer a
    later seed when it lies inside the current candidate body and is itself a
    viable center with a separated up/down relation to the previous center.
    """

    last_start = min(current_body_end, len(formal) - 5)
    for start in range(current_start + 1, last_start + 1):
        candidate = establish_center(
            formal[start : start + 5],
            structural_level,
            source_kind,
        )
        if candidate is None:
            continue
        offset = start + 5
        geometry_stopped = False
        while offset < len(formal):
            try:
                candidate, _event_value = advance_center(
                    candidate,
                    formal[offset],
                )
            except ValueError as exc:
                if str(exc) not in _GEOMETRY_STOP_ERRORS:
                    raise
                geometry_stopped = True
                break
            if candidate.state is CenterState.COMPLETED:
                break
            offset += 1
        if geometry_stopped:
            continue
        try:
            relation = classify_center_relation(previous_center, candidate)
        except ValueError:
            continue
        if relation in (CenterRelation.UP_TREND, CenterRelation.DOWN_TREND):
            return True
    return False


def _find_later_completed_candidate(
    formal: tuple[ConstituentUnit, ...],
    current_start: int,
    structural_level: int,
    source_kind: SourceKind,
    *,
    last_completion_offset: int | None = None,
) -> tuple[int, int, TrendCenter, list[CenterEvent]] | None:
    """Find the first later seed that is already complete in this prefix.

    A broad early seed can remain geometrically ongoing while a narrower
    five-unit consolidation inside its tail has already produced a same-side
    leave and an outside first return.  Waiting for a still later unit to
    invalidate the broad seed makes that completed center appear
    retroactively.  Completion is stronger evidence than an overlapping
    ongoing seed, so select the first later completed candidate immediately.
    """

    last_offset = (
        len(formal) - 1
        if last_completion_offset is None
        else min(last_completion_offset, len(formal) - 1)
    )
    earliest = None
    earliest_key = None
    for start in range(current_start + 1, last_offset - 4):
        # A five-unit seed needs at least one following return unit.  Once a
        # later start cannot beat the earliest completion already found, no
        # subsequent start can beat it either.
        if earliest_key is not None and start + 5 >= earliest_key[0]:
            break
        candidate = establish_center(
            formal[start : start + 5],
            structural_level,
            source_kind,
        )
        if candidate is None:
            continue
        candidate_events = [
            _event(
                candidate,
                CenterEventKind.ESTABLISHED,
                candidate.established_market_time,
                candidate.available_at,
                leave=candidate.initial_exit_unit,
            )
        ]
        offset = start + 5
        while offset <= last_offset:
            try:
                candidate, event = advance_center(candidate, formal[offset])
            except ValueError as exc:
                if str(exc) not in _GEOMETRY_STOP_ERRORS:
                    raise
                break
            candidate_events.append(event)
            if candidate.state is CenterState.COMPLETED:
                key = (offset, start)
                if earliest_key is None or key < earliest_key:
                    earliest_key = key
                    earliest = (
                        start,
                        offset,
                        candidate,
                        candidate_events,
                    )
                break
            offset += 1
    return earliest


def calculate_centers(
    units,
    structural_level: int,
    source_kind: SourceKind,
) -> CenterLevelResult:
    values = tuple(units)
    source_kind = SourceKind(source_kind)
    validate_unit_sequence(values, structural_level, source_kind)
    price_basis_revision = values[0].price_basis_revision if values else None

    locked_count = 0
    for item in values:
        if not item.locked:
            break
        locked_count += 1
    formal = values[:locked_count]

    centers: list[TrendCenter] = []
    events: list[CenterEvent] = []
    previews: list[CenterPreview] = []
    i = 0
    replay_from = 0
    while i + 4 < len(formal):
        center = establish_center(
            formal[i : i + 5],
            structural_level,
            source_kind,
        )
        if center is None:
            observation = forming_preview(
                formal[i : i + 5],
                structural_level,
                source_kind,
            )
            if (
                observation is not None
                and observation.state is CenterPreviewState.TOUCH_ONLY
                and observation not in previews
            ):
                previews.append(observation)
            i += 1
            continue

        candidate_events = [
            _event(
                center,
                CenterEventKind.ESTABLISHED,
                center.established_market_time,
                center.available_at,
                leave=center.initial_exit_unit,
            )
        ]
        j = i + 5
        geometry_stopped = False
        while j < len(formal):
            try:
                center, event = advance_center(center, formal[j])
            except ValueError as exc:
                if str(exc) not in _GEOMETRY_STOP_ERRORS:
                    raise
                geometry_stopped = True
                break
            candidate_events.append(event)
            if center.state is CenterState.COMPLETED:
                break
            j += 1
        completed_later = _find_later_completed_candidate(
            formal,
            i,
            structural_level,
            source_kind,
            last_completion_offset=(
                j
                if geometry_stopped or center.state is CenterState.COMPLETED
                else len(formal) - 1
            ),
        )
        if completed_later is not None and (
            center.state is not CenterState.COMPLETED
            or completed_later[1] < j
        ):
            i, j, center, candidate_events = completed_later
            geometry_stopped = False
        if geometry_stopped:
            # A candidate that can only leave opposite to its entry direction
            # is not retained as a historical ongoing center.  Slide one unit
            # and allow a later consolidation window to become the center.
            i += 1
            continue
        if centers:
            try:
                relation = classify_center_relation(centers[-1], center)
            except ValueError:
                relation = None
            if (
                relation is CenterRelation.UPGRADE
                and _has_later_trend_candidate(
                    centers[-1],
                    # A completed center is immutable once its completion
                    # return has arrived.  Later units must not retroactively
                    # introduce an alternative seed and make that historical
                    # center disappear from a live prefix.
                    formal[: j + 1],
                    i,
                    j - 1,
                    structural_level,
                    source_kind,
                )
            ):
                i += 1
                continue
        centers.append(center)
        events.extend(candidate_events)
        replay_from = i
        if center.state is CenterState.COMPLETED:
            # Adjacent centers may share the completed center's leaving unit:
            # it is simultaneously the next center's entering unit.  Start one
            # unit before the completion return, then slide to the return when
            # that shared-boundary candidate is not viable.
            i = max(i + 1, j - 1)
            continue
        break

    latest_live_preview = None
    if locked_count < len(values) and len(values) >= 5:
        # A valid five-unit window can end before the final provisional unit.
        # Scan every window that intersects the unlocked suffix and select the
        # latest consolidation candidate instead of assuming ``values[-5:]``.
        first_live_start = max(0, locked_count - 4)
        for start in range(first_live_start, len(values) - 4):
            preview = establish_center_preview(
                values[start : start + 5],
                structural_level,
                source_kind,
            )
            if preview is None and centers:
                preview = _establish_trend_linked_center_preview(
                    values[start : start + 5],
                    centers[-1],
                    structural_level,
                    source_kind,
                )
            if preview is not None:
                preview = _advance_center_preview_lifecycle(
                    preview,
                    values[start : start + 5],
                    values[start + 5 :],
                )
            if preview is not None:
                # A geometrically completed candidate is stronger lifecycle
                # evidence than a later overlapping forming seed.  This avoids
                # replacing a visible third-class completion with a shorter
                # live-edge window.
                if (
                    latest_live_preview is None
                    or preview.state is CenterPreviewState.COMPLETED
                    or latest_live_preview.state is not CenterPreviewState.COMPLETED
                ):
                    latest_live_preview = preview
        if centers and centers[-1].state is CenterState.ONGOING:
            projected = _project_ongoing_center_preview(
                centers[-1],
                values[locked_count:],
            )
            # A distinct later center candidate owns the live edge.  Project
            # the formal center only when no such candidate exists; otherwise
            # an old center's provisional completion would hide the newer
            # consolidation (the SH.000001 linked-center case).
            if projected is not None and latest_live_preview is None:
                latest_live_preview = projected
        if latest_live_preview is not None and latest_live_preview not in previews:
            previews.append(latest_live_preview)

    if latest_live_preview is None:
        tail_start = max(0, len(values) - 5)
        tail = values[tail_start:]
        if tail and (
            len(tail) < 5
            or any(not item.locked for item in tail)
            or establish_center(tail, structural_level, source_kind) is None
        ):
            preview = forming_preview(tail, structural_level, source_kind)
            if len(tail) == 5 and (
                preview is None
                or preview.state is not CenterPreviewState.TOUCH_ONLY
            ):
                preview = None
            if preview is not None and preview not in previews:
                previews.append(preview)

    return CenterLevelResult(
        structural_level=structural_level,
        price_basis_revision=price_basis_revision,
        centers=tuple(centers),
        previews=tuple(previews),
        events=tuple(events),
        locked_unit_count=locked_count,
        replay_from=replay_from,
    )
