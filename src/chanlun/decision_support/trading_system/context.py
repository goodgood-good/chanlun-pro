from __future__ import annotations

from datetime import datetime, timedelta

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    StructuralPoint,
    TimeframeContext,
)


_CONTEXT_POINT_MAX_AGE = {
    "1m": timedelta(days=1),
    "5m": timedelta(days=4),
    "30m": timedelta(days=30),
    "d": timedelta(days=180),
}


def context_point_max_age(frequency: str) -> timedelta:
    """返回不同物理周期方向点可影响当前上下文的最长时间。"""

    try:
        return _CONTEXT_POINT_MAX_AGE[frequency]
    except KeyError as exc:
        raise ValueError("unsupported context frequency") from exc


def classify_context(
    *,
    frequency: str,
    current_direction: ContextDirection,
    points: tuple[StructuralPoint, ...],
    as_of: datetime,
) -> TimeframeContext:
    observed_at = normalize_datetime(as_of, "as_of")
    visible = tuple(
        point
        for point in points
        if point.confirmed
        and point.confirmed_at is not None
        and point.available_at <= observed_at
    )
    cutoff = observed_at - context_point_max_age(frequency)
    # 一个点要影响当前上下文，形态锚点和首次可用时间都必须仍在活动窗口内。
    # 这可以阻止很早形成、很晚才被递归确认的结构被误当成当前反转信号。
    fresh = tuple(
        point
        for point in visible
        if point.anchor_at >= cutoff and point.available_at >= cutoff
    )
    latest_by_level: dict[tuple[str, int], StructuralPoint] = {}
    for point in fresh:
        lane = (point.tower, point.recursive_level)
        previous = latest_by_level.get(lane)
        if previous is None or (point.available_at, point.point_id) > (
            previous.available_at,
            previous.point_id,
        ):
            latest_by_level[lane] = point
    active = tuple(latest_by_level.values())
    dominant = max(
        active,
        key=lambda point: (
            point.available_at,
            point.recursive_level,
            point.point_id,
        ),
        default=None,
    )
    if dominant is None:
        disposition = "neutral"
        reasons = (
            ("directional_points_expired",)
            if visible
            else ("no_active_directional_point",)
        )
    elif dominant.side == "sell" and current_direction == "down":
        disposition = "hostile"
        reasons = ("confirmed_sell_with_down_structure",)
    elif dominant.side == "buy" and current_direction != "down":
        disposition = "supportive"
        reasons = ("confirmed_buy_structure",)
    else:
        disposition = "neutral"
        reasons = ("mixed_or_transition_structure",)
    return TimeframeContext(
        frequency=frequency,
        direction=current_direction,
        disposition=disposition,
        hard_block=disposition == "hostile",
        dominant_point_id=None if dominant is None else dominant.point_id,
        dominant_point_type=None if dominant is None else dominant.point_type,
        reason_codes=reasons,
        observed_at=observed_at,
    )
