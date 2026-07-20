from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.paper_admission as paper_admission_module

from chanlun.decision_support.exit_evaluation_store import (
    ExitEvaluationCommitment,
    ExitEvaluationSnapshot,
    SQLiteExitEvaluationStore,
)
from chanlun.decision_support.paper_adapter import (
    PaperBar,
    PaperFill,
    PaperIntent,
    PaperLedgerState,
    PaperLot,
)
from chanlun.decision_support.paper_admission import (
    PaperAccountSnapshot,
    SQLitePaperLedger,
)
from chanlun.decision_support.paper_read_model import PaperResearchReadModel
from chanlun.decision_support.paper_runtime import (
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.risk import RiskPolicy
from chanlun.decision_support.strategy_run import (
    StrategyRunIdentity,
    establish_strategy_run,
    trusted_bar_schema_fingerprint,
)


_NOW = datetime(2026, 7, 14, 10, 35, tzinfo=timezone(timedelta(hours=8)))


def _assert_research_only(payload: dict[str, object]) -> None:
    assert payload["schema_version"] == 1
    assert payload["mode"] == "research_paper"
    assert payload["read_only"] is True
    assert payload["auto_order_enabled"] is False
    assert payload["live_order_capability"] is False


def test_empty_sqlite_projection_is_explicitly_research_only(tmp_path) -> None:
    model = PaperResearchReadModel(
        SQLitePaperLedger(
            tmp_path / "paper-ledger.sqlite3",
            initial_cash=Decimal("100000"),
        ),
        exit_store=SQLiteExitEvaluationStore(
            tmp_path / "exit-evaluations.sqlite3"
        ),
    )

    payloads = (
        model.status(),
        model.account(),
        model.positions(),
        model.intents(),
        model.fills(),
        model.exits(),
    )

    for payload in payloads:
        _assert_research_only(payload)
    assert model.status() == {
        "schema_version": 1,
        "mode": "research_paper",
        "read_only": True,
        "auto_order_enabled": False,
        "live_order_capability": False,
        "ledger_revision": 0,
        "exit_evaluation_revision": 0,
        "intent_count": 0,
        "pending_intent_count": 0,
        "fill_count": 0,
        "lot_count": 0,
        "position_count": 0,
    }
    assert model.account()["valuation_basis"] == (
        "cost_basis_not_mark_to_market"
    )
    assert model.account()["initial_cash"] == "100000.00"
    assert model.positions()["items"] == []
    assert model.intents()["items"] == []
    assert model.fills()["items"] == []
    assert model.exits()["items"] == []
    for unsafe_method in ("admit", "process_bar", "sell", "execute"):
        assert not hasattr(model, unsafe_method)


def test_projection_rejects_missing_exit_store(tmp_path) -> None:
    ledger = SQLitePaperLedger(
        tmp_path / "paper-ledger.sqlite3",
        initial_cash=Decimal("100000"),
    )

    with pytest.raises(TypeError, match="exit_store"):
        PaperResearchReadModel(ledger, exit_store=None)


def test_projection_rejects_runtime_without_bulk_exit_attestation(tmp_path) -> None:
    runtime = SimpleNamespace(
        health=lambda: object(),
        is_exit_snapshot_committed=lambda _snapshot: True,
    )

    with pytest.raises(TypeError, match="attest_exit_snapshots"):
        PaperResearchReadModel(
            SQLitePaperLedger(
                tmp_path / "bulk-contract-ledger.sqlite3",
                initial_cash=Decimal("100000"),
            ),
            exit_store=SQLiteExitEvaluationStore(
                tmp_path / "bulk-contract-exits.sqlite3"
            ),
            runtime=runtime,
        )


def test_paper_read_model_rejects_non_read_only_health(tmp_path) -> None:
    runtime = SimpleNamespace(
        health=lambda: SimpleNamespace(
            mode="research_paper",
            read_only=False,
            auto_order_enabled=False,
            live_order_capability=False,
        ),
        attest_exit_snapshots=lambda snapshots: (False,) * len(snapshots),
    )
    model = PaperResearchReadModel(
        SQLitePaperLedger(
            tmp_path / "non-read-only-ledger.sqlite3",
            initial_cash=Decimal("100000"),
        ),
        exit_store=SQLiteExitEvaluationStore(
            tmp_path / "non-read-only-exits.sqlite3"
        ),
        runtime=runtime,
    )

    with pytest.raises(TypeError, match="health mode"):
        model.status()


class _LedgerStub:
    def __init__(self, state: PaperLedgerState) -> None:
        self.state = state

    def load(self) -> PaperLedgerState:
        return self.state

    def account_snapshot(self) -> PaperAccountSnapshot:
        return PaperAccountSnapshot(
            initial_cash=Decimal("100000.00"),
            cash_balance=Decimal("98990.00"),
            reserved_buying_power=Decimal("0.00"),
            available_buying_power=Decimal("98990.00"),
            positions_cost=Decimal("1000.00"),
            cost_basis_equity=Decimal("99990.00"),
        )


def _ledger_state() -> PaperLedgerState:
    intent = PaperIntent(
        event_id="entry-event-1",
        event_data_fingerprint="sha256:" + "0" * 64,
        review_id="review-1",
        risk_snapshot_id="risk-1",
        admission_authorization_id="authorization-1",
        admission_payload_fingerprint="sha256:" + "1" * 64,
        admitted_at=_NOW,
        risk_expires_at=_NOW + timedelta(minutes=30),
        entry_event_id="entry-event-1",
        code="SH.600001",
        side="buy",
        risk_shares=100,
        requested_shares=100,
        remaining_shares=0,
        signal_bar_id="signal-bar-1",
        signal_at=_NOW - timedelta(minutes=5),
        limit_pct=Decimal("0.10"),
        status="filled",
        reason="paper_fill",
        fee_schedule_fingerprint="sha256:" + "2" * 64,
        execution_policy_fingerprint="sha256:" + "3" * 64,
    )
    fill = PaperFill(
        fill_id="fill-1",
        event_id=intent.event_id,
        entry_event_id=intent.entry_event_id,
        review_id=intent.review_id,
        risk_snapshot_id=intent.risk_snapshot_id,
        code=intent.code,
        side=intent.side,
        shares=100,
        reference_price=Decimal("9.99"),
        price=Decimal("10.00"),
        gross_value=Decimal("1000.00"),
        commission=Decimal("5.00"),
        stamp_duty=Decimal("0.00"),
        transfer_fee=Decimal("0.02"),
        regulatory_fee=Decimal("0.00"),
        slippage_cost=Decimal("1.00"),
        trade_cost=Decimal("5.02"),
        filled_at=_NOW + timedelta(minutes=1),
        bar_id="fill-bar-1",
    )
    lot = PaperLot(
        code=intent.code,
        shares=100,
        price=Decimal("10.00"),
        opened_at=fill.filled_at,
        entry_event_id=intent.entry_event_id,
        entry_review_id=intent.review_id,
        entry_risk_snapshot_id=intent.risk_snapshot_id,
    )
    return PaperLedgerState(
        revision=7,
        intents=(intent,),
        fills=(fill,),
        lots=(lot,),
        processed_bar_ids=(fill.bar_id,),
    )


def _exit_snapshot() -> ExitEvaluationSnapshot:
    return ExitEvaluationSnapshot(
        entry_event_id="entry-event-1",
        evaluation_cycle_id="sha256:" + "4" * 64,
        entry_provenance_fingerprint="sha256:" + "5" * 64,
        exit_evidence_policy_fingerprint="sha256:" + "6" * 64,
        certified_corpus_manifest_fingerprint="sha256:" + "7" * 64,
        source_pdf_fingerprint="sha256:" + "8" * 64,
        bar_structure_payload_fingerprint="sha256:" + "9" * 64,
        risk_context_payload_fingerprint="sha256:" + "a" * 64,
        quote_payload_fingerprint="sha256:" + "b" * 64,
        algorithm_version="chanlun-exit-runtime-v2",
        evaluation_version=2,
        recommendation_payload={"action": "observe", "urgency": "none"},
        evaluated_at=_NOW + timedelta(minutes=2),
    )


def test_projection_serializes_ledger_and_verified_exits(tmp_path) -> None:
    exit_store = SQLiteExitEvaluationStore(tmp_path / "exit.sqlite3")
    exit_snapshot = _exit_snapshot()
    exit_store.persist(exit_snapshot, expected_revision=0)
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
    )

    account = model.account()
    position = model.positions()["items"][0]
    intent = model.intents()["items"][0]
    fill = model.fills()["items"][0]
    exit_item = model.exits()["items"][0]

    assert account["available_buying_power"] == "98990.00"
    assert account["cost_basis_equity"] == "99990.00"
    assert position == {
        "code": "SH.600001",
        "shares": 100,
        "average_price": "10.00",
        "opened_at": "2026-07-14T10:36:00+08:00",
        "entry_event_id": "entry-event-1",
        "entry_review_id": "review-1",
        "entry_risk_snapshot_id": "risk-1",
    }
    assert intent["admission_authorization_id"] == "authorization-1"
    assert intent["limit_pct"] == "0.10"
    assert intent["admitted_at"] == "2026-07-14T10:35:00+08:00"
    assert fill["price"] == "10.00"
    assert fill["trade_cost"] == "5.02"
    assert fill["filled_at"] == "2026-07-14T10:36:00+08:00"
    assert exit_item == exit_snapshot.to_dict()


