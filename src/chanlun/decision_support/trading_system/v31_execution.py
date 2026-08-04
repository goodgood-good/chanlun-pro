from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.v3_bar_execution import (
    BarProxyExecutionStatus,
    BarProxyMatchResult,
    HistoricalMinuteExecutionBar,
    bar_proxy_parameter_snapshot,
    match_historical_minute_bars,
)
from chanlun.decision_support.trading_system.v3_decision import V3DecisionIntent
from chanlun.decision_support.trading_system.v3_execution import (
    InstrumentKind,
    V3FeeModel,
    V3OrderIntent,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    etf_parameter_snapshot,
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v31_compliance import (
    ProgramTradingComplianceSnapshot,
    evaluate_program_trading_compliance,
)
from chanlun.decision_support.trading_system.v31_parameters import (
    StrategyV31Parameters,
)


_BUY_ACTIONS = {
    "ENTRY_INTENT",
    "TACTICAL_BUYBACK_INTENT",
    "PROTECTIVE_BUYBACK_INTENT",
    "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
}
_SELL_ACTIONS = {
    "STRATEGIC_REDUCE_INTENT",
    "STRATEGIC_EXIT_INTENT",
    "TACTICAL_SELL_INTENT",
    "TACTICAL_THIRD_SELL_EXIT_INTENT",
}


@dataclass(frozen=True, slots=True)
class V31PreparedOrder:
    """One V3.1 decision bound to the frozen V3 execution contract.

    The execution engine is intentionally reused rather than copied.  The
    wrapper retains both parameter identities so an order/fill can be traced
    to the V3.1 decision snapshot and to the parent execution snapshot that
    validates it.
    """

    v31_parameter_set_id: str
    parent_v3_parameter_set_id: str
    decision_action: str
    compliance_snapshot_id: str
    order: V3OrderIntent

    def __post_init__(self) -> None:
        if not self.v31_parameter_set_id or not self.compliance_snapshot_id:
            raise ValueError("V3.1 prepared order audit identity is required")
        if self.order.parameter_set_id != self.parent_v3_parameter_set_id:
            raise ValueError("V3.1 order is not bound to its parent V3 snapshot")
        if self.decision_action not in _BUY_ACTIONS | _SELL_ACTIONS:
            raise ValueError("V3.1 prepared order requires an order-producing action")


@dataclass(frozen=True, slots=True)
class V31ReplayMatchResult:
    v31_parameter_set_id: str
    parent_v3_parameter_set_id: str
    compliance_snapshot_id: str
    result: BarProxyMatchResult


def _parent_parameters(parameters: StrategyV31Parameters):
    return (
        individual_parameter_snapshot()
        if parameters.selection_path == "INDIVIDUAL_THREE_PROGRAM"
        else etf_parameter_snapshot()
    )


def prepare_v31_order(
    intent: V3DecisionIntent,
    *,
    parameters: StrategyV31Parameters,
    compliance: ProgramTradingComplianceSnapshot,
    instrument_kind: InstrumentKind,
    created_at: datetime,
    broker_confirmed_at: datetime,
    quantity_increment: int,
    expires_at: datetime | None,
) -> V31PreparedOrder:
    """Translate an auditable decision into the shared execution order.

    Optional intents require an explicit expiry supplied by the replay or
    paper adapter.  Persistent exits never expire.  No price or timing value
    is guessed in this boundary layer.
    """

    if intent.action not in _BUY_ACTIONS | _SELL_ACTIONS:
        raise ValueError("decision does not produce an order")
    if intent.quantity <= 0 or intent.price_cap_or_floor is None:
        raise ValueError("order decision requires positive quantity and price boundary")
    created = normalize_datetime(created_at, "created_at")
    confirmed = normalize_datetime(broker_confirmed_at, "broker_confirmed_at")
    if created < intent.confirmation_time:
        raise ValueError("order cannot be created before signal confirmation")
    compliance_decision = evaluate_program_trading_compliance(
        compliance,
        as_of=created,
        parameters=parameters,
    )
    if not compliance_decision.allowed:
        raise ValueError(
            "V3.1 compliance gate rejected order preparation: "
            + ",".join(compliance_decision.reason_codes)
        )
    if intent.persistence == "PERSISTENT_EXIT":
        if expires_at is not None:
            raise ValueError("persistent V3.1 exit cannot expire")
    elif intent.persistence == "OPTIONAL":
        if expires_at is None:
            raise ValueError("optional V3.1 order requires an explicit expiry")
    else:
        raise ValueError("order-producing V3.1 intent has invalid persistence")

    parent = _parent_parameters(parameters)
    if parent.parameter_set_id != parameters.parent_v3_parameter_set_id:
        raise ValueError("V3.1 parent execution snapshot changed")
    side: Literal["buy", "sell"] = (
        "buy" if intent.action in _BUY_ACTIONS else "sell"
    )
    identity = {
        "schema": "chanlun-v31-order-identity/v1",
        "v31_parameter_set_id": parameters.parameter_set_id,
        "parent_v3_parameter_set_id": parent.parameter_set_id,
        "action": intent.action,
        "rule_id": intent.rule_id,
        "symbol": intent.symbol,
        "quantity": intent.quantity,
        "limit_price": intent.price_cap_or_floor,
        "confirmation_time": intent.confirmation_time,
        "created_at": created,
        "broker_confirmed_at": confirmed,
        "structure_snapshot_id": intent.structure_snapshot_id,
        "selection_snapshot_id": intent.selection_snapshot_id,
        "account_snapshot_id": intent.account_snapshot_id,
    }
    digest = sha256_json(identity)
    order = V3OrderIntent(
        client_order_id=f"v31-order:{digest[7:]}",
        intent_id=f"v31-intent:{digest[7:]}",
        parameter_set_id=parent.parameter_set_id,
        rule_id=intent.rule_id,
        structure_snapshot_id=intent.structure_snapshot_id,
        selection_snapshot_id=intent.selection_snapshot_id,
        account_snapshot_id=intent.account_snapshot_id,
        symbol=intent.symbol,
        instrument_kind=instrument_kind,
        side=side,
        quantity=intent.quantity,
        limit_price=intent.price_cap_or_floor,
        signal_bar_end=intent.confirmation_time,
        created_at=created,
        broker_confirmed_at=confirmed,
        expires_at=expires_at,
        persistence=intent.persistence,
        quantity_increment=quantity_increment,
    )
    return V31PreparedOrder(
        v31_parameter_set_id=parameters.parameter_set_id,
        parent_v3_parameter_set_id=parent.parameter_set_id,
        decision_action=intent.action,
        compliance_snapshot_id=compliance.snapshot_id,
        order=order,
    )


def match_v31_historical_minute_bars(
    prepared: V31PreparedOrder,
    *,
    parameters: StrategyV31Parameters,
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    status: BarProxyExecutionStatus,
    fee_model: V3FeeModel,
    fee_session: date,
) -> V31ReplayMatchResult:
    """Run the same conservative completed-minute matcher used by V3."""

    if prepared.v31_parameter_set_id != parameters.parameter_set_id:
        raise ValueError("prepared order and V3.1 parameter snapshots differ")
    parent = _parent_parameters(parameters)
    if prepared.parent_v3_parameter_set_id != parent.parameter_set_id:
        raise ValueError("prepared order and parent V3 snapshots differ")
    result = match_historical_minute_bars(
        prepared.order,
        bars=bars,
        status=status,
        fee_model=fee_model,
        fee_session=fee_session,
        strategy_parameters=parent,
        proxy_parameters=bar_proxy_parameter_snapshot(parent),
    )
    return V31ReplayMatchResult(
        v31_parameter_set_id=parameters.parameter_set_id,
        parent_v3_parameter_set_id=parent.parameter_set_id,
        compliance_snapshot_id=prepared.compliance_snapshot_id,
        result=result,
    )


__all__ = [
    "V31PreparedOrder",
    "V31ReplayMatchResult",
    "match_v31_historical_minute_bars",
    "prepare_v31_order",
]
