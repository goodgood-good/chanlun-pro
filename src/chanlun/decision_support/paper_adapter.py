from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock
from typing import Callable, Protocol, runtime_checkable

from .fingerprints import normalize_datetime, sha256_json
from .models import DecisionEvent, EventState
from .risk_snapshot import RiskSnapshot


LIVE_ORDER_CAPABILITY = False

_BUY_SIGNALS = frozenset({"1buy", "2buy", "3buy", "3buy_nest", "1buy_nest"})
_SELL_SIGNALS = frozenset({"1sell", "2sell", "3sell", "3sell_nest", "1sell_nest"})
_PRICE_QUANTUM = Decimal("0.01")
_MONEY_QUANTUM = Decimal("0.01")
_STANDALONE_EXECUTION_ALGORITHM_VERSION = (
    "paper-next-tradable-bar-close-limit-v3"
)


def _intent_is_inactive(intent: PaperIntent) -> bool:
    return intent.status == "expired_risk_snapshot" or intent.status.startswith(
        "cancelled_"
    )


class PaperAdapterError(ValueError):
    """Base error for fail-closed paper-adapter validation."""


class PaperAdapterEligibilityError(PaperAdapterError):
    """The event is not eligible for the isolated paper ledger."""


class PaperAdapterConflictError(PaperAdapterError):
    """An idempotency key was reused with different immutable facts."""


class PaperLedgerConflictError(RuntimeError):
    """The ledger revision changed before a compare-and-swap commit."""


class PaperLedgerIntegrityError(RuntimeError):
    """Persisted paper-ledger rows violate restart-safe invariants."""


@dataclass(frozen=True, slots=True)
class PaperFeeSchedule:
    """Explicit paper-only fee assumptions; defaults are not broker universal.

    ``regulatory_fee_rate`` is the conservative combined execution levy used by
    the simulator: the 0.00341% exchange handling fee plus the 0.002% securities
    regulatory fee.  Keeping the combined rate in the existing persisted field
    avoids silently omitting either non-broker charge.
    """

    commission_rate: Decimal = Decimal("0.0003")
    minimum_commission: Decimal = Decimal("5")
    sell_stamp_duty_rate: Decimal = Decimal("0.0005")
    transfer_fee_rate: Decimal = Decimal("0.00001")
    regulatory_fee_rate: Decimal = Decimal("0.0000541")
    slippage_rate: Decimal = Decimal("0")
    price_quantum: Decimal = _PRICE_QUANTUM
    money_quantum: Decimal = _MONEY_QUANTUM

    def __post_init__(self) -> None:
        for field_name in (
            "commission_rate",
            "sell_stamp_duty_rate",
            "transfer_fee_rate",
            "regulatory_fee_rate",
            "slippage_rate",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
                or value >= 1
            ):
                raise ValueError(f"{field_name} must be a Decimal in [0, 1)")
        if (
            not isinstance(self.minimum_commission, Decimal)
            or not self.minimum_commission.is_finite()
            or self.minimum_commission < 0
        ):
            raise ValueError("minimum_commission must be a non-negative Decimal")
        for field_name in ("price_quantum", "money_quantum"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive Decimal")

    def policy_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "commission_rate": self.commission_rate,
            "minimum_commission": self.minimum_commission,
            "sell_stamp_duty_rate": self.sell_stamp_duty_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "regulatory_fee_rate": self.regulatory_fee_rate,
            "regulatory_fee_semantics": (
                "exchange_handling_0.0000341_plus_regulatory_0.00002"
            ),
            "slippage_rate": self.slippage_rate,
            "price_quantum": self.price_quantum,
            "money_quantum": self.money_quantum,
            "rounding": "ROUND_HALF_UP",
        }

    @property
    def fingerprint(self) -> str:
        return sha256_json(self.policy_payload())


DEFAULT_PAPER_FEE_SCHEDULE = PaperFeeSchedule()


