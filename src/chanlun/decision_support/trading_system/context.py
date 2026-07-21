from __future__ import annotations

from datetime import datetime

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    StructuralPoint,
    TimeframeContext,
)


def classify_context(
    *,
    frequency: str,
    current_direction: ContextDirection,
    points: tuple[StructuralPoint, ...],
    as_of: datetime,
) -> TimeframeContext:
    observed_at = normalize_datetime(as_of, "as_of")
    active = tuple(
        point
        for point in points
        if point.confirmed
        and point.confirmed_at is not None
        and point.available_at <= observed_at
    )
    dominant = max(
        active,
        key=lambda point: (
            point.recursive_level,
            point.available_at,
            point.point_id,
        ),
        default=None,
    )
    if dominant is None:
        disposition = "neutral"
        reasons = ("no_active_directional_point",)
    elif dominant.side == "sell" and current_direction == "down":
        disposition = "hostile"
        reasons = ("confirmed_sell_with_down_structure",)
    elif dominant.side == "buy":
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
