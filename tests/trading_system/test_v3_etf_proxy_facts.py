from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    DailyMarketBar,
    EtfProxyPitRepository,
    EtfTrackingMapping,
    FrozenStructureBar,
    RiskCenterPointEvidence,
    RiskDiagnosticBuyPointEvidenceFacts,
    RiskMappingPointEvidenceFacts,
    RiskStructureStateFact,
    aggregate_completed_period_bars,
    apply_qmt_causal_adjustments,
    build_benchmark_structure_risk_facts,
    build_etf_proxy_candidate_decision,
    build_higher_timeframe_risk_facts,
    build_risk_structure_state_fact,
    latest_completed_bottom_fractal_anchor,
    load_qmt_corporate_action_ledger,
    member_ma_strength_category_fast,
    select_unique_top_center_mapping,
)
from chanlun.decision_support.trading_system import v3_etf_proxy_facts as etf_facts
from chanlun.decision_support.trading_system.v3_selection import (
    AccountEntryGate,
    CompletedDailyClose,
    HigherTimeframeRiskSnapshot,
    SectorMemberHistory,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
    member_ma_strength_category,
)


CN = ZoneInfo("Asia/Shanghai")


def close_time(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 15, tzinfo=CN)


def market_bar(session: date, close: int) -> DailyMarketBar:
    value = Decimal(close)
    return DailyMarketBar(
        session=session,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("100"),
        known_at=close_time(session),
    )


def create_pit_database(
    path: Path,
    *,
    missing_second_member_bar: bool = False,
    membership_count: int = 2,
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memberships (
            candidate_session TEXT NOT NULL,
            source_update_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (candidate_session, code)
        ) WITHOUT ROWID;
        CREATE TABLE security_master (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            ipo_date TEXT NOT NULL,
            out_date TEXT,
            security_type TEXT NOT NULL,
            status TEXT NOT NULL,
            queried_at TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE daily_bars (
            code TEXT NOT NULL,
            session TEXT NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            previous_close TEXT NOT NULL,
            volume TEXT NOT NULL,
            amount TEXT NOT NULL,
            trade_status TEXT NOT NULL,
            is_st TEXT NOT NULL,
            PRIMARY KEY (code, session)
        ) WITHOUT ROWID;
        CREATE TABLE adjustment_factors (
            code TEXT NOT NULL,
            effective_on TEXT NOT NULL,
            forward_factor TEXT NOT NULL,
            backward_factor TEXT NOT NULL,
            adjustment_factor TEXT NOT NULL,
            PRIMARY KEY (code, effective_on)
        ) WITHOUT ROWID;
        CREATE TABLE trading_calendar (
            calendar_date TEXT PRIMARY KEY,
            is_trading_day TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    sessions = tuple(date(2020, 1, 2) + timedelta(days=index) for index in range(10))
    connection.executemany(
        "INSERT INTO trading_calendar VALUES (?, '1')",
        ((session.isoformat(),) for session in sessions),
    )
    decision_session = sessions[-1]
    members = ["sh.600000", "sz.000001"] + [
        f"sz.{index:06d}" for index in range(2, membership_count)
    ]
    connection.executemany(
        "INSERT INTO memberships VALUES (?, ?, ?, ?)",
        (
            (
                decision_session.isoformat(),
                sessions[0].isoformat(),
                code,
                code,
            )
            for code in members
        ),
    )
    for code in ("sh.600000", "sz.000001"):
        connection.execute(
            "INSERT INTO security_master VALUES (?, ?, ?, '', '1', '1', ?)",
            (code, code, sessions[0].isoformat(), close_time(decision_session).isoformat()),
        )
        connection.executemany(
            "INSERT INTO adjustment_factors VALUES (?, ?, '1', ?, ?)",
            (
                (code, sessions[0].isoformat(), "1", "1"),
                # A future factor must not rewrite any earlier close.
                (code, date(2021, 1, 1).isoformat(), "2", "2"),
            ),
        )
        for index, session in enumerate(sessions):
            if missing_second_member_bar and code == "sz.000001" and index == 5:
                continue
            close = str(index + 10)
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, '100', '1000', '1', '0')",
                (code, session.isoformat(), close, close, close, close, close),
            )
    connection.commit()
    connection.close()


def test_week_and_month_bars_require_completed_calendar_periods() -> None:
    sessions = (
        date(2020, 1, 27),
        date(2020, 1, 28),
        date(2020, 1, 29),
        date(2020, 1, 30),
        date(2020, 1, 31),
        date(2020, 2, 3),
        date(2020, 2, 4),
        date(2020, 2, 5),
        date(2020, 2, 6),
        date(2020, 2, 7),
    )
    rows = tuple(market_bar(session, 10 + index) for index, session in enumerate(sessions))
    mid_second_week = close_time(date(2020, 2, 5))
    weeks = aggregate_completed_period_bars(
        rows,
        trading_sessions=sessions,
        decision_time=mid_second_week,
        period="W",
        calendar_coverage_end=date(2020, 2, 9),
    )
    months = aggregate_completed_period_bars(
        rows,
        trading_sessions=sessions,
        decision_time=mid_second_week,
        period="M",
        calendar_coverage_end=date(2020, 2, 29),
    )
    assert tuple(row.period_key for row in weeks) == ("2020-W05",)
    assert tuple(row.period_key for row in months) == ("2020-01",)


def test_high_timeframe_risk_fails_closed_without_structure_states() -> None:
    sessions = tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(900))
    rows = tuple(market_bar(session, 100 + (index % 7)) for index, session in enumerate(sessions))
    decision = close_time(sessions[-1])
    result = build_higher_timeframe_risk_facts(
        rows,
        trading_sessions=sessions,
        calendar_coverage_end=date(2020, 12, 31),
        decision_time=decision,
        structure_states=(),
        snapshot_id="risk:test",
    )
    assert result.snapshot is None
    assert result.gate == "UNRESOLVED"
    assert {
        "M_FROZEN_STRUCTURE_RISK_FACT_MISSING",
        "W_FROZEN_STRUCTURE_RISK_FACT_MISSING",
        "D_FROZEN_STRUCTURE_RISK_FACT_MISSING",
    }.issubset({blocker.code for blocker in result.blockers})


