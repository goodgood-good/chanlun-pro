from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByUserDecision,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    UserDecisionConflictError,
)
from tests.decision_support.conftest import ts


@pytest.fixture
def decision_store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByUserDecision.__table__.create(engine)
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    try:
        yield store, engine
    finally:
        engine.dispose()


def _append(store, event, *, action="accepted", key="request-key-001", **changes):
    values = {
        "event_id": event.event_id,
        "user_id": "test-user",
        "action": action,
        "note": "人工图表核对完成",
        "event_data_fingerprint": event.data_fingerprint,
        "decided_at": ts("2026-07-13T10:40:00+08:00"),
        "idempotency_key": key,
    }
    return store.append_user_decision(**(values | changes))


def test_user_decision_retry_is_idempotent(
    decision_store,
    make_bound_decision_event,
):
    store, _engine = decision_store
    event = make_bound_decision_event()
    store.append_event(event)

    first = _append(store, event)
    second = _append(
        store,
        event,
        decided_at=first.decided_at + timedelta(seconds=5),
    )

    assert first == second
    assert first.action == "accepted"
    assert store.list_user_decisions(event.event_id) == (first,)


def test_user_decision_same_key_with_different_payload_conflicts(
    decision_store,
    make_bound_decision_event,
):
    store, _engine = decision_store
    event = make_bound_decision_event()
    store.append_event(event)
    _append(store, event)

    with pytest.raises(UserDecisionConflictError, match="idempotency"):
        _append(store, event, action="ignored")


def test_user_decision_rejects_order_action_and_fingerprint_mismatch(
    decision_store,
    make_bound_decision_event,
):
    store, _engine = decision_store
    event = make_bound_decision_event()
    store.append_event(event)

    with pytest.raises(ValueError, match="action"):
        _append(store, event, action="place_order")
    with pytest.raises(UserDecisionConflictError, match="fingerprint"):
        _append(
            store,
            event,
            event_data_fingerprint="sha256:" + "f" * 64,
        )
    assert store.list_user_decisions(event.event_id) == ()


def test_user_decision_table_has_physical_idempotency_constraint(
    decision_store,
):
    _store, engine = decision_store

    constraints = inspect(engine).get_unique_constraints(
        TableByUserDecision.__tablename__
    )

    assert {
        tuple(item["column_names"])
        for item in constraints
    } >= {
        ("decision_id",),
        ("event_id", "user_id", "idempotency_key"),
    }
