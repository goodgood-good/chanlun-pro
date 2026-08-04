"""Physical-timeframe structure evidence for early stock screening.

This module is deliberately separate from ``CL.get_strict_evidence``.  The
global strict endpoint recursively promotes completed trend types.  Early
screening instead consumes four real market-data frequencies independently;
within each frequency, old strokes build segments and only segment-sourced
level zero is tradable evidence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.divergence import collect_strict_divergences
from chanlun.core.strict_structure.identity import (
    build_strict_evidence_revision,
    stable_structure_id,
)
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictStructureResult,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine
from chanlun.core.strict_structure.strength import MacdStrengthProvider
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate


SCREENING_STRUCTURE_SCOPE = "physical-timeframe-level-zero"
SCREENING_STRUCTURE_FREQUENCIES = ("d", "30m", "5m", "1m")


def build_screening_evidence(
    cd,
    *,
    source_closed_at: datetime,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
    strict_config_revision: str,
) -> StrictEvidenceResult:
    """Build one non-recursive, segment-sourced evidence snapshot.

    ``cd.get_xds()`` intentionally includes the active unfinished terminal
    segment.  ``calculate_centers`` keeps locked units formal and carries every
    unlocked suffix unit into ``CenterPreview`` evidence, so an unfinished
    segment participates without being promoted to a confirmed point.
    """

    if not isinstance(source_closed_at, datetime):
        raise TypeError("source_closed_at must be a datetime")
    if (
        not isinstance(structure_price_quantum, Decimal)
        or not structure_price_quantum.is_finite()
        or structure_price_quantum <= 0
    ):
        raise ValueError("structure_price_quantum must be a positive Decimal")
    if not price_basis_revision or not strict_config_revision:
        raise ValueError("screening structure identity is required")

    registry = UnitLockRegistry(price_basis_revision)
    segment_units = adapt_lines(
        cd.get_xds(),
        0,
        SourceKind.SEGMENT,
        structure_price_quantum,
        source_closed_at,
        registry,
    )
    center_result = calculate_centers(
        segment_units,
        0,
        SourceKind.SEGMENT,
    )
    centers_for_trends = tuple(
        center
        for index, center in enumerate(center_result.centers)
        if center.state is CenterState.COMPLETED
        or index == len(center_result.centers) - 1
    )
    assembly = assemble_trend_types(
        centers_for_trends,
        segment_units,
        0,
    )
    level_zero = StrictLevelResult(
        structural_level=0,
        units=segment_units,
        center_result=center_result,
        trend_types=assembly.current_trends,
        completed_trends=assembly.completed_trends,
    )
    structure = StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision=price_basis_revision,
        levels=(level_zero,),
    )

    stroke_units = adapt_lines(
        cd.get_bis(),
        0,
        SourceKind.STROKE_OBSERVATION,
        structure_price_quantum,
        source_closed_at,
        registry,
    )
    stroke_observations = calculate_centers(
        stroke_units,
        0,
        SourceKind.STROKE_OBSERVATION,
    )

    strength = MacdStrengthProvider(cd)
    signal_engine = StrictSignalEngine(
        structure=structure,
        strength=strength,
        price_quantum=structure_price_quantum,
    )
    first = signal_engine.first_class_points()
    second = signal_engine.second_class_points(first)
    third = signal_engine.third_class_points()
    confirmed_by_id = {}
    for point in (*first, *second, *third):
        previous = confirmed_by_id.setdefault(point.point_id, point)
        if previous != point:
            raise ValueError("screening point id maps to conflicting evidence")
    confirmed = tuple(
        sorted(
            confirmed_by_id.values(),
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )
    approaching = signal_engine.approaching_points(source_closed_at)
    divergences = collect_strict_divergences(structure, strength)
    structure_revision = build_strict_evidence_revision(
        symbol=cd.get_code(),
        source_frequency=cd.get_frequency(),
        price_basis_revision=price_basis_revision,
        strict_config_revision=strict_config_revision,
        structure=structure,
        confirmed_points=confirmed,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol=cd.get_code(),
        source_frequency=cd.get_frequency(),
        source_closed_at=source_closed_at,
        price_basis_revision=price_basis_revision,
        structure_price_quantum=structure_price_quantum,
        strict_config_revision=strict_config_revision,
        structure_revision=structure_revision,
        structure=structure,
        stroke_center_observations=stroke_observations,
        confirmed_points=confirmed,
        approaching_points=approaching,
        divergences=divergences,
    )


def unfinished_segment_candidates(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
) -> tuple[ProvisionalCandidate, ...]:
    """Expose completed live center previews as non-actionable candidates.

    A preview can geometrically contain entry, core, leave and first return,
    yet remain non-formal because at least one segment is unfinished.  This is
    useful early-screening evidence, but it must never masquerade as a locked
    third-class point.
    """

    if evidence.symbol != code or evidence.source_frequency != source_frequency:
        raise ValueError("screening evidence context mismatch")
    if len(evidence.structure.levels) != 1:
        raise ValueError("early screening accepts exactly physical level zero")
    level = evidence.structure.levels[0]
    units = {unit.unit_id: unit for unit in level.units}
    output: dict[str, ProvisionalCandidate] = {}
    for preview in level.center_result.previews:
        if (
            preview.state is not CenterPreviewState.COMPLETED
            or preview.source_kind is not SourceKind.SEGMENT
            or preview.structural_level != 0
            or preview.zd_tick is None
            or preview.zg_tick is None
            or preview.completion_return_unit_id is None
        ):
            continue
        try:
            body = tuple(units[unit_id] for unit_id in preview.unit_ids)
            return_unit = units[preview.completion_return_unit_id]
        except KeyError as exc:
            raise ValueError("center preview references an unavailable segment") from exc
        if not body or all(unit.locked for unit in (*body, return_unit)):
            raise ValueError("unfinished center preview must contain a live segment")
        entry = body[0]
        leave = body[-1]
        if (
            entry.direction == "up"
            and leave.direction == "up"
            and return_unit.direction == "down"
            and return_unit.low_tick >= preview.zg_tick
        ):
            point_type = "3buy"
            side = "buy"
            anchor_tick = return_unit.low_tick
        elif (
            entry.direction == "down"
            and leave.direction == "down"
            and return_unit.direction == "up"
            and return_unit.high_tick <= preview.zd_tick
        ):
            point_type = "3sell"
            side = "sell"
            anchor_tick = return_unit.high_tick
        else:
            continue
        candidate_id = stable_structure_id(
            "chanlun-screening-unfinished-segment-candidate/v1",
            evidence.price_basis_revision,
            source_frequency,
            point_type,
            preview.unit_ids,
            preview.completion_return_unit_id,
            preview.zd_tick,
            preview.zg_tick,
        )
        output[candidate_id] = ProvisionalCandidate(
            candidate_id=candidate_id,
            code=code,
            point_type=point_type,
            side=side,
            status="provisional",
            source_frequency=source_frequency,
            tower="formal",
            recursive_level=0,
            observed_at=preview.available_at,
            anchor_price=float(
                evidence.structure_price_quantum * Decimal(anchor_tick)
            ),
            missing_conditions=(
                "unfinished_segment_lock",
                "formal_center_confirmation",
            ),
            evidence_codes=(
                "physical_timeframe_level_zero",
                "old_pen_segment_chain",
                "unfinished_segment_participates",
                "provisional_center_completion",
                "core_boundary_held",
            ),
        )
    return tuple(
        sorted(
            output.values(),
            key=lambda candidate: (
                candidate.observed_at,
                candidate.candidate_id,
            ),
        )
    )


def merge_provisional_candidates(
    formal_tail: tuple[ProvisionalCandidate, ...],
    unfinished_tail: tuple[ProvisionalCandidate, ...],
) -> tuple[ProvisionalCandidate, ...]:
    """Merge preview sources without showing the same live point twice."""

    output = list(formal_tail)
    semantic = {
        (item.point_type, item.observed_at, round(item.anchor_price, 12))
        for item in formal_tail
    }
    for item in unfinished_tail:
        key = (item.point_type, item.observed_at, round(item.anchor_price, 12))
        if key not in semantic:
            output.append(item)
            semantic.add(key)
    return tuple(sorted(output, key=lambda item: (item.observed_at, item.candidate_id)))


__all__ = (
    "SCREENING_STRUCTURE_FREQUENCIES",
    "SCREENING_STRUCTURE_SCOPE",
    "build_screening_evidence",
    "merge_provisional_candidates",
    "unfinished_segment_candidates",
)
