"""筛选与归档校验共用的冻结成对预热策略。

实时扫描器会比较完整可用前缀产生的语义活动尾部，与去掉最早三分之一后
产生的尾部。生产者以及所有判断快照是否适合前向模拟归档的消费者，都必须
使用相同常量和数量关系。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    StructuralPoint,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)


SCREENING_WARMUP_FREQUENCIES = ("d", "30m", "5m", "1m")
SCREENING_QMT_30M_FALLBACK_REASON_CODE = (
    "QMT_NATIVE_30M_INVALID_RESAMPLED_FROM_COMPLETED_5M"
)
SCREENING_WARMUP_DIFFERENCE_CODES = frozenset(
    {
        "WARMUP_DIRECTION_CHANGED",
        "WARMUP_ACTIVE_POINT_LANES_CHANGED",
        "WARMUP_POINT_STATUS_CHANGED",
        "WARMUP_POINT_TIMING_CHANGED",
        "WARMUP_PRICE_OR_BOUNDARY_CHANGED",
        "WARMUP_STRUCTURE_IDENTITY_CHANGED",
        "WARMUP_POINT_EVIDENCE_CHANGED",
        "WARMUP_OTHER_SEMANTIC_CHANGED",
    }
)
SCREENING_WARMUP_REQUIRED_BARS: Mapping[str, int] = MappingProxyType(
    {"d": 480, "30m": 480, "5m": 960, "1m": 1440}
)
# This is the production data-admission floor.  It is deliberately distinct
# from the stricter convergence history above: a 5m sell may still be needed
# for risk reduction when the optional convergence comparison is unavailable,
# while a feed shorter than this floor cannot publish any structural decision.
SCREENING_MINIMUM_BARS_BY_FREQUENCY: Mapping[str, int] = MappingProxyType(
    {"d": 240, "30m": 240, "5m": 480, "1m": 960}
)
SCREENING_WARMUP_DAYS_BY_FREQUENCY: Mapping[str, int] = MappingProxyType(
    {"1m": 30, "5m": 120, "30m": 365}
)
SCREENING_CANONICAL_REQUEST_BARS: Mapping[str, int] = MappingProxyType(
    {"d": 1600, "30m": 4000, "5m": 12000, "1m": 12000}
)


def expected_screening_warmup_suffix_bar_count(full_bar_count: int) -> int:
    """返回 ``frame.iloc[len//3:]`` 产生的精确后缀长度。"""

    if type(full_bar_count) is not int or full_bar_count <= 0:
        raise ValueError("warmup full bar count must be a positive integer")
    return full_bar_count - full_bar_count // 3


def screening_warmup_reason_code(
    *,
    frequency: str,
    converged: bool,
    full_bar_count: int,
    suffix_bar_count: int,
) -> str:
    """校验一次活动门控测量并返回规范原因码。"""

    if frequency not in SCREENING_WARMUP_REQUIRED_BARS:
        raise ValueError("unsupported screening warmup frequency")
    if type(converged) is not bool:
        raise ValueError("warmup convergence must be an exact bool")
    if type(full_bar_count) is not int or full_bar_count <= 0:
        raise ValueError("warmup full bar count must be a positive integer")
    if type(suffix_bar_count) is not int or suffix_bar_count < 0:
        raise ValueError("warmup suffix bar count must be a non-negative integer")
    required = SCREENING_WARMUP_REQUIRED_BARS[frequency]
    if full_bar_count < required:
        if converged or suffix_bar_count != 0:
            raise ValueError("insufficient warmup history contradicts its result")
        return "WARMUP_HISTORY_INSUFFICIENT"
    expected_suffix = expected_screening_warmup_suffix_bar_count(full_bar_count)
    if suffix_bar_count != expected_suffix:
        raise ValueError("warmup suffix count contradicts the frozen split")
    return "WARMUP_TAIL_STABLE" if converged else "WARMUP_TAIL_DIVERGED"


def five_minute_warmup_converged(raw: object) -> bool | None:
    """Read the physical 5m warmup gate from a serialized warmup document.

    Current production documents contain one row for every physical period.
    The aggregate fallback preserves compatibility with old compact callers;
    malformed or ambiguous row sets return ``None`` so consumers fail closed.
    """

    if not isinstance(raw, Mapping):
        return None
    rows = raw.get("by_frequency")
    if isinstance(rows, list) and rows:
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("frequency") == "5m"
        ]
        if len(matches) != 1 or type(matches[0].get("converged")) is not bool:
            return None
        return bool(matches[0]["converged"])
    aggregate = raw.get("converged")
    return aggregate if type(aggregate) is bool else None


def screening_warmup_tail_signature(
    *,
    direction: ContextDirection,
    points: Sequence[StructuralPoint],
    not_before: datetime,
    trade_level_only: bool = False,
) -> tuple[object, ...]:
    """Build the shared semantic tail used by live and historical gates."""

    cutoff = normalize_datetime(not_before, "warmup tail not_before")
    latest: dict[
        tuple[str, str, int, str],
        tuple[datetime, tuple[object, ...]],
    ] = {}
    for point in points:
        if trade_level_only and not is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        ):
            continue
        observed_at = point.available_at
        if point.terminal_segment is None and (
            observed_at < cutoff or point.anchor_at < cutoff
        ):
            continue
        terminal_role = (
            "legacy"
            if point.terminal_segment is None
            else point.terminal_segment.role
        )
        lane = (
            point.point_type,
            point.tower,
            point.recursive_level,
            terminal_role,
        )
        semantic = (
            point.side,
            point.status,
            point.source_frequency,
            point.anchor_at.isoformat(),
            None if point.confirmed_at is None else point.confirmed_at.isoformat(),
            point.available_at.isoformat(),
            point.price_basis_revision,
            point.structure_anchor_price,
            point.structure_invalidation_price,
            point.center_zd,
            point.center_zg,
            point.center_ordinal,
            point.variant,
            point.divergence_kind,
            point.evidence_codes,
            (
                None
                if point.terminal_segment is None
                else (
                    point.terminal_segment.role,
                    point.terminal_segment.source_kind.value,
                    point.terminal_segment.direction,
                    point.terminal_segment.state,
                    point.terminal_segment.market_start.isoformat(),
                    point.terminal_segment.market_end.isoformat(),
                )
            ),
        )
        previous = latest.get(lane)
        if previous is None or observed_at > previous[0]:
            latest[lane] = (observed_at, semantic)
    return (
        None if trade_level_only else direction,
        tuple(
            (lane, observed_at.isoformat(), semantic)
            for lane, (observed_at, semantic) in sorted(latest.items())
        ),
    )


__all__ = (
    "SCREENING_QMT_30M_FALLBACK_REASON_CODE",
    "SCREENING_CANONICAL_REQUEST_BARS",
    "SCREENING_MINIMUM_BARS_BY_FREQUENCY",
    "SCREENING_WARMUP_DIFFERENCE_CODES",
    "SCREENING_WARMUP_FREQUENCIES",
    "SCREENING_WARMUP_DAYS_BY_FREQUENCY",
    "SCREENING_WARMUP_REQUIRED_BARS",
    "expected_screening_warmup_suffix_bar_count",
    "five_minute_warmup_converged",
    "screening_warmup_tail_signature",
    "screening_warmup_reason_code",
)
