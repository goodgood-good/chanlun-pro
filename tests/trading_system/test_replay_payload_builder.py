from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.bar_execution import (
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.multisymbol_replay import (
    ETF_REQUIRED_CANDIDATE_GATES,
    strict_replay_contract,
)
from chanlun.decision_support.trading_system.parameters import (
    etf_parameter_snapshot,
)
from chanlun.decision_support.trading_system.replay_payload_builder import (
    CORPORATE_ACTION_SCHEMA,
    FACT_LEDGER_SCHEMA,
    PRESCREEN_SCHEMA,
    build_replay_payload,
)
from chanlun.decision_support.trading_system.structure_signal_adapter import (
    REQUIRED_STRUCTURE_RULES,
)
from chanlun.decision_support.trading_system.timeframe_alignment import (
    alignment_contract,
)
from tools.backtest_multisymbol_events import run_payload


CN = ZoneInfo("Asia/Shanghai")
D0 = date(2026, 7, 20)
SYMBOL = "SH.510300"
STRUCTURE_LEDGER_HASH = "sha256:" + "b" * 64


def at(session: date, hour: int, minute: int) -> datetime:
    return datetime(
        session.year,
        session.month,
        session.day,
        hour,
        minute,
        tzinfo=CN,
    )


def bar(
    opened_at: datetime,
    sequence: int,
    *,
    low: str,
    high: str,
    volume: str,
) -> HistoricalMinuteExecutionBar:
    low_value = Decimal(low)
    high_value = Decimal(high)
    middle = (low_value + high_value) / 2
    return HistoricalMinuteExecutionBar(
        symbol=SYMBOL,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        sequence=sequence,
        raw_open=middle,
        raw_high=high_value,
        raw_low=low_value,
        raw_close=middle,
        raw_volume=Decimal(volume),
        source_id=f"raw:{opened_at.isoformat()}:{sequence}",
    )


def chain_document(decision: datetime) -> dict[str, object]:
    return {
        "l0_point_id": "l0:entry",
        "l0_center_id": "l0:center",
        "l1_departure_evidence_id": "l1:departure",
        "l1_return_evidence_id": "l1:return",
        "l1_evidence_kind": "COMPLETED_CONSTITUENT_UNIT",
        "l2_locator_point_id": "l2:locator",
        "decision_at": decision.isoformat(),
        "return_low": "9",
        "l0_zg": "8",
        "l2_confirmation_bar_high": "10",
        "structural_invalidation_price": "8",
    }


def stable_hash(
    payload: dict[str, object],
    *,
    excluded: frozenset[str],
) -> str:
    stable = {key: value for key, value in payload.items() if key not in excluded}
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def prescreen(*, chains: tuple[dict[str, object], ...]) -> dict[str, object]:
    contract = alignment_contract()
    payload: dict[str, object] = {
        "schema": PRESCREEN_SCHEMA,
        "mapping": {"L0": "30m", "L1": "5m", "L2": "1m"},
        "alignment_contract": contract.document(),
        "alignment_parameter_set_id": contract.parameter_set_id,
        "frozen_core_modified": False,
        "symbol_reports": (
            {
                "project_code": SYMBOL,
                "provider_symbol": "510300.SH",
                "source_start": D0.isoformat(),
                "source_end": (D0 + timedelta(days=1)).isoformat(),
                "structurally_legal_chain_count": len(chains),
                "aligned_entry_chains": chains,
                "adjustment_gate": {"formal_chain_eligibility": True},
            },
        ),
    }
    payload["content_sha256"] = stable_hash(
        payload,
        excluded=frozenset({"content_sha256"}),
    )
    return payload


def health() -> dict[str, object]:
    return {
        "data_complete": True,
        "broker_healthy": True,
        "reconciliation_passed": True,
        "timestamps_monotonic": True,
        "account_transfer_registered": True,
    }


def candidate(decision: datetime) -> dict[str, object]:
    return {
        "symbol": SYMBOL,
        "parameter_set_id": etf_parameter_snapshot().parameter_set_id,
        "selection_path": "ETF_PROXY",
        "accepted": True,
        "checks": tuple(
            {
                "gate": gate,
                "passed": True,
                "code": f"PASS_TEST_{gate.upper()}",
                "detail": "frozen synthetic fact",
            }
            for gate in sorted(ETF_REQUIRED_CANDIDATE_GATES)
        ),
        "fundamental_role": "ETF_PROXY",
        "relative_value_status": "ETF_PROXY",
        "sector_strength": "9",
        "confirmation_time": decision.isoformat(),
        "higher_timeframe_risk_buyable": True,
    }


def status(session: date, known_at: datetime) -> dict[str, object]:
    return {
        "symbol": SYMBOL,
        "known_at": known_at.isoformat(),
        "effective_session": session.isoformat(),
        "listed": True,
        "suspended": False,
        "continuity_active": True,
        "point_in_time_state_complete": True,
        "corporate_action_state_complete": True,
        "sellable_quantity": 0,
        "limit_up": "15",
        "limit_down": "5",
        "buy_quantity_increment": 100,
        "sell_quantity_increment": 100,
        "fee_schedule_id": "fees:test",
    }


def fact_ledger(entry_at: datetime, exit_at: datetime) -> dict[str, object]:
    contract = strict_replay_contract()
    payload: dict[str, object] = {
        "schema": FACT_LEDGER_SCHEMA,
        "strategy_parameter_set_id": contract.strategy_parameter_set_id,
        "timeframe_override_parameter_set_id": (
            contract.timeframe_override_parameter_set_id
        ),
        "alignment_contract_id": contract.effective_alignment_contract_id,
        "alignment_parameter_set_id": (contract.effective_alignment_parameter_set_id),
        "fee_model": {
            "schedule_id": "fees:test",
            "currency_quantum": "0.01",
            "rates": (
                {
                    "effective_from": "2020-01-01",
                    "commission_rate": "0.0003",
                    "minimum_commission": "5",
                    "stock_sell_stamp_rate": "0.001",
                    "transfer_rate": "0",
                    "other_buy_rate": "0",
                    "other_sell_rate": "0",
                },
            ),
        },
        "execution_policy": {
            "broker_latency_seconds": 0,
            "optional_ttl_l2_completed_bars": 1,
        },
        "entry_facts": (
            {
                "symbol": SYMBOL,
                "l0_point_id": "l0:entry",
                "decision_time": entry_at.isoformat(),
                "structure_snapshot_id": "structure:entry",
                "selection_snapshot_id": "selection:entry",
                "account_snapshot_id": "account:entry",
                "candidate": candidate(entry_at),
                "q_plan": 400,
                "selection_fact_ids": ("selection:entry",),
                "risk_fact_ids": ("risk:market", "risk:symbol"),
                "frozen_structure_fact_ids": (),
                "health": health(),
            },
        ),
        "structure_coverage": (
            {
                "symbol": SYMBOL,
                "start_at": entry_at.isoformat(),
                "end_at": at(D0 + timedelta(days=1), 15, 0).isoformat(),
                "frequencies": ("30m", "5m", "1m"),
                "recursive_level": 0,
                "complete": True,
                "source_ledger_sha256": STRUCTURE_LEDGER_HASH,
                "missing_data_was_inferred": False,
                "rule_coverage": {
                    rule: "COMPLETE" for rule in REQUIRED_STRUCTURE_RULES
                },
            },
        ),
        "structure_signal_facts": (
            {
                "event_id": "exit:l0-third-sell",
                "symbol": SYMBOL,
                "decision_time": exit_at.isoformat(),
                "confirmation_time": exit_at.isoformat(),
                "structure_snapshot_id": "structure:exit",
                "account_snapshot_id": "account:exit",
                "completed": True,
                "recursive_level": 0,
                "source_frequencies": ("30m",),
                "strategic": {"l0_third_sell": True},
                "tactical": {},
                "health": health(),
                "price_cap_or_floor": "9",
                "frozen_structure_fact_ids": ("signal:l0-third-sell",),
                "risk_fact_ids": (),
                "execution_persistence": "PERSISTENT_EXIT",
                "source_ledger_sha256": STRUCTURE_LEDGER_HASH,
                "all_required_facts_resolved": True,
                "unresolved_reason_codes": (),
                "emit_to_replay": True,
            },
        ),
        "execution_status_facts": (
            status(D0, entry_at - timedelta(seconds=1)),
            status(D0 + timedelta(days=1), exit_at - timedelta(seconds=1)),
        ),
    }
    payload["content_sha256"] = stable_hash(
        payload,
        excluded=frozenset({"generated_at", "content_sha256"}),
    )
    return payload


def corporate_ledger(*, cash_event: bool) -> dict[str, object]:
    events: tuple[dict[str, object], ...] = ()
    if cash_event:
        events = (
            {
                "effective_on": (D0 + timedelta(days=1)).isoformat(),
                "availability_policy": ("EFFECTIVE_SESSION_OPEN_RESEARCH_ASSUMPTION"),
                "raw": {
                    "time": 0.0,
                    "interest": 0.1,
                    "stockBonus": 0.0,
                    "stockGift": 0.0,
                    "allotNum": 0.0,
                    "allotPrice": 0.0,
                    "gugai": 0.0,
                    "dr": 1.0,
                },
            },
        )
    payload: dict[str, object] = {
        "schema": CORPORATE_ACTION_SCHEMA,
        "generated_at": "2026-07-26T00:00:00+08:00",
        "instruments": (
            {
                "code": "510300.SH",
                "status": "EFFECTIVE_DATED_EVENTS_AVAILABLE",
                "events": events,
            },
        ),
    }
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
    ).encode()
    payload["content_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def market_bars(
    entry_at: datetime, exit_at: datetime
) -> tuple[HistoricalMinuteExecutionBar, ...]:
    action_at = at(D0 + timedelta(days=1), 9, 30)
    return (
        bar(
            entry_at - timedelta(minutes=1),
            1,
            low="9.8",
            high="10",
            volume="1000",
        ),
        bar(entry_at, 2, low="9.7", high="9.9", volume="8000"),
        bar(action_at, 3, low="9.2", high="9.4", volume="1000"),
        bar(
            exit_at - timedelta(minutes=1),
            4,
            low="9.3",
            high="9.5",
            volume="1000",
        ),
        bar(exit_at, 5, low="9.4", high="9.6", volume="8000"),
    )