def test_fast_basket_category_matches_frozen_selection_rule() -> None:
    start = date(2019, 1, 1)
    decision = close_time(start + timedelta(days=279))
    closes = tuple(
        CompletedDailyClose(
            session=start + timedelta(days=index),
            close=Decimal(100 + ((index * 17) % 31) - (index % 7)),
            known_at=close_time(start + timedelta(days=index)),
        )
        for index in range(280)
    )
    member = SectorMemberHistory(
        symbol="sh.600000",
        listed_on=start,
        history_status="COMPLETE",
        closes=closes,
    )
    expected = member_ma_strength_category(
        member,
        anchor_session=start + timedelta(days=240),
        decision_time=decision,
    )
    actual = member_ma_strength_category_fast(
        closes,
        anchor_session=start + timedelta(days=240),
        decision_time=decision,
    )
    assert actual == expected


def test_bottom_fractal_anchor_uses_third_completed_bar_and_old_pen() -> None:
    values = (
        10,
        11,
        12,
        11,
        10,
        9,
        10,
        11,
        12,
        13,
        12,
        11,
        10,
        9,
        8,
        9,
        10,
        11,
        10,
        9,
        8,
        7,
        8,
        9,
        10,
        11,
        10,
    )
    start = date(2020, 1, 1)
    rows = tuple(
        market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = close_time(rows[-1].session)
    result = latest_completed_bottom_fractal_anchor(
        rows,
        decision_time=decision,
        symbol="CSI.000300",
    )
    assert result.resolved
    assert result.pen_definition_mode == "ORIGINAL_OLD_PEN"
    assert result.fractal_middle_session == date(2020, 1, 22)
    assert result.anchor_session == date(2020, 1, 23)
    # Supplying future rows while keeping the same decision prefix cannot
    # alter the historical anchor.
    future = rows + tuple(
        market_bar(start + timedelta(days=27 + index), 20 + index)
        for index in range(5)
    )
    assert latest_completed_bottom_fractal_anchor(
        future,
        decision_time=decision,
        symbol="CSI.000300",
    ).anchor_session == result.anchor_session


def test_center_mapping_boundaries_are_inclusive_and_highest_level_wins() -> None:
    start = datetime(2020, 1, 1, 15, tzinfo=CN)
    end = datetime(2020, 1, 31, 15, tzinfo=CN)
    evidence = (
        RiskCenterPointEvidence(
            center_id="lower-at-start",
            center_level_rank=1,
            center_completed=True,
            center_expanded=False,
            point_type="1sell",
            point_anchor_at=start,
            point_available_at=start,
        ),
        RiskCenterPointEvidence(
            center_id="highest-at-end",
            center_level_rank=2,
            center_completed=True,
            center_expanded=False,
            point_type="2sell",
            point_anchor_at=end,
            point_available_at=end,
        ),
        RiskCenterPointEvidence(
            center_id="center-a",
            center_level_rank=2,
            center_completed=True,
            center_expanded=False,
            point_type="3sell",
            point_anchor_at=end,
            point_available_at=end,
        ),
    )
    mapped, candidates = select_unique_top_center_mapping(
        evidence,
        interval_start=start,
        interval_end=end,
        decision_time=end,
    )
    assert mapped == "highest-at-end"
    assert candidates == ("highest-at-end",)


def test_two_highest_centers_or_future_point_remain_unresolved() -> None:
    start = datetime(2020, 1, 1, 15, tzinfo=CN)
    end = datetime(2020, 1, 31, 15, tzinfo=CN)
    evidence = tuple(
        RiskCenterPointEvidence(
            center_id=center_id,
            center_level_rank=2,
            center_completed=True,
            center_expanded=False,
            point_type="1sell",
            point_anchor_at=start,
            point_available_at=end,
        )
        for center_id in ("a", "b")
    ) + (
        RiskCenterPointEvidence(
            center_id="future",
            center_level_rank=3,
            center_completed=True,
            center_expanded=False,
            point_type="2sell",
            point_anchor_at=end,
            point_available_at=end + timedelta(days=1),
        ),
    )
    mapped, candidates = select_unique_top_center_mapping(
        evidence,
        interval_start=start,
        interval_end=end,
        decision_time=end,
    )
    assert mapped is None
    assert candidates == ("a", "b")


def test_mapping_supply_explains_failure_layer_without_relaxing_selector() -> None:
    start = datetime(2020, 1, 10, 15, tzinfo=CN)
    end = datetime(2020, 1, 20, 15, tzinfo=CN)

    def point(
        center_id: str,
        point_type: str,
        anchor: datetime,
        *,
        completed: bool = True,
    ) -> RiskCenterPointEvidence:
        return RiskCenterPointEvidence(
            center_id=center_id,
            center_level_rank=2,
            center_completed=completed,
            center_expanded=False,
            point_type=point_type,  # type: ignore[arg-type]
            point_anchor_at=anchor,
            point_available_at=end,
        )

    cases = (
        (
            (point("third", "3sell", start), point("buy", "3buy", end)),
            "ONLY_THIRD_CLASS_POINTS",
        ),
        (
            (point("outside", "1sell", start - timedelta(days=1)),),
            "SELL12_OUTSIDE_TOP_FRACTAL",
        ),
        (
            (point("forming", "2sell", start, completed=False),),
            "SELL12_CENTER_INCOMPLETE",
        ),
        (
            (point("a", "1sell", start), point("b", "2sell", end)),
            "HIGHEST_MAPPING_NOT_UNIQUE",
        ),
        ((point("unique", "1sell", start),), "UNIQUE_MAPPING"),
    )
    for evidence, expected in cases:
        _mapped, candidates = select_unique_top_center_mapping(
            evidence,
            interval_start=start,
            interval_end=end,
            decision_time=end,
        )
        supply = etf_facts._risk_mapping_supply_facts(
            evidence,
            lower_structure_available=True,
            interval=(start, end),
            mapping_candidate_ids=candidates,
        )
        assert supply.classification == expected
        assert type(supply).from_document(supply.document()) == supply


def test_mapping_supply_retains_stable_causal_point_identity() -> None:
    start = datetime(2020, 1, 10, 15, tzinfo=CN)
    end = datetime(2020, 1, 20, 15, tzinfo=CN)
    evidence = (
        RiskCenterPointEvidence(
            center_id="center-a",
            center_level_rank=2,
            center_completed=True,
            center_expanded=False,
            point_type="1sell",
            point_anchor_at=start,
            point_available_at=end,
        ),
        RiskCenterPointEvidence(
            center_id="center-a",
            center_level_rank=2,
            center_completed=True,
            center_expanded=False,
            point_type="3sell",
            point_anchor_at=end,
            point_available_at=end,
        ),
    )
    supply = etf_facts._risk_mapping_supply_facts(
        evidence,
        lower_structure_available=True,
        interval=(start, end),
        mapping_candidate_ids=("center-a",),
        source_symbol="SH.000001",
        source_frequency="30m",
    )

    assert supply.point_evidence is not None
    assert len(supply.point_evidence) == 2
    point = supply.point_evidence[0]
    assert point.point_id == RiskMappingPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="30m",
        center_id="center-a",
        center_level_rank=2,
        point_type="1sell",
        point_anchor_at=start,
        point_available_at=end,
    )
    assert point.inside_active_top_interval is True
    assert point.highest_mapping_candidate is True
    assert supply.point_evidence[1].point_type == "3sell"
    assert supply.point_evidence[1].highest_mapping_candidate is False
    assert type(supply).from_document(supply.document()) == supply

    # Event-relative membership may change, but the structural point identity
    # must remain stable across a later top-fractal interval.
    later = etf_facts._risk_mapping_supply_facts(
        evidence,
        lower_structure_available=True,
        interval=(end + timedelta(days=1), end + timedelta(days=2)),
        mapping_candidate_ids=(),
        source_symbol="SH.000001",
        source_frequency="30m",
    )
    assert later.point_evidence is not None
    assert later.point_evidence[0].point_id == point.point_id
    assert later.point_evidence[0].inside_active_top_interval is False
    assert later.point_evidence[0].highest_mapping_candidate is False

    forged = supply.document()
    forged["point_evidence"][0]["source_symbol"] = "SH.000002"  # type: ignore[index]
    with pytest.raises(ValueError, match="identity does not match"):
        type(supply).from_document(forged)


