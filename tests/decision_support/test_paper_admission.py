from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import inspect
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionTransition,
    TableByLLMReview,
    TableByLLMReviewClaim,
    TableByPaperAdmissionAuthorization,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import DecisionEventStore
from chanlun.decision_support.evidence import EvidencePacket, RuleEvidenceBinding
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.manual_check_workflow import (
    FileManualCheckStore,
    ManualCheckAttempt,
    ManualCheckPending,
    RequiredManualCheck,
)
from chanlun.decision_support.manual_checks import ManualCheckSnapshot
from chanlun.decision_support.models import EventState
from chanlun.decision_support import paper_admission as paper_admission_module
from chanlun.decision_support.paper_adapter import (
    ConfirmedEventPaperAdapter,
    PaperAdapterConflictError,
    PaperAdapterEligibilityError,
    PaperBar,
    PaperFeeSchedule,
    PaperLedgerConflictError,
    PaperLedgerIntegrityError,
)
from chanlun.decision_support.paper_admission import (
    SQLitePaperLedger,
    TrustedPaperAdmission,
    TrustedPaperAdmissionError,
    bind_risk_snapshot_packet_fingerprint,
)
from chanlun.decision_support.paper_runtime import (
    ExplicitPaperTradingCalendar,
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
    TrustedPaperBarIntegrityError,
)
from chanlun.decision_support.risk import QuoteSnapshot, RiskDecision, RiskPolicy
from chanlun.decision_support.risk_snapshot import RiskSnapshot
from chanlun.decision_support.runtime import PaperRiskAccountProvider
from chanlun.decision_support.rule_cards import EvaluationVerdict
from chanlun.decision_support.rule_context import RuleRuntimeFacts
from tests.decision_support.conftest import ts


class _ZeroBarClock:
    def count_closed_bars(self, event, asof) -> int:
        return 0


class _MutableClock:
    def __init__(self, current) -> None:
        self.current = current

    def __call__(self):
        return self.current


class _TestBarSource:
    def __init__(self, *bars: PaperBar) -> None:
        self._bars = {bar.bar_id: bar for bar in bars}

    def register(self, *bars: PaperBar) -> None:
        self._bars.update((bar.bar_id, bar) for bar in bars)

    def get_bar(self, bar_id: str) -> PaperBar | None:
        return self._bars.get(bar_id)

    def attest_cycle_bar(
        self,
        bar_id: str,
        *,
        allow_current_started: bool = False,
    ) -> PaperBar | None:
        assert isinstance(allow_current_started, bool)
        return self._bars.get(bar_id)


class _PaperRiskQuotes:
    def quote_for_code(self, code, closed_at):
        return QuoteSnapshot(
            code=code,
            price=Decimal("10"),
            quote_time=closed_at,
            entry_tradable=True,
            exit_tradable=True,
            limit_up_locked=False,
            limit_down_locked=False,
        )

    def risk_quote(self, security, closed_at):
        return self.quote_for_code(security.code, closed_at)

    def paper_bar(self, code, closed_at):
        return PaperBar(
            code=code,
            opened_at=closed_at - timedelta(minutes=5),
            closed_at=closed_at,
            open_price=Decimal("10"),
            close_price=Decimal("10"),
            previous_close=Decimal("10"),
        )


class _TestRiskAuthority:
    @contextmanager
    def admission_guard(self, **_bindings):
        yield lambda _ledger_revision: None


class _RejectInsideLedgerRiskAuthority:
    def __init__(self) -> None:
        self.called_revision = None

    @contextmanager
    def admission_guard(self, **_bindings):
        def validate(ledger_revision):
            self.called_revision = ledger_revision
            raise RuntimeError("paper_risk_state_changed")

        yield validate


_TEST_FEE_SCHEDULE = PaperFeeSchedule(
    commission_rate=Decimal("0.0003"),
    minimum_commission=Decimal("5"),
    sell_stamp_duty_rate=Decimal("0.0005"),
    transfer_fee_rate=Decimal("0.00001"),
    regulatory_fee_rate=Decimal("0.0000541"),
    slippage_rate=Decimal("0"),
)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bar(
    opened_at: str,
    *,
    closed_at: str | None = None,
    price: str = "10",
    close_price: str | None = None,
    previous_close: str = "10",
    suspended: bool = False,
    limit_up_locked: bool = False,
    limit_down_locked: bool = False,
    max_fill_shares: int | None = None,
) -> PaperBar:
    opened = ts(opened_at)
    return PaperBar(
        code="SH.600519",
        opened_at=opened,
        closed_at=(
            ts(closed_at)
            if closed_at is not None
            else opened + timedelta(minutes=5)
        ),
        open_price=Decimal(price),
        close_price=Decimal(close_price or price),
        previous_close=Decimal(previous_close),
        suspended=suspended,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        max_fill_shares=max_fill_shares,
    )


