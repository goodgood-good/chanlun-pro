"""Isolated human-confirmed paper intents and causal 1m bar fills.

Nothing in this module imports a broker transport.  A ``PAPER_OBSERVE`` review
can create an immutable virtual intent; only a later completed and tradable 1m
bar can create a virtual fill.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Literal, Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.market_rules import is_st_name
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    validate_a_share_complete_session_closes,
    validate_a_share_completed_one_minute_interval,
)
from chanlun.decision_support.trading_system.bar_execution import (
    STRICT_BAR_CROSS_RULE,
    STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
    STRICT_BAR_PRICE_RULE,
    STRICT_BAR_VOLUME_PARTICIPATION,
    adverse_observed_bar_price,
    assess_strict_limit_bar,
    strict_bar_volume_capacity,
)
from chanlun.decision_support.trading_system.file_lock import interprocess_file_lock
from chanlun.decision_support.trading_system.human_paper_accounting import (
    HumanPaperAccountingParameters,
    assess_human_paper_portfolio_fill,
)
from chanlun.decision_support.trading_system.models import (
    EntryExecutionBoundary,
    parse_entry_execution_boundary_document,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    MONITOR_ONLY_WARNING_CODE,
    HumanReviewAlert,
    HumanReviewFeedback,
    validate_human_review_feedback_causality,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    catalog_capture_entry,
)


LEDGER_SCHEMA = "chanlun-human-paper-ledger"
EXECUTION_EVIDENCE_SCHEMA = "chanlun-human-paper-execution-evidence"
EXECUTION_FACT_SCHEMA = "chanlun-human-paper-execution-facts"
ENTRY_SELECTION_EVIDENCE_SCHEMA = (
    "chanlun-human-paper-entry-selection-evidence"
)
ENTRY_SELECTION_EXACT_ATTESTATION = (
    "EXACT_REVISION_NAME_AND_MEMBERSHIP_MATCH"
)
PAPER_CONTRACT_ID = sha256_json(
    {
        "schema": "chanlun-human-paper-contract",
        "quantity": 100,
        "fill_source": STRICT_BAR_PRICE_RULE,
        "fill_timestamp_rule": STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
        "buy_strict_cross_rule": STRICT_BAR_CROSS_RULE,
        "buy_max_bar_volume_participation": format(
            STRICT_BAR_VOLUME_PARTICIPATION,
            "f",
        ),
        "sell_requires_virtual_position": True,
        "sell_t_plus_one": True,
        "buy_requires_market_gate": "GREEN",
        "buy_requires_sector_gate": "GREEN",
        "buy_requires_symbol_gate": "GREEN",
        "buy_rejects_authenticated_warmup_divergence": True,
        "human_trend_type_confirmation_required": True,
        "sell_is_never_blocked_by_risk_gate": True,
        "same_session_execution_facts_required": True,
        "security_status_snapshot_required": True,
        "corporate_action_snapshot_required": True,
        "st_buy_prohibited": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RISK_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})


@dataclass(frozen=True, slots=True)
class HumanPaperEntrySelectionEvidence:
    """Hash-bound QMT catalog proof used to admit one virtual 30m buy.

    The full sector catalog remains in the immutable QMT capture ledger.  This
    compact object binds the paper intent to the exact ledger entry, ranking
    evidence and human-review time used at admission; the audit below then
    replays the name and membership check against that archived entry.
    """

    feedback_id: str
    candidate_id: str
    source_screen_content_sha256: str
    symbol: str
    sector_id: str
    sector_name: str
    sector_ranking_evidence_id: str
    sector_ranking_observed_at: datetime
    sector_catalog_revision: str
    sector_catalog_entry_sha256: str
    sector_catalog_captured_at: datetime
    attested_at: datetime
    attestation: str = ENTRY_SELECTION_EXACT_ATTESTATION
    schema: str = ENTRY_SELECTION_EVIDENCE_SCHEMA
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in (
            "sector_ranking_observed_at",
            "sector_catalog_captured_at",
            "attested_at",
        ):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        for field in (
            "feedback_id",
            "candidate_id",
            "source_screen_content_sha256",
            "sector_ranking_evidence_id",
            "sector_catalog_revision",
            "sector_catalog_entry_sha256",
        ):
            if _SHA256.fullmatch(str(getattr(self, field))) is None:
                raise ValueError(
                    f"human paper entry selection {field} is invalid"
                )
        if not self.symbol or not self.sector_id or not self.sector_name.strip():
            raise ValueError("human paper entry selection identity is incomplete")
        if (
            self.sector_catalog_captured_at > self.sector_ranking_observed_at
            or self.sector_ranking_observed_at > self.attested_at
            or self.sector_catalog_captured_at.date()
            != self.sector_ranking_observed_at.date()
            or self.sector_ranking_observed_at.date() != self.attested_at.date()
        ):
            raise ValueError("human paper entry selection chronology is invalid")
        if (
            self.attestation != ENTRY_SELECTION_EXACT_ATTESTATION
            or self.schema != ENTRY_SELECTION_EVIDENCE_SCHEMA
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human paper entry selection attestation changed")

    def _stable_document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "feedback_id": self.feedback_id,
            "candidate_id": self.candidate_id,
            "source_screen_content_sha256": (
                self.source_screen_content_sha256
            ),
            "symbol": self.symbol,
            "sector_id": self.sector_id,
            "sector_name": self.sector_name,
            "sector_ranking_evidence_id": self.sector_ranking_evidence_id,
            "sector_ranking_observed_at": (
                self.sector_ranking_observed_at.isoformat()
            ),
            "sector_catalog_revision": self.sector_catalog_revision,
            "sector_catalog_entry_sha256": self.sector_catalog_entry_sha256,
            "sector_catalog_captured_at": (
                self.sector_catalog_captured_at.isoformat()
            ),
            "attested_at": self.attested_at.isoformat(),
            "attestation": self.attestation,
            "live_status": self.live_status,
        }

    @property
    def evidence_id(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "evidence_id": self.evidence_id}


def parse_human_paper_entry_selection_evidence(
    raw: object,
) -> HumanPaperEntrySelectionEvidence:
    expected = {
        "schema",
        "feedback_id",
        "candidate_id",
        "source_screen_content_sha256",
        "symbol",
        "sector_id",
        "sector_name",
        "sector_ranking_evidence_id",
        "sector_ranking_observed_at",
        "sector_catalog_revision",
        "sector_catalog_entry_sha256",
        "sector_catalog_captured_at",
        "attested_at",
        "attestation",
        "live_status",
        "evidence_id",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("human paper entry selection evidence is malformed")
    try:
        evidence = HumanPaperEntrySelectionEvidence(
            feedback_id=str(raw["feedback_id"]),
            candidate_id=str(raw["candidate_id"]),
            source_screen_content_sha256=str(
                raw["source_screen_content_sha256"]
            ),
            symbol=str(raw["symbol"]),
            sector_id=str(raw["sector_id"]),
            sector_name=str(raw["sector_name"]),
            sector_ranking_evidence_id=str(raw["sector_ranking_evidence_id"]),
            sector_ranking_observed_at=datetime.fromisoformat(
                str(raw["sector_ranking_observed_at"])
            ),
            sector_catalog_revision=str(raw["sector_catalog_revision"]),
            sector_catalog_entry_sha256=str(
                raw["sector_catalog_entry_sha256"]
            ),
            sector_catalog_captured_at=datetime.fromisoformat(
                str(raw["sector_catalog_captured_at"])
            ),
            attested_at=datetime.fromisoformat(str(raw["attested_at"])),
            attestation=str(raw["attestation"]),
            schema=str(raw["schema"]),
            live_status=str(raw["live_status"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "human paper entry selection evidence is invalid"
        ) from exc
    if raw.get("evidence_id") != evidence.evidence_id:
        raise ValueError("human paper entry selection evidence identity changed")
    return evidence


@dataclass(frozen=True, slots=True)
class HumanPaperIntent:
    feedback_id: str
    candidate_id: str
    source_screen_content_sha256: str
    symbol: str
    side: Literal["BUY", "SELL"]
    created_at: datetime
    earliest_fill_at: datetime
    quantity: int
    reference_price: Decimal | None
    structural_invalidation_price: Decimal | None
    market_risk_gate: str
    sector_risk_gate: str
    symbol_risk_gate: str
    status: Literal[
        "PENDING",
        "BLOCKED_BY_RISK_GATE",
        "OBSERVATION_ONLY",
    ]
    reason_codes: tuple[str, ...]
    signal_lifecycle_id: str
    entry_confirmation_bar_closed_at: datetime | None = None
    entry_price_cap: Decimal | None = None
    entry_valid_until: datetime | None = None
    entry_boundary_evidence_id: str | None = None
    entry_execution_boundary: EntryExecutionBoundary | None = None
    entry_selection_evidence: HumanPaperEntrySelectionEvidence | None = None
    paper_contract_id: str = PAPER_CONTRACT_ID
    automated_order_authorized: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in ("created_at", "earliest_fill_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        for field in (
            "entry_confirmation_bar_closed_at",
            "entry_valid_until",
        ):
            if getattr(self, field) is not None:
                object.__setattr__(
                    self,
                    field,
                    normalize_datetime(getattr(self, field), field),
                )
        if self.earliest_fill_at < self.created_at:
            raise ValueError("paper intent fill time cannot predate creation")
        if self.quantity != 100:
            raise ValueError("human paper intent uses one fixed A-share lot")
        if any(
            value is not None
            and (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            )
            for value in (
                self.reference_price,
                self.structural_invalidation_price,
                self.entry_price_cap,
            )
        ):
            raise ValueError("human paper intent prices must be positive and finite")
        boundary_values = (
            self.entry_confirmation_bar_closed_at,
            self.entry_price_cap,
            self.entry_valid_until,
            self.entry_boundary_evidence_id,
        )
        if any(value is not None for value in boundary_values) and any(
            value is None for value in boundary_values
        ):
            raise ValueError("human paper entry execution boundary is incomplete")
        if self.entry_price_cap is not None and (
            self.entry_valid_until is None
            or self.entry_confirmation_bar_closed_at is None
            or self.entry_valid_until < self.entry_confirmation_bar_closed_at
            or _SHA256.fullmatch(str(self.entry_boundary_evidence_id)) is None
        ):
            raise ValueError("human paper entry execution boundary is invalid")
        if self.entry_execution_boundary is not None and (
            not isinstance(
                self.entry_execution_boundary,
                EntryExecutionBoundary,
            )
            or self.entry_execution_boundary.symbol != self.symbol
            or self.entry_execution_boundary.confirmation_bar_closed_at
            != self.entry_confirmation_bar_closed_at
            or self.entry_execution_boundary.raw_high != self.entry_price_cap
            or self.entry_execution_boundary.entry_valid_until
            != self.entry_valid_until
            or self.entry_execution_boundary.evidence_id
            != self.entry_boundary_evidence_id
        ):
            raise ValueError(
                "human paper full entry execution boundary does not match"
            )
        if self.side == "SELL" and any(
            value is not None for value in boundary_values
        ):
            raise ValueError("persistent sell intent cannot carry an entry TTL")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("human paper intent side is invalid")
        selection = self.entry_selection_evidence
        if selection is not None and (
            not isinstance(selection, HumanPaperEntrySelectionEvidence)
            or self.side != "BUY"
            or selection.feedback_id != self.feedback_id
            or selection.candidate_id != self.candidate_id
            or selection.source_screen_content_sha256
            != self.source_screen_content_sha256
            or selection.symbol != self.symbol
            or selection.attested_at != self.created_at
        ):
            raise ValueError(
                "human paper entry selection evidence does not match intent"
            )
        if any(
            gate not in _RISK_GATES
            for gate in (
                self.market_risk_gate,
                self.sector_risk_gate,
                self.symbol_risk_gate,
            )
        ):
            raise ValueError("human paper intent risk gate is invalid")
        if self.status not in {
            "PENDING",
            "BLOCKED_BY_RISK_GATE",
            "OBSERVATION_ONLY",
        }:
            raise ValueError("human paper intent status is invalid")
        if self.paper_contract_id != PAPER_CONTRACT_ID:
            raise ValueError("human paper contract identity changed")
        if self.automated_order_authorized or self.live_status != "LIVE_DISABLED":
            raise ValueError("human paper intent cannot authorize live trading")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("human paper intent reasons must be unique")
        if _SHA256.fullmatch(self.signal_lifecycle_id) is None:
            raise ValueError("human paper signal lifecycle identity is invalid")

    @property
    def intent_id(self) -> str:
        return sha256_json(self._stable_document())

    def _stable_document(self) -> dict[str, object]:
        stable = asdict(self)
        stable["entry_execution_boundary"] = (
            None
            if self.entry_execution_boundary is None
            else self.entry_execution_boundary.document()
        )
        stable["entry_selection_evidence"] = (
            None
            if self.entry_selection_evidence is None
            else self.entry_selection_evidence.document()
        )
        return _jsonable(stable)

    def document(self) -> dict[str, object]:
        """Portable intent retaining the complete raw 1m boundary proof."""

        return {**self._stable_document(), "intent_id": self.intent_id}


@dataclass(frozen=True, slots=True)
class HumanPaperMinuteBar:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    complete: bool = True
    suspended: bool = False
    limit_up_locked: bool = False
    limit_down_locked: bool = False
    buy_eligible: bool = False
    sell_eligible: bool = False
    security_status_complete: bool = False
    corporate_action_state_complete: bool = False
    execution_snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in ("opened_at", "closed_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if self.closed_at <= self.opened_at:
            raise ValueError("paper minute bar close must follow open")
        validate_a_share_completed_one_minute_interval(
            self.opened_at,
            self.closed_at,
        )
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("paper minute OHLC must be positive")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("paper minute OHLC is inconsistent")
        if self.volume < 0:
            raise ValueError("paper minute volume cannot be negative")
        if self.execution_snapshot_sha256 is not None and (
            _SHA256.fullmatch(self.execution_snapshot_sha256) is None
        ):
            raise ValueError("paper execution snapshot identity is invalid")
        if (
            self.security_status_complete
            or self.corporate_action_state_complete
            or self.buy_eligible
            or self.sell_eligible
        ) and self.execution_snapshot_sha256 is None:
            raise ValueError("complete paper execution facts require a snapshot identity")


@dataclass(frozen=True, slots=True)
class HumanPaperFill:
    intent_id: str
    symbol: str
    side: Literal["SELL"]
    quantity: int
    price: Decimal
    filled_at: datetime
    source_bar_closed_at: datetime
    execution_snapshot_sha256: str
    fill_model: str = STRICT_BAR_PRICE_RULE
    tick_data_used: bool = False
    virtual_only: bool = True
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in ("filled_at", "source_bar_closed_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if (
            self.quantity != 100
            or not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price <= 0
        ):
            raise ValueError("paper fill quantity or price is invalid")
        if self.side != "SELL":
            raise ValueError("basic human paper fills are SELL-only")
        if _SHA256.fullmatch(self.execution_snapshot_sha256) is None:
            raise ValueError("paper fill execution snapshot identity is invalid")
        validate_a_share_completed_one_minute_interval(
            self.source_bar_closed_at - timedelta(minutes=1),
            self.source_bar_closed_at,
        )
        if (
            self.filled_at != self.source_bar_closed_at
            or self.fill_model != STRICT_BAR_PRICE_RULE
            or self.tick_data_used
            or not self.virtual_only
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("paper fill boundary changed")

    @property
    def fill_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class HumanPaperCancellation:
    """Append-only human supersession of one still-pending virtual intent."""

    intent_id: str
    superseding_feedback_id: str
    candidate_id: str
    signal_lifecycle_id: str
    cancelled_at: datetime
    reason_code: Literal["SUPERSEDED_BY_LATER_HUMAN_FEEDBACK"]
    status: Literal["CANCELLED"] = "CANCELLED"
    paper_contract_id: str = PAPER_CONTRACT_ID
    virtual_only: bool = True
    broker_transport_available: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cancelled_at",
            normalize_datetime(self.cancelled_at, "cancelled_at"),
        )
        if any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.intent_id,
                self.superseding_feedback_id,
                self.candidate_id,
                self.signal_lifecycle_id,
            )
        ):
            raise ValueError("human paper cancellation identity is invalid")
        if self.reason_code != "SUPERSEDED_BY_LATER_HUMAN_FEEDBACK":
            raise ValueError("human paper cancellation reason is invalid")
        if (
            self.status != "CANCELLED"
            or self.paper_contract_id != PAPER_CONTRACT_ID
            or not self.virtual_only
            or self.broker_transport_available
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human paper cancellation safety boundary changed")

    @property
    def cancellation_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class HumanPaperOperationsCancellation:
    """Terminal cancellation of an optional BUY after an execution gate closes.

    This is deliberately different from :class:`HumanPaperCancellation`.
    Human supersession may replace an older judgement in the same signal
    lifecycle.  An operations cancellation consumes the lifecycle: once the
    required same-session evidence is unavailable *or* proves that the
    instrument cannot be bought, that historical BUY may never be back-filled
    after recovery/reopening.  Persistent SELL intents are not eligible for
    this event and continue independently when their own facts are complete.
    """

    intent_id: str
    symbol: str
    candidate_id: str
    signal_lifecycle_id: str
    cancelled_at: datetime
    execution_fact_snapshot_sha256: str
    execution_evidence_snapshot_sha256: str
    grid_status: Literal[
        "EXECUTION_FACT_MISSING_FAIL_CLOSED",
        "INCOMPLETE_FAIL_CLOSED",
        "INVALID_FAIL_CLOSED",
        "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
        "COMPLETE",
    ]
    reason_code: Literal[
        "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT",
        "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE",
    ] = "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
    status: Literal["CANCELLED"] = "CANCELLED"
    operations_state: Literal[
        "OPERATIONS_HALT",
        "SECURITY_GATE_CLOSED",
    ] = "OPERATIONS_HALT"
    paper_contract_id: str = PAPER_CONTRACT_ID
    virtual_only: bool = True
    broker_transport_available: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cancelled_at",
            normalize_datetime(self.cancelled_at, "cancelled_at"),
        )
        if not self.symbol or any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.intent_id,
                self.candidate_id,
                self.execution_fact_snapshot_sha256,
                self.execution_evidence_snapshot_sha256,
            )
        ) or _SHA256.fullmatch(self.signal_lifecycle_id) is None:
            raise ValueError("human paper operations cancellation identity is invalid")
        if self.grid_status not in {
            "EXECUTION_FACT_MISSING_FAIL_CLOSED",
            "INCOMPLETE_FAIL_CLOSED",
            "INVALID_FAIL_CLOSED",
            "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
            "COMPLETE",
        }:
            raise ValueError("human paper operations cancellation grid status is invalid")
        data_failure_statuses = {
            "EXECUTION_FACT_MISSING_FAIL_CLOSED",
            "INCOMPLETE_FAIL_CLOSED",
            "INVALID_FAIL_CLOSED",
            "COMPLETE",
        }
        security_gate_statuses = {
            "INCOMPLETE_FAIL_CLOSED",
            "INVALID_FAIL_CLOSED",
            "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
            "COMPLETE",
        }
        reason_state_valid = (
            self.reason_code
            == "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
            and self.operations_state == "OPERATIONS_HALT"
            and self.grid_status in data_failure_statuses
        ) or (
            self.reason_code
            == "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE"
            and self.operations_state == "SECURITY_GATE_CLOSED"
            and self.grid_status in security_gate_statuses
        )
        if (
            not reason_state_valid
            or self.status != "CANCELLED"
            or self.paper_contract_id != PAPER_CONTRACT_ID
            or not self.virtual_only
            or self.broker_transport_available
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError(
                "human paper operations cancellation safety boundary changed"
            )

    @property
    def cancellation_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class HumanPaperExecutionRejection:
    """Terminal optional-entry rejection proved by completed 1m evidence."""

    intent_id: str
    symbol: str
    candidate_bar_opened_at: datetime
    candidate_bar_closed_at: datetime
    candidate_price: Decimal | None
    entry_price_cap: Decimal
    entry_valid_until: datetime
    execution_snapshot_sha256: str
    reason_code: Literal[
        "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR",
        "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL",
    ]
    rejected_at: datetime
    status: Literal["EXECUTION_REJECTED"] = "EXECUTION_REJECTED"
    paper_contract_id: str = PAPER_CONTRACT_ID
    virtual_only: bool = True
    broker_transport_available: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in (
            "candidate_bar_opened_at",
            "candidate_bar_closed_at",
            "entry_valid_until",
            "rejected_at",
        ):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if (
            not self.intent_id
            or not self.symbol
            or self.rejected_at != self.candidate_bar_closed_at
            or not self.entry_price_cap.is_finite()
            or self.entry_price_cap <= 0
            or _SHA256.fullmatch(self.execution_snapshot_sha256) is None
        ):
            raise ValueError("human paper execution rejection facts are invalid")
        validate_a_share_completed_one_minute_interval(
            self.candidate_bar_opened_at,
            self.candidate_bar_closed_at,
        )
        if self.reason_code == "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR":
            if (
                self.candidate_price is None
                or not self.candidate_price.is_finite()
                or self.candidate_price <= self.entry_price_cap
                or self.candidate_bar_closed_at > self.entry_valid_until
            ):
                raise ValueError("human paper price-cap rejection is invalid")
        elif self.reason_code == "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL":
            if (
                self.candidate_price is not None
                or self.candidate_bar_closed_at < self.entry_valid_until
            ):
                raise ValueError("human paper TTL rejection is invalid")
        else:  # pragma: no cover - Literal runtime guard
            raise ValueError("human paper execution rejection reason is invalid")
        if (
            self.status != "EXECUTION_REJECTED"
            or self.paper_contract_id != PAPER_CONTRACT_ID
            or not self.virtual_only
            or self.broker_transport_available
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human paper execution rejection safety changed")

    @property
    def rejection_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class HumanPaperDecisionPositionMark:
    symbol: str
    quantity: int
    price: Decimal
    market_value: Decimal

    def __post_init__(self) -> None:
        if (
            not self.symbol
            or self.quantity <= 0
            or self.price <= 0
            or self.market_value != Decimal(self.quantity) * self.price
        ):
            raise ValueError("human paper decision position mark is invalid")


@dataclass(frozen=True, slots=True)
class HumanPaperPortfolioFill:
    """Atomic BUY fill plus the exact portfolio approval that allowed it."""

    intent_id: str
    symbol: str
    side: Literal["BUY"]
    quantity: int
    price: Decimal
    filled_at: datetime
    source_bar_closed_at: datetime
    execution_snapshot_sha256: str
    portfolio_decision_sha256: str
    accounting_contract_id: str
    available_cash: Decimal
    current_market_value: Decimal
    account_equity: Decimal
    notional: Decimal
    terminal_buy_fee: Decimal
    required_cash: Decimal
    occupied_slots: int
    slot_count: int
    slot_fraction: Decimal
    slot_notional_cap: Decimal
    account_exposure_cap: Decimal
    account_exposure_notional_cap: Decimal
    post_trade_gross_market_value: Decimal
    position_marks: tuple[HumanPaperDecisionPositionMark, ...]
    fill_model: str = STRICT_BAR_PRICE_RULE
    tick_data_used: bool = False
    virtual_only: bool = True
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in ("filled_at", "source_bar_closed_at"):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.intent_id,
                self.execution_snapshot_sha256,
                self.portfolio_decision_sha256,
                self.accounting_contract_id,
            )
        ):
            raise ValueError("human paper portfolio fill identity is invalid")
        if (
            not self.symbol
            or self.side != "BUY"
            or self.quantity != 100
            or self.price <= 0
            or self.available_cash < 0
            or self.current_market_value < 0
            or self.account_equity <= 0
            or self.notional <= 0
            or self.terminal_buy_fee < 0
            or self.required_cash <= 0
            or self.occupied_slots < 0
            or self.slot_count != 5
            or self.slot_fraction != Decimal("0.18")
            or self.account_exposure_cap != Decimal("0.90")
        ):
            raise ValueError("human paper portfolio fill values are invalid")
        if (
            self.notional != Decimal(self.quantity) * self.price
            or self.required_cash != self.notional + self.terminal_buy_fee
            or self.account_equity
            != self.available_cash + self.current_market_value
            or self.slot_notional_cap
            != self.account_equity * self.slot_fraction
            or self.account_exposure_notional_cap
            != self.account_equity * self.account_exposure_cap
            or self.post_trade_gross_market_value
            != self.current_market_value + self.notional
        ):
            raise ValueError("human paper portfolio fill equations changed")
        validate_a_share_completed_one_minute_interval(
            self.source_bar_closed_at - timedelta(minutes=1),
            self.source_bar_closed_at,
        )
        mark_symbols = tuple(value.symbol for value in self.position_marks)
        if (
            mark_symbols != tuple(sorted(mark_symbols))
            or len(mark_symbols) != len(set(mark_symbols))
            or self.occupied_slots != len(self.position_marks)
            or self.current_market_value
            != sum(
                (value.market_value for value in self.position_marks),
                Decimal("0"),
            )
        ):
            raise ValueError("human paper portfolio fill marks are invalid")
        if (
            self.symbol in mark_symbols
            or (
                self.symbol not in mark_symbols
                and self.occupied_slots >= self.slot_count
            )
            or self.required_cash > self.available_cash
            or self.notional > self.slot_notional_cap
            or self.post_trade_gross_market_value
            > self.account_exposure_notional_cap
        ):
            raise ValueError("human paper portfolio fill was not allowed")
        if (
            self.filled_at != self.source_bar_closed_at
            or self.fill_model != STRICT_BAR_PRICE_RULE
            or self.tick_data_used
            or not self.virtual_only
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("paper portfolio fill boundary changed")
        stable = {
            "schema": "chanlun-human-paper-portfolio-decision",
            "accounting_contract_id": self.accounting_contract_id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "price": format(self.price, "f"),
            "session": self.filled_at.date().isoformat(),
            "available_cash": format(self.available_cash, "f"),
            "current_market_value": format(self.current_market_value, "f"),
            "account_equity": format(self.account_equity, "f"),
            "notional": format(self.notional, "f"),
            "terminal_buy_fee": format(self.terminal_buy_fee, "f"),
            "required_cash": format(self.required_cash, "f"),
            "occupied_slots": self.occupied_slots,
            "slot_count": self.slot_count,
            "slot_fraction": format(self.slot_fraction, "f"),
            "slot_notional_cap": format(self.slot_notional_cap, "f"),
            "account_exposure_cap": format(self.account_exposure_cap, "f"),
            "account_exposure_notional_cap": format(
                self.account_exposure_notional_cap,
                "f",
            ),
            "post_trade_gross_market_value": format(
                self.post_trade_gross_market_value,
                "f",
            ),
            "position_marks": [
                {
                    "symbol": value.symbol,
                    "quantity": value.quantity,
                    "price": format(value.price, "f"),
                    "market_value": format(value.market_value, "f"),
                }
                for value in self.position_marks
            ],
            "allowed": True,
            "reason_codes": (),
            "slot_fraction_notional_gate_evaluable": True,
            "account_exposure_notional_gate_evaluable": True,
            "fixed_one_lot_diagnostic": True,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
        if self.portfolio_decision_sha256 != sha256_json(stable):
            raise ValueError("human paper portfolio fill decision identity changed")

    @property
    def fill_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class HumanPaperPortfolioRejection:
    """Terminal rejection with synchronous marks and 18%/90% gates."""

    intent_id: str
    symbol: str
    quantity: int
    candidate_bar_opened_at: datetime
    candidate_bar_closed_at: datetime
    candidate_price: Decimal
    execution_snapshot_sha256: str
    portfolio_decision_sha256: str
    accounting_contract_id: str
    available_cash: Decimal
    current_market_value: Decimal
    account_equity: Decimal
    notional: Decimal
    terminal_buy_fee: Decimal
    required_cash: Decimal
    occupied_slots: int
    slot_count: int
    slot_fraction: Decimal
    slot_notional_cap: Decimal
    account_exposure_cap: Decimal
    account_exposure_notional_cap: Decimal
    post_trade_gross_market_value: Decimal
    position_marks: tuple[HumanPaperDecisionPositionMark, ...]
    reason_codes: tuple[str, ...]
    rejected_at: datetime
    status: Literal["PORTFOLIO_REJECTED"] = "PORTFOLIO_REJECTED"
    paper_contract_id: str = PAPER_CONTRACT_ID
    virtual_only: bool = True
    broker_transport_available: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        for field in (
            "candidate_bar_opened_at",
            "candidate_bar_closed_at",
            "rejected_at",
        ):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.intent_id,
                self.execution_snapshot_sha256,
                self.portfolio_decision_sha256,
                self.accounting_contract_id,
            )
        ):
            raise ValueError("human paper portfolio rejection identity is invalid")
        if (
            not self.symbol
            or self.quantity != 100
            or self.candidate_price <= 0
            or self.available_cash < 0
            or self.current_market_value < 0
            or self.account_equity <= 0
            or self.notional <= 0
            or self.terminal_buy_fee < 0
            or self.required_cash <= 0
            or self.occupied_slots < 0
            or self.slot_count != 5
            or self.slot_fraction != Decimal("0.18")
            or self.account_exposure_cap != Decimal("0.90")
        ):
            raise ValueError("human paper portfolio rejection values are invalid")
        if (
            self.notional != Decimal(self.quantity) * self.candidate_price
            or self.required_cash != self.notional + self.terminal_buy_fee
            or self.account_equity
            != self.available_cash + self.current_market_value
            or self.slot_notional_cap
            != self.account_equity * self.slot_fraction
            or self.account_exposure_notional_cap
            != self.account_equity * self.account_exposure_cap
            or self.post_trade_gross_market_value
            != self.current_market_value + self.notional
        ):
            raise ValueError("human paper portfolio rejection equations changed")
        if self.rejected_at != self.candidate_bar_closed_at:
            raise ValueError("human paper portfolio rejection timing is invalid")
        validate_a_share_completed_one_minute_interval(
            self.candidate_bar_opened_at,
            self.candidate_bar_closed_at,
        )
        mark_symbols = tuple(value.symbol for value in self.position_marks)
        if (
            mark_symbols != tuple(sorted(mark_symbols))
            or len(mark_symbols) != len(set(mark_symbols))
            or self.occupied_slots != len(self.position_marks)
            or self.current_market_value
            != sum(
                (value.market_value for value in self.position_marks),
                Decimal("0"),
            )
        ):
            raise ValueError("human paper portfolio rejection marks are invalid")
        expected_reasons = tuple(
            value
            for value, violated in (
                (
                    "VIRTUAL_SYMBOL_ALREADY_OCCUPIES_STRATEGIC_SLOT",
                    self.symbol in mark_symbols,
                ),
                (
                    "NO_FREE_VIRTUAL_STRATEGIC_SLOT",
                    self.symbol not in mark_symbols
                    and self.occupied_slots >= self.slot_count,
                ),
                (
                    "INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES",
                    self.required_cash > self.available_cash,
                ),
                (
                    "VIRTUAL_ENTRY_EXCEEDS_ONE_SLOT_NOTIONAL_CAP",
                    self.notional > self.slot_notional_cap,
                ),
                (
                    "VIRTUAL_ACCOUNT_EXPOSURE_CAP_EXCEEDED",
                    self.post_trade_gross_market_value
                    > self.account_exposure_notional_cap,
                ),
            )
            if violated
        )
        if not expected_reasons or self.reason_codes != expected_reasons:
            raise ValueError("human paper portfolio rejection reasons are invalid")
        if (
            self.status != "PORTFOLIO_REJECTED"
            or self.paper_contract_id != PAPER_CONTRACT_ID
            or not self.virtual_only
            or self.broker_transport_available
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human paper portfolio rejection safety boundary changed")
        stable = {
            "schema": "chanlun-human-paper-portfolio-decision",
            "accounting_contract_id": self.accounting_contract_id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "price": format(self.candidate_price, "f"),
            "session": self.candidate_bar_opened_at.date().isoformat(),
            "available_cash": format(self.available_cash, "f"),
            "current_market_value": format(self.current_market_value, "f"),
            "account_equity": format(self.account_equity, "f"),
            "notional": format(self.notional, "f"),
            "terminal_buy_fee": format(self.terminal_buy_fee, "f"),
            "required_cash": format(self.required_cash, "f"),
            "occupied_slots": self.occupied_slots,
            "slot_count": self.slot_count,
            "slot_fraction": format(self.slot_fraction, "f"),
            "slot_notional_cap": format(self.slot_notional_cap, "f"),
            "account_exposure_cap": format(self.account_exposure_cap, "f"),
            "account_exposure_notional_cap": format(
                self.account_exposure_notional_cap,
                "f",
            ),
            "post_trade_gross_market_value": format(
                self.post_trade_gross_market_value,
                "f",
            ),
            "position_marks": [
                {
                    "symbol": value.symbol,
                    "quantity": value.quantity,
                    "price": format(value.price, "f"),
                    "market_value": format(value.market_value, "f"),
                }
                for value in self.position_marks
            ],
            "allowed": False,
            "reason_codes": self.reason_codes,
            "slot_fraction_notional_gate_evaluable": True,
            "account_exposure_notional_gate_evaluable": True,
            "fixed_one_lot_diagnostic": True,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
        if self.portfolio_decision_sha256 != sha256_json(stable):
            raise ValueError("human paper portfolio decision identity changed")

    @property
    def rejection_id(self) -> str:
        return sha256_json(asdict(self))


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _document(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "paper_contract_id": PAPER_CONTRACT_ID,
        "events": [dict(value) for value in events],
        "broker_transport_available": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    output["content_sha256"] = sha256_json(output)
    return output


def human_paper_ledger_content_sha256(
    events: Sequence[Mapping[str, object]],
) -> str:
    """Return the canonical identity of a caller-validated event prefix."""

    return str(_document(events)["content_sha256"])


def human_paper_event_effective_at(event: Mapping[str, object]) -> datetime:
    """Return the causal market/review time carried by one validated event."""

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("human paper event payload is malformed")
    field_by_kind = {
        "INTENT": "created_at",
        "FILL": "filled_at",
        "CANCEL": "cancelled_at",
        "OPERATIONS_CANCEL": "cancelled_at",
        "EXECUTION_REJECT": "rejected_at",
        "PORTFOLIO_REJECT": "rejected_at",
    }
    field = field_by_kind.get(str(event.get("kind") or ""))
    if field is None:
        raise ValueError("human paper event kind has no causal time")
    try:
        return normalize_datetime(
            datetime.fromisoformat(str(payload[field])),
            field,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("human paper event causal time is malformed") from exc


def human_paper_ledger_prefix_for_identity(
    events: Sequence[Mapping[str, object]],
    *,
    content_sha256: str,
    observed_at: datetime | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Resolve one immutable ledger identity to its exact historical prefix.

    The live paper ledger is append-only, while a daily valuation must keep
    referring to the book that existed when that close was captured.  A
    syntactically valid hash is not enough: it must be the content identity of
    one prefix of the currently validated ledger.  Callers must pass events
    from :func:`load_human_paper_ledger`; this helper deliberately does not
    invent or repair an event chain.
    """

    if _SHA256.fullmatch(content_sha256) is None:
        raise ValueError("human paper ledger prefix identity is invalid")
    values = tuple(events)
    for length in range(len(values) + 1):
        prefix = values[:length]
        if human_paper_ledger_content_sha256(prefix) == content_sha256:
            if observed_at is not None:
                observed = normalize_datetime(observed_at, "observed_at")
                if any(
                    human_paper_event_effective_at(event) > observed
                    for event in prefix
                ):
                    raise ValueError(
                        "human paper ledger prefix contains a future event"
                    )
                if any(
                    human_paper_event_effective_at(event) <= observed
                    for event in values[length:]
                ):
                    raise ValueError(
                        "human paper ledger prefix omits a causal event"
                    )
            return prefix
    raise ValueError("human paper ledger identity is not a historical prefix")


