from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from chanlun.core.strict_structure.identity import build_center_id, stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterEvent,
    CenterEventKind,
    CenterLevelResult,
    CenterPreview,
    CenterPreviewState,
    CenterState,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    TrendCenter,
    center_seed_size,
)


def close_center_at_divergence(
    center: TrendCenter,
    divergence: DivergenceEvidence,
) -> TrendCenter:
    """在同宽背驰边界处冻结中枢。"""

    signal = center.lifecycle_leave_unit
    if signal is None:
        raise ValueError("divergence closure requires a center departure")
    signal_extreme = signal.high_tick if signal.direction == "up" else signal.low_tick
    if (
        divergence.structural_level != center.structural_level
        or divergence.source_kind is not center.source_kind
        or divergence.price_basis_revision != center.price_basis_revision
        or divergence.signal_leg_unit_ids[0] != signal.unit_id
        or divergence.direction != signal.direction
        or not divergence.is_divergent
    ):
        raise ValueError("center divergence closure evidence does not match its leave")
    if divergence.available_at < center.available_at:
        raise ValueError("divergence closure cannot precede center availability")
    width = divergence.comparison_width
    if width == 1:
        if (
            divergence.signal_unit_id != signal.unit_id
            or divergence.anchor_at != signal.market_end
            or divergence.anchor_tick != signal_extreme
        ):
            raise ValueError("one-unit divergence must anchor at the raw leave")
        pending_leave = signal
        completion_leave = None
        completion_return = None
        completed_at = None
    else:
        completion_leave = center.completion_leave_unit
        completion_return = center.completion_return_unit
        if (
            completion_leave != signal
            or completion_return is None
            or divergence.signal_leg_unit_ids[:2]
            != (completion_leave.unit_id, completion_return.unit_id)
        ):
            raise ValueError(
                "three-unit divergence requires the center leave and outside return"
            )
        pending_leave = None
        completed_at = center.completed_at
    evidence_units = (
        *(() if center.entry_unit is None else (center.entry_unit,)),
        *center.body_units,
        *center.failed_departure_units,
    )
    available_at = max(
        divergence.available_at,
        signal.available_at,
        *(item.available_at for item in evidence_units),
    )
    return replace(
        center,
        state=CenterState.DIVERGENCE_CLOSED,
        pending_leave_unit=pending_leave,
        completion_leave_unit=completion_leave,
        completion_return_unit=completion_return,
        completed_at=completed_at,
        available_at=available_at,
        boundary_divergence_id=divergence.divergence_id,
        boundary_anchor_unit_id=divergence.signal_unit_id,
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
    """返回两个相邻单元是否不能前后连接。

    线段永远有方向，相邻线段必然一上一下，所以线段层保持严格交替。

    走势类型也由相接端点决定方向；盘整是走势分类，不是无方向连接件。
    相邻同向走势必须先按结合律合并成一个走势，再做同级别递归。
    ``oscillatory_ids`` 只为旧回放接口兼容保留，不再豁免方向约束。
    """

    # Every formal movement has an endpoint direction, including a
    # consolidation.  The argument remains for replay/API compatibility only;
    # it must never exempt a movement from strict up/down alternation.
    del oscillatory_ids
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
    """返回单元闭区间是否与中枢核心闭区间相交。"""

    return max(item.low_tick, zd_tick) <= min(item.high_tick, zg_tick)


def _positive_overlap(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
) -> bool:
    """返回单元与中枢核心是否存在正宽度交集。"""

    return max(item.low_tick, zd_tick) < min(item.high_tick, zg_tick)


def _overlaps_core(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
    source_kind: SourceKind,
) -> bool:
    """应用与来源类型对应的重叠规则。

    线段/笔中枢要求正宽度重叠。递归输入是已完成的低级别走势类型，保留原始
    闭区间规则；一个价格跳动点上的相等也构成有效中枢边界。
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
    """首次回返只有越过相应边界才算重新进入中枢。

    严格策略规则下，等于边界仍算在外：``low >= ZG`` 为三买，
    ``high <= ZD`` 为三卖。
    """

    return (
        ret.low_tick < zg_tick if leave.direction == "up" else ret.high_tick > zd_tick
    )


def _outside_in_direction(
    item: ConstituentUnit,
    zd_tick: int,
    zg_tick: int,
) -> bool:
    return (
        item.end_tick > zg_tick if item.direction == "up" else item.end_tick < zd_tick
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
    """返回一个重叠单元是否最终收在任一核心边界之外。"""

    return _touches_core(
        item,
        center.zd_tick,
        center.zg_tick,
    ) and _outside_in_direction(item, center.zd_tick, center.zg_tick)


def _seed_size(source_kind: SourceKind) -> int:
    """返回 ``establish_center`` 消耗的候选窗口宽度。

    物理线段/笔中枢使用五角色窗口：进入段 + 中间三段核心 + 独立离开段。
    五段都必须与冻结核心正宽重叠。递归中枢仍由三个已完成的低级别走势
    类型构成，不能把物理线段直接冒充高一级走势。
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
        entry_unit_id=(None if entry_unit is None else entry_unit.unit_id),
        initial_unit_ids=tuple(item.unit_id for item in initial_units),
        establishment_leave_unit_id=(
            None
            if establishment_leave_unit is None
            else establishment_leave_unit.unit_id
        ),
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
        establishment_leave = None
        core_units = values
        evidence = values if seed_entry is None else (seed_entry,) + values
    else:
        seed_entry = values[0]
        core_units = values[1:4]
        establishment_leave = values[4]
        evidence = values
    if source_kind is SourceKind.TREND_TYPE and seed_entry is not None:
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if not seed_entry.locked:
            return None
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
    extension_units = ()
    zd_tick, zg_tick = _core(core_units)
    if (
        zd_tick > zg_tick
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else zd_tick >= zg_tick
    ):
        return None
    if any(
        not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in core_units
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE:
        if (
            not _positive_overlap(seed_entry, zd_tick, zg_tick)
            or not _positive_overlap(establishment_leave, zd_tick, zg_tick)
            or not _outside_in_direction(establishment_leave, zd_tick, zg_tick)
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
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    entry_unit: ConstituentUnit | None = None,
) -> CenterPreview | None:
    """构建进入、本体和离开角色相互分离的临时证据。"""

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
        establishment_leave = None
        core_units = values
        body_units = core_units
        evidence = values if seed_entry is None else (seed_entry,) + values
    else:
        seed_entry = values[0]
        core_units = values[1:4]
        establishment_leave = values[4]
        body_units = core_units
        evidence = values
    if source_kind is SourceKind.TREND_TYPE and seed_entry is not None:
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
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
        not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in core_units
    ):
        return None
    if source_kind is not SourceKind.TREND_TYPE:
        if (
            not _positive_overlap(seed_entry, zd_tick, zg_tick)
            or not _positive_overlap(establishment_leave, zd_tick, zg_tick)
            or not _outside_in_direction(establishment_leave, zd_tick, zg_tick)
        ):
            return None
    return CenterPreview(
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=price_basis_revision,
        entry_unit_id=(None if seed_entry is None else seed_entry.unit_id),
        unit_ids=tuple(item.unit_id for item in body_units),
        state=CenterPreviewState.FORMING,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in evidence),
        pending_leave_unit_id=(
            None if establishment_leave is None else establishment_leave.unit_id
        ),
        establishment_leave_unit_id=(
            None if establishment_leave is None else establishment_leave.unit_id
        ),
    )