def _snapshot(
    event,
    *,
    shares: int = 500,
    evaluated_at=None,
    expires_at=None,
) -> RiskSnapshot:
    evaluated_at = evaluated_at or event.observed_at
    decision = RiskDecision(
        allowed=True,
        shares=shares,
        planned_risk_cash=Decimal("100"),
        target_weight=Decimal("0.05"),
        entry_reference=Decimal("10"),
        reasons=(),
        daily_loss_locked=False,
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


@pytest.fixture
def trusted_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.sqlite3'}")
    for table in (
        TableByDecisionEvent.__table__,
        TableByDecisionTransition.__table__,
        TableByRiskSnapshot.__table__,
        TableByLLMReviewClaim.__table__,
        TableByLLMReview.__table__,
        TableByPaperAdmissionAuthorization.__table__,
    ):
        table.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    coordination_clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    store = DecisionEventStore(factory, clock=coordination_clock)
    store._test_coordination_clock = coordination_clock
    try:
        yield store, factory
    finally:
        engine.dispose()


def _move_to_review_pending(
    store: DecisionEventStore,
    event,
    *,
    manual_record: ManualCheckPending | None = None,
    manual_pending_id: str | None = None,
    manual_payload_fingerprint: str | None = None,
) -> None:
    store.append_event(event)
    store.append_transition(
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        occurred_at=event.observed_at,
        reason="risk_allowed",
        actor="risk_engine",
    )
    store.append_transition(
        event.event_id,
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
        occurred_at=event.observed_at,
        reason=(
            "risk_allowed"
            if manual_record is None
            else (
                "manual_check_approved:"
                f"{manual_pending_id or manual_record.pending_id}:"
                f"{manual_payload_fingerprint or manual_record.payload_fingerprint}"
            )
        ),
        actor=(
            "event_service"
            if manual_record is None
            else "manual_check_workflow"
        ),
    )


def _confirm(
    store: DecisionEventStore,
    event,
    *,
    review_id: str,
    occurred_at,
) -> None:
    store.append_transition(
        event.event_id,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
        occurred_at=occurred_at,
        reason="review_verdict:CONFIRM",
        actor=f"review:{review_id}",
    )


def _insert_validated_review(
    session_factory,
    *,
    event,
    risk_snapshot_id: str,
    review_id: str,
    packet_fingerprint: str,
    created_at,
    reviewed_data_fingerprint: str | None = None,
) -> None:
    payload = {
        "verdict": "CONFIRM",
        "reviewed_event_id": event.event_id,
        "reviewed_data_fingerprint": (
            reviewed_data_fingerprint or event.data_fingerprint
        ),
        "reviewed_packet_fingerprint": packet_fingerprint,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content_bytes = len(content.encode("utf-8"))
    with session_factory() as session:
        session.add(
            TableByLLMReviewClaim(
                review_id=review_id,
                event_id=event.event_id,
                packet_fingerprint=packet_fingerprint,
                provider="fixture",
                model="fixture-model",
                prompt_version="chanlun-review-v3",
                owner_token="fixture-owner",
                fencing_token=1,
                lease_expires_at=created_at + timedelta(hours=1),
                finalized=True,
                created_at=created_at,
            )
        )
        session.flush()
        session.add(
            TableByLLMReview(
                review_id=review_id,
                event_id=event.event_id,
                risk_snapshot_id=risk_snapshot_id,
                packet_fingerprint=packet_fingerprint,
                reviewed_data_fingerprint=(
                    reviewed_data_fingerprint or event.data_fingerprint
                ),
                provider="fixture",
                model="fixture-model",
                prompt_version="chanlun-review-v3",
                fencing_token=1,
                status="validated",
                provider_ok=True,
                verdict="CONFIRM",
                response_content=content,
                response_content_bytes=content_bytes,
                response_content_sha256=_digest(content),
                response_content_truncated=False,
                raw_response=content,
                raw_response_bytes=content_bytes,
                raw_response_sha256=_digest(content),
                raw_response_truncated=False,
                parsed_response_json=content,
                validation_errors_json="[]",
                attempt_count=1,
                latency_ms=1,
                error_code=None,
                error_message=None,
                error_message_bytes=0,
                error_message_sha256=None,
                error_message_truncated=False,
                created_at=created_at,
            )
        )
        session.commit()


def _packet(event, snapshot, base_fingerprint: str) -> EvidencePacket:
    binding = RuleEvidenceBinding(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        supporting_evidence_ids=("original-support",),
        counterevidence_ids=("original-counter",),
        image_ids=(),
    )
    return EvidencePacket(
        event=event,
        risk=snapshot.decision,
        rule_evidence_binding=binding,
        supporting=(),
        counter_evidence=(),
        image_evidence=(),
        reviewable=True,
        blockers=(),
        packet_fingerprint=base_fingerprint,
    )


def _gateway(
    *,
    store: DecisionEventStore,
    ledger: SQLitePaperLedger,
    snapshot: RiskSnapshot,
    base_fingerprint: str,
    now,
    clock=None,
    bar_source=None,
    manual_check_store=None,
    risk_authority_provider=None,
    fee_schedule: PaperFeeSchedule = _TEST_FEE_SCHEDULE,
    buying_power_buffer_rate: Decimal = Decimal("0.01"),
) -> TrustedPaperAdmission:
    test_coordination_clock = getattr(
        store,
        "_test_coordination_clock",
        None,
    )
    if isinstance(test_coordination_clock, _MutableClock):
        test_coordination_clock.current = now
    service = DecisionEventService(store, _ZeroBarClock(), clock=lambda: now)
    return TrustedPaperAdmission(
        service,
        ledger,
        evidence_packet_provider=lambda event, risk: _packet(
            event,
            risk,
            base_fingerprint,
        ),
        fee_schedule=fee_schedule,
        bar_source=bar_source or _TestBarSource(
            _bar("2026-07-13T10:30:00+08:00")
        ),
        manual_check_store=manual_check_store,
        risk_authority_provider=(
            risk_authority_provider or _TestRiskAuthority()
        ),
        clock=clock or (lambda: now),
        buying_power_buffer_rate=buying_power_buffer_rate,
    )


def _manual_record(
    event,
    snapshot: RiskSnapshot,
    *,
    status: str = "approved",
) -> ManualCheckPending:
    check_id = "chart.manual-entry-confirmed"
    evidence_ids = ("original-support",)
    pending = ManualCheckPending(
        event_id=event.event_id,
        event_data_fingerprint=event.data_fingerprint,
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        context_fingerprint=event.data_fingerprint,
        risk_snapshot_id=snapshot.snapshot_id,
        created_at=event.observed_at,
        required_checks=(
            RequiredManualCheck(check_id, evidence_ids, "confirm chart"),
        ),
        runtime_facts=RuleRuntimeFacts(),
    )
    snapshot_record = ManualCheckSnapshot(
        manual_check_id=check_id,
        value=True,
        operator_id="operator.fixture",
        recorded_at=event.observed_at + timedelta(seconds=10),
        event_id=event.event_id,
        context_fingerprint=event.data_fingerprint,
        evidence_ids=evidence_ids,
    )
    outcome = "advanced" if status == "approved" else "validated"
    attempt = ManualCheckAttempt(
        pending_id=pending.pending_id,
        submitted_at=event.observed_at + timedelta(seconds=10),
        snapshots=(snapshot_record,),
        outcome=outcome,
        reasons=(),
        evaluation_verdict=EvaluationVerdict.CONFIRM.value,
        evaluation_input_fingerprint=event.data_fingerprint,
    )
    return replace(pending, status=status, attempts=(attempt,))


def _manual_gateway_case(
    *,
    tmp_path,
    store: DecisionEventStore,
    session_factory,
    event,
    status: str = "approved",
    include_manual_store: bool = True,
    transition_pending_id: str | None = None,
) -> tuple[
    RiskSnapshot,
    TrustedPaperAdmission,
    FileManualCheckStore,
    ManualCheckPending,
]:
    snapshot = _snapshot(event)
    record = _manual_record(event, snapshot, status=status)
    manual_store = FileManualCheckStore(tmp_path / "manual-approvals")
    manual_store.put_if_absent(record)
    base = "sha256:" + "6" * 64
    packet_fingerprint = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(
        store,
        event,
        manual_record=record,
        manual_pending_id=transition_pending_id,
    )
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id="review-manual-approval",
        packet_fingerprint=packet_fingerprint,
        created_at=reviewed_at,
    )
    _confirm(
        store,
        event,
        review_id="review-manual-approval",
        occurred_at=reviewed_at,
    )
    gateway = _gateway(
        store=store,
        ledger=SQLitePaperLedger(
            tmp_path / "manual-paper.sqlite3",
            initial_cash=Decimal("100000"),
        ),
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        manual_check_store=(manual_store if include_manual_store else None),
    )
    return snapshot, gateway, manual_store, record


def _confirmed_gateway(
    *,
    store: DecisionEventStore,
    session_factory,
    event,
    ledger: SQLitePaperLedger,
    now,
    clock=None,
    signal_bar: PaperBar | None = None,
    bar_source=None,
) -> tuple[RiskSnapshot, TrustedPaperAdmission]:
    snapshot = _snapshot(event)
    base = "sha256:" + "9" * 64
    packet_fingerprint = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-market-integrity:" + event.event_id
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=packet_fingerprint,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=now,
        clock=clock,
        bar_source=bar_source,
    )
    gateway.admit(
        event.event_id,
        signal_bar or _bar("2026-07-13T10:30:00+08:00"),
        risk_snapshot_id=snapshot.snapshot_id,
    )
    return snapshot, gateway


def test_trusted_gateway_requires_explicit_fee_schedule() -> None:
    parameter = inspect.signature(TrustedPaperAdmission).parameters.get(
        "fee_schedule"
    )

    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_trusted_gateway_requires_explicit_bar_source() -> None:
    parameter = inspect.signature(TrustedPaperAdmission).parameters.get(
        "bar_source"
    )

    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_trusted_gateway_requires_explicit_paper_risk_authority() -> None:
    parameter = inspect.signature(TrustedPaperAdmission).parameters.get(
        "risk_authority_provider"
    )

    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