def test_zero_chain_builds_a_legal_empty_replay_without_fact_invention() -> None:
    started = at(D0, 9, 30)
    built = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=()),),
        fact_ledger=None,
        bars_by_symbol={},
        corporate_action_ledger=None,
        initial_cash=Decimal("1000000"),
        started_at=started,
    )
    assert built.discovered_legal_chain_count == 0
    assert built.generated_entry_event_count == 0
    assert built.empty_replay
    assert built.runnable
    assert not built.return_evaluation_allowed
    assert built.payload["batches"] == ()
    assert any(
        item.code == "STRUCTURALLY_LEGAL_CHAIN_ZERO_EMPTY_REPLAY"
        for item in built.diagnostics
    )
    report = run_payload(built.payload)
    result = report["result"]
    assert result["metrics"]["net_return"] == "0"
    assert result["metrics"]["max_drawdown"] == "0"
    assert result["orders"] == []


def test_real_facts_generate_entry_cash_action_and_frozen_exit_then_replay() -> None:
    entry_at = at(D0, 10, 0)
    next_session = D0 + timedelta(days=1)
    exit_at = at(next_session, 11, 0)
    built = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=(chain_document(entry_at),)),),
        fact_ledger=fact_ledger(entry_at, exit_at),
        bars_by_symbol={SYMBOL: market_bars(entry_at, exit_at)},
        corporate_action_ledger=corporate_ledger(cash_event=True),
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert built.discovered_legal_chain_count == 1
    assert built.generated_entry_event_count == 1
    assert built.generated_structure_event_count == 1
    assert built.generated_cash_distribution_count == 1
    assert not built.empty_replay
    assert built.runnable
    assert built.return_evaluation_allowed
    assert not [
        value
        for value in built.diagnostics
        if value.severity in {"UNRESOLVED", "ERROR"}
    ]

    report = run_payload(built.payload, input_sha256="sha256:synthetic-real-facts")
    result = report["result"]
    assert [row["intent"]["action"] for row in result["intents"]] == [
        "ENTRY_INTENT",
        "STRATEGIC_EXIT_INTENT",
    ]
    assert len(result["orders"]) == 2
    assert result["orders"][0]["match"]["filled_quantity"] == 400
    assert result["orders"][1]["match"]["filled_quantity"] == 400
    assert result["positions"] == []
    assert result["corporate_actions"][0]["held_quantity"] == 400
    assert result["corporate_actions"][0]["cash_distribution"] == "40.0"
    assert result["metrics"]["strategic_cycle_count"] == 1
    assert result["metrics"]["total_fees"] == "10.00"
    assert result["live_status"] == "LIVE_DISABLED"


