#!/usr/bin/env python3
"""Evaluate one exact-PIT strict strategy ETF_PROXY candidate without trading.

The request supplies the technical, tradeability, account, sector-risk and
symbol-risk snapshots.  This tool rebuilds selection, basket strength and the
CSI300 market risk at the same decision time, then calls the shared strict strategy
``evaluate_candidate`` core.  Optional ledger fields can be passed through to
produce ``chanlun-frozen-decision-fact-ledger`` for the replay builder.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from chanlun.decision_support.trading_system.etf_proxy_facts import (
    DailyMarketBar,
    EtfProxyCandidateDecisionFacts,
    EtfProxyPitRepository,
    EtfTrackingMapping,
    FrozenStructureBar,
    build_etf_proxy_candidate_decision,
)
from chanlun.decision_support.trading_system.multisymbol_replay import (
    strict_replay_contract,
)
from chanlun.decision_support.trading_system.replay_payload_builder import (
    FACT_LEDGER_SCHEMA,
)
from chanlun.decision_support.trading_system.selection import (
    AccountEntryGate,
    CandidateDecision,
    HigherTimeframeRiskSnapshot,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
)


CN = ZoneInfo("Asia/Shanghai")
REQUEST_SCHEMA = "chanlun-etf-proxy-candidate-request"
RESULT_SCHEMA = "chanlun-etf-proxy-candidate-evaluation"
DEFAULT_PIT_DATABASE = Path(
    ".cache/chanlun_external_pit/etf_proxy_pit.sqlite3"
)
DEFAULT_MARKET_DATABASE = Path(
    ".cache/chanlun_available_data/financial_data_query_bars.sqlite3"
)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _datetime(value: object, name: str) -> datetime:
    return datetime.fromisoformat(_string(value, name))


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must be an exact decimal string or integer")
    if not isinstance(value, (str, int)):
        raise TypeError(f"{name} must be an exact decimal string or integer")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _datetime(value, name)


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value


def _tracking_mapping(value: Mapping[str, object]) -> EtfTrackingMapping:
    return EtfTrackingMapping(
        symbol=_string(value.get("symbol"), "tracking_mapping.symbol"),
        tracked_index=_string(
            value.get("tracked_index"), "tracking_mapping.tracked_index"
        ),
        known_at=_datetime(value.get("known_at"), "tracking_mapping.known_at"),
        effective_from=_datetime(
            value.get("effective_from"), "tracking_mapping.effective_from"
        ),
        valid_until=_datetime(
            value.get("valid_until"), "tracking_mapping.valid_until"
        ),
        evidence_ids=tuple(
            _string(item, "tracking_mapping.evidence_ids[]")
            for item in _sequence(
                value.get("evidence_ids"), "tracking_mapping.evidence_ids"
            )
        ),
        authoritative=_strict_bool(
            value.get("authoritative"), "tracking_mapping.authoritative"
        ),
    )


def _risk(value: Mapping[str, object], name: str) -> HigherTimeframeRiskSnapshot:
    return HigherTimeframeRiskSnapshot(
        snapshot_id=_string(value.get("snapshot_id"), f"{name}.snapshot_id"),
        observed_at=_datetime(value.get("observed_at"), f"{name}.observed_at"),
        monthly=_string(value.get("monthly"), f"{name}.monthly"),  # type: ignore[arg-type]
        weekly=_string(value.get("weekly"), f"{name}.weekly"),  # type: ignore[arg-type]
        daily=_string(value.get("daily"), f"{name}.daily"),  # type: ignore[arg-type]
        monthly_ma5=_optional_decimal(
            value.get("monthly_ma5"), f"{name}.monthly_ma5"
        ),
        weekly_ma5=_optional_decimal(
            value.get("weekly_ma5"), f"{name}.weekly_ma5"
        ),
        daily_ma5=_optional_decimal(value.get("daily_ma5"), f"{name}.daily_ma5"),
        mapping_unique=_strict_bool(
            value.get("mapping_unique"), f"{name}.mapping_unique"
        ),
    )


def _tradeability(value: Mapping[str, object]) -> TradeabilitySnapshot:
    optional_int_fields = ("buy_quantity_increment", "sell_quantity_increment")
    parsed_optional_ints = {
        field: None if value.get(field) is None else int(value[field])
        for field in optional_int_fields
    }
    return TradeabilitySnapshot(
        symbol=_string(value.get("symbol"), "tradeability.symbol"),
        observed_at=_datetime(
            value.get("observed_at"), "tradeability.observed_at"
        ),
        listed=_strict_bool(value.get("listed"), "tradeability.listed"),
        st=_strict_bool(value.get("st"), "tradeability.st"),
        suspended=_strict_bool(
            value.get("suspended"), "tradeability.suspended"
        ),
        reliable_continuous_market_data=_strict_bool(
            value.get("reliable_continuous_market_data"),
            "tradeability.reliable_continuous_market_data",
        ),
        continuity_status=_string(
            value.get("continuity_status"), "tradeability.continuity_status"
        ),  # type: ignore[arg-type]
        structure_history_sufficient=_strict_bool(
            value.get("structure_history_sufficient"),
            "tradeability.structure_history_sufficient",
        ),
        price_tick=_optional_decimal(value.get("price_tick"), "tradeability.price_tick"),
        buy_quantity_increment=parsed_optional_ints["buy_quantity_increment"],
        sell_quantity_increment=parsed_optional_ints["sell_quantity_increment"],
        fee_schedule_id=(
            None
            if value.get("fee_schedule_id") is None
            else _string(value.get("fee_schedule_id"), "tradeability.fee_schedule_id")
        ),
        price_limits_known=_strict_bool(
            value.get("price_limits_known"), "tradeability.price_limits_known"
        ),
        trading_calendar_known=_strict_bool(
            value.get("trading_calendar_known"),
            "tradeability.trading_calendar_known",
        ),
        completed_daily_volume_sessions=int(
            value.get("completed_daily_volume_sessions", -1)
        ),
        completed_same_clock_l2_sessions=int(
            value.get("completed_same_clock_l2_sessions", -1)
        ),
        median_daily_raw_volume=_optional_decimal(
            value.get("median_daily_raw_volume"),
            "tradeability.median_daily_raw_volume",
        ),
        median_same_clock_l2_volume=_optional_decimal(
            value.get("median_same_clock_l2_volume"),
            "tradeability.median_same_clock_l2_volume",
        ),
        quote_coverage=_optional_decimal(
            value.get("quote_coverage"), "tradeability.quote_coverage"
        ),
        median_spread_ticks=_optional_decimal(
            value.get("median_spread_ticks"), "tradeability.median_spread_ticks"
        ),
        current_quote_valid_and_fresh=_strict_bool(
            value.get("current_quote_valid_and_fresh"),
            "tradeability.current_quote_valid_and_fresh",
        ),
        q_liquidity_cap=int(value.get("q_liquidity_cap", -1)),
    )


def _technical(value: Mapping[str, object]) -> TechnicalEntrySnapshot:
    return TechnicalEntrySnapshot(
        structure_snapshot_id=_string(
            value.get("structure_snapshot_id"), "technical.structure_snapshot_id"
        ),
        observed_at=_datetime(value.get("observed_at"), "technical.observed_at"),
        price_basis_revision=_string(
            value.get("price_basis_revision"), "technical.price_basis_revision"
        ),
        stroke_mode=_string(
            value.get("stroke_mode"), "technical.stroke_mode"
        ),
        l0_source_frequency=_string(
            value.get("l0_source_frequency"), "technical.l0_source_frequency"
        ),
        l1_source_frequency=_string(
            value.get("l1_source_frequency"), "technical.l1_source_frequency"
        ),
        l2_source_frequency=_string(
            value.get("l2_source_frequency"), "technical.l2_source_frequency"
        ),
        direct_recursive_levels_unique=_strict_bool(
            value.get("direct_recursive_levels_unique"),
            "technical.direct_recursive_levels_unique",
        ),
        all_components_completed=_strict_bool(
            value.get("all_components_completed"),
            "technical.all_components_completed",
        ),
        l0_center_id=(
            None
            if value.get("l0_center_id") is None
            else _string(value.get("l0_center_id"), "technical.l0_center_id")
        ),
        l0_center_ordinal=(
            None
            if value.get("l0_center_ordinal") is None
            else int(value["l0_center_ordinal"])
        ),
        l0_center_completed=_strict_bool(
            value.get("l0_center_completed"), "technical.l0_center_completed"
        ),
        l0_point_type=(
            None
            if value.get("l0_point_type") is None
            else _string(value.get("l0_point_type"), "technical.l0_point_type")
        ),
        l0_point_id=(
            None
            if value.get("l0_point_id") is None
            else _string(value.get("l0_point_id"), "technical.l0_point_id")
        ),
        l0_point_confirmation_time=_optional_datetime(
            value.get("l0_point_confirmation_time"),
            "technical.l0_point_confirmation_time",
        ),
        l1_departure_completed=_strict_bool(
            value.get("l1_departure_completed"),
            "technical.l1_departure_completed",
        ),
        l1_first_return_completed=_strict_bool(
            value.get("l1_first_return_completed"),
            "technical.l1_first_return_completed",
        ),
        first_return_low=_optional_decimal(
            value.get("first_return_low"), "technical.first_return_low"
        ),
        l0_zg=_optional_decimal(value.get("l0_zg"), "technical.l0_zg"),
        l2_locator=_string(value.get("l2_locator"), "technical.l2_locator"),  # type: ignore[arg-type]
        l2_point_id=(
            None
            if value.get("l2_point_id") is None
            else _string(value.get("l2_point_id"), "technical.l2_point_id")
        ),
        l2_confirmation_bar_high=_optional_decimal(
            value.get("l2_confirmation_bar_high"),
            "technical.l2_confirmation_bar_high",
        ),
        level_relation_mode=_string(
            value.get("level_relation_mode", "DIRECT_RECURSIVE"),
            "technical.level_relation_mode",
        ),  # type: ignore[arg-type]
        level_relation_contract_id=(
            None
            if value.get("level_relation_contract_id") is None
            else _string(
                value.get("level_relation_contract_id"),
                "technical.level_relation_contract_id",
            )
        ),
    )


def _account(value: Mapping[str, object]) -> AccountEntryGate:
    return AccountEntryGate(
        observed_at=_datetime(value.get("observed_at"), "account.observed_at"),
        operations_normal=_strict_bool(
            value.get("operations_normal"), "account.operations_normal"
        ),
        reconciliation_passed=_strict_bool(
            value.get("reconciliation_passed"), "account.reconciliation_passed"
        ),
        free_strategic_slot=_strict_bool(
            value.get("free_strategic_slot"), "account.free_strategic_slot"
        ),
        drawdown=_decimal(value.get("drawdown"), "account.drawdown"),
        no_active_symbol_order=_strict_bool(
            value.get("no_active_symbol_order"),
            "account.no_active_symbol_order",
        ),
    )


def _daily_bars(database: Path) -> tuple[DailyMarketBar, ...]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = tuple(
            connection.execute(
                """
                SELECT bar_time, open, high, low, close, volume
                FROM bars
                WHERE symbol='000300.CSI' AND period='P_Day1'
                  AND adj_type='S_Unsplit'
                ORDER BY bar_time
                """
            )
        )
    return tuple(
        DailyMarketBar(
            session=(session := date.fromisoformat(str(row[0])[:10])),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            known_at=datetime.combine(session, time(15), tzinfo=CN),
        )
        for row in rows
    )


def _trading_calendar(database: Path) -> tuple[date, ...]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return tuple(
            date.fromisoformat(str(row[0]))
            for row in connection.execute(
                """
                SELECT calendar_date FROM trading_calendar
                WHERE is_trading_day='1' ORDER BY calendar_date
                """
            )
        )


def _completed_30m_bars(path: Path | None) -> tuple[FrozenStructureBar, ...]:
    if path is None:
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("bars") if isinstance(payload, Mapping) else payload
    rows = _sequence(records, "benchmark_30m.bars")
    bars: list[FrozenStructureBar] = []
    for index, item in enumerate(rows):
        value = _mapping(item, f"benchmark_30m.bars[{index}]")
        bars.append(
            FrozenStructureBar(
                end_at=_datetime(value.get("end_at"), f"bars[{index}].end_at"),
                open=_decimal(value.get("open"), f"bars[{index}].open"),
                high=_decimal(value.get("high"), f"bars[{index}].high"),
                low=_decimal(value.get("low"), f"bars[{index}].low"),
                close=_decimal(value.get("close"), f"bars[{index}].close"),
                volume=_decimal(value.get("volume"), f"bars[{index}].volume"),
                completed=_strict_bool(
                    value.get("completed"), f"bars[{index}].completed"
                ),
            )
        )
    ends = tuple(value.end_at for value in bars)
    if ends != tuple(sorted(set(ends))):
        raise ValueError("benchmark 30m bars must be unique and chronological")
    return tuple(bars)


def _candidate_document(value: CandidateDecision | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "symbol": value.symbol,
        "parameter_set_id": value.parameter_set_id,
        "selection_path": value.selection_path,
        "accepted": value.accepted,
        "checks": tuple(
            {
                "gate": check.gate,
                "passed": check.passed,
                "code": check.code,
                "detail": check.detail,
            }
            for check in value.checks
        ),
        "fundamental_role": value.fundamental_role,
        "relative_value_status": value.relative_value_status,
        "sector_strength": (
            None if value.sector_strength is None else format(value.sector_strength, "f")
        ),
        "confirmation_time": (
            None
            if value.confirmation_time is None
            else value.confirmation_time.isoformat()
        ),
    }


def _blockers(values: object) -> tuple[dict[str, str], ...]:
    return tuple(
        {"field": value.field, "code": value.code, "detail": value.detail}
        for value in values
    )


def _evaluation_document(
    facts: EtfProxyCandidateDecisionFacts,
    *,
    decision_time: datetime,
    benchmark_30m_source: Path | None,
) -> dict[str, object]:
    selection = facts.selection
    basket = selection.basket
    market = facts.market_risk.snapshot
    return {
        "schema": RESULT_SCHEMA,
        "decision_time": decision_time.isoformat(),
        "grade": facts.grade,
        "full_system_eligible": facts.full_system_eligible,
        "live_status": "LIVE_DISABLED",
        "selection": {
            "grade": selection.grade,
            "snapshot_id": (
                None if selection.snapshot is None else selection.snapshot.snapshot_id
            ),
            "basket_mapping_id": None if basket is None else basket.mapping_id,
            "candidate_session": (
                None if basket is None else basket.candidate_session.isoformat()
            ),
            "member_count": None if basket is None else len(basket.members),
            "blockers": _blockers(selection.blockers),
        },
        "basket_strength": {
            "grade": facts.basket_strength.grade,
            "snapshot_id": facts.basket_strength.snapshot.snapshot_id,
            "resolved": facts.basket_strength.snapshot.resolved,
            "strength": (
                None
                if facts.basket_strength.snapshot.strength is None
                else format(facts.basket_strength.snapshot.strength, "f")
            ),
            "blockers": _blockers(facts.basket_strength.blockers),
        },
        "market_risk": {
            "snapshot_id": None if market is None else market.snapshot_id,
            "gate": facts.market_risk.gate,
            "states": tuple(
                {
                    "period": value.fact.period,
                    "state": value.fact.state,
                    "mapping_unique": value.fact.mapping_unique,
                    "mapped_center_id": value.fact.mapped_center_id,
                    "evidence_bar_end": (
                        None
                        if value.fact.evidence_bar_end is None
                        else value.fact.evidence_bar_end.isoformat()
                    ),
                    "source_revision": value.fact.source_revision,
                    "blockers": _blockers(value.blockers),
                }
                for value in facts.benchmark_structure.states
            ),
            "ma5": tuple(
                {
                    "period": period,
                    "value": None if average is None else format(average, "f"),
                }
                for period, average in facts.market_risk.ma5
            ),
            "completed_30m_prefix_count": (
                facts.benchmark_structure.completed_30m_prefix_count
            ),
            "completed_30m_source": (
                "NOT_SUPPLIED"
                if benchmark_30m_source is None
                else str(benchmark_30m_source.resolve())
            ),
            "blockers": _blockers(facts.market_risk.blockers),
        },
        "candidate": _candidate_document(facts.decision),
        "blockers": _blockers(facts.blockers),
    }


def _stable_content_sha256(payload: Mapping[str, object]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _fact_ledger(
    facts: EtfProxyCandidateDecisionFacts,
    request: Mapping[str, object],
    technical: TechnicalEntrySnapshot,
    sector_risk: HigherTimeframeRiskSnapshot,
    symbol_risk: HigherTimeframeRiskSnapshot,
) -> dict[str, object] | None:
    raw = request.get("fact_ledger")
    if raw is None:
        return None
    config = _mapping(raw, "fact_ledger")
    contract = strict_replay_contract()
    decision = facts.decision
    entry_facts: tuple[dict[str, object], ...] = ()
    if decision is not None and decision.accepted:
        if technical.l0_point_id is None or decision.confirmation_time is None:
            raise ValueError("accepted candidate requires L0 point and confirmation time")
        if facts.candidate_snapshot is None or facts.selection.snapshot is None:
            raise ValueError("accepted candidate is missing its immutable fact snapshots")
        if decision.confirmation_time > facts.candidate_snapshot.decision_time:
            raise ValueError("candidate confirmation cannot follow decision time")
        q_plan = int(config.get("q_plan", 0))
        if q_plan <= 0:
            raise ValueError("accepted candidate fact ledger requires positive q_plan")
        health = dict(_mapping(config.get("health"), "fact_ledger.health"))
        selection_ids = tuple(
            str(item)
            for item in config.get(
                "selection_fact_ids", (facts.selection.snapshot.snapshot_id,)
            )
        )
        market_snapshot = facts.market_risk.snapshot
        if market_snapshot is None:
            raise ValueError("accepted candidate requires market risk snapshot")
        risk_ids = tuple(
            str(item)
            for item in config.get(
                "risk_fact_ids",
                (
                    market_snapshot.snapshot_id,
                    sector_risk.snapshot_id,
                    symbol_risk.snapshot_id,
                ),
            )
        )
        frozen_ids = tuple(
            str(item)
            for item in config.get(
                "frozen_structure_fact_ids",
                tuple(
                    item
                    for item in (
                        technical.structure_snapshot_id,
                        technical.l0_center_id,
                        technical.l0_point_id,
                        technical.l2_point_id,
                    )
                    if item is not None
                ),
            )
        )
        entry_facts = (
            {
                "symbol": decision.symbol,
                "l0_point_id": technical.l0_point_id,
                "decision_time": facts.candidate_snapshot.decision_time.isoformat(),
                "structure_snapshot_id": technical.structure_snapshot_id,
                "selection_snapshot_id": facts.selection.snapshot.snapshot_id,
                "account_snapshot_id": _string(
                    config.get("account_snapshot_id"),
                    "fact_ledger.account_snapshot_id",
                ),
                "candidate": _candidate_document(decision),
                "q_plan": q_plan,
                "selection_fact_ids": selection_ids,
                "risk_fact_ids": risk_ids,
                "frozen_structure_fact_ids": frozen_ids,
                "health": health,
            },
        )
    payload: dict[str, object] = {
        "schema": FACT_LEDGER_SCHEMA,
        "generated_at": datetime.now(CN).isoformat(),
        "strategy_parameter_set_id": contract.strategy_parameter_set_id,
        "timeframe_override_parameter_set_id": (
            contract.timeframe_override_parameter_set_id
        ),
        "alignment_contract_id": contract.effective_alignment_contract_id,
        "alignment_parameter_set_id": contract.effective_alignment_parameter_set_id,
        "fee_model": dict(_mapping(config.get("fee_model"), "fact_ledger.fee_model")),
        "execution_policy": dict(
            _mapping(config.get("execution_policy"), "fact_ledger.execution_policy")
        ),
        "entry_facts": entry_facts,
        "structure_coverage": tuple(
            _mapping(item, "fact_ledger.structure_coverage[]")
            for item in _sequence(
                config.get("structure_coverage", ()),
                "fact_ledger.structure_coverage",
            )
        ),
        "structure_signal_facts": tuple(
            _mapping(item, "fact_ledger.structure_signal_facts[]")
            for item in _sequence(
                config.get("structure_signal_facts", ()),
                "fact_ledger.structure_signal_facts",
            )
        ),
        "execution_status_facts": tuple(
            _mapping(item, "fact_ledger.execution_status_facts[]")
            for item in _sequence(
                config.get("execution_status_facts", ()),
                "fact_ledger.execution_status_facts",
            )
        ),
        "candidate_audits": (
            {
                "symbol": (
                    facts.selection.snapshot.symbol
                    if facts.selection.snapshot is not None
                    else "UNRESOLVED"
                ),
                "selection_grade": facts.selection.grade,
                "fact_grade": facts.grade,
                "candidate": _candidate_document(decision),
                "blockers": _blockers(facts.blockers),
            },
        ),
        "live_status": "LIVE_DISABLED",
    }
    payload["content_sha256"] = _stable_content_sha256(payload)
    return payload


def evaluate_request(
    request: Mapping[str, object],
    *,
    pit_database: Path,
    market_database: Path,
    benchmark_30m_path: Path | None,
) -> dict[str, object]:
    if request.get("schema") != REQUEST_SCHEMA:
        raise ValueError("ETF proxy candidate request schema is unsupported")
    decision_time = _datetime(request.get("decision_time"), "decision_time")
    mapping = _tracking_mapping(
        _mapping(request.get("tracking_mapping"), "tracking_mapping")
    )
    tradeability = _tradeability(
        _mapping(request.get("tradeability"), "tradeability")
    )
    technical = _technical(_mapping(request.get("technical"), "technical"))
    account = _account(_mapping(request.get("account"), "account"))
    sector_risk = _risk(_mapping(request.get("sector_risk"), "sector_risk"), "sector_risk")
    symbol_risk = _risk(_mapping(request.get("symbol_risk"), "symbol_risk"), "symbol_risk")
    daily = _daily_bars(market_database)
    calendar = _trading_calendar(pit_database)
    if not daily or not calendar:
        raise ValueError("benchmark daily bars and trading calendar are required")
    benchmark_30m = _completed_30m_bars(benchmark_30m_path)
    facts = build_etf_proxy_candidate_decision(
        EtfProxyPitRepository(pit_database),
        mapping,
        decision_time=decision_time,
        benchmark_daily_bars=daily,
        benchmark_completed_30m_bars=benchmark_30m,
        trading_sessions=calendar,
        calendar_coverage_end=calendar[-1],
        tradeability=tradeability,
        sector_risk=sector_risk,
        symbol_risk=symbol_risk,
        technical=technical,
        account=account,
        reviewer=_string(request.get("reviewer"), "reviewer"),
        signature=_string(request.get("signature"), "signature"),
    )
    evaluation = _evaluation_document(
        facts,
        decision_time=decision_time,
        benchmark_30m_source=benchmark_30m_path,
    )
    ledger = _fact_ledger(facts, request, technical, sector_risk, symbol_risk)
    payload: dict[str, object] = {
        "schema": "chanlun-etf-proxy-candidate-cli-output",
        "generated_at": datetime.now(CN).isoformat(),
        "evaluation": evaluation,
        "fact_ledger": ledger,
        "live_status": "LIVE_DISABLED",
    }
    payload["content_sha256"] = _stable_content_sha256(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--pit-database", type=Path, default=DEFAULT_PIT_DATABASE)
    parser.add_argument(
        "--market-database", type=Path, default=DEFAULT_MARKET_DATABASE
    )
    parser.add_argument("--benchmark-30m-json", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    request = _mapping(
        json.loads(arguments.request.read_text(encoding="utf-8")), "request"
    )
    payload = evaluate_request(
        request,
        pit_database=arguments.pit_database,
        market_database=arguments.market_database,
        benchmark_30m_path=arguments.benchmark_30m_json,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if arguments.output is None:
        print(encoded, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = arguments.output.with_suffix(arguments.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        temporary.replace(arguments.output)
        print(str(arguments.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
