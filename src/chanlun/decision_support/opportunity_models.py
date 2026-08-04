from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json


@dataclass(frozen=True, slots=True)
class AmbiguousGics3Membership:
    code: str
    source_sector_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.code or self.source_sector_ids != tuple(
            sorted(set(self.source_sector_ids))
        ):
            raise ValueError("ambiguous GICS3 membership must be identified and sorted")


@dataclass(frozen=True, slots=True)
class SectorDefinition:
    sector_id: str
    source: str
    level: str
    name: str
    normalized_name: str
    parent_gics1_id: str
    parent_gics1_name: str
    members: tuple[str, ...]
    eligible_for_entry: bool
    reason_codes: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        source: str,
        level: str,
        name: str,
        normalized_name: str,
        parent_gics1_id: str,
        parent_gics1_name: str,
        members: tuple[str, ...],
        eligible_for_entry: bool,
        reason_codes: tuple[str, ...],
    ) -> SectorDefinition:
        sector_id = "sector:" + sha256_json(
            {
                "source": source,
                "level": level,
                "normalized_name": normalized_name,
            }
        ).removeprefix("sha256:")
        return cls(
            sector_id=sector_id,
            source=source,
            level=level,
            name=name,
            normalized_name=normalized_name,
            parent_gics1_id=parent_gics1_id,
            parent_gics1_name=parent_gics1_name,
            members=tuple(sorted(set(members))),
            eligible_for_entry=eligible_for_entry,
            reason_codes=tuple(sorted(set(reason_codes))),
        )

    def __post_init__(self) -> None:
        required = (
            self.sector_id,
            self.source,
            self.level,
            self.name,
            self.normalized_name,
            self.parent_gics1_id,
            self.parent_gics1_name,
        )
        if not all(value and value.strip() for value in required):
            raise ValueError("sector identity fields are required")
        if self.members != tuple(sorted(set(self.members))):
            raise ValueError("sector members must be unique and sorted")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("sector reasons must be unique and sorted")
        if self.eligible_for_entry == bool(self.reason_codes):
            raise ValueError("eligible sector cannot have rejection reasons")


@dataclass(frozen=True, slots=True)
class GicsCatalogSnapshot:
    source: str
    source_service_id: str
    captured_at: datetime
    sectors: tuple[SectorDefinition, ...]
    ambiguous_gics3_memberships: tuple[AmbiguousGics3Membership, ...]
    invalid_codes: tuple[str, ...]
    empty_sector_names: tuple[str, ...]
    parent_mapping_conflicts: tuple[str, ...]
    membership_fingerprint: str
    eligible_for_entry: bool
    reason_codes: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        source: str,
        source_service_id: str,
        captured_at: datetime,
        sectors: tuple[SectorDefinition, ...],
        ambiguous_gics3_memberships: tuple[AmbiguousGics3Membership, ...],
        invalid_codes: tuple[str, ...],
        empty_sector_names: tuple[str, ...],
        parent_mapping_conflicts: tuple[str, ...],
        eligible_for_entry: bool,
        reason_codes: tuple[str, ...],
    ) -> GicsCatalogSnapshot:
        observed = normalize_datetime(captured_at, "captured_at")
        normalized_sectors = tuple(sorted(sectors, key=lambda value: value.sector_id))
        normalized_ambiguous = tuple(
            sorted(ambiguous_gics3_memberships, key=lambda value: value.code)
        )
        invalid = tuple(sorted(set(invalid_codes)))
        empty = tuple(sorted(set(empty_sector_names)))
        conflicts = tuple(sorted(set(parent_mapping_conflicts)))
        reasons = tuple(sorted(set(reason_codes)))
        fingerprint = sha256_json(
            {
                "schema": "qmt-gics-membership/v1",
                "source": source,
                "sectors": normalized_sectors,
                "ambiguous": normalized_ambiguous,
                "invalid_codes": invalid,
                "empty_sector_names": empty,
                "parent_mapping_conflicts": conflicts,
                "eligible_for_entry": eligible_for_entry,
                "reason_codes": reasons,
            }
        )
        return cls(
            source=source,
            source_service_id=source_service_id,
            captured_at=observed,
            sectors=normalized_sectors,
            ambiguous_gics3_memberships=normalized_ambiguous,
            invalid_codes=invalid,
            empty_sector_names=empty,
            parent_mapping_conflicts=conflicts,
            membership_fingerprint=fingerprint,
            eligible_for_entry=eligible_for_entry,
            reason_codes=reasons,
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at",
            normalize_datetime(self.captured_at, "captured_at"),
        )
        if not self.source or not self.source_service_id:
            raise ValueError("catalog source identity is required")
        if not self.membership_fingerprint.startswith("sha256:"):
            raise ValueError("catalog membership fingerprint is required")
        if self.eligible_for_entry == bool(self.reason_codes):
            raise ValueError("eligible catalog cannot have rejection reasons")


@dataclass(frozen=True, slots=True)
class SectorBreadthMetric:
    sector_id: str
    captured_at: datetime
    catalog_member_count: int
    usable_quote_count: int
    usable_return_count: int
    advancing_count: int
    missing_count: int
    quote_coverage_ratio: Decimal
    quote_coverage_score: Decimal
    advance_ratio_score: Decimal | None
    median_return_percentile: Decimal | None
    breadth_score: Decimal | None
    eligible_for_entry: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at",
            normalize_datetime(self.captured_at, "captured_at"),
        )
        counts = (
            self.catalog_member_count,
            self.usable_quote_count,
            self.usable_return_count,
            self.advancing_count,
            self.missing_count,
        )
        if not self.sector_id or any(value < 0 for value in counts):
            raise ValueError("breadth identity and counts are invalid")
        if not Decimal("0") <= self.quote_coverage_ratio <= Decimal("1"):
            raise ValueError("quote coverage ratio must be in [0, 1]")
        if self.eligible_for_entry == bool(self.reason_codes):
            raise ValueError("eligible breadth metric cannot have rejection reasons")


__all__ = [
    "AmbiguousGics3Membership",
    "GicsCatalogSnapshot",
    "SectorBreadthMetric",
    "SectorDefinition",
]
