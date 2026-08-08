from __future__ import annotations

from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    StrictStructureResult,
    TrendKind,
    TrendState,
)
from chanlun.core.strict_structure.signals import _comparison_unit
from chanlun.core.strict_structure.strength import (
    MacdStrengthUnavailable,
    compare_divergence,
)


def _outside_fixed_core(item, center) -> bool:
    return (
        item.end_tick > center.zg_tick
        if item.direction == "up"
        else item.end_tick < center.zd_tick
    )


def _consolidation_pair(center):
    if (
        center.state is not CenterState.COMPLETED
        or center.source_kind is SourceKind.STROKE_OBSERVATION
        or center.completion_leave_unit is None
    ):
        return None
    signal = center.completion_leave_unit
    for earlier in reversed(center.extension_units):
        if (
            earlier.locked
            and earlier.direction == signal.direction
            and _outside_fixed_core(earlier, center)
            and earlier.market_end <= signal.market_start
        ):
            return earlier, signal
    return None


def _trend_pair(trend):
    if (
        trend.kind is not TrendKind.TREND
        or trend.state is not TrendState.COMPLETE
        or len(trend.centers) < 2
    ):
        return None
    signal = trend.centers[-1].completion_leave_unit
    if signal is None or signal.source_kind is SourceKind.STROKE_OBSERVATION:
        return None
    earlier = _comparison_unit(trend, signal)
    return None if earlier is None else (earlier, signal)


def collect_strict_divergences(structure, strength):
    if not isinstance(structure, StrictStructureResult):
        raise TypeError("structure must be a StrictStructureResult")
    by_id = {}
    for level in structure.levels:
        for center in level.center_result.centers:
            pair = _consolidation_pair(center)
            if pair is None:
                continue
            try:
                evidence = compare_divergence(
                    *pair,
                    strength,
                    kind="consolidation",
                )
            except MacdStrengthUnavailable:
                continue
            if evidence.is_divergent:
                previous = by_id.setdefault(evidence.divergence_id, evidence)
                if previous != evidence:
                    raise ValueError("divergence id maps to conflicting evidence")

        for trend in level.completed_trends:
            pair = _trend_pair(trend)
            if pair is None:
                continue
            try:
                evidence = compare_divergence(
                    *pair,
                    strength,
                    kind="trend",
                )
            except MacdStrengthUnavailable:
                continue
            if evidence.is_divergent:
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
