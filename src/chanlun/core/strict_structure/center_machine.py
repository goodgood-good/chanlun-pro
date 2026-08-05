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


def _conflicting_pair(
    previous: ConstituentUnit,
    current: ConstituentUnit,
    oscillatory_ids: frozenset[str],
) -> bool:
    """Return whether two adjacent units may not follow one another.

    线段永远有方向，相邻线段必然一上一下，所以线段层保持严格交替。

    走势类型层不同：盘整没有方向，不参与交替判定；但两段有向
    趋势若直接相邻且同向，不能作为两个独立走势单元递归。原文的结合律
    并非取消方向约束，而是要求先把这种同向相邻走势合并成一个走势，
    再做同级别分解。

    因此 ``oscillatory_ids`` 只豁免真正的盘整连接件；其余直接相邻单元仍必须
    方向相反。线段层恒传空集，故与原严格交替完全等价。
    """

    if previous.unit_id in oscillatory_ids or current.unit_id in oscillatory_ids:
        return False
    return previous.direction == current.direction


def _alternates(
    values: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str] = frozenset(),
) -> bool:
    return not any(
        _conflicting_pair(previous, current, oscillatory_ids)
        for previous, current in zip(values, values[1:])
    )


def _touches_core(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    """Return whether a closed component interval intersects the closed core."""

    return max(item.low_tick, zd_tick) <= min(item.high_tick, zg_tick)


def _return_reenters_core(
    leave: ConstituentUnit,
    ret: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
) -> bool:
    """A first return re-enters only after crossing the relevant boundary.

    Equality stays outside by the V3 contract: ``low >= ZG`` is a third buy
    and ``high <= ZD`` is a third sell.
    """

    return (
        ret.low_tick < zg_tick
        if leave.direction == "up"
        else ret.high_tick > zd_tick
    )


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
    if len(initial_units) != 3:
        raise ValueError("center seed must contain exactly three units")
    return (
        max(item.low_tick for item in initial_units),
        min(item.high_tick for item in initial_units),
    )


def _is_leave_candidate(
    center: TrendCenter,
    item: ConstituentUnit,
) -> bool:
    """Whether a touching component departs beyond the frozen core."""

    return _outside_in_direction(item, center.zd_tick, center.zg_tick)


def _seed_size(source_kind: SourceKind) -> int:
    """Return the original-text first-three establishment width."""

    SourceKind(source_kind)
    return 3


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
            "chanlun-center-event/v4",
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
    initial_units: tuple[ConstituentUnit, ...],
    structural_level: int,
    source_kind: SourceKind,
    price_basis_revision: str,
    zd_tick: int,
    zg_tick: int,
) -> TrendCenter:
    center_id = stable_structure_id(
        "chanlun-center/v4",
        price_basis_revision,
        structural_level,
        source_kind.value,
        tuple(item.unit_id for item in initial_units),
        zd_tick,
        zg_tick,
    )
    last = initial_units[-1]
    # All three seed components belong to the core.  The next component is the
    # first one that may become a departure.
    pending_leave = None
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
        established_market_time=last.market_end,
        established_at=last.confirmed_at,
        last_touch_market_time=last.market_end,
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> TrendCenter | None:
    values = tuple(initial_units)
    if len(values) != _seed_size(source_kind) or not _alternates(
        values, oscillatory_ids
    ):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    if any(not item.locked for item in values):
        return None
    zd_tick, zg_tick = _core(values)
    if zd_tick > zg_tick:
        return None
    if any(not _touches_core(item, zd_tick, zg_tick) for item in values):
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> CenterPreview | None:
    """Build a non-formal first-three candidate containing provisional units.

    Segment calculation can expose more than one unlocked unit at the live
    edge.  Such a candidate is display evidence only, but every provisional
    unit may participate in the first-three overlap test.
    """

    values = tuple(initial_units)
    if len(values) != _seed_size(source_kind) or not _alternates(
        values, oscillatory_ids
    ):
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
    if zd_tick > zg_tick:
        return None
    if any(not _touches_core(item, zd_tick, zg_tick) for item in values):
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> CenterPreview | None:
    """Advance provisional center geometry without promoting it to formal evidence.

    Unlocked segments may establish, extend and geometrically complete a center for
    display.  The result deliberately remains a non-tradable ``CenterPreview``;
    formal centers and confirmed third-class points still require locked units.
    """

    body = list(initial_units)
    required = _seed_size(preview.source_kind)
    if len(body) < required or tuple(item.unit_id for item in body) != preview.unit_ids:
        raise ValueError("preview lifecycle seed mismatch")
    if preview.state is not CenterPreviewState.FORMING:
        raise ValueError("only a forming preview can advance")
    if preview.zd_tick is None or preview.zg_tick is None:
        raise ValueError("preview lifecycle requires a positive core")

    pending = None
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
            _conflicting_pair(previous, item, oscillatory_ids)
            or item.start_tick != previous.end_tick
            or item.market_start < previous.market_end
        ):
            raise ValueError("preview transition must be connected and alternating")
        available_at = max(available_at, item.available_at)

        if pending is not None:
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
                    pending_leave_unit_id=None,
                    completion_return_unit_id=item.unit_id,
                )
            if _return_reenters_core(
                pending,
                item,
                preview.zd_tick,
                preview.zg_tick,
            ) and _touches_core(item, preview.zd_tick, preview.zg_tick):
                body.append(item)
                pending = (
                    item
                    if _outside_in_direction(
                        item,
                        preview.zd_tick,
                        preview.zg_tick,
                    )
                    else None
                )
                continue
            return None

        if not _touches_core(item, preview.zd_tick, preview.zg_tick):
            return None
        body.append(item)
        pending = (
            item
            if _outside_in_direction(item, preview.zd_tick, preview.zg_tick)
            else None
        )

    return replace(
        preview,
        unit_ids=tuple(value.unit_id for value in body),
        available_at=available_at,
        pending_leave_unit_id=(None if pending is None else pending.unit_id),
    )


