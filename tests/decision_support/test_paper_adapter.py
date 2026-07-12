from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from chanlun.decision_support import paper_adapter as paper_adapter_module
from chanlun.decision_support.models import EventState
from chanlun.decision_support.paper_adapter import (
    ConfirmedEventPaperAdapter,
    InMemoryPaperLedger,
    PaperAdapterConflictError,
    PaperAdapterEligibilityError,
    PaperBar,
    PaperLedgerIntegrityError,
    reconcile_paper_ledger,
)
from chanlun.decision_support.risk import RiskDecision
from chanlun.decision_support.risk_snapshot import RiskSnapshot
from tests.decision_support.conftest import ts


def _bar(
    opened_at: str,
    *,
    closed_at: str | None = None,
    code: str = "SH.600519",
    open_price: str = "10",
    close_price: str | None = None,
    previous_close: str = "10",
    suspended: bool = False,
    limit_up_locked: bool = False,
    limit_down_locked: bool = False,
    max_fill_shares: int | None = None,
) -> PaperBar:
    opened = ts(opened_at)
    return PaperBar(
        code=code,
        opened_at=opened,
        closed_at=(ts(closed_at) if closed_at is not None else opened + timedelta(minutes=5)),
        open_price=Decimal(open_price),
        close_price=Decimal(close_price or open_price),
        previous_close=Decimal(previous_close),
        suspended=suspended,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        max_fill_shares=max_fill_shares,
    )


def _risk_snapshot(
    event,
    *,
    shares: int = 500,
    allowed: bool = True,
    evaluated_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> RiskSnapshot:
    evaluated_at = evaluated_at or event.observed_at
    effective_shares = shares if allowed else 0
    decision = RiskDecision(
        allowed=allowed,
        shares=effective_shares,
        planned_risk_cash=(
            Decimal("0.20") * effective_shares
            if effective_shares
            else Decimal("0")
        ),
        target_weight=Decimal("0.20") if allowed else Decimal("0"),
        entry_reference=Decimal("10"),
        reasons=() if allowed else ("daily_loss_lock",),
        daily_loss_locked=not allowed,
        drawdown_locked=False,
        evaluated_at=evaluated_at,
    )
    return RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=decision,
        observed_at=evaluated_at,
        expires_at=expires_at or evaluated_at + timedelta(hours=2),
    )


@pytest.mark.parametrize(
    "changed",
    (
        {"open_price": "10.01"},
        {"close_price": "10.01"},
        {"closed_at": "2026-07-13T10:45:00+08:00"},
        {"previous_close": "9.99"},
        {"suspended": True},
        {"limit_up_locked": True},
        {"limit_down_locked": True},
        {"max_fill_shares": 100},
    ),
)
def test_bar_identity_binds_complete_execution_payload(changed) -> None:
    baseline = _bar("2026-07-13T10:35:00+08:00")

    assert _bar("2026-07-13T10:35:00+08:00", **changed).bar_id != baseline.bar_id


def test_default_fee_schedule_conservatively_combines_handling_and_regulatory_fees(
) -> None:
    schedule = paper_adapter_module.PaperFeeSchedule()

    assert schedule.regulatory_fee_rate == Decimal("0.0000541")


def test_small_fill_applies_minimum_commission_and_cent_rounding(
    make_bound_decision_event,
) -> None:
    schedule = paper_adapter_module.PaperFeeSchedule(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        sell_stamp_duty_rate=Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00001"),
        regulatory_fee_rate=Decimal("0.0000541"),
        slippage_rate=Decimal("0.001"),
    )
    event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(
        InMemoryPaperLedger(),
        fee_schedule=schedule,
    )
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-cent-fee",
        risk_snapshot=_risk_snapshot(event, shares=100),
        received_at=event.observed_at,
    )

    fill = adapter.on_bar(
        _bar("2026-07-13T10:35:00+08:00", open_price="10.005")
    )[0]

    assert fill.reference_price == Decimal("10.01")
    assert fill.price == Decimal("10.02")
    assert fill.gross_value == Decimal("1002.00")
    assert fill.commission == Decimal("5.00")
    assert fill.stamp_duty == Decimal("0.00")
    assert fill.transfer_fee == Decimal("0.01")
    assert fill.regulatory_fee == Decimal("0.05")
    assert fill.slippage_cost == Decimal("1.00")
    assert fill.trade_cost == Decimal("6.06")
    assert all(
        value.as_tuple().exponent == -2
        for value in (
            fill.reference_price,
            fill.price,
            fill.gross_value,
            fill.commission,
            fill.stamp_duty,
            fill.transfer_fee,
            fill.regulatory_fee,
            fill.slippage_cost,
            fill.trade_cost,
        )
    )


