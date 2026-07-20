from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import LONGTEXT as MySQLLongText
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar
from sqlalchemy.types import TypeDecorator

from chanlun.db_models.base import Base


_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _identity_string(length: int):
    return String(length).with_variant(
        MySQLVarchar(length, collation="utf8mb4_bin"),
        "mysql",
    )


def _audit_text():
    return Text().with_variant(MySQLLongText(), "mysql")


class _UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLDateTime(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError("database datetime must be datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database datetime must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_MARKET_TIMEZONE)


class TableByDecisionEvent(Base):
    __tablename__ = "cl_decision_event"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="uq_cl_decision_event_event_id",
        ),
        Index(
            "ix_cl_decision_event_market_code_observed",
            "market",
            "code",
            "observed_at",
        ),
        Index(
            "ix_cl_decision_event_strategy_run_observed",
            "strategy_run_id",
            "strategy_run_epoch",
            "strategy_run_fingerprint",
            "observed_at",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(255), nullable=False)
    market = Column(String(20), nullable=False)
    code = Column(String(32), nullable=False)
    observed_at = Column(_UTCDateTime(), nullable=False)
    strategy_track = Column(String(40), nullable=False)
    data_fingerprint = Column(String(71), nullable=False)
    config_fingerprint = Column(String(71), nullable=False)
    strategy_run_id = Column(_identity_string(80), nullable=True)
    strategy_run_epoch = Column(Integer, nullable=True)
    strategy_run_fingerprint = Column(_identity_string(71), nullable=True)
    payload_json = Column(Text, nullable=False)


class TableByDecisionTransition(Base):
    __tablename__ = "cl_decision_transition"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "from_state",
            name="uq_cl_decision_transition_event_from_state",
        ),
        Index(
            "ix_cl_decision_transition_event_id_id",
            "event_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_state = Column(String(40), nullable=False)
    to_state = Column(String(40), nullable=False)
    occurred_at = Column(_UTCDateTime(), nullable=False)
    reason = Column(Text, nullable=False)
    actor = Column(String(100), nullable=False)


class TableByDecisionReview(Base):
    __tablename__ = "cl_decision_review"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            name="uq_cl_decision_review_review_id",
        ),
        Index(
            "ix_cl_decision_review_event_id_id",
            "event_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(String(255), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewed_data_fingerprint = Column(String(71), nullable=False)
    verdict = Column(String(20), nullable=False)
    reviewed_at = Column(_UTCDateTime(), nullable=False)
    applied = Column(Boolean, nullable=False)
    state = Column(String(40), nullable=False)
    reason = Column(String(100), nullable=False)


class TableByUserDecision(Base):
    __tablename__ = "cl_decision_user_decision"
    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            name="uq_cl_decision_user_decision_id",
        ),
        UniqueConstraint(
            "event_id",
            "user_id",
            "idempotency_key",
            name="uq_cl_decision_user_decision_request",
        ),
        Index(
            "ix_cl_decision_user_decision_event_id_id",
            "event_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    decision_id = Column(_identity_string(255), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id = Column(_identity_string(191), nullable=False)
    action = Column(String(32), nullable=False)
    note = Column(Text, nullable=True)
    event_data_fingerprint = Column(_identity_string(71), nullable=False)
    idempotency_key = Column(_identity_string(128), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    decided_at = Column(_UTCDateTime(), nullable=False)


class TableByRiskSnapshot(Base):
    __tablename__ = "cl_decision_risk_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            name="uq_cl_decision_risk_snapshot_id",
        ),
        UniqueConstraint(
            "identity_fingerprint",
            name="uq_cl_decision_risk_snapshot_identity",
        ),
        Index(
            "ix_cl_decision_risk_snapshot_event_evaluated",
            "event_id",
            "evaluated_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(_identity_string(255), nullable=False)
    identity_fingerprint = Column(_identity_string(71), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_data_fingerprint = Column(_identity_string(71), nullable=False)
    rule_id = Column(_identity_string(191), nullable=False)
    rule_card_version = Column(Integer, nullable=False)
    rule_card_fingerprint = Column(_identity_string(71), nullable=False)
    rule_set_fingerprint = Column(_identity_string(71), nullable=False)
    corpus_manifest_fingerprint = Column(_identity_string(71), nullable=False)
    algorithm_fingerprint = Column(_identity_string(71), nullable=False)
    evaluation_input_fingerprint = Column(_identity_string(71), nullable=False)
    observed_at = Column(_UTCDateTime(), nullable=False)
    evaluated_at = Column(_UTCDateTime(), nullable=False)
    expires_at = Column(_UTCDateTime(), nullable=False)
    decision_allowed = Column(Boolean, nullable=False)
    shares = Column(Integer, nullable=False)
    planned_risk_cash = Column(String(100), nullable=False)
    target_weight = Column(String(100), nullable=False)
    entry_reference = Column(String(100), nullable=False)
    decision_reasons_json = Column(Text, nullable=False)
    daily_loss_locked = Column(Boolean, nullable=False)
    drawdown_locked = Column(Boolean, nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByPaperAdmissionAuthorization(Base):
    __tablename__ = "cl_decision_paper_admission_authorization"
    __table_args__ = (
        UniqueConstraint(
            "authorization_id",
            name="uq_cl_decision_paper_admission_authorization_id",
        ),
        UniqueConstraint(
            "event_id",
            name="uq_cl_decision_paper_admission_event_id",
        ),
        Index(
            "ix_cl_decision_paper_admission_authorized",
            "authorized_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    authorization_id = Column(_identity_string(255), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_data_fingerprint = Column(_identity_string(71), nullable=False)
    review_id = Column(
        _identity_string(255),
        ForeignKey("cl_decision_llm_review.review_id", ondelete="RESTRICT"),
        nullable=False,
    )
    risk_snapshot_id = Column(
        _identity_string(255),
        ForeignKey(
            "cl_decision_risk_snapshot.snapshot_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    confirmation_transition_id = Column(
        Integer,
        ForeignKey("cl_decision_transition.id", ondelete="RESTRICT"),
        nullable=False,
    )
    manual_check_pending_id = Column(_identity_string(255), nullable=True)
    manual_check_payload_fingerprint = Column(
        _identity_string(71),
        nullable=True,
    )
    packet_fingerprint = Column(_identity_string(71), nullable=False)
    authorized_at = Column(_UTCDateTime(), nullable=False)
    risk_expires_at = Column(_UTCDateTime(), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByRiskLatchAudit(Base):
    __tablename__ = "cl_decision_risk_latch_audit"
    __table_args__ = (
        UniqueConstraint(
            "audit_id",
            name="uq_cl_decision_risk_latch_audit_id",
        ),
        UniqueConstraint(
            "identity_fingerprint",
            name="uq_cl_decision_risk_latch_audit_identity",
        ),
        Index(
            "ix_cl_decision_risk_latch_event_occurred",
            "event_id",
            "occurred_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(_identity_string(255), nullable=False)
    identity_fingerprint = Column(_identity_string(71), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_id = Column(
        _identity_string(255),
        ForeignKey(
            "cl_decision_risk_snapshot.snapshot_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    latch_kind = Column(String(40), nullable=False)
    action = Column(String(40), nullable=False)
    previous_locked = Column(Boolean, nullable=False)
    current_locked = Column(Boolean, nullable=False)
    actor = Column(_identity_string(191), nullable=False)
    reason = Column(Text, nullable=False)
    occurred_at = Column(_UTCDateTime(), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByLLMReviewClaim(Base):
    __tablename__ = "cl_decision_llm_review_claim"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            name="uq_cl_decision_llm_claim_review_id",
        ),
        UniqueConstraint(
            "event_id",
            "packet_fingerprint",
            "provider",
            "model",
            "prompt_version",
            name="uq_cl_decision_llm_claim_identity",
        ),
        Index(
            "ix_cl_decision_llm_claim_event_id_id",
            "event_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(_identity_string(255), nullable=False)
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    packet_fingerprint = Column(_identity_string(71), nullable=False)
    provider = Column(_identity_string(40), nullable=False)
    model = Column(_identity_string(191), nullable=False)
    prompt_version = Column(_identity_string(64), nullable=False)
    owner_token = Column(_identity_string(64), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    lease_expires_at = Column(_UTCDateTime(), nullable=False)
    finalized = Column(Boolean, nullable=False)
    created_at = Column(_UTCDateTime(), nullable=False)


class TableByLLMReviewAttempt(Base):
    __tablename__ = "cl_decision_llm_review_attempt"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            name="uq_cl_decision_llm_attempt_id",
        ),
        UniqueConstraint(
            "review_id",
            "owner_token",
            "fencing_token",
            "attempt_number",
            name="uq_cl_decision_llm_attempt_owner_number",
        ),
        Index(
            "ix_cl_decision_llm_attempt_review_id_id",
            "review_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(_identity_string(255), nullable=False)
    review_id = Column(
        _identity_string(255),
        ForeignKey("cl_decision_llm_review_claim.review_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_token = Column(_identity_string(64), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    attempt_number = Column(Integer, nullable=False)
    provider = Column(_identity_string(40), nullable=False)
    model = Column(_identity_string(191), nullable=False)
    ok = Column(Boolean, nullable=False)
    retryable = Column(Boolean, nullable=False)
    response_content = Column(_audit_text(), nullable=True)
    response_content_bytes = Column(Integer, nullable=False)
    response_content_sha256 = Column(String(71), nullable=True)
    response_content_truncated = Column(Boolean, nullable=False)
    raw_response = Column(_audit_text(), nullable=False)
    raw_response_bytes = Column(Integer, nullable=False)
    raw_response_sha256 = Column(String(71), nullable=False)
    raw_response_truncated = Column(Boolean, nullable=False)
    error_code = Column(String(100), nullable=True)
    error_message = Column(_audit_text(), nullable=True)
    error_message_bytes = Column(Integer, nullable=False)
    error_message_sha256 = Column(String(71), nullable=True)
    error_message_truncated = Column(Boolean, nullable=False)
    latency_ms = Column(Integer, nullable=False)
    started_at = Column(_UTCDateTime(), nullable=False)
    completed_at = Column(_UTCDateTime(), nullable=False)


class TableByLLMReview(Base):
    __tablename__ = "cl_decision_llm_review"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            name="uq_cl_decision_llm_review_review_id",
        ),
        UniqueConstraint(
            "event_id",
            "packet_fingerprint",
            "provider",
            "model",
            "prompt_version",
            name="uq_cl_decision_llm_review_identity",
        ),
        Index(
            "ix_cl_decision_llm_review_event_id_id",
            "event_id",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(
        _identity_string(255),
        ForeignKey("cl_decision_llm_review_claim.review_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    risk_snapshot_id = Column(
        _identity_string(255),
        ForeignKey(
            "cl_decision_risk_snapshot.snapshot_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    packet_fingerprint = Column(_identity_string(71), nullable=False)
    reviewed_data_fingerprint = Column(_identity_string(71), nullable=False)
    provider = Column(_identity_string(40), nullable=False)
    model = Column(_identity_string(191), nullable=False)
    prompt_version = Column(_identity_string(64), nullable=False)
    fencing_token = Column(Integer, nullable=False)
    status = Column(String(40), nullable=False)
    provider_ok = Column(Boolean, nullable=False)
    verdict = Column(String(20), nullable=False)
    response_content = Column(_audit_text(), nullable=True)
    response_content_bytes = Column(Integer, nullable=False)
    response_content_sha256 = Column(String(71), nullable=True)
    response_content_truncated = Column(Boolean, nullable=False)
    raw_response = Column(_audit_text(), nullable=False)
    raw_response_bytes = Column(Integer, nullable=False)
    raw_response_sha256 = Column(String(71), nullable=False)
    raw_response_truncated = Column(Boolean, nullable=False)
    parsed_response_json = Column(_audit_text(), nullable=True)
    validation_errors_json = Column(_audit_text(), nullable=False)
    attempt_count = Column(Integer, nullable=False)
    latency_ms = Column(Integer, nullable=False)
    error_code = Column(String(100), nullable=True)
    error_message = Column(_audit_text(), nullable=True)
    error_message_bytes = Column(Integer, nullable=False)
    error_message_sha256 = Column(String(71), nullable=True)
    error_message_truncated = Column(Boolean, nullable=False)
    created_at = Column(_UTCDateTime(), nullable=False)


class TableBySectorSelection(Base):
    __tablename__ = "cl_decision_sector_selection"
    __table_args__ = (
        UniqueConstraint(
            "selection_id",
            name="uq_cl_decision_sector_selection_id",
        ),
        Index(
            "ix_cl_decision_sector_selection_latest",
            "market",
            "scope",
            "status",
            "bar_closed_at",
            "observed_at",
            "id",
        ),
        CheckConstraint(
            "stale IN (0, 1)",
            name="ck_cl_decision_sector_selection_stale_bool",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    selection_id = Column(_identity_string(255), nullable=False)
    market = Column(_identity_string(20), nullable=False)
    scope = Column(_identity_string(40), nullable=False)
    observed_at = Column(_UTCDateTime(), nullable=False)
    bar_closed_at = Column(_UTCDateTime(), nullable=False)
    membership_fingerprint = Column(_identity_string(71), nullable=False)
    policy_fingerprint = Column(_identity_string(71), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    envelope_fingerprint = Column(_identity_string(71), nullable=False)
    status = Column(_identity_string(20), nullable=False)
    stale = Column(Boolean, nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByTriggerObservation(Base):
    __tablename__ = "cl_decision_trigger_observation"
    __table_args__ = (
        UniqueConstraint(
            "trigger_id",
            name="uq_cl_decision_trigger_observation_id",
        ),
        Index(
            "ix_cl_decision_trigger_selection_bar",
            "selection_id",
            "bar_closed_at",
            "observed_at",
            "id",
        ),
        Index(
            "ix_cl_decision_trigger_event_match",
            "selection_id",
            "market",
            "code",
            "sector_id",
            "strategy_run_id",
            "strategy_run_epoch",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_id = Column(_identity_string(255), nullable=False)
    trigger_fingerprint = Column(_identity_string(71), nullable=False)
    market = Column(_identity_string(20), nullable=False)
    code = Column(_identity_string(32), nullable=False)
    sector_id = Column(_identity_string(255), nullable=False)
    selection_id = Column(
        _identity_string(255),
        ForeignKey(
            "cl_decision_sector_selection.selection_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    selection_fingerprint = Column(_identity_string(71), nullable=False)
    bar_opened_at = Column(_UTCDateTime(), nullable=False)
    bar_closed_at = Column(_UTCDateTime(), nullable=False)
    observed_at = Column(_UTCDateTime(), nullable=False)
    source_frequency = Column(_identity_string(20), nullable=False)
    source_bar_fingerprint = Column(_identity_string(71), nullable=False)
    source_state_fingerprint = Column(_identity_string(71), nullable=False)
    direction = Column(_identity_string(20), nullable=False)
    bs_type = Column(_identity_string(40), nullable=False)
    signal_key = Column(_identity_string(71), nullable=False)
    signal_fingerprint = Column(_identity_string(71), nullable=False)
    physical_5m_closed_at = Column(_UTCDateTime(), nullable=False)
    related_5m_observation_id = Column(_identity_string(255), nullable=False)
    five_minute_signal_fingerprint = Column(_identity_string(71), nullable=False)
    gate_fingerprint = Column(_identity_string(71), nullable=False)
    rule_id = Column(_identity_string(191), nullable=False)
    rule_card_version = Column(Integer, nullable=False)
    predicate_result_fingerprint = Column(_identity_string(71), nullable=False)
    rule_binding_fingerprint = Column(_identity_string(71), nullable=False)
    strategy_run_id = Column(_identity_string(80), nullable=False)
    strategy_run_epoch = Column(Integer, nullable=False)
    strategy_run_fingerprint = Column(_identity_string(71), nullable=False)
    research_only = Column(Boolean, nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByExitAttentionObservation(Base):
    __tablename__ = "cl_decision_exit_attention_observation"
    __table_args__ = (
        UniqueConstraint(
            "exit_attention_id",
            name="uq_cl_decision_exit_attention_id",
        ),
        Index(
            "ix_cl_decision_exit_attention_code_bar",
            "market",
            "code",
            "bar_closed_at",
            "observed_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    exit_attention_id = Column(_identity_string(255), nullable=False)
    market = Column(_identity_string(20), nullable=False)
    code = Column(_identity_string(32), nullable=False)
    sector_id = Column(_identity_string(255), nullable=True)
    bar_opened_at = Column(_UTCDateTime(), nullable=False)
    bar_closed_at = Column(_UTCDateTime(), nullable=False)
    observed_at = Column(_UTCDateTime(), nullable=False)
    source_frequency = Column(_identity_string(20), nullable=False)
    source_bar_fingerprint = Column(_identity_string(71), nullable=False)
    source_state_fingerprint = Column(_identity_string(71), nullable=False)
    signal_key = Column(_identity_string(71), nullable=False)
    signal_fingerprint = Column(_identity_string(71), nullable=False)
    visibility_state = Column(_identity_string(40), nullable=False)
    required_epoch = Column(Integer, nullable=False)
    required_set_fingerprint = Column(_identity_string(71), nullable=False)
    strategy_run_id = Column(_identity_string(80), nullable=False)
    strategy_run_epoch = Column(Integer, nullable=False)
    strategy_run_fingerprint = Column(_identity_string(71), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)


class TableByPhysicalOneMinuteCheckpoint(Base):
    __tablename__ = "cl_decision_physical_one_minute_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "checkpoint_id",
            name="uq_cl_decision_physical_1m_checkpoint_id",
        ),
        UniqueConstraint(
            "market",
            "code",
            "engine_policy_fingerprint",
            "strategy_run_id",
            "strategy_run_epoch",
            "strategy_run_fingerprint",
            name="uq_cl_decision_physical_1m_checkpoint_identity",
        ),
        Index(
            "ix_cl_decision_physical_1m_checkpoint_updated",
            "market",
            "code",
            "updated_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id = Column(_identity_string(255), nullable=False)
    market = Column(_identity_string(20), nullable=False)
    code = Column(_identity_string(32), nullable=False)
    engine_policy_fingerprint = Column(_identity_string(71), nullable=False)
    strategy_run_id = Column(_identity_string(80), nullable=False)
    strategy_run_epoch = Column(Integer, nullable=False)
    strategy_run_fingerprint = Column(_identity_string(71), nullable=False)
    analysis_first_bar_closed_at = Column(_UTCDateTime(), nullable=False)
    bootstrap_closed_at = Column(_UTCDateTime(), nullable=False)
    last_bar_closed_at = Column(_UTCDateTime(), nullable=False)
    processed_bar_count = Column(Integer, nullable=False)
    last_source_bar_fingerprint = Column(_identity_string(71), nullable=False)
    source_chain_fingerprint = Column(_identity_string(71), nullable=False)
    seen_signal_keys_json = Column(_audit_text(), nullable=False)
    current_signal_keys_json = Column(_audit_text(), nullable=False)
    current_sell_signal_keys_json = Column(_audit_text(), nullable=False)
    entry_active = Column(Boolean, nullable=False)
    entry_epoch = Column(Integer, nullable=False)
    required_active = Column(Boolean, nullable=False)
    required_epoch = Column(Integer, nullable=False)
    exit_alerted_signal_keys_json = Column(_audit_text(), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)
    cycle_fingerprint = Column(_identity_string(71), nullable=False)
    cycle_json = Column(_audit_text(), nullable=False)
    updated_at = Column(_UTCDateTime(), nullable=False)


class TableByTriggerEventLink(Base):
    __tablename__ = "cl_decision_trigger_event_link"
    __table_args__ = (
        UniqueConstraint(
            "trigger_id",
            name="uq_cl_decision_trigger_event_link_trigger",
        ),
        UniqueConstraint(
            "event_id",
            name="uq_cl_decision_trigger_event_link_event",
        ),
        Index(
            "ix_cl_decision_trigger_event_link_linked",
            "linked_at",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_id = Column(
        _identity_string(255),
        ForeignKey(
            "cl_decision_trigger_observation.trigger_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_id = Column(
        String(255),
        ForeignKey("cl_decision_event.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    linked_at = Column(_UTCDateTime(), nullable=False)


class TableBySectorPreferenceRevision(Base):
    __tablename__ = "cl_decision_sector_preference_revision"
    __table_args__ = (
        UniqueConstraint(
            "sector_id",
            "revision",
            name="uq_cl_decision_sector_preference_revision",
        ),
        UniqueConstraint(
            "sector_id",
            "idempotency_key",
            name="uq_cl_decision_sector_preference_idempotency",
        ),
        Index(
            "ix_cl_decision_sector_preference_latest",
            "sector_id",
            "revision",
            "id",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sector_id = Column(_identity_string(255), nullable=False)
    action = Column(_identity_string(20), nullable=False)
    revision = Column(Integer, nullable=False)
    expected_revision = Column(Integer, nullable=False)
    idempotency_key = Column(_identity_string(128), nullable=False)
    operator_id = Column(_identity_string(191), nullable=False)
    reason = Column(_audit_text(), nullable=False)
    changed_at = Column(_UTCDateTime(), nullable=False)
    pinned_at = Column(_UTCDateTime(), nullable=True)
    request_fingerprint = Column(_identity_string(71), nullable=False)
    payload_fingerprint = Column(_identity_string(71), nullable=False)
    payload_json = Column(_audit_text(), nullable=False)