def _project_ongoing_center_preview(
    center: TrendCenter,
    following_units,
    oscillatory_ids: frozenset[str] = frozenset(),
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
        oscillatory_ids,
    )


def _validate_transition_unit(
    center: TrendCenter,
    item: ConstituentUnit,
    oscillatory_ids: frozenset[str] = frozenset(),
) -> None:
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
    if _conflicting_pair(previous, item, oscillatory_ids):
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
    pending_leave = item if _is_leave_candidate(center, item) else None
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> tuple[TrendCenter, CenterEvent]:
    if center.state is CenterState.COMPLETED:
        raise ValueError("completed center cannot transition")
    if not item.locked:
        raise ValueError("formal center transition must be locked")
    _validate_transition_unit(center, item, oscillatory_ids)

    pending = center.pending_leave_unit
    if pending is not None:
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
        if _return_reenters_core(
            pending,
            item,
            center.zd_tick,
            center.zg_tick,
        ) and _touches_core(item, center.zd_tick, center.zg_tick):
            return _append_extension_return(center, item)
        raise ValueError(
            "return geometry is neither extension nor third-class completion"
        )

    if not _touches_core(item, center.zd_tick, center.zg_tick):
        raise ValueError("ongoing center unit must re-enter the core")
    pending_leave = item if _is_leave_candidate(center, item) else None
    return _append_body_unit(center, item, pending_leave=pending_leave)


