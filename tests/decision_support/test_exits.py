from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from chanlun.decision_support.exits import (
    ExitSignalSnapshot,
    ExitStatus,
    ExitTrigger,
    TriggerEvidence,
    evaluate_exit_intent,
    select_exit_intent,
)
from chanlun.decision_support.risk import HoldingSnapshot


def _holding(event, *, code: str | None = None, shares: int = 1000, sellable=1000):
    return HoldingSnapshot(
        code=code or event.code,
        shares=shares,
        sellable_shares=sellable,
        opened_at=event.observed_at - timedelta(days=1),
        average_price=Decimal("9.5"),
    )


def _signals(
    event,
    *active: ExitTrigger,
    operation_bs_type: str | None = "3sell",
    control_direction: str = "up",
    trigger_price: Decimal = Decimal("10"),
) -> ExitSignalSnapshot:
    active_set = set(active)
    return ExitSignalSnapshot(
        observed_at=event.observed_at,
        trigger_price=trigger_price,
        hard_risk=ExitTrigger.HARD_RISK in active_set,
        structural_invalidation=(
            ExitTrigger.STRUCTURAL_INVALIDATION in active_set
        ),
        control_level_down=ExitTrigger.CONTROL_LEVEL_DOWN in active_set,
        control_level_sell=ExitTrigger.CONTROL_LEVEL_SELL in active_set,
        operation_level_sell=ExitTrigger.OPERATION_LEVEL_SELL in active_set,
        control_direction=control_direction,
        operation_bs_type=(
            operation_bs_type
            if ExitTrigger.OPERATION_LEVEL_SELL in active_set
            else None
        ),
        operation_level=1,
        control_level=2,
        swing_level=0,
        evidence=tuple(
            TriggerEvidence(
                trigger=trigger,
                evidence_ids=(f"evidence:{trigger.value}",),
                reason=f"observed {trigger.value}",
            )
            for trigger in active
        ),
    )


@pytest.mark.parametrize(
    ("active", "expected"),
    (
        (
            (
                ExitTrigger.HARD_RISK,
                ExitTrigger.STRUCTURAL_INVALIDATION,
                ExitTrigger.CONTROL_LEVEL_SELL,
                ExitTrigger.OPERATION_LEVEL_SELL,
            ),
            ExitTrigger.HARD_RISK,
        ),
        (
            (
                ExitTrigger.STRUCTURAL_INVALIDATION,
                ExitTrigger.CONTROL_LEVEL_SELL,
                ExitTrigger.OPERATION_LEVEL_SELL,
            ),
            ExitTrigger.STRUCTURAL_INVALIDATION,
        ),
        (
            (
                ExitTrigger.CONTROL_LEVEL_DOWN,
                ExitTrigger.CONTROL_LEVEL_SELL,
                ExitTrigger.OPERATION_LEVEL_SELL,
            ),
            ExitTrigger.CONTROL_LEVEL_DOWN,
        ),
        (
            (
                ExitTrigger.CONTROL_LEVEL_SELL,
                ExitTrigger.OPERATION_LEVEL_SELL,
            ),
            ExitTrigger.CONTROL_LEVEL_SELL,
        ),
        (
            (ExitTrigger.OPERATION_LEVEL_SELL,),
            ExitTrigger.OPERATION_LEVEL_SELL,
        ),
    ),
)
def test_exit_trigger_priority_is_fixed(
    make_decision_event,
    active,
    expected,
) -> None:
    event = make_decision_event()
    position = _holding(event)
    direction = (
        "down" if ExitTrigger.CONTROL_LEVEL_DOWN in active else "up"
    )

    selection = select_exit_intent(
        entry_event_id="entry:event:1",
        position=position,
        signals=_signals(
            event,
            *active,
            control_direction=direction,
        ),
    )

    assert selection.intent is not None
    assert selection.intent.trigger is expected
    assert selection.intent.full_exit is (
        expected is not ExitTrigger.OPERATION_LEVEL_SELL
    )
    assert selection.intent.requested_shares == (
        position.shares
        if expected is not ExitTrigger.OPERATION_LEVEL_SELL
        else 200
    )
    assert set(item.trigger for item in selection.intent.evidence) == set(active)


