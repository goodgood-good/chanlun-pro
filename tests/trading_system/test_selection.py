from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from chanlun.decision_support.trading_system.parameters import (
    etf_parameter_snapshot,
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.models import SectorAssessment
from chanlun.decision_support.trading_system.sector_trigger import (
    build_current_qmt_sector_trigger,
    build_sector_trigger_snapshot,
)
from chanlun.decision_support.trading_system.selection import (
    AccountEntryGate,
    CandidateSnapshot,
    CompletedDailyClose,
    HigherTimeframeRiskSnapshot,
    SectorMemberHistory,
    SectorStrengthSnapshot,
    SelectionResearchSnapshot,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
    advance_top_risk_state,
    build_sector_strength_snapshot,
    calculate_entry_liquidity_cap,
    completed_ma5_at,
    completed_sma,
    evaluate_candidate,
    member_ma_strength_category,
    rank_candidate_decisions,
)


CN = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 24, 14, 31, tzinfo=CN)


def green_risk(name: str) -> HigherTimeframeRiskSnapshot:
    return HigherTimeframeRiskSnapshot(
        snapshot_id=name,
        observed_at=NOW,
        monthly="NONE",
        weekly="RESOLVED_CONTINUATION",
        daily="INTERMEDIATE",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=True,
    )


def valid_sector_trigger(*, symbol: str, source: str = "QMT_SW1_PIT"):
    assessment = SectorAssessment(
        sector_id="SW1:bank",
        sector_name="bank",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("structural_ranking_only",),
    )
    return build_sector_trigger_snapshot(
        symbol=symbol,
        assessment=assessment,
        observed_at=NOW,
        source=source,
        catalog_revision="sha256:" + "a" * 64,
        catalog_captured_at=NOW - timedelta(hours=1),
        membership_known_at=NOW - timedelta(days=30),
        membership_valid_until=NOW + timedelta(days=30),
        members=tuple(sorted((symbol, "SH.600036"))),
        latest_completed_bar_at=NOW - timedelta(minutes=1),
        expected_latest_bar_at=NOW - timedelta(minutes=1),
        data_complete=True,
    )


