from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import hashlib
import inspect
import json
import threading

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionTransition,
    TableByLLMReview,
    TableByLLMReviewClaim,
    TableByPaperAdmissionAuthorization,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    EventConflictError,
    InvalidEventTransition,
)
from chanlun.decision_support.models import EventState
from chanlun.decision_support.risk import RiskDecision
from chanlun.decision_support.risk_snapshot import RiskSnapshot


def _snapshot(event) -> RiskSnapshot:
    return RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=RiskDecision(
            allowed=True,
            shares=500,
            planned_risk_cash=Decimal("100"),
            target_weight=Decimal("0.05"),
            entry_reference=Decimal("10"),
            reasons=(),
            daily_loss_locked=False,
            drawdown_locked=False,
            evaluated_at=event.observed_at,
        ),
        observed_at=event.observed_at,
        expires_at=event.observed_at + timedelta(hours=2),
    )


@pytest.fixture
def authorization_store(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'authorization.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )

    @sqlalchemy_event.listens_for(engine, "connect")
    def enable_sqlite_integrity(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=2000")
        cursor.close()
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
    clock = {"now": None}

    def now():
        assert clock["now"] is not None
        return clock["now"]

    try:
        yield DecisionEventStore(factory, clock=now), factory, clock, engine
    finally:
        engine.dispose()


def _confirmed_event(
    store,
    factory,
    event,
    snapshot,
    *,
    packet_fingerprint,
    review_pending_actor="event_service",
    review_pending_reason="risk_allowed",
):
    review_id = "review-paper-authorization"
    reviewed_at = event.observed_at + timedelta(seconds=30)
    review_payload = json.dumps(
        {
            "reviewed_data_fingerprint": event.data_fingerprint,
            "reviewed_event_id": event.event_id,
            "reviewed_packet_fingerprint": packet_fingerprint,
            "verdict": "CONFIRM",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    review_sha256 = "sha256:" + hashlib.sha256(
        review_payload.encode("utf-8")
    ).hexdigest()
    review_bytes = len(review_payload.encode("utf-8"))
    store.append_event(event)
    store.append_risk_snapshot(snapshot)
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
        reason=review_pending_reason,
        actor=review_pending_actor,
    )
    with factory() as session:
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
                lease_expires_at=reviewed_at + timedelta(hours=1),
                finalized=True,
                created_at=reviewed_at,
            )
        )
        session.flush()
        session.add(
            TableByLLMReview(
                review_id=review_id,
                event_id=event.event_id,
                risk_snapshot_id=snapshot.snapshot_id,
                packet_fingerprint=packet_fingerprint,
                reviewed_data_fingerprint=event.data_fingerprint,
                provider="fixture",
                model="fixture-model",
                prompt_version="chanlun-review-v3",
                fencing_token=1,
                status="validated",
                provider_ok=True,
                verdict="CONFIRM",
                response_content=review_payload,
                response_content_bytes=review_bytes,
                response_content_sha256=review_sha256,
                response_content_truncated=False,
                raw_response=review_payload,
                raw_response_bytes=review_bytes,
                raw_response_sha256=review_sha256,
                raw_response_truncated=False,
                parsed_response_json=review_payload,
                validation_errors_json="[]",
                attempt_count=1,
                latency_ms=1,
                error_code=None,
                error_message=None,
                error_message_bytes=0,
                error_message_sha256=None,
                error_message_truncated=False,
                created_at=reviewed_at,
            )
        )
        session.commit()
    store.append_transition(
        event.event_id,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
        occurred_at=reviewed_at,
        reason="review_verdict:CONFIRM",
        actor=f"review:{review_id}",
    )
    return review_id, reviewed_at


def test_store_issues_idempotent_authorization_from_locked_confirmed_state(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "a" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    authorized_at = reviewed_at + timedelta(seconds=1)
    clock["now"] = authorized_at

    first = store.issue_paper_admission_authorization(
        event_id=event.event_id,
    )
    second = store.issue_paper_admission_authorization(
        event_id=event.event_id,
    )

    assert second == first
    assert first.event_id == event.event_id
    assert first.review_id == review_id
    assert first.risk_snapshot_id == snapshot.snapshot_id
    assert first.packet_fingerprint == packet_fingerprint
    assert first.manual_check_pending_id is None
    assert first.manual_check_payload_fingerprint is None
    assert first.authorized_at == authorized_at
    assert store.get_paper_admission_authorization(event.event_id) == first


def test_store_issues_authorization_bound_to_exact_manual_approval_transition(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    pending_id = "manual-pending:" + "1" * 64
    approval_fingerprint = "sha256:" + "2" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint="sha256:" + "3" * 64,
        review_pending_actor="manual_check_workflow",
        review_pending_reason=(
            f"manual_check_approved:{pending_id}:{approval_fingerprint}"
        ),
    )
    clock["now"] = reviewed_at + timedelta(seconds=1)

    authorization = store.issue_paper_admission_authorization(
        event_id=event.event_id
    )

    assert authorization.review_id == review_id
    assert authorization.manual_check_pending_id == pending_id
    assert (
        authorization.manual_check_payload_fingerprint
        == approval_fingerprint
    )
    with factory() as session:
        row = session.query(TableByPaperAdmissionAuthorization).one()
        payload = json.loads(row.payload_json)
    assert payload["schema_version"] == 2
    assert payload["manual_check_pending_id"] == pending_id
    assert (
        payload["manual_check_payload_fingerprint"]
        == approval_fingerprint
    )


def test_store_rejects_tampered_manual_binding_column_on_reload(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    pending_id = "manual-pending:" + "6" * 64
    approval_fingerprint = "sha256:" + "7" * 64
    _review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint="sha256:" + "8" * 64,
        review_pending_actor="manual_check_workflow",
        review_pending_reason=(
            f"manual_check_approved:{pending_id}:{approval_fingerprint}"
        ),
    )
    clock["now"] = reviewed_at + timedelta(seconds=1)
    store.issue_paper_admission_authorization(event_id=event.event_id)
    with factory() as session:
        row = session.query(TableByPaperAdmissionAuthorization).one()
        row.manual_check_payload_fingerprint = "sha256:" + "9" * 64
        session.commit()

    with pytest.raises(
        EventConflictError,
        match="columns disagree with payload",
    ):
        store.get_paper_admission_authorization(event.event_id)


def test_store_rejects_noncanonical_manual_approval_transition(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    _review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint="sha256:" + "4" * 64,
        review_pending_actor="manual_check_workflow",
        review_pending_reason=(
            "manual_check_approved:manual-pending:"
            + "5" * 64
        ),
    )
    clock["now"] = reviewed_at + timedelta(seconds=1)

    with pytest.raises(
        InvalidEventTransition,
        match="review-pending transition mismatch",
    ):
        store.issue_paper_admission_authorization(event_id=event.event_id)


def test_store_rejects_noncanonical_machine_review_pending_transition(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    _review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint="sha256:" + "a" * 64,
        review_pending_actor="event_service",
        review_pending_reason="risk_allowed ",
    )
    clock["now"] = reviewed_at + timedelta(seconds=1)

    with pytest.raises(
        InvalidEventTransition,
        match="review-pending transition mismatch",
    ):
        store.issue_paper_admission_authorization(event_id=event.event_id)


def test_store_rejects_authorization_after_risk_snapshot_is_superseded(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    first_snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "b" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        first_snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    second_snapshot = RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=RiskDecision(
            allowed=True,
            shares=600,
            planned_risk_cash=Decimal("120"),
            target_weight=Decimal("0.06"),
            entry_reference=Decimal("10"),
            reasons=(),
            daily_loss_locked=False,
            drawdown_locked=False,
            evaluated_at=reviewed_at + timedelta(milliseconds=1),
        ),
        observed_at=event.observed_at,
        expires_at=event.observed_at + timedelta(hours=2),
    )
    store.append_risk_snapshot(second_snapshot)
    clock["now"] = reviewed_at + timedelta(seconds=1)

    with pytest.raises(InvalidEventTransition, match="review risk snapshot mismatch"):
        store.issue_paper_admission_authorization(
            event_id=event.event_id,
        )

    assert store.get_paper_admission_authorization(event.event_id) is None


def test_later_invalidation_cannot_rewrite_historical_authorization(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "c" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    authorized_at = reviewed_at + timedelta(seconds=1)
    clock["now"] = authorized_at
    authorization = store.issue_paper_admission_authorization(
        event_id=event.event_id,
    )
    store.append_transition(
        event.event_id,
        EventState.CONFIRMED,
        EventState.INVALIDATED,
        occurred_at=authorized_at + timedelta(seconds=1),
        reason="later_structure_invalidation",
        actor="scanner",
    )

    retried = store.issue_paper_admission_authorization(
        event_id=event.event_id,
    )

    assert retried == authorization
    assert retried.authorized_at == authorized_at
    assert store.current_state(event.event_id) is EventState.INVALIDATED


def test_authorization_uses_store_clock_and_cannot_be_backdated(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "d" * 64
    review_id, _reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    clock["now"] = snapshot.expires_at + timedelta(seconds=1)

    assert tuple(
        inspect.signature(store.issue_paper_admission_authorization).parameters
    ) == ("event_id",)
    with pytest.raises(InvalidEventTransition, match="risk snapshot unusable"):
        store.issue_paper_admission_authorization(
            event_id=event.event_id,
        )


def test_authorization_rejects_review_bound_to_superseded_risk_snapshot(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    first_snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "e" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        first_snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    second_snapshot = RiskSnapshot.capture(
        event=event,
        evaluation_input_fingerprint=event.data_fingerprint,
        decision=RiskDecision(
            allowed=True,
            shares=600,
            planned_risk_cash=Decimal("120"),
            target_weight=Decimal("0.06"),
            entry_reference=Decimal("10"),
            reasons=(),
            daily_loss_locked=False,
            drawdown_locked=False,
            evaluated_at=reviewed_at + timedelta(milliseconds=1),
        ),
        observed_at=event.observed_at,
        expires_at=event.observed_at + timedelta(hours=2),
    )
    store.append_risk_snapshot(second_snapshot)
    clock["now"] = reviewed_at + timedelta(seconds=1)

    with pytest.raises(InvalidEventTransition, match="review risk snapshot mismatch"):
        store.issue_paper_admission_authorization(
            event_id=event.event_id,
        )


def test_authorization_rejects_unfinalized_review_claim(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, _engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "0" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    with factory() as session:
        claim = session.query(TableByLLMReviewClaim).filter_by(
            review_id=review_id
        ).one()
        claim.finalized = False
        session.commit()
    clock["now"] = reviewed_at + timedelta(seconds=1)

    with pytest.raises(InvalidEventTransition, match="trusted review claim mismatch"):
        store.issue_paper_admission_authorization(event_id=event.event_id)


def test_sqlite_authorization_linearizes_before_concurrent_invalidation(
    authorization_store,
    make_bound_decision_event,
) -> None:
    store, factory, clock, engine = authorization_store
    event = make_bound_decision_event()
    snapshot = _snapshot(event)
    packet_fingerprint = "sha256:" + "f" * 64
    review_id, reviewed_at = _confirmed_event(
        store,
        factory,
        event,
        snapshot,
        packet_fingerprint=packet_fingerprint,
    )
    clock["now"] = reviewed_at + timedelta(seconds=1)
    insert_reached = threading.Event()
    invalidation_finished = threading.Event()
    completion_order: list[str] = []
    failures: list[BaseException] = []

    def pause_before_authorization_insert(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if (
            threading.current_thread().name == "authorization-thread"
            and "INSERT INTO cl_decision_paper_admission_authorization" in statement
        ):
            insert_reached.set()
            invalidation_finished.wait(0.5)

    sqlalchemy_event.listen(
        engine,
        "before_cursor_execute",
        pause_before_authorization_insert,
    )

    def authorize() -> None:
        try:
            store.issue_paper_admission_authorization(
                event_id=event.event_id,
            )
            completion_order.append("authorization")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def invalidate() -> None:
        assert insert_reached.wait(2)
        try:
            store.append_transition(
                event.event_id,
                EventState.CONFIRMED,
                EventState.INVALIDATED,
                occurred_at=reviewed_at + timedelta(seconds=2),
                reason="concurrent_structure_invalidation",
                actor="scanner",
            )
            completion_order.append("invalidation")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            invalidation_finished.set()

    authorization_thread = threading.Thread(
        target=authorize,
        name="authorization-thread",
    )
    invalidation_thread = threading.Thread(
        target=invalidate,
        name="invalidation-thread",
    )
    authorization_thread.start()
    invalidation_thread.start()
    authorization_thread.join(5)
    invalidation_thread.join(5)
    sqlalchemy_event.remove(
        engine,
        "before_cursor_execute",
        pause_before_authorization_insert,
    )

    assert not authorization_thread.is_alive()
    assert not invalidation_thread.is_alive()
    assert failures == []
    assert completion_order == ["authorization", "invalidation"]
    assert store.get_paper_admission_authorization(event.event_id) is not None
    assert store.current_state(event.event_id) is EventState.INVALIDATED
