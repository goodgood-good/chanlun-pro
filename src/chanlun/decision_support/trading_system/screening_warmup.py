"""筛选与归档校验共用的冻结成对预热策略。

实时扫描器会比较完整可用前缀产生的语义活动尾部，与去掉最早三分之一后
产生的尾部。生产者以及所有判断快照是否适合前向模拟归档的消费者，都必须
使用相同常量和数量关系。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


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


__all__ = (
    "SCREENING_QMT_30M_FALLBACK_REASON_CODE",
    "SCREENING_WARMUP_DIFFERENCE_CODES",
    "SCREENING_WARMUP_FREQUENCIES",
    "SCREENING_WARMUP_REQUIRED_BARS",
    "expected_screening_warmup_suffix_bar_count",
    "screening_warmup_reason_code",
)
