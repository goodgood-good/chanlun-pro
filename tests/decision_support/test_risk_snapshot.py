from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByRiskLatchAudit,
    TableByRiskSnapshot,
    TableByUserDecision,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    RiskLatchAuditConflictError,
    RiskSnapshotConflictError,
)
from chanlun.decision_support import event_store as event_store_module
from chanlun.decision_support.risk import RiskPolicy, evaluate_entry
from chanlun.decision_support.risk_snapshot import (
    RiskLatchAction,
    RiskLatchAudit,
    RiskLatchKind,
    RiskSnapshot,
)
from tests.decision_support.conftest import ts


@pytest.fixture
def risk_store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByRiskSnapshot.__table__.create(engine)
    TableByRiskLatchAudit.__table__.create(engine)
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    try:
        yield store, engine
    finally:
        engine.dispose()


def _snapshot(
    event,
    make_risk_context,
    *,
    daily_loss_locked: bool = False,
    drawdown_locked: bool = False,
    expires_at=None,
):
    evaluated_at = ts("2026-07-13T10:36:00+08:00")
    context = make_risk_context(
        asof=evaluated_at,
        quote_time=evaluated_at,
        daily_loss_locked=daily_loss_locked,
        drawdown_locked=drawdown_locked,
    )
    decision = evaluate_entry(event, context, RiskPolicy.conservative())
    return RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=decision,
        observed_at=context.quote.quote_time,
        expires_at=expires_at or evaluated_at + timedelta(minutes=5),
    )