@dataclass(frozen=True, slots=True)
class PaperBar:
    """Immutable completed bar used for close-time paper execution.

    ``max_fill_shares`` may depend on the complete interval volume, so every
    execution fact derived from this object is effective only at ``closed_at``.
    """

    code: str
    opened_at: datetime
    closed_at: datetime
    open_price: Decimal
    close_price: Decimal
    previous_close: Decimal
    suspended: bool = False
    limit_up_locked: bool = False
    limit_down_locked: bool = False
    max_fill_shares: int | None = None

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code must be non-empty")
        object.__setattr__(
            self,
            "opened_at",
            normalize_datetime(self.opened_at, "opened_at"),
        )
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        if self.closed_at <= self.opened_at:
            raise ValueError("closed_at must be after opened_at")
        for field_name in ("open_price", "close_price", "previous_close"):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be a positive Decimal")
        if (
            self.max_fill_shares is not None
            and (
                isinstance(self.max_fill_shares, bool)
                or not isinstance(self.max_fill_shares, int)
                or self.max_fill_shares < 0
            )
        ):
            raise ValueError("max_fill_shares must be a non-negative integer")

    @property
    def bar_id(self) -> str:
        digest = sha256_json(
            {
                "code": self.code,
                "opened_at": self.opened_at,
                "closed_at": self.closed_at,
                "open_price": self.open_price,
                "close_price": self.close_price,
                "previous_close": self.previous_close,
                "suspended": self.suspended,
                "limit_up_locked": self.limit_up_locked,
                "limit_down_locked": self.limit_down_locked,
                "max_fill_shares": self.max_fill_shares,
            }
        )
        return "paper-bar:" + digest[7:]


@dataclass(frozen=True, slots=True)
class PaperBarCursor:
    code: str
    opened_at: datetime
    bar_id: str

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("code must be non-empty")
        object.__setattr__(
            self,
            "opened_at",
            normalize_datetime(self.opened_at, "opened_at"),
        )
        if not self.bar_id:
            raise ValueError("bar_id must be non-empty")


@dataclass(frozen=True, slots=True)
class PaperIntent:
    event_id: str
    event_data_fingerprint: str
    review_id: str
    risk_snapshot_id: str
    admission_authorization_id: str
    admission_payload_fingerprint: str
    admitted_at: datetime
    risk_expires_at: datetime
    entry_event_id: str
    code: str
    side: str
    risk_shares: int
    requested_shares: int
    remaining_shares: int
    signal_bar_id: str
    signal_at: datetime
    limit_pct: Decimal
    status: str
    reason: str
    fee_schedule_fingerprint: str = ""
    execution_policy_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    event_id: str
    entry_event_id: str
    review_id: str
    risk_snapshot_id: str
    code: str
    side: str
    shares: int
    reference_price: Decimal
    price: Decimal
    gross_value: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    regulatory_fee: Decimal
    slippage_cost: Decimal
    trade_cost: Decimal
    filled_at: datetime
    bar_id: str


@dataclass(frozen=True, slots=True)
class PaperLot:
    code: str
    shares: int
    price: Decimal
    opened_at: datetime
    entry_event_id: str
    entry_review_id: str
    entry_risk_snapshot_id: str


@dataclass(frozen=True, slots=True)
class PaperPosition:
    code: str
    shares: int
    average_price: Decimal
    entry_event_id: str
    entry_review_id: str
    entry_risk_snapshot_id: str

    @property
    def weighted_average_price(self) -> Decimal:
        return self.average_price


@dataclass(frozen=True, slots=True)
class PaperLedgerState:
    revision: int = 0
    intents: tuple[PaperIntent, ...] = ()
    fills: tuple[PaperFill, ...] = ()
    lots: tuple[PaperLot, ...] = ()
    processed_bar_ids: tuple[str, ...] = ()
    bar_cursors: tuple[PaperBarCursor, ...] = ()


@runtime_checkable
class PaperLedgerPort(Protocol):
    def load(self) -> PaperLedgerState: ...

    def commit(
        self,
        *,
        expected_revision: int,
        state: PaperLedgerState,
    ) -> None: ...


