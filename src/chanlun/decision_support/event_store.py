from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from threading import RLock
from typing import Callable, Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByPaperAdmissionAuthorization,
    TableByRiskLatchAudit,
    TableByRiskSnapshot,
    TableByUserDecision,
    TableByLLMReview,
    TableByLLMReviewAttempt,
    TableByLLMReviewClaim,
)

from .fingerprints import normalize_datetime, sha256_json
from .manual_check_binding import (
    MANUAL_CHECK_TRANSITION_ACTOR,
    parse_manual_check_transition,
)
from .models import DecisionEvent, EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .risk_snapshot import (
    RiskLatchAudit,
    RiskLatchKind,
    RiskSnapshot,
)


class EventConflictError(RuntimeError):
    pass


class InvalidEventTransition(RuntimeError):
    pass


class EventStateConflictError(InvalidEventTransition):
    pass


class EventNotFoundError(KeyError):
    pass


class ReviewConflictError(RuntimeError):
    pass


class UserDecisionConflictError(RuntimeError):
    pass


class RiskSnapshotConflictError(RuntimeError):
    pass


class RiskLatchAuditConflictError(RuntimeError):
    pass


class LLMReviewClaimLostError(ReviewConflictError):
    pass


@dataclass(frozen=True, slots=True)
class StoredTransition:
    id: int
    event_id: str
    from_state: EventState
    to_state: EventState
    occurred_at: datetime
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class TransitionSpec:
    from_state: EventState
    to_state: EventState
    occurred_at: datetime
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class StoredEventSnapshot:
    event: DecisionEvent
    state: EventState
    transitions: tuple[StoredTransition, ...]


@dataclass(frozen=True, slots=True)
class StoredReview:
    id: int
    review_id: str
    event_id: str
    reviewed_data_fingerprint: str
    verdict: str
    reviewed_at: datetime
    applied: bool
    state: EventState
    reason: str


@dataclass(frozen=True, slots=True)
class StoredUserDecision:
    id: int
    decision_id: str
    event_id: str
    user_id: str
    action: str
    note: str | None
    event_data_fingerprint: str
    idempotency_key: str
    payload_fingerprint: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPaperAdmissionAuthorization:
    id: int
    authorization_id: str
    event_id: str
    event_data_fingerprint: str
    review_id: str
    risk_snapshot_id: str
    confirmation_transition_id: int
    manual_check_pending_id: str | None
    manual_check_payload_fingerprint: str | None
    packet_fingerprint: str
    authorized_at: datetime
    risk_expires_at: datetime
    payload_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredLLMReview:
    id: int
    review_id: str
    event_id: str
    risk_snapshot_id: str
    packet_fingerprint: str
    reviewed_data_fingerprint: str
    provider: str
    model: str
    prompt_version: str
    fencing_token: int
    status: str
    provider_ok: bool
    verdict: str
    response_content: str | None
    response_content_bytes: int
    response_content_sha256: str | None
    response_content_truncated: bool
    raw_response: str
    raw_response_bytes: int
    raw_response_sha256: str
    raw_response_truncated: bool
    parsed_response_json: str | None
    validation_errors: tuple[str, ...]
    attempt_count: int
    latency_ms: int
    error_code: str | None
    error_message: str | None
    error_message_bytes: int
    error_message_sha256: str | None
    error_message_truncated: bool
    created_at: datetime

    @property
    def ok(self) -> bool:
        return self.status == "validated"


@dataclass(frozen=True, slots=True)
class LLMReviewClaim:
    review_id: str
    event_id: str
    packet_fingerprint: str
    provider: str
    model: str
    prompt_version: str
    owner_token: str
    fencing_token: int
    lease_expires_at: datetime
    finalized: bool
    created_at: datetime
    acquired: bool


@dataclass(frozen=True, slots=True)
class StoredLLMReviewAttempt:
    id: int
    attempt_id: str
    review_id: str
    event_id: str
    owner_token: str
    fencing_token: int
    attempt_number: int
    provider: str
    model: str
    ok: bool
    retryable: bool
    response_content: str | None
    response_content_bytes: int
    response_content_sha256: str | None
    response_content_truncated: bool
    raw_response: str
    raw_response_bytes: int
    raw_response_sha256: str
    raw_response_truncated: bool
    error_code: str | None
    error_message: str | None
    error_message_bytes: int
    error_message_sha256: str | None
    error_message_truncated: bool
    latency_ms: int
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class _AuditText:
    text: str | None
    original_bytes: int
    sha256: str | None
    truncated: bool


_ALLOWED_TRANSITIONS = {
    EventState.DETECTED: frozenset(
        {
            EventState.RISK_CHECKED,
            EventState.EXPIRED,
            EventState.INVALIDATED,
        }
    ),
    EventState.RISK_CHECKED: frozenset(
        {
            EventState.REVIEW_PENDING,
            EventState.REJECTED,
            EventState.EXPIRED,
            EventState.INVALIDATED,
        }
    ),
    EventState.REVIEW_PENDING: frozenset(
        {
            EventState.CONFIRMED,
            EventState.REJECTED,
            EventState.ABSTAINED,
            EventState.EXPIRED,
            EventState.INVALIDATED,
        }
    ),
    EventState.CONFIRMED: frozenset(
        {
            EventState.ACTED,
            EventState.EXPIRED,
            EventState.INVALIDATED,
        }
    ),
    EventState.REJECTED: frozenset(),
    EventState.ABSTAINED: frozenset(),
    EventState.EXPIRED: frozenset(),
    EventState.INVALIDATED: frozenset(),
    EventState.ACTED: frozenset(),
}
_REVIEW_OUTCOME_STATES = frozenset(
    {
        EventState.CONFIRMED,
        EventState.REJECTED,
        EventState.ABSTAINED,
    }
)
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_USER_DECISION_ACTIONS = frozenset(
    {"accepted", "ignored", "executed_externally"}
)
_LLM_REVIEW_STATUSES = frozenset(
    {"validated", "validation_failed", "provider_failed", "local_abstain"}
)
_LLM_REVIEW_VERDICTS = frozenset({"CONFIRM", "WATCH", "REJECT", "ABSTAIN"})
MAX_LLM_AUDIT_BYTES = 1024 * 1024
MAX_LLM_ERROR_BYTES = 8 * 1024
_MAX_CLAIM_LEASE_SECONDS = 24 * 60 * 60


def _bounded_audit_text(
    value: str | None,
    field_name: str,
    *,
    allow_none: bool,
    max_bytes: int = MAX_LLM_AUDIT_BYTES,
) -> _AuditText:
    if value is None:
        if not allow_none:
            raise TypeError(f"{field_name} must be text")
        return _AuditText(None, 0, None, False)
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text or None")
    payload = value.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if len(payload) <= max_bytes:
        return _AuditText(value, len(payload), digest, False)
    bounded = payload[:max_bytes].decode("utf-8", errors="ignore")
    return _AuditText(bounded, len(payload), digest, True)


def _event_payload(event: DecisionEvent) -> str:
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _event_from_row(row: TableByDecisionEvent) -> DecisionEvent:
    try:
        payload = json.loads(row.payload_json)
        event = DecisionEvent.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise EventConflictError("stored event payload is invalid") from exc
    stored_strategy_run = (
        row.strategy_run_id,
        row.strategy_run_epoch,
        row.strategy_run_fingerprint,
    )
    payload_strategy_run = (
        event.strategy_run_id,
        event.strategy_run_epoch,
        event.strategy_run_fingerprint,
    )
    if stored_strategy_run != payload_strategy_run:
        raise EventConflictError(
            "stored event strategy-run columns disagree with payload"
        )
    return event


def _transition_from_row(
    row: TableByDecisionTransition,
) -> StoredTransition:
    return StoredTransition(
        id=row.id,
        event_id=row.event_id,
        from_state=EventState(row.from_state),
        to_state=EventState(row.to_state),
        occurred_at=normalize_datetime(row.occurred_at, "occurred_at"),
        reason=row.reason,
        actor=row.actor,
    )


def _review_from_row(row: TableByDecisionReview) -> StoredReview:
    return StoredReview(
        id=row.id,
        review_id=row.review_id,
        event_id=row.event_id,
        reviewed_data_fingerprint=row.reviewed_data_fingerprint,
        verdict=row.verdict,
        reviewed_at=normalize_datetime(row.reviewed_at, "reviewed_at"),
        applied=bool(row.applied),
        state=EventState(row.state),
        reason=row.reason,
    )


def _user_decision_from_row(
    row: TableByUserDecision,
) -> StoredUserDecision:
    return StoredUserDecision(
        id=row.id,
        decision_id=row.decision_id,
        event_id=row.event_id,
        user_id=row.user_id,
        action=row.action,
        note=row.note,
        event_data_fingerprint=row.event_data_fingerprint,
        idempotency_key=row.idempotency_key,
        payload_fingerprint=row.payload_fingerprint,
        decided_at=normalize_datetime(row.decided_at, "decided_at"),
    )


def _paper_authorization_from_row(
    row: TableByPaperAdmissionAuthorization,
) -> StoredPaperAdmissionAuthorization:
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventConflictError(
            "stored paper admission authorization payload is invalid"
        ) from exc
    expected = {
        "schema_version": 2,
        "authorization_id": row.authorization_id,
        "event_id": row.event_id,
        "event_data_fingerprint": row.event_data_fingerprint,
        "review_id": row.review_id,
        "risk_snapshot_id": row.risk_snapshot_id,
        "confirmation_transition_id": row.confirmation_transition_id,
        "manual_check_pending_id": row.manual_check_pending_id,
        "manual_check_payload_fingerprint": (
            row.manual_check_payload_fingerprint
        ),
        "packet_fingerprint": row.packet_fingerprint,
        "authorized_at": normalize_datetime(
            row.authorized_at,
            "authorized_at",
        ).isoformat(),
        "risk_expires_at": normalize_datetime(
            row.risk_expires_at,
            "risk_expires_at",
        ).isoformat(),
    }
    identity = {
        key: expected[key]
        for key in (
            "event_id",
            "event_data_fingerprint",
            "review_id",
            "risk_snapshot_id",
            "confirmation_transition_id",
            "manual_check_pending_id",
            "manual_check_payload_fingerprint",
            "packet_fingerprint",
        )
    }
    derived_authorization_id = "paper-auth:" + sha256_json(identity)[7:]
    if (
        row.authorization_id != derived_authorization_id
        or payload != expected
        or sha256_json(payload) != row.payload_fingerprint
    ):
        raise EventConflictError(
            "stored paper admission authorization columns disagree with payload"
        )
    return StoredPaperAdmissionAuthorization(
        id=row.id,
        authorization_id=row.authorization_id,
        event_id=row.event_id,
        event_data_fingerprint=row.event_data_fingerprint,
        review_id=row.review_id,
        risk_snapshot_id=row.risk_snapshot_id,
        confirmation_transition_id=row.confirmation_transition_id,
        manual_check_pending_id=row.manual_check_pending_id,
        manual_check_payload_fingerprint=(
            row.manual_check_payload_fingerprint
        ),
        packet_fingerprint=row.packet_fingerprint,
        authorized_at=normalize_datetime(row.authorized_at, "authorized_at"),
        risk_expires_at=normalize_datetime(row.risk_expires_at, "risk_expires_at"),
        payload_fingerprint=row.payload_fingerprint,
    )