def test_runtime_projection_withholds_exit_snapshots_until_cycle_commit(
    tmp_path,
) -> None:
    exit_store = SQLiteExitEvaluationStore(tmp_path / "committed-exits.sqlite3")
    committed = _exit_snapshot()
    provisional = replace(
        committed,
        entry_event_id="entry-event-provisional",
        evaluation_cycle_id="sha256:" + "d" * 64,
        evaluated_at=committed.evaluated_at + timedelta(minutes=5),
    )
    exit_store.persist(committed, expected_revision=0)
    exit_store.persist(provisional, expected_revision=1)
    runtime = SimpleNamespace(
        health=lambda: object(),
        attest_exit_snapshots=lambda snapshots: tuple(
            snapshot == committed for snapshot in snapshots
        ),
    )
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
        runtime=runtime,
    )

    payload = model.exits()

    assert payload["count"] == 1
    assert payload["provisional_count"] == 1
    assert payload["publication_basis"] == "exact_exit_manifest_membership"
    assert payload["items"] == [committed.to_dict()]


@pytest.mark.parametrize(
    "bulk_result",
    [
        (True,),
        [True, False],
        (True, 1),
    ],
)
def test_runtime_projection_withholds_all_exits_on_invalid_bulk_result(
    tmp_path,
    bulk_result,
) -> None:
    exit_store = SQLiteExitEvaluationStore(tmp_path / "invalid-bulk-exits.sqlite3")
    first = _exit_snapshot()
    second = replace(
        first,
        entry_event_id="entry-event-second",
        evaluation_cycle_id=_fp("d"),
    )
    exit_store.persist(first, expected_revision=0)
    exit_store.persist(second, expected_revision=1)
    calls = 0

    def attest_exit_snapshots(_snapshots):
        nonlocal calls
        calls += 1
        return bulk_result

    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
        runtime=SimpleNamespace(
            health=lambda: object(),
            attest_exit_snapshots=attest_exit_snapshots,
        ),
    )

    payload = model.exits()

    assert calls == 1
    assert payload["count"] == 0
    assert payload["provisional_count"] == 2
    assert payload["items"] == []