def _advance_center_preview_lifecycle(
    preview: CenterPreview,
    initial_units,
    following_units,
    oscillatory_ids: frozenset[str] = frozenset(),
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
        item.available_at
        for item in (
            *((entry,) if entry is not None else ()),
            *body,
            *failed_departures,
            *((pending,) if pending else ()),
        )
    )
    occupied_ids = {item.unit_id for item in body}
    occupied_ids.update(item.unit_id for item in failed_departures)
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
                    failed_departure_unit_ids=tuple(
                        value.unit_id for value in failed_departures
                    ),
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
                # 回返越回核心时，旧离开立即失败。若同一回返最终越过另一侧边界，
                # 它同时成为下一段外部离开，而不是被并入中枢本体。
                failed_departures.append(pending)
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

        if _outside_in_direction(item, preview.zd_tick, preview.zg_tick):
            if not _touches_core(item, preview.zd_tick, preview.zg_tick):
                return None
            pending = item
        else:
            if not _overlaps_core(
                item,
                preview.zd_tick,
                preview.zg_tick,
                preview.source_kind,
            ):
                return None
            body.append(item)
        occupied_ids.add(item.unit_id)

    return replace(
        preview,
        unit_ids=tuple(value.unit_id for value in body),
        failed_departure_unit_ids=tuple(value.unit_id for value in failed_departures),
        available_at=available_at,
        pending_leave_unit_id=(None if pending is None else pending.unit_id),
    )