def test_structural_and_control_down_tie_is_deterministic(
    make_decision_event,
) -> None:
    event = make_decision_event()

    selection = select_exit_intent(
        entry_event_id="entry:event:tie",
        position=_holding(event),
        signals=_signals(
            event,
            ExitTrigger.STRUCTURAL_INVALIDATION,
            ExitTrigger.CONTROL_LEVEL_DOWN,
            control_direction="down",
        ),
    )

    assert selection.intent is not None
    assert selection.intent.trigger is ExitTrigger.STRUCTURAL_INVALIDATION
    assert "co_priority:control_level_down" in selection.intent.reasons


def test_full_exit_is_bound_and_output_is_immutable_serializable(
    make_decision_event,
) -> None:
    event = make_decision_event()
    selection = select_exit_intent(
        entry_event_id="entry:event:bound",
        position=_holding(event),
        signals=_signals(event, ExitTrigger.CONTROL_LEVEL_SELL),
    )
    intent = selection.intent

    assert intent is not None
    assert intent.entry_event_id == "entry:event:bound"
    assert intent.code == event.code
    assert intent.intent_id.startswith("exit:")
    assert json.loads(json.dumps(intent.to_dict()))["code"] == event.code
    with pytest.raises(FrozenInstanceError):
        intent.requested_shares = 1


@pytest.mark.parametrize(
    ("bs_type", "direction", "shares", "expected_fraction", "expected_shares"),
    (
        ("1sell", "up", 1000, Decimal("1"), 1000),
        ("2sell", "up", 1000, Decimal("0.5"), 500),
        ("3sell", "up", 1000, Decimal("0.25"), 200),
        ("3sell", "neutral", 1000, Decimal("0.5"), 500),
    ),
)
def test_operation_sell_uses_explicit_original_layered_fraction_and_lot(
    make_decision_event,
    bs_type,
    direction,
    shares,
    expected_fraction,
    expected_shares,
) -> None:
    event = make_decision_event()

    selection = select_exit_intent(
        entry_event_id="entry:event:layered",
        position=_holding(event, shares=shares),
        signals=_signals(
            event,
            ExitTrigger.OPERATION_LEVEL_SELL,
            operation_bs_type=bs_type,
            control_direction=direction,
        ),
    )

    assert selection.intent is not None
    assert selection.intent.layered_fraction == expected_fraction
    assert selection.intent.requested_shares == expected_shares
    assert selection.intent.requested_shares % 100 == 0


def test_sublot_operation_exit_does_not_create_stuck_intent(
    make_decision_event,
) -> None:
    event = make_decision_event()

    selection = select_exit_intent(
        entry_event_id="entry:event:sublot",
        position=_holding(event, shares=200, sellable=200),
        signals=_signals(event, ExitTrigger.OPERATION_LEVEL_SELL),
    )

    assert selection.intent is None
    assert selection.reasons == ("layered_exit_below_lot",)


