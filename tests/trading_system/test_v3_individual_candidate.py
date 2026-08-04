from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.models import SectorAssessment
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    BenchmarkStructureRiskFacts,
    HigherTimeframeRiskFacts,
    RiskStructureStateFact,
    RiskStructureStateFacts,
)
from chanlun.decision_support.trading_system.v3_individual_candidate import (
    build_individual_candidate_decision,
)
from chanlun.decision_support.trading_system.v3_individual_research import (
    FINANCIAL_SERVICE_CATALOG_ID,
    IndividualSelectionFacts,
)
from chanlun.decision_support.trading_system.v3_qmt_higher_timeframe import (
    QmtHigherTimeframeInputs,
    QmtHigherTimeframeRiskEnvelope,
    QmtHigherTimeframeWarmupEvidence,
)
from chanlun.decision_support.trading_system.v3_sector_trigger import (
    build_sector_trigger_snapshot,
)
from chanlun.decision_support.trading_system.v3_selection import (
    AccountEntryGate,
    HigherTimeframeRiskSnapshot,
    SectorStrengthSnapshot,
    SelectionResearchSnapshot,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
)


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 27, 14, 1, tzinfo=CN)
SHA = "sha256:" + "a" * 64
SYMBOL = "600000.SH"
SECTOR = "qmt-gics3:bank"


def green_risk_envelope(symbol: str) -> QmtHigherTimeframeRiskEnvelope:
    snapshot = HigherTimeframeRiskSnapshot(
        snapshot_id=f"risk:{symbol}",
        observed_at=NOW,
        monthly="NONE",
        weekly="NONE",
        daily="NONE",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=True,
    )
    states = tuple(
        RiskStructureStateFacts(
            fact=RiskStructureStateFact(
                period=period,
                state="NONE",
                observed_at=NOW,
                evidence_bar_end=None,
                mapping_unique=True,
                mapped_center_id=None,
                pen_definition_mode="ORIGINAL_OLD_PEN",
                source_revision=SHA,
            ),
            active_top_interval=None,
            mapped_center_id=None,
            mapping_candidate_ids=(),
            blockers=(),
        )
        for period in ("M", "W", "D")
    )
    structure = BenchmarkStructureRiskFacts(
        states=states,
        completed_30m_prefix_count=1,
        blockers=(),
    )
    inputs = QmtHigherTimeframeInputs(
        symbol=symbol,
        observed_at=NOW,
        daily_bars=(),
        completed_30m_bars=(),
        price_basis_revision=SHA,
        source_base_stream_revision=SHA,
        source_revision=SHA,
        blockers=(),
        source_base_frequency="1m",
    )
    risk = HigherTimeframeRiskFacts(
        snapshot=snapshot,
        period_bars=(),
        ma5=(("M", Decimal("10")), ("W", Decimal("10")), ("D", Decimal("10"))),
        blockers=(),
    )
    return QmtHigherTimeframeRiskEnvelope(
        inputs=inputs,
        structure=structure,
        risk=risk,
        warmup=QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=480,
            full_daily_bar_count=480,
            suffix_daily_bar_count=320,
            converged=True,
            reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
            full_signature=SHA,
            suffix_signature=SHA,
        ),
        grade="FULL_SYSTEM_ELIGIBLE",
        blockers=(),
    )


def selection() -> IndividualSelectionFacts:
    snapshot = SelectionResearchSnapshot(
        snapshot_id="research:600000",
        symbol=SYMBOL,
        path="INDIVIDUAL_THREE_PROGRAM",
        effective_at=NOW - timedelta(days=1),
        known_at=NOW - timedelta(days=2),
        valid_until=NOW + timedelta(days=30),
        reviewer="reviewer-a",
        signature="signed:v1",
        official_evidence_ids=("evidence:a",),
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        point_in_time_total_market_cap=Decimal("10000000000"),
        peer_set_id=SHA,
    )
    return IndividualSelectionFacts(
        snapshot=snapshot,
        grade="FULL_SYSTEM_ELIGIBLE",
        evidence_bundle_id="bundle:a",
        service_catalog_id=FINANCIAL_SERVICE_CATALOG_ID,
        blockers=(),
    )


