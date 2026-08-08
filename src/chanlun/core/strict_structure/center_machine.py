from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterEvent,
    CenterEventKind,
    CenterLevelResult,
    CenterPreview,
    CenterPreviewState,
    CenterState,
    ConstituentUnit,
    SourceKind,
    TrendCenter,
    center_seed_size,
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


def _positive_overlap(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
) -> bool:
    """Return whether a unit and core share a positive-width interval."""

    return max(item.low_tick, zd_tick) < min(item.high_tick, zg_tick)


def _overlaps_core(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
    source_kind: SourceKind,
) -> bool:
    """Apply the source-specific overlap contract.

    Line/stroke centers require a positive-width overlap.  Recursive inputs are
    already-completed lower-level trend types and retain the original closed
    interval rule, where equality at one tick is a valid center boundary.
    """

    if SourceKind(source_kind) is SourceKind.TREND_TYPE:
        return _touches_core(item, zd_tick, zg_tick)
    return _positive_overlap(item, zd_tick, zg_tick)


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


def _core(core_units: tuple[ConstituentUnit, ...]) -> tuple[int, int]:
    if len(core_units) != 3:
        raise ValueError("center core must contain exactly three units")
    return (
        max(item.low_tick for item in core_units),
        min(item.high_tick for item in core_units),
    )


def _is_leave_candidate(
    center: TrendCenter,
    item: ConstituentUnit,
) -> bool:
    """Whether an overlapping unit finishes outside either core boundary."""

    return _outside_in_direction(item, center.zd_tick, center.zg_tick)


def _seed_size(source_kind: SourceKind) -> int:
    """Return the candidate width consumed by ``establish_center``.

    Physical line/stroke centers require one immutable five-segment window:
    entry + middle-three core + maturity.  The maturity segment may either be
    an external departure or the first extension. Recursive centers are still
    built from three completed lower-level trend types.
    """

    return (
        center_seed_size(source_kind)
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else 5
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
            "chanlun-center-event/v8",
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
    entry_unit: ConstituentUnit,
    establishment_unit: ConstituentUnit | None,
    establishment_leave_unit: ConstituentUnit | None,
    initial_units: tuple[ConstituentUnit, ...],
    extension_units: tuple[ConstituentUnit, ...],
    pending_leave: ConstituentUnit | None,
    evidence_units: tuple[ConstituentUnit, ...],
    structural_level: int,
    source_kind: SourceKind,
    price_basis_revision: str,
    zd_tick: int,
    zg_tick: int,
) -> TrendCenter:
    center_id = stable_structure_id(
        # Keep identities stable for centers whose five establishment units
        # were already valid under the previous contract.
        "chanlun-center/v9",
        price_basis_revision,
        structural_level,
        source_kind.value,
        entry_unit.unit_id,
        tuple(item.unit_id for item in initial_units),
        (
            None
            if establishment_unit is None
            else establishment_unit.unit_id
        ),
        zd_tick,
        zg_tick,
    )
    body_units = initial_units + extension_units
    maturity = establishment_unit or initial_units[-1]
    return TrendCenter(
        center_id=center_id,
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        state=CenterState.ONGOING,
        entry_unit=entry_unit,
        establishment_unit=establishment_unit,
        establishment_leave_unit=establishment_leave_unit,
        initial_units=initial_units,
        body_units=body_units,
        extension_units=extension_units,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        dd_tick=min(item.low_tick for item in body_units),
        gg_tick=max(item.high_tick for item in body_units),
        body_start_market_time=initial_units[0].market_start,
        established_market_time=maturity.market_end,
        established_at=maturity.confirmed_at,
        last_touch_market_time=body_units[-1].market_end,
        pending_leave_unit=pending_leave,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(item.available_at for item in evidence_units),
        body_revision=len(extension_units),
    )


