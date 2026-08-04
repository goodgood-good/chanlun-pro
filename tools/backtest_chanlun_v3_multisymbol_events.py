#!/usr/bin/env python3
"""Replay a strict V3 multi-symbol point-in-time event artifact.

The input contains already-frozen selection, risk, structure/alignment and
execution facts.  This adapter only deserializes those facts; it neither
derives nor repairs a trading signal.  The shared V3 decision core and shared
completed-minute matcher remain the only decision and fill implementations.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.v3_bar_execution import (
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.v3_decision import (
    StrategicSignalFacts,
    SystemHealthFacts,
    TacticalSignalFacts,
    V3DecisionInput,
)
from chanlun.decision_support.trading_system.v3_execution import (
    V3FeeModel,
    V3FeeRateAt,
)
from chanlun.decision_support.trading_system.v3_multisymbol_replay import (
    ReplayBatch,
    ReplayCashDistributionFact,
    ReplayDecisionEvent,
    ReplayFactBindings,
    ReplayPriceFact,
    StrictV3MultiSymbolReplayEngine,
)
from chanlun.decision_support.trading_system.v3_selection import (
    CandidateDecision,
    GateCheck,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    V31AlignedEntryChain,
    V31AlignmentContract,
)
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    independent_timeframe_override,
)


INPUT_SCHEMA = "chanlun-strict-v3-multisymbol-replay-input/v1"
OUTPUT_SCHEMA = "chanlun-strict-v3-multisymbol-replay-result/v1"


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TypeError(f"{field_name} must be a JSON object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{field_name} must be a JSON array")
    return value


def _required(value: Mapping[str, object], field_name: str) -> object:
    if field_name not in value:
        raise KeyError(f"required artifact field is missing: {field_name}")
    return value[field_name]


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    return None if value is None else _string(value, field_name)


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must use an exact decimal string or integer")
    if not isinstance(value, (str, int)):
        raise TypeError(f"{field_name} must use an exact decimal string or integer")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    return None if value is None else _decimal(value, field_name)


def _datetime(value: object, field_name: str) -> datetime:
    text = _string(value, field_name)
    result = datetime.fromisoformat(text)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field_name} must include an exchange timezone offset")
    return result


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    return None if value is None else _datetime(value, field_name)


def _date(value: object, field_name: str) -> date:
    return date.fromisoformat(_string(value, field_name))


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{field_name}[]")
        for item in _sequence(value, field_name)
    )


def _parse_fee_model(value: object) -> V3FeeModel:
    row = _mapping(value, "fee_model")
    rates = tuple(
        V3FeeRateAt(
            effective_from=_date(
                _required(rate, "effective_from"),
                "fee_model.rates[].effective_from",
            ),
            commission_rate=_decimal(
                _required(rate, "commission_rate"),
                "fee_model.rates[].commission_rate",
            ),
            minimum_commission=_decimal(
                _required(rate, "minimum_commission"),
                "fee_model.rates[].minimum_commission",
            ),
            stock_sell_stamp_rate=_decimal(
                _required(rate, "stock_sell_stamp_rate"),
                "fee_model.rates[].stock_sell_stamp_rate",
            ),
            transfer_rate=_decimal(
                _required(rate, "transfer_rate"),
                "fee_model.rates[].transfer_rate",
            ),
            other_buy_rate=_decimal(
                rate.get("other_buy_rate", "0"),
                "fee_model.rates[].other_buy_rate",
            ),
            other_sell_rate=_decimal(
                rate.get("other_sell_rate", "0"),
                "fee_model.rates[].other_sell_rate",
            ),
        )
        for item in _sequence(_required(row, "rates"), "fee_model.rates")
        for rate in (_mapping(item, "fee_model.rates[]"),)
    )
    return V3FeeModel(
        schedule_id=_string(_required(row, "schedule_id"), "fee_model.schedule_id"),
        rates=rates,
        currency_quantum=_decimal(
            row.get("currency_quantum", "0.01"),
            "fee_model.currency_quantum",
        ),
    )


def _parse_candidate(value: object) -> CandidateDecision | None:
    if value is None:
        return None
    row = _mapping(value, "facts.candidate")
    checks = tuple(
        GateCheck(
            gate=_string(_required(check, "gate"), "candidate.checks[].gate"),
            passed=_boolean(
                _required(check, "passed"),
                "candidate.checks[].passed",
            ),
            code=_string(_required(check, "code"), "candidate.checks[].code"),
            detail=_string(
                _required(check, "detail"),
                "candidate.checks[].detail",
            ),
        )
        for item in _sequence(row.get("checks", ()), "candidate.checks")
        for check in (_mapping(item, "candidate.checks[]"),)
    )
    return CandidateDecision(
        symbol=_string(_required(row, "symbol"), "candidate.symbol"),
        parameter_set_id=_string(
            _required(row, "parameter_set_id"),
            "candidate.parameter_set_id",
        ),
        selection_path=_string(  # type: ignore[arg-type]
            _required(row, "selection_path"),
            "candidate.selection_path",
        ),
        accepted=_boolean(_required(row, "accepted"), "candidate.accepted"),
        checks=checks,
        fundamental_role=_string(  # type: ignore[arg-type]
            _required(row, "fundamental_role"),
            "candidate.fundamental_role",
        ),
        relative_value_status=_string(  # type: ignore[arg-type]
            _required(row, "relative_value_status"),
            "candidate.relative_value_status",
        ),
        sector_strength=_optional_decimal(
            row.get("sector_strength"),
            "candidate.sector_strength",
        ),
        confirmation_time=_optional_datetime(
            row.get("confirmation_time"),
            "candidate.confirmation_time",
        ),
        higher_timeframe_risk_buyable=(
            None
            if "higher_timeframe_risk_buyable" not in row
            or row.get("higher_timeframe_risk_buyable") is None
            else _boolean(
                row.get("higher_timeframe_risk_buyable"),
                "candidate.higher_timeframe_risk_buyable",
            )
        ),
    )


def _parse_chain(value: object) -> V31AlignedEntryChain | None:
    if value is None:
        return None
    row = _mapping(value, "bindings.aligned_entry_chain")
    return V31AlignedEntryChain(
        l0_point_id=_string(_required(row, "l0_point_id"), "chain.l0_point_id"),
        l0_center_id=_string(_required(row, "l0_center_id"), "chain.l0_center_id"),
        l1_departure_evidence_id=_string(
            _required(row, "l1_departure_evidence_id"),
            "chain.l1_departure_evidence_id",
        ),
        l1_return_evidence_id=_string(
            _required(row, "l1_return_evidence_id"),
            "chain.l1_return_evidence_id",
        ),
        l1_evidence_kind=_string(  # type: ignore[arg-type]
            _required(row, "l1_evidence_kind"),
            "chain.l1_evidence_kind",
        ),
        l2_locator_point_id=_string(
            _required(row, "l2_locator_point_id"),
            "chain.l2_locator_point_id",
        ),
        decision_at=_datetime(_required(row, "decision_at"), "chain.decision_at"),
        return_low=_decimal(_required(row, "return_low"), "chain.return_low"),
        l0_zg=_decimal(_required(row, "l0_zg"), "chain.l0_zg"),
        l2_confirmation_bar_high=_decimal(
            _required(row, "l2_confirmation_bar_high"),
            "chain.l2_confirmation_bar_high",
        ),
        structural_invalidation_price=_decimal(
            _required(row, "structural_invalidation_price"),
            "chain.structural_invalidation_price",
        ),
    )


def _parse_bindings(value: object) -> ReplayFactBindings:
    row = _mapping(value, "bindings")
    return ReplayFactBindings(
        timeframe_override_parameter_set_id=_optional_string(
            row.get("timeframe_override_parameter_set_id"),
            "bindings.timeframe_override_parameter_set_id",
        ),
        alignment_contract_id=_optional_string(
            row.get("alignment_contract_id"),
            "bindings.alignment_contract_id",
        ),
        alignment_parameter_set_id=_optional_string(
            row.get("alignment_parameter_set_id"),
            "bindings.alignment_parameter_set_id",
        ),
        frozen_structure_fact_ids=_strings(
            row.get("frozen_structure_fact_ids", ()),
            "bindings.frozen_structure_fact_ids",
        ),
        selection_fact_ids=_strings(
            row.get("selection_fact_ids", ()),
            "bindings.selection_fact_ids",
        ),
        risk_fact_ids=_strings(
            row.get("risk_fact_ids", ()),
            "bindings.risk_fact_ids",
        ),
        aligned_entry_chain=_parse_chain(row.get("aligned_entry_chain")),
        all_required_facts_resolved=_boolean(
            row.get("all_required_facts_resolved", True),
            "bindings.all_required_facts_resolved",
        ),
        unresolved_reason_codes=_strings(
            row.get("unresolved_reason_codes", ()),
            "bindings.unresolved_reason_codes",
        ),
    )


def _parse_facts(value: object) -> V3DecisionInput:
    row = _mapping(value, "facts")
    health = _mapping(_required(row, "health"), "facts.health")
    strategic = _mapping(_required(row, "strategic"), "facts.strategic")
    tactical = _mapping(_required(row, "tactical"), "facts.tactical")
    return V3DecisionInput(
        symbol=_string(_required(row, "symbol"), "facts.symbol"),
        decision_time=_datetime(
            _required(row, "decision_time"),
            "facts.decision_time",
        ),
        confirmation_time=_datetime(
            _required(row, "confirmation_time"),
            "facts.confirmation_time",
        ),
        structure_snapshot_id=_string(
            _required(row, "structure_snapshot_id"),
            "facts.structure_snapshot_id",
        ),
        selection_snapshot_id=_optional_string(
            row.get("selection_snapshot_id"),
            "facts.selection_snapshot_id",
        ),
        account_snapshot_id=_string(
            _required(row, "account_snapshot_id"),
            "facts.account_snapshot_id",
        ),
        strategic_state=_string(  # type: ignore[arg-type]
            _required(row, "strategic_state"),
            "facts.strategic_state",
        ),
        health=SystemHealthFacts(
            data_complete=_boolean(
                _required(health, "data_complete"),
                "facts.health.data_complete",
            ),
            broker_healthy=_boolean(
                _required(health, "broker_healthy"),
                "facts.health.broker_healthy",
            ),
            reconciliation_passed=_boolean(
                _required(health, "reconciliation_passed"),
                "facts.health.reconciliation_passed",
            ),
            timestamps_monotonic=_boolean(
                _required(health, "timestamps_monotonic"),
                "facts.health.timestamps_monotonic",
            ),
            account_transfer_registered=_boolean(
                _required(health, "account_transfer_registered"),
                "facts.health.account_transfer_registered",
            ),
        ),
        strategic=StrategicSignalFacts(**strategic),  # type: ignore[arg-type]
        tactical=TacticalSignalFacts(**tactical),  # type: ignore[arg-type]
        cycle_ledger=None,
        candidate=_parse_candidate(row.get("candidate")),
        q_plan=_integer(_required(row, "q_plan"), "facts.q_plan"),
        price_cap_or_floor=_optional_decimal(
            row.get("price_cap_or_floor"),
            "facts.price_cap_or_floor",
        ),
        active_order_id=_optional_string(
            row.get("active_order_id"),
            "facts.active_order_id",
        ),
        all_structure_inputs_completed=_boolean(
            row.get("all_structure_inputs_completed", True),
            "facts.all_structure_inputs_completed",
        ),
    )


def _parse_status(value: object) -> BarProxyExecutionStatus:
    row = _mapping(value, "execution_status")
    return BarProxyExecutionStatus(
        known_at=_datetime(_required(row, "known_at"), "status.known_at"),
        effective_session=_date(
            _required(row, "effective_session"),
            "status.effective_session",
        ),
        listed=_boolean(_required(row, "listed"), "status.listed"),
        suspended=_boolean(_required(row, "suspended"), "status.suspended"),
        continuity_active=_boolean(
            _required(row, "continuity_active"),
            "status.continuity_active",
        ),
        point_in_time_state_complete=_boolean(
            _required(row, "point_in_time_state_complete"),
            "status.point_in_time_state_complete",
        ),
        corporate_action_state_complete=_boolean(
            _required(row, "corporate_action_state_complete"),
            "status.corporate_action_state_complete",
        ),
        sellable_quantity=_integer(
            _required(row, "sellable_quantity"),
            "status.sellable_quantity",
        ),
        limit_up=_decimal(_required(row, "limit_up"), "status.limit_up"),
        limit_down=_decimal(_required(row, "limit_down"), "status.limit_down"),
        buy_quantity_increment=_integer(
            _required(row, "buy_quantity_increment"),
            "status.buy_quantity_increment",
        ),
        sell_quantity_increment=_integer(
            _required(row, "sell_quantity_increment"),
            "status.sell_quantity_increment",
        ),
        fee_schedule_id=_optional_string(
            row.get("fee_schedule_id"),
            "status.fee_schedule_id",
        ),
    )


def _parse_bar(value: object) -> HistoricalMinuteExecutionBar:
    row = _mapping(value, "bars[]")
    return HistoricalMinuteExecutionBar(
        symbol=_string(_required(row, "symbol"), "bars[].symbol"),
        opened_at=_datetime(_required(row, "opened_at"), "bars[].opened_at"),
        closed_at=_datetime(_required(row, "closed_at"), "bars[].closed_at"),
        sequence=_integer(_required(row, "sequence"), "bars[].sequence"),
        raw_open=_decimal(_required(row, "raw_open"), "bars[].raw_open"),
        raw_high=_decimal(_required(row, "raw_high"), "bars[].raw_high"),
        raw_low=_decimal(_required(row, "raw_low"), "bars[].raw_low"),
        raw_close=_decimal(_required(row, "raw_close"), "bars[].raw_close"),
        raw_volume=_decimal(_required(row, "raw_volume"), "bars[].raw_volume"),
        source_id=_string(_required(row, "source_id"), "bars[].source_id"),
        complete=_boolean(row.get("complete", True), "bars[].complete"),
        phase=_string(row.get("phase", "CONTINUOUS"), "bars[].phase"),  # type: ignore[arg-type]
    )


def _parse_mark(value: object) -> ReplayPriceFact:
    row = _mapping(value, "marks[]")
    return ReplayPriceFact(
        symbol=_string(_required(row, "symbol"), "marks[].symbol"),
        available_at=_datetime(
            _required(row, "available_at"),
            "marks[].available_at",
        ),
        raw_close=_decimal(_required(row, "raw_close"), "marks[].raw_close"),
        source_id=_string(_required(row, "source_id"), "marks[].source_id"),
        complete=_boolean(row.get("complete", True), "marks[].complete"),
        price_basis=_string(
            row.get("price_basis", "RAW_UNADJUSTED"),
            "marks[].price_basis",
        ),
    )


def _parse_cash_distribution(value: object) -> ReplayCashDistributionFact:
    row = _mapping(value, "cash_distributions[]")
    return ReplayCashDistributionFact(
        action_id=_string(
            _required(row, "action_id"),
            "cash_distributions[].action_id",
        ),
        symbol=_string(
            _required(row, "symbol"),
            "cash_distributions[].symbol",
        ),
        effective_at=_datetime(
            _required(row, "effective_at"),
            "cash_distributions[].effective_at",
        ),
        known_at=_datetime(
            _required(row, "known_at"),
            "cash_distributions[].known_at",
        ),
        cash_per_share=_decimal(
            _required(row, "cash_per_share"),
            "cash_distributions[].cash_per_share",
        ),
        source_id=_string(
            _required(row, "source_id"),
            "cash_distributions[].source_id",
        ),
        source_ledger_sha256=_string(
            _required(row, "source_ledger_sha256"),
            "cash_distributions[].source_ledger_sha256",
        ),
        point_in_time_complete=_boolean(
            row.get("point_in_time_complete", True),
            "cash_distributions[].point_in_time_complete",
        ),
    )


def _parse_event(value: object) -> ReplayDecisionEvent:
    row = _mapping(value, "events[]")
    broker_position_value = row.get("broker_position_quantity")
    return ReplayDecisionEvent(
        event_id=_string(_required(row, "event_id"), "events[].event_id"),
        facts=_parse_facts(_required(row, "facts")),
        bindings=_parse_bindings(_required(row, "bindings")),
        created_at=_datetime(_required(row, "created_at"), "events[].created_at"),
        broker_confirmed_at=_datetime(
            _required(row, "broker_confirmed_at"),
            "events[].broker_confirmed_at",
        ),
        expires_at=_optional_datetime(
            row.get("expires_at"),
            "events[].expires_at",
        ),
        execution_status=_parse_status(_required(row, "execution_status")),
        broker_position_quantity=(
            None
            if broker_position_value is None
            else _integer(
                broker_position_value,
                "events[].broker_position_quantity",
            )
        ),
        bars=tuple(
            _parse_bar(item)
            for item in _sequence(row.get("bars", ()), "events[].bars")
        ),
        account_position_source=_string(  # type: ignore[arg-type]
            row.get("account_position_source", "EXTERNAL_SNAPSHOT"),
            "events[].account_position_source",
        ),
        sellable_quantity_source=_string(  # type: ignore[arg-type]
            row.get("sellable_quantity_source", "EXTERNAL_SNAPSHOT"),
            "events[].sellable_quantity_source",
        ),
    )


def _parse_batch(value: object) -> ReplayBatch:
    row = _mapping(value, "batches[]")
    return ReplayBatch(
        batch_id=_string(_required(row, "batch_id"), "batches[].batch_id"),
        decision_at=_datetime(
            _required(row, "decision_at"),
            "batches[].decision_at",
        ),
        valuation_at=_datetime(
            _required(row, "valuation_at"),
            "batches[].valuation_at",
        ),
        events=tuple(
            _parse_event(item)
            for item in _sequence(_required(row, "events"), "batches[].events")
        ),
        decision_marks=tuple(
            _parse_mark(item)
            for item in _sequence(
                row.get("decision_marks", ()),
                "batches[].decision_marks",
            )
        ),
        valuation_marks=tuple(
            _parse_mark(item)
            for item in _sequence(
                row.get("valuation_marks", ()),
                "batches[].valuation_marks",
            )
        ),
        cash_distributions=tuple(
            _parse_cash_distribution(item)
            for item in _sequence(
                row.get("cash_distributions", ()),
                "batches[].cash_distributions",
            )
        ),
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"result contains unsupported value: {type(value).__name__}")


def _validate_builder_contract(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Reject a payload whose builder identity is not this frozen contract.

    Without this gate ``builder_contract`` is decorative provenance: a payload
    produced by a different alignment or override version would still be
    replayed, and only the deeper per-event fact gates would (maybe) catch it.
    """

    contract = _mapping(_required(payload, "builder_contract"), "builder_contract")
    override = independent_timeframe_override()
    expected = {
        "alignment_contract_id": V31AlignmentContract().contract_id,
        "alignment_parameter_set_id": V31AlignmentContract().parameter_set_id,
        "timeframe_override_parameter_set_id": override.parameter_set_id,
        "live_status": "LIVE_DISABLED",
    }
    for key, want in expected.items():
        got = contract.get(key)
        if got != want:
            raise ValueError(
                f"builder_contract.{key} must be {want!r}, got {got!r}"
            )
    for key in ("strategy_parameter_set_id", "execution_parameter_set_id"):
        if not isinstance(contract.get(key), str) or not str(
            contract.get(key)
        ).startswith("sha256:"):
            raise ValueError(f"builder_contract.{key} must be a sha256 identity")
    return contract


def run_payload(
    payload: Mapping[str, object],
    *,
    input_sha256: str = "UNRESOLVED_IN_MEMORY_INPUT",
) -> dict[str, object]:
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"input schema must be exactly {INPUT_SCHEMA}")
    builder_contract = _validate_builder_contract(payload)
    engine = StrictV3MultiSymbolReplayEngine(
        initial_cash=_decimal(_required(payload, "initial_cash"), "initial_cash"),
        started_at=_datetime(_required(payload, "started_at"), "started_at"),
        fee_model=_parse_fee_model(_required(payload, "fee_model")),
    )
    batches = tuple(
        _parse_batch(item)
        for item in _sequence(_required(payload, "batches"), "batches")
    )
    result = engine.replay(batches)
    report: dict[str, object] = {
        "schema": OUTPUT_SCHEMA,
        "input_schema": INPUT_SCHEMA,
        "input_sha256": input_sha256,
        "contract_parameter_set_id": result.contract.parameter_set_id,
        "builder_contract": dict(builder_contract),
        "result": _jsonable(result),
    }
    canonical = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    report["report_sha256"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    payload = json.loads(raw.decode("utf-8"), parse_float=str)
    report = run_payload(
        _mapping(payload, "root"),
        input_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