def test_persistent_exit_keeps_working_across_later_sessions() -> None:
    """R-04：持久战略退出被当日阻断后，必须仍能在后续交易日继续成交。

    引擎要求 PERSISTENT_EXIT 不设到期；若 builder 只喂决策当日剩余分钟柱，
    被 T+1、停牌或跌停无成交挡住的退出就再也拿不到可成交柱，等于静默放弃。
    """

    entry_at = at(D0, 10, 0)
    next_session = D0 + timedelta(days=1)
    exit_at = at(next_session, 11, 0)
    later_session_bar = bar(
        at(D0 + timedelta(days=2), 10, 0),
        6,
        low="9.4",
        high="9.6",
        volume="8000",
    )
    built = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=(chain_document(entry_at),)),),
        fact_ledger=fact_ledger(entry_at, exit_at),
        bars_by_symbol={SYMBOL: market_bars(entry_at, exit_at) + (later_session_bar,)},
        corporate_action_ledger=corporate_ledger(cash_event=True),
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert built.generated_structure_event_count == 1

    events = [event for batch in built.payload["batches"] for event in batch["events"]]
    # 持久退出以“无到期”识别；可放弃单必有到期时刻。
    exits = [event for event in events if event["expires_at"] is None]
    optionals = [event for event in events if event["expires_at"] is not None]
    assert len(exits) == 1 and len(optionals) == 1

    # 柱供给跨过 session 边界到数据尽头。
    exit_bar_ends = {value["closed_at"] for value in exits[0]["bars"]}
    assert later_session_bar.closed_at.isoformat() in exit_bar_ends
    assert len({value["opened_at"][:10] for value in exits[0]["bars"]}) > 1

    # 可放弃单不受影响：仍然只有一根 L2 柱的有效期。
    assert optionals[0]["expires_at"] is not None
    assert later_session_bar.closed_at.isoformat() not in {
        value["closed_at"] for value in optionals[0]["bars"]
    }


