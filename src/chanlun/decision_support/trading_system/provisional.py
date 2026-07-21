from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from chanlun.core.strict_structure.models import (
    StrictEvidenceResult,
    StrictPointStatus,
)
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import (
    PointSide,
    PointType,
    StructureTower,
)


@dataclass(frozen=True, slots=True)
class ProvisionalCandidate:
    candidate_id: str
    code: str
    point_type: PointType
    side: PointSide
    status: Literal["provisional"]
    source_frequency: str
    tower: StructureTower
    recursive_level: int
    observed_at: datetime
    anchor_price: float
    missing_conditions: tuple[str, ...]
    evidence_codes: tuple[str, ...]
    actionable: Literal[False] = False

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id is required")
        if self.status != "provisional" or self.actionable is not False:
            raise ValueError("provisional candidates must remain non-actionable")
        expected_side = "buy" if self.point_type.endswith("buy") else "sell"
        if self.side != expected_side:
            raise ValueError("point_type and side disagree")
        if self.tower != "formal" or self.recursive_level < 0:
            raise ValueError("invalid provisional structure identity")
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.anchor_price <= 0:
            raise ValueError("anchor_price must be positive")
        if not self.missing_conditions:
            raise ValueError("missing_conditions cannot be empty")
        for name, values in (
            ("missing_conditions", self.missing_conditions),
            ("evidence_codes", self.evidence_codes),
        ):
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{name} must be non-empty and unique")


def extract_provisional_candidates(
    evidence: StrictEvidenceResult,
    *,
    code: str,
    source_frequency: str,
    as_of: datetime,
) -> tuple[ProvisionalCandidate, ...]:
    closed_at = normalize_datetime(as_of, "as_of")
    if evidence.symbol != code or evidence.source_frequency != source_frequency:
        raise ValueError("strict evidence context mismatch")
    if normalize_datetime(evidence.source_closed_at, "source_closed_at") > closed_at:
        raise ValueError("strict evidence snapshot is after as_of")

    output: list[ProvisionalCandidate] = []
    for raw in tuple(evidence.approaching_points):
        if raw.status is not StrictPointStatus.APPROACHING:
            raise ValueError("strict approaching endpoint returned non-approaching point")
        observed_at = normalize_datetime(raw.available_at, "point.available_at")
        if observed_at > closed_at:
            raise ValueError("strict point is available after as_of")
        output.append(
            ProvisionalCandidate(
                candidate_id=raw.point_id,
                code=code,
                point_type=cast(PointType, raw.point_type),
                side=cast(PointSide, raw.side),
                status="provisional",
                source_frequency=source_frequency,
                tower="formal",
                recursive_level=raw.structural_level,
                observed_at=observed_at,
                anchor_price=float(raw.structure_anchor_price),
                missing_conditions=raw.missing_conditions,
                evidence_codes=raw.evidence_codes,
            )
        )
    return tuple(
        sorted(
            output,
            key=lambda candidate: (
                candidate.observed_at,
                candidate.candidate_id,
            ),
        )
    )