def test_trusted_gateway_accepts_exact_approved_manual_authorization(
    tmp_path,
    trusted_store,
    make_bound_decision_event,
) -> None:
    store, factory = trusted_store
    event = make_bound_decision_event()
    snapshot, gateway, _manual_store, record = _manual_gateway_case(
        tmp_path=tmp_path,
        store=store,
        session_factory=factory,
        event=event,
    )

    intent = gateway.admit(
        event.event_id,
        _bar("2026-07-13T10:30:00+08:00"),
        risk_snapshot_id=snapshot.snapshot_id,
    )
    authorization = store.get_paper_admission_authorization(event.event_id)

    assert intent.event_id == event.event_id
    assert authorization is not None
    assert authorization.manual_check_pending_id == record.pending_id
    assert (
        authorization.manual_check_payload_fingerprint
        == record.payload_fingerprint
    )


def test_trusted_gateway_rejects_manual_authorization_without_approval_store(
    tmp_path,
    trusted_store,
    make_bound_decision_event,
) -> None:
    store, factory = trusted_store
    event = make_bound_decision_event()
    snapshot, gateway, _manual_store, _record = _manual_gateway_case(
        tmp_path=tmp_path,
        store=store,
        session_factory=factory,
        event=event,
        include_manual_store=False,
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="manual_check_store_required",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )


def test_trusted_gateway_rejects_manual_authorization_bound_to_pending_record(
    tmp_path,
    trusted_store,
    make_bound_decision_event,
) -> None:
    store, factory = trusted_store
    event = make_bound_decision_event()
    snapshot, gateway, _manual_store, _record = _manual_gateway_case(
        tmp_path=tmp_path,
        store=store,
        session_factory=factory,
        event=event,
        status="pending",
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="manual_check_approval_not_approved",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )


def test_trusted_gateway_rejects_forged_manual_approval_identity(
    tmp_path,
    trusted_store,
    make_bound_decision_event,
) -> None:
    store, factory = trusted_store
    event = make_bound_decision_event()
    snapshot, gateway, _manual_store, _record = _manual_gateway_case(
        tmp_path=tmp_path,
        store=store,
        session_factory=factory,
        event=event,
        transition_pending_id="manual-pending:" + "f" * 64,
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="manual_check_approval_binding_mismatch",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )


def test_trusted_gateway_rejects_tampered_manual_approval_payload(
    tmp_path,
    trusted_store,
    make_bound_decision_event,
) -> None:
    store, factory = trusted_store
    event = make_bound_decision_event()
    snapshot, gateway, manual_store, record = _manual_gateway_case(
        tmp_path=tmp_path,
        store=store,
        session_factory=factory,
        event=event,
    )
    path = manual_store.root / f"{record.pending_id[15:]}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "pending"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="manual_check_approval_unavailable",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )


def test_sqlite_cash_is_rounded_to_cents_at_the_boundary(tmp_path) -> None:
    ledger = SQLitePaperLedger(
        tmp_path / "cash-rounding.sqlite3",
        initial_cash=Decimal("100.005"),
    )

    account = ledger.account_snapshot()

    assert account.initial_cash == Decimal("100.01")
    assert account.cash_balance == Decimal("100.01")
    assert account.available_buying_power == Decimal("100.01")


