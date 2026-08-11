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
            raise ValueError("next center cannot precede the previous completion leave")

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


def _group_is_divergence_complete(group, constituent_units, divergence):
    terminal_center = group[-1]
    leave = terminal_center.lifecycle_leave_unit
    unit_ids = tuple(item.unit_id for item in constituent_units)
    signal_ids = divergence.signal_leg_unit_ids
    if len(signal_ids) > len(unit_ids):
        return False
    return (
        len(group) >= 1
        and all(center.state is CenterState.COMPLETED for center in group[:-1])
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
            terminal_divergence_id=(
                None
                if terminal_divergence is None
                else terminal_divergence.divergence_id
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
    )


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
        if len(group) >= 2:
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
                    raise ValueError(
                        "盘整背驰的离开段末端不在同级别单元序列中"
                    )
                compared = (divergence, signal)
    except (MacdStrengthUnavailable, KeyError):
        # 稀疏回放或测试强度表可能只保存目标趋势的 MACD 切片；缺失的单中枢
        # 切片与正式提供者抛出的 MacdStrengthUnavailable 语义相同。
        return None
    if compared is None:
        return None
    divergence, signal = compared
    if not divergence.is_divergent:
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
        raise ValueError("divergence trend requires completed prior centers")
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
        0
        if group_start_unit_id is None
        else index.get(group_start_unit_id, -1)
    )
    if not 0 <= group_start <= index[values[0].entry_unit.unit_id]:
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
        if current.state is not CenterState.COMPLETED:
            # 实时中枢仍可能被后续五段窗口替换。它可以开启形成中边界，但在自身
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
            terminal_return = group[-1].completion_return_unit
            if terminal_return is None:
                raise ValueError("complete trend requires terminal return")
            # 前一中枢的完成回返是下一个五段中枢最早的来源单元；这样既保持递归
            # 走势单元相邻，也不会重复使用上一离开段。
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
