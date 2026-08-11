from __future__ import annotations

from dataclasses import replace

from chanlun.core.strict_structure.models import (
    CenterState,
    StrictStructureResult,
    TrendKind,
)
from chanlun.core.strict_structure.strength import (
    MacdStrengthUnavailable,
    center_departure_comparison_leg,
    center_entry_comparison_leg,
    compare_comparison_legs,
)


def _center_consolidation_comparison_legs(
    center,
    units,
    *,
    allowed_states,
    not_before_unit_id: str | None = None,
):
    if center.state not in allowed_states:
        return None
    entry = center_entry_comparison_leg(
        center,
        units,
        not_before_unit_id=not_before_unit_id,
    )
    departure = (
        None
        if entry is None
        else center_departure_comparison_leg(center, units, width=entry.width)
    )
    if (
        entry is None
        or departure is None
        or entry.measurement_unit.direction
        != departure.measurement_unit.direction
        or entry.measurement_unit.market_end
        > departure.measurement_unit.market_start
    ):
        return None
    return entry, departure


def center_consolidation_comparison_legs(
    center,
    units,
    *,
    not_before_unit_id: str | None = None,
):
    """返回单中枢盘整背驰使用的同宽进入段与离开段。"""

    return _center_consolidation_comparison_legs(
        center,
        units,
        allowed_states=frozenset(
            {CenterState.ONGOING, CenterState.COMPLETED}
        ),
        not_before_unit_id=not_before_unit_id,
    )


def _compare_center_consolidation_divergence(
    center,
    units,
    strength,
    *,
    allowed_states,
    movement_start_unit_id: str | None = None,
):
    source_units = tuple(units)
    pair = _center_consolidation_comparison_legs(
        center,
        source_units,
        allowed_states=allowed_states,
        not_before_unit_id=movement_start_unit_id,
    )
    if pair is None:
        return None
    evidence = compare_comparison_legs(*pair, strength, kind="consolidation")

    # 离开段不但要越过进入段极值，还必须创出整段盘整走势的新极值；否则
    # 只能视为中枢内部震荡，不能冻结走势边界或生成一类点。
    unit_index = {item.unit_id: index for index, item in enumerate(source_units)}
    start_id = movement_start_unit_id or pair[0].units[0].unit_id
    start_index = unit_index.get(start_id)
    terminal = pair[1].terminal_unit
    terminal_index = unit_index.get(terminal.unit_id)
    if (
        start_index is None
        or terminal_index is None
        or start_index >= terminal_index
    ):
        raise ValueError("盘整背驰区间不在同级别单元序列中")
    prior_units = source_units[start_index:terminal_index]
    makes_movement_extreme = (
        terminal.high_tick > max(item.high_tick for item in prior_units)
        if terminal.direction == "up"
        else terminal.low_tick < min(item.low_tick for item in prior_units)
    )
    if not makes_movement_extreme:
        evidence = replace(evidence, price_extreme_confirmed=False)
    return evidence


def compare_center_consolidation_divergence(
    center,
    units,
    strength,
    *,
    movement_start_unit_id: str | None = None,
):
    """比较单中枢的进入段与离开段，并返回正式盘整背驰证据。"""

    return _compare_center_consolidation_divergence(
        center,
        units,
        strength,
        allowed_states=frozenset(
            {CenterState.ONGOING, CenterState.COMPLETED}
        ),
        movement_start_unit_id=movement_start_unit_id,
    )


def collect_strict_divergences(structure, strength):
    if not isinstance(structure, StrictStructureResult):
        raise TypeError("structure must be a StrictStructureResult")
    by_id = {}
    for level in structure.levels:
        trend_center_ids = {
            center.center_id
            for trend in (*level.trend_types, *level.completed_trends)
            if trend.kind is TrendKind.TREND
            for center in trend.centers
        }
        for boundary in level.decomposition_boundaries:
            evidence = boundary.divergence
            previous = by_id.setdefault(evidence.divergence_id, evidence)
            if previous != evidence:
                raise ValueError("divergence id maps to conflicting evidence")

        for center in level.center_result.centers:
            # 两个分离中枢已经组成趋势时，只保留趋势末端的 A/C 比较；不能再给
            # 其中的单个中枢重复标注盘整背驰，否则同一段走势会有两种分类。
            if center.center_id in trend_center_ids:
                continue
            try:
                evidence = compare_center_consolidation_divergence(
                    center,
                    level.units,
                    strength,
                )
            except MacdStrengthUnavailable:
                continue
            if evidence is not None and evidence.is_divergent:
                previous = by_id.setdefault(evidence.divergence_id, evidence)
                if previous != evidence:
                    raise ValueError("divergence id maps to conflicting evidence")

        for trend in level.completed_trends:
            evidence = trend.terminal_divergence
            if evidence is None or not evidence.is_divergent:
                continue
            previous = by_id.setdefault(evidence.divergence_id, evidence)
            if previous != evidence:
                raise ValueError("divergence id maps to conflicting evidence")

    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (
                item.available_at,
                item.structural_level,
                item.kind,
                item.divergence_id,
            ),
        )
    )


def merge_formal_divergence_ledger(
    structure,
    confirmed_points,
    divergences=(),
):
    """汇总结构与买卖点中嵌入的全部正式背驰证据。"""

    by_id = {}

    def record(evidence) -> None:
        if evidence is None:
            return
        previous = by_id.setdefault(evidence.divergence_id, evidence)
        if previous != evidence:
            raise ValueError("divergence id maps to conflicting evidence")

    for evidence in divergences:
        record(evidence)
    for point in confirmed_points:
        record(point.divergence)
    for level in structure.levels:
        for trend in (*level.trend_types, *level.completed_trends):
            record(trend.terminal_divergence)
        for boundary in level.decomposition_boundaries:
            record(boundary.divergence)
    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (
                item.available_at,
                item.structural_level,
                item.kind,
                item.divergence_id,
            ),
        )
    )
