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
    CANONICAL_POINT_TYPE_SET,
    PointSide,
    PointType,
    PointVariant,
    StructureTower,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    structural_point_id_map,
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
    invalidation_price: float
    price_basis_revision: str
    variant: PointVariant
    center_id: str | None
    center_zd: float | None
    center_zg: float | None
    center_ordinal: int | None
    divergence_kind: str | None
    missing_conditions: tuple[str, ...]
    evidence_codes: tuple[str, ...]
    actionable: Literal[False] = False
    parent_point_id: str | None = None
    related_point_ids: tuple[str, ...] = ()
    small_to_large_carrier_unit_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id is required")
        if self.status != "provisional" or self.actionable is not False:
            raise ValueError("provisional candidates must remain non-actionable")
        if self.point_type not in CANONICAL_POINT_TYPE_SET:
            raise ValueError("盘中候选买卖点类型无效")
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
        if self.invalidation_price <= 0:
            raise ValueError("invalidation_price must be positive")
        if self.side == "buy" and self.invalidation_price > self.anchor_price:
            raise ValueError("买点失效价不能高于锚点价")
        if self.side == "sell" and self.invalidation_price < self.anchor_price:
            raise ValueError("卖点失效价不能低于锚点价")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision 不能为空")
        if self.variant not in {
            "standard",
            "strict",
            "weak_divergence",
            "boundary_touch",
        }:
            raise ValueError("盘中候选点形态无效")
        if (self.center_zd is None) != (self.center_zg is None):
            raise ValueError("盘中候选点必须同时保留中枢上下沿")
        if (
            self.center_zd is not None
            and self.center_zg is not None
            and self.center_zd > self.center_zg
        ):
            raise ValueError("盘中候选点中枢区间无效")
        if self.center_ordinal is not None and self.center_ordinal <= 0:
            raise ValueError("盘中候选点中枢序号必须为正数")
        if self.point_type in {"3buy", "3sell"} and (
            self.center_id is None
            or self.center_zd is None
            or self.center_zg is None
            or self.center_ordinal is None
        ):
            raise ValueError("盘中三类点必须保留完整中枢血缘")
        if self.point_type not in {"3buy", "3sell"} and self.center_ordinal is not None:
            raise ValueError("中枢序号只属于三类点")
        if not self.missing_conditions:
            raise ValueError("missing_conditions cannot be empty")
        object.__setattr__(self, "related_point_ids", tuple(self.related_point_ids))
        object.__setattr__(
            self,
            "small_to_large_carrier_unit_ids",
            tuple(self.small_to_large_carrier_unit_ids),
        )
        for name, values in (
            ("missing_conditions", self.missing_conditions),
            ("evidence_codes", self.evidence_codes),
        ):
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{name} must be non-empty and unique")
        if self.parent_point_id is not None and not self.parent_point_id:
            raise ValueError("parent_point_id must be a non-empty string")
        if any(not value for value in self.related_point_ids) or len(
            set(self.related_point_ids)
        ) != len(self.related_point_ids):
            raise ValueError("related_point_ids must be unique non-empty strings")
        if self.candidate_id in self.related_point_ids:
            raise ValueError("provisional candidate cannot reference itself")
        if self.point_type in {"2buy", "2sell"} and self.parent_point_id is None:
            raise ValueError("二类预判点必须引用已确认的一类父点")
        is_small_to_large = "small_to_large_reversal" in self.evidence_codes
        if is_small_to_large:
            if (
                self.point_type not in {"2buy", "2sell"}
                or self.related_point_ids != (self.parent_point_id,)
                or len(self.small_to_large_carrier_unit_ids) != 3
                or len(set(self.small_to_large_carrier_unit_ids)) != 3
                or any(not value for value in self.small_to_large_carrier_unit_ids)
            ):
                raise ValueError("小转大二类预判点的父点及三段载体不完整")
        elif self.related_point_ids or self.small_to_large_carrier_unit_ids:
            raise ValueError("普通预判点不能携带小转大证据")


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

    confirmed_id_map = structural_point_id_map(
        evidence.confirmed_points,
        code=code,
        source_frequency=source_frequency,
    )
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
                invalidation_price=float(raw.structure_invalidation_price),
                price_basis_revision=raw.price_basis_revision,
                variant=cast(PointVariant, raw.variant.value),
                center_id=raw.center_id,
                center_zd=(
                    None
                    if raw.center_zd_tick is None
                    else float(raw.price_quantum * raw.center_zd_tick)
                ),
                center_zg=(
                    None
                    if raw.center_zg_tick is None
                    else float(raw.price_quantum * raw.center_zg_tick)
                ),
                center_ordinal=raw.center_ordinal,
                divergence_kind=(
                    None if raw.divergence is None else raw.divergence.kind
                ),
                missing_conditions=raw.missing_conditions,
                evidence_codes=raw.evidence_codes,
                parent_point_id=(
                    None
                    if raw.parent_point_id is None
                    else confirmed_id_map[raw.parent_point_id]
                ),
                related_point_ids=tuple(
                    confirmed_id_map[point_id]
                    for point_id in raw.related_point_ids
                ),
                small_to_large_carrier_unit_ids=(
                    raw.small_to_large_carrier_unit_ids
                ),
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
