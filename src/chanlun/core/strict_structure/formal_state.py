from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chanlun.core.strict_structure.models import (
    CenterRelation,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
    TrendKind,
    TrendState,
    TrendType,
)
from chanlun.core.strict_structure.center_relation import classify_center_relation


FormalDirection = Literal["up", "down", "neutral"]


@dataclass(frozen=True, slots=True)
class _FormalEvidenceView:
    structure: StrictStructureResult
    confirmed_points: tuple
    source_closed_at: datetime


@dataclass(frozen=True, slots=True)
class FormalDirectionState:
    """当前可对外发布的正式走势方向及其证据来源。"""

    direction: FormalDirection
    structural_level: int | None
    trend_id: str | None
    support_point_id: str | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.direction not in {"up", "down", "neutral"}:
            raise ValueError("正式方向取值不受支持")
        if (self.structural_level is None) != (self.trend_id is None):
            raise ValueError("正式方向的走势来源不完整")
        if self.structural_level is not None and self.structural_level < 0:
            raise ValueError("正式方向级别不能为负数")
        if not self.reason_codes or len(self.reason_codes) != len(
            set(self.reason_codes)
        ):
            raise ValueError("正式方向原因不能为空且不能重复")


def _neutral(
    reason: str,
    *,
    structural_level: int | None = None,
    trend_id: str | None = None,
) -> FormalDirectionState:
    return FormalDirectionState(
        direction="neutral",
        structural_level=structural_level,
        trend_id=trend_id,
        support_point_id=None,
        reason_codes=(reason,),
    )


def _reversal_support(evidence, *, level, previous, current):
    current_direction = semantic_trend_direction(current)
    if current_direction is None:
        return None
    expected = {"1buy", "2buy"} if current_direction == "up" else {"1sell", "2sell"}
    candidates = tuple(
        point
        for point in evidence.confirmed_points
        if point.status is StrictPointStatus.CONFIRMED
        and point.structural_level == level.structural_level
        and point.point_type in expected
        and previous.market_end <= point.anchor_at <= current.market_end
        and point.available_at <= evidence.source_closed_at
    )
    return max(
        candidates,
        key=lambda point: (point.available_at, point.point_id),
        default=None,
    )


def _trend_reaches_tail(trend, units) -> bool:
    positions = {unit.unit_id: index for index, unit in enumerate(units)}
    terminal_index = positions.get(trend.terminal_unit.unit_id)
    if terminal_index is None:
        return False
    # 一个走势尾部之后最多允许保留“首次回返 + 当前未形成新中枢的一段”。超过
    # 两段仍未被该走势覆盖时，旧走势不能继续代表当前方向。
    return len(units) - 1 - terminal_index <= 2


def semantic_trend_direction(trend: TrendType) -> FormalDirection | None:
    """返回走势定义上的方向，不使用整段首尾净位移代替中枢关系。"""

    if not isinstance(trend, TrendType):
        raise TypeError("走势定义方向只能由 TrendType 计算")
    if trend.kind is TrendKind.CONSOLIDATION:
        return None
    relations = tuple(
        classify_center_relation(previous, current)
        for previous, current in zip(trend.centers, trend.centers[1:])
    )
    if not relations:
        raise ValueError("正式趋势必须至少包含两个中枢")
    if all(relation is CenterRelation.UP_TREND for relation in relations):
        return "up"
    if all(relation is CenterRelation.DOWN_TREND for relation in relations):
        return "down"
    raise ValueError("正式趋势的中枢关系方向不一致")