def test_runtime_projection_withholds_all_exits_when_bulk_attestation_raises(
    tmp_path,
) -> None:
    exit_store = SQLiteExitEvaluationStore(tmp_path / "raising-bulk-exits.sqlite3")
    first = _exit_snapshot()
    second = replace(
        first,
        entry_event_id="entry-event-second",
        evaluation_cycle_id=_fp("d"),
    )
    exit_store.persist(first, expected_revision=0)
    exit_store.persist(second, expected_revision=1)
    calls = 0

    def attest_exit_snapshots(_snapshots):
        nonlocal calls
        calls += 1
        raise RuntimeError("bulk attestation unavailable")

    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
        runtime=SimpleNamespace(
            health=lambda: object(),
            attest_exit_snapshots=attest_exit_snapshots,
        ),
    )

    payload = model.exits()

    assert calls == 1
    assert payload["count"] == 0
    assert payload["provisional_count"] == 2
    assert payload["items"] == []


def test_status_exposes_persisted_observation_gates_and_policy_identity(
    tmp_path,
) -> None:
    runtime = SimpleNamespace(
        attest_exit_snapshots=lambda snapshots: (True,) * len(snapshots),
        health=lambda: SimpleNamespace(
            mode="research_paper",
            read_only=True,
            auto_order_enabled=False,
            live_order_capability=False,
            bar_store=SimpleNamespace(
                bar_count=321,
                observed_trading_days=19,
                degraded=True,
                degraded_reason="paper_bar_gap_detected",
                last_bar_closed_at=_NOW,
                last_attempted_bar_closed_at=_NOW + timedelta(minutes=5),
                last_attempt_complete=False,
                last_attempt_failure="TrustedPaperBarIntegrityError",
                calendar_preflight_failure_at=_NOW + timedelta(minutes=10),
                calendar_preflight_failure="paper_calendar_date_out_of_coverage",
            ),
            bar_cycles=100,
            bar_cycle_failures=1,
            admission_cycles=50,
            admission_failures=2,
            admitted_event_count=3,
            last_error="TrustedPaperBarIntegrityError",
            exit_coverage=SimpleNamespace(
                bar_closed_at=_NOW - timedelta(minutes=5),
                open_entry_count=2,
                snapshot_count=1,
                failure_count=1,
                complete=True,
                fresh=False,
                scan_code="scan_failed",
                failures={"entry-2": "resolver_failed"},
            ),
        )
    )
    policy = SimpleNamespace(
        fee_schedule_fingerprint="sha256:" + "c" * 64,
        execution_policy_fingerprint="sha256:" + "d" * 64,
    )
    strategy_run_payload = {
        "run_id": "paper-run-fixture",
        "epoch": 7,
        "fingerprint": "sha256:" + "e" * 64,
        "state": "active",
        "started_at": _NOW.isoformat(),
        "evidence_scope": "current_epoch_only",
        "store_bindings_complete": True,
        "switch_capability": "cold_stop_drain_required",
        "rolling_switch_supported": False,
        "mutation_lease_protocol": "durable_registry_v1",
        "inflight_mutation_count": 0,
        "mutations_drained": True,
        "identity": {"schema_version": 1},
    }
    strategy_run = SimpleNamespace(
        status_payload=lambda: dict(strategy_run_payload),
    )
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=SQLiteExitEvaluationStore(tmp_path / "status-exits.sqlite3"),
        runtime=runtime,
        policy_provider=policy,
        strategy_run=strategy_run,
    )

    status = model.status()

    assert status["trusted_bar_store"] == {
        "bar_count": 321,
        "observed_trading_days": 19,
        "degraded": True,
        "degraded_reason": "paper_bar_gap_detected",
        "last_bar_closed_at": "2026-07-14T10:35:00+08:00",
        "last_attempted_bar_closed_at": "2026-07-14T10:40:00+08:00",
        "last_attempt_complete": False,
        "last_attempt_failure": "TrustedPaperBarIntegrityError",
        "calendar_preflight_failure_at": "2026-07-14T10:45:00+08:00",
        "calendar_preflight_failure": "paper_calendar_date_out_of_coverage",
    }
    assert status["paper_observation_gate"] == {
        "passed": False,
        "run_id": "paper-run-fixture",
        "epoch": 7,
        "strategy_run_fingerprint": "sha256:" + "e" * 64,
        "evidence_scope": "current_epoch_only",
        "trading_days": 19,
        "minimum_trading_days": 20,
        "remaining_trading_days": 1,
        "executable_events": 1,
        "minimum_executable_events": 30,
        "remaining_executable_events": 29,
        "reasons": [
            "trusted_bar_store_degraded",
            "paper_runtime_unhealthy",
            "paper_bar_cycle_incomplete",
            "paper_calendar_preflight_failed",
            "paper_scan_not_complete",
            "paper_exit_coverage_stale",
            "paper_exit_coverage_failure",
            "insufficient_paper_trading_days",
            "insufficient_paper_executable_events",
        ],
    }
    assert status["exit_coverage"] == {
        "bar_closed_at": "2026-07-14T10:30:00+08:00",
        "open_entry_count": 2,
        "snapshot_count": 1,
        "failure_count": 1,
        "complete": True,
        "fresh": False,
        "scan_code": "scan_failed",
        "cycle_failure": None,
        "failures": {"entry-2": "resolver_failed"},
    }
    assert status["broker_compliance_confirmation"] == "pending"
    assert status["promotion_eligible"] is False
    assert status["switch_capability"] == "cold_stop_drain_required"
    assert status["rolling_switch_supported"] is False
    assert status["mutation_lease_protocol"] == "durable_registry_v1"
    assert status["inflight_mutation_count"] == 0
    assert status["mutations_drained"] is True
    assert status["fee_schedule_fingerprint"] == "sha256:" + "c" * 64
    assert status["execution_policy_fingerprint"] == "sha256:" + "d" * 64
    assert status["strategy_run"] == strategy_run_payload
    assert status["runtime_counters"]["bar_cycle_failures"] == 1
    assert status["runtime_counters"]["admission_failures"] == 2