def test_buy_side_supply_is_causal_diagnostic_only_and_cannot_map() -> None:
    """一/二买解释方向供给，但不能成为顶分型的一/二卖映射。"""

    decision = datetime(2020, 1, 20, 15, tzinfo=CN)
    center_start = datetime(2020, 1, 2, 15, tzinfo=CN)
    center_end = datetime(2020, 1, 10, 15, tzinfo=CN)
    center = SimpleNamespace(
        index=7,
        zd=Decimal("9"),
        zg=Decimal("11"),
        type="zd",
        done=True,
        real=True,
        expanded_with=(),
        start=SimpleNamespace(
            start=SimpleNamespace(k=SimpleNamespace(date=center_start))
        ),
        end=SimpleNamespace(
            end=SimpleNamespace(k=SimpleNamespace(date=center_end))
        ),
    )

    def line(
        point_type: str,
        *,
        locked_at: datetime,
        completed: bool = True,
    ) -> SimpleNamespace:
        point = SimpleNamespace(name=point_type, zs=center)
        return SimpleNamespace(
            locked_at=locked_at,
            end=SimpleNamespace(k=SimpleNamespace(date=center_end)),
            is_done=lambda: completed,
            get_mmds=lambda: (point,),
        )

    state = SimpleNamespace(
        get_bi_zss=lambda: (center,),
        get_xd_zss=lambda: (),
        get_bis=lambda: (
            # Equality boundary: available exactly at decision is visible.
            line("1buy", locked_at=decision),
            # A future lock and an unfinished line are both excluded.
            line("2buy", locked_at=decision + timedelta(minutes=1)),
            line("2buy", locked_at=decision, completed=False),
        ),
        get_xds=lambda: (),
    )
    all_evidence = etf_facts._lower_risk_evidence(
        state,
        frequency="30m",
        decision_time=decision,
    )
    assert tuple(row.point_type for row in all_evidence) == ("1buy",)

    mapped, candidates = select_unique_top_center_mapping(
        all_evidence,
        interval_start=center_start,
        interval_end=decision,
        decision_time=decision,
    )
    assert mapped is None
    assert candidates == ()

    supply = etf_facts._risk_mapping_supply_facts(
        (),
        diagnostic_buy_evidence=all_evidence,
        lower_structure_available=True,
        interval=(center_start, decision),
        mapping_candidate_ids=(),
        source_symbol="SH.000001",
        source_frequency="30m",
    )
    assert supply.classification == "NO_LOWER_POINT_EVIDENCE"
    assert supply.point_evidence_count == 0
    assert supply.diagnostic_buy_point_type_counts == (
        ("1buy", 1),
        ("2buy", 0),
    )
    assert (
        supply.diagnostic_directional_classification
        == "BUY12_PRESENT_SELL12_ABSENT"
    )
    assert supply.diagnostic_buy_point_evidence is not None
    assert len(supply.diagnostic_buy_point_evidence) == 1
    diagnostic_point = supply.diagnostic_buy_point_evidence[0]
    assert isinstance(diagnostic_point, RiskDiagnosticBuyPointEvidenceFacts)
    assert diagnostic_point.point_type == "1buy"
    assert diagnostic_point.inside_active_top_interval is True
    assert diagnostic_point.point_id == RiskDiagnosticBuyPointEvidenceFacts.identity(
        source_symbol="SH.000001",
        source_frequency="30m",
        center_id=diagnostic_point.center_id,
        center_level_rank=diagnostic_point.center_level_rank,
        point_type="1buy",
        point_anchor_at=diagnostic_point.point_anchor_at,
        point_available_at=diagnostic_point.point_available_at,
    )
    assert type(supply).from_document(supply.document()) == supply

    forged = supply.document()
    forged["diagnostic_directional_classification"] = "SELL12_PRESENT"
    with pytest.raises(
        ValueError, match="diagnostic directional classification is inconsistent"
    ):
        type(supply).from_document(forged)

    promoted = supply.document()
    promoted["diagnostic_buy_point_evidence"][0]["mapping_eligible"] = True
    with pytest.raises(ValueError, match="promoted into mapping evidence"):
        type(supply).from_document(promoted)

    legacy = supply.document()
    del legacy["diagnostic_buy_point_type_counts"]
    del legacy["diagnostic_directional_classification"]
    del legacy["diagnostic_buy_point_evidence"]
    legacy_supply = type(supply).from_document(legacy)
    assert legacy_supply.diagnostic_buy_point_type_counts is None
    assert (
        legacy_supply.diagnostic_directional_classification
        == "NOT_RECORDED_LEGACY"
    )