def test_same_code_bar_cursor_rejects_out_of_order_and_payload_conflict(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-bar-cursor",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )
    latest = _bar("2026-07-13T10:40:00+08:00", open_price="10.10")
    adapter.on_bar(latest)

    with pytest.raises(PaperAdapterConflictError, match="paper_bar_out_of_order"):
        adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    with pytest.raises(PaperAdapterConflictError, match="paper_bar_payload_conflict"):
        adapter.on_bar(
            _bar("2026-07-13T10:40:00+08:00", open_price="10.11")
        )

    assert adapter.on_bar(latest) == ()


def test_fill_never_uses_bar_not_wholly_after_admission(
    make_bound_decision_event,
) -> None:
    signal_at = ts("2026-07-13T10:05:00+08:00")
    admitted_at = ts("2026-07-13T10:07:00+08:00")
    event = make_bound_decision_event(
        observed_at=signal_at,
        signal_at=signal_at,
        quote_time=signal_at,
        bar_closed_at=signal_at,
    )
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:00:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-no-retroactive-fill",
        risk_snapshot=_risk_snapshot(event),
        received_at=admitted_at,
    )

    assert adapter.on_bar(_bar("2026-07-13T10:05:00+08:00")) == ()
    assert adapter.intents()[0].status == "pending_admission_time"
    fills = adapter.on_bar(_bar("2026-07-13T10:10:00+08:00"))

    assert len(fills) == 1
    assert fills[0].filled_at == ts("2026-07-13T10:15:00+08:00")


