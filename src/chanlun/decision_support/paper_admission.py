from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Protocol, runtime_checkable

from .event_service import DecisionEventService, EventView
from .event_store import (
    StoredLLMReview,
    StoredPaperAdmissionAuthorization,
    StoredTransition,
)
from .evidence import EvidencePacket
from .fingerprints import normalize_datetime, sha256_json
from .models import DecisionEvent, EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .paper_adapter import (
    ConfirmedEventPaperAdapter,
    InMemoryPaperLedger,
    PaperAdapterEligibilityError,
    PaperBar,
    PaperBarCursor,
    PaperFill,
    PaperFeeSchedule,
    PaperIntent,
    PaperLedgerConflictError,
    PaperLedgerIntegrityError,
    PaperLedgerPort,
    PaperLedgerState,
    PaperLot,
    reconcile_paper_ledger,
)
from .risk_snapshot import RiskSnapshot


LIVE_ORDER_CAPABILITY = False
_BUY_SIGNALS = frozenset({"1buy", "2buy", "3buy", "3buy_nest", "1buy_nest"})
_STATE_SCHEMA_VERSION = 3
_CASH_QUANTUM = Decimal("0.01")
_PAPER_EXECUTION_ALGORITHM_VERSION = (
    "paper-next-tradable-bar-close-limit-v3"
)


class TrustedPaperAdmissionError(PaperAdapterEligibilityError):
    """A persisted trust binding failed; no paper intent may be created."""


class _PaperBuyFillAuthorityChanged(TrustedPaperAdmissionError):
    """A staged buy lost authoritative eligibility before ledger commit."""


@runtime_checkable
class TrustedPaperBarSource(Protocol):
    """Trust root that resolves bars bound to an audited paper cycle."""

    def attest_cycle_bar(
        self,
        bar_id: str,
        *,
        allow_current_started: bool = False,
    ) -> PaperBar | None: ...


@runtime_checkable
class ManualCheckStore(Protocol):
    """Minimal read-only audit binding required by paper admission."""

    def get_for_event(self, event_id: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    initial_cash: Decimal
    cash_balance: Decimal
    reserved_buying_power: Decimal
    available_buying_power: Decimal
    positions_cost: Decimal
    cost_basis_equity: Decimal


@dataclass(frozen=True, slots=True)
class _PaperAdmissionBinding:
    authorization_id: str
    payload_fingerprint: str
    authorized_at: datetime


@dataclass(frozen=True, slots=True)
class _PaperExecutionPolicyBinding:
    fee_schedule_fingerprint: str
    execution_policy_fingerprint: str
    policy_json: str


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_fingerprint(value: object, field_name: str) -> str:
    value = _required_text(value, field_name)
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite Decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field_name} must be a {qualifier}finite Decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _policy_json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Mapping):
        return {
            _required_text(key, "policy key"): _policy_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_policy_json_value(item) for item in value]
    raise TypeError(f"unsupported paper policy value: {type(value).__name__}")


def _execution_policy_document(
    fee_schedule: PaperFeeSchedule,
    buying_power_buffer_rate: Decimal,
) -> tuple[dict[str, object], str]:
    document = {
        "schema_version": 1,
        "algorithm_version": _PAPER_EXECUTION_ALGORITHM_VERSION,
        "fee_schedule_fingerprint": fee_schedule.fingerprint,
        "fee_schedule": _policy_json_value(fee_schedule.policy_payload()),
        "buying_power_buffer_rate": _decimal_text(
            buying_power_buffer_rate
        ),
        "admission_algorithm": "atomic-inbox-reservation-intent-cas",
        "fill_algorithm": (
            "completed-next-tradable-bar-close-after-admission-"
            "with-daily-price-limit-clamp"
        ),
    }
    return document, sha256_json(document)


def paper_execution_policy_fingerprint(
    fee_schedule: PaperFeeSchedule,
    buying_power_buffer_rate: Decimal = Decimal("0.01"),
) -> str:
    if not isinstance(fee_schedule, PaperFeeSchedule):
        raise TypeError("fee_schedule must be PaperFeeSchedule")
    buffer_rate = _decimal(
        buying_power_buffer_rate,
        "buying_power_buffer_rate",
    )
    if buffer_rate < 0 or buffer_rate >= 1:
        raise ValueError("buying_power_buffer_rate must be in [0, 1)")
    return _execution_policy_document(fee_schedule, buffer_rate)[1]


