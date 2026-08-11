"""Screening helpers over the canonical recursive strict evidence graph.

Every physical market-data frequency is still analyzed independently, but it
uses ``CL.get_strict_evidence()`` exactly like charts, replay and monitoring.
This module owns only screening-specific provisional presentation; it does not
implement another center, divergence or buy/sell-point calculator.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    SourceKind,
    StrictEvidenceResult,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate


SCREENING_STRUCTURE_SCOPE = "physical-timeframe-recursive"
SCREENING_STRUCTURE_FREQUENCIES = ("d", "30m", "5m", "1m")


def build_screening_evidence(
    cd,
    *,
    source_closed_at: datetime,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
    strict_config_revision: str,
) -> StrictEvidenceResult:
    """Return the sole recursive evidence snapshot already owned by ``CL``."""

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
    getter = getattr(cd, "get_strict_evidence", None)
    if not callable(getter):
        raise TypeError("screening state must expose canonical strict evidence")
    evidence = getter()
    if not isinstance(evidence, StrictEvidenceResult):
        raise TypeError("canonical strict endpoint returned invalid evidence")
    if (
        evidence.symbol != cd.get_code()
        or evidence.source_frequency != cd.get_frequency()
        or evidence.price_basis_revision != price_basis_revision
        or evidence.structure_price_quantum != structure_price_quantum
        or evidence.strict_config_revision != strict_config_revision
        or evidence.source_closed_at > source_closed_at
    ):
        raise ValueError("canonical strict evidence context mismatch")
    return evidence


def unfinished_segment_candidates(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
) -> tuple[ProvisionalCandidate, ...]:
    """Expose completed live center previews as non-actionable candidates.

    A preview can geometrically identify an external entry, a three-unit core,
    an external leave and its first return,
    yet remain non-formal because at least one segment is unfinished.  This is
    useful early-screening evidence, but it must never masquerade as a locked
    third-class point.
    """

    if evidence.symbol != code or evidence.source_frequency != source_frequency:
        raise ValueError("screening evidence context mismatch")
    if not evidence.structure.levels:
        # The canonical recursive endpoint omits level 0 until at least one
        # physical segment exists.  Sparse sector composites can legitimately
        # be in that state; they simply have no unfinished preview to expose.
        return ()
    level = evidence.structure.levels[0]
    if level.structural_level != 0:
        raise ValueError("screening evidence base level is invalid")
    units = {unit.unit_id: unit for unit in level.units}
    output: dict[str, ProvisionalCandidate] = {}
    for preview in level.center_result.previews:
        if (
            preview.state is not CenterPreviewState.COMPLETED
            or preview.source_kind is not SourceKind.SEGMENT
            or preview.structural_level != 0
            or preview.zd_tick is None
            or preview.zg_tick is None
            or preview.completion_leave_unit_id is None
            or preview.completion_return_unit_id is None
        ):
            continue
        try:
            entry = units[preview.entry_unit_id]
            body = tuple(units[unit_id] for unit_id in preview.unit_ids)
            leave = units[preview.completion_leave_unit_id]
            return_unit = units[preview.completion_return_unit_id]
        except KeyError as exc:
            raise ValueError("center preview references an unavailable segment") from exc
        if not body or all(
            unit.locked for unit in (entry, *body, leave, return_unit)
        ):
            raise ValueError("unfinished center preview must contain a live segment")
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
            "chanlun-screening-unfinished-segment-candidate",
            evidence.price_basis_revision,
            source_frequency,
            point_type,
            preview.entry_unit_id,
            preview.unit_ids,
            preview.completion_leave_unit_id,
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
                "physical_timeframe_recursive_base_level",
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