def _resolve_level_formal_direction(
    evidence: StrictEvidenceResult,
    level: StrictLevelResult,
) -> FormalDirectionState:
    """解析一个递归级别尾部的正式方向。"""

    if not level.units:
        return _neutral("formal_units_unavailable")
    if not level.trend_types:
        return _neutral("current_suffix_has_no_formal_trend")

    current = level.trend_types[-1]
    provenance = {
        "structural_level": level.structural_level,
        "trend_id": current.trend_id,
    }
    if not _trend_reaches_tail(current, level.units):
        return _neutral("current_suffix_is_outside_formal_trend", **provenance)
    if current.kind is TrendKind.CONSOLIDATION:
        return _neutral("current_formal_movement_is_consolidation", **provenance)
    if current.state is TrendState.LOCKED or current.terminal_divergence is not None:
        return _neutral("current_formal_trend_has_ended", **provenance)

    current_direction = semantic_trend_direction(current)
    if current_direction is None:
        raise ValueError("非盘整走势缺少正式方向")
    previous = next(
        (
            trend
            for trend in reversed(level.trend_types[:-1])
            if trend.kind is TrendKind.TREND
        ),
        None,
    )
    support = None
    previous_direction = (
        None if previous is None else semantic_trend_direction(previous)
    )
    if previous_direction is not None and previous_direction != current_direction:
        support = _reversal_support(
            evidence,
            level=level,
            previous=previous,
            current=current,
        )
        if support is None:
            return _neutral(
                "direction_change_lacks_first_or_second_point",
                **provenance,
            )

    return FormalDirectionState(
        direction=current_direction,
        structural_level=level.structural_level,
        trend_id=current.trend_id,
        support_point_id=None if support is None else support.point_id,
        reason_codes=(
            "current_directional_trend",
            *(
                ()
                if support is None
                else ("direction_change_supported_by_first_or_second_point",)
            ),
        ),
    )


def resolve_level_formal_direction(
    evidence: StrictEvidenceResult,
    structural_level: int,
) -> FormalDirectionState:
    """返回指定递归级别的正式方向，供图表与决策审计共同使用。"""

    if not isinstance(evidence, StrictEvidenceResult):
        raise TypeError("正式方向必须基于 StrictEvidenceResult 解析")
    if type(structural_level) is not int or structural_level < 0:
        raise ValueError("正式方向级别必须是非负整数")
    level = next(
        (
            item
            for item in evidence.structure.levels
            if item.structural_level == structural_level
        ),
        None,
    )
    if level is None:
        raise ValueError("正式方向级别不存在于严格结构中")
    return _resolve_level_formal_direction(evidence, level)


def resolve_formal_direction(
    evidence: StrictEvidenceResult | _FormalEvidenceView,
) -> FormalDirectionState:
    """解析当前唯一的正式方向，不把历史盘整净位移冒充为趋势。

    结构层可以保留供递归使用的几何 ``up/down`` 端点方向，但对外方向必须同时
    满足四项条件：属于当前时间尾部、尾部被一个尚未结束的趋势覆盖、走势类型
    不是盘整；若相对上一条同级有向趋势发生反转，还必须存在同级一类或小转大
    二类点作为支撑。
    """

    levels = tuple(evidence.structure.levels)
    visible_units = tuple(unit for level in levels for unit in level.units)
    if not visible_units:
        return _neutral("formal_units_unavailable")
    current_market_end = max(unit.market_end for unit in visible_units)

    for level in reversed(levels):
        if not level.units or level.units[-1].market_end != current_market_end:
            continue
        return _resolve_level_formal_direction(evidence, level)

    return _neutral("no_formal_level_reaches_current_suffix")


def resolve_formal_direction_from_components(
    *,
    structure: StrictStructureResult,
    confirmed_points,
    source_closed_at: datetime,
) -> FormalDirectionState:
    """Resolve direction without constructing a revision-hashed evidence snapshot."""

    if not isinstance(structure, StrictStructureResult):
        raise TypeError("formal direction structure must be StrictStructureResult")
    if (
        not isinstance(source_closed_at, datetime)
        or source_closed_at.tzinfo is None
        or source_closed_at.utcoffset() is None
    ):
        raise ValueError("formal direction source close must be timezone-aware")
    view = _FormalEvidenceView(
        structure=structure,
        confirmed_points=tuple(confirmed_points),
        source_closed_at=source_closed_at,
    )
    return resolve_formal_direction(view)


def current_formal_direction(evidence: StrictEvidenceResult) -> FormalDirection:
    """返回供选股、监听和回测共同使用的当前正式方向。"""

    return resolve_formal_direction(evidence).direction


def current_formal_direction_from_components(
    *,
    structure: StrictStructureResult,
    confirmed_points,
    source_closed_at: datetime,
) -> FormalDirection:
    return resolve_formal_direction_from_components(
        structure=structure,
        confirmed_points=confirmed_points,
        source_closed_at=source_closed_at,
    ).direction