def test_confirmed_event_fills_only_on_next_completed_tradable_bar_close(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    ledger = InMemoryPaperLedger()
    adapter = ConfirmedEventPaperAdapter(ledger)

    intent = adapter.apply_confirmed_event(
        event,
        signal_bar,
        event_state=EventState.CONFIRMED,
        review_id="review-1",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )

    assert intent.status == "pending_next_bar"
    assert adapter.on_bar(signal_bar) == ()
    fills = adapter.on_bar(
        _bar("2026-07-13T10:35:00+08:00", open_price="10.20")
    )
    assert len(fills) == 1
    assert fills[0].price == Decimal("10.20")
    assert fills[0].shares == 500
    assert adapter.positions()[0].shares == 500


def test_volume_bound_fill_uses_bar_close_price_time_and_risk_boundary(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    schedule = paper_adapter_module.PaperFeeSchedule(
        minimum_commission=Decimal("0"),
        transfer_fee_rate=Decimal("0"),
        regulatory_fee_rate=Decimal("0"),
        slippage_rate=Decimal("0.001"),
    )
    ledger = InMemoryPaperLedger()
    adapter = ConfirmedEventPaperAdapter(
        ledger,
        fee_schedule=schedule,
    )
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-close-time-fill",
        risk_snapshot=_risk_snapshot(
            event,
            shares=500,
            expires_at=ts("2026-07-13T10:41:00+08:00"),
        ),
        received_at=event.observed_at,
    )

    fill = adapter.on_bar(
        _bar(
            "2026-07-13T10:35:00+08:00",
            open_price="10.00",
            close_price="10.50",
            max_fill_shares=200,
        )
    )[0]

    assert fill.shares == 200
    assert fill.reference_price == Decimal("10.50")
    assert fill.price == Decimal("10.51")
    assert fill.filled_at == ts("2026-07-13T10:40:00+08:00")
    assert ledger.load().lots[0].opened_at == fill.filled_at
    assert adapter.intents()[0].reason == "partially_filled_at_tradable_bar_close"

    expired_event = make_bound_decision_event(
        observed_at=ts("2026-07-13T11:05:00+08:00"),
    )
    expired_adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    expired_adapter.apply_confirmed_event(
        expired_event,
        _bar("2026-07-13T11:00:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-close-time-risk",
        risk_snapshot=_risk_snapshot(
            expired_event,
            expires_at=ts("2026-07-13T11:09:00+08:00"),
        ),
        received_at=expired_event.observed_at,
    )

    assert expired_adapter.on_bar(
        _bar(
            "2026-07-13T11:05:00+08:00",
            close_price="10.20",
            max_fill_shares=500,
        )
    ) == ()
    assert expired_adapter.intents()[0].status == "expired_risk_snapshot"


@pytest.mark.parametrize(
    ("bs_type", "open_price", "close_price", "expected_status"),
    (
        ("1buy", "10.00", "11.00", "pending_limit_up"),
        ("1sell", "10.00", "9.00", "pending_limit_down"),
    ),
)
def test_price_limit_gate_uses_close_price(
    make_bound_decision_event,
    bs_type,
    open_price,
    close_price,
    expected_status,
) -> None:
    entry_event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-close-limit-entry",
        risk_snapshot=_risk_snapshot(entry_event),
        received_at=entry_event.observed_at,
    )
    adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    event = entry_event
    if bs_type == "1sell":
        observed_at = ts("2026-07-14T10:35:00+08:00")
        event = make_bound_decision_event(
            bs_type=bs_type,
            observed_at=observed_at,
            signal_at=observed_at,
            quote_time=observed_at,
            bar_closed_at=observed_at,
        )
        adapter.apply_confirmed_event(
            event,
            _bar("2026-07-14T10:30:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id="review-close-limit-exit",
            risk_snapshot=_risk_snapshot(event),
            received_at=event.observed_at,
        )
    else:
        adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
        adapter.apply_confirmed_event(
            event,
            _bar("2026-07-13T10:30:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id="review-close-limit-buy",
            risk_snapshot=_risk_snapshot(event),
            received_at=event.observed_at,
        )

    assert adapter.on_bar(
        _bar(
            "2026-07-14T10:35:00+08:00"
            if bs_type == "1sell"
            else "2026-07-13T10:35:00+08:00",
            open_price=open_price,
            close_price=close_price,
        )
    ) == ()
    assert adapter.intents()[-1].status == expected_status


def test_slippage_price_is_clamped_to_a_share_daily_limits(
    make_bound_decision_event,
) -> None:
    entry_event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(
        InMemoryPaperLedger(),
        fee_schedule=paper_adapter_module.PaperFeeSchedule(
            slippage_rate=Decimal("0.001"),
        ),
    )
    adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-limit-clamp-entry",
        risk_snapshot=_risk_snapshot(entry_event),
        received_at=entry_event.observed_at,
    )

    buy_fill = adapter.on_bar(
        _bar(
            "2026-07-13T10:35:00+08:00",
            close_price="10.999",
            previous_close="10",
        )
    )[0]

    assert buy_fill.reference_price == Decimal("11.00")
    assert buy_fill.price == Decimal("11.00")

    exit_at = ts("2026-07-14T10:35:00+08:00")
    exit_event = make_bound_decision_event(
        bs_type="1sell",
        observed_at=exit_at,
        signal_at=exit_at,
        quote_time=exit_at,
        bar_closed_at=exit_at,
    )
    adapter.apply_confirmed_event(
        exit_event,
        _bar("2026-07-14T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-limit-clamp-exit",
        risk_snapshot=_risk_snapshot(exit_event),
        received_at=exit_event.observed_at,
    )

    sell_fill = adapter.on_bar(
        _bar(
            "2026-07-14T10:35:00+08:00",
            close_price="9.001",
            previous_close="10",
        )
    )[0]

    assert sell_fill.reference_price == Decimal("9.00")
    assert sell_fill.price == Decimal("9.00")


def test_event_id_retry_is_idempotent_and_changed_binding_conflicts(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    snapshot = _risk_snapshot(event)
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    first = adapter.apply_confirmed_event(
        event,
        signal_bar,
        event_state=EventState.CONFIRMED,
        review_id="review-1",
        risk_snapshot=snapshot,
        received_at=event.observed_at,
    )

    retried = adapter.apply_confirmed_event(
        event,
        signal_bar,
        event_state=EventState.CONFIRMED,
        review_id="review-1",
        risk_snapshot=snapshot,
        received_at=snapshot.expires_at + timedelta(minutes=1),
    )

    assert retried == first
    assert adapter.intents() == (first,)
    with pytest.raises(PaperAdapterConflictError, match="event_id_conflict"):
        adapter.apply_confirmed_event(
            event,
            signal_bar,
            event_state=EventState.CONFIRMED,
            review_id="review-changed",
            risk_snapshot=snapshot,
            received_at=event.observed_at,
        )
    changed_limit = replace(
        event,
        market_constraints=replace(event.market_constraints, limit_pct=None),
    )
    with pytest.raises(PaperAdapterConflictError, match="event_id_conflict"):
        adapter.apply_confirmed_event(
            changed_limit,
            signal_bar,
            event_state=EventState.CONFIRMED,
            review_id="review-1",
            risk_snapshot=snapshot,
            received_at=event.observed_at,
        )


def test_zero_or_sub_lot_fill_never_creates_position(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-zero",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )

    fills = adapter.on_bar(
        _bar(
            "2026-07-13T10:35:00+08:00",
            open_price="10.10",
            max_fill_shares=99,
        )
    )

    assert fills == ()
    assert adapter.fills() == ()
    assert adapter.positions() == ()
    assert adapter.intents()[0].status == "pending_zero_fill"
    assert adapter.intents()[0].remaining_shares == 500


def test_partial_entry_uses_actual_quantity_weighted_price_and_bar_idempotency(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-partial",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )
    first_bar = _bar(
        "2026-07-13T10:35:00+08:00",
        open_price="10",
        max_fill_shares=200,
    )

    first_fill = adapter.on_bar(first_bar)

    assert first_fill[0].shares == 200
    assert adapter.on_bar(first_bar) == ()
    assert adapter.positions()[0].shares == 200
    assert adapter.intents()[0].status == "partially_filled"
    assert adapter.intents()[0].remaining_shares == 300

    second_fill = adapter.on_bar(
        _bar(
            "2026-07-13T10:40:00+08:00",
            open_price="12",
            previous_close="11",
            max_fill_shares=300,
        )
    )

    assert second_fill[0].shares == 300
    assert adapter.positions()[0].shares == 500
    assert adapter.positions()[0].weighted_average_price == Decimal("11.2")
    assert adapter.intents()[0].status == "filled"
    assert adapter.intents()[0].remaining_shares == 0
    assert len(adapter.fills()) == 2


def test_suspension_and_limit_up_keep_entry_pending(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-blocked-entry",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )

    assert adapter.on_bar(
        _bar("2026-07-13T10:35:00+08:00", suspended=True)
    ) == ()
    assert adapter.intents()[0].status == "pending_suspended"
    assert adapter.on_bar(
        _bar("2026-07-13T10:40:00+08:00", limit_up_locked=True)
    ) == ()
    assert adapter.intents()[0].status == "pending_limit_up"
    assert adapter.positions() == ()

    fills = adapter.on_bar(_bar("2026-07-13T10:45:00+08:00"))

    assert fills[0].shares == 500
    assert adapter.intents()[0].status == "filled"


def test_adapter_fails_closed_for_ineligible_events(
    make_bound_decision_event,
    make_decision_event,
) -> None:
    event = make_bound_decision_event()
    signal_bar = _bar("2026-07-13T10:30:00+08:00")

    def apply(candidate, snapshot, state) -> None:
        ConfirmedEventPaperAdapter(InMemoryPaperLedger()).apply_confirmed_event(
            candidate,
            signal_bar,
            event_state=state,
            review_id="review-gate",
            risk_snapshot=snapshot,
            received_at=event.observed_at,
        )

    with pytest.raises(PaperAdapterEligibilityError, match="event_state_not_confirmed"):
        apply(event, _risk_snapshot(event), EventState.RISK_CHECKED)
    with pytest.raises(PaperAdapterEligibilityError, match="event_rule_binding_missing"):
        apply(make_decision_event(), _risk_snapshot(event), EventState.CONFIRMED)
    stale = _risk_snapshot(
        event,
        expires_at=event.observed_at + timedelta(minutes=1),
    )
    with pytest.raises(PaperAdapterEligibilityError, match="risk_snapshot_expired"):
        ConfirmedEventPaperAdapter(InMemoryPaperLedger()).apply_confirmed_event(
            event,
            signal_bar,
            event_state=EventState.CONFIRMED,
            review_id="review-stale",
            risk_snapshot=stale,
            received_at=stale.expires_at,
        )
    with pytest.raises(PaperAdapterEligibilityError, match="risk_decision_not_allowed"):
        apply(event, _risk_snapshot(event, allowed=False), EventState.CONFIRMED)
    with pytest.raises(PaperAdapterEligibilityError, match="risk_shares_below_one_lot"):
        apply(event, _risk_snapshot(event, shares=99), EventState.CONFIRMED)
    with pytest.raises(PaperAdapterEligibilityError, match="event_state_not_confirmed"):
        apply(event, _risk_snapshot(event), "not-a-real-state")


def test_t_plus_one_and_limit_down_keep_exit_pending_after_risk_expiry(
    make_bound_decision_event,
) -> None:
    entry_event = make_bound_decision_event()
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-entry",
        risk_snapshot=_risk_snapshot(entry_event),
        received_at=entry_event.observed_at,
    )
    adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    exit_at = ts("2026-07-13T11:05:00+08:00")
    exit_event = make_bound_decision_event(
        bs_type="1sell",
        observed_at=exit_at,
        signal_at=exit_at,
        quote_time=exit_at,
        bar_closed_at=exit_at,
    )
    exit_snapshot = _risk_snapshot(
        exit_event,
        expires_at=exit_at + timedelta(minutes=1),
    )

    exit_intent = adapter.apply_confirmed_event(
        exit_event,
        _bar("2026-07-13T11:00:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-exit",
        risk_snapshot=exit_snapshot,
        received_at=exit_event.observed_at,
    )

    assert exit_intent.side == "sell"
    assert exit_intent.entry_event_id == entry_event.event_id
    assert adapter.on_bar(_bar("2026-07-13T11:05:00+08:00")) == ()
    assert adapter.intents()[-1].status == "pending_t1"
    assert adapter.positions()[0].shares == 500
    assert adapter.on_bar(
        _bar("2026-07-14T09:30:00+08:00", limit_down_locked=True)
    ) == ()
    assert adapter.intents()[-1].status == "pending_limit_down"

    fills = adapter.on_bar(_bar("2026-07-14T09:35:00+08:00", open_price="9.5"))

    assert fills[0].side == "sell"
    assert fills[0].shares == 500
    assert fills[0].entry_event_id == entry_event.event_id
    assert adapter.positions() == ()
    assert adapter.intents()[-1].status == "filled"


def test_lot_floor_fixed_ids_and_all_fee_components_are_exact(
    make_bound_decision_event,
) -> None:
    ledger = InMemoryPaperLedger()
    adapter = ConfirmedEventPaperAdapter(
        ledger,
        fee_schedule=paper_adapter_module.PaperFeeSchedule(
            commission_rate=Decimal("0.0003"),
            minimum_commission=Decimal("5"),
            sell_stamp_duty_rate=Decimal("0.0005"),
            transfer_fee_rate=Decimal("0.00001"),
            regulatory_fee_rate=Decimal("0.0000541"),
            slippage_rate=Decimal("0.001"),
        ),
    )
    entry_event = make_bound_decision_event()
    entry_snapshot = _risk_snapshot(entry_event, shares=550)
    entry_intent = adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-fee-entry",
        risk_snapshot=entry_snapshot,
        received_at=entry_event.observed_at,
    )

    buy_fill = adapter.on_bar(
        _bar(
            "2026-07-13T10:35:00+08:00",
            open_price="8",
            close_price="10",
        )
    )[0]

    assert entry_intent.requested_shares == 500
    assert entry_intent.entry_event_id == entry_event.event_id
    assert entry_intent.review_id == "review-fee-entry"
    assert entry_intent.risk_snapshot_id == entry_snapshot.snapshot_id
    assert buy_fill.entry_event_id == entry_event.event_id
    assert buy_fill.review_id == "review-fee-entry"
    assert buy_fill.risk_snapshot_id == entry_snapshot.snapshot_id
    assert buy_fill.price == Decimal("10.01")
    assert buy_fill.gross_value == Decimal("5005.00")
    assert buy_fill.commission == Decimal("5.00")
    assert buy_fill.stamp_duty == Decimal("0.00")
    assert buy_fill.transfer_fee == Decimal("0.05")
    assert buy_fill.regulatory_fee == Decimal("0.27")
    assert buy_fill.slippage_cost == Decimal("5.00")
    assert buy_fill.trade_cost == Decimal("10.32")
    assert adapter.positions()[0].entry_review_id == "review-fee-entry"
    assert adapter.positions()[0].entry_risk_snapshot_id == entry_snapshot.snapshot_id

    exit_at = ts("2026-07-14T10:35:00+08:00")
    exit_event = make_bound_decision_event(
        bs_type="1sell",
        observed_at=exit_at,
        signal_at=exit_at,
        quote_time=exit_at,
        bar_closed_at=exit_at,
    )
    exit_snapshot = _risk_snapshot(exit_event)
    adapter.apply_confirmed_event(
        exit_event,
        _bar("2026-07-14T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-fee-exit",
        risk_snapshot=exit_snapshot,
        received_at=exit_at,
    )

    sell_fill = adapter.on_bar(
        _bar(
            "2026-07-14T10:35:00+08:00",
            open_price="15",
            close_price="12",
        )
    )[0]

    assert sell_fill.entry_event_id == entry_event.event_id
    assert sell_fill.review_id == "review-fee-exit"
    assert sell_fill.risk_snapshot_id == exit_snapshot.snapshot_id
    assert sell_fill.price == Decimal("11.99")
    assert sell_fill.gross_value == Decimal("5995.00")
    assert sell_fill.commission == Decimal("5.00")
    assert sell_fill.stamp_duty == Decimal("3.00")
    assert sell_fill.transfer_fee == Decimal("0.06")
    assert sell_fill.regulatory_fee == Decimal("0.32")
    assert sell_fill.slippage_cost == Decimal("5.00")
    assert sell_fill.trade_cost == Decimal("13.38")


def test_expired_risk_snapshot_cannot_fill_after_delay(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    snapshot = _risk_snapshot(
        event,
        expires_at=event.observed_at + timedelta(minutes=1),
    )
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-expiring",
        risk_snapshot=snapshot,
        received_at=event.observed_at,
    )

    fills = adapter.on_bar(_bar("2026-07-13T10:40:00+08:00"))

    assert fills == ()
    assert adapter.positions() == ()
    assert adapter.intents()[0].status == "expired_risk_snapshot"
    assert adapter.intents()[0].remaining_shares == 500


def test_restart_reconciles_pending_exit_without_duplicate_fill(
    make_bound_decision_event,
) -> None:
    ledger = InMemoryPaperLedger()
    first_adapter = ConfirmedEventPaperAdapter(ledger)
    entry_event = make_bound_decision_event()
    first_adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-restart-entry",
        risk_snapshot=_risk_snapshot(entry_event),
        received_at=entry_event.observed_at,
    )
    first_adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    exit_at = ts("2026-07-14T10:35:00+08:00")
    exit_event = make_bound_decision_event(
        bs_type="1sell",
        observed_at=exit_at,
        signal_at=exit_at,
        quote_time=exit_at,
        bar_closed_at=exit_at,
    )
    exit_snapshot = _risk_snapshot(exit_event, shares=900)
    signal_bar = _bar("2026-07-14T10:30:00+08:00")
    first_adapter.apply_confirmed_event(
        exit_event,
        signal_bar,
        event_state=EventState.CONFIRMED,
        review_id="review-restart-exit",
        risk_snapshot=exit_snapshot,
        received_at=exit_at,
    )
    blocked_bar = _bar(
        "2026-07-14T10:35:00+08:00",
        limit_down_locked=True,
    )
    assert first_adapter.on_bar(blocked_bar) == ()

    before_restart = reconcile_paper_ledger(ledger)

    assert before_restart.pending_intent_count == 1
    assert before_restart.position_count == 1
    restarted = ConfirmedEventPaperAdapter(ledger)
    replayed = restarted.apply_confirmed_event(
        exit_event,
        signal_bar,
        event_state=EventState.CONFIRMED,
        review_id="review-restart-exit",
        risk_snapshot=exit_snapshot,
        received_at=exit_at,
    )
    assert replayed.event_id == exit_event.event_id
    assert restarted.on_bar(blocked_bar) == ()
    assert len(restarted.fills()) == 1

    exit_fills = restarted.on_bar(
        _bar("2026-07-14T10:40:00+08:00", open_price="9.8")
    )

    assert len(exit_fills) == 1
    assert len(restarted.fills()) == 2
    assert restarted.positions() == ()
    after_restart = reconcile_paper_ledger(ledger)
    assert after_restart.pending_intent_count == 0
    assert after_restart.position_count == 0


def test_reconciliation_rejects_duplicate_fills_and_zero_quantity_lots(
    make_bound_decision_event,
) -> None:
    ledger = InMemoryPaperLedger()
    adapter = ConfirmedEventPaperAdapter(ledger)
    event = make_bound_decision_event()
    adapter.apply_confirmed_event(
        event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-integrity",
        risk_snapshot=_risk_snapshot(event),
        received_at=event.observed_at,
    )
    adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    state = ledger.load()

    duplicate_fill_ledger = InMemoryPaperLedger(
        replace(state, fills=state.fills + (state.fills[0],))
    )
    with pytest.raises(PaperLedgerIntegrityError, match="duplicate_fill_id"):
        reconcile_paper_ledger(duplicate_fill_ledger)

    zero_lot_ledger = InMemoryPaperLedger(
        replace(state, lots=(replace(state.lots[0], shares=0),))
    )
    with pytest.raises(PaperLedgerIntegrityError, match="invalid_lot_shares"):
        ConfirmedEventPaperAdapter(zero_lot_ledger)

    invalid_revision_ledger = InMemoryPaperLedger(
        replace(state, revision="not-an-integer")
    )
    with pytest.raises(PaperLedgerIntegrityError, match="invalid_ledger_revision"):
        reconcile_paper_ledger(invalid_revision_ledger)


def test_price_limit_is_derived_from_previous_close_when_lock_flag_is_absent(
    make_bound_decision_event,
) -> None:
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    entry_event = make_bound_decision_event()
    adapter.apply_confirmed_event(
        entry_event,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-derived-limit-entry",
        risk_snapshot=_risk_snapshot(entry_event),
        received_at=entry_event.observed_at,
    )

    assert adapter.on_bar(
        _bar(
            "2026-07-13T10:35:00+08:00",
            open_price="3.66",
            previous_close="3.33",
        )
    ) == ()
    assert adapter.intents()[0].status == "pending_limit_up"
    adapter.on_bar(
        _bar(
            "2026-07-13T10:40:00+08:00",
            open_price="3.50",
            previous_close="3.33",
        )
    )

    exit_at = ts("2026-07-14T10:35:00+08:00")
    exit_event = make_bound_decision_event(
        bs_type="1sell",
        observed_at=exit_at,
        signal_at=exit_at,
        quote_time=exit_at,
        bar_closed_at=exit_at,
    )
    adapter.apply_confirmed_event(
        exit_event,
        _bar("2026-07-14T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-derived-limit-exit",
        risk_snapshot=_risk_snapshot(exit_event),
        received_at=exit_at,
    )

    assert adapter.on_bar(
        _bar(
            "2026-07-14T10:35:00+08:00",
            open_price="3.00",
            previous_close="3.33",
        )
    ) == ()
    assert adapter.intents()[-1].status == "pending_limit_down"


def test_module_exposes_no_live_order_capability_or_trading_imports() -> None:
    assert paper_adapter_module.LIVE_ORDER_CAPABILITY is False
    source = Path(paper_adapter_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    forbidden_modules = (
        "chanlun.trader",
        "chanlun.exchange",
        "chanlun.recursive_bt.sim.paper",
        "xtquant",
        "qmt",
        "easytrader",
    )
    assert not any(
        module.startswith(forbidden_modules) for module in imported_modules
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {"place_order", "submit_order", "send_order", "order_stock"}
    )


def test_new_entry_is_rejected_while_entry_or_position_is_open(
    make_bound_decision_event,
) -> None:
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    first = make_bound_decision_event()
    adapter.apply_confirmed_event(
        first,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-first-entry",
        risk_snapshot=_risk_snapshot(first),
        received_at=first.observed_at,
    )
    second_at = ts("2026-07-13T10:40:00+08:00")
    second = make_bound_decision_event(
        bs_type="2buy",
        observed_at=second_at,
        signal_at=second_at,
        quote_time=second_at,
        bar_closed_at=second_at,
    )

    with pytest.raises(PaperAdapterEligibilityError, match="paper_entry_already_open"):
        adapter.apply_confirmed_event(
            second,
            _bar("2026-07-13T10:35:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id="review-second-entry",
            risk_snapshot=_risk_snapshot(second),
            received_at=second_at,
        )

    adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    third_at = ts("2026-07-13T10:45:00+08:00")
    third = make_bound_decision_event(
        bs_type="3buy",
        observed_at=third_at,
        signal_at=third_at,
        quote_time=third_at,
        bar_closed_at=third_at,
    )
    with pytest.raises(PaperAdapterEligibilityError, match="paper_entry_already_open"):
        adapter.apply_confirmed_event(
            third,
            _bar("2026-07-13T10:40:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id="review-third-entry",
            risk_snapshot=_risk_snapshot(third),
            received_at=third_at,
        )


def test_new_exit_is_rejected_while_an_exit_is_pending(
    make_bound_decision_event,
) -> None:
    adapter = ConfirmedEventPaperAdapter(InMemoryPaperLedger())
    entry = make_bound_decision_event()
    adapter.apply_confirmed_event(
        entry,
        _bar("2026-07-13T10:30:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-pending-exit-entry",
        risk_snapshot=_risk_snapshot(entry),
        received_at=entry.observed_at,
    )
    adapter.on_bar(_bar("2026-07-13T10:35:00+08:00"))
    first_exit_at = ts("2026-07-13T11:05:00+08:00")
    first_exit = make_bound_decision_event(
        bs_type="1sell",
        observed_at=first_exit_at,
        signal_at=first_exit_at,
        quote_time=first_exit_at,
        bar_closed_at=first_exit_at,
    )
    adapter.apply_confirmed_event(
        first_exit,
        _bar("2026-07-13T11:00:00+08:00"),
        event_state=EventState.CONFIRMED,
        review_id="review-first-exit",
        risk_snapshot=_risk_snapshot(
            first_exit,
            expires_at=first_exit_at + timedelta(days=2),
        ),
        received_at=first_exit_at,
    )
    adapter.on_bar(_bar("2026-07-13T11:05:00+08:00"))
    assert adapter.intents()[-1].status == "pending_t1"
    second_exit_at = ts("2026-07-13T11:10:00+08:00")
    second_exit = make_bound_decision_event(
        bs_type="2sell",
        observed_at=second_exit_at,
        signal_at=second_exit_at,
        quote_time=second_exit_at,
        bar_closed_at=second_exit_at,
    )

    with pytest.raises(PaperAdapterEligibilityError, match="paper_exit_already_pending"):
        adapter.apply_confirmed_event(
            second_exit,
            _bar("2026-07-13T11:05:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id="review-second-exit",
            risk_snapshot=_risk_snapshot(
                second_exit,
                expires_at=second_exit_at + timedelta(days=2),
            ),
            received_at=second_exit_at,
        )
