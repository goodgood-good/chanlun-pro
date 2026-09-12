from __future__ import annotations
from dataclasses import replace
from datetime import datetime
from chanlun.core.strict_structure.center_geometry import return_outside_core
from chanlun.core.strict_structure.geometry_replay import replay_geometry
from chanlun.core.strict_structure.identity import build_center_id, stable_structure_id
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


def _conflicting_pair(previous: ConstituentUnit, current: ConstituentUnit) -> bool:
    """Adjacent physical units must alternate direction."""
    return previous.direction == current.direction


def _alternates(values: tuple[ConstituentUnit, ...]) -> bool:
    return not any(
        (
            _conflicting_pair(previous, current)
            for (previous, current) in zip(values, values[1:])
        )
    )


def _touches_core(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    """返回单元闭区间是否与中枢核心闭区间相交。"""
    return max(item.low_tick, zd_tick) <= min(item.high_tick, zg_tick)


def _positive_overlap(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    """返回单元与中枢核心是否存在正宽度交集。"""
    return max(item.low_tick, zd_tick) < min(item.high_tick, zg_tick)


def _overlaps_core(
    item: ConstituentUnit, zd_tick: int, zg_tick: int, source_kind: SourceKind
) -> bool:
    """Physical center roles must overlap the core with positive width."""
    return _positive_overlap(item, zd_tick, zg_tick)


def _return_reenters_core(
    leave: ConstituentUnit, ret: ConstituentUnit, zd_tick: int, zg_tick: int
) -> bool:
    """触及核心闭区间即回到边界；等值不构成三类点。"""
    direction = leave.direction
    return not return_outside_core(
        direction=direction,
        low_tick=ret.low_tick,
        high_tick=ret.high_tick,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )


def _outside_in_direction(item: ConstituentUnit, zd_tick: int, zg_tick: int) -> bool:
    return (
        item.end_tick > zg_tick if item.direction == "up" else item.end_tick < zd_tick
    )


def _core(core_units: tuple[ConstituentUnit, ...]) -> tuple[int, int]:
    if len(core_units) != 3:
        raise ValueError("center core must contain exactly three units")
    return (
        max((item.low_tick for item in core_units)),
        min((item.high_tick for item in core_units)),
    )


def _is_leave_candidate(center: TrendCenter, item: ConstituentUnit) -> bool:
    """返回一个重叠单元是否最终收在任一核心边界之外。"""
    return _touches_core(
        item, center.zd_tick, center.zg_tick
    ) and _outside_in_direction(item, center.zd_tick, center.zg_tick)


def _seed_size(source_kind: SourceKind) -> int:
    """Entry, three core units and an independent leave require five roles."""
    return 5


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
            "chanlun-center-event",
            center.price_basis_revision,
            center.structural_level,
            center.source_kind.value,
            center.center_id,
            kind.value,
            center.body_revision,
            market_time,
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
    values: tuple[ConstituentUnit, ...], structural_level: int, source_kind: SourceKind
) -> str:
    if not values:
        raise ValueError("center candidate cannot be empty")
    source_kind = SourceKind(source_kind)
    if any(
        (
            item.structural_level != structural_level
            or item.source_kind is not source_kind
            for item in values
        )
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
    entry_unit: ConstituentUnit | None,
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
    center_id = build_center_id(
        price_basis_revision=price_basis_revision,
        structural_level=structural_level,
        source_kind=source_kind.value,
        entry_unit_id=None if entry_unit is None else entry_unit.unit_id,
        initial_unit_ids=tuple((item.unit_id for item in initial_units)),
        establishment_leave_unit_id=None
        if establishment_leave_unit is None
        else establishment_leave_unit.unit_id,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )
    body_units = initial_units + extension_units
    maturity = establishment_leave_unit or initial_units[-1]
    return TrendCenter(
        center_id=center_id,
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        state=CenterState.ONGOING,
        entry_unit=entry_unit,
        establishment_leave_unit=establishment_leave_unit,
        initial_units=initial_units,
        body_units=body_units,
        extension_units=extension_units,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        dd_tick=min((item.low_tick for item in body_units)),
        gg_tick=max((item.high_tick for item in body_units)),
        body_start_market_time=initial_units[0].market_start,
        established_market_time=maturity.market_end,
        established_at=maturity.confirmed_at,
        last_touch_market_time=body_units[-1].market_end,
        pending_leave_unit=pending_leave,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max((item.available_at for item in evidence_units)),
        body_revision=len(extension_units),
    )


def establish_center(
    initial_units,
    structural_level: int,
    source_kind: SourceKind,
    *,
    entry_unit: ConstituentUnit | None = None,
) -> TrendCenter | None:
    values = tuple(initial_units)
    source_kind = SourceKind(source_kind)
    width = _seed_size(source_kind)
    if entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    if len(values) != width or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(values, structural_level, source_kind)
    if any((not item.locked for item in values)):
        return None
    seed_entry = values[0]
    core_units = values[1:4]
    establishment_leave = values[4]
    evidence = values
    extension_units = ()
    (zd_tick, zg_tick) = _core(core_units)
    if zd_tick >= zg_tick:
        return None
    if any(
        (not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in core_units)
    ):
        return None
    if (
        not _positive_overlap(seed_entry, zd_tick, zg_tick)
        or not _positive_overlap(establishment_leave, zd_tick, zg_tick)
        or (not _outside_in_direction(establishment_leave, zd_tick, zg_tick))
    ):
        return None
    return _new_ongoing_center(
        seed_entry,
        establishment_leave,
        core_units,
        extension_units,
        establishment_leave,
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
    *,
    entry_unit: ConstituentUnit | None = None,
) -> CenterPreview | None:
    """构建进入、本体和离开角色相互分离的临时证据。"""
    values = tuple(initial_units)
    source_kind = SourceKind(source_kind)
    width = _seed_size(source_kind)
    if entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    if len(values) != width or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(values, structural_level, source_kind)
    seed_entry = values[0]
    core_units = values[1:4]
    establishment_leave = values[4]
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
    (zd_tick, zg_tick) = _core(core_units)
    if zd_tick >= zg_tick:
        return None
    if any(
        (not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in core_units)
    ):
        return None
    if (
        not _positive_overlap(seed_entry, zd_tick, zg_tick)
        or not _positive_overlap(establishment_leave, zd_tick, zg_tick)
        or (not _outside_in_direction(establishment_leave, zd_tick, zg_tick))
    ):
        return None
    return CenterPreview(
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        entry_unit_id=None if seed_entry is None else seed_entry.unit_id,
        unit_ids=tuple((item.unit_id for item in body_units)),
        state=CenterPreviewState.FORMING,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max((item.available_at for item in evidence)),
        pending_leave_unit_id=None
        if establishment_leave is None
        else establishment_leave.unit_id,
        establishment_leave_unit_id=None
        if establishment_leave is None
        else establishment_leave.unit_id,
    )


def _advance_center_preview_lifecycle(
    preview: CenterPreview, initial_units, following_units
) -> CenterPreview | None:
    """推进临时中枢几何，但不把它提升为正式证据。

    未锁定线段可以为显示而建立、延伸并在几何上完成中枢，但结果仍故意保持为
    不可交易的 ``CenterPreview``；正式中枢与已确认三类点仍要求单元锁定。
    """
    known = tuple(initial_units)
    by_id = {item.unit_id: item for item in known}
    try:
        entry = None if preview.entry_unit_id is None else by_id[preview.entry_unit_id]
        body = [by_id[item_id] for item_id in preview.unit_ids]
        failed_departures = [
            by_id[item_id] for item_id in preview.failed_departure_unit_ids
        ]
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
    available_at = max(
        (
            item.available_at
            for item in (
                *((entry,) if entry is not None else ()),
                *body,
                *failed_departures,
                *((pending,) if pending else ()),
            )
        )
    )
    occupied_ids = {item.unit_id for item in body}
    occupied_ids.update((item.unit_id for item in failed_departures))
    if entry is not None:
        occupied_ids.add(entry.unit_id)
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
            _conflicting_pair(previous, item)
            or item.start_tick != previous.end_tick
            or item.market_start < previous.market_end
        ):
            raise ValueError("preview transition must be connected and alternating")
        available_at = max(available_at, item.available_at)
        if pending is not None:
            completes_up = (
                pending.direction == "up"
                and item.direction == "down"
                and return_outside_core(
                    direction="up",
                    low_tick=item.low_tick,
                    high_tick=item.high_tick,
                    zd_tick=preview.zd_tick,
                    zg_tick=preview.zg_tick,
                )
            )
            completes_down = (
                pending.direction == "down"
                and item.direction == "up"
                and return_outside_core(
                    direction="down",
                    low_tick=item.low_tick,
                    high_tick=item.high_tick,
                    zd_tick=preview.zd_tick,
                    zg_tick=preview.zg_tick,
                )
            )
            if completes_up or completes_down:
                return replace(
                    preview,
                    unit_ids=tuple((value.unit_id for value in body)),
                    failed_departure_unit_ids=tuple(
                        (value.unit_id for value in failed_departures)
                    ),
                    state=CenterPreviewState.COMPLETED,
                    available_at=available_at,
                    pending_leave_unit_id=None,
                    completion_leave_unit_id=pending.unit_id,
                    completion_return_unit_id=item.unit_id,
                )
            if _return_reenters_core(
                pending, item, preview.zd_tick, preview.zg_tick
            ) and _touches_core(item, preview.zd_tick, preview.zg_tick):
                failed_departures.append(pending)
                if _outside_in_direction(item, preview.zd_tick, preview.zg_tick):
                    pending = item
                else:
                    body.append(item)
                    pending = None
                occupied_ids.add(item.unit_id)
                continue
            return None
        if _outside_in_direction(item, preview.zd_tick, preview.zg_tick):
            if not _touches_core(item, preview.zd_tick, preview.zg_tick):
                return None
            pending = item
        else:
            if not _touches_core(item, preview.zd_tick, preview.zg_tick):
                return None
            body.append(item)
        occupied_ids.add(item.unit_id)
    return replace(
        preview,
        unit_ids=tuple((value.unit_id for value in body)),
        failed_departure_unit_ids=tuple((value.unit_id for value in failed_departures)),
        available_at=available_at,
        pending_leave_unit_id=None if pending is None else pending.unit_id,
    )


def _project_ongoing_center_preview(
    center: TrendCenter, following_units
) -> CenterPreview | None:
    """使用临时单元投影一个已正式成立但仍在进行的中枢。"""
    if center.state is not CenterState.ONGOING:
        return None
    following = tuple(following_units)
    if not following or all((item.locked for item in following)):
        return None
    known = (
        (() if center.entry_unit is None else (center.entry_unit,))
        + center.body_units
        + center.failed_departure_units
        + (() if center.pending_leave_unit is None else (center.pending_leave_unit,))
    )
    preview = CenterPreview(
        structural_level=center.structural_level,
        source_kind=center.source_kind,
        price_basis_revision=center.price_basis_revision,
        entry_unit_id=None if center.entry_unit is None else center.entry_unit.unit_id,
        unit_ids=tuple((item.unit_id for item in center.body_units)),
        state=CenterPreviewState.FORMING,
        zd_tick=center.zd_tick,
        zg_tick=center.zg_tick,
        available_at=center.available_at,
        failed_departure_unit_ids=tuple(
            (item.unit_id for item in center.failed_departure_units)
        ),
        pending_leave_unit_id=None
        if center.pending_leave_unit is None
        else center.pending_leave_unit.unit_id,
        establishment_leave_unit_id=None
        if center.establishment_leave_unit is None
        else center.establishment_leave_unit.unit_id,
    )
    try:
        return _advance_center_preview_lifecycle(preview, known, following)
    except ValueError as exc:
        if str(exc) != "preview transition must be connected and alternating":
            raise
        return None


def _preview_matches_center_seed(preview: CenterPreview, center: TrendCenter) -> bool:
    """返回临时证据是否是该正式归属中枢的投影。"""
    seed_width = center_seed_size(center.source_kind)
    active_seed = (
        center.entry_unit.unit_id if center.entry_unit is not None else None,
        *(item.unit_id for item in center.initial_units[:seed_width]),
        center.establishment_leave_unit.unit_id
        if center.establishment_leave_unit is not None
        else None,
    )
    preview_seed = (
        preview.entry_unit_id,
        *preview.unit_ids[:seed_width],
        preview.establishment_leave_unit_id,
    )
    return active_seed == preview_seed


def _validate_transition_unit(center: TrendCenter, item: ConstituentUnit) -> None:
    if (
        item.structural_level != center.structural_level
        or item.source_kind is not center.source_kind
    ):
        raise ValueError("transition level/source mismatch")
    if item.price_basis_revision != center.price_basis_revision:
        raise ValueError("transition price basis mismatch")
    occupied = {
        value.unit_id for value in (*center.body_units, *center.failed_departure_units)
    }
    if center.entry_unit is not None:
        occupied.add(center.entry_unit.unit_id)
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
    if _conflicting_pair(previous, item):
        raise ValueError("center transition must alternate")
    if item.market_start < previous.market_end:
        raise ValueError("center transition intervals must not overlap")


def _append_body_unit(
    center: TrendCenter, item: ConstituentUnit
) -> tuple[TrendCenter, CenterEvent]:
    extension_units = center.extension_units + (item,)
    body_units = center.initial_units + extension_units
    envelope = body_units + ()
    updated = replace(
        center,
        body_units=body_units,
        extension_units=extension_units,
        dd_tick=min((value.low_tick for value in envelope)),
        gg_tick=max((value.high_tick for value in envelope)),
        last_touch_market_time=item.market_end,
        pending_leave_unit=None,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(center.available_at, item.available_at),
        body_revision=len(extension_units),
    )
    return (
        updated,
        _event(
            updated, CenterEventKind.EXTENDED, item.market_end, updated.available_at
        ),
    )


def _watch_external_leave(
    center: TrendCenter, item: ConstituentUnit
) -> tuple[TrendCenter, CenterEvent]:
    updated = replace(
        center,
        pending_leave_unit=item,
        available_at=max(center.available_at, item.available_at),
    )
    kind = (
        CenterEventKind.BREAKOUT_WATCH_UP
        if center.departure_direction(item) == "up"
        else CenterEventKind.BREAKOUT_WATCH_DOWN
    )
    return (
        updated,
        _event(updated, kind, item.market_end, updated.available_at, leave=item),
    )


def _fold_failed_departure(
    center: TrendCenter, ret: ConstituentUnit
) -> tuple[TrendCenter, CenterEvent]:
    """记录已证伪离开；只有真正回到核心的单元进入中枢本体。"""
    leave = center.pending_leave_unit
    if leave is None:
        raise ValueError("failed departure fold requires a pending leave")
    crossed_opposite_boundary = _outside_in_direction(
        ret, center.zd_tick, center.zg_tick
    )
    failed_departures = center.failed_departure_units + (
        () if center._is_seed_exit(leave) else (leave,)
    )
    returned_to_body = () if crossed_opposite_boundary else (ret,)
    extension_units = center.extension_units + returned_to_body
    body_units = center.initial_units + extension_units
    envelope = body_units + ()
    updated = replace(
        center,
        body_units=body_units,
        extension_units=extension_units,
        failed_departure_units=failed_departures,
        dd_tick=min((value.low_tick for value in envelope)),
        gg_tick=max((value.high_tick for value in envelope)),
        last_touch_market_time=body_units[-1].market_end,
        pending_leave_unit=ret if crossed_opposite_boundary else None,
        completion_leave_unit=None,
        completion_return_unit=None,
        completed_at=None,
        available_at=max(center.available_at, leave.available_at, ret.available_at),
        body_revision=len(extension_units),
    )
    if crossed_opposite_boundary:
        kind = (
            CenterEventKind.BREAKOUT_WATCH_UP
            if center.departure_direction(ret) == "up"
            else CenterEventKind.BREAKOUT_WATCH_DOWN
        )
        return (
            updated,
            _event(updated, kind, ret.market_end, updated.available_at, leave=ret),
        )
    return (
        updated,
        _event(updated, CenterEventKind.EXTENDED, ret.market_end, updated.available_at),
    )


def _complete_center(
    center: TrendCenter, leave: ConstituentUnit, ret: ConstituentUnit
) -> tuple[TrendCenter, CenterEvent]:
    direction = center.departure_direction(leave)
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
    return (
        updated,
        _event(
            updated, kind, ret.market_end, updated.available_at, leave=leave, ret=ret
        ),
    )


def _supersede_center(
    center: TrendCenter,
    successor: TrendCenter,
    bridge_units: tuple[ConstituentUnit, ...],
) -> tuple[TrendCenter, CenterEvent]:
    """Close a center only after a locked disjoint successor is established."""
    if center.state is not CenterState.ONGOING:
        raise ValueError("only an ongoing center can be superseded")
    pending = center.pending_leave_unit
    if pending is not None and center._is_seed_exit(pending):
        center = replace(center, pending_leave_unit=None)
        pending = None
    if pending is not None and (not bridge_units or bridge_units[0] != pending):
        raise ValueError(
            "supersession must retain an unresolved departure as bridge context"
        )
    if not (successor.zd_tick > center.zg_tick or successor.zg_tick < center.zd_tick):
        raise ValueError("successor center core must be outside the old core")
    updated = replace(
        center,
        state=CenterState.SUPERSEDED,
        pending_leave_unit=None,
        superseded_by_center_id=successor.center_id,
        superseded_at=successor.established_at,
        supersession_bridge_units=bridge_units,
        available_at=max(
            center.available_at,
            successor.available_at,
            *(item.available_at for item in bridge_units),
        ),
    )
    return (
        updated,
        _event(
            updated,
            CenterEventKind.SUPERSEDED,
            successor.established_market_time,
            updated.available_at,
        ),
    )


@replay_geometry
def advance_center(
    center: TrendCenter, item: ConstituentUnit
) -> tuple[TrendCenter, CenterEvent]:
    if center.state is not CenterState.ONGOING:
        if center.state is CenterState.COMPLETED:
            raise ValueError("completed center cannot transition")
        raise ValueError("closed center cannot transition")
    if not item.locked:
        raise ValueError("formal center transition must be locked")
    _validate_transition_unit(center, item)
    pending = center.pending_leave_unit
    if pending is not None:
        departure_direction = center.departure_direction(pending)
        if (
            departure_direction == "up"
            and item.is_return_from("up")
            and return_outside_core(
                direction="up",
                low_tick=item.low_tick,
                high_tick=item.high_tick,
                zd_tick=center.zd_tick,
                zg_tick=center.zg_tick,
            )
        ):
            return _complete_center(center, pending, item)
        if (
            departure_direction == "down"
            and item.is_return_from("down")
            and return_outside_core(
                direction="down",
                low_tick=item.low_tick,
                high_tick=item.high_tick,
                zd_tick=center.zd_tick,
                zg_tick=center.zg_tick,
            )
        ):
            return _complete_center(center, pending, item)
        if _return_reenters_core(
            pending, item, center.zd_tick, center.zg_tick
        ) and center._touches_core(item):
            return _fold_failed_departure(center, item)
        raise ValueError(
            "return geometry is neither extension nor third-class completion"
        )
    if _is_leave_candidate(center, item):
        return _watch_external_leave(center, item)
    if not center._touches_core(item):
        raise ValueError("ongoing center unit must re-enter the core")
    return _append_body_unit(center, item)


def forming_preview(
    candidate,
    structural_level: int,
    source_kind: SourceKind,
    *,
    entry_unit: ConstituentUnit | None = None,
) -> CenterPreview | None:
    values = tuple(candidate)
    source_kind = SourceKind(source_kind)
    maximum = _seed_size(source_kind)
    if entry_unit is not None:
        values = (entry_unit,) + values
        entry_unit = None
    minimum = 2
    if not minimum <= len(values) <= maximum or not _alternates(values):
        return None
    price_basis_revision = _validate_seed_context(values, structural_level, source_kind)
    establishment_leave = None
    seed_entry = values[0]
    body = values[1:4]
    core_ready = len(values) >= 4
    establishment_leave = values[4] if len(values) == 5 else None
    evidence = values
    pending_leave = None
    zd_tick = None
    zg_tick = None
    state = CenterPreviewState.FORMING
    if core_ready:
        core_units = tuple(body[:3])
        (zd_tick, zg_tick) = _core(core_units)
        if zd_tick > zg_tick:
            return None
        if zd_tick == zg_tick:
            state = CenterPreviewState.TOUCH_ONLY
        if state is CenterPreviewState.TOUCH_ONLY:
            if any(
                (item.low_tick > zd_tick or item.high_tick < zg_tick for item in body)
            ):
                return None
        elif any(
            (not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in body)
        ):
            return None
        if state is CenterPreviewState.TOUCH_ONLY:
            if not _touches_core(seed_entry, zd_tick, zg_tick):
                return None
            if establishment_leave is not None and (
                not _touches_core(establishment_leave, zd_tick, zg_tick)
            ):
                return None
        else:
            if not _positive_overlap(seed_entry, zd_tick, zg_tick):
                return None
            if establishment_leave is not None and (
                not _positive_overlap(establishment_leave, zd_tick, zg_tick)
            ):
                return None
        if establishment_leave is not None:
            if not _outside_in_direction(establishment_leave, zd_tick, zg_tick):
                return None
            pending_leave = establishment_leave
    return CenterPreview(
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        entry_unit_id=None if seed_entry is None else seed_entry.unit_id,
        unit_ids=tuple((item.unit_id for item in body)),
        state=state,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max((item.available_at for item in evidence)),
        pending_leave_unit_id=None if pending_leave is None else pending_leave.unit_id,
        establishment_leave_unit_id=None
        if establishment_leave is None
        else establishment_leave.unit_id,
    )


def validate_unit_sequence(
    values: tuple[ConstituentUnit, ...], structural_level: int, source_kind: SourceKind
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
            if _conflicting_pair(previous, item):
                raise ValueError("unit directions must alternate")
            if item.start_tick != previous.end_tick:
                raise ValueError("adjacent unit prices must connect")
            if item.market_start < previous.market_end:
                raise ValueError("unit market intervals must not overlap")
        previous = item


def _next_scan_start_after_completion(
    completion_return_offset: int, source_kind: SourceKind
) -> int:
    """The completed leave is the entry of the next five-role window."""
    if completion_return_offset <= 0:
        raise ValueError("completion return must follow a leave unit")
    return completion_return_offset - 1


def _first_disjoint_successor_seed(
    values: tuple[ConstituentUnit, ...],
    start: int,
    center: TrendCenter,
    structural_level: int,
    source_kind: SourceKind,
) -> tuple[int, TrendCenter] | None:
    """A disjoint successor must have five locked physical roles."""
    width = _seed_size(source_kind)
    for candidate_start in range(start, len(values) - width + 1):
        candidate = establish_center(
            values[candidate_start : candidate_start + width],
            structural_level,
            source_kind,
            entry_unit=None,
        )
        if candidate is None:
            continue
        if candidate.zd_tick > center.zg_tick or candidate.zg_tick < center.zd_tick:
            return (candidate_start, candidate)
    return None


def _first_successor_preview(
    values: tuple[ConstituentUnit, ...],
    resume_from: int,
    structural_level: int,
    source_kind: SourceKind,
) -> CenterPreview | None:
    """返回预览完成后首个因果实时中枢。

    三个同级单元的候选核心一旦具备重叠就可见；锁定后直接提升为正式中枢。
    """
    width = _seed_size(source_kind)
    minimum_ready = 4
    last_start = len(values) - minimum_ready
    for start in range(resume_from, last_start + 1):
        remaining = len(values) - start
        entry = None
        if remaining >= width:
            seed = values[start : start + width]
            preview = establish_center_preview(
                seed, structural_level, source_kind, entry_unit=entry
            )
            if preview is not None:
                lifecycle_seed = seed
                if entry is not None:
                    lifecycle_seed = (entry,) + lifecycle_seed
                preview = _advance_center_preview_lifecycle(
                    preview, lifecycle_seed, values[start + width :]
                )
        else:
            preview = forming_preview(
                values[start:], structural_level, source_kind, entry_unit=entry
            )
        if (
            preview is not None
            and preview.state
            in (CenterPreviewState.FORMING, CenterPreviewState.COMPLETED)
            and (preview.zd_tick is not None)
            and (preview.zg_tick is not None)
        ):
            return preview
    return None


def _successor_previews_after_completion(
    values: tuple[ConstituentUnit, ...],
    completed: CenterPreview,
    structural_level: int,
    source_kind: SourceKind,
) -> tuple[CenterPreview, ...]:
    """在临时三类点完成后串联后续中枢预览。

    已完成中枢的离开段会共享为下一物理中枢的进入段。实时边缘必须重复此步骤：
    一个临时中枢可能已经完成，而第二个中枢只有四个单元，尚无第五成熟段。
    """
    offsets = {item.unit_id: index for (index, item) in enumerate(values)}
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
        )
        if successor is None or successor == current or successor in successors:
            break
        successors.append(successor)
        current = successor
    return tuple(successors)


def calculate_centers(
    units, structural_level: int, source_kind: SourceKind
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
    width = _seed_size(source_kind)
    centers: list[TrendCenter] = []
    events: list[CenterEvent] = []
    previews: list[CenterPreview] = []
    i = 0
    replay_from = 0
    while i + width - 1 < len(formal):
        center = establish_center(
            formal[i : i + width], structural_level, source_kind, entry_unit=None
        )
        if center is None:
            observation = forming_preview(
                formal[i : i + width], structural_level, source_kind, entry_unit=None
            )
            if (
                observation is not None
                and observation.state is CenterPreviewState.TOUCH_ONLY
                and (observation not in previews)
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
        geometry_stop_at = None
        successor_start = None
        while j < len(formal):
            try:
                (center, event) = advance_center(center, formal[j])
            except ValueError as exc:
                if str(exc) not in _GEOMETRY_STOP_ERRORS:
                    raise
                geometry_stop_at = j
                break
            candidate_events.append(event)
            if center.state is CenterState.COMPLETED:
                break
            j += 1
        if center.state is CenterState.ONGOING and geometry_stop_at is not None:
            successor_seed = _first_disjoint_successor_seed(
                formal, geometry_stop_at, center, structural_level, source_kind
            )
            if successor_seed is not None:
                (successor_start, successor) = successor_seed
                bridge_end = successor_start + int(
                    successor.entry_unit == formal[successor_start]
                )
                bridge_units = (
                    *(
                        ()
                        if center.pending_leave_unit is None
                        else (center.pending_leave_unit,)
                    ),
                    *formal[geometry_stop_at:bridge_end],
                )
                (center, superseded_event) = _supersede_center(
                    center, successor, bridge_units
                )
                candidate_events.append(superseded_event)
        centers.append(center)
        events.extend(candidate_events)
        replay_from = i
        if center.state is CenterState.COMPLETED:
            i = max(i + 1, _next_scan_start_after_completion(j, source_kind))
            continue
        if center.state is CenterState.SUPERSEDED:
            if successor_start is None:
                raise ValueError("superseded center requires a successor offset")
            i = successor_start
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
                (
                    index
                    for (index, item) in enumerate(values)
                    if item.unit_id == completion_return.unit_id
                )
            )
            post_completion_resume = _next_scan_start_after_completion(
                completion_return_offset, source_kind
            )
        first_live_start = max(
            0,
            locked_count - (width - 1),
            0 if post_completion_resume is None else post_completion_resume,
        )
        for start in range(first_live_start, len(values) - width + 1):
            entry = None
            preview = establish_center_preview(
                values[start : start + width],
                structural_level,
                source_kind,
                entry_unit=entry,
            )
            if preview is not None:
                lifecycle_seed = values[start : start + width]
                if entry is not None:
                    lifecycle_seed = (entry,) + lifecycle_seed
                preview = _advance_center_preview_lifecycle(
                    preview, lifecycle_seed, values[start + width :]
                )
            if preview is not None:
                if post_completion_resume is not None:
                    latest_live_preview = preview
                    break
                if (
                    latest_live_preview is None
                    or preview.state is CenterPreviewState.COMPLETED
                    or latest_live_preview.state is not CenterPreviewState.COMPLETED
                ):
                    latest_live_preview = preview
        if centers and centers[-1].state is CenterState.ONGOING:
            projected = _project_ongoing_center_preview(
                centers[-1], values[locked_count:]
            )
            latest_live_preview = projected
        if latest_live_preview is not None and latest_live_preview not in previews:
            previews.append(latest_live_preview)
            active_owner = (
                centers[-1]
                if centers and centers[-1].state is CenterState.ONGOING
                else None
            )
            if latest_live_preview.state is CenterPreviewState.COMPLETED and (
                active_owner is None
                or _preview_matches_center_seed(latest_live_preview, active_owner)
            ):
                for successor in _successor_previews_after_completion(
                    values, latest_live_preview, structural_level, source_kind
                ):
                    if successor not in previews:
                        previews.append(successor)
    if latest_live_preview is None:
        tail_start = max(
            0,
            len(values) - width,
            0 if post_completion_resume is None else post_completion_resume,
        )
        tail = values[tail_start:]
        tail_entry = None
        if tail and (
            len(tail) < width
            or any((not item.locked for item in tail))
            or establish_center(
                tail, structural_level, source_kind, entry_unit=tail_entry
            )
            is None
        ):
            preview = forming_preview(
                tail, structural_level, source_kind, entry_unit=tail_entry
            )
            if len(tail) == width and (
                preview is None or preview.state is not CenterPreviewState.TOUCH_ONLY
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
