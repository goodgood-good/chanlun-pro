"""Read-only accounting for the human-confirmed virtual fill ledger.

The virtual fill engine deliberately has no broker or account dependency.  This
module reconstructs its cash movements with the *same frozen fee snapshot* used
by the research replay.  It does not manufacture end-of-day marks: cash, fees,
FIFO cost basis and realised P&L are accounting facts, while portfolio return,
drawdown and Sharpe remain unavailable until an immutable daily valuation
ledger exists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.execution import (
    InstrumentKind,
    FeeModel,
    FeeRateAt,
)
from chanlun.decision_support.trading_system.forward_paper import (
    FROZEN_RESEARCH_PARAMETER_SET_ID,
    load_frozen_forward_contract,
)


ACCOUNTING_SCHEMA = "chanlun-human-paper-accounting"
PORTFOLIO_DECISION_AUDIT_SCHEMA = (
    "chanlun-human-paper-portfolio-decision-audit"
)
PORTFOLIO_FILL_DECISION_AUDIT_SCHEMA = (
    "chanlun-human-paper-portfolio-fill-decision-audit"
)
_EXPECTED_FEE_SCHEDULE = {
    "schedule_id": "A_SHARE_RESEARCH_2025",
    "commission_rate": "0.0003",
    "minimum_commission": "5",
    "stock_sell_stamp_rate": "0.0005",
    "transfer_rate": "0.00001",
    "other_buy_rate": "0",
    "other_sell_rate": "0",
    "currency_quantum": "0.01",
}


@dataclass(frozen=True, slots=True)
class HumanPaperAccountingParameters:
    strategy_parameter_set_id: str
    parameter_snapshot_sha256: str
    effective_from: date
    initial_cash: Decimal
    slot_count: int
    slot_fraction: Decimal
    account_exposure_cap: Decimal
    instrument_kind: InstrumentKind
    fee_model: FeeModel
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if self.strategy_parameter_set_id != FROZEN_RESEARCH_PARAMETER_SET_ID:
            raise ValueError("human paper accounting parameter identity changed")
        if (
            self.initial_cash != Decimal("1000000")
            or self.slot_count != 5
            or self.slot_fraction != Decimal("0.18")
            or self.account_exposure_cap != Decimal("0.90")
            or self.instrument_kind != "A_SHARE_STOCK"
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human paper accounting capital contract changed")

    @property
    def accounting_contract_id(self) -> str:
        rate = self.fee_model.rates[0]
        fee_rate = asdict(rate)
        fee_rate["effective_from"] = rate.effective_from.isoformat()
        return sha256_json(
            {
                "schema": "chanlun-human-paper-accounting-contract",
                "strategy_parameter_set_id": self.strategy_parameter_set_id,
                "parameter_snapshot_sha256": self.parameter_snapshot_sha256,
                "effective_from": self.effective_from.isoformat(),
                "initial_cash": self.initial_cash,
                "slot_count": self.slot_count,
                "slot_fraction": self.slot_fraction,
                "account_exposure_cap": self.account_exposure_cap,
                "instrument_kind": self.instrument_kind,
                "fee_schedule_id": self.fee_model.schedule_id,
                "fee_rate": fee_rate,
                "currency_quantum": self.fee_model.currency_quantum,
                "daily_valuation_required_for_performance": True,
                "broker_transport_available": False,
                "live_status": self.live_status,
            }
        )


def load_human_paper_accounting_parameters(
    parameter_snapshot_path: Path,
) -> HumanPaperAccountingParameters:
    """Load and independently freeze the fee/capital subset of the snapshot."""

    contract = load_frozen_forward_contract(parameter_snapshot_path)
    try:
        payload = json.loads(parameter_snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("human paper accounting parameter snapshot cannot be read") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("human paper accounting parameter snapshot is invalid")
    fee = payload.get("fee_schedule")
    if not isinstance(fee, Mapping) or dict(fee) != _EXPECTED_FEE_SCHEDULE:
        raise ValueError("frozen human paper fee schedule changed")
    if payload.get("effective_start") != "2025-08-01":
        raise ValueError("frozen human paper accounting effective date changed")
    try:
        effective_from = date.fromisoformat(str(payload["effective_start"]))
        rate = FeeRateAt(
            effective_from=effective_from,
            commission_rate=Decimal(str(fee["commission_rate"])),
            minimum_commission=Decimal(str(fee["minimum_commission"])),
            stock_sell_stamp_rate=Decimal(str(fee["stock_sell_stamp_rate"])),
            transfer_rate=Decimal(str(fee["transfer_rate"])),
            other_buy_rate=Decimal(str(fee["other_buy_rate"])),
            other_sell_rate=Decimal(str(fee["other_sell_rate"])),
        )
        fee_model = FeeModel(
            schedule_id=str(fee["schedule_id"]),
            rates=(rate,),
            currency_quantum=Decimal(str(fee["currency_quantum"])),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("frozen human paper fee schedule is invalid") from exc
    return HumanPaperAccountingParameters(
        strategy_parameter_set_id=contract.strategy_parameter_set_id,
        parameter_snapshot_sha256=contract.strategy_parameter_snapshot_sha256,
        effective_from=effective_from,
        initial_cash=Decimal(contract.initial_cash),
        slot_count=contract.slot_count,
        slot_fraction=Decimal(contract.slot_fraction),
        account_exposure_cap=Decimal(contract.account_exposure_cap),
        # The frozen selection path is QMT current-sector stocks.  ETF proxy is
        # a separate parameter snapshot and must never be mixed into this book.
        instrument_kind="A_SHARE_STOCK",
        fee_model=fee_model,
    )


def _money(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum)


def rebuild_human_paper_accounting(
    events: Sequence[Mapping[str, object]],
    *,
    parameters: HumanPaperAccountingParameters,
    execution_evidence_status: str,
) -> dict[str, object]:
    """Rebuild fill cash flows without pretending they are a marked portfolio."""

    quantum = parameters.fee_model.currency_quantum
    cash = parameters.initial_cash
    total_fees = Decimal("0")
    turnover = Decimal("0")
    realised = Decimal("0")
    fill_count = 0
    closed_cycle_count = 0
    violations: list[str] = []
    # Each FIFO lot stores [quantity, total remaining acquisition cost, session].
    lots: dict[str, list[list[object]]] = {}

    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "FILL" or not isinstance(payload, Mapping):
            continue
        try:
            symbol = str(payload["symbol"])
            side = str(payload["side"])
            quantity = int(payload["quantity"])
            price = Decimal(str(payload["price"]))
            session = datetime.fromisoformat(str(payload["filled_at"])).date()
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("human paper accounting fill is malformed") from exc
        if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0:
            raise ValueError("human paper accounting fill values are invalid")
        notional = Decimal(quantity) * price
        fee = parameters.fee_model.order_cost(
            side="buy" if side == "BUY" else "sell",
            instrument_kind=parameters.instrument_kind,
            quantity=quantity,
            price=price,
            session=session,
        )
        fill_count += 1
        turnover += notional
        total_fees += fee
        if side == "BUY":
            cash -= notional + fee
            lots.setdefault(symbol, []).append([quantity, notional + fee, session])
        else:
            remaining = quantity
            released_cost = Decimal("0")
            for lot in lots.get(symbol, []):
                lot_quantity = int(lot[0])
                if not lot_quantity:
                    continue
                take = min(lot_quantity, remaining)
                lot_cost = Decimal(str(lot[1]))
                take_cost = lot_cost * Decimal(take) / Decimal(lot_quantity)
                lot[0] = lot_quantity - take
                lot[1] = lot_cost - take_cost
                released_cost += take_cost
                remaining -= take
                if not remaining:
                    break
            if remaining:
                raise ValueError("human paper accounting contains a virtual oversell")
            proceeds = notional - fee
            cash += proceeds
            realised += proceeds - released_cost
            closed_cycle_count += 1

        open_symbols = sum(
            any(int(lot[0]) > 0 for lot in symbol_lots)
            for symbol_lots in lots.values()
        )
        if cash < 0 and "NEGATIVE_VIRTUAL_CASH" not in violations:
            violations.append("NEGATIVE_VIRTUAL_CASH")
        if (
            open_symbols > parameters.slot_count
            and "VIRTUAL_SLOT_COUNT_EXCEEDED" not in violations
        ):
            violations.append("VIRTUAL_SLOT_COUNT_EXCEEDED")

    positions: dict[str, dict[str, object]] = {}
    remaining_cost = Decimal("0")
    for symbol, symbol_lots in sorted(lots.items()):
        quantity = sum(int(lot[0]) for lot in symbol_lots)
        if not quantity:
            continue
        cost = sum(
            (Decimal(str(lot[1])) for lot in symbol_lots if int(lot[0]) > 0),
            Decimal("0"),
        )
        remaining_cost += cost
        positions[symbol] = {
            "quantity": quantity,
            "remaining_cost_basis": format(_money(cost, quantum), "f"),
            "average_cost": format(cost / Decimal(quantity), "f"),
            "oldest_acquired_session": min(
                str(lot[2]) for lot in symbol_lots if int(lot[0]) > 0
            ),
        }
    cost_exposure_ratio = (
        remaining_cost / parameters.initial_cash
        if parameters.initial_cash
        else Decimal("0")
    )
    if cost_exposure_ratio > parameters.account_exposure_cap:
        violations.append("VIRTUAL_COST_BASIS_EXPOSURE_CAP_EXCEEDED")

    evidence_complete = (
        execution_evidence_status == "COMPLETE"
        if fill_count
        else execution_evidence_status == "NO_FILLS"
    )
    if not fill_count:
        status = "NO_FILLS"
    elif not evidence_complete:
        status = "EXECUTION_EVIDENCE_UNVERIFIED"
    elif violations:
        status = "CONSTRAINT_VIOLATION"
    elif positions:
        status = "OPEN_POSITIONS_UNMARKED"
    else:
        status = "CLOSED_BOOK_NO_DAILY_EQUITY"

    reasons = ["HUMAN_REVIEW_SCREENING_IS_NOT_PORTFOLIO_BACKTEST"]
    if not fill_count:
        reasons.append("NO_VIRTUAL_FILL_SAMPLE")
    if not evidence_complete:
        reasons.append("EXECUTION_EVIDENCE_NOT_COMPLETE")
    if positions:
        reasons.extend(
            (
                "OPEN_POSITIONS_REQUIRE_IMMUTABLE_DAILY_MARKS",
                "CORPORATE_ACTION_CASH_RECONCILIATION_NOT_ATTACHED",
            )
        )
    reasons.append("DAILY_EQUITY_CURVE_UNAVAILABLE")
    reasons.extend(violations)

    stable: dict[str, object] = {
        "schema": ACCOUNTING_SCHEMA,
        "accounting_contract_id": parameters.accounting_contract_id,
        "strategy_parameter_set_id": parameters.strategy_parameter_set_id,
        "parameter_snapshot_sha256": parameters.parameter_snapshot_sha256,
        "status": status,
        "accounting_valid": evidence_complete and not violations,
        "performance_evaluable": False,
        "fee_model_attached": True,
        "fee_schedule_id": parameters.fee_model.schedule_id,
        "cash_ledger_attached": True,
        "cash_ledger_complete": evidence_complete and not positions and not violations,
        "equity_curve_available": False,
        "initial_cash": format(parameters.initial_cash, "f"),
        "cash_balance": format(_money(cash, quantum), "f"),
        "total_fees": format(_money(total_fees, quantum), "f"),
        "turnover_notional": format(_money(turnover, quantum), "f"),
        "realized_pnl": format(_money(realised, quantum), "f"),
        "remaining_cost_basis": format(_money(remaining_cost, quantum), "f"),
        "cost_basis_exposure_ratio": format(cost_exposure_ratio, "f"),
        "fill_count": fill_count,
        "closed_cycle_count": closed_cycle_count,
        "open_position_count": len(positions),
        "positions": positions,
        "constraint_violations": violations,
        "execution_evidence_status": execution_evidence_status,
        "reason_codes": reasons,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def assess_human_paper_portfolio_fill(
    events: Sequence[Mapping[str, object]],
    *,
    parameters: HumanPaperAccountingParameters,
    symbol: str,
    quantity: int,
    price: Decimal,
    session: date,
    position_marks: Mapping[str, Decimal],
) -> dict[str, object]:
    """Evaluate cash, slots and 18%/90% caps from synchronous 1m marks."""

    if not symbol or quantity <= 0 or price <= 0:
        raise ValueError("human paper portfolio candidate is invalid")
    fill_count = sum(
        event.get("kind") == "FILL" and isinstance(event.get("payload"), Mapping)
        for event in events
    )
    accounting = rebuild_human_paper_accounting(
        events,
        parameters=parameters,
        execution_evidence_status="COMPLETE" if fill_count else "NO_FILLS",
    )
    if accounting.get("constraint_violations"):
        raise ValueError("existing human paper account constraints are invalid")
    positions = accounting.get("positions")
    if not isinstance(positions, Mapping):
        raise ValueError("human paper positions are unavailable")
    if set(position_marks) != set(positions):
        raise ValueError("synchronous marks do not cover every open position")

    mark_rows: list[dict[str, object]] = []
    current_market_value = Decimal("0")
    for position_symbol, raw_position in sorted(positions.items()):
        if not isinstance(raw_position, Mapping):
            raise ValueError("human paper position is malformed")
        mark = Decimal(str(position_marks[position_symbol]))
        quantity_at_decision = int(raw_position["quantity"])
        if mark <= 0 or quantity_at_decision <= 0:
            raise ValueError("human paper synchronous position mark is invalid")
        market_value = Decimal(quantity_at_decision) * mark
        current_market_value += market_value
        mark_rows.append(
            {
                "symbol": position_symbol,
                "quantity": quantity_at_decision,
                "price": format(mark, "f"),
                "market_value": format(
                    _money(market_value, parameters.fee_model.currency_quantum),
                    "f",
                ),
            }
        )

    cash = Decimal(str(accounting["cash_balance"]))
    equity = cash + current_market_value
    if equity <= 0:
        raise ValueError("human paper decision-time equity is not positive")
    fee = parameters.fee_model.order_cost(
        side="buy",
        instrument_kind=parameters.instrument_kind,
        quantity=quantity,
        price=price,
        session=session,
    )
    notional = Decimal(quantity) * price
    required_cash = notional + fee
    occupied_slots = len(positions)
    slot_cap = equity * parameters.slot_fraction
    exposure_cap = equity * parameters.account_exposure_cap
    post_trade_gross = current_market_value + notional
    reasons: list[str] = []
    if symbol in positions:
        reasons.append("VIRTUAL_SYMBOL_ALREADY_OCCUPIES_STRATEGIC_SLOT")
    if symbol not in positions and occupied_slots >= parameters.slot_count:
        reasons.append("NO_FREE_VIRTUAL_STRATEGIC_SLOT")
    if required_cash > cash:
        reasons.append("INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES")
    if notional > slot_cap:
        reasons.append("VIRTUAL_ENTRY_EXCEEDS_ONE_SLOT_NOTIONAL_CAP")
    if post_trade_gross > exposure_cap:
        reasons.append("VIRTUAL_ACCOUNT_EXPOSURE_CAP_EXCEEDED")

    stable: dict[str, object] = {
        "schema": "chanlun-human-paper-portfolio-decision",
        "accounting_contract_id": parameters.accounting_contract_id,
        "symbol": symbol,
        "quantity": quantity,
        "price": format(price, "f"),
        "session": session.isoformat(),
        "available_cash": format(_money(cash, parameters.fee_model.currency_quantum), "f"),
        "current_market_value": format(
            _money(current_market_value, parameters.fee_model.currency_quantum),
            "f",
        ),
        "account_equity": format(
            _money(equity, parameters.fee_model.currency_quantum),
            "f",
        ),
        "notional": format(_money(notional, parameters.fee_model.currency_quantum), "f"),
        "terminal_buy_fee": format(fee, "f"),
        "required_cash": format(
            _money(required_cash, parameters.fee_model.currency_quantum),
            "f",
        ),
        "occupied_slots": occupied_slots,
        "slot_count": parameters.slot_count,
        "slot_fraction": format(parameters.slot_fraction, "f"),
        "slot_notional_cap": format(slot_cap, "f"),
        "account_exposure_cap": format(parameters.account_exposure_cap, "f"),
        "account_exposure_notional_cap": format(exposure_cap, "f"),
        "post_trade_gross_market_value": format(post_trade_gross, "f"),
        "position_marks": mark_rows,
        "allowed": not reasons,
        "reason_codes": reasons,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "fixed_one_lot_diagnostic": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def audit_human_paper_portfolio_decisions(
    events: Sequence[Mapping[str, object]],
    *,
    parameters: HumanPaperAccountingParameters,
) -> dict[str, object]:
    """Recompute every portfolio rejection from its strict ledger prefix."""

    rejection_indexes = tuple(
        index
        for index, event in enumerate(events)
        if event.get("kind") == "PORTFOLIO_REJECT"
        and isinstance(event.get("payload"), Mapping)
    )
    invalid: list[dict[str, str]] = []
    for index in rejection_indexes:
        payload = events[index]["payload"]
        assert isinstance(payload, Mapping)
        rejection_id = str(payload.get("rejection_id") or "")
        try:
            raw_marks = payload["position_marks"]
            if not isinstance(raw_marks, list):
                raise ValueError("portfolio rejection marks are malformed")
            marks = {
                str(value["symbol"]): Decimal(str(value["price"]))
                for value in raw_marks
                if isinstance(value, Mapping)
            }
            if len(marks) != len(raw_marks):
                raise ValueError("portfolio rejection marks are not unique")
            candidate_at = datetime.fromisoformat(
                str(payload["candidate_bar_opened_at"])
            )
            expected = assess_human_paper_portfolio_fill(
                events[:index],
                parameters=parameters,
                symbol=str(payload["symbol"]),
                quantity=int(payload["quantity"]),
                price=Decimal(str(payload["candidate_price"])),
                session=candidate_at.date(),
                position_marks=marks,
            )
            decimal_fields = {
                "candidate_price": "price",
                "available_cash": "available_cash",
                "current_market_value": "current_market_value",
                "account_equity": "account_equity",
                "notional": "notional",
                "terminal_buy_fee": "terminal_buy_fee",
                "required_cash": "required_cash",
                "slot_fraction": "slot_fraction",
                "slot_notional_cap": "slot_notional_cap",
                "account_exposure_cap": "account_exposure_cap",
                "account_exposure_notional_cap": (
                    "account_exposure_notional_cap"
                ),
                "post_trade_gross_market_value": (
                    "post_trade_gross_market_value"
                ),
            }
            if (
                payload.get("accounting_contract_id")
                != expected["accounting_contract_id"]
                or payload.get("portfolio_decision_sha256")
                != expected["content_sha256"]
                or payload.get("symbol") != expected["symbol"]
                or int(payload["quantity"]) != int(expected["quantity"])
                or any(
                    Decimal(str(payload[actual]))
                    != Decimal(str(expected[derived]))
                    for actual, derived in decimal_fields.items()
                )
                or int(payload["occupied_slots"])
                != int(expected["occupied_slots"])
                or int(payload["slot_count"]) != int(expected["slot_count"])
                or raw_marks != expected["position_marks"]
                or tuple(payload.get("reason_codes") or ())
                != tuple(expected["reason_codes"])
                or expected["allowed"] is not False
            ):
                raise ValueError("portfolio rejection prefix reconstruction disagrees")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "rejection_id": rejection_id,
                    "event_index": str(index),
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )

    status = "NO_REJECTIONS" if not rejection_indexes else "COMPLETE"
    if invalid:
        status = "INVALID"
    return {
        "schema": PORTFOLIO_DECISION_AUDIT_SCHEMA,
        "status": status,
        "rejection_count": len(rejection_indexes),
        "verified_rejection_count": len(rejection_indexes) - len(invalid),
        "invalid_decisions": invalid,
        "accounting_contract_id": parameters.accounting_contract_id,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_portfolio_fill_decisions(
    events: Sequence[Mapping[str, object]],
    *,
    parameters: HumanPaperAccountingParameters,
) -> dict[str, object]:
    """Recompute every atomic BUY-fill approval from its ledger prefix."""

    approved_fill_indexes = tuple(
        index
        for index, event in enumerate(events)
        if event.get("kind") == "FILL"
        and isinstance(event.get("payload"), Mapping)
        and "portfolio_decision_sha256" in event["payload"]
    )
    invalid: list[dict[str, str]] = []
    for index in approved_fill_indexes:
        payload = events[index]["payload"]
        assert isinstance(payload, Mapping)
        fill_id = str(payload.get("fill_id") or "")
        try:
            raw_marks = payload["position_marks"]
            if not isinstance(raw_marks, list):
                raise ValueError("portfolio fill marks are malformed")
            marks = {
                str(value["symbol"]): Decimal(str(value["price"]))
                for value in raw_marks
                if isinstance(value, Mapping)
            }
            if len(marks) != len(raw_marks):
                raise ValueError("portfolio fill marks are not unique")
            filled_at = datetime.fromisoformat(str(payload["filled_at"]))
            expected = assess_human_paper_portfolio_fill(
                events[:index],
                parameters=parameters,
                symbol=str(payload["symbol"]),
                quantity=int(payload["quantity"]),
                price=Decimal(str(payload["price"])),
                session=filled_at.date(),
                position_marks=marks,
            )
            decimal_fields = {
                "price": "price",
                "available_cash": "available_cash",
                "current_market_value": "current_market_value",
                "account_equity": "account_equity",
                "notional": "notional",
                "terminal_buy_fee": "terminal_buy_fee",
                "required_cash": "required_cash",
                "slot_fraction": "slot_fraction",
                "slot_notional_cap": "slot_notional_cap",
                "account_exposure_cap": "account_exposure_cap",
                "account_exposure_notional_cap": (
                    "account_exposure_notional_cap"
                ),
                "post_trade_gross_market_value": (
                    "post_trade_gross_market_value"
                ),
            }
            if (
                payload.get("side") != "BUY"
                or payload.get("accounting_contract_id")
                != expected["accounting_contract_id"]
                or payload.get("portfolio_decision_sha256")
                != expected["content_sha256"]
                or payload.get("symbol") != expected["symbol"]
                or int(payload["quantity"]) != int(expected["quantity"])
                or any(
                    Decimal(str(payload[actual]))
                    != Decimal(str(expected[derived]))
                    for actual, derived in decimal_fields.items()
                )
                or int(payload["occupied_slots"])
                != int(expected["occupied_slots"])
                or int(payload["slot_count"]) != int(expected["slot_count"])
                or raw_marks != expected["position_marks"]
                or expected["allowed"] is not True
                or tuple(expected["reason_codes"])
            ):
                raise ValueError(
                    "portfolio fill approval prefix reconstruction disagrees"
                )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "fill_id": fill_id,
                    "event_index": str(index),
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )

    status = "NO_APPROVED_FILLS" if not approved_fill_indexes else "COMPLETE"
    if invalid:
        status = "INVALID"
    return {
        "schema": PORTFOLIO_FILL_DECISION_AUDIT_SCHEMA,
        "status": status,
        "approved_fill_count": len(approved_fill_indexes),
        "verified_approved_fill_count": len(approved_fill_indexes) - len(invalid),
        "invalid_decisions": invalid,
        "accounting_contract_id": parameters.accounting_contract_id,
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "synchronous_open_position_one_minute_marks_required": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


__all__ = (
    "ACCOUNTING_SCHEMA",
    "PORTFOLIO_DECISION_AUDIT_SCHEMA",
    "PORTFOLIO_FILL_DECISION_AUDIT_SCHEMA",
    "HumanPaperAccountingParameters",
    "assess_human_paper_portfolio_fill",
    "audit_human_paper_portfolio_decisions",
    "audit_human_paper_portfolio_fill_decisions",
    "load_human_paper_accounting_parameters",
    "rebuild_human_paper_accounting",
)