class InMemoryPaperLedger:
    """Process-local test ledger; it has no broker or order-submission surface."""

    def __init__(self, state: PaperLedgerState | None = None) -> None:
        self._state = state or PaperLedgerState()
        self._lock = Lock()

    def load(self) -> PaperLedgerState:
        with self._lock:
            return self._state

    def commit(
        self,
        *,
        expected_revision: int,
        state: PaperLedgerState,
    ) -> None:
        with self._lock:
            if self._state.revision != expected_revision:
                raise PaperLedgerConflictError("paper ledger revision conflict")
            if state.revision != expected_revision + 1:
                raise ValueError("paper ledger revision must advance by one")
            self._state = state


@dataclass(frozen=True, slots=True)
class PaperLedgerReconciliation:
    revision: int
    intent_count: int
    pending_intent_count: int
    fill_count: int
    lot_count: int
    position_count: int


def reconcile_paper_ledger(ledger: PaperLedgerPort) -> PaperLedgerReconciliation:
    state = ledger.load()
    if not isinstance(state, PaperLedgerState):
        raise PaperLedgerIntegrityError("invalid_ledger_state")
    if (
        isinstance(state.revision, bool)
        or not isinstance(state.revision, int)
        or state.revision < 0
    ):
        raise PaperLedgerIntegrityError("invalid_ledger_revision")
    event_ids = [intent.event_id for intent in state.intents]
    if len(event_ids) != len(set(event_ids)):
        raise PaperLedgerIntegrityError("duplicate_event_id")
    fill_ids = [fill.fill_id for fill in state.fills]
    if len(fill_ids) != len(set(fill_ids)):
        raise PaperLedgerIntegrityError("duplicate_fill_id")
    if len(state.processed_bar_ids) != len(set(state.processed_bar_ids)):
        raise PaperLedgerIntegrityError("duplicate_processed_bar_id")
    cursor_codes = [cursor.code for cursor in state.bar_cursors]
    if len(cursor_codes) != len(set(cursor_codes)):
        raise PaperLedgerIntegrityError("duplicate_bar_cursor_code")
    for cursor in state.bar_cursors:
        if cursor.bar_id not in state.processed_bar_ids:
            raise PaperLedgerIntegrityError("bar_cursor_not_processed")

    intents_by_event = {intent.event_id: intent for intent in state.intents}
    filled_by_event: dict[str, int] = {}
    for fill in state.fills:
        intent = intents_by_event.get(fill.event_id)
        if intent is None:
            raise PaperLedgerIntegrityError("fill_without_intent")
        if fill.shares <= 0 or fill.shares % 100:
            raise PaperLedgerIntegrityError("invalid_fill_shares")
        if fill.bar_id not in state.processed_bar_ids:
            raise PaperLedgerIntegrityError("fill_bar_not_processed")
        if (
            fill.side != intent.side
            or fill.entry_event_id != intent.entry_event_id
            or fill.review_id != intent.review_id
            or fill.risk_snapshot_id != intent.risk_snapshot_id
        ):
            raise PaperLedgerIntegrityError("fill_intent_binding_mismatch")
        filled_by_event[fill.event_id] = (
            filled_by_event.get(fill.event_id, 0) + fill.shares
        )

    for intent in state.intents:
        if (
            intent.requested_shares < 100
            or intent.requested_shares % 100
            or intent.risk_shares < intent.requested_shares
            or intent.risk_shares % 100
            or intent.remaining_shares < 0
            or intent.remaining_shares % 100
            or intent.remaining_shares > intent.requested_shares
        ):
            raise PaperLedgerIntegrityError("invalid_intent_shares")
        if (
            filled_by_event.get(intent.event_id, 0)
            != intent.requested_shares - intent.remaining_shares
        ):
            raise PaperLedgerIntegrityError("intent_fill_quantity_mismatch")

    for lot in state.lots:
        if lot.shares <= 0 or lot.shares % 100:
            raise PaperLedgerIntegrityError("invalid_lot_shares")
        if not lot.price.is_finite() or lot.price <= 0:
            raise PaperLedgerIntegrityError("invalid_lot_price")
        entry_intent = intents_by_event.get(lot.entry_event_id)
        if entry_intent is None or entry_intent.side != "buy":
            raise PaperLedgerIntegrityError("lot_without_entry_intent")
        if (
            lot.entry_review_id != entry_intent.review_id
            or lot.entry_risk_snapshot_id != entry_intent.risk_snapshot_id
        ):
            raise PaperLedgerIntegrityError("lot_entry_binding_mismatch")
    return PaperLedgerReconciliation(
        revision=state.revision,
        intent_count=len(state.intents),
        pending_intent_count=sum(
            intent.remaining_shares > 0
            and not _intent_is_inactive(intent)
            for intent in state.intents
        ),
        fill_count=len(state.fills),
        lot_count=len(state.lots),
        position_count=len({lot.code for lot in state.lots}),
    )