def forming_preview(
    candidate,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
) -> CenterPreview | None:
    values = tuple(candidate)
    maximum = _seed_size(source_kind)
    if not 1 <= len(values) <= maximum or not _alternates(
        values, oscillatory_ids
    ):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    zd_tick = None
    zg_tick = None
    state = CenterPreviewState.FORMING
    core_ready = 3
    if len(values) >= core_ready:
        zd_tick, zg_tick = _core(values)
        if zd_tick > zg_tick:
            return None
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
    oscillatory_ids: frozenset[str] = frozenset(),
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
            if _conflicting_pair(previous, item, oscillatory_ids):
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> bool:
    """Return whether a later overlapping window preserves the trend leg.

    A too-early seed can absorb the connecting rise/fall into its envelope and
    turn two separated centers into an artificial upgrade.  Only prefer a
    later seed when it lies inside the current candidate body and is itself a
    viable center with a separated up/down relation to the previous center.
    """

    width = _seed_size(source_kind)
    last_start = min(current_body_end, len(formal) - width)
    for start in range(current_start + 1, last_start + 1):
        candidate = establish_center(
            formal[start : start + width],
            structural_level,
            source_kind,
            oscillatory_ids,
        )
        if candidate is None:
            continue
        offset = start + width
        geometry_stopped = False
        while offset < len(formal):
            try:
                candidate, _event_value = advance_center(
                    candidate,
                    formal[offset],
                    oscillatory_ids,
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
    oscillatory_ids: frozenset[str] = frozenset(),
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
    width = _seed_size(source_kind)
    for start in range(current_start + 1, last_offset - width + 1):
        # A five-unit seed needs at least one following return unit.  Once a
        # later start cannot beat the earliest completion already found, no
        # subsequent start can beat it either.
        if earliest_key is not None and start + width >= earliest_key[0]:
            break
        candidate = establish_center(
            formal[start : start + width],
            structural_level,
            source_kind,
            oscillatory_ids,
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
        offset = start + width
        while offset <= last_offset:
            try:
                candidate, event = advance_center(
                    candidate, formal[offset], oscillatory_ids
                )
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
    oscillatory_ids: frozenset[str] = frozenset(),
) -> CenterLevelResult:
    values = tuple(units)
    source_kind = SourceKind(source_kind)
    validate_unit_sequence(values, structural_level, source_kind, oscillatory_ids)
    price_basis_revision = values[0].price_basis_revision if values else None

    locked_count = 0
    for item in values:
        if not item.locked:
            break
        locked_count += 1
    formal = values[:locked_count]
    width = _seed_size(source_kind)

    centers: list[TrendCenter] = []
    events: list[CenterEvent] = []
    previews: list[CenterPreview] = []
    i = 0
    replay_from = 0
    while i + width - 1 < len(formal):
        center = establish_center(
            formal[i : i + width],
            structural_level,
            source_kind,
            oscillatory_ids,
        )
        if center is None:
            observation = forming_preview(
                formal[i : i + width],
                structural_level,
                source_kind,
                oscillatory_ids,
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
                # All seed units belong to the core; departure evidence starts
                # with the following completed lower-level component.
                leave=None,
            )
        ]
        j = i + width
        geometry_stopped = False
        while j < len(formal):
            try:
                center, event = advance_center(center, formal[j], oscillatory_ids)
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
            oscillatory_ids=oscillatory_ids,
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
                    oscillatory_ids,
                )
            ):
                i += 1
                continue
        centers.append(center)
        events.extend(candidate_events)
        replay_from = i
        if center.state is CenterState.COMPLETED:
            # The departure belongs to the completed center body.  Its first
            # outside return is confirmation evidence (not body) and is the
            # earliest unit eligible to seed the next center.  Reusing the
            # departure in both centers makes their bodies overlap and forces
            # the trend assembler either to duplicate a source unit or omit a
            # center component.  Starting at the return gives a unique,
            # contiguous same-level decomposition.
            i = max(i + 1, j)
            continue
        break

    latest_live_preview = None
    if locked_count < len(values) and len(values) >= width:
        # A valid source-specific seed can end before the final provisional unit.
        # Scan every window that intersects the unlocked suffix and select the
        # latest consolidation candidate instead of assuming ``values[-5:]``.
        first_live_start = max(0, locked_count - (width - 1))
        for start in range(first_live_start, len(values) - width + 1):
            preview = establish_center_preview(
                values[start : start + width],
                structural_level,
                source_kind,
                oscillatory_ids,
            )
            if preview is not None:
                preview = _advance_center_preview_lifecycle(
                    preview,
                    values[start : start + width],
                    values[start + width :],
                    oscillatory_ids,
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
                oscillatory_ids,
            )
            # A formal ongoing center owns the entire provisional suffix.
            # Any shifted three-unit window inside that suffix is merely an
            # alternative decomposition until the source units lock; showing
            # it beside the owner created the duplicate unfinished centers
            # reported on TSLA/SH.513100 and could also manufacture a second
            # provisional third-class point.  Fail closed: expose only the
            # projection rooted at the immutable formal center_id.  If that
            # projection is geometrically invalid, expose no replacement until
            # the locked-prefix calculation can resolve the boundary.
            latest_live_preview = projected
        if latest_live_preview is not None and latest_live_preview not in previews:
            previews.append(latest_live_preview)

    if latest_live_preview is None:
        tail_start = max(0, len(values) - width)
        tail = values[tail_start:]
        if tail and (
            len(tail) < width
            or any(not item.locked for item in tail)
            or establish_center(tail, structural_level, source_kind, oscillatory_ids)
            is None
        ):
            preview = forming_preview(
                tail, structural_level, source_kind, oscillatory_ids
            )
            if len(tail) == width and (
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
