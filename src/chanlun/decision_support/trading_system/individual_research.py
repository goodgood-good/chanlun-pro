"""Financial-data service evidence for the strict strategy three-program stock path.

The original lessons do not define numerical leader, growth or undervaluation
formulae.  This module therefore validates evidence and a signed adjudication;
it never turns a vendor metric into an automatic investment conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.selection import (
    FundamentalRole,
    IndustryOpportunity,
    RelativeValue,
    SelectionResearchSnapshot,
)


ResearchProgram = Literal[
    "INDUSTRY_OPPORTUNITY",
    "FUNDAMENTAL_ROLE",
    "RELATIVE_VALUE",
]
EvidenceSubjectKind = Literal["STOCK", "SECTOR", "INDUSTRY_CHAIN", "PUBLICATION"]
ResearchGrade = Literal["FULL_SYSTEM_ELIGIBLE", "RESEARCH_ONLY", "UNRESOLVED"]

# Every path below was returned by the financial-data-query skill's tool
# recall on 2026-07-27.  Runtime acquisition must reject any URL not in this
# immutable allow-list instead of guessing a similarly named service.
INDUSTRY_CLASSIFICATION_URL = "/api/stock_fnd/industry-classification"
DAILY_VALUATION_URL = "/api/stock/daily-valuation-indicators"
SECTOR_VALUATION_URL = "/api/sector/sector-valuation"

PROGRAM_SERVICE_URLS: dict[ResearchProgram, frozenset[str]] = {
    "INDUSTRY_OPPORTUNITY": frozenset(
        {
            INDUSTRY_CLASSIFICATION_URL,
            "/api/common/industry-chain-tree",
            "/api/info/research-reports",
            "/api/info/news/search_evidence",
            "/api/info/news-global-search",
            "/api/sector/sector-financial-point",
            "/api/sector/sector-financial-cumulative",
        }
    ),
    "FUNDAMENTAL_ROLE": frozenset(
        {
            INDUSTRY_CLASSIFICATION_URL,
            "/api/stock_fnd/balance-sheet",
            "/api/stock_fnd/growth-rates-quarter",
            "/api/stock_fnd/solvency-leverage-metrics",
            "/api/stock_fnd/specialty-metrics-period",
            "/api/stock_fnd/main-business-industry",
            "/api/stock_fnd/consensus-details",
            "/api/stock_fnd/rating-summary",
            "/api/info/research-reports",
            SECTOR_VALUATION_URL,
        }
    ),
    "RELATIVE_VALUE": frozenset(
        {
            DAILY_VALUATION_URL,
            SECTOR_VALUATION_URL,
            "/api/stock_fnd/style-classification",
            "/api/stock_fnd/target-price",
            "/api/stock_fnd/consensus-details",
            "/api/index_fnd/index-fnd-financial-ratios",
            "/api/sector/sector-financial-point",
            "/api/sector/sector-financial-cumulative",
        }
    ),
}
FINANCIAL_SERVICE_CATALOG_ID = sha256_json(
    {
        "schema": "chanlun-financial-data-service-catalog",
        "programs": {
            program: tuple(sorted(urls))
            for program, urls in PROGRAM_SERVICE_URLS.items()
        },
    }
)


def _require_sha256(value: str, label: str) -> None:
    if not (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{label} must be a sha256 identity")


@dataclass(frozen=True, slots=True)
class ResearchEvidenceBlocker:
    field: str
    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.field or not self.code or not self.detail:
            raise ValueError("research blocker must be explicit")


@dataclass(frozen=True, slots=True)
class FinancialDataEvidence:
    evidence_id: str
    program: ResearchProgram
    service_url: str
    subject_id: str
    subject_kind: EvidenceSubjectKind
    entity_resolution_id: str
    published_at: datetime
    captured_at: datetime
    payload_sha256: str
    source_fields: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    point_in_time_attested: bool
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "published_at",
            normalize_datetime(self.published_at, "published_at"),
        )
        object.__setattr__(
            self,
            "captured_at",
            normalize_datetime(self.captured_at, "captured_at"),
        )
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.evidence_id, self.subject_id)
        ):
            raise ValueError("financial evidence identity is required")
        if self.program not in PROGRAM_SERVICE_URLS:
            raise ValueError("unsupported three-program evidence lane")
        if self.service_url not in PROGRAM_SERVICE_URLS[self.program]:
            raise ValueError(
                f"service URL is not recalled for {self.program}: {self.service_url}"
            )
        _require_sha256(self.entity_resolution_id, "entity_resolution_id")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if self.captured_at < self.published_at:
            raise ValueError("evidence cannot be captured before publication")
        if (
            not self.source_fields
            or len(self.source_fields) != len(set(self.source_fields))
            or not self.source_record_ids
            or len(self.source_record_ids) != len(set(self.source_record_ids))
        ):
            raise ValueError("evidence fields and record ids must be non-empty and unique")

    def visible_at(self, decision_time: datetime) -> bool:
        decision = normalize_datetime(decision_time, "decision_time")
        # A later capture can support a historical decision only when the
        # service returned an as-published/effective-dated vintage rather than
        # a present-day restatement.
        capture_is_causal = self.captured_at <= decision or self.point_in_time_attested
        return self.published_at <= decision and capture_is_causal and not self.issues


@dataclass(frozen=True, slots=True)
class IndividualResearchEvidenceBundle:
    bundle_id: str
    symbol: str
    evidence: tuple[FinancialDataEvidence, ...]
    peer_set_id: str
    peer_symbols: tuple[str, ...]
    market_cap_evidence_id: str
    point_in_time_total_market_cap: Decimal

    def __post_init__(self) -> None:
        if not self.bundle_id or not self.symbol or not self.peer_set_id:
            raise ValueError("individual research bundle identity is required")
        _require_sha256(self.peer_set_id, "peer_set_id")
        evidence_ids = tuple(value.evidence_id for value in self.evidence)
        if not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("research evidence ids must be non-empty and unique")
        if self.peer_symbols != tuple(sorted(set(self.peer_symbols))):
            raise ValueError("peer symbols must be unique and sorted")
        if self.symbol not in self.peer_symbols or len(self.peer_symbols) < 2:
            raise ValueError("peer set must contain the candidate and at least one peer")
        if self.point_in_time_total_market_cap <= 0:
            raise ValueError("point-in-time total market cap must be positive")
        matches = tuple(
            value
            for value in self.evidence
            if value.evidence_id == self.market_cap_evidence_id
        )
        if len(matches) != 1:
            raise ValueError("market-cap evidence identity must resolve exactly once")
        [market_cap] = matches
        if (
            market_cap.program != "RELATIVE_VALUE"
            or market_cap.service_url != DAILY_VALUATION_URL
            or market_cap.subject_id != self.symbol
            or "total_market_cap" not in market_cap.source_fields
        ):
            raise ValueError("market cap must come from the recalled daily valuation field")

    def _visible(self, program: ResearchProgram, decision_time: datetime) -> tuple[FinancialDataEvidence, ...]:
        return tuple(
            value
            for value in self.evidence
            if value.program == program and value.visible_at(decision_time)
        )

    def readiness_blockers(
        self,
        program: ResearchProgram,
        decision_time: datetime,
    ) -> tuple[ResearchEvidenceBlocker, ...]:
        visible = self._visible(program, decision_time)
        blockers: list[ResearchEvidenceBlocker] = []
        if not visible:
            blockers.append(
                ResearchEvidenceBlocker(
                    program,
                    f"{program}_POINT_IN_TIME_EVIDENCE_MISSING",
                    normalize_datetime(decision_time, "decision_time").isoformat(),
                )
            )
            return tuple(blockers)
        if program == "INDUSTRY_OPPORTUNITY":
            if not any(value.service_url == INDUSTRY_CLASSIFICATION_URL for value in visible):
                blockers.append(
                    ResearchEvidenceBlocker(
                        program,
                        "INDUSTRY_CLASSIFICATION_EVIDENCE_MISSING",
                        INDUSTRY_CLASSIFICATION_URL,
                    )
                )
            if not any(
                value.service_url
                not in {INDUSTRY_CLASSIFICATION_URL, "/api/sector/sector-financial-point", "/api/sector/sector-financial-cumulative"}
                for value in visible
            ):
                blockers.append(
                    ResearchEvidenceBlocker(
                        program,
                        "INDUSTRY_LONG_TERM_OPPORTUNITY_EVIDENCE_MISSING",
                        "classification alone is not an opportunity judgment",
                    )
                )
        elif program == "FUNDAMENTAL_ROLE":
            financial_urls = {
                "/api/stock_fnd/balance-sheet",
                "/api/stock_fnd/growth-rates-quarter",
                "/api/stock_fnd/solvency-leverage-metrics",
                "/api/stock_fnd/specialty-metrics-period",
                "/api/stock_fnd/main-business-industry",
            }
            if not any(value.service_url in financial_urls for value in visible):
                blockers.append(
                    ResearchEvidenceBlocker(
                        program,
                        "FUNDAMENTAL_DISCLOSURE_EVIDENCE_MISSING",
                        "no disclosure-dated operating or balance-sheet fact",
                    )
                )
        else:
            candidate_market_cap = any(
                value.evidence_id == self.market_cap_evidence_id for value in visible
            )
            comparison = any(
                value.service_url == SECTOR_VALUATION_URL
                or (
                    value.service_url == DAILY_VALUATION_URL
                    and value.subject_id != self.symbol
                )
                for value in visible
            )
            if not candidate_market_cap:
                blockers.append(
                    ResearchEvidenceBlocker(
                        program,
                        "POINT_IN_TIME_TOTAL_MARKET_CAP_NOT_VISIBLE",
                        self.market_cap_evidence_id,
                    )
                )
            if not comparison:
                blockers.append(
                    ResearchEvidenceBlocker(
                        program,
                        "PEER_OR_SECTOR_VALUATION_EVIDENCE_MISSING",
                        self.peer_set_id,
                    )
                )
        return tuple(blockers)


@dataclass(frozen=True, slots=True)
class SignedThreeProgramAdjudication:
    adjudication_id: str
    signed_at: datetime
    effective_at: datetime
    valid_until: datetime
    reviewer: str
    signature: str
    industry_opportunity_status: IndustryOpportunity
    fundamental_role: FundamentalRole
    relative_value_status: RelativeValue

    def __post_init__(self) -> None:
        for field in ("signed_at", "effective_at", "valid_until"):
            object.__setattr__(self, field, normalize_datetime(getattr(self, field), field))
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.adjudication_id, self.reviewer, self.signature)
        ):
            raise ValueError("signed three-program adjudication is required")
        if not self.signed_at <= self.effective_at <= self.valid_until:
            raise ValueError("three-program adjudication time order is invalid")
        if self.industry_opportunity_status == "NOT_APPLICABLE":
            raise ValueError("individual path cannot skip industry opportunity")
        if self.fundamental_role == "ETF_PROXY" or self.relative_value_status == "ETF_PROXY":
            raise ValueError("individual adjudication cannot use ETF_PROXY states")


@dataclass(frozen=True, slots=True)
class IndividualSelectionFacts:
    snapshot: SelectionResearchSnapshot | None
    grade: ResearchGrade
    evidence_bundle_id: str
    service_catalog_id: str
    blockers: tuple[ResearchEvidenceBlocker, ...]

    @property
    def full_system_eligible(self) -> bool:
        return self.grade == "FULL_SYSTEM_ELIGIBLE" and self.snapshot is not None


def build_individual_selection_facts(
    bundle: IndividualResearchEvidenceBundle,
    adjudication: SignedThreeProgramAdjudication,
    *,
    decision_time: datetime,
) -> IndividualSelectionFacts:
    """Validate independent evidence lanes and emit the existing strict strategy snapshot."""

    decision = normalize_datetime(decision_time, "decision_time")
    blockers: list[ResearchEvidenceBlocker] = []
    blockers_by_program: dict[ResearchProgram, tuple[ResearchEvidenceBlocker, ...]] = {}
    requested: tuple[tuple[ResearchProgram, bool], ...] = (
        (
            "INDUSTRY_OPPORTUNITY",
            adjudication.industry_opportunity_status == "PASS",
        ),
        (
            "FUNDAMENTAL_ROLE",
            adjudication.fundamental_role in {"LEADER", "GROWTH_CHALLENGER"},
        ),
        (
            "RELATIVE_VALUE",
            adjudication.relative_value_status in {"UNDERVALUED", "FAIR"},
        ),
    )
    for program, positive in requested:
        if positive:
            lane_blockers = bundle.readiness_blockers(program, decision)
            blockers_by_program[program] = lane_blockers
            blockers.extend(lane_blockers)
    if adjudication.signed_at > decision:
        blockers.append(
            ResearchEvidenceBlocker(
                "adjudication",
                "THREE_PROGRAM_ADJUDICATION_FROM_FUTURE",
                adjudication.signed_at.isoformat(),
            )
        )
    visible_evidence = tuple(
        value for value in bundle.evidence if value.visible_at(decision)
    )
    market_cap_visible = any(
        value.evidence_id == bundle.market_cap_evidence_id
        for value in visible_evidence
    )
    if not market_cap_visible:
        blockers.append(
            ResearchEvidenceBlocker(
                "point_in_time_total_market_cap",
                "TOTAL_MARKET_CAP_EVIDENCE_NOT_VISIBLE",
                bundle.market_cap_evidence_id,
            )
        )
    if not visible_evidence:
        blockers.append(
            ResearchEvidenceBlocker(
                "evidence",
                "NO_FINANCIAL_DATA_EVIDENCE_VISIBLE",
                decision.isoformat(),
            )
        )
    known_at = max(
        (adjudication.signed_at, *(value.published_at for value in visible_evidence)),
    )
    if known_at > adjudication.effective_at:
        blockers.append(
            ResearchEvidenceBlocker(
                "effective_at",
                "RESEARCH_EFFECTIVE_BEFORE_ALL_EVIDENCE_KNOWN",
                f"known={known_at.isoformat()}; effective={adjudication.effective_at.isoformat()}",
            )
        )

    snapshot = None
    if visible_evidence and market_cap_visible and known_at <= adjudication.effective_at:
        official_ids = tuple(sorted(value.evidence_id for value in visible_evidence))
        industry_status: IndustryOpportunity = (
            "UNRESOLVED"
            if blockers_by_program.get("INDUSTRY_OPPORTUNITY")
            else adjudication.industry_opportunity_status
        )
        fundamental_role: FundamentalRole = (
            "UNRESOLVED"
            if blockers_by_program.get("FUNDAMENTAL_ROLE")
            else adjudication.fundamental_role
        )
        relative_status: RelativeValue = (
            "UNRESOLVED"
            if blockers_by_program.get("RELATIVE_VALUE")
            else adjudication.relative_value_status
        )
        snapshot = SelectionResearchSnapshot(
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-individual-selection",
                    "bundle_id": bundle.bundle_id,
                    "adjudication_id": adjudication.adjudication_id,
                    "evidence_ids": official_ids,
                }
            ),
            symbol=bundle.symbol,
            path="INDIVIDUAL_THREE_PROGRAM",
            effective_at=adjudication.effective_at,
            known_at=known_at,
            valid_until=adjudication.valid_until,
            reviewer=adjudication.reviewer,
            signature=adjudication.signature,
            official_evidence_ids=official_ids,
            industry_opportunity_status=industry_status,
            fundamental_role=fundamental_role,
            relative_value_status=relative_status,
            point_in_time_total_market_cap=bundle.point_in_time_total_market_cap,
            peer_set_id=bundle.peer_set_id,
        )
    positive_selection = all(value for _program, value in requested)
    if snapshot is None:
        grade: ResearchGrade = "UNRESOLVED"
    elif positive_selection and not blockers:
        grade = "FULL_SYSTEM_ELIGIBLE"
    else:
        grade = "RESEARCH_ONLY"
    return IndividualSelectionFacts(
        snapshot=snapshot,
        grade=grade,
        evidence_bundle_id=bundle.bundle_id,
        service_catalog_id=FINANCIAL_SERVICE_CATALOG_ID,
        blockers=tuple(blockers),
    )


__all__ = (
    "DAILY_VALUATION_URL",
    "FINANCIAL_SERVICE_CATALOG_ID",
    "FinancialDataEvidence",
    "INDUSTRY_CLASSIFICATION_URL",
    "IndividualResearchEvidenceBundle",
    "IndividualSelectionFacts",
    "PROGRAM_SERVICE_URLS",
    "ResearchEvidenceBlocker",
    "SECTOR_VALUATION_URL",
    "SignedThreeProgramAdjudication",
    "build_individual_selection_facts",
)