def test_diagnostic_buys_do_not_change_unique_sell_mapping() -> None:
    start = datetime(2020, 1, 10, 15, tzinfo=CN)
    end = datetime(2020, 1, 20, 15, tzinfo=CN)
    sell = RiskCenterPointEvidence(
        center_id="sell-center",
        center_level_rank=2,
        center_completed=True,
        center_expanded=False,
        point_type="1sell",
        point_anchor_at=start,
        point_available_at=end,
    )
    buy = RiskCenterPointEvidence(
        center_id="higher-buy-center",
        center_level_rank=99,
        center_completed=True,
        center_expanded=False,
        point_type="1buy",
        point_anchor_at=end,
        point_available_at=end,
    )
    assert select_unique_top_center_mapping(
        (sell,),
        interval_start=start,
        interval_end=end,
        decision_time=end,
    ) == select_unique_top_center_mapping(
        (sell, buy),
        interval_start=start,
        interval_end=end,
        decision_time=end,
    )
    supply = etf_facts._risk_mapping_supply_facts(
        (sell,),
        diagnostic_buy_evidence=(buy,),
        lower_structure_available=True,
        interval=(start, end),
        mapping_candidate_ids=("sell-center",),
    )
    assert supply.classification == "UNIQUE_MAPPING"
    assert supply.diagnostic_directional_classification == "SELL12_PRESENT"