def test_trusted_cycle_rejects_future_unclosed_and_non_session_bars(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    future_bar = _bar("2026-07-13T11:35:00+08:00")
    outside_session_bar = _bar("2026-07-13T12:00:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        future_bar,
        outside_session_bar,
    )
    ledger = SQLitePaperLedger(
        tmp_path / "trusted-cycle.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=clock.current,
        clock=clock,
        bar_source=source,
    )

    with pytest.raises(TrustedPaperAdmissionError, match="paper_bar_not_closed"):
        gateway.process_bar(future_bar)

    clock.current = ts("2026-07-13T12:10:00+08:00")
    with pytest.raises(
        TrustedPaperAdmissionError,
        match="paper_bar_outside_a_share_session",
    ):
        gateway.process_bar(outside_session_bar)

    assert ledger.load().fills == ()
    assert ledger.load().processed_bar_ids == ()


def test_admission_rejects_future_signal_bar_before_reserving_cash(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    now = ts("2026-07-13T10:36:00+08:00")
    ledger = SQLitePaperLedger(
        tmp_path / "future-signal.sqlite3",
        initial_cash=Decimal("100000"),
    )
    future_signal = _bar("2026-07-13T11:35:00+08:00")

    with pytest.raises(TrustedPaperAdmissionError, match="paper_bar_not_closed"):
        _confirmed_gateway(
            store=store,
            session_factory=session_factory,
            event=event,
            ledger=ledger,
            now=now,
            signal_bar=future_signal,
            bar_source=_TestBarSource(future_signal),
        )

    assert ledger.load().intents == ()
    assert ledger.account_snapshot().reserved_buying_power == Decimal("0")


def test_trusted_gateway_direct_mutations_hold_strategy_run_lease() -> None:
    active: list[str] = []
    entered: list[str] = []

    class StrategyRun:
        @staticmethod
        def status_payload():
            return {
                "state": "active",
                "evidence_scope": "current_epoch_only",
                "store_bindings_complete": True,
            }

        @contextmanager
        def mutation_lease(self, operation: str):
            active.append(operation)
            entered.append(operation)
            try:
                yield object()
            finally:
                assert active.pop() == operation

    class StopMutation(Exception):
        pass

    gateway = object.__new__(TrustedPaperAdmission)
    gateway._strategy_run = StrategyRun()

    def attest(bar, **_kwargs):
        assert active in (
            ["paper_admission.process_bar"],
            ["paper_admission.admit"],
        )
        raise StopMutation

    gateway._attest_bar = attest
    bar = _bar("2025-01-06T09:30:00+08:00")

    with pytest.raises(StopMutation):
        gateway.process_bar(bar)
    with pytest.raises(StopMutation):
        gateway.admit("event-fixture", bar, risk_snapshot_id="risk-fixture")

    assert active == []
    assert entered == [
        "paper_admission.process_bar",
        "paper_admission.admit",
    ]


def test_public_process_bar_rejects_bar_missing_from_trusted_source(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    now = clock.current
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    source = _TestBarSource(signal_bar)
    ledger = SQLitePaperLedger(
        tmp_path / "bar-attestation.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=now,
        clock=clock,
        bar_source=source,
    )
    candidate = _bar("2026-07-13T10:40:00+08:00")

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_not_attested",
    ):
        gateway.process_bar(candidate)

    source.register(candidate)
    clock.current = ts("2026-07-13T10:50:00+08:00")
    assert len(gateway.process_bar(candidate)) == 1
    assert ledger.load().processed_bar_ids == (candidate.bar_id,)


def test_degraded_sqlite_trusted_bar_store_blocks_new_direct_admission(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "7" * 64
    packet_fingerprint = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-degraded-bar-source"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=packet_fingerprint,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "degraded-direct-admission-bars.sqlite3"
    )
    bar_store.put(signal_bar)
    with pytest.raises(TrustedPaperBarIntegrityError):
        bar_store.put(replace(signal_bar, open_price=Decimal("10.01")))
    assert bar_store.health().degraded is True
    assert bar_store.get_bar(signal_bar.bar_id) == signal_bar
    ledger = SQLitePaperLedger(
        tmp_path / "degraded-direct-admission-ledger.sqlite3",
        initial_cash=Decimal("100000"),
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        bar_source=bar_store,
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_degraded",
    ):
        gateway.admit(
            event.event_id,
            signal_bar,
            risk_snapshot_id=snapshot.snapshot_id,
        )

    assert ledger.load().intents == ()


def test_incomplete_trusted_bar_attempt_blocks_direct_gateway_path() -> None:
    gateway = object.__new__(TrustedPaperAdmission)
    gateway._bar_source = SimpleNamespace(
        health=lambda: SimpleNamespace(
            degraded=False,
            last_attempted_bar_closed_at=ts("2026-07-13T10:40:00+08:00"),
            last_attempt_complete=False,
        )
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_cycle_incomplete",
    ):
        gateway._validate_bar_source_health()


def test_bar_source_without_cycle_attestation_api_fails_closed() -> None:
    bar = _bar("2026-07-13T10:30:00+08:00")
    gateway = object.__new__(TrustedPaperAdmission)
    gateway._bar_source = SimpleNamespace(
        health=lambda: SimpleNamespace(
            degraded=False,
            last_attempted_bar_closed_at=bar.closed_at,
            last_attempt_complete=True,
            last_attempt_failure=None,
        ),
        get_bar=lambda bar_id: bar if bar_id == bar.bar_id else None,
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_cycle_attestation_unavailable",
    ):
        gateway._attest_bar(bar)


def test_only_current_started_attempt_is_attestable_for_process_bar() -> None:
    bar = _bar("2026-07-13T10:40:00+08:00")
    health = SimpleNamespace(
        degraded=False,
        last_attempted_bar_closed_at=bar.closed_at,
        last_attempt_complete=False,
        last_attempt_failure=None,
    )
    gateway = object.__new__(TrustedPaperAdmission)
    gateway._bar_source = SimpleNamespace(
        health=lambda: health,
        get_bar=lambda bar_id: bar if bar_id == bar.bar_id else None,
        attest_cycle_bar=lambda bar_id, **_kwargs: (
            bar if bar_id == bar.bar_id else None
        ),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_cycle_incomplete",
    ):
        gateway._attest_bar(bar)
    assert gateway._attest_bar(
        bar,
        allow_current_started=True,
    ) == bar
    health.last_attempt_failure = "TrustedPaperBarIntegrityError"
    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_cycle_incomplete",
    ):
        gateway._attest_bar(bar, allow_current_started=True)


def test_calendar_preflight_failure_blocks_direct_admission_but_allows_recovery_bar(
) -> None:
    bar = _bar("2026-07-13T10:40:00+08:00")
    health = SimpleNamespace(
        degraded=False,
        last_attempted_bar_closed_at=ts("2026-07-13T10:35:00+08:00"),
        last_attempt_complete=True,
        last_attempt_failure=None,
        calendar_preflight_failure_at=bar.closed_at,
        calendar_preflight_failure="paper_calendar_date_out_of_coverage",
    )
    gateway = object.__new__(TrustedPaperAdmission)
    gateway._bar_source = SimpleNamespace(
        health=lambda: health,
        get_bar=lambda bar_id: bar if bar_id == bar.bar_id else None,
        attest_cycle_bar=lambda bar_id, **_kwargs: (
            bar if bar_id == bar.bar_id else None
        ),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_calendar_preflight_failed",
    ):
        gateway._attest_bar(bar)

    health.last_attempted_bar_closed_at = bar.closed_at
    health.last_attempt_complete = False
    assert gateway._attest_bar(
        bar,
        allow_current_started=True,
    ) == bar


def test_untracked_put_plus_started_attempt_cannot_fill(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    calendar = ExplicitPaperTradingCalendar(
        (date(2026, 7, 13),),
        source_id="fixture-a-share-calendar",
        source_fingerprint="sha256:" + "b" * 64,
    )
    session = calendar.session_for(date(2026, 7, 13))
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    bridge = _bar("2026-07-13T10:35:00+08:00")
    candidate = _bar("2026-07-13T10:40:00+08:00")
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "untracked-started-bars.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    bar_store.record_cycle(
        session=session,
        bar_closed_at=signal_bar.closed_at,
        required_codes=(signal_bar.code,),
        optional_codes=(),
        bars={signal_bar.code: signal_bar},
        optional_failures={},
    )
    bar_store.complete_cycle(signal_bar.closed_at)
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    ledger = SQLitePaperLedger(
        tmp_path / "untracked-started-ledger.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=clock.current,
        clock=clock,
        signal_bar=signal_bar,
        bar_source=bar_store,
    )
    bar_store.record_cycle(
        session=session,
        bar_closed_at=bridge.closed_at,
        required_codes=(bridge.code,),
        optional_codes=(),
        bars={bridge.code: bridge},
        optional_failures={},
    )
    bar_store.complete_cycle(bridge.closed_at)
    bar_store.put(candidate)
    bar_store.start_cycle_attempt(
        session=session,
        bar_closed_at=candidate.closed_at,
    )
    clock.current = ts("2026-07-13T10:50:00+08:00")

    with pytest.raises(TrustedPaperAdmissionError):
        gateway.process_bar(candidate)

    assert ledger.load().fills == ()
    assert ledger.load().processed_bar_ids == ()


def test_completed_signal_and_current_recorded_cycle_are_cycle_attested(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    calendar = ExplicitPaperTradingCalendar(
        (date(2026, 7, 13),),
        source_id="fixture-a-share-calendar",
        source_fingerprint="sha256:" + "c" * 64,
    )
    session = calendar.session_for(date(2026, 7, 13))
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    bridge = _bar("2026-07-13T10:35:00+08:00")
    candidate = _bar("2026-07-13T10:40:00+08:00")
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "cycle-attested-bars.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    bar_store.record_cycle(
        session=session,
        bar_closed_at=signal_bar.closed_at,
        required_codes=(signal_bar.code,),
        optional_codes=(),
        bars={signal_bar.code: signal_bar},
        optional_failures={},
    )
    bar_store.complete_cycle(signal_bar.closed_at)
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    ledger = SQLitePaperLedger(
        tmp_path / "cycle-attested-ledger.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=clock.current,
        clock=clock,
        signal_bar=signal_bar,
        bar_source=bar_store,
    )
    assert len(ledger.load().intents) == 1
    bar_store.record_cycle(
        session=session,
        bar_closed_at=bridge.closed_at,
        required_codes=(bridge.code,),
        optional_codes=(),
        bars={bridge.code: bridge},
        optional_failures={},
    )
    bar_store.complete_cycle(bridge.closed_at)
    bar_store.record_cycle(
        session=session,
        bar_closed_at=candidate.closed_at,
        required_codes=(candidate.code,),
        optional_codes=(),
        bars={candidate.code: candidate},
        optional_failures={},
    )
    clock.current = ts("2026-07-13T10:50:00+08:00")

    fills = gateway.process_bar(candidate)

    assert len(fills) == 1
    assert ledger.load().processed_bar_ids == (candidate.bar_id,)


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    (
        ("invalidated", "cancelled_authoritative_invalidation"),
        ("superseded", "cancelled_risk_snapshot_superseded"),
    ),
)
def test_pending_buy_is_cancelled_when_authority_changes_before_fill(
    mode,
    expected_status,
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    next_bar = _bar("2026-07-13T10:40:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        next_bar,
    )
    ledger = SQLitePaperLedger(
        tmp_path / f"pending-authority-{mode}.sqlite3",
        initial_cash=Decimal("100000"),
    )
    snapshot, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=clock.current,
        clock=clock,
        bar_source=source,
    )
    if mode == "invalidated":
        store.append_transition(
            event.event_id,
            EventState.CONFIRMED,
            EventState.INVALIDATED,
            occurred_at=clock.current + timedelta(minutes=1),
            reason="structure_invalidated_before_paper_fill",
            actor="scanner",
        )
    else:
        store.append_risk_snapshot(
            _snapshot(
                event,
                shares=600,
                evaluated_at=snapshot.evaluated_at + timedelta(minutes=1),
            )
        )
    clock.current = ts("2026-07-13T10:50:00+08:00")

    assert gateway.process_bar(next_bar) == ()
    state = ledger.load()
    assert state.fills == ()
    assert state.lots == ()
    assert state.intents[0].status == expected_status
    assert ledger.account_snapshot().reserved_buying_power == Decimal("0")


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    (
        ("invalidated", "cancelled_authoritative_invalidation"),
        ("superseded", "cancelled_risk_snapshot_superseded"),
    ),
)
def test_buy_authority_is_rechecked_after_staging_before_bar_commit(
    mode,
    expected_status,
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))

    class AuthorityChangingLedger(SQLitePaperLedger):
        changed = False

        def _commit_trusted_bar(self, **kwargs) -> None:
            if not self.changed and any(
                fill.side == "buy" for fill in kwargs["state"].fills
            ):
                self.changed = True
                if mode == "invalidated":
                    store.append_transition(
                        event.event_id,
                        EventState.CONFIRMED,
                        EventState.INVALIDATED,
                        occurred_at=clock.current,
                        reason="test_invalidation_at_paper_commit_boundary",
                        actor="scanner",
                    )
                else:
                    store.append_risk_snapshot(
                        _snapshot(
                            event,
                            shares=600,
                            evaluated_at=clock.current,
                        )
                    )
            super()._commit_trusted_bar(**kwargs)

    next_bar = _bar("2026-07-13T10:40:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        next_bar,
    )
    ledger = AuthorityChangingLedger(
        tmp_path / f"commit-authority-{mode}.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=clock.current,
        clock=clock,
        bar_source=source,
    )
    clock.current = ts("2026-07-13T10:50:00+08:00")

    assert gateway.process_bar(next_bar) == ()
    state = ledger.load()
    assert state.fills == ()
    assert state.lots == ()
    assert state.intents[0].status == expected_status
    assert state.processed_bar_ids == (next_bar.bar_id,)