def test_t_plus_one_exit_stays_pending_and_same_inputs_are_idempotent(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    position = _holding(event, sellable=0)
    context = make_risk_context(
        holdings=(position,),
        asof=event.observed_at,
    )
    selection = select_exit_intent(
        entry_event_id="entry:event:t1",
        position=position,
        signals=_signals(event, ExitTrigger.STRUCTURAL_INVALIDATION),
    )
    intent = selection.intent
    assert intent is not None

    first = evaluate_exit_intent(intent, event, position, context)
    second = evaluate_exit_intent(intent, event, position, context)

    assert first == second
    assert first.status is ExitStatus.PENDING
    assert first.executable_shares == 0
    assert first.pending_shares == position.shares
    assert first.intent_id == intent.intent_id
    assert "t_plus_one" in first.reasons


def test_pending_intent_retries_on_next_tradable_bar(
    make_decision_event,
    make_risk_context,
) -> None:
    first_event = make_decision_event()
    first_position = _holding(first_event, sellable=0)
    intent = select_exit_intent(
        entry_event_id="entry:event:retry",
        position=first_position,
        signals=_signals(first_event, ExitTrigger.OPERATION_LEVEL_SELL),
    ).intent
    assert intent is not None
    first_context = make_risk_context(
        holdings=(first_position,),
        asof=first_event.observed_at,
    )
    blocked = evaluate_exit_intent(
        intent,
        first_event,
        first_position,
        first_context,
    )

    next_time = first_event.observed_at + timedelta(days=1)
    next_event = make_decision_event(
        observed_at=next_time,
        quote_time=next_time,
    )
    next_position = replace(first_position, sellable_shares=first_position.shares)
    next_context = make_risk_context(
        holdings=(next_position,),
        asof=next_time,
    )
    retried = evaluate_exit_intent(
        intent,
        next_event,
        next_position,
        next_context,
    )

    assert blocked.status is ExitStatus.PENDING
    assert retried.status is ExitStatus.EXECUTABLE
    assert retried.intent_id == blocked.intent_id
    assert retried.executable_shares == intent.requested_shares
    assert retried.pending_shares == 0


@pytest.mark.parametrize(
    "context_changes",
    (
        {"limit_down_locked": True},
        {"exit_tradable": False},
    ),
)
def test_limit_down_or_untradable_exit_remains_pending(
    make_decision_event,
    make_risk_context,
    context_changes,
) -> None:
    event = make_decision_event()
    position = _holding(event)
    context = make_risk_context(
        holdings=(position,),
        asof=event.observed_at,
        **context_changes,
    )
    intent = select_exit_intent(
        entry_event_id="entry:event:blocked",
        position=position,
        signals=_signals(event, ExitTrigger.CONTROL_LEVEL_SELL),
    ).intent
    assert intent is not None

    outcome = evaluate_exit_intent(intent, event, position, context)

    assert outcome.status is ExitStatus.PENDING
    assert outcome.executable_shares == 0
    assert outcome.pending_shares == position.shares


def test_no_trigger_returns_auditable_noop(make_decision_event) -> None:
    event = make_decision_event()

    selection = select_exit_intent(
        entry_event_id="entry:event:noop",
        position=_holding(event),
        signals=_signals(event),
    )

    assert selection.intent is None
    assert selection.reasons == ("no_exit_trigger",)
    assert json.loads(json.dumps(selection.to_dict()))["intent"] is None


@pytest.mark.parametrize(
    "mutation",
    (
        {"hard_risk": None},
        {"control_direction": "unknown"},
        {"operation_level_sell": True, "operation_bs_type": None},
        {"control_level_down": True, "control_direction": "up"},
    ),
)
def test_unknown_or_conflicting_signal_state_fails_closed(
    make_decision_event,
    mutation,
) -> None:
    event = make_decision_event()
    values = _signals(event).__dict__ if hasattr(_signals(event), "__dict__") else {
        field: getattr(_signals(event), field)
        for field in ExitSignalSnapshot.__dataclass_fields__
    }
    values.update(mutation)

    with pytest.raises(ValueError):
        ExitSignalSnapshot(**values)


@pytest.mark.parametrize("price", (Decimal("NaN"), Decimal("Infinity")))
def test_non_finite_trigger_price_fails_closed(make_decision_event, price) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match="trigger_price must be finite"):
        _signals(event, ExitTrigger.CONTROL_LEVEL_SELL, trigger_price=price)


def test_trigger_evidence_must_match_active_signals(make_decision_event) -> None:
    event = make_decision_event()
    signals = _signals(event, ExitTrigger.HARD_RISK)

    with pytest.raises(ValueError, match="evidence must match active triggers"):
        replace(signals, evidence=())


def test_position_and_intent_code_conflicts_never_exit(
    make_decision_event,
    make_risk_context,
) -> None:
    event = make_decision_event()
    position = _holding(event)
    intent = select_exit_intent(
        entry_event_id="entry:event:identity",
        position=position,
        signals=_signals(event, ExitTrigger.HARD_RISK),
    ).intent
    assert intent is not None
    other = _holding(event, code="SZ.000001")

    with pytest.raises(ValueError, match="intent and position code mismatch"):
        evaluate_exit_intent(
            intent,
            event,
            other,
            make_risk_context(holdings=(position,), asof=event.observed_at),
        )
