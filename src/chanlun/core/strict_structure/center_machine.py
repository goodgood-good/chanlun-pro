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
        pending_leave_unit=initial_units[4],
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
    if not _outside_in_direction(values[4], zd_tick, zg_tick):
        return None
    return _new_ongoing_center(
        values,
        structural_level,
        SourceKind(source_kind),
        price_basis_revision,
        zd_tick,
        zg_tick,
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
        if _outside_in_direction(item, center.zd_tick, center.zg_tick)
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
        if _outside_in_direction(item, center.zd_tick, center.zg_tick)
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

        events.append(
            _event(
                center,
                CenterEventKind.ESTABLISHED,
                center.established_market_time,
                center.available_at,
                leave=center.initial_exit_unit,
            )
        )
        j = i + 5
        geometry_stopped = False
        while j < len(formal):
            try:
                center, event = advance_center(center, formal[j])
            except ValueError as exc:
                if str(exc) not in {
                    "ongoing center unit must re-enter the core",
                    "return geometry is neither extension nor third-class completion",
                }:
                    raise
                geometry_stopped = True
                break
            events.append(event)
            if center.state is CenterState.COMPLETED:
                break
            j += 1
        centers.append(center)
        replay_from = i
        if geometry_stopped:
            i = j
            continue
        if center.state is CenterState.COMPLETED:
            i = j
            continue
        break

    tail_start = max(0, len(values) - 5)
    tail = values[tail_start:]
    if tail and (
        len(tail) < 5
        or any(not item.locked for item in tail)
        or establish_center(tail, structural_level, source_kind) is None
    ):
        preview = forming_preview(tail, structural_level, source_kind)
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
