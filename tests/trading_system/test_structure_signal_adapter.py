from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.decision import SystemHealthFacts
from chanlun.decision_support.trading_system.structure_signal_adapter import (
    FrozenCenterPhaseFact,
    FrozenCompletedTrendFact,
    FrozenSignalExecutionFact,
    build_structure_signal_ledger,
)


CN = ZoneInfo("Asia/Shanghai")
SYMBOL = "SH.510300"
START = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
END = datetime(2026, 7, 20, 15, 0, tzinfo=CN)
SOURCE_HASH = "sha256:" + "a" * 64


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 20, hour, minute, tzinfo=CN)


def point(
    point_id: str,
    *,
    frequency: str,
    point_type: str,
    anchor_at: datetime,
    available_at: datetime,
    parent_point_id: str | None = None,
    evidence_codes: tuple[str, ...] = (),
    recursive_level: int = 0,
) -> StructuralPoint:
    buy = point_type.endswith("buy")
    return StructuralPoint(
        point_id=point_id,
        code=SYMBOL,
        point_type=point_type,  # type: ignore[arg-type]
        side="buy" if buy else "sell",
        status="confirmed",
        variant="standard",
        source_frequency=frequency,
        price_basis_revision="pit-adjusted:test",
        tower="formal",
        recursive_level=recursive_level,
        anchor_at=anchor_at,
        confirmed_at=available_at,
        available_at=available_at,
        structure_anchor_price=10.0,
        structure_invalidation_price=9.0 if buy else 11.0,
        center_id=f"center:{frequency}:{point_id}",
        center_zd=Decimal("9"),  # type: ignore[arg-type]
        center_zg=Decimal("10"),  # type: ignore[arg-type]
        center_ordinal=1,
        divergence_kind=None,
        parent_point_id=parent_point_id,
        evidence_codes=evidence_codes,
    )


def trend(
    trend_id: str,
    *,
    frequency: str,
    direction: str,
    market_end: datetime,
    available_at: datetime,
    recursive_level: int = 0,
) -> FrozenCompletedTrendFact:
    return FrozenCompletedTrendFact(
        trend_id=trend_id,
        source_frequency=frequency,  # type: ignore[arg-type]
        recursive_level=recursive_level,
        price_basis_revision="pit-adjusted:test",
        direction=direction,  # type: ignore[arg-type]
        market_start=market_end - timedelta(minutes=30),
        market_end=market_end,
        confirmed_at=available_at,
        available_at=available_at,
        source_fact_ids=(trend_id, f"{trend_id}:terminal"),
    )


def phase(
    observed_at: datetime,
    *,
    source_frequency: str = "5m",
    recursive_level: int = 0,
) -> FrozenCenterPhaseFact:
    return FrozenCenterPhaseFact(
        center_id="l1:center:active",
        source_frequency=source_frequency,
        recursive_level=recursive_level,
        phase="OSCILLATION",
        available_at=observed_at,
        structure_snapshot_id=f"phase:{observed_at.isoformat()}",
        source_fact_ids=("l1:center:active",),
    )


def health() -> SystemHealthFacts:
    return SystemHealthFacts(
        data_complete=True,
        broker_healthy=True,
        reconciliation_passed=True,
        timestamps_monotonic=True,
        account_transfer_registered=True,
    )


def execution(
    signal: StructuralPoint,
    *,
    locator: StructuralPoint | None = None,
    side: str,
) -> FrozenSignalExecutionFact:
    boundary = signal if locator is None else locator
    return FrozenSignalExecutionFact(
        signal_point_id=signal.point_id,
        known_at=signal.available_at - timedelta(seconds=1),
        account_snapshot_id=f"account:{signal.point_id}",
        health=health(),
        price_cap_or_floor=Decimal("9.90") if side == "sell" else Decimal("10.10"),
        boundary_fact_id=f"raw-bar-boundary:{boundary.point_id}",
        boundary_point_id=boundary.point_id,
        locator_point_id=None if locator is None else locator.point_id,
        risk_fact_ids=(f"risk:{signal.point_id}",),
        source_fact_ids=(f"execution:{signal.point_id}",),
        q_liquidity_cap=500,
        broker_sellable_tactical_qty=500,
        cash_affordable_buyback_qty=500,
        l2_reached_required_half=True,
        zn_at_or_above_a=True,
        higher_timeframe_allows_ordinary_buyback=True,
        higher_timeframe_allows_third_sell_recovery=True,
        tactical_adaptation="PASS",
        every_partial_prefix_edge="PASS",
    )