def test_runtime_gate_fails_closed_when_strategy_run_is_unavailable(tmp_path) -> None:
    runtime = SimpleNamespace(
        attest_exit_snapshots=lambda snapshots: (True,) * len(snapshots),
        health=lambda: SimpleNamespace(
            mode="research_paper",
            read_only=True,
            auto_order_enabled=False,
            live_order_capability=False,
            bar_store=SimpleNamespace(
                bar_count=960,
                observed_trading_days=20,
                degraded=False,
                degraded_reason=None,
                last_bar_closed_at=_NOW,
                last_attempted_bar_closed_at=_NOW,
                last_attempt_complete=True,
                last_attempt_failure=None,
            ),
            bar_cycles=960,
            bar_cycle_failures=0,
            admission_cycles=30,
            admission_failures=0,
            admitted_event_count=30,
            last_error=None,
            exit_coverage=SimpleNamespace(
                bar_closed_at=_NOW,
                open_entry_count=1,
                snapshot_count=1,
                failure_count=0,
                complete=True,
                fresh=True,
                scan_code="scan_complete",
                cycle_failure=None,
                failures={},
            ),
        )
    )
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=SQLiteExitEvaluationStore(tmp_path / "missing-run.sqlite3"),
        runtime=runtime,
    )

    status = model.status()

    assert status["strategy_run"] == {
        "state": "unavailable",
        "evidence_scope": "none",
        "store_bindings_complete": False,
    }
    assert "strategy_run_unavailable" in status["paper_observation_gate"][
        "reasons"
    ]
    assert status["paper_observation_gate"]["passed"] is False