def trigger():
    assessment = SectorAssessment(
        sector_id=SECTOR,
        sector_name="bank",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("structural_ranking_only",),
    )
    return build_sector_trigger_snapshot(
        symbol=SYMBOL,
        assessment=assessment,
        observed_at=NOW,
        source="QMT_GICS3_CURRENT",
        catalog_revision=SHA,
        catalog_captured_at=NOW - timedelta(hours=1),
        membership_known_at=NOW - timedelta(hours=1),
        membership_valid_until=NOW.replace(hour=23, minute=59),
        members=(SYMBOL, "600036.SH"),
        latest_completed_bar_at=NOW.replace(minute=0),
        expected_latest_bar_at=NOW.replace(minute=0),
        data_complete=True,
    )


def tradeability() -> TradeabilitySnapshot:
    return TradeabilitySnapshot(
        symbol=SYMBOL,
        observed_at=NOW,
        listed=True,
        st=False,
        suspended=False,
        reliable_continuous_market_data=True,
        continuity_status="ACTIVE",
        structure_history_sufficient=True,
        price_tick=Decimal("0.01"),
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        fee_schedule_id="fees:v1",
        price_limits_known=True,
        trading_calendar_known=True,
        completed_daily_volume_sessions=20,
        completed_same_clock_l2_sessions=20,
        median_daily_raw_volume=Decimal("10000000"),
        median_same_clock_l2_volume=Decimal("100000"),
        quote_coverage=Decimal("0.99"),
        median_spread_ticks=Decimal("2"),
        current_quote_valid_and_fresh=True,
        q_liquidity_cap=5000,
    )


def technical() -> TechnicalEntrySnapshot:
    return TechnicalEntrySnapshot(
        structure_snapshot_id="structure:v1",
        observed_at=NOW,
        price_basis_revision=SHA,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=True,
        all_components_completed=True,
        l0_center_id="center:l0:1",
        l0_center_ordinal=1,
        l0_center_completed=True,
        l0_point_type="3buy",
        l0_point_id="point:l0:3buy",
        l0_point_confirmation_time=NOW - timedelta(minutes=1),
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=Decimal("10"),
        l0_zg=Decimal("10"),
        l2_locator="L2_FIRST_BUY",
        l2_point_id="point:l2:1buy",
        l2_confirmation_bar_high=Decimal("10.2"),
    )


def build(**risk_overrides):
    market = risk_overrides.get("market", green_risk_envelope("SH.000001"))
    sector = risk_overrides.get("sector", green_risk_envelope(SECTOR))
    symbol = risk_overrides.get("symbol", green_risk_envelope(SYMBOL))
    return build_individual_candidate_decision(
        decision_time=NOW,
        selection=selection(),
        sector_trigger=trigger(),
        market_risk=market,
        sector_risk=sector,
        symbol_risk=symbol,
        tradeability=tradeability(),
        sector_strength=SectorStrengthSnapshot(
            snapshot_id="strength:v1",
            sector_id=SECTOR,
            observed_at=NOW,
            anchor_session=date(2026, 6, 1),
            member_count=2,
            categories=((SYMBOL, 9), ("600036.SH", 8)),
            strength=Decimal("8.5"),
            rank=1,
        ),
        technical=technical(),
        account=AccountEntryGate(
            observed_at=NOW,
            operations_normal=True,
            reconciliation_passed=True,
            free_strategic_slot=True,
            drawdown=Decimal("0"),
            no_active_symbol_order=True,
        ),
    )


def test_complete_sector_first_individual_path_enters_shared_candidate_core() -> None:
    result = build()

    assert result.full_system_eligible is True
    assert result.decision is not None and result.decision.accepted is True
    assert "PASS_QMT_SECTOR_TRIGGER_POINT_IN_TIME" in result.decision.passed_reason_codes
    assert "PASS_30M_5M_1M_WINDOWS" in result.decision.passed_reason_codes


def test_valid_decision_with_noncertified_base_stream_stays_research_only() -> None:
    symbol = green_risk_envelope(SYMBOL)
    symbol = replace(symbol, grade="RESEARCH_ONLY")

    result = build(symbol=symbol)

    assert result.decision is not None and result.decision.accepted is True
    assert result.grade == "RESEARCH_ONLY"
    assert result.full_system_eligible is False


def test_missing_higher_timeframe_snapshot_prevents_candidate_evaluation() -> None:
    symbol = green_risk_envelope(SYMBOL)
    symbol = replace(
        symbol,
        risk=replace(symbol.risk, snapshot=None),
        grade="UNRESOLVED",
    )

    result = build(symbol=symbol)

    assert result.decision is None
    assert result.grade == "UNRESOLVED"
    assert "SYMBOL_HIGHER_TIMEFRAME_RISK_UNAVAILABLE" in {
        blocker.code for blocker in result.blockers
    }