def test_trusted_bar_cursor_survives_restart_and_binds_payload(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    now = clock.current
    path = tmp_path / "bar-cursor.sqlite3"
    ledger = SQLitePaperLedger(path, initial_cash=Decimal("100000"))
    latest = _bar("2026-07-13T10:40:00+08:00", price="10.10")
    changed = _bar("2026-07-13T10:40:00+08:00", price="10.11")
    older = _bar("2026-07-13T10:35:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        latest,
        changed,
        older,
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=now,
        clock=clock,
        bar_source=source,
    )

    clock.current = ts("2026-07-13T10:50:00+08:00")
    fills = gateway.process_bar(latest)

    assert len(fills) == 1
    assert gateway.process_bar(latest) == ()
    with pytest.raises(PaperAdapterConflictError, match="paper_bar_payload_conflict"):
        gateway.process_bar(changed)

    restarted_ledger = SQLitePaperLedger(path, initial_cash=Decimal("100000"))
    restarted = _gateway(
        store=store,
        ledger=restarted_ledger,
        snapshot=_snapshot(event),
        base_fingerprint="sha256:" + "9" * 64,
        now=clock.current,
        clock=clock,
        bar_source=source,
    )
    with pytest.raises(PaperAdapterConflictError, match="paper_bar_out_of_order"):
        restarted.process_bar(older)


def test_sqlite_ledger_rejects_legacy_adapter_bar_commit(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    now = clock.current
    ledger = SQLitePaperLedger(
        tmp_path / "legacy-bar-bypass.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=now,
    )
    assert not hasattr(gateway, "adapter")
    bypass = ConfirmedEventPaperAdapter(
        ledger,
        fee_schedule=_TEST_FEE_SCHEDULE,
    )

    with pytest.raises(
        PaperLedgerIntegrityError,
        match="trusted_paper_commit_capability_required",
    ):
        bypass.on_bar(_bar("2026-07-13T10:40:00+08:00"))

    assert ledger.load().fills == ()
    assert ledger.load().processed_bar_ids == ()


@pytest.mark.parametrize("conflicts", (1, 3))
def test_trusted_bar_commit_retries_are_bounded(
    conflicts,
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    class ConflictLedger(SQLitePaperLedger):
        def __init__(self, *args, **kwargs) -> None:
            self.attempts = 0
            super().__init__(*args, **kwargs)

        def _commit_trusted_bar(self, **kwargs) -> None:
            self.attempts += 1
            if self.attempts <= conflicts:
                raise PaperLedgerConflictError("injected paper ledger revision conflict")
            super()._commit_trusted_bar(**kwargs)

    store, session_factory = trusted_store
    event = make_bound_decision_event()
    clock = _MutableClock(ts("2026-07-13T10:36:00+08:00"))
    now = clock.current
    ledger = ConflictLedger(
        tmp_path / f"bounded-retry-{conflicts}.sqlite3",
        initial_cash=Decimal("100000"),
    )
    bar = _bar("2026-07-13T10:40:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        bar,
    )
    _, gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=now,
        clock=clock,
        bar_source=source,
    )

    clock.current = ts("2026-07-13T10:50:00+08:00")
    if conflicts == 1:
        assert len(gateway.process_bar(bar)) == 1
        assert ledger.attempts == 2
    else:
        with pytest.raises(PaperLedgerConflictError):
            gateway.process_bar(bar)
        assert ledger.attempts == 3
        assert ledger.load().processed_bar_ids == ()


def test_forged_confirmation_actor_without_stored_review_is_rejected(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, _ = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _confirm(
        store,
        event,
        review_id="forged-review-id",
        occurred_at=event.observed_at + timedelta(seconds=30),
    )
    ledger = SQLitePaperLedger(
        tmp_path / "paper.sqlite3",
        initial_cash=Decimal("100000"),
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint="sha256:" + "a" * 64,
        now=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_review_missing",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )

    assert ledger.load().intents == ()


def test_wrong_review_packet_binding_is_rejected(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    review_id = "review-wrong-binding"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint="sha256:" + "b" * 64,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger = SQLitePaperLedger(
        tmp_path / "paper.sqlite3",
        initial_cash=Decimal("100000"),
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint="sha256:" + "a" * 64,
        now=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="review_packet_binding_mismatch",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )

    assert ledger.load().intents == ()


def test_review_pending_event_cannot_be_admitted_even_with_validated_review(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "a" * 64
    packet = _packet(event, snapshot, base)
    bound = bind_risk_snapshot_packet_fingerprint(packet, snapshot)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id="review-not-applied",
        packet_fingerprint=bound,
        created_at=event.observed_at + timedelta(seconds=30),
    )
    ledger = SQLitePaperLedger(
        tmp_path / "paper.sqlite3",
        initial_cash=Decimal("100000"),
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="event_state_not_confirmed",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )

    assert ledger.load().intents == ()


def test_successful_admission_and_fill_survive_process_restart(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "a" * 64
    packet = _packet(event, snapshot, base)
    bound = bind_risk_snapshot_packet_fingerprint(packet, snapshot)
    review_id = "review-trusted"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger_path = tmp_path / "paper.sqlite3"
    ledger = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    clock = _MutableClock(event.observed_at + timedelta(minutes=1))
    now = clock.current
    fill_bar = _bar("2026-07-13T10:40:00+08:00", price="10.20")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        fill_bar,
    )
    first_gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=now,
        clock=clock,
        bar_source=source,
    )

    first = first_gateway.admit(
        event.event_id,
        _bar("2026-07-13T10:30:00+08:00"),
        risk_snapshot_id=snapshot.snapshot_id,
    )

    restarted_ledger = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    restarted_gateway = _gateway(
        store=store,
        ledger=restarted_ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=now,
        clock=clock,
        bar_source=source,
    )
    retried = restarted_gateway.admit(
        event.event_id,
        _bar("2026-07-13T10:30:00+08:00"),
        risk_snapshot_id=snapshot.snapshot_id,
    )
    clock.current = ts("2026-07-13T10:45:00+08:00")
    fills = restarted_gateway.process_bar(fill_bar)
    after_fill = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    state = after_fill.load()
    account = after_fill.account_snapshot()
    authorization = store.get_paper_admission_authorization(event.event_id)

    assert retried == first
    assert authorization is not None
    assert len(state.intents) == 1
    assert (
        state.intents[0].admission_authorization_id
        == authorization.authorization_id
    )
    assert (
        state.intents[0].admission_payload_fingerprint
        == authorization.payload_fingerprint
    )
    assert state.intents[0].admitted_at == now
    assert len(state.fills) == 1
    assert len(state.lots) == 1
    assert fills[0] == state.fills[0]
    assert account.cash_balance == Decimal("94894.67")
    assert account.positions_cost > Decimal("0")
    assert account.available_buying_power == account.cash_balance


def test_outbox_crash_recovery_uses_inbox_time_for_not_before_and_replay(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
    monkeypatch,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "7" * 64
    packet_fingerprint = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-outbox-crash-recovery"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=packet_fingerprint,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)

    ledger_path = tmp_path / "outbox-crash-recovery.sqlite3"
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    pre_recovery_bar = _bar("2026-07-13T10:40:00+08:00", price="10.20")
    bar_source = _TestBarSource(signal_bar, pre_recovery_bar)
    authorization_time = ts("2026-07-13T10:36:00+08:00")
    authorization_clock = _MutableClock(authorization_time)
    first_ledger = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    first_gateway = _gateway(
        store=store,
        ledger=first_ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=authorization_time,
        clock=authorization_clock,
        bar_source=bar_source,
    )

    def crash_before_inbox_commit(**_kwargs) -> None:
        raise RuntimeError("simulated crash before inbox commit")

    monkeypatch.setattr(
        first_ledger,
        "_commit_trusted_admission",
        crash_before_inbox_commit,
    )
    with pytest.raises(RuntimeError, match="simulated crash before inbox commit"):
        first_gateway.admit(
            event.event_id,
            signal_bar,
            risk_snapshot_id=snapshot.snapshot_id,
        )

    authorization = store.get_paper_admission_authorization(event.event_id)
    assert authorization is not None
    assert authorization.authorized_at == authorization_time
    assert first_ledger.load().intents == ()

    recovery_time = ts("2026-07-13T10:46:00+08:00")
    recovery_clock = _MutableClock(recovery_time)
    recovered_ledger = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    recovered_gateway = _gateway(
        store=store,
        ledger=recovered_ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=recovery_time,
        clock=recovery_clock,
        bar_source=bar_source,
    )
    recovered = recovered_gateway.admit(
        event.event_id,
        signal_bar,
        risk_snapshot_id=snapshot.snapshot_id,
    )

    assert recovered.admitted_at == recovery_time
    assert recovered.admitted_at > authorization.authorized_at
    with sqlite3.connect(recovered_ledger.path) as connection:
        inbox_created_at = connection.execute(
            "SELECT created_at FROM paper_trusted_admission WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0]
    assert inbox_created_at == recovery_time.isoformat()

    recovery_clock.current = ts("2026-07-13T10:51:00+08:00")
    replayed = recovered_gateway.admit(
        event.event_id,
        signal_bar,
        risk_snapshot_id=snapshot.snapshot_id,
    )
    assert replayed == recovered
    assert recovered_ledger.load().intents == (recovered,)

    assert recovered_gateway.process_bar(pre_recovery_bar) == ()
    state = recovered_ledger.load()
    assert state.fills == ()
    assert state.intents[0].admitted_at == recovery_time


def test_trusted_intent_persists_execution_policy_fingerprints(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    ledger = SQLitePaperLedger(
        tmp_path / "policy-fingerprint.sqlite3",
        initial_cash=Decimal("100000"),
    )
    _, gateway_instance = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=ledger,
        now=event.observed_at + timedelta(minutes=1),
    )

    intent = ledger.load().intents[0]
    with sqlite3.connect(ledger.path) as connection:
        persisted = connection.execute(
            """
            SELECT fee_schedule_fingerprint, execution_policy_fingerprint
            FROM paper_execution_policy
            WHERE singleton_id = 1
            """
        ).fetchone()

    assert intent.fee_schedule_fingerprint == _TEST_FEE_SCHEDULE.fingerprint
    assert intent.execution_policy_fingerprint.startswith("sha256:")
    assert (
        gateway_instance.fee_schedule_fingerprint
        == intent.fee_schedule_fingerprint
    )
    assert (
        gateway_instance.execution_policy_fingerprint
        == intent.execution_policy_fingerprint
    )
    assert persisted == (
        intent.fee_schedule_fingerprint,
        intent.execution_policy_fingerprint,
    )


def test_execution_policy_declares_limit_clamp_and_rejects_legacy_close_policy(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
    monkeypatch,
) -> None:
    document, fingerprint = paper_admission_module._execution_policy_document(
        _TEST_FEE_SCHEDULE,
        Decimal("0.01"),
    )

    assert document["algorithm_version"] == (
        "paper-next-tradable-bar-close-limit-v3"
    )
    assert document["fill_algorithm"] == (
        "completed-next-tradable-bar-close-after-admission-"
        "with-daily-price-limit-clamp"
    )

    store, session_factory = trusted_store
    event = make_bound_decision_event()
    path = tmp_path / "reject-old-open-policy.sqlite3"
    first_ledger = SQLitePaperLedger(path, initial_cash=Decimal("100000"))
    snapshot, _gateway_instance = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=first_ledger,
        now=event.observed_at + timedelta(minutes=1),
    )
    assert first_ledger.load().intents[0].execution_policy_fingerprint == fingerprint

    monkeypatch.setattr(
        paper_admission_module,
        "_PAPER_EXECUTION_ALGORITHM_VERSION",
        "paper-next-tradable-bar-close-v2",
    )
    restarted = SQLitePaperLedger(path, initial_cash=Decimal("100000"))
    with pytest.raises(
        PaperLedgerIntegrityError,
        match="paper_execution_policy_mismatch",
    ):
        _gateway(
            store=store,
            ledger=restarted,
            snapshot=snapshot,
            base_fingerprint="sha256:" + "9" * 64,
            now=event.observed_at + timedelta(minutes=1),
            fee_schedule=_TEST_FEE_SCHEDULE,
            buying_power_buffer_rate=Decimal("0.01"),
        )


def test_paper_admission_rejects_second_open_entry_for_same_code(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    ledger = SQLitePaperLedger(
        tmp_path / "single-entry-per-code.sqlite3",
        initial_cash=Decimal("100000"),
    )
    first_event = make_bound_decision_event()
    first_bar = _bar("2026-07-13T10:30:00+08:00")
    second_event = make_bound_decision_event(
        observed_at=ts("2026-07-13T10:40:00+08:00"),
    )
    second_bar = _bar("2026-07-13T10:35:00+08:00")
    source = _TestBarSource(first_bar, second_bar)
    _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=first_event,
        ledger=ledger,
        now=ts("2026-07-13T10:36:00+08:00"),
        signal_bar=first_bar,
        bar_source=source,
    )

    with pytest.raises(
        PaperAdapterEligibilityError,
        match="paper_entry_already_open",
    ):
        _confirmed_gateway(
            store=store,
            session_factory=session_factory,
            event=second_event,
            ledger=ledger,
            now=ts("2026-07-13T10:41:00+08:00"),
            signal_bar=second_bar,
            bar_source=source,
        )

    state = ledger.load()
    assert [intent.event_id for intent in state.intents] == [
        first_event.event_id
    ]
    assert ledger.account_snapshot().reserved_buying_power > Decimal("0")


@pytest.mark.parametrize(
    "changed_component",
    ("fee_rate", "price_rounding", "slippage", "buffer", "algorithm_version"),
)
def test_pending_intent_restart_rejects_changed_execution_policy(
    changed_component,
    trusted_store,
    make_bound_decision_event,
    tmp_path,
    monkeypatch,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    path = tmp_path / f"changed-policy-{changed_component}.sqlite3"
    first_ledger = SQLitePaperLedger(path, initial_cash=Decimal("100000"))
    snapshot, _first_gateway = _confirmed_gateway(
        store=store,
        session_factory=session_factory,
        event=event,
        ledger=first_ledger,
        now=event.observed_at + timedelta(minutes=1),
    )
    changed_schedule = _TEST_FEE_SCHEDULE
    changed_buffer = Decimal("0.01")
    if changed_component == "fee_rate":
        changed_schedule = replace(
            _TEST_FEE_SCHEDULE,
            commission_rate=Decimal("0.00031"),
        )
    elif changed_component == "price_rounding":
        changed_schedule = replace(
            _TEST_FEE_SCHEDULE,
            price_quantum=Decimal("0.001"),
        )
    elif changed_component == "slippage":
        changed_schedule = replace(
            _TEST_FEE_SCHEDULE,
            slippage_rate=Decimal("0.0001"),
        )
    elif changed_component == "buffer":
        changed_buffer = Decimal("0.02")
    else:
        monkeypatch.setattr(
            paper_admission_module,
            "_PAPER_EXECUTION_ALGORITHM_VERSION",
            "paper-next-bar-open-v999",
            raising=False,
        )
    restarted = SQLitePaperLedger(path, initial_cash=Decimal("100000"))

    with pytest.raises(
        PaperLedgerIntegrityError,
        match="paper_execution_policy_mismatch",
    ):
        _gateway(
            store=store,
            ledger=restarted,
            snapshot=snapshot,
            base_fingerprint="sha256:" + "9" * 64,
            now=event.observed_at + timedelta(minutes=1),
            fee_schedule=changed_schedule,
            buying_power_buffer_rate=changed_buffer,
        )


def test_trusted_admission_uses_one_atomic_ledger_write_path(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "2" * 64
    bound = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-atomic-admission"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger = SQLitePaperLedger(
        tmp_path / "atomic-admission.sqlite3",
        initial_cash=Decimal("100000"),
    )
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        bar_source=_TestBarSource(signal_bar),
    )

    assert not hasattr(ledger, "reserve_buying_power")
    assert not hasattr(ledger, "_authorize_trusted_admission")

    intent = gateway.admit(
        event.event_id,
        signal_bar,
        risk_snapshot_id=snapshot.snapshot_id,
    )
    state = SQLitePaperLedger(
        ledger.path,
        initial_cash=Decimal("100000"),
    ).load()

    assert state.intents == (intent,)
    assert state.revision == 1


def test_trusted_admission_rechecks_paper_account_revision_before_final_cas(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "a" * 64
    bound = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-paper-risk-race"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger = SQLitePaperLedger(
        tmp_path / "paper-risk-race-ledger.sqlite3",
        initial_cash=Decimal("100000"),
    )
    authority = PaperRiskAccountProvider(
        data_provider=_PaperRiskQuotes(),
        ledger=ledger,
        risk_state=SQLitePaperRiskState(
            tmp_path / "paper-risk-race-state.sqlite3",
            policy=RiskPolicy.conservative(),
        ),
    )
    authority(event, event, event.observed_at)
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        bar_source=_TestBarSource(signal_bar),
        risk_authority_provider=authority,
    )
    state = ledger.load()
    ledger.commit(
        expected_revision=state.revision,
        state=replace(state, revision=state.revision + 1),
        _capability=ledger._trusted_bar_capability(),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="paper_risk_ledger_revision_changed",
    ):
        gateway.admit(
            event.event_id,
            signal_bar,
            risk_snapshot_id=snapshot.snapshot_id,
        )

    assert ledger.load().intents == ()


def test_final_risk_validator_failure_rolls_back_authorization_and_intent(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "b" * 64
    bound = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id="review-final-risk-rollback",
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(
        store,
        event,
        review_id="review-final-risk-rollback",
        occurred_at=reviewed_at,
    )
    ledger = SQLitePaperLedger(
        tmp_path / "final-risk-rollback.sqlite3",
        initial_cash=Decimal("100000"),
    )
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    authority = _RejectInsideLedgerRiskAuthority()
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        bar_source=_TestBarSource(signal_bar),
        risk_authority_provider=authority,
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="paper_risk_state_changed",
    ):
        gateway.admit(
            event.event_id,
            signal_bar,
            risk_snapshot_id=snapshot.snapshot_id,
        )

    with sqlite3.connect(ledger.path) as connection:
        paper_authorizations = connection.execute(
            "SELECT COUNT(*) FROM paper_trusted_admission"
        ).fetchone()[0]
        reservations = connection.execute(
            "SELECT COUNT(*) FROM paper_buying_power_reservation"
        ).fetchone()[0]
        revision = connection.execute(
            "SELECT revision FROM paper_ledger WHERE singleton_id = 1"
        ).fetchone()[0]
    assert authority.called_revision == 0
    assert paper_authorizations == 0
    assert reservations == 0
    assert revision == 0
    assert ledger.load().intents == ()


def test_trusted_admission_rolls_back_inbox_reservation_and_intent_together(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "3" * 64
    bound = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-atomic-rollback"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger = SQLitePaperLedger(
        tmp_path / "atomic-rollback.sqlite3",
        initial_cash=Decimal("100000"),
    )
    signal_bar = _bar("2026-07-13T10:30:00+08:00")
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
        bar_source=_TestBarSource(signal_bar),
    )
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER force_atomic_admission_rollback
            BEFORE UPDATE ON paper_ledger
            BEGIN
                SELECT RAISE(ABORT, 'forced ledger update failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced ledger update failure"):
        gateway.admit(
            event.event_id,
            signal_bar,
            risk_snapshot_id=snapshot.snapshot_id,
        )

    with sqlite3.connect(ledger.path) as connection:
        revision = connection.execute(
            "SELECT revision FROM paper_ledger WHERE singleton_id = 1"
        ).fetchone()[0]
        authorization_count = connection.execute(
            "SELECT COUNT(*) FROM paper_trusted_admission"
        ).fetchone()[0]
        reservation_count = connection.execute(
            "SELECT COUNT(*) FROM paper_buying_power_reservation"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER force_atomic_admission_rollback")

    assert revision == 0
    assert authorization_count == 0
    assert reservation_count == 0
    assert ledger.load().intents == ()

    recovered = gateway.admit(
        event.event_id,
        signal_bar,
        risk_snapshot_id=snapshot.snapshot_id,
    )
    assert ledger.load().intents == (recovered,)


def test_expired_unfilled_entry_releases_buying_power_after_restart(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "e" * 64
    packet_fingerprint = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-expired-reservation"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=packet_fingerprint,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    ledger_path = tmp_path / "expired.sqlite3"
    ledger = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    clock = _MutableClock(event.observed_at + timedelta(minutes=1))
    expiry_bar = _bar("2026-07-13T13:30:00+08:00")
    source = _TestBarSource(
        _bar("2026-07-13T10:30:00+08:00"),
        expiry_bar,
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=clock.current,
        clock=clock,
        bar_source=source,
    )
    gateway.admit(
        event.event_id,
        _bar("2026-07-13T10:30:00+08:00"),
        risk_snapshot_id=snapshot.snapshot_id,
    )
    assert ledger.account_snapshot().reserved_buying_power > 0

    clock.current = ts("2026-07-13T13:35:00+08:00")
    assert gateway.process_bar(expiry_bar) == ()
    restarted = SQLitePaperLedger(
        ledger_path,
        initial_cash=Decimal("100000"),
    )
    account = restarted.account_snapshot()

    assert restarted.load().intents[0].status == "expired_risk_snapshot"
    assert account.reserved_buying_power == Decimal("0")
    assert account.available_buying_power == account.cash_balance


def test_durable_ledger_enforces_buying_power_and_blocks_adapter_bypass(
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, session_factory = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    base = "sha256:" + "c" * 64
    bound = bind_risk_snapshot_packet_fingerprint(
        _packet(event, snapshot, base),
        snapshot,
    )
    review_id = "review-no-cash"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    _move_to_review_pending(store, event)
    store.append_risk_snapshot(snapshot)
    _insert_validated_review(
        session_factory,
        event=event,
        risk_snapshot_id=snapshot.snapshot_id,
        review_id=review_id,
        packet_fingerprint=bound,
        created_at=reviewed_at,
    )
    _confirm(store, event, review_id=review_id, occurred_at=reviewed_at)
    poor_ledger = SQLitePaperLedger(
        tmp_path / "poor.sqlite3",
        initial_cash=Decimal("100"),
    )
    gateway = _gateway(
        store=store,
        ledger=poor_ledger,
        snapshot=snapshot,
        base_fingerprint=base,
        now=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(
        TrustedPaperAdmissionError,
        match="paper_buying_power_insufficient",
    ):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot.snapshot_id,
        )

    bypass_ledger = SQLitePaperLedger(
        tmp_path / "bypass.sqlite3",
        initial_cash=Decimal("100000"),
    )
    bypass = ConfirmedEventPaperAdapter(bypass_ledger)
    with pytest.raises(
        PaperLedgerIntegrityError,
        match="trusted_paper_commit_capability_required",
    ):
        bypass.apply_confirmed_event(
            event,
            _bar("2026-07-13T10:30:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id=review_id,
            risk_snapshot=snapshot,
            received_at=event.observed_at + timedelta(minutes=1),
        )

    assert bypass_ledger.load().intents == ()


def test_forged_local_authorization_and_reservation_cannot_enable_legacy_adapter(
    make_bound_decision_event,
    tmp_path,
) -> None:
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    review_id = "forged-local-review"
    authorization_id = "forged-local-paper-authorization"
    payload_fingerprint = "sha256:" + "f" * 64
    admitted_at = event.observed_at + timedelta(minutes=1)
    ledger = SQLitePaperLedger(
        tmp_path / "forged-local-inbox.sqlite3",
        initial_cash=Decimal("100000"),
    )
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            """
            INSERT INTO paper_trusted_admission (
                event_id,
                event_data_fingerprint,
                review_id,
                risk_snapshot_id,
                admission_authorization_id,
                admission_payload_fingerprint,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.data_fingerprint,
                review_id,
                snapshot.snapshot_id,
                authorization_id,
                payload_fingerprint,
                admitted_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO paper_buying_power_reservation (
                event_id,
                required_cash,
                created_at
            ) VALUES (?, ?, ?)
            """,
            (event.event_id, "5050", admitted_at.isoformat()),
        )

    bypass = ConfirmedEventPaperAdapter(
        ledger,
        fee_schedule=_TEST_FEE_SCHEDULE,
    )
    with pytest.raises(
        PaperLedgerIntegrityError,
        match="trusted_paper_commit_capability_required",
    ):
        bypass.apply_confirmed_event(
            event,
            _bar("2026-07-13T10:30:00+08:00"),
            event_state=EventState.CONFIRMED,
            review_id=review_id,
            risk_snapshot=snapshot,
            received_at=admitted_at,
            admission_authorization_id=authorization_id,
            admission_payload_fingerprint=payload_fingerprint,
        )

    assert ledger.load().intents == ()


def test_legacy_split_admission_write_interfaces_are_not_exposed(tmp_path) -> None:
    ledger = SQLitePaperLedger(
        tmp_path / "no-legacy-split-writes.sqlite3",
        initial_cash=Decimal("100000"),
    )

    assert not hasattr(ledger, "_authorize_trusted_admission")
    assert not hasattr(ledger, "reserve_buying_power")
    assert not hasattr(ledger, "_release_orphan_authorization")
    assert not hasattr(ledger, "release_orphan_reservation")


def test_reopen_rejects_initial_cash_identity_change(tmp_path) -> None:
    path = tmp_path / "paper.sqlite3"
    SQLitePaperLedger(path, initial_cash=Decimal("100000"))

    with pytest.raises(
        PaperLedgerIntegrityError,
        match="initial_cash_mismatch",
    ):
        SQLitePaperLedger(path, initial_cash=Decimal("200000"))


def test_bound_packet_identity_includes_exact_risk_snapshot(
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()
    first = _snapshot(event, shares=500)
    second_decision = RiskDecision(
        allowed=True,
        shares=600,
        planned_risk_cash=Decimal("120"),
        target_weight=Decimal("0.06"),
        entry_reference=Decimal("10"),
        reasons=(),
        daily_loss_locked=False,
        drawdown_locked=False,
        evaluated_at=event.observed_at + timedelta(seconds=1),
    )
    second = RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=second_decision,
        observed_at=event.observed_at,
        expires_at=event.observed_at + timedelta(hours=2),
    )
    base = "sha256:" + "d" * 64

    assert bind_risk_snapshot_packet_fingerprint(
        _packet(event, first, base),
        first,
    ) == sha256_json(
        {
            "evidence_packet_fingerprint": base,
            "risk_snapshot_id": first.snapshot_id,
        }
    )
    assert bind_risk_snapshot_packet_fingerprint(
        _packet(event, first, base),
        first,
    ) != bind_risk_snapshot_packet_fingerprint(
        _packet(event, second, base),
        second,
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("expired", "risk_snapshot_expired"),
        ("denied", "risk_decision_not_allowed"),
        ("missing", "risk_snapshot_missing"),
    ),
)
def test_risk_snapshot_must_be_fresh_allowed_and_exact(
    mode,
    expected,
    trusted_store,
    make_bound_decision_event,
    tmp_path,
) -> None:
    store, _ = trusted_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    if mode == "denied":
        denied = RiskDecision(
            allowed=False,
            shares=0,
            planned_risk_cash=Decimal("0"),
            target_weight=Decimal("0"),
            entry_reference=Decimal("10"),
            reasons=("daily_loss_lock",),
            daily_loss_locked=True,
            drawdown_locked=False,
            evaluated_at=event.observed_at,
        )
        snapshot = RiskSnapshot.capture(
            event=event,
            evaluation_input_fingerprint=event.data_fingerprint,
            decision=denied,
            observed_at=event.observed_at,
            expires_at=event.observed_at + timedelta(hours=2),
        )
    _move_to_review_pending(store, event)
    if mode != "missing":
        store.append_risk_snapshot(snapshot)
    _confirm(
        store,
        event,
        review_id="review-risk-gate",
        occurred_at=event.observed_at + timedelta(seconds=30),
    )
    now = (
        snapshot.expires_at
        if mode == "expired"
        else event.observed_at + timedelta(minutes=1)
    )
    ledger = SQLitePaperLedger(
        tmp_path / f"{mode}.sqlite3",
        initial_cash=Decimal("100000"),
    )
    gateway = _gateway(
        store=store,
        ledger=ledger,
        snapshot=snapshot,
        base_fingerprint="sha256:" + "e" * 64,
        now=now,
    )
    snapshot_id = (
        "risk-snapshot:" + "f" * 64
        if mode == "missing"
        else snapshot.snapshot_id
    )

    with pytest.raises(TrustedPaperAdmissionError, match=expected):
        gateway.admit(
            event.event_id,
            _bar("2026-07-13T10:30:00+08:00"),
            risk_snapshot_id=snapshot_id,
        )

    assert ledger.load().intents == ()
