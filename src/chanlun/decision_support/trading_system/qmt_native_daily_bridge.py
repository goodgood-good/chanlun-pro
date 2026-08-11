"""Certify native QMT daily history against the authoritative one-minute base.

The strict strategy specification requires 5m/30m bars to be aggregated from one
authoritative 1m stream.  It does not require throwing away older native daily
history when the installed minute cache is shorter.  This module permits that
older daily prefix only after every session shared with the completed 1m base
reconciles on OHLCV and price-basis metadata.

The bridge never invents or fills a bar.  Native daily rows are used only
before the first completed 1m session; the overlapping and newer daily tail is
always the aggregation produced by ``build_qmt_same_base_stream_frames``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
from typing import Literal, Mapping, Sequence

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtSameBaseStreamFrames,
)


QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID = (
    "chanlun-qmt-native-daily-reconciled-with-one-minute"
)
QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID = (
    "chanlun-qmt-native-daily-calendar-coverage"
)
QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY = "1m+native-d"
_REQUIRED = ("date", "open", "high", "low", "close", "volume")
_PRICE_FIELDS = ("open", "high", "low", "close")


class QmtNativeDailyReconciliationError(ValueError):
    """The native daily prefix cannot be certified for decision use."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        calendar_coverage_evidence: (
            QmtNativeDailyCalendarCoverageEvidence | None
        ) = None,
    ) -> None:
        if not code.startswith("QMT_NATIVE_DAILY_"):
            raise ValueError("native-daily reconciliation code is invalid")
        self.code = code
        self.detail = detail
        self.calendar_coverage_evidence = calendar_coverage_evidence
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class QmtNativeDailyCalendarCoverageEvidence:
    """Causal proof of a native-daily/session-calendar comparison.

    A missing native daily row is deliberately labelled *unexplained*.  This
    contract never upgrades an absent bar to a suspension or another lawful
    non-trading state without an independently captured point-in-time status
    fact.  It therefore improves diagnosis while preserving the existing
    fail-closed decision.
    """

    symbol: str
    observed_at: datetime
    native_first_session: date
    native_last_session: date
    calendar_first_session: date
    calendar_last_session: date
    native_daily_bar_count: int
    expected_calendar_session_count: int
    native_only_sessions: tuple[date, ...]
    unexplained_calendar_only_sessions: tuple[date, ...]
    trading_calendar_revision: str
    status: Literal[
        "EXACT",
        "NATIVE_SESSION_OUTSIDE_CALENDAR",
        "UNEXPLAINED_CALENDAR_SESSION_MISSING",
        "MIXED_SESSION_MISMATCH",
    ]
    contract_id: str = QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
    data_grade: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        object.__setattr__(self, "native_only_sessions", tuple(self.native_only_sessions))
        object.__setattr__(
            self,
            "unexplained_calendar_only_sessions",
            tuple(self.unexplained_calendar_only_sessions),
        )
        dates = (
            self.native_first_session,
            self.native_last_session,
            self.calendar_first_session,
            self.calendar_last_session,
            *self.native_only_sessions,
            *self.unexplained_calendar_only_sessions,
        )
        if not self.symbol or any(type(value) is not date for value in dates):
            raise ValueError("native-daily calendar coverage identity is invalid")
        if (
            self.native_first_session > self.native_last_session
            or self.calendar_first_session > self.calendar_last_session
            or type(self.native_daily_bar_count) is not int
            or type(self.expected_calendar_session_count) is not int
            or self.native_daily_bar_count <= 0
            or self.expected_calendar_session_count <= 0
        ):
            raise ValueError("native-daily calendar coverage range is invalid")
        for values in (
            self.native_only_sessions,
            self.unexplained_calendar_only_sessions,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("native-daily calendar gaps must be unique and ordered")
        if any(
            value < self.native_first_session or value > self.native_last_session
            for value in self.unexplained_calendar_only_sessions
        ):
            raise ValueError("native-daily missing session is outside its native range")
        if self.native_daily_bar_count != (
            self.expected_calendar_session_count
            - len(self.unexplained_calendar_only_sessions)
            + len(self.native_only_sessions)
        ):
            raise ValueError("native-daily calendar counts are inconsistent")
        expected_status = (
            "MIXED_SESSION_MISMATCH"
            if self.native_only_sessions
            and self.unexplained_calendar_only_sessions
            else "NATIVE_SESSION_OUTSIDE_CALENDAR"
            if self.native_only_sessions
            else "UNEXPLAINED_CALENDAR_SESSION_MISSING"
            if self.unexplained_calendar_only_sessions
            else "EXACT"
        )
        if self.status != expected_status:
            raise ValueError("native-daily calendar status contradicts its gaps")
        if not self.trading_calendar_revision.startswith("sha256:"):
            raise ValueError("native-daily calendar revision is invalid")
        if (
            self.contract_id
            != QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
            or self.data_grade != "RESEARCH_ONLY"
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("native-daily calendar safety contract changed")

    def _stable_document(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "symbol": self.symbol,
            "observed_at": self.observed_at.isoformat(),
            "native_first_session": self.native_first_session.isoformat(),
            "native_last_session": self.native_last_session.isoformat(),
            "calendar_first_session": self.calendar_first_session.isoformat(),
            "calendar_last_session": self.calendar_last_session.isoformat(),
            "native_daily_bar_count": self.native_daily_bar_count,
            "expected_calendar_session_count": self.expected_calendar_session_count,
            "native_only_session_count": len(self.native_only_sessions),
            "native_only_sessions": [
                value.isoformat() for value in self.native_only_sessions
            ],
            "unexplained_calendar_only_session_count": len(
                self.unexplained_calendar_only_sessions
            ),
            "unexplained_calendar_only_sessions": [
                value.isoformat()
                for value in self.unexplained_calendar_only_sessions
            ],
            "trading_calendar_revision": self.trading_calendar_revision,
            "status": self.status,
            "missing_session_interpretation": (
                "UNEXPLAINED_NEVER_INFERRED_AS_SUSPENSION"
            ),
            "point_in_time_status_evidence_present": False,
            "entry_disposition": (
                "NO_CALENDAR_BLOCKER" if self.status == "EXACT" else "FAIL_CLOSED"
            ),
            "prefix_only": True,
            "data_grade": self.data_grade,
            "live_status": self.live_status,
        }

    @property
    def coverage_revision(self) -> str:
        return sha256_json(self._stable_document())

    def document(self) -> dict[str, object]:
        stable = self._stable_document()
        return {**stable, "coverage_revision": self.coverage_revision}

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, object],
    ) -> QmtNativeDailyCalendarCoverageEvidence:
        try:
            if (
                type(value["native_daily_bar_count"]) is not int
                or type(value["expected_calendar_session_count"]) is not int
                or type(value["native_only_session_count"]) is not int
                or type(value["unexplained_calendar_only_session_count"])
                is not int
                or not isinstance(value["native_only_sessions"], list)
                or not isinstance(
                    value["unexplained_calendar_only_sessions"], list
                )
                or value["point_in_time_status_evidence_present"] is not False
                or value["prefix_only"] is not True
            ):
                raise ValueError("native-daily calendar scalar types are invalid")
            native_only = tuple(
                date.fromisoformat(str(item))
                for item in value["native_only_sessions"]  # type: ignore[index]
            )
            missing = tuple(
                date.fromisoformat(str(item))
                for item in value["unexplained_calendar_only_sessions"]  # type: ignore[index]
            )
            result = cls(
                symbol=str(value["symbol"]),
                observed_at=datetime.fromisoformat(str(value["observed_at"])),
                native_first_session=date.fromisoformat(
                    str(value["native_first_session"])
                ),
                native_last_session=date.fromisoformat(
                    str(value["native_last_session"])
                ),
                calendar_first_session=date.fromisoformat(
                    str(value["calendar_first_session"])
                ),
                calendar_last_session=date.fromisoformat(
                    str(value["calendar_last_session"])
                ),
                native_daily_bar_count=int(value["native_daily_bar_count"]),
                expected_calendar_session_count=int(
                    value["expected_calendar_session_count"]
                ),
                native_only_sessions=native_only,
                unexplained_calendar_only_sessions=missing,
                trading_calendar_revision=str(value["trading_calendar_revision"]),
                status=str(value["status"]),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("native-daily calendar coverage document is malformed") from exc
        if dict(value) != result.document():
            raise ValueError("native-daily calendar coverage document is non-canonical")
        return result


@dataclass(frozen=True, slots=True)
class QmtNativeDailyReconciliationEvidence:
    symbol: str
    observed_at: datetime
    native_daily_bar_count: int
    one_minute_daily_bar_count: int
    overlap_session_count: int
    first_overlap_session: str
    last_overlap_session: str
    native_daily_content_revision: str
    one_minute_base_revision: str
    price_basis_revision: str
    trading_calendar_revision: str
    price_tolerance_quanta: int
    price_difference_identities: tuple[str, ...]
    max_observed_price_difference_quanta: int
    reconciled_source_revision: str
    contract_id: str = QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.symbol:
            raise ValueError("native-daily reconciliation symbol is required")
        if (
            self.native_daily_bar_count <= 0
            or self.one_minute_daily_bar_count <= 0
            or self.overlap_session_count <= 0
        ):
            raise ValueError("native-daily reconciliation counts must be positive")
        if self.price_tolerance_quanta not in {0, 1}:
            raise ValueError("native-daily price tolerance must be zero or one quantum")
        if (
            self.max_observed_price_difference_quanta < 0
            or self.max_observed_price_difference_quanta
            > self.price_tolerance_quanta
            or len(self.price_difference_identities)
            != len(set(self.price_difference_identities))
        ):
            raise ValueError("native-daily price-difference evidence is invalid")
        for field in (
            "native_daily_content_revision",
            "one_minute_base_revision",
            "price_basis_revision",
            "trading_calendar_revision",
            "reconciled_source_revision",
        ):
            if not getattr(self, field).startswith("sha256:"):
                raise ValueError(f"{field} must be a sha256 identity")
        if (
            self.contract_id != QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("native-daily reconciliation safety contract changed")

    def document(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "symbol": self.symbol,
            "observed_at": self.observed_at.isoformat(),
            "native_daily_bar_count": self.native_daily_bar_count,
            "one_minute_daily_bar_count": self.one_minute_daily_bar_count,
            "overlap_session_count": self.overlap_session_count,
            "first_overlap_session": self.first_overlap_session,
            "last_overlap_session": self.last_overlap_session,
            "native_daily_content_revision": self.native_daily_content_revision,
            "one_minute_base_revision": self.one_minute_base_revision,
            "price_basis_revision": self.price_basis_revision,
            "trading_calendar_revision": self.trading_calendar_revision,
            "price_tolerance_quanta": self.price_tolerance_quanta,
            "price_difference_count": len(self.price_difference_identities),
            "price_difference_session_count": len(
                {
                    value.split(":", 1)[0]
                    for value in self.price_difference_identities
                }
            ),
            "price_difference_identities": list(
                self.price_difference_identities
            ),
            "max_observed_price_difference_quanta": (
                self.max_observed_price_difference_quanta
            ),
            "reconciled_source_revision": self.reconciled_source_revision,
            "all_overlap_ohlcv_equal": not self.price_difference_identities,
            "all_overlap_ohlcv_within_declared_tolerance": True,
            "native_daily_role": "LEFT_HISTORY_BEFORE_ONE_MINUTE_BASE_ONLY",
            "intraday_role": "ONE_MINUTE_DERIVED_30M_AND_DAILY_TAIL",
            "live_status": self.live_status,
        }


@dataclass(frozen=True, slots=True)
class QmtNativeDailyBridge:
    daily: pd.DataFrame
    thirty_minute: pd.DataFrame
    evidence: QmtNativeDailyReconciliationEvidence
    calendar_coverage_evidence: QmtNativeDailyCalendarCoverageEvidence


def _fail(
    code: str,
    detail: str,
    *,
    calendar_coverage_evidence: QmtNativeDailyCalendarCoverageEvidence | None = None,
) -> None:
    raise QmtNativeDailyReconciliationError(
        code,
        detail,
        calendar_coverage_evidence=calendar_coverage_evidence,
    )


def _normalized_native_daily(
    frame: pd.DataFrame,
    *,
    observed_at: datetime,
) -> pd.DataFrame:
    missing = set(_REQUIRED).difference(frame.columns)
    if missing:
        _fail("QMT_NATIVE_DAILY_COLUMNS_UNRESOLVED", repr(sorted(missing)))
    result = frame.loc[:, list(_REQUIRED)].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    if result["date"].dt.tz is None:
        _fail("QMT_NATIVE_DAILY_TIMEZONE_UNRESOLVED", "timezone-naive date")
    local = result["date"].dt.tz_convert("Asia/Shanghai")
    # QMT local fixed records label daily bars at midnight while ExchangeQMT
    # normalises them to 15:00.  The bar is knowable only at the latter time.
    result["date"] = pd.DatetimeIndex(
        [
            timestamp.normalize() + pd.Timedelta(hours=15)
            for timestamp in local
        ]
    )
    result = result[result["date"] <= pd.Timestamp(observed_at)].copy()
    if result.empty:
        _fail("QMT_NATIVE_DAILY_HISTORY_UNAVAILABLE", "no completed daily bars")
    if result["date"].duplicated().any() or not result["date"].is_monotonic_increasing:
        _fail("QMT_NATIVE_DAILY_SEQUENCE_INVALID", "duplicate or unordered session")
    for field in (*_PRICE_FIELDS, "volume"):
        result[field] = pd.to_numeric(result[field], errors="raise")
    numeric = result.loc[:, [*_PRICE_FIELDS, "volume"]].astype(float)
    prices = numeric.loc[:, list(_PRICE_FIELDS)]
    invalid = (
        ~numeric.map(math.isfinite).all(axis=1)
        | (prices <= 0).any(axis=1)
        | (numeric["volume"] < 0)
        | (numeric["high"] < prices.max(axis=1))
        | (numeric["low"] > prices.min(axis=1))
    )
    if bool(invalid.any()):
        _fail("QMT_NATIVE_DAILY_OHLCV_INVALID", "invalid native daily row")
    result.loc[:, [*_PRICE_FIELDS, "volume"]] = numeric
    attrs = dict(frame.attrs)
    result = result.reset_index(drop=True)
    result.attrs = attrs
    return result


def _price(value: object, quantum: Decimal) -> Decimal:
    return Decimal(str(float(value))).quantize(quantum)


def _volume(value: object) -> Decimal:
    return Decimal(str(float(value))).normalize()


def build_qmt_native_daily_bridge(
    *,
    symbol: str,
    native_daily_frame: pd.DataFrame,
    same_base: QmtSameBaseStreamFrames,
    decision_time: datetime,
    trading_sessions: Sequence[date],
    max_price_difference_quanta: int = 0,
) -> QmtNativeDailyBridge:
    """Return a daily/30m pair with a cryptographic reconciliation lineage."""

    observed = normalize_datetime(decision_time, "decision_time")
    if type(max_price_difference_quanta) is not int or (
        max_price_difference_quanta not in {0, 1}
    ):
        raise ValueError("max_price_difference_quanta must be exactly 0 or 1")
    if same_base.symbol != symbol:
        _fail("QMT_NATIVE_DAILY_SYMBOL_MISMATCH", f"{same_base.symbol} != {symbol}")
    if same_base.session_issues or same_base.price_basis_revision is None:
        _fail(
            "QMT_NATIVE_DAILY_ONE_MINUTE_BASE_UNRESOLVED",
            ",".join(value.code for value in same_base.session_issues)
            or "price basis unresolved",
        )
    if same_base.daily.empty or same_base.thirty_minute.empty:
        _fail(
            "QMT_NATIVE_DAILY_ONE_MINUTE_BASE_UNRESOLVED",
            "completed 1m-derived daily/30m tail is empty",
        )
    native = _normalized_native_daily(
        native_daily_frame,
        observed_at=observed,
    )
    native_attrs = dict(native_daily_frame.attrs)
    intraday_attrs = dict(same_base.daily.attrs)
    price_fields = (
        "price_basis_provider",
        "price_basis_adjustment",
        "price_basis_revision",
        "structure_price_quantum",
    )
    mismatched = tuple(
        field
        for field in price_fields
        if native_attrs.get(field) != intraday_attrs.get(field)
    )
    if mismatched or native_attrs.get("price_basis_provider") != "qmt":
        _fail(
            "QMT_NATIVE_DAILY_PRICE_BASIS_MISMATCH",
            f"fields={mismatched!r}; provider={native_attrs.get('price_basis_provider')!r}",
        )
    try:
        quantum = Decimal(str(native_attrs["structure_price_quantum"]))
    except Exception as exc:
        raise QmtNativeDailyReconciliationError(
            "QMT_NATIVE_DAILY_PRICE_BASIS_MISMATCH",
            "structure price quantum is invalid",
        ) from exc
    if quantum <= 0:
        _fail("QMT_NATIVE_DAILY_PRICE_BASIS_MISMATCH", "non-positive quantum")

    native_by_session = {
        pd.Timestamp(row.date).date(): row for row in native.itertuples(index=False)
    }
    derived = same_base.daily.loc[:, list(_REQUIRED)].copy()
    derived_by_session = {
        pd.Timestamp(row.date).date(): row for row in derived.itertuples(index=False)
    }
    native_sessions = set(native_by_session)
    derived_sessions = set(derived_by_session)
    supplied_calendar = tuple(trading_sessions)
    if (
        not supplied_calendar
        or supplied_calendar != tuple(sorted(set(supplied_calendar)))
        or any(type(value) is not date for value in supplied_calendar)
    ):
        _fail(
            "QMT_NATIVE_DAILY_TRADING_CALENDAR_INVALID",
            "calendar must be a non-empty, unique, chronological date sequence",
        )
    # A caller may own a longer immutable exchange calendar, but a decision
    # identity must never change merely because later sessions were appended.
    # Bind and validate only the prefix visible no later than this decision day.
    calendar = tuple(
        value for value in supplied_calendar if value <= observed.date()
    )
    if not calendar:
        _fail(
            "QMT_NATIVE_DAILY_TRADING_CALENDAR_COVERAGE_INSUFFICIENT",
            "calendar has no decision-time-visible session",
        )
    calendar_set = set(calendar)
    if min(calendar) > min(native_sessions) or max(calendar) < max(native_sessions):
        _fail(
            "QMT_NATIVE_DAILY_TRADING_CALENDAR_COVERAGE_INSUFFICIENT",
            f"calendar={min(calendar)}..{max(calendar)}; "
            f"native={min(native_sessions)}..{max(native_sessions)}",
        )
    expected_native_sessions = {
        value
        for value in calendar
        if min(native_sessions) <= value <= max(native_sessions)
    }
    trading_calendar_revision = sha256_json(
        {
            "schema": "chanlun-qmt-native-daily-trading-calendar-prefix",
            "sessions": tuple(value.isoformat() for value in calendar),
        }
    )
    native_only_sessions = tuple(sorted(native_sessions - calendar_set))
    unexplained_calendar_only_sessions = tuple(
        sorted(expected_native_sessions - native_sessions)
    )
    calendar_coverage_evidence = QmtNativeDailyCalendarCoverageEvidence(
        symbol=symbol,
        observed_at=observed,
        native_first_session=min(native_sessions),
        native_last_session=max(native_sessions),
        calendar_first_session=min(calendar),
        calendar_last_session=max(calendar),
        native_daily_bar_count=len(native_sessions),
        expected_calendar_session_count=len(expected_native_sessions),
        native_only_sessions=native_only_sessions,
        unexplained_calendar_only_sessions=(
            unexplained_calendar_only_sessions
        ),
        trading_calendar_revision=trading_calendar_revision,
        status=(
            "MIXED_SESSION_MISMATCH"
            if native_only_sessions and unexplained_calendar_only_sessions
            else "NATIVE_SESSION_OUTSIDE_CALENDAR"
            if native_only_sessions
            else "UNEXPLAINED_CALENDAR_SESSION_MISSING"
            if unexplained_calendar_only_sessions
            else "EXACT"
        ),
    )
    if calendar_coverage_evidence.status != "EXACT":
        _fail(
            "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
            f"native_only={list(native_only_sessions)!r}; "
            f"calendar_only={list(unexplained_calendar_only_sessions)!r}",
            calendar_coverage_evidence=calendar_coverage_evidence,
        )
    overlap = tuple(sorted(native_sessions & derived_sessions))
    if not overlap:
        _fail(
            "QMT_NATIVE_DAILY_NO_ONE_MINUTE_OVERLAP",
            "no completed session is shared by native daily and 1m",
        )
    common_start = max(min(native_sessions), min(derived_sessions))
    common_end = min(max(native_sessions), max(derived_sessions))
    if common_start <= common_end:
        native_common = {
            value for value in native_sessions if common_start <= value <= common_end
        }
        derived_common = {
            value for value in derived_sessions if common_start <= value <= common_end
        }
        if native_common != derived_common:
            _fail(
                "QMT_NATIVE_DAILY_SESSION_COVERAGE_MISMATCH",
                f"native_only={sorted(native_common-derived_common)!r}; "
                f"one_minute_only={sorted(derived_common-native_common)!r}",
            )
    if max(native_sessions) > max(derived_sessions):
        _fail(
            "QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",
            f"native={max(native_sessions)}; one_minute={max(derived_sessions)}",
        )

    mismatches: list[str] = []
    price_differences: list[str] = []
    maximum_price_difference_quanta = 0
    for session in overlap:
        left = native_by_session[session]
        right = derived_by_session[session]
        for field in _PRICE_FIELDS:
            difference = abs(
                _price(getattr(left, field), quantum)
                - _price(getattr(right, field), quantum)
            )
            if difference:
                identity = f"{session}:{field}"
                difference_quanta = int(difference / quantum)
                price_differences.append(identity)
                maximum_price_difference_quanta = max(
                    maximum_price_difference_quanta,
                    difference_quanta,
                )
                if difference_quanta > max_price_difference_quanta:
                    mismatches.append(identity)
        if _volume(left.volume) != _volume(right.volume):
            mismatches.append(f"{session}:volume")
    if mismatches:
        _fail(
            "QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH",
            ",".join(mismatches[:20]),
        )

    native_content_revision = sha256_json(
        {
            "schema": "chanlun-qmt-native-daily-visible-prefix",
            "symbol": symbol,
            "observed_at": observed,
            "price_basis": {
                field: native_attrs.get(field) for field in price_fields
            },
            "rows": tuple(
                {
                    "date": pd.Timestamp(row.date).to_pydatetime(),
                    **{field: float(getattr(row, field)) for field in _PRICE_FIELDS},
                    "volume": float(row.volume),
                }
                for row in native.itertuples(index=False)
            ),
        }
    )
    reconciled_source_revision = sha256_json(
        {
            "contract_id": QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
            "symbol": symbol,
            "observed_at": observed,
            "native_daily_content_revision": native_content_revision,
            "one_minute_base_revision": same_base.source_base_stream_revision,
            "overlap_sessions": tuple(value.isoformat() for value in overlap),
            "price_basis_revision": same_base.price_basis_revision,
            "trading_calendar_revision": trading_calendar_revision,
            "price_tolerance_quanta": max_price_difference_quanta,
            "price_difference_identities": tuple(price_differences),
        }
    )

    first_derived = min(derived_sessions)
    older_native = native[
        native["date"].dt.date < first_derived
    ].copy()
    daily = pd.concat((older_native, derived), ignore_index=True)
    daily = daily.sort_values("date", kind="stable").reset_index(drop=True)
    thirty = same_base.thirty_minute.copy()
    lineage = {
        "source_base_stream_revision": reconciled_source_revision,
        "source_base_frequency": QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
        "qmt_native_daily_reconciliation_contract_id": (
            QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
        ),
        "qmt_native_daily_content_revision": native_content_revision,
        "qmt_intraday_one_minute_base_revision": (
            same_base.source_base_stream_revision
        ),
        "qmt_native_daily_overlap_session_count": len(overlap),
        "qmt_native_daily_trading_calendar_revision": (
            trading_calendar_revision
        ),
        "qmt_native_daily_price_tolerance_quanta": (
            max_price_difference_quanta
        ),
        "qmt_native_daily_price_difference_count": len(price_differences),
    }
    daily.attrs = {**intraday_attrs, **lineage, "derived_frequency": "d"}
    thirty.attrs = {
        **dict(same_base.thirty_minute.attrs),
        **lineage,
        "derived_frequency": "30m",
    }
    evidence = QmtNativeDailyReconciliationEvidence(
        symbol=symbol,
        observed_at=observed,
        native_daily_bar_count=len(native),
        one_minute_daily_bar_count=len(derived),
        overlap_session_count=len(overlap),
        first_overlap_session=overlap[0].isoformat(),
        last_overlap_session=overlap[-1].isoformat(),
        native_daily_content_revision=native_content_revision,
        one_minute_base_revision=same_base.source_base_stream_revision,
        price_basis_revision=same_base.price_basis_revision,
        trading_calendar_revision=trading_calendar_revision,
        price_tolerance_quanta=max_price_difference_quanta,
        price_difference_identities=tuple(price_differences),
        max_observed_price_difference_quanta=(
            maximum_price_difference_quanta
        ),
        reconciled_source_revision=reconciled_source_revision,
    )
    return QmtNativeDailyBridge(
        daily=daily,
        thirty_minute=thirty,
        evidence=evidence,
        calendar_coverage_evidence=calendar_coverage_evidence,
    )


__all__ = (
    "QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID",
    "QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID",
    "QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY",
    "QmtNativeDailyBridge",
    "QmtNativeDailyCalendarCoverageEvidence",
    "QmtNativeDailyReconciliationError",
    "QmtNativeDailyReconciliationEvidence",
    "build_qmt_native_daily_bridge",
)