def establish_center(
    initial_units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    entry_unit: ConstituentUnit | None = None,
) -> TrendCenter | None:
    values = tuple(initial_units)
    source_kind = SourceKind(source_kind)
    width = _seed_size(source_kind)
    if source_kind is not SourceKind.TREND_TYPE and entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    if len(values) != width or not _alternates(values, oscillatory_ids):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    if any(not item.locked for item in values):
        return None
    if source_kind is SourceKind.TREND_TYPE:
        seed_entry = entry_unit
        if seed_entry is None:
            return None
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if not seed_entry.locked:
            return None
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
        core_units = values
        establishment_unit = None
        initial_leave = None
        extension_units = ()
        evidence = (seed_entry,) + values
    else:
        seed_entry = values[0]
        core_units = values[1:4]
        establishment_unit = values[4]
        initial_leave = None
        extension_units = ()
        evidence = values
    zd_tick, zg_tick = _core(core_units)
    if (
        zd_tick > zg_tick
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else zd_tick >= zg_tick
    ):
        return None
    if any(
        not _overlaps_core(item, zd_tick, zg_tick, source_kind)
        for item in core_units
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE and (
        not _positive_overlap(seed_entry, zd_tick, zg_tick)
        or not _positive_overlap(establishment_unit, zd_tick, zg_tick)
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE:
        if _outside_in_direction(establishment_unit, zd_tick, zg_tick):
            initial_leave = establishment_unit
        else:
            extension_units = (establishment_unit,)
    return _new_ongoing_center(
        seed_entry,
        establishment_unit,
        initial_leave,
        core_units,
        extension_units,
        initial_leave,
        evidence,
        structural_level,
        source_kind,
        price_basis_revision,
        zd_tick,
        zg_tick,
    )


def establish_center_preview(
    initial_units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    entry_unit: ConstituentUnit | None = None,
) -> CenterPreview | None:
    """Build provisional evidence with entry/body/leave roles kept separate."""

    values = tuple(initial_units)
    source_kind = SourceKind(source_kind)
    width = _seed_size(source_kind)
    if source_kind is not SourceKind.TREND_TYPE and entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    if len(values) != width or not _alternates(values, oscillatory_ids):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    if source_kind is SourceKind.TREND_TYPE:
        seed_entry = entry_unit
        if seed_entry is None:
            return None
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
        core_units = values
        establishment_unit = None
        initial_leave = None
        body_units = core_units
        evidence = (seed_entry,) + values
    else:
        seed_entry = values[0]
        core_units = values[1:4]
        establishment_unit = values[4]
        initial_leave = None
        body_units = core_units
        evidence = values
    unlocked_seen = False
    for item in evidence:
        if not item.locked:
            unlocked_seen = True
        elif unlocked_seen:
            return None
    if not unlocked_seen:
        return None
    zd_tick, zg_tick = _core(core_units)
    if (
        zd_tick > zg_tick
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else zd_tick >= zg_tick
    ):
        return None
    if any(
        not _overlaps_core(item, zd_tick, zg_tick, source_kind)
        for item in core_units
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE and (
        not _positive_overlap(seed_entry, zd_tick, zg_tick)
        or not _positive_overlap(establishment_unit, zd_tick, zg_tick)
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE:
        if _outside_in_direction(establishment_unit, zd_tick, zg_tick):
            initial_leave = establishment_unit
        else:
            body_units = core_units + (establishment_unit,)
    return CenterPreview(
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        entry_unit_id=seed_entry.unit_id,
        unit_ids=tuple(item.unit_id for item in body_units),
        state=CenterPreviewState.FORMING,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in evidence),
        pending_leave_unit_id=(
            None if initial_leave is None else initial_leave.unit_id
        ),
        establishment_unit_id=(
            None if establishment_unit is None else establishment_unit.unit_id
        ),
        establishment_leave_unit_id=(
            None if initial_leave is None else initial_leave.unit_id
        ),
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

    known = tuple(initial_units)
    by_id = {item.unit_id: item for item in known}
    try:
        entry = by_id[preview.entry_unit_id]
        body = [by_id[item_id] for item_id in preview.unit_ids]
        pending = (
            None
            if preview.pending_leave_unit_id is None
            else by_id[preview.pending_leave_unit_id]
        )
    except KeyError as exc:
        raise ValueError("preview lifecycle seed mismatch") from exc
    if preview.state is not CenterPreviewState.FORMING:
        raise ValueError("only a forming preview can advance")
    if preview.zd_tick is None or preview.zg_tick is None:
        raise ValueError("preview lifecycle requires a positive core")

    available_at = max(item.available_at for item in (entry, *body, *((pending,) if pending else ())))
    occupied_ids = {entry.unit_id, *(item.unit_id for item in body)}
    if pending is not None:
        occupied_ids.add(pending.unit_id)

    for item in following_units:
        previous = pending or body[-1]
        if (
            item.structural_level != preview.structural_level
            or item.source_kind is not preview.source_kind
            or item.price_basis_revision != preview.price_basis_revision
        ):
            raise ValueError("preview transition level/source/basis mismatch")
        if item.unit_id in occupied_ids:
            raise ValueError("preview transition unit already belongs to lifecycle")
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
                    completion_leave_unit_id=pending.unit_id,
                    completion_return_unit_id=item.unit_id,
                )
            if _return_reenters_core(
                pending,
                item,
                preview.zd_tick,
                preview.zg_tick,
            ) and _overlaps_core(
                item,
                preview.zd_tick,
                preview.zg_tick,
                preview.source_kind,
            ):
                # The old departure failed as soon as the return crossed back
                # into the core. If that same return finishes beyond the
                # opposite boundary, it simultaneously becomes the next
                # external departure instead of being swallowed by the body.
                body.append(pending)
                if _outside_in_direction(
                    item,
                    preview.zd_tick,
                    preview.zg_tick,
                ):
                    pending = item
                else:
                    body.append(item)
                    pending = None
                occupied_ids.add(item.unit_id)
                continue
            return None

        if not _overlaps_core(
            item,
            preview.zd_tick,
            preview.zg_tick,
            preview.source_kind,
        ):
            return None
        if _outside_in_direction(item, preview.zd_tick, preview.zg_tick):
            pending = item
        else:
            body.append(item)
        occupied_ids.add(item.unit_id)

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
    known = (center.entry_unit,) + center.body_units + (
        () if center.pending_leave_unit is None else (center.pending_leave_unit,)
    )
    preview = CenterPreview(
        structural_level=center.structural_level,
        source_kind=center.source_kind,
        price_basis_revision=center.price_basis_revision,
        entry_unit_id=center.entry_unit.unit_id,
        unit_ids=tuple(item.unit_id for item in center.body_units),
        state=CenterPreviewState.FORMING,
        zd_tick=center.zd_tick,
        zg_tick=center.zg_tick,
        available_at=center.available_at,
        pending_leave_unit_id=(
            None
            if center.pending_leave_unit is None
            else center.pending_leave_unit.unit_id
        ),
        establishment_leave_unit_id=(
            None
            if center.establishment_leave_unit is None
            else center.establishment_leave_unit.unit_id
        ),
        establishment_unit_id=(
            None
            if center.establishment_unit is None
            else center.establishment_unit.unit_id
        ),
    )
    return _advance_center_preview_lifecycle(
        preview,
        known,
        following,
        oscillatory_ids,
    )


def _preview_matches_center_seed(
    preview: CenterPreview,
    center: TrendCenter,
) -> bool:
    """Return whether provisional evidence projects this formal owner."""

    seed_width = center_seed_size(center.source_kind)
    active_seed = (
        center.entry_unit.unit_id,
        *(item.unit_id for item in center.initial_units[:seed_width]),
        *(
            ()
            if center.source_kind is SourceKind.TREND_TYPE
            or center.establishment_unit is None
            else (center.establishment_unit.unit_id,)
        ),
    )
    preview_seed = (
        preview.entry_unit_id,
        *preview.unit_ids[:seed_width],
        *(
            ()
            if preview.source_kind is SourceKind.TREND_TYPE
            or preview.establishment_unit_id is None
            else (preview.establishment_unit_id,)
        ),
    )
    return active_seed == preview_seed


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
    occupied = {
        center.entry_unit.unit_id,
        *(value.unit_id for value in center.body_units),
    }
    if center.pending_leave_unit is not None:
        occupied.add(center.pending_leave_unit.unit_id)
    if item.unit_id in occupied:
        raise ValueError("transition unit id already belongs to center")
    if (
        center.completion_return_unit is not None
        and item.unit_id == center.completion_return_unit.unit_id
    ):
        raise ValueError("transition unit id already belongs to center")
    previous = center.pending_leave_unit or center.body_units[-1]
    if item.start_tick != previous.end_tick:
        raise ValueError("center transition must connect")
    if _conflicting_pair(previous, item, oscillatory_ids):
        raise ValueError("center transition must alternate")
    if item.market_start < previous.market_end:
        raise ValueError("center transition intervals must not overlap")


def _append_body_unit(
    center: TrendCenter,
    item: ConstituentUnit,
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
        pending_leave_unit=None,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(center.available_at, item.available_at),
        body_revision=len(extension_units),
    )
    return updated, _event(
        updated,
        CenterEventKind.EXTENDED,
        item.market_end,
        updated.available_at,
    )


def _watch_external_leave(
    center: TrendCenter,
    item: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    updated = replace(
        center,
        pending_leave_unit=item,
        available_at=max(center.available_at, item.available_at),
    )
    kind = (
        CenterEventKind.BREAKOUT_WATCH_UP
        if item.direction == "up"
        else CenterEventKind.BREAKOUT_WATCH_DOWN
    )
    return updated, _event(
        updated,
        kind,
        item.market_end,
        updated.available_at,
        leave=item,
    )


def _fold_failed_departure(
    center: TrendCenter,
    ret: ConstituentUnit,
) -> tuple[TrendCenter, CenterEvent]:
    """Fold a failed leave and preserve a same-unit opposite departure."""

    leave = center.pending_leave_unit
    if leave is None:
        raise ValueError("failed departure fold requires a pending leave")
    crossed_opposite_boundary = _outside_in_direction(
        ret,
        center.zd_tick,
        center.zg_tick,
    )
    folded_units = (leave,) if crossed_opposite_boundary else (leave, ret)
    extension_units = center.extension_units + folded_units
    body_units = center.initial_units + extension_units
    updated = replace(
        center,
        body_units=body_units,
        extension_units=extension_units,
        dd_tick=min(value.low_tick for value in body_units),
        gg_tick=max(value.high_tick for value in body_units),
        last_touch_market_time=body_units[-1].market_end,
        pending_leave_unit=(ret if crossed_opposite_boundary else None),
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(center.available_at, leave.available_at, ret.available_at),
        body_revision=len(extension_units),
    )
    if crossed_opposite_boundary:
        kind = (
            CenterEventKind.BREAKOUT_WATCH_UP
            if ret.direction == "up"
            else CenterEventKind.BREAKOUT_WATCH_DOWN
        )
        return updated, _event(
            updated,
            kind,
            ret.market_end,
            updated.available_at,
            leave=ret,
        )
    return updated, _event(
        updated,
        CenterEventKind.EXTENDED,
        ret.market_end,
        updated.available_at,
    )


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
        available_at=max(center.available_at, leave.available_at, ret.available_at),
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
        ) and _overlaps_core(
            item,
            center.zd_tick,
            center.zg_tick,
            center.source_kind,
        ):
            return _fold_failed_departure(center, item)
        raise ValueError(
            "return geometry is neither extension nor third-class completion"
        )

    if not _overlaps_core(
        item,
        center.zd_tick,
        center.zg_tick,
        center.source_kind,
    ):
        raise ValueError("ongoing center unit must re-enter the core")
    if _is_leave_candidate(center, item):
        return _watch_external_leave(center, item)
    return _append_body_unit(center, item)


def forming_preview(
    candidate,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    entry_unit: ConstituentUnit | None = None,
) -> CenterPreview | None:
    values = tuple(candidate)
    source_kind = SourceKind(source_kind)
    maximum = _seed_size(source_kind)
    if source_kind is not SourceKind.TREND_TYPE and entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    minimum = 1 if source_kind is SourceKind.TREND_TYPE else 2
    if not minimum <= len(values) <= maximum or not _alternates(
        values, oscillatory_ids
    ):
        return None
    price_basis_revision = _validate_seed_context(
        values,
        structural_level,
        source_kind,
    )
    establishment_unit = None
    establishment_leave = None
    if source_kind is SourceKind.TREND_TYPE:
        seed_entry = entry_unit
        if seed_entry is None:
            return None
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
        body = values
        core_ready = len(values) >= 3
        pending_leave = None
        evidence = (seed_entry,) + values
    else:
        seed_entry = values[0]
        body = values[1:4]
        core_ready = len(values) >= 4
        establishment_unit = values[4] if len(values) == 5 else None
        pending_leave = None
        evidence = values

    zd_tick = None
    zg_tick = None
    state = CenterPreviewState.FORMING
    if core_ready:
        core_units = tuple(body[:3])
        zd_tick, zg_tick = _core(core_units)
        if zd_tick > zg_tick:
            return None
        if (
            zd_tick == zg_tick
            and source_kind is not SourceKind.TREND_TYPE
        ):
            state = CenterPreviewState.TOUCH_ONLY
        if state is CenterPreviewState.TOUCH_ONLY:
            # Zero-width intersections are diagnostic observations only.  A
            # component must at least contain the shared boundary; this never
            # promotes to a formal center and the chart gate will not draw it.
            if any(
                item.low_tick > zd_tick or item.high_tick < zg_tick
                for item in body
            ):
                return None
        elif any(
            not _overlaps_core(item, zd_tick, zg_tick, source_kind)
            for item in body
        ):
            return None
        if source_kind is not SourceKind.TREND_TYPE:
            if state is CenterPreviewState.TOUCH_ONLY:
                if not _touches_core(seed_entry, zd_tick, zg_tick):
                    return None
                if establishment_unit is not None and not _touches_core(
                    establishment_unit, zd_tick, zg_tick
                ):
                    return None
            else:
                if not _positive_overlap(seed_entry, zd_tick, zg_tick):
                    return None
                if establishment_unit is not None and not _positive_overlap(
                    establishment_unit,
                    zd_tick,
                    zg_tick,
                ):
                    return None
            if establishment_unit is not None:
                if _outside_in_direction(establishment_unit, zd_tick, zg_tick):
                    pending_leave = establishment_unit
                    establishment_leave = establishment_unit
                else:
                    body = body + (establishment_unit,)
    return CenterPreview(
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        entry_unit_id=seed_entry.unit_id,
        unit_ids=tuple(item.unit_id for item in body),
        state=state,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in evidence),
        pending_leave_unit_id=(
            None if pending_leave is None else pending_leave.unit_id
        ),
        establishment_unit_id=(
            None
            if source_kind is SourceKind.TREND_TYPE
            or establishment_unit is None
            else establishment_unit.unit_id
        ),
        establishment_leave_unit_id=(
            None
            if source_kind is SourceKind.TREND_TYPE
            or establishment_leave is None
            else establishment_leave.unit_id
        ),
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


def _scanner_entry(
    values: tuple[ConstituentUnit, ...],
    start: int,
    source_kind: SourceKind,
) -> ConstituentUnit | None:
    """Return the external entry owned by a scanner seed.

    Recursive seeds begin with core unit A. Their immediately preceding
    same-level unit is the external entry, so the first stream element cannot
    seed a fully auditable recursive center. Physical five-role seeds already
    contain their entry as the first of the five units and therefore return
    ``None`` here.
    """

    source_kind = SourceKind(source_kind)
    if source_kind is not SourceKind.TREND_TYPE:
        return None
    # ``calculate_centers`` also calls this helper while probing an empty or
    # not-yet-warmed tail.  Such a tail has no auditable external entry and
    # must yield no preview, not index an absent stream element.
    return values[start - 1] if 0 < start <= len(values) else None


def _next_scan_start_after_completion(
    completion_return_offset: int,
    source_kind: SourceKind,
) -> int:
    """Resume with the completed center's leave as the next entry.

    A physical five-role seed contains its entry, so it starts one unit before
    the completion return.  A recursive seed starts with core A and obtains the
    preceding leave through ``_scanner_entry``; it therefore starts exactly at
    the completion return.
    """

    if completion_return_offset <= 0:
        raise ValueError("completion return must follow a leave unit")
    return (
        completion_return_offset
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else completion_return_offset - 1
    )


def _first_successor_preview(
    values: tuple[ConstituentUnit, ...],
    resume_from: int,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str],
) -> CenterPreview | None:
    """Return the first causal live center after a preview completion.

    A physical center becomes *visible* as soon as its external entry and
    middle-three core are present.  Its fifth component is deliberately not
    required here: that component promotes the preview to a formal center,
    but an unfinished fourth segment may already close a positive core.  Full
    establishment windows still pass through ``establish_center_preview`` so
    an already-observed invalid fifth component cannot be ignored.
    """

    width = _seed_size(source_kind)
    minimum_ready = 3 if source_kind is SourceKind.TREND_TYPE else 4
    last_start = len(values) - minimum_ready
    for start in range(resume_from, last_start + 1):
        remaining = len(values) - start
        entry = _scanner_entry(values, start, source_kind)
        if remaining >= width:
            seed = values[start : start + width]
            preview = establish_center_preview(
                seed,
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=entry,
            )
            if preview is not None:
                lifecycle_seed = seed
                if entry is not None:
                    lifecycle_seed = (entry,) + lifecycle_seed
                preview = _advance_center_preview_lifecycle(
                    preview,
                    lifecycle_seed,
                    values[start + width :],
                    oscillatory_ids,
                )
        else:
            preview = forming_preview(
                values[start:],
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=entry,
            )
        if (
            preview is not None
            and preview.state
            in (CenterPreviewState.FORMING, CenterPreviewState.COMPLETED)
            and preview.zd_tick is not None
            and preview.zg_tick is not None
        ):
            return preview
    return None


def _successor_previews_after_completion(
    values: tuple[ConstituentUnit, ...],
    completed: CenterPreview,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str],
) -> tuple[CenterPreview, ...]:
    """Chain previews after a provisional third-class completion.

    The completed center's leave is shared as the next physical center's
    entry.  Repeating this step is important at the live edge: one provisional
    center may already be complete while a second one is only four components
    old and therefore has no fifth maturity component yet.
    """

    offsets = {item.unit_id: index for index, item in enumerate(values)}
    successors: list[CenterPreview] = []
    current = completed
    visited_returns: set[str] = set()
    while current.state is CenterPreviewState.COMPLETED:
        return_id = current.completion_return_unit_id
        if return_id is None or return_id in visited_returns:
            break
        visited_returns.add(return_id)
        return_offset = offsets.get(return_id)
        if return_offset is None:
            break
        successor = _first_successor_preview(
            values,
            _next_scan_start_after_completion(return_offset, source_kind),
            structural_level,
            source_kind,
            oscillatory_ids,
        )
        if successor is None or successor == current or successor in successors:
            break
        successors.append(successor)
        current = successor
    return tuple(successors)


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
    three-unit core inside its tail has already produced a same-side
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
        # A three-unit seed needs later lifecycle evidence to complete. Once a
        # later start cannot beat the earliest completion already found, no
        # subsequent start can beat it either.
        if earliest_key is not None and start + width >= earliest_key[0]:
            break
        candidate = establish_center(
            formal[start : start + width],
            structural_level,
            source_kind,
            oscillatory_ids,
            entry_unit=_scanner_entry(formal, start, source_kind),
        )
        if candidate is None:
            continue
        candidate_events = [
            _event(
                candidate,
                CenterEventKind.ESTABLISHED,
                candidate.established_market_time,
                candidate.available_at,
                leave=candidate.pending_leave_unit,
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
    i = 1 if source_kind is SourceKind.TREND_TYPE else 0
    replay_from = 0
    while i + width - 1 < len(formal):
        center = establish_center(
            formal[i : i + width],
            structural_level,
            source_kind,
            oscillatory_ids,
            entry_unit=_scanner_entry(formal, i, source_kind),
        )
        if center is None:
            observation = forming_preview(
                formal[i : i + width],
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=_scanner_entry(formal, i, source_kind),
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
                leave=center.pending_leave_unit,
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
        previous_leave = (
            None if not centers else centers[-1].completion_leave_unit
        )
        owns_post_completion_tail = previous_leave is not None
        # After a third-class completion the scanner resumes at the completed
        # leave and slides forward. The first five-unit seed that matures in
        # that suffix is the causal owner, whether its entry is the leave, the
        # completion return, or a later unit. A narrower internal seed cannot
        # replace it merely because it happens to complete sooner.
        completed_later = (
            None
            if owns_post_completion_tail
            else _find_later_completed_candidate(
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
        )
        if completed_later is not None and (
            center.state is not CenterState.COMPLETED
            or completed_later[1] < j
        ):
            i, j, center, candidate_events = completed_later
            geometry_stopped = False
        if geometry_stopped:
            # This seed cannot consume the next same-level component as an
            # extension or a valid departure/return lifecycle. Slide one unit
            # and allow a later consolidation window to become the center.
            i += 1
            continue
        # Physical centers can only be created by the immutable five-segment
        # establishment window.  Recursive trend-type centers keep their
        # three-completed-trend seed.
        if not center.has_minimum_physical_roles:
            replay_from = i
            break
        centers.append(center)
        events.extend(candidate_events)
        replay_from = i
        if center.state is CenterState.COMPLETED:
            # The external leave may simultaneously serve as the next center's
            # entry.  Resume far enough back to test that shared-boundary seed;
            # a failed seed will slide forward normally on the next iteration.
            i = max(
                i + 1,
                _next_scan_start_after_completion(j, source_kind),
            )
            continue
        break

    latest_live_preview = None
    post_completion_resume = None
    if locked_count < len(values) and len(values) >= width:
        if centers and centers[-1].state is CenterState.COMPLETED:
            completion_return = centers[-1].completion_return_unit
            if completion_return is None:
                raise ValueError("completed center requires a completion return")
            completion_return_offset = next(
                index
                for index, item in enumerate(values)
                if item.unit_id == completion_return.unit_id
            )
            post_completion_resume = _next_scan_start_after_completion(
                completion_return_offset,
                source_kind,
            )
        # A valid core can end before the final provisional unit. Scan every
        # window intersecting the unlocked suffix, but never scan back into a
        # completed center. After a third-class point the first viable preview
        # owns the suffix under the same causal rule as the formal scanner.
        first_live_start = max(
            1 if source_kind is SourceKind.TREND_TYPE else 0,
            locked_count - (width - 1),
            0 if post_completion_resume is None else post_completion_resume,
        )
        for start in range(first_live_start, len(values) - width + 1):
            entry = _scanner_entry(values, start, source_kind)
            preview = establish_center_preview(
                values[start : start + width],
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=entry,
            )
            if preview is not None:
                lifecycle_seed = values[start : start + width]
                if entry is not None:
                    lifecycle_seed = (entry,) + lifecycle_seed
                preview = _advance_center_preview_lifecycle(
                    preview,
                    lifecycle_seed,
                    values[start + width :],
                    oscillatory_ids,
                )
            if preview is not None:
                if post_completion_resume is not None:
                    latest_live_preview = preview
                    break
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
            # Any shifted source-specific seed inside that suffix is merely an
            # alternative decomposition until the source units lock; showing
            # it beside the owner created the duplicate unfinished centers
            # reported on TSLA/SH.513100 and could also manufacture a second
            # provisional third-class point.  Fail closed: expose only the
            # projection rooted at the immutable formal center_id.  If that
            # projection is geometrically invalid, expose no replacement until
            # the locked-prefix calculation can resolve the boundary.
            if projected is None:
                # A shifted ordinary forming window cannot displace the formal
                # owner.  A shifted COMPLETED preview is different: its locked
                # leave plus outside first return is stronger lifecycle
                # evidence and must survive so a visible 3-buy/3-sell is not
                # regressed to an old ongoing box.
                if (
                    latest_live_preview is None
                    or latest_live_preview.state
                    is not CenterPreviewState.COMPLETED
                ):
                    latest_live_preview = None
            elif (
                latest_live_preview is None
                or latest_live_preview.state
                is not CenterPreviewState.COMPLETED
                or projected.state is CenterPreviewState.COMPLETED
            ):
                latest_live_preview = projected
        if latest_live_preview is not None and latest_live_preview not in previews:
            previews.append(latest_live_preview)
            active_owner = (
                centers[-1]
                if centers and centers[-1].state is CenterState.ONGOING
                else None
            )
            if (
                latest_live_preview.state is CenterPreviewState.COMPLETED
                and (
                    active_owner is None
                    or _preview_matches_center_seed(
                        latest_live_preview,
                        active_owner,
                    )
                )
            ):
                for successor in _successor_previews_after_completion(
                    values,
                    latest_live_preview,
                    structural_level,
                    source_kind,
                    oscillatory_ids,
                ):
                    if successor not in previews:
                        previews.append(successor)

    if latest_live_preview is None:
        tail_start = max(
            1 if source_kind is SourceKind.TREND_TYPE else 0,
            len(values) - width,
            0 if post_completion_resume is None else post_completion_resume,
        )
        tail = values[tail_start:]
        tail_entry = _scanner_entry(values, tail_start, source_kind)
        if tail and (
            len(tail) < width
            or any(not item.locked for item in tail)
            or establish_center(
                tail,
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=tail_entry,
            )
            is None
        ):
            preview = forming_preview(
                tail,
                structural_level,
                source_kind,
                oscillatory_ids,
                entry_unit=tail_entry,
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
