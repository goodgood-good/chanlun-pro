from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.v3_individual_research import (
    DAILY_VALUATION_URL,
    INDUSTRY_CLASSIFICATION_URL,
    SECTOR_VALUATION_URL,
    FinancialDataEvidence,
    IndividualResearchEvidenceBundle,
    SignedThreeProgramAdjudication,
    build_individual_selection_facts,
)


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 27, 14, 0, tzinfo=CN)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def evidence(
    evidence_id: str,
    program: str,
    service_url: str,
    *,
    subject_id: str = "600000.SH",
    subject_kind: str = "STOCK",
    fields: tuple[str, ...] = ("symbol", "disclosure_date"),
    published_at: datetime | None = None,
) -> FinancialDataEvidence:
    published = published_at or NOW - timedelta(days=10)
    return FinancialDataEvidence(
        evidence_id=evidence_id,
        program=program,
        service_url=service_url,
        subject_id=subject_id,
        subject_kind=subject_kind,
        entity_resolution_id=SHA_A,
        published_at=published,
        captured_at=max(published, NOW - timedelta(hours=1)),
        payload_sha256=SHA_B,
        source_fields=fields,
        source_record_ids=(f"record:{evidence_id}",),
        point_in_time_versioned=True,
    )


def valid_bundle() -> IndividualResearchEvidenceBundle:
    values = (
        evidence(
            "industry-classification",
            "INDUSTRY_OPPORTUNITY",
            INDUSTRY_CLASSIFICATION_URL,
        ),
        evidence(
            "industry-report",
            "INDUSTRY_OPPORTUNITY",
            "/api/v1/info/research-reports",
        ),
        evidence(
            "balance-sheet",
            "FUNDAMENTAL_ROLE",
            "/api/v1/stock_fnd/balance-sheet",
        ),
        evidence(
            "candidate-valuation",
            "RELATIVE_VALUE",
            DAILY_VALUATION_URL,
            fields=("symbol", "trading_day", "total_market_cap", "pe_ttm"),
        ),
        evidence(
            "sector-valuation",
            "RELATIVE_VALUE",
            SECTOR_VALUATION_URL,
            subject_id="sector:bank",
            subject_kind="SECTOR",
            fields=("symbol", "end_date", "avg_mv", "pe_ttm"),
        ),
    )
    return IndividualResearchEvidenceBundle(
        bundle_id="bundle:600000:20260727",
        symbol="600000.SH",
        evidence=values,
        peer_set_id="sha256:" + "c" * 64,
        peer_symbols=("000001.SZ", "600000.SH"),
        market_cap_evidence_id="candidate-valuation",
        point_in_time_total_market_cap=Decimal("123000000000"),
    )


def valid_adjudication() -> SignedThreeProgramAdjudication:
    return SignedThreeProgramAdjudication(
        adjudication_id="adjudication:600000:20260727",
        signed_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=30),
        reviewer="researcher-a",
        signature="signed:v1",
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
    )


def test_complete_three_program_evidence_builds_full_individual_snapshot() -> None:
    result = build_individual_selection_facts(
        valid_bundle(),
        valid_adjudication(),
        decision_time=NOW,
    )

    assert result.grade == "FULL_SYSTEM_ELIGIBLE"
    assert result.blockers == ()
    assert result.snapshot is not None
    assert result.snapshot.path == "INDIVIDUAL_THREE_PROGRAM"
    assert result.snapshot.point_in_time_total_market_cap == Decimal("123000000000")


def test_future_industry_evidence_downgrades_lane_to_unresolved() -> None:
    bundle = valid_bundle()
    values = tuple(
        replace(value, published_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
        if value.evidence_id == "industry-report"
        else value
        for value in bundle.evidence
    )
    bundle = replace(bundle, evidence=values)

    result = build_individual_selection_facts(
        bundle,
        valid_adjudication(),
        decision_time=NOW,
    )

    assert result.grade == "RESEARCH_ONLY"
    assert result.snapshot is not None
    assert result.snapshot.industry_opportunity_status == "UNRESOLVED"
    assert "INDUSTRY_LONG_TERM_OPPORTUNITY_EVIDENCE_MISSING" in {
        blocker.code for blocker in result.blockers
    }


def test_future_market_cap_evidence_prevents_snapshot_construction() -> None:
    bundle = valid_bundle()
    values = tuple(
        replace(value, published_at=NOW + timedelta(days=1), captured_at=NOW + timedelta(days=1))
        if value.evidence_id == "candidate-valuation"
        else value
        for value in bundle.evidence
    )

    result = build_individual_selection_facts(
        replace(bundle, evidence=values),
        valid_adjudication(),
        decision_time=NOW,
    )

    assert result.snapshot is None
    assert result.grade == "UNRESOLVED"
    assert "TOTAL_MARKET_CAP_EVIDENCE_NOT_VISIBLE" in {
        blocker.code for blocker in result.blockers
    }


def test_unknown_financial_service_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="service URL is not recalled"):
        evidence(
            "invented",
            "RELATIVE_VALUE",
            "/api/v1/stock/invented-valuation",
        )


def test_relative_value_requires_peer_or_sector_comparison() -> None:
    bundle = valid_bundle()
    bundle = replace(
        bundle,
        evidence=tuple(
            value for value in bundle.evidence if value.evidence_id != "sector-valuation"
        ),
    )

    result = build_individual_selection_facts(
        bundle,
        valid_adjudication(),
        decision_time=NOW,
    )

    assert result.snapshot is not None
    assert result.snapshot.relative_value_status == "UNRESOLVED"
    assert "PEER_OR_SECTOR_VALUATION_EVIDENCE_MISSING" in {
        blocker.code for blocker in result.blockers
    }