_CN = ZoneInfo("Asia/Shanghai")


def _fp(character: str) -> str:
    return "sha256:" + character * 64


class _EpochCalendar:
    def __init__(self, trading_days: tuple[date, ...], fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self._trading_days = trading_days

    @staticmethod
    def _closes(trading_day: date) -> tuple[datetime, ...]:
        values: list[datetime] = []
        closed_at = datetime.combine(trading_day, time(9, 35), _CN)
        while closed_at.time() <= time(11, 30):
            values.append(closed_at)
            closed_at += timedelta(minutes=5)
        closed_at = datetime.combine(trading_day, time(13, 5), _CN)
        while closed_at.time() <= time(15, 0):
            values.append(closed_at)
            closed_at += timedelta(minutes=5)
        return tuple(values)

    def session_for(self, trading_day: date) -> SimpleNamespace:
        position = self._trading_days.index(trading_day)
        return SimpleNamespace(
            trading_day=trading_day,
            previous_trading_day=(
                None if position == 0 else self._trading_days[position - 1]
            ),
            expected_bar_closes=self._closes(trading_day),
            calendar_fingerprint=self.fingerprint,
        )


def test_read_model_publishes_only_real_exact_manifest_membership(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=_CN)
    calendar = _EpochCalendar((closed_at.date(),), _fp("e"))
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "exact-publication-bars.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    bar = PaperBar(
        code="SH.600001",
        opened_at=closed_at - timedelta(minutes=5),
        closed_at=closed_at,
        open_price=Decimal("10.00"),
        close_price=Decimal("10.00"),
        previous_close=Decimal("10.00"),
        max_fill_shares=1000,
    )
    bar_store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=(bar.code,),
        bars={bar.code: bar},
        optional_failures={},
    )
    committed = replace(_exit_snapshot(), evaluated_at=closed_at)
    raw_after_completion = replace(
        committed,
        entry_event_id="entry-event-raw-after-complete",
        evaluation_cycle_id=_fp("d"),
    )
    bar_store.complete_cycle(
        closed_at,
        exit_commitments=(
            ExitEvaluationCommitment.from_snapshot(committed),
        ),
    )
    exit_store = SQLiteExitEvaluationStore(
        tmp_path / "exact-publication-exits.sqlite3"
    )
    exit_store.persist(committed, expected_revision=0)
    exit_store.persist(raw_after_completion, expected_revision=1)
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
        runtime=SimpleNamespace(
            health=lambda: object(),
            attest_exit_snapshots=bar_store.attest_exit_snapshots,
        ),
    )

    payload = model.exits()

    assert payload["count"] == 1
    assert payload["provisional_count"] == 1
    assert payload["items"] == [committed.to_dict()]