def _project_ongoing_center_preview(
    center: TrendCenter,
    following_units,
    oscillatory_ids: frozenset[str] = frozenset(),
) -> CenterPreview | None:
    """使用临时单元投影一个已正式成立但仍在进行的中枢。"""

    if center.state is not CenterState.ONGOING:
        return None
    following = tuple(following_units)
    if not following or all(item.locked for item in following):
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
        entry_unit_id=(
            None if center.entry_unit is None else center.entry_unit.unit_id
        ),
        unit_ids=tuple(item.unit_id for item in center.body_units),
        state=CenterPreviewState.FORMING,
        zd_tick=center.zd_tick,
        zg_tick=center.zg_tick,
        available_at=center.available_at,
        failed_departure_unit_ids=tuple(
            item.unit_id for item in center.failed_departure_units
        ),
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
    )
    try:
        return _advance_center_preview_lifecycle(
            preview,
            known,
            following,
            oscillatory_ids,
        )
    except ValueError as exc:
        if str(exc) != "preview transition must be connected and alternating":
            raise
        # The locked replay may have stopped at a geometry boundary before the
        # live suffix.  In that case the suffix is not adjacent to this center
        # and therefore cannot be projected as its lifecycle.  It remains
        # eligible for an independently scanned preview above.
        return None


