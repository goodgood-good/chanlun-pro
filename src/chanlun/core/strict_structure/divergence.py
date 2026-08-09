from __future__ import annotations

from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    StrictStructureResult,
)
from chanlun.core.strict_structure.strength import (
    MacdStrengthUnavailable,
    compare_divergence,
    compare_terminal_trend_divergence,
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


def collect_strict_divergences(structure, strength):
    if not isinstance(structure, StrictStructureResult):
        raise TypeError("structure must be a StrictStructureResult")
    by_id = {}
    for level in structure.levels:
        for boundary in level.decomposition_boundaries:
            evidence = boundary.divergence
            previous = by_id.setdefault(evidence.divergence_id, evidence)
            if previous != evidence:
                raise ValueError("divergence id maps to conflicting evidence")

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
            evidence = trend.terminal_divergence
            if evidence is None:
                try:
                    compared = compare_terminal_trend_divergence(
                        trend.centers,
                        level.units,
                        strength,
                        trend_start_unit_id=trend.constituent_units[0].unit_id,
                    )
                except MacdStrengthUnavailable:
                    continue
                evidence = None if compared is None else compared[0]
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
    """Close the top-level ledger over every embedded formal divergence."""

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