def test_read_model_bulk_attests_mixed_snapshots_with_one_global_validation(
    tmp_path,
    monkeypatch,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=_CN)
    calendar = _EpochCalendar((closed_at.date(),), _fp("e"))
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "bulk-publication-bars.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    bar = PaperBar(
        code="SH.600001",
        opened_at=closed_at - timedelta(minutes=5),
        closed_at=closed_at,
        open_price=Decimal("10.00"),
        close_price=Decimal("10.00"),
        previous_close=Decimal("10.00"),
        max_fill_shares=1000,
    )
    bar_store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=(bar.code,),
        bars={bar.code: bar},
        optional_failures={},
    )
    committed = replace(_exit_snapshot(), evaluated_at=closed_at)
    uncommitted = replace(
        committed,
        entry_event_id="entry-event-uncommitted",
        evaluation_cycle_id=_fp("c"),
    )
    clean_tamper_target = replace(
        committed,
        entry_event_id="entry-event-tampered",
        evaluation_cycle_id=_fp("d"),
    )
    tampered = replace(
        clean_tamper_target,
        recommendation_payload={"action": "sell", "urgency": "immediate"},
    )
    bar_store.complete_cycle(
        closed_at,
        exit_commitments=(
            ExitEvaluationCommitment.from_snapshot(committed),
            ExitEvaluationCommitment.from_snapshot(clean_tamper_target),
        ),
    )
    exit_store = SQLiteExitEvaluationStore(
        tmp_path / "bulk-publication-exits.sqlite3"
    )
    for revision, snapshot in enumerate((committed, uncommitted, tampered)):
        exit_store.persist(snapshot, expected_revision=revision)

    validation_calls = {"signal": 0, "exit": 0}
    validate_signal = bar_store._validate_signal_observation_log
    validate_exit = bar_store._validate_exit_manifest_log

    def counted_signal(connection):
        validation_calls["signal"] += 1
        return validate_signal(connection)

    def counted_exit(connection):
        validation_calls["exit"] += 1
        return validate_exit(connection)

    monkeypatch.setattr(
        bar_store,
        "_validate_signal_observation_log",
        counted_signal,
    )
    monkeypatch.setattr(
        bar_store,
        "_validate_exit_manifest_log",
        counted_exit,
    )
    model = PaperResearchReadModel(
        _LedgerStub(_ledger_state()),
        exit_store=exit_store,
        runtime=SimpleNamespace(
            health=lambda: object(),
            attest_exit_snapshots=bar_store.attest_exit_snapshots,
        ),
    )

    payload = model.exits()

    assert payload["count"] == 1
    assert payload["provisional_count"] == 2
    assert validation_calls == {"signal": 1, "exit": 1}
    assert payload["publication_basis"] == "exact_exit_manifest_membership"
    assert payload["items"] == [committed.to_dict()]


def _weekdays(start: date, count: int) -> tuple[date, ...]:
    values: list[date] = []
    candidate = start
    while len(values) < count:
        if candidate.weekday() < 5:
            values.append(candidate)
        candidate += timedelta(days=1)
    return tuple(values)


def _identity_for_read_model_epoch(
    *,
    calendar_fingerprint: str,
    execution_policy_fingerprint: str,
    fee_schedule_fingerprint: str,
) -> StrategyRunIdentity:
    return StrategyRunIdentity(
        rule_set_fingerprint=_fp("1"),
        corpus_manifest_fingerprint=_fp("2"),
        source_pdf_fingerprint=_fp("3"),
        rule_algorithm_fingerprint=_fp("4"),
        strategy_engine_build_fingerprint=_fp("5"),
        scanner_algorithm_fingerprint=_fp("6"),
        structure_algorithm_fingerprint=_fp("7"),
        universe_policy_fingerprint=_fp("8"),
        monitor_policy_fingerprint=_fp("9"),
        review_provider="fixture-provider",
        review_model="fixture-model",
        review_prompt_version="fixture-prompt-v1",
        review_schema_fingerprint=_fp("a"),
        review_runtime_policy_fingerprint=_fp("b"),
        execution_policy_fingerprint=execution_policy_fingerprint,
        fee_schedule_fingerprint=fee_schedule_fingerprint,
        initial_cash=Decimal("100000.00"),
        account_algorithm_fingerprint=_fp("c"),
        risk_policy_fingerprint=_fp("c"),
        exit_policy_fingerprint=_fp("d"),
        exit_algorithm_fingerprint=_fp("e"),
        calendar_fingerprint=calendar_fingerprint,
        bar_provider_fingerprint=_fp("f"),
        bar_schema_fingerprint=trusted_bar_schema_fingerprint(),
    )


def _epoch_store_bundle(tmp_path, name: str, calendar_fingerprint: str):
    root = tmp_path / name
    root.mkdir()
    paths = {
        "ledger": root / "ledger.sqlite3",
        "bar": root / "bars.sqlite3",
        "risk": root / "risk.sqlite3",
        "exit": root / "exit.sqlite3",
    }
    ledger = SQLitePaperLedger(
        paths["ledger"],
        initial_cash=Decimal("100000.00"),
    )
    bar_store = SQLiteTrustedPaperBarStore(
        paths["bar"],
        calendar_fingerprint=calendar_fingerprint,
    )
    SQLitePaperRiskState(paths["risk"], policy=RiskPolicy.conservative())
    exit_store = SQLiteExitEvaluationStore(paths["exit"])
    return paths, ledger, bar_store, exit_store