def test_old_pen_risk_adapter_emits_formed_unresolved_without_lower_mapping() -> None:
    values = (10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12)
    start = date(2020, 1, 1)
    bars = tuple(
        FrozenStructureBar(
            end_at=close_time(start + timedelta(days=index)),
            open=Decimal(value),
            high=Decimal(value) + Decimal("1"),
            low=Decimal(value) - Decimal("1"),
            close=Decimal(value),
            volume=Decimal("100"),
        )
        for index, value in enumerate(values)
    )
    decision = bars[-1].end_at
    result = build_risk_structure_state_fact(
        period="D",
        high_frequency="d",
        lower_frequency="30m",
        high_bars=bars,
        lower_bars=(),
        decision_time=decision,
        symbol="CSI.000300",
    )
    assert result.fact.state == "FORMED_UNRESOLVED"
    assert result.fact.mapping_unique is False
    assert result.blockers[0].code == (
        "NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL"
    )
    assert result.fact.pen_definition_mode == "ORIGINAL_OLD_PEN"
    assert result.mapping_supply is not None
    assert result.mapping_supply.classification == "LOWER_STRUCTURE_UNAVAILABLE"
    assert result.mapping_supply.point_evidence_count == 0


def qmt_snapshot(path: Path) -> None:
    event = {
        "effective_on": "2020-01-03",
        "availability_policy": "EFFECTIVE_SESSION_OPEN_RESEARCH_ASSUMPTION",
        "raw": {
            "time": 1.0,
            "interest": 0.0,
            "stockBonus": 0.0,
            "stockGift": 0.0,
            "allotNum": 0.0,
            "allotPrice": 0.0,
            "gugai": 0.0,
            "dr": 2.0,
        },
    }
    payload = {
        "schema": "chanlun-qmt-etf-corporate-actions/v1",
        "generated_at": "2026-07-26T00:00:00+08:00",
        "source_store_sha256": "sha256:source",
        "instruments": [
            {
                "code": "159919.SZ",
                "status": "EFFECTIVE_DATED_EVENTS_AVAILABLE",
                "provider_columns": [
                    "time",
                    "interest",
                    "stockBonus",
                    "stockGift",
                    "allotNum",
                    "allotPrice",
                    "gugai",
                    "dr",
                ],
                "events": [event],
            }
        ],
    }
    payload["content_sha256"] = etf_facts._qmt_snapshot_content_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_qmt_ledger_requires_authority_attestation_but_remains_research_usable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "qmt.json"
    qmt_snapshot(path)
    research = load_qmt_corporate_action_ledger(path, symbol="159919.SZ")
    assert research.ledger is not None
    assert research.grade == "RESEARCH_ONLY"
    assert research.blockers[0].code == (
        "QMT_CORPORATE_ACTION_AUTHORITY_ATTESTATION_MISSING"
    )
    certified = load_qmt_corporate_action_ledger(
        path,
        symbol="159919.SZ",
        expected_source_store_sha256="sha256:source",
        authority_attestation_id="authority:qmt-source-store:v1",
    )
    assert certified.grade == "FULL_SYSTEM_ELIGIBLE"
    assert certified.blockers == ()


