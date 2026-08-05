from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from chanlun.decision_support.models import LevelSnapshot, StrategyTrack
from chanlun.decision_support.risk import (
    HoldingSnapshot,
    PendingExitSnapshot,
    QuoteSnapshot,
    RiskContext,
    RiskPolicy,
    evaluate_entry,
    evaluate_exit,
)


def _holding(
    *,
    code: str = "SH.600519",
    shares: int = 400,
    sellable_shares: int | None = None,
    opened_at,
) -> HoldingSnapshot:
    return HoldingSnapshot(
        code=code,
        shares=shares,
        sellable_shares=shares if sellable_shares is None else sellable_shares,
        opened_at=opened_at,
        average_price=Decimal("10"),
    )


def test_risk_sizing_never_exceeds_half_percent(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(price=10.0, stop_below=9.0)
    context = make_risk_context(
        account_equity="100000",
        available_cash="100000",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is True
    assert decision.shares == 400
    assert decision.planned_risk_cash <= Decimal("500")
    assert decision.target_weight == Decimal("0.2")
    assert decision.reasons == ()


def test_risk_weight_uses_native_30m_direction_not_recursive_display_label(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(
        bs_type="2buy",
        big_dir="down",
        mid_dir="up",
    )
    recursive = tuple(
        replace(
            level,
            source_frequency="1m",
            source_bar_closed_at=event.bar_closed_at,
        )
        for level in event.levels
    )
    native_5m = LevelSnapshot(
        "5m",
        0,
        "up",
        True,
        9.0,
        10.0,
        9.2,
        9.8,
        source_frequency="5m",
        source_bar_closed_at=event.bar_closed_at,
    )
    native_30m = replace(
        native_5m,
        frequency="30m",
        source_frequency="30m",
        direction="up",
    )
    multitimeframe = replace(
        event,
        levels=(*recursive, native_5m, native_30m),
    )
    context = make_risk_context(asof=event.observed_at)

    decision = evaluate_entry(
        multitimeframe,
        context,
        RiskPolicy.conservative(),
    )

    assert decision.target_weight == Decimal("0.18")


def test_risk_weight_uses_native_trade_gate_direction(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(
        bs_type="2buy",
        big_dir="up",
        mid_dir="up",
    )
    recursive = tuple(
        replace(
            level,
            source_frequency="1m",
            source_bar_closed_at=event.bar_closed_at,
        )
        for level in event.levels
    )
    native_5m = LevelSnapshot(
        "5m",
        0,
        "up",
        True,
        9.0,
        10.0,
        9.2,
        9.8,
        trade_gate_direction="up",
        source_frequency="5m",
        source_bar_closed_at=event.bar_closed_at,
    )
    native_30m = replace(
        native_5m,
        frequency="30m",
        source_frequency="30m",
        trade_gate_direction="down",
    )
    multitimeframe = replace(
        event,
        levels=(*recursive, native_5m, native_30m),
    )
    context = make_risk_context(asof=event.observed_at)

    decision = evaluate_entry(
        multitimeframe,
        context,
        RiskPolicy.conservative(),
    )

    assert decision.target_weight == Decimal("0.15")


def test_daily_one_percent_loss_locks_new_entries(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        account_equity="90000",
        day_start_equity="100000",
        day_pnl="-1000",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "daily_loss_lock" in decision.reasons
    assert decision.shares == 0


def test_daily_loss_uses_day_start_equity_boundary(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        account_equity="90000",
        day_start_equity="100000",
        day_pnl="-999.99",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert "daily_loss_lock" not in decision.reasons


def test_missing_structural_stop_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(stop_below=None)

    decision = evaluate_entry(
        event,
        make_risk_context(asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "missing_structural_stop" in decision.reasons


def test_invalid_structural_stop_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(price=10.0, stop_below=10.0)

    decision = evaluate_entry(
        event,
        make_risk_context(asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "invalid_structural_stop" in decision.reasons


def test_strategy_drawdown_eight_percent_locks_entries(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        strategy_drawdown="0.08",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "strategy_drawdown_lock" in decision.reasons


def test_five_positions_lock_new_entries(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    holdings = tuple(
        _holding(
            code=f"SH.60050{index}",
            opened_at=event.observed_at - timedelta(days=1),
        )
        for index in range(5)
    )

    decision = evaluate_entry(
        event,
        make_risk_context(holdings=holdings, asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "max_positions" in decision.reasons


def test_existing_position_blocks_duplicate_entry(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    holding = _holding(
        code=event.code,
        opened_at=event.observed_at - timedelta(days=1),
    )

    decision = evaluate_entry(
        event,
        make_risk_context(holdings=(holding,), asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "existing_position" in decision.reasons


def test_a_share_t_plus_one_metadata_is_required(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    event = replace(
        event,
        market_constraints=replace(event.market_constraints, t_plus=0),
    )

    decision = evaluate_entry(
        event,
        make_risk_context(asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "invalid_t_plus_metadata" in decision.reasons


def test_limit_up_lock_blocks_entry(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    decision = evaluate_entry(
        event,
        make_risk_context(
            asof=event.observed_at,
            limit_up_locked=True,
        ),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "limit_up_locked" in decision.reasons


def test_untradable_quote_blocks_entry(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    decision = evaluate_entry(
        event,
        make_risk_context(
            asof=event.observed_at,
            entry_tradable=False,
        ),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "entry_not_tradable" in decision.reasons


def test_zero_share_plan_is_rejected(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(price=10.0, stop_below=9.0)
    context = make_risk_context(
        account_equity="1000",
        day_start_equity="1000",
        available_cash="1000",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "zero_shares" in decision.reasons
    assert decision.shares == 0


def test_cash_cap_rounds_down_to_board_lot(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(price=10.0, stop_below=9.0)
    context = make_risk_context(
        account_equity="100000",
        available_cash="2500",
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is True
    assert decision.shares == 200


def test_source_faithful_target_weight_is_deterministic(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(track=StrategyTrack.CHANLUN_SOURCE_FAITHFUL)
    context = make_risk_context(asof=event.observed_at)

    first = evaluate_entry(event, context, RiskPolicy.conservative())
    second = evaluate_entry(event, context, RiskPolicy.conservative())

    assert first.target_weight == second.target_weight
    assert first.target_weight > Decimal("0")


def test_stale_quote_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        asof=event.observed_at + timedelta(minutes=6),
        quote_time=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "stale_quote" in decision.reasons


def test_risk_context_before_event_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        asof=event.observed_at - timedelta(seconds=1),
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "risk_context_before_event" in decision.reasons


def test_same_day_exit_remains_pending_under_t_plus_one(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    position = _holding(
        opened_at=event.observed_at - timedelta(minutes=30),
        sellable_shares=0,
    )
    context = make_risk_context(
        holdings=(position,),
        asof=event.observed_at,
    )

    decision = evaluate_exit(event, position, context)

    assert decision.allowed is False
    assert decision.blocked_by_t1 is True
    assert decision.requested_shares == position.shares
    assert decision.pending_shares == position.shares
    assert decision.reason == "structural_exit"


def test_limit_down_exit_remains_pending(
    make_decision_event,
    make_risk_context,
) -> None:
    observed_at = make_decision_event().observed_at + timedelta(days=1)
    event = make_decision_event(
        observed_at=observed_at,
        quote_time=observed_at,
    )
    position = _holding(opened_at=observed_at - timedelta(days=1))
    context = make_risk_context(
        holdings=(position,),
        limit_down_locked=True,
        asof=observed_at,
    )

    decision = evaluate_exit(event, position, context)

    assert decision.allowed is False
    assert decision.blocked_by_limit is True
    assert decision.pending_shares == position.shares
    assert "limit_down_locked" in decision.reasons


def test_next_day_structural_exit_is_allowed(
    make_decision_event,
    make_risk_context,
) -> None:
    observed_at = make_decision_event().observed_at + timedelta(days=1)
    event = make_decision_event(
        observed_at=observed_at,
        quote_time=observed_at,
    )
    position = _holding(opened_at=observed_at - timedelta(days=1))
    context = make_risk_context(
        holdings=(position,),
        asof=observed_at,
    )

    decision = evaluate_exit(event, position, context)

    assert decision.allowed is True
    assert decision.executable_shares == position.shares
    assert decision.pending_shares == 0


def test_daily_loss_lock_never_blocks_exit(
    make_decision_event,
    make_risk_context,
) -> None:
    observed_at = make_decision_event().observed_at + timedelta(days=1)
    event = make_decision_event(
        observed_at=observed_at,
        quote_time=observed_at,
    )
    position = _holding(opened_at=observed_at - timedelta(days=1))
    context = make_risk_context(
        holdings=(position,),
        day_pnl="-1000",
        asof=observed_at,
    )

    decision = evaluate_exit(event, position, context)

    assert decision.allowed is True
    assert "daily_loss_lock" not in decision.reasons


def test_latest_quote_recalculates_risk_and_cash_caps(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event(price=10.0, stop_below=9.0)
    context = make_risk_context(
        entry_reference="20",
        asof=event.observed_at + timedelta(minutes=1),
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.entry_reference == Decimal("20")
    assert decision.allowed is False
    assert decision.shares == 0
    assert "zero_shares" in decision.reasons


def test_pending_t_plus_one_exit_locks_new_entries(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    holding = _holding(
        code="SZ.000001",
        shares=100,
        sellable_shares=0,
        opened_at=event.observed_at,
    )
    pending = PendingExitSnapshot(
        code=holding.code,
        shares=holding.shares,
        reason="structural_exit",
        blocked_by_t1=True,
        blocked_by_limit=False,
    )
    context = make_risk_context(
        holdings=(holding,),
        pending_exits=(pending,),
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "pending_exit_lock" in decision.reasons


def test_daily_loss_lock_remains_latched_after_value_recovers(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        day_pnl="-999",
        daily_loss_locked=True,
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "daily_loss_lock" in decision.reasons


def test_drawdown_lock_remains_latched_until_manual_reset(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    context = make_risk_context(
        strategy_drawdown="0.079",
        drawdown_locked=True,
        asof=event.observed_at,
    )

    decision = evaluate_entry(event, context, RiskPolicy.conservative())

    assert decision.allowed is False
    assert "strategy_drawdown_lock" in decision.reasons


def test_exit_rejects_position_not_equal_to_authoritative_holding(
    make_decision_event,
    make_risk_context,
) -> None:
    observed_at = make_decision_event().observed_at + timedelta(days=1)
    event = make_decision_event(
        observed_at=observed_at,
        quote_time=observed_at,
    )
    authoritative = _holding(
        shares=100,
        opened_at=observed_at - timedelta(days=1),
    )
    forged = _holding(
        shares=10_000,
        opened_at=authoritative.opened_at,
    )
    context = make_risk_context(
        holdings=(authoritative,),
        asof=observed_at,
    )

    decision = evaluate_exit(event, forged, context)

    assert decision.allowed is False
    assert decision.requested_shares == authoritative.shares
    assert decision.executable_shares == 0
    assert "position_snapshot_mismatch" in decision.reasons


def test_missing_day_start_equity_is_rejected(make_decision_event) -> None:
    event = make_decision_event()
    quote = QuoteSnapshot(
        code=event.code,
        price=Decimal("10"),
        quote_time=event.observed_at,
        entry_tradable=True,
        exit_tradable=True,
        limit_up_locked=False,
        limit_down_locked=False,
    )

    with pytest.raises(TypeError, match="day_start_equity"):
        RiskContext(
            account_equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            holdings=(),
            pending_exits=(),
            day_pnl=Decimal("0"),
            strategy_drawdown=Decimal("0"),
            daily_loss_locked=False,
            drawdown_locked=False,
            quote=quote,
            asof=event.observed_at,
        )


def test_inconsistent_board_limit_metadata_fails_closed(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    event = replace(
        event,
        market_constraints=replace(
            event.market_constraints,
            limit_pct=None,
        ),
    )

    decision = evaluate_entry(
        event,
        make_risk_context(asof=event.observed_at),
        RiskPolicy.conservative(),
    )

    assert decision.allowed is False
    assert "invalid_limit_metadata" in decision.reasons


@pytest.mark.parametrize(
    ("code", "name", "board"),
    [
        ("SZ.300001", "*ST测试", "gem"),
        ("SH.688001", "ST测试", "star"),
    ],
)
def test_growth_board_st_metadata_does_not_block_exit(
    make_decision_event,
    make_risk_context,
    code: str,
    name: str,
    board: str,
) -> None:
    observed_at = make_decision_event().observed_at + timedelta(days=1)
    event = make_decision_event(
        code=code,
        name=name,
        board=board,
        limit_pct=0.20,
        observed_at=observed_at,
        quote_time=observed_at,
    )
    position = _holding(
        code=code,
        shares=100,
        opened_at=observed_at - timedelta(days=1),
    )
    context = make_risk_context(
        holdings=(position,),
        quote_code=code,
        asof=observed_at,
    )

    decision = evaluate_exit(event, position, context)

    assert decision.allowed is True
    assert decision.executable_shares == position.shares
    assert "invalid_limit_metadata" not in decision.reasons


def test_risk_types_reject_naive_time_and_float_money(
    make_decision_event,
) -> None:
    from datetime import datetime

    from chanlun.decision_support.risk import RiskContext

    event = make_decision_event()
    quote = QuoteSnapshot(
        code=event.code,
        price=Decimal("10"),
        quote_time=event.observed_at,
        entry_tradable=True,
        exit_tradable=True,
        limit_up_locked=False,
        limit_down_locked=False,
    )
    with pytest.raises(ValueError, match="asof must be timezone-aware"):
        RiskContext(
            account_equity=Decimal("100000"),
            day_start_equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            holdings=(),
            pending_exits=(),
            day_pnl=Decimal("0"),
            strategy_drawdown=Decimal("0"),
            daily_loss_locked=False,
            drawdown_locked=False,
            quote=quote,
            asof=datetime(2026, 7, 13, 10, 35),
        )
    with pytest.raises(ValueError, match="account_equity must be Decimal"):
        RiskContext(
            account_equity=100000.0,
            day_start_equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            holdings=(),
            pending_exits=(),
            day_pnl=Decimal("0"),
            strategy_drawdown=Decimal("0"),
            daily_loss_locked=False,
            drawdown_locked=False,
            quote=quote,
            asof=event.observed_at,
        )