def _completed_round_trip_state(
    count: int,
    *,
    prefix: str,
    fee_schedule_fingerprint: str,
    execution_policy_fingerprint: str,
) -> PaperLedgerState:
    intents: list[PaperIntent] = []
    fills: list[PaperFill] = []
    processed_bar_ids: list[str] = []
    for index in range(count):
        entry_event_id = f"{prefix}-buy-{index:02d}"
        exit_event_id = f"{prefix}-sell-{index:02d}"
        code = f"SH.{600000 + index:06d}"
        for side, event_id in (("buy", entry_event_id), ("sell", exit_event_id)):
            bar_id = f"{prefix}-{side}-bar-{index:02d}"
            intent = PaperIntent(
                event_id=event_id,
                event_data_fingerprint=sha256_json(
                    {"schema_version": 1, "event_id": event_id}
                ),
                review_id=f"{prefix}-{side}-review-{index:02d}",
                risk_snapshot_id=f"{prefix}-{side}-risk-{index:02d}",
                admission_authorization_id=(
                    f"{prefix}-{side}-authorization-{index:02d}"
                ),
                admission_payload_fingerprint=sha256_json(
                    {"schema_version": 1, "authorization_event_id": event_id}
                ),
                admitted_at=_NOW,
                risk_expires_at=_NOW + timedelta(minutes=30),
                entry_event_id=entry_event_id,
                code=code,
                side=side,
                risk_shares=100,
                requested_shares=100,
                remaining_shares=0,
                signal_bar_id=f"{prefix}-{side}-signal-{index:02d}",
                signal_at=_NOW - timedelta(minutes=5),
                limit_pct=Decimal("0.10"),
                status="filled",
                reason="paper_fill",
                fee_schedule_fingerprint=fee_schedule_fingerprint,
                execution_policy_fingerprint=execution_policy_fingerprint,
            )
            intents.append(intent)
            fills.append(
                PaperFill(
                    fill_id=f"{prefix}-{side}-fill-{index:02d}",
                    event_id=event_id,
                    entry_event_id=entry_event_id,
                    review_id=intent.review_id,
                    risk_snapshot_id=intent.risk_snapshot_id,
                    code=code,
                    side=side,
                    shares=100,
                    reference_price=Decimal("10.00"),
                    price=Decimal("10.00"),
                    gross_value=Decimal("1000.00"),
                    commission=Decimal("0.00"),
                    stamp_duty=Decimal("0.00"),
                    transfer_fee=Decimal("0.00"),
                    regulatory_fee=Decimal("0.00"),
                    slippage_cost=Decimal("0.00"),
                    trade_cost=Decimal("0.00"),
                    filled_at=_NOW + timedelta(minutes=1),
                    bar_id=bar_id,
                )
            )
            processed_bar_ids.append(bar_id)
    return PaperLedgerState(
        revision=1,
        intents=tuple(intents),
        fills=tuple(fills),
        lots=(),
        processed_bar_ids=tuple(processed_bar_ids),
    )


def _seed_completed_buy_events(
    ledger: SQLitePaperLedger,
    count: int,
    *,
    prefix: str,
    policy_document: dict[str, object],
) -> None:
    fee_fingerprint = policy_document["fee_schedule_fingerprint"]
    execution_fingerprint = sha256_json(policy_document)
    assert isinstance(fee_fingerprint, str)
    state = _completed_round_trip_state(
        count,
        prefix=prefix,
        fee_schedule_fingerprint=fee_fingerprint,
        execution_policy_fingerprint=execution_fingerprint,
    )
    state_json = paper_admission_module._state_json(state)
    policy_json = paper_admission_module._execution_policy_json(policy_document)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            """
            UPDATE paper_ledger
            SET revision = ?, state_json = ?, state_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                state.revision,
                state_json,
                paper_admission_module._text_sha256(state_json),
            ),
        )
        connection.execute(
            """
            INSERT INTO paper_execution_policy (
                singleton_id, fee_schedule_fingerprint,
                execution_policy_fingerprint, policy_json, policy_sha256
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (
                fee_fingerprint,
                execution_fingerprint,
                policy_json,
                paper_admission_module._text_sha256(policy_json),
            ),
        )
    assert len(ledger.load().fills) == count * 2


def _record_observed_days(
    store: SQLiteTrustedPaperBarStore,
    calendar: _EpochCalendar,
) -> None:
    code = "SH.600001"
    for trading_day in calendar._trading_days:
        session = calendar.session_for(trading_day)
        for closed_at in session.expected_bar_closes:
            bar = PaperBar(
                code=code,
                opened_at=closed_at - timedelta(minutes=5),
                closed_at=closed_at,
                open_price=Decimal("10.00"),
                close_price=Decimal("10.00"),
                previous_close=Decimal("10.00"),
                max_fill_shares=1000,
            )
            store.record_cycle(
                session=session,
                bar_closed_at=closed_at,
                required_codes=(),
                optional_codes=(code,),
                bars={code: bar},
                optional_failures={},
            )
            store.complete_cycle(closed_at)