def test_qmt_adjustment_applies_on_equal_effective_boundary_never_before(
    tmp_path: Path,
) -> None:
    path = tmp_path / "qmt.json"
    qmt_snapshot(path)
    ledger = load_qmt_corporate_action_ledger(path, symbol="159919.SZ")
    bars = tuple(
        FrozenStructureBar(
            end_at=close_time(date(2020, 1, 2) + timedelta(days=index)),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("100"),
        )
        for index in range(3)
    )
    adjusted = apply_qmt_causal_adjustments(
        bars,
        ledger_facts=ledger,
        decision_time=bars[-1].end_at,
    )
    assert tuple(bar.close for bar in adjusted.bars) == (
        Decimal("10"),
        Decimal("20"),
        Decimal("20"),
    )
    assert len(adjusted.applied_event_ids) == 1


def test_high_timeframe_risk_uses_completed_ma5_and_old_pen_states() -> None:
    sessions = tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(1100))
    rows = tuple(market_bar(session, 100 + (index % 11)) for index, session in enumerate(sessions))
    decision = close_time(sessions[-1])
    states = tuple(
        RiskStructureStateFact(
            period=period,
            state="NONE",
            observed_at=decision,
            evidence_bar_end=None,
            mapping_unique=True,
            mapped_center_id=None,
            pen_definition_mode="ORIGINAL_OLD_PEN",
            source_revision=f"source:{period}",
        )
        for period in ("M", "W", "D")
    )
    result = build_higher_timeframe_risk_facts(
        rows,
        trading_sessions=sessions,
        calendar_coverage_end=date(2021, 12, 31),
        decision_time=decision,
        structure_states=states,
        snapshot_id="risk:test",
    )
    assert result.snapshot is not None
    assert result.snapshot.gate == "GREEN"
    assert all(value is not None for _period, value in result.ma5)


def test_sparse_basket_is_never_carried_forward(tmp_path: Path) -> None:
    database = tmp_path / "pit.sqlite3"
    create_pit_database(database)
    repository = EtfProxyPitRepository(database)
    stored = repository.available_membership_sessions()[0]
    assert repository.exact_basket(stored) is not None
    assert repository.exact_basket(stored - timedelta(days=1)) is None