def _preview_matches_center_seed(
    preview: CenterPreview,
    center: TrendCenter,
) -> bool:
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
    """记录已证伪离开；只有真正回到核心的单元进入中枢本体。"""

    leave = center.pending_leave_unit
    if leave is None:
        raise ValueError("failed departure fold requires a pending leave")
    crossed_opposite_boundary = _outside_in_direction(
        ret,
        center.zd_tick,
        center.zg_tick,
    )
    failed_departures = center.failed_departure_units + (leave,)
    returned_to_body = () if crossed_opposite_boundary else (ret,)
    extension_units = center.extension_units + returned_to_body
    body_units = center.initial_units + extension_units
    updated = replace(
        center,
        body_units=body_units,
        extension_units=extension_units,
        failed_departure_units=failed_departures,
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


def _supersede_center(
    center: TrendCenter,
    successor: TrendCenter,
    bridge_units: tuple[ConstituentUnit, ...],
) -> tuple[TrendCenter, CenterEvent]:
    """Close a center only after a locked disjoint successor is established."""

    if center.state is not CenterState.ONGOING:
        raise ValueError("only an ongoing center can be superseded")
    pending = center.pending_leave_unit
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
    return updated, _event(
        updated,
        CenterEventKind.SUPERSEDED,
        successor.established_market_time,
        updated.available_at,
    )


def advance_center(
    center: TrendCenter,
    item: ConstituentUnit,
    oscillatory_ids: frozenset[str] = frozenset(),
) -> tuple[TrendCenter, CenterEvent]:
    if center.state is not CenterState.ONGOING:
        if center.state is CenterState.COMPLETED:
            raise ValueError("completed center cannot transition")
        raise ValueError("closed center cannot transition")
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

    # 离开单元只需要从本体边界向外收盘；若恰从 ZD/ZG 起步，它与核心只有
    # 零宽度接触，也仍是有效离开观察，而不是要求未来数据重选中枢。
    if _is_leave_candidate(center, item):
        return _watch_external_leave(center, item)
    if not _overlaps_core(
        item,
        center.zd_tick,
        center.zg_tick,
        center.source_kind,
    ):
        raise ValueError("ongoing center unit must re-enter the core")
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
    establishment_leave = None
    if source_kind is SourceKind.TREND_TYPE:
        seed_entry = entry_unit
        body = values
        core_ready = len(values) >= 3
        evidence = values if seed_entry is None else (seed_entry,) + values
    else:
        seed_entry = values[0]
        body = values[1:4]
        core_ready = len(values) >= 4
        establishment_leave = values[4] if len(values) == 5 else None
        evidence = values
    if source_kind is SourceKind.TREND_TYPE and seed_entry is not None:
        _validate_seed_context(
            (seed_entry,) + values,
            structural_level,
            source_kind,
        )
        if _conflicting_pair(seed_entry, values[0], oscillatory_ids):
            return None
    pending_leave = None

    zd_tick = None
    zg_tick = None
    state = CenterPreviewState.FORMING
    if core_ready:
        core_units = tuple(body[:3])
        zd_tick, zg_tick = _core(core_units)
        if zd_tick > zg_tick:
            return None
        if zd_tick == zg_tick and source_kind is not SourceKind.TREND_TYPE:
            state = CenterPreviewState.TOUCH_ONLY
        if state is CenterPreviewState.TOUCH_ONLY:
            # 零宽度交集只作为诊断观察。单元至少必须包含共享边界；这种情况永远
            # 不会提升为正式中枢，图表门槛也不会绘制它。
            if any(
                item.low_tick > zd_tick or item.high_tick < zg_tick for item in body
            ):
                return None
        elif any(
            not _overlaps_core(item, zd_tick, zg_tick, source_kind) for item in body
        ):
            return None
        if source_kind is not SourceKind.TREND_TYPE:
            if state is CenterPreviewState.TOUCH_ONLY:
                if not _touches_core(seed_entry, zd_tick, zg_tick):
                    return None
                if establishment_leave is not None and not _touches_core(
                    establishment_leave, zd_tick, zg_tick
                ):
                    return None
            else:
                if not _positive_overlap(seed_entry, zd_tick, zg_tick):
                    return None
                if establishment_leave is not None and not _positive_overlap(
                    establishment_leave, zd_tick, zg_tick
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
        entry_unit_id=(None if seed_entry is None else seed_entry.unit_id),
        unit_ids=tuple(item.unit_id for item in body),
        state=state,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        available_at=max(item.available_at for item in evidence),
        pending_leave_unit_id=(
            None if pending_leave is None else pending_leave.unit_id
        ),
        establishment_leave_unit_id=(
            None if establishment_leave is None else establishment_leave.unit_id
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
    """返回扫描器种子所拥有的外部进入段。

    物理五角色窗口自身已经包含进入段，因此返回 ``None``。递归中枢从三个
    已完成走势类型的核心开始，紧邻前一走势保留为外部进入证据，但不进入身份。
    """

    # ``calculate_centers`` 探测空尾部或尚未预热的尾部时也会调用本函数。此类
    # 尾部没有可审计外部进入段，应当不产生预览，而不是索引不存在的流单元。
    if SourceKind(source_kind) is not SourceKind.TREND_TYPE:
        return None
    return values[start - 1] if 0 < start <= len(values) else None


def _next_scan_start_after_completion(
    completion_return_offset: int,
    source_kind: SourceKind,
) -> int:
    """把已完成中枢的离开段作为下一进入段继续扫描。

    物理种子从完成离开段重新作为下一进入段开始；递归种子从完成回返开始，
    并由扫描器取前一走势作为进入证据。
    """

    if completion_return_offset <= 0:
        raise ValueError("completion return must follow a leave unit")
    return (
        completion_return_offset
        if SourceKind(source_kind) is SourceKind.TREND_TYPE
        else completion_return_offset - 1
    )


def _first_disjoint_successor_seed(
    values: tuple[ConstituentUnit, ...],
    start: int,
    center: TrendCenter,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str],
) -> tuple[int, TrendCenter] | None:
    """查找旧中枢之外最早锁定的来源特定成立窗口。

    搜索从首个无法推进旧中枢的单元开始，不把旧核心末段伪造成离开段。
    物理五角色或递归三走势尚未全部锁定前，旧中枢继续保持进行中。
    """

    width = _seed_size(source_kind)
    for candidate_start in range(start, len(values) - width + 1):
        candidate = establish_center(
            values[candidate_start : candidate_start + width],
            structural_level,
            source_kind,
            oscillatory_ids,
            entry_unit=_scanner_entry(values, candidate_start, source_kind),
        )
        if candidate is None:
            continue
        if candidate.zd_tick > center.zg_tick or candidate.zg_tick < center.zd_tick:
            return candidate_start, candidate
    return None


def _first_successor_preview(
    values: tuple[ConstituentUnit, ...],
    resume_from: int,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str],
) -> CenterPreview | None:
    """返回预览完成后首个因果实时中枢。

    三个同级单元的候选核心一旦具备重叠就可见；锁定后直接提升为正式中枢。
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
    """在临时三类点完成后串联后续中枢预览。

    已完成中枢的离开段会共享为下一物理中枢的进入段。实时边缘必须重复此步骤：
    一个临时中枢可能已经完成，而第二个中枢只有四个单元，尚无第五成熟段。
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


def calculate_centers(
    units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    hard_boundary_after_ids: frozenset[str] = frozenset(),
    boundary_closed_centers: tuple[TrendCenter, ...] = (),
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

    hard_boundaries = frozenset(hard_boundary_after_ids)
    closed_by_anchor = {}
    for center in tuple(boundary_closed_centers):
        anchor_id = center.boundary_anchor_unit_id
        if (
            center.state is not CenterState.DIVERGENCE_CLOSED
            or anchor_id is None
            or anchor_id not in hard_boundaries
        ):
            raise ValueError(
                "boundary-closed centers must match declared hard boundaries"
            )
        previous = closed_by_anchor.setdefault(anchor_id, center)
        if previous != center:
            raise ValueError("hard boundary center evidence conflicts")
    if set(closed_by_anchor) != set(hard_boundaries):
        raise ValueError("every hard boundary requires its closed center snapshot")
    if hard_boundaries:
        offsets = {item.unit_id: index for index, item in enumerate(values)}
        if len(offsets) != len(values):
            raise ValueError("unit ids must be unique")
        missing = hard_boundaries.difference(offsets)
        if missing:
            raise ValueError("hard center boundary references a missing unit")
        if any(offsets[item] >= locked_count for item in hard_boundaries):
            raise ValueError("hard center boundary requires a locked unit")

        boundary_offsets = tuple(sorted(offsets[item] for item in hard_boundaries))
        starts = (0, *(offset + 1 for offset in boundary_offsets))
        ends = (*(offset + 1 for offset in boundary_offsets), len(values))
        centers: list[TrendCenter] = []
        events_by_id: dict[str, CenterEvent] = {}
        previews: tuple[CenterPreview, ...] = ()
        replay_from = starts[-1]
        for partition_index, (start, end) in enumerate(zip(starts, ends)):
            partition = values[start:end]
            if not partition:
                continue
            partition_ids = {item.unit_id for item in partition}
            result = calculate_centers(
                partition,
                structural_level,
                source_kind,
                frozenset(item for item in oscillatory_ids if item in partition_ids),
            )
            final_partition = partition_index == len(starts) - 1
            if final_partition:
                retained = result.centers
            else:
                boundary_center = closed_by_anchor[partition[-1].unit_id]
                matches = tuple(
                    center
                    for center in result.centers
                    if center.center_id == boundary_center.center_id
                )
                if len(matches) != 1:
                    raise ValueError(
                        "hard boundary center is missing from causal partition replay"
                    )
                live = matches[0]
                if boundary_center.pending_leave_unit is not None:
                    lifecycle_matches = (
                        live.state is CenterState.ONGOING
                        and live.pending_leave_unit
                        == boundary_center.pending_leave_unit
                        and live.completion_leave_unit is None
                        and live.completion_return_unit is None
                    )
                else:
                    lifecycle_matches = (
                        live.state is CenterState.COMPLETED
                        and live.pending_leave_unit is None
                        and live.completion_leave_unit
                        == boundary_center.completion_leave_unit
                        and live.completion_return_unit
                        == boundary_center.completion_return_unit
                        and live.completed_at == boundary_center.completed_at
                    )
                if (
                    not lifecycle_matches
                    or live.entry_unit != boundary_center.entry_unit
                    or live.body_units != boundary_center.body_units
                    or live.failed_departure_units
                    != boundary_center.failed_departure_units
                ):
                    raise ValueError(
                        "hard boundary center changed during causal partition replay"
                    )
                retained = tuple(
                    boundary_center
                    if center.center_id == boundary_center.center_id
                    else center
                    for center in result.centers
                    if center.structurally_closed
                    or center.center_id == boundary_center.center_id
                )
            retained_ids = {center.center_id for center in retained}
            centers.extend(retained)
            for event in result.events:
                if event.center_id not in retained_ids:
                    continue
                previous = events_by_id.setdefault(event.event_id, event)
                if previous != event:
                    raise ValueError("center event id maps to conflicting evidence")
            if final_partition:
                previews = result.previews
                replay_from = start + result.replay_from

        if len({center.center_id for center in centers}) != len(centers):
            raise ValueError("partitioned center identities must be unique")
        return CenterLevelResult(
            structural_level=structural_level,
            price_basis_revision=price_basis_revision,
            centers=tuple(centers),
            previews=previews,
            events=tuple(events_by_id.values()),
            locked_unit_count=locked_count,
            replay_from=replay_from,
        )

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
        geometry_stop_at = None
        successor_start = None
        while j < len(formal):
            try:
                center, event = advance_center(center, formal[j], oscillatory_ids)
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
                formal,
                geometry_stop_at,
                center,
                structural_level,
                source_kind,
                oscillatory_ids,
            )
            if successor_seed is not None:
                successor_start, successor = successor_seed
                bridge_units = (
                    *(
                        ()
                        if center.pending_leave_unit is None
                        else (center.pending_leave_unit,)
                    ),
                    *formal[geometry_stop_at:successor_start],
                )
                center, superseded_event = _supersede_center(
                    center,
                    successor,
                    bridge_units,
                )
                candidate_events.append(superseded_event)
        # 来源特定的成立窗口锁定后（物理层五角色、递归层三走势），中枢身份
        # 已经成立；后续单元只能推进或停止生命周期，不能用更晚窗口追溯替换。
        # 几何停止时仍保留这个因果前缀快照。
        centers.append(center)
        events.extend(candidate_events)
        replay_from = i
        if center.state is CenterState.COMPLETED:
            # 外部离开段可同时作为下一中枢进入段。扫描应回退到足以验证该共享边界
            # 种子的位置；若种子失败，下一轮会正常向前滑动。
            i = max(
                i + 1,
                _next_scan_start_after_completion(j, source_kind),
            )
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
                index
                for index, item in enumerate(values)
                if item.unit_id == completion_return.unit_id
            )
            post_completion_resume = _next_scan_start_after_completion(
                completion_return_offset,
                source_kind,
            )
        # 有效核心可以在最后一个临时单元之前结束。扫描所有与未锁定后缀相交的
        # 窗口，但绝不能回扫进已完成中枢。三类点之后，首个可行预览按与正式
        # 扫描器相同的因果规则拥有该后缀。
        first_live_start = max(
            0,
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
                # 几何已完成候选比更晚的重叠形成中种子具有更强生命周期证据，
                # 避免用更短的实时边缘窗口替换可见三类点完成状态。
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
            # 正式进行中中枢拥有整个临时后缀。来源特定的偏移种子在单元锁定前只是
            # 另一种划分；即使偏移窗口在几何上已经“完成”，只要原中枢仍能合法吸收
            # 整个实时后缀，它就仍是原中枢内部的滑动子窗口，不能被提升为第二个中枢。
            # SH.601059 曾因此同时显示前一 ongoing 中枢和一个与其价格核心重叠的
            # completed 预览。采用唯一活动归属：能投影时始终保留原 center_id 的
            # 投影；不能投影时，在锁定前缀明确完成或取代旧中枢前不暴露偏移候选。
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
                or _preview_matches_center_seed(
                    latest_live_preview,
                    active_owner,
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
            0,
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