def _intent_from_payload(payload: object) -> HumanPaperIntent:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper intent payload is malformed")
    field_names = tuple(field.name for field in fields(HumanPaperIntent))
    expected = set(field_names) | {"intent_id"}
    if set(payload) != expected:
        raise ValueError("human paper intent fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        for name in (
            "created_at",
            "earliest_fill_at",
            "entry_confirmation_bar_closed_at",
            "entry_valid_until",
        ):
            if name not in values or values[name] is None:
                continue
            values[name] = datetime.fromisoformat(str(values[name]))
        for name in (
            "reference_price",
            "structural_invalidation_price",
            "entry_price_cap",
        ):
            if name not in values:
                continue
            values[name] = (
                None if values[name] is None else Decimal(str(values[name]))
            )
        if values.get("entry_execution_boundary") is not None:
            values["entry_execution_boundary"] = (
                parse_entry_execution_boundary_document(
                    values["entry_execution_boundary"]
                )
            )
        if values.get("entry_selection_evidence") is not None:
            values["entry_selection_evidence"] = (
                parse_human_paper_entry_selection_evidence(
                    values["entry_selection_evidence"]
                )
            )
        values["reason_codes"] = tuple(values["reason_codes"])
        intent = HumanPaperIntent(**values)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("human paper intent payload is invalid") from exc
    if payload.get("intent_id") != intent.intent_id:
        raise ValueError("human paper intent identity changed")
    return intent