def _execution_policy_json(document: Mapping[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _integer(value: object, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime") from exc
    return normalize_datetime(parsed, field_name)


def _object(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    field_name: str,
) -> None:
    if frozenset(payload) != expected:
        raise ValueError(f"{field_name} fields are invalid")


def _intent_payload(intent: PaperIntent) -> dict[str, object]:
    return {
        "event_id": intent.event_id,
        "event_data_fingerprint": intent.event_data_fingerprint,
        "review_id": intent.review_id,
        "risk_snapshot_id": intent.risk_snapshot_id,
        "admission_authorization_id": intent.admission_authorization_id,
        "admission_payload_fingerprint": intent.admission_payload_fingerprint,
        "admitted_at": intent.admitted_at.isoformat(),
        "risk_expires_at": intent.risk_expires_at.isoformat(),
        "entry_event_id": intent.entry_event_id,
        "code": intent.code,
        "side": intent.side,
        "risk_shares": intent.risk_shares,
        "requested_shares": intent.requested_shares,
        "remaining_shares": intent.remaining_shares,
        "signal_bar_id": intent.signal_bar_id,
        "signal_at": intent.signal_at.isoformat(),
        "limit_pct": _decimal_text(intent.limit_pct),
        "status": intent.status,
        "reason": intent.reason,
        "fee_schedule_fingerprint": intent.fee_schedule_fingerprint,
        "execution_policy_fingerprint": (
            intent.execution_policy_fingerprint
        ),
    }


def _fill_payload(fill: PaperFill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "event_id": fill.event_id,
        "entry_event_id": fill.entry_event_id,
        "review_id": fill.review_id,
        "risk_snapshot_id": fill.risk_snapshot_id,
        "code": fill.code,
        "side": fill.side,
        "shares": fill.shares,
        "reference_price": _decimal_text(fill.reference_price),
        "price": _decimal_text(fill.price),
        "gross_value": _decimal_text(fill.gross_value),
        "commission": _decimal_text(fill.commission),
        "stamp_duty": _decimal_text(fill.stamp_duty),
        "transfer_fee": _decimal_text(fill.transfer_fee),
        "regulatory_fee": _decimal_text(fill.regulatory_fee),
        "slippage_cost": _decimal_text(fill.slippage_cost),
        "trade_cost": _decimal_text(fill.trade_cost),
        "filled_at": fill.filled_at.isoformat(),
        "bar_id": fill.bar_id,
    }


def _lot_payload(lot: PaperLot) -> dict[str, object]:
    return {
        "code": lot.code,
        "shares": lot.shares,
        "price": _decimal_text(lot.price),
        "opened_at": lot.opened_at.isoformat(),
        "entry_event_id": lot.entry_event_id,
        "entry_review_id": lot.entry_review_id,
        "entry_risk_snapshot_id": lot.entry_risk_snapshot_id,
    }


def _bar_cursor_payload(cursor: PaperBarCursor) -> dict[str, object]:
    return {
        "code": cursor.code,
        "opened_at": cursor.opened_at.isoformat(),
        "bar_id": cursor.bar_id,
    }


def _state_payload(state: PaperLedgerState) -> dict[str, object]:
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "revision": state.revision,
        "intents": [_intent_payload(value) for value in state.intents],
        "fills": [_fill_payload(value) for value in state.fills],
        "lots": [_lot_payload(value) for value in state.lots],
        "processed_bar_ids": list(state.processed_bar_ids),
        "bar_cursors": [_bar_cursor_payload(value) for value in state.bar_cursors],
    }


def _state_json(state: PaperLedgerState) -> str:
    return json.dumps(
        _state_payload(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_intent(value: object) -> PaperIntent:
    payload = _object(value, "intent")
    fields = frozenset(
        {
            "event_id",
            "event_data_fingerprint",
            "review_id",
            "risk_snapshot_id",
            "admission_authorization_id",
            "admission_payload_fingerprint",
            "admitted_at",
            "risk_expires_at",
            "entry_event_id",
            "code",
            "side",
            "risk_shares",
            "requested_shares",
            "remaining_shares",
            "signal_bar_id",
            "signal_at",
            "limit_pct",
            "status",
            "reason",
            "fee_schedule_fingerprint",
            "execution_policy_fingerprint",
        }
    )
    _exact_fields(payload, fields, "intent")
    return PaperIntent(
        event_id=_required_text(payload["event_id"], "event_id"),
        event_data_fingerprint=_required_text(
            payload["event_data_fingerprint"],
            "event_data_fingerprint",
        ),
        review_id=_required_text(payload["review_id"], "review_id"),
        risk_snapshot_id=_required_text(
            payload["risk_snapshot_id"],
            "risk_snapshot_id",
        ),
        admission_authorization_id=_required_text(
            payload["admission_authorization_id"],
            "admission_authorization_id",
        ),
        admission_payload_fingerprint=_required_text(
            payload["admission_payload_fingerprint"],
            "admission_payload_fingerprint",
        ),
        admitted_at=_datetime(payload["admitted_at"], "admitted_at"),
        risk_expires_at=_datetime(payload["risk_expires_at"], "risk_expires_at"),
        entry_event_id=_required_text(payload["entry_event_id"], "entry_event_id"),
        code=_required_text(payload["code"], "code"),
        side=_required_text(payload["side"], "side"),
        risk_shares=_integer(payload["risk_shares"], "risk_shares"),
        requested_shares=_integer(payload["requested_shares"], "requested_shares"),
        remaining_shares=_integer(payload["remaining_shares"], "remaining_shares"),
        signal_bar_id=_required_text(payload["signal_bar_id"], "signal_bar_id"),
        signal_at=_datetime(payload["signal_at"], "signal_at"),
        limit_pct=_decimal(payload["limit_pct"], "limit_pct", positive=True),
        status=_required_text(payload["status"], "status"),
        reason=_required_text(payload["reason"], "reason"),
        fee_schedule_fingerprint=_required_fingerprint(
            payload["fee_schedule_fingerprint"],
            "fee_schedule_fingerprint",
        ),
        execution_policy_fingerprint=_required_fingerprint(
            payload["execution_policy_fingerprint"],
            "execution_policy_fingerprint",
        ),
    )


def _parse_fill(value: object) -> PaperFill:
    payload = _object(value, "fill")
    fields = frozenset(
        {
            "fill_id",
            "event_id",
            "entry_event_id",
            "review_id",
            "risk_snapshot_id",
            "code",
            "side",
            "shares",
            "reference_price",
            "price",
            "gross_value",
            "commission",
            "stamp_duty",
            "transfer_fee",
            "regulatory_fee",
            "slippage_cost",
            "trade_cost",
            "filled_at",
            "bar_id",
        }
    )
    _exact_fields(payload, fields, "fill")
    return PaperFill(
        fill_id=_required_text(payload["fill_id"], "fill_id"),
        event_id=_required_text(payload["event_id"], "event_id"),
        entry_event_id=_required_text(payload["entry_event_id"], "entry_event_id"),
        review_id=_required_text(payload["review_id"], "review_id"),
        risk_snapshot_id=_required_text(
            payload["risk_snapshot_id"],
            "risk_snapshot_id",
        ),
        code=_required_text(payload["code"], "code"),
        side=_required_text(payload["side"], "side"),
        shares=_integer(payload["shares"], "shares", minimum=1),
        reference_price=_decimal(
            payload["reference_price"],
            "reference_price",
            positive=True,
        ),
        price=_decimal(payload["price"], "price", positive=True),
        gross_value=_decimal(payload["gross_value"], "gross_value", positive=True),
        commission=_decimal(payload["commission"], "commission"),
        stamp_duty=_decimal(payload["stamp_duty"], "stamp_duty"),
        transfer_fee=_decimal(payload["transfer_fee"], "transfer_fee"),
        regulatory_fee=_decimal(payload["regulatory_fee"], "regulatory_fee"),
        slippage_cost=_decimal(payload["slippage_cost"], "slippage_cost"),
        trade_cost=_decimal(payload["trade_cost"], "trade_cost"),
        filled_at=_datetime(payload["filled_at"], "filled_at"),
        bar_id=_required_text(payload["bar_id"], "bar_id"),
    )


def _parse_lot(value: object) -> PaperLot:
    payload = _object(value, "lot")
    fields = frozenset(
        {
            "code",
            "shares",
            "price",
            "opened_at",
            "entry_event_id",
            "entry_review_id",
            "entry_risk_snapshot_id",
        }
    )
    _exact_fields(payload, fields, "lot")
    return PaperLot(
        code=_required_text(payload["code"], "code"),
        shares=_integer(payload["shares"], "shares", minimum=1),
        price=_decimal(payload["price"], "price", positive=True),
        opened_at=_datetime(payload["opened_at"], "opened_at"),
        entry_event_id=_required_text(payload["entry_event_id"], "entry_event_id"),
        entry_review_id=_required_text(
            payload["entry_review_id"],
            "entry_review_id",
        ),
        entry_risk_snapshot_id=_required_text(
            payload["entry_risk_snapshot_id"],
            "entry_risk_snapshot_id",
        ),
    )


def _parse_bar_cursor(value: object) -> PaperBarCursor:
    payload = _object(value, "bar cursor")
    _exact_fields(
        payload,
        frozenset({"code", "opened_at", "bar_id"}),
        "bar cursor",
    )
    return PaperBarCursor(
        code=_required_text(payload["code"], "bar cursor code"),
        opened_at=_datetime(payload["opened_at"], "bar cursor opened_at"),
        bar_id=_required_text(payload["bar_id"], "bar cursor bar_id"),
    )


def _parse_state(value: str) -> PaperLedgerState:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PaperLedgerIntegrityError("invalid_ledger_json") from exc
    payload = _object(raw, "paper ledger")
    schema_version = payload.get("schema_version")
    fields = {
        "schema_version",
        "revision",
        "intents",
        "fills",
        "lots",
        "processed_bar_ids",
    }
    if schema_version == _STATE_SCHEMA_VERSION:
        fields.add("bar_cursors")
    elif schema_version != 1:
        raise PaperLedgerIntegrityError("unsupported_ledger_schema")
    _exact_fields(payload, frozenset(fields), "paper ledger")
    for field_name in fields - {"schema_version", "revision"}:
        if not isinstance(payload[field_name], list):
            raise PaperLedgerIntegrityError(f"invalid_{field_name}")
    try:
        state = PaperLedgerState(
            revision=_integer(payload["revision"], "revision"),
            intents=tuple(_parse_intent(item) for item in payload["intents"]),
            fills=tuple(_parse_fill(item) for item in payload["fills"]),
            lots=tuple(_parse_lot(item) for item in payload["lots"]),
            processed_bar_ids=tuple(
                _required_text(item, "processed_bar_id")
                for item in payload["processed_bar_ids"]
            ),
            bar_cursors=tuple(
                _parse_bar_cursor(item)
                for item in payload.get("bar_cursors", ())
            ),
        )
    except (TypeError, ValueError) as exc:
        raise PaperLedgerIntegrityError("invalid_ledger_payload") from exc
    _validate_state(state)
    if schema_version == 1 and state.processed_bar_ids:
        raise PaperLedgerIntegrityError("legacy_ledger_missing_bar_cursor")
    return state


def _validate_state(state: PaperLedgerState) -> None:
    if not isinstance(state, PaperLedgerState):
        raise PaperLedgerIntegrityError("invalid_ledger_state")
    reconcile_paper_ledger(InMemoryPaperLedger(state))
    for intent in state.intents:
        if intent.side not in {"buy", "sell"}:
            raise PaperLedgerIntegrityError("invalid_intent_side")
        for field_name in (
            "event_id",
            "event_data_fingerprint",
            "review_id",
            "risk_snapshot_id",
            "admission_authorization_id",
            "admission_payload_fingerprint",
            "entry_event_id",
            "code",
            "signal_bar_id",
            "status",
            "reason",
            "fee_schedule_fingerprint",
            "execution_policy_fingerprint",
        ):
            if not isinstance(getattr(intent, field_name), str) or not getattr(
                intent,
                field_name,
            ):
                raise PaperLedgerIntegrityError("invalid_intent_identity")
        normalize_datetime(intent.risk_expires_at, "risk_expires_at")
        normalize_datetime(intent.admitted_at, "admitted_at")
        normalize_datetime(intent.signal_at, "signal_at")
        try:
            _required_fingerprint(
                intent.fee_schedule_fingerprint,
                "fee_schedule_fingerprint",
            )
            _required_fingerprint(
                intent.execution_policy_fingerprint,
                "execution_policy_fingerprint",
            )
        except ValueError as exc:
            raise PaperLedgerIntegrityError(
                "invalid_intent_execution_policy"
            ) from exc
        if not intent.limit_pct.is_finite() or not Decimal("0") < intent.limit_pct < 1:
            raise PaperLedgerIntegrityError("invalid_intent_limit_pct")
    for fill in state.fills:
        if fill.side not in {"buy", "sell"}:
            raise PaperLedgerIntegrityError("invalid_fill_side")
        if any(
            not value.is_finite() or value < 0
            for value in (
                fill.commission,
                fill.stamp_duty,
                fill.transfer_fee,
                fill.regulatory_fee,
                fill.slippage_cost,
                fill.trade_cost,
            )
        ):
            raise PaperLedgerIntegrityError("invalid_fill_cost")


def _cash_balance(state: PaperLedgerState, initial_cash: Decimal) -> Decimal:
    balance = initial_cash
    for fill in state.fills:
        if fill.side == "buy":
            balance -= (
                fill.gross_value
                + fill.commission
                + fill.stamp_duty
                + fill.transfer_fee
                + fill.regulatory_fee
            )
        elif fill.side == "sell":
            balance += (
                fill.gross_value
                - fill.commission
                - fill.stamp_duty
                - fill.transfer_fee
                - fill.regulatory_fee
            )
        else:
            raise PaperLedgerIntegrityError("invalid_fill_side")
    return balance


def _reserved_buying_power(
    state: PaperLedgerState,
    reservations: Mapping[str, Decimal],
) -> Decimal:
    intents = {intent.event_id: intent for intent in state.intents}
    reserved = Decimal("0")
    for event_id, amount in reservations.items():
        intent = intents.get(event_id)
        if intent is None:
            reserved += amount
            continue
        if intent.side != "buy":
            raise PaperLedgerIntegrityError("reservation_not_bound_to_buy")
        if intent.requested_shares <= 0:
            raise PaperLedgerIntegrityError("reservation_invalid_intent")
        if (
            intent.remaining_shares == 0
            or intent.status == "expired_risk_snapshot"
            or intent.status.startswith("cancelled_")
        ):
            continue
        reserved += amount * Decimal(intent.remaining_shares) / Decimal(
            intent.requested_shares
        )
    return reserved


def _load_reservations(connection: sqlite3.Connection) -> dict[str, Decimal]:
    rows = connection.execute(
        "SELECT event_id, required_cash FROM paper_buying_power_reservation"
    ).fetchall()
    reservations: dict[str, Decimal] = {}
    for row in rows:
        event_id = _required_text(row[0], "reservation event_id")
        amount = _decimal(row[1], "required_cash", positive=True)
        if event_id in reservations:
            raise PaperLedgerIntegrityError("duplicate_buying_power_reservation")
        reservations[event_id] = amount
    return reservations


class SQLitePaperLedger(PaperLedgerPort):
    """Restart-safe paper-only ledger with SQLite CAS and cash reservations."""

    def __init__(self, path: str | Path, *, initial_cash: Decimal) -> None:
        self._path = Path(path).expanduser().absolute()
        self._initial_cash = _decimal(
            initial_cash,
            "initial_cash",
            positive=True,
        ).quantize(_CASH_QUANTUM, rounding=ROUND_HALF_UP)
        self._lock = RLock()
        self.__trusted_bar_capability = object()
        self.__trusted_admission_capability = object()
        self._mutation_fence = MutationLeaseGuard()
        if self._path.exists() and not self._path.is_file():
            raise ValueError("paper ledger path must be a file")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def initial_cash(self) -> Decimal:
        return self._initial_cash

    def bind_strategy_run(self, strategy_run: object) -> None:
        bindings = getattr(strategy_run, "store_bindings", {})
        binding = bindings.get("ledger") if isinstance(bindings, Mapping) else None
        self._mutation_fence.bind(
            strategy_run,
            expected_store_role="ledger",
            expected_store_path=self._path,
            expected_store_instance_id=getattr(
                binding,
                "store_instance_id",
                None,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        initial_state = PaperLedgerState()
        state_json = _state_json(initial_state)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_ledger (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL,
                    initial_cash TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_buying_power_reservation (
                    event_id TEXT PRIMARY KEY,
                    required_cash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trusted_admission (
                    event_id TEXT PRIMARY KEY,
                    event_data_fingerprint TEXT NOT NULL,
                    review_id TEXT NOT NULL,
                    risk_snapshot_id TEXT NOT NULL,
                    admission_authorization_id TEXT NOT NULL,
                    admission_payload_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_execution_policy (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    fee_schedule_fingerprint TEXT NOT NULL,
                    execution_policy_fingerprint TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL
                )
                """
            )
            admission_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(paper_trusted_admission)"
                ).fetchall()
            }
            for column_name in (
                "admission_authorization_id",
                "admission_payload_fingerprint",
            ):
                if column_name not in admission_columns:
                    connection.execute(
                        f"ALTER TABLE paper_trusted_admission "
                        f"ADD COLUMN {column_name} TEXT"
                    )
            legacy_rows = connection.execute(
                """
                SELECT event_id, event_data_fingerprint, review_id,
                       risk_snapshot_id, created_at
                FROM paper_trusted_admission
                WHERE admission_authorization_id IS NULL
                   OR admission_payload_fingerprint IS NULL
                """
            ).fetchall()
            for legacy in legacy_rows:
                identity = {
                    "event_id": legacy[0],
                    "event_data_fingerprint": legacy[1],
                    "review_id": legacy[2],
                    "risk_snapshot_id": legacy[3],
                }
                identity_fingerprint = sha256_json(identity)
                authorization_id = "legacy-paper-auth:" + identity_fingerprint[7:]
                payload_fingerprint = sha256_json(
                    {
                        **identity,
                        "authorization_id": authorization_id,
                        "authorized_at": legacy[4],
                    }
                )
                connection.execute(
                    """
                    UPDATE paper_trusted_admission
                    SET admission_authorization_id = ?,
                        admission_payload_fingerprint = ?
                    WHERE event_id = ?
                    """,
                    (authorization_id, payload_fingerprint, legacy[0]),
                )
            row = connection.execute(
                "SELECT initial_cash FROM paper_ledger WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO paper_ledger (
                        singleton_id,
                        revision,
                        initial_cash,
                        state_json,
                        state_sha256
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        initial_state.revision,
                        _decimal_text(self._initial_cash),
                        state_json,
                        _text_sha256(state_json),
                    ),
                )
            elif _decimal(row[0], "stored initial_cash", positive=True) != self._initial_cash:
                raise PaperLedgerIntegrityError("initial_cash_mismatch")
        self.load()

    @staticmethod
    def _state_from_row(row: sqlite3.Row | tuple[object, ...]) -> PaperLedgerState:
        revision, state_json, stored_sha256 = row
        if not isinstance(state_json, str) or not isinstance(stored_sha256, str):
            raise PaperLedgerIntegrityError("invalid_ledger_row")
        if _text_sha256(state_json) != stored_sha256:
            raise PaperLedgerIntegrityError("ledger_checksum_mismatch")
        state = _parse_state(state_json)
        if state.revision != revision:
            raise PaperLedgerIntegrityError("ledger_revision_mismatch")
        return state

    @staticmethod
    def _execution_policy_from_row(
        row: sqlite3.Row | tuple[object, ...],
    ) -> _PaperExecutionPolicyBinding:
        fee_fingerprint = _required_fingerprint(
            row[0],
            "stored fee_schedule_fingerprint",
        )
        execution_fingerprint = _required_fingerprint(
            row[1],
            "stored execution_policy_fingerprint",
        )
        policy_json = _required_text(row[2], "stored policy_json")
        policy_sha256 = _required_fingerprint(row[3], "stored policy_sha256")
        if _text_sha256(policy_json) != policy_sha256:
            raise PaperLedgerIntegrityError(
                "paper_execution_policy_checksum_mismatch"
            )
        try:
            document = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            raise PaperLedgerIntegrityError(
                "paper_execution_policy_json_invalid"
            ) from exc
        if not isinstance(document, Mapping):
            raise PaperLedgerIntegrityError(
                "paper_execution_policy_json_invalid"
            )
        if (
            document.get("schema_version") != 1
            or document.get("fee_schedule_fingerprint") != fee_fingerprint
            or sha256_json(document) != execution_fingerprint
        ):
            raise PaperLedgerIntegrityError(
                "paper_execution_policy_binding_invalid"
            )
        return _PaperExecutionPolicyBinding(
            fee_schedule_fingerprint=fee_fingerprint,
            execution_policy_fingerprint=execution_fingerprint,
            policy_json=policy_json,
        )

    @classmethod
    def _load_execution_policy(
        cls,
        connection: sqlite3.Connection,
    ) -> _PaperExecutionPolicyBinding | None:
        row = connection.execute(
            """
            SELECT fee_schedule_fingerprint,
                   execution_policy_fingerprint,
                   policy_json,
                   policy_sha256
            FROM paper_execution_policy
            WHERE singleton_id = 1
            """
        ).fetchone()
        return None if row is None else cls._execution_policy_from_row(row)

    @staticmethod
    def _validate_execution_policy_state(
        state: PaperLedgerState,
        binding: _PaperExecutionPolicyBinding | None,
    ) -> None:
        if not state.intents:
            return
        if binding is None:
            raise PaperLedgerIntegrityError("paper_execution_policy_missing")
        for intent in state.intents:
            if (
                intent.fee_schedule_fingerprint
                != binding.fee_schedule_fingerprint
                or intent.execution_policy_fingerprint
                != binding.execution_policy_fingerprint
            ):
                raise PaperLedgerIntegrityError(
                    "paper_execution_policy_intent_mismatch"
                )

    def _bind_execution_policy(
        self,
        *,
        fee_schedule_fingerprint: str,
        execution_policy_fingerprint: str,
        policy_document: Mapping[str, object],
        capability: object,
    ) -> _PaperExecutionPolicyBinding:
        self._mutation_fence.require()
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        fee_schedule_fingerprint = _required_fingerprint(
            fee_schedule_fingerprint,
            "fee_schedule_fingerprint",
        )
        execution_policy_fingerprint = _required_fingerprint(
            execution_policy_fingerprint,
            "execution_policy_fingerprint",
        )
        if (
            policy_document.get("schema_version") != 1
            or policy_document.get("fee_schedule_fingerprint")
            != fee_schedule_fingerprint
            or sha256_json(policy_document) != execution_policy_fingerprint
        ):
            raise PaperLedgerIntegrityError(
                "paper_execution_policy_binding_invalid"
            )
        policy_json = _execution_policy_json(policy_document)
        candidate = _PaperExecutionPolicyBinding(
            fee_schedule_fingerprint=fee_schedule_fingerprint,
            execution_policy_fingerprint=execution_policy_fingerprint,
            policy_json=policy_json,
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._load_execution_policy(connection)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO paper_execution_policy (
                            singleton_id,
                            fee_schedule_fingerprint,
                            execution_policy_fingerprint,
                            policy_json,
                            policy_sha256
                        ) VALUES (1, ?, ?, ?, ?)
                        """,
                        (
                            fee_schedule_fingerprint,
                            execution_policy_fingerprint,
                            policy_json,
                            _text_sha256(policy_json),
                        ),
                    )
                elif existing != candidate:
                    raise PaperLedgerIntegrityError(
                        "paper_execution_policy_mismatch"
                    )
                connection.commit()
                return candidate
            except BaseException:
                connection.rollback()
                raise

    def load(self) -> PaperLedgerState:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision, state_json, state_sha256
                FROM paper_ledger
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                raise PaperLedgerIntegrityError("ledger_row_missing")
            state = self._state_from_row(row)
            binding = self._load_execution_policy(connection)
            self._validate_execution_policy_state(state, binding)
            return state

    @mutation_fenced("paper_ledger.commit")
    def commit(
        self,
        *,
        expected_revision: int,
        state: PaperLedgerState,
        _trusted_bar: PaperBar | None = None,
        _processed_at: datetime | None = None,
        _capability: object | None = None,
        _buy_fill_authority_validator: Callable[
            [tuple[PaperIntent, ...]],
            None,
        ]
        | None = None,
    ) -> None:
        self._mutation_fence.require()
        _integer(expected_revision, "expected_revision")
        _validate_state(state)
        if _trusted_bar is None:
            if _processed_at is not None:
                raise PaperLedgerIntegrityError(
                    "trusted_paper_bar_capability_required"
                )
            if _capability is not self.__trusted_bar_capability:
                raise PaperLedgerIntegrityError(
                    "trusted_paper_commit_capability_required"
                )
        else:
            if not isinstance(_trusted_bar, PaperBar):
                raise TypeError("trusted bar must be PaperBar")
            if _capability is not self.__trusted_bar_capability:
                raise PaperLedgerIntegrityError(
                    "trusted_paper_bar_capability_required"
                )
            if _processed_at is None:
                raise PaperLedgerIntegrityError("trusted_paper_bar_time_missing")
            _processed_at = normalize_datetime(_processed_at, "processed_at")
            if _trusted_bar.closed_at > _processed_at:
                raise PaperLedgerIntegrityError("trusted_paper_bar_not_closed")
        if state.revision != expected_revision + 1:
            raise ValueError("paper ledger revision must advance by one")
        state_json = _state_json(state)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, state_json, state_sha256, initial_cash
                    FROM paper_ledger
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperLedgerIntegrityError("ledger_row_missing")
                current = self._state_from_row(row[:3])
                execution_policy = self._load_execution_policy(connection)
                self._validate_execution_policy_state(
                    current,
                    execution_policy,
                )
                self._validate_execution_policy_state(
                    state,
                    execution_policy,
                )
                if current.revision != expected_revision:
                    raise PaperLedgerConflictError("paper ledger revision conflict")
                if _trusted_bar is not None:
                    if state.fills[: len(current.fills)] != current.fills:
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_bar_fill_history_mismatch"
                        )
                    new_buy_event_ids = {
                        fill.event_id
                        for fill in state.fills[len(current.fills) :]
                        if fill.side == "buy"
                    }
                    if new_buy_event_ids:
                        if not callable(_buy_fill_authority_validator):
                            raise PaperLedgerIntegrityError(
                                "paper_buy_fill_authority_validator_required"
                            )
                        intents_by_event = {
                            intent.event_id: intent for intent in state.intents
                        }
                        try:
                            buy_intents = tuple(
                                intents_by_event[event_id]
                                for event_id in sorted(new_buy_event_ids)
                            )
                        except KeyError as exc:
                            raise PaperLedgerIntegrityError(
                                "paper_buy_fill_intent_missing"
                            ) from exc
                        _buy_fill_authority_validator(buy_intents)
                bar_mutation = (
                    state.processed_bar_ids != current.processed_bar_ids
                    or state.bar_cursors != current.bar_cursors
                    or state.fills != current.fills
                    or state.lots != current.lots
                )
                if bar_mutation and _trusted_bar is None:
                    raise PaperLedgerIntegrityError(
                        "trusted_paper_bar_capability_required"
                    )
                if _trusted_bar is not None:
                    if state.processed_bar_ids != (
                        current.processed_bar_ids + (_trusted_bar.bar_id,)
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_bar_state_mismatch"
                        )
                    cursor = next(
                        (
                            item
                            for item in state.bar_cursors
                            if item.code == _trusted_bar.code
                        ),
                        None,
                    )
                    if (
                        cursor is None
                        or cursor.opened_at != _trusted_bar.opened_at
                        or cursor.bar_id != _trusted_bar.bar_id
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_bar_cursor_mismatch"
                        )
                initial_cash = _decimal(row[3], "stored initial_cash", positive=True)
                reservations = _load_reservations(connection)
                authorization_rows = connection.execute(
                    """
                    SELECT event_id, event_data_fingerprint, review_id,
                           risk_snapshot_id, admission_authorization_id,
                           admission_payload_fingerprint
                    FROM paper_trusted_admission
                    """
                ).fetchall()
                authorizations = {
                    _required_text(item[0], "authorization event_id"): (
                        _required_text(
                            item[1],
                            "authorization event_data_fingerprint",
                        ),
                        _required_text(item[2], "authorization review_id"),
                        _required_text(
                            item[3],
                            "authorization risk_snapshot_id",
                        ),
                        _required_text(
                            item[4],
                            "authorization admission_authorization_id",
                        ),
                        _required_text(
                            item[5],
                            "authorization admission_payload_fingerprint",
                        ),
                    )
                    for item in authorization_rows
                }
                for intent in state.intents:
                    authorization = authorizations.get(intent.event_id)
                    if authorization is None:
                        raise PaperLedgerIntegrityError(
                            "missing_trusted_paper_admission"
                        )
                    if authorization != (
                        intent.event_data_fingerprint,
                        intent.review_id,
                        intent.risk_snapshot_id,
                        intent.admission_authorization_id,
                        intent.admission_payload_fingerprint,
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_binding_mismatch"
                        )
                    if (
                        intent.side == "buy"
                        and intent.remaining_shares > 0
                        and intent.status != "expired_risk_snapshot"
                        and not intent.status.startswith("cancelled_")
                        and intent.event_id not in reservations
                    ):
                        raise PaperLedgerIntegrityError(
                            "missing_buying_power_reservation"
                        )
                cash = _cash_balance(state, initial_cash)
                reserved = _reserved_buying_power(state, reservations)
                if cash < 0:
                    raise PaperLedgerIntegrityError("negative_paper_cash")
                if cash < reserved:
                    raise PaperLedgerIntegrityError(
                        "paper_buying_power_overcommitted"
                    )
                cursor = connection.execute(
                    """
                    UPDATE paper_ledger
                    SET revision = ?, state_json = ?, state_sha256 = ?
                    WHERE singleton_id = 1 AND revision = ?
                    """,
                    (
                        state.revision,
                        state_json,
                        _text_sha256(state_json),
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PaperLedgerConflictError(
                        "paper ledger revision conflict"
                    )
                inactive_buy_events = tuple(
                    intent.event_id
                    for intent in state.intents
                    if intent.side == "buy"
                    and (
                        intent.remaining_shares == 0
                        or intent.status == "expired_risk_snapshot"
                        or intent.status.startswith("cancelled_")
                    )
                )
                if inactive_buy_events:
                    connection.executemany(
                        "DELETE FROM paper_buying_power_reservation "
                        "WHERE event_id = ?",
                        ((event_id,) for event_id in inactive_buy_events),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _trusted_bar_capability(self) -> object:
        return self.__trusted_bar_capability

    def _trusted_admission_capability(self) -> object:
        return self.__trusted_admission_capability

    def _commit_trusted_admission(
        self,
        *,
        expected_revision: int,
        state: PaperLedgerState,
        event_id: str,
        event_data_fingerprint: str,
        review_id: str,
        risk_snapshot_id: str,
        admission_authorization_id: str,
        admission_payload_fingerprint: str,
        admitted_at: datetime,
        required_cash: Decimal | None,
        risk_authority_validator: Callable[[int], None] | None,
        capability: object,
    ) -> None:
        """Atomically consume an event authorization into the paper ledger.

        The event database authorization is an append-only outbox record.  This
        transaction is the matching paper-ledger inbox: authorization binding,
        buying-power reservation, and the intent state advance either all commit
        or all roll back.
        """

        self._mutation_fence.require()
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        _integer(expected_revision, "expected_revision")
        _validate_state(state)
        event_id = _required_text(event_id, "event_id")
        event_data_fingerprint = _required_text(
            event_data_fingerprint,
            "event_data_fingerprint",
        )
        review_id = _required_text(review_id, "review_id")
        risk_snapshot_id = _required_text(risk_snapshot_id, "risk_snapshot_id")
        admission_authorization_id = _required_text(
            admission_authorization_id,
            "admission_authorization_id",
        )
        admission_payload_fingerprint = _required_text(
            admission_payload_fingerprint,
            "admission_payload_fingerprint",
        )
        admitted_at = normalize_datetime(admitted_at, "admitted_at")
        if required_cash is not None:
            required_cash = _decimal(
                required_cash,
                "required_cash",
                positive=True,
            ).quantize(_CASH_QUANTUM, rounding=ROUND_HALF_UP)
        if state.revision not in {expected_revision, expected_revision + 1}:
            raise ValueError(
                "trusted paper admission must be idempotent or advance by one"
            )

        target = next(
            (intent for intent in state.intents if intent.event_id == event_id),
            None,
        )
        if target is None:
            raise PaperLedgerIntegrityError("trusted_paper_admission_intent_missing")
        expected_binding = (
            event_data_fingerprint,
            review_id,
            risk_snapshot_id,
            admission_authorization_id,
            admission_payload_fingerprint,
            admitted_at,
        )
        if (
            target.event_data_fingerprint,
            target.review_id,
            target.risk_snapshot_id,
            target.admission_authorization_id,
            target.admission_payload_fingerprint,
            target.admitted_at,
        ) != expected_binding:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_binding_mismatch"
            )
        if target.side == "buy" and required_cash is None:
            raise PaperLedgerIntegrityError("missing_buying_power_reservation")
        if target.side != "buy" and required_cash is not None:
            raise PaperLedgerIntegrityError("reservation_not_bound_to_buy")

        state_json = _state_json(state)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, state_json, state_sha256, initial_cash
                    FROM paper_ledger
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperLedgerIntegrityError("ledger_row_missing")
                current = self._state_from_row(row[:3])
                execution_policy = self._load_execution_policy(connection)
                self._validate_execution_policy_state(
                    current,
                    execution_policy,
                )
                self._validate_execution_policy_state(
                    state,
                    execution_policy,
                )
                if current.revision != expected_revision:
                    raise PaperLedgerConflictError("paper ledger revision conflict")

                existing_target = next(
                    (
                        intent
                        for intent in current.intents
                        if intent.event_id == event_id
                    ),
                    None,
                )
                if state.revision == expected_revision:
                    if state != current or existing_target != target:
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_idempotence_mismatch"
                        )
                else:
                    if existing_target is not None:
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_duplicate_intent"
                        )
                    if (
                        state.intents != current.intents + (target,)
                        or state.fills != current.fills
                        or state.lots != current.lots
                        or state.processed_bar_ids != current.processed_bar_ids
                        or state.bar_cursors != current.bar_cursors
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_state_mismatch"
                        )
                    if target.side == "buy":
                        if not callable(risk_authority_validator):
                            raise PaperLedgerIntegrityError(
                                "paper_risk_authority_validator_required"
                            )
                        try:
                            risk_authority_validator(current.revision)
                        except Exception as exc:
                            reason = str(exc)
                            if not reason.startswith("paper_risk_"):
                                reason = "paper_risk_authority_validation_failed"
                            raise TrustedPaperAdmissionError(reason) from exc

                stored_authorization = connection.execute(
                    """
                    SELECT event_data_fingerprint, review_id, risk_snapshot_id,
                           admission_authorization_id,
                           admission_payload_fingerprint, created_at
                    FROM paper_trusted_admission
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                authorization_row = (
                    event_data_fingerprint,
                    review_id,
                    risk_snapshot_id,
                    admission_authorization_id,
                    admission_payload_fingerprint,
                    admitted_at.isoformat(),
                )
                if stored_authorization is None:
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
                        (event_id, *authorization_row),
                    )
                elif tuple(stored_authorization) != authorization_row:
                    raise PaperLedgerIntegrityError(
                        "trusted_paper_admission_conflict"
                    )

                initial_cash = _decimal(
                    row[3],
                    "stored initial_cash",
                    positive=True,
                )
                reservations = _load_reservations(connection)
                stored_reservation = connection.execute(
                    """
                    SELECT required_cash, created_at
                    FROM paper_buying_power_reservation
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if required_cash is not None:
                    reservation_row = (
                        _decimal_text(required_cash),
                        admitted_at.isoformat(),
                    )
                    if stored_reservation is None:
                        cash = _cash_balance(current, initial_cash)
                        reserved = _reserved_buying_power(current, reservations)
                        if required_cash > cash - reserved:
                            raise TrustedPaperAdmissionError(
                                "paper_buying_power_insufficient"
                            )
                        connection.execute(
                            """
                            INSERT INTO paper_buying_power_reservation (
                                event_id, required_cash, created_at
                            ) VALUES (?, ?, ?)
                            """,
                            (event_id, *reservation_row),
                        )
                        reservations[event_id] = required_cash
                    elif tuple(stored_reservation) != reservation_row:
                        raise PaperLedgerIntegrityError(
                            "buying_power_reservation_conflict"
                        )
                elif stored_reservation is not None:
                    raise PaperLedgerIntegrityError("reservation_not_bound_to_buy")

                authorization_rows = connection.execute(
                    """
                    SELECT event_id, event_data_fingerprint, review_id,
                           risk_snapshot_id, admission_authorization_id,
                           admission_payload_fingerprint, created_at
                    FROM paper_trusted_admission
                    """
                ).fetchall()
                authorizations = {
                    _required_text(item[0], "authorization event_id"): (
                        _required_text(
                            item[1],
                            "authorization event_data_fingerprint",
                        ),
                        _required_text(item[2], "authorization review_id"),
                        _required_text(
                            item[3],
                            "authorization risk_snapshot_id",
                        ),
                        _required_text(
                            item[4],
                            "authorization admission_authorization_id",
                        ),
                        _required_text(
                            item[5],
                            "authorization admission_payload_fingerprint",
                        ),
                        _datetime(item[6], "authorization created_at"),
                    )
                    for item in authorization_rows
                }
                for intent in state.intents:
                    if authorizations.get(intent.event_id) != (
                        intent.event_data_fingerprint,
                        intent.review_id,
                        intent.risk_snapshot_id,
                        intent.admission_authorization_id,
                        intent.admission_payload_fingerprint,
                        intent.admitted_at,
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_binding_mismatch"
                        )
                    if (
                        intent.side == "buy"
                        and intent.remaining_shares > 0
                        and intent.status != "expired_risk_snapshot"
                        and not intent.status.startswith("cancelled_")
                        and intent.event_id not in reservations
                    ):
                        raise PaperLedgerIntegrityError(
                            "missing_buying_power_reservation"
                        )
                cash = _cash_balance(state, initial_cash)
                reserved = _reserved_buying_power(state, reservations)
                if cash < 0:
                    raise PaperLedgerIntegrityError("negative_paper_cash")
                if cash < reserved:
                    raise PaperLedgerIntegrityError(
                        "paper_buying_power_overcommitted"
                    )

                if state.revision == expected_revision + 1:
                    cursor = connection.execute(
                        """
                        UPDATE paper_ledger
                        SET revision = ?, state_json = ?, state_sha256 = ?
                        WHERE singleton_id = 1 AND revision = ?
                        """,
                        (
                            state.revision,
                            state_json,
                            _text_sha256(state_json),
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PaperLedgerConflictError(
                            "paper ledger revision conflict"
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _commit_trusted_bar(
        self,
        *,
        expected_revision: int,
        state: PaperLedgerState,
        bar: PaperBar,
        processed_at: datetime,
        capability: object,
        buy_fill_authority_validator: Callable[
            [tuple[PaperIntent, ...]],
            None,
        ],
    ) -> None:
        """Commit a trusted bar after the final authoritative buy check.

        The validator runs while the paper SQLite write lock is held and before
        its state update.  The event store is a separate database, so this is a
        fail-closed read boundary rather than a distributed transaction: an
        invalidation committed after that read belongs to the next reconciliation
        boundary and can never authorize a later bar fill.
        """
        self._mutation_fence.require()
        self.commit(
            expected_revision=expected_revision,
            state=state,
            _trusted_bar=bar,
            _processed_at=processed_at,
            _capability=capability,
            _buy_fill_authority_validator=(
                buy_fill_authority_validator
            ),
        )

    def _cancel_pending_entries(
        self,
        cancellations: Mapping[str, tuple[str, str]],
        *,
        capability: object,
    ) -> None:
        self._mutation_fence.require()
        if capability is not self.__trusted_bar_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_reconciliation_capability_required"
            )
        if not cancellations:
            return
        for attempt in range(3):
            state = self.load()
            changed = False
            updated: list[PaperIntent] = []
            for intent in state.intents:
                cancellation = cancellations.get(intent.event_id)
                if (
                    cancellation is None
                    or intent.side != "buy"
                    or intent.remaining_shares == 0
                    or intent.status == "expired_risk_snapshot"
                    or intent.status.startswith("cancelled_")
                ):
                    updated.append(intent)
                    continue
                status, reason = cancellation
                updated.append(replace(intent, status=status, reason=reason))
                changed = True
            if not changed:
                return
            next_state = replace(
                state,
                revision=state.revision + 1,
                intents=tuple(updated),
            )
            try:
                self.commit(
                    expected_revision=state.revision,
                    state=next_state,
                    _capability=self.__trusted_bar_capability,
                )
                return
            except PaperLedgerConflictError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def __authorize_trusted_admission_legacy_disabled(
        self,
        *,
        event_id: str,
        event_data_fingerprint: str,
        review_id: str,
        risk_snapshot_id: str,
        admission_authorization_id: str,
        admission_payload_fingerprint: str,
        created_at: datetime,
        capability: object,
    ) -> _PaperAdmissionBinding:
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        identity = (
            _required_text(event_data_fingerprint, "event_data_fingerprint"),
            _required_text(review_id, "review_id"),
            _required_text(risk_snapshot_id, "risk_snapshot_id"),
        )
        admission_authorization_id = _required_text(
            admission_authorization_id,
            "admission_authorization_id",
        )
        admission_payload_fingerprint = _required_text(
            admission_payload_fingerprint,
            "admission_payload_fingerprint",
        )
        event_id = _required_text(event_id, "event_id")
        created_at = normalize_datetime(created_at, "created_at")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT event_data_fingerprint, review_id, risk_snapshot_id,
                           admission_authorization_id,
                           admission_payload_fingerprint, created_at
                    FROM paper_trusted_admission
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing[:3]) != identity or existing[3] != (
                        admission_authorization_id
                    ):
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_conflict"
                        )
                    connection.commit()
                    return _PaperAdmissionBinding(
                        authorization_id=_required_text(
                            existing[3],
                            "stored admission_authorization_id",
                        ),
                        payload_fingerprint=_required_text(
                            existing[4],
                            "stored admission_payload_fingerprint",
                        ),
                        authorized_at=_datetime(existing[5], "stored created_at"),
                    )
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
                        event_id,
                        *identity,
                        admission_authorization_id,
                        admission_payload_fingerprint,
                        created_at.isoformat(),
                    ),
                )
                connection.commit()
                return _PaperAdmissionBinding(
                    authorization_id=admission_authorization_id,
                    payload_fingerprint=admission_payload_fingerprint,
                    authorized_at=created_at,
                )
            except BaseException:
                connection.rollback()
                raise

    def __release_orphan_authorization_legacy_disabled(
        self,
        *,
        event_id: str,
        event_data_fingerprint: str,
        review_id: str,
        risk_snapshot_id: str,
        admission_authorization_id: str,
        admission_payload_fingerprint: str,
        capability: object,
    ) -> None:
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        event_id = _required_text(event_id, "event_id")
        identity = (
            _required_text(event_data_fingerprint, "event_data_fingerprint"),
            _required_text(review_id, "review_id"),
            _required_text(risk_snapshot_id, "risk_snapshot_id"),
            _required_text(
                admission_authorization_id,
                "admission_authorization_id",
            ),
            _required_text(
                admission_payload_fingerprint,
                "admission_payload_fingerprint",
            ),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, state_json, state_sha256
                    FROM paper_ledger
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperLedgerIntegrityError("ledger_row_missing")
                state = self._state_from_row(row)
                if any(intent.event_id == event_id for intent in state.intents):
                    connection.commit()
                    return
                existing = connection.execute(
                    """
                    SELECT event_data_fingerprint, review_id, risk_snapshot_id,
                           admission_authorization_id,
                           admission_payload_fingerprint
                    FROM paper_trusted_admission
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != identity:
                        raise PaperLedgerIntegrityError(
                            "trusted_paper_admission_conflict"
                        )
                    connection.execute(
                        "DELETE FROM paper_trusted_admission WHERE event_id = ?",
                        (event_id,),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def __reserve_buying_power_legacy_disabled(
        self,
        *,
        event_id: str,
        required_cash: Decimal,
        created_at: datetime,
        capability: object,
    ) -> None:
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        event_id = _required_text(event_id, "event_id")
        required_cash = _decimal(
            required_cash,
            "required_cash",
            positive=True,
        ).quantize(_CASH_QUANTUM, rounding=ROUND_HALF_UP)
        created_at = normalize_datetime(created_at, "created_at")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, state_json, state_sha256, initial_cash
                    FROM paper_ledger
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperLedgerIntegrityError("ledger_row_missing")
                state = self._state_from_row(row[:3])
                initial_cash = _decimal(row[3], "stored initial_cash", positive=True)
                reservations = _load_reservations(connection)
                existing = reservations.get(event_id)
                if existing is not None:
                    if existing != required_cash:
                        raise PaperLedgerIntegrityError(
                            "buying_power_reservation_conflict"
                        )
                    connection.commit()
                    return
                cash = _cash_balance(state, initial_cash)
                reserved = _reserved_buying_power(state, reservations)
                if required_cash > cash - reserved:
                    raise TrustedPaperAdmissionError(
                        "paper_buying_power_insufficient"
                    )
                connection.execute(
                    """
                    INSERT INTO paper_buying_power_reservation (
                        event_id,
                        required_cash,
                        created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        event_id,
                        _decimal_text(required_cash),
                        created_at.isoformat(),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def __release_orphan_reservation_legacy_disabled(
        self,
        *,
        event_id: str,
        required_cash: Decimal,
        capability: object,
    ) -> None:
        if capability is not self.__trusted_admission_capability:
            raise PaperLedgerIntegrityError(
                "trusted_paper_admission_capability_required"
            )
        event_id = _required_text(event_id, "event_id")
        required_cash = _decimal(
            required_cash,
            "required_cash",
            positive=True,
        ).quantize(_CASH_QUANTUM, rounding=ROUND_HALF_UP)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, state_json, state_sha256
                    FROM paper_ledger
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperLedgerIntegrityError("ledger_row_missing")
                state = self._state_from_row(row)
                if any(intent.event_id == event_id for intent in state.intents):
                    connection.commit()
                    return
                reservation = connection.execute(
                    """
                    SELECT required_cash
                    FROM paper_buying_power_reservation
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if reservation is not None:
                    stored = _decimal(
                        reservation[0],
                        "stored required_cash",
                        positive=True,
                    )
                    if stored != required_cash:
                        raise PaperLedgerIntegrityError(
                            "buying_power_reservation_conflict"
                        )
                    connection.execute(
                        """
                        DELETE FROM paper_buying_power_reservation
                        WHERE event_id = ?
                        """,
                        (event_id,),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def account_snapshot(self) -> PaperAccountSnapshot:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision, state_json, state_sha256, initial_cash
                FROM paper_ledger
                WHERE singleton_id = 1
                """
            ).fetchone()
            if row is None:
                raise PaperLedgerIntegrityError("ledger_row_missing")
            state = self._state_from_row(row[:3])
            initial_cash = _decimal(row[3], "stored initial_cash", positive=True)
            reservations = _load_reservations(connection)
        cash = _cash_balance(state, initial_cash)
        reserved = _reserved_buying_power(state, reservations)
        if cash < 0 or cash < reserved:
            raise PaperLedgerIntegrityError("paper_buying_power_overcommitted")
        positions_cost = sum(
            (lot.price * lot.shares for lot in state.lots),
            start=Decimal("0"),
        )
        return PaperAccountSnapshot(
            initial_cash=initial_cash,
            cash_balance=cash,
            reserved_buying_power=reserved,
            available_buying_power=cash - reserved,
            positions_cost=positions_cost,
            cost_basis_equity=cash + positions_cost,
        )


def bind_risk_snapshot_packet_fingerprint(
    packet: EvidencePacket,
    snapshot: RiskSnapshot,
) -> str:
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    if not isinstance(snapshot, RiskSnapshot):
        raise TypeError("snapshot must be RiskSnapshot")
    if packet.event.event_id != snapshot.event_id:
        raise ValueError("packet and risk snapshot event mismatch")
    if packet.event.data_fingerprint != snapshot.event_data_fingerprint:
        raise ValueError("packet and risk snapshot data mismatch")
    if packet.risk != snapshot.decision:
        raise ValueError("packet and risk snapshot decision mismatch")
    return sha256_json(
        {
            "evidence_packet_fingerprint": packet.packet_fingerprint,
            "risk_snapshot_id": snapshot.snapshot_id,
        }
    )


def _review_core(payload_json: str | None, field_name: str) -> Mapping[str, object]:
    if not isinstance(payload_json, str) or not payload_json:
        raise TrustedPaperAdmissionError(f"{field_name}_missing")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise TrustedPaperAdmissionError(f"{field_name}_invalid") from exc
    if not isinstance(payload, Mapping):
        raise TrustedPaperAdmissionError(f"{field_name}_invalid")
    return payload


class TrustedPaperAdmission:
    """Production paper gateway; callers cannot assert state or review identity."""

    def __init__(
        self,
        event_service: DecisionEventService,
        ledger: SQLitePaperLedger,
        *,
        evidence_packet_provider: Callable[
            [DecisionEvent, RiskSnapshot],
            EvidencePacket,
        ],
        fee_schedule: PaperFeeSchedule,
        bar_source: TrustedPaperBarSource,
        manual_check_store: ManualCheckStore | None = None,
        risk_authority_provider: object,
        clock: Callable[[], datetime] | None = None,
        buying_power_buffer_rate: Decimal = Decimal("0.01"),
        strategy_run: object | None = None,
        event_eligibility_provider: Callable[[object], bool] | None = None,
    ) -> None:
        if not isinstance(event_service, DecisionEventService):
            raise TypeError("event_service must be DecisionEventService")
        if not isinstance(ledger, SQLitePaperLedger):
            raise TypeError("ledger must be SQLitePaperLedger")
        if not callable(evidence_packet_provider):
            raise TypeError("evidence_packet_provider must be callable")
        if not isinstance(fee_schedule, PaperFeeSchedule):
            raise TypeError("fee_schedule must be PaperFeeSchedule")
        if not isinstance(bar_source, TrustedPaperBarSource):
            raise TypeError("bar_source must implement TrustedPaperBarSource")
        if manual_check_store is not None and not isinstance(
            manual_check_store,
            ManualCheckStore,
        ):
            raise TypeError("manual_check_store must provide get_for_event")
        if not callable(
            getattr(risk_authority_provider, "admission_guard", None)
        ):
            raise TypeError(
                "risk_authority_provider must expose admission_guard"
            )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "status_payload", None)
        ):
            raise TypeError("strategy_run must provide status_payload")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "mutation_lease", None)
        ):
            raise TypeError("strategy_run must provide mutation_lease")
        if event_eligibility_provider is not None and not callable(
            event_eligibility_provider
        ):
            raise TypeError("event_eligibility_provider must be callable")
        buffer_rate = _decimal(
            buying_power_buffer_rate,
            "buying_power_buffer_rate",
        )
        if buffer_rate < 0 or buffer_rate >= 1:
            raise ValueError("buying_power_buffer_rate must be in [0, 1)")
        self._event_service = event_service
        self._ledger = ledger
        self._evidence_packet_provider = evidence_packet_provider
        self._bar_source = bar_source
        self._manual_check_store = manual_check_store
        self._risk_authority_provider = risk_authority_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._buying_power_buffer_rate = buffer_rate
        self._fee_schedule = fee_schedule
        self._strategy_run = strategy_run
        self._event_eligibility_provider = event_eligibility_provider
        self.__bar_commit_capability = ledger._trusted_bar_capability()
        self.__admission_commit_capability = (
            ledger._trusted_admission_capability()
        )
        policy_document, execution_policy_fingerprint = (
            _execution_policy_document(fee_schedule, buffer_rate)
        )
        self._execution_policy_fingerprint = execution_policy_fingerprint
        with self._strategy_mutation_lease(
            "paper_admission.bind_execution_policy"
        ):
            self._require_active_strategy_run()
            ledger._bind_execution_policy(
                fee_schedule_fingerprint=fee_schedule.fingerprint,
                execution_policy_fingerprint=execution_policy_fingerprint,
                policy_document=policy_document,
                capability=self.__admission_commit_capability,
            )
        self._adapter = ConfirmedEventPaperAdapter(
            ledger,
            fee_schedule=fee_schedule,
            execution_policy_fingerprint=execution_policy_fingerprint,
        )

    @property
    def fee_schedule_fingerprint(self) -> str:
        return self._fee_schedule.fingerprint

    @property
    def execution_policy_fingerprint(self) -> str:
        return self._execution_policy_fingerprint

    def _now(self) -> datetime:
        return normalize_datetime(self._clock(), "clock")

    def _require_active_strategy_run(self) -> None:
        if self._strategy_run is None:
            return
        status = self._strategy_run.status_payload()
        if (
            not isinstance(status, Mapping)
            or status.get("state") != "active"
            or status.get("evidence_scope") != "current_epoch_only"
            or status.get("store_bindings_complete") is not True
        ):
            raise TrustedPaperAdmissionError(
                "paper_strategy_run_not_active"
            )

    def _strategy_mutation_lease(self, operation: str):
        if self._strategy_run is None:
            return nullcontext()
        lease_provider = getattr(self._strategy_run, "mutation_lease", None)
        if not callable(lease_provider):
            raise TrustedPaperAdmissionError(
                "paper_strategy_run_mutation_lease_unavailable"
            )
        return lease_provider(operation)

    def _validate_bar_source_health(
        self,
        *,
        bar: PaperBar | None = None,
        allow_current_started: bool = False,
    ) -> None:
        health_reader = getattr(self._bar_source, "health", None)
        if health_reader is None:
            return
        if not callable(health_reader):
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_health_invalid"
            )
        try:
            health = health_reader()
        except Exception as exc:
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_health_unavailable"
            ) from exc
        degraded = getattr(health, "degraded", None)
        if not isinstance(degraded, bool):
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_health_invalid"
            )
        degraded_reason = getattr(health, "degraded_reason", None)
        if degraded_reason is not None and (
            not isinstance(degraded_reason, str) or not degraded_reason
        ):
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_health_invalid"
            )
        last_attempted = getattr(
            health,
            "last_attempted_bar_closed_at",
            None,
        )
        recovery_cycle = False
        if last_attempted is not None:
            last_complete = getattr(health, "last_attempt_complete", None)
            if not isinstance(last_complete, bool):
                raise TrustedPaperAdmissionError(
                    "trusted_paper_bar_source_health_invalid"
                )
            last_failure = getattr(health, "last_attempt_failure", None)
            recovery_cycle = (
                allow_current_started
                and isinstance(bar, PaperBar)
                and bar.closed_at == last_attempted
                and last_failure is None
            )
            if not last_complete and not recovery_cycle:
                raise TrustedPaperAdmissionError(
                    "trusted_paper_bar_source_cycle_incomplete"
                )
        recovering_persistence_fail_stop = (
            degraded
            and degraded_reason
            == "paper_calendar_preflight_persistence_failed"
            and recovery_cycle
        )
        if degraded and not recovering_persistence_fail_stop:
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_degraded"
            )
        preflight_failed_at = getattr(
            health,
            "calendar_preflight_failure_at",
            None,
        )
        preflight_failure = getattr(
            health,
            "calendar_preflight_failure",
            None,
        )
        if preflight_failed_at is None:
            if preflight_failure is not None:
                raise TrustedPaperAdmissionError(
                    "trusted_paper_bar_source_health_invalid"
                )
        else:
            if (
                not isinstance(preflight_failed_at, datetime)
                or not isinstance(preflight_failure, str)
                or not preflight_failure
            ):
                raise TrustedPaperAdmissionError(
                    "trusted_paper_bar_source_health_invalid"
                )
            recovery_started = (
                recovery_cycle
                and isinstance(bar, PaperBar)
                and bar.closed_at
                >= preflight_failed_at.replace(second=0, microsecond=0)
            )
            if not recovery_started:
                raise TrustedPaperAdmissionError(
                    "trusted_paper_calendar_preflight_failed"
                )

    def _attest_bar(
        self,
        bar: PaperBar,
        *,
        allow_current_started: bool = False,
    ) -> PaperBar:
        self._validate_bar_source_health(
            bar=bar,
            allow_current_started=allow_current_started,
        )
        cycle_attestation = getattr(
            self._bar_source,
            "attest_cycle_bar",
            None,
        )
        if not callable(cycle_attestation):
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_cycle_attestation_unavailable"
            )
        try:
            canonical = cycle_attestation(
                bar.bar_id,
                allow_current_started=allow_current_started,
            )
        except Exception as exc:
            raise TrustedPaperAdmissionError(
                "trusted_paper_bar_source_unavailable"
            ) from exc
        if canonical is None:
            raise TrustedPaperAdmissionError("trusted_paper_bar_not_attested")
        if not isinstance(canonical, PaperBar):
            raise TrustedPaperAdmissionError("trusted_paper_bar_source_invalid")
        if canonical != bar or canonical.bar_id != bar.bar_id:
            raise TrustedPaperAdmissionError("trusted_paper_bar_payload_mismatch")
        return canonical

    @staticmethod
    def _validate_trusted_bar(bar: PaperBar, *, as_of: datetime) -> None:
        if bar.closed_at > as_of:
            raise TrustedPaperAdmissionError("paper_bar_not_closed")
        if bar.opened_at.weekday() >= 5 or bar.opened_at.date() != bar.closed_at.date():
            raise TrustedPaperAdmissionError("paper_bar_outside_a_share_session")
        opened = bar.opened_at.timetz().replace(tzinfo=None)
        closed = bar.closed_at.timetz().replace(tzinfo=None)
        sessions = (
            (time(9, 30), time(11, 30)),
            (time(13, 0), time(15, 0)),
        )
        if not any(start <= opened < end and opened < closed <= end for start, end in sessions):
            raise TrustedPaperAdmissionError("paper_bar_outside_a_share_session")

    def _reconcile_pending_entries(self) -> None:
        cancellations: dict[str, tuple[str, str]] = {}
        for intent in self._ledger.load().intents:
            if (
                intent.side != "buy"
                or intent.remaining_shares == 0
                or intent.status == "expired_risk_snapshot"
                or intent.status.startswith("cancelled_")
            ):
                continue
            view = self._event_service.get(intent.event_id)
            if view.state is not EventState.CONFIRMED:
                cancellations[intent.event_id] = (
                    "cancelled_authoritative_invalidation",
                    "event_no_longer_confirmed_before_paper_fill",
                )
                continue
            snapshots = self._event_service.store.list_risk_snapshots(
                intent.event_id
            )
            if not snapshots:
                cancellations[intent.event_id] = (
                    "cancelled_risk_snapshot_missing",
                    "risk_snapshot_missing_before_paper_fill",
                )
            elif snapshots[-1].snapshot_id != intent.risk_snapshot_id:
                cancellations[intent.event_id] = (
                    "cancelled_risk_snapshot_superseded",
                    "risk_snapshot_superseded_before_paper_fill",
                )
        self._ledger._cancel_pending_entries(
            cancellations,
            capability=self.__bar_commit_capability,
        )

    def _validate_buy_fill_authority(
        self,
        intents: tuple[PaperIntent, ...],
    ) -> None:
        for intent in intents:
            if intent.side != "buy":
                raise PaperLedgerIntegrityError(
                    "paper_buy_fill_authority_side_mismatch"
                )
            view = self._event_service.get(intent.event_id)
            if (
                view.state is not EventState.CONFIRMED
                or view.event.data_fingerprint
                != intent.event_data_fingerprint
            ):
                raise _PaperBuyFillAuthorityChanged(
                    "paper_buy_event_changed_before_commit"
                )
            snapshots = self._event_service.store.list_risk_snapshots(
                intent.event_id
            )
            if (
                not snapshots
                or snapshots[-1].snapshot_id != intent.risk_snapshot_id
            ):
                raise _PaperBuyFillAuthorityChanged(
                    "paper_buy_risk_changed_before_commit"
                )

    def process_bar(self, bar: PaperBar) -> tuple[PaperFill, ...]:
        with self._strategy_mutation_lease("paper_admission.process_bar"):
            return self._process_bar_under_mutation_lease(bar)

    def _process_bar_under_mutation_lease(
        self,
        bar: PaperBar,
    ) -> tuple[PaperFill, ...]:
        self._require_active_strategy_run()
        if not isinstance(bar, PaperBar):
            raise TypeError("bar must be PaperBar")
        bar = self._attest_bar(bar, allow_current_started=True)
        for attempt in range(3):
            now = self._now()
            self._validate_trusted_bar(bar, as_of=now)
            self._reconcile_pending_entries()
            try:
                return self._adapter._on_trusted_bar(
                    bar,
                    commit_capability=self.__bar_commit_capability,
                    processed_at=now,
                    buy_fill_authority_validator=(
                        self._validate_buy_fill_authority
                    ),
                )
            except PaperLedgerConflictError:
                if attempt == 2:
                    raise
            except _PaperBuyFillAuthorityChanged:
                self._reconcile_pending_entries()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _confirmation_transition(view: EventView) -> StoredTransition:
        matches = tuple(
            transition
            for transition in view.transitions
            if transition.from_state is EventState.REVIEW_PENDING
            and transition.to_state is EventState.CONFIRMED
            and transition.reason == "review_verdict:CONFIRM"
            and transition.actor.startswith("review:")
        )
        if len(matches) != 1:
            raise TrustedPaperAdmissionError(
                "trusted_confirmation_transition_missing"
            )
        return matches[0]

    def _risk_snapshot(
        self,
        event: DecisionEvent,
        risk_snapshot_id: str,
        *,
        as_of: datetime,
    ) -> RiskSnapshot:
        risk_snapshot_id = _required_text(risk_snapshot_id, "risk_snapshot_id")
        snapshot = self._event_service.store.get_risk_snapshot(risk_snapshot_id)
        if snapshot is None:
            raise TrustedPaperAdmissionError("risk_snapshot_missing")
        snapshots = self._event_service.store.list_risk_snapshots(event.event_id)
        if not snapshots or snapshots[-1].snapshot_id != risk_snapshot_id:
            raise TrustedPaperAdmissionError("risk_snapshot_superseded")
        validation = snapshot.validate_for_review(event, as_of=as_of)
        if not validation.usable:
            raise TrustedPaperAdmissionError(";".join(validation.reasons))
        return snapshot

    def _trusted_review(
        self,
        view: EventView,
        snapshot: RiskSnapshot,
        *,
        as_of: datetime,
    ) -> StoredLLMReview:
        transition = self._confirmation_transition(view)
        review_id = transition.actor.removeprefix("review:")
        reviews = tuple(
            review
            for review in self._event_service.store.list_llm_reviews(
                view.event.event_id
            )
            if review.review_id == review_id
        )
        if len(reviews) != 1:
            raise TrustedPaperAdmissionError("trusted_review_missing")
        review = reviews[0]
        if (
            review.status != "validated"
            or not review.provider_ok
            or review.verdict != "CONFIRM"
            or review.validation_errors
            or review.error_code is not None
            or review.error_message is not None
            or review.response_content_truncated
            or review.raw_response_truncated
        ):
            raise TrustedPaperAdmissionError("trusted_review_not_validated_confirm")
        if review.event_id != view.event.event_id:
            raise TrustedPaperAdmissionError("review_event_binding_mismatch")
        if review.risk_snapshot_id != snapshot.snapshot_id:
            raise TrustedPaperAdmissionError("review_risk_snapshot_binding_mismatch")
        if review.reviewed_data_fingerprint != view.event.data_fingerprint:
            raise TrustedPaperAdmissionError("review_data_binding_mismatch")
        if review.created_at != transition.occurred_at:
            raise TrustedPaperAdmissionError("review_transition_time_mismatch")
        if (
            review.created_at < snapshot.evaluated_at
            or review.created_at >= snapshot.expires_at
            or review.created_at > as_of
        ):
            raise TrustedPaperAdmissionError("review_time_not_risk_bound")
        packet = self._evidence_packet_provider(view.event, snapshot)
        if not isinstance(packet, EvidencePacket):
            raise TypeError("evidence_packet_provider must return EvidencePacket")
        if packet.event != view.event or packet.risk != snapshot.decision:
            raise TrustedPaperAdmissionError("evidence_packet_binding_mismatch")
        if not packet.reviewable or packet.blockers:
            raise TrustedPaperAdmissionError("evidence_packet_not_reviewable")
        binding = packet.rule_evidence_binding
        binding_fields = (
            "rule_id",
            "rule_card_version",
            "rule_card_fingerprint",
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "algorithm_fingerprint",
        )
        if binding is None or any(
            getattr(binding, field_name) != getattr(view.event, field_name)
            for field_name in binding_fields
        ):
            raise TrustedPaperAdmissionError(
                "evidence_rule_binding_mismatch"
            )
        expected_packet = bind_risk_snapshot_packet_fingerprint(packet, snapshot)
        if review.packet_fingerprint != expected_packet:
            raise TrustedPaperAdmissionError("review_packet_binding_mismatch")
        if review.response_content is None:
            raise TrustedPaperAdmissionError("review_response_content_missing")
        content_bytes = len(review.response_content.encode("utf-8"))
        if (
            content_bytes != review.response_content_bytes
            or _text_sha256(review.response_content)
            != review.response_content_sha256
        ):
            raise TrustedPaperAdmissionError("review_response_audit_mismatch")
        for field_name, payload in (
            (
                "review_response_content",
                _review_core(review.response_content, "review_response_content"),
            ),
            (
                "review_parsed_response",
                _review_core(
                    review.parsed_response_json,
                    "review_parsed_response",
                ),
            ),
        ):
            expected = {
                "verdict": "CONFIRM",
                "reviewed_event_id": view.event.event_id,
                "reviewed_data_fingerprint": view.event.data_fingerprint,
                "reviewed_packet_fingerprint": expected_packet,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise TrustedPaperAdmissionError(f"{field_name}_binding_mismatch")
        return review

    def _resolve(
        self,
        event_id: str,
        risk_snapshot_id: str,
    ) -> tuple[EventView, RiskSnapshot, StoredLLMReview, datetime]:
        event_id = _required_text(event_id, "event_id")
        now = self._now()
        view = self._event_service.get(event_id)
        if view.state is not EventState.CONFIRMED:
            raise TrustedPaperAdmissionError("event_state_not_confirmed")
        snapshot = self._risk_snapshot(
            view.event,
            risk_snapshot_id,
            as_of=now,
        )
        review = self._trusted_review(view, snapshot, as_of=now)
        return view, snapshot, review, now

    def _validate_manual_authorization(
        self,
        authorization: StoredPaperAdmissionAuthorization,
        event: DecisionEvent,
        snapshot: RiskSnapshot,
    ) -> None:
        pending_id = authorization.manual_check_pending_id
        payload_fingerprint = (
            authorization.manual_check_payload_fingerprint
        )
        if pending_id is None and payload_fingerprint is None:
            return
        if pending_id is None or payload_fingerprint is None:
            raise TrustedPaperAdmissionError(
                "manual_check_approval_binding_mismatch"
            )

        if self._manual_check_store is None:
            raise TrustedPaperAdmissionError("manual_check_store_required")
        try:
            record = self._manual_check_store.get_for_event(event.event_id)
        except Exception as exc:
            raise TrustedPaperAdmissionError(
                "manual_check_approval_unavailable"
            ) from exc
        if record is None:
            raise TrustedPaperAdmissionError(
                "manual_check_approval_unavailable"
            )
        if record.status != "approved":
            raise TrustedPaperAdmissionError(
                "manual_check_approval_not_approved"
            )
        event_bindings = (
            (record.event_id, event.event_id),
            (record.event_data_fingerprint, event.data_fingerprint),
            (record.rule_id, event.rule_id),
            (record.rule_card_version, event.rule_card_version),
            (record.rule_card_fingerprint, event.rule_card_fingerprint),
            (record.rule_set_fingerprint, event.rule_set_fingerprint),
            (
                record.corpus_manifest_fingerprint,
                event.corpus_manifest_fingerprint,
            ),
            (record.algorithm_fingerprint, event.algorithm_fingerprint),
            (record.context_fingerprint, event.data_fingerprint),
            (record.risk_snapshot_id, snapshot.snapshot_id),
            (record.pending_id, pending_id),
            (record.payload_fingerprint, payload_fingerprint),
        )
        if any(actual != expected for actual, expected in event_bindings):
            raise TrustedPaperAdmissionError(
                "manual_check_approval_binding_mismatch"
            )

    def _paper_risk_admission_guard(
        self,
        *,
        event: DecisionEvent,
        snapshot: RiskSnapshot,
        ledger_revision: int,
        already_admitted: bool,
        signal_bar_id: str,
    ) -> tuple[ExitStack, Callable[[int], None] | None]:
        stack = ExitStack()
        if (
            already_admitted
            or event.signal.bs_type.casefold() not in _BUY_SIGNALS
        ):
            return stack, None
        guard = self._risk_authority_provider.admission_guard(
            event_id=event.event_id,
            evaluated_at=snapshot.evaluated_at,
            ledger_revision=ledger_revision,
            daily_loss_locked=snapshot.decision.daily_loss_locked,
            drawdown_locked=snapshot.decision.drawdown_locked,
            signal_bar_id=signal_bar_id,
        )
        try:
            validator = stack.enter_context(guard)
            if not callable(validator):
                raise TypeError("paper risk guard must yield a validator")
        except Exception as exc:
            stack.close()
            reason = str(exc)
            if not reason.startswith("paper_risk_"):
                reason = "paper_risk_authority_validation_failed"
            raise TrustedPaperAdmissionError(reason) from exc
        return stack, validator

    def admit(
        self,
        event_id: str,
        signal_bar: PaperBar,
        *,
        risk_snapshot_id: str,
    ) -> PaperIntent:
        with self._strategy_mutation_lease("paper_admission.admit"):
            return self._admit_under_mutation_lease(
                event_id,
                signal_bar,
                risk_snapshot_id=risk_snapshot_id,
            )

    def _admit_under_mutation_lease(
        self,
        event_id: str,
        signal_bar: PaperBar,
        *,
        risk_snapshot_id: str,
    ) -> PaperIntent:
        self._require_active_strategy_run()
        if not isinstance(signal_bar, PaperBar):
            raise TypeError("signal_bar must be PaperBar")
        signal_bar = self._attest_bar(signal_bar)
        view, snapshot, review, now = self._resolve(
            event_id,
            risk_snapshot_id,
        )
        if self._event_eligibility_provider is not None:
            eligible = self._event_eligibility_provider(view.event)
            if type(eligible) is not bool:
                raise TypeError(
                    "event eligibility provider must return boolean"
                )
            if not eligible:
                raise TrustedPaperAdmissionError(
                    "event_outside_current_strategy_run"
                )
        self._validate_trusted_bar(signal_bar, as_of=now)
        if signal_bar.code != view.event.code:
            raise TrustedPaperAdmissionError("signal_bar_code_mismatch")
        if signal_bar.closed_at != view.event.bar_closed_at:
            raise TrustedPaperAdmissionError("signal_bar_close_mismatch")
        authorization = (
            self._event_service.store.issue_paper_admission_authorization(
                event_id=view.event.event_id,
            )
        )
        authorization_at = authorization.authorized_at
        if (
            authorization.event_id != view.event.event_id
            or authorization.event_data_fingerprint
            != view.event.data_fingerprint
            or authorization.review_id != review.review_id
            or authorization.risk_snapshot_id != snapshot.snapshot_id
            or authorization.packet_fingerprint != review.packet_fingerprint
            or authorization.risk_expires_at != snapshot.expires_at
            or authorization_at > now
        ):
            raise TrustedPaperAdmissionError(
                "event_store_paper_authorization_binding_mismatch"
            )
        self._validate_manual_authorization(
            authorization,
            view.event,
            snapshot,
        )
        admission_authorization_id = authorization.authorization_id
        admission_payload_fingerprint = authorization.payload_fingerprint
        reservation: Decimal | None = None
        if view.event.signal.bs_type.casefold() in _BUY_SIGNALS:
            shares = snapshot.decision.shares // 100 * 100
            if shares < 100:
                raise TrustedPaperAdmissionError("risk_shares_below_one_lot")
            reservation = (
                snapshot.decision.entry_reference
                * shares
                * (Decimal("1") + self._buying_power_buffer_rate)
            )
        for attempt in range(3):
            (
                current_view,
                current_snapshot,
                current_review,
                attempt_now,
            ) = self._resolve(event_id, risk_snapshot_id)
            if (
                current_view.event.data_fingerprint
                != view.event.data_fingerprint
                or current_snapshot.snapshot_id != snapshot.snapshot_id
                or current_review.review_id != review.review_id
            ):
                raise TrustedPaperAdmissionError(
                    "trusted_admission_identity_changed"
                )
            current_state = self._ledger.load()
            persisted_intent = next(
                (
                    item
                    for item in current_state.intents
                    if item.event_id == event_id
                ),
                None,
            )
            already_admitted = persisted_intent is not None
            inbox_admitted_at = (
                persisted_intent.admitted_at
                if persisted_intent is not None
                else attempt_now
            )
            staged_ledger = InMemoryPaperLedger(current_state)
            staged_adapter = ConfirmedEventPaperAdapter(
                staged_ledger,
                fee_schedule=self._fee_schedule,
                execution_policy_fingerprint=(
                    self._execution_policy_fingerprint
                ),
            )
            intent = staged_adapter.apply_confirmed_event(
                current_view.event,
                signal_bar,
                event_state=current_view.state,
                review_id=current_review.review_id,
                risk_snapshot=current_snapshot,
                received_at=inbox_admitted_at,
                admission_authorization_id=admission_authorization_id,
                admission_payload_fingerprint=admission_payload_fingerprint,
            )
            try:
                risk_stack, risk_validator = self._paper_risk_admission_guard(
                    event=current_view.event,
                    snapshot=current_snapshot,
                    ledger_revision=current_state.revision,
                    already_admitted=already_admitted,
                    signal_bar_id=signal_bar.bar_id,
                )
                with risk_stack:
                    self._ledger._commit_trusted_admission(
                        expected_revision=current_state.revision,
                        state=staged_ledger.load(),
                        event_id=view.event.event_id,
                        event_data_fingerprint=view.event.data_fingerprint,
                        review_id=review.review_id,
                        risk_snapshot_id=snapshot.snapshot_id,
                        admission_authorization_id=admission_authorization_id,
                        admission_payload_fingerprint=(
                            admission_payload_fingerprint
                        ),
                        admitted_at=inbox_admitted_at,
                        required_cash=reservation,
                        risk_authority_validator=risk_validator,
                        capability=self.__admission_commit_capability,
                    )
                return intent
            except PaperLedgerConflictError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")


__all__ = [
    "LIVE_ORDER_CAPABILITY",
    "PaperAccountSnapshot",
    "SQLitePaperLedger",
    "TrustedPaperAdmission",
    "TrustedPaperAdmissionError",
    "bind_risk_snapshot_packet_fingerprint",
    "paper_execution_policy_fingerprint",
]