def _risk_snapshot_payload(snapshot: RiskSnapshot) -> str:
    return json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _risk_snapshot_from_row(row: TableByRiskSnapshot) -> RiskSnapshot:
    try:
        payload = json.loads(row.payload_json)
        snapshot = RiskSnapshot.from_dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RiskSnapshotConflictError(
            "stored risk snapshot payload is invalid"
        ) from exc
    decision = snapshot.decision
    expected_reasons = json.dumps(
        list(decision.reasons),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    physical_values = (
        (row.snapshot_id, snapshot.snapshot_id),
        (row.identity_fingerprint, snapshot.identity_fingerprint),
        (row.event_id, snapshot.event_id),
        (row.event_data_fingerprint, snapshot.event_data_fingerprint),
        (row.rule_id, snapshot.rule_id),
        (row.rule_card_version, snapshot.rule_card_version),
        (row.rule_card_fingerprint, snapshot.rule_card_fingerprint),
        (row.rule_set_fingerprint, snapshot.rule_set_fingerprint),
        (
            row.corpus_manifest_fingerprint,
            snapshot.corpus_manifest_fingerprint,
        ),
        (row.algorithm_fingerprint, snapshot.algorithm_fingerprint),
        (
            row.evaluation_input_fingerprint,
            snapshot.evaluation_input_fingerprint,
        ),
        (
            normalize_datetime(row.observed_at, "observed_at"),
            snapshot.observed_at,
        ),
        (
            normalize_datetime(row.evaluated_at, "evaluated_at"),
            snapshot.evaluated_at,
        ),
        (
            normalize_datetime(row.expires_at, "expires_at"),
            snapshot.expires_at,
        ),
        (bool(row.decision_allowed), decision.allowed),
        (row.shares, decision.shares),
        (row.planned_risk_cash, str(decision.planned_risk_cash)),
        (row.target_weight, str(decision.target_weight)),
        (row.entry_reference, str(decision.entry_reference)),
        (row.decision_reasons_json, expected_reasons),
        (bool(row.daily_loss_locked), decision.daily_loss_locked),
        (bool(row.drawdown_locked), decision.drawdown_locked),
        (row.payload_fingerprint, snapshot.payload_fingerprint),
    )
    if any(actual != expected for actual, expected in physical_values):
        raise RiskSnapshotConflictError(
            "stored risk snapshot columns disagree with payload"
        )
    return snapshot


def _risk_latch_payload(audit: RiskLatchAudit) -> str:
    return json.dumps(
        audit.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _risk_latch_from_row(row: TableByRiskLatchAudit) -> RiskLatchAudit:
    try:
        payload = json.loads(row.payload_json)
        audit = RiskLatchAudit.from_dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RiskLatchAuditConflictError(
            "stored risk latch audit payload is invalid"
        ) from exc
    physical_values = (
        (row.audit_id, audit.audit_id),
        (row.identity_fingerprint, audit.identity_fingerprint),
        (row.event_id, audit.event_id),
        (row.snapshot_id, audit.snapshot_id),
        (row.latch_kind, audit.latch_kind.value),
        (row.action, audit.action.value),
        (bool(row.previous_locked), audit.previous_locked),
        (bool(row.current_locked), audit.current_locked),
        (row.actor, audit.actor),
        (row.reason, audit.reason),
        (
            normalize_datetime(row.occurred_at, "occurred_at"),
            audit.occurred_at,
        ),
        (row.payload_fingerprint, audit.payload_fingerprint),
    )
    if any(actual != expected for actual, expected in physical_values):
        raise RiskLatchAuditConflictError(
            "stored risk latch audit columns disagree with payload"
        )
    return audit


def _risk_latch_event_lock_statement(event_id: str):
    return (
        select(TableByDecisionEvent.id)
        .where(TableByDecisionEvent.event_id == event_id)
        .with_for_update()
    )


def _llm_review_from_row(row: TableByLLMReview) -> StoredLLMReview:
    try:
        validation_errors = json.loads(row.validation_errors_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventConflictError(
            "stored LLM review validation errors are invalid"
        ) from exc
    if not isinstance(validation_errors, list) or any(
        not isinstance(error, str) or not error for error in validation_errors
    ):
        raise EventConflictError(
            "stored LLM review validation errors are invalid"
        )
    return StoredLLMReview(
        id=row.id,
        review_id=row.review_id,
        event_id=row.event_id,
        risk_snapshot_id=row.risk_snapshot_id,
        packet_fingerprint=row.packet_fingerprint,
        reviewed_data_fingerprint=row.reviewed_data_fingerprint,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        fencing_token=row.fencing_token,
        status=row.status,
        provider_ok=bool(row.provider_ok),
        verdict=row.verdict,
        response_content=row.response_content,
        response_content_bytes=row.response_content_bytes,
        response_content_sha256=row.response_content_sha256,
        response_content_truncated=bool(row.response_content_truncated),
        raw_response=row.raw_response,
        raw_response_bytes=row.raw_response_bytes,
        raw_response_sha256=row.raw_response_sha256,
        raw_response_truncated=bool(row.raw_response_truncated),
        parsed_response_json=row.parsed_response_json,
        validation_errors=tuple(validation_errors),
        attempt_count=row.attempt_count,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        error_message=row.error_message,
        error_message_bytes=row.error_message_bytes,
        error_message_sha256=row.error_message_sha256,
        error_message_truncated=bool(row.error_message_truncated),
        created_at=normalize_datetime(row.created_at, "created_at"),
    )


def _llm_claim_from_row(
    row: TableByLLMReviewClaim,
    *,
    acquired: bool,
) -> LLMReviewClaim:
    return LLMReviewClaim(
        review_id=row.review_id,
        event_id=row.event_id,
        packet_fingerprint=row.packet_fingerprint,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        owner_token=row.owner_token,
        fencing_token=row.fencing_token,
        lease_expires_at=normalize_datetime(
            row.lease_expires_at,
            "lease_expires_at",
        ),
        finalized=bool(row.finalized),
        created_at=normalize_datetime(row.created_at, "created_at"),
        acquired=acquired,
    )


def _llm_attempt_from_row(
    row: TableByLLMReviewAttempt,
) -> StoredLLMReviewAttempt:
    return StoredLLMReviewAttempt(
        id=row.id,
        attempt_id=row.attempt_id,
        review_id=row.review_id,
        event_id=row.event_id,
        owner_token=row.owner_token,
        fencing_token=row.fencing_token,
        attempt_number=row.attempt_number,
        provider=row.provider,
        model=row.model,
        ok=bool(row.ok),
        retryable=bool(row.retryable),
        response_content=row.response_content,
        response_content_bytes=row.response_content_bytes,
        response_content_sha256=row.response_content_sha256,
        response_content_truncated=bool(row.response_content_truncated),
        raw_response=row.raw_response,
        raw_response_bytes=row.raw_response_bytes,
        raw_response_sha256=row.raw_response_sha256,
        raw_response_truncated=bool(row.raw_response_truncated),
        error_code=row.error_code,
        error_message=row.error_message,
        error_message_bytes=row.error_message_bytes,
        error_message_sha256=row.error_message_sha256,
        error_message_truncated=bool(row.error_message_truncated),
        latency_ms=row.latency_ms,
        started_at=normalize_datetime(row.started_at, "started_at"),
        completed_at=normalize_datetime(row.completed_at, "completed_at"),
    )


def _state(value: EventState | str, field_name: str) -> EventState:
    try:
        return EventState(value)
    except (TypeError, ValueError) as exc:
        raise InvalidEventTransition(f"invalid {field_name}") from exc


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _bounded_text(value: object, field_name: str, max_length: int) -> str:
    text_value = _required_text(value, field_name)
    if len(text_value) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return text_value


def _bounded_optional_text(
    value: object,
    field_name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, max_length)


class DecisionEventStore:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._session_factory = session_factory
        self._coordination_clock = clock
        self._mutation_fence = MutationLeaseGuard()
        self._binding_lock = RLock()
        self._strategy_run_binding: tuple[str, int, str] | None = None

    def bind_strategy_run(self, strategy_run: object) -> None:
        run_id = _bounded_text(
            getattr(strategy_run, "run_id", None),
            "strategy_run.run_id",
            80,
        )
        epoch = getattr(strategy_run, "epoch", None)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise ValueError("strategy_run.epoch must be a positive integer")
        fingerprint = _required_fingerprint(
            getattr(strategy_run, "strategy_run_fingerprint", None),
            "strategy_run.strategy_run_fingerprint",
        )
        binding = (run_id, epoch, fingerprint)
        with self._binding_lock:
            if (
                self._strategy_run_binding is not None
                and self._strategy_run_binding != binding
            ):
                raise EventConflictError("strategy_run_store_rebind_forbidden")
            self._mutation_fence.bind(strategy_run)
            if self._strategy_run_binding is None:
                self._strategy_run_binding = binding

    def _require_current_strategy_event_value(
        self,
        event: DecisionEvent,
    ) -> None:
        with self._binding_lock:
            binding = self._strategy_run_binding
        if binding is None:
            return
        actual = (
            event.strategy_run_id,
            event.strategy_run_epoch,
            event.strategy_run_fingerprint,
        )
        if actual != binding:
            raise EventConflictError("event_outside_current_strategy_run")

    def _require_current_strategy_event_row(
        self,
        row: TableByDecisionEvent,
    ) -> None:
        with self._binding_lock:
            binding = self._strategy_run_binding
        if binding is None:
            return
        actual = (
            row.strategy_run_id,
            row.strategy_run_epoch,
            row.strategy_run_fingerprint,
        )
        if actual != binding:
            raise EventConflictError("event_outside_current_strategy_run")

    def _require_current_strategy_event_id(
        self,
        session: Session,
        event_id: str,
    ) -> None:
        with self._binding_lock:
            binding = self._strategy_run_binding
        if binding is None:
            return
        row = session.scalar(
            select(TableByDecisionEvent).where(
                TableByDecisionEvent.event_id == event_id
            )
        )
        if row is None:
            raise EventNotFoundError(event_id)
        self._require_current_strategy_event_row(row)

    def _coordination_now(self, session: Session) -> datetime:
        if self._coordination_clock is not None:
            return normalize_datetime(self._coordination_clock(), "clock")
        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "mysql":
            value = session.scalar(text("SELECT UTC_TIMESTAMP(6)"))
        else:
            value = session.scalar(select(func.current_timestamp()))
        if not isinstance(value, datetime):
            raise RuntimeError("database did not return a coordination timestamp")
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return normalize_datetime(value, "database clock")

    @staticmethod
    def _begin_write_serialization(session: Session) -> None:
        """Acquire SQLite's cross-process writer lock before trust reads.

        MySQL write paths serialize on the parent event row with FOR UPDATE.
        SQLite compiles FOR UPDATE away, so its equivalent trust boundary is
        a BEGIN IMMEDIATE issued before the first SELECT in the transaction.
        """

        bind = session.get_bind()
        if bind is not None and bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @mutation_fenced("decision_event_store.append_event")
    def append_event(self, event: DecisionEvent) -> DecisionEvent:
        self._mutation_fence.require()
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        self._require_current_strategy_event_value(event)
        payload = _event_payload(event)
        row = TableByDecisionEvent(
            event_id=event.event_id,
            market=event.market,
            code=event.code,
            observed_at=event.observed_at,
            strategy_track=event.strategy_track.value,
            data_fingerprint=event.data_fingerprint,
            config_fingerprint=event.config_fingerprint,
            strategy_run_id=event.strategy_run_id,
            strategy_run_epoch=event.strategy_run_epoch,
            strategy_run_fingerprint=event.strategy_run_fingerprint,
            payload_json=payload,
        )
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
                return event
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(TableByDecisionEvent).where(
                        TableByDecisionEvent.event_id == event.event_id
                    )
                )
                if existing is None or existing.payload_json != payload:
                    raise EventConflictError(
                        f"immutable event conflict: {event.event_id}"
                    )
                return _event_from_row(existing)

    def get_event(self, event_id: str) -> DecisionEvent | None:
        _required_text(event_id, "event_id")
        with self._session_factory() as session:
            row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            return None if row is None else _event_from_row(row)

    def get_snapshot(self, event_id: str) -> StoredEventSnapshot:
        event_id = _required_text(event_id, "event_id")
        with self._session_factory() as session:
            event_row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            if event_row is None:
                raise EventNotFoundError(event_id)
            transition_rows = session.scalars(
                select(TableByDecisionTransition)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id)
            ).all()
            transitions = tuple(
                _transition_from_row(row) for row in transition_rows
            )
            state = (
                EventState.DETECTED
                if not transitions
                else transitions[-1].to_state
            )
            return StoredEventSnapshot(
                event=_event_from_row(event_row),
                state=state,
                transitions=transitions,
            )

    def list_events(
        self,
        *,
        market: str | None = None,
        code: str | None = None,
        strategy_run_id: str | None = None,
        strategy_run_epoch: int | None = None,
        strategy_run_fingerprint: str | None = None,
        limit: int | None = None,
    ) -> tuple[DecisionEvent, ...]:
        statement = select(TableByDecisionEvent)
        if market is not None:
            statement = statement.where(
                TableByDecisionEvent.market == _required_text(market, "market")
            )
        if code is not None:
            statement = statement.where(
                TableByDecisionEvent.code == _required_text(code, "code")
            )
        strategy_run_filter = (
            strategy_run_id,
            strategy_run_epoch,
            strategy_run_fingerprint,
        )
        provided_strategy_run_fields = sum(
            value is not None for value in strategy_run_filter
        )
        if provided_strategy_run_fields not in (0, 3):
            raise ValueError(
                "strategy-run filter fields must be provided together"
            )
        if provided_strategy_run_fields == 3:
            strategy_run_id = _bounded_text(
                strategy_run_id,
                "strategy_run_id",
                80,
            )
            if (
                isinstance(strategy_run_epoch, bool)
                or not isinstance(strategy_run_epoch, int)
                or strategy_run_epoch <= 0
            ):
                raise ValueError(
                    "strategy_run_epoch must be a positive integer"
                )
            strategy_run_fingerprint = _required_fingerprint(
                strategy_run_fingerprint,
                "strategy_run_fingerprint",
            )
            statement = statement.where(
                TableByDecisionEvent.strategy_run_id == strategy_run_id,
                TableByDecisionEvent.strategy_run_epoch == strategy_run_epoch,
                TableByDecisionEvent.strategy_run_fingerprint
                == strategy_run_fingerprint,
            )
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            statement = statement.limit(limit)
        statement = statement.order_by(
            TableByDecisionEvent.observed_at,
            TableByDecisionEvent.id,
        )
        with self._session_factory() as session:
            return tuple(
                _event_from_row(row)
                for row in session.scalars(statement).all()
            )

    def list_current_strategy_events(self) -> tuple[DecisionEvent, ...]:
        """List only the exact bound strategy run, or all in standalone mode."""

        with self._binding_lock:
            binding = self._strategy_run_binding
        if binding is None:
            return self.list_events()
        return self.list_events(
            strategy_run_id=binding[0],
            strategy_run_epoch=binding[1],
            strategy_run_fingerprint=binding[2],
        )

    def count_events(self, event_id: str | None = None) -> int:
        statement = select(func.count(TableByDecisionEvent.id))
        if event_id is not None:
            statement = statement.where(
                TableByDecisionEvent.event_id
                == _required_text(event_id, "event_id")
            )
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    @mutation_fenced("decision_event_store.append_user_decision")
    def append_user_decision(
        self,
        *,
        event_id: str,
        user_id: str,
        action: str,
        note: str | None,
        event_data_fingerprint: str,
        decided_at: datetime,
        idempotency_key: str,
    ) -> StoredUserDecision:
        self._mutation_fence.require()
        event_id = _bounded_text(event_id, "event_id", 255)
        user_id = _bounded_text(user_id, "user_id", 191)
        action = _bounded_text(action, "action", 32)
        if action not in _USER_DECISION_ACTIONS:
            raise ValueError("action must be an allowed user decision")
        if note == "":
            note = None
        note = _bounded_optional_text(note, "note", 1000)
        event_data_fingerprint = _required_fingerprint(
            event_data_fingerprint,
            "event_data_fingerprint",
        )
        decided_at = normalize_datetime(decided_at, "decided_at")
        idempotency_key = _bounded_text(
            idempotency_key,
            "idempotency_key",
            128,
        )
        if _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency_key has invalid format")
        payload_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                {
                    "action": action,
                    "event_data_fingerprint": event_data_fingerprint,
                    "event_id": event_id,
                    "idempotency_key": idempotency_key,
                    "note": note,
                    "user_id": user_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        decision_id = "user-decision:" + hashlib.sha256(
            f"{event_id}\0{user_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()

        with self._session_factory() as session:
            event_row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            if event_row is None:
                raise EventNotFoundError(event_id)
            self._require_current_strategy_event_row(event_row)
            event = _event_from_row(event_row)
            if event.rule_binding_status == "legacy_unbound":
                raise UserDecisionConflictError(
                    "legacy-unbound events are read-only"
                )
            if event.data_fingerprint != event_data_fingerprint:
                raise UserDecisionConflictError(
                    "event data fingerprint mismatch"
                )
            if decided_at < event.observed_at:
                raise ValueError("decided_at cannot be before the event")
            existing = session.scalar(
                select(TableByUserDecision).where(
                    TableByUserDecision.event_id == event_id,
                    TableByUserDecision.user_id == user_id,
                    TableByUserDecision.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.payload_fingerprint != payload_fingerprint:
                    raise UserDecisionConflictError(
                        "user decision idempotency conflict"
                    )
                return _user_decision_from_row(existing)
            row = TableByUserDecision(
                decision_id=decision_id,
                event_id=event_id,
                user_id=user_id,
                action=action,
                note=note,
                event_data_fingerprint=event_data_fingerprint,
                idempotency_key=idempotency_key,
                payload_fingerprint=payload_fingerprint,
                decided_at=decided_at,
            )
            session.add(row)
            try:
                session.commit()
                return _user_decision_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(
                    select(TableByUserDecision).where(
                        TableByUserDecision.event_id == event_id,
                        TableByUserDecision.user_id == user_id,
                        TableByUserDecision.idempotency_key
                        == idempotency_key,
                    )
                )
                if (
                    concurrent is not None
                    and concurrent.payload_fingerprint == payload_fingerprint
                ):
                    return _user_decision_from_row(concurrent)
                raise UserDecisionConflictError(
                    "user decision idempotency conflict"
                )

    def list_user_decisions(
        self,
        event_id: str,
    ) -> tuple[StoredUserDecision, ...]:
        event_id = _bounded_text(event_id, "event_id", 255)
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByUserDecision)
                .where(TableByUserDecision.event_id == event_id)
                .order_by(TableByUserDecision.id)
            ).all()
            return tuple(_user_decision_from_row(row) for row in rows)

    @mutation_fenced("decision_event_store.append_risk_snapshot")
    def append_risk_snapshot(self, snapshot: RiskSnapshot) -> RiskSnapshot:
        self._mutation_fence.require()
        if not isinstance(snapshot, RiskSnapshot):
            raise TypeError("snapshot must be RiskSnapshot")
        payload = _risk_snapshot_payload(snapshot)
        decision = snapshot.decision
        reasons_json = json.dumps(
            list(decision.reasons),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._session_factory() as session:
            self._begin_write_serialization(session)
            event_row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == snapshot.event_id
                ).with_for_update()
            )
            if event_row is None:
                raise EventNotFoundError(snapshot.event_id)
            self._require_current_strategy_event_row(event_row)
            event = _event_from_row(event_row)
            binding_reasons = snapshot.event_binding_reasons(event)
            if binding_reasons:
                raise RiskSnapshotConflictError(
                    "risk snapshot event binding mismatch: "
                    + ",".join(binding_reasons)
                )
            if snapshot.observed_at < event.observed_at:
                raise RiskSnapshotConflictError(
                    "risk snapshot observation predates event"
                )
            existing = session.scalar(
                select(TableByRiskSnapshot).where(
                    TableByRiskSnapshot.snapshot_id == snapshot.snapshot_id
                )
            )
            if existing is not None:
                stored = _risk_snapshot_from_row(existing)
                if (
                    existing.payload_fingerprint != snapshot.payload_fingerprint
                    or existing.payload_json != payload
                ):
                    raise RiskSnapshotConflictError(
                        "immutable risk snapshot conflict: "
                        + snapshot.snapshot_id
                    )
                return stored
            row = TableByRiskSnapshot(
                snapshot_id=snapshot.snapshot_id,
                identity_fingerprint=snapshot.identity_fingerprint,
                event_id=snapshot.event_id,
                event_data_fingerprint=snapshot.event_data_fingerprint,
                rule_id=snapshot.rule_id,
                rule_card_version=snapshot.rule_card_version,
                rule_card_fingerprint=snapshot.rule_card_fingerprint,
                rule_set_fingerprint=snapshot.rule_set_fingerprint,
                corpus_manifest_fingerprint=(
                    snapshot.corpus_manifest_fingerprint
                ),
                algorithm_fingerprint=snapshot.algorithm_fingerprint,
                evaluation_input_fingerprint=(
                    snapshot.evaluation_input_fingerprint
                ),
                observed_at=snapshot.observed_at,
                evaluated_at=snapshot.evaluated_at,
                expires_at=snapshot.expires_at,
                decision_allowed=decision.allowed,
                shares=decision.shares,
                planned_risk_cash=str(decision.planned_risk_cash),
                target_weight=str(decision.target_weight),
                entry_reference=str(decision.entry_reference),
                decision_reasons_json=reasons_json,
                daily_loss_locked=decision.daily_loss_locked,
                drawdown_locked=decision.drawdown_locked,
                payload_fingerprint=snapshot.payload_fingerprint,
                payload_json=payload,
            )
            session.add(row)
            try:
                session.commit()
                return _risk_snapshot_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(
                    select(TableByRiskSnapshot).where(
                        TableByRiskSnapshot.snapshot_id
                        == snapshot.snapshot_id
                    )
                )
                if concurrent is None:
                    concurrent = session.scalar(
                        select(TableByRiskSnapshot).where(
                            TableByRiskSnapshot.identity_fingerprint
                            == snapshot.identity_fingerprint
                        )
                    )
                if concurrent is not None:
                    stored = _risk_snapshot_from_row(concurrent)
                    if (
                        concurrent.payload_fingerprint
                        == snapshot.payload_fingerprint
                        and concurrent.payload_json == payload
                    ):
                        return stored
                raise RiskSnapshotConflictError(
                    "immutable risk snapshot conflict: "
                    + snapshot.snapshot_id
                )

    def get_risk_snapshot(self, snapshot_id: str) -> RiskSnapshot | None:
        snapshot_id = _bounded_text(snapshot_id, "snapshot_id", 255)
        with self._session_factory() as session:
            row = session.scalar(
                select(TableByRiskSnapshot).where(
                    TableByRiskSnapshot.snapshot_id == snapshot_id
                )
            )
            return None if row is None else _risk_snapshot_from_row(row)

    def list_risk_snapshots(
        self,
        event_id: str,
    ) -> tuple[RiskSnapshot, ...]:
        event_id = _bounded_text(event_id, "event_id", 255)
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByRiskSnapshot)
                .where(TableByRiskSnapshot.event_id == event_id)
                .order_by(TableByRiskSnapshot.evaluated_at, TableByRiskSnapshot.id)
            ).all()
            return tuple(_risk_snapshot_from_row(row) for row in rows)

    @mutation_fenced(
        "decision_event_store.issue_paper_admission_authorization"
    )
    def issue_paper_admission_authorization(
        self,
        *,
        event_id: str,
    ) -> StoredPaperAdmissionAuthorization:
        """Atomically freeze the authoritative entry identities in the event DB.

        This is the paper-admission authorization point.  Later event or risk
        changes remain later audit facts; they cannot rewrite this immutable
        record or silently change the paper intent identity.
        """

        self._mutation_fence.require()
        event_id = _bounded_text(event_id, "event_id", 255)
        with self._session_factory() as session:
            self._begin_write_serialization(session)
            event_row = session.scalar(
                select(TableByDecisionEvent)
                .where(TableByDecisionEvent.event_id == event_id)
                .with_for_update()
            )
            if event_row is None:
                raise EventNotFoundError(event_id)
            self._require_current_strategy_event_row(event_row)

            existing_row = session.scalar(
                select(TableByPaperAdmissionAuthorization).where(
                    TableByPaperAdmissionAuthorization.event_id == event_id
                )
            )
            if existing_row is not None:
                return _paper_authorization_from_row(existing_row)
            authorized_at = self._coordination_now(session)

            event = _event_from_row(event_row)
            event_data_fingerprint = event.data_fingerprint
            transition_rows = session.scalars(
                select(TableByDecisionTransition)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id)
            ).all()
            current_state = (
                EventState.DETECTED
                if not transition_rows
                else EventState(transition_rows[-1].to_state)
            )
            if current_state is not EventState.CONFIRMED:
                raise InvalidEventTransition(
                    "paper authorization requires confirmed event state"
                )
            review_pending_transitions = tuple(
                row
                for row in transition_rows
                if row.from_state == EventState.RISK_CHECKED.value
                and row.to_state == EventState.REVIEW_PENDING.value
            )
            if len(review_pending_transitions) != 1:
                raise InvalidEventTransition(
                    "paper authorization review-pending transition mismatch"
                )
            review_pending = review_pending_transitions[0]
            manual_check_pending_id: str | None = None
            manual_check_payload_fingerprint: str | None = None
            if (
                review_pending.actor == "event_service"
                and review_pending.reason == "risk_allowed"
            ):
                pass
            elif review_pending.actor == MANUAL_CHECK_TRANSITION_ACTOR:
                try:
                    manual_binding = parse_manual_check_transition(
                        review_pending.actor,
                        review_pending.reason,
                    )
                except ValueError as exc:
                    raise InvalidEventTransition(
                        "paper authorization review-pending transition mismatch"
                    ) from exc
                if manual_binding is None:
                    raise InvalidEventTransition(
                        "paper authorization review-pending transition mismatch"
                    )
                (
                    manual_check_pending_id,
                    manual_check_payload_fingerprint,
                ) = manual_binding
            else:
                raise InvalidEventTransition(
                    "paper authorization review-pending transition mismatch"
                )
            confirmations = tuple(
                row
                for row in transition_rows
                if row.from_state == EventState.REVIEW_PENDING.value
                and row.to_state == EventState.CONFIRMED.value
                and row.reason == "review_verdict:CONFIRM"
                and row.actor.startswith("review:")
            )
            if len(confirmations) != 1:
                raise InvalidEventTransition(
                    "paper authorization confirmation transition mismatch"
                )
            confirmation = confirmations[0]
            review_id = _bounded_text(
                confirmation.actor.removeprefix("review:"),
                "review_id",
                255,
            )

            risk_rows = session.scalars(
                select(TableByRiskSnapshot)
                .where(TableByRiskSnapshot.event_id == event_id)
                .order_by(
                    TableByRiskSnapshot.evaluated_at,
                    TableByRiskSnapshot.id,
                )
            ).all()
            if not risk_rows:
                raise InvalidEventTransition(
                    "paper authorization risk snapshot is missing"
                )
            risk_snapshot = _risk_snapshot_from_row(risk_rows[-1])
            risk_snapshot_id = risk_snapshot.snapshot_id
            validation = risk_snapshot.validate_for_review(
                event,
                as_of=authorized_at,
            )
            if not validation.usable:
                raise InvalidEventTransition(
                    "paper authorization risk snapshot unusable: "
                    + ",".join(validation.reasons)
                )

            review_row = session.scalar(
                select(TableByLLMReview).where(
                    TableByLLMReview.review_id == review_id
                )
            )
            if review_row is None:
                raise InvalidEventTransition(
                    "paper authorization trusted review missing"
                )
            review = _llm_review_from_row(review_row)
            packet_fingerprint = review.packet_fingerprint
            claim_row = session.scalar(
                select(TableByLLMReviewClaim).where(
                    TableByLLMReviewClaim.review_id == review_id
                )
            )
            if (
                claim_row is None
                or claim_row.event_id != event_id
                or claim_row.packet_fingerprint != packet_fingerprint
                or claim_row.provider != review.provider
                or claim_row.model != review.model
                or claim_row.prompt_version != review.prompt_version
                or claim_row.fencing_token != review.fencing_token
                or not bool(claim_row.finalized)
            ):
                raise InvalidEventTransition(
                    "paper authorization trusted review claim mismatch"
                )
            if (
                review.event_id != event_id
                or review.risk_snapshot_id != risk_snapshot_id
                or review.reviewed_data_fingerprint != event_data_fingerprint
                or review.packet_fingerprint != packet_fingerprint
                or review.status != "validated"
                or review.provider_ok is not True
                or review.verdict != "CONFIRM"
                or review.validation_errors
                or review.error_code is not None
                or review.error_message is not None
                or review.response_content is None
                or review.parsed_response_json is None
                or review.response_content_truncated
                or review.raw_response_truncated
                or review.created_at
                != normalize_datetime(
                    confirmation.occurred_at,
                    "confirmation.occurred_at",
                )
                or review.created_at > authorized_at
            ):
                reason = (
                    "paper authorization review risk snapshot mismatch"
                    if review.risk_snapshot_id != risk_snapshot_id
                    else "paper authorization trusted review mismatch"
                )
                raise InvalidEventTransition(reason)
            content_bytes = review.response_content.encode("utf-8")
            if (
                len(content_bytes) != review.response_content_bytes
                or "sha256:" + hashlib.sha256(content_bytes).hexdigest()
                != review.response_content_sha256
            ):
                raise InvalidEventTransition(
                    "paper authorization trusted review content mismatch"
                )
            expected_review_binding = {
                "verdict": "CONFIRM",
                "reviewed_event_id": event_id,
                "reviewed_data_fingerprint": event_data_fingerprint,
                "reviewed_packet_fingerprint": packet_fingerprint,
            }
            for field_name, raw_payload in (
                ("content", review.response_content),
                ("parsed", review.parsed_response_json),
            ):
                try:
                    review_payload = json.loads(raw_payload)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise InvalidEventTransition(
                        "paper authorization trusted review "
                        + field_name
                        + " mismatch"
                    ) from exc
                if not isinstance(review_payload, dict) or any(
                    review_payload.get(key) != value
                    for key, value in expected_review_binding.items()
                ):
                    raise InvalidEventTransition(
                        "paper authorization trusted review "
                        + field_name
                        + " mismatch"
                    )

            identity = {
                "event_id": event_id,
                "event_data_fingerprint": event_data_fingerprint,
                "review_id": review_id,
                "risk_snapshot_id": risk_snapshot_id,
                "confirmation_transition_id": confirmation.id,
                "manual_check_pending_id": manual_check_pending_id,
                "manual_check_payload_fingerprint": (
                    manual_check_payload_fingerprint
                ),
                "packet_fingerprint": packet_fingerprint,
            }
            identity_fingerprint = sha256_json(identity)
            authorization_id = "paper-auth:" + identity_fingerprint[7:]
            payload = {
                "schema_version": 2,
                "authorization_id": authorization_id,
                **identity,
                "authorized_at": authorized_at.isoformat(),
                "risk_expires_at": risk_snapshot.expires_at.isoformat(),
            }
            payload_fingerprint = sha256_json(payload)
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            row = TableByPaperAdmissionAuthorization(
                authorization_id=authorization_id,
                event_id=event_id,
                event_data_fingerprint=event_data_fingerprint,
                review_id=review_id,
                risk_snapshot_id=risk_snapshot_id,
                confirmation_transition_id=confirmation.id,
                manual_check_pending_id=manual_check_pending_id,
                manual_check_payload_fingerprint=(
                    manual_check_payload_fingerprint
                ),
                packet_fingerprint=packet_fingerprint,
                authorized_at=authorized_at,
                risk_expires_at=risk_snapshot.expires_at,
                payload_fingerprint=payload_fingerprint,
                payload_json=payload_json,
            )
            session.add(row)
            try:
                session.commit()
                return _paper_authorization_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent_row = session.scalar(
                    select(TableByPaperAdmissionAuthorization).where(
                        TableByPaperAdmissionAuthorization.event_id == event_id
                    )
                )
                if concurrent_row is not None:
                    return _paper_authorization_from_row(concurrent_row)
                raise EventConflictError(
                    "paper admission authorization identity conflict"
                )

    def get_paper_admission_authorization(
        self,
        event_id: str,
    ) -> StoredPaperAdmissionAuthorization | None:
        event_id = _bounded_text(event_id, "event_id", 255)
        with self._session_factory() as session:
            row = session.scalar(
                select(TableByPaperAdmissionAuthorization).where(
                    TableByPaperAdmissionAuthorization.event_id == event_id
                )
            )
            return None if row is None else _paper_authorization_from_row(row)

    def get_risk_snapshot_for_review(
        self,
        event: DecisionEvent,
        *,
        as_of: datetime,
    ) -> RiskSnapshot | None:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        as_of = normalize_datetime(as_of, "as_of")
        with self._session_factory() as session:
            row = session.scalar(
                select(TableByRiskSnapshot)
                .where(TableByRiskSnapshot.event_id == event.event_id)
                .order_by(
                    TableByRiskSnapshot.evaluated_at.desc(),
                    TableByRiskSnapshot.id.desc(),
                )
                .limit(1)
            )
            if row is None:
                return None
            snapshot = _risk_snapshot_from_row(row)
            validation = snapshot.validate_for_review(event, as_of=as_of)
            return snapshot if validation.usable else None

    @mutation_fenced("decision_event_store.append_risk_latch_audit")
    def append_risk_latch_audit(
        self,
        audit: RiskLatchAudit,
    ) -> RiskLatchAudit:
        self._mutation_fence.require()
        if not isinstance(audit, RiskLatchAudit):
            raise TypeError("audit must be RiskLatchAudit")
        payload = _risk_latch_payload(audit)
        with self._session_factory() as session:
            self._begin_write_serialization(session)
            locked_event_id = session.scalar(
                _risk_latch_event_lock_statement(audit.event_id)
            )
            if locked_event_id is None:
                raise RiskLatchAuditConflictError(
                    "risk latch event is missing"
                )
            self._require_current_strategy_event_id(
                session,
                audit.event_id,
            )
            snapshot_row = session.scalar(
                select(TableByRiskSnapshot).where(
                    TableByRiskSnapshot.snapshot_id == audit.snapshot_id
                )
            )
            if snapshot_row is None:
                raise RiskLatchAuditConflictError(
                    "risk latch source snapshot is missing"
                )
            snapshot = _risk_snapshot_from_row(snapshot_row)
            if snapshot.event_id != audit.event_id:
                raise RiskLatchAuditConflictError(
                    "risk latch event binding mismatch"
                )
            if audit.occurred_at < snapshot.evaluated_at:
                raise RiskLatchAuditConflictError(
                    "risk latch audit predates source snapshot"
                )
            source_locked = (
                snapshot.decision.daily_loss_locked
                if audit.latch_kind is RiskLatchKind.DAILY_LOSS
                else snapshot.decision.drawdown_locked
            )
            if not source_locked:
                raise RiskLatchAuditConflictError(
                    "risk latch source snapshot is not locked"
                )
            existing = session.scalar(
                select(TableByRiskLatchAudit).where(
                    TableByRiskLatchAudit.audit_id == audit.audit_id
                )
            )
            if existing is not None:
                stored = _risk_latch_from_row(existing)
                if (
                    existing.payload_fingerprint != audit.payload_fingerprint
                    or existing.payload_json != payload
                ):
                    raise RiskLatchAuditConflictError(
                        "immutable risk latch audit conflict: " + audit.audit_id
                    )
                return stored
            latest_row = session.scalar(
                select(TableByRiskLatchAudit)
                .where(
                    TableByRiskLatchAudit.event_id == audit.event_id,
                    TableByRiskLatchAudit.latch_kind
                    == audit.latch_kind.value,
                )
                .order_by(
                    TableByRiskLatchAudit.occurred_at.desc(),
                    TableByRiskLatchAudit.id.desc(),
                )
                .limit(1)
            )
            if latest_row is not None:
                latest = _risk_latch_from_row(latest_row)
                if audit.occurred_at < latest.occurred_at:
                    raise RiskLatchAuditConflictError(
                        "risk latch audit time cannot move backwards"
                    )
                if audit.previous_locked is not latest.current_locked:
                    raise RiskLatchAuditConflictError(
                        "risk latch audit state is not append-contiguous"
                    )
            elif audit.action.value == "manual_reset":
                raise RiskLatchAuditConflictError(
                    "manual reset requires a prior latch audit"
                )
            row = TableByRiskLatchAudit(
                audit_id=audit.audit_id,
                identity_fingerprint=audit.identity_fingerprint,
                event_id=audit.event_id,
                snapshot_id=audit.snapshot_id,
                latch_kind=audit.latch_kind.value,
                action=audit.action.value,
                previous_locked=audit.previous_locked,
                current_locked=audit.current_locked,
                actor=audit.actor,
                reason=audit.reason,
                occurred_at=audit.occurred_at,
                payload_fingerprint=audit.payload_fingerprint,
                payload_json=payload,
            )
            session.add(row)
            try:
                session.commit()
                return _risk_latch_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(
                    select(TableByRiskLatchAudit).where(
                        TableByRiskLatchAudit.audit_id == audit.audit_id
                    )
                )
                if concurrent is None:
                    concurrent = session.scalar(
                        select(TableByRiskLatchAudit).where(
                            TableByRiskLatchAudit.identity_fingerprint
                            == audit.identity_fingerprint
                        )
                    )
                if concurrent is not None:
                    stored = _risk_latch_from_row(concurrent)
                    if (
                        concurrent.payload_fingerprint
                        == audit.payload_fingerprint
                        and concurrent.payload_json == payload
                    ):
                        return stored
                raise RiskLatchAuditConflictError(
                    "immutable risk latch audit conflict: " + audit.audit_id
                )

    def list_risk_latch_audits(
        self,
        event_id: str,
    ) -> tuple[RiskLatchAudit, ...]:
        event_id = _bounded_text(event_id, "event_id", 255)
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByRiskLatchAudit)
                .where(TableByRiskLatchAudit.event_id == event_id)
                .order_by(TableByRiskLatchAudit.id)
            ).all()
            return tuple(_risk_latch_from_row(row) for row in rows)

    @mutation_fenced("decision_event_store.append_transition")
    def append_transition(
        self,
        event_id: str,
        from_state: EventState | str,
        to_state: EventState | str,
        *,
        occurred_at: datetime,
        reason: str,
        actor: str,
    ) -> StoredTransition:
        self._mutation_fence.require()
        return self.append_transition_chain(
            event_id,
            (
                TransitionSpec(
                    from_state=_state(from_state, "from_state"),
                    to_state=_state(to_state, "to_state"),
                    occurred_at=occurred_at,
                    reason=reason,
                    actor=actor,
                ),
            ),
        )[0]

    @mutation_fenced("decision_event_store.append_transition_chain")
    def append_transition_chain(
        self,
        event_id: str,
        transitions: Sequence[TransitionSpec],
    ) -> tuple[StoredTransition, ...]:
        self._mutation_fence.require()
        event_id = _required_text(event_id, "event_id")
        if isinstance(transitions, (str, bytes)) or not isinstance(
            transitions,
            Sequence,
        ):
            raise TypeError("transitions must be a sequence")
        if not transitions:
            raise ValueError("transitions cannot be empty")

        normalized: list[TransitionSpec] = []
        seen_sources: set[EventState] = set()
        for item in transitions:
            if not isinstance(item, TransitionSpec):
                raise TypeError("transitions must contain TransitionSpec values")
            source = _state(item.from_state, "from_state")
            target = _state(item.to_state, "to_state")
            if source in seen_sources:
                raise InvalidEventTransition("transition chain repeats from_state")
            seen_sources.add(source)
            if target not in _ALLOWED_TRANSITIONS[source]:
                raise InvalidEventTransition(
                    f"illegal transition: {source.value} -> {target.value}"
                )
            normalized.append(
                TransitionSpec(
                    source,
                    target,
                    normalize_datetime(item.occurred_at, "occurred_at"),
                    _required_text(item.reason, "reason"),
                    _required_text(item.actor, "actor"),
                )
            )
        for previous, current in zip(normalized, normalized[1:]):
            if previous.to_state is not current.from_state:
                raise InvalidEventTransition("transition chain is not contiguous")

        with self._session_factory() as session:
            self._begin_write_serialization(session)
            event_row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == event_id
                ).with_for_update()
            )
            if event_row is None:
                raise EventNotFoundError(event_id)
            self._require_current_strategy_event_row(event_row)
            existing_rows = session.scalars(
                select(TableByDecisionTransition)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id)
            ).all()
            by_source = {
                EventState(row.from_state): row for row in existing_rows
            }
            current_state = (
                EventState.DETECTED
                if not existing_rows
                else EventState(existing_rows[-1].to_state)
            )
            earliest = (
                event_row.observed_at
                if not existing_rows
                else existing_rows[-1].occurred_at
            )
            result_rows: list[TableByDecisionTransition] = []
            new_rows: list[TableByDecisionTransition] = []
            for item in normalized:
                existing = by_source.get(item.from_state)
                if existing is not None:
                    if not self._same_transition(
                        existing,
                        item.to_state,
                        item.reason,
                        item.actor,
                    ):
                        raise EventStateConflictError(
                            f"current state is {current_state.value}, "
                            f"not {item.from_state.value}"
                        )
                    result_rows.append(existing)
                    continue
                if current_state is not item.from_state:
                    raise EventStateConflictError(
                        f"current state is {current_state.value}, "
                        f"not {item.from_state.value}"
                    )
                if item.occurred_at < earliest:
                    raise InvalidEventTransition(
                        "transition time cannot move backwards"
                    )
                row = TableByDecisionTransition(
                    event_id=event_id,
                    from_state=item.from_state.value,
                    to_state=item.to_state.value,
                    occurred_at=item.occurred_at,
                    reason=item.reason,
                    actor=item.actor,
                )
                session.add(row)
                result_rows.append(row)
                new_rows.append(row)
                current_state = item.to_state
                earliest = item.occurred_at

            if not new_rows:
                return tuple(_transition_from_row(row) for row in result_rows)
            try:
                session.commit()
                return tuple(
                    _transition_from_row(row) for row in result_rows
                )
            except IntegrityError:
                session.rollback()
                concurrent_rows = session.scalars(
                    select(TableByDecisionTransition).where(
                        TableByDecisionTransition.event_id == event_id,
                        TableByDecisionTransition.from_state.in_(
                            [item.from_state.value for item in normalized]
                        ),
                    )
                ).all()
                concurrent_by_source = {
                    EventState(row.from_state): row for row in concurrent_rows
                }
                if all(
                    item.from_state in concurrent_by_source
                    and self._same_transition(
                        concurrent_by_source[item.from_state],
                        item.to_state,
                        item.reason,
                        item.actor,
                    )
                    for item in normalized
                ):
                    return tuple(
                        _transition_from_row(concurrent_by_source[item.from_state])
                        for item in normalized
                    )
                raise EventStateConflictError(
                    "concurrent transition chain conflict"
                )

    @staticmethod
    def _same_transition(
        row: TableByDecisionTransition,
        target: EventState,
        reason: str,
        actor: str,
    ) -> bool:
        return (
            row.to_state == target.value
            and row.reason == reason
            and row.actor == actor
        )

    def list_transitions(
        self,
        event_id: str,
    ) -> tuple[StoredTransition, ...]:
        event_id = _required_text(event_id, "event_id")
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByDecisionTransition)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id)
            ).all()
            return tuple(_transition_from_row(row) for row in rows)

    def current_state(self, event_id: str) -> EventState:
        event_id = _required_text(event_id, "event_id")
        with self._session_factory() as session:
            event_exists = session.scalar(
                select(TableByDecisionEvent.id).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            if event_exists is None:
                raise EventNotFoundError(event_id)
            state = session.scalar(
                select(TableByDecisionTransition.to_state)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id.desc())
                .limit(1)
            )
            return EventState.DETECTED if state is None else EventState(state)

    @mutation_fenced("decision_event_store.append_review_application")
    def append_review_application(
        self,
        *,
        review_id: str,
        event_id: str,
        reviewed_data_fingerprint: str,
        verdict: str,
        reviewed_at: datetime,
        target_state: EventState | str,
        transition_reason: str,
        actor: str,
        expire_if_pending: bool = False,
    ) -> StoredReview:
        self._mutation_fence.require()
        review_id = _required_text(review_id, "review_id")
        event_id = _required_text(event_id, "event_id")
        reviewed_data_fingerprint = _required_text(
            reviewed_data_fingerprint,
            "reviewed_data_fingerprint",
        )
        verdict = _required_text(verdict, "verdict")
        reviewed_at = normalize_datetime(reviewed_at, "reviewed_at")
        target = _state(target_state, "target_state")
        transition_reason = _required_text(
            transition_reason,
            "transition_reason",
        )
        actor = _required_text(actor, "actor")
        if not isinstance(expire_if_pending, bool):
            raise TypeError("expire_if_pending must be bool")
        if target not in _ALLOWED_TRANSITIONS[EventState.REVIEW_PENDING]:
            raise InvalidEventTransition("invalid review target state")

        request = (
            event_id,
            reviewed_data_fingerprint,
            verdict,
            reviewed_at,
        )
        with self._session_factory() as session:
            self._begin_write_serialization(session)
            self._require_current_strategy_event_id(session, event_id)
            existing_review = session.scalar(
                select(TableByDecisionReview).where(
                    TableByDecisionReview.review_id == review_id
                )
            )
            if existing_review is not None:
                if self._same_review_request(existing_review, request):
                    return _review_from_row(existing_review)
                raise ReviewConflictError(
                    f"review_id already used with different payload: {review_id}"
                )

            event_row = session.scalar(
                select(TableByDecisionEvent).where(
                    TableByDecisionEvent.event_id == event_id
                ).with_for_update()
            )
            if event_row is None:
                raise EventNotFoundError(event_id)
            transition_rows = session.scalars(
                select(TableByDecisionTransition)
                .where(TableByDecisionTransition.event_id == event_id)
                .order_by(TableByDecisionTransition.id)
            ).all()
            current = (
                EventState.DETECTED
                if not transition_rows
                else EventState(transition_rows[-1].to_state)
            )
            earliest = (
                event_row.observed_at
                if not transition_rows
                else transition_rows[-1].occurred_at
            )

            transition_row: TableByDecisionTransition | None = None
            if current is not EventState.REVIEW_PENDING:
                applied = False
                outcome_state = current
                if event_row.data_fingerprint != reviewed_data_fingerprint:
                    outcome_reason = "stale_data_fingerprint"
                else:
                    outcome_reason = (
                        "review_transition_conflict"
                        if current in _REVIEW_OUTCOME_STATES
                        else "event_not_review_pending"
                    )
            elif reviewed_at < earliest:
                applied = False
                outcome_state = current
                outcome_reason = "review_time_before_state"
            elif expire_if_pending:
                applied = False
                outcome_state = EventState.EXPIRED
                outcome_reason = (
                    "stale_data_fingerprint"
                    if event_row.data_fingerprint != reviewed_data_fingerprint
                    else "event_not_review_pending"
                )
                transition_row = TableByDecisionTransition(
                    event_id=event_id,
                    from_state=EventState.REVIEW_PENDING.value,
                    to_state=EventState.EXPIRED.value,
                    occurred_at=reviewed_at,
                    reason="freshness_window_exceeded",
                    actor="event_service",
                )
                session.add(transition_row)
            elif event_row.data_fingerprint != reviewed_data_fingerprint:
                applied = False
                outcome_state = current
                outcome_reason = "stale_data_fingerprint"
            else:
                applied = True
                outcome_state = target
                outcome_reason = "applied"
                transition_row = TableByDecisionTransition(
                    event_id=event_id,
                    from_state=EventState.REVIEW_PENDING.value,
                    to_state=target.value,
                    occurred_at=reviewed_at,
                    reason=transition_reason,
                    actor=actor,
                )
                session.add(transition_row)

            review_row = TableByDecisionReview(
                review_id=review_id,
                event_id=event_id,
                reviewed_data_fingerprint=reviewed_data_fingerprint,
                verdict=verdict,
                reviewed_at=reviewed_at,
                applied=applied,
                state=outcome_state.value,
                reason=outcome_reason,
            )
            session.add(review_row)
            try:
                session.commit()
                return _review_from_row(review_row)
            except IntegrityError:
                session.rollback()
                concurrent_review = session.scalar(
                    select(TableByDecisionReview).where(
                        TableByDecisionReview.review_id == review_id
                    )
                )
                if concurrent_review is not None:
                    if self._same_review_request(concurrent_review, request):
                        return _review_from_row(concurrent_review)
                    raise ReviewConflictError(
                        "review_id already used with different payload: "
                        f"{review_id}"
                    )

                current_value = session.scalar(
                    select(TableByDecisionTransition.to_state)
                    .where(TableByDecisionTransition.event_id == event_id)
                    .order_by(TableByDecisionTransition.id.desc())
                    .limit(1)
                )
                conflict_state = (
                    EventState.DETECTED
                    if current_value is None
                    else EventState(current_value)
                )
                conflict_row = TableByDecisionReview(
                    review_id=review_id,
                    event_id=event_id,
                    reviewed_data_fingerprint=reviewed_data_fingerprint,
                    verdict=verdict,
                    reviewed_at=reviewed_at,
                    applied=False,
                    state=conflict_state.value,
                    reason="review_transition_conflict",
                )
                session.add(conflict_row)
                session.commit()
                return _review_from_row(conflict_row)

    @staticmethod
    def _same_review_request(
        row: TableByDecisionReview,
        request: tuple[str, str, str, datetime],
    ) -> bool:
        event_id, data_fingerprint, verdict, reviewed_at = request
        return (
            row.event_id == event_id
            and row.reviewed_data_fingerprint == data_fingerprint
            and row.verdict == verdict
            and normalize_datetime(row.reviewed_at, "reviewed_at") == reviewed_at
        )

    def list_reviews(self, event_id: str) -> tuple[StoredReview, ...]:
        event_id = _required_text(event_id, "event_id")
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByDecisionReview)
                .where(TableByDecisionReview.event_id == event_id)
                .order_by(TableByDecisionReview.id)
            ).all()
            return tuple(_review_from_row(row) for row in rows)

    def count_reviews(self, event_id: str | None = None) -> int:
        statement = select(func.count(TableByDecisionReview.id))
        if event_id is not None:
            statement = statement.where(
                TableByDecisionReview.event_id
                == _required_text(event_id, "event_id")
            )
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    @mutation_fenced("decision_event_store.acquire_llm_review_claim")
    def acquire_llm_review_claim(
        self,
        *,
        review_id: str,
        event_id: str,
        packet_fingerprint: str,
        provider: str,
        model: str,
        prompt_version: str,
        owner_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LLMReviewClaim:
        self._mutation_fence.require()
        review_id = _bounded_text(review_id, "review_id", 255)
        event_id = _required_text(event_id, "event_id")
        packet_fingerprint = _required_fingerprint(
            packet_fingerprint,
            "packet_fingerprint",
        )
        provider = _bounded_text(provider, "provider", 40)
        model = _bounded_text(model, "model", 191)
        prompt_version = _bounded_text(prompt_version, "prompt_version", 64)
        owner_token = _bounded_text(owner_token, "owner_token", 64)
        now = normalize_datetime(now, "now")
        lease_expires_at = normalize_datetime(
            lease_expires_at,
            "lease_expires_at",
        )
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")
        lease_seconds = (lease_expires_at - now).total_seconds()
        if lease_seconds > _MAX_CLAIM_LEASE_SECONDS:
            raise ValueError("LLM review claim lease exceeds 24 hours")
        identity = (
            event_id,
            packet_fingerprint,
            provider,
            model,
            prompt_version,
        )

        def identity_query():
            return select(TableByLLMReviewClaim).where(
                TableByLLMReviewClaim.event_id == identity[0],
                TableByLLMReviewClaim.packet_fingerprint == identity[1],
                TableByLLMReviewClaim.provider == identity[2],
                TableByLLMReviewClaim.model == identity[3],
                TableByLLMReviewClaim.prompt_version == identity[4],
            )

        with self._session_factory() as session:
            self._require_current_strategy_event_id(session, event_id)
            coordination_now = self._coordination_now(session)
            actual_lease_expires_at = coordination_now + timedelta(
                seconds=lease_seconds
            )
            existing = session.scalar(identity_query())
            if existing is not None:
                if existing.review_id != review_id:
                    raise EventConflictError(
                        "LLM review identity has an unexpected review_id"
                    )
                if bool(existing.finalized):
                    return _llm_claim_from_row(existing, acquired=False)
                existing_lease = normalize_datetime(
                    existing.lease_expires_at,
                    "lease_expires_at",
                )
                if (
                    existing.owner_token == owner_token
                    and existing_lease > coordination_now
                ):
                    if existing_lease < actual_lease_expires_at:
                        result = session.execute(
                            update(TableByLLMReviewClaim)
                            .where(
                                TableByLLMReviewClaim.id == existing.id,
                                TableByLLMReviewClaim.owner_token == owner_token,
                                TableByLLMReviewClaim.fencing_token
                                == existing.fencing_token,
                                TableByLLMReviewClaim.finalized.is_(False),
                                TableByLLMReviewClaim.lease_expires_at
                                == existing.lease_expires_at,
                            )
                            .values(
                                lease_expires_at=actual_lease_expires_at
                            )
                        )
                        if result.rowcount == 1:
                            session.commit()
                            renewed = session.scalar(identity_query())
                            if renewed is None:
                                raise EventConflictError(
                                    "LLM review claim disappeared"
                                )
                            return _llm_claim_from_row(
                                renewed,
                                acquired=renewed.owner_token == owner_token,
                            )
                        session.rollback()
                        current = session.scalar(identity_query())
                        if current is None:
                            raise EventConflictError(
                                "LLM review claim disappeared"
                            )
                        return _llm_claim_from_row(current, acquired=False)
                    return _llm_claim_from_row(existing, acquired=True)
                if existing_lease > coordination_now:
                    return _llm_claim_from_row(existing, acquired=False)
                result = session.execute(
                    update(TableByLLMReviewClaim)
                    .where(
                        TableByLLMReviewClaim.id == existing.id,
                        TableByLLMReviewClaim.owner_token
                        == existing.owner_token,
                        TableByLLMReviewClaim.fencing_token
                        == existing.fencing_token,
                        TableByLLMReviewClaim.finalized.is_(False),
                        TableByLLMReviewClaim.lease_expires_at
                        == existing.lease_expires_at,
                    )
                    .values(
                        owner_token=owner_token,
                        fencing_token=existing.fencing_token + 1,
                        lease_expires_at=actual_lease_expires_at,
                    )
                )
                if result.rowcount == 1:
                    session.commit()
                    claimed = session.scalar(identity_query())
                    if claimed is None:
                        raise EventConflictError("LLM review claim disappeared")
                    return _llm_claim_from_row(claimed, acquired=True)
                session.rollback()
                current = session.scalar(identity_query())
                if current is None:
                    raise EventConflictError("LLM review claim disappeared")
                return _llm_claim_from_row(current, acquired=False)

            reused_id = session.scalar(
                select(TableByLLMReviewClaim).where(
                    TableByLLMReviewClaim.review_id == review_id
                )
            )
            if reused_id is not None:
                raise ReviewConflictError(
                    f"review_id already used with different identity: {review_id}"
                )
            event_exists = session.scalar(
                select(TableByDecisionEvent.id).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            if event_exists is None:
                raise EventNotFoundError(event_id)
            row = TableByLLMReviewClaim(
                review_id=review_id,
                event_id=event_id,
                packet_fingerprint=packet_fingerprint,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                owner_token=owner_token,
                fencing_token=1,
                lease_expires_at=actual_lease_expires_at,
                finalized=False,
                created_at=coordination_now,
            )
            session.add(row)
            try:
                session.commit()
                return _llm_claim_from_row(row, acquired=True)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(identity_query())
                if concurrent is not None:
                    if concurrent.review_id != review_id:
                        raise EventConflictError(
                            "LLM review identity has an unexpected review_id"
                        )
                    return _llm_claim_from_row(concurrent, acquired=False)
                raise

    @mutation_fenced("decision_event_store.append_llm_review_attempt")
    def append_llm_review_attempt(
        self,
        *,
        attempt_id: str,
        review_id: str,
        event_id: str,
        owner_token: str,
        fencing_token: int,
        attempt_number: int,
        provider: str,
        model: str,
        ok: bool,
        retryable: bool,
        response_content: str | None,
        raw_response: str,
        error_code: str | None,
        error_message: str | None,
        latency_ms: int,
        started_at: datetime,
        completed_at: datetime,
    ) -> StoredLLMReviewAttempt:
        self._mutation_fence.require()
        attempt_id = _bounded_text(attempt_id, "attempt_id", 255)
        review_id = _bounded_text(review_id, "review_id", 255)
        event_id = _required_text(event_id, "event_id")
        owner_token = _bounded_text(owner_token, "owner_token", 64)
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token must be a positive integer")
        provider = _bounded_text(provider, "provider", 40)
        model = _bounded_text(model, "model", 191)
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number <= 0
        ):
            raise ValueError("attempt_number must be a positive integer")
        if type(ok) is not bool or type(retryable) is not bool:
            raise TypeError("ok and retryable must be boolean")
        content_audit = _bounded_audit_text(
            response_content,
            "response_content",
            allow_none=True,
        )
        raw_audit = _bounded_audit_text(
            raw_response,
            "raw_response",
            allow_none=False,
        )
        error_code = _bounded_optional_text(error_code, "error_code", 100)
        if error_message is not None:
            _required_text(error_message, "error_message")
        error_audit = _bounded_audit_text(
            error_message,
            "error_message",
            allow_none=True,
            max_bytes=MAX_LLM_ERROR_BYTES,
        )
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        started_at = normalize_datetime(started_at, "started_at")
        completed_at = normalize_datetime(completed_at, "completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at cannot be before started_at")
        if ok:
            if not response_content:
                raise ValueError("successful attempt requires response content")
            if content_audit.truncated or raw_audit.truncated:
                raise ValueError("successful attempt exceeds the audit limit")
            if error_code is not None or error_audit.text is not None or retryable:
                raise ValueError("successful attempt cannot carry failure metadata")
        elif error_code is None:
            raise ValueError("failed attempt requires failure metadata")

        with self._session_factory() as session:
            self._require_current_strategy_event_id(session, event_id)
            existing = session.scalar(
                select(TableByLLMReviewAttempt).where(
                    TableByLLMReviewAttempt.attempt_id == attempt_id
                )
            )
            if existing is not None:
                return _llm_attempt_from_row(existing)
            claim = session.scalar(
                select(TableByLLMReviewClaim).where(
                    TableByLLMReviewClaim.review_id == review_id,
                    TableByLLMReviewClaim.event_id == event_id,
                )
            )
            if claim is None:
                raise LLMReviewClaimLostError("LLM review claim is missing")
            if fencing_token > claim.fencing_token:
                raise LLMReviewClaimLostError(
                    "LLM review attempt has a future fencing token"
                )
            if claim.provider != provider or claim.model != model:
                raise ReviewConflictError(
                    "LLM review attempt identity does not match claim"
                )
            if (
                bool(claim.finalized)
                and claim.owner_token == owner_token
                and claim.fencing_token == fencing_token
            ):
                raise LLMReviewClaimLostError("LLM review claim is finalized")
            row = TableByLLMReviewAttempt(
                attempt_id=attempt_id,
                review_id=review_id,
                event_id=event_id,
                owner_token=owner_token,
                fencing_token=fencing_token,
                attempt_number=attempt_number,
                provider=provider,
                model=model,
                ok=ok,
                retryable=retryable,
                response_content=content_audit.text,
                response_content_bytes=content_audit.original_bytes,
                response_content_sha256=content_audit.sha256,
                response_content_truncated=content_audit.truncated,
                raw_response=raw_audit.text,
                raw_response_bytes=raw_audit.original_bytes,
                raw_response_sha256=raw_audit.sha256,
                raw_response_truncated=raw_audit.truncated,
                error_code=error_code,
                error_message=error_audit.text,
                error_message_bytes=error_audit.original_bytes,
                error_message_sha256=error_audit.sha256,
                error_message_truncated=error_audit.truncated,
                latency_ms=latency_ms,
                started_at=started_at,
                completed_at=completed_at,
            )
            session.add(row)
            try:
                session.commit()
                return _llm_attempt_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(
                    select(TableByLLMReviewAttempt).where(
                        TableByLLMReviewAttempt.attempt_id == attempt_id
                    )
                )
                if concurrent is not None:
                    return _llm_attempt_from_row(concurrent)
                raise

    def find_llm_review(
        self,
        *,
        event_id: str,
        packet_fingerprint: str,
        provider: str,
        model: str,
        prompt_version: str,
    ) -> StoredLLMReview | None:
        event_id = _required_text(event_id, "event_id")
        packet_fingerprint = _required_fingerprint(
            packet_fingerprint,
            "packet_fingerprint",
        )
        provider = _bounded_text(provider, "provider", 40)
        model = _bounded_text(model, "model", 191)
        prompt_version = _bounded_text(prompt_version, "prompt_version", 64)
        with self._session_factory() as session:
            row = session.scalar(
                select(TableByLLMReview).where(
                    TableByLLMReview.event_id == event_id,
                    TableByLLMReview.packet_fingerprint == packet_fingerprint,
                    TableByLLMReview.provider == provider,
                    TableByLLMReview.model == model,
                    TableByLLMReview.prompt_version == prompt_version,
                )
            )
            return None if row is None else _llm_review_from_row(row)

    @mutation_fenced("decision_event_store.append_llm_review")
    def append_llm_review(
        self,
        *,
        review_id: str,
        owner_token: str,
        fencing_token: int,
        event_id: str,
        risk_snapshot_id: str,
        packet_fingerprint: str,
        reviewed_data_fingerprint: str,
        provider: str,
        model: str,
        prompt_version: str,
        status: str,
        provider_ok: bool,
        verdict: str,
        response_content: str | None,
        raw_response: str,
        parsed_response_json: str | None,
        validation_errors: Sequence[str],
        attempt_count: int,
        latency_ms: int,
        error_code: str | None,
        error_message: str | None,
        created_at: datetime,
    ) -> StoredLLMReview:
        self._mutation_fence.require()
        review_id = _bounded_text(review_id, "review_id", 255)
        owner_token = _bounded_text(owner_token, "owner_token", 64)
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token must be a positive integer")
        event_id = _required_text(event_id, "event_id")
        risk_snapshot_id = _bounded_text(
            risk_snapshot_id,
            "risk_snapshot_id",
            255,
        )
        packet_fingerprint = _required_fingerprint(
            packet_fingerprint,
            "packet_fingerprint",
        )
        reviewed_data_fingerprint = _required_fingerprint(
            reviewed_data_fingerprint,
            "reviewed_data_fingerprint",
        )
        provider = _bounded_text(provider, "provider", 40)
        model = _bounded_text(model, "model", 191)
        prompt_version = _bounded_text(prompt_version, "prompt_version", 64)
        if status not in _LLM_REVIEW_STATUSES:
            raise ValueError("status is invalid")
        if type(provider_ok) is not bool:
            raise TypeError("provider_ok must be boolean")
        if verdict not in _LLM_REVIEW_VERDICTS:
            raise ValueError("verdict is invalid")
        content_audit = _bounded_audit_text(
            response_content,
            "response_content",
            allow_none=True,
        )
        raw_audit = _bounded_audit_text(
            raw_response,
            "raw_response",
            allow_none=False,
        )
        if parsed_response_json is not None and not isinstance(
            parsed_response_json,
            str,
        ):
            raise TypeError("parsed_response_json must be text or None")
        if isinstance(validation_errors, (str, bytes)):
            raise TypeError("validation_errors must be a sequence")
        normalized_errors = tuple(validation_errors)
        if any(
            not isinstance(error, str) or not error
            for error in normalized_errors
        ):
            raise ValueError(
                "validation_errors must contain non-empty strings"
            )
        if len(normalized_errors) != len(set(normalized_errors)):
            raise ValueError("validation_errors must be unique")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError("attempt_count must be a non-negative integer")
        if (
            isinstance(latency_ms, bool)
            or not isinstance(latency_ms, int)
            or latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer")
        error_code = _bounded_optional_text(error_code, "error_code", 100)
        if error_message is not None:
            _required_text(error_message, "error_message")
        error_audit = _bounded_audit_text(
            error_message,
            "error_message",
            allow_none=True,
            max_bytes=MAX_LLM_ERROR_BYTES,
        )
        created_at = normalize_datetime(created_at, "created_at")

        if status == "validated":
            if not provider_ok or normalized_errors:
                raise ValueError("validated review must be provider-valid")
            if parsed_response_json is None or not response_content:
                raise ValueError(
                    "validated review requires response content and parsed JSON"
                )
            if content_audit.truncated or raw_audit.truncated:
                raise ValueError("validated review exceeds the audit limit")
        elif status == "validation_failed":
            if not provider_ok or not normalized_errors or not response_content:
                raise ValueError(
                    "validation_failed review requires content and validation errors"
                )
        elif provider_ok:
            raise ValueError("local/provider failure cannot be provider-valid")
        if status != "validated" and verdict != "ABSTAIN":
            message = (
                "non-validated review verdict must be ABSTAIN"
                if provider_ok
                else "failed review verdict must be ABSTAIN"
            )
            raise ValueError(message)
        if provider_ok:
            if error_code is not None or error_audit.text is not None:
                raise ValueError("provider-valid review cannot carry an error")
            if attempt_count < 1:
                raise ValueError("provider-valid review requires an attempt")
        else:
            if response_content is not None:
                raise ValueError("failed review cannot carry response content")
            if error_code is None:
                raise ValueError("failed review requires error_code")
            if status == "local_abstain" and attempt_count != 0:
                raise ValueError("local abstain cannot carry provider attempts")
            if status == "provider_failed" and attempt_count < 1:
                raise ValueError("provider failure requires an attempt")

        identity = (
            event_id,
            packet_fingerprint,
            provider,
            model,
            prompt_version,
        )
        with self._session_factory() as session:
            self._require_current_strategy_event_id(session, event_id)
            existing = session.scalar(
                select(TableByLLMReview).where(
                    TableByLLMReview.event_id == identity[0],
                    TableByLLMReview.packet_fingerprint == identity[1],
                    TableByLLMReview.provider == identity[2],
                    TableByLLMReview.model == identity[3],
                    TableByLLMReview.prompt_version == identity[4],
                )
            )
            if existing is not None:
                if existing.risk_snapshot_id != risk_snapshot_id:
                    raise ReviewConflictError(
                        "LLM review risk snapshot identity conflict"
                    )
                return _llm_review_from_row(existing)
            claim = session.scalar(
                select(TableByLLMReviewClaim).where(
                    TableByLLMReviewClaim.review_id == review_id,
                    TableByLLMReviewClaim.event_id == identity[0],
                    TableByLLMReviewClaim.packet_fingerprint == identity[1],
                    TableByLLMReviewClaim.provider == identity[2],
                    TableByLLMReviewClaim.model == identity[3],
                    TableByLLMReviewClaim.prompt_version == identity[4],
                )
            )
            if (
                claim is None
                or claim.owner_token != owner_token
                or claim.fencing_token != fencing_token
            ):
                raise LLMReviewClaimLostError(
                    "LLM review claim is not owned by this caller"
                )
            attempt_rows = session.scalars(
                select(TableByLLMReviewAttempt)
                .where(
                    TableByLLMReviewAttempt.review_id == review_id,
                    TableByLLMReviewAttempt.owner_token == owner_token,
                    TableByLLMReviewAttempt.fencing_token == fencing_token,
                )
                .order_by(TableByLLMReviewAttempt.id)
            ).all()
            if len(attempt_rows) != attempt_count:
                raise ReviewConflictError(
                    "LLM review attempt_count does not match audit rows"
                )
            if attempt_rows:
                final_attempt = attempt_rows[-1]
                if (
                    bool(final_attempt.ok) != provider_ok
                    or final_attempt.raw_response != raw_audit.text
                    or final_attempt.raw_response_bytes
                    != raw_audit.original_bytes
                    or final_attempt.raw_response_sha256 != raw_audit.sha256
                    or bool(final_attempt.raw_response_truncated)
                    != raw_audit.truncated
                    or sum(row.latency_ms for row in attempt_rows) != latency_ms
                ):
                    raise ReviewConflictError(
                        "LLM review final does not match audit attempts"
                    )
                if provider_ok:
                    if (
                        final_attempt.response_content != content_audit.text
                        or final_attempt.response_content_bytes
                        != content_audit.original_bytes
                        or final_attempt.response_content_sha256
                        != content_audit.sha256
                        or bool(final_attempt.response_content_truncated)
                        != content_audit.truncated
                    ):
                        raise ReviewConflictError(
                            "LLM review content does not match final attempt"
                        )
                elif (
                    final_attempt.error_code != error_code
                    or final_attempt.error_message != error_audit.text
                    or final_attempt.error_message_bytes
                    != error_audit.original_bytes
                    or final_attempt.error_message_sha256
                    != error_audit.sha256
                    or bool(final_attempt.error_message_truncated)
                    != error_audit.truncated
                ):
                    raise ReviewConflictError(
                        "LLM review failure does not match final attempt"
                    )
            elif status != "local_abstain":
                raise ReviewConflictError(
                    "only local abstain may omit provider attempts"
                )
            reused_id = session.scalar(
                select(TableByLLMReview).where(
                    TableByLLMReview.review_id == review_id
                )
            )
            if reused_id is not None:
                raise ReviewConflictError(
                    f"review_id already used with different identity: {review_id}"
                )
            event_exists = session.scalar(
                select(TableByDecisionEvent.id).where(
                    TableByDecisionEvent.event_id == event_id
                )
            )
            if event_exists is None:
                raise EventNotFoundError(event_id)
            risk_snapshot_row = session.scalar(
                select(TableByRiskSnapshot).where(
                    TableByRiskSnapshot.snapshot_id == risk_snapshot_id
                )
            )
            if (
                risk_snapshot_row is None
                or risk_snapshot_row.event_id != event_id
            ):
                raise ReviewConflictError(
                    "LLM review risk snapshot binding is invalid"
                )

            coordination_now = self._coordination_now(session)
            finalized = session.execute(
                update(TableByLLMReviewClaim)
                .where(
                    TableByLLMReviewClaim.id == claim.id,
                    TableByLLMReviewClaim.owner_token == owner_token,
                    TableByLLMReviewClaim.fencing_token == fencing_token,
                    TableByLLMReviewClaim.finalized.is_(False),
                    TableByLLMReviewClaim.lease_expires_at > coordination_now,
                )
                .values(finalized=True)
            )
            if finalized.rowcount != 1:
                current = session.scalar(
                    select(TableByLLMReviewClaim).where(
                        TableByLLMReviewClaim.id == claim.id
                    )
                )
                if current is None:
                    raise LLMReviewClaimLostError(
                        "LLM review claim is missing"
                    )
                if (
                    current.owner_token != owner_token
                    or current.fencing_token != fencing_token
                ):
                    raise LLMReviewClaimLostError(
                        "LLM review claim is not owned by this caller"
                    )
                if bool(current.finalized):
                    raise LLMReviewClaimLostError(
                        "LLM review claim is finalized"
                    )
                raise LLMReviewClaimLostError(
                    "LLM review claim lease expired"
                )

            row = TableByLLMReview(
                review_id=review_id,
                event_id=event_id,
                risk_snapshot_id=risk_snapshot_id,
                packet_fingerprint=packet_fingerprint,
                reviewed_data_fingerprint=reviewed_data_fingerprint,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                fencing_token=fencing_token,
                status=status,
                provider_ok=provider_ok,
                verdict=verdict,
                response_content=content_audit.text,
                response_content_bytes=content_audit.original_bytes,
                response_content_sha256=content_audit.sha256,
                response_content_truncated=content_audit.truncated,
                raw_response=raw_audit.text,
                raw_response_bytes=raw_audit.original_bytes,
                raw_response_sha256=raw_audit.sha256,
                raw_response_truncated=raw_audit.truncated,
                parsed_response_json=parsed_response_json,
                validation_errors_json=json.dumps(
                    normalized_errors,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                attempt_count=attempt_count,
                latency_ms=latency_ms,
                error_code=error_code,
                error_message=error_audit.text,
                error_message_bytes=error_audit.original_bytes,
                error_message_sha256=error_audit.sha256,
                error_message_truncated=error_audit.truncated,
                created_at=created_at,
            )
            session.add(row)
            try:
                session.commit()
                return _llm_review_from_row(row)
            except IntegrityError:
                session.rollback()
                concurrent = session.scalar(
                    select(TableByLLMReview).where(
                        TableByLLMReview.event_id == identity[0],
                        TableByLLMReview.packet_fingerprint == identity[1],
                        TableByLLMReview.provider == identity[2],
                        TableByLLMReview.model == identity[3],
                        TableByLLMReview.prompt_version == identity[4],
                    )
                )
                if concurrent is not None:
                    return _llm_review_from_row(concurrent)
                reused_id = session.scalar(
                    select(TableByLLMReview).where(
                        TableByLLMReview.review_id == review_id
                    )
                )
                if reused_id is not None:
                    raise ReviewConflictError(
                        "review_id already used with different identity: "
                        f"{review_id}"
                    )
                raise

    def list_llm_reviews(self, event_id: str) -> tuple[StoredLLMReview, ...]:
        event_id = _required_text(event_id, "event_id")
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByLLMReview)
                .where(TableByLLMReview.event_id == event_id)
                .order_by(TableByLLMReview.id)
            ).all()
            return tuple(_llm_review_from_row(row) for row in rows)

    def count_llm_reviews(self, event_id: str | None = None) -> int:
        statement = select(func.count(TableByLLMReview.id))
        if event_id is not None:
            statement = statement.where(
                TableByLLMReview.event_id
                == _required_text(event_id, "event_id")
            )
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    def list_llm_review_attempts(
        self,
        review_id: str,
    ) -> tuple[StoredLLMReviewAttempt, ...]:
        review_id = _bounded_text(review_id, "review_id", 255)
        with self._session_factory() as session:
            rows = session.scalars(
                select(TableByLLMReviewAttempt)
                .where(TableByLLMReviewAttempt.review_id == review_id)
                .order_by(TableByLLMReviewAttempt.id)
            ).all()
            return tuple(_llm_attempt_from_row(row) for row in rows)

    def count_llm_review_attempts(self, review_id: str | None = None) -> int:
        statement = select(func.count(TableByLLMReviewAttempt.id))
        if review_id is not None:
            statement = statement.where(
                TableByLLMReviewAttempt.review_id
                == _bounded_text(review_id, "review_id", 255)
            )
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    def count_transitions(self, event_id: str | None = None) -> int:
        statement = select(func.count(TableByDecisionTransition.id))
        if event_id is not None:
            statement = statement.where(
                TableByDecisionTransition.event_id
                == _required_text(event_id, "event_id")
            )
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)