def test_snapshot_captures_complete_bound_risk_decision_and_round_trips(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    event = make_bound_decision_event()
    snapshot = _snapshot(event, make_risk_context)

    assert snapshot.event_id == event.event_id
    assert snapshot.event_data_fingerprint == event.data_fingerprint
    assert snapshot.rule_id == event.rule_id
    assert snapshot.rule_card_version == event.rule_card_version
    assert snapshot.rule_card_fingerprint == event.rule_card_fingerprint
    assert snapshot.rule_set_fingerprint == event.rule_set_fingerprint
    assert (
        snapshot.corpus_manifest_fingerprint
        == event.corpus_manifest_fingerprint
    )
    assert snapshot.algorithm_fingerprint == event.algorithm_fingerprint
    assert snapshot.decision.allowed is True
    payload = snapshot.to_dict()
    assert set(payload["decision"]) == {
        "allowed",
        "shares",
        "planned_risk_cash",
        "target_weight",
        "entry_reference",
        "reasons",
        "daily_loss_locked",
        "drawdown_locked",
        "evaluated_at",
    }
    assert payload["decision"]["planned_risk_cash"] == str(
        snapshot.decision.planned_risk_cash
    )
    assert RiskSnapshot.from_dict(payload) == snapshot
    json.dumps(payload, allow_nan=False, sort_keys=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.event_id = "other"


def test_snapshot_rejects_legacy_event_and_invalid_risk_numbers(
    make_decision_event,
    make_bound_decision_event,
    make_risk_context,
) -> None:
    legacy = make_decision_event()
    context = make_risk_context(
        asof=ts("2026-07-13T10:36:00+08:00"),
        quote_time=ts("2026-07-13T10:36:00+08:00"),
    )
    decision = evaluate_entry(
        make_bound_decision_event(),
        context,
        RiskPolicy.conservative(),
    )

    with pytest.raises(ValueError, match="rule-bound"):
        RiskSnapshot.capture(
            event=legacy,
            evaluation_input_fingerprint=legacy.data_fingerprint,
            decision=decision,
            observed_at=context.quote.quote_time,
            expires_at=context.asof + timedelta(minutes=5),
        )

    event = make_bound_decision_event()
    for changed in (
        replace(decision, planned_risk_cash=Decimal("NaN")),
        replace(decision, shares=-1),
    ):
        with pytest.raises(ValueError, match="RiskDecision"):
            RiskSnapshot.capture(
                event=event,
                evaluation_input_fingerprint=event.data_fingerprint,
                decision=changed,
                observed_at=context.quote.quote_time,
                expires_at=context.asof + timedelta(minutes=5),
            )

    snapshot = _snapshot(event, make_risk_context)
    with pytest.raises(ValueError, match="evaluation input fingerprint"):
        replace(
            snapshot,
            evaluation_input_fingerprint="sha256:" + "5" * 64,
        )


def test_review_validation_fails_closed_on_expiry_or_event_fingerprint_change(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    event = make_bound_decision_event()
    snapshot = _snapshot(event, make_risk_context)

    fresh = snapshot.validate_for_review(
        event,
        as_of=snapshot.expires_at - timedelta(microseconds=1),
    )
    expired = snapshot.validate_for_review(
        event,
        as_of=snapshot.expires_at,
    )
    changed_event = replace(
        event,
        data_fingerprint="sha256:" + "e" * 64,
    )
    mismatched = snapshot.validate_for_review(
        changed_event,
        as_of=snapshot.evaluated_at,
    )

    assert fresh.usable is True
    assert fresh.reasons == ()
    assert expired.usable is False
    assert expired.reasons == ("risk_snapshot_expired",)
    assert mismatched.usable is False
    assert "event_data_fingerprint_mismatch" in mismatched.reasons


def test_disallowed_risk_snapshot_is_not_reviewable(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    event = make_bound_decision_event()
    snapshot = _snapshot(
        event,
        make_risk_context,
        daily_loss_locked=True,
    )

    validation = snapshot.validate_for_review(
        event,
        as_of=snapshot.evaluated_at,
    )

    assert snapshot.decision.allowed is False
    assert validation.usable is False
    assert "risk_decision_not_allowed" in validation.reasons


def test_latch_audit_is_append_only_domain_record_with_manual_reset(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    event = make_bound_decision_event()
    snapshot = _snapshot(
        event,
        make_risk_context,
        daily_loss_locked=True,
    )
    latched = RiskLatchAudit.record(
        snapshot=snapshot,
        latch_kind=RiskLatchKind.DAILY_LOSS,
        action=RiskLatchAction.LATCHED,
        actor="risk-engine",
        reason="daily_loss_limit_reached",
        occurred_at=snapshot.evaluated_at,
    )
    reset = RiskLatchAudit.record(
        snapshot=snapshot,
        latch_kind=RiskLatchKind.DAILY_LOSS,
        action=RiskLatchAction.MANUAL_RESET,
        actor="operator-001",
        reason="broker_ledger_reconciled",
        occurred_at=snapshot.evaluated_at + timedelta(minutes=1),
    )

    assert (latched.previous_locked, latched.current_locked) == (False, True)
    assert (reset.previous_locked, reset.current_locked) == (True, False)
    assert RiskLatchAudit.from_dict(reset.to_dict()) == reset
    with pytest.raises(dataclasses.FrozenInstanceError):
        reset.reason = "changed"


def test_latch_audit_rejects_reset_without_a_latched_source_snapshot(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    snapshot = _snapshot(make_bound_decision_event(), make_risk_context)

    with pytest.raises(ValueError, match="source snapshot is not locked"):
        RiskLatchAudit.record(
            snapshot=snapshot,
            latch_kind=RiskLatchKind.DAILY_LOSS,
            action=RiskLatchAction.MANUAL_RESET,
            actor="operator-001",
            reason="invalid_reset_attempt",
            occurred_at=snapshot.evaluated_at + timedelta(minutes=1),
        )


def test_store_appends_snapshot_idempotently_and_rejects_payload_conflict(
    risk_store,
    make_bound_decision_event,
    make_risk_context,
) -> None:
    store, _engine = risk_store
    event = make_bound_decision_event()
    store.append_event(event)
    snapshot = _snapshot(event, make_risk_context)

    first = store.append_risk_snapshot(snapshot)
    second = store.append_risk_snapshot(snapshot)
    changed_payload = replace(
        snapshot,
        expires_at=snapshot.expires_at + timedelta(minutes=1),
    )

    assert first == second == snapshot
    assert changed_payload.snapshot_id == snapshot.snapshot_id
    with pytest.raises(RiskSnapshotConflictError, match="immutable"):
        store.append_risk_snapshot(changed_payload)
    assert store.list_risk_snapshots(event.event_id) == (snapshot,)


def test_store_returns_only_current_event_bound_unexpired_snapshot_for_review(
    risk_store,
    make_bound_decision_event,
    make_risk_context,
) -> None:
    store, _engine = risk_store
    event = make_bound_decision_event()
    store.append_event(event)
    snapshot = _snapshot(event, make_risk_context)
    store.append_risk_snapshot(snapshot)

    assert store.get_risk_snapshot_for_review(
        event,
        as_of=snapshot.evaluated_at,
    ) == snapshot
    assert store.get_risk_snapshot_for_review(
        event,
        as_of=snapshot.expires_at,
    ) is None
    changed_event = replace(
        event,
        data_fingerprint="sha256:" + "e" * 64,
    )
    assert store.get_risk_snapshot_for_review(
        changed_event,
        as_of=snapshot.evaluated_at,
    ) is None


def test_store_appends_latch_and_reset_audits_without_mutating_history(
    risk_store,
    make_bound_decision_event,
    make_risk_context,
) -> None:
    store, _engine = risk_store
    event = make_bound_decision_event()
    store.append_event(event)
    snapshot = _snapshot(
        event,
        make_risk_context,
        drawdown_locked=True,
    )
    store.append_risk_snapshot(snapshot)
    latched = RiskLatchAudit.record(
        snapshot=snapshot,
        latch_kind=RiskLatchKind.STRATEGY_DRAWDOWN,
        action=RiskLatchAction.LATCHED,
        actor="risk-engine",
        reason="drawdown_limit_reached",
        occurred_at=snapshot.evaluated_at,
    )
    reset = RiskLatchAudit.record(
        snapshot=snapshot,
        latch_kind=RiskLatchKind.STRATEGY_DRAWDOWN,
        action=RiskLatchAction.MANUAL_RESET,
        actor="operator-001",
        reason="manual_risk_review_completed",
        occurred_at=snapshot.evaluated_at + timedelta(minutes=1),
    )

    assert store.append_risk_latch_audit(latched) == latched
    assert store.append_risk_latch_audit(latched) == latched
    assert store.append_risk_latch_audit(reset) == reset
    assert store.list_risk_latch_audits(event.event_id) == (latched, reset)

    changed = replace(latched, reason="changed_payload")
    assert changed.audit_id == latched.audit_id
    with pytest.raises(RiskLatchAuditConflictError, match="immutable"):
        store.append_risk_latch_audit(changed)
    assert store.list_risk_latch_audits(event.event_id) == (latched, reset)


def test_risk_latch_append_uses_parent_event_row_lock() -> None:
    statement = event_store_module._risk_latch_event_lock_statement("event-1")

    compiled = str(statement.compile(dialect=mysql.dialect())).upper()

    assert "FOR UPDATE" in compiled
    assert "CL_DECISION_EVENT" in compiled


def test_risk_tables_have_physical_append_only_identity_constraints(
    risk_store,
) -> None:
    store, engine = risk_store
    snapshot_constraints = inspect(engine).get_unique_constraints(
        TableByRiskSnapshot.__tablename__
    )
    audit_constraints = inspect(engine).get_unique_constraints(
        TableByRiskLatchAudit.__tablename__
    )

    assert {tuple(item["column_names"]) for item in snapshot_constraints} >= {
        ("snapshot_id",),
        ("identity_fingerprint",),
    }
    assert {tuple(item["column_names"]) for item in audit_constraints} >= {
        ("audit_id",),
        ("identity_fingerprint",),
    }
    assert hasattr(store, "append_user_decision")
    assert TableByUserDecision.__tablename__ == "cl_decision_user_decision"