def audit_human_paper_entry_boundary_attestations(
    events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Recompute every self-contained raw-1m optional-entry attestation.

    Every boundary-bearing intent must retain the complete raw OHLCV proof.
    This audit intentionally does
    not replace the immutable source-report link—it proves that the ledger
    itself retains a recomputable OHLCV boundary.
    """

    boundary_intent_count = 0
    verified = 0
    invalid: list[dict[str, str]] = []
    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "INTENT" or not isinstance(payload, Mapping):
            continue
        if payload.get("side") != "BUY":
            continue
        boundary_id_present = payload.get("entry_boundary_evidence_id") is not None
        full_present = payload.get("entry_execution_boundary") is not None
        if not boundary_id_present and not full_present:
            continue
        boundary_intent_count += 1
        intent_id = str(payload.get("intent_id") or "UNKNOWN_INTENT")
        if not full_present:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": "ENTRY_EXECUTION_BOUNDARY_REQUIRED",
                }
            )
            continue
        try:
            boundary = parse_entry_execution_boundary_document(
                payload["entry_execution_boundary"]
            )
            confirmation = normalize_datetime(
                datetime.fromisoformat(
                    str(payload["entry_confirmation_bar_closed_at"])
                ),
                "entry_confirmation_bar_closed_at",
            )
            valid_until = normalize_datetime(
                datetime.fromisoformat(str(payload["entry_valid_until"])),
                "entry_valid_until",
            )
            price_cap = Decimal(str(payload["entry_price_cap"]))
            if (
                boundary.symbol != payload.get("symbol")
                or boundary.confirmation_bar_closed_at != confirmation
                or boundary.raw_high != price_cap
                or boundary.entry_valid_until != valid_until
                or boundary.evidence_id
                != payload.get("entry_boundary_evidence_id")
            ):
                raise ValueError("full boundary does not match reduced terms")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            continue
        verified += 1
    if invalid:
        status = "INVALID"
    elif boundary_intent_count:
        status = "COMPLETE"
    else:
        status = "NO_BOUNDARY_INTENTS"
    return {
        "schema": (
            "chanlun-human-paper-entry-boundary-attestation-audit"
        ),
        "status": status,
        "boundary_intent_count": boundary_intent_count,
        "verified_full_boundary_count": verified,
        "invalid_attestations": invalid,
        "raw_unadjusted_one_minute_ohlcv_self_contained": (
            status in {"COMPLETE", "NO_BOUNDARY_INTENTS"}
        ),
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_entry_selection_attestations(
    events: Sequence[Mapping[str, object]],
    *,
    sector_catalog_entries: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Replay exact QMT name/member admission for evidence-bearing buys.

    An evidence object without its referenced catalog is retained but cannot
    be called verified.  A supplied entry is accepted only after its canonical
    QMT catalog identity is recomputed; matching a convenient revision string
    alone is insufficient.
    """

    catalog_index: dict[str, list[Mapping[str, object]]] = {}
    for entry in sector_catalog_entries:
        if isinstance(entry, Mapping):
            catalog_index.setdefault(
                str(entry.get("entry_sha256") or ""),
                [],
            ).append(entry)

    attested_count = 0
    verified = 0
    verified_intent_ids: list[str] = []
    unavailable: list[str] = []
    invalid: list[dict[str, str]] = []
    evidence_ids: set[str] = set()
    catalog_entry_ids: set[str] = set()
    for event in events:
        payload = event.get("payload")
        if (
            event.get("kind") != "INTENT"
            or not isinstance(payload, Mapping)
            or payload.get("side") != "BUY"
            or payload.get("entry_selection_evidence") is None
        ):
            continue
        attested_count += 1
        intent_id = str(payload.get("intent_id") or "UNKNOWN_INTENT")
        try:
            evidence = parse_human_paper_entry_selection_evidence(
                payload["entry_selection_evidence"]
            )
            created_at = normalize_datetime(
                datetime.fromisoformat(str(payload["created_at"])),
                "created_at",
            )
            if (
                evidence.feedback_id != payload.get("feedback_id")
                or evidence.candidate_id != payload.get("candidate_id")
                or evidence.source_screen_content_sha256
                != payload.get("source_screen_content_sha256")
                or evidence.symbol != payload.get("symbol")
                or evidence.attested_at != created_at
            ):
                raise ValueError(
                    "selection evidence does not match its paper intent"
                )
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            continue

        matches = catalog_index.get(evidence.sector_catalog_entry_sha256, [])
        if not matches:
            unavailable.append(intent_id)
            continue
        if len(matches) != 1:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": "QMT_CATALOG_ENTRY_IDENTITY_DUPLICATED",
                }
            )
            continue
        entry = matches[0]
        try:
            canonical = catalog_capture_entry(
                {
                    "source": "qmt_gics3_components",
                    "captured_at": entry["captured_at"],
                    "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
                    "catalog_revision": entry["catalog_revision"],
                    "sectors": entry["sectors"],
                },
                previous_entry_sha256=entry.get("previous_entry_sha256"),
            )
            if dict(entry) != canonical:
                raise ValueError("QMT catalog entry is not canonical")
            captured_at = normalize_datetime(
                datetime.fromisoformat(str(entry["captured_at"])),
                "catalog.captured_at",
            )
            sector = next(
                (
                    value
                    for value in entry["sectors"]
                    if isinstance(value, Mapping)
                    and value.get("sector_id") == evidence.sector_id
                ),
                None,
            )
            if (
                entry.get("entry_sha256")
                != evidence.sector_catalog_entry_sha256
                or entry.get("catalog_revision")
                != evidence.sector_catalog_revision
                or captured_at != evidence.sector_catalog_captured_at
                or sector is None
                or sector.get("name") != evidence.sector_name
                or evidence.symbol
                not in {str(value) for value in sector.get("member_codes") or ()}
            ):
                raise ValueError(
                    "QMT catalog does not prove exact sector name and membership"
                )
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            continue
        verified += 1
        verified_intent_ids.append(intent_id)
        evidence_ids.add(evidence.evidence_id)
        catalog_entry_ids.add(evidence.sector_catalog_entry_sha256)

    if invalid:
        status = "INVALID"
    elif unavailable:
        status = "INCOMPLETE_CATALOG_ARCHIVE"
    elif attested_count:
        status = "COMPLETE"
    else:
        status = "NO_SELECTION_ATTESTATIONS"
    return {
        "schema": "chanlun-human-paper-entry-selection-attestation-audit",
        "status": status,
        "attested_buy_intent_count": attested_count,
        "verified_catalog_binding_count": verified,
        "verified_buy_intent_ids": sorted(verified_intent_ids),
        "catalog_unavailable_intent_ids": unavailable,
        "invalid_attestations": invalid,
        "selection_evidence_ids": sorted(evidence_ids),
        "catalog_entry_sha256s": sorted(catalog_entry_ids),
        "exact_qmt_revision_name_and_membership_verified": status
        in {"COMPLETE", "NO_SELECTION_ATTESTATIONS"},
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_entry_selection_source_bindings(
    events: Sequence[Mapping[str, object]],
    *,
    alerts_by_source_content_sha256: Mapping[
        str, Sequence[HumanReviewAlert]
    ],
) -> dict[str, object]:
    """Bind required selection proofs to immutable source ranking rows.

    File discovery stays with the page or forward runtime.  This pure audit is
    the single semantic core used by both, so an execution process cannot
    accept a catalog proof that the review page would reject as unrelated to
    the source candidate.
    """

    sources: dict[str, tuple[HumanReviewAlert, ...]] = {}
    malformed_sources: set[str] = set()
    for source_hash, raw_alerts in alerts_by_source_content_sha256.items():
        if _SHA256.fullmatch(str(source_hash)) is None:
            malformed_sources.add(str(source_hash))
            continue
        alerts = tuple(raw_alerts)
        if any(not isinstance(value, HumanReviewAlert) for value in alerts):
            malformed_sources.add(str(source_hash))
            continue
        candidate_ids = tuple(value.candidate_id for value in alerts)
        if len(candidate_ids) != len(set(candidate_ids)):
            malformed_sources.add(str(source_hash))
            continue
        sources[str(source_hash)] = alerts

    required_count = 0
    verified = 0
    verified_intent_ids: list[str] = []
    unavailable: list[str] = []
    invalid: list[dict[str, str]] = []
    for event in events:
        payload = event.get("payload")
        if (
            event.get("kind") != "INTENT"
            or not isinstance(payload, Mapping)
            or payload.get("side") != "BUY"
        ):
            continue
        intent_id = str(payload.get("intent_id") or "UNKNOWN_INTENT")
        source_hash = str(payload.get("source_screen_content_sha256") or "")
        if source_hash in malformed_sources:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": "SOURCE_REPORT_ALERT_SET_INVALID",
                }
            )
            continue
        source_alerts = sources.get(source_hash)
        if source_alerts is None:
            unavailable.append(intent_id)
            continue
        source_alert = next(
            (
                value
                for value in source_alerts
                if value.candidate_id == payload.get("candidate_id")
            ),
            None,
        )
        if source_alert is None:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": "SOURCE_REPORT_CANDIDATE_NOT_FOUND",
                }
            )
            continue
        ranking = source_alert.sector_ranking_evidence
        required = (
            source_alert.alert_type == "POSSIBLE_30M_BUY"
            and ranking is not None
        )
        raw_evidence = payload.get("entry_selection_evidence")
        if not required:
            if raw_evidence is not None:
                invalid.append(
                    {
                        "intent_id": intent_id,
                        "reason": "SELECTION_EVIDENCE_NOT_APPLICABLE_TO_SOURCE",
                    }
                )
            continue
        required_count += 1
        if raw_evidence is None:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": "ENTRY_SELECTION_EVIDENCE_REQUIRED",
                }
            )
            continue
        try:
            evidence = parse_human_paper_entry_selection_evidence(raw_evidence)
            if (
                evidence.feedback_id != payload.get("feedback_id")
                or evidence.candidate_id != source_alert.candidate_id
                or evidence.source_screen_content_sha256 != source_hash
                or evidence.symbol != source_alert.symbol
                or evidence.sector_id != source_alert.sector_id
                or evidence.sector_id != ranking.sector_id
                or evidence.sector_name != ranking.sector_name
                or evidence.sector_ranking_evidence_id != ranking.evidence_id
                or evidence.sector_ranking_observed_at != ranking.observed_at
                or evidence.sector_catalog_revision
                != ranking.sector_catalog_revision
            ):
                raise ValueError("ledger selection differs from source ranking")
        except (TypeError, ValueError) as exc:
            invalid.append(
                {
                    "intent_id": intent_id,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            continue
        verified += 1
        verified_intent_ids.append(intent_id)

    if invalid:
        status = "INVALID"
    elif unavailable:
        status = "INCOMPLETE_SOURCE_ARCHIVE"
    elif required_count:
        status = "COMPLETE"
    else:
        status = "NO_REQUIRED_SELECTION_INTENTS"
    return {
        "schema": "chanlun-human-paper-entry-selection-source-audit",
        "status": status,
        "required_live_ranked_buy_intent_count": required_count,
        "verified_source_binding_count": verified,
        "verified_required_buy_intent_ids": sorted(verified_intent_ids),
        "source_unavailable_intent_ids": unavailable,
        "invalid_source_bindings": invalid,
        "immutable_source_ranking_resolved": status
        in {"COMPLETE", "NO_REQUIRED_SELECTION_INTENTS"},
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def _fill_from_payload(
    payload: object,
) -> HumanPaperFill | HumanPaperPortfolioFill:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper fill payload is malformed")
    sell_fields = tuple(field.name for field in fields(HumanPaperFill))
    portfolio_fields = tuple(
        field.name for field in fields(HumanPaperPortfolioFill)
    )
    actual_fields = set(payload)
    if (
        payload.get("side") == "SELL"
        and actual_fields == set(sell_fields) | {"fill_id"}
    ):
        fill_type = HumanPaperFill
        field_names = sell_fields
    elif (
        payload.get("side") == "BUY"
        and actual_fields == set(portfolio_fields) | {"fill_id"}
    ):
        fill_type = HumanPaperPortfolioFill
        field_names = portfolio_fields
    else:
        raise ValueError("human paper fill fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        for name in ("filled_at", "source_bar_closed_at"):
            values[name] = datetime.fromisoformat(str(values[name]))
        decimal_names = {"price"}
        if fill_type is HumanPaperPortfolioFill:
            decimal_names.update(
                {
                    "available_cash",
                    "current_market_value",
                    "account_equity",
                    "notional",
                    "terminal_buy_fee",
                    "required_cash",
                    "slot_fraction",
                    "slot_notional_cap",
                    "account_exposure_cap",
                    "account_exposure_notional_cap",
                    "post_trade_gross_market_value",
                }
            )
            raw_marks = values["position_marks"]
            if not isinstance(raw_marks, list):
                raise TypeError("portfolio fill marks are malformed")
            values["position_marks"] = tuple(
                HumanPaperDecisionPositionMark(
                    symbol=str(value["symbol"]),
                    quantity=int(value["quantity"]),
                    price=Decimal(str(value["price"])),
                    market_value=Decimal(str(value["market_value"])),
                )
                for value in raw_marks
                if isinstance(value, Mapping)
            )
            if len(values["position_marks"]) != len(raw_marks):
                raise TypeError("portfolio fill marks are malformed")
        for name in decimal_names:
            values[name] = Decimal(str(values[name]))
        fill = fill_type(**values)
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("human paper fill payload is invalid") from exc
    if payload.get("fill_id") != fill.fill_id:
        raise ValueError("human paper fill identity changed")
    return fill


def _cancellation_from_payload(payload: object) -> HumanPaperCancellation:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper cancellation payload is malformed")
    field_names = tuple(field.name for field in fields(HumanPaperCancellation))
    if set(payload) != set(field_names) | {"cancellation_id"}:
        raise ValueError("human paper cancellation fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        values["cancelled_at"] = datetime.fromisoformat(
            str(values["cancelled_at"])
        )
        cancellation = HumanPaperCancellation(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError("human paper cancellation payload is invalid") from exc
    if payload.get("cancellation_id") != cancellation.cancellation_id:
        raise ValueError("human paper cancellation identity changed")
    return cancellation


def _operations_cancellation_from_payload(
    payload: object,
) -> HumanPaperOperationsCancellation:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper operations cancellation payload is malformed")
    field_names = tuple(
        field.name for field in fields(HumanPaperOperationsCancellation)
    )
    if set(payload) != set(field_names) | {"cancellation_id"}:
        raise ValueError("human paper operations cancellation fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        values["cancelled_at"] = datetime.fromisoformat(
            str(values["cancelled_at"])
        )
        cancellation = HumanPaperOperationsCancellation(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "human paper operations cancellation payload is invalid"
        ) from exc
    if payload.get("cancellation_id") != cancellation.cancellation_id:
        raise ValueError("human paper operations cancellation identity changed")
    return cancellation


def _execution_rejection_from_payload(
    payload: object,
) -> HumanPaperExecutionRejection:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper execution rejection payload is malformed")
    field_names = tuple(field.name for field in fields(HumanPaperExecutionRejection))
    if set(payload) != set(field_names) | {"rejection_id"}:
        raise ValueError("human paper execution rejection fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        for name in (
            "candidate_bar_opened_at",
            "candidate_bar_closed_at",
            "entry_valid_until",
            "rejected_at",
        ):
            values[name] = datetime.fromisoformat(str(values[name]))
        values["candidate_price"] = (
            None
            if values["candidate_price"] is None
            else Decimal(str(values["candidate_price"]))
        )
        values["entry_price_cap"] = Decimal(str(values["entry_price_cap"]))
        rejection = HumanPaperExecutionRejection(**values)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("human paper execution rejection payload is invalid") from exc
    if payload.get("rejection_id") != rejection.rejection_id:
        raise ValueError("human paper execution rejection identity changed")
    return rejection


def _portfolio_rejection_from_payload(
    payload: object,
) -> HumanPaperPortfolioRejection:
    if not isinstance(payload, Mapping):
        raise ValueError("human paper portfolio rejection payload is malformed")
    field_names = tuple(field.name for field in fields(HumanPaperPortfolioRejection))
    if set(payload) != set(field_names) | {"rejection_id"}:
        raise ValueError("human paper portfolio rejection fields changed")
    values = {name: payload[name] for name in field_names}
    try:
        for name in (
            "candidate_bar_opened_at",
            "candidate_bar_closed_at",
            "rejected_at",
        ):
            values[name] = datetime.fromisoformat(str(values[name]))
        for name in (
            "candidate_price",
            "available_cash",
            "current_market_value",
            "account_equity",
            "notional",
            "terminal_buy_fee",
            "required_cash",
            "slot_fraction",
            "slot_notional_cap",
            "account_exposure_cap",
            "account_exposure_notional_cap",
            "post_trade_gross_market_value",
        ):
            values[name] = Decimal(str(values[name]))
        raw_marks = values["position_marks"]
        if not isinstance(raw_marks, list):
            raise TypeError("portfolio rejection marks are malformed")
        values["position_marks"] = tuple(
            HumanPaperDecisionPositionMark(
                symbol=str(value["symbol"]),
                quantity=int(value["quantity"]),
                price=Decimal(str(value["price"])),
                market_value=Decimal(str(value["market_value"])),
            )
            for value in raw_marks
            if isinstance(value, Mapping)
        )
        if len(values["position_marks"]) != len(raw_marks):
            raise TypeError("portfolio rejection marks are malformed")
        values["reason_codes"] = tuple(values["reason_codes"])
        rejection = HumanPaperPortfolioRejection(**values)
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("human paper portfolio rejection payload is invalid") from exc
    if payload.get("rejection_id") != rejection.rejection_id:
        raise ValueError("human paper portfolio rejection identity changed")
    return rejection


def load_human_paper_ledger(path: Path) -> dict[str, object]:
    if not path.is_file():
        return _document(())
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != LEDGER_SCHEMA:
        raise ValueError("unsupported human paper ledger")
    claimed = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if claimed != sha256_json(stable):
        raise ValueError("human paper ledger content hash mismatch")
    if (
        payload.get("paper_contract_id") != PAPER_CONTRACT_ID
        or payload.get("broker_transport_available") is not False
        or payload.get("automated_order_authorized") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("human paper ledger safety boundary changed")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("human paper events must be a list")
    previous = None
    seen: set[str] = set()
    intents: dict[str, HumanPaperIntent] = {}
    filled_intents: set[str] = set()
    cancelled_intents: set[str] = set()
    execution_rejected_intents: set[str] = set()
    portfolio_rejected_intents: set[str] = set()
    lots_by_symbol: dict[str, list[list[object]]] = {}
    active_pending_lifecycles: dict[str, str] = {}
    consumed_signal_lifecycles: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"human paper event {index} is malformed")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            raise ValueError(f"human paper event identity invalid at {index}")
        stable_event = dict(event)
        stable_event.pop("event_id", None)
        if stable_event.get("previous_event_id") != previous:
            raise ValueError(f"human paper event chain broke at {index}")
        if event_id != sha256_json(stable_event):
            raise ValueError(f"human paper event hash mismatch at {index}")
        kind = event.get("kind")
        if kind == "INTENT":
            intent_payload = event.get("payload")
            intent = _intent_from_payload(intent_payload)
            if not isinstance(intent_payload, Mapping):
                raise ValueError("human paper intent payload is malformed")
            intent_id = str(intent_payload["intent_id"])
            if intent_id in intents:
                raise ValueError("human paper intent identity is duplicated")
            lifecycle = intent.signal_lifecycle_id
            if intent.status == "PENDING":
                if lifecycle in consumed_signal_lifecycles:
                    raise ValueError(
                        "human paper pending intent reused a consumed signal lifecycle"
                    )
                active_intent_id = active_pending_lifecycles.get(lifecycle)
                if active_intent_id is not None and active_intent_id != intent_id:
                    raise ValueError(
                        "human paper signal lifecycle has duplicate pending intents"
                    )
                active_pending_lifecycles[lifecycle] = intent_id
            intents[intent_id] = intent
        elif kind == "FILL":
            fill = _fill_from_payload(event.get("payload"))
            intent = intents.get(fill.intent_id)
            if intent is None:
                raise ValueError("human paper fill has no preceding intent")
            if (
                fill.intent_id in filled_intents
                or fill.intent_id in cancelled_intents
                or fill.intent_id in execution_rejected_intents
                or fill.intent_id in portfolio_rejected_intents
            ):
                raise ValueError("human paper intent was already terminal")
            if (
                intent.status != "PENDING"
                or fill.symbol != intent.symbol
                or fill.side != intent.side
                or fill.quantity != intent.quantity
                or fill.filled_at < intent.earliest_fill_at
                or (
                    fill.side == "BUY"
                    and (
                        intent.entry_price_cap is None
                        or intent.entry_valid_until is None
                        or fill.price > intent.entry_price_cap
                        or fill.source_bar_closed_at > intent.entry_valid_until
                    )
                )
            ):
                raise ValueError("human paper fill does not match its intent")
            if fill.side == "SELL":
                lots = lots_by_symbol.setdefault(fill.symbol, [])
                if _sellable_quantity(lots, fill.filled_at) < fill.quantity:
                    raise ValueError(
                        "human paper ledger contains an oversell or T+1 violation"
                    )
                _consume_sellable_lots(
                    lots,
                    at=fill.filled_at,
                    quantity=fill.quantity,
                )
            else:
                lots_by_symbol.setdefault(fill.symbol, []).append(
                    [fill.filled_at.date(), fill.quantity]
                )
            active_pending_lifecycles.pop(intent.signal_lifecycle_id, None)
            if intent.signal_lifecycle_id in consumed_signal_lifecycles:
                raise ValueError(
                    "human paper signal lifecycle has multiple terminal outcomes"
                )
            consumed_signal_lifecycles.add(intent.signal_lifecycle_id)
            filled_intents.add(fill.intent_id)
        elif kind == "CANCEL":
            cancellation = _cancellation_from_payload(event.get("payload"))
            intent = intents.get(cancellation.intent_id)
            if intent is None:
                raise ValueError("human paper cancellation has no preceding intent")
            if (
                intent.status != "PENDING"
                or cancellation.intent_id in filled_intents
                or cancellation.intent_id in cancelled_intents
                or cancellation.intent_id in execution_rejected_intents
                or cancellation.intent_id in portfolio_rejected_intents
                or cancellation.cancelled_at < intent.created_at
                or cancellation.signal_lifecycle_id != intent.signal_lifecycle_id
            ):
                raise ValueError("human paper cancellation does not match its intent")
            active_pending_lifecycles.pop(intent.signal_lifecycle_id, None)
            cancelled_intents.add(cancellation.intent_id)
        elif kind == "OPERATIONS_CANCEL":
            cancellation = _operations_cancellation_from_payload(
                event.get("payload")
            )
            intent = intents.get(cancellation.intent_id)
            if intent is None:
                raise ValueError(
                    "human paper operations cancellation has no preceding intent"
                )
            if (
                intent.status != "PENDING"
                or intent.side != "BUY"
                or cancellation.intent_id in filled_intents
                or cancellation.intent_id in cancelled_intents
                or cancellation.intent_id in execution_rejected_intents
                or cancellation.intent_id in portfolio_rejected_intents
                or cancellation.cancelled_at < intent.created_at
                or cancellation.symbol != intent.symbol
                or cancellation.candidate_id != intent.candidate_id
                or cancellation.signal_lifecycle_id
                != intent.signal_lifecycle_id
            ):
                raise ValueError(
                    "human paper operations cancellation does not match its intent"
                )
            lifecycle = intent.signal_lifecycle_id
            active_pending_lifecycles.pop(lifecycle, None)
            if lifecycle in consumed_signal_lifecycles:
                raise ValueError(
                    "human paper signal lifecycle has multiple terminal outcomes"
                )
            consumed_signal_lifecycles.add(lifecycle)
            cancelled_intents.add(cancellation.intent_id)
        elif kind == "EXECUTION_REJECT":
            rejection = _execution_rejection_from_payload(event.get("payload"))
            intent = intents.get(rejection.intent_id)
            if intent is None:
                raise ValueError(
                    "human paper execution rejection has no preceding intent"
                )
            if (
                intent.status != "PENDING"
                or intent.side != "BUY"
                or rejection.intent_id in filled_intents
                or rejection.intent_id in cancelled_intents
                or rejection.intent_id in execution_rejected_intents
                or rejection.intent_id in portfolio_rejected_intents
                or rejection.symbol != intent.symbol
                or intent.entry_price_cap != rejection.entry_price_cap
                or intent.entry_valid_until != rejection.entry_valid_until
                or (
                    rejection.reason_code
                    == "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR"
                    and rejection.candidate_bar_opened_at
                    < intent.earliest_fill_at
                )
            ):
                raise ValueError(
                    "human paper execution rejection does not match its intent"
                )
            active_pending_lifecycles.pop(intent.signal_lifecycle_id, None)
            if intent.signal_lifecycle_id in consumed_signal_lifecycles:
                raise ValueError(
                    "human paper signal lifecycle has multiple terminal outcomes"
                )
            consumed_signal_lifecycles.add(intent.signal_lifecycle_id)
            execution_rejected_intents.add(rejection.intent_id)
        elif kind == "PORTFOLIO_REJECT":
            rejection = _portfolio_rejection_from_payload(event.get("payload"))
            intent = intents.get(rejection.intent_id)
            if intent is None:
                raise ValueError("human paper portfolio rejection has no preceding intent")
            if (
                intent.status != "PENDING"
                or intent.side != "BUY"
                or rejection.intent_id in filled_intents
                or rejection.intent_id in cancelled_intents
                or rejection.intent_id in execution_rejected_intents
                or rejection.intent_id in portfolio_rejected_intents
                or rejection.symbol != intent.symbol
                or rejection.quantity != intent.quantity
                or rejection.candidate_bar_opened_at < intent.earliest_fill_at
                or intent.entry_price_cap is None
                or intent.entry_valid_until is None
                or rejection.candidate_price > intent.entry_price_cap
                or rejection.candidate_bar_closed_at > intent.entry_valid_until
            ):
                raise ValueError(
                    "human paper portfolio rejection does not match its intent"
                )
            active_pending_lifecycles.pop(intent.signal_lifecycle_id, None)
            if intent.signal_lifecycle_id in consumed_signal_lifecycles:
                raise ValueError(
                    "human paper signal lifecycle has multiple terminal outcomes"
                )
            consumed_signal_lifecycles.add(intent.signal_lifecycle_id)
            portfolio_rejected_intents.add(rejection.intent_id)
        else:
            raise ValueError(f"human paper event kind invalid at {index}")
        previous = event_id
        seen.add(event_id)
    return payload


def _validate_operations_cancellation_against_events(
    cancellation: HumanPaperOperationsCancellation,
    events: Sequence[Mapping[str, object]],
) -> None:
    intent_payload = next(
        (
            event["payload"]
            for event in events
            if event.get("kind") == "INTENT"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("intent_id") == cancellation.intent_id
        ),
        None,
    )
    if not isinstance(intent_payload, Mapping):
        raise ValueError(
            "human paper operations cancellation has no preceding intent"
        )
    intent = _intent_from_payload(intent_payload)
    if (
        cancellation.intent_id in human_paper_terminal_intent_ids(events)
        or intent.status != "PENDING"
        or intent.side != "BUY"
        or cancellation.cancelled_at < intent.created_at
        or cancellation.symbol != intent.symbol
        or cancellation.candidate_id != intent.candidate_id
        or cancellation.signal_lifecycle_id != intent.signal_lifecycle_id
    ):
        raise ValueError(
            "human paper operations cancellation does not match its intent"
        )


def _append_event_unlocked(
    path: Path,
    *,
    kind: str,
    payload: Mapping[str, object],
    identity_field: str,
    identity: str,
) -> tuple[dict[str, object], dict[str, object]]:
    ledger = load_human_paper_ledger(path)
    events = [dict(value) for value in ledger["events"]]
    existing = next(
        (
            value
            for value in events
            if value.get("kind") == kind
            and isinstance(value.get("payload"), Mapping)
            and value["payload"].get(identity_field) == identity
        ),
        None,
    )
    if existing is not None:
        return ledger, existing
    if kind == "INTENT":
        candidate = _intent_from_payload(payload)
        lifecycle = candidate.signal_lifecycle_id
        if candidate.status == "PENDING":
            if lifecycle in human_paper_consumed_signal_lifecycle_ids(events):
                raise ValueError(
                    "human paper pending intent reused a consumed signal lifecycle"
                )
            terminal_ids = human_paper_terminal_intent_ids(events)
            active_same_lifecycle = tuple(
                str(value["payload"]["intent_id"])
                for value in events
                if value.get("kind") == "INTENT"
                and isinstance(value.get("payload"), Mapping)
                and value["payload"].get("status") == "PENDING"
                and value["payload"].get("signal_lifecycle_id") == lifecycle
                and str(value["payload"].get("intent_id")) not in terminal_ids
            )
            if active_same_lifecycle:
                raise ValueError(
                    "human paper signal lifecycle has duplicate pending intents"
                )
    elif kind == "OPERATIONS_CANCEL":
        cancellation = _operations_cancellation_from_payload(payload)
        _validate_operations_cancellation_against_events(
            cancellation,
            events,
        )
    elif kind == "EXECUTION_REJECT":
        rejection = _execution_rejection_from_payload(payload)
        terminal_ids = human_paper_terminal_intent_ids(events)
        intent_payload = next(
            (
                value["payload"]
                for value in events
                if value.get("kind") == "INTENT"
                and isinstance(value.get("payload"), Mapping)
                and value["payload"].get("intent_id") == rejection.intent_id
            ),
            None,
        )
        if not isinstance(intent_payload, Mapping):
            raise ValueError(
                "human paper execution rejection has no preceding intent"
            )
        intent = _intent_from_payload(intent_payload)
        if (
            rejection.intent_id in terminal_ids
            or intent.status != "PENDING"
            or intent.side != "BUY"
            or rejection.symbol != intent.symbol
            or rejection.entry_price_cap != intent.entry_price_cap
            or rejection.entry_valid_until != intent.entry_valid_until
            or (
                rejection.reason_code
                == "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR"
                and rejection.candidate_bar_opened_at
                < intent.earliest_fill_at
            )
        ):
            raise ValueError(
                "human paper execution rejection does not match its intent"
            )
    stable_event: dict[str, object] = {
        "kind": kind,
        "payload": _jsonable(payload),
        "previous_event_id": None if not events else events[-1]["event_id"],
    }
    event = {**stable_event, "event_id": sha256_json(stable_event)}
    events.append(event)
    document = _document(events)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return document, event


def _append_event(
    path: Path,
    *,
    kind: str,
    payload: Mapping[str, object],
    identity_field: str,
    identity: str,
) -> tuple[dict[str, object], dict[str, object]]:
    with interprocess_file_lock(path.with_suffix(path.suffix + ".lock")):
        return _append_event_unlocked(
            path,
            kind=kind,
            payload=payload,
            identity_field=identity_field,
            identity=identity,
        )


def _earliest_causal_one_minute_bar_close(observed_at: datetime) -> datetime:
    """Close time of the first exchange-aligned 1m bar not already in flight.

    Without tick data an intent created inside an open minute cannot use that
    minute's opening price.  It can first observe the next whole minute bar;
    an intent created exactly on the minute may use the bar opening then.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    opened_at = observed.replace(second=0, microsecond=0)
    if opened_at < observed:
        opened_at += timedelta(minutes=1)
    return opened_at + timedelta(minutes=1)


def _buy_ohlcv_terminal_execution_action(
    *,
    raw_high: Decimal,
    raw_low: Decimal,
    raw_volume: Decimal,
    limit_price: Decimal,
    quantity: int,
) -> Literal["FILL", "PRICE_CAP_REJECT"] | None:
    """Return whether one completed bar proves a fill or a no-chase reject.

    A bar wholly above the cap proves that the optional order crossed its
    adverse boundary.  A fill needs the stricter shared bar-proxy rule: the
    complete range must be below the cap and five-percent capacity must cover
    the fixed lot.  Touch and mixed bars remain unobservable without prints.
    """

    assessment = assess_strict_limit_bar(
        side="buy",
        limit_price=limit_price,
        raw_high=raw_high,
        raw_low=raw_low,
    )
    # No-chase is a price-path verdict, not a liquidity inference.  Once a
    # completed eligible bar is wholly above the attested cap, even zero
    # reported volume proves that this optional order must not remain alive
    # waiting for a later cheaper bar.  Capacity is required only to infer a
    # fill from OHLCV.
    if raw_low > limit_price:
        return "PRICE_CAP_REJECT"
    if (
        assessment.whole_bar_crossed
        and strict_bar_volume_capacity(
            raw_volume,
            quantity_increment=100,
        )
        >= quantity
    ):
        return "FILL"
    return None


def _buy_bar_has_terminal_execution_outcome(
    bar: HumanPaperMinuteBar,
    *,
    limit_price: Decimal,
    quantity: int,
) -> bool:
    return (
        _buy_ohlcv_terminal_execution_action(
            raw_high=bar.high,
            raw_low=bar.low,
            raw_volume=bar.volume,
            limit_price=limit_price,
            quantity=quantity,
        )
        is not None
    )


def build_human_paper_intent(
    *,
    feedback: HumanReviewFeedback,
    alert: HumanReviewAlert,
    virtual_position_quantity: int = 0,
    reserved_virtual_sell_quantity: int = 0,
    signal_lifecycle_terminal: bool = False,
    entry_selection_evidence: HumanPaperEntrySelectionEvidence | None = None,
) -> HumanPaperIntent | None:
    if (
        virtual_position_quantity < 0
        or virtual_position_quantity % 100
        or reserved_virtual_sell_quantity < 0
        or reserved_virtual_sell_quantity % 100
        or reserved_virtual_sell_quantity > virtual_position_quantity
    ):
        raise ValueError("virtual position quantity must use A-share lots")
    if feedback.disposition != "PAPER_OBSERVE":
        return None
    validate_human_review_feedback_causality(
        feedback,
        alert,
        source_screen_content_sha256=feedback.source_screen_content_sha256,
    )
    is_buy = feedback.point_judgement.startswith("BUY_")
    is_sell = feedback.point_judgement.startswith("SELL_")
    if not (is_buy or is_sell):
        return None
    ranking = alert.sector_ranking_evidence
    exact_sector_selection_required = (
        is_buy
        and alert.alert_type == "POSSIBLE_30M_BUY"
        and ranking is not None
    )
    if exact_sector_selection_required:
        # Keep this check in the shared decision/ledger core as well as the
        # page service.  A direct caller may not bypass the exact QMT catalog
        # gate merely by avoiding the web endpoint.
        if entry_selection_evidence is None:
            return None
        if (
            entry_selection_evidence.feedback_id != feedback.feedback_id
            or entry_selection_evidence.candidate_id != feedback.candidate_id
            or entry_selection_evidence.source_screen_content_sha256
            != feedback.source_screen_content_sha256
            or entry_selection_evidence.symbol != alert.symbol
            or entry_selection_evidence.sector_id != alert.sector_id
            or entry_selection_evidence.sector_id != ranking.sector_id
            or entry_selection_evidence.sector_name != ranking.sector_name
            or entry_selection_evidence.sector_ranking_evidence_id
            != ranking.evidence_id
            or entry_selection_evidence.sector_ranking_observed_at
            != ranking.observed_at
            or entry_selection_evidence.sector_catalog_revision
            != ranking.sector_catalog_revision
            or entry_selection_evidence.attested_at != feedback.reviewed_at
        ):
            raise ValueError(
                "human paper entry selection evidence differs from source"
            )
    elif entry_selection_evidence is not None:
        raise ValueError(
            "human paper entry selection evidence is not applicable"
        )
    role_unclassified_sell = alert.alert_type == "POSSIBLE_SELL_REVIEW"
    expected_levels = (
        frozenset({"30M", "5M"})
        if role_unclassified_sell
        else frozenset(
            {"5M" if alert.alert_type.startswith("POSSIBLE_5M_") else "30M"}
        )
    )
    expected_level_reason = (
        "EXPECTED_REVIEW_LEVEL_30M_OR_5M"
        if role_unclassified_sell
        else f"EXPECTED_REVIEW_LEVEL_{next(iter(expected_levels))}"
    )
    structure_confirmed = (
        feedback.center_judgement == "CONFIRMED"
        and feedback.level_judgement in expected_levels
    )
    side: Literal["BUY", "SELL"] = "SELL" if is_sell else "BUY"
    program_side: Literal["BUY", "SELL"] = (
        "BUY"
        if alert.alert_type
        in {"POSSIBLE_30M_BUY", "POSSIBLE_5M_TACTICAL_BUYBACK"}
        else "SELL"
    )
    tactical_review = alert.alert_type.startswith("POSSIBLE_5M_") or (
        role_unclassified_sell and feedback.level_judgement == "5M"
    )
    if side != program_side:
        # Reviewers may disagree with a coarse program clue and that judgement
        # remains valuable feedback.  It must not, however, turn a sell-source
        # candidate into a virtual buy (or vice versa) without a matching
        # source structure and causal execution boundary.
        status = "OBSERVATION_ONLY"
        reasons = (
            "HUMAN_POINT_SIDE_CONTRADICTS_PROGRAM_CLUE",
            "CONTRADICTORY_REVIEW_CANNOT_CREATE_VIRTUAL_INTENT",
        )
    elif not structure_confirmed:
        status = "OBSERVATION_ONLY"
        reasons = (
            "HUMAN_STRUCTURE_CONFIRMATION_INCOMPLETE",
            expected_level_reason,
        )
    elif feedback.trend_judgement == "UNCERTAIN":
        # The human-assisted contract delegates trend-type recognition to the
        # reviewer.  Center and level alone therefore cannot authorize even a
        # virtual observation fill while the trend type remains unresolved.
        status = "OBSERVATION_ONLY"
        reasons = (
            "HUMAN_TREND_TYPE_CONFIRMATION_INCOMPLETE",
            "HUMAN_CONFIRM_TREND_TYPE_BEFORE_VIRTUAL_INTENT",
        )
    elif signal_lifecycle_terminal:
        # One immutable buy/sell point may have only one terminal paper
        # outcome.  Reusing it after a completed strategic cycle would turn
        # an old structure into a new entry without a new source point.
        status = "OBSERVATION_ONLY"
        reasons = (
            "SIGNAL_LIFECYCLE_ALREADY_CONSUMED",
            "NEW_STRUCTURE_REQUIRED_FOR_NEW_VIRTUAL_CYCLE",
        )
    elif tactical_review:
        # This diagnostic book always trades one A-share lot.  The frozen
        # tactical target is floor_to_lot(Q_CYCLE * 0.25), which is zero for
        # a 100-share cycle.  Letting a 5m signal trade 100 shares would sell
        # the 30m core or increase Q_CYCLE, both explicitly forbidden by the
        # strategy contract.  Keep the human judgement, but do not create an
        # executable paper intent until a real batch ledger exists.
        status = "OBSERVATION_ONLY"
        reasons = (
            "FIXED_ONE_LOT_TACTICAL_TARGET_BELOW_TRADING_UNIT",
            "TACTICAL_REVIEW_OBSERVATION_ONLY",
        )
    elif is_buy and MONITOR_ONLY_WARNING_CODE in alert.warning_codes:
        # Watchlists, previous signals and virtual holdings remain in the same
        # human-review queue so an operator can inspect them and preserve exit
        # continuity. They are not a current QMT sector trigger, however, and
        # human feedback must not turn that monitoring supplement into a new
        # strategic entry. The warning is part of the alert candidate hash and
        # is derived from the validated live selection scope.
        status = "OBSERVATION_ONLY"
        reasons = (
            "BUY_NOT_TRIGGERED_BY_CURRENT_QMT_SECTOR",
            "MONITOR_ONLY_NEW_ENTRY_PROHIBITED",
        )
    elif is_buy and virtual_position_quantity >= 100:
        status = "OBSERVATION_ONLY"
        reasons = (
            "VIRTUAL_STRATEGIC_CYCLE_ALREADY_OPEN",
            "ONE_SECURITY_ONE_STRATEGIC_SLOT",
        )
    elif is_buy and (
        alert.market_risk_gate != "GREEN"
        or alert.sector_risk_gate != "GREEN"
        or alert.symbol_risk_gate != "GREEN"
    ):
        status = "BLOCKED_BY_RISK_GATE"
        reasons = (
            "HIGHER_TIMEFRAME_GATE_NOT_GREEN",
            f"MARKET_GATE_{alert.market_risk_gate}",
            f"SECTOR_GATE_{alert.sector_risk_gate}",
            f"SYMBOL_GATE_{alert.symbol_risk_gate}",
        )
    elif is_buy and any(
        value in alert.warning_codes
        for value in (
            "WARMUP_NOT_CONVERGED",
            "WARMUP_CONVERGENCE_GATE_FAILED",
        )
    ):
        # Warmup is a data-sufficiency boundary rather than an interpretive
        # Chanlun judgement.  A reviewer may classify the clue, but cannot
        # make an unstable prefix causally suitable for a new virtual entry.
        # Existing-position exits deliberately bypass this branch.
        status = "OBSERVATION_ONLY"
        reasons = (
            "WARMUP_CONVERGENCE_REQUIRED_FOR_VIRTUAL_ENTRY",
            "WARMUP_DIVERGENCE_IS_NOT_HUMAN_OVERRIDABLE",
        )
    elif is_buy and any(
        value is None
        for value in (
            alert.entry_confirmation_bar_closed_at,
            alert.entry_price_cap,
            alert.entry_valid_until,
            alert.entry_boundary_evidence_id,
        )
    ):
        status = "OBSERVATION_ONLY"
        reasons = (
            "BUY_EXECUTION_BOUNDARY_EVIDENCE_MISSING",
            "STRUCTURE_ANCHOR_IS_NOT_A_BUY_PRICE_CAP",
        )
    elif (
        is_buy
        and alert.entry_valid_until is not None
        and feedback.reviewed_at >= alert.entry_valid_until
    ):
        status = "OBSERVATION_ONLY"
        reasons = (
            "BUY_ENTRY_TTL_EXPIRED_BEFORE_HUMAN_CONFIRMATION",
            "NEW_STRUCTURE_REQUIRED_NO_PRICE_CHASING",
        )
    elif (
        is_buy
        and alert.entry_valid_until is not None
        and _earliest_causal_one_minute_bar_close(feedback.reviewed_at)
        > alert.entry_valid_until
    ):
        # A review made after the current minute opened cannot causally claim
        # that bar's open.  If the next whole 1m bar would close after the
        # frozen locator TTL, the optional entry is impossible at creation
        # time and must not linger as a misleading PENDING intent.
        status = "OBSERVATION_ONLY"
        reasons = (
            "NO_CAUSAL_1M_EXECUTION_BAR_REMAINS_BEFORE_TTL",
            "NEW_STRUCTURE_REQUIRED_NO_PRICE_CHASING",
        )
    elif (
        is_sell
        and virtual_position_quantity - reserved_virtual_sell_quantity < 100
    ):
        status = "OBSERVATION_ONLY"
        reasons = ("SELL_REVIEW_HAS_NO_VIRTUAL_POSITION",)
    else:
        status = "PENDING"
        reasons = (
            "HUMAN_CONFIRMED_VIRTUAL_EXIT"
            if is_sell
            else "HUMAN_CONFIRMED_PAPER_OBSERVE",
        )
    return HumanPaperIntent(
        feedback_id=feedback.feedback_id,
        candidate_id=feedback.candidate_id,
        source_screen_content_sha256=feedback.source_screen_content_sha256,
        symbol=alert.symbol,
        side=side,
        created_at=feedback.reviewed_at,
        earliest_fill_at=feedback.reviewed_at,
        quantity=100,
        reference_price=alert.reference_price,
        structural_invalidation_price=alert.structural_invalidation_price,
        market_risk_gate=alert.market_risk_gate,
        sector_risk_gate=alert.sector_risk_gate,
        symbol_risk_gate=alert.symbol_risk_gate,
        status=status,
        reason_codes=reasons,
        signal_lifecycle_id=feedback.signal_lifecycle_id,
        entry_confirmation_bar_closed_at=(
            alert.entry_confirmation_bar_closed_at if is_buy else None
        ),
        entry_price_cap=alert.entry_price_cap if is_buy else None,
        entry_valid_until=alert.entry_valid_until if is_buy else None,
        entry_boundary_evidence_id=(
            alert.entry_boundary_evidence_id if is_buy else None
        ),
        entry_execution_boundary=(
            alert.entry_execution_boundary if is_buy else None
        ),
        entry_selection_evidence=(
            entry_selection_evidence if is_buy else None
        ),
    )


def append_human_paper_intent(
    path: Path,
    intent: HumanPaperIntent,
) -> tuple[dict[str, object], dict[str, object]]:
    payload = intent.document()
    return _append_event(
        path,
        kind="INTENT",
        payload=payload,
        identity_field="intent_id",
        identity=intent.intent_id,
    )


def human_paper_cancelled_intent_ids(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    return frozenset(
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind") in {"CANCEL", "OPERATIONS_CANCEL"}
        and isinstance(event.get("payload"), Mapping)
    )


def human_paper_execution_rejected_intent_ids(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    return frozenset(
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind") == "EXECUTION_REJECT"
        and isinstance(event.get("payload"), Mapping)
    )


def human_paper_portfolio_rejected_intent_ids(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    return frozenset(
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind") == "PORTFOLIO_REJECT"
        and isinstance(event.get("payload"), Mapping)
    )


def human_paper_terminal_intent_ids(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    filled = {
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind") == "FILL"
        and isinstance(event.get("payload"), Mapping)
    }
    return frozenset(
        filled
        | set(human_paper_cancelled_intent_ids(events))
        | set(human_paper_execution_rejected_intent_ids(events))
        | set(human_paper_portfolio_rejected_intent_ids(events))
    )


def human_paper_consumed_signal_lifecycle_ids(
    events: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    """Return source-point lifecycles with a terminal execution decision.

    Human supersession (``CANCEL``) deliberately does not consume a lifecycle:
    the latest judgement may replace an older still-pending intent.  A fill,
    pre-trade rejection, or execution-gate operations cancellation does consume
    it, because retrying the same immutable point after data recovery or a
    security reopening would be an unproved new strategic cycle or a non-causal
    historical back-fill.
    """

    intents = {
        str(event["payload"]["intent_id"]): event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    }
    terminal_ids = {
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind")
        in {
            "FILL",
            "OPERATIONS_CANCEL",
            "EXECUTION_REJECT",
            "PORTFOLIO_REJECT",
        }
        and isinstance(event.get("payload"), Mapping)
    }
    return frozenset(
        str(lifecycle)
        for intent_id in terminal_ids
        if (intent := intents.get(intent_id)) is not None
        for lifecycle in (intent["signal_lifecycle_id"],)
    )


def reconcile_human_paper_feedback(
    path: Path,
    *,
    feedback: HumanReviewFeedback,
    alert: HumanReviewAlert,
    entry_selection_evidence: HumanPaperEntrySelectionEvidence | None = None,
) -> tuple[
    dict[str, object],
    dict[str, object] | None,
    tuple[dict[str, object], ...],
    bool,
]:
    """Make the latest human judgement authoritative without mutating history.

    A newer feedback entry cancels every older, unfilled pending intent for the
    same signal lifecycle before an optional replacement intent is appended.
    The paper ledger lock covers cancellation, position/reservation rebuild and
    replacement creation as one cross-process operation.
    """

    validate_human_review_feedback_causality(
        feedback,
        alert,
        source_screen_content_sha256=feedback.source_screen_content_sha256,
    )
    with interprocess_file_lock(path.with_suffix(path.suffix + ".lock")):
        document = load_human_paper_ledger(path)
        events = tuple(document["events"])
        filled_ids = {
            str(event["payload"]["intent_id"])
            for event in events
            if event.get("kind") == "FILL"
            and isinstance(event.get("payload"), Mapping)
        }
        cancelled_ids = set(human_paper_cancelled_intent_ids(events))
        terminal_ids = human_paper_terminal_intent_ids(events)
        cancellation_events: list[dict[str, object]] = []
        for event in events:
            payload = event.get("payload")
            if event.get("kind") != "INTENT" or not isinstance(payload, Mapping):
                continue
            intent_id = str(payload.get("intent_id") or "")
            if (
                payload.get("status") != "PENDING"
                or intent_id in filled_ids
                or intent_id in cancelled_ids
                or intent_id in terminal_ids
                or payload.get("signal_lifecycle_id") != alert.signal_lifecycle_id
                or payload.get("feedback_id") == feedback.feedback_id
            ):
                continue
            created_at = normalize_datetime(
                datetime.fromisoformat(str(payload["created_at"])),
                "created_at",
            )
            if feedback.reviewed_at < created_at:
                raise ValueError(
                    "superseding human feedback predates a pending virtual intent"
                )
            cancellation = HumanPaperCancellation(
                intent_id=intent_id,
                superseding_feedback_id=feedback.feedback_id,
                candidate_id=feedback.candidate_id,
                signal_lifecycle_id=alert.signal_lifecycle_id,
                cancelled_at=feedback.reviewed_at,
                reason_code="SUPERSEDED_BY_LATER_HUMAN_FEEDBACK",
            )
            document, cancellation_event = _append_event_unlocked(
                path,
                kind="CANCEL",
                payload={
                    **_jsonable(asdict(cancellation)),
                    "cancellation_id": cancellation.cancellation_id,
                },
                identity_field="cancellation_id",
                identity=cancellation.cancellation_id,
            )
            cancellation_events.append(cancellation_event)
            cancelled_ids.add(intent_id)

        current_events = tuple(document["events"])
        virtual_positions = human_paper_position_quantities(current_events)
        reserved_sells = human_paper_pending_sell_quantities(current_events)
        virtual_position_quantity = virtual_positions.get(alert.symbol, 0)
        reserved_virtual_sell_quantity = reserved_sells.get(alert.symbol, 0)
        paper_intent = build_human_paper_intent(
            feedback=feedback,
            alert=alert,
            virtual_position_quantity=virtual_position_quantity,
            reserved_virtual_sell_quantity=reserved_virtual_sell_quantity,
            signal_lifecycle_terminal=(
                alert.signal_lifecycle_id
                in human_paper_consumed_signal_lifecycle_ids(current_events)
            ),
            entry_selection_evidence=entry_selection_evidence,
        )
        intent_event = None
        if paper_intent is not None:
            before_count = len(document["events"])
            document, intent_event = _append_event_unlocked(
                path,
                kind="INTENT",
                payload={
                    **paper_intent.document(),
                },
                identity_field="intent_id",
                identity=paper_intent.intent_id,
            )
            changed = bool(cancellation_events) or len(document["events"]) > before_count
        else:
            changed = bool(cancellation_events)
        return document, intent_event, tuple(cancellation_events), changed


def human_paper_position_quantities(
    events: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Rebuild virtual positions from immutable fills; reject an oversell."""

    positions: dict[str, int] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "FILL" or not isinstance(payload, Mapping):
            continue
        symbol = str(payload["symbol"])
        quantity = int(payload["quantity"])
        if quantity <= 0 or quantity % 100:
            raise ValueError("human paper fill quantity is invalid")
        side = payload.get("side")
        current = positions.get(symbol, 0)
        if side == "BUY":
            positions[symbol] = current + quantity
        elif side == "SELL":
            if quantity > current:
                raise ValueError("human paper ledger contains a virtual oversell")
            positions[symbol] = current - quantity
        else:
            raise ValueError("human paper fill side is invalid")
    return {symbol: quantity for symbol, quantity in positions.items() if quantity}


def human_paper_pending_sell_quantities(
    events: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Reserve virtual positions already promised to unfilled sell intents."""

    terminal_intents = human_paper_terminal_intent_ids(events)
    reserved: dict[str, int] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "INTENT" or not isinstance(payload, Mapping):
            continue
        if (
            payload.get("side") != "SELL"
            or payload.get("status") != "PENDING"
            or str(payload.get("intent_id")) in terminal_intents
        ):
            continue
        symbol = str(payload["symbol"])
        quantity = int(payload["quantity"])
        if quantity <= 0 or quantity % 100:
            raise ValueError("pending virtual sell quantity is invalid")
        reserved[symbol] = reserved.get(symbol, 0) + quantity
    return reserved


def _virtual_lots(
    events: Sequence[Mapping[str, object]],
) -> dict[str, list[list[object]]]:
    """Return FIFO [acquired_session, remaining_quantity] virtual lots."""

    lots: dict[str, list[list[object]]] = {}
    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "FILL" or not isinstance(payload, Mapping):
            continue
        symbol = str(payload["symbol"])
        quantity = int(payload["quantity"])
        if payload.get("side") == "BUY":
            acquired = datetime.fromisoformat(str(payload["filled_at"])).date()
            lots.setdefault(symbol, []).append([acquired, quantity])
            continue
        if payload.get("side") != "SELL":
            raise ValueError("human paper fill side is invalid")
        remaining = quantity
        for lot in lots.get(symbol, []):
            take = min(int(lot[1]), remaining)
            lot[1] = int(lot[1]) - take
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise ValueError("human paper ledger contains a virtual oversell")
    return lots


def human_paper_oldest_open_lot_sessions(
    events: Sequence[Mapping[str, object]],
) -> dict[str, date]:
    """Return the acquisition session of each symbol's oldest remaining lot.

    This deliberately follows FIFO depletion instead of the first BUY ever
    seen in the ledger.  After a strategic cycle is fully closed and a later
    cycle opens, company actions before the new acquisition must not be
    misclassified as unresolved events of the current position.
    """

    result: dict[str, date] = {}
    for symbol, lots in _virtual_lots(events).items():
        remaining_sessions = tuple(
            acquired
            for acquired, quantity in lots
            if int(quantity) > 0 and isinstance(acquired, date)
        )
        if remaining_sessions:
            result[symbol] = min(remaining_sessions)
    return result


def _sellable_quantity(lots: Sequence[Sequence[object]], at: datetime) -> int:
    return sum(
        int(lot[1])
        for lot in lots
        if lot[0] < at.date()
    )


def _consume_sellable_lots(
    lots: list[list[object]],
    *,
    at: datetime,
    quantity: int,
) -> None:
    remaining = quantity
    for lot in lots:
        if lot[0] >= at.date():
            continue
        take = min(int(lot[1]), remaining)
        lot[1] = int(lot[1]) - take
        remaining -= take
        if not remaining:
            return
    raise ValueError("virtual sell fill exceeds T+1 sellable quantity")


def _settle_human_paper_intents_unlocked(
    path: Path,
    *,
    bars_by_symbol: Mapping[str, Sequence[HumanPaperMinuteBar]],
    accounting_parameters: HumanPaperAccountingParameters,
    operations_cancellations: Sequence[
        HumanPaperOperationsCancellation
    ] = (),
    entry_provenance_blocked_intent_ids: Sequence[str] = (),
    causal_gap_blocked_intent_ids: Sequence[str] = (),
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    ledger = load_human_paper_ledger(path)
    events = tuple(ledger["events"])
    if any(
        not isinstance(value, HumanPaperOperationsCancellation)
        for value in operations_cancellations
    ):
        raise TypeError("operations cancellations must use the frozen model")
    operation_cancel_by_intent = {
        value.intent_id: value for value in operations_cancellations
    }
    if len(operation_cancel_by_intent) != len(operations_cancellations):
        raise ValueError("operations cancellation intent is duplicated")
    for cancellation in operations_cancellations:
        _validate_operations_cancellation_against_events(
            cancellation,
            events,
        )
    filled_intents = {
        str(event["payload"]["intent_id"])
        for event in events
        if event.get("kind") == "FILL" and isinstance(event.get("payload"), Mapping)
    }
    terminal_intents = set(human_paper_terminal_intent_ids(events))
    intents = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "INTENT" and isinstance(event.get("payload"), Mapping)
    )
    blocked_ids = tuple(entry_provenance_blocked_intent_ids)
    if (
        len(blocked_ids) != len(set(blocked_ids))
        or any(_SHA256.fullmatch(str(value)) is None for value in blocked_ids)
    ):
        raise ValueError("entry provenance blocked intent identities are invalid")
    blocked_id_set = frozenset(blocked_ids)
    causal_blocked_ids = tuple(causal_gap_blocked_intent_ids)
    if (
        len(causal_blocked_ids) != len(set(causal_blocked_ids))
        or any(
            _SHA256.fullmatch(str(value)) is None
            for value in causal_blocked_ids
        )
    ):
        raise ValueError("causal gap blocked intent identities are invalid")
    causal_blocked_id_set = frozenset(causal_blocked_ids)
    intent_by_id = {str(value["intent_id"]): value for value in intents}
    if any(
        intent_id not in intent_by_id
        or intent_id in terminal_intents
        or intent_by_id[intent_id].get("status") != "PENDING"
        or intent_by_id[intent_id].get("side") != "BUY"
        for intent_id in blocked_id_set
    ):
        raise ValueError(
            "entry provenance may block only current pending BUY intents"
        )
    if any(
        intent_id not in intent_by_id
        or intent_id in terminal_intents
        or intent_by_id[intent_id].get("status") != "PENDING"
        for intent_id in causal_blocked_id_set
    ):
        raise ValueError("causal gap may block only current pending intents")
    settlement_blocked_id_set = blocked_id_set | causal_blocked_id_set
    if settlement_blocked_id_set & set(operation_cancel_by_intent):
        raise ValueError(
            "a settlement block cannot also cancel the same intent"
        )
    lots_by_symbol = _virtual_lots(events)
    document = ledger
    capital_evaluations: list[dict[str, object]] = []
    portfolio_mark_blocked_intents: set[str] = set()
    while True:
        choices: list[
            tuple[
                datetime,
                datetime,
                datetime,
                int,
                str,
                str,
                Mapping[str, object],
                HumanPaperMinuteBar,
            ]
        ] = []
        for intent_index, intent in enumerate(intents):
            intent_id = str(intent["intent_id"])
            if (
                intent_id in terminal_intents
                or intent_id in operation_cancel_by_intent
                or intent_id in settlement_blocked_id_set
                or intent_id in portfolio_mark_blocked_intents
                or intent.get("status") != "PENDING"
            ):
                continue
            earliest = normalize_datetime(
                datetime.fromisoformat(str(intent["earliest_fill_at"])),
                "earliest_fill_at",
            )
            quantity = int(intent["quantity"])
            side = str(intent["side"])
            if side not in {"BUY", "SELL"}:
                raise ValueError("human paper intent side is invalid")
            entry_valid_until = (
                None
                if intent.get("entry_valid_until") is None
                else normalize_datetime(
                    datetime.fromisoformat(str(intent["entry_valid_until"])),
                    "entry_valid_until",
                )
            )
            entry_price_cap = (
                None
                if intent.get("entry_price_cap") is None
                else Decimal(str(intent["entry_price_cap"]))
            )
            if side == "BUY" and (
                entry_valid_until is None or entry_price_cap is None
            ):
                raise ValueError("pending BUY intent lacks its execution boundary")
            candidates = tuple(
                sorted(
                    (
                        bar
                        for bar in bars_by_symbol.get(str(intent["symbol"]), ())
                        if bar.complete
                        and bar.opened_at >= earliest
                        and (
                            side == "SELL"
                            or (
                                entry_valid_until is not None
                                and bar.closed_at <= entry_valid_until
                            )
                        )
                        and bar.security_status_complete
                        and bar.corporate_action_state_complete
                        and (
                            bar.buy_eligible
                            if side == "BUY"
                            else bar.sell_eligible
                        )
                        and not bar.suspended
                        and not (
                            bar.limit_up_locked
                            if side == "BUY"
                            else bar.limit_down_locked
                        )
                        and (
                            (
                                side == "BUY"
                                and entry_price_cap is not None
                                and _buy_bar_has_terminal_execution_outcome(
                                    bar,
                                    limit_price=entry_price_cap,
                                    quantity=quantity,
                                )
                            )
                            or (
                                side == "SELL"
                                and bar.volume >= quantity
                                and strict_bar_volume_capacity(
                                    bar.volume,
                                    quantity_increment=100,
                                )
                                >= quantity
                                and _sellable_quantity(
                                    lots_by_symbol.get(
                                        str(intent["symbol"]), ()
                                    ),
                                    bar.opened_at,
                                )
                                >= quantity
                            )
                        )
                    ),
                    key=lambda value: value.opened_at,
                )
            )
            if not candidates:
                if side == "BUY" and entry_valid_until is not None:
                    expiry_evidence = tuple(
                        sorted(
                            (
                                bar
                                for bar in bars_by_symbol.get(
                                    str(intent["symbol"]), ()
                                )
                                if bar.complete
                                and bar.closed_at >= entry_valid_until
                                and bar.security_status_complete
                                and bar.corporate_action_state_complete
                            ),
                            key=lambda value: (value.closed_at, value.opened_at),
                        )
                    )
                    if expiry_evidence:
                        bar = expiry_evidence[0]
                        created_at = normalize_datetime(
                            datetime.fromisoformat(str(intent["created_at"])),
                            "created_at",
                        )
                        choices.append(
                            (
                                bar.closed_at,
                                bar.closed_at,
                                created_at,
                                intent_index,
                                intent_id,
                                "TTL_REJECT",
                                intent,
                                bar,
                            )
                        )
                continue
            bar = candidates[0]
            created_at = normalize_datetime(
                datetime.fromisoformat(str(intent["created_at"])),
                "created_at",
            )
            choices.append(
                (
                    bar.closed_at,
                    bar.opened_at,
                    created_at,
                    intent_index,
                    intent_id,
                    "FILL_OR_CAP_REJECT",
                    intent,
                    bar,
                )
            )
        if not choices:
            break
        *_order, action, intent, bar = min(
            choices,
            key=lambda value: value[:5],
        )
        intent_id = str(intent["intent_id"])
        quantity = int(intent["quantity"])
        side = str(intent["side"])
        execution_price = adverse_observed_bar_price(
            side="buy" if side == "BUY" else "sell",
            raw_high=bar.high,
            raw_low=bar.low,
        )
        if side == "BUY":
            entry_price_cap = Decimal(str(intent["entry_price_cap"]))
            entry_valid_until = normalize_datetime(
                datetime.fromisoformat(str(intent["entry_valid_until"])),
                "entry_valid_until",
            )
            if action == "TTL_REJECT" or bar.low > entry_price_cap:
                reason_code = (
                    "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL"
                    if action == "TTL_REJECT"
                    else "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR"
                )
                rejection = HumanPaperExecutionRejection(
                    intent_id=intent_id,
                    symbol=str(intent["symbol"]),
                    candidate_bar_opened_at=bar.opened_at,
                    candidate_bar_closed_at=bar.closed_at,
                    candidate_price=(
                        None if action == "TTL_REJECT" else execution_price
                    ),
                    entry_price_cap=entry_price_cap,
                    entry_valid_until=entry_valid_until,
                    execution_snapshot_sha256=str(
                        bar.execution_snapshot_sha256
                    ),
                    reason_code=reason_code,
                    rejected_at=bar.closed_at,
                )
                document, _event = _append_event_unlocked(
                    path,
                    kind="EXECUTION_REJECT",
                    payload={
                        **_jsonable(asdict(rejection)),
                        "rejection_id": rejection.rejection_id,
                    },
                    identity_field="rejection_id",
                    identity=rejection.rejection_id,
                )
                terminal_intents.add(intent_id)
                capital_evaluations.append(
                    {
                        "result": "EXECUTION_REJECTED",
                        "intent_id": intent_id,
                        "rejection_id": rejection.rejection_id,
                        "reason_codes": [reason_code],
                        "candidate_bar_opened_at": bar.opened_at.isoformat(),
                        "candidate_bar_closed_at": bar.closed_at.isoformat(),
                        "execution_snapshot_sha256": (
                            bar.execution_snapshot_sha256
                        ),
                        "tick_data_used": False,
                        "broker_transport_available": False,
                        "live_status": "LIVE_DISABLED",
                    }
                )
                continue
        decision: dict[str, object] | None = None
        if side == "BUY":
            position_quantities = human_paper_position_quantities(
                tuple(document["events"])
            )
            position_marks: dict[str, Decimal] = {}
            unresolved_marks: list[dict[str, str]] = []
            for position_symbol in sorted(position_quantities):
                matches = tuple(
                    value
                    for value in bars_by_symbol.get(position_symbol, ())
                    if value.opened_at == bar.opened_at
                    and value.closed_at == bar.closed_at
                )
                if len(matches) != 1:
                    unresolved_marks.append(
                        {
                            "symbol": position_symbol,
                            "reason": "EXACT_SYNCHRONOUS_1M_BAR_NOT_UNIQUE",
                        }
                    )
                    continue
                mark_bar = matches[0]
                if (
                    not mark_bar.complete
                    or mark_bar.suspended
                    or not mark_bar.security_status_complete
                    or not mark_bar.corporate_action_state_complete
                    or mark_bar.execution_snapshot_sha256
                    != bar.execution_snapshot_sha256
                ):
                    unresolved_marks.append(
                        {
                            "symbol": position_symbol,
                            "reason": "SYNCHRONOUS_1M_MARK_FACTS_INCOMPLETE",
                        }
                    )
                    continue
                position_marks[position_symbol] = mark_bar.close
            if unresolved_marks:
                capital_evaluations.append(
                    {
                        "schema": "chanlun-human-paper-portfolio-mark-resolution",
                        "result": "PORTFOLIO_MARKS_UNRESOLVED",
                        "intent_id": intent_id,
                        "symbol": str(intent["symbol"]),
                        "candidate_bar_opened_at": bar.opened_at.isoformat(),
                        "candidate_bar_closed_at": bar.closed_at.isoformat(),
                        "execution_snapshot_sha256": bar.execution_snapshot_sha256,
                        "open_position_count": len(position_quantities),
                        "resolved_position_mark_count": len(position_marks),
                        "unresolved_position_marks": unresolved_marks,
                        "reason_codes": [
                            "ALL_OPEN_POSITIONS_REQUIRE_EXACT_SYNCHRONOUS_1M_MARKS"
                        ],
                        "optional_buy_deferred_for_unresolved_marks": True,
                        "persistent_exit_processing_continues": True,
                        "slot_fraction_notional_gate_evaluable": False,
                        "account_exposure_notional_gate_evaluable": False,
                        "minimum_market_data_frequency": "1m",
                        "tick_data_used": False,
                        "broker_transport_available": False,
                        "live_status": "LIVE_DISABLED",
                    }
                )
                portfolio_mark_blocked_intents.add(intent_id)
                continue
            decision = assess_human_paper_portfolio_fill(
                tuple(document["events"]),
                parameters=accounting_parameters,
                symbol=str(intent["symbol"]),
                quantity=quantity,
                price=execution_price,
                session=bar.closed_at.date(),
                position_marks=position_marks,
            )
            evaluation = {
                **decision,
                "intent_id": intent_id,
                "candidate_bar_opened_at": bar.opened_at.isoformat(),
                "candidate_bar_closed_at": bar.closed_at.isoformat(),
                "execution_snapshot_sha256": bar.execution_snapshot_sha256,
            }
            if decision["allowed"] is not True:
                rejection = HumanPaperPortfolioRejection(
                    intent_id=intent_id,
                    symbol=str(intent["symbol"]),
                    quantity=quantity,
                    candidate_bar_opened_at=bar.opened_at,
                    candidate_bar_closed_at=bar.closed_at,
                    candidate_price=execution_price,
                    execution_snapshot_sha256=str(bar.execution_snapshot_sha256),
                    portfolio_decision_sha256=str(decision["content_sha256"]),
                    accounting_contract_id=(
                        accounting_parameters.accounting_contract_id
                    ),
                    available_cash=Decimal(str(decision["available_cash"])),
                    current_market_value=Decimal(
                        str(decision["current_market_value"])
                    ),
                    account_equity=Decimal(str(decision["account_equity"])),
                    notional=Decimal(str(decision["notional"])),
                    terminal_buy_fee=Decimal(str(decision["terminal_buy_fee"])),
                    required_cash=Decimal(str(decision["required_cash"])),
                    occupied_slots=int(decision["occupied_slots"]),
                    slot_count=int(decision["slot_count"]),
                    slot_fraction=Decimal(str(decision["slot_fraction"])),
                    slot_notional_cap=Decimal(
                        str(decision["slot_notional_cap"])
                    ),
                    account_exposure_cap=Decimal(
                        str(decision["account_exposure_cap"])
                    ),
                    account_exposure_notional_cap=Decimal(
                        str(decision["account_exposure_notional_cap"])
                    ),
                    post_trade_gross_market_value=Decimal(
                        str(decision["post_trade_gross_market_value"])
                    ),
                    position_marks=tuple(
                        HumanPaperDecisionPositionMark(
                            symbol=str(value["symbol"]),
                            quantity=int(value["quantity"]),
                            price=Decimal(str(value["price"])),
                            market_value=Decimal(str(value["market_value"])),
                        )
                        for value in decision["position_marks"]
                    ),
                    reason_codes=tuple(decision["reason_codes"]),
                    rejected_at=bar.closed_at,
                )
                document, _event = _append_event_unlocked(
                    path,
                    kind="PORTFOLIO_REJECT",
                    payload={
                        **_jsonable(asdict(rejection)),
                        "rejection_id": rejection.rejection_id,
                    },
                    identity_field="rejection_id",
                    identity=rejection.rejection_id,
                )
                terminal_intents.add(intent_id)
                capital_evaluations.append(
                    {
                        **evaluation,
                        "result": "PORTFOLIO_REJECTED",
                        "rejection_id": rejection.rejection_id,
                    }
                )
                continue
        if side == "SELL":
            _consume_sellable_lots(
                lots_by_symbol.setdefault(str(intent["symbol"]), []),
                at=bar.closed_at,
                quantity=quantity,
            )
        if side == "BUY":
            if decision is None or decision.get("allowed") is not True:
                raise ValueError("allowed portfolio fill decision is unavailable")
            fill: HumanPaperFill | HumanPaperPortfolioFill = HumanPaperPortfolioFill(
                    intent_id=intent_id,
                    symbol=str(intent["symbol"]),
                    side="BUY",
                    quantity=quantity,
                    price=execution_price,
                    filled_at=bar.closed_at,
                    source_bar_closed_at=bar.closed_at,
                    execution_snapshot_sha256=str(
                        bar.execution_snapshot_sha256
                    ),
                    portfolio_decision_sha256=str(
                        decision["content_sha256"]
                    ),
                    accounting_contract_id=str(
                        decision["accounting_contract_id"]
                    ),
                    available_cash=Decimal(
                        str(decision["available_cash"])
                    ),
                    current_market_value=Decimal(
                        str(decision["current_market_value"])
                    ),
                    account_equity=Decimal(str(decision["account_equity"])),
                    notional=Decimal(str(decision["notional"])),
                    terminal_buy_fee=Decimal(
                        str(decision["terminal_buy_fee"])
                    ),
                    required_cash=Decimal(str(decision["required_cash"])),
                    occupied_slots=int(decision["occupied_slots"]),
                    slot_count=int(decision["slot_count"]),
                    slot_fraction=Decimal(str(decision["slot_fraction"])),
                    slot_notional_cap=Decimal(
                        str(decision["slot_notional_cap"])
                    ),
                    account_exposure_cap=Decimal(
                        str(decision["account_exposure_cap"])
                    ),
                    account_exposure_notional_cap=Decimal(
                        str(decision["account_exposure_notional_cap"])
                    ),
                    post_trade_gross_market_value=Decimal(
                        str(decision["post_trade_gross_market_value"])
                    ),
                    position_marks=tuple(
                        HumanPaperDecisionPositionMark(
                            symbol=str(value["symbol"]),
                            quantity=int(value["quantity"]),
                            price=Decimal(str(value["price"])),
                            market_value=Decimal(str(value["market_value"])),
                        )
                        for value in decision["position_marks"]
                    ),
                )
        else:
            fill = HumanPaperFill(
                intent_id=intent_id,
                symbol=str(intent["symbol"]),
                side=side,
                quantity=quantity,
                price=execution_price,
                filled_at=bar.closed_at,
                source_bar_closed_at=bar.closed_at,
                execution_snapshot_sha256=str(
                    bar.execution_snapshot_sha256
                ),
            )
        document, _event = _append_event_unlocked(
            path,
            kind="FILL",
            payload={**_jsonable(asdict(fill)), "fill_id": fill.fill_id},
            identity_field="fill_id",
            identity=fill.fill_id,
        )
        filled_intents.add(intent_id)
        terminal_intents.add(intent_id)
        if side == "BUY":
            capital_evaluations.append(
                {
                    **evaluation,
                    "result": "FILL_ALLOWED",
                    "fill_id": fill.fill_id,
                }
            )
        if side == "BUY":
            lots_by_symbol.setdefault(str(intent["symbol"]), []).append(
                [bar.closed_at.date(), quantity]
            )
    # The operations decision is recorded only after every independently
    # provable historical fill/rejection.  The same ledger lock covers both
    # phases, so a concurrent reviewer cannot re-open or replace the failed
    # optional BUY between settlement and cancellation.
    for cancellation in sorted(
        operations_cancellations,
        key=lambda value: (value.cancelled_at, value.intent_id),
    ):
        document, _event = _append_event_unlocked(
            path,
            kind="OPERATIONS_CANCEL",
            payload={
                **_jsonable(asdict(cancellation)),
                "cancellation_id": cancellation.cancellation_id,
            },
            identity_field="cancellation_id",
            identity=cancellation.cancellation_id,
        )
        terminal_intents.add(cancellation.intent_id)
        capital_evaluations.append(
            {
                "schema": "chanlun-human-paper-operations-cancellation",
                "result": cancellation.reason_code,
                "intent_id": cancellation.intent_id,
                "symbol": cancellation.symbol,
                "cancellation_id": cancellation.cancellation_id,
                "cancelled_at": cancellation.cancelled_at.isoformat(),
                "grid_status": cancellation.grid_status,
                "operations_state": cancellation.operations_state,
                "execution_fact_snapshot_sha256": (
                    cancellation.execution_fact_snapshot_sha256
                ),
                "execution_evidence_snapshot_sha256": (
                    cancellation.execution_evidence_snapshot_sha256
                ),
                "tick_data_used": False,
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
        )
    return document, tuple(capital_evaluations)


def settle_human_paper_intents_with_portfolio_controls(
    path: Path,
    *,
    bars_by_symbol: Mapping[str, Sequence[HumanPaperMinuteBar]],
    accounting_parameters: HumanPaperAccountingParameters,
    operations_cancellations: Sequence[
        HumanPaperOperationsCancellation
    ] = (),
    entry_provenance_blocked_intent_ids: Sequence[str] = (),
    causal_gap_blocked_intent_ids: Sequence[str] = (),
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Production settlement with marks, provenance blocks and cancels.

    A provenance block is deliberately non-terminal: a BUY remains pending
    while its archived QMT sector proof is unavailable or invalid.  SELL
    intents never enter this set, so risk-reducing exits continue normally.
    """

    with interprocess_file_lock(path.with_suffix(path.suffix + ".lock")):
        return _settle_human_paper_intents_unlocked(
            path,
            bars_by_symbol=bars_by_symbol,
            accounting_parameters=accounting_parameters,
            operations_cancellations=operations_cancellations,
            entry_provenance_blocked_intent_ids=(
                entry_provenance_blocked_intent_ids
            ),
            causal_gap_blocked_intent_ids=causal_gap_blocked_intent_ids,
        )


def _semantic_document(path: Path, *, schema: str) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise ValueError("paper execution evidence schema changed")
    claimed = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if claimed != sha256_json(stable):
        raise ValueError("paper execution evidence content hash mismatch")
    if path.stem != str(claimed)[7:]:
        raise ValueError("paper execution evidence path identity changed")
    return payload


def load_human_paper_execution_capture(
    *,
    forward_root: Path,
    session: date,
) -> tuple[dict[str, object], dict[str, object]]:
    """Load one promoted QMT execution capture and prove its immutable objects.

    This read-only adapter is shared by daily valuation so the close mark uses
    the same previously frozen 1m grid and instrument facts as settlement.  It
    intentionally exposes no market-data, account or order transport.
    """

    session_root = forward_root / "sessions" / session.isoformat()
    evidence_alias = session_root / "paper_execution_evidence.json"
    facts_alias = session_root / "paper_execution_facts.json"
    if not evidence_alias.is_file() or not facts_alias.is_file():
        raise ValueError("promoted execution capture is missing")

    def promoted(
        alias: Path,
        *,
        kind: str,
        schema: str,
    ) -> dict[str, object]:
        raw = json.loads(alias.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("promoted execution capture is malformed")
        identity = str(raw.get("content_sha256") or "")
        if _SHA256.fullmatch(identity) is None:
            raise ValueError("promoted execution capture identity is invalid")
        object_path = alias.parent / "objects" / kind / f"{identity[7:]}.json"
        if not object_path.is_file():
            raise ValueError("immutable execution capture object is missing")
        immutable = _semantic_document(object_path, schema=schema)
        if immutable != raw:
            raise ValueError("execution capture alias and object disagree")
        return immutable

    evidence = promoted(
        evidence_alias,
        kind="paper_execution_evidence",
        schema=EXECUTION_EVIDENCE_SCHEMA,
    )
    facts = promoted(
        facts_alias,
        kind="paper_execution_facts",
        schema=EXECUTION_FACT_SCHEMA,
    )
    _captured_at, bars_by_symbol, audits_by_symbol = (
        _validate_execution_document_envelope(
            evidence=evidence,
            facts=facts,
            session=session,
        )
    )
    for symbol, audit in audits_by_symbol.items():
        if audit.get("status") != "COMPLETE":
            continue
        raw_bars = bars_by_symbol.get(symbol)
        if not isinstance(raw_bars, list):
            raise ValueError("complete execution grid bars are missing")
        _validate_full_a_share_execution_session_grid(
            raw_bars,
            session=session,
        )
    return evidence, facts


def _validate_execution_evidence_bar_intervals(
    bars: Sequence[object],
) -> None:
    """Reject a re-hashed evidence object containing non-1m time windows."""

    for value in bars:
        if not isinstance(value, Mapping):
            raise ValueError("execution evidence contains a malformed bar")
        opened_at = normalize_datetime(
            datetime.fromisoformat(str(value["opened_at"])),
            "opened_at",
        )
        closed_at = normalize_datetime(
            datetime.fromisoformat(str(value["closed_at"])),
            "closed_at",
        )
        validate_a_share_completed_one_minute_interval(opened_at, closed_at)


def _validate_full_a_share_execution_session_grid(
    bars: Sequence[object],
    *,
    session: date,
) -> None:
    """Prove that no earlier completed 1m execution opportunity is missing."""

    _validate_execution_evidence_bar_intervals(bars)
    closes: list[datetime] = []
    for value in bars:
        if not isinstance(value, Mapping) or value.get("complete") is not True:
            raise ValueError("execution evidence contains an incomplete 1m bar")
        closes.append(
            normalize_datetime(
                datetime.fromisoformat(str(value["closed_at"])),
                "closed_at",
            )
        )
    validate_a_share_complete_session_closes(closes, session=session)


def _validate_execution_document_envelope(
    *,
    evidence: Mapping[str, object],
    facts: Mapping[str, object],
    session: date,
) -> tuple[
    datetime,
    Mapping[str, object],
    dict[str, Mapping[str, object]],
]:
    """Recompute one execution capture's document-level facts.

    Content hashes prove only that a document was not changed after it was
    named.  They do not prove that self-reported counts, capture timestamps or
    bar-grid summaries were true before the document was re-hashed.  Every
    terminal audit therefore uses this common envelope verifier before it
    trusts any individual bar or instrument row.
    """

    session_text = session.isoformat()
    captured_at = normalize_datetime(
        datetime.fromisoformat(str(evidence["captured_at"])),
        "captured_at",
    )
    fact_identity = str(evidence.get("execution_fact_snapshot_sha256") or "")
    if (
        evidence.get("session") != session_text
        or captured_at.date() != session
        or _SHA256.fullmatch(fact_identity) is None
        or facts.get("content_sha256") != fact_identity
        or evidence.get("fill_model") != STRICT_BAR_PRICE_RULE
        or evidence.get("fill_timestamp_rule")
        != STRICT_BAR_EXECUTION_TIMESTAMP_RULE
        or evidence.get("buy_strict_cross_rule") != STRICT_BAR_CROSS_RULE
        or evidence.get("buy_max_bar_volume_participation")
        != format(STRICT_BAR_VOLUME_PARTICIPATION, "f")
        or evidence.get("minimum_market_data_frequency") != "1m"
        or evidence.get("tick_data_used") is not False
        or evidence.get("account_api_used") is not False
        or evidence.get("broker_transport_available") is not False
        or evidence.get("live_status") != "LIVE_DISABLED"
        or facts.get("session") != session_text
        or facts.get("captured_at") != evidence.get("captured_at")
        or facts.get("source")
        != "QMT_READ_ONLY_INSTRUMENT_DETAIL_AND_DIVID_FACTORS"
        or facts.get("minimum_market_data_frequency") != "1m"
        or facts.get("tick_data_used") is not False
        or facts.get("account_api_used") is not False
        or facts.get("broker_transport_available") is not False
        or facts.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("execution evidence document envelope changed")

    pending_ids = evidence.get("pending_intent_ids")
    if (
        not isinstance(pending_ids, list)
        or any(not isinstance(value, str) or not value for value in pending_ids)
        or pending_ids != sorted(set(pending_ids))
    ):
        raise ValueError("execution evidence pending intent set is malformed")

    bars_by_symbol = evidence.get("bars_by_symbol")
    if not isinstance(bars_by_symbol, Mapping):
        raise ValueError("execution evidence bar map is malformed")
    requested_symbols = set()
    for raw_symbol, raw_bars in bars_by_symbol.items():
        if not isinstance(raw_symbol, str) or not raw_symbol:
            raise ValueError("execution evidence symbol is malformed")
        if not isinstance(raw_bars, list):
            raise ValueError("execution evidence symbol bars are malformed")
        requested_symbols.add(raw_symbol)

    raw_audits = evidence.get("bar_grid_audits")
    if not isinstance(raw_audits, list):
        raise ValueError("execution grid audits are malformed")
    audits_by_symbol: dict[str, Mapping[str, object]] = {}
    successful_grid_states = {
        "COMPLETE",
        "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
    }
    allowed_grid_states = successful_grid_states | {
        "EXECUTION_FACT_MISSING_FAIL_CLOSED",
        "INCOMPLETE_FAIL_CLOSED",
        "INVALID_FAIL_CLOSED",
    }
    for raw_audit in raw_audits:
        if not isinstance(raw_audit, Mapping):
            raise ValueError("execution grid audit row is malformed")
        symbol = raw_audit.get("symbol")
        status = raw_audit.get("status")
        native_count = raw_audit.get("native_row_count")
        normalized_count = raw_audit.get("normalized_row_count")
        complete_sessions = raw_audit.get("complete_sessions")
        issues = raw_audit.get("session_issues")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol in audits_by_symbol
            or symbol not in requested_symbols
            or status not in allowed_grid_states
            or type(native_count) is not int
            or native_count < 0
            or type(normalized_count) is not int
            or normalized_count < 0
            or not isinstance(complete_sessions, list)
            or not isinstance(issues, list)
        ):
            raise ValueError("execution grid audit row cannot be recomputed")
        symbol_bars = bars_by_symbol[symbol]
        if normalized_count != len(symbol_bars):
            raise ValueError("execution grid normalized row count changed")
        if status == "COMPLETE":
            if (
                normalized_count != 240
                or complete_sessions != [session_text]
                or issues
            ):
                raise ValueError("complete execution grid summary changed")
        elif (
            normalized_count != 0
            or complete_sessions
            or (
                status == "NOT_REQUIRED_INSTRUMENT_INELIGIBLE"
                and issues
            )
        ):
            raise ValueError("non-complete execution grid summary changed")
        audits_by_symbol[symbol] = raw_audit
    if set(audits_by_symbol) != requested_symbols:
        raise ValueError("execution grid audit symbol coverage changed")
    expected_all_grids_complete = all(
        value.get("status") in successful_grid_states
        for value in audits_by_symbol.values()
    )
    if (
        type(evidence.get("all_required_bar_grids_complete")) is not bool
        or evidence.get("all_required_bar_grids_complete")
        is not expected_all_grids_complete
    ):
        raise ValueError("execution evidence aggregate grid status changed")

    fact_rows = facts.get("symbols")
    error_rows = facts.get("errors")
    if not isinstance(fact_rows, list) or not isinstance(error_rows, list):
        raise ValueError("execution fact rows are malformed")
    fact_symbols: set[str] = set()
    expected_sources = [
        "QMT_GET_INSTRUMENT_DETAIL",
        "QMT_GET_DIVID_FACTORS",
    ]
    for fact in fact_rows:
        if not isinstance(fact, Mapping):
            raise ValueError("execution fact row is malformed")
        symbol = fact.get("symbol")
        factor_start = fact.get("factor_start")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol in fact_symbols
            or symbol not in requested_symbols
            or fact.get("session") != session_text
            or fact.get("trading_day") != session_text
            or not isinstance(fact.get("native_code"), str)
            or not fact.get("native_code")
            or fact.get("source_methods") != expected_sources
            or fact.get("tick_data_used") is not False
            or fact.get("account_api_used") is not False
            or not isinstance(factor_start, str)
            or date.fromisoformat(factor_start) > session
        ):
            raise ValueError("execution fact row envelope changed")
        fact_symbols.add(symbol)
    error_symbols: set[str] = set()
    for error in error_rows:
        if not isinstance(error, Mapping):
            raise ValueError("execution fact error row is malformed")
        symbol = error.get("symbol")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol not in requested_symbols
            or not isinstance(error.get("reason"), str)
            or not error.get("reason")
        ):
            raise ValueError("execution fact error row envelope changed")
        error_symbols.add(symbol)
    if fact_symbols | error_symbols != requested_symbols:
        raise ValueError("execution fact requested symbol coverage changed")
    expected_all_facts_complete = (
        fact_symbols == requested_symbols
        and not error_rows
        and all(
            fact.get("security_status_complete") is True
            and fact.get("corporate_action_state_complete") is True
            for fact in fact_rows
        )
    )
    if (
        type(facts.get("requested_symbol_count")) is not int
        or facts.get("requested_symbol_count") != len(requested_symbols)
        or type(facts.get("complete_symbol_count")) is not int
        or facts.get("complete_symbol_count") != len(fact_rows)
        or type(facts.get("all_complete")) is not bool
        or facts.get("all_complete") is not expected_all_facts_complete
    ):
        raise ValueError("execution fact aggregate coverage changed")
    return captured_at, bars_by_symbol, audits_by_symbol


def _execution_evidence_capture_state(
    events: Sequence[Mapping[str, object]],
    *,
    identity: str,
) -> tuple[
    dict[str, int],
    dict[str, date],
    dict[str, list[list[object]]],
]:
    """Rebuild the virtual ledger immediately before one evidence snapshot.

    A settlement records independently executable fills/rejections before an
    optional-BUY operations cancellation.  The first event referencing the
    content-addressed snapshot is therefore the only causal ledger boundary
    shared by every outcome produced from that capture.
    """

    reference_indexes = tuple(
        index
        for index, event in enumerate(events)
        if isinstance(event.get("payload"), Mapping)
        and (
            event["payload"].get("execution_snapshot_sha256") == identity
            or event["payload"].get("execution_evidence_snapshot_sha256")
            == identity
        )
    )
    if not reference_indexes:
        raise ValueError("execution evidence has no ledger capture boundary")
    prefix = events[: min(reference_indexes)]
    return (
        human_paper_position_quantities(prefix),
        human_paper_oldest_open_lot_sessions(prefix),
        _virtual_lots(prefix),
    )


def _unreferenced_execution_document_capture_state(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
    identity: str,
    captured_at: datetime,
    intent_id: str,
) -> tuple[
    dict[str, int],
    dict[str, date],
    dict[str, list[list[object]]],
]:
    """Rebuild state for a capture that produced no terminal ledger event.

    Referenced snapshots use the exact first-reference boundary above.  A
    no-action snapshot has no such event, so its state consists of every fill
    preceding the pending intent plus later fills whose own immutable capture
    demonstrably predates this document.  Equal-time foreign captures are
    ambiguous and fail closed.
    """

    if any(
        isinstance(event.get("payload"), Mapping)
        and (
            event["payload"].get("execution_snapshot_sha256") == identity
            or event["payload"].get("execution_evidence_snapshot_sha256")
            == identity
        )
        for event in events
    ):
        return _execution_evidence_capture_state(events, identity=identity)
    intent_indexes = tuple(
        index
        for index, event in enumerate(events)
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("intent_id") == intent_id
    )
    if len(intent_indexes) != 1:
        raise ValueError("continuity intent capture boundary is unavailable")
    intent_index = intent_indexes[0]
    capture_events = list(events[:intent_index])
    for event in events[intent_index:]:
        payload = event.get("payload")
        if event.get("kind") != "FILL" or not isinstance(payload, Mapping):
            continue
        fill_identity = str(payload.get("execution_snapshot_sha256") or "")
        if _SHA256.fullmatch(fill_identity) is None:
            raise ValueError("later fill capture identity is unavailable")
        filled_at = normalize_datetime(
            datetime.fromisoformat(str(payload["filled_at"])),
            "filled_at",
        )
        fill_path = (
            forward_root
            / "sessions"
            / filled_at.date().isoformat()
            / "objects"
            / "paper_execution_evidence"
            / f"{fill_identity[7:]}.json"
        )
        fill_evidence = _semantic_document(
            fill_path,
            schema=EXECUTION_EVIDENCE_SCHEMA,
        )
        fill_captured_at = normalize_datetime(
            datetime.fromisoformat(str(fill_evidence["captured_at"])),
            "captured_at",
        )
        if fill_captured_at == captured_at:
            raise ValueError("equal-time execution captures cannot be ordered")
        if fill_captured_at < captured_at:
            capture_events.append(event)
    prefix = tuple(capture_events)
    return (
        human_paper_position_quantities(prefix),
        human_paper_oldest_open_lot_sessions(prefix),
        _virtual_lots(prefix),
    )


def _validate_execution_fact_against_capture_state(
    *,
    fact: Mapping[str, object],
    facts: Mapping[str, object],
    symbol: str,
    session: date,
    capture_positions: Mapping[str, int],
    capture_oldest_lots: Mapping[str, date],
) -> dict[str, object]:
    """Independently recompute one QMT fact row from raw fields and FIFO state."""

    raw_flags = tuple(
        fact.get(name) for name in ("suspended", "expired", "is_st")
    )
    if any(type(value) is not bool for value in raw_flags):
        raise ValueError("execution security flags are malformed")
    suspended, expired, is_st = raw_flags
    if "expiry_date" not in fact:
        raise ValueError("execution expiry date is missing")
    raw_expiry_date = fact.get("expiry_date")
    if raw_expiry_date is None:
        expected_expired = False
    elif isinstance(raw_expiry_date, str):
        expected_expired = date.fromisoformat(raw_expiry_date) < session
    else:
        raise ValueError("execution expiry date is malformed")
    instrument_status = fact.get("instrument_status")
    if (
        type(instrument_status) is not int
        or suspended is not (instrument_status >= 1)
        or expired is not expected_expired
        or is_st is not is_st_name(str(fact.get("instrument_name") or ""))
    ):
        raise ValueError("execution raw security status cannot be recomputed")
    expected_buy_eligible = not suspended and not expired and not is_st
    expected_sell_eligible = not suspended and not expired
    if (
        fact.get("buy_eligible") is not expected_buy_eligible
        or fact.get("sell_eligible") is not expected_sell_eligible
    ):
        raise ValueError("execution security eligibility cannot be recomputed")
    security_status_complete = fact.get("security_status_complete")
    corporate_action_state_complete = fact.get(
        "corporate_action_state_complete"
    )
    if (
        type(security_status_complete) is not bool
        or type(corporate_action_state_complete) is not bool
    ):
        raise ValueError("execution fact completeness is malformed")
    if not security_status_complete:
        raise ValueError(
            "present execution fact cannot claim security status incomplete"
        )

    expected_position_quantity = capture_positions.get(symbol, 0)
    expected_oldest_session = capture_oldest_lots.get(symbol)
    if (
        type(fact.get("virtual_position_quantity")) is not int
        or fact.get("virtual_position_quantity") != expected_position_quantity
        or fact.get("oldest_virtual_acquired_session")
        != (
            None
            if expected_oldest_session is None
            else expected_oldest_session.isoformat()
        )
    ):
        raise ValueError("execution fact virtual position provenance changed")

    corporate_actions = fact.get("corporate_actions")
    if not isinstance(corporate_actions, list):
        raise ValueError("execution corporate actions are malformed")
    action_sessions: list[date] = []
    for action in corporate_actions:
        if not isinstance(action, Mapping):
            raise ValueError("execution corporate action row is malformed")
        effective_on = action.get("effective_on")
        if not isinstance(effective_on, str):
            raise ValueError("execution corporate action date is malformed")
        action_session = date.fromisoformat(effective_on)
        if action_session > session:
            raise ValueError("execution corporate action uses a future date")
        action_sessions.append(action_session)
    expected_position_action_conflict = (
        expected_position_quantity > 0
        and expected_oldest_session is not None
        and any(
            expected_oldest_session <= action_session <= session
            for action_session in action_sessions
        )
    )
    if (
        type(fact.get("position_corporate_action_conflict")) is not bool
        or fact.get("position_corporate_action_conflict")
        is not expected_position_action_conflict
        or corporate_action_state_complete
        is not (not expected_position_action_conflict)
    ):
        raise ValueError(
            "execution corporate-action completeness cannot be recomputed"
        )
    fact_errors = facts.get("errors")
    if not isinstance(fact_errors, list):
        raise ValueError("execution fact errors are malformed")
    corporate_conflict_errors = tuple(
        error
        for error in fact_errors
        if isinstance(error, Mapping)
        and error.get("symbol") == symbol
        and error.get("reason")
        == "VIRTUAL_POSITION_CORPORATE_ACTION_RECONCILIATION_REQUIRED"
    )
    if len(corporate_conflict_errors) != int(expected_position_action_conflict):
        raise ValueError(
            "execution corporate-action error receipt is inconsistent"
        )
    return {
        "buy_eligible": expected_buy_eligible,
        "sell_eligible": expected_sell_eligible,
        "security_status_complete": security_status_complete,
        "corporate_action_state_complete": corporate_action_state_complete,
        "position_corporate_action_conflict": (
            expected_position_action_conflict
        ),
        "virtual_position_quantity": expected_position_quantity,
        "oldest_virtual_acquired_session": expected_oldest_session,
    }


def _validate_execution_bar_against_fact(
    *,
    bar: Mapping[str, object],
    fact: Mapping[str, object],
    verified_fact: Mapping[str, object],
    symbol: str,
) -> None:
    """Recompute one completed 1m bar's eligibility and limit-lock flags."""

    boolean_fields = (
        "complete",
        "suspended",
        "limit_up_locked",
        "limit_down_locked",
        "buy_eligible",
        "sell_eligible",
        "security_status_complete",
        "corporate_action_state_complete",
    )
    if any(type(bar.get(name)) is not bool for name in boolean_fields):
        raise ValueError("execution bar boolean facts are malformed")
    try:
        open_price = Decimal(str(bar["open"]))
        high = Decimal(str(bar["high"]))
        low = Decimal(str(bar["low"]))
        close = Decimal(str(bar["close"]))
        volume = Decimal(str(bar["volume"]))
        limit_up = Decimal(str(fact["limit_up"]))
        limit_down = Decimal(str(fact["limit_down"]))
    except (InvalidOperation, KeyError, ValueError) as exc:
        raise ValueError("execution bar price facts are malformed") from exc
    prices = (open_price, high, low, close, limit_up, limit_down)
    if (
        any(not value.is_finite() or value <= 0 for value in prices)
        or not volume.is_finite()
        or volume < 0
        or low > min(open_price, close)
        or high < max(open_price, close)
        or low > high
        or limit_down >= limit_up
    ):
        raise ValueError("execution bar OHLCV or limit prices are invalid")
    one_price_bar = high == low
    expected_limit_up_locked = one_price_bar and high == limit_up
    expected_limit_down_locked = one_price_bar and low == limit_down
    if (
        bar.get("symbol") != symbol
        or bar.get("complete") is not True
        or bar.get("suspended") is not fact.get("suspended")
        or bar.get("buy_eligible") != verified_fact["buy_eligible"]
        or bar.get("sell_eligible") != verified_fact["sell_eligible"]
        or bar.get("security_status_complete")
        is not verified_fact["security_status_complete"]
        or bar.get("corporate_action_state_complete")
        is not verified_fact["corporate_action_state_complete"]
        or bar.get("limit_up_locked") is not expected_limit_up_locked
        or bar.get("limit_down_locked") is not expected_limit_down_locked
    ):
        raise ValueError(
            "execution bar eligibility or limit-lock state cannot be recomputed"
        )


def _verify_synchronous_position_marks(
    *,
    raw_marks: object,
    bars_by_symbol: Mapping[str, object],
    facts: Mapping[str, object],
    capture_positions: Mapping[str, int],
    capture_oldest_lots: Mapping[str, date],
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    """Verify marks against the exact decision interval and fact object."""

    if not isinstance(raw_marks, list):
        raise ValueError("portfolio decision marks are malformed")
    fact_rows = facts.get("symbols")
    if not isinstance(fact_rows, list):
        raise ValueError("portfolio mark instrument facts are invalid")
    seen_mark_symbols: set[str] = set()
    for mark in raw_marks:
        if not isinstance(mark, Mapping):
            raise ValueError("portfolio decision mark is malformed")
        mark_symbol = str(mark.get("symbol") or "")
        if not mark_symbol or mark_symbol in seen_mark_symbols:
            raise ValueError("portfolio decision mark symbol is invalid")
        seen_mark_symbols.add(mark_symbol)
        mark_quantity = int(mark.get("quantity") or 0)
        mark_price = Decimal(str(mark.get("price")))
        mark_value = Decimal(str(mark.get("market_value")))
        if (
            mark_quantity <= 0
            or mark_price <= 0
            or mark_value != Decimal(mark_quantity) * mark_price
        ):
            raise ValueError("portfolio decision mark value is invalid")
        mark_bars = bars_by_symbol.get(mark_symbol)
        if not isinstance(mark_bars, list):
            raise ValueError("portfolio decision mark has no execution bars")
        _validate_full_a_share_execution_session_grid(
            mark_bars,
            session=opened_at.date(),
        )
        mark_matches = tuple(
            value
            for value in mark_bars
            if isinstance(value, Mapping)
            and value.get("opened_at") == opened_at.isoformat()
            and value.get("closed_at") == closed_at.isoformat()
        )
        if len(mark_matches) != 1:
            raise ValueError(
                "portfolio mark does not resolve one synchronous 1m bar"
            )
        mark_bar = mark_matches[0]
        if (
            mark_bar.get("symbol") != mark_symbol
            or Decimal(str(mark_bar.get("close"))) != mark_price
            or mark_bar.get("complete") is not True
            or mark_bar.get("security_status_complete") is not True
            or mark_bar.get("corporate_action_state_complete") is not True
            or mark_bar.get("suspended") is not False
        ):
            raise ValueError(
                "portfolio mark and synchronous 1m bar facts disagree"
            )
        matching_mark_facts = tuple(
            value
            for value in fact_rows
            if isinstance(value, Mapping) and value.get("symbol") == mark_symbol
        )
        if len(matching_mark_facts) != 1:
            raise ValueError(
                "portfolio mark does not resolve one instrument fact"
            )
        mark_fact = matching_mark_facts[0]
        verified_mark_fact = _validate_execution_fact_against_capture_state(
            fact=mark_fact,
            facts=facts,
            symbol=mark_symbol,
            session=opened_at.date(),
            capture_positions=capture_positions,
            capture_oldest_lots=capture_oldest_lots,
        )
        for execution_bar in mark_bars:
            if not isinstance(execution_bar, Mapping):
                raise ValueError("portfolio mark execution bar is malformed")
            _validate_execution_bar_against_fact(
                bar=execution_bar,
                fact=mark_fact,
                verified_fact=verified_mark_fact,
                symbol=mark_symbol,
            )
        if (
            verified_mark_fact["security_status_complete"] is not True
            or verified_mark_fact["corporate_action_state_complete"] is not True
            or verified_mark_fact["virtual_position_quantity"] != mark_quantity
            or mark_fact.get("suspended") is not False
            or mark_bar.get("buy_eligible")
            != verified_mark_fact["buy_eligible"]
            or mark_bar.get("sell_eligible")
            != verified_mark_fact["sell_eligible"]
        ):
            raise ValueError("portfolio mark instrument facts and bar disagree")


def audit_human_paper_execution_evidence(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
    _capture_state_events: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Prove every virtual fill against immutable status and exact 1m bar facts."""

    fills = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "FILL" and isinstance(event.get("payload"), Mapping)
    )
    intents = {
        str(event["payload"].get("intent_id") or ""): event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    }
    if not fills:
        return {
            "status": "NO_FILLS",
            "fill_count": 0,
            "verified_fill_count": 0,
            "unique_execution_evidence_count": 0,
            "missing_evidence": [],
            "invalid_evidence": [],
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }

    cache: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    capture_state_cache: dict[
        str,
        tuple[
            dict[str, int],
            dict[str, date],
            dict[str, list[list[object]]],
        ],
    ] = {}
    missing: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    verified = 0
    capture_events = (
        events if _capture_state_events is None else _capture_state_events
    )
    for fill in fills:
        fill_id = str(fill.get("fill_id") or "")
        identity = str(fill.get("execution_snapshot_sha256") or "")
        symbol = str(fill.get("symbol") or "")
        try:
            if _SHA256.fullmatch(identity) is None:
                raise ValueError("fill execution evidence identity is invalid")
            if identity not in capture_state_cache:
                capture_state_cache[identity] = _execution_evidence_capture_state(
                    capture_events,
                    identity=identity,
                )
            filled_at = normalize_datetime(
                datetime.fromisoformat(str(fill["filled_at"])),
                "filled_at",
            )
            source_bar_closed_at = normalize_datetime(
                datetime.fromisoformat(str(fill["source_bar_closed_at"])),
                "source_bar_closed_at",
            )
            source_bar_opened_at = source_bar_closed_at - timedelta(minutes=1)
            validate_a_share_completed_one_minute_interval(
                source_bar_opened_at,
                source_bar_closed_at,
            )
            if filled_at != source_bar_closed_at:
                raise ValueError("fill was backdated before its source bar completed")
            session_root = forward_root / "sessions" / filled_at.date().isoformat()
            evidence_path = (
                session_root
                / "objects"
                / "paper_execution_evidence"
                / f"{identity[7:]}.json"
            )
            if not evidence_path.is_file():
                missing.append(
                    {
                        "fill_id": fill_id,
                        "execution_snapshot_sha256": identity,
                        "reason": "EXECUTION_EVIDENCE_OBJECT_MISSING",
                    }
                )
                continue

            if identity not in cache:
                evidence = _semantic_document(
                    evidence_path,
                    schema=EXECUTION_EVIDENCE_SCHEMA,
                )
                fact_identity = str(evidence.get("execution_fact_snapshot_sha256") or "")
                if _SHA256.fullmatch(fact_identity) is None:
                    raise ValueError("execution fact snapshot identity is invalid")
                fact_path = (
                    session_root
                    / "objects"
                    / "paper_execution_facts"
                    / f"{fact_identity[7:]}.json"
                )
                if not fact_path.is_file():
                    raise ValueError("execution fact snapshot object is missing")
                facts = _semantic_document(fact_path, schema=EXECUTION_FACT_SCHEMA)
                cache[identity] = evidence, facts
            evidence, facts = cache[identity]
            captured_at, bars_by_symbol, _audits_by_symbol = (
                _validate_execution_document_envelope(
                    evidence=evidence,
                    facts=facts,
                    session=filled_at.date(),
                )
            )
            if captured_at < source_bar_closed_at:
                raise ValueError("execution evidence was not captured after its source bar")
            pending_ids = evidence.get("pending_intent_ids")
            if not isinstance(pending_ids, list) or fill.get("intent_id") not in pending_ids:
                raise ValueError("fill intent is absent from execution evidence")
            symbol_bars = bars_by_symbol.get(symbol)
            if not isinstance(symbol_bars, list):
                raise ValueError("fill symbol has no execution bars")
            _validate_full_a_share_execution_session_grid(
                symbol_bars,
                session=filled_at.date(),
            )
            matches = tuple(
                bar
                for bar in symbol_bars
                if isinstance(bar, Mapping)
                and bar.get("opened_at") == source_bar_opened_at.isoformat()
                and bar.get("closed_at") == source_bar_closed_at.isoformat()
            )
            if len(matches) != 1:
                raise ValueError("fill does not resolve one exact execution bar")
            bar = matches[0]
            side = str(fill.get("side") or "")
            intent = intents.get(str(fill.get("intent_id") or ""))
            if intent is None:
                raise ValueError("fill intent is unavailable")
            expected_execution_price = adverse_observed_bar_price(
                side="buy" if side == "BUY" else "sell",
                raw_high=Decimal(str(bar.get("high"))),
                raw_low=Decimal(str(bar.get("low"))),
            )
            if (
                side not in {"BUY", "SELL"}
                or bar.get("symbol") != symbol
                or expected_execution_price
                != Decimal(str(fill.get("price")))
                or strict_bar_volume_capacity(
                    Decimal(str(bar.get("volume"))),
                    quantity_increment=100,
                )
                < int(fill.get("quantity") or 0)
                or bar.get("complete") is not True
                or bar.get("security_status_complete") is not True
                or bar.get("corporate_action_state_complete") is not True
                or bar.get("suspended") is not False
                or (
                    side == "BUY"
                    and (
                        bar.get("buy_eligible") is not True
                        or bar.get("limit_up_locked") is not False
                    )
                )
                or (
                    side == "SELL"
                    and (
                        bar.get("sell_eligible") is not True
                        or bar.get("limit_down_locked") is not False
                    )
                )
            ):
                raise ValueError("fill and exact execution bar facts disagree")
            if side == "BUY":
                earliest = normalize_datetime(
                    datetime.fromisoformat(str(intent["earliest_fill_at"])),
                    "earliest_fill_at",
                )
                valid_until = normalize_datetime(
                    datetime.fromisoformat(str(intent["entry_valid_until"])),
                    "entry_valid_until",
                )
                price_cap = Decimal(str(intent["entry_price_cap"]))
                terminal_actions: list[
                    tuple[
                        datetime,
                        datetime,
                        Literal["FILL", "PRICE_CAP_REJECT"],
                    ]
                ] = []
                for value in symbol_bars:
                    if not isinstance(value, Mapping):
                        continue
                    opened = normalize_datetime(
                        datetime.fromisoformat(str(value["opened_at"])),
                        "opened_at",
                    )
                    closed = normalize_datetime(
                        datetime.fromisoformat(str(value["closed_at"])),
                        "closed_at",
                    )
                    if (
                        opened >= earliest
                        and closed <= valid_until
                        and value.get("complete") is True
                        and value.get("security_status_complete") is True
                        and value.get("corporate_action_state_complete") is True
                        and value.get("buy_eligible") is True
                        and value.get("suspended") is False
                        and value.get("limit_up_locked") is False
                    ):
                        terminal_action = (
                            _buy_ohlcv_terminal_execution_action(
                                raw_high=Decimal(str(value.get("high"))),
                                raw_low=Decimal(str(value.get("low"))),
                                raw_volume=Decimal(str(value.get("volume"))),
                                limit_price=price_cap,
                                quantity=int(fill.get("quantity") or 0),
                            )
                        )
                        if terminal_action is not None:
                            terminal_actions.append(
                                (opened, closed, terminal_action)
                            )
                if (
                    not terminal_actions
                    or min(terminal_actions)
                    != (source_bar_opened_at, source_bar_closed_at, "FILL")
                    or Decimal(str(fill.get("price"))) > price_cap
                ):
                    raise ValueError(
                        "buy fill was not the first eligible in-cap TTL 1m bar"
                    )

            fact_rows = facts.get("symbols")
            if not isinstance(fact_rows, list):
                raise ValueError("execution fact symbol rows are invalid")
            matching_facts = tuple(
                value
                for value in fact_rows
                if isinstance(value, Mapping) and value.get("symbol") == symbol
            )
            if len(matching_facts) != 1:
                raise ValueError("fill does not resolve one exact instrument fact")
            fact = matching_facts[0]
            (
                capture_positions,
                capture_oldest_lots,
                capture_lots,
            ) = capture_state_cache[identity]
            verified_fact = _validate_execution_fact_against_capture_state(
                fact=fact,
                facts=facts,
                symbol=symbol,
                session=filled_at.date(),
                capture_positions=capture_positions,
                capture_oldest_lots=capture_oldest_lots,
            )
            for execution_bar in symbol_bars:
                if not isinstance(execution_bar, Mapping):
                    raise ValueError("fill execution bar is malformed")
                _validate_execution_bar_against_fact(
                    bar=execution_bar,
                    fact=fact,
                    verified_fact=verified_fact,
                    symbol=symbol,
                )
            if (
                facts.get("session") != filled_at.date().isoformat()
                or facts.get("tick_data_used") is not False
                or facts.get("account_api_used") is not False
                or facts.get("broker_transport_available") is not False
                or facts.get("live_status") != "LIVE_DISABLED"
                or verified_fact["security_status_complete"] is not True
                or verified_fact["corporate_action_state_complete"] is not True
                or fact.get("suspended") is not False
                or bar.get("buy_eligible") != verified_fact["buy_eligible"]
                or bar.get("sell_eligible") != verified_fact["sell_eligible"]
            ):
                raise ValueError("execution instrument facts and bar evidence disagree")
            if side == "SELL" and _sellable_quantity(
                capture_lots.get(symbol, ()),
                filled_at,
            ) < int(fill.get("quantity") or 0):
                raise ValueError(
                    "sell fill is not supported by capture-time T+1 lots"
                )
            if "position_marks" in fill:
                _verify_synchronous_position_marks(
                    raw_marks=fill["position_marks"],
                    bars_by_symbol=bars_by_symbol,
                    facts=facts,
                    capture_positions=capture_positions,
                    capture_oldest_lots=capture_oldest_lots,
                    opened_at=source_bar_opened_at,
                    closed_at=source_bar_closed_at,
                )
            verified += 1
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "fill_id": fill_id,
                    "execution_snapshot_sha256": identity,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )

    status = "COMPLETE"
    if invalid:
        status = "INVALID"
    elif missing:
        status = "MISSING"
    return {
        "status": status,
        "fill_count": len(fills),
        "verified_fill_count": verified,
        "unique_execution_evidence_count": len(cache),
        "missing_evidence": missing,
        "invalid_evidence": invalid,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_execution_rejection_evidence(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
) -> dict[str, object]:
    """Prove price-cap and TTL rejections against immutable 1m evidence."""

    rejections = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "EXECUTION_REJECT"
        and isinstance(event.get("payload"), Mapping)
    )
    schema = "chanlun-human-paper-execution-rejection-evidence-audit"
    if not rejections:
        return {
            "schema": schema,
            "status": "NO_REJECTIONS",
            "rejection_count": 0,
            "verified_rejection_count": 0,
            "unique_execution_evidence_count": 0,
            "missing_evidence": [],
            "invalid_evidence": [],
            "first_eligible_bar_verified": True,
            "price_cap_and_ttl_verified": True,
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    intents = {
        str(event["payload"].get("intent_id") or ""): event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    }
    cache: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    capture_state_cache: dict[
        str,
        tuple[
            dict[str, int],
            dict[str, date],
            dict[str, list[list[object]]],
        ],
    ] = {}
    missing: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    verified = 0
    for rejection in rejections:
        rejection_id = str(rejection.get("rejection_id") or "")
        identity = str(rejection.get("execution_snapshot_sha256") or "")
        try:
            if _SHA256.fullmatch(identity) is None:
                raise ValueError("execution rejection evidence identity is invalid")
            if identity not in capture_state_cache:
                capture_state_cache[identity] = _execution_evidence_capture_state(
                    events,
                    identity=identity,
                )
            opened_at = normalize_datetime(
                datetime.fromisoformat(str(rejection["candidate_bar_opened_at"])),
                "candidate_bar_opened_at",
            )
            closed_at = normalize_datetime(
                datetime.fromisoformat(str(rejection["candidate_bar_closed_at"])),
                "candidate_bar_closed_at",
            )
            session_root = forward_root / "sessions" / opened_at.date().isoformat()
            evidence_path = (
                session_root
                / "objects"
                / "paper_execution_evidence"
                / f"{identity[7:]}.json"
            )
            if not evidence_path.is_file():
                missing.append(
                    {
                        "rejection_id": rejection_id,
                        "execution_snapshot_sha256": identity,
                        "reason": "EXECUTION_EVIDENCE_OBJECT_MISSING",
                    }
                )
                continue
            if identity not in cache:
                evidence = _semantic_document(
                    evidence_path,
                    schema=EXECUTION_EVIDENCE_SCHEMA,
                )
                fact_identity = str(
                    evidence.get("execution_fact_snapshot_sha256") or ""
                )
                facts = _semantic_document(
                    session_root
                    / "objects"
                    / "paper_execution_facts"
                    / f"{fact_identity[7:]}.json",
                    schema=EXECUTION_FACT_SCHEMA,
                )
                cache[identity] = evidence, facts
            evidence, facts = cache[identity]
            captured_at, bars_by_symbol, _audits_by_symbol = (
                _validate_execution_document_envelope(
                    evidence=evidence,
                    facts=facts,
                    session=opened_at.date(),
                )
            )
            if (
                captured_at < closed_at
            ):
                raise ValueError("execution rejection evidence safety changed")
            pending_ids = evidence.get("pending_intent_ids")
            if (
                not isinstance(pending_ids, list)
                or rejection.get("intent_id") not in pending_ids
            ):
                raise ValueError("rejected intent is absent from execution evidence")
            symbol = str(rejection.get("symbol") or "")
            if not isinstance(symbol_bars := bars_by_symbol.get(symbol), list):
                raise ValueError("execution rejection symbol bars are unavailable")
            _validate_full_a_share_execution_session_grid(
                symbol_bars,
                session=opened_at.date(),
            )
            fact_rows = facts.get("symbols")
            if not isinstance(fact_rows, list):
                raise ValueError("execution rejection facts are malformed")
            matching_facts = tuple(
                value
                for value in fact_rows
                if isinstance(value, Mapping) and value.get("symbol") == symbol
            )
            if len(matching_facts) != 1:
                raise ValueError(
                    "execution rejection does not resolve one instrument fact"
                )
            (
                capture_positions,
                capture_oldest_lots,
                _capture_lots,
            ) = capture_state_cache[identity]
            verified_fact = _validate_execution_fact_against_capture_state(
                fact=matching_facts[0],
                facts=facts,
                symbol=symbol,
                session=opened_at.date(),
                capture_positions=capture_positions,
                capture_oldest_lots=capture_oldest_lots,
            )
            if (
                verified_fact["security_status_complete"] is not True
                or verified_fact["corporate_action_state_complete"] is not True
                or verified_fact["buy_eligible"] is not True
            ):
                raise ValueError(
                    "execution rejection instrument facts and bars disagree"
                )
            for execution_bar in symbol_bars:
                if not isinstance(execution_bar, Mapping):
                    raise ValueError("execution rejection bar is malformed")
                _validate_execution_bar_against_fact(
                    bar=execution_bar,
                    fact=matching_facts[0],
                    verified_fact=verified_fact,
                    symbol=symbol,
                )
            matches = tuple(
                value
                for value in symbol_bars
                if isinstance(value, Mapping)
                and value.get("opened_at") == opened_at.isoformat()
                and value.get("closed_at") == closed_at.isoformat()
            )
            if len(matches) != 1:
                raise ValueError("execution rejection bar is not unique")
            candidate = matches[0]
            intent = intents.get(str(rejection.get("intent_id") or ""))
            if intent is None:
                raise ValueError("execution rejection intent is unavailable")
            earliest = normalize_datetime(
                datetime.fromisoformat(str(intent["earliest_fill_at"])),
                "earliest_fill_at",
            )
            valid_until = normalize_datetime(
                datetime.fromisoformat(str(intent["entry_valid_until"])),
                "entry_valid_until",
            )
            cap = Decimal(str(intent["entry_price_cap"]))
            quantity = int(intent["quantity"])
            terminal_actions: list[
                tuple[
                    datetime,
                    datetime,
                    Literal["FILL", "PRICE_CAP_REJECT"],
                    Decimal,
                ]
            ] = []
            for value in symbol_bars:
                if not isinstance(value, Mapping):
                    continue
                opened = normalize_datetime(
                    datetime.fromisoformat(str(value["opened_at"])),
                    "opened_at",
                )
                closed = normalize_datetime(
                    datetime.fromisoformat(str(value["closed_at"])),
                    "closed_at",
                )
                if (
                    opened >= earliest
                    and closed <= valid_until
                    and value.get("complete") is True
                    and value.get("security_status_complete") is True
                    and value.get("corporate_action_state_complete") is True
                    and value.get("buy_eligible") is True
                    and value.get("suspended") is False
                    and value.get("limit_up_locked") is False
                ):
                    terminal_action = _buy_ohlcv_terminal_execution_action(
                        raw_high=Decimal(str(value.get("high"))),
                        raw_low=Decimal(str(value.get("low"))),
                        raw_volume=Decimal(str(value.get("volume"))),
                        limit_price=cap,
                        quantity=quantity,
                    )
                    if terminal_action is not None:
                        terminal_actions.append(
                            (
                                opened,
                                closed,
                                terminal_action,
                                Decimal(str(value.get("open"))),
                            )
                        )
            reason = rejection.get("reason_code")
            if reason == "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR":
                if (
                    not terminal_actions
                    or min(terminal_actions)[:3]
                    != (opened_at, closed_at, "PRICE_CAP_REJECT")
                    or Decimal(str(candidate.get("low"))) <= cap
                    or Decimal(str(candidate.get("high")))
                    != Decimal(str(rejection.get("candidate_price")))
                ):
                    raise ValueError("price-cap rejection is not first eligible bar")
            elif reason == "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL":
                if terminal_actions or closed_at < valid_until:
                    raise ValueError("TTL rejection has an earlier eligible bar")
            else:
                raise ValueError("execution rejection reason is unsupported")
            verified += 1
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "rejection_id": rejection_id,
                    "execution_snapshot_sha256": identity,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    status = "INVALID" if invalid else "MISSING" if missing else "COMPLETE"
    return {
        "schema": schema,
        "status": status,
        "rejection_count": len(rejections),
        "verified_rejection_count": verified,
        "unique_execution_evidence_count": len(cache),
        "missing_evidence": missing,
        "invalid_evidence": invalid,
        "first_eligible_bar_verified": status == "COMPLETE",
        "price_cap_and_ttl_verified": status == "COMPLETE",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_operations_cancellation_evidence(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
) -> dict[str, object]:
    """Prove each optional-BUY cancellation from immutable QMT evidence.

    Data faults require either an unavailable/failed one-minute execution grid
    or a complete grid whose company-action state is independently proven
    incomplete from the pre-settlement FIFO lot window.  Security-gate
    closures instead require one exact same-session fact whose raw suspension,
    expiry or ST state independently recomputes to ``buy_eligible=False``.  A
    security-gate cancellation may coexist with a complete grid when the same
    symbol still has a persistent SELL to execute.
    """

    cancellations = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "OPERATIONS_CANCEL"
        and isinstance(event.get("payload"), Mapping)
    )
    schema = "chanlun-human-paper-operations-cancellation-evidence-audit"
    if not cancellations:
        return {
            "schema": schema,
            "status": "NO_CANCELLATIONS",
            "cancellation_count": 0,
            "verified_cancellation_count": 0,
            "unique_execution_evidence_count": 0,
            "missing_evidence": [],
            "invalid_evidence": [],
            "data_fault_cancellation_count": 0,
            "execution_fact_incomplete_cancellation_count": 0,
            "execution_fact_incomplete_reason_counts": {
                "SECURITY_STATUS_INCOMPLETE": 0,
                "CORPORATE_ACTION_RECONCILIATION_REQUIRED": 0,
            },
            "security_gate_cancellation_count": 0,
            "security_gate_reason_counts": {
                "SUSPENDED": 0,
                "EXPIRED": 0,
                "ST_BUY_PROHIBITED": 0,
            },
            "optional_buy_operations_cancellation_verified": True,
            "optional_buy_data_fault_cancellation_verified": True,
            "optional_buy_security_gate_cancellation_verified": True,
            "persistent_exit_untouched": True,
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    intents = {
        str(event["payload"].get("intent_id") or ""): event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    }
    cache: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    capture_state_cache: dict[
        str,
        tuple[
            dict[str, int],
            dict[str, date],
            dict[str, list[list[object]]],
        ],
    ] = {}
    missing: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    verified = 0
    data_fault_count = 0
    fact_incomplete_count = 0
    fact_incomplete_reason_counts = {
        "SECURITY_STATUS_INCOMPLETE": 0,
        "CORPORATE_ACTION_RECONCILIATION_REQUIRED": 0,
    }
    security_gate_count = 0
    security_reason_counts = {
        "SUSPENDED": 0,
        "EXPIRED": 0,
        "ST_BUY_PROHIBITED": 0,
    }
    for payload in cancellations:
        cancellation_id = str(payload.get("cancellation_id") or "")
        identity = str(
            payload.get("execution_evidence_snapshot_sha256") or ""
        )
        try:
            cancellation = _operations_cancellation_from_payload(payload)
            data_halt = (
                cancellation.reason_code
                == "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
            )
            security_gate = (
                cancellation.reason_code
                == "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE"
            )
            if _SHA256.fullmatch(identity) is None:
                raise ValueError(
                    "operations cancellation evidence identity is invalid"
                )
            if identity not in capture_state_cache:
                capture_state_cache[identity] = (
                    _execution_evidence_capture_state(
                        events,
                        identity=identity,
                    )
                )
            session_root = (
                forward_root
                / "sessions"
                / cancellation.cancelled_at.date().isoformat()
            )
            evidence_path = (
                session_root
                / "objects"
                / "paper_execution_evidence"
                / f"{identity[7:]}.json"
            )
            if not evidence_path.is_file():
                missing.append(
                    {
                        "cancellation_id": cancellation_id,
                        "execution_evidence_snapshot_sha256": identity,
                        "reason": "EXECUTION_EVIDENCE_OBJECT_MISSING",
                    }
                )
                continue
            if identity not in cache:
                evidence = _semantic_document(
                    evidence_path,
                    schema=EXECUTION_EVIDENCE_SCHEMA,
                )
                fact_identity = str(
                    evidence.get("execution_fact_snapshot_sha256") or ""
                )
                if _SHA256.fullmatch(fact_identity) is None:
                    raise ValueError("execution fact evidence identity is invalid")
                fact_path = (
                    session_root
                    / "objects"
                    / "paper_execution_facts"
                    / f"{fact_identity[7:]}.json"
                )
                if not fact_path.is_file():
                    missing.append(
                        {
                            "cancellation_id": cancellation_id,
                            "execution_evidence_snapshot_sha256": identity,
                            "reason": "EXECUTION_FACT_OBJECT_MISSING",
                        }
                    )
                    continue
                facts = _semantic_document(
                    fact_path,
                    schema=EXECUTION_FACT_SCHEMA,
                )
                cache[identity] = evidence, facts
            evidence, facts = cache[identity]
            captured_at, bars_by_symbol, audits_by_symbol = (
                _validate_execution_document_envelope(
                    evidence=evidence,
                    facts=facts,
                    session=cancellation.cancelled_at.date(),
                )
            )
            fact_identity = str(
                evidence.get("execution_fact_snapshot_sha256") or ""
            )
            if (
                captured_at != cancellation.cancelled_at
                or evidence.get("session")
                != cancellation.cancelled_at.date().isoformat()
                or cancellation.execution_fact_snapshot_sha256
                != fact_identity
            ):
                raise ValueError(
                    "operations cancellation evidence safety boundary changed"
                )
            pending_ids = evidence.get("pending_intent_ids")
            if (
                not isinstance(pending_ids, list)
                or cancellation.intent_id not in pending_ids
            ):
                raise ValueError(
                    "cancelled intent is absent from execution evidence"
                )
            matching_audits = tuple(
                value
                for symbol, value in audits_by_symbol.items()
                if symbol == cancellation.symbol
            )
            if (
                len(matching_audits) != 1
                or matching_audits[0].get("status")
                != cancellation.grid_status
            ):
                raise ValueError(
                    "operations cancellation grid failure is not unique"
                )
            symbol_bars = bars_by_symbol.get(cancellation.symbol)
            if data_halt:
                normalized_rows = matching_audits[0].get("normalized_row_count")
                if cancellation.grid_status == "COMPLETE":
                    if (
                        not isinstance(symbol_bars, list)
                        or len(symbol_bars) != 240
                        or normalized_rows != 240
                    ):
                        raise ValueError(
                            "complete data-fault grid does not contain 240 bars"
                        )
                elif symbol_bars != [] or normalized_rows != 0:
                    raise ValueError(
                        "failed execution grid unexpectedly contains usable bars"
                    )
            if security_gate:
                normalized_rows = matching_audits[0].get("normalized_row_count")
                if cancellation.grid_status == "COMPLETE":
                    if (
                        not isinstance(symbol_bars, list)
                        or len(symbol_bars) != 240
                        or normalized_rows != 240
                    ):
                        raise ValueError(
                            "complete security-gate grid does not contain 240 bars"
                        )
                elif symbol_bars != [] or normalized_rows != 0:
                    raise ValueError(
                        "non-complete security-gate grid unexpectedly contains bars"
                    )
            intent = intents.get(cancellation.intent_id)
            if (
                not isinstance(intent, Mapping)
                or intent.get("side") != "BUY"
                or intent.get("symbol") != cancellation.symbol
                or intent.get("candidate_id") != cancellation.candidate_id
                or intent.get("signal_lifecycle_id")
                != cancellation.signal_lifecycle_id
            ):
                raise ValueError(
                    "operations cancellation intent provenance changed"
                )
            fact_rows = facts.get("symbols")
            if not isinstance(fact_rows, list):
                raise ValueError("execution fact symbol rows are invalid")
            matching_facts = tuple(
                value
                for value in fact_rows
                if isinstance(value, Mapping)
                and value.get("symbol") == cancellation.symbol
            )
            fact_incomplete = False
            if cancellation.grid_status == "EXECUTION_FACT_MISSING_FAIL_CLOSED":
                if security_gate:
                    raise ValueError(
                        "security-gate cancellation requires an execution fact"
                    )
                if matching_facts:
                    raise ValueError(
                        "missing-fact cancellation unexpectedly has a fact row"
                    )
            else:
                if len(matching_facts) != 1:
                    raise ValueError(
                        "operations cancellation lacks one exact fact row"
                    )
                fact = matching_facts[0]
                (
                    capture_positions,
                    capture_oldest_lots,
                    _capture_lots,
                ) = capture_state_cache[identity]
                verified_fact = _validate_execution_fact_against_capture_state(
                    fact=fact,
                    facts=facts,
                    symbol=cancellation.symbol,
                    session=cancellation.cancelled_at.date(),
                    capture_positions=capture_positions,
                    capture_oldest_lots=capture_oldest_lots,
                )
                if isinstance(symbol_bars, list):
                    for execution_bar in symbol_bars:
                        if not isinstance(execution_bar, Mapping):
                            raise ValueError(
                                "operations cancellation bar is malformed"
                            )
                        _validate_execution_bar_against_fact(
                            bar=execution_bar,
                            fact=fact,
                            verified_fact=verified_fact,
                            symbol=cancellation.symbol,
                        )
                suspended = bool(fact["suspended"])
                expired = bool(fact["expired"])
                is_st = bool(fact["is_st"])
                expected_buy_eligible = bool(verified_fact["buy_eligible"])
                security_status_complete = bool(
                    verified_fact["security_status_complete"]
                )
                corporate_action_state_complete = bool(
                    verified_fact["corporate_action_state_complete"]
                )
                fact_incomplete = (
                    not security_status_complete
                    or not corporate_action_state_complete
                )
                if (
                    data_halt
                    and security_status_complete
                    and not expected_buy_eligible
                ):
                    raise ValueError(
                        "data-fault cancellation actually belongs to security gate"
                    )
                if security_gate and (
                    not security_status_complete or expected_buy_eligible
                ):
                    raise ValueError(
                        "security-gate cancellation has an eligible BUY fact"
                    )
                if data_halt and (
                    cancellation.grid_status == "COMPLETE"
                    and not fact_incomplete
                ):
                    raise ValueError(
                        "complete-grid data cancellation lacks an incomplete fact"
                    )
                if data_halt and fact_incomplete:
                    fact_incomplete_count += 1
                    if not security_status_complete:
                        fact_incomplete_reason_counts[
                            "SECURITY_STATUS_INCOMPLETE"
                        ] += 1
                    if not corporate_action_state_complete:
                        fact_incomplete_reason_counts[
                            "CORPORATE_ACTION_RECONCILIATION_REQUIRED"
                        ] += 1
                if security_gate:
                    if suspended:
                        security_reason_counts["SUSPENDED"] += 1
                    if expired:
                        security_reason_counts["EXPIRED"] += 1
                    if is_st:
                        security_reason_counts["ST_BUY_PROHIBITED"] += 1
            if data_halt:
                data_fault_count += 1
            elif security_gate:
                security_gate_count += 1
            verified += 1
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "cancellation_id": cancellation_id,
                    "execution_evidence_snapshot_sha256": identity,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    status = "INVALID" if invalid else "MISSING" if missing else "COMPLETE"
    return {
        "schema": schema,
        "status": status,
        "cancellation_count": len(cancellations),
        "verified_cancellation_count": verified,
        "unique_execution_evidence_count": len(cache),
        "missing_evidence": missing,
        "invalid_evidence": invalid,
        "data_fault_cancellation_count": data_fault_count,
        "execution_fact_incomplete_cancellation_count": fact_incomplete_count,
        "execution_fact_incomplete_reason_counts": fact_incomplete_reason_counts,
        "security_gate_cancellation_count": security_gate_count,
        "security_gate_reason_counts": security_reason_counts,
        "optional_buy_operations_cancellation_verified": status == "COMPLETE",
        "optional_buy_data_fault_cancellation_verified": status == "COMPLETE",
        "optional_buy_security_gate_cancellation_verified": status == "COMPLETE",
        "persistent_exit_untouched": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def audit_human_paper_portfolio_rejection_evidence(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
) -> dict[str, object]:
    """Prove each portfolio rejection against exact immutable 1m facts.

    The immutable rejection proves the frozen cash equation.  This audit also
    proves that its price came from the first eligible completed 1m bar in the
    same immutable execution snapshot.  Earlier sessions remain covered by
    the separate pending-continuity audit.
    """

    rejections = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "PORTFOLIO_REJECT"
        and isinstance(event.get("payload"), Mapping)
    )
    schema = "chanlun-human-paper-portfolio-rejection-evidence-audit"
    if not rejections:
        return {
            "schema": schema,
            "status": "NO_REJECTIONS",
            "rejection_count": 0,
            "verified_rejection_count": 0,
            "unique_execution_evidence_count": 0,
            "missing_evidence": [],
            "invalid_evidence": [],
            "first_eligible_bar_verified": True,
            "synchronous_position_marks_verified": True,
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }

    # Reuse the exact-fill verifier for the shared immutable market/fact
    # contract.  These rows exist only in memory and never enter the ledger.
    synthetic_fills = tuple(
        {
            "kind": "FILL",
            "payload": {
                "fill_id": str(value.get("rejection_id") or ""),
                "intent_id": value.get("intent_id"),
                "symbol": value.get("symbol"),
                "side": "BUY",
                "quantity": value.get("quantity"),
                "price": value.get("candidate_price"),
                "filled_at": value.get("candidate_bar_closed_at"),
                "source_bar_closed_at": value.get("candidate_bar_closed_at"),
                "execution_snapshot_sha256": value.get(
                    "execution_snapshot_sha256"
                ),
            },
        }
        for value in rejections
    )
    # The shared verifier also proves the BUY boundary against the originating
    # intent.  Keep those immutable intent rows in the synthetic audit stream;
    # passing only the fabricated fill rows would make every legitimate
    # capital rejection fail with ``fill intent is unavailable``.
    intent_events = tuple(
        event
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    )
    shared = audit_human_paper_execution_evidence(
        intent_events + synthetic_fills,
        forward_root=forward_root,
        _capture_state_events=events,
    )

    def rejection_rows(name: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for raw in shared.get(name, []):
            if not isinstance(raw, Mapping):
                continue
            row = {str(key): str(value) for key, value in raw.items()}
            row["rejection_id"] = row.pop("fill_id", "")
            rows.append(row)
        return rows

    missing = rejection_rows("missing_evidence")
    invalid = rejection_rows("invalid_evidence")
    invalid_ids = {value["rejection_id"] for value in invalid}
    missing_ids = {value["rejection_id"] for value in missing}
    intents = {
        str(event["payload"].get("intent_id") or ""): event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
    }

    # The shared verifier proves the selected bar.  This pass proves it was
    # not selected after an earlier eligible 1m bar in that same snapshot and
    # that every open-position mark came from the exact same completed 1m
    # interval and immutable instrument-fact snapshot.
    for rejection in rejections:
        rejection_id = str(rejection.get("rejection_id") or "")
        if rejection_id in invalid_ids | missing_ids:
            continue
        identity = str(rejection.get("execution_snapshot_sha256") or "")
        try:
            opened_at = normalize_datetime(
                datetime.fromisoformat(str(rejection["candidate_bar_opened_at"])),
                "candidate_bar_opened_at",
            )
            closed_at = normalize_datetime(
                datetime.fromisoformat(str(rejection["candidate_bar_closed_at"])),
                "candidate_bar_closed_at",
            )
            intent = intents.get(str(rejection.get("intent_id") or ""))
            if intent is None:
                raise ValueError("capital rejection intent is unavailable")
            earliest_fill_at = normalize_datetime(
                datetime.fromisoformat(str(intent["earliest_fill_at"])),
                "earliest_fill_at",
            )
            valid_until = normalize_datetime(
                datetime.fromisoformat(str(intent["entry_valid_until"])),
                "entry_valid_until",
            )
            price_cap = Decimal(str(intent["entry_price_cap"]))
            evidence_path = (
                forward_root
                / "sessions"
                / opened_at.date().isoformat()
                / "objects"
                / "paper_execution_evidence"
                / f"{identity[7:]}.json"
            )
            evidence = _semantic_document(
                evidence_path,
                schema=EXECUTION_EVIDENCE_SCHEMA,
            )
            fact_identity = str(
                evidence.get("execution_fact_snapshot_sha256") or ""
            )
            if _SHA256.fullmatch(fact_identity) is None:
                raise ValueError("execution fact snapshot identity is invalid")
            facts = _semantic_document(
                forward_root
                / "sessions"
                / opened_at.date().isoformat()
                / "objects"
                / "paper_execution_facts"
                / f"{fact_identity[7:]}.json",
                schema=EXECUTION_FACT_SCHEMA,
            )
            _captured_at, bars_by_symbol, _audits_by_symbol = (
                _validate_execution_document_envelope(
                    evidence=evidence,
                    facts=facts,
                    session=opened_at.date(),
                )
            )
            symbol_bars = bars_by_symbol.get(str(rejection.get("symbol") or ""))
            if not isinstance(symbol_bars, list):
                raise ValueError("capital rejection symbol has no execution bars")
            quantity = int(rejection.get("quantity") or 0)
            eligible: list[tuple[datetime, datetime]] = []
            for bar in symbol_bars:
                if not isinstance(bar, Mapping):
                    continue
                bar_opened_at = normalize_datetime(
                    datetime.fromisoformat(str(bar["opened_at"])),
                    "opened_at",
                )
                bar_closed_at = normalize_datetime(
                    datetime.fromisoformat(str(bar["closed_at"])),
                    "closed_at",
                )
                if (
                    bar_opened_at >= earliest_fill_at
                    and bar_closed_at <= valid_until
                    and bar.get("complete") is True
                    and bar.get("security_status_complete") is True
                    and bar.get("corporate_action_state_complete") is True
                    and bar.get("buy_eligible") is True
                    and bar.get("suspended") is False
                    and bar.get("limit_up_locked") is False
                    and Decimal(str(bar.get("volume"))) >= quantity
                    and _buy_ohlcv_terminal_execution_action(
                        raw_high=Decimal(str(bar.get("high"))),
                        raw_low=Decimal(str(bar.get("low"))),
                        raw_volume=Decimal(str(bar.get("volume"))),
                        limit_price=price_cap,
                        quantity=quantity,
                    )
                    == "FILL"
                ):
                    eligible.append((bar_opened_at, bar_closed_at))
            if not eligible or min(eligible) != (opened_at, closed_at):
                raise ValueError(
                    "capital rejection was not decided at the first eligible 1m bar"
                )
            (
                capture_positions,
                capture_oldest_lots,
                _capture_lots,
            ) = _execution_evidence_capture_state(events, identity=identity)
            raw_marks = rejection.get("position_marks")
            if not isinstance(raw_marks, list):
                raise ValueError("portfolio rejection position marks are missing")
            _verify_synchronous_position_marks(
                raw_marks=raw_marks,
                bars_by_symbol=bars_by_symbol,
                facts=facts,
                capture_positions=capture_positions,
                capture_oldest_lots=capture_oldest_lots,
                opened_at=opened_at,
                closed_at=closed_at,
            )
            mark_symbols = {
                str(mark.get("symbol") or "")
                for mark in raw_marks
                if isinstance(mark, Mapping)
            }
            if mark_symbols != set(capture_positions):
                raise ValueError("portfolio rejection mark coverage changed")
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "rejection_id": rejection_id,
                    "execution_snapshot_sha256": identity,
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            invalid_ids.add(rejection_id)

    verified = len(rejections) - len(invalid_ids | missing_ids)
    status = "COMPLETE"
    if invalid:
        status = "INVALID"
    elif missing:
        status = "MISSING"
    return {
        "schema": schema,
        "status": status,
        "rejection_count": len(rejections),
        "verified_rejection_count": verified,
        "unique_execution_evidence_count": int(
            shared.get("unique_execution_evidence_count") or 0
        ),
        "missing_evidence": missing,
        "invalid_evidence": invalid,
        "first_eligible_bar_verified": status == "COMPLETE",
        "synchronous_position_marks_verified": status == "COMPLETE",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def _continuity_evidence_for_intent(
    *,
    forward_root: Path,
    session: date,
    intent: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
) -> tuple[str | None, str]:
    """Resolve evidence proving why one intent legitimately stayed pending."""

    intent_id = str(intent.get("intent_id") or "")
    symbol = str(intent.get("symbol") or "")
    side = str(intent.get("side") or "")
    session_root = forward_root / "sessions" / session.isoformat()
    evidence_root = session_root / "objects" / "paper_execution_evidence"
    if not evidence_root.is_dir():
        return None, "FULL_SESSION_EXECUTION_EVIDENCE_MISSING"
    close_at = datetime.combine(session, time(15), tzinfo=None)
    terminal_outcome_proven = False
    for path in sorted(evidence_root.glob("*.json")):
        try:
            evidence = _semantic_document(path, schema=EXECUTION_EVIDENCE_SCHEMA)
            fact_identity = str(
                evidence.get("execution_fact_snapshot_sha256") or ""
            )
            if _SHA256.fullmatch(fact_identity) is None:
                continue
            fact_path = (
                session_root
                / "objects"
                / "paper_execution_facts"
                / f"{fact_identity[7:]}.json"
            )
            facts = _semantic_document(fact_path, schema=EXECUTION_FACT_SCHEMA)
            captured_at, bars_by_symbol, audits_by_symbol = (
                _validate_execution_document_envelope(
                    evidence=evidence,
                    facts=facts,
                    session=session,
                )
            )
            if captured_at.date() != session or captured_at.replace(
                tzinfo=None
            ) < close_at:
                continue
            (
                capture_positions,
                capture_oldest_lots,
                capture_lots,
            ) = _unreferenced_execution_document_capture_state(
                events,
                forward_root=forward_root,
                identity=str(evidence["content_sha256"]),
                captured_at=captured_at,
                intent_id=intent_id,
            )
            pending_ids = evidence.get("pending_intent_ids")
            if not isinstance(pending_ids, list) or intent_id not in pending_ids:
                continue
            bars = bars_by_symbol.get(symbol)
            if not isinstance(bars, list):
                continue
            rows = facts.get("symbols")
            if not isinstance(rows, list):
                continue
            matches = tuple(
                row
                for row in rows
                if isinstance(row, Mapping) and row.get("symbol") == symbol
            )
            audit = audits_by_symbol.get(symbol)
            if not isinstance(audit, Mapping):
                continue
            if not matches:
                if (
                    side == "SELL"
                    and audit.get("status")
                    == "EXECUTION_FACT_MISSING_FAIL_CLOSED"
                    and not bars
                ):
                    return str(evidence["content_sha256"]), ""
                if side == "BUY":
                    terminal_outcome_proven = True
                continue
            if len(matches) != 1:
                continue
            fact = matches[0]
            verified_fact = _validate_execution_fact_against_capture_state(
                fact=fact,
                facts=facts,
                symbol=symbol,
                session=session,
                capture_positions=capture_positions,
                capture_oldest_lots=capture_oldest_lots,
            )
            if audit.get("status") == "COMPLETE":
                _validate_full_a_share_execution_session_grid(
                    bars,
                    session=session,
                )
                for execution_bar in bars:
                    if not isinstance(execution_bar, Mapping):
                        raise ValueError("continuity execution bar is malformed")
                    _validate_execution_bar_against_fact(
                        bar=execution_bar,
                        fact=fact,
                        verified_fact=verified_fact,
                        symbol=symbol,
                    )
            elif bars:
                continue

            if side == "SELL":
                if (
                    verified_fact["sell_eligible"] is not True
                    or verified_fact["corporate_action_state_complete"]
                    is not True
                    or audit.get("status") != "COMPLETE"
                ):
                    return str(evidence["content_sha256"]), ""
                quantity = int(intent.get("quantity") or 0)
                executable = any(
                    isinstance(bar, Mapping)
                    and bar.get("complete") is True
                    and bar.get("security_status_complete") is True
                    and bar.get("corporate_action_state_complete") is True
                    and bar.get("sell_eligible") is True
                    and bar.get("suspended") is False
                    and bar.get("limit_down_locked") is False
                    and Decimal(str(bar.get("volume"))) >= quantity
                    and _sellable_quantity(
                        capture_lots.get(symbol, ()),
                        normalize_datetime(
                            datetime.fromisoformat(str(bar["opened_at"])),
                            "opened_at",
                        ),
                    )
                    >= quantity
                    for bar in bars
                )
                if executable:
                    terminal_outcome_proven = True
                    continue
                return str(evidence["content_sha256"]), ""

            if side != "BUY":
                continue
            valid_until_raw = intent.get("entry_valid_until")
            price_cap_raw = intent.get("entry_price_cap")
            if valid_until_raw is None or price_cap_raw is None:
                raise ValueError("pending BUY intent lacks its execution boundary")
            if (
                verified_fact["buy_eligible"] is not True
                or verified_fact["corporate_action_state_complete"]
                is not True
                or audit.get("status") != "COMPLETE"
            ):
                # Optional BUY data/security failures are terminal operations
                # cancellations.  Remaining pending is therefore a gap.
                terminal_outcome_proven = True
                continue
            earliest = normalize_datetime(
                datetime.fromisoformat(str(intent["earliest_fill_at"])),
                "earliest_fill_at",
            )
            valid_until = normalize_datetime(
                datetime.fromisoformat(str(valid_until_raw)),
                "entry_valid_until",
            )
            quantity = int(intent.get("quantity") or 0)
            candidates = tuple(
                sorted(
                    (
                        (
                            bar,
                            _buy_ohlcv_terminal_execution_action(
                                raw_high=Decimal(str(bar.get("high"))),
                                raw_low=Decimal(str(bar.get("low"))),
                                raw_volume=Decimal(str(bar.get("volume"))),
                                limit_price=Decimal(str(price_cap_raw)),
                                quantity=quantity,
                            ),
                        )
                        for bar in bars
                        if isinstance(bar, Mapping)
                        and normalize_datetime(
                            datetime.fromisoformat(str(bar["opened_at"])),
                            "opened_at",
                        )
                        >= earliest
                        and normalize_datetime(
                            datetime.fromisoformat(str(bar["closed_at"])),
                            "closed_at",
                        )
                        <= valid_until
                        and bar.get("complete") is True
                        and bar.get("security_status_complete") is True
                        and bar.get("corporate_action_state_complete") is True
                        and bar.get("buy_eligible") is True
                        and bar.get("suspended") is False
                        and bar.get("limit_up_locked") is False
                        and _buy_ohlcv_terminal_execution_action(
                            raw_high=Decimal(str(bar.get("high"))),
                            raw_low=Decimal(str(bar.get("low"))),
                            raw_volume=Decimal(str(bar.get("volume"))),
                            limit_price=Decimal(str(price_cap_raw)),
                            quantity=quantity,
                        )
                        is not None
                    ),
                    key=lambda value: str(value[0]["opened_at"]),
                )
            )
            if not candidates:
                expiry_evidence = any(
                    isinstance(bar, Mapping)
                    and normalize_datetime(
                        datetime.fromisoformat(str(bar["closed_at"])),
                        "closed_at",
                    )
                    >= valid_until
                    and bar.get("complete") is True
                    and bar.get("security_status_complete") is True
                    and bar.get("corporate_action_state_complete") is True
                    for bar in bars
                )
                if expiry_evidence:
                    terminal_outcome_proven = True
                    continue
                return str(evidence["content_sha256"]), ""

            first_candidate, first_action = candidates[0]
            candidate_opened_at = normalize_datetime(
                datetime.fromisoformat(str(first_candidate["opened_at"])),
                "opened_at",
            )
            candidate_closed_at = normalize_datetime(
                datetime.fromisoformat(str(first_candidate["closed_at"])),
                "closed_at",
            )
            if first_action == "PRICE_CAP_REJECT":
                terminal_outcome_proven = True
                continue
            marks_resolved = True
            for position_symbol in capture_positions:
                position_bars = bars_by_symbol.get(position_symbol)
                position_audit = audits_by_symbol.get(position_symbol)
                position_facts = tuple(
                    row
                    for row in rows
                    if isinstance(row, Mapping)
                    and row.get("symbol") == position_symbol
                )
                if (
                    not isinstance(position_bars, list)
                    or not isinstance(position_audit, Mapping)
                    or position_audit.get("status") != "COMPLETE"
                    or len(position_facts) != 1
                ):
                    marks_resolved = False
                    break
                position_fact = position_facts[0]
                verified_position_fact = (
                    _validate_execution_fact_against_capture_state(
                        fact=position_fact,
                        facts=facts,
                        symbol=position_symbol,
                        session=session,
                        capture_positions=capture_positions,
                        capture_oldest_lots=capture_oldest_lots,
                    )
                )
                position_matches = tuple(
                    bar
                    for bar in position_bars
                    if isinstance(bar, Mapping)
                    and bar.get("opened_at") == candidate_opened_at.isoformat()
                    and bar.get("closed_at") == candidate_closed_at.isoformat()
                )
                if len(position_matches) != 1:
                    marks_resolved = False
                    break
                position_bar = position_matches[0]
                _validate_execution_bar_against_fact(
                    bar=position_bar,
                    fact=position_fact,
                    verified_fact=verified_position_fact,
                    symbol=position_symbol,
                )
                if (
                    position_bar.get("complete") is not True
                    or position_bar.get("suspended") is not False
                    or position_bar.get("security_status_complete") is not True
                    or position_bar.get("corporate_action_state_complete")
                    is not True
                ):
                    marks_resolved = False
                    break
            if not marks_resolved:
                return str(evidence["content_sha256"]), ""
            terminal_outcome_proven = True
        except (InvalidOperation, KeyError, OSError, TypeError, ValueError):
            continue
    return (
        None,
        (
            "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
            if terminal_outcome_proven
            else "FULL_SESSION_EXECUTION_EVIDENCE_MISSING"
        ),
    )


def audit_human_paper_pending_continuity(
    events: Sequence[Mapping[str, object]],
    *,
    forward_root: Path,
    current_session: date,
    trading_sessions: Sequence[date],
) -> dict[str, object]:
    """Fail closed when a pending intent skipped a possible execution session."""

    terminal_ids = human_paper_terminal_intent_ids(events)
    pending = tuple(
        event["payload"]
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("status") == "PENDING"
        and str(event["payload"].get("intent_id")) not in terminal_ids
    )
    if not pending:
        return {
            "status": "NO_PENDING_INTENTS",
            "pending_intent_count": 0,
            "pending_intent_ids": [],
            "required_intent_session_count": 0,
            "covered_intent_session_count": 0,
            "gap_intent_count": 0,
            "gap_intent_ids": [],
            "gaps": [],
            "execution_evidence_sha256s": [],
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    sessions = tuple(sorted(set(trading_sessions)))
    if sessions != tuple(trading_sessions) or any(
        value >= current_session for value in sessions
    ):
        raise ValueError("pending continuity trading sessions are invalid")
    required_count = 0
    covered_count = 0
    gaps: list[dict[str, str]] = []
    evidence_ids: set[str] = set()
    for intent in pending:
        intent_id = str(intent["intent_id"])
        symbol = str(intent["symbol"])
        earliest = normalize_datetime(
            datetime.fromisoformat(str(intent["earliest_fill_at"])),
            "earliest_fill_at",
        )
        valid_until = (
            normalize_datetime(
                datetime.fromisoformat(str(intent["entry_valid_until"])),
                "entry_valid_until",
            )
            if intent.get("side") == "BUY"
            and intent.get("entry_valid_until") is not None
            else None
        )
        required = tuple(
            session
            for session in sessions
            if (
                valid_until is None or session <= valid_until.date()
            )
            and (
                session > earliest.date()
                or (
                    session == earliest.date()
                    and earliest.timetz().replace(tzinfo=None) < time(14, 59)
                )
            )
        )
        for session in required:
            required_count += 1
            evidence_id, gap_reason = _continuity_evidence_for_intent(
                forward_root=forward_root,
                session=session,
                intent=intent,
                events=events,
            )
            if evidence_id is None:
                gaps.append(
                    {
                        "intent_id": intent_id,
                        "symbol": symbol,
                        "session": session.isoformat(),
                        "reason": gap_reason,
                    }
                )
            else:
                covered_count += 1
                evidence_ids.add(evidence_id)
    gap_ids = sorted({value["intent_id"] for value in gaps})
    return {
        "status": "CAUSAL_GAPS" if gaps else "COMPLETE",
        "pending_intent_count": len(pending),
        "pending_intent_ids": sorted(str(value["intent_id"]) for value in pending),
        "required_intent_session_count": required_count,
        "covered_intent_session_count": covered_count,
        "gap_intent_count": len(gap_ids),
        "gap_intent_ids": gap_ids,
        "gaps": gaps,
        "execution_evidence_sha256s": sorted(evidence_ids),
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def latest_human_paper_pending_continuity(
    paper_events: Sequence[Mapping[str, object]],
    forward_events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Expose only a continuity receipt for the current immutable pending set."""

    terminal_ids = human_paper_terminal_intent_ids(paper_events)
    pending_ids = sorted(
        str(event["payload"]["intent_id"])
        for event in paper_events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("status") == "PENDING"
        and str(event["payload"].get("intent_id")) not in terminal_ids
    )
    if not pending_ids:
        return {
            "status": "NO_PENDING_INTENTS",
            "pending_intent_count": 0,
            "pending_intent_ids": [],
            "gap_intent_count": 0,
            "gap_intent_ids": [],
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    for event in reversed(tuple(forward_events)):
        evidence = event.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        settlement = evidence.get("human_paper_settlement")
        if not isinstance(settlement, Mapping):
            continue
        continuity = settlement.get("pending_continuity")
        if not isinstance(continuity, Mapping):
            continue
        if (
            continuity.get("status") not in {"COMPLETE", "CAUSAL_GAPS"}
            or continuity.get("pending_intent_ids") != pending_ids
            or continuity.get("tick_data_used") is not False
            or continuity.get("broker_transport_available") is not False
            or continuity.get("live_status") != "LIVE_DISABLED"
        ):
            continue
        return dict(continuity)
    return {
        "status": "UNPROVEN",
        "pending_intent_count": len(pending_ids),
        "pending_intent_ids": pending_ids,
        "gap_intent_count": None,
        "gap_intent_ids": [],
        "reason_codes": ["CURRENT_PENDING_SET_HAS_NO_CONTINUITY_RECEIPT"],
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


__all__ = (
    "ENTRY_SELECTION_EVIDENCE_SCHEMA",
    "ENTRY_SELECTION_EXACT_ATTESTATION",
    "EXECUTION_EVIDENCE_SCHEMA",
    "HumanPaperDecisionPositionMark",
    "HumanPaperCancellation",
    "HumanPaperExecutionRejection",
    "HumanPaperFill",
    "HumanPaperEntrySelectionEvidence",
    "HumanPaperIntent",
    "HumanPaperMinuteBar",
    "HumanPaperOperationsCancellation",
    "HumanPaperPortfolioFill",
    "HumanPaperPortfolioRejection",
    "PAPER_CONTRACT_ID",
    "audit_human_paper_portfolio_rejection_evidence",
    "audit_human_paper_entry_boundary_attestations",
    "audit_human_paper_entry_selection_attestations",
    "audit_human_paper_entry_selection_source_bindings",
    "audit_human_paper_execution_evidence",
    "audit_human_paper_execution_rejection_evidence",
    "audit_human_paper_operations_cancellation_evidence",
    "audit_human_paper_pending_continuity",
    "append_human_paper_intent",
    "build_human_paper_intent",
    "human_paper_cancelled_intent_ids",
    "human_paper_consumed_signal_lifecycle_ids",
    "human_paper_execution_rejected_intent_ids",
    "human_paper_event_effective_at",
    "human_paper_ledger_content_sha256",
    "human_paper_ledger_prefix_for_identity",
    "human_paper_portfolio_rejected_intent_ids",
    "human_paper_pending_sell_quantities",
    "human_paper_oldest_open_lot_sessions",
    "human_paper_position_quantities",
    "human_paper_terminal_intent_ids",
    "latest_human_paper_pending_continuity",
    "load_human_paper_execution_capture",
    "load_human_paper_ledger",
    "parse_human_paper_entry_selection_evidence",
    "reconcile_human_paper_feedback",
    "settle_human_paper_intents_with_portfolio_controls",
    "validate_a_share_completed_one_minute_interval",
)