def valid_candidate(*, symbol: str = "SH.600000") -> CandidateSnapshot:
    research = SelectionResearchSnapshot(
        snapshot_id=f"research:{symbol}",
        symbol=symbol,
        path="INDIVIDUAL_THREE_PROGRAM",
        effective_at=NOW - timedelta(days=2),
        known_at=NOW - timedelta(days=3),
        valid_until=NOW + timedelta(days=20),
        reviewer="reviewer-a",
        signature="signed:test",
        official_evidence_ids=("filing:2026q2",),
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        point_in_time_total_market_cap=Decimal("10000000000"),
        peer_set_id="peer:bank:202607",
    )
    tradeability = TradeabilitySnapshot(
        symbol=symbol,
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
        fee_schedule_id="broker-fees:202607",
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
    strength = SectorStrengthSnapshot(
        snapshot_id="sector-strength:test",
        sector_id="SW1:bank",
        observed_at=NOW,
        anchor_session=date(2026, 6, 1),
        member_count=2,
        categories=(("SH.600000", 8), ("SH.600036", 9)),
        strength=Decimal("8.5"),
        rank=1,
    )
    technical = TechnicalEntrySnapshot(
        structure_snapshot_id="strict:snapshot:test",
        observed_at=NOW,
        price_basis_revision="pit-adjustment:test",
        stroke_mode="strict-cl-k-distance",
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
        first_return_low=Decimal("10.00"),
        l0_zg=Decimal("10.00"),
        l2_locator="L2_FIRST_BUY",
        l2_point_id="point:l2:1buy",
        l2_confirmation_bar_high=Decimal("10.20"),
    )
    account = AccountEntryGate(
        observed_at=NOW,
        operations_normal=True,
        reconciliation_passed=True,
        free_strategic_slot=True,
        drawdown=Decimal("0.099"),
        no_active_symbol_order=True,
    )
    return CandidateSnapshot(
        symbol=symbol,
        market="A",
        sector_id="SW1:bank",
        decision_time=NOW,
        research=research,
        tradeability=tradeability,
        market_risk=green_risk("market"),
        sector_risk=green_risk("sector"),
        symbol_risk=green_risk("symbol"),
        sector_strength=strength,
        technical=technical,
        account=account,
        sector_trigger=valid_sector_trigger(symbol=symbol),
    )


def test_valid_individual_candidate_accepts_equal_third_buy_boundary() -> None:
    decision = evaluate_candidate(valid_candidate(), individual_parameter_snapshot())
    assert decision.accepted is True
    assert "PASS_THIRD_BUY_ABOVE_OR_EQUAL_ZG" in decision.passed_reason_codes
    assert decision.rejected_reason_codes == ()


def test_individual_candidate_without_sector_trigger_fails_closed() -> None:
    candidate = replace(valid_candidate(), sector_trigger=None)

    decision = evaluate_candidate(candidate, individual_parameter_snapshot())

    assert decision.accepted is False
    assert (
        "REJECT_QMT_SECTOR_TRIGGER_MISSING_OR_INVALID"
        in decision.rejected_reason_codes
    )


def test_current_qmt_membership_cannot_be_backfilled_to_another_session() -> None:
    trigger = valid_sector_trigger(
        symbol="SH.600000",
        source="QMT_GICS3_CURRENT",
    )

    assert trigger.passes(NOW) is True
    assert trigger.passes(NOW + timedelta(days=1)) is False


def test_current_qmt_catalog_and_completed_sector_frame_build_trigger() -> None:
    sector_id = "SW1:bank"
    catalog = {
        "source": "qmt_gics3_components",
        "captured_at": (NOW - timedelta(hours=1)).isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": "sha256:" + "a" * 64,
        "sectors": [
            {
                "sector_id": sector_id,
                "name": "bank",
                "member_codes": ["SH.600000", "SH.600036"],
            }
        ],
    }
    frame = pd.DataFrame(
        {
            "date": [NOW - timedelta(minutes=1)],
            "open": [1000],
            "high": [1001],
            "low": [999],
            "close": [1000],
            "volume": [2],
        }
    )
    frame.attrs["sector_membership_revision"] = "sha256:" + "d" * 64
    assessment = SectorAssessment(
        sector_id=sector_id,
        sector_name="bank",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("structural_ranking_only",),
    )

    trigger = build_current_qmt_sector_trigger(
        symbol="SH.600000",
        assessment=assessment,
        catalog=catalog,
        sector_frame=frame,
        decision_time=NOW,
        expected_latest_bar_at=NOW - timedelta(minutes=1),
    )

    assert trigger.passes(NOW) is True
    assert trigger.market_data_membership_revision == "sha256:" + "d" * 64


def test_user_override_independent_timeframes_can_replace_direct_recursion_gate() -> None:
    candidate = valid_candidate()
    candidate = replace(
        candidate,
        technical=replace(
            candidate.technical,
            direct_recursive_levels_unique=False,
            level_relation_mode="USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
            level_relation_contract_id="sha256:independent-timeframes",
        ),
    )

    decision = evaluate_candidate(candidate, individual_parameter_snapshot())

    assert decision.accepted is True
    assert (
        "PASS_USER_OVERRIDE_INDEPENDENT_TIMEFRAMES"
        in decision.passed_reason_codes
    )


def test_drawdown_equal_to_halt_boundary_is_rejected_with_all_reasons() -> None:
    candidate = valid_candidate()
    candidate = replace(
        candidate,
        account=replace(candidate.account, drawdown=Decimal("0.10")),
    )
    decision = evaluate_candidate(candidate, individual_parameter_snapshot())
    assert decision.accepted is False
    assert "REJECT_DRAWDOWN_10PCT_OR_MORE" in decision.rejected_reason_codes
    assert "PASS_STRICT_STROKE_MODE" in decision.passed_reason_codes


def test_unresolved_research_and_missing_quote_fail_closed() -> None:
    candidate = valid_candidate()
    candidate = replace(
        candidate,
        research=replace(
            candidate.research,
            industry_opportunity_status="UNRESOLVED",
            relative_value_status="UNRESOLVED",
        ),
        tradeability=replace(candidate.tradeability, quote_coverage=None),
    )
    decision = evaluate_candidate(candidate, individual_parameter_snapshot())
    assert decision.accepted is False
    assert {
        "REJECT_INDUSTRY_OPPORTUNITY",
        "REJECT_RELATIVE_VALUE",
        "REJECT_QUOTE_COVERAGE",
    }.issubset(decision.rejected_reason_codes)


def test_st_and_inconsistent_liquidity_cap_fail_closed() -> None:
    candidate = valid_candidate()
    candidate = replace(
        candidate,
        tradeability=replace(candidate.tradeability, st=True, q_liquidity_cap=5100),
    )
    decision = evaluate_candidate(candidate, individual_parameter_snapshot())
    assert "REJECT_ST" in decision.rejected_reason_codes
    assert (
        "REJECT_LIQUIDITY_CAP_UNRESOLVED_OR_INCONSISTENT"
        in decision.rejected_reason_codes
    )
    assert (
        calculate_entry_liquidity_cap(
            candidate.tradeability, individual_parameter_snapshot()
        )
        == 5000
    )


def test_etf_proxy_cannot_run_under_individual_parameter_snapshot() -> None:
    candidate = valid_candidate(symbol="SH.510300")
    research = SelectionResearchSnapshot(
        snapshot_id="research:etf",
        symbol=candidate.symbol,
        path="ETF_PROXY",
        effective_at=NOW - timedelta(days=2),
        known_at=NOW - timedelta(days=3),
        valid_until=NOW + timedelta(days=20),
        reviewer="reviewer-etf",
        signature="signed:etf:test",
        official_evidence_ids=("index-methodology:test",),
        industry_opportunity_status="NOT_APPLICABLE",
        fundamental_role="ETF_PROXY",
        relative_value_status="ETF_PROXY",
        point_in_time_total_market_cap=None,
        peer_set_id=None,
        basket_mapping_id="basket:hs300:20260724",
    )
    candidate = replace(candidate, research=research)
    assert evaluate_candidate(candidate, etf_parameter_snapshot()).accepted is True
    mismatch = evaluate_candidate(candidate, individual_parameter_snapshot())
    assert mismatch.accepted is False
    assert "REJECT_SELECTION_PATH_MISMATCH" in mismatch.rejected_reason_codes


def test_candidate_ranking_is_frozen_and_deterministic() -> None:
    leader = evaluate_candidate(valid_candidate(symbol="SH.600000"), individual_parameter_snapshot())
    challenger_snapshot = valid_candidate(symbol="SZ.000001")
    challenger_snapshot = replace(
        challenger_snapshot,
        research=replace(
            challenger_snapshot.research,
            fundamental_role="GROWTH_CHALLENGER",
        ),
        sector_strength=replace(
            challenger_snapshot.sector_strength,
            categories=(("SH.600000", 9), ("SH.600036", 9)),
            strength=Decimal("9"),
        ),
    )
    challenger = evaluate_candidate(challenger_snapshot, individual_parameter_snapshot())
    ranked = rank_candidate_decisions((challenger, leader))
    assert tuple(value.symbol for value in ranked) == ("SH.600000", "SZ.000001")


def test_candidate_ranking_prefers_strict_green_before_role_and_strength() -> None:
    """§3.3: 高周期可买必须先于基本面角色和板块强弱。"""

    amber_leader = replace(
        evaluate_candidate(
            valid_candidate(symbol="SH.600000"),
            individual_parameter_snapshot(),
        ),
        sector_strength=Decimal("9"),
        higher_timeframe_risk_buyable=False,
    )
    green_challenger = replace(
        evaluate_candidate(
            valid_candidate(symbol="SZ.000001"),
            individual_parameter_snapshot(),
        ),
        fundamental_role="GROWTH_CHALLENGER",
        relative_value_status="FAIR",
        sector_strength=Decimal("1"),
        higher_timeframe_risk_buyable=True,
    )

    ranked = rank_candidate_decisions((amber_leader, green_challenger))

    assert tuple(value.symbol for value in ranked) == ("SZ.000001", "SH.600000")


def test_sector_strength_retains_missing_member_as_unresolved() -> None:
    member = SectorMemberHistory(
        symbol="SH.600000",
        listed_on=date(2000, 1, 1),
        history_status="UNEXPLAINED_GAP",
        closes=(),
    )
    snapshot = build_sector_strength_snapshot(
        snapshot_id="sector:gap",
        sector_id="SW1:bank",
        anchor_session=date(2026, 6, 1),
        decision_time=NOW,
        members=(member,),
        rank=1,
    )
    assert snapshot.resolved is False
    assert snapshot.member_count == 1
    assert snapshot.strength is None


def test_ma_equal_to_close_is_not_counted_as_standing_above() -> None:
    closes = tuple(
        CompletedDailyClose(
            session=date(2026, 6, 1) + timedelta(days=index),
            close=Decimal("10"),
            known_at=NOW - timedelta(days=20 - index),
        )
        for index in range(10)
    )
    member = SectorMemberHistory(
        symbol="SH.600000",
        listed_on=date(2000, 1, 1),
        history_status="COMPLETE",
        closes=closes,
    )
    assert member_ma_strength_category(
        member,
        anchor_session=date(2026, 6, 1),
        decision_time=NOW,
    ) == 1
    assert completed_sma((Decimal("1"),) * 4, 5) is None


def test_top_fractal_risk_state_machine_matches_frozen_transitions() -> None:
    formed = advance_top_risk_state("NONE", "TOP_FRACTAL_MAPPING_UNIQUE")
    red = advance_top_risk_state(formed.current, "CENTER_THIRD_SELL_UNEXTENDED")
    cleared = advance_top_risk_state(
        red.current, "OPPOSITE_FRACTAL_COMPLETES_DOWN_PEN"
    )
    assert (formed.current, red.current, cleared.current) == (
        "FORMED",
        "PEN_RISK_CONFIRMED",
        "NONE",
    )
    with pytest.raises(ValueError, match="unresolved top-risk transition"):
        advance_top_risk_state("NONE", "CENTER_THIRD_BUY")


def test_ma5_uses_only_five_completed_point_in_time_visible_closes() -> None:
    rows = tuple(
        CompletedDailyClose(
            session=date(2026, 7, 14) + timedelta(days=index),
            close=Decimal(index + 1),
            known_at=NOW - timedelta(days=5 - index),
            completed=True,
        )
        for index in range(5)
    ) + (
        CompletedDailyClose(
            session=NOW.date(),
            close=Decimal("100"),
            known_at=NOW + timedelta(minutes=1),
            completed=True,
        ),
    )
    assert completed_ma5_at(rows, decision_time=NOW) == Decimal("3")


def test_known_nonunique_top_mapping_is_explicit_amber_not_unknown() -> None:
    risk = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:known-nonunique",
        observed_at=NOW,
        monthly="NONE",
        weekly="FORMED_UNRESOLVED",
        daily="NONE",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=False,
    )
    assert risk.gate == "AMBER"


def test_confirmed_red_risk_has_priority_over_amber_at_any_lower_period() -> None:
    risk = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:red-over-amber",
        observed_at=NOW,
        monthly="FORMED",
        weekly="NONE",
        daily="PEN_RISK_CONFIRMED",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=True,
    )
    assert risk.gate == "RED"


def test_equal_amber_boundary_rejects_entry_without_hiding_other_checks() -> None:
    candidate = valid_candidate()
    amber = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:amber",
        observed_at=NOW,
        monthly="NONE",
        weekly="FORMED_UNRESOLVED",
        daily="NONE",
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=False,
    )
    candidate = replace(candidate, market_risk=amber)
    decision = evaluate_candidate(candidate, individual_parameter_snapshot())
    assert decision.accepted is False
    assert "REJECT_MARKET_RISK_AMBER" in decision.rejected_reason_codes
    assert "PASS_THIRD_BUY_ABOVE_OR_EQUAL_ZG" in decision.passed_reason_codes