def test_chain_with_missing_fact_ledger_is_empty_and_explicitly_unresolved() -> None:
    decision = at(D0, 10, 0)
    built = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=(chain_document(decision),)),),
        fact_ledger=None,
        bars_by_symbol={},
        corporate_action_ledger=None,
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert built.discovered_legal_chain_count == 1
    assert built.generated_entry_event_count == 0
    assert not built.return_evaluation_allowed
    codes = {item.code for item in built.diagnostics}
    assert "UNRESOLVED_FROZEN_DECISION_FACT_LEDGER_MISSING" in codes
    assert "NO_ENTRY_EVENT_SURVIVED_FACT_GATES" in codes


def test_prescreen_and_fact_ledgers_require_exact_content_hashes() -> None:
    entry_at = at(D0, 10, 0)
    exit_at = at(D0 + timedelta(days=1), 11, 0)
    altered_prescreen = prescreen(chains=(chain_document(entry_at),))
    altered_prescreen["highest_status"] = "TAMPERED_AFTER_HASH"
    rejected_prescreen = build_replay_payload(
        prescreen_artifacts=(altered_prescreen,),
        fact_ledger=None,
        bars_by_symbol={},
        corporate_action_ledger=None,
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert not rejected_prescreen.runnable
    assert any(
        value.code == "PRESCREEN_CONTENT_HASH_MISMATCH"
        for value in rejected_prescreen.diagnostics
    )

    altered_facts = fact_ledger(entry_at, exit_at)
    altered_facts["execution_policy"]["broker_latency_seconds"] = 1  # type: ignore[index]
    rejected_facts = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=(chain_document(entry_at),)),),
        fact_ledger=altered_facts,
        bars_by_symbol={SYMBOL: market_bars(entry_at, exit_at)},
        corporate_action_ledger=corporate_ledger(cash_event=False),
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert not rejected_facts.runnable
    assert rejected_facts.generated_entry_event_count == 0
    assert any(
        value.code == "FACT_LEDGER_CONTENT_HASH_MISMATCH"
        for value in rejected_facts.diagnostics
    )


def test_candidate_parent_identity_is_rejected_before_return_is_allowed() -> None:
    entry_at = at(D0, 10, 0)
    exit_at = at(D0 + timedelta(days=1), 11, 0)
    facts = fact_ledger(entry_at, exit_at)
    entry = facts["entry_facts"][0]  # type: ignore[index]
    entry["candidate"]["parameter_set_id"] = "sha256:" + "0" * 64  # type: ignore[index]
    facts["content_sha256"] = stable_hash(
        facts,
        excluded=frozenset({"generated_at", "content_sha256"}),
    )
    built = build_replay_payload(
        prescreen_artifacts=(prescreen(chains=(chain_document(entry_at),)),),
        fact_ledger=facts,
        bars_by_symbol={SYMBOL: market_bars(entry_at, exit_at)},
        corporate_action_ledger=corporate_ledger(cash_event=False),
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
    )
    assert built.generated_entry_event_count == 0
    assert not built.return_evaluation_allowed
    assert any(
        value.code == "UNRESOLVED_CANDIDATE_PARENT_PARAMETER_BINDING"
        for value in built.diagnostics
    )