def test_adapter_maps_only_explicit_completed_structure_signals() -> None:
    l0_third_sell = point(
        "l0:3sell",
        frequency="30m",
        point_type="3sell",
        anchor_at=at(9, 59),
        available_at=at(10, 0),
    )
    l2_sell = point(
        "l2:1sell",
        frequency="1m",
        point_type="1sell",
        anchor_at=at(10, 8),
        available_at=at(10, 9),
    )
    l1_third_sell = point(
        "l1:3sell",
        frequency="5m",
        point_type="3sell",
        anchor_at=at(10, 9),
        available_at=at(10, 10),
    )
    l2_buy = point(
        "l2:1buy",
        frequency="1m",
        point_type="1buy",
        anchor_at=at(10, 28),
        available_at=at(10, 29),
    )
    l1_first_buy = point(
        "l1:1buy",
        frequency="5m",
        point_type="1buy",
        anchor_at=at(10, 28),
        available_at=at(10, 30),
    )
    l2_protective = point(
        "l2:2buy",
        frequency="1m",
        point_type="2buy",
        anchor_at=at(10, 58),
        available_at=at(10, 59),
        parent_point_id=l2_buy.point_id,
        evidence_codes=(
            "complete_adjacent_rebound",
            "complete_first_pullback",
        ),
    )
    l1_third_buy = point(
        "l1:3buy",
        frequency="5m",
        point_type="3buy",
        anchor_at=at(10, 59),
        available_at=at(11, 0),
    )
    executions = (
        execution(l0_third_sell, side="sell"),
        execution(l2_sell, side="sell"),
        execution(l1_third_sell, locator=l2_sell, side="sell"),
        execution(l2_buy, side="buy"),
        execution(l1_first_buy, locator=l2_buy, side="buy"),
        execution(l2_protective, side="buy"),
        execution(l1_third_buy, locator=l2_protective, side="buy"),
    )
    ledger = build_structure_signal_ledger(
        symbol=SYMBOL,
        l0_points=(l0_third_sell,),
        l1_points=(l1_third_sell, l1_first_buy, l1_third_buy),
        l2_points=(l2_sell, l2_buy, l2_protective),
        completed_trends=(
            trend(
                "l2:up-trend",
                frequency="1m",
                direction="up",
                market_end=l2_sell.anchor_at,
                available_at=l2_sell.available_at,
            ),
            trend(
                "l2:down-trend",
                frequency="1m",
                direction="down",
                market_end=l2_buy.anchor_at,
                available_at=l2_buy.available_at,
            ),
            trend(
                "l1:down-trend",
                frequency="5m",
                direction="down",
                market_end=l1_first_buy.anchor_at,
                available_at=l1_first_buy.available_at,
            ),
        ),
        l1_center_phases=(phase(at(9, 50)),),
        execution_facts=executions,
        coverage_start=START,
        coverage_end=END,
        source_ledger_sha256=SOURCE_HASH,
    )
    rows = {
        str(value["signal_kind"]): value
        for value in ledger.structure_signal_facts
    }
    assert {
        "L0_THIRD_SELL",
        "L1_THIRD_SELL",
        "THIRD_SELL_RECOVERY",
        "L1_THIRD_BUY_PROTECTION",
        "ORDINARY_TACTICAL_SELL",
        "ORDINARY_TACTICAL_BUYBACK",
    }.issubset(rows)
    assert rows["L0_THIRD_SELL"]["strategic"] == {"l0_third_sell": True}
    assert rows["L1_THIRD_SELL"]["tactical"]["l1_third_sell"] is True
    assert (
        rows["THIRD_SELL_RECOVERY"]["tactical"][
            "third_sell_recovery_first_or_second_buy"
        ]
        is True
    )
    assert rows["ORDINARY_TACTICAL_SELL"]["emit_to_replay"] is True
    assert rows["ORDINARY_TACTICAL_BUYBACK"]["emit_to_replay"] is True
    assert ledger.coverage["complete"] is False
    assert ledger.rule_coverage["FIRST_UP_LEG_FAILED"] == "UNRESOLVED"
    assert ledger.rule_coverage["SECOND_SELL_CONFIRM"] == "UNRESOLVED"
    assert ledger.rule_coverage["L0_UPMOVE_DIVERGENCE"] == "UNRESOLVED"


