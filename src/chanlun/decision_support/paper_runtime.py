"""Restart-safe research-paper orchestration primitives.

This module has no broker, order-submission, or live execution surface.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .exit_evaluation_store import (
    ExitEvaluationCommitment,
    ExitEvaluationIntegrityError,
    ExitEvaluationService,
    ExitEvaluationSnapshot,
)
from .exit_runtime import ExitEvaluationRequest
from .fingerprints import normalize_datetime, sha256_json
from .models import EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .paper_adapter import PaperBar
from .risk import (
    HoldingSnapshot,
    PendingExitSnapshot,
    QuoteSnapshot,
    RiskContext,
    RiskPolicy,
)


LIVE_ORDER_CAPABILITY = False
_CN = ZoneInfo("Asia/Shanghai")


class TrustedPaperBarIntegrityError(RuntimeError):
    """A persisted canonical bar or its sequence failed closed."""


def _valid_sha256_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


@dataclass(frozen=True, slots=True)
class PaperTradingSession:
    """Auditable A-share trading-day identity supplied by a trusted calendar."""

    trading_day: date
    previous_trading_day: date | None
    expected_bar_closes: tuple[datetime, ...]
    calendar_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.trading_day, date) or isinstance(
            self.trading_day,
            datetime,
        ):
            raise TypeError("trading_day must be a date")
        if self.previous_trading_day is not None and (
            not isinstance(self.previous_trading_day, date)
            or isinstance(self.previous_trading_day, datetime)
            or self.previous_trading_day >= self.trading_day
        ):
            raise ValueError("previous_trading_day must precede trading_day")
        if not _valid_sha256_fingerprint(self.calendar_fingerprint):
            raise ValueError("calendar_fingerprint must be a sha256 fingerprint")
        closes = tuple(
            normalize_datetime(value, "expected_bar_close")
            for value in self.expected_bar_closes
        )
        expected_times: list[time] = []
        current = datetime.combine(self.trading_day, time(9, 35), _CN)
        while current.time() <= time(11, 30):
            expected_times.append(current.time())
            current += timedelta(minutes=5)
        current = datetime.combine(self.trading_day, time(13, 5), _CN)
        while current.time() <= time(15, 0):
            expected_times.append(current.time())
            current += timedelta(minutes=5)
        if (
            len(closes) != 48
            or len(closes) != len(set(closes))
            or tuple(sorted(closes)) != closes
            or any(value.astimezone(_CN).date() != self.trading_day for value in closes)
            or tuple(value.astimezone(_CN).time() for value in closes)
            != tuple(expected_times)
        ):
            raise ValueError(
                "expected_bar_closes must be the exact 48 A-share 5-minute closes"
            )
        object.__setattr__(self, "expected_bar_closes", closes)


class ExplicitPaperTradingCalendar:
    """Finite, audited A-share calendar with no weekday/holiday inference."""

    _JSON_FIELDS = {
        "schema_version",
        "market",
        "timezone",
        "source_id",
        "source_fingerprint",
        "coverage_start",
        "coverage_end",
        "trading_days",
        "calendar_fingerprint",
    }

    def __init__(
        self,
        trading_days: Sequence[date],
        *,
        source_id: str,
        source_fingerprint: str,
        coverage_start: date | None = None,
        coverage_end: date | None = None,
    ) -> None:
        if isinstance(trading_days, (str, bytes)):
            raise TypeError("trading_days must be a sequence of dates")
        days = tuple(trading_days)
        if (
            not days
            or any(
                not isinstance(value, date) or isinstance(value, datetime)
                for value in days
            )
            or tuple(sorted(days)) != days
            or len(days) != len(set(days))
        ):
            raise ValueError(
                "trading_days must be non-empty, unique, and strictly increasing"
            )
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or source_id != source_id.strip()
            or len(source_id) > 255
        ):
            raise ValueError("source_id must be a bounded non-empty string")
        if not _valid_sha256_fingerprint(source_fingerprint):
            raise ValueError("source_fingerprint must be a sha256 fingerprint")
        coverage_start = days[0] if coverage_start is None else coverage_start
        coverage_end = days[-1] if coverage_end is None else coverage_end
        if (
            not isinstance(coverage_start, date)
            or isinstance(coverage_start, datetime)
            or not isinstance(coverage_end, date)
            or isinstance(coverage_end, datetime)
            or coverage_start > days[0]
            or coverage_end < days[-1]
            or coverage_start > coverage_end
        ):
            raise ValueError(
                "calendar coverage must contain every explicit trading day"
            )
        self._trading_days = days
        self._day_positions = {
            trading_day: position
            for position, trading_day in enumerate(days)
        }
        self.source_id = source_id
        self.source_fingerprint = source_fingerprint
        self.coverage_start = coverage_start
        self.coverage_end = coverage_end
        self.fingerprint = sha256_json(self._identity_payload())

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "market": "a",
            "timezone": "Asia/Shanghai",
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "trading_days": [value.isoformat() for value in self._trading_days],
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._identity_payload(),
            "calendar_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_json_file(cls, path: str | Path) -> ExplicitPaperTradingCalendar:
        source_path = Path(path).expanduser().absolute()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_json_invalid"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != cls._JSON_FIELDS:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_json_schema_invalid"
            )
        if (
            raw.get("schema_version") != 1
            or raw.get("market") != "a"
            or raw.get("timezone") != "Asia/Shanghai"
            or not isinstance(raw.get("trading_days"), list)
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_json_schema_invalid"
            )
        try:
            trading_days = tuple(
                date.fromisoformat(value)
                for value in raw["trading_days"]
            )
            calendar = cls(
                trading_days,
                source_id=raw["source_id"],
                source_fingerprint=raw["source_fingerprint"],
                coverage_start=date.fromisoformat(raw["coverage_start"]),
                coverage_end=date.fromisoformat(raw["coverage_end"]),
            )
        except (TypeError, ValueError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_json_schema_invalid"
            ) from exc
        if raw.get("calendar_fingerprint") != calendar.fingerprint:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_declared_fingerprint_mismatch"
            )
        return calendar

    @staticmethod
    def _expected_closes(trading_day: date) -> tuple[datetime, ...]:
        values: list[datetime] = []
        current = datetime.combine(trading_day, time(9, 35), _CN)
        while current.time() <= time(11, 30):
            values.append(current)
            current += timedelta(minutes=5)
        current = datetime.combine(trading_day, time(13, 5), _CN)
        while current.time() <= time(15, 0):
            values.append(current)
            current += timedelta(minutes=5)
        return tuple(values)

    def session_for(self, trading_day: date) -> PaperTradingSession | None:
        if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
            raise TypeError("trading_day must be a date")
        if not self.coverage_start <= trading_day <= self.coverage_end:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_date_out_of_coverage"
            )
        position = self._day_positions.get(trading_day)
        if position is None:
            return None
        previous = (
            None if position == 0 else self._trading_days[position - 1]
        )
        return PaperTradingSession(
            trading_day=trading_day,
            previous_trading_day=previous,
            expected_bar_closes=self._expected_closes(trading_day),
            calendar_fingerprint=self.fingerprint,
        )


def _validated_trading_session(
    value: object,
    *,
    calendar_fingerprint: str,
) -> PaperTradingSession:
    if isinstance(value, PaperTradingSession):
        session = value
    else:
        session = PaperTradingSession(
            trading_day=getattr(value, "trading_day", None),
            previous_trading_day=getattr(value, "previous_trading_day", None),
            expected_bar_closes=tuple(
                getattr(value, "expected_bar_closes", ())
            ),
            calendar_fingerprint=getattr(
                value,
                "calendar_fingerprint",
                None,
            ),
        )
    if session.calendar_fingerprint != calendar_fingerprint:
        raise TrustedPaperBarIntegrityError(
            "paper_calendar_fingerprint_mismatch"
        )
    return session


@dataclass(frozen=True, slots=True)
class TrustedPaperBarStoreHealth:
    bar_count: int
    observed_trading_days: int
    degraded: bool
    degraded_reason: str | None
    last_bar_closed_at: datetime | None
    last_attempted_bar_closed_at: datetime | None = None
    last_attempt_complete: bool | None = None
    last_attempt_failure: str | None = None
    calendar_preflight_failure_at: datetime | None = None
    calendar_preflight_failure: str | None = None


_SIGNAL_OBSERVATION_STATES = frozenset(
    {
        "trusted_first_seen",
        "baseline_not_fresh",
        "quarantined_unknown",
    }
)


@dataclass(frozen=True, slots=True)
class PreparedSignalObservationBatch:
    run_id: str
    epoch: int
    strategy_run_fingerprint: str
    identity_sha256: str
    store_instance_id: str
    bar_closed_at: datetime
    manifests: Mapping[str, tuple[str, ...]]
    segment_ids: Mapping[str, str]
    states: Mapping[str, Mapping[str, str]]
    first_observed_at: Mapping[str, Mapping[str, datetime]]
    attempt_generation: int
    prior_attempt_ambiguous: bool
    resolution_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a non-empty string")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise ValueError("epoch must be a positive integer")
        for field_name in (
            "strategy_run_fingerprint",
            "identity_sha256",
            "resolution_sha256",
        ):
            if not _valid_sha256_fingerprint(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be a sha256 fingerprint")
        if not isinstance(self.store_instance_id, str) or not self.store_instance_id:
            raise ValueError("store_instance_id must be a non-empty string")
        if (
            isinstance(self.attempt_generation, bool)
            or not isinstance(self.attempt_generation, int)
            or self.attempt_generation <= 0
            or not isinstance(self.prior_attempt_ambiguous, bool)
            or self.prior_attempt_ambiguous != (self.attempt_generation > 1)
        ):
            raise ValueError("signal observation attempt binding is invalid")
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        manifests = {
            code: tuple(signal_fingerprints)
            for code, signal_fingerprints in self.manifests.items()
        }
        segment_ids = dict(self.segment_ids)
        states = {
            code: MappingProxyType(dict(signal_states))
            for code, signal_states in self.states.items()
        }
        first_observed_at = {
            code: MappingProxyType(
                {
                    signal_fingerprint: normalize_datetime(
                        observed_at,
                        "first_observed_at",
                    )
                    for signal_fingerprint, observed_at in observations.items()
                }
            )
            for code, observations in self.first_observed_at.items()
        }
        if not (
            set(manifests)
            == set(segment_ids)
            == set(states)
            == set(first_observed_at)
        ):
            raise ValueError("signal observation code bindings are inconsistent")
        for code, signal_fingerprints in manifests.items():
            if (
                not isinstance(code, str)
                or not code
                or tuple(sorted(signal_fingerprints)) != signal_fingerprints
                or len(set(signal_fingerprints)) != len(signal_fingerprints)
                or any(
                    not _valid_sha256_fingerprint(value)
                    for value in signal_fingerprints
                )
                or not isinstance(segment_ids[code], str)
                or not segment_ids[code]
                or set(states[code]) != set(signal_fingerprints)
                or not set(first_observed_at[code]).issubset(signal_fingerprints)
                or any(
                    value not in _SIGNAL_OBSERVATION_STATES
                    for value in states[code].values()
                )
            ):
                raise ValueError("signal observation payload is invalid")
        object.__setattr__(self, "manifests", MappingProxyType(manifests))
        object.__setattr__(self, "segment_ids", MappingProxyType(segment_ids))
        object.__setattr__(self, "states", MappingProxyType(states))
        object.__setattr__(
            self,
            "first_observed_at",
            MappingProxyType(first_observed_at),
        )


@dataclass(frozen=True, slots=True)
class PaperBarCycleResult:
    code: str
    occurred_at: datetime
    bar_closed_at: datetime | None
    persisted_bar_count: int = 0
    fill_count: int = 0
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PaperAdmissionCycleResult:
    occurred_at: datetime
    admitted_count: int
    skipped_count: int
    failures: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", MappingProxyType(dict(self.failures)))


@dataclass(frozen=True, slots=True)
class PaperResearchHealth:
    mode: str
    auto_order_enabled: bool
    live_order_capability: bool
    bar_store: TrustedPaperBarStoreHealth
    bar_cycles: int
    bar_cycle_failures: int
    admission_cycles: int
    admission_failures: int
    admitted_event_count: int
    last_error: str | None
    exit_coverage: PaperExitCoverageHealth | None = None


@dataclass(frozen=True, slots=True)
class PaperRiskMark:
    revision: int
    asof: datetime
    account_equity: Decimal
    day_start_equity: Decimal
    high_water_equity: Decimal
    day_pnl: Decimal
    strategy_drawdown: Decimal
    daily_loss_locked: bool
    drawdown_locked: bool


@dataclass(frozen=True, slots=True)
class PaperExitCoverage:
    bar_closed_at: datetime
    open_entry_ids: tuple[str, ...]
    snapshot_entry_ids: tuple[str, ...]
    failures: Mapping[str, str]
    scan_code: str | None = None
    cycle_failure: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        open_ids = tuple(sorted(self.open_entry_ids))
        snapshot_ids = tuple(sorted(self.snapshot_entry_ids))
        if (
            len(open_ids) != len(set(open_ids))
            or len(snapshot_ids) != len(set(snapshot_ids))
            or any(not isinstance(value, str) or not value for value in open_ids)
            or any(not isinstance(value, str) or not value for value in snapshot_ids)
        ):
            raise ValueError("exit coverage identities must be unique non-empty strings")
        failure_map = dict(self.failures)
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(reason, str)
            or not reason
            or len(reason) > 255
            for key, reason in failure_map.items()
        ):
            raise ValueError("exit coverage failures must be bounded strings")
        if (
            set(snapshot_ids).intersection(failure_map)
            or set(snapshot_ids).union(failure_map) != set(open_ids)
        ):
            raise ValueError("exit coverage must account for every open entry exactly once")
        if self.scan_code is not None and (
            not isinstance(self.scan_code, str)
            or not self.scan_code
            or len(self.scan_code) > 255
        ):
            raise ValueError("scan_code must be a bounded non-empty string")
        if self.cycle_failure is not None and (
            not isinstance(self.cycle_failure, str)
            or not self.cycle_failure
            or len(self.cycle_failure) > 255
        ):
            raise ValueError("cycle_failure must be a bounded non-empty string")
        object.__setattr__(self, "open_entry_ids", open_ids)
        object.__setattr__(self, "snapshot_entry_ids", snapshot_ids)
        object.__setattr__(
            self,
            "failures",
            MappingProxyType(dict(sorted(failure_map.items()))),
        )

    @property
    def complete(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PaperExitCoverageHealth:
    bar_closed_at: datetime
    open_entry_count: int
    snapshot_count: int
    failure_count: int
    complete: bool
    fresh: bool
    scan_code: str | None
    cycle_failure: str | None
    failures: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        object.__setattr__(self, "failures", MappingProxyType(dict(self.failures)))


@dataclass(frozen=True, slots=True)
class PaperExitCycleResult:
    bar_closed_at: datetime
    evaluated_count: int
    failures: Mapping[str, str]
    cycle_failure: str | None = None
    commitments: tuple[ExitEvaluationCommitment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bar_closed_at",
            normalize_datetime(self.bar_closed_at, "bar_closed_at"),
        )
        if (
            isinstance(self.evaluated_count, bool)
            or not isinstance(self.evaluated_count, int)
            or self.evaluated_count < 0
        ):
            raise ValueError("evaluated_count must be a non-negative integer")
        if self.cycle_failure is not None and (
            not isinstance(self.cycle_failure, str)
            or not self.cycle_failure
            or len(self.cycle_failure) > 255
        ):
            raise ValueError("cycle_failure must be a bounded non-empty string")
        commitments = SQLiteTrustedPaperBarStore._normalize_exit_commitments(
            self.commitments
        )
        if len(commitments) != self.evaluated_count:
            raise ValueError(
                "exit commitments must match evaluated_count exactly"
            )
        object.__setattr__(self, "commitments", commitments)
        object.__setattr__(self, "failures", MappingProxyType(dict(self.failures)))


def _payload_sha256(payload_json: str) -> str:
    return "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _bar_payload(bar: PaperBar) -> dict[str, object]:
    return {
        "schema_version": 2,
        "bar_id": bar.bar_id,
        "code": bar.code,
        "opened_at": bar.opened_at.isoformat(),
        "closed_at": bar.closed_at.isoformat(),
        "open_price": _decimal_text(bar.open_price),
        "close_price": _decimal_text(bar.close_price),
        "previous_close": _decimal_text(bar.previous_close),
        "suspended": bar.suspended,
        "limit_up_locked": bar.limit_up_locked,
        "limit_down_locked": bar.limit_down_locked,
        "max_fill_shares": bar.max_fill_shares,
    }


def _bar_json(bar: PaperBar) -> str:
    return json.dumps(
        _bar_payload(bar),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}") from exc
    try:
        return normalize_datetime(parsed, field_name)
    except (TypeError, ValueError) as exc:
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}") from exc


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise TrustedPaperBarIntegrityError(f"invalid_{field_name}")
    return parsed


def _paper_bar_from_row(row: tuple[object, ...]) -> PaperBar:
    bar_id, code, opened_at, closed_at, payload_json, payload_sha256 = row
    if not isinstance(payload_json, str) or not isinstance(payload_sha256, str):
        raise TrustedPaperBarIntegrityError("trusted_bar_payload_invalid")
    if _payload_sha256(payload_json) != payload_sha256:
        raise TrustedPaperBarIntegrityError("trusted_bar_checksum_mismatch")
    try:
        raw = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise TrustedPaperBarIntegrityError("trusted_bar_payload_invalid") from exc
    expected_fields = {
        "schema_version",
        "bar_id",
        "code",
        "opened_at",
        "closed_at",
        "open_price",
        "close_price",
        "previous_close",
        "suspended",
        "limit_up_locked",
        "limit_down_locked",
        "max_fill_shares",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise TrustedPaperBarIntegrityError("trusted_bar_payload_invalid")
    if raw["schema_version"] != 2:
        raise TrustedPaperBarIntegrityError("trusted_bar_schema_unsupported")
    for field_name in ("suspended", "limit_up_locked", "limit_down_locked"):
        if type(raw[field_name]) is not bool:
            raise TrustedPaperBarIntegrityError(f"invalid_{field_name}")
    max_fill_shares = raw["max_fill_shares"]
    if max_fill_shares is not None and (
        isinstance(max_fill_shares, bool)
        or not isinstance(max_fill_shares, int)
        or max_fill_shares < 0
    ):
        raise TrustedPaperBarIntegrityError("invalid_max_fill_shares")
    try:
        bar = PaperBar(
            code=str(raw["code"]),
            opened_at=_parse_datetime(raw["opened_at"], "opened_at"),
            closed_at=_parse_datetime(raw["closed_at"], "closed_at"),
            open_price=_parse_decimal(raw["open_price"], "open_price"),
            close_price=_parse_decimal(raw["close_price"], "close_price"),
            previous_close=_parse_decimal(
                raw["previous_close"],
                "previous_close",
            ),
            suspended=raw["suspended"],
            limit_up_locked=raw["limit_up_locked"],
            limit_down_locked=raw["limit_down_locked"],
            max_fill_shares=max_fill_shares,
        )
    except (TypeError, ValueError) as exc:
        raise TrustedPaperBarIntegrityError("trusted_bar_payload_invalid") from exc
    if (
        not isinstance(bar_id, str)
        or not isinstance(code, str)
        or raw["bar_id"] != bar_id
        or bar.bar_id != bar_id
        or bar.code != code
        or bar.opened_at != _parse_datetime(opened_at, "row_opened_at")
        or bar.closed_at != _parse_datetime(closed_at, "row_closed_at")
        or _bar_json(bar) != payload_json
    ):
        raise TrustedPaperBarIntegrityError("trusted_bar_identity_mismatch")
    return bar


def _validate_session_bar(bar: PaperBar) -> None:
    if bar.closed_at - bar.opened_at != timedelta(minutes=5):
        raise TrustedPaperBarIntegrityError("paper_bar_not_five_minutes")
    if bar.opened_at.date() != bar.closed_at.date() or bar.opened_at.weekday() >= 5:
        raise TrustedPaperBarIntegrityError("paper_bar_outside_a_share_session")
    opened = bar.opened_at.timetz().replace(tzinfo=None)
    closed = bar.closed_at.timetz().replace(tzinfo=None)
    sessions = (
        (time(9, 30), time(11, 30)),
        (time(13, 0), time(15, 0)),
    )
    if not any(start <= opened < closed <= end for start, end in sessions):
        raise TrustedPaperBarIntegrityError("paper_bar_outside_a_share_session")


def _expected_next_close(previous: datetime, current: datetime) -> datetime | None:
    if current.date() != previous.date():
        return None
    previous_time = previous.timetz().replace(tzinfo=None)
    if previous_time == time(11, 30):
        return previous.replace(hour=13, minute=5)
    if previous_time == time(15, 0):
        return None
    return previous + timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _ExitManifestValidatedPrefix:
    event_count: int
    max_sequence: int
    history_head_sha256: str | None
    log_state_payload_sha256: str
    tail_closed_at: str | None
    tail_previous_manifest_sha256: str | None
    tail_payload_sha256: str | None


class SQLiteTrustedPaperBarStore:
    """Immutable canonical PaperBar archive and observation continuity gate."""

    def __init__(
        self,
        path: str | Path,
        *,
        calendar_fingerprint: str | None = None,
    ) -> None:
        if calendar_fingerprint is not None and not _valid_sha256_fingerprint(
            calendar_fingerprint
        ):
            raise ValueError(
                "calendar_fingerprint must be a sha256 fingerprint"
            )
        self._path = Path(path).expanduser().absolute()
        self._calendar_fingerprint = calendar_fingerprint
        self._lock = RLock()
        self._mutation_fence = MutationLeaseGuard()
        self._preflight_fail_stop_path = self._path.with_name(
            self._path.name + ".calendar-preflight-fail-stop.json"
        )
        self._preflight_fail_stop_dir = self._path.with_name(
            self._path.name + ".calendar-preflight-fail-stop"
        )
        self._signal_observation_anchor_dir = self._path.with_name(
            self._path.name + ".signal-observation-anchor"
        )
        self._exit_manifest_anchor_dir = self._path.with_name(
            self._path.name + ".exit-manifest-anchor"
        )
        self._process_fail_stop_reason: str | None = None
        self._process_fail_stop_at: datetime | None = None
        self._signal_observation_strategy_run: object | None = None
        self._strategy_run_binding: tuple[str, int, str, str, str] | None = None
        self._exit_manifest_validated_prefix: (
            _ExitManifestValidatedPrefix | None
        ) = None
        self._signal_observation_binding: tuple[
            str,
            int,
            str,
            str,
            str,
        ] | None = None
        if self._path.exists() and not self._path.is_file():
            raise ValueError("trusted paper bar path must be a file")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load_preflight_fail_stop()
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def calendar_fingerprint(self) -> str | None:
        return self._calendar_fingerprint

    def bind_strategy_run(self, strategy_run: object) -> None:
        bindings = getattr(strategy_run, "store_bindings", {})
        binding = bindings.get("bar") if isinstance(bindings, Mapping) else None
        run_binding = (
            getattr(binding, "run_id", None),
            getattr(binding, "epoch", None),
            getattr(binding, "strategy_run_fingerprint", None),
            getattr(binding, "identity_sha256", None),
            getattr(binding, "store_instance_id", None),
        )
        if (
            not isinstance(run_binding[0], str)
            or not run_binding[0]
            or isinstance(run_binding[1], bool)
            or not isinstance(run_binding[1], int)
            or run_binding[1] <= 0
            or not _valid_sha256_fingerprint(run_binding[2])
            or not _valid_sha256_fingerprint(run_binding[3])
            or not isinstance(run_binding[4], str)
            or not run_binding[4]
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_strategy_binding_invalid"
            )
        normalized_binding = (
            run_binding[0],
            run_binding[1],
            run_binding[2],
            run_binding[3],
            run_binding[4],
        )
        with self._lock:
            self._mutation_fence.bind(
                strategy_run,
                expected_store_role="bar",
                expected_store_path=self._path,
                expected_store_instance_id=getattr(
                    binding,
                    "store_instance_id",
                    None,
                ),
            )
            if self._strategy_run_binding not in (None, normalized_binding):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_strategy_rebind_forbidden"
                )
            self._strategy_run_binding = normalized_binding
            with self._connect() as connection:
                self._validate_segment_ledger(connection)
                self._validate_exit_manifest_schema(connection)
                self._validate_exit_manifest_log(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @staticmethod
    def _preflight_fail_stop_payload(failed_at: datetime) -> dict[str, object]:
        return {
            "schema_version": 1,
            "failed_at": normalize_datetime(
                failed_at,
                "failed_at",
            ).isoformat(),
            "reason": "paper_calendar_preflight_persistence_failed",
        }

    def _preflight_fail_stop_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        if self._preflight_fail_stop_path.exists():
            if not self._preflight_fail_stop_path.is_file():
                raise ValueError("legacy fail-stop path invalid")
            paths.append(self._preflight_fail_stop_path)
        if self._preflight_fail_stop_dir.exists():
            if not self._preflight_fail_stop_dir.is_dir():
                raise ValueError("fail-stop event directory invalid")
            for path in sorted(self._preflight_fail_stop_dir.iterdir()):
                if not path.is_file() or path.suffix != ".json":
                    raise ValueError("fail-stop event path invalid")
                paths.append(path)
        return tuple(paths)

    def _decode_preflight_fail_stop(self, path: Path) -> datetime:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "failed_at",
            "reason",
            "payload_sha256",
        }:
            raise ValueError("fail-stop schema invalid")
        failed_at = _parse_datetime(raw["failed_at"], "failed_at")
        payload = self._preflight_fail_stop_payload(failed_at)
        checksum = self._payload_checksum(payload)
        if (
            raw["reason"]
            != "paper_calendar_preflight_persistence_failed"
            or raw["payload_sha256"] != checksum
        ):
            raise ValueError("fail-stop checksum invalid")
        if (
            path.parent == self._preflight_fail_stop_dir
            and path.name != checksum[7:] + ".json"
        ):
            raise ValueError("fail-stop event identity invalid")
        return failed_at

    def _load_preflight_fail_stop(self) -> None:
        try:
            paths = self._preflight_fail_stop_paths()
            failed_events = tuple(
                self._decode_preflight_fail_stop(path) for path in paths
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            self._process_fail_stop_reason = (
                "paper_calendar_preflight_fail_stop_invalid"
            )
            self._process_fail_stop_at = None
            return
        if not failed_events:
            return
        latest = max(failed_events)
        if self._process_fail_stop_reason == (
            "paper_calendar_preflight_fail_stop_invalid"
        ):
            return
        if (
            self._process_fail_stop_at is None
            or latest > self._process_fail_stop_at
        ):
            self._process_fail_stop_reason = (
                "paper_calendar_preflight_persistence_failed"
            )
            self._process_fail_stop_at = latest

    def _activate_preflight_fail_stop(self, failed_at: datetime) -> None:
        normalized = normalize_datetime(failed_at, "failed_at")
        if (
            self._process_fail_stop_at is None
            or normalized > self._process_fail_stop_at
        ):
            self._process_fail_stop_reason = (
                "paper_calendar_preflight_persistence_failed"
            )
            self._process_fail_stop_at = normalized
        payload = self._preflight_fail_stop_payload(normalized)
        checksum = self._payload_checksum(payload)
        encoded = {
            **payload,
            "payload_sha256": checksum,
        }

        def persist(final: Path, *, create_parent: bool) -> bool:
            temporary = self._path.parent / (
                final.name + f".{os.getpid()}.{id(self)}.tmp"
            )
            try:
                if create_parent:
                    final.parent.mkdir(exist_ok=True)
                    if not final.parent.is_dir():
                        raise OSError("fail-stop event directory unavailable")
                with temporary.open(
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    json.dump(
                        encoded,
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, final)
                return True
            except OSError:
                return False
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        primary = self._preflight_fail_stop_dir / (checksum[7:] + ".json")
        if persist(primary, create_parent=True):
            return
        persist(self._preflight_fail_stop_path, create_parent=False)

    def _clear_preflight_fail_stop(
        self,
        *,
        observed_at: datetime,
    ) -> None:
        observed_at = normalize_datetime(observed_at, "observed_at")
        try:
            paths = self._preflight_fail_stop_paths()
            events = tuple(
                (path, self._decode_preflight_fail_stop(path))
                for path in paths
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            self._process_fail_stop_reason = (
                "paper_calendar_preflight_fail_stop_invalid"
            )
            self._process_fail_stop_at = None
            return
        for path, failed_at in events:
            if failed_at > observed_at:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return
        self._process_fail_stop_reason = None
        self._process_fail_stop_at = None
        self._load_preflight_fail_stop()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_meta (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    calendar_fingerprint TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO trusted_paper_bar_meta (
                    singleton_id, calendar_fingerprint
                ) VALUES (1, NULL)
                """
            )
            persisted_fingerprint = connection.execute(
                """
                SELECT calendar_fingerprint FROM trusted_paper_bar_meta
                WHERE singleton_id = 1
                """
            ).fetchone()[0]
            if persisted_fingerprint is not None and not _valid_sha256_fingerprint(
                persisted_fingerprint
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_fingerprint_invalid"
                )
            if self._calendar_fingerprint is not None:
                if persisted_fingerprint is None:
                    connection.execute(
                        """
                        UPDATE trusted_paper_bar_meta
                        SET calendar_fingerprint = ? WHERE singleton_id = 1
                        """,
                        (self._calendar_fingerprint,),
                    )
                    persisted_fingerprint = self._calendar_fingerprint
                elif persisted_fingerprint != self._calendar_fingerprint:
                    raise TrustedPaperBarIntegrityError(
                        "paper_calendar_fingerprint_mismatch"
                    )
            self._calendar_fingerprint = persisted_fingerprint
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar (
                    bar_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    UNIQUE (code, closed_at)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_trusted_paper_bar_closed
                ON trusted_paper_bar (closed_at, code)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_segment (
                    segment_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK (required IN (0, 1)),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    end_reason TEXT,
                    calendar_fingerprint TEXT NOT NULL,
                    UNIQUE (code, started_at)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_trusted_bar_active_segment
                ON trusted_paper_bar_segment (code) WHERE ended_at IS NULL
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_segment_member (
                    bar_id TEXT PRIMARY KEY,
                    segment_id TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK (required IN (0, 1)),
                    FOREIGN KEY (bar_id) REFERENCES trusted_paper_bar (bar_id),
                    FOREIGN KEY (segment_id)
                        REFERENCES trusted_paper_bar_segment (segment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                ix_trusted_bar_segment_member_tail
                ON trusted_paper_bar_segment_member (
                    segment_id, closed_at DESC
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_cycle (
                    closed_at TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL,
                    slot_index INTEGER NOT NULL CHECK (
                        slot_index >= 0 AND slot_index < 48
                    ),
                    calendar_fingerprint TEXT NOT NULL,
                    required_codes_json TEXT NOT NULL,
                    optional_codes_json TEXT NOT NULL,
                    persisted_codes_json TEXT NOT NULL,
                    optional_failures_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0 CHECK (
                        completed IN (0, 1)
                    ),
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_attempt (
                    closed_at TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL,
                    slot_index INTEGER NOT NULL CHECK (
                        slot_index >= 0 AND slot_index < 48
                    ),
                    calendar_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('started', 'complete', 'failed')
                    ),
                    failure_reason TEXT,
                    attempt_generation INTEGER NOT NULL DEFAULT 1 CHECK (
                        attempt_generation > 0
                    ),
                    updated_at TEXT NOT NULL
                )
                """
            )
            attempt_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(trusted_paper_bar_attempt)"
                )
            }
            if "attempt_generation" not in attempt_columns:
                connection.execute(
                    """
                    ALTER TABLE trusted_paper_bar_attempt
                    ADD COLUMN attempt_generation INTEGER NOT NULL DEFAULT 1
                    CHECK (attempt_generation > 0)
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                trusted_paper_exit_manifest_log_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    event_count INTEGER NOT NULL CHECK (event_count >= 0),
                    max_sequence INTEGER NOT NULL CHECK (max_sequence >= 0),
                    history_head_sha256 TEXT,
                    payload_sha256 TEXT NOT NULL
                )
                """
            )
            empty_exit_manifest_log_state = {
                "schema_version": 1,
                "event_count": 0,
                "max_sequence": 0,
                "history_head_sha256": None,
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO trusted_paper_exit_manifest_log_state (
                    singleton_id, event_count, max_sequence,
                    history_head_sha256, payload_sha256
                ) VALUES (1, 0, 0, NULL, ?)
                """,
                (sha256_json(empty_exit_manifest_log_state),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_exit_manifest (
                    closed_at TEXT PRIMARY KEY,
                    manifest_sequence INTEGER NOT NULL UNIQUE CHECK (
                        manifest_sequence > 0
                    ),
                    previous_manifest_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    FOREIGN KEY (closed_at)
                        REFERENCES trusted_paper_bar_cycle (closed_at)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_signal_observation_log_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    event_count INTEGER NOT NULL CHECK (event_count >= 0),
                    max_sequence INTEGER NOT NULL CHECK (max_sequence >= 0),
                    history_head_sha256 TEXT,
                    payload_sha256 TEXT NOT NULL
                )
                """
            )
            empty_observation_log_state = {
                "schema_version": 1,
                "event_count": 0,
                "max_sequence": 0,
                "history_head_sha256": None,
            }
            connection.execute(
                """
                INSERT OR IGNORE INTO trusted_signal_observation_log_state (
                    singleton_id, event_count, max_sequence,
                    history_head_sha256, payload_sha256
                ) VALUES (1, 0, 0, NULL, ?)
                """,
                (sha256_json(empty_observation_log_state),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_signal_observation_cycle (
                    observation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    strategy_run_fingerprint TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    store_instance_id TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    previous_manifest_sha256 TEXT,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    UNIQUE (run_id, closed_at)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                ix_trusted_signal_observation_closed
                ON trusted_signal_observation_cycle (closed_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_signal_first_observation (
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    strategy_run_fingerprint TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    store_instance_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    signal_fingerprint TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    first_cycle_closed_at TEXT NOT NULL,
                    first_segment_id TEXT NOT NULL,
                    observation_state TEXT NOT NULL CHECK (
                        observation_state IN (
                            'trusted_first_seen', 'baseline_not_fresh'
                        )
                    ),
                    observation_sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY (run_id, code, signal_fingerprint),
                    FOREIGN KEY (observation_sequence)
                        REFERENCES trusted_signal_observation_cycle (
                            observation_sequence
                        )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_signal_segment_observation (
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    strategy_run_fingerprint TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    store_instance_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    signal_fingerprint TEXT NOT NULL,
                    first_observed_at TEXT,
                    first_cycle_closed_at TEXT NOT NULL,
                    observation_state TEXT NOT NULL CHECK (
                        observation_state IN (
                            'trusted_first_seen',
                            'baseline_not_fresh',
                            'quarantined_unknown'
                        )
                    ),
                    observation_sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY (
                        run_id, segment_id, code, signal_fingerprint
                    ),
                    FOREIGN KEY (segment_id)
                        REFERENCES trusted_paper_bar_segment (segment_id),
                    FOREIGN KEY (observation_sequence)
                        REFERENCES trusted_signal_observation_cycle (
                            observation_sequence
                        ),
                    CHECK (
                        (observation_state = 'quarantined_unknown'
                         AND first_observed_at IS NULL)
                        OR
                        (observation_state != 'quarantined_unknown'
                         AND first_observed_at IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_calendar_preflight (
                    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    failed_at TEXT NOT NULL,
                    failure_reason TEXT NOT NULL CHECK (
                        length(failure_reason) > 0
                        AND length(failure_reason) <= 255
                    ),
                    resolved_at TEXT,
                    resolved_by_bar_closed_at TEXT,
                    payload_sha256 TEXT,
                    CHECK (
                        (resolved_at IS NULL
                         AND resolved_by_bar_closed_at IS NULL)
                        OR
                        (resolved_at IS NOT NULL
                         AND resolved_by_bar_closed_at IS NOT NULL)
                    )
                )
                """
            )
            preflight_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(trusted_paper_bar_calendar_preflight)"
                )
            }
            if "payload_sha256" not in preflight_columns:
                connection.execute(
                    """
                    ALTER TABLE trusted_paper_bar_calendar_preflight
                    ADD COLUMN payload_sha256 TEXT
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                trusted_paper_bar_calendar_preflight_resolution (
                    failure_id INTEGER PRIMARY KEY,
                    resolved_at TEXT NOT NULL,
                    resolved_by_bar_closed_at TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    FOREIGN KEY (failure_id)
                        REFERENCES trusted_paper_bar_calendar_preflight (
                            failure_id
                        )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                trusted_paper_bar_calendar_preflight_watermark (
                    closed_at TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    cycle_payload_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    FOREIGN KEY (closed_at)
                        REFERENCES trusted_paper_bar_cycle (closed_at)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_paper_calendar_preflight_unresolved
                ON trusted_paper_bar_calendar_preflight (
                    resolved_at, failed_at, failure_id
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trusted_paper_bar_health (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
                    degraded_reason TEXT,
                    updated_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO trusted_paper_bar_health (
                    singleton_id, degraded, degraded_reason, updated_at
                ) VALUES (1, 0, NULL, NULL)
                """
            )
            self._validate_exit_manifest_schema(connection)
            try:
                self._validate_segment_ledger(connection)
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
                raise
            persisted_rows = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar ORDER BY closed_at, code
                """
            )
            for row in persisted_rows:
                try:
                    self._decode_row(connection, row)
                except TrustedPaperBarIntegrityError:
                    break
            try:
                attested_v2_cycle_checksums = (
                    self._validate_exit_manifest_log(connection)
                )
                exit_manifest_validation_error = None
            except TrustedPaperBarIntegrityError as exc:
                attested_v2_cycle_checksums = None
                exit_manifest_validation_error = exc
            cycle_rows = connection.execute(
                """
                SELECT closed_at, trading_day, slot_index,
                       calendar_fingerprint, required_codes_json,
                       optional_codes_json, persisted_codes_json,
                       optional_failures_json, payload_sha256
                FROM trusted_paper_bar_cycle ORDER BY closed_at
                """
            )
            for row in cycle_rows:
                try:
                    self._decode_cycle_row(
                        connection,
                        row,
                        attested_v2_cycle_checksums=(
                            attested_v2_cycle_checksums
                        ),
                    )
                except TrustedPaperBarIntegrityError:
                    self._mark_degraded(
                        connection,
                        "paper_bar_cycle_integrity_failure",
                        datetime.now(_CN),
                    )
                    raise
            try:
                self._validate_calendar_preflight_log(connection)
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
            try:
                self._validate_signal_observation_log(connection)
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
            if exit_manifest_validation_error is not None:
                self._mark_degraded(
                    connection,
                    str(exit_manifest_validation_error),
                    datetime.now(_CN),
                )
            if self._has_unbound_bars(connection):
                self._mark_degraded(
                    connection,
                    "paper_bar_unbound_from_v2_cycle",
                    datetime.now(_CN),
                )
    @staticmethod
    def _signal_observation_log_state_payload(
        *,
        event_count: int,
        max_sequence: int,
        history_head_sha256: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_count": event_count,
            "max_sequence": max_sequence,
            "history_head_sha256": history_head_sha256,
        }

    @staticmethod
    def _signal_observation_first_payload(
        *,
        run_id: str,
        epoch: int,
        strategy_run_fingerprint: str,
        identity_sha256: str,
        store_instance_id: str,
        code: str,
        signal_fingerprint: str,
        first_observed_at: str,
        first_cycle_closed_at: str,
        first_segment_id: str,
        observation_state: str,
        observation_sequence: int,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "epoch": epoch,
            "strategy_run_fingerprint": strategy_run_fingerprint,
            "identity_sha256": identity_sha256,
            "store_instance_id": store_instance_id,
            "code": code,
            "signal_fingerprint": signal_fingerprint,
            "first_observed_at": first_observed_at,
            "first_cycle_closed_at": first_cycle_closed_at,
            "first_segment_id": first_segment_id,
            "observation_state": observation_state,
            "observation_sequence": observation_sequence,
        }

    @staticmethod
    def _signal_observation_segment_payload(
        *,
        run_id: str,
        epoch: int,
        strategy_run_fingerprint: str,
        identity_sha256: str,
        store_instance_id: str,
        segment_id: str,
        code: str,
        signal_fingerprint: str,
        first_observed_at: str | None,
        first_cycle_closed_at: str,
        observation_state: str,
        observation_sequence: int,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "epoch": epoch,
            "strategy_run_fingerprint": strategy_run_fingerprint,
            "identity_sha256": identity_sha256,
            "store_instance_id": store_instance_id,
            "segment_id": segment_id,
            "code": code,
            "signal_fingerprint": signal_fingerprint,
            "first_observed_at": first_observed_at,
            "first_cycle_closed_at": first_cycle_closed_at,
            "observation_state": observation_state,
            "observation_sequence": observation_sequence,
        }

    @staticmethod
    def _normalize_signal_observation_manifests(
        manifests: Mapping[str, Sequence[str]],
    ) -> dict[str, tuple[str, ...]]:
        if not isinstance(manifests, Mapping):
            raise TypeError("signal observation manifests must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for code, values in manifests.items():
            if not isinstance(code, str) or not code:
                raise ValueError("signal observation codes must be non-empty")
            if isinstance(values, (str, bytes)):
                raise TypeError("signal fingerprints must be a sequence")
            fingerprints = tuple(sorted(values))
            if (
                len(set(fingerprints)) != len(fingerprints)
                or any(
                    not _valid_sha256_fingerprint(value)
                    for value in fingerprints
                )
            ):
                raise ValueError("signal fingerprints must be unique sha256 values")
            normalized[code] = fingerprints
        return dict(sorted(normalized.items()))

    @staticmethod
    def _signal_observation_cycle_payload(
        *,
        binding: tuple[str, int, str, str, str],
        closed_at: str,
        manifests: Mapping[str, tuple[str, ...]],
        segment_ids: Mapping[str, str],
        states: Mapping[str, Mapping[str, str]],
        first_observed_at: Mapping[str, Mapping[str, datetime]],
        segment_payload_sha256: Mapping[str, Mapping[str, str]],
        attempt_generation: int,
        prior_attempt_ambiguous: bool,
        resolution_sha256: str,
        previous_manifest_sha256: str | None,
    ) -> dict[str, object]:
        run_id, epoch, strategy_fingerprint, identity_sha256, store_id = binding
        return {
            "schema_version": 4,
            "run_id": run_id,
            "epoch": epoch,
            "strategy_run_fingerprint": strategy_fingerprint,
            "identity_sha256": identity_sha256,
            "store_instance_id": store_id,
            "closed_at": closed_at,
            "attempt_generation": attempt_generation,
            "prior_attempt_ambiguous": prior_attempt_ambiguous,
            "manifests": {
                code: {
                    "segment_id": segment_ids[code],
                    "signal_fingerprints": list(signal_fingerprints),
                    "states": dict(states[code]),
                    "segment_first_observed_at": {
                        signal_fingerprint: (
                            None
                            if signal_fingerprint
                            not in first_observed_at[code]
                            else first_observed_at[code][
                                signal_fingerprint
                            ].isoformat()
                        )
                        for signal_fingerprint in signal_fingerprints
                    },
                    "segment_payload_sha256": dict(
                        segment_payload_sha256[code]
                    ),
                }
                for code, signal_fingerprints in manifests.items()
            },
            "resolution_sha256": resolution_sha256,
            "previous_manifest_sha256": previous_manifest_sha256,
        }

    @staticmethod
    def _prepared_signal_observation_payload(
        *,
        binding: tuple[str, int, str, str, str],
        closed_at: datetime,
        manifests: Mapping[str, tuple[str, ...]],
        segment_ids: Mapping[str, str],
        states: Mapping[str, Mapping[str, str]],
        first_observed_at: Mapping[str, Mapping[str, datetime]],
        attempt_generation: int,
        prior_attempt_ambiguous: bool,
    ) -> dict[str, object]:
        run_id, epoch, strategy_fingerprint, identity_sha256, store_id = binding
        return {
            "schema_version": 2,
            "run_id": run_id,
            "epoch": epoch,
            "strategy_run_fingerprint": strategy_fingerprint,
            "identity_sha256": identity_sha256,
            "store_instance_id": store_id,
            "closed_at": closed_at,
            "attempt_generation": attempt_generation,
            "prior_attempt_ambiguous": prior_attempt_ambiguous,
            "manifests": manifests,
            "segment_ids": segment_ids,
            "states": states,
            "first_observed_at": first_observed_at,
        }

    def bind_signal_observation_strategy_run(self, strategy_run: object) -> None:
        status_provider = getattr(strategy_run, "status_payload", None)
        if not callable(status_provider):
            raise TypeError("strategy_run must provide status_payload")
        status = status_provider()
        if (
            not isinstance(status, Mapping)
            or status.get("state") != "active"
            or status.get("evidence_scope") != "current_epoch_only"
            or status.get("store_bindings_complete") is not True
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_strategy_run_not_active"
            )
        bindings = getattr(strategy_run, "store_bindings", None)
        store_paths = getattr(strategy_run, "store_paths", None)
        if not isinstance(bindings, Mapping) or not isinstance(store_paths, Mapping):
            raise TypeError("strategy_run store bindings are unavailable")
        bar_binding = bindings.get("bar")
        try:
            resolved_path = Path(store_paths["bar"]).expanduser().absolute()
        except (KeyError, TypeError) as exc:
            raise TypeError("strategy_run bar store path is unavailable") from exc
        binding = (
            getattr(bar_binding, "run_id", None),
            getattr(bar_binding, "epoch", None),
            getattr(bar_binding, "strategy_run_fingerprint", None),
            getattr(bar_binding, "identity_sha256", None),
            getattr(bar_binding, "store_instance_id", None),
        )
        run_id, epoch, strategy_fingerprint, identity_sha256, store_id = binding
        if (
            resolved_path != self._path
            or getattr(bar_binding, "store_role", None) != "bar"
            or run_id != getattr(strategy_run, "run_id", None)
            or epoch != getattr(strategy_run, "epoch", None)
            or strategy_fingerprint
            != getattr(strategy_run, "strategy_run_fingerprint", None)
            or not isinstance(run_id, str)
            or not run_id
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch <= 0
            or not _valid_sha256_fingerprint(strategy_fingerprint)
            or not _valid_sha256_fingerprint(identity_sha256)
            or not isinstance(store_id, str)
            or not store_id
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_strategy_binding_invalid"
            )
        with self._lock, self._connect() as connection:
            self._validate_signal_observation_log(connection)
        if self._signal_observation_binding not in (None, binding):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_strategy_rebind_forbidden"
            )
        self._signal_observation_strategy_run = strategy_run
        self._signal_observation_binding = binding

    def _require_signal_observation_binding(
        self,
    ) -> tuple[str, int, str, str, str]:
        strategy_run = self._signal_observation_strategy_run
        binding = self._signal_observation_binding
        if strategy_run is None or binding is None:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_strategy_binding_missing"
            )
        status = strategy_run.status_payload()
        if (
            not isinstance(status, Mapping)
            or status.get("state") != "active"
            or status.get("run_id") != binding[0]
            or status.get("epoch") != binding[1]
            or status.get("fingerprint") != binding[2]
            or status.get("evidence_scope") != "current_epoch_only"
            or status.get("store_bindings_complete") is not True
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_strategy_run_not_active"
            )
        return binding

    @staticmethod
    def _decode_signal_observation_manifest_payload(
        payload: object,
    ) -> tuple[
        dict[str, tuple[str, ...]],
        dict[str, str],
        dict[str, dict[str, str]],
        dict[str, dict[str, datetime]],
        dict[str, dict[str, str]],
    ]:
        if not isinstance(payload, dict) or payload.get("schema_version") != 4:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_manifest_schema_invalid"
            )
        raw_manifests = payload.get("manifests")
        if not isinstance(raw_manifests, dict):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_manifest_schema_invalid"
            )
        manifests: dict[str, tuple[str, ...]] = {}
        segments: dict[str, str] = {}
        states: dict[str, dict[str, str]] = {}
        first_observed_at: dict[str, dict[str, datetime]] = {}
        segment_payload_sha256: dict[str, dict[str, str]] = {}
        for code, raw in raw_manifests.items():
            if not isinstance(code, str) or not code or not isinstance(raw, dict):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_schema_invalid"
                )
            segment_id = raw.get("segment_id")
            values = raw.get("signal_fingerprints")
            raw_states = raw.get("states")
            raw_first_observed_at = raw.get("segment_first_observed_at")
            raw_segment_payload_sha256 = raw.get(
                "segment_payload_sha256"
            )
            if (
                set(raw)
                != {
                    "segment_id",
                    "signal_fingerprints",
                    "states",
                    "segment_first_observed_at",
                    "segment_payload_sha256",
                }
                or not isinstance(segment_id, str)
                or not segment_id
                or not isinstance(values, list)
                or not isinstance(raw_states, dict)
                or not isinstance(raw_first_observed_at, dict)
                or not isinstance(raw_segment_payload_sha256, dict)
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_schema_invalid"
                )
            fingerprints = tuple(values)
            if (
                tuple(sorted(fingerprints)) != fingerprints
                or len(set(fingerprints)) != len(fingerprints)
                or any(
                    not _valid_sha256_fingerprint(value)
                    for value in fingerprints
                )
                or set(raw_states) != set(fingerprints)
                or any(
                    value not in _SIGNAL_OBSERVATION_STATES
                    for value in raw_states.values()
                )
                or set(raw_first_observed_at) != set(fingerprints)
                or set(raw_segment_payload_sha256) != set(fingerprints)
                or any(
                    not _valid_sha256_fingerprint(value)
                    for value in raw_segment_payload_sha256.values()
                )
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_schema_invalid"
                )
            manifests[code] = fingerprints
            segments[code] = segment_id
            states[code] = dict(raw_states)
            first_observed_at[code] = {}
            for signal_fingerprint in fingerprints:
                raw_first = raw_first_observed_at[signal_fingerprint]
                state = raw_states[signal_fingerprint]
                if (state == "quarantined_unknown") != (raw_first is None):
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_manifest_schema_invalid"
                    )
                if raw_first is not None:
                    try:
                        first_observed_at[code][signal_fingerprint] = (
                            _parse_datetime(raw_first, "first_observed_at")
                        )
                    except (TypeError, ValueError) as exc:
                        raise TrustedPaperBarIntegrityError(
                            "signal_observation_manifest_schema_invalid"
                        ) from exc
            segment_payload_sha256[code] = dict(
                raw_segment_payload_sha256
            )
        return (
            manifests,
            segments,
            states,
            first_observed_at,
            segment_payload_sha256,
        )

    def _classify_segment_signal_prefix(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        segment_id: str,
        code: str,
        signal_fingerprint: str,
        target_closed_at: str,
        decoded_rows: Mapping[
            tuple[str, str], tuple[dict[str, object], int]
        ],
        before_sequence: int,
    ) -> str:
        prior_members = connection.execute(
            """
            SELECT closed_at
            FROM trusted_paper_bar_segment_member
            WHERE segment_id = ? AND closed_at < ?
            ORDER BY closed_at
            """,
            (segment_id, target_closed_at),
        ).fetchall()
        if not prior_members:
            return "baseline_not_fresh"
        for (member_closed_at,) in prior_members:
            previous_observation = decoded_rows.get((run_id, member_closed_at))
            if (
                previous_observation is None
                or previous_observation[1] >= before_sequence
            ):
                return "quarantined_unknown"
            if previous_observation[0].get("prior_attempt_ambiguous") is True:
                return "quarantined_unknown"
            previous_manifests, previous_segments, *_ = (
                self._decode_signal_observation_manifest_payload(
                    previous_observation[0]
                )
            )
            if previous_segments.get(code) != segment_id:
                raise TrustedPaperBarIntegrityError(
                    "signal_segment_observation_integrity_failure"
                )
            if signal_fingerprint in previous_manifests.get(code, ()):
                raise TrustedPaperBarIntegrityError(
                    "signal_segment_observation_integrity_failure"
                )
        return "trusted_first_seen"

    def _validate_signal_observation_log(
        self,
        connection: sqlite3.Connection,
    ) -> dict[tuple[str, str], tuple[dict[str, object], int]]:
        self._validate_segment_ledger(connection)
        rows = connection.execute(
            """
            SELECT observation_sequence, run_id, epoch,
                   strategy_run_fingerprint, identity_sha256,
                   store_instance_id, closed_at,
                   previous_manifest_sha256, payload_json, payload_sha256
            FROM trusted_signal_observation_cycle
            ORDER BY observation_sequence
            """
        ).fetchall()
        previous_sha256: str | None = None
        occurrences: dict[
            tuple[str, str, str],
            tuple[int, str, str, tuple[str, int, str, str, str]],
        ] = {}
        seen_segment_authority: set[tuple[str, str, str, str]] = set()
        decoded_rows: dict[tuple[str, str], tuple[dict[str, object], int]] = {}
        for expected_sequence, row in enumerate(rows, start=1):
            (
                sequence,
                run_id,
                epoch,
                strategy_fingerprint,
                identity_sha256,
                store_id,
                closed_at,
                previous,
                payload_json,
                payload_sha256,
            ) = row
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_integrity_failure"
                ) from exc
            if (
                sequence != expected_sequence
                or not isinstance(payload, dict)
                or set(payload)
                != {
                    "schema_version",
                    "run_id",
                    "epoch",
                    "strategy_run_fingerprint",
                    "identity_sha256",
                    "store_instance_id",
                    "closed_at",
                    "attempt_generation",
                    "prior_attempt_ambiguous",
                    "manifests",
                    "resolution_sha256",
                    "previous_manifest_sha256",
                }
                or payload.get("schema_version") != 4
                or payload.get("run_id") != run_id
                or payload.get("epoch") != epoch
                or payload.get("strategy_run_fingerprint")
                != strategy_fingerprint
                or payload.get("identity_sha256") != identity_sha256
                or payload.get("store_instance_id") != store_id
                or payload.get("closed_at") != closed_at
                or not _valid_sha256_fingerprint(
                    payload.get("resolution_sha256")
                )
                or payload.get("previous_manifest_sha256") != previous
                or previous != previous_sha256
                or sha256_json(payload) != payload_sha256
                or not isinstance(run_id, str)
                or not run_id
                or isinstance(epoch, bool)
                or not isinstance(epoch, int)
                or epoch <= 0
                or not _valid_sha256_fingerprint(strategy_fingerprint)
                or not _valid_sha256_fingerprint(identity_sha256)
                or not isinstance(store_id, str)
                or not store_id
                or isinstance(payload.get("attempt_generation"), bool)
                or not isinstance(payload.get("attempt_generation"), int)
                or payload.get("attempt_generation") <= 0
                or not isinstance(payload.get("prior_attempt_ambiguous"), bool)
                or payload.get("prior_attempt_ambiguous")
                != (payload.get("attempt_generation") > 1)
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_integrity_failure"
                )
            (
                manifests,
                segments,
                cycle_states,
                cycle_first_observed_at,
                cycle_segment_payload_sha256,
            ) = self._decode_signal_observation_manifest_payload(payload)
            binding = (
                run_id,
                epoch,
                strategy_fingerprint,
                identity_sha256,
                store_id,
            )
            try:
                normalized_closed_at = _parse_datetime(
                    closed_at,
                    "closed_at",
                )
            except (TypeError, ValueError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_integrity_failure"
                ) from exc
            resolution_payload = self._prepared_signal_observation_payload(
                binding=binding,
                closed_at=normalized_closed_at,
                manifests=manifests,
                segment_ids=segments,
                states=cycle_states,
                first_observed_at=cycle_first_observed_at,
                attempt_generation=payload["attempt_generation"],
                prior_attempt_ambiguous=payload["prior_attempt_ambiguous"],
            )
            if sha256_json(resolution_payload) != payload.get(
                "resolution_sha256"
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_manifest_integrity_failure"
                )
            cycle = connection.execute(
                """
                SELECT required_codes_json, completed
                FROM trusted_paper_bar_cycle WHERE closed_at = ?
                """,
                (closed_at,),
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT status, attempt_generation
                FROM trusted_paper_bar_attempt WHERE closed_at = ?
                """,
                (closed_at,),
            ).fetchone()
            if cycle is None:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_cycle_binding_invalid"
                )
            try:
                required_codes = tuple(sorted(json.loads(cycle[0])))
            except (TypeError, json.JSONDecodeError, IndexError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_cycle_binding_invalid"
                ) from exc
            if (
                not bool(cycle[1])
                or attempt != ("complete", payload["attempt_generation"])
                or tuple(manifests) != required_codes
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_cycle_binding_invalid"
                )
            for code, segment_id in segments.items():
                member = connection.execute(
                    """
                    SELECT m.segment_id, m.required, s.code, s.required
                    FROM trusted_paper_bar_segment_member AS m
                    JOIN trusted_paper_bar_segment AS s
                      ON s.segment_id = m.segment_id
                    WHERE m.closed_at = ? AND s.code = ?
                    """,
                    (closed_at, code),
                ).fetchone()
                if member != (segment_id, 1, code, 1):
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_segment_binding_invalid"
                    )
                for signal_fingerprint in manifests[code]:
                    occurrences.setdefault(
                        (run_id, code, signal_fingerprint),
                        (sequence, closed_at, segment_id, binding),
                    )
                    segment_key = (
                        run_id,
                        segment_id,
                        code,
                        signal_fingerprint,
                    )
                    segment_row = connection.execute(
                        """
                        SELECT run_id, epoch, strategy_run_fingerprint,
                               identity_sha256, store_instance_id, segment_id,
                               code, signal_fingerprint, first_observed_at,
                               first_cycle_closed_at, observation_state,
                               observation_sequence, payload_json,
                               payload_sha256
                        FROM trusted_signal_segment_observation
                        WHERE run_id = ? AND segment_id = ? AND code = ?
                          AND signal_fingerprint = ?
                        """,
                        segment_key,
                    ).fetchone()
                    if segment_row is None:
                        raise TrustedPaperBarIntegrityError(
                            "signal_segment_observation_integrity_failure"
                        )
                    (
                        segment_run_id,
                        segment_epoch,
                        segment_strategy_fingerprint,
                        segment_identity_sha256,
                        segment_store_id,
                        stored_segment_id,
                        segment_code,
                        segment_signal_fingerprint,
                        segment_first_observed_at,
                        segment_first_cycle_closed_at,
                        segment_state,
                        segment_sequence,
                        segment_payload_json,
                        segment_payload_sha256,
                    ) = segment_row
                    segment_payload = self._signal_observation_segment_payload(
                        run_id=segment_run_id,
                        epoch=segment_epoch,
                        strategy_run_fingerprint=(
                            segment_strategy_fingerprint
                        ),
                        identity_sha256=segment_identity_sha256,
                        store_instance_id=segment_store_id,
                        segment_id=stored_segment_id,
                        code=segment_code,
                        signal_fingerprint=segment_signal_fingerprint,
                        first_observed_at=segment_first_observed_at,
                        first_cycle_closed_at=segment_first_cycle_closed_at,
                        observation_state=segment_state,
                        observation_sequence=segment_sequence,
                    )
                    cycle_first = cycle_first_observed_at[code].get(
                        signal_fingerprint
                    )
                    cycle_first_iso = (
                        None if cycle_first is None else cycle_first.isoformat()
                    )
                    if (
                        segment_row[:8]
                        != (
                            run_id,
                            epoch,
                            strategy_fingerprint,
                            identity_sha256,
                            store_id,
                            segment_id,
                            code,
                            signal_fingerprint,
                        )
                        or segment_state
                        != cycle_states[code][signal_fingerprint]
                        or segment_first_observed_at != cycle_first_iso
                        or segment_payload_sha256
                        != cycle_segment_payload_sha256[code][
                            signal_fingerprint
                        ]
                        or segment_payload_json
                        != json.dumps(
                            segment_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        or segment_payload_sha256
                        != sha256_json(segment_payload)
                    ):
                        raise TrustedPaperBarIntegrityError(
                            "signal_segment_observation_integrity_failure"
                        )
                    if segment_key not in seen_segment_authority:
                        expected_segment_state = (
                            "quarantined_unknown"
                            if payload["prior_attempt_ambiguous"]
                            else self._classify_segment_signal_prefix(
                                connection,
                                run_id=run_id,
                                segment_id=segment_id,
                                code=code,
                                signal_fingerprint=signal_fingerprint,
                                target_closed_at=closed_at,
                                decoded_rows=decoded_rows,
                                before_sequence=sequence,
                            )
                        )
                        expected_segment_first = (
                            None
                            if expected_segment_state
                            == "quarantined_unknown"
                            else closed_at
                        )
                        if (
                            segment_sequence != sequence
                            or segment_first_cycle_closed_at != closed_at
                            or segment_state != expected_segment_state
                            or segment_first_observed_at
                            != expected_segment_first
                        ):
                            raise TrustedPaperBarIntegrityError(
                                "signal_segment_observation_integrity_failure"
                            )
                        seen_segment_authority.add(segment_key)
            decoded_rows[(run_id, closed_at)] = (payload, sequence)
            previous_sha256 = payload_sha256

        state_row = connection.execute(
            """
            SELECT event_count, max_sequence, history_head_sha256, payload_sha256
            FROM trusted_signal_observation_log_state WHERE singleton_id = 1
            """
        ).fetchone()
        expected_state = self._signal_observation_log_state_payload(
            event_count=len(rows),
            max_sequence=len(rows),
            history_head_sha256=previous_sha256,
        )
        if (
            state_row is None
            or state_row[:3]
            != (len(rows), len(rows), previous_sha256)
            or state_row[3] != sha256_json(expected_state)
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_log_state_invalid"
            )
        sequence_row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?",
            ("trusted_signal_observation_cycle",),
        ).fetchone()
        if (0 if sequence_row is None else sequence_row[0]) != len(rows):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_log_sequence_invalid"
            )

        first_rows = connection.execute(
            """
            SELECT run_id, epoch, strategy_run_fingerprint, identity_sha256,
                   store_instance_id, code, signal_fingerprint,
                   first_observed_at, first_cycle_closed_at, first_segment_id,
                   observation_state, observation_sequence,
                   payload_json, payload_sha256
            FROM trusted_signal_first_observation
            ORDER BY run_id, code, signal_fingerprint
            """
        ).fetchall()
        seen_first: set[tuple[str, str, str]] = set()
        for row in first_rows:
            (
                run_id,
                epoch,
                strategy_fingerprint,
                identity_sha256,
                store_id,
                code,
                signal_fingerprint,
                first_observed_at,
                first_cycle_closed_at,
                first_segment_id,
                observation_state,
                observation_sequence,
                payload_json,
                payload_sha256,
            ) = row
            key = (run_id, code, signal_fingerprint)
            occurrence = occurrences.get(key)
            payload = self._signal_observation_first_payload(
                run_id=run_id,
                epoch=epoch,
                strategy_run_fingerprint=strategy_fingerprint,
                identity_sha256=identity_sha256,
                store_instance_id=store_id,
                code=code,
                signal_fingerprint=signal_fingerprint,
                first_observed_at=first_observed_at,
                first_cycle_closed_at=first_cycle_closed_at,
                first_segment_id=first_segment_id,
                observation_state=observation_state,
                observation_sequence=observation_sequence,
            )
            expected_state_name = "baseline_not_fresh"
            if occurrence is not None:
                first_cycle_payload = decoded_rows.get(
                    (run_id, first_cycle_closed_at)
                )
                authority_state = (
                    "quarantined_unknown"
                    if first_cycle_payload is not None
                    and first_cycle_payload[0].get("prior_attempt_ambiguous")
                    is True
                    else self._classify_segment_signal_prefix(
                        connection,
                        run_id=run_id,
                        segment_id=first_segment_id,
                        code=code,
                        signal_fingerprint=signal_fingerprint,
                        target_closed_at=first_cycle_closed_at,
                        decoded_rows=decoded_rows,
                        before_sequence=observation_sequence,
                    )
                )
                if authority_state == "trusted_first_seen":
                    expected_state_name = "trusted_first_seen"
            if (
                key in seen_first
                or occurrence is None
                or occurrence
                != (
                    observation_sequence,
                    first_cycle_closed_at,
                    first_segment_id,
                    (
                        run_id,
                        epoch,
                        strategy_fingerprint,
                        identity_sha256,
                        store_id,
                    ),
                )
                or first_observed_at != first_cycle_closed_at
                or observation_state != expected_state_name
                or payload_json
                != json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                or payload_sha256 != sha256_json(payload)
            ):
                raise TrustedPaperBarIntegrityError(
                    "signal_first_observation_integrity_failure"
                )
            seen_first.add(key)
        if seen_first != set(occurrences):
            raise TrustedPaperBarIntegrityError(
                "signal_first_observation_log_incomplete"
            )
        stored_segment_authority = {
            tuple(row)
            for row in connection.execute(
                """
                SELECT run_id, segment_id, code, signal_fingerprint
                FROM trusted_signal_segment_observation
                """
            )
        }
        if stored_segment_authority != seen_segment_authority:
            raise TrustedPaperBarIntegrityError(
                "signal_segment_observation_integrity_failure"
            )
        self._validate_signal_observation_anchors(rows)
        return decoded_rows

    @staticmethod
    def _signal_observation_anchor_payload(
        *,
        observation_sequence: int,
        run_id: str,
        closed_at: str,
        history_head_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "observation_sequence": observation_sequence,
            "run_id": run_id,
            "closed_at": closed_at,
            "history_head_sha256": history_head_sha256,
        }

    def _signal_observation_anchor_path(
        self,
        observation_sequence: int,
        history_head_sha256: str,
    ) -> Path:
        return self._signal_observation_anchor_dir / (
            f"{observation_sequence:020d}-{history_head_sha256[7:]}.json"
        )

    def _write_signal_observation_anchor(
        self,
        *,
        observation_sequence: int,
        run_id: str,
        closed_at: str,
        history_head_sha256: str,
    ) -> None:
        payload = self._signal_observation_anchor_payload(
            observation_sequence=observation_sequence,
            run_id=run_id,
            closed_at=closed_at,
            history_head_sha256=history_head_sha256,
        )
        encoded = {
            **payload,
            "payload_sha256": sha256_json(payload),
        }
        final = self._signal_observation_anchor_path(
            observation_sequence,
            history_head_sha256,
        )
        temporary = self._path.parent / (
            final.name + f".{os.getpid()}.{id(self)}.tmp"
        )
        self._signal_observation_anchor_dir.mkdir(exist_ok=True)
        if not self._signal_observation_anchor_dir.is_dir():
            raise TrustedPaperBarIntegrityError(
                "signal_observation_anchor_unavailable"
            )
        if final.exists():
            try:
                existing = json.loads(final.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_invalid"
                ) from exc
            if existing != encoded:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_conflict"
                )
            return
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    encoded,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_signal_observation_anchors(
        self,
        rows: Sequence[tuple[object, ...]],
    ) -> None:
        if self._signal_observation_anchor_dir.exists():
            if not self._signal_observation_anchor_dir.is_dir():
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_mismatch"
                )
            files = tuple(sorted(self._signal_observation_anchor_dir.iterdir()))
            if any(not path.is_file() or path.suffix != ".json" for path in files):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_mismatch"
                )
        else:
            files = ()
        expected_paths: list[Path] = []
        for row in rows:
            sequence = row[0]
            run_id = row[1]
            closed_at = row[6]
            history_head_sha256 = row[9]
            expected = self._signal_observation_anchor_payload(
                observation_sequence=sequence,
                run_id=run_id,
                closed_at=closed_at,
                history_head_sha256=history_head_sha256,
            )
            expected_encoded = {
                **expected,
                "payload_sha256": sha256_json(expected),
            }
            path = self._signal_observation_anchor_path(
                sequence,
                history_head_sha256,
            )
            expected_paths.append(path)
            try:
                actual = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_mismatch"
                ) from exc
            if actual != expected_encoded:
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_anchor_mismatch"
                )
        if tuple(files) != tuple(expected_paths):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_anchor_mismatch"
            )

    def _prepare_signal_observation_batch_connection(
        self,
        connection: sqlite3.Connection,
        *,
        closed_at: datetime,
        manifests: Mapping[str, tuple[str, ...]],
        binding: tuple[str, int, str, str, str],
    ) -> PreparedSignalObservationBatch:
        self._validate_segment_ledger(connection)
        decoded_rows = self._validate_signal_observation_log(connection)
        cycle = connection.execute(
            """
            SELECT required_codes_json, completed
            FROM trusted_paper_bar_cycle WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT status, attempt_generation
            FROM trusted_paper_bar_attempt WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        if (
            cycle is None
            or bool(cycle[1])
            or attempt is None
            or attempt[0] not in ("started", "failed")
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_cycle_not_preparable"
            )
        attempt_generation = int(attempt[1])
        prior_attempt_ambiguous = attempt_generation > 1
        try:
            required_codes = tuple(sorted(json.loads(cycle[0])))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_required_codes_invalid"
            ) from exc
        if tuple(manifests) != required_codes:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_required_scope_mismatch"
            )
        run_id = binding[0]
        segment_ids: dict[str, str] = {}
        states: dict[str, dict[str, str]] = {}
        first_observed_at: dict[str, dict[str, datetime]] = {}
        for code, signal_fingerprints in manifests.items():
            member = connection.execute(
                """
                SELECT m.segment_id, m.required, s.required
                FROM trusted_paper_bar_segment_member AS m
                JOIN trusted_paper_bar_segment AS s
                  ON s.segment_id = m.segment_id
                WHERE m.closed_at = ? AND s.code = ?
                """,
                (closed_at.isoformat(), code),
            ).fetchone()
            if member is None or member[1:] != (1, 1):
                raise TrustedPaperBarIntegrityError(
                    "signal_observation_segment_binding_missing"
                )
            segment_id = member[0]
            segment_ids[code] = segment_id
            states[code] = {}
            first_observed_at[code] = {}
            for signal_fingerprint in signal_fingerprints:
                existing = connection.execute(
                    """
                    SELECT first_observed_at, observation_state
                    FROM trusted_signal_segment_observation
                    WHERE run_id = ? AND segment_id = ? AND code = ?
                      AND signal_fingerprint = ?
                    """,
                    (run_id, segment_id, code, signal_fingerprint),
                ).fetchone()
                if existing is not None:
                    states[code][signal_fingerprint] = existing[1]
                    if existing[0] is not None:
                        first_observed_at[code][signal_fingerprint] = (
                            _parse_datetime(
                                existing[0],
                                "first_observed_at",
                            )
                        )
                else:
                    state = (
                        "quarantined_unknown"
                        if prior_attempt_ambiguous
                        else self._classify_segment_signal_prefix(
                            connection,
                            run_id=run_id,
                            segment_id=segment_id,
                            code=code,
                            signal_fingerprint=signal_fingerprint,
                            target_closed_at=closed_at.isoformat(),
                            decoded_rows=decoded_rows,
                            before_sequence=len(decoded_rows) + 1,
                        )
                    )
                    states[code][signal_fingerprint] = state
                    if state != "quarantined_unknown":
                        first_observed_at[code][signal_fingerprint] = closed_at
        resolution_payload = self._prepared_signal_observation_payload(
            binding=binding,
            closed_at=closed_at,
            manifests=manifests,
            segment_ids=segment_ids,
            states=states,
            first_observed_at=first_observed_at,
            attempt_generation=attempt_generation,
            prior_attempt_ambiguous=prior_attempt_ambiguous,
        )
        return PreparedSignalObservationBatch(
            run_id=binding[0],
            epoch=binding[1],
            strategy_run_fingerprint=binding[2],
            identity_sha256=binding[3],
            store_instance_id=binding[4],
            bar_closed_at=closed_at,
            manifests=manifests,
            segment_ids=segment_ids,
            states=states,
            first_observed_at=first_observed_at,
            attempt_generation=attempt_generation,
            prior_attempt_ambiguous=prior_attempt_ambiguous,
            resolution_sha256=sha256_json(resolution_payload),
        )

    def prepare_signal_observation_batch(
        self,
        bar_closed_at: datetime,
        manifests: Mapping[str, Sequence[str]],
    ) -> PreparedSignalObservationBatch:
        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        normalized = self._normalize_signal_observation_manifests(manifests)
        binding = self._require_signal_observation_binding()
        with self._lock, self._connect() as connection:
            return self._prepare_signal_observation_batch_connection(
                connection,
                closed_at=closed_at,
                manifests=normalized,
                binding=binding,
            )

    def _attest_committed_signal_observation_batch(
        self,
        connection: sqlite3.Connection,
        *,
        closed_at: datetime,
        batch: PreparedSignalObservationBatch,
        binding: tuple[str, int, str, str, str],
    ) -> None:
        self._validate_signal_observation_log(connection)
        resolution_payload = self._prepared_signal_observation_payload(
            binding=binding,
            closed_at=closed_at,
            manifests=batch.manifests,
            segment_ids=batch.segment_ids,
            states=batch.states,
            first_observed_at=batch.first_observed_at,
            attempt_generation=batch.attempt_generation,
            prior_attempt_ambiguous=batch.prior_attempt_ambiguous,
        )
        row = connection.execute(
            """
            SELECT payload_json FROM trusted_signal_observation_cycle
            WHERE run_id = ? AND closed_at = ?
            """,
            (binding[0], closed_at.isoformat()),
        ).fetchone()
        if row is None:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_committed_batch_missing"
            )
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "signal_observation_manifest_integrity_failure"
            ) from exc
        (
            manifests,
            segment_ids,
            states,
            first_observed_at,
            _segment_payload_sha256,
        ) = self._decode_signal_observation_manifest_payload(payload)
        if (
            sha256_json(resolution_payload) != batch.resolution_sha256
            or payload.get("resolution_sha256") != batch.resolution_sha256
            or payload.get("attempt_generation") != batch.attempt_generation
            or payload.get("prior_attempt_ambiguous")
            != batch.prior_attempt_ambiguous
            or manifests != dict(batch.manifests)
            or segment_ids != dict(batch.segment_ids)
            or states
            != {
                code: dict(values)
                for code, values in batch.states.items()
            }
            or first_observed_at
            != {
                code: dict(values)
                for code, values in batch.first_observed_at.items()
            }
        ):
            raise TrustedPaperBarIntegrityError(
                "signal_observation_batch_resolution_changed"
            )

    def _mark_degraded(
        self,
        connection: sqlite3.Connection,
        reason: str,
        at: datetime,
    ) -> None:
        if not self._mutation_fence.can_persist_internal_fail_stop():
            if self._process_fail_stop_reason is None:
                self._process_fail_stop_reason = reason
                self._process_fail_stop_at = at
            return
        connection.execute(
            """
            UPDATE trusted_paper_bar_health
            SET degraded = 1,
                degraded_reason = COALESCE(degraded_reason, ?),
                updated_at = COALESCE(updated_at, ?)
            WHERE singleton_id = 1
            """,
            (reason, at.isoformat()),
        )
        connection.commit()

    def _decode_row(
        self,
        connection: sqlite3.Connection,
        row: tuple[object, ...],
    ) -> PaperBar:
        try:
            return _paper_bar_from_row(row)
        except TrustedPaperBarIntegrityError:
            self._mark_degraded(
                connection,
                "paper_bar_integrity_failure",
                datetime.now(_CN),
            )
            raise

    def _validate_segment_ledger(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        try:
            cycle_scopes: dict[str, dict[str, object]] = {}
            cycle_order: list[tuple[datetime, dict[str, object]]] = []
            for row in connection.execute(
                """
                SELECT closed_at, trading_day, slot_index,
                       calendar_fingerprint, required_codes_json,
                       optional_codes_json, persisted_codes_json,
                       optional_failures_json
                FROM trusted_paper_bar_cycle ORDER BY closed_at
                """
            ):
                (
                    cycle_closed_at,
                    trading_day,
                    slot_index,
                    calendar_fingerprint,
                    required_json,
                    optional_json,
                    persisted_json,
                    failures_json,
                ) = row
                required = json.loads(required_json)
                optional = json.loads(optional_json)
                persisted = json.loads(persisted_json)
                failures = json.loads(failures_json)
                closed_datetime = _parse_datetime(
                    cycle_closed_at,
                    "segment_cycle_closed_at",
                )
                trading_date = date.fromisoformat(trading_day)
                if (
                    isinstance(slot_index, bool)
                    or not isinstance(slot_index, int)
                    or slot_index < 0
                    or slot_index >= 48
                    or closed_datetime.date() != trading_date
                    or not _valid_sha256_fingerprint(calendar_fingerprint)
                    or any(
                        not isinstance(values, list)
                        or values != sorted(values)
                        or len(values) != len(set(values))
                        or any(
                            not isinstance(code, str) or not code
                            for code in values
                        )
                        for values in (required, optional, persisted)
                    )
                    or set(required) & set(optional)
                    or not isinstance(failures, dict)
                    or any(
                        not isinstance(code, str)
                        or not code
                        or not isinstance(reason, str)
                        or not reason
                        for code, reason in failures.items()
                    )
                    or set(failures) != set(optional) - set(persisted)
                    or set(persisted)
                    != set(required) | (set(optional) - set(failures))
                ):
                    raise ValueError("invalid segment cycle scope")
                scope: dict[str, object] = {
                    "closed_at": cycle_closed_at,
                    "closed_datetime": closed_datetime,
                    "trading_day": trading_date,
                    "slot_index": slot_index,
                    "calendar_fingerprint": calendar_fingerprint,
                    "required": frozenset(required),
                    "optional": frozenset(optional),
                    "persisted": frozenset(persisted),
                    "failures": frozenset(failures),
                }
                cycle_scopes[cycle_closed_at] = scope
                cycle_order.append((closed_datetime, scope))
            cycle_ordinals = {
                str(scope["closed_at"]): ordinal
                for ordinal, (_closed_datetime, scope) in enumerate(cycle_order)
            }

            segment_rows = connection.execute(
                """
                SELECT segment_id, code, required, started_at,
                       ended_at, end_reason, calendar_fingerprint
                FROM trusted_paper_bar_segment
                ORDER BY code, started_at, segment_id
                """
            ).fetchall()
            segments = {row[0]: row for row in segment_rows}
            if len(segments) != len(segment_rows):
                raise ValueError("duplicate segment")
            members_by_segment: dict[
                str, list[tuple[str, dict[str, object]]]
            ] = {segment_id: [] for segment_id in segments}
            member_rows = connection.execute(
                """
                SELECT m.bar_id, m.segment_id, m.closed_at, m.required,
                       b.bar_id, b.code, b.closed_at
                FROM trusted_paper_bar_segment_member AS m
                LEFT JOIN trusted_paper_bar AS b ON b.bar_id = m.bar_id
                ORDER BY m.closed_at, m.bar_id
                """
            ).fetchall()
            for member_row in member_rows:
                (
                    member_bar_id,
                    segment_id,
                    member_closed_at,
                    member_required,
                    stored_bar_id,
                    bar_code,
                    bar_closed_at,
                ) = member_row
                segment = segments.get(segment_id)
                scope = cycle_scopes.get(member_closed_at)
                if segment is None or scope is None:
                    raise ValueError("orphan segment member")
                (
                    _stored_segment_id,
                    segment_code,
                    segment_required,
                    _started_at,
                    _ended_at,
                    _end_reason,
                    segment_calendar,
                ) = segment
                expected_required = int(
                    segment_code in scope["required"]
                )
                if (
                    stored_bar_id != member_bar_id
                    or bar_code != segment_code
                    or bar_closed_at != member_closed_at
                    or member_required not in (0, 1)
                    or segment_required not in (0, 1)
                    or member_required != segment_required
                    or member_required != expected_required
                    or segment_code not in scope["persisted"]
                    or (
                        not bool(segment_required)
                        and segment_code not in scope["optional"]
                    )
                    or segment_calendar != scope["calendar_fingerprint"]
                ):
                    raise ValueError("invalid segment member binding")
                members_by_segment[segment_id].append(
                    (member_closed_at, scope)
                )

            legal_end_reasons = {
                "requirement_changed",
                "observation_gap",
                "optional_unavailable",
                "membership_removed",
            }

            def slots_are_adjacent(
                previous: dict[str, object],
                current: dict[str, object],
            ) -> bool:
                if previous["trading_day"] == current["trading_day"]:
                    return current["slot_index"] == previous["slot_index"] + 1
                return (
                    previous["slot_index"] == 47
                    and current["slot_index"] == 0
                    and current["trading_day"] > previous["trading_day"]
                )

            ranges_by_code: dict[
                str,
                list[tuple[datetime, datetime, bool, str, str | None]],
            ] = {}
            for segment in segment_rows:
                (
                    segment_id,
                    code,
                    required,
                    started_at,
                    ended_at,
                    end_reason,
                    calendar_fingerprint,
                ) = segment
                members = members_by_segment[segment_id]
                if (
                    not isinstance(code, str)
                    or not code
                    or required not in (0, 1)
                    or not _valid_sha256_fingerprint(calendar_fingerprint)
                    or (
                        self._calendar_fingerprint is not None
                        and calendar_fingerprint != self._calendar_fingerprint
                    )
                    or not members
                ):
                    raise ValueError("invalid segment metadata")
                started_datetime = _parse_datetime(
                    started_at,
                    "segment_started_at",
                )
                expected_segment_id = sha256_json(
                    {
                        "schema_version": 1,
                        "code": code,
                        "required": bool(required),
                        "started_at": started_datetime,
                        "calendar_fingerprint": calendar_fingerprint,
                    }
                )
                if segment_id != expected_segment_id:
                    raise ValueError("invalid segment id preimage")
                members.sort(key=lambda item: item[0])
                first_closed_at = members[0][0]
                last_closed_at = members[-1][0]
                first_datetime = _parse_datetime(
                    first_closed_at,
                    "segment_first_member",
                )
                last_datetime = _parse_datetime(
                    last_closed_at,
                    "segment_last_member",
                )
                if started_at != first_closed_at:
                    raise ValueError("segment start mismatch")
                for previous_member, current_member in zip(
                    members,
                    members[1:],
                    strict=False,
                ):
                    if not slots_are_adjacent(
                        previous_member[1],
                        current_member[1],
                    ):
                        raise ValueError("segment member discontinuity")
                    if cycle_ordinals[current_member[0]] != (
                        cycle_ordinals[previous_member[0]] + 1
                    ):
                        raise ValueError("segment member skips recorded cycle")
                if ended_at is None:
                    if end_reason is not None or any(
                        cycle_closed_at > last_datetime
                        for cycle_closed_at, _scope in cycle_order
                    ):
                        raise ValueError("invalid active segment")
                else:
                    if (
                        ended_at != last_closed_at
                        or end_reason not in legal_end_reasons
                    ):
                        raise ValueError("invalid closed segment")
                    next_scope = next(
                        (
                            scope
                            for cycle_closed_at, scope in cycle_order
                            if cycle_closed_at > last_datetime
                        ),
                        None,
                    )
                    if next_scope is None:
                        raise ValueError("closed segment without successor")
                    next_required = code in next_scope["required"]
                    next_optional = code in next_scope["optional"]
                    next_persisted = code in next_scope["persisted"]
                    if next_persisted:
                        if next_required != bool(required):
                            expected_end_reason = "requirement_changed"
                        elif (
                            bool(required)
                            or (
                                next_scope["trading_day"]
                                == members[-1][1]["trading_day"]
                                and slots_are_adjacent(
                                    members[-1][1],
                                    next_scope,
                                )
                            )
                        ):
                            raise ValueError("invalid segment split")
                        else:
                            expected_end_reason = "observation_gap"
                    elif next_optional and code in next_scope["failures"]:
                        expected_end_reason = "optional_unavailable"
                    elif not next_required and not next_optional:
                        expected_end_reason = "membership_removed"
                    else:
                        raise ValueError("underivable segment end")
                    if end_reason != expected_end_reason:
                        raise ValueError("segment end reason mismatch")
                ranges_by_code.setdefault(code, []).append(
                    (
                        first_datetime,
                        last_datetime,
                        ended_at is None,
                        segment_id,
                        end_reason,
                    )
                )

            for ranges in ranges_by_code.values():
                ranges.sort(key=lambda item: item[0])
                active_count = sum(int(item[2]) for item in ranges)
                if active_count > 1 or (
                    active_count == 1 and not ranges[-1][2]
                ):
                    raise ValueError("invalid active segment ordering")
                for previous, current in zip(
                    ranges,
                    ranges[1:],
                    strict=False,
                ):
                    if previous[1] >= current[0] or previous[2]:
                        raise ValueError("overlapping segments")
        except TrustedPaperBarIntegrityError as exc:
            if str(exc) == "paper_bar_segment_integrity_failure":
                raise
            raise TrustedPaperBarIntegrityError(
                "paper_bar_segment_integrity_failure"
            ) from exc
        except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_bar_segment_integrity_failure"
            ) from exc

    @staticmethod
    def _has_unbound_bars(connection: sqlite3.Connection) -> bool:
        rows = connection.execute(
            """
            SELECT b.code, b.closed_at,
                   m.bar_id, m.closed_at, m.required,
                   s.code, s.required, s.calendar_fingerprint,
                   c.calendar_fingerprint, c.persisted_codes_json
            FROM trusted_paper_bar AS b
            LEFT JOIN trusted_paper_bar_segment_member AS m
              ON m.bar_id = b.bar_id
            LEFT JOIN trusted_paper_bar_segment AS s
              ON s.segment_id = m.segment_id
            LEFT JOIN trusted_paper_bar_cycle AS c
              ON c.closed_at = m.closed_at
            """
        )
        for row in rows:
            (
                code,
                closed_at,
                member_bar_id,
                member_closed_at,
                member_required,
                segment_code,
                segment_required,
                segment_fingerprint,
                cycle_fingerprint,
                persisted_json,
            ) = row
            try:
                persisted_codes = json.loads(persisted_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                return True
            if (
                member_bar_id is None
                or member_closed_at != closed_at
                or member_required not in (0, 1)
                or segment_code != code
                or segment_required != member_required
                or segment_fingerprint != cycle_fingerprint
                or not isinstance(persisted_codes, list)
                or code not in persisted_codes
            ):
                return True
        return False

    @staticmethod
    def _cycle_payload(
        *,
        closed_at: str,
        trading_day: str,
        slot_index: int,
        calendar_fingerprint: str,
        required_codes: Sequence[str],
        optional_codes: Sequence[str],
        persisted_codes: Sequence[str],
        optional_failures: Mapping[str, str],
        bar_bindings: Mapping[str, Mapping[str, str]] | None = None,
        bar_binding_schema_version: int | None = None,
    ) -> dict[str, object]:
        schema_version = 1
        if bar_bindings is not None:
            includes_segment = {
                "segment_id" in binding for binding in bar_bindings.values()
            }
            if len(includes_segment) > 1:
                raise ValueError("bar bindings must use one schema version")
            if bar_binding_schema_version is not None:
                if bar_binding_schema_version not in (2, 3):
                    raise ValueError("bar binding schema version must be 2 or 3")
                schema_version = bar_binding_schema_version
            else:
                schema_version = 3 if includes_segment != {False} else 2
        elif bar_binding_schema_version is not None:
            raise ValueError("bar binding schema requires bar bindings")
        payload: dict[str, object] = {
            "schema_version": schema_version,
            "closed_at": closed_at,
            "trading_day": trading_day,
            "slot_index": slot_index,
            "calendar_fingerprint": calendar_fingerprint,
            "required_codes": list(required_codes),
            "optional_codes": list(optional_codes),
            "persisted_codes": list(persisted_codes),
            "optional_failures": dict(optional_failures),
        }
        if bar_bindings is not None:
            payload["bar_bindings"] = {
                code: dict(binding)
                for code, binding in sorted(bar_bindings.items())
            }
        return payload

    def _canonical_cycle_bars(
        self,
        connection: sqlite3.Connection,
        *,
        closed_at: str,
        calendar_fingerprint: str,
        required_codes: Sequence[str],
        persisted_codes: Sequence[str],
    ) -> tuple[tuple[PaperBar, ...], dict[str, dict[str, str]]]:
        required = set(required_codes)
        bars: list[PaperBar] = []
        bindings: dict[str, dict[str, str]] = {}
        for code in persisted_codes:
            row = connection.execute(
                """
                SELECT b.bar_id, b.code, b.opened_at, b.closed_at,
                       b.payload_json, b.payload_sha256,
                       m.segment_id, m.closed_at, m.required,
                       s.code, s.required, s.calendar_fingerprint
                FROM trusted_paper_bar AS b
                LEFT JOIN trusted_paper_bar_segment_member AS m
                  ON m.bar_id = b.bar_id
                LEFT JOIN trusted_paper_bar_segment AS s
                  ON s.segment_id = m.segment_id
                WHERE b.code = ? AND b.closed_at = ?
                """,
                (code, closed_at),
            ).fetchone()
            expected_required = int(code in required)
            if (
                row is None
                or not isinstance(row[6], str)
                or not row[6]
                or row[7:]
                != (
                    closed_at,
                    expected_required,
                    code,
                    expected_required,
                    calendar_fingerprint,
                )
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_binding_invalid"
                )
            bar = self._decode_row(connection, row[:6])
            bars.append(bar)
            bindings[code] = {
                "bar_id": bar.bar_id,
                "payload_sha256": row[5],
                "segment_id": row[6],
            }
        return tuple(bars), bindings

    def _decode_cycle_row(
        self,
        connection: sqlite3.Connection,
        row: tuple[object, ...],
        *,
        attested_v2_cycle_checksums: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, object], tuple[PaperBar, ...]]:
        (
            closed_at,
            trading_day,
            slot_index,
            calendar_fingerprint,
            required_json,
            optional_json,
            persisted_json,
            failures_json,
            payload_sha256,
        ) = row
        try:
            required = json.loads(required_json)
            optional = json.loads(optional_json)
            persisted = json.loads(persisted_json)
            failures = json.loads(failures_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_bar_cycle_payload_invalid"
            ) from exc
        if (
            not isinstance(closed_at, str)
            or not isinstance(trading_day, str)
            or isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or not _valid_sha256_fingerprint(calendar_fingerprint)
            or any(
                not isinstance(values, list)
                or any(not isinstance(code, str) or not code for code in values)
                for values in (required, optional, persisted)
            )
            or not isinstance(failures, dict)
            or any(
                not isinstance(code, str)
                or not code
                or not isinstance(reason, str)
                or not reason
                for code, reason in failures.items()
            )
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_bar_cycle_payload_invalid"
            )
        bars, bar_bindings = self._canonical_cycle_bars(
            connection,
            closed_at=closed_at,
            calendar_fingerprint=calendar_fingerprint,
            required_codes=required,
            persisted_codes=persisted,
        )
        payload = self._cycle_payload(
            closed_at=closed_at,
            trading_day=trading_day,
            slot_index=slot_index,
            calendar_fingerprint=calendar_fingerprint,
            required_codes=required,
            optional_codes=optional,
            persisted_codes=persisted,
            optional_failures=failures,
            bar_bindings=bar_bindings,
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if _payload_sha256(encoded) != payload_sha256:
            v2_bindings = {
                code: {
                    "bar_id": binding["bar_id"],
                    "payload_sha256": binding["payload_sha256"],
                }
                for code, binding in bar_bindings.items()
            }
            v2_payload = self._cycle_payload(
                closed_at=closed_at,
                trading_day=trading_day,
                slot_index=slot_index,
                calendar_fingerprint=calendar_fingerprint,
                required_codes=required,
                optional_codes=optional,
                persisted_codes=persisted,
                optional_failures=failures,
                bar_bindings=v2_bindings,
                bar_binding_schema_version=2,
            )
            v2_encoded = json.dumps(
                v2_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if _payload_sha256(v2_encoded) == payload_sha256:
                cycle_state = connection.execute(
                    """
                    SELECT completed FROM trusted_paper_bar_cycle
                    WHERE closed_at = ?
                    """,
                    (closed_at,),
                ).fetchone()
                if cycle_state != (1,):
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_v2_unattested"
                    )
                if attested_v2_cycle_checksums is None:
                    attested_v2_cycle_checksums = (
                        self._validate_exit_manifest_log(connection)
                    )
                if (
                    attested_v2_cycle_checksums.get(closed_at)
                    != payload_sha256
                ):
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_v2_unattested"
                    )
                return payload, bars
            legacy_payload = self._cycle_payload(
                closed_at=closed_at,
                trading_day=trading_day,
                slot_index=slot_index,
                calendar_fingerprint=calendar_fingerprint,
                required_codes=required,
                optional_codes=optional,
                persisted_codes=persisted,
                optional_failures=failures,
            )
            legacy_encoded = json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if _payload_sha256(legacy_encoded) == payload_sha256:
                reason = "paper_bar_cycle_legacy_checksum_unattested"
                self._mark_degraded(
                    connection,
                    reason,
                    datetime.now(_CN),
                )
                raise TrustedPaperBarIntegrityError(reason)
            raise TrustedPaperBarIntegrityError(
                "paper_bar_cycle_checksum_mismatch"
            )
        return payload, bars

    @staticmethod
    def _validate_code_set(values: Sequence[str], field: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise TypeError(f"{field} must be a sequence of codes")
        result = tuple(sorted(values))
        if (
            len(result) != len(set(result))
            or any(not isinstance(code, str) or not code for code in result)
        ):
            raise ValueError(f"{field} must contain unique non-empty codes")
        return result

    @staticmethod
    def _segments_are_adjacent(
        *,
        previous_trading_day: str,
        previous_slot: int,
        session: PaperTradingSession,
        current_slot: int,
    ) -> bool:
        if previous_trading_day == session.trading_day.isoformat():
            return current_slot == previous_slot + 1
        return (
            previous_slot == 47
            and current_slot == 0
            and session.previous_trading_day is not None
            and previous_trading_day
            == session.previous_trading_day.isoformat()
        )

    def _attempt_identity(
        self,
        session: object,
        bar_closed_at: datetime,
    ) -> tuple[PaperTradingSession, datetime, int, str]:
        fingerprint = self._calendar_fingerprint or getattr(
            session,
            "calendar_fingerprint",
            None,
        )
        if not _valid_sha256_fingerprint(fingerprint):
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_fingerprint_missing"
            )
        normalized_session = _validated_trading_session(
            session,
            calendar_fingerprint=fingerprint,
        )
        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        try:
            slot_index = normalized_session.expected_bar_closes.index(closed_at)
        except ValueError as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_bar_not_in_calendar_session"
            ) from exc
        return normalized_session, closed_at, slot_index, fingerprint

    def _start_cycle_attempt_connection(
        self,
        connection: sqlite3.Connection,
        *,
        normalized_session: PaperTradingSession,
        closed_at: datetime,
        slot_index: int,
        fingerprint: str,
        increment_started: bool,
    ) -> None:
        existing = connection.execute(
            """
            SELECT trading_day, slot_index, calendar_fingerprint,
                   status, attempt_generation
            FROM trusted_paper_bar_attempt WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        expected = (
            normalized_session.trading_day.isoformat(),
            slot_index,
            fingerprint,
        )
        if existing is not None and existing[:3] != expected:
            raise TrustedPaperBarIntegrityError(
                "paper_bar_attempt_identity_conflict"
            )
        now = datetime.now(_CN).isoformat()
        if existing is None:
            connection.execute(
                """
                INSERT INTO trusted_paper_bar_attempt (
                    closed_at, trading_day, slot_index,
                    calendar_fingerprint, status, failure_reason,
                    attempt_generation, updated_at
                ) VALUES (?, ?, ?, ?, 'started', NULL, 1, ?)
                """,
                (closed_at.isoformat(), *expected, now),
            )
        elif existing[3] != "complete" and (
            existing[3] != "started" or increment_started
        ):
            connection.execute(
                """
                UPDATE trusted_paper_bar_attempt
                SET status = 'started', failure_reason = NULL,
                    attempt_generation = attempt_generation + 1,
                    updated_at = ?
                WHERE closed_at = ?
                """,
                (now, closed_at.isoformat()),
            )

    @staticmethod
    def _attest_cycle_attempt_state(
        connection: sqlite3.Connection,
        *,
        closed_at: str,
        completed: bool,
        expected_generation: int | None = None,
    ) -> tuple[str, int, str | None]:
        attempt = connection.execute(
            """
            SELECT status, attempt_generation, failure_reason
            FROM trusted_paper_bar_attempt WHERE closed_at = ?
            """,
            (closed_at,),
        ).fetchone()
        if attempt is None:
            raise TrustedPaperBarIntegrityError("paper_bar_attempt_missing")
        status, attempt_generation, failure_reason = attempt
        if (
            isinstance(attempt_generation, bool)
            or not isinstance(attempt_generation, int)
            or attempt_generation <= 0
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_bar_attempt_generation_mismatch"
            )
        if completed:
            if status != "complete" or failure_reason is not None:
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_attempt_replay_invalid"
                )
        elif status != "started" or failure_reason is not None:
            raise TrustedPaperBarIntegrityError(
                "paper_bar_attempt_not_started"
            )
        if (
            expected_generation is not None
            and attempt_generation != expected_generation
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_bar_attempt_generation_mismatch"
            )
        return status, attempt_generation, failure_reason

    @mutation_fenced("trusted_paper_bar_store.start_cycle_attempt")
    def start_cycle_attempt(
        self,
        *,
        session: object,
        bar_closed_at: datetime,
    ) -> None:
        self._mutation_fence.require()
        normalized_session, closed_at, slot_index, fingerprint = (
            self._attempt_identity(session, bar_closed_at)
        )
        with self._lock, self._connect() as connection:
            self._validate_segment_ledger(connection)
            self._start_cycle_attempt_connection(
                connection,
                normalized_session=normalized_session,
                closed_at=closed_at,
                slot_index=slot_index,
                fingerprint=fingerprint,
                increment_started=True,
            )
            connection.commit()

    @mutation_fenced("trusted_paper_bar_store.fail_cycle_attempt")
    def fail_cycle_attempt(
        self,
        bar_closed_at: datetime,
        reason: str,
    ) -> None:
        self._mutation_fence.require()
        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        with self._lock, self._connect() as connection:
            self._validate_segment_ledger(connection)
            row = connection.execute(
                """
                SELECT status FROM trusted_paper_bar_attempt
                WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()
            if row is None:
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_attempt_missing"
                )
            if row[0] != "complete":
                connection.execute(
                    """
                    UPDATE trusted_paper_bar_attempt
                    SET status = 'failed', failure_reason = ?, updated_at = ?
                    WHERE closed_at = ?
                    """,
                    (
                        reason,
                        datetime.now(_CN).isoformat(),
                        closed_at.isoformat(),
                    ),
                )
                connection.commit()

    @staticmethod
    def _calendar_preflight_failure_payload(
        *,
        failed_at: str,
        reason: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "calendar_preflight_failed",
            "failed_at": failed_at,
            "reason": reason,
        }

    @staticmethod
    def _calendar_preflight_resolution_payload(
        *,
        failure_id: int,
        failure_payload_sha256: str,
        resolved_at: str,
        resolved_by_bar_closed_at: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "calendar_preflight_resolved",
            "failure_id": failure_id,
            "failure_payload_sha256": failure_payload_sha256,
            "resolved_at": resolved_at,
            "resolved_by_bar_closed_at": resolved_by_bar_closed_at,
        }

    @staticmethod
    def _calendar_preflight_watermark_payload(
        *,
        closed_at: str,
        observed_at: str,
        cycle_payload_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "calendar_preflight_watermark_committed",
            "closed_at": closed_at,
            "observed_at": observed_at,
            "cycle_payload_sha256": cycle_payload_sha256,
        }

    @staticmethod
    def _payload_checksum(payload: Mapping[str, object]) -> str:
        return _payload_sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _normalize_exit_commitments(
        commitments: Sequence[ExitEvaluationCommitment],
    ) -> tuple[ExitEvaluationCommitment, ...]:
        if isinstance(commitments, (str, bytes)):
            raise TypeError(
                "exit_commitments must contain ExitEvaluationCommitment values"
            )
        frozen = tuple(commitments)
        if not all(
            isinstance(commitment, ExitEvaluationCommitment)
            for commitment in frozen
        ):
            raise TypeError(
                "exit_commitments must contain ExitEvaluationCommitment values"
            )
        normalized = tuple(sorted(frozen))
        if (
            len(set(normalized)) != len(normalized)
            or len({item.snapshot_id for item in normalized}) != len(normalized)
            or len(
                {
                    (item.entry_event_id, item.evaluation_cycle_id)
                    for item in normalized
                }
            )
            != len(normalized)
        ):
            raise ValueError("exit_commitments must be unique")
        return normalized

    @staticmethod
    def _exit_manifest_log_state_payload(
        *,
        event_count: int,
        max_sequence: int,
        history_head_sha256: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_count": event_count,
            "max_sequence": max_sequence,
            "history_head_sha256": history_head_sha256,
        }

    @staticmethod
    def _validate_exit_manifest_schema(
        connection: sqlite3.Connection,
    ) -> None:
        manifest_columns = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in connection.execute(
                "PRAGMA table_info(trusted_paper_exit_manifest)"
            )
        )
        expected_manifest_columns = (
            ("closed_at", "TEXT", 0, 1),
            ("manifest_sequence", "INTEGER", 1, 0),
            ("previous_manifest_sha256", "TEXT", 0, 0),
            ("payload_json", "TEXT", 1, 0),
            ("payload_sha256", "TEXT", 1, 0),
        )
        state_columns = tuple(
            (row[1], str(row[2]).upper(), row[3], row[5])
            for row in connection.execute(
                "PRAGMA table_info(trusted_paper_exit_manifest_log_state)"
            )
        )
        expected_state_columns = (
            ("singleton_id", "INTEGER", 0, 1),
            ("event_count", "INTEGER", 1, 0),
            ("max_sequence", "INTEGER", 1, 0),
            ("history_head_sha256", "TEXT", 0, 0),
            ("payload_sha256", "TEXT", 1, 0),
        )
        sequence_unique = False
        for index_row in connection.execute(
            "PRAGMA index_list(trusted_paper_exit_manifest)"
        ):
            if index_row[2] != 1 or index_row[4] != 0:
                continue
            index_columns = tuple(
                row[2]
                for row in connection.execute(
                    f"PRAGMA index_info({json.dumps(index_row[1])})"
                )
            )
            if index_columns == ("manifest_sequence",):
                sequence_unique = True
                break
        if (
            manifest_columns != expected_manifest_columns
            or state_columns != expected_state_columns
            or not sequence_unique
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_schema_invalid"
            )

    @staticmethod
    def _exit_manifest_payload(
        *,
        manifest_sequence: int,
        previous_manifest_sha256: str | None,
        closed_at: str,
        strategy_run_binding: tuple[str, int, str, str, str] | None,
        bar_cycle_payload_sha256: str,
        signal_observation_payload_sha256: str | None,
        signal_observation_resolution_sha256: str | None,
        commitments: tuple[ExitEvaluationCommitment, ...],
    ) -> dict[str, object]:
        strategy_run = (
            None
            if strategy_run_binding is None
            else {
                "run_id": strategy_run_binding[0],
                "epoch": strategy_run_binding[1],
                "strategy_run_fingerprint": strategy_run_binding[2],
                "identity_sha256": strategy_run_binding[3],
                "store_instance_id": strategy_run_binding[4],
            }
        )
        return {
            "schema_version": 1,
            "event": "paper_exit_manifest_committed",
            "manifest_sequence": manifest_sequence,
            "previous_manifest_sha256": previous_manifest_sha256,
            "closed_at": closed_at,
            "strategy_run": strategy_run,
            "bar_cycle_payload_sha256": bar_cycle_payload_sha256,
            "signal_observation_payload_sha256": (
                signal_observation_payload_sha256
            ),
            "signal_observation_resolution_sha256": (
                signal_observation_resolution_sha256
            ),
            "commitments": [item.to_dict() for item in commitments],
        }

    def _decode_exit_manifest_row(
        self,
        connection: sqlite3.Connection,
        row: tuple[object, ...],
    ) -> tuple[ExitEvaluationCommitment, ...]:
        if len(row) != 5:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        (
            manifest_sequence,
            closed_at,
            previous_manifest_sha256,
            payload_json,
            payload_sha256,
        ) = row
        if (
            isinstance(manifest_sequence, bool)
            or not isinstance(manifest_sequence, int)
            or manifest_sequence <= 0
            or not isinstance(closed_at, str)
            or (
                previous_manifest_sha256 is not None
                and not _valid_sha256_fingerprint(previous_manifest_sha256)
            )
            or not isinstance(payload_json, str)
            or not isinstance(payload_sha256, str)
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        if _payload_sha256(payload_json) != payload_sha256:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            ) from exc
        required_fields = {
            "schema_version",
            "event",
            "manifest_sequence",
            "previous_manifest_sha256",
            "closed_at",
            "strategy_run",
            "bar_cycle_payload_sha256",
            "signal_observation_payload_sha256",
            "signal_observation_resolution_sha256",
            "commitments",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required_fields
            or payload["schema_version"] != 1
            or payload["event"] != "paper_exit_manifest_committed"
            or payload["manifest_sequence"] != manifest_sequence
            or payload["previous_manifest_sha256"]
            != previous_manifest_sha256
            or payload["closed_at"] != closed_at
            or not _valid_sha256_fingerprint(
                payload["bar_cycle_payload_sha256"]
            )
            or not isinstance(payload["commitments"], list)
            or json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            != payload_json
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        try:
            commitments = tuple(
                ExitEvaluationCommitment.from_dict(item)
                for item in payload["commitments"]
            )
            normalized_commitments = self._normalize_exit_commitments(
                commitments
            )
        except (TypeError, ValueError, ExitEvaluationIntegrityError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            ) from exc
        if commitments != normalized_commitments:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        try:
            manifest_closed_at = _parse_datetime(
                closed_at,
                "paper_exit_manifest_closed_at",
            )
        except TrustedPaperBarIntegrityError as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            ) from exc
        if any(
            commitment.evaluated_at != manifest_closed_at
            for commitment in commitments
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_cycle_mismatch"
            )

        raw_binding = payload["strategy_run"]
        manifest_binding: tuple[str, int, str, str, str] | None
        if raw_binding is None:
            manifest_binding = None
        elif isinstance(raw_binding, dict) and set(raw_binding) == {
            "run_id",
            "epoch",
            "strategy_run_fingerprint",
            "identity_sha256",
            "store_instance_id",
        }:
            manifest_binding = (
                raw_binding["run_id"],
                raw_binding["epoch"],
                raw_binding["strategy_run_fingerprint"],
                raw_binding["identity_sha256"],
                raw_binding["store_instance_id"],
            )
            if (
                not isinstance(manifest_binding[0], str)
                or not manifest_binding[0]
                or isinstance(manifest_binding[1], bool)
                or not isinstance(manifest_binding[1], int)
                or manifest_binding[1] <= 0
                or not _valid_sha256_fingerprint(manifest_binding[2])
                or not _valid_sha256_fingerprint(manifest_binding[3])
                or not isinstance(manifest_binding[4], str)
                or not manifest_binding[4]
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_integrity_failure"
                )
        else:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_integrity_failure"
            )
        if (
            self._strategy_run_binding is not None
            and manifest_binding != self._strategy_run_binding
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_strategy_mismatch"
            )

        cycle = connection.execute(
            """
            SELECT completed, payload_sha256
            FROM trusted_paper_bar_cycle WHERE closed_at = ?
            """,
            (closed_at,),
        ).fetchone()
        if (
            cycle is None
            or cycle[0] != 1
            or cycle[1] != payload["bar_cycle_payload_sha256"]
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_cycle_binding_invalid"
            )
        observation = connection.execute(
            """
            SELECT payload_json, payload_sha256
            FROM trusted_signal_observation_cycle WHERE closed_at = ?
            """,
            (closed_at,),
        ).fetchone()
        if observation is None:
            if (
                payload["signal_observation_payload_sha256"] is not None
                or payload["signal_observation_resolution_sha256"] is not None
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_observation_binding_invalid"
                )
        else:
            try:
                observation_payload = json.loads(observation[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_observation_binding_invalid"
                ) from exc
            if (
                observation[1]
                != payload["signal_observation_payload_sha256"]
                or not isinstance(observation_payload, dict)
                or observation_payload.get("resolution_sha256")
                != payload["signal_observation_resolution_sha256"]
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_observation_binding_invalid"
                )
        return commitments

    def _validate_exit_manifest_log(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, str]:
        self._exit_manifest_validated_prefix = None
        cycles = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT closed_at, completed
                FROM trusted_paper_bar_cycle ORDER BY closed_at
                """
            )
        }
        rows = tuple(
            connection.execute(
                """
                SELECT manifest_sequence, closed_at,
                       previous_manifest_sha256,
                       payload_json, payload_sha256
                FROM trusted_paper_exit_manifest ORDER BY manifest_sequence
                """
            )
        )
        manifest_closes = {row[1] for row in rows}
        completed_closes = {
            closed_at for closed_at, completed in cycles.items() if completed == 1
        }
        if (
            len(manifest_closes) != len(rows)
            or manifest_closes != completed_closes
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_coverage_invalid"
            )
        previous_manifest_sha256 = None
        attested_cycle_checksums: dict[str, str] = {}
        for expected_sequence, row in enumerate(rows, start=1):
            if (
                row[0] != expected_sequence
                or row[2] != previous_manifest_sha256
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_history_invalid"
                )
            self._decode_exit_manifest_row(connection, row)
            attested_cycle_checksums[row[1]] = json.loads(row[3])[
                "bar_cycle_payload_sha256"
            ]
            previous_manifest_sha256 = row[4]
        state_row = connection.execute(
            """
            SELECT event_count, max_sequence,
                   history_head_sha256, payload_sha256
            FROM trusted_paper_exit_manifest_log_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        expected_state = self._exit_manifest_log_state_payload(
            event_count=len(rows),
            max_sequence=len(rows),
            history_head_sha256=previous_manifest_sha256,
        )
        if (
            state_row is None
            or state_row[:3]
            != (len(rows), len(rows), previous_manifest_sha256)
            or state_row[3] != sha256_json(expected_state)
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_log_state_invalid"
            )
        self._validate_exit_manifest_anchors(rows)
        tail = rows[-1] if rows else None
        self._exit_manifest_validated_prefix = _ExitManifestValidatedPrefix(
            event_count=len(rows),
            max_sequence=len(rows),
            history_head_sha256=previous_manifest_sha256,
            log_state_payload_sha256=state_row[3],
            tail_closed_at=None if tail is None else tail[1],
            tail_previous_manifest_sha256=(
                None if tail is None else tail[2]
            ),
            tail_payload_sha256=None if tail is None else tail[4],
        )
        return attested_cycle_checksums

    def _attest_exit_manifest_append_prefix(
        self,
        connection: sqlite3.Connection,
    ) -> _ExitManifestValidatedPrefix:
        """Verify the authenticated manifest tail before one append."""

        def full_rebase() -> _ExitManifestValidatedPrefix:
            self._validate_exit_manifest_log(connection)
            refreshed = self._exit_manifest_validated_prefix
            if refreshed is None:
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_log_state_invalid"
                )
            return refreshed

        cached = self._exit_manifest_validated_prefix
        state_row = connection.execute(
            """
            SELECT event_count, max_sequence,
                   history_head_sha256, payload_sha256
            FROM trusted_paper_exit_manifest_log_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if cached is None or state_row != (
            cached.event_count,
            cached.max_sequence,
            cached.history_head_sha256,
            cached.log_state_payload_sha256,
        ):
            return full_rebase()
        if cached.event_count == 0:
            unexpected = connection.execute(
                """
                SELECT manifest_sequence
                FROM trusted_paper_exit_manifest
                ORDER BY manifest_sequence DESC LIMIT 1
                """
            ).fetchone()
            if unexpected is not None:
                return full_rebase()
            return cached
        row = connection.execute(
            """
            SELECT manifest_sequence, closed_at,
                   previous_manifest_sha256,
                   payload_json, payload_sha256
            FROM trusted_paper_exit_manifest
            WHERE manifest_sequence = ?
            """,
            (cached.max_sequence,),
        ).fetchone()
        if row is None or (
            row[0],
            row[1],
            row[2],
            row[4],
        ) != (
            cached.max_sequence,
            cached.tail_closed_at,
            cached.tail_previous_manifest_sha256,
            cached.tail_payload_sha256,
        ):
            return full_rebase()
        try:
            self._decode_exit_manifest_row(connection, row)
            self._validate_exit_manifest_anchor(row)
        except TrustedPaperBarIntegrityError:
            return full_rebase()
        return cached

    def _attest_committed_exit_manifest(
        self,
        connection: sqlite3.Connection,
        *,
        closed_at: datetime,
        commitments: tuple[ExitEvaluationCommitment, ...],
    ) -> None:
        self._validate_exit_manifest_log(connection)
        row = connection.execute(
            """
            SELECT manifest_sequence, closed_at,
                   previous_manifest_sha256,
                   payload_json, payload_sha256
            FROM trusted_paper_exit_manifest WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        if row is None:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_missing"
            )
        if self._decode_exit_manifest_row(connection, row) != commitments:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_replay_mismatch"
            )

    @staticmethod
    def _exit_manifest_anchor_payload(
        *,
        manifest_sequence: int,
        closed_at: str,
        history_head_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "manifest_sequence": manifest_sequence,
            "closed_at": closed_at,
            "history_head_sha256": history_head_sha256,
        }

    def _exit_manifest_anchor_path(
        self,
        manifest_sequence: int,
        history_head_sha256: str,
    ) -> Path:
        return self._exit_manifest_anchor_dir / (
            f"{manifest_sequence:020d}-{history_head_sha256[7:]}.json"
        )

    def _write_exit_manifest_anchor(
        self,
        *,
        manifest_sequence: int,
        closed_at: str,
        history_head_sha256: str,
    ) -> None:
        payload = self._exit_manifest_anchor_payload(
            manifest_sequence=manifest_sequence,
            closed_at=closed_at,
            history_head_sha256=history_head_sha256,
        )
        encoded = {**payload, "payload_sha256": sha256_json(payload)}
        final = self._exit_manifest_anchor_path(
            manifest_sequence,
            history_head_sha256,
        )
        temporary = self._path.parent / (
            final.name + f".{os.getpid()}.{id(self)}.tmp"
        )
        self._exit_manifest_anchor_dir.mkdir(exist_ok=True)
        if not self._exit_manifest_anchor_dir.is_dir():
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_unavailable"
            )
        if final.exists():
            try:
                existing = json.loads(final.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_anchor_invalid"
                ) from exc
            if existing != encoded:
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_anchor_conflict"
                )
            return
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    encoded,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_exit_manifest_anchors(
        self,
        rows: Sequence[tuple[object, ...]],
    ) -> None:
        if self._exit_manifest_anchor_dir.exists():
            if not self._exit_manifest_anchor_dir.is_dir():
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_anchor_mismatch"
                )
            files = tuple(sorted(self._exit_manifest_anchor_dir.iterdir()))
            if any(
                not path.is_file() or path.suffix != ".json"
                for path in files
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_anchor_mismatch"
                )
        else:
            files = ()
        expected_paths = [
            self._validate_exit_manifest_anchor(row) for row in rows
        ]
        if tuple(files) != tuple(expected_paths):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_mismatch"
            )

    def _validate_exit_manifest_anchor(
        self,
        row: tuple[object, ...],
    ) -> Path:
        if len(row) != 5:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_mismatch"
            )
        sequence = row[0]
        closed_at = row[1]
        history_head_sha256 = row[4]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or not isinstance(closed_at, str)
            or not _valid_sha256_fingerprint(history_head_sha256)
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_mismatch"
            )
        expected = self._exit_manifest_anchor_payload(
            manifest_sequence=sequence,
            closed_at=closed_at,
            history_head_sha256=history_head_sha256,
        )
        expected_encoded = {
            **expected,
            "payload_sha256": sha256_json(expected),
        }
        path = self._exit_manifest_anchor_path(
            sequence,
            history_head_sha256,
        )
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_mismatch"
            ) from exc
        if actual != expected_encoded:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_manifest_anchor_mismatch"
            )
        return path

    def _committed_calendar_preflight_watermark(
        self,
        connection: sqlite3.Connection,
        *,
        closed_at: datetime,
    ) -> datetime | None:
        row = connection.execute(
            """
            SELECT observed_at, cycle_payload_sha256, payload_sha256
            FROM trusted_paper_bar_calendar_preflight_watermark
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        observed_at, cycle_payload_sha256, payload_sha256 = row
        cycle = connection.execute(
            """
            SELECT completed, payload_sha256
            FROM trusted_paper_bar_cycle WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        try:
            parsed_observed_at = _parse_datetime(
                observed_at,
                "calendar_preflight_watermark_observed_at",
            )
        except TrustedPaperBarIntegrityError as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_preflight_watermark_invalid"
            ) from exc
        payload = self._calendar_preflight_watermark_payload(
            closed_at=closed_at.isoformat(),
            observed_at=observed_at,
            cycle_payload_sha256=cycle_payload_sha256,
        )
        if (
            cycle is None
            or cycle[0] != 1
            or not isinstance(cycle_payload_sha256, str)
            or cycle[1] != cycle_payload_sha256
            or not isinstance(payload_sha256, str)
            or self._payload_checksum(payload) != payload_sha256
            or parsed_observed_at.isoformat() != observed_at
            or parsed_observed_at < closed_at
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_preflight_watermark_invalid"
            )
        return parsed_observed_at

    def _validate_calendar_preflight_log(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        failures: dict[int, tuple[str, str, str]] = {}
        for row in connection.execute(
            """
            SELECT failure_id, failed_at, failure_reason,
                   resolved_at, resolved_by_bar_closed_at, payload_sha256
            FROM trusted_paper_bar_calendar_preflight
            ORDER BY failure_id
            """
        ):
            (
                failure_id,
                failed_at,
                reason,
                legacy_resolved_at,
                legacy_resolved_by,
                payload_sha256,
            ) = row
            payload = self._calendar_preflight_failure_payload(
                failed_at=failed_at,
                reason=reason,
            )
            if (
                legacy_resolved_at is not None
                or legacy_resolved_by is not None
                or not isinstance(payload_sha256, str)
                or self._payload_checksum(payload) != payload_sha256
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_preflight_log_unattested"
                )
            failures[failure_id] = (failed_at, reason, payload_sha256)
        sequence_row = connection.execute(
            """
            SELECT seq FROM sqlite_sequence
            WHERE name = 'trusted_paper_bar_calendar_preflight'
            """
        ).fetchone()
        sequence = 0 if sequence_row is None else sequence_row[0]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or tuple(failures) != tuple(range(1, sequence + 1))
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_preflight_log_sequence_invalid"
            )
        for row in connection.execute(
            """
            SELECT failure_id, resolved_at,
                   resolved_by_bar_closed_at, payload_sha256
            FROM trusted_paper_bar_calendar_preflight_resolution
            ORDER BY failure_id
            """
        ):
            failure_id, resolved_at, resolved_by, payload_sha256 = row
            failure = failures.get(failure_id)
            if failure is None:
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_preflight_resolution_orphaned"
                )
            payload = self._calendar_preflight_resolution_payload(
                failure_id=failure_id,
                failure_payload_sha256=failure[2],
                resolved_at=resolved_at,
                resolved_by_bar_closed_at=resolved_by,
            )
            if (
                not isinstance(payload_sha256, str)
                or self._payload_checksum(payload) != payload_sha256
                or resolved_at < failure[0]
                or resolved_by > resolved_at
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_preflight_resolution_invalid"
                )
        for (closed_at,) in connection.execute(
            """
            SELECT closed_at
            FROM trusted_paper_bar_calendar_preflight_watermark
            ORDER BY closed_at
            """
        ):
            try:
                parsed_closed_at = _parse_datetime(
                    closed_at,
                    "calendar_preflight_watermark_closed_at",
                )
            except TrustedPaperBarIntegrityError as exc:
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_preflight_watermark_invalid"
                ) from exc
            if parsed_closed_at.isoformat() != closed_at:
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_preflight_watermark_invalid"
                )
            self._committed_calendar_preflight_watermark(
                connection,
                closed_at=parsed_closed_at,
            )

    def _validated_observed_trading_days(
        self,
        connection: sqlite3.Connection,
        *,
        attested_v2_cycle_checksums: Mapping[str, str],
    ) -> int:
        completed_slots: dict[tuple[str, str], list[int]] = {}
        cycle_rows = connection.execute(
            """
            SELECT closed_at, trading_day, slot_index,
                   calendar_fingerprint, required_codes_json,
                   optional_codes_json, persisted_codes_json,
                   optional_failures_json, payload_sha256,
                   completed, completed_at
            FROM trusted_paper_bar_cycle
            ORDER BY closed_at
            """
        ).fetchall()
        for row in cycle_rows:
            try:
                payload, _bars = self._decode_cycle_row(
                    connection,
                    row[:9],
                    attested_v2_cycle_checksums=attested_v2_cycle_checksums,
                )
                completed = row[9]
                completed_at = row[10]
                attempt = connection.execute(
                    """
                    SELECT trading_day, slot_index, calendar_fingerprint,
                           status, failure_reason
                    FROM trusted_paper_bar_attempt
                    WHERE closed_at = ?
                    """,
                    (payload["closed_at"],),
                ).fetchone()
                expected_attempt = (
                    payload["trading_day"],
                    payload["slot_index"],
                    payload["calendar_fingerprint"],
                )
                if completed == 1:
                    self._attest_cycle_attempt_state(
                        connection,
                        closed_at=payload["closed_at"],
                        completed=True,
                    )
                if (
                    completed not in (0, 1)
                    or attempt is None
                    or attempt[:3] != expected_attempt
                    or (
                        self._calendar_fingerprint is not None
                        and payload["calendar_fingerprint"]
                        != self._calendar_fingerprint
                    )
                    or (
                        bool(completed)
                        and (
                            attempt[3:] != ("complete", None)
                            or not isinstance(completed_at, str)
                        )
                    )
                    or (
                        not bool(completed)
                        and (
                            completed_at is not None
                            or attempt[3] == "complete"
                            or (
                                attempt[3] == "started"
                                and attempt[4] is not None
                            )
                            or (
                                attempt[3] == "failed"
                                and (
                                    not isinstance(attempt[4], str)
                                    or not attempt[4]
                                )
                            )
                        )
                    )
                ):
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_attestation_invalid"
                    )
            except TrustedPaperBarIntegrityError:
                self._mark_degraded(
                    connection,
                    "paper_bar_cycle_integrity_failure",
                    datetime.now(_CN),
                )
                continue
            if bool(completed) and payload["persisted_codes"]:
                key = (
                    payload["trading_day"],
                    payload["calendar_fingerprint"],
                )
                completed_slots.setdefault(key, []).append(payload["slot_index"])
        expected_slots = tuple(range(48))
        return sum(
            1
            for slots in completed_slots.values()
            if tuple(sorted(slots)) == expected_slots
        )

    @mutation_fenced(
        "trusted_paper_bar_store.record_calendar_preflight_failure"
    )
    def record_calendar_preflight_failure(
        self,
        *,
        failed_at: datetime,
        reason: str,
    ) -> None:
        self._mutation_fence.require()
        normalized_at = normalize_datetime(
            failed_at,
            "failed_at",
        )
        if (
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 255
        ):
            raise ValueError("reason must be a bounded non-empty string")
        payload = self._calendar_preflight_failure_payload(
            failed_at=normalized_at.isoformat(),
            reason=reason,
        )
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO trusted_paper_bar_calendar_preflight (
                        failed_at, failure_reason,
                        resolved_at, resolved_by_bar_closed_at,
                        payload_sha256
                    ) VALUES (?, ?, NULL, NULL, ?)
                    """,
                    (
                        normalized_at.isoformat(),
                        reason,
                        self._payload_checksum(payload),
                    ),
                )
                connection.commit()
        except Exception:
            with self._lock:
                self._activate_preflight_fail_stop(normalized_at)
            raise

    @mutation_fenced("trusted_paper_bar_store.record_cycle")
    def record_cycle(
        self,
        *,
        session: object,
        bar_closed_at: datetime,
        required_codes: Sequence[str],
        optional_codes: Sequence[str],
        bars: Mapping[str, PaperBar],
        optional_failures: Mapping[str, str],
    ) -> tuple[PaperBar, ...]:
        """Atomically persist one audited slot and its membership segments."""

        self._mutation_fence.require()
        if not isinstance(bars, Mapping):
            raise TypeError("bars must be a mapping")
        if not isinstance(optional_failures, Mapping):
            raise TypeError("optional_failures must be a mapping")
        normalized_session, closed_at, slot_index, fingerprint = (
            self._attempt_identity(session, bar_closed_at)
        )
        required = self._validate_code_set(required_codes, "required_codes")
        optional = self._validate_code_set(optional_codes, "optional_codes")
        if set(required) & set(optional):
            raise ValueError("required_codes and optional_codes must be disjoint")
        membership = set(required) | set(optional)
        normalized_bars = dict(bars)
        failures = dict(optional_failures)
        if (
            any(code not in membership for code in normalized_bars)
            or any(code not in optional for code in failures)
            or set(normalized_bars) & set(failures)
            or not set(required).issubset(normalized_bars)
            or set(optional) - set(normalized_bars) != set(failures)
            or any(
                not isinstance(reason, str) or not reason
                for reason in failures.values()
            )
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_bar_cycle_membership_invalid"
            )
        for code, bar in normalized_bars.items():
            if (
                not isinstance(bar, PaperBar)
                or bar.code != code
                or bar.closed_at != closed_at
            ):
                raise TrustedPaperBarIntegrityError(
                    "frozen paper bar binding mismatch"
                )
            _validate_session_bar(bar)
        persisted = tuple(sorted(normalized_bars))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_segment_ledger(connection)
            self._start_cycle_attempt_connection(
                connection,
                normalized_session=normalized_session,
                closed_at=closed_at,
                slot_index=slot_index,
                fingerprint=fingerprint,
                increment_started=False,
            )
            existing_cycle = connection.execute(
                """
                SELECT closed_at, trading_day, slot_index,
                       calendar_fingerprint, required_codes_json,
                       optional_codes_json, persisted_codes_json,
                       optional_failures_json, payload_sha256
                FROM trusted_paper_bar_cycle WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()
            if existing_cycle is not None:
                try:
                    stored_payload, canonical_bars = self._decode_cycle_row(
                        connection,
                        existing_cycle,
                    )
                except TrustedPaperBarIntegrityError:
                    self._mark_degraded(
                        connection,
                        "paper_bar_cycle_integrity_failure",
                        closed_at,
                    )
                    raise
                stored_bindings = stored_payload.get("bar_bindings")
                if (
                    not isinstance(stored_bindings, dict)
                    or set(stored_bindings) != set(persisted)
                ):
                    self._mark_degraded(
                        connection,
                        "paper_bar_cycle_payload_conflict",
                        closed_at,
                    )
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_payload_conflict"
                    )
                replay_bindings = {
                    code: {
                        "bar_id": normalized_bars[code].bar_id,
                        "payload_sha256": _payload_sha256(
                            _bar_json(normalized_bars[code])
                        ),
                        "segment_id": stored_bindings[code]["segment_id"],
                    }
                    for code in persisted
                }
                payload = self._cycle_payload(
                    closed_at=closed_at.isoformat(),
                    trading_day=normalized_session.trading_day.isoformat(),
                    slot_index=slot_index,
                    calendar_fingerprint=fingerprint,
                    required_codes=required,
                    optional_codes=optional,
                    persisted_codes=persisted,
                    optional_failures=failures,
                    bar_bindings=replay_bindings,
                    bar_binding_schema_version=3,
                )
                if stored_payload != payload:
                    self._mark_degraded(
                        connection,
                        "paper_bar_cycle_payload_conflict",
                        closed_at,
                    )
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_payload_conflict"
                    )
                return canonical_bars
            last_cycle = connection.execute(
                "SELECT MAX(closed_at) FROM trusted_paper_bar_cycle"
            ).fetchone()[0]
            if last_cycle is not None and closed_at.isoformat() <= last_cycle:
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_out_of_order"
                )
            stored_fingerprint = connection.execute(
                """
                SELECT calendar_fingerprint FROM trusted_paper_bar_meta
                WHERE singleton_id = 1
                """
            ).fetchone()[0]
            if stored_fingerprint is None:
                connection.execute(
                    """
                    UPDATE trusted_paper_bar_meta SET calendar_fingerprint = ?
                    WHERE singleton_id = 1
                    """,
                    (fingerprint,),
                )
                self._calendar_fingerprint = fingerprint
            elif stored_fingerprint != fingerprint:
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_fingerprint_mismatch"
                )

            for code, bar in normalized_bars.items():
                existing_identity = connection.execute(
                    """
                    SELECT bar_id, code, opened_at, closed_at,
                           payload_json, payload_sha256
                    FROM trusted_paper_bar WHERE bar_id = ?
                    """,
                    (bar.bar_id,),
                ).fetchone()
                if (
                    existing_identity is not None
                    and self._decode_row(connection, existing_identity) != bar
                ):
                    self._mark_degraded(
                        connection,
                        "paper_bar_identity_collision",
                        closed_at,
                    )
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_identity_collision"
                    )
                existing = connection.execute(
                    """
                    SELECT bar_id, code, opened_at, closed_at,
                           payload_json, payload_sha256
                    FROM trusted_paper_bar WHERE code = ? AND closed_at = ?
                    """,
                    (code, closed_at.isoformat()),
                ).fetchone()
                if existing is not None and self._decode_row(connection, existing) != bar:
                    self._mark_degraded(
                        connection,
                        "paper_bar_payload_conflict",
                        closed_at,
                    )
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_payload_conflict"
                    )

            active_rows = {
                row[1]: row
                for row in connection.execute(
                    """
                    SELECT segment_id, code, required
                    FROM trusted_paper_bar_segment WHERE ended_at IS NULL
                    """
                )
            }
            plans: dict[str, tuple[str | None, bool, str]] = {}
            for code in persisted:
                current_required = code in required
                active = active_rows.get(code)
                start_new = active is None
                close_reason: str | None = None
                if active is not None:
                    segment_id, _code, was_required = active
                    latest = connection.execute(
                        """
                        SELECT c.trading_day, c.slot_index
                        FROM trusted_paper_bar_segment_member AS m
                        JOIN trusted_paper_bar_cycle AS c
                          ON c.closed_at = m.closed_at
                        WHERE m.segment_id = ?
                        ORDER BY m.closed_at DESC LIMIT 1
                        """,
                        (segment_id,),
                    ).fetchone()
                    if latest is None:
                        raise TrustedPaperBarIntegrityError(
                            "paper_bar_segment_empty"
                        )
                    requirement_changed = bool(was_required) != current_required
                    adjacent = self._segments_are_adjacent(
                        previous_trading_day=latest[0],
                        previous_slot=latest[1],
                        session=normalized_session,
                        current_slot=slot_index,
                    )
                    if requirement_changed:
                        start_new = True
                        close_reason = "requirement_changed"
                    elif not adjacent and current_required:
                        raise TrustedPaperBarIntegrityError(
                            "required_paper_bar_gap"
                        )
                    elif not adjacent:
                        start_new = True
                        close_reason = "observation_gap"
                planned_segment_id = (
                    sha256_json(
                        {
                            "schema_version": 1,
                            "code": code,
                            "required": current_required,
                            "started_at": closed_at,
                            "calendar_fingerprint": fingerprint,
                        }
                    )
                    if start_new
                    else active[0]
                )
                plans[code] = (
                    close_reason,
                    start_new,
                    planned_segment_id,
                )

            bar_bindings = {
                code: {
                    "bar_id": normalized_bars[code].bar_id,
                    "payload_sha256": _payload_sha256(
                        _bar_json(normalized_bars[code])
                    ),
                    "segment_id": plans[code][2],
                }
                for code in persisted
            }
            payload = self._cycle_payload(
                closed_at=closed_at.isoformat(),
                trading_day=normalized_session.trading_day.isoformat(),
                slot_index=slot_index,
                calendar_fingerprint=fingerprint,
                required_codes=required,
                optional_codes=optional,
                persisted_codes=persisted,
                optional_failures=failures,
                bar_bindings=bar_bindings,
                bar_binding_schema_version=3,
            )
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            checksum = _payload_sha256(encoded)

            for code, active in active_rows.items():
                if code in persisted and plans[code][0] is None:
                    continue
                if code in persisted:
                    reason = plans[code][0]
                elif code in failures:
                    reason = "optional_unavailable"
                elif code not in membership:
                    reason = "membership_removed"
                else:
                    continue
                last_member = connection.execute(
                    """
                    SELECT MAX(closed_at)
                    FROM trusted_paper_bar_segment_member
                    WHERE segment_id = ?
                    """,
                    (active[0],),
                ).fetchone()[0]
                connection.execute(
                    """
                    UPDATE trusted_paper_bar_segment
                    SET ended_at = ?, end_reason = ?
                    WHERE segment_id = ? AND ended_at IS NULL
                    """,
                    (last_member, reason, active[0]),
                )

            for code in persisted:
                close_reason, start_new, segment_id = plans[code]
                active = active_rows.get(code)
                if start_new:
                    connection.execute(
                        """
                        INSERT INTO trusted_paper_bar_segment (
                            segment_id, code, required, started_at,
                            ended_at, end_reason, calendar_fingerprint
                        ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
                        """,
                        (
                            segment_id,
                            code,
                            int(code in required),
                            closed_at.isoformat(),
                            fingerprint,
                        ),
                    )
                else:
                    if active is None or close_reason is not None:
                        raise TrustedPaperBarIntegrityError(
                            "paper_bar_segment_state_invalid"
                        )
                    if segment_id != active[0]:
                        raise TrustedPaperBarIntegrityError(
                            "paper_bar_segment_state_invalid"
                        )
                bar = normalized_bars[code]
                payload_json = _bar_json(bar)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO trusted_paper_bar (
                        bar_id, code, opened_at, closed_at,
                        payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bar.bar_id,
                        bar.code,
                        bar.opened_at.isoformat(),
                        bar.closed_at.isoformat(),
                        payload_json,
                        _payload_sha256(payload_json),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO trusted_paper_bar_segment_member (
                        bar_id, segment_id, closed_at, required
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        bar.bar_id,
                        segment_id,
                        closed_at.isoformat(),
                        int(code in required),
                    ),
                )
            connection.execute(
                """
                INSERT INTO trusted_paper_bar_cycle (
                    closed_at, trading_day, slot_index,
                    calendar_fingerprint, required_codes_json,
                    optional_codes_json, persisted_codes_json,
                    optional_failures_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    closed_at.isoformat(),
                    normalized_session.trading_day.isoformat(),
                    slot_index,
                    fingerprint,
                    json.dumps(required, separators=(",", ":")),
                    json.dumps(optional, separators=(",", ":")),
                    json.dumps(persisted, separators=(",", ":")),
                    json.dumps(
                        failures,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    checksum,
                ),
            )
            self._validate_segment_ledger(connection)
            connection.commit()
        return tuple(normalized_bars[code] for code in persisted)

    @mutation_fenced("trusted_paper_bar_store.complete_cycle")
    def complete_cycle(
        self,
        bar_closed_at: datetime,
        *,
        calendar_observed_at: datetime | None = None,
        signal_observation_batch: PreparedSignalObservationBatch | None = None,
        exit_commitments: Sequence[ExitEvaluationCommitment] | None = None,
    ) -> None:
        self._mutation_fence.require()
        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        observed_at = (
            None
            if calendar_observed_at is None
            else normalize_datetime(
                calendar_observed_at,
                "calendar_observed_at",
            )
        )
        if observed_at is not None and observed_at < closed_at:
            raise ValueError("calendar_observed_at must not precede bar_closed_at")
        if signal_observation_batch is not None and not isinstance(
            signal_observation_batch,
            PreparedSignalObservationBatch,
        ):
            raise TypeError(
                "signal_observation_batch must be PreparedSignalObservationBatch"
            )
        normalized_exit_commitments = (
            None
            if exit_commitments is None
            else self._normalize_exit_commitments(exit_commitments)
        )
        with self._lock, self._connect() as connection:
            if normalized_exit_commitments is None:
                if self._strategy_run_binding is not None:
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_manifest_required"
                    )
                normalized_exit_commitments = ()
            if any(
                commitment.evaluated_at != closed_at
                for commitment in normalized_exit_commitments
            ):
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_cycle_mismatch"
                )
            connection.execute("BEGIN IMMEDIATE")
            self._validate_segment_ledger(connection)
            row = connection.execute(
                """
                SELECT completed, payload_sha256 FROM trusted_paper_bar_cycle
                WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()
            if row is None:
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_missing"
                )
            self._attest_cycle_attempt_state(
                connection,
                closed_at=closed_at.isoformat(),
                completed=bool(row[0]),
                expected_generation=(
                    None
                    if signal_observation_batch is None
                    else signal_observation_batch.attempt_generation
                ),
            )
            if not bool(row[0]):
                cycle_attestation_row = connection.execute(
                    """
                    SELECT closed_at, trading_day, slot_index,
                           calendar_fingerprint, required_codes_json,
                           optional_codes_json, persisted_codes_json,
                           optional_failures_json, payload_sha256
                    FROM trusted_paper_bar_cycle WHERE closed_at = ?
                    """,
                    (closed_at.isoformat(),),
                ).fetchone()
                if cycle_attestation_row is None:
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_missing"
                    )
                self._decode_cycle_row(connection, cycle_attestation_row)
            if signal_observation_batch is None:
                observation_count = connection.execute(
                    "SELECT COUNT(*) FROM trusted_signal_observation_cycle"
                ).fetchone()[0]
                if (
                    self._signal_observation_binding is not None
                    or observation_count
                ):
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_batch_required"
                    )
                if bool(row[0]):
                    self._attest_committed_exit_manifest(
                        connection,
                        closed_at=closed_at,
                        commitments=normalized_exit_commitments,
                    )
                    self._validate_calendar_preflight_log(connection)
                    committed_watermark = (
                        self._committed_calendar_preflight_watermark(
                            connection,
                            closed_at=closed_at,
                        )
                    )
                    connection.rollback()
                    if (
                        observed_at is not None
                        and committed_watermark is not None
                    ):
                        self._clear_preflight_fail_stop(
                            observed_at=committed_watermark
                        )
                    return
            else:
                binding = self._require_signal_observation_binding()
                expected_binding = (
                    signal_observation_batch.run_id,
                    signal_observation_batch.epoch,
                    signal_observation_batch.strategy_run_fingerprint,
                    signal_observation_batch.identity_sha256,
                    signal_observation_batch.store_instance_id,
                )
                if expected_binding != binding:
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_batch_strategy_mismatch"
                    )
                if signal_observation_batch.bar_closed_at != closed_at:
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_batch_cycle_mismatch"
                    )
                if bool(row[0]):
                    self._attest_committed_signal_observation_batch(
                        connection,
                        closed_at=closed_at,
                        batch=signal_observation_batch,
                        binding=binding,
                    )
                    self._attest_committed_exit_manifest(
                        connection,
                        closed_at=closed_at,
                        commitments=normalized_exit_commitments,
                    )
                    self._validate_calendar_preflight_log(connection)
                    committed_watermark = (
                        self._committed_calendar_preflight_watermark(
                            connection,
                            closed_at=closed_at,
                        )
                    )
                    connection.rollback()
                    if (
                        observed_at is not None
                        and committed_watermark is not None
                    ):
                        self._clear_preflight_fail_stop(
                            observed_at=committed_watermark
                        )
                    return
                reprepared = self._prepare_signal_observation_batch_connection(
                    connection,
                    closed_at=closed_at,
                    manifests=dict(signal_observation_batch.manifests),
                    binding=binding,
                )
                if reprepared != signal_observation_batch:
                    raise TrustedPaperBarIntegrityError(
                        "signal_observation_batch_resolution_changed"
                    )
                state = connection.execute(
                    """
                    SELECT event_count, max_sequence, history_head_sha256
                    FROM trusted_signal_observation_log_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                previous_sha256 = state[2]
                observation_sequence = int(state[1]) + 1
                segment_payload_sha256: dict[str, dict[str, str]] = {}
                new_segment_rows: dict[
                    tuple[str, str], tuple[dict[str, object], str, str]
                ] = {}
                for code, signal_fingerprints in (
                    signal_observation_batch.manifests.items()
                ):
                    segment_payload_sha256[code] = {}
                    segment_id = signal_observation_batch.segment_ids[code]
                    for signal_fingerprint in signal_fingerprints:
                        existing_segment = connection.execute(
                            """
                            SELECT payload_sha256
                            FROM trusted_signal_segment_observation
                            WHERE run_id = ? AND segment_id = ? AND code = ?
                              AND signal_fingerprint = ?
                            """,
                            (
                                binding[0],
                                segment_id,
                                code,
                                signal_fingerprint,
                            ),
                        ).fetchone()
                        if existing_segment is not None:
                            segment_payload_sha256[code][signal_fingerprint] = (
                                existing_segment[0]
                            )
                            continue
                        prepared_state = signal_observation_batch.states[code][
                            signal_fingerprint
                        ]
                        segment_first_at = (
                            signal_observation_batch.first_observed_at[code].get(
                                signal_fingerprint
                            )
                        )
                        segment_payload = (
                            self._signal_observation_segment_payload(
                                run_id=binding[0],
                                epoch=binding[1],
                                strategy_run_fingerprint=binding[2],
                                identity_sha256=binding[3],
                                store_instance_id=binding[4],
                                segment_id=segment_id,
                                code=code,
                                signal_fingerprint=signal_fingerprint,
                                first_observed_at=(
                                    None
                                    if segment_first_at is None
                                    else segment_first_at.isoformat()
                                ),
                                first_cycle_closed_at=closed_at.isoformat(),
                                observation_state=prepared_state,
                                observation_sequence=observation_sequence,
                            )
                        )
                        segment_payload_json = json.dumps(
                            segment_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        segment_checksum = sha256_json(segment_payload)
                        segment_payload_sha256[code][signal_fingerprint] = (
                            segment_checksum
                        )
                        new_segment_rows[(code, signal_fingerprint)] = (
                            segment_payload,
                            segment_payload_json,
                            segment_checksum,
                        )
                observation_payload = self._signal_observation_cycle_payload(
                    binding=binding,
                    closed_at=closed_at.isoformat(),
                    manifests=signal_observation_batch.manifests,
                    segment_ids=signal_observation_batch.segment_ids,
                    states=signal_observation_batch.states,
                    first_observed_at=(
                        signal_observation_batch.first_observed_at
                    ),
                    segment_payload_sha256=segment_payload_sha256,
                    attempt_generation=(
                        signal_observation_batch.attempt_generation
                    ),
                    prior_attempt_ambiguous=(
                        signal_observation_batch.prior_attempt_ambiguous
                    ),
                    resolution_sha256=signal_observation_batch.resolution_sha256,
                    previous_manifest_sha256=previous_sha256,
                )
                observation_payload_json = json.dumps(
                    observation_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                observation_payload_sha256 = sha256_json(observation_payload)
                connection.execute(
                    """
                    INSERT INTO trusted_signal_observation_cycle (
                        observation_sequence, run_id, epoch,
                        strategy_run_fingerprint,
                        identity_sha256, store_instance_id, closed_at,
                        previous_manifest_sha256, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_sequence,
                        binding[0],
                        binding[1],
                        binding[2],
                        binding[3],
                        binding[4],
                        closed_at.isoformat(),
                        previous_sha256,
                        observation_payload_json,
                        observation_payload_sha256,
                    ),
                )
                for code, signal_fingerprints in (
                    signal_observation_batch.manifests.items()
                ):
                    for signal_fingerprint in signal_fingerprints:
                        prepared_state = signal_observation_batch.states[code][
                            signal_fingerprint
                        ]
                        segment_first_at = (
                            signal_observation_batch.first_observed_at[code].get(
                                signal_fingerprint
                            )
                        )
                        segment_id = signal_observation_batch.segment_ids[code]
                        new_segment = new_segment_rows.get(
                            (code, signal_fingerprint)
                        )
                        if new_segment is not None:
                            (
                                _segment_payload,
                                segment_payload_json,
                                segment_checksum,
                            ) = new_segment
                            connection.execute(
                                """
                                INSERT INTO trusted_signal_segment_observation (
                                    run_id, epoch, strategy_run_fingerprint,
                                    identity_sha256, store_instance_id,
                                    segment_id, code, signal_fingerprint,
                                    first_observed_at, first_cycle_closed_at,
                                    observation_state, observation_sequence,
                                    payload_json, payload_sha256
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    binding[0],
                                    binding[1],
                                    binding[2],
                                    binding[3],
                                    binding[4],
                                    segment_id,
                                    code,
                                    signal_fingerprint,
                                    (
                                        None
                                        if segment_first_at is None
                                        else segment_first_at.isoformat()
                                    ),
                                    closed_at.isoformat(),
                                    prepared_state,
                                    observation_sequence,
                                    segment_payload_json,
                                    segment_checksum,
                                ),
                            )
                        existing_first = connection.execute(
                            """
                            SELECT 1 FROM trusted_signal_first_observation
                            WHERE run_id = ? AND code = ?
                              AND signal_fingerprint = ?
                            """,
                            (binding[0], code, signal_fingerprint),
                        ).fetchone()
                        if existing_first is not None:
                            continue
                        persisted_state = (
                            "trusted_first_seen"
                            if prepared_state == "trusted_first_seen"
                            else "baseline_not_fresh"
                        )
                        first_at = signal_observation_batch.first_observed_at[
                            code
                        ].get(signal_fingerprint, closed_at)
                        first_payload = self._signal_observation_first_payload(
                            run_id=binding[0],
                            epoch=binding[1],
                            strategy_run_fingerprint=binding[2],
                            identity_sha256=binding[3],
                            store_instance_id=binding[4],
                            code=code,
                            signal_fingerprint=signal_fingerprint,
                            first_observed_at=first_at.isoformat(),
                            first_cycle_closed_at=closed_at.isoformat(),
                            first_segment_id=(
                                signal_observation_batch.segment_ids[code]
                            ),
                            observation_state=persisted_state,
                            observation_sequence=observation_sequence,
                        )
                        first_payload_json = json.dumps(
                            first_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        connection.execute(
                            """
                            INSERT INTO trusted_signal_first_observation (
                                run_id, epoch, strategy_run_fingerprint,
                                identity_sha256, store_instance_id, code,
                                signal_fingerprint, first_observed_at,
                                first_cycle_closed_at, first_segment_id,
                                observation_state, observation_sequence,
                                payload_json, payload_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                binding[0],
                                binding[1],
                                binding[2],
                                binding[3],
                                binding[4],
                                code,
                                signal_fingerprint,
                                first_at.isoformat(),
                                closed_at.isoformat(),
                                signal_observation_batch.segment_ids[code],
                                persisted_state,
                                observation_sequence,
                                first_payload_json,
                                sha256_json(first_payload),
                            ),
                        )
                event_count = int(state[0]) + 1
                max_sequence = observation_sequence
                log_state_payload = self._signal_observation_log_state_payload(
                    event_count=event_count,
                    max_sequence=max_sequence,
                    history_head_sha256=observation_payload_sha256,
                )
                connection.execute(
                    """
                    UPDATE trusted_signal_observation_log_state
                    SET event_count = ?, max_sequence = ?,
                        history_head_sha256 = ?, payload_sha256 = ?
                    WHERE singleton_id = 1
                    """,
                    (
                        event_count,
                        max_sequence,
                        observation_payload_sha256,
                        sha256_json(log_state_payload),
                    ),
                )
            if signal_observation_batch is None:
                observation_payload_sha256 = None
                observation_resolution_sha256 = None
            else:
                observation_resolution_sha256 = (
                    signal_observation_batch.resolution_sha256
                )
            self._attest_exit_manifest_append_prefix(connection)
            exit_manifest_state = connection.execute(
                """
                SELECT event_count, max_sequence, history_head_sha256
                FROM trusted_paper_exit_manifest_log_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            if exit_manifest_state is None:
                raise TrustedPaperBarIntegrityError(
                    "paper_exit_manifest_log_state_invalid"
                )
            manifest_sequence = int(exit_manifest_state[1]) + 1
            previous_manifest_sha256 = exit_manifest_state[2]
            exit_manifest_payload = self._exit_manifest_payload(
                manifest_sequence=manifest_sequence,
                previous_manifest_sha256=previous_manifest_sha256,
                closed_at=closed_at.isoformat(),
                strategy_run_binding=self._strategy_run_binding,
                bar_cycle_payload_sha256=row[1],
                signal_observation_payload_sha256=(
                    observation_payload_sha256
                ),
                signal_observation_resolution_sha256=(
                    observation_resolution_sha256
                ),
                commitments=normalized_exit_commitments,
            )
            exit_manifest_json = json.dumps(
                exit_manifest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            exit_manifest_sha256 = _payload_sha256(exit_manifest_json)
            connection.execute(
                """
                INSERT INTO trusted_paper_exit_manifest (
                    closed_at, manifest_sequence,
                    previous_manifest_sha256,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    closed_at.isoformat(),
                    manifest_sequence,
                    previous_manifest_sha256,
                    exit_manifest_json,
                    exit_manifest_sha256,
                ),
            )
            exit_manifest_event_count = int(exit_manifest_state[0]) + 1
            exit_manifest_log_state = self._exit_manifest_log_state_payload(
                event_count=exit_manifest_event_count,
                max_sequence=manifest_sequence,
                history_head_sha256=exit_manifest_sha256,
            )
            exit_manifest_log_state_checksum = sha256_json(
                exit_manifest_log_state
            )
            connection.execute(
                """
                UPDATE trusted_paper_exit_manifest_log_state
                SET event_count = ?, max_sequence = ?,
                    history_head_sha256 = ?, payload_sha256 = ?
                WHERE singleton_id = 1
                """,
                (
                    exit_manifest_event_count,
                    manifest_sequence,
                    exit_manifest_sha256,
                    exit_manifest_log_state_checksum,
                ),
            )
            if not bool(row[0]):
                connection.execute(
                    """
                    UPDATE trusted_paper_bar_cycle
                    SET completed = 1, completed_at = ?
                    WHERE closed_at = ?
                    """,
                    (datetime.now(_CN).isoformat(), closed_at.isoformat()),
                )
                if observed_at is not None:
                    watermark_payload = (
                        self._calendar_preflight_watermark_payload(
                            closed_at=closed_at.isoformat(),
                            observed_at=observed_at.isoformat(),
                            cycle_payload_sha256=row[1],
                        )
                    )
                    connection.execute(
                        """
                        INSERT INTO
                        trusted_paper_bar_calendar_preflight_watermark (
                            closed_at, observed_at,
                            cycle_payload_sha256, payload_sha256
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            closed_at.isoformat(),
                            observed_at.isoformat(),
                            row[1],
                            self._payload_checksum(watermark_payload),
                        ),
                    )
            connection.execute(
                """
                UPDATE trusted_paper_bar_attempt
                SET status = 'complete', failure_reason = NULL, updated_at = ?
                WHERE closed_at = ?
                """,
                (datetime.now(_CN).isoformat(), closed_at.isoformat()),
            )
            if observed_at is not None:
                unresolved = connection.execute(
                    """
                    SELECT f.failure_id, f.payload_sha256
                    FROM trusted_paper_bar_calendar_preflight AS f
                    LEFT JOIN trusted_paper_bar_calendar_preflight_resolution AS r
                      ON r.failure_id = f.failure_id
                    WHERE r.failure_id IS NULL AND f.failed_at <= ?
                    ORDER BY f.failure_id
                    """,
                    (observed_at.isoformat(),),
                ).fetchall()
                for failure_id, failure_payload_sha256 in unresolved:
                    resolution_payload = (
                        self._calendar_preflight_resolution_payload(
                            failure_id=failure_id,
                            failure_payload_sha256=failure_payload_sha256,
                            resolved_at=observed_at.isoformat(),
                            resolved_by_bar_closed_at=closed_at.isoformat(),
                        )
                    )
                    connection.execute(
                        """
                        INSERT INTO
                        trusted_paper_bar_calendar_preflight_resolution (
                            failure_id, resolved_at,
                            resolved_by_bar_closed_at, payload_sha256
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            failure_id,
                            observed_at.isoformat(),
                            closed_at.isoformat(),
                            self._payload_checksum(resolution_payload),
                        ),
                    )
            if signal_observation_batch is not None:
                self._write_signal_observation_anchor(
                    observation_sequence=observation_sequence,
                    run_id=binding[0],
                    closed_at=closed_at.isoformat(),
                    history_head_sha256=observation_payload_sha256,
                )
            self._write_exit_manifest_anchor(
                manifest_sequence=manifest_sequence,
                closed_at=closed_at.isoformat(),
                history_head_sha256=exit_manifest_sha256,
            )
            connection.commit()
            self._exit_manifest_validated_prefix = (
                _ExitManifestValidatedPrefix(
                    event_count=exit_manifest_event_count,
                    max_sequence=manifest_sequence,
                    history_head_sha256=exit_manifest_sha256,
                    log_state_payload_sha256=(
                        exit_manifest_log_state_checksum
                    ),
                    tail_closed_at=closed_at.isoformat(),
                    tail_previous_manifest_sha256=(
                        previous_manifest_sha256
                    ),
                    tail_payload_sha256=exit_manifest_sha256,
                )
            )
            if observed_at is not None and not bool(row[0]):
                self._clear_preflight_fail_stop(observed_at=observed_at)

    @mutation_fenced("trusted_paper_bar_store.put")
    def put(self, bar: PaperBar) -> PaperBar:
        self._mutation_fence.require()
        if not isinstance(bar, PaperBar):
            raise TypeError("bar must be PaperBar")
        _validate_session_bar(bar)
        payload_json = _bar_json(bar)
        payload_sha256 = _payload_sha256(payload_json)
        with self._lock, self._connect() as connection:
            existing_id = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar WHERE bar_id = ?
                """,
                (bar.bar_id,),
            ).fetchone()
            if existing_id is not None:
                existing = self._decode_row(connection, existing_id)
                if existing != bar:
                    self._mark_degraded(
                        connection,
                        "paper_bar_payload_conflict",
                        bar.closed_at,
                    )
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_payload_conflict"
                    )
                return existing
            existing_close = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar
                WHERE code = ? AND closed_at = ?
                """,
                (bar.code, bar.closed_at.isoformat()),
            ).fetchone()
            if existing_close is not None:
                self._decode_row(connection, existing_close)
                self._mark_degraded(
                    connection,
                    "paper_bar_payload_conflict",
                    bar.closed_at,
                )
                raise TrustedPaperBarIntegrityError("paper_bar_payload_conflict")
            latest_row = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar
                WHERE code = ? ORDER BY closed_at DESC LIMIT 1
                """,
                (bar.code,),
            ).fetchone()
            if latest_row is not None:
                latest = self._decode_row(connection, latest_row)
                if bar.closed_at <= latest.closed_at:
                    raise TrustedPaperBarIntegrityError("paper_bar_out_of_order")
                expected = _expected_next_close(latest.closed_at, bar.closed_at)
                if expected != bar.closed_at:
                    raise TrustedPaperBarIntegrityError("paper_bar_gap_detected")
            connection.execute(
                """
                INSERT INTO trusted_paper_bar (
                    bar_id, code, opened_at, closed_at,
                    payload_json, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bar.bar_id,
                    bar.code,
                    bar.opened_at.isoformat(),
                    bar.closed_at.isoformat(),
                    payload_json,
                    payload_sha256,
                ),
            )
            connection.commit()
        return bar

    def get_bar(self, bar_id: str) -> PaperBar | None:
        if not isinstance(bar_id, str) or not bar_id:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar WHERE bar_id = ?
                """,
                (bar_id,),
            ).fetchone()
            return None if row is None else self._decode_row(connection, row)

    def is_cycle_complete(self, bar_closed_at: datetime) -> bool:
        """Attest whether one exact trusted-bar cycle is publishable."""

        closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT closed_at, trading_day, slot_index,
                       calendar_fingerprint, required_codes_json,
                       optional_codes_json, persisted_codes_json,
                       optional_failures_json, payload_sha256,
                       completed, completed_at
                FROM trusted_paper_bar_cycle WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()
            if row is None:
                return False
            try:
                self._validate_signal_observation_log(connection)
                self._validate_exit_manifest_log(connection)
                payload, _bars = self._decode_cycle_row(connection, row[:9])
                completed = row[9]
                completed_at = row[10]
                attempt = connection.execute(
                    """
                    SELECT trading_day, slot_index, calendar_fingerprint,
                           status, failure_reason
                    FROM trusted_paper_bar_attempt WHERE closed_at = ?
                    """,
                    (closed_at.isoformat(),),
                ).fetchone()
                expected_attempt = (
                    payload["trading_day"],
                    payload["slot_index"],
                    payload["calendar_fingerprint"],
                )
                if completed == 1:
                    self._attest_cycle_attempt_state(
                        connection,
                        closed_at=closed_at.isoformat(),
                        completed=True,
                    )
                if (
                    payload["closed_at"] != closed_at.isoformat()
                    or completed not in (0, 1)
                    or attempt is None
                    or attempt[:3] != expected_attempt
                    or (
                        self._calendar_fingerprint is not None
                        and payload["calendar_fingerprint"]
                        != self._calendar_fingerprint
                    )
                    or (
                        bool(completed)
                        and (
                            not isinstance(completed_at, str)
                            or _parse_datetime(
                                completed_at,
                                "completed_at",
                            )
                            < closed_at
                        )
                    )
                    or (
                        not bool(completed)
                        and (
                            completed_at is not None
                            or attempt[3] == "complete"
                            or (
                                attempt[3] == "started"
                                and attempt[4] is not None
                            )
                            or (
                                attempt[3] == "failed"
                                and (
                                    not isinstance(attempt[4], str)
                                    or not attempt[4]
                                )
                            )
                        )
                    )
                ):
                    raise TrustedPaperBarIntegrityError(
                        "paper_bar_cycle_attestation_invalid"
                    )
                if self._signal_observation_binding is not None:
                    observation = connection.execute(
                        """
                        SELECT run_id, epoch, strategy_run_fingerprint,
                               identity_sha256, store_instance_id
                        FROM trusted_signal_observation_cycle
                        WHERE closed_at = ?
                        """,
                        (closed_at.isoformat(),),
                    ).fetchone()
                    if observation != self._signal_observation_binding:
                        raise TrustedPaperBarIntegrityError(
                            "signal_observation_cycle_attestation_missing"
                        )
            except TrustedPaperBarIntegrityError as exc:
                reason = str(exc)
                if not reason.startswith("signal_observation_"):
                    reason = "paper_bar_cycle_attestation_invalid"
                self._mark_degraded(
                    connection,
                    reason,
                    datetime.now(_CN),
                )
                return False
            return bool(completed)

    def attest_exit_snapshots(
        self,
        snapshots: Sequence[ExitEvaluationSnapshot],
    ) -> tuple[bool, ...]:
        """Bulk-attest exact snapshot membership with one global validation."""

        if isinstance(snapshots, (str, bytes)) or not isinstance(
            snapshots,
            Sequence,
        ):
            raise TypeError("snapshots must be a sequence")
        normalized = tuple(snapshots)
        if any(
            not isinstance(snapshot, ExitEvaluationSnapshot)
            for snapshot in normalized
        ):
            raise TypeError("snapshots must contain ExitEvaluationSnapshot")
        if not normalized:
            return ()
        expected = tuple(
            ExitEvaluationCommitment.from_snapshot(snapshot)
            for snapshot in normalized
        )
        requested_closes = {
            commitment.evaluated_at.isoformat() for commitment in expected
        }
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN")
                self._validate_signal_observation_log(connection)
                self._validate_exit_manifest_log(connection)
                manifest_memberships: dict[
                    str,
                    frozenset[ExitEvaluationCommitment],
                ] = {}
                rows = connection.execute(
                    """
                    SELECT manifest_sequence, closed_at,
                           previous_manifest_sha256,
                           payload_json, payload_sha256
                    FROM trusted_paper_exit_manifest
                    ORDER BY manifest_sequence
                    """
                ).fetchall()
                for row in rows:
                    if row[1] in requested_closes:
                        manifest_memberships[row[1]] = frozenset(
                            self._decode_exit_manifest_row(connection, row)
                        )
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
                return (False,) * len(normalized)
        return tuple(
            commitment
            in manifest_memberships.get(
                commitment.evaluated_at.isoformat(),
                frozenset(),
            )
            for commitment in expected
        )

    def is_exit_snapshot_committed(
        self,
        snapshot: ExitEvaluationSnapshot,
    ) -> bool:
        """Attest one snapshot through the bulk manifest-membership path."""

        return self.attest_exit_snapshots((snapshot,))[0]

    def attest_cycle_bar(
        self,
        bar_id: str,
        *,
        allow_current_started: bool = False,
    ) -> PaperBar | None:
        if not isinstance(bar_id, str) or not bar_id:
            return None
        if not isinstance(allow_current_started, bool):
            raise TypeError("allow_current_started must be bool")
        with self._lock, self._connect() as connection:
            self._validate_segment_ledger(connection)
            bar_row = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar WHERE bar_id = ?
                """,
                (bar_id,),
            ).fetchone()
            if bar_row is None:
                return None
            bar = self._decode_row(connection, bar_row)
            cycle_row = connection.execute(
                """
                SELECT closed_at, trading_day, slot_index,
                       calendar_fingerprint, required_codes_json,
                       optional_codes_json, persisted_codes_json,
                       optional_failures_json, payload_sha256
                FROM trusted_paper_bar_cycle WHERE closed_at = ?
                """,
                (bar.closed_at.isoformat(),),
            ).fetchone()
            cycle_state = connection.execute(
                """
                SELECT completed FROM trusted_paper_bar_cycle
                WHERE closed_at = ?
                """,
                (bar.closed_at.isoformat(),),
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT trading_day, slot_index, calendar_fingerprint, status
                FROM trusted_paper_bar_attempt WHERE closed_at = ?
                """,
                (bar.closed_at.isoformat(),),
            ).fetchone()
            if cycle_row is None or cycle_state is None or attempt is None:
                self._mark_degraded(
                    connection,
                    "paper_bar_unbound_from_v2_cycle",
                    bar.closed_at,
                )
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_attestation_missing"
                )
            try:
                payload, canonical_bars = self._decode_cycle_row(
                    connection,
                    cycle_row,
                )
            except TrustedPaperBarIntegrityError:
                self._mark_degraded(
                    connection,
                    "paper_bar_cycle_attestation_invalid",
                    bar.closed_at,
                )
                raise
            canonical = next(
                (item for item in canonical_bars if item.bar_id == bar_id),
                None,
            )
            expected_attempt = (
                payload["trading_day"],
                payload["slot_index"],
                payload["calendar_fingerprint"],
            )
            completed = bool(cycle_state[0])
            status = attempt[3]
            status_allowed = (
                completed and status == "complete"
            ) or (
                allow_current_started
                and not completed
                and status == "started"
            )
            if (
                canonical is None
                or canonical != bar
                or attempt[:3] != expected_attempt
            ):
                self._mark_degraded(
                    connection,
                    "paper_bar_cycle_attestation_invalid",
                    bar.closed_at,
                )
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_attestation_invalid"
                )
            if not status_allowed:
                raise TrustedPaperBarIntegrityError(
                    "paper_bar_cycle_attestation_status_invalid"
                )
            return canonical

    def get_for_code_at(self, code: str, closed_at: datetime) -> PaperBar | None:
        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        closed_at = normalize_datetime(closed_at, "closed_at")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT bar_id, code, opened_at, closed_at,
                       payload_json, payload_sha256
                FROM trusted_paper_bar
                WHERE code = ? AND closed_at = ?
                """,
                (code, closed_at.isoformat()),
            ).fetchone()
            return None if row is None else self._decode_row(connection, row)

    def health(self) -> TrustedPaperBarStoreHealth:
        with self._lock, self._connect() as connection:
            self._load_preflight_fail_stop()
            try:
                self._validate_calendar_preflight_log(connection)
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
            try:
                self._validate_signal_observation_log(connection)
            except TrustedPaperBarIntegrityError as exc:
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
            try:
                attested_v2_cycle_checksums = (
                    self._validate_exit_manifest_log(connection)
                )
            except TrustedPaperBarIntegrityError as exc:
                attested_v2_cycle_checksums = {}
                self._mark_degraded(
                    connection,
                    str(exc),
                    datetime.now(_CN),
                )
            if self._has_unbound_bars(connection):
                self._mark_degraded(
                    connection,
                    "paper_bar_unbound_from_v2_cycle",
                    datetime.now(_CN),
                )
            count, last = connection.execute(
                """
                SELECT COUNT(*), MAX(closed_at)
                FROM trusted_paper_bar
                """
            ).fetchone()
            days = self._validated_observed_trading_days(
                connection,
                attested_v2_cycle_checksums=attested_v2_cycle_checksums,
            )
            degraded, reason = connection.execute(
                """
                SELECT degraded, degraded_reason
                FROM trusted_paper_bar_health WHERE singleton_id = 1
                """
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT closed_at, status, failure_reason
                FROM trusted_paper_bar_attempt
                ORDER BY closed_at DESC LIMIT 1
                """
            ).fetchone()
            calendar_preflight = connection.execute(
                """
                SELECT f.failed_at, f.failure_reason
                FROM trusted_paper_bar_calendar_preflight AS f
                LEFT JOIN trusted_paper_bar_calendar_preflight_resolution AS r
                  ON r.failure_id = f.failure_id
                WHERE r.failure_id IS NULL
                ORDER BY f.failed_at DESC, f.failure_id DESC LIMIT 1
                """
            ).fetchone()
        effective_degraded = (
            bool(degraded) or self._process_fail_stop_reason is not None
        )
        effective_reason = reason or self._process_fail_stop_reason
        effective_preflight = calendar_preflight
        if (
            effective_preflight is None
            and self._process_fail_stop_reason is not None
            and self._process_fail_stop_at is not None
        ):
            effective_preflight = (
                self._process_fail_stop_at.isoformat(),
                self._process_fail_stop_reason,
            )
        return TrustedPaperBarStoreHealth(
            bar_count=int(count),
            observed_trading_days=int(days),
            degraded=effective_degraded,
            degraded_reason=effective_reason,
            last_bar_closed_at=(
                None if last is None else _parse_datetime(last, "last_bar_closed_at")
            ),
            last_attempted_bar_closed_at=(
                None
                if attempt is None
                else _parse_datetime(
                    attempt[0],
                    "last_attempted_bar_closed_at",
                )
            ),
            last_attempt_complete=(
                None if attempt is None else attempt[1] == "complete"
            ),
            last_attempt_failure=(
                None if attempt is None else attempt[2]
            ),
            calendar_preflight_failure_at=(
                None
                if effective_preflight is None
                else _parse_datetime(
                    effective_preflight[0],
                    "calendar_preflight_failure_at",
                )
            ),
            calendar_preflight_failure=(
                None
                if effective_preflight is None
                else effective_preflight[1]
            ),
        )


class SQLitePaperRiskState:
    """Persist paper-equity day/high-water state and sticky risk latches."""

    _FIELDS = {
        "schema_version",
        "revision",
        "risk_policy_fingerprint",
        "asof",
        "trading_day",
        "account_equity",
        "day_start_equity",
        "high_water_equity",
        "daily_loss_locked",
        "drawdown_locked",
    }

    def __init__(self, path: str | Path, *, policy: RiskPolicy) -> None:
        if not isinstance(policy, RiskPolicy):
            raise TypeError("policy must be RiskPolicy")
        self._path = Path(path).expanduser().absolute()
        self._policy = policy
        self._policy_fingerprint = sha256_json(policy)
        self._lock = RLock()
        self._mutation_fence = MutationLeaseGuard()
        if self._path.exists() and not self._path.is_file():
            raise ValueError("paper risk state path must be a file")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def policy_fingerprint(self) -> str:
        return self._policy_fingerprint

    def bind_strategy_run(self, strategy_run: object) -> None:
        bindings = getattr(strategy_run, "store_bindings", {})
        binding = bindings.get("risk") if isinstance(bindings, Mapping) else None
        self._mutation_fence.bind(
            strategy_run,
            expected_store_role="risk",
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
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_risk_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL,
                    risk_policy_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                """
                SELECT revision, risk_policy_fingerprint,
                       payload_json, payload_sha256
                FROM paper_risk_state WHERE singleton_id = 1
                """
            ).fetchone()
            if row is not None:
                self._parse_row(row)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_exit_coverage (
                    bar_closed_at TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                """
                SELECT bar_closed_at, payload_json, payload_sha256
                FROM paper_exit_coverage ORDER BY bar_closed_at
                """
            )
            for coverage_row in rows:
                self._coverage_from_row(coverage_row)

    def _parse_row(
        self,
        row: tuple[object, ...],
    ) -> tuple[dict[str, object], PaperRiskMark]:
        revision, policy_fingerprint, payload_json, payload_sha256 = row
        if policy_fingerprint != self._policy_fingerprint:
            raise TrustedPaperBarIntegrityError("paper_risk_policy_mismatch")
        if not isinstance(payload_json, str) or not isinstance(payload_sha256, str):
            raise TrustedPaperBarIntegrityError("paper_risk_state_invalid")
        if _payload_sha256(payload_json) != payload_sha256:
            raise TrustedPaperBarIntegrityError("paper_risk_state_checksum_mismatch")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise TrustedPaperBarIntegrityError("paper_risk_state_invalid") from exc
        if not isinstance(payload, dict) or set(payload) != self._FIELDS:
            raise TrustedPaperBarIntegrityError("paper_risk_state_invalid")
        if (
            payload["schema_version"] != 1
            or payload["revision"] != revision
            or payload["risk_policy_fingerprint"] != policy_fingerprint
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
        ):
            raise TrustedPaperBarIntegrityError("paper_risk_state_identity_mismatch")
        for field_name in ("daily_loss_locked", "drawdown_locked"):
            if type(payload[field_name]) is not bool:
                raise TrustedPaperBarIntegrityError("paper_risk_state_invalid")
        asof = _parse_datetime(payload["asof"], "paper_risk_asof")
        if payload["trading_day"] != asof.date().isoformat():
            raise TrustedPaperBarIntegrityError("paper_risk_trading_day_mismatch")
        account_equity = _parse_decimal(
            payload["account_equity"],
            "paper_risk_account_equity",
        )
        day_start = _parse_decimal(
            payload["day_start_equity"],
            "paper_risk_day_start_equity",
        )
        high_water = _parse_decimal(
            payload["high_water_equity"],
            "paper_risk_high_water_equity",
        )
        if high_water < account_equity:
            raise TrustedPaperBarIntegrityError("paper_risk_high_water_invalid")
        drawdown = (high_water - account_equity) / high_water
        mark = PaperRiskMark(
            revision=revision,
            asof=asof,
            account_equity=account_equity,
            day_start_equity=day_start,
            high_water_equity=high_water,
            day_pnl=account_equity - day_start,
            strategy_drawdown=drawdown,
            daily_loss_locked=payload["daily_loss_locked"],
            drawdown_locked=payload["drawdown_locked"],
        )
        return payload, mark

    @staticmethod
    def _json(payload: Mapping[str, object]) -> str:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @mutation_fenced("paper_risk_state.mark")
    def mark(self, account_equity: Decimal, asof: datetime) -> PaperRiskMark:
        self._mutation_fence.require()
        if (
            not isinstance(account_equity, Decimal)
            or not account_equity.is_finite()
            or account_equity <= 0
        ):
            raise ValueError("account_equity must be a positive Decimal")
        asof = normalize_datetime(asof, "asof")
        with self._lock, self._connect() as connection:
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
                    revision = 1
                    day_start = account_equity
                    high_water = account_equity
                    daily_loss_locked = False
                    drawdown_locked = False
                else:
                    _payload, previous = self._parse_row(row)
                    if asof < previous.asof:
                        raise TrustedPaperBarIntegrityError(
                            "paper_risk_time_moved_backwards"
                        )
                    if asof == previous.asof:
                        if account_equity != previous.account_equity:
                            raise TrustedPaperBarIntegrityError(
                                "paper_risk_same_time_equity_conflict"
                            )
                        connection.commit()
                        return previous
                    revision = previous.revision + 1
                    new_day = asof.date() != previous.asof.date()
                    day_start = (
                        account_equity if new_day else previous.day_start_equity
                    )
                    high_water = max(previous.high_water_equity, account_equity)
                    daily_loss_locked = (
                        False if new_day else previous.daily_loss_locked
                    )
                    drawdown_locked = previous.drawdown_locked
                day_pnl = account_equity - day_start
                drawdown = (high_water - account_equity) / high_water
                daily_loss_locked = daily_loss_locked or (
                    day_pnl
                    <= -(day_start * self._policy.daily_loss_fraction)
                )
                drawdown_locked = drawdown_locked or (
                    drawdown >= self._policy.max_drawdown_fraction
                )
                payload = {
                    "schema_version": 1,
                    "revision": revision,
                    "risk_policy_fingerprint": self._policy_fingerprint,
                    "asof": asof.isoformat(),
                    "trading_day": asof.date().isoformat(),
                    "account_equity": _decimal_text(account_equity),
                    "day_start_equity": _decimal_text(day_start),
                    "high_water_equity": _decimal_text(high_water),
                    "daily_loss_locked": daily_loss_locked,
                    "drawdown_locked": drawdown_locked,
                }
                payload_json = self._json(payload)
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO paper_risk_state (
                            singleton_id, revision, risk_policy_fingerprint,
                            payload_json, payload_sha256
                        ) VALUES (1, ?, ?, ?, ?)
                        """,
                        (
                            revision,
                            self._policy_fingerprint,
                            payload_json,
                            _payload_sha256(payload_json),
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE paper_risk_state
                        SET revision = ?, payload_json = ?, payload_sha256 = ?
                        WHERE singleton_id = 1 AND revision = ?
                        """,
                        (
                            revision,
                            payload_json,
                            _payload_sha256(payload_json),
                            revision - 1,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TrustedPaperBarIntegrityError(
                            "paper_risk_revision_conflict"
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PaperRiskMark(
            revision=revision,
            asof=asof,
            account_equity=account_equity,
            day_start_equity=day_start,
            high_water_equity=high_water,
            day_pnl=day_pnl,
            strategy_drawdown=drawdown,
            daily_loss_locked=daily_loss_locked,
            drawdown_locked=drawdown_locked,
        )

    @staticmethod
    def _coverage_payload(coverage: PaperExitCoverage) -> dict[str, object]:
        return {
            "schema_version": 1,
            "bar_closed_at": coverage.bar_closed_at.isoformat(),
            "open_entry_ids": list(coverage.open_entry_ids),
            "snapshot_entry_ids": list(coverage.snapshot_entry_ids),
            "failures": dict(coverage.failures),
            "scan_code": coverage.scan_code,
            "cycle_failure": coverage.cycle_failure,
        }

    @classmethod
    def _coverage_json(cls, coverage: PaperExitCoverage) -> str:
        return json.dumps(
            cls._coverage_payload(coverage),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def _coverage_from_row(
        cls,
        row: tuple[object, ...],
    ) -> PaperExitCoverage:
        bar_closed_at, payload_json, payload_sha256 = row
        if not isinstance(payload_json, str) or not isinstance(payload_sha256, str):
            raise TrustedPaperBarIntegrityError("paper_exit_coverage_invalid")
        if _payload_sha256(payload_json) != payload_sha256:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_coverage_checksum_mismatch"
            )
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_coverage_invalid"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "bar_closed_at",
            "open_entry_ids",
            "snapshot_entry_ids",
            "failures",
            "scan_code",
            "cycle_failure",
        }:
            raise TrustedPaperBarIntegrityError("paper_exit_coverage_invalid")
        try:
            coverage = PaperExitCoverage(
                bar_closed_at=_parse_datetime(
                    payload["bar_closed_at"],
                    "paper_exit_coverage_bar",
                ),
                open_entry_ids=tuple(payload["open_entry_ids"]),
                snapshot_entry_ids=tuple(payload["snapshot_entry_ids"]),
                failures=payload["failures"],
                scan_code=payload["scan_code"],
                cycle_failure=payload["cycle_failure"],
            )
        except (TypeError, ValueError) as exc:
            raise TrustedPaperBarIntegrityError(
                "paper_exit_coverage_invalid"
            ) from exc
        if (
            payload["schema_version"] != 1
            or not isinstance(bar_closed_at, str)
            or coverage.bar_closed_at.isoformat() != bar_closed_at
            or cls._coverage_json(coverage) != payload_json
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_exit_coverage_identity_mismatch"
            )
        return coverage

    @mutation_fenced("paper_risk_state.record_exit_coverage")
    def record_exit_coverage(
        self,
        *,
        bar_closed_at: datetime,
        open_entry_ids: tuple[str, ...],
        snapshot_entry_ids: tuple[str, ...],
        failures: Mapping[str, str],
        cycle_failure: str | None = None,
    ) -> PaperExitCoverage:
        self._mutation_fence.require()
        coverage = PaperExitCoverage(
            bar_closed_at=bar_closed_at,
            open_entry_ids=open_entry_ids,
            snapshot_entry_ids=snapshot_entry_ids,
            failures=failures,
            cycle_failure=cycle_failure,
        )
        payload_json = self._coverage_json(coverage)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT bar_closed_at, payload_json, payload_sha256
                    FROM paper_exit_coverage WHERE bar_closed_at = ?
                    """,
                    (coverage.bar_closed_at.isoformat(),),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO paper_exit_coverage (
                            bar_closed_at, payload_json, payload_sha256
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            coverage.bar_closed_at.isoformat(),
                            payload_json,
                            _payload_sha256(payload_json),
                        ),
                    )
                elif self._coverage_from_row(row) != coverage:
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_coverage_conflict"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return coverage

    @mutation_fenced("paper_risk_state.record_exit_scan_outcome")
    def record_exit_scan_outcome(
        self,
        bar_closed_at: datetime,
        scan_code: str,
    ) -> PaperExitCoverage:
        self._mutation_fence.require()
        bar_closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        if not isinstance(scan_code, str) or not scan_code or len(scan_code) > 255:
            raise ValueError("scan_code must be a bounded non-empty string")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT bar_closed_at, payload_json, payload_sha256
                    FROM paper_exit_coverage WHERE bar_closed_at = ?
                    """,
                    (bar_closed_at.isoformat(),),
                ).fetchone()
                if row is None:
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_coverage_missing"
                    )
                current = self._coverage_from_row(row)
                if current.scan_code is not None and current.scan_code != scan_code:
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_scan_outcome_conflict"
                    )
                updated = PaperExitCoverage(
                    bar_closed_at=current.bar_closed_at,
                    open_entry_ids=current.open_entry_ids,
                    snapshot_entry_ids=current.snapshot_entry_ids,
                    failures=current.failures,
                    scan_code=scan_code,
                    cycle_failure=current.cycle_failure,
                )
                payload_json = self._coverage_json(updated)
                connection.execute(
                    """
                    UPDATE paper_exit_coverage
                    SET payload_json = ?, payload_sha256 = ?
                    WHERE bar_closed_at = ?
                    """,
                    (
                        payload_json,
                        _payload_sha256(payload_json),
                        bar_closed_at.isoformat(),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return updated

    def latest_exit_coverage(self) -> PaperExitCoverage | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT bar_closed_at, payload_json, payload_sha256
                FROM paper_exit_coverage ORDER BY bar_closed_at DESC LIMIT 1
                """
            ).fetchone()
            return None if row is None else self._coverage_from_row(row)


class PaperExitAnalysisCycle:
    """Build exit requests only from authoritative paper-ledger positions."""

    def __init__(
        self,
        *,
        ledger: object,
        data_provider: object,
        entry_resolver: object,
        risk_state: SQLitePaperRiskState,
        exit_service: ExitEvaluationService,
        strategy_run: object | None = None,
    ) -> None:
        if not callable(getattr(ledger, "load", None)) or not callable(
            getattr(ledger, "account_snapshot", None)
        ):
            raise TypeError("ledger must provide load and account_snapshot")
        if not callable(getattr(data_provider, "structure_for_code", None)) or not callable(
            getattr(data_provider, "quote_for_code", None)
        ):
            raise TypeError("data_provider must expose position structure and quote")
        if not callable(getattr(entry_resolver, "resolve", None)):
            raise TypeError("entry_resolver must provide resolve")
        if not isinstance(risk_state, SQLitePaperRiskState):
            raise TypeError("risk_state must be SQLitePaperRiskState")
        if not isinstance(exit_service, ExitEvaluationService):
            raise TypeError("exit_service must be ExitEvaluationService")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "status_payload", None)
        ):
            raise TypeError("strategy_run must provide status_payload")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "mutation_lease", None)
        ):
            raise TypeError("strategy_run must provide mutation_lease")
        self.ledger = ledger
        self.data_provider = data_provider
        self.entry_resolver = entry_resolver
        self.risk_state = risk_state
        self.exit_service = exit_service
        self._strategy_run = strategy_run

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
            raise TrustedPaperBarIntegrityError(
                "paper_strategy_run_not_active"
            )

    def _strategy_mutation_lease(self, operation: str):
        if self._strategy_run is None:
            return nullcontext()
        lease_provider = getattr(self._strategy_run, "mutation_lease", None)
        if not callable(lease_provider):
            raise TrustedPaperBarIntegrityError(
                "paper_strategy_run_mutation_lease_unavailable"
            )
        return lease_provider(operation)

    def latest_coverage(self) -> PaperExitCoverage | None:
        return self.risk_state.latest_exit_coverage()

    def record_scan_outcome(
        self,
        bar_closed_at: datetime,
        scan_code: str,
    ) -> PaperExitCoverage:
        with self._strategy_mutation_lease(
            "paper_exit.record_scan_outcome"
        ):
            return self._record_scan_outcome_under_mutation_lease(
                bar_closed_at,
                scan_code,
            )

    def _record_scan_outcome_under_mutation_lease(
        self,
        bar_closed_at: datetime,
        scan_code: str,
    ) -> PaperExitCoverage:
        self._require_active_strategy_run()
        return self.risk_state.record_exit_scan_outcome(bar_closed_at, scan_code)

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, str) or not reason:
            reason = str(exc) or type(exc).__name__
        if len(reason) > 255:
            reason = type(exc).__name__
        return reason

    @staticmethod
    def _validate_batch_outcomes(
        expected_cycles: Mapping[str, str],
        batch: object,
    ) -> tuple[tuple[str, ...], dict[str, str], str | None]:
        snapshots: dict[str, object] = {}
        failures: dict[str, str] = {}
        invalid_batch = False
        for snapshot in tuple(getattr(batch, "snapshots")):
            entry_event_id = getattr(snapshot, "entry_event_id", None)
            cycle_id = getattr(snapshot, "evaluation_cycle_id", None)
            expected_cycle = expected_cycles.get(entry_event_id)
            if expected_cycle is None:
                invalid_batch = True
                continue
            if cycle_id != expected_cycle:
                failures[entry_event_id] = "exit_snapshot_cycle_mismatch"
                continue
            if entry_event_id in snapshots:
                failures[entry_event_id] = "exit_snapshot_duplicate"
                snapshots.pop(entry_event_id, None)
                continue
            snapshots[entry_event_id] = snapshot
        for failure in tuple(getattr(batch, "failures")):
            entry_event_id = getattr(failure, "entry_event_id", None)
            cycle_id = getattr(failure, "evaluation_cycle_id", None)
            expected_cycle = expected_cycles.get(entry_event_id)
            if expected_cycle is None or cycle_id != expected_cycle:
                invalid_batch = True
                continue
            snapshots.pop(entry_event_id, None)
            reason = getattr(failure, "reason", None)
            failures[entry_event_id] = (
                reason
                if isinstance(reason, str) and 0 < len(reason) <= 255
                else "exit_evaluation_failure_invalid"
            )
        cycle_failure = None
        if invalid_batch:
            cycle_failure = "exit_evaluation_unexpected_outcome"
            for entry_event_id in expected_cycles:
                snapshots.pop(entry_event_id, None)
                failures[entry_event_id] = cycle_failure
        for entry_event_id in expected_cycles:
            if entry_event_id not in snapshots and entry_event_id not in failures:
                failures[entry_event_id] = "exit_snapshot_missing"
        return tuple(sorted(snapshots)), failures, cycle_failure

    @staticmethod
    def _holding(
        code: str,
        lots: tuple[object, ...],
        bar_closed_at: datetime,
    ) -> HoldingSnapshot:
        shares = sum(int(getattr(lot, "shares")) for lot in lots)
        if shares <= 0:
            raise TrustedPaperBarIntegrityError("paper_position_has_no_shares")
        average_price = sum(
            Decimal(getattr(lot, "price")) * int(getattr(lot, "shares"))
            for lot in lots
        ) / shares
        opened_at = min(
            normalize_datetime(getattr(lot, "opened_at"), "lot.opened_at")
            for lot in lots
        )
        trading_day = bar_closed_at.astimezone(_CN).date()
        sellable = sum(
            int(getattr(lot, "shares"))
            for lot in lots
            if normalize_datetime(
                getattr(lot, "opened_at"),
                "lot.opened_at",
            ).astimezone(_CN).date()
            < trading_day
        )
        return HoldingSnapshot(
            code=code,
            shares=shares,
            sellable_shares=sellable,
            opened_at=opened_at,
            average_price=average_price,
        )

    @staticmethod
    def _pending_exits(
        intents: tuple[object, ...],
    ) -> tuple[PendingExitSnapshot, ...]:
        pending: list[PendingExitSnapshot] = []
        for intent in intents:
            if (
                getattr(intent, "side", None) != "sell"
                or int(getattr(intent, "remaining_shares", 0)) <= 0
                or str(getattr(intent, "status", "")).startswith("cancelled_")
            ):
                continue
            status = str(getattr(intent, "status", "pending"))
            pending.append(
                PendingExitSnapshot(
                    code=str(getattr(intent, "code")),
                    shares=int(getattr(intent, "remaining_shares")),
                    reason=str(getattr(intent, "reason", status)),
                    blocked_by_t1="t1" in status.casefold(),
                    blocked_by_limit="limit" in status.casefold(),
                )
            )
        if len({item.code for item in pending}) != len(pending):
            raise TrustedPaperBarIntegrityError(
                "duplicate_pending_paper_exit"
            )
        return tuple(sorted(pending, key=lambda item: item.code))

    def __call__(self, bar_closed_at: datetime) -> PaperExitCycleResult:
        with self._strategy_mutation_lease("paper_exit.analysis_cycle"):
            return self._call_under_mutation_lease(bar_closed_at)

    def _call_under_mutation_lease(
        self,
        bar_closed_at: datetime,
    ) -> PaperExitCycleResult:
        self._require_active_strategy_run()
        bar_closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        state = self.ledger.load()
        lots_by_entry: dict[str, list[object]] = {}
        for lot in tuple(getattr(state, "lots", ())):
            entry_event_id = getattr(lot, "entry_event_id", None)
            code = getattr(lot, "code", None)
            if not isinstance(entry_event_id, str) or not entry_event_id:
                raise TrustedPaperBarIntegrityError(
                    "paper_lot_entry_identity_invalid"
                )
            if not isinstance(code, str) or not code:
                raise TrustedPaperBarIntegrityError("paper_lot_code_invalid")
            lots_by_entry.setdefault(entry_event_id, []).append(lot)
        open_entry_ids = tuple(sorted(lots_by_entry))
        snapshot_entry_ids: tuple[str, ...] = ()
        commitments: tuple[ExitEvaluationCommitment, ...] = ()
        failures: dict[str, str] = {}
        cycle_failure: str | None = None
        try:
            holdings_by_entry: dict[str, HoldingSnapshot] = {}
            holdings_by_code: dict[str, HoldingSnapshot] = {}
            quotes: dict[str, QuoteSnapshot] = {}
            for entry_event_id, raw_lots in sorted(lots_by_entry.items()):
                lots = tuple(raw_lots)
                codes = {str(getattr(lot, "code")) for lot in lots}
                if len(codes) != 1:
                    raise TrustedPaperBarIntegrityError(
                        "paper_entry_lot_code_conflict"
                    )
                code = next(iter(codes))
                if code in holdings_by_code:
                    raise TrustedPaperBarIntegrityError(
                        "multiple_paper_entries_for_code"
                    )
                holding = self._holding(code, lots, bar_closed_at)
                quote = self.data_provider.quote_for_code(code, bar_closed_at)
                if not isinstance(quote, QuoteSnapshot):
                    raise TypeError("quote_for_code must return QuoteSnapshot")
                holdings_by_entry[entry_event_id] = holding
                holdings_by_code[code] = holding
                quotes[code] = quote

            account = self.ledger.account_snapshot()
            cash = getattr(account, "cash_balance", None)
            available = getattr(account, "available_buying_power", None)
            if not isinstance(cash, Decimal) or not isinstance(available, Decimal):
                raise TypeError("paper account snapshot is invalid")
            equity = cash + sum(
                quotes[code].price * holding.shares
                for code, holding in holdings_by_code.items()
            )
            mark = self.risk_state.mark(equity, bar_closed_at)
            holdings = tuple(
                holdings_by_code[code] for code in sorted(holdings_by_code)
            )
            pending_exits = self._pending_exits(
                tuple(getattr(state, "intents", ()))
            )
            contexts = {
                code: RiskContext(
                    account_equity=mark.account_equity,
                    day_start_equity=mark.day_start_equity,
                    available_cash=available,
                    holdings=holdings,
                    pending_exits=pending_exits,
                    day_pnl=mark.day_pnl,
                    strategy_drawdown=mark.strategy_drawdown,
                    daily_loss_locked=mark.daily_loss_locked,
                    drawdown_locked=mark.drawdown_locked,
                    quote=quote,
                    asof=bar_closed_at,
                )
                for code, quote in quotes.items()
            }

            requests: list[ExitEvaluationRequest] = []
            expected_cycles: dict[str, str] = {}
            for entry_event_id, holding in sorted(holdings_by_entry.items()):
                try:
                    link = self.entry_resolver.resolve(entry_event_id, holding)
                    if link is None:
                        raise TrustedPaperBarIntegrityError(
                            "authoritative_entry_link_missing"
                        )
                    if getattr(link.position, "entry_event_id", None) != entry_event_id:
                        raise TrustedPaperBarIntegrityError(
                            "authoritative_entry_identity_mismatch"
                        )
                    structure = self.data_provider.structure_for_code(
                        holding.code,
                        bar_closed_at,
                    )
                    cycle_id = getattr(structure, "current_cycle_id", None)
                    if not isinstance(cycle_id, str) or not cycle_id:
                        raise TrustedPaperBarIntegrityError(
                            "exit_evaluation_cycle_missing"
                        )
                    request = ExitEvaluationRequest(
                        position=link.position,
                        entry_event=link.entry_event,
                        structure=structure,
                        risk_context=contexts[holding.code],
                        bar_closed_at=bar_closed_at,
                        evaluation_cycle_id=cycle_id,
                    )
                    requests.append(request)
                    expected_cycles[entry_event_id] = cycle_id
                except Exception as exc:
                    failures[entry_event_id] = self._failure_reason(exc)

            try:
                batch = self.exit_service.evaluate_and_persist_many(tuple(requests))
            except Exception as exc:
                reason = self._failure_reason(exc)
                cycle_failure = reason
                for entry_event_id in expected_cycles:
                    failures[entry_event_id] = reason
            else:
                (
                    snapshot_entry_ids,
                    batch_failures,
                    batch_cycle_failure,
                ) = self._validate_batch_outcomes(expected_cycles, batch)
                failures.update(batch_failures)
                cycle_failure = batch_cycle_failure
                valid_entry_ids = set(snapshot_entry_ids)
                commitments = tuple(
                    sorted(
                        ExitEvaluationCommitment.from_snapshot(snapshot)
                        for snapshot in tuple(getattr(batch, "snapshots"))
                        if getattr(snapshot, "entry_event_id", None)
                        in valid_entry_ids
                    )
                )
                if len(commitments) != len(snapshot_entry_ids):
                    raise TrustedPaperBarIntegrityError(
                        "exit_snapshot_commitment_count_mismatch"
                    )
            if self.ledger.load() != state:
                raise TrustedPaperBarIntegrityError(
                    "paper_ledger_changed_during_exit_cycle"
                )
        except Exception as exc:
            reason = self._failure_reason(exc)
            cycle_failure = reason
            snapshot_entry_ids = ()
            commitments = ()
            failures = {entry_event_id: reason for entry_event_id in open_entry_ids}

        coverage = self.risk_state.record_exit_coverage(
            bar_closed_at=bar_closed_at,
            open_entry_ids=open_entry_ids,
            snapshot_entry_ids=snapshot_entry_ids,
            failures=failures,
            cycle_failure=cycle_failure,
        )
        return PaperExitCycleResult(
            bar_closed_at=bar_closed_at,
            evaluated_count=len(coverage.snapshot_entry_ids),
            failures=coverage.failures,
            cycle_failure=coverage.cycle_failure,
            commitments=commitments,
        )


class PaperResearchRuntime:
    """Single-lock coordinator for paper bars, fills, exits, and analysis.

    It consumes immutable EventStore authorizations through the supplied
    trusted admission gateway.  It never creates broker orders or live trades.
    """

    def __init__(
        self,
        *,
        data_provider: object,
        analysis_runtime: object,
        bar_store: SQLiteTrustedPaperBarStore,
        paper_gateway: object,
        event_store: object,
        trading_calendar: object,
        exit_cycle: Callable[[datetime], object] | None = None,
        admitted_event_ids_provider: Callable[[], object] | None = None,
        event_eligibility_provider: Callable[[object], bool] | None = None,
        strategy_run: object | None = None,
    ) -> None:
        if not isinstance(bar_store, SQLiteTrustedPaperBarStore):
            raise TypeError("bar_store must be SQLiteTrustedPaperBarStore")
        if exit_cycle is not None and not callable(exit_cycle):
            raise TypeError("exit_cycle must be callable")
        if admitted_event_ids_provider is not None and not callable(
            admitted_event_ids_provider
        ):
            raise TypeError("admitted_event_ids_provider must be callable")
        if event_eligibility_provider is not None and not callable(
            event_eligibility_provider
        ):
            raise TypeError("event_eligibility_provider must be callable")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "status_payload", None)
        ):
            raise TypeError("strategy_run must provide status_payload")
        if strategy_run is not None and not callable(
            getattr(strategy_run, "mutation_lease", None)
        ):
            raise TypeError("strategy_run must provide mutation_lease")
        calendar_fingerprint = getattr(trading_calendar, "fingerprint", None)
        session_for = getattr(trading_calendar, "session_for", None)
        if not _valid_sha256_fingerprint(calendar_fingerprint) or not callable(
            session_for
        ):
            raise TypeError(
                "trading_calendar must expose fingerprint and session_for"
            )
        if (
            bar_store.calendar_fingerprint is not None
            and bar_store.calendar_fingerprint != calendar_fingerprint
        ):
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_fingerprint_mismatch"
            )
        self.data_provider = data_provider
        self.analysis_runtime = analysis_runtime
        self.bar_store = bar_store
        self.paper_gateway = paper_gateway
        self.event_store = event_store
        self.trading_calendar = trading_calendar
        self._calendar_fingerprint = calendar_fingerprint
        self._calendar_session_for = session_for
        self._exit_cycle = exit_cycle
        self._admitted_event_ids_provider = admitted_event_ids_provider
        self._event_eligibility_provider = event_eligibility_provider
        self._strategy_run = strategy_run
        self._admitted_events: set[str] = set()
        self._lock = RLock()
        self._bar_cycles = 0
        self._bar_cycle_failures = 0
        self._admission_cycles = 0
        self._admission_failures = 0
        self._last_bar_error: str | None = None
        self._last_admission_error: str | None = None

    def _closed_bar(
        self,
        asof: datetime,
    ) -> tuple[PaperTradingSession | None, datetime | None]:
        normalized = normalize_datetime(asof, "asof")
        trading_day = normalized.astimezone(_CN).date()
        raw_session = self._calendar_session_for(trading_day)
        if raw_session is None:
            return None, None
        session = _validated_trading_session(
            raw_session,
            calendar_fingerprint=self._calendar_fingerprint,
        )
        if session.trading_day != trading_day:
            raise TrustedPaperBarIntegrityError(
                "paper_calendar_trading_day_mismatch"
            )
        closed = normalized.replace(second=0, microsecond=0)
        if closed not in session.expected_bar_closes:
            return session, None
        return session, closed

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
            raise RuntimeError("paper strategy-run is not active")

    def _strategy_mutation_lease(self, operation: str):
        if self._strategy_run is None:
            return nullcontext()
        lease_provider = getattr(self._strategy_run, "mutation_lease", None)
        if not callable(lease_provider):
            raise RuntimeError("paper strategy-run mutation lease is unavailable")
        return lease_provider(operation)

    def _known_admitted_events(self) -> set[str]:
        values: object = self._admitted_events
        if self._admitted_event_ids_provider is not None:
            values = self._admitted_event_ids_provider()
        if isinstance(values, (str, bytes)):
            raise TypeError("admitted event ids must be an iterable of strings")
        try:
            result = set(values)
        except TypeError as exc:
            raise TypeError(
                "admitted event ids must be an iterable of strings"
            ) from exc
        if any(not isinstance(value, str) or not value for value in result):
            raise ValueError("admitted event ids must be non-empty strings")
        result.update(self._admitted_events)
        return result

    def bar_cycle(self, asof: datetime) -> PaperBarCycleResult:
        with self._strategy_mutation_lease("paper_runtime.bar_cycle"):
            return self._bar_cycle_under_mutation_lease(asof)

    def _bar_cycle_under_mutation_lease(
        self,
        asof: datetime,
    ) -> PaperBarCycleResult:
        self._require_active_strategy_run()
        occurred_at = normalize_datetime(asof, "asof")
        with self._lock:
            try:
                try:
                    session, bar_closed_at = self._closed_bar(occurred_at)
                except Exception as exc:
                    failure_reason = str(exc).strip()
                    if not failure_reason or len(failure_reason) > 255:
                        failure_reason = type(exc).__name__
                    self.bar_store.record_calendar_preflight_failure(
                        failed_at=occurred_at,
                        reason=failure_reason,
                    )
                    raise
                if bar_closed_at is None:
                    return PaperBarCycleResult(
                        "bar_not_closed",
                        occurred_at,
                        None,
                    )
                if session is None:
                    raise TrustedPaperBarIntegrityError(
                        "paper_calendar_session_missing"
                    )
                self.bar_store.start_cycle_attempt(
                    session=session,
                    bar_closed_at=bar_closed_at,
                )
                bar_health = self.bar_store.health()
                recovery_fail_stop = (
                    bar_health.degraded
                    and bar_health.degraded_reason
                    == "paper_calendar_preflight_persistence_failed"
                )
                if bar_health.degraded and not recovery_fail_stop:
                    raise TrustedPaperBarIntegrityError(
                        "trusted_paper_bar_store_degraded"
                    )
                universe_provider = getattr(
                    self.data_provider,
                    "universe_provider",
                    None,
                )
                paper_bar_provider = getattr(
                    self.data_provider,
                    "paper_bar",
                    None,
                )
                required_codes_provider = getattr(
                    self.data_provider,
                    "required_codes",
                    None,
                )
                process_bar = getattr(self.paper_gateway, "process_bar", None)
                scan_cycle = getattr(self.analysis_runtime, "scan_cycle", None)
                if not all(
                    callable(value)
                    for value in (
                        universe_provider,
                        paper_bar_provider,
                        required_codes_provider,
                        process_bar,
                        scan_cycle,
                    )
                ):
                    raise TypeError("paper bar cycle dependencies are incomplete")
                universe = universe_provider(bar_closed_at)
                securities = getattr(universe, "securities", None)
                if isinstance(securities, (str, bytes)) or securities is None:
                    raise TypeError("frozen universe securities are unavailable")
                codes = tuple(sorted({getattr(item, "code", None) for item in securities}))
                if any(not isinstance(code, str) or not code for code in codes):
                    raise TypeError("frozen universe contains an invalid code")
                required_codes = self.bar_store._validate_code_set(
                    required_codes_provider(bar_closed_at),
                    "required_codes",
                )
                if not set(required_codes).issubset(codes):
                    raise TrustedPaperBarIntegrityError(
                        "required_pinned_code_missing_from_universe"
                    )
                optional_codes = tuple(
                    code for code in codes if code not in set(required_codes)
                )
                failures_provider = getattr(self.data_provider, "failures", None)
                provider_failures = (
                    dict(failures_provider(bar_closed_at))
                    if callable(failures_provider)
                    else {}
                )
                if any(code not in codes for code in provider_failures):
                    raise TrustedPaperBarIntegrityError(
                        "frozen_provider_failure_identity_invalid"
                    )
                if set(required_codes) & set(provider_failures):
                    raise TrustedPaperBarIntegrityError(
                        "required_paper_bar_unavailable"
                    )
                bars_by_code: dict[str, PaperBar] = {}
                optional_failures: dict[str, str] = {}
                for code in codes:
                    if code in provider_failures:
                        reason = provider_failures[code]
                        optional_failures[code] = (
                            reason
                            if isinstance(reason, str) and reason
                            else "provider_failure"
                        )
                        continue
                    try:
                        bar = paper_bar_provider(code, bar_closed_at)
                    except Exception as exc:
                        if code in required_codes:
                            raise TrustedPaperBarIntegrityError(
                                "required_paper_bar_unavailable"
                            ) from exc
                        optional_failures[code] = type(exc).__name__
                        continue
                    if (
                        not isinstance(bar, PaperBar)
                        or bar.code != code
                        or bar.closed_at != bar_closed_at
                    ):
                        if code in required_codes:
                            raise TrustedPaperBarIntegrityError(
                                "required_paper_bar_binding_mismatch"
                            )
                        optional_failures[code] = "bar_binding_mismatch"
                        continue
                    bars_by_code[code] = bar
                bars = self.bar_store.record_cycle(
                    session=session,
                    bar_closed_at=bar_closed_at,
                    required_codes=required_codes,
                    optional_codes=optional_codes,
                    bars=bars_by_code,
                    optional_failures=optional_failures,
                )
                prepare_signal_observations = getattr(
                    self.data_provider,
                    "prepare_signal_observation_cycle",
                    None,
                )
                signal_observation_batch_provider = getattr(
                    self.data_provider,
                    "signal_observation_batch",
                    None,
                )
                if callable(prepare_signal_observations) != callable(
                    signal_observation_batch_provider
                ):
                    raise TypeError(
                        "signal observation cycle dependencies are incomplete"
                    )
                prepared_signal_observation_batch = (
                    prepare_signal_observations(bar_closed_at)
                    if callable(prepare_signal_observations)
                    else None
                )
                fills = tuple(
                    fill
                    for bar in bars
                    for fill in tuple(process_bar(bar))
                )
                if self._exit_cycle is None:
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_cycle_missing"
                    )
                exit_result = self._exit_cycle(bar_closed_at)
                if (
                    not isinstance(exit_result, PaperExitCycleResult)
                    or exit_result.bar_closed_at != bar_closed_at
                    or any(
                        not isinstance(commitment, ExitEvaluationCommitment)
                        or commitment.evaluated_at != bar_closed_at
                        for commitment in exit_result.commitments
                    )
                ):
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_cycle_result_invalid"
                    )
                latest_coverage = getattr(
                    self._exit_cycle,
                    "latest_coverage",
                    None,
                )
                if callable(latest_coverage):
                    coverage = latest_coverage()
                    if (
                        not isinstance(coverage, PaperExitCoverage)
                        or coverage.bar_closed_at != bar_closed_at
                        or not coverage.complete
                        or len(coverage.snapshot_entry_ids)
                        != exit_result.evaluated_count
                        or dict(coverage.failures) != dict(exit_result.failures)
                        or coverage.cycle_failure != exit_result.cycle_failure
                    ):
                        raise TrustedPaperBarIntegrityError(
                            "paper_exit_coverage_invalid"
                        )
                record_scan_outcome = getattr(
                    self._exit_cycle,
                    "record_scan_outcome",
                    None,
                )
                if exit_result.failures or exit_result.cycle_failure is not None:
                    if callable(record_scan_outcome):
                        record_scan_outcome(
                            bar_closed_at,
                            "scan_skipped_exit_failure",
                        )
                    raise TrustedPaperBarIntegrityError(
                        "paper_exit_coverage_failure"
                    )
                try:
                    scan_result = scan_cycle(occurred_at)
                except Exception as exc:
                    if callable(record_scan_outcome):
                        record_scan_outcome(
                            bar_closed_at,
                            f"scan_exception:{type(exc).__name__}",
                        )
                    raise
                scan_code = getattr(scan_result, "code", None)
                if not isinstance(scan_code, str) or not scan_code:
                    scan_code = "scan_result_invalid"
                if getattr(scan_result, "bar_closed_at", None) != bar_closed_at:
                    scan_code = "scan_bar_binding_mismatch"
                queue_overflow = getattr(scan_result, "queue_overflow", 0)
                if (
                    isinstance(queue_overflow, bool)
                    or not isinstance(queue_overflow, int)
                    or queue_overflow != 0
                ):
                    scan_code = "scan_review_queue_overflow"
                if callable(record_scan_outcome):
                    record_scan_outcome(bar_closed_at, scan_code)
                if scan_code != "scan_complete":
                    raise TrustedPaperBarIntegrityError(
                        "paper_scan_not_complete"
                    )
                complete_cycle_arguments: dict[str, object] = {
                    "calendar_observed_at": occurred_at,
                    "exit_commitments": exit_result.commitments,
                }
                if callable(signal_observation_batch_provider):
                    final_signal_observation_batch = (
                        signal_observation_batch_provider(bar_closed_at)
                    )
                    if (
                        final_signal_observation_batch
                        is not prepared_signal_observation_batch
                    ):
                        raise TrustedPaperBarIntegrityError(
                            "signal_observation_batch_identity_changed"
                        )
                    complete_cycle_arguments["signal_observation_batch"] = (
                        final_signal_observation_batch
                    )
                self.bar_store.complete_cycle(
                    bar_closed_at,
                    **complete_cycle_arguments,
                )
            except Exception as exc:
                failure_name = type(exc).__name__
                attempted_at = locals().get("bar_closed_at")
                if isinstance(attempted_at, datetime):
                    try:
                        self.bar_store.fail_cycle_attempt(
                            attempted_at,
                            failure_name,
                        )
                    except Exception:
                        failure_name = (
                            failure_name + ":attempt_failure_not_persisted"
                        )
                self._bar_cycles += 1
                self._bar_cycle_failures += 1
                self._last_bar_error = failure_name
                return PaperBarCycleResult(
                    "bar_cycle_failed",
                    occurred_at,
                    locals().get("bar_closed_at"),
                    detail=type(exc).__name__,
                )
            self._bar_cycles += 1
            self._last_bar_error = None
            return PaperBarCycleResult(
                "bar_cycle_complete",
                occurred_at,
                bar_closed_at,
                persisted_bar_count=len(bars),
                fill_count=len(fills),
            )

    def admission_cycle(self, asof: datetime) -> PaperAdmissionCycleResult:
        with self._strategy_mutation_lease("paper_runtime.admission_cycle"):
            return self._admission_cycle_under_mutation_lease(asof)

    def _admission_cycle_under_mutation_lease(
        self,
        asof: datetime,
    ) -> PaperAdmissionCycleResult:
        self._require_active_strategy_run()
        occurred_at = normalize_datetime(asof, "asof")
        with self._lock:
            list_events = getattr(self.event_store, "list_events", None)
            get_snapshot = getattr(self.event_store, "get_snapshot", None)
            list_risk_snapshots = getattr(
                self.event_store,
                "list_risk_snapshots",
                None,
            )
            admit = getattr(self.paper_gateway, "admit", None)
            if not all(
                callable(value)
                for value in (
                    list_events,
                    get_snapshot,
                    list_risk_snapshots,
                    admit,
                )
            ):
                raise TypeError("paper admission cycle dependencies are incomplete")
            known = self._known_admitted_events()
            admitted = 0
            skipped = 0
            failures: dict[str, str] = {}
            events = tuple(list_events())
            for event in sorted(events, key=lambda item: getattr(item, "event_id", "")):
                event_id = getattr(event, "event_id", None)
                if not isinstance(event_id, str) or not event_id:
                    raise TypeError("event store returned an invalid event identity")
                if event_id in known:
                    skipped += 1
                    continue
                try:
                    if self._event_eligibility_provider is not None:
                        eligible = self._event_eligibility_provider(event)
                        if type(eligible) is not bool:
                            raise TypeError(
                                "event eligibility provider must return boolean"
                            )
                        if not eligible:
                            skipped += 1
                            continue
                    snapshot = get_snapshot(event_id)
                    if getattr(snapshot, "state", None) is not EventState.CONFIRMED:
                        skipped += 1
                        continue
                    bar_health = self.bar_store.health()
                    if bar_health.degraded:
                        raise TrustedPaperBarIntegrityError(
                            "trusted_paper_bar_store_degraded"
                        )
                    if (
                        bar_health.last_attempted_bar_closed_at is not None
                        and bar_health.last_attempt_complete is not True
                    ):
                        raise TrustedPaperBarIntegrityError(
                            "trusted_paper_bar_cycle_incomplete"
                        )
                    if bar_health.calendar_preflight_failure_at is not None:
                        raise TrustedPaperBarIntegrityError(
                            "trusted_paper_calendar_preflight_failed"
                        )
                    bar = self.bar_store.get_for_code_at(
                        getattr(event, "code", None),
                        getattr(event, "bar_closed_at", None),
                    )
                    if bar is None:
                        raise TrustedPaperBarIntegrityError(
                            "authorized_signal_bar_missing"
                        )
                    risks = tuple(list_risk_snapshots(event_id))
                    if not risks:
                        raise TrustedPaperBarIntegrityError(
                            "authorized_risk_snapshot_missing"
                        )
                    risk_snapshot_id = getattr(risks[-1], "snapshot_id", None)
                    if not isinstance(risk_snapshot_id, str) or not risk_snapshot_id:
                        raise TypeError("risk snapshot identity is invalid")
                    admit(
                        event_id,
                        bar,
                        risk_snapshot_id=risk_snapshot_id,
                    )
                except Exception as exc:
                    failures[event_id] = type(exc).__name__
                    continue
                self._admitted_events.add(event_id)
                known.add(event_id)
                admitted += 1
            self._admission_cycles += 1
            self._admission_failures += len(failures)
            self._last_admission_error = (
                None if not failures else next(iter(failures.values()))
            )
            return PaperAdmissionCycleResult(
                occurred_at=occurred_at,
                admitted_count=admitted,
                skipped_count=skipped,
                failures=failures,
            )

    def is_cycle_complete(self, bar_closed_at: datetime) -> bool:
        return self.bar_store.is_cycle_complete(bar_closed_at)

    def is_exit_snapshot_committed(
        self,
        snapshot: ExitEvaluationSnapshot,
    ) -> bool:
        return self.bar_store.is_exit_snapshot_committed(snapshot)

    def attest_exit_snapshots(
        self,
        snapshots: Sequence[ExitEvaluationSnapshot],
    ) -> tuple[bool, ...]:
        return self.bar_store.attest_exit_snapshots(snapshots)

    def health(self) -> PaperResearchHealth:
        bar_store_health = self.bar_store.health()
        coverage_health = None
        latest_coverage = getattr(self._exit_cycle, "latest_coverage", None)
        if callable(latest_coverage):
            coverage = latest_coverage()
            if coverage is not None:
                if not isinstance(coverage, PaperExitCoverage):
                    raise TypeError("paper exit coverage health is invalid")
                coverage_health = PaperExitCoverageHealth(
                    bar_closed_at=coverage.bar_closed_at,
                    open_entry_count=len(coverage.open_entry_ids),
                    snapshot_count=len(coverage.snapshot_entry_ids),
                    failure_count=len(coverage.failures),
                    complete=coverage.complete,
                    fresh=(
                        bar_store_health.last_attempt_complete is True
                        and bar_store_health.last_attempted_bar_closed_at
                        is not None
                        and bar_store_health.calendar_preflight_failure_at
                        is None
                        and coverage.bar_closed_at
                        == bar_store_health.last_attempted_bar_closed_at
                    ),
                    scan_code=coverage.scan_code,
                    cycle_failure=coverage.cycle_failure,
                    failures=coverage.failures,
                )
        return PaperResearchHealth(
            mode="research_paper",
            auto_order_enabled=False,
            live_order_capability=False,
            bar_store=bar_store_health,
            bar_cycles=self._bar_cycles,
            bar_cycle_failures=self._bar_cycle_failures,
            admission_cycles=self._admission_cycles,
            admission_failures=self._admission_failures,
            admitted_event_count=len(self._known_admitted_events()),
            last_error=(
                self._last_bar_error or self._last_admission_error
            ),
            exit_coverage=coverage_health,
        )


def make_paper_pinned_codes_provider(
    ledger: object,
    event_store: object,
    *,
    event_eligibility_provider: Callable[[object], bool] | None = None,
) -> Callable[[], tuple[str, ...]]:
    """Pin paper positions and not-yet-consumed decisions into market cycles."""

    load = getattr(ledger, "load", None)
    list_events = getattr(event_store, "list_events", None)
    get_snapshot = getattr(event_store, "get_snapshot", None)
    if not callable(load):
        raise TypeError("ledger must provide load")
    if not callable(list_events) or not callable(get_snapshot):
        raise TypeError("event_store must provide list_events and get_snapshot")
    if event_eligibility_provider is not None and not callable(
        event_eligibility_provider
    ):
        raise TypeError("event_eligibility_provider must be callable")

    def provide() -> tuple[str, ...]:
        state = load()
        codes: set[str] = set()
        for lot in tuple(getattr(state, "lots", ())):
            code = getattr(lot, "code", None)
            if not isinstance(code, str) or not code:
                raise TypeError("paper lot has an invalid code")
            codes.add(code)
        admitted = {
            getattr(intent, "event_id", None)
            for intent in tuple(getattr(state, "intents", ()))
        }
        if any(not isinstance(event_id, str) or not event_id for event_id in admitted):
            raise TypeError("paper intent has an invalid event identity")
        for intent in tuple(getattr(state, "intents", ())):
            if (
                getattr(intent, "remaining_shares", 0) > 0
                and getattr(intent, "status", None) != "expired_risk_snapshot"
                and not str(getattr(intent, "status", "")).startswith("cancelled_")
            ):
                code = getattr(intent, "code", None)
                if not isinstance(code, str) or not code:
                    raise TypeError("paper intent has an invalid code")
                codes.add(code)
        for event in tuple(list_events()):
            if event_eligibility_provider is not None:
                eligible = event_eligibility_provider(event)
                if type(eligible) is not bool:
                    raise TypeError(
                        "event eligibility provider must return boolean"
                    )
                if not eligible:
                    continue
            event_id = getattr(event, "event_id", None)
            code = getattr(event, "code", None)
            if not isinstance(event_id, str) or not event_id:
                raise TypeError("event store returned an invalid event identity")
            if not isinstance(code, str) or not code:
                raise TypeError("event store returned an invalid event code")
            state_value = getattr(get_snapshot(event_id), "state", None)
            if state_value in {
                EventState.RISK_CHECKED,
                EventState.REVIEW_PENDING,
                EventState.CONFIRMED,
            } and event_id not in admitted:
                codes.add(code)
        return tuple(sorted(codes))

    return provide


def register_paper_research_jobs(
    scheduler: object,
    paper_runtime: PaperResearchRuntime,
    analysis_runtime: object,
    *,
    strategy_run: object,
) -> dict[str, object]:
    """Register one serialized bar coordinator plus review/admission consumers."""

    if not isinstance(paper_runtime, PaperResearchRuntime):
        raise TypeError("paper_runtime must be PaperResearchRuntime")
    add_job = getattr(scheduler, "add_job", None)
    config = getattr(analysis_runtime, "config", None)
    review_cycle = getattr(analysis_runtime, "review_cycle", None)
    if not callable(add_job):
        raise TypeError("scheduler must provide add_job")
    if config is None or not callable(review_cycle):
        raise TypeError("analysis_runtime is incomplete")
    strategy_status_provider = getattr(strategy_run, "status_payload", None)
    if not callable(strategy_status_provider):
        raise TypeError("strategy_run must provide status_payload")
    strategy_mutation_lease = getattr(strategy_run, "mutation_lease", None)
    if not callable(strategy_mutation_lease):
        raise TypeError("strategy_run must provide mutation_lease")
    if getattr(config, "enabled", None) is not True:
        return {}
    interval = getattr(config, "scan_interval_seconds", None)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise ValueError("scan_interval_seconds must be positive")

    def require_active_strategy_run() -> None:
        status = strategy_status_provider()
        if (
            not isinstance(status, Mapping)
            or status.get("state") != "active"
            or status.get("evidence_scope") != "current_epoch_only"
            or status.get("store_bindings_complete") is not True
        ):
            raise RuntimeError("paper strategy-run is not active")

    def bar_job() -> PaperBarCycleResult:
        with strategy_mutation_lease("paper_scheduler.bar_job"):
            require_active_strategy_run()
            return paper_runtime.bar_cycle(datetime.now(_CN))

    def admission_job() -> PaperAdmissionCycleResult:
        with strategy_mutation_lease("paper_scheduler.admission_job"):
            require_active_strategy_run()
            return paper_runtime.admission_cycle(datetime.now(_CN))

    def review_job() -> object:
        with strategy_mutation_lease("paper_scheduler.review_job"):
            require_active_strategy_run()
            return review_cycle()

    common = {
        "trigger": "interval",
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": interval,
    }
    bar = add_job(
        bar_job,
        id="decision_support_bar_cycle",
        seconds=interval,
        **common,
    )
    review = add_job(
        review_job,
        id="decision_support_review",
        seconds=max(1, min(5, interval)),
        **common,
    )
    admission = add_job(
        admission_job,
        id="decision_support_paper_admission",
        seconds=max(1, min(5, interval)),
        **common,
    )
    return {"bar": bar, "review": review, "admission": admission}


__all__ = (
    "ExplicitPaperTradingCalendar",
    "LIVE_ORDER_CAPABILITY",
    "PaperAdmissionCycleResult",
    "PaperBarCycleResult",
    "PaperExitAnalysisCycle",
    "PaperExitCoverage",
    "PaperExitCoverageHealth",
    "PaperExitCycleResult",
    "PaperRiskMark",
    "PaperResearchHealth",
    "PaperResearchRuntime",
    "PaperTradingSession",
    "SQLitePaperRiskState",
    "SQLiteTrustedPaperBarStore",
    "TrustedPaperBarIntegrityError",
    "TrustedPaperBarStoreHealth",
    "make_paper_pinned_codes_provider",
    "register_paper_research_jobs",
)
