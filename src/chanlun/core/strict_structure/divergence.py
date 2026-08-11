from __future__ import annotations

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


def _consolidation_pair(center, units):
    """Return the same width-matched A/C legs used by trend divergence."""

    if (
        center.state is not CenterState.COMPLETED
    ):
        return None
    entry = center_entry_comparison_leg(center, units)
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
            # Once two separated centers form a trend, its terminal A/C
            # comparison is the sole classification.  Emitting an additional
            # consolidation label for each embedded center would give one
            # structural movement two incompatible divergence kinds.
            if center.center_id in trend_center_ids:
                continue
            pair = _consolidation_pair(center, level.units)
            if pair is None:
                continue
            try:
                evidence = compare_comparison_legs(
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