def _runtime_health(store: SQLiteTrustedPaperBarStore) -> SimpleNamespace:
    def health() -> SimpleNamespace:
        bar_health = store.health()
        return SimpleNamespace(
            mode="research_paper",
            read_only=True,
            auto_order_enabled=False,
            live_order_capability=False,
            bar_store=bar_health,
            bar_cycles=bar_health.bar_count,
            bar_cycle_failures=0,
            admission_cycles=0,
            admission_failures=0,
            admitted_event_count=0,
            last_error=None,
            exit_coverage=SimpleNamespace(
                bar_closed_at=bar_health.last_bar_closed_at,
                open_entry_count=0,
                snapshot_count=0,
                failure_count=0,
                complete=True,
                fresh=True,
                scan_code="scan_complete",
                cycle_failure=None,
                failures={},
            ),
        )

    return SimpleNamespace(
        health=health,
        attest_exit_snapshots=store.attest_exit_snapshots,
    )


def test_read_model_never_carries_epoch_one_evidence_into_epoch_two(
    tmp_path,
) -> None:
    registry_path = tmp_path / "strategy-runs.sqlite3"
    calendar_fingerprint = _fp("0")
    fee_fingerprint = _fp("f")
    policy_document: dict[str, object] = {
        "schema_version": 1,
        "algorithm_version": "read-model-epoch-fixture-v1",
        "fee_schedule_fingerprint": fee_fingerprint,
    }
    execution_fingerprint = sha256_json(policy_document)
    identity_one = _identity_for_read_model_epoch(
        calendar_fingerprint=calendar_fingerprint,
        execution_policy_fingerprint=execution_fingerprint,
        fee_schedule_fingerprint=fee_fingerprint,
    )
    epoch_one_days = _weekdays(date(2026, 6, 1), 19)
    epoch_two_days = _weekdays(epoch_one_days[-1] + timedelta(days=1), 1)

    paths_one, ledger_one, bars_one, exits_one = _epoch_store_bundle(
        tmp_path,
        "epoch-one",
        calendar_fingerprint,
    )
    active_one = establish_strategy_run(
        registry_path,
        requested_epoch=1,
        identity=identity_one,
        store_paths=paths_one,
        now=datetime.combine(epoch_one_days[0], time(8, 0), _CN),
    )
    _seed_completed_buy_events(
        ledger_one,
        29,
        prefix="epoch-one",
        policy_document=policy_document,
    )
    _record_observed_days(
        bars_one,
        _EpochCalendar(epoch_one_days, calendar_fingerprint),
    )
    model_one = PaperResearchReadModel(
        ledger_one,
        exit_store=exits_one,
        runtime=_runtime_health(bars_one),
        strategy_run=active_one,
    )
    status_one = model_one.status()
    assert status_one["paper_observation_gate"]["trading_days"] == 19
    assert status_one["paper_observation_gate"]["executable_events"] == 29
    assert status_one["promotion_eligible"] is False

    paths_two, ledger_two, bars_two, exits_two = _epoch_store_bundle(
        tmp_path,
        "epoch-two",
        calendar_fingerprint,
    )
    identity_two = replace(
        identity_one,
        strategy_engine_build_fingerprint=_fp("a"),
    )
    active_two = establish_strategy_run(
        registry_path,
        requested_epoch=2,
        identity=identity_two,
        store_paths=paths_two,
        now=datetime.combine(epoch_two_days[0], time(8, 0), _CN),
    )
    _seed_completed_buy_events(
        ledger_two,
        1,
        prefix="epoch-two",
        policy_document=policy_document,
    )
    _record_observed_days(
        bars_two,
        _EpochCalendar(epoch_two_days, calendar_fingerprint),
    )
    model_two = PaperResearchReadModel(
        ledger_two,
        exit_store=exits_two,
        runtime=_runtime_health(bars_two),
        strategy_run=active_two,
    )

    status_two = model_two.status()
    gate_two = status_two["paper_observation_gate"]
    assert status_two["strategy_run"]["run_id"] == active_two.run_id
    assert status_two["strategy_run"]["epoch"] == 2
    assert gate_two["run_id"] == active_two.run_id
    assert gate_two["epoch"] == 2
    assert gate_two["trading_days"] == 1
    assert gate_two["executable_events"] == 1
    assert gate_two["remaining_trading_days"] == 19
    assert gate_two["remaining_executable_events"] == 29
    assert status_two["switch_capability"] == "cold_stop_drain_required"
    assert status_two["rolling_switch_supported"] is False
    assert status_two["promotion_eligible"] is False

    stale_status = model_one.status()
    assert stale_status["strategy_run"]["state"] == "invalid"
    assert "strategy_run_invalid" in stale_status["paper_observation_gate"][
        "reasons"
    ]
    assert stale_status["promotion_eligible"] is False