def test_missing_links_and_tactical_adaptation_are_explicitly_unresolved() -> None:
    l0_sell = point(
        "l0:1sell",
        frequency="30m",
        point_type="1sell",
        anchor_at=at(9, 59),
        available_at=at(10, 0),
    )
    l2_sell = point(
        "l2:1sell:missing",
        frequency="1m",
        point_type="1sell",
        anchor_at=at(10, 8),
        available_at=at(10, 9),
    )
    ledger = build_structure_signal_ledger(
        symbol=SYMBOL,
        l0_points=(l0_sell,),
        l1_points=(),
        l2_points=(l2_sell,),
        completed_trends=(),
        l1_center_phases=(),
        execution_facts=(),
        coverage_start=START,
        coverage_end=END,
        source_ledger_sha256=SOURCE_HASH,
    )
    codes = {value.code for value in ledger.diagnostics}
    assert "UNRESOLVED_L0_SELL_POINT_REQUIRES_STRATEGIC_CYCLE_CONTEXT" in codes
    assert "UNRESOLVED_COMPLETED_TREND_LINK_MISSING" in codes
    assert "UNRESOLVED_L1_CENTER_PHASE_AT_SIGNAL" in codes
    assert "UNRESOLVED_TACTICAL_ADAPTATION_20_PAIR_FACTS" in codes
    assert not any(
        value["emit_to_replay"] for value in ledger.structure_signal_facts
    )


def test_appending_future_frozen_facts_cannot_change_prior_signal_rows() -> None:
    first = point(
        "l0:3sell:prefix",
        frequency="30m",
        point_type="3sell",
        anchor_at=at(9, 59),
        available_at=at(10, 0),
    )
    future = point(
        "l0:3sell:future",
        frequency="30m",
        point_type="3sell",
        anchor_at=at(13, 29),
        available_at=at(13, 30),
    )
    prefix = build_structure_signal_ledger(
        symbol=SYMBOL,
        l0_points=(first,),
        l1_points=(),
        l2_points=(),
        completed_trends=(),
        l1_center_phases=(),
        execution_facts=(execution(first, side="sell"),),
        coverage_start=START,
        coverage_end=END,
        source_ledger_sha256=SOURCE_HASH,
    )
    full = build_structure_signal_ledger(
        symbol=SYMBOL,
        l0_points=(first, future),
        l1_points=(),
        l2_points=(),
        completed_trends=(),
        l1_center_phases=(),
        execution_facts=(
            execution(first, side="sell"),
            execution(future, side="sell"),
        ),
        coverage_start=START,
        coverage_end=END,
        source_ledger_sha256=SOURCE_HASH,
    )
    cutoff = first.available_at.isoformat()
    assert prefix.structure_signal_facts == tuple(
        value
        for value in full.structure_signal_facts
        if str(value["decision_time"]) <= cutoff
    )


def test_direct_recursive_mode_preserves_raw_1m_levels_and_logical_roles() -> None:
    l0_sell = point(
        "raw-level-2:3sell",
        frequency="1m",
        recursive_level=2,
        point_type="3sell",
        anchor_at=at(9, 59),
        available_at=at(10, 0),
    )
    locator = point(
        "raw-level-0:1sell",
        frequency="1m",
        recursive_level=0,
        point_type="1sell",
        anchor_at=at(10, 8),
        available_at=at(10, 9),
    )
    l1_sell = point(
        "raw-level-1:3sell",
        frequency="1m",
        recursive_level=1,
        point_type="3sell",
        anchor_at=at(10, 9),
        available_at=at(10, 10),
    )

    ledger = build_structure_signal_ledger(
        symbol=SYMBOL,
        l0_points=(l0_sell,),
        l1_points=(l1_sell,),
        l2_points=(locator,),
        completed_trends=(
            trend(
                "raw-level-0:up-trend",
                frequency="1m",
                recursive_level=0,
                direction="up",
                market_end=locator.anchor_at,
                available_at=locator.available_at,
            ),
        ),
        l1_center_phases=(
            phase(
                at(9, 50),
                source_frequency="1m",
                recursive_level=1,
            ),
        ),
        execution_facts=(
            execution(l0_sell, side="sell"),
            execution(locator, side="sell"),
            execution(l1_sell, locator=locator, side="sell"),
        ),
        coverage_start=START,
        coverage_end=END,
        source_ledger_sha256=SOURCE_HASH,
        level_relation_mode="DIRECT_RECURSIVE",
    )

    rows = {
        value["signal_kind"]: value for value in ledger.structure_signal_facts
    }
    assert rows["L0_THIRD_SELL"]["source_frequencies"] == ("30m",)
    assert rows["L1_THIRD_SELL"]["source_frequencies"] == ("5m", "1m")
    assert rows["ORDINARY_TACTICAL_SELL"]["source_frequencies"] == ("1m",)
    assert ledger.coverage["level_relation_mode"] == "DIRECT_RECURSIVE"
    assert ledger.coverage["raw_recursive_levels"] == {
        "L0": 2,
        "L1": 1,
        "L2": 0,
    }
    assert l0_sell.point_id in rows["L0_THIRD_SELL"][
        "frozen_structure_fact_ids"
    ]