class ConfirmedEventPaperAdapter:
    def __init__(
        self,
        ledger: PaperLedgerPort,
        *,
        fee_schedule: PaperFeeSchedule | None = None,
        commission_rate: Decimal | None = None,
        sell_stamp_duty_rate: Decimal | None = None,
        slippage_rate: Decimal | None = None,
        execution_policy_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(ledger, PaperLedgerPort):
            raise TypeError("ledger must implement PaperLedgerPort")
        legacy_rates = (commission_rate, sell_stamp_duty_rate, slippage_rate)
        if fee_schedule is not None and any(rate is not None for rate in legacy_rates):
            raise ValueError("fee_schedule cannot be combined with legacy fee rates")
        if fee_schedule is None and any(rate is not None for rate in legacy_rates):
            fee_schedule = PaperFeeSchedule(
                commission_rate=(
                    Decimal("0.0003")
                    if commission_rate is None
                    else commission_rate
                ),
                minimum_commission=Decimal("0"),
                sell_stamp_duty_rate=(
                    Decimal("0.0005")
                    if sell_stamp_duty_rate is None
                    else sell_stamp_duty_rate
                ),
                transfer_fee_rate=Decimal("0"),
                regulatory_fee_rate=Decimal("0"),
                slippage_rate=(Decimal("0") if slippage_rate is None else slippage_rate),
            )
        if fee_schedule is None:
            fee_schedule = DEFAULT_PAPER_FEE_SCHEDULE
        if not isinstance(fee_schedule, PaperFeeSchedule):
            raise TypeError("fee_schedule must be PaperFeeSchedule")
        self._ledger = ledger
        self._fee_schedule = fee_schedule
        if execution_policy_fingerprint is None:
            execution_policy_fingerprint = sha256_json(
                {
                    "schema_version": 1,
                    "algorithm_version": (
                        _STANDALONE_EXECUTION_ALGORITHM_VERSION
                    ),
                    "fee_schedule_fingerprint": fee_schedule.fingerprint,
                    "mode": "standalone_adapter",
                }
            )
        if (
            not isinstance(execution_policy_fingerprint, str)
            or not execution_policy_fingerprint.startswith("sha256:")
            or len(execution_policy_fingerprint) != 71
        ):
            raise ValueError(
                "execution_policy_fingerprint must use sha256:<64 hex>"
            )
        self._execution_policy_fingerprint = execution_policy_fingerprint
        reconcile_paper_ledger(ledger)

    def apply_confirmed_event(
        self,
        event: DecisionEvent,
        signal_bar: PaperBar,
        *,
        event_state: EventState | str,
        review_id: str,
        risk_snapshot: RiskSnapshot,
        received_at: datetime | None = None,
        admission_authorization_id: str | None = None,
        admission_payload_fingerprint: str | None = None,
    ) -> PaperIntent:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        try:
            normalized_state = EventState(event_state)
        except (TypeError, ValueError) as exc:
            raise PaperAdapterEligibilityError("event_state_not_confirmed") from exc
        if normalized_state is not EventState.CONFIRMED:
            raise PaperAdapterEligibilityError("event_state_not_confirmed")
        if event.rule_binding_status != "bound":
            raise PaperAdapterEligibilityError("event_rule_binding_missing")
        if event.market != "a":
            raise PaperAdapterEligibilityError("unsupported_market")
        if event.market_constraints.lot != 100 or event.market_constraints.t_plus != 1:
            raise PaperAdapterEligibilityError("invalid_a_share_settlement_metadata")
        if not review_id:
            raise PaperAdapterEligibilityError("review_id_missing")
        if signal_bar.code != event.code:
            raise PaperAdapterEligibilityError("signal_bar_code_mismatch")
        if not isinstance(risk_snapshot, RiskSnapshot):
            raise TypeError("risk_snapshot must be RiskSnapshot")
        received_at = normalize_datetime(
            received_at or event.observed_at,
            "received_at",
        )
        admission_authorization_id = admission_authorization_id or (
            "legacy-paper-admission:" + event.event_id
        )
        if not admission_authorization_id:
            raise PaperAdapterEligibilityError("admission_authorization_id_missing")
        admission_payload_fingerprint = admission_payload_fingerprint or sha256_json(
            {
                "event_id": event.event_id,
                "event_data_fingerprint": event.data_fingerprint,
                "review_id": review_id,
                "risk_snapshot_id": risk_snapshot.snapshot_id,
            }
        )
        shares = risk_snapshot.decision.shares // 100 * 100
        risk_shares = shares
        signal_type = event.signal.bs_type.casefold()
        if signal_type in _BUY_SIGNALS:
            side = "buy"
        elif signal_type in _SELL_SIGNALS:
            side = "sell"
        else:
            raise PaperAdapterEligibilityError("unsupported_signal_type")

        state = self._ledger.load()
        existing = next(
            (item for item in state.intents if item.event_id == event.event_id),
            None,
        )
        raw_limit_pct = event.market_constraints.limit_pct
        try:
            if raw_limit_pct is None:
                raise ValueError("limit_pct_missing")
            limit_pct = Decimal(str(raw_limit_pct))
            if not limit_pct.is_finite() or not Decimal("0") < limit_pct < Decimal("1"):
                raise ValueError("limit_pct_invalid")
        except (ArithmeticError, ValueError) as exc:
            if existing is not None:
                raise PaperAdapterConflictError("event_id_conflict") from exc
            reason = "limit_pct_missing" if raw_limit_pct is None else "limit_pct_invalid"
            raise PaperAdapterEligibilityError(reason) from exc
        if existing is not None:
            identity = (
                event.data_fingerprint,
                review_id,
                risk_snapshot.snapshot_id,
                admission_authorization_id,
                admission_payload_fingerprint,
                event.code,
                side,
                risk_shares,
                signal_bar.bar_id,
                event.bar_closed_at,
                limit_pct,
                self._fee_schedule.fingerprint,
                self._execution_policy_fingerprint,
            )
            persisted_identity = (
                existing.event_data_fingerprint,
                existing.review_id,
                existing.risk_snapshot_id,
                existing.admission_authorization_id,
                existing.admission_payload_fingerprint,
                existing.code,
                existing.side,
                existing.risk_shares,
                existing.signal_bar_id,
                existing.signal_at,
                existing.limit_pct,
                existing.fee_schedule_fingerprint,
                existing.execution_policy_fingerprint,
            )
            if identity != persisted_identity:
                raise PaperAdapterConflictError("event_id_conflict")
            return existing

        validation = risk_snapshot.validate_for_review(event, as_of=received_at)
        if not validation.usable:
            raise PaperAdapterEligibilityError(";".join(validation.reasons))
        if shares < 100:
            raise PaperAdapterEligibilityError("risk_shares_below_one_lot")
        entry_event_id = event.event_id
        if side == "buy":
            has_position = any(lot.code == event.code for lot in state.lots)
            has_pending_entry = any(
                intent.code == event.code
                and intent.side == "buy"
                and intent.remaining_shares > 0
                and not _intent_is_inactive(intent)
                for intent in state.intents
            )
            if has_position or has_pending_entry:
                raise PaperAdapterEligibilityError("paper_entry_already_open")
        else:
            has_pending_exit = any(
                intent.code == event.code
                and intent.side == "sell"
                and intent.remaining_shares > 0
                and not _intent_is_inactive(intent)
                for intent in state.intents
            )
            if has_pending_exit:
                raise PaperAdapterEligibilityError("paper_exit_already_pending")
            position_lots = tuple(
                lot for lot in state.lots if lot.code == event.code
            )
            position_shares = sum(lot.shares for lot in position_lots)
            if position_shares < 100:
                raise PaperAdapterEligibilityError("paper_exit_without_position")
            entry_ids = {lot.entry_event_id for lot in position_lots}
            if len(entry_ids) != 1:
                raise PaperAdapterConflictError("position_entry_id_conflict")
            entry_event_id = position_lots[0].entry_event_id
            shares = min(shares, position_shares) // 100 * 100

        intent = PaperIntent(
            event_id=event.event_id,
            event_data_fingerprint=event.data_fingerprint,
            review_id=review_id,
            risk_snapshot_id=risk_snapshot.snapshot_id,
            admission_authorization_id=admission_authorization_id,
            admission_payload_fingerprint=admission_payload_fingerprint,
            admitted_at=received_at,
            risk_expires_at=risk_snapshot.expires_at,
            entry_event_id=entry_event_id,
            code=event.code,
            side=side,
            risk_shares=risk_shares,
            requested_shares=shares,
            remaining_shares=shares,
            signal_bar_id=signal_bar.bar_id,
            signal_at=event.bar_closed_at,
            limit_pct=limit_pct,
            status="pending_next_bar",
            reason="awaiting_next_tradable_bar",
            fee_schedule_fingerprint=self._fee_schedule.fingerprint,
            execution_policy_fingerprint=(
                self._execution_policy_fingerprint
            ),
        )
        new_state = replace(
            state,
            revision=state.revision + 1,
            intents=state.intents + (intent,),
        )
        self._ledger.commit(expected_revision=state.revision, state=new_state)
        return intent

    def on_bar(self, bar: PaperBar) -> tuple[PaperFill, ...]:
        return self._process_bar(
            bar,
            commit_capability=None,
            processed_at=None,
        )

    def _on_trusted_bar(
        self,
        bar: PaperBar,
        *,
        commit_capability: object,
        processed_at: datetime,
        buy_fill_authority_validator: Callable[
            [tuple[PaperIntent, ...]],
            None,
        ],
    ) -> tuple[PaperFill, ...]:
        return self._process_bar(
            bar,
            commit_capability=commit_capability,
            processed_at=normalize_datetime(processed_at, "processed_at"),
            buy_fill_authority_validator=buy_fill_authority_validator,
        )

    def _process_bar(
        self,
        bar: PaperBar,
        *,
        commit_capability: object | None,
        processed_at: datetime | None,
        buy_fill_authority_validator: Callable[
            [tuple[PaperIntent, ...]],
            None,
        ]
        | None = None,
    ) -> tuple[PaperFill, ...]:
        if not isinstance(bar, PaperBar):
            raise TypeError("bar must be PaperBar")
        state = self._ledger.load()
        cursor = next(
            (item for item in state.bar_cursors if item.code == bar.code),
            None,
        )
        if cursor is not None:
            if bar.opened_at < cursor.opened_at:
                raise PaperAdapterConflictError("paper_bar_out_of_order")
            if bar.opened_at == cursor.opened_at:
                if bar.bar_id != cursor.bar_id:
                    raise PaperAdapterConflictError("paper_bar_payload_conflict")
                return ()
        if bar.bar_id in state.processed_bar_ids:
            raise PaperLedgerIntegrityError("processed_bar_without_latest_cursor")
        updated_intents = list(state.intents)
        lots = list(state.lots)
        new_fills: list[PaperFill] = []
        for index, intent in enumerate(updated_intents):
            if intent.code != bar.code or intent.remaining_shares == 0:
                continue
            if _intent_is_inactive(intent):
                continue
            if bar.bar_id == intent.signal_bar_id or bar.opened_at < intent.signal_at:
                continue
            if bar.opened_at < intent.admitted_at:
                updated_intents[index] = replace(
                    intent,
                    status="pending_admission_time",
                    reason="bar_opened_before_trusted_admission",
                )
                continue
            if intent.side == "buy" and bar.closed_at >= intent.risk_expires_at:
                updated_intents[index] = replace(
                    intent,
                    status="expired_risk_snapshot",
                    reason="risk_snapshot_expired_before_fill",
                )
                continue
            if bar.suspended:
                updated_intents[index] = replace(
                    intent,
                    status="pending_suspended",
                    reason="bar_is_suspended",
                )
                continue
            price_tick = self._fee_schedule.price_quantum
            limit_up_price = (
                bar.previous_close * (1 + intent.limit_pct)
            ).quantize(price_tick, rounding=ROUND_HALF_UP)
            limit_down_price = (
                bar.previous_close * (1 - intent.limit_pct)
            ).quantize(price_tick, rounding=ROUND_HALF_UP)
            if intent.side == "buy" and (
                bar.limit_up_locked or bar.close_price >= limit_up_price
            ):
                updated_intents[index] = replace(
                    intent,
                    status="pending_limit_up",
                    reason="entry_blocked_by_limit_up",
                )
                continue
            if intent.side == "sell" and (
                bar.limit_down_locked or bar.close_price <= limit_down_price
            ):
                updated_intents[index] = replace(
                    intent,
                    status="pending_limit_down",
                    reason="exit_blocked_by_limit_down",
                )
                continue
            available_shares = intent.remaining_shares
            if intent.side == "sell":
                sellable_shares = sum(
                    lot.shares
                    for lot in lots
                    if lot.code == intent.code
                    and lot.opened_at.date() < bar.closed_at.date()
                )
                if sellable_shares < 100:
                    updated_intents[index] = replace(
                        intent,
                        status="pending_t1",
                        reason="exit_blocked_by_t_plus_one",
                    )
                    continue
                available_shares = min(available_shares, sellable_shares)
            fill_cap = (
                available_shares
                if bar.max_fill_shares is None
                else min(available_shares, bar.max_fill_shares)
            )
            shares = fill_cap // 100 * 100
            if shares < 100:
                updated_intents[index] = replace(
                    intent,
                    status="pending_zero_fill",
                    reason="bar_liquidity_below_one_lot",
                )
                continue
            if intent.side == "buy":
                execution_price = min(
                    bar.close_price
                    * (1 + self._fee_schedule.slippage_rate),
                    limit_up_price,
                )
            else:
                execution_price = max(
                    bar.close_price
                    * (1 - self._fee_schedule.slippage_rate),
                    limit_down_price,
                )
            reference_price = bar.close_price.quantize(
                self._fee_schedule.price_quantum,
                rounding=ROUND_HALF_UP,
            )
            execution_price = execution_price.quantize(
                self._fee_schedule.price_quantum,
                rounding=ROUND_HALF_UP,
            )
            gross_value = (execution_price * shares).quantize(
                self._fee_schedule.money_quantum,
                rounding=ROUND_HALF_UP,
            )
            commission = max(
                gross_value * self._fee_schedule.commission_rate,
                self._fee_schedule.minimum_commission,
            ).quantize(self._fee_schedule.money_quantum, rounding=ROUND_HALF_UP)
            stamp_duty = (
                gross_value * self._fee_schedule.sell_stamp_duty_rate
                if intent.side == "sell"
                else Decimal("0")
            ).quantize(self._fee_schedule.money_quantum, rounding=ROUND_HALF_UP)
            transfer_fee = (
                gross_value * self._fee_schedule.transfer_fee_rate
            ).quantize(self._fee_schedule.money_quantum, rounding=ROUND_HALF_UP)
            regulatory_fee = (
                gross_value * self._fee_schedule.regulatory_fee_rate
            ).quantize(self._fee_schedule.money_quantum, rounding=ROUND_HALF_UP)
            slippage_cost = (abs(execution_price - reference_price) * shares).quantize(
                self._fee_schedule.money_quantum,
                rounding=ROUND_HALF_UP,
            )
            trade_cost = (
                commission
                + stamp_duty
                + transfer_fee
                + regulatory_fee
                + slippage_cost
            ).quantize(self._fee_schedule.money_quantum, rounding=ROUND_HALF_UP)
            fill_id = "paper-fill:" + sha256_json(
                {"event_id": intent.event_id, "bar_id": bar.bar_id}
            )[7:]
            fill = PaperFill(
                fill_id=fill_id,
                event_id=intent.event_id,
                entry_event_id=intent.entry_event_id,
                review_id=intent.review_id,
                risk_snapshot_id=intent.risk_snapshot_id,
                code=intent.code,
                side=intent.side,
                shares=shares,
                reference_price=reference_price,
                price=execution_price,
                gross_value=gross_value,
                commission=commission,
                stamp_duty=stamp_duty,
                transfer_fee=transfer_fee,
                regulatory_fee=regulatory_fee,
                slippage_cost=slippage_cost,
                trade_cost=trade_cost,
                filled_at=bar.closed_at,
                bar_id=bar.bar_id,
            )
            new_fills.append(fill)
            if intent.side == "buy":
                lots.append(
                    PaperLot(
                        code=intent.code,
                        shares=shares,
                        price=fill.price,
                        opened_at=bar.closed_at,
                        entry_event_id=intent.entry_event_id,
                        entry_review_id=intent.review_id,
                        entry_risk_snapshot_id=intent.risk_snapshot_id,
                    )
                )
            else:
                shares_to_remove = shares
                retained_lots: list[PaperLot] = []
                for lot in lots:
                    eligible = (
                        lot.code == intent.code
                        and lot.opened_at.date() < bar.closed_at.date()
                    )
                    if not eligible or shares_to_remove == 0:
                        retained_lots.append(lot)
                        continue
                    removed = min(lot.shares, shares_to_remove)
                    shares_to_remove -= removed
                    if removed < lot.shares:
                        retained_lots.append(
                            replace(lot, shares=lot.shares - removed)
                        )
                lots = retained_lots
            remaining_shares = intent.remaining_shares - shares
            updated_intents[index] = replace(
                intent,
                remaining_shares=remaining_shares,
                status=("filled" if remaining_shares == 0 else "partially_filled"),
                reason=(
                    "filled_at_next_tradable_bar_close"
                    if remaining_shares == 0
                    else "partially_filled_at_tradable_bar_close"
                ),
            )
        new_state = replace(
            state,
            revision=state.revision + 1,
            intents=tuple(updated_intents),
            fills=state.fills + tuple(new_fills),
            lots=tuple(lots),
            processed_bar_ids=state.processed_bar_ids + (bar.bar_id,),
            bar_cursors=tuple(
                item for item in state.bar_cursors if item.code != bar.code
            )
            + (
                PaperBarCursor(
                    code=bar.code,
                    opened_at=bar.opened_at,
                    bar_id=bar.bar_id,
                ),
            ),
        )
        if commit_capability is None:
            self._ledger.commit(expected_revision=state.revision, state=new_state)
        else:
            trusted_commit = getattr(self._ledger, "_commit_trusted_bar", None)
            if not callable(trusted_commit) or processed_at is None:
                raise PaperLedgerIntegrityError(
                    "trusted_paper_bar_capability_unavailable"
                )
            trusted_commit(
                expected_revision=state.revision,
                state=new_state,
                bar=bar,
                processed_at=processed_at,
                capability=commit_capability,
                buy_fill_authority_validator=(
                    buy_fill_authority_validator
                ),
            )
        return tuple(new_fills)

    def intents(self) -> tuple[PaperIntent, ...]:
        return self._ledger.load().intents

    def fills(self) -> tuple[PaperFill, ...]:
        return self._ledger.load().fills

    def positions(self) -> tuple[PaperPosition, ...]:
        lots_by_code: dict[str, list[PaperLot]] = {}
        for lot in self._ledger.load().lots:
            lots_by_code.setdefault(lot.code, []).append(lot)
        positions: list[PaperPosition] = []
        for code, lots in sorted(lots_by_code.items()):
            shares = sum(lot.shares for lot in lots)
            average_price = sum(
                (lot.price * lot.shares for lot in lots),
                start=Decimal("0"),
            ) / shares
            first = lots[0]
            positions.append(
                PaperPosition(
                    code=code,
                    shares=shares,
                    average_price=average_price,
                    entry_event_id=first.entry_event_id,
                    entry_review_id=first.entry_review_id,
                    entry_risk_snapshot_id=first.entry_risk_snapshot_id,
                )
            )
        return tuple(positions)


def apply_confirmed_event(
    adapter: ConfirmedEventPaperAdapter,
    event: DecisionEvent,
    signal_bar: PaperBar,
    **kwargs: object,
) -> PaperIntent:
    return adapter.apply_confirmed_event(event, signal_bar, **kwargs)