def test_selection_snapshot_is_research_only_without_authoritative_mapping(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pit.sqlite3"
    create_pit_database(database, membership_count=300)
    repository = EtfProxyPitRepository(database)
    session = repository.available_membership_sessions()[0]
    decision = close_time(session)
    mapping = EtfTrackingMapping(
        symbol="510300.SH",
        tracked_index="CSI.000300",
        known_at=close_time(date(2019, 1, 1)),
        effective_from=close_time(date(2019, 1, 1)),
        valid_until=close_time(date(2030, 1, 1)),
        evidence_ids=("RESEARCH_SOURCE:ETF_TRACKING_DECLARATION",),
        authoritative=False,
    )
    result = repository.build_selection_facts(
        mapping,
        decision_time=decision,
        reviewer="research-adapter",
        signature="research-only",
    )
    assert result.snapshot is not None
    assert result.snapshot.symbol == "SH.510300"
    assert result.grade == "RESEARCH_ONLY"
    assert {
        "BASKET_INTRADAY_PUBLICATION_TIMESTAMP_UNAVAILABLE",
        "ETF_TRACKING_MAPPING_NOT_AUTHORITATIVE",
    }.issubset({blocker.code for blocker in result.blockers})


def test_missing_member_history_rejects_whole_basket(tmp_path: Path) -> None:
    database = tmp_path / "pit.sqlite3"
    create_pit_database(database, missing_second_member_bar=True)
    repository = EtfProxyPitRepository(database)
    session = repository.available_membership_sessions()[0]
    result = repository.build_basket_strength_facts(
        decision_time=close_time(session),
        anchor_session=session - timedelta(days=4),
    )
    assert result.snapshot.resolved is False
    assert result.grade == "UNRESOLVED"
    assert "UNEXPLAINED_MEMBER_DAILY_GAP" in {
        blocker.code for blocker in result.blockers
    }


def test_future_adjustment_factor_does_not_change_historical_strength(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pit.sqlite3"
    create_pit_database(database)
    repository = EtfProxyPitRepository(database)
    session = repository.available_membership_sessions()[0]
    result = repository.build_basket_strength_facts(
        decision_time=close_time(session),
        anchor_session=session - timedelta(days=4),
    )
    assert result.snapshot.resolved is True
    assert result.snapshot.strength == Decimal("2")
    assert result.grade == "RESEARCH_ONLY"


def test_benchmark_daily_risk_consumes_equal_boundary_completed_30m_prefix(
    monkeypatch,
) -> None:
    values = (10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12)
    start = date(2020, 1, 1)
    rows = tuple(
        market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = rows[-1].known_at
    completed_30m = tuple(
        FrozenStructureBar(
            end_at=end_at,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10"),
            volume=Decimal("100"),
        )
        for end_at in (
            close_time(date(2020, 1, 9)),
            datetime(2020, 1, 10, 15, tzinfo=CN),
            close_time(date(2020, 1, 11)),
        )
    )
    original_structure = etf_facts._old_pen_structure_state
    lower_state = object()

    def structure_state(bars, *, symbol, frequency, decision_time):
        if frequency == "30m":
            assert tuple(bars) == completed_30m
            return lower_state
        return original_structure(
            bars,
            symbol=symbol,
            frequency=frequency,
            decision_time=decision_time,
        )

    def lower_evidence(state, *, frequency, decision_time):
        assert state is lower_state
        assert frequency == "30m"
        boundary = close_time(date(2020, 1, 9))
        return (
            RiskCenterPointEvidence(
                center_id="center:30m:equal-start",
                center_level_rank=1,
                center_completed=True,
                center_expanded=False,
                point_type="1sell",
                point_anchor_at=boundary,
                point_available_at=boundary,
            ),
        )

    monkeypatch.setattr(etf_facts, "_old_pen_structure_state", structure_state)
    monkeypatch.setattr(etf_facts, "_lower_risk_evidence", lower_evidence)
    result = build_benchmark_structure_risk_facts(
        rows,
        trading_sessions=tuple(row.session for row in rows),
        calendar_coverage_end=rows[-1].session,
        decision_time=decision,
        completed_30m_bars=completed_30m,
    )
    daily = result.states[2]
    assert daily.active_top_interval == (
        close_time(date(2020, 1, 9)),
        close_time(date(2020, 1, 11)),
    )
    assert daily.fact.state == "FORMED"
    assert daily.fact.mapping_unique is True
    assert daily.fact.mapped_center_id == "center:30m:equal-start"
    assert result.completed_30m_prefix_count == 3
    assert not {
        "CSI300_COMPLETED_30M_BARS_MISSING",
        "CSI300_30M_COVERAGE_DOES_NOT_SPAN_D_TOP_FRACTAL",
    }.intersection(blocker.code for blocker in result.blockers)

    future = FrozenStructureBar(
        end_at=decision + timedelta(minutes=30),
        open=Decimal("20"),
        high=Decimal("21"),
        low=Decimal("19"),
        close=Decimal("20"),
        volume=Decimal("100"),
    )
    with_future = build_benchmark_structure_risk_facts(
        rows,
        trading_sessions=tuple(row.session for row in rows),
        calendar_coverage_end=rows[-1].session,
        decision_time=decision,
        completed_30m_bars=completed_30m + (future,),
    )
    assert with_future.states[2].fact.source_revision == daily.fact.source_revision
    assert with_future.completed_30m_prefix_count == 3


def test_missing_benchmark_30m_is_explicit_and_cannot_be_green() -> None:
    values = (10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12)
    start = date(2020, 1, 1)
    rows = tuple(
        market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = rows[-1].known_at
    result = build_benchmark_structure_risk_facts(
        rows,
        trading_sessions=tuple(row.session for row in rows),
        calendar_coverage_end=rows[-1].session,
        decision_time=decision,
        completed_30m_bars=(),
    )
    daily = result.states[2]
    assert daily.fact.state == "FORMED_UNRESOLVED"
    assert daily.fact.mapping_unique is False
    assert "CSI300_COMPLETED_30M_BARS_MISSING" in {
        blocker.code for blocker in result.blockers
    }
    snapshot = HigherTimeframeRiskSnapshot(
        snapshot_id="risk:no-30m",
        observed_at=decision,
        monthly=result.states[0].fact.state,
        weekly=result.states[1].fact.state,
        daily=daily.fact.state,
        monthly_ma5=Decimal("10"),
        weekly_ma5=Decimal("10"),
        daily_ma5=Decimal("10"),
        mapping_unique=False,
    )
    assert snapshot.gate == "AMBER"


def test_candidate_interface_uses_exact_pit_session_and_same_decision_core(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pit.sqlite3"
    create_pit_database(database, membership_count=300)
    repository = EtfProxyPitRepository(database)
    session = repository.available_membership_sessions()[0]
    decision = close_time(session)
    mapping = EtfTrackingMapping(
        symbol="SH.510300",
        tracked_index="CSI.000300",
        known_at=close_time(date(2019, 1, 1)),
        effective_from=close_time(date(2019, 1, 1)),
        valid_until=close_time(date(2030, 1, 1)),
        evidence_ids=("RESEARCH_SOURCE:ETF_TRACKING_DECLARATION",),
        authoritative=False,
    )
    benchmark_start = session - timedelta(days=1099)
    benchmark = tuple(
        market_bar(
            benchmark_start + timedelta(days=index),
            100 + (index % 11),
        )
        for index in range(1100)
    )
    calendar = tuple(row.session for row in benchmark)
    tradeability = TradeabilitySnapshot(
        symbol="SH.510300",
        observed_at=decision,
        listed=True,
        st=False,
        suspended=False,
        reliable_continuous_market_data=True,
        continuity_status="ACTIVE",
        structure_history_sufficient=True,
        price_tick=Decimal("0.001"),
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        fee_schedule_id="fees:research",
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
    technical = TechnicalEntrySnapshot(
        structure_snapshot_id="technical:510300:test",
        observed_at=decision,
        price_basis_revision="pit:test",
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=True,
        all_components_completed=True,
        l0_center_id="center:l0:first",
        l0_center_ordinal=1,
        l0_center_completed=True,
        l0_point_type="3buy",
        l0_point_id="point:l0:3buy",
        l0_point_confirmation_time=decision - timedelta(minutes=1),
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=Decimal("4"),
        l0_zg=Decimal("4"),
        l2_locator="L2_FIRST_BUY",
        l2_point_id="point:l2:1buy",
        l2_confirmation_bar_high=Decimal("4.1"),
    )
    account = AccountEntryGate(
        observed_at=decision,
        operations_normal=True,
        reconciliation_passed=True,
        free_strategic_slot=True,
        drawdown=Decimal("0"),
        no_active_symbol_order=True,
    )

    def green_risk(snapshot_id: str) -> HigherTimeframeRiskSnapshot:
        return HigherTimeframeRiskSnapshot(
            snapshot_id=snapshot_id,
            observed_at=decision,
            monthly="NONE",
            weekly="RESOLVED_CONTINUATION",
            daily="INTERMEDIATE",
            monthly_ma5=Decimal("10"),
            weekly_ma5=Decimal("10"),
            daily_ma5=Decimal("10"),
            mapping_unique=True,
        )

    result = build_etf_proxy_candidate_decision(
        repository,
        mapping,
        decision_time=decision,
        benchmark_daily_bars=benchmark,
        benchmark_completed_30m_bars=(),
        trading_sessions=calendar,
        calendar_coverage_end=calendar[-1],
        tradeability=tradeability,
        sector_risk=green_risk("sector"),
        symbol_risk=green_risk("symbol"),
        technical=technical,
        account=account,
        reviewer="research-adapter",
        signature="RESEARCH_ONLY/LIVE_DISABLED",
    )
    assert result.selection.basket is not None
    assert result.selection.basket.candidate_session == session
    assert result.candidate_snapshot is not None
    assert result.decision is not None
    assert result.decision.accepted is False
    assert any(
        code in {"REJECT_MARKET_RISK_AMBER", "REJECT_MARKET_RISK_UNRESOLVED"}
        for code in result.decision.rejected_reason_codes
    )
    assert result.grade == "RESEARCH_ONLY"
    assert "CSI300_COMPLETED_30M_BARS_MISSING" in {
        blocker.code for blocker in result.blockers
    }

    previous = build_etf_proxy_candidate_decision(
        repository,
        mapping,
        decision_time=decision - timedelta(days=1),
        benchmark_daily_bars=benchmark,
        benchmark_completed_30m_bars=(),
        trading_sessions=calendar,
        calendar_coverage_end=calendar[-1],
        tradeability=tradeability,
        sector_risk=green_risk("sector"),
        symbol_risk=green_risk("symbol"),
        technical=technical,
        account=account,
        reviewer="research-adapter",
        signature="RESEARCH_ONLY/LIVE_DISABLED",
    )
    assert previous.selection.snapshot is None
    assert previous.decision is None
    assert "EXACT_DECISION_SESSION_BASKET_MISSING" in {
        blocker.code for blocker in previous.blockers
    }
