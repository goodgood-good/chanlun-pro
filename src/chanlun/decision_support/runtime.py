"""Production adapters for the bounded decision-support runtime.

The adapters in this module only freeze market/Chanlun analysis inputs and
compose the scanner with the opt-in monitor.  They deliberately have no
execution, notification, broker, or order dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import math
from pathlib import Path
import re
import sqlite3
from threading import Lock
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.recursive_bt.engine.engine import SELLS, Signal

from .event_factory import snapshot_levels
from .exit_runtime import build_exit_evaluation_cycle_id
from .fingerprints import normalize_datetime, sha256_json
from .market_rules import a_share_board, a_share_limit_pct
from .monitor import DecisionSupportRuntime, MonitorConfig
from .mutation_fence import (
    MutationFenceError,
    MutationLeaseGuard,
    mutation_fenced,
)
from .paper_adapter import PaperBar
from .paper_runtime import (
    PreparedSignalObservationBatch,
    SQLiteTrustedPaperBarStore,
)
from .risk import (
    HoldingSnapshot,
    PendingExitSnapshot,
    QuoteSnapshot,
    RiskContext,
)
from .scanner import (
    DecisionScanner,
    SymbolStructureSnapshot,
    UniverseSnapshot,
)
from .strategies import TREND_BUYS
from .strategy_run import read_strategy_run_binding
from .universe import EligibleSecurity, SecuritySnapshot, UniversePolicy


_CN = ZoneInfo("Asia/Shanghai")
_CENT = Decimal("0.01")


def _market_datetime(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=_CN)
    return normalize_datetime(value, field_name)


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _freeze_signal(value: object) -> Signal:
    if not isinstance(value, Signal):
        raise TypeError("refresh signals must contain Signal values")
    bs_type = str(value.bs_type)
    if not bs_type:
        raise ValueError("signal bs_type must be non-empty")
    level = value.level
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        raise ValueError("signal level must be non-negative")
    return Signal(
        date=_market_datetime(value.date, "signal.date"),
        level=level,
        bs_type=bs_type,
        price=_finite_number(value.price, "signal.price"),
        nest_operable=value.nest_operable,
        nest_depth=int(value.nest_depth or 0),
        structural_stop_below=(
            None
            if value.structural_stop_below is None
            else _finite_number(
                value.structural_stop_below,
                "signal.structural_stop_below",
            )
        ),
        structural_stop_above=(
            None
            if value.structural_stop_above is None
            else _finite_number(
                value.structural_stop_above,
                "signal.structural_stop_above",
            )
        ),
        zs_zd=(
            None
            if value.zs_zd is None
            else _finite_number(value.zs_zd, "signal.zs_zd")
        ),
        zs_zg=(
            None
            if value.zs_zg is None
            else _finite_number(value.zs_zg, "signal.zs_zg")
        ),
        divergence_kind=value.divergence_kind,
        live_divergence=value.live_divergence,
        confirmation_bs_type=value.confirmation_bs_type,
    )


@dataclass(frozen=True, slots=True)
class LiveUniverseDefinition:
    market: str
    codes: tuple[str, ...]
    names: Mapping[str, str]
    selection_candidates: tuple[object, ...] = ()
    required_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.market != "a":
            raise ValueError("only the A-share market is supported")
        codes = tuple(self.codes)
        if not codes or any(
            not isinstance(code, str) or not code.strip() for code in codes
        ):
            raise ValueError("codes must contain non-empty strings")
        if len(codes) != len(set(codes)):
            raise ValueError("codes must not contain duplicates")
        if not isinstance(self.names, Mapping):
            raise TypeError("names must be a mapping")
        names = dict(self.names)
        if any(
            not isinstance(code, str)
            or not isinstance(name, str)
            or not name.strip()
            for code, name in names.items()
        ):
            raise ValueError("names must map strings to non-empty strings")
        candidates = tuple(self.selection_candidates)
        required_codes = tuple(self.required_codes)
        if (
            len(required_codes) != len(set(required_codes))
            or any(
                not isinstance(code, str) or not code.strip()
                for code in required_codes
            )
            or not set(required_codes).issubset(codes)
        ):
            raise ValueError(
                "required_codes must be unique non-empty members of codes"
            )
        object.__setattr__(self, "codes", codes)
        object.__setattr__(self, "names", MappingProxyType(names))
        object.__setattr__(self, "selection_candidates", candidates)
        object.__setattr__(self, "required_codes", required_codes)


@dataclass(frozen=True, slots=True)
class RiskAccountSnapshot:
    account_equity: Decimal
    day_start_equity: Decimal
    available_cash: Decimal
    holdings: tuple[HoldingSnapshot, ...]
    pending_exits: tuple[PendingExitSnapshot, ...]
    day_pnl: Decimal
    strategy_drawdown: Decimal
    daily_loss_locked: bool
    drawdown_locked: bool
    asof: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "account_equity",
            "day_start_equity",
            "available_cash",
            "day_pnl",
            "strategy_drawdown",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field_name} must be a finite Decimal")
        if self.account_equity <= 0 or self.day_start_equity <= 0:
            raise ValueError("account equity values must be positive")
        if self.available_cash < 0:
            raise ValueError("available_cash must be non-negative")
        if not Decimal("0") <= self.strategy_drawdown <= Decimal("1"):
            raise ValueError("strategy_drawdown must be in [0, 1]")
        holdings = tuple(self.holdings)
        pending = tuple(self.pending_exits)
        if not all(isinstance(item, HoldingSnapshot) for item in holdings):
            raise TypeError("holdings must contain HoldingSnapshot values")
        if not all(isinstance(item, PendingExitSnapshot) for item in pending):
            raise TypeError("pending_exits must contain PendingExitSnapshot values")
        for field_name in ("daily_loss_locked", "drawdown_locked"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")
        object.__setattr__(self, "holdings", holdings)
        object.__setattr__(self, "pending_exits", pending)
        object.__setattr__(self, "asof", normalize_datetime(self.asof, "asof"))


class PaperRiskAuthorityError(RuntimeError):
    """A paper entry no longer matches its persisted account-risk authority."""


@dataclass(frozen=True, slots=True)
class PaperRiskAuthorityBinding:
    event_id: str
    evaluated_at: datetime
    ledger_revision: int
    risk_state_revision: int
    account_equity: Decimal
    daily_loss_locked: bool
    drawdown_locked: bool
    signal_bar_id: str
    valuation_fingerprint: str
    binding_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        evaluated_at = normalize_datetime(self.evaluated_at, "evaluated_at")
        for field_name in ("ledger_revision", "risk_state_revision"):
            value = getattr(self, field_name)
            minimum = 0 if field_name == "ledger_revision" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{field_name} is invalid")
        if (
            not isinstance(self.account_equity, Decimal)
            or not self.account_equity.is_finite()
            or self.account_equity <= 0
        ):
            raise ValueError("account_equity must be a positive Decimal")
        for field_name in ("daily_loss_locked", "drawdown_locked"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")
        if not isinstance(self.signal_bar_id, str) or not self.signal_bar_id.startswith(
            "paper-bar:"
        ):
            raise ValueError("signal_bar_id is invalid")
        if (
            not isinstance(self.valuation_fingerprint, str)
            or not self.valuation_fingerprint.startswith("sha256:")
            or len(self.valuation_fingerprint) != 71
        ):
            raise ValueError("valuation_fingerprint is invalid")
        object.__setattr__(self, "evaluated_at", evaluated_at)
        expected = sha256_json(self._identity_payload())
        if self.binding_fingerprint and self.binding_fingerprint != expected:
            raise PaperRiskAuthorityError(
                "paper_risk_authority_binding_checksum_mismatch"
            )
        object.__setattr__(self, "binding_fingerprint", expected)

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "evaluated_at": self.evaluated_at,
            "ledger_revision": self.ledger_revision,
            "risk_state_revision": self.risk_state_revision,
            "account_equity": self.account_equity,
            "daily_loss_locked": self.daily_loss_locked,
            "drawdown_locked": self.drawdown_locked,
            "signal_bar_id": self.signal_bar_id,
            "valuation_fingerprint": self.valuation_fingerprint,
        }


class PaperRiskAccountProvider:
    """Derive entry risk exclusively from one paper ledger and risk-state DB."""

    def __init__(self, *, data_provider: object, ledger: object, risk_state: object) -> None:
        if any(
            not callable(getattr(data_provider, name, None))
            for name in ("risk_quote", "quote_for_code", "paper_bar")
        ):
            raise TypeError("data_provider must expose frozen paper quotes")
        if not callable(getattr(ledger, "load", None)) or not callable(
            getattr(ledger, "account_snapshot", None)
        ):
            raise TypeError("ledger must expose load and account_snapshot")
        required_risk_state = ("mark", "_connect", "_parse_row")
        if any(not callable(getattr(risk_state, name, None)) for name in required_risk_state):
            raise TypeError("risk_state must expose persistent paper-risk state")
        if not hasattr(risk_state, "_lock"):
            raise TypeError("risk_state must expose a serialization lock")
        self._data_provider = data_provider
        self._ledger = ledger
        self._risk_state = risk_state
        self._mutation_fence = MutationLeaseGuard()
        self._strategy_run_binding: tuple[str, int, str] | None = None
        self._initialize_binding_store()

    def bind_strategy_run(self, strategy_run: object) -> None:
        binding = (
            getattr(strategy_run, "run_id", None),
            getattr(strategy_run, "epoch", None),
            getattr(strategy_run, "strategy_run_fingerprint", None),
        )
        if (
            not isinstance(binding[0], str)
            or not binding[0]
            or isinstance(binding[1], bool)
            or not isinstance(binding[1], int)
            or binding[1] <= 0
            or not isinstance(binding[2], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", binding[2]) is None
        ):
            raise ValueError("strategy run binding is invalid")
        if self._strategy_run_binding not in (None, binding):
            raise ValueError("strategy run binding cannot change")

        store_bindings = getattr(strategy_run, "store_bindings", None)
        if not isinstance(store_bindings, Mapping):
            raise MutationFenceError("mutation_fence_store_role_unavailable")
        dependency_validator = MutationLeaseGuard()
        for store_role, store in (
            ("ledger", self._ledger),
            ("risk", self._risk_state),
        ):
            store_path = getattr(store, "path", None)
            if not isinstance(store_path, (str, Path)):
                raise MutationFenceError(
                    "mutation_fence_store_path_unavailable"
                )
            expected_store_binding = store_bindings.get(store_role)
            expected_instance_id = getattr(
                expected_store_binding,
                "store_instance_id",
                None,
            )
            if not isinstance(expected_instance_id, str) or not expected_instance_id:
                raise MutationFenceError(
                    "mutation_fence_store_instance_mismatch"
                )
            dependency_validator.bind(
                strategy_run,
                expected_store_role=store_role,
                expected_store_path=store_path,
                expected_store_instance_id=expected_instance_id,
            )
            physical_store_binding = read_strategy_run_binding(store_path)
            if physical_store_binding != expected_store_binding:
                if (
                    physical_store_binding is None
                    or physical_store_binding.store_instance_id
                    != expected_instance_id
                ):
                    raise MutationFenceError(
                        "mutation_fence_store_instance_mismatch"
                    )
                raise MutationFenceError(
                    "mutation_fence_store_binding_mismatch"
                )

        self._mutation_fence.bind(strategy_run)
        self._strategy_run_binding = binding

    def _connect(self) -> sqlite3.Connection:
        connection = self._risk_state._connect()
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("risk_state connection is invalid")
        return connection

    def _initialize_binding_store(self) -> None:
        with self._risk_state._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_risk_authority_binding (
                    event_id TEXT PRIMARY KEY,
                    evaluated_at TEXT NOT NULL,
                    ledger_revision INTEGER NOT NULL,
                    risk_state_revision INTEGER NOT NULL,
                    account_equity TEXT NOT NULL,
                    daily_loss_locked INTEGER NOT NULL,
                    drawdown_locked INTEGER NOT NULL,
                    signal_bar_id TEXT NOT NULL,
                    valuation_fingerprint TEXT NOT NULL,
                    binding_fingerprint TEXT NOT NULL
                )
                """
            )
            binding_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(paper_risk_authority_binding)"
                ).fetchall()
            }
            for column_name in ("signal_bar_id", "valuation_fingerprint"):
                if column_name not in binding_columns:
                    connection.execute(
                        f"ALTER TABLE paper_risk_authority_binding "
                        f"ADD COLUMN {column_name} TEXT"
                    )
            rows = connection.execute(
                """
                SELECT event_id, evaluated_at, ledger_revision,
                       risk_state_revision, account_equity,
                       daily_loss_locked, drawdown_locked,
                       signal_bar_id, valuation_fingerprint,
                       binding_fingerprint
                FROM paper_risk_authority_binding
                """
            ).fetchall()
            for row in rows:
                self._binding_from_row(row)

    @staticmethod
    def _binding_from_row(row: tuple[object, ...]) -> PaperRiskAuthorityBinding:
        if len(row) != 10:
            raise PaperRiskAuthorityError("paper_risk_authority_binding_invalid")
        try:
            evaluated_at = datetime.fromisoformat(str(row[1]))
            account_equity = Decimal(str(row[4]))
        except (ValueError, ArithmeticError) as exc:
            raise PaperRiskAuthorityError(
                "paper_risk_authority_binding_invalid"
            ) from exc
        for position in (5, 6):
            if row[position] not in (0, 1):
                raise PaperRiskAuthorityError(
                    "paper_risk_authority_binding_invalid"
                )
        try:
            return PaperRiskAuthorityBinding(
                event_id=row[0],
                evaluated_at=evaluated_at,
                ledger_revision=row[2],
                risk_state_revision=row[3],
                account_equity=account_equity,
                daily_loss_locked=bool(row[5]),
                drawdown_locked=bool(row[6]),
                signal_bar_id=row[7],
                valuation_fingerprint=row[8],
                binding_fingerprint=row[9],
            )
        except (TypeError, ValueError) as exc:
            raise PaperRiskAuthorityError(
                "paper_risk_authority_binding_invalid"
            ) from exc

    @classmethod
    def _load_binding(
        cls,
        connection: sqlite3.Connection,
        event_id: str,
    ) -> PaperRiskAuthorityBinding | None:
        row = connection.execute(
            """
            SELECT event_id, evaluated_at, ledger_revision,
                   risk_state_revision, account_equity,
                   daily_loss_locked, drawdown_locked,
                   signal_bar_id, valuation_fingerprint,
                   binding_fingerprint
            FROM paper_risk_authority_binding
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        return None if row is None else cls._binding_from_row(row)

    def binding_for(self, event_id: str) -> PaperRiskAuthorityBinding:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        with self._risk_state._lock, self._connect() as connection:
            binding = self._load_binding(connection, event_id)
        if binding is None:
            raise PaperRiskAuthorityError("paper_risk_authority_binding_missing")
        return binding

    def _consistent_account_state(self) -> tuple[object, object]:
        for _attempt in range(3):
            before = self._ledger.load()
            account = self._ledger.account_snapshot()
            after = self._ledger.load()
            if before == after:
                revision = getattr(before, "revision", None)
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 0
                ):
                    raise PaperRiskAuthorityError(
                        "paper_risk_ledger_revision_invalid"
                    )
                return before, account
        raise PaperRiskAuthorityError("paper_risk_ledger_changed_during_snapshot")

    @staticmethod
    def _holdings(state: object, closed_at: datetime) -> tuple[HoldingSnapshot, ...]:
        lots_by_code: dict[str, list[object]] = {}
        for lot in tuple(getattr(state, "lots", ())):
            code = getattr(lot, "code", None)
            shares = getattr(lot, "shares", None)
            price = getattr(lot, "price", None)
            opened_at = getattr(lot, "opened_at", None)
            if (
                not isinstance(code, str)
                or not code
                or isinstance(shares, bool)
                or not isinstance(shares, int)
                or shares <= 0
                or not isinstance(price, Decimal)
                or not price.is_finite()
                or price <= 0
            ):
                raise PaperRiskAuthorityError("paper_risk_lot_invalid")
            normalize_datetime(opened_at, "lot.opened_at")
            lots_by_code.setdefault(code, []).append(lot)
        holdings: list[HoldingSnapshot] = []
        trading_day = closed_at.astimezone(_CN).date()
        for code, lots in sorted(lots_by_code.items()):
            shares = sum(lot.shares for lot in lots)
            average_price = sum(
                (lot.price * lot.shares for lot in lots),
                start=Decimal("0"),
            ) / shares
            opened_at = min(
                normalize_datetime(lot.opened_at, "lot.opened_at") for lot in lots
            )
            sellable = sum(
                lot.shares
                for lot in lots
                if normalize_datetime(
                    lot.opened_at,
                    "lot.opened_at",
                ).astimezone(_CN).date()
                < trading_day
            )
            holdings.append(
                HoldingSnapshot(
                    code=code,
                    shares=shares,
                    sellable_shares=sellable,
                    opened_at=opened_at,
                    average_price=average_price,
                )
            )
        return tuple(holdings)

    @staticmethod
    def _pending_exits(state: object) -> tuple[PendingExitSnapshot, ...]:
        pending: list[PendingExitSnapshot] = []
        for intent in tuple(getattr(state, "intents", ())):
            remaining = getattr(intent, "remaining_shares", 0)
            status = str(getattr(intent, "status", ""))
            if (
                getattr(intent, "side", None) != "sell"
                or isinstance(remaining, bool)
                or not isinstance(remaining, int)
                or remaining <= 0
                or status.startswith("cancelled_")
            ):
                continue
            pending.append(
                PendingExitSnapshot(
                    code=str(getattr(intent, "code", "")),
                    shares=remaining,
                    reason=str(getattr(intent, "reason", status or "pending")),
                    blocked_by_t1="t1" in status.casefold(),
                    blocked_by_limit="limit" in status.casefold(),
                )
            )
        if len({item.code for item in pending}) != len(pending):
            raise PaperRiskAuthorityError("duplicate_pending_paper_exit")
        return tuple(sorted(pending, key=lambda item: item.code))

    @staticmethod
    def _account_decimal(account: object, field_name: str) -> Decimal:
        value = getattr(account, field_name, None)
        if not isinstance(value, Decimal) or not value.is_finite():
            raise PaperRiskAuthorityError("paper_risk_account_snapshot_invalid")
        return value

    def _persist_binding(self, binding: PaperRiskAuthorityBinding, mark: object) -> None:
        self._mutation_fence.require()
        with self._risk_state._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, risk_policy_fingerprint,
                           payload_json, payload_sha256
                    FROM paper_risk_state WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise PaperRiskAuthorityError("paper_risk_state_missing")
                _payload, current_mark = self._risk_state._parse_row(row)
                if current_mark != mark:
                    raise PaperRiskAuthorityError(
                        "paper_risk_state_changed_during_binding"
                    )
                if getattr(self._ledger.load(), "revision", None) != (
                    binding.ledger_revision
                ):
                    raise PaperRiskAuthorityError(
                        "paper_risk_ledger_changed_during_binding"
                    )
                existing = self._load_binding(connection, binding.event_id)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO paper_risk_authority_binding (
                            event_id, evaluated_at, ledger_revision,
                            risk_state_revision, account_equity,
                            daily_loss_locked, drawdown_locked,
                            signal_bar_id, valuation_fingerprint,
                            binding_fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            binding.event_id,
                            binding.evaluated_at.isoformat(),
                            binding.ledger_revision,
                            binding.risk_state_revision,
                            format(binding.account_equity, "f"),
                            int(binding.daily_loss_locked),
                            int(binding.drawdown_locked),
                            binding.signal_bar_id,
                            binding.valuation_fingerprint,
                            binding.binding_fingerprint,
                        ),
                    )
                elif existing != binding:
                    raise PaperRiskAuthorityError(
                        "paper_risk_authority_binding_conflict"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _quote_payload(quote: QuoteSnapshot) -> dict[str, object]:
        return {
            "code": quote.code,
            "price": quote.price,
            "quote_time": quote.quote_time,
            "entry_tradable": quote.entry_tradable,
            "exit_tradable": quote.exit_tradable,
            "limit_up_locked": quote.limit_up_locked,
            "limit_down_locked": quote.limit_down_locked,
        }

    def _bar_for_quote(
        self,
        code: str,
        closed_at: datetime,
        quote: QuoteSnapshot,
    ) -> PaperBar:
        bar = self._data_provider.paper_bar(code, closed_at)
        if (
            not isinstance(bar, PaperBar)
            or bar.code != code
            or bar.closed_at != closed_at
            or bar.close_price != quote.price
        ):
            raise PaperRiskAuthorityError("paper_risk_quote_bar_binding_invalid")
        return bar

    @mutation_fenced("paper_risk_account_provider.evaluate")
    def __call__(
        self,
        security: object,
        event: object,
        bar_closed_at: datetime,
    ) -> RiskContext:
        self._mutation_fence.require()
        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        code = getattr(security, "code", None)
        event_id = getattr(event, "event_id", None)
        if not isinstance(code, str) or getattr(event, "code", None) != code:
            raise ValueError("event and security code mismatch")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        if self._strategy_run_binding is not None and (
            getattr(event, "strategy_run_id", None),
            getattr(event, "strategy_run_epoch", None),
            getattr(event, "strategy_run_fingerprint", None),
        ) != self._strategy_run_binding:
            raise PaperRiskAuthorityError(
                "paper_risk_event_outside_strategy_run"
            )
        state, account = self._consistent_account_state()
        holdings = self._holdings(state, closed_at)
        cash = self._account_decimal(account, "cash_balance")
        available = self._account_decimal(account, "available_buying_power")
        if cash < 0 or available < 0:
            raise PaperRiskAuthorityError("paper_risk_account_snapshot_invalid")
        market_value = Decimal("0")
        valuation_positions: list[dict[str, object]] = []
        for holding in holdings:
            position_quote = self._data_provider.quote_for_code(
                holding.code,
                closed_at,
            )
            if (
                not isinstance(position_quote, QuoteSnapshot)
                or position_quote.code != holding.code
                or position_quote.quote_time != closed_at
            ):
                raise PaperRiskAuthorityError("paper_risk_position_quote_invalid")
            position_bar = self._bar_for_quote(
                holding.code,
                closed_at,
                position_quote,
            )
            market_value += position_quote.price * holding.shares
            valuation_positions.append(
                {
                    "code": holding.code,
                    "shares": holding.shares,
                    "quote": self._quote_payload(position_quote),
                    "bar_id": position_bar.bar_id,
                }
            )
        account_equity = cash + market_value
        if account_equity <= 0:
            raise PaperRiskAuthorityError("paper_risk_account_equity_invalid")
        quote = self._data_provider.risk_quote(security, closed_at)
        if (
            not isinstance(quote, QuoteSnapshot)
            or quote.code != code
            or quote.quote_time != closed_at
        ):
            raise PaperRiskAuthorityError("paper_risk_entry_quote_invalid")
        signal_bar = self._bar_for_quote(code, closed_at, quote)
        valuation_fingerprint = sha256_json(
            {
                "schema_version": 1,
                "evaluated_at": closed_at,
                "cash_balance": cash,
                "available_buying_power": available,
                "entry": {
                    "code": code,
                    "quote": self._quote_payload(quote),
                    "bar_id": signal_bar.bar_id,
                },
                "positions": valuation_positions,
            }
        )
        mark = self._risk_state.mark(account_equity, closed_at)
        binding = PaperRiskAuthorityBinding(
            event_id=event_id,
            evaluated_at=closed_at,
            ledger_revision=getattr(state, "revision"),
            risk_state_revision=getattr(mark, "revision"),
            account_equity=account_equity,
            daily_loss_locked=getattr(mark, "daily_loss_locked"),
            drawdown_locked=getattr(mark, "drawdown_locked"),
            signal_bar_id=signal_bar.bar_id,
            valuation_fingerprint=valuation_fingerprint,
        )
        self._persist_binding(binding, mark)
        return RiskContext(
            account_equity=account_equity,
            day_start_equity=getattr(mark, "day_start_equity"),
            available_cash=available,
            holdings=holdings,
            pending_exits=self._pending_exits(state),
            day_pnl=getattr(mark, "day_pnl"),
            strategy_drawdown=getattr(mark, "strategy_drawdown"),
            daily_loss_locked=binding.daily_loss_locked,
            drawdown_locked=binding.drawdown_locked,
            quote=quote,
            asof=closed_at,
        )

    def _validate_locked_authority(
        self,
        connection: sqlite3.Connection,
        binding: PaperRiskAuthorityBinding,
        *,
        ledger_revision: int,
        evaluated_at: datetime,
        daily_loss_locked: bool,
        drawdown_locked: bool,
        signal_bar_id: str,
    ) -> None:
        current_binding = self._load_binding(connection, binding.event_id)
        if current_binding != binding:
            raise PaperRiskAuthorityError("paper_risk_authority_binding_changed")
        if binding.evaluated_at != evaluated_at:
            raise PaperRiskAuthorityError("paper_risk_evaluation_time_changed")
        if binding.ledger_revision != ledger_revision:
            raise PaperRiskAuthorityError("paper_risk_ledger_revision_changed")
        if binding.signal_bar_id != signal_bar_id:
            raise PaperRiskAuthorityError("paper_risk_signal_bar_changed")
        if (
            daily_loss_locked
            or drawdown_locked
            or binding.daily_loss_locked
            or binding.drawdown_locked
        ):
            raise PaperRiskAuthorityError("paper_risk_latch_locked")
        row = connection.execute(
            """
            SELECT revision, risk_policy_fingerprint,
                   payload_json, payload_sha256
            FROM paper_risk_state WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            raise PaperRiskAuthorityError("paper_risk_state_missing")
        _payload, current_mark = self._risk_state._parse_row(row)
        if (
            current_mark.revision != binding.risk_state_revision
            or current_mark.asof != binding.evaluated_at
            or current_mark.account_equity != binding.account_equity
            or current_mark.daily_loss_locked
            or current_mark.drawdown_locked
        ):
            raise PaperRiskAuthorityError("paper_risk_state_changed")

    @contextmanager
    def admission_guard(
        self,
        *,
        event_id: str,
        evaluated_at: datetime,
        ledger_revision: int,
        daily_loss_locked: bool,
        drawdown_locked: bool,
        signal_bar_id: str,
    ) -> Iterator[Callable[[int], None]]:
        evaluated_at = normalize_datetime(evaluated_at, "evaluated_at")
        with self._risk_state._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                binding = self._load_binding(connection, event_id)
                if binding is None:
                    raise PaperRiskAuthorityError(
                        "paper_risk_authority_binding_missing"
                    )
                if getattr(self._ledger.load(), "revision", None) != ledger_revision:
                    raise PaperRiskAuthorityError(
                        "paper_risk_ledger_revision_changed"
                    )
                self._validate_locked_authority(
                    connection,
                    binding,
                    ledger_revision=ledger_revision,
                    evaluated_at=evaluated_at,
                    daily_loss_locked=daily_loss_locked,
                    drawdown_locked=drawdown_locked,
                    signal_bar_id=signal_bar_id,
                )

                def validate_inside_ledger(
                    current_ledger_revision: int,
                ) -> None:
                    self._validate_locked_authority(
                        connection,
                        binding,
                        ledger_revision=current_ledger_revision,
                        evaluated_at=evaluated_at,
                        daily_loss_locked=daily_loss_locked,
                        drawdown_locked=drawdown_locked,
                        signal_bar_id=signal_bar_id,
                    )

                yield validate_inside_ledger
            finally:
                connection.rollback()


class _FrozenCL:
    def __init__(self, frequency: str, levels: Sequence[object]) -> None:
        self.frequency = frequency
        self._levels = tuple(copy.deepcopy(tuple(levels)))

    def get_recursive_branch_levels(self) -> tuple[object, ...]:
        return self._levels


@dataclass(frozen=True, slots=True)
class _PreparedSymbol:
    security: SecuritySnapshot
    structure: SymbolStructureSnapshot | None
    bar_closes: tuple[datetime, ...]
    paper_bar: PaperBar | None


@dataclass(frozen=True, slots=True)
class _PreparedCycle:
    bar_closed_at: datetime
    universe: UniverseSnapshot
    structures: Mapping[str, SymbolStructureSnapshot]
    bar_closes: Mapping[str, tuple[datetime, ...]]
    paper_bars: Mapping[str, PaperBar]
    failures: Mapping[str, str]
    required_codes: tuple[str, ...]


class LiveDecisionDataProvider:
    """Freeze one internally consistent A-share 5-minute scan snapshot."""

    def __init__(
        self,
        *,
        universe_resolver: Callable[[], LiveUniverseDefinition],
        state_factory: Callable[[str], object],
        max_completed_bars: int = 2_000,
        paper_participation_rate: Decimal = Decimal("0.01"),
        max_cached_paper_bars: int = 10_000,
    ) -> None:
        if not callable(universe_resolver):
            raise TypeError("universe_resolver must be callable")
        if not callable(state_factory):
            raise TypeError("state_factory must be callable")
        if (
            isinstance(max_completed_bars, bool)
            or not isinstance(max_completed_bars, int)
            or max_completed_bars <= 0
        ):
            raise ValueError("max_completed_bars must be positive")
        if (
            not isinstance(paper_participation_rate, Decimal)
            or not paper_participation_rate.is_finite()
            or paper_participation_rate <= 0
            or paper_participation_rate > Decimal("0.10")
        ):
            raise ValueError(
                "paper_participation_rate must be a Decimal in (0, 0.10]"
            )
        if (
            isinstance(max_cached_paper_bars, bool)
            or not isinstance(max_cached_paper_bars, int)
            or max_cached_paper_bars <= 0
        ):
            raise ValueError("max_cached_paper_bars must be positive")
        self._universe_resolver = universe_resolver
        self._state_factory = state_factory
        self._max_completed_bars = max_completed_bars
        self._paper_participation_rate = paper_participation_rate
        self._max_cached_paper_bars = max_cached_paper_bars
        self._paper_bars_by_id: OrderedDict[str, PaperBar] = OrderedDict()
        self._states: dict[str, object] = {}
        self._signal_observation_store: SQLiteTrustedPaperBarStore | None = None
        self._signal_observation_strategy_run: object | None = None
        self._prepared_signal_observation_batch: (
            PreparedSignalObservationBatch | None
        ) = None
        self._cycle: _PreparedCycle | None = None
        self._lock = Lock()

    @staticmethod
    def _validate_bar_close(value: datetime) -> datetime:
        closed_at = normalize_datetime(value, "bar_closed_at")
        if (
            closed_at.minute % 5 != 0
            or closed_at.second != 0
            or closed_at.microsecond != 0
        ):
            raise ValueError("bar_closed_at must identify a closed 5-minute bar")
        return closed_at

    @staticmethod
    def _trusted_name(
        definition: LiveUniverseDefinition,
        code: str,
    ) -> tuple[str, bool]:
        value = definition.names.get(code)
        if not isinstance(value, str) or not value.strip():
            return code, False
        name = value.strip()
        return name, name.casefold() != code.casefold()

    @staticmethod
    def _empty_security(code: str, name: str) -> SecuritySnapshot:
        return SecuritySnapshot(
            market="a",
            code=code,
            name=name,
            listed_days=None,
            suspended=None,
            delisting=None,
            avg_turnover_20d=None,
            quote_time=None,
            limit_up_locked=None,
            limit_down_locked=None,
        )

    @staticmethod
    def _bar_values(cd: object) -> list[dict[str, object]]:
        getter = getattr(cd, "get_src_klines", None)
        if not callable(getter):
            raise TypeError("operation CL must expose get_src_klines")
        raw_values = getter()
        if isinstance(raw_values, (str, bytes)) or not isinstance(
            raw_values,
            Sequence,
        ):
            raise TypeError("source klines must be a sequence")
        values: list[dict[str, object]] = []
        previous_start: datetime | None = None
        for position, raw in enumerate(raw_values):
            start = _market_datetime(getattr(raw, "date", None), "kline.date")
            if previous_start is not None and start <= previous_start:
                raise ValueError("source klines must be strictly chronological")
            previous_start = start
            source_index = getattr(raw, "index", position)
            if (
                isinstance(source_index, bool)
                or not isinstance(source_index, int)
                or source_index < 0
            ):
                raise ValueError("kline index must be non-negative")
            open_price = _finite_number(getattr(raw, "o", None), "kline.open")
            high = _finite_number(getattr(raw, "h", None), "kline.high")
            low = _finite_number(getattr(raw, "l", None), "kline.low")
            close = _finite_number(getattr(raw, "c", None), "kline.close")
            volume = _finite_number(getattr(raw, "a", None), "kline.volume")
            if min(open_price, high, low, close) <= 0 or volume < 0:
                raise ValueError("kline values are outside valid bounds")
            if not (low <= open_price <= high and low <= close <= high):
                raise ValueError("kline price range is inconsistent")
            values.append(
                {
                    "source_index": source_index,
                    "started_at": start,
                    "closed_at": start + timedelta(minutes=5),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
        return values

    @staticmethod
    def _average_turnover_20d(values: Sequence[Mapping[str, object]]) -> float | None:
        by_day: dict[object, float] = {}
        for bar in values:
            started_at = bar["started_at"]
            if not isinstance(started_at, datetime):
                raise TypeError("started_at must be datetime")
            turnover = float(bar["close"]) * float(bar["volume"])
            if not math.isfinite(turnover) or turnover < 0:
                raise ValueError("turnover must be finite and non-negative")
            by_day[started_at.date()] = by_day.get(started_at.date(), 0.0) + turnover
        if len(by_day) < 20:
            return None
        last_days = sorted(by_day)[-20:]
        return sum(by_day[day] for day in last_days) / 20.0

    @staticmethod
    def _limit_flags(
        code: str,
        name: str,
        close: float,
        previous_close: object,
    ) -> tuple[bool | None, bool | None]:
        try:
            previous = Decimal(str(previous_close))
        except Exception:
            return None, None
        if not previous.is_finite() or previous <= 0:
            return None, None
        board = a_share_board(code, name)
        limit = a_share_limit_pct(board or "")
        if limit is None:
            return None, None
        upper = (previous * (Decimal("1") + limit)).quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
        lower = (previous * (Decimal("1") - limit)).quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
        current = Decimal(str(close))
        return current >= upper, current <= lower

    @staticmethod
    def _trusted_previous_close(state: object) -> Decimal:
        raw = getattr(state, "prev_close", None)
        if isinstance(raw, bool):
            raise ValueError("state.prev_close must be a positive finite number")
        try:
            value = Decimal(str(raw))
        except Exception as exc:
            raise ValueError(
                "state.prev_close must be a positive finite number"
            ) from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("state.prev_close must be a positive finite number")
        return value

    def _participating_shares(self, volume: float) -> int:
        participating = (
            Decimal(str(volume)) * self._paper_participation_rate
        ).to_integral_value(rounding=ROUND_FLOOR)
        return (int(participating) // 100) * 100

    @staticmethod
    def _candidate_time(value: object) -> datetime | None:
        try:
            return _market_datetime(value, "candidate.signal_time")
        except (TypeError, ValueError):
            return None

    @classmethod
    def _selection_flags(
        cls,
        code: str,
        signals: Sequence[Signal],
        candidates: Sequence[object],
    ) -> tuple[bool, bool]:
        trend = tuple(signal for signal in signals if signal.bs_type in TREND_BUYS)
        if not trend:
            return False, False
        matched: list[object] = []
        for signal in trend:
            matches = tuple(
                candidate
                for candidate in candidates
                if getattr(candidate, "code", None) == code
                and getattr(candidate, "bs_type", None) == signal.bs_type
                and cls._candidate_time(getattr(candidate, "signal_time", None))
                == signal.date
            )
            if len(matches) != 1:
                return False, False
            matched.append(matches[0])
        return (
            all(type(getattr(item, "fund_ok", None)) is bool and item.fund_ok for item in matched),
            all(
                type(getattr(item, "comparison_ok", None)) is bool
                and item.comparison_ok
                for item in matched
            ),
        )

    def _prepare_symbol(
        self,
        definition: LiveUniverseDefinition,
        code: str,
        state: object,
        bar_closed_at: datetime,
        *,
        required: bool,
    ) -> _PreparedSymbol:
        name, trusted_name = self._trusted_name(definition, code)
        refresh = getattr(state, "refresh", None)
        if not callable(refresh):
            raise TypeError("state must expose refresh")
        raw_signals = refresh()
        if isinstance(raw_signals, (str, bytes)) or not isinstance(
            raw_signals,
            Sequence,
        ):
            raise TypeError("state refresh must return a sequence")
        signals = tuple(_freeze_signal(value) for value in raw_signals)
        cd = getattr(state, "cd_op", None)
        values = self._bar_values(cd)
        if not values:
            return _PreparedSymbol(
                self._empty_security(code, name),
                None,
                (),
                None,
            )
        if any(value["closed_at"] > bar_closed_at for value in values):
            raise ValueError("operation CL contains a future or unclosed bar")
        completed = tuple(
            value for value in values if value["closed_at"] <= bar_closed_at
        )
        exact = completed[-1]["closed_at"] == bar_closed_at
        listed_days = len({value["started_at"].date() for value in completed})
        turnover = self._average_turnover_20d(completed)
        latest_time = completed[-1]["closed_at"]
        if not exact:
            security = SecuritySnapshot(
                market="a",
                code=code,
                name=name,
                listed_days=listed_days,
                suspended=None,
                delisting=("退" in name) if trusted_name else None,
                avg_turnover_20d=turnover,
                quote_time=latest_time,
                limit_up_locked=None,
                limit_down_locked=None,
            )
            return _PreparedSymbol(
                security,
                None,
                tuple(value["closed_at"] for value in completed),
                None,
            )
        previous_close = self._trusted_previous_close(state)
        limit_up, limit_down = self._limit_flags(
            code,
            name,
            float(completed[-1]["close"]),
            previous_close,
        )
        if None in (limit_up, limit_down):
            raise ValueError("paper bar price-limit facts are unavailable")
        paper_bar = PaperBar(
            code=code,
            opened_at=completed[-1]["started_at"],
            closed_at=completed[-1]["closed_at"],
            open_price=Decimal(str(completed[-1]["open"])),
            close_price=Decimal(str(completed[-1]["close"])),
            previous_close=previous_close,
            suspended=False,
            limit_up_locked=limit_up,
            limit_down_locked=limit_down,
            max_fill_shares=self._participating_shares(
                float(completed[-1]["volume"])
            ),
        )
        security = SecuritySnapshot(
            market="a",
            code=code,
            name=name,
            listed_days=listed_days,
            suspended=False,
            delisting=("退" in name) if trusted_name else None,
            avg_turnover_20d=turnover,
            quote_time=bar_closed_at,
            limit_up_locked=limit_up,
            limit_down_locked=limit_down,
        )
        levels = snapshot_levels(cd)
        frozen_cd = _FrozenCL("5m", levels)
        config_getter = getattr(cd, "get_config", None)
        if not callable(config_getter):
            raise TypeError("operation CL must expose get_config")
        raw_config = config_getter()
        if not isinstance(raw_config, Mapping):
            raise TypeError("operation CL config must be a mapping")
        config = MappingProxyType(copy.deepcopy(dict(raw_config)))
        frozen_bars = tuple(
            MappingProxyType(
                {
                    "closed_at": value["closed_at"],
                    "open": value["open"],
                    "high": value["high"],
                    "low": value["low"],
                    "close": value["close"],
                    "volume": value["volume"],
                }
            )
            for value in completed[-self._max_completed_bars :]
        )
        fund_ok, comparison_ok = self._selection_flags(
            code,
            signals,
            definition.selection_candidates,
        )
        sell_signal_fingerprints = tuple(
            sorted(
                sha256_json(signal)
                for signal in signals
                if signal.bs_type in SELLS
            )
        )
        signal_observation_states = (
            {
                signal_fingerprint: "quarantined_unknown"
                for signal_fingerprint in sell_signal_fingerprints
            }
            if required
            else {}
        )
        structure = SymbolStructureSnapshot(
            frequency="5m",
            cd=frozen_cd,
            signals=signals,
            first_visible_bar=int(completed[-1]["source_index"]),
            completed_bars=frozen_bars,
            config=config,
            operation_bar_closed=True,
            fund_ok=fund_ok,
            comparison_ok=comparison_ok,
            current_cycle_id=build_exit_evaluation_cycle_id(
                code=code,
                frequency="5m",
                bar_closed_at=bar_closed_at,
                structure_source_fingerprint=sha256_json(
                    {
                        "levels": levels,
                        "signals": signals,
                        "first_visible_bar": int(
                            completed[-1]["source_index"]
                        ),
                        "completed_bars": frozen_bars,
                        "config": config,
                    }
                ),
            ),
            signals_first_observed_at={},
            signal_observation_states=signal_observation_states,
        )
        return _PreparedSymbol(
            security,
            structure,
            tuple(value["closed_at"] for value in completed),
            paper_bar,
        )

    def _prepare_cycle(self, bar_closed_at: datetime) -> _PreparedCycle:
        definition = self._universe_resolver()
        if not isinstance(definition, LiveUniverseDefinition):
            raise TypeError("universe_resolver must return LiveUniverseDefinition")
        desired = set(definition.codes)
        for code in tuple(self._states):
            if code not in desired:
                self._states.pop(code, None)
        securities: list[SecuritySnapshot] = []
        structures: dict[str, SymbolStructureSnapshot] = {}
        bar_closes: dict[str, tuple[datetime, ...]] = {}
        paper_bars: dict[str, PaperBar] = {}
        failures: dict[str, str] = {}
        for code in sorted(definition.codes):
            name, _trusted = self._trusted_name(definition, code)
            try:
                state = self._states.get(code)
                if state is None:
                    state = self._state_factory(code)
                    self._states[code] = state
                prepared = self._prepare_symbol(
                    definition,
                    code,
                    state,
                    bar_closed_at,
                    required=code in definition.required_codes,
                )
            except Exception as exc:
                failures[code] = type(exc).__name__
                securities.append(self._empty_security(code, name))
                continue
            securities.append(prepared.security)
            bar_closes[code] = prepared.bar_closes
            if prepared.structure is not None:
                structures[code] = prepared.structure
            if prepared.paper_bar is not None:
                paper_bars[code] = prepared.paper_bar
        if len(paper_bars) > self._max_cached_paper_bars:
            raise RuntimeError("current paper bar set exceeds bounded cache")
        for paper_bar in paper_bars.values():
            existing = self._paper_bars_by_id.get(paper_bar.bar_id)
            if existing is not None and existing != paper_bar:
                raise RuntimeError("paper bar payload identity collision")
            self._paper_bars_by_id[paper_bar.bar_id] = paper_bar
            self._paper_bars_by_id.move_to_end(paper_bar.bar_id)
        while len(self._paper_bars_by_id) > self._max_cached_paper_bars:
            self._paper_bars_by_id.popitem(last=False)
        universe = UniverseSnapshot(bar_closed_at, tuple(securities))
        return _PreparedCycle(
            bar_closed_at=bar_closed_at,
            universe=universe,
            structures=MappingProxyType(structures),
            bar_closes=MappingProxyType(bar_closes),
            paper_bars=MappingProxyType(paper_bars),
            failures=MappingProxyType(failures),
            required_codes=definition.required_codes,
        )

    def bind_signal_observation_store(
        self,
        store: SQLiteTrustedPaperBarStore,
        strategy_run: object,
    ) -> None:
        if not isinstance(store, SQLiteTrustedPaperBarStore):
            raise TypeError("store must be SQLiteTrustedPaperBarStore")
        with self._lock:
            if self._cycle is not None:
                raise RuntimeError(
                    "signal observation store must bind before first universe access"
                )
            if self._signal_observation_store not in (None, store):
                raise RuntimeError("signal observation store cannot be rebound")
            store.bind_signal_observation_strategy_run(strategy_run)
            self._signal_observation_store = store
            self._signal_observation_strategy_run = strategy_run

    def prepare_signal_observation_cycle(
        self,
        bar_closed_at: datetime,
    ) -> PreparedSignalObservationBatch:
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            store = self._signal_observation_store
            if store is None or self._signal_observation_strategy_run is None:
                raise RuntimeError("signal observation store is not bound")
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise RuntimeError(
                    "frozen signal observation cycle is unavailable"
                )
            manifests: dict[str, tuple[str, ...]] = {}
            for code in self._cycle.required_codes:
                structure = self._cycle.structures.get(code)
                if structure is None:
                    raise RuntimeError(
                        "required signal observation structure is unavailable"
                    )
                manifests[code] = tuple(
                    sorted(
                        sha256_json(signal)
                        for signal in structure.signals
                        if signal.bs_type in SELLS
                    )
                )
            prepared = store.prepare_signal_observation_batch(
                closed_at,
                manifests,
            )
            structures = dict(self._cycle.structures)
            for code in self._cycle.required_codes:
                structures[code] = replace(
                    structures[code],
                    signals_first_observed_at=(
                        prepared.first_observed_at[code]
                    ),
                    signal_observation_states=prepared.states[code],
                )
            self._cycle = replace(
                self._cycle,
                structures=MappingProxyType(structures),
            )
            self._prepared_signal_observation_batch = prepared
            return prepared

    def signal_observation_batch(
        self,
        bar_closed_at: datetime,
    ) -> PreparedSignalObservationBatch:
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            prepared = self._prepared_signal_observation_batch
            if prepared is None or prepared.bar_closed_at != closed_at:
                raise RuntimeError(
                    "prepared signal observation batch is unavailable"
                )
            return prepared

    def universe_provider(self, bar_closed_at: datetime) -> UniverseSnapshot:
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                self._cycle = self._prepare_cycle(closed_at)
                self._prepared_signal_observation_batch = None
            return self._cycle.universe

    def structure_provider(
        self,
        security: EligibleSecurity,
        bar_closed_at: datetime,
    ) -> SymbolStructureSnapshot:
        closed_at = self._validate_bar_close(bar_closed_at)
        code = getattr(security, "code", None)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise KeyError("no current eligible structure for requested bar")
            structure = self._cycle.structures.get(code)
            if structure is None:
                raise KeyError("no current eligible structure for requested bar")
            return structure

    def structure_for_code(
        self,
        code: str,
        bar_closed_at: datetime,
    ) -> SymbolStructureSnapshot:
        """Return a pinned position's structure without entry-universe filtering."""

        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise KeyError("no current position structure for requested bar")
            structure = self._cycle.structures.get(code)
            if structure is None:
                raise KeyError("no current position structure for requested bar")
            return structure

    def quote_for_code(
        self,
        code: str,
        bar_closed_at: datetime,
    ) -> QuoteSnapshot:
        """Return a frozen quote for exits, including entry-excluded positions."""

        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise KeyError("no current position quote for requested bar")
            structure = self._cycle.structures.get(code)
            security = next(
                (
                    item
                    for item in self._cycle.universe.securities
                    if item.code == code
                ),
                None,
            )
            if structure is None or security is None or not structure.completed_bars:
                raise KeyError("no current position quote for requested bar")
            latest = structure.completed_bars[-1]
            if latest.get("closed_at") != closed_at:
                raise KeyError("no current position quote for requested bar")
            price = Decimal(str(latest.get("close")))
            if not price.is_finite() or price <= 0:
                raise ValueError("current position quote price is invalid")
            suspended = security.suspended is not False
            return QuoteSnapshot(
                code=code,
                price=price,
                quote_time=closed_at,
                entry_tradable=(
                    not suspended and not bool(security.limit_up_locked)
                ),
                exit_tradable=(
                    not suspended and not bool(security.limit_down_locked)
                ),
                limit_up_locked=bool(security.limit_up_locked),
                limit_down_locked=bool(security.limit_down_locked),
            )

    def paper_bar(self, code: str, bar_closed_at: datetime) -> PaperBar:
        """Return the current cycle's canonical paper bar for one code."""

        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise KeyError("no current canonical paper bar for requested bar")
            bar = self._cycle.paper_bars.get(code)
            if bar is None:
                raise KeyError("no current canonical paper bar for requested bar")
            return bar

    def get_bar(self, bar_id: str) -> PaperBar | None:
        """Resolve an immutable canonical bar by its full-payload identity."""

        if not isinstance(bar_id, str) or not bar_id:
            return None
        with self._lock:
            return self._paper_bars_by_id.get(bar_id)

    def risk_quote(
        self,
        security: EligibleSecurity,
        bar_closed_at: datetime,
    ) -> QuoteSnapshot:
        if not isinstance(security, EligibleSecurity):
            raise TypeError("security must be EligibleSecurity")
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise RuntimeError("frozen quote is unavailable for requested bar")
            structure = self._cycle.structures.get(security.code)
            if structure is None or not structure.completed_bars:
                raise RuntimeError("frozen quote is unavailable for requested bar")
            latest = structure.completed_bars[-1]
            if latest.get("closed_at") != closed_at:
                raise RuntimeError("frozen quote does not match requested bar")
            price = Decimal(str(latest.get("close")))
        return QuoteSnapshot(
            code=security.code,
            price=price,
            quote_time=security.quote_time,
            entry_tradable=security.entry_tradable,
            exit_tradable=security.exit_tradable,
            limit_up_locked=bool(security.security.limit_up_locked),
            limit_down_locked=bool(security.security.limit_down_locked),
        )

    def failures(self, bar_closed_at: datetime) -> dict[str, str]:
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                return {}
            return dict(self._cycle.failures)

    def required_codes(self, bar_closed_at: datetime) -> tuple[str, ...]:
        closed_at = self._validate_bar_close(bar_closed_at)
        with self._lock:
            if self._cycle is None or self._cycle.bar_closed_at != closed_at:
                raise RuntimeError(
                    "frozen required-code set is unavailable for requested bar"
                )
            return self._cycle.required_codes

    def count_closed_bars(self, event: object, asof: datetime) -> int:
        """Count only operation bars present in the latest frozen feed view."""

        if getattr(event, "market", None) != "a":
            raise ValueError("closed-bar clock supports only A shares")
        if getattr(event, "signal_frequency", None) != "5m":
            raise ValueError("closed-bar clock supports only 5-minute events")
        code = getattr(event, "code", None)
        if not isinstance(code, str) or not code:
            raise ValueError("event code must be a non-empty string")
        observed_at = normalize_datetime(
            getattr(event, "observed_at", None),
            "event.observed_at",
        )
        normalized_asof = normalize_datetime(asof, "asof")
        if normalized_asof < observed_at:
            raise ValueError("asof cannot be before event observation")
        with self._lock:
            if self._cycle is None or code not in self._cycle.bar_closes:
                raise RuntimeError("observed bars unavailable for event")
            closes = self._cycle.bar_closes[code]
            return sum(
                observed_at < closed_at <= normalized_asof
                for closed_at in closes
            )


def live_data_provider_from_dynamic_monitor(
    monitor: object,
    *,
    max_completed_bars: int = 2_000,
    pinned_codes_provider: Callable[[], Sequence[str]] | None = None,
    paper_participation_rate: Decimal = Decimal("0.01"),
    max_cached_paper_bars: int = 10_000,
) -> LiveDecisionDataProvider:
    """Reuse monitor configuration/universe while owning separate CL states.

    The returned provider never calls the monitor's run loop.  In particular,
    it does not share ``monitor.states`` and cannot reach legacy notification
    or execution side effects.
    """

    if getattr(monitor, "market", None) != "a":
        raise ValueError("dynamic monitor adapter supports only A shares")
    if pinned_codes_provider is not None and not callable(pinned_codes_provider):
        raise TypeError("pinned_codes_provider must be callable")
    monitor_lock = getattr(monitor, "_lock", None)
    if not callable(getattr(monitor_lock, "acquire", None)) or not callable(
        getattr(monitor_lock, "release", None)
    ):
        raise TypeError("dynamic monitor must expose a runtime lock")
    current_universe = getattr(monitor, "current_universe", None)
    exchange_getter = getattr(monitor, "_exchange", None)
    new_state = getattr(monitor, "_new_state", None)
    if not all(callable(value) for value in (current_universe, exchange_getter, new_state)):
        raise TypeError("dynamic monitor is missing analysis provider methods")

    def resolve() -> LiveUniverseDefinition:
        pinned_codes: tuple[str, ...] = ()
        if pinned_codes_provider is not None:
            raw_pinned = pinned_codes_provider()
            if isinstance(raw_pinned, (str, bytes)) or not isinstance(
                raw_pinned,
                Sequence,
            ):
                raise TypeError("pinned_codes_provider must return a sequence")
            pinned_codes = tuple(raw_pinned)
            if any(
                not isinstance(code, str) or not code.strip()
                for code in pinned_codes
            ):
                raise ValueError("pinned codes must be non-empty strings")
            if len(pinned_codes) != len(set(pinned_codes)):
                raise ValueError("pinned codes must not contain duplicates")
        with monitor_lock:
            resolved = current_universe()
            if (
                not isinstance(resolved, tuple)
                or len(resolved) != 2
                or isinstance(resolved[0], (str, bytes))
                or not isinstance(resolved[0], Sequence)
                or not isinstance(resolved[1], Mapping)
            ):
                raise TypeError("current_universe must return codes and names")
            base_codes = tuple(resolved[0])
            base_code_set = set(base_codes)
            codes = base_codes + tuple(
                code for code in pinned_codes if code not in base_code_set
            )
            names = dict(resolved[1])
            exchange = exchange_getter()
            for code in codes:
                if isinstance(names.get(code), str) and names[code].strip():
                    continue
                try:
                    info = exchange.stock_info(code)
                except Exception:
                    info = None
                name = (info or {}).get("name") if isinstance(info, Mapping) else None
                if isinstance(name, str) and name.strip():
                    names[code] = name.strip()
            candidates = tuple(
                getattr(monitor, "last_selection_candidates", ()) or ()
            )
        return LiveUniverseDefinition(
            market="a",
            codes=codes,
            names=names,
            selection_candidates=candidates,
            required_codes=pinned_codes,
        )

    def create_state(code: str) -> object:
        with monitor_lock:
            return new_state(code, exchange_getter())

    return LiveDecisionDataProvider(
        universe_resolver=resolve,
        state_factory=create_state,
        max_completed_bars=max_completed_bars,
        paper_participation_rate=paper_participation_rate,
        max_cached_paper_bars=max_cached_paper_bars,
    )


def make_risk_context_provider(
    *,
    data_provider: LiveDecisionDataProvider,
    account_provider: Callable[[datetime], RiskAccountSnapshot],
) -> Callable[[EligibleSecurity, object, datetime], RiskContext]:
    if not isinstance(data_provider, LiveDecisionDataProvider):
        raise TypeError("data_provider must be LiveDecisionDataProvider")
    if not callable(account_provider):
        raise TypeError("account_provider must be callable")

    def provide(
        security: EligibleSecurity,
        event: object,
        bar_closed_at: datetime,
    ) -> RiskContext:
        closed_at = data_provider._validate_bar_close(bar_closed_at)
        if getattr(event, "code", None) != security.code:
            raise ValueError("event and security code mismatch")
        account = account_provider(closed_at)
        if not isinstance(account, RiskAccountSnapshot):
            raise TypeError("account_provider must return RiskAccountSnapshot")
        if account.asof != closed_at:
            raise RuntimeError("account snapshot is not current for requested bar")
        return RiskContext(
            account_equity=account.account_equity,
            day_start_equity=account.day_start_equity,
            available_cash=account.available_cash,
            holdings=account.holdings,
            pending_exits=account.pending_exits,
            day_pnl=account.day_pnl,
            strategy_drawdown=account.strategy_drawdown,
            daily_loss_locked=account.daily_loss_locked,
            drawdown_locked=account.drawdown_locked,
            quote=data_provider.risk_quote(security, closed_at),
            asof=closed_at,
        )

    return provide


@dataclass(frozen=True, slots=True)
class DecisionSupportComposition:
    data_provider: object
    scanner: DecisionScanner
    runtime: DecisionSupportRuntime


def build_decision_support_runtime(
    *,
    data_provider: object,
    risk_context_provider: Callable[..., object] | None,
    event_service: object,
    rule_engine: object,
    reviewer: Callable[[str], object],
    manual_check_workflow: object | None = None,
    event_strategy_run_binder: Callable[[object], object] | None = None,
    monitor_config: MonitorConfig | None = None,
    pending_review_loader: Callable[[], Sequence[str]] | None = None,
    universe_policy: UniversePolicy | None = None,
    max_market_age_seconds: int = 300,
    processed_bar_limit: int = 2_048,
) -> DecisionSupportComposition:
    if not callable(risk_context_provider):
        raise TypeError("risk_context_provider must be an explicit callable")
    universe_provider = getattr(data_provider, "universe_provider", None)
    structure_provider = getattr(data_provider, "structure_provider", None)
    if not callable(universe_provider) or not callable(structure_provider):
        raise TypeError("data_provider must expose universe and structure providers")
    if not callable(reviewer):
        raise TypeError("reviewer must be callable")
    if event_strategy_run_binder is not None and not callable(
        event_strategy_run_binder
    ):
        raise TypeError("event_strategy_run_binder must be callable")
    scanner = DecisionScanner(
        universe_provider=universe_provider,
        structure_provider=structure_provider,
        risk_context_provider=risk_context_provider,
        event_service=event_service,
        rule_engine=rule_engine,
        manual_check_workflow=manual_check_workflow,
        event_strategy_run_binder=event_strategy_run_binder,
        universe_policy=universe_policy,
        max_market_age_seconds=max_market_age_seconds,
        processed_bar_limit=processed_bar_limit,
    )
    runtime = DecisionSupportRuntime(
        scanner,
        reviewer,
        config=monitor_config,
        pending_review_loader=pending_review_loader,
    )
    return DecisionSupportComposition(data_provider, scanner, runtime)
