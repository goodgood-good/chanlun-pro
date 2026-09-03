"""Derive causal strict strategy timeframes from one completed QMT one-minute stream.

QMT's A-share one-minute history is end-labelled, but a complete session has
241 rows: a 09:30 call-auction/opening event plus 240 completed continuous
auction bars (09:31..11:30 and 13:01..15:00).  The live current-session stream
can omit minutes with no trades, including 09:31.  Treating all 241 rows as
regular minutes shifts every 30-minute boundary and double-counts the session
shape, while requiring a literal 09:31 row makes the live monitor reject a
legitimate sparse tape.

This adapter merges the 09:30 event into the 09:31 bar, then derives 5m, 30m
and daily bars from exactly the same normalized one-minute prefix.  Every
derived frame carries the same content hash so the data gate can prove actual
base-stream identity rather than merely observe the same vendor name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
from typing import Literal, Sequence

import numpy as np
import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_completed_one_minute_closes,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import FactBlocker


QmtSameBaseGrade = Literal[
    "FULL_SYSTEM_ELIGIBLE",
    "RESEARCH_ONLY",
    "UNRESOLVED",
]
_REQUIRED = ("date", "open", "high", "low", "close", "volume")
_CN = "Asia/Shanghai"
QMT_COMPLETED_ONE_MINUTE_GRID_REVISION = (
    # 完整的 241 行到 240 行映射保持不变。稀疏实时交易日属于当前因果前缀，有意不伪装成
    # 新完成网格，也不使最近一份因果完整日级筛选发布失效。
    "QMT_A_SHARE_END_LABELLED_241_TO_COMPLETED_240_TRADE_AWARE"
)
_QMT_SAME_BASE_STREAM_REVISION_SCHEMA = (
    "chanlun-qmt-same-base-stream-v2-little-endian-vector"
)
_MINUTE_IN_NANOSECONDS = 60_000_000_000
_COMPLETED_MINUTE_OFFSETS = np.concatenate(
    (
        np.arange(9 * 60 + 31, 11 * 60 + 31, dtype=np.int64),
        np.arange(13 * 60 + 1, 15 * 60 + 1, dtype=np.int64),
    )
) * _MINUTE_IN_NANOSECONDS
_NATIVE_MINUTE_OFFSETS = np.concatenate(
    (
        np.asarray((9 * 60 + 30,), dtype=np.int64) * _MINUTE_IN_NANOSECONDS,
        _COMPLETED_MINUTE_OFFSETS,
    )
)
_MISSING_SESSION_DETAIL = (
    "trading-calendar session is absent from the QMT 1m prefix"
)
_INVALID_SESSION_GRID_DETAIL = (
    "expected 09:30 opening event plus the completed 240-bar grid, "
    "or an exact current-session prefix"
)
_SESSION_ISSUE_CLASSIFICATIONS = {
    "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING": (
        "UNCLASSIFIED_EXPECTED_SESSION_ABSENCE"
    ),
    "QMT_ONE_MINUTE_SESSION_GRID_INVALID": "INVALID_ONE_MINUTE_SESSION_GRID",
}


@dataclass(frozen=True, slots=True)
class QmtMinuteSessionIssue:
    session: date
    code: str
    observed_rows: int
    detail: str

    def __post_init__(self) -> None:
        if type(self.session) is not date:
            raise ValueError("QMT minute session issue requires an exact date")
        if self.code not in _SESSION_ISSUE_CLASSIFICATIONS:
            raise ValueError("unsupported QMT minute session issue code")
        if type(self.observed_rows) is not int or self.observed_rows < 0:
            raise ValueError("QMT minute session observed rows must be non-negative")
        expected_detail = {
            "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING": _MISSING_SESSION_DETAIL,
            "QMT_ONE_MINUTE_SESSION_GRID_INVALID": _INVALID_SESSION_GRID_DETAIL,
        }[self.code]
        if self.detail != expected_detail:
            raise ValueError("QMT minute session issue detail changed")
        if (
            self.code == "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING"
            and self.observed_rows != 0
        ):
            raise ValueError("a missing QMT session must contain zero observed rows")

    def document(self) -> dict[str, object]:
        """Return exact, presentation-only evidence for a failed session gate.

        An absent one-minute session can be a real suspension or a data gap.
        Neither QMT's current instrument status nor a market-phase calendar
        proves that historical distinction, so the document deliberately keeps
        the absence unclassified and the entry disposition fail-closed.
        """

        return {
            "session": self.session.isoformat(),
            "code": self.code,
            "observed_rows": self.observed_rows,
            "classification": _SESSION_ISSUE_CLASSIFICATIONS[self.code],
            "detail": self.detail,
            "historical_trade_status_proven": False,
            "entry_disposition": "FAIL_CLOSED",
        }


@dataclass(frozen=True, slots=True)
class QmtMinuteSourceBoundaryExclusion:
    """An incomplete first source session excluded before evaluation.

    QMT's finite one-minute cache may begin in the middle of a historical
    session even though the caller requested an earlier range.  Such a row
    prefix is not a malformed session *inside* the evaluated interval: it is
    the observable left boundary of the installed source.  It may be omitted
    only when it is the first observed session and strictly precedes the
    caller's frozen evaluation boundary.  The omission is included in the
    base-stream identity so it can never happen silently.
    """

    session: date
    observed_rows: int
    first_observed_at: datetime
    last_observed_at: datetime
    evaluation_not_before: date
    code: str = "QMT_ONE_MINUTE_LEADING_SOURCE_SLICE_EXCLUDED"

    def __post_init__(self) -> None:
        if (
            type(self.session) is not date
            or type(self.evaluation_not_before) is not date
            or self.session >= self.evaluation_not_before
            or type(self.observed_rows) is not int
            or self.observed_rows <= 0
            or self.first_observed_at.tzinfo is None
            or self.last_observed_at.tzinfo is None
            or self.first_observed_at.date() != self.session
            or self.last_observed_at.date() != self.session
            or self.first_observed_at > self.last_observed_at
            or self.code != "QMT_ONE_MINUTE_LEADING_SOURCE_SLICE_EXCLUDED"
        ):
            raise ValueError("invalid QMT leading source-boundary exclusion")

    def document(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "code": self.code,
            "observed_rows": self.observed_rows,
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "evaluation_not_before": self.evaluation_not_before.isoformat(),
            "classification": "FINITE_SOURCE_LEFT_BOUNDARY",
            "used_as_completed_intraday_session": False,
            "entry_disposition": "EXCLUDED_BEFORE_EVALUATION",
        }


@dataclass(frozen=True, slots=True)
class QmtSameBaseStreamFrames:
    symbol: str
    observed_at: datetime
    one_minute: pd.DataFrame
    five_minute: pd.DataFrame
    thirty_minute: pd.DataFrame
    daily: pd.DataFrame
    source_base_stream_revision: str
    price_basis_revision: str | None
    complete_sessions: tuple[date, ...]
    partial_session: date | None
    session_issues: tuple[QmtMinuteSessionIssue, ...]
    grade: QmtSameBaseGrade
    blockers: tuple[FactBlocker, ...]
    source_boundary_exclusions: tuple[QmtMinuteSourceBoundaryExclusion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.symbol:
            raise ValueError("QMT same-base stream requires a symbol")
        if not self.source_base_stream_revision.startswith("sha256:"):
            raise ValueError("QMT same-base stream requires a sha256 identity")

    @property
    def full_system_eligible(self) -> bool:
        return self.grade == "FULL_SYSTEM_ELIGIBLE"


def _completed_times(session: date) -> tuple[datetime, ...]:
    return a_share_completed_one_minute_closes(session, timezone=None)


def _visible_one_minute(
    frame: pd.DataFrame,
    *,
    decision_time: datetime,
) -> pd.DataFrame:
    missing = set(_REQUIRED).difference(frame.columns)
    if missing:
        raise ValueError(f"QMT 1m frame is missing columns: {sorted(missing)!r}")
    work = frame.loc[:, list(_REQUIRED)].copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    if work["date"].dt.tz is None:
        raise ValueError("QMT 1m completion times must be timezone-aware")
    work["date"] = work["date"].dt.tz_convert(_CN)
    work = work[work["date"] <= pd.Timestamp(decision_time)].copy()
    if work["date"].duplicated().any() or not work["date"].is_monotonic_increasing:
        raise ValueError("QMT 1m completion times must be unique and chronological")
    for field in ("open", "high", "low", "close", "volume"):
        work[field] = pd.to_numeric(work[field], errors="raise")
    if not work.empty:
        prices = work[["open", "high", "low", "close"]]
        invalid = (
            (prices <= 0).any(axis=1)
            | (work["volume"] < 0)
            | (work["high"] < prices.max(axis=1))
            | (work["low"] > prices.min(axis=1))
        )
        if invalid.any():
            raise ValueError("QMT 1m frame contains invalid OHLCV")
    work.attrs = dict(frame.attrs)
    return work


def normalize_qmt_opening_event_for_completed_minutes(
    rows: pd.DataFrame,
) -> pd.DataFrame:
    """Merge QMT's 09:30 event into 09:31, never emit it as a 1m bar.

    The small public adapter is shared by structure construction and virtual
    execution so both paths interpret QMT's 241-row native session the same
    way.  During the live session QMT can omit zero-trade minutes.  When 09:31
    is absent, a positive-volume 09:30 auction event is therefore relabelled
    as the completed 09:31 bar made entirely from that real auction evidence;
    a zero-volume placeholder is discarded.  Later sparse minutes remain
    sparse: this adapter never invents prices, volume, or synthetic trades.
    Other grid validation remains the caller's responsibility.
    """

    snapshot_attrs = dict(rows.attrs)
    if rows.empty or pd.Timestamp(rows.iloc[0]["date"]).time() != time(9, 30):
        result = rows.copy()
        result.attrs = snapshot_attrs
        return result
    if len(rows) == 1:
        # 09:30 边界已知，但尚无标准一分钟 K 线完成，不能把它变成独立定位 K 线。
        result = rows.iloc[0:0].copy()
        result.attrs = snapshot_attrs
        return result
    opening_at = pd.Timestamp(rows.iloc[0]["date"])
    next_at = pd.Timestamp(rows.iloc[1]["date"])
    next_native = next_at.tz_localize(None).to_pydatetime()
    if (
        next_at.date() != opening_at.date()
        or next_native not in _completed_times(opening_at.date())
    ):
        raise ValueError(
            "QMT 09:30 opening event is not followed by a valid "
            "completed continuous-auction minute"
        )
    opening = rows.iloc[0]
    opening_volume = float(opening["volume"])
    # 部分标的会暴露成交量为零且价格沿用或过期的 09:30 占位；它不是已成交集合竞价事实，
    # 不得覆盖首根真实连续竞价 K 线。正成交量开盘事件是真实价量证据，继续合并。
    if next_at.time() == time(9, 31):
        first_bar = rows.iloc[1].copy()
        if opening_volume > 0:
            first_bar["open"] = opening["open"]
            first_bar["high"] = max(
                float(opening["high"]), float(first_bar["high"])
            )
            first_bar["low"] = min(
                float(opening["low"]), float(first_bar["low"])
            )
            first_bar["volume"] = opening_volume + float(first_bar["volume"])
        merged = pd.concat(
            (pd.DataFrame([first_bar]), rows.iloc[2:].copy()),
            ignore_index=True,
        )
    elif opening_volume > 0:
        # QMT 的 09:31 连续竞价切片没有成交，但正成交量开盘集合竞价仍是已观测市场事实，
        # 属于首个已完成区间。将其结束标签改到 09:31，而不是附着到更晚成交或伪造中间 K 线。
        first_bar = opening.copy()
        first_bar["date"] = opening_at + pd.Timedelta(minutes=1)
        merged = pd.concat(
            (pd.DataFrame([first_bar]), rows.iloc[1:].copy()),
            ignore_index=True,
        )
    else:
        # 零成交量 09:30 占位不含成交证据；保持后续已观测 K 线不变既因果又无损。
        merged = rows.iloc[1:].copy().reset_index(drop=True)
    result = merged.loc[:, list(_REQUIRED)]
    result.attrs = snapshot_attrs
    return result


def normalize_qmt_opening_events_for_completed_minutes(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """Normalize every QMT session while preserving exact price-basis attrs.

    The singular adapter above deliberately handles one session because the
    strict same-base builder validates each exchange grid independently.  Page
    screening receives a multi-session prefix from ``ExchangeQMT.klines``;
    this wrapper applies the identical auction merge to every observed date so
    native chart analysis and replay/execution cannot drift onto 241 versus
    240 bars per complete session.
    """

    snapshot_attrs = dict(frame.attrs)
    if frame.empty:
        result = frame.copy()
        result.attrs = snapshot_attrs
        return result
    missing = set(_REQUIRED).difference(frame.columns)
    if missing:
        raise ValueError(f"QMT 1m frame is missing columns: {sorted(missing)!r}")
    work = frame.loc[:, list(_REQUIRED)].copy()
    dates = pd.to_datetime(work["date"], errors="raise")
    if dates.dt.tz is None:
        raise ValueError("QMT 1m completion times must be timezone-aware")
    dates = dates.dt.tz_convert(_CN)
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("QMT 1m completion times must be unique and chronological")
    work.loc[:, "date"] = dates
    for field in ("open", "high", "low", "close", "volume"):
        work[field] = pd.to_numeric(work[field], errors="raise")
    prices = work[["open", "high", "low", "close"]]
    invalid = (
        (prices <= 0).any(axis=1)
        | (work["volume"] < 0)
        | (work["high"] < prices.max(axis=1))
        | (work["low"] > prices.min(axis=1))
    )
    if invalid.any():
        raise ValueError("QMT 1m frame contains invalid OHLCV")
    work.attrs = snapshot_attrs
    parts = tuple(
        normalize_qmt_opening_event_for_completed_minutes(
            rows.reset_index(drop=True)
        )
        for _, rows in work.groupby(dates.dt.date, sort=True)
    )
    result = (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame(columns=_REQUIRED)
    )
    result = result.loc[:, list(_REQUIRED)]
    result.attrs = snapshot_attrs
    return result


def _aggregate_intraday(one_minute: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if one_minute.empty:
        return pd.DataFrame(columns=_REQUIRED)
    # The input is already a unique chronological completed-minute stream.
    # Build every session bucket in one vectorized groupby instead of creating
    # two DataFrames and two groupby plans per trading day.
    sessions = one_minute["date"].dt.normalize()
    grouped_sessions = one_minute.groupby(sessions, sort=False)
    positions = grouped_sessions.cumcount()
    session_sizes = grouped_sessions["date"].transform("size")
    complete_mask = positions < (session_sizes // minutes * minutes)
    if not complete_mask.any():
        return pd.DataFrame(columns=_REQUIRED)
    complete = one_minute.loc[complete_mask, list(_REQUIRED)].copy()
    complete["_session"] = sessions.loc[complete_mask].array
    complete["_bucket"] = (positions.loc[complete_mask] // minutes).array
    keys = ["_session", "_bucket"]
    aggregated = complete.groupby(keys, sort=True).agg(
        date=("date", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    # QMT may carry stale prices in zero-volume minute placeholders.  Such
    # rows retain the exchange grid but must not contribute OHLC facts when a
    # bucket contains real trades.  An all-zero bucket keeps its deterministic
    # carried bar, matching the previous implementation.
    traded = complete[complete["volume"] > 0]
    if not traded.empty:
        traded_prices = traded.groupby(keys, sort=True).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
        )
        aggregated.loc[
            traded_prices.index,
            ["open", "high", "low", "close"],
        ] = traded_prices.loc[:, ["open", "high", "low", "close"]]
    return aggregated.reset_index(drop=True).loc[:, list(_REQUIRED)]


def _qmt_same_base_stream_revision(
    *,
    symbol: str,
    one_minute: pd.DataFrame,
    input_attrs: dict[str, object],
    price_basis_revision: str | None,
    issues: Sequence[QmtMinuteSessionIssue],
    source_boundary_exclusions: Sequence[QmtMinuteSourceBoundaryExclusion],
) -> str:
    """Hash normalized bars without materializing one Python dict per row."""

    metadata = sha256_json(
        {
            "schema": _QMT_SAME_BASE_STREAM_REVISION_SCHEMA,
            "symbol": symbol,
            "grid_revision": QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
            "price_basis_provider": input_attrs.get("price_basis_provider"),
            "price_basis_adjustment": input_attrs.get("price_basis_adjustment"),
            "price_basis_revision": price_basis_revision,
            "row_count": len(one_minute),
            "timestamp_encoding": "utc-nanoseconds-int64-little-endian",
            "value_columns": tuple(_REQUIRED[1:]),
            "value_encoding": "float64-little-endian-c-row-major",
            "session_issues": tuple(
                {
                    "session": value.session.isoformat(),
                    "code": value.code,
                    "observed_rows": value.observed_rows,
                    "detail": value.detail,
                }
                for value in issues
            ),
            "source_boundary_exclusions": tuple(
                value.document() for value in source_boundary_exclusions
            ),
        }
    ).encode("ascii")
    timestamps = (
        np.empty(0, dtype="<i8")
        if one_minute.empty
        else np.asarray(
            pd.DatetimeIndex(one_minute["date"]).tz_convert("UTC").asi8,
            dtype="<i8",
        )
    )
    values = np.ascontiguousarray(
        one_minute.loc[:, list(_REQUIRED[1:])].to_numpy(
            dtype=np.float64,
            copy=True,
        ),
        dtype="<f8",
    )
    # Canonical JSON treated signed zero as integer zero; retain that identity.
    values[values == 0.0] = 0.0
    digest = hashlib.sha256()
    for segment in (
        _QMT_SAME_BASE_STREAM_REVISION_SCHEMA.encode("ascii"),
        metadata,
        timestamps.tobytes(order="C"),
        values.tobytes(order="C"),
    ):
        digest.update(len(segment).to_bytes(8, byteorder="big", signed=False))
        digest.update(segment)
    return f"sha256:{digest.hexdigest()}"


def _aggregate_daily(
    one_minute: pd.DataFrame,
    complete_sessions: Sequence[date],
) -> pd.DataFrame:
    if one_minute.empty:
        return pd.DataFrame(columns=_REQUIRED)
    complete = frozenset(complete_sessions)
    rows = one_minute[one_minute["date"].dt.date.isin(complete)].copy()
    if rows.empty:
        return pd.DataFrame(columns=_REQUIRED)
    return (
        rows.groupby(rows["date"].dt.date, sort=True)
        .agg(
            date=("date", "last"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index(drop=True)
        .loc[:, list(_REQUIRED)]
    )


def _attach_lineage(
    frame: pd.DataFrame,
    *,
    input_attrs: dict[str, object],
    base_revision: str,
    derived_frequency: str,
) -> None:
    frame.attrs = {
        key: input_attrs[key]
        for key in (
            "structure_price_quantum",
            "price_basis_revision",
            "price_basis_provider",
            "price_basis_adjustment",
        )
        if key in input_attrs
    }
    frame.attrs.update(
        {
            "source_base_stream_revision": base_revision,
            "source_base_frequency": "1m",
            "derived_frequency": derived_frequency,
            "qmt_session_grid_revision": QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
        }
    )


def build_qmt_same_base_stream_frames(
    *,
    symbol: str,
    one_minute_frame: pd.DataFrame,
    decision_time: datetime,
    expected_sessions: Sequence[date] | None = None,
    evaluation_not_before: date | None = None,
) -> QmtSameBaseStreamFrames:
    """Freeze a QMT 1m prefix and derive causal 5m/30m/daily frames.

    A partial session is accepted only for the decision date and contributes
    only fully completed 5m/30m buckets.  Any incomplete historical session is
    rejected and explicitly downgrades the data grade, except for the first
    observable source session when it lies strictly before the frozen
    ``evaluation_not_before`` boundary.  That finite-cache boundary is omitted
    and cryptographically disclosed rather than masquerading as a full day.
    """

    decision = normalize_datetime(decision_time, "decision_time")
    visible = _visible_one_minute(one_minute_frame, decision_time=decision)
    normalized_visible = visible.copy()
    accepted_rows = np.zeros(len(visible), dtype=bool)
    complete_sessions: list[date] = []
    partial_session: date | None = None
    issues: list[QmtMinuteSessionIssue] = []
    source_boundary_exclusions: list[QmtMinuteSourceBoundaryExclusion] = []
    observed_sessions: set[date] = set()
    first_visible_session = (
        None if visible.empty else pd.Timestamp(visible.iloc[0]["date"]).date()
    )

    if evaluation_not_before is not None and type(evaluation_not_before) is not date:
        raise TypeError("evaluation_not_before must be an exact date")

    if visible.empty:
        session_ranges: tuple[tuple[int, int], ...] = ()
        minute_offsets = np.empty(0, dtype=np.int64)
    else:
        completion_ns = visible["date"].array.asi8
        session_ns = visible["date"].dt.normalize().array.asi8
        minute_offsets = completion_ns - session_ns
        changes = np.flatnonzero(session_ns[1:] != session_ns[:-1]) + 1
        starts = np.concatenate((np.asarray((0,)), changes))
        ends = np.concatenate((changes, np.asarray((len(visible),))))
        session_ranges = tuple(zip(starts.tolist(), ends.tolist()))

    for start, end in session_ranges:
        session = pd.Timestamp(visible.iloc[start]["date"]).date()
        observed_count = end - start
        observed_sessions.add(session)
        actual = minute_offsets[start:end]
        is_full_native = np.array_equal(actual, _NATIVE_MINUTE_OFFSETS)
        is_full_without_opening_event = np.array_equal(
            actual,
            _COMPLETED_MINUTE_OFFSETS,
        )
        is_native_prefix = (
            observed_count <= len(_NATIVE_MINUTE_OFFSETS)
            and np.array_equal(
                actual,
                _NATIVE_MINUTE_OFFSETS[:observed_count],
            )
        )
        is_completed_prefix = (
            observed_count <= len(_COMPLETED_MINUTE_OFFSETS)
            and np.array_equal(
                actual,
                _COMPLETED_MINUTE_OFFSETS[:observed_count],
            )
        )
        is_current_prefix = session == decision.date() and (
            is_native_prefix or is_completed_prefix
        )
        if not (is_full_native or is_full_without_opening_event or is_current_prefix):
            if (
                session == first_visible_session
                and evaluation_not_before is not None
                and session < evaluation_not_before
            ):
                source_boundary_exclusions.append(
                    QmtMinuteSourceBoundaryExclusion(
                        session=session,
                        observed_rows=observed_count,
                        first_observed_at=pd.Timestamp(
                            visible.iloc[start]["date"]
                        ).to_pydatetime(),
                        last_observed_at=pd.Timestamp(
                            visible.iloc[end - 1]["date"]
                        ).to_pydatetime(),
                        evaluation_not_before=evaluation_not_before,
                    )
                )
                continue
            issues.append(
                QmtMinuteSessionIssue(
                    session=session,
                    code="QMT_ONE_MINUTE_SESSION_GRID_INVALID",
                    observed_rows=observed_count,
                    detail=(
                        _INVALID_SESSION_GRID_DETAIL
                    ),
                )
            )
            continue
        has_opening_event = is_full_native or is_native_prefix
        normalized_count = observed_count - 1 if has_opening_event else observed_count
        if has_opening_event and observed_count > 1:
            accepted_rows[start + 1 : end] = True
            opening_volume = float(normalized_visible.iloc[start]["volume"])
            if opening_volume > 0:
                first_bar = start + 1
                opening = normalized_visible.iloc[start]
                normalized_visible.iat[
                    first_bar,
                    normalized_visible.columns.get_loc("open"),
                ] = opening["open"]
                normalized_visible.iat[
                    first_bar,
                    normalized_visible.columns.get_loc("high"),
                ] = max(
                    float(opening["high"]),
                    float(normalized_visible.iloc[first_bar]["high"]),
                )
                normalized_visible.iat[
                    first_bar,
                    normalized_visible.columns.get_loc("low"),
                ] = min(
                    float(opening["low"]),
                    float(normalized_visible.iloc[first_bar]["low"]),
                )
                normalized_visible.iat[
                    first_bar,
                    normalized_visible.columns.get_loc("volume"),
                ] = opening_volume + float(
                    normalized_visible.iloc[first_bar]["volume"]
                )
        elif not has_opening_event:
            accepted_rows[start:end] = True
        if normalized_count == 240:
            complete_sessions.append(session)
        else:
            partial_session = session

    if expected_sessions is None:
        calendar_blocker = FactBlocker(
            "trading_calendar",
            "QMT_ONE_MINUTE_TRADING_CALENDAR_UNRESOLVED",
            "expected_sessions was not supplied",
        )
    else:
        calendar_blocker = None
        expected = tuple(sorted(set(expected_sessions)))
        if expected and visible.empty:
            missing = expected
        elif visible.empty:
            missing = ()
        else:
            first_observed = min(observed_sessions)
            last_relevant = min(decision.date(), max(expected)) if expected else decision.date()
            missing = tuple(
                value
                for value in expected
                if first_observed <= value <= last_relevant
                and value not in observed_sessions
            )
        for session in missing:
            issues.append(
                QmtMinuteSessionIssue(
                    session=session,
                    code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
                    observed_rows=0,
                    detail=_MISSING_SESSION_DETAIL,
                )
            )

    one_minute = normalized_visible.loc[accepted_rows, list(_REQUIRED)].reset_index(
        drop=True
    )

    input_attrs = dict(one_minute_frame.attrs)
    price_basis_revision = input_attrs.get("price_basis_revision")
    price_basis_revision = (
        str(price_basis_revision)
        if isinstance(price_basis_revision, str)
        and price_basis_revision.startswith("sha256:")
        else None
    )
    blockers: list[FactBlocker] = []
    if calendar_blocker is not None:
        blockers.append(calendar_blocker)
    if price_basis_revision is None:
        blockers.append(
            FactBlocker(
                "price_basis_revision",
                "QMT_ONE_MINUTE_PRICE_BASIS_UNRESOLVED",
                str(input_attrs.get("price_basis_error_code", "missing revision")),
            )
        )
    blockers.extend(
        FactBlocker("one_minute_session", value.code, f"{value.session}: {value.detail}")
        for value in issues
    )
    if one_minute.empty:
        blockers.append(
            FactBlocker(
                "one_minute_bars",
                "QMT_ONE_MINUTE_NO_ACCEPTED_COMPLETED_BARS",
                symbol,
            )
        )

    base_revision = _qmt_same_base_stream_revision(
        symbol=symbol,
        one_minute=one_minute,
        input_attrs=input_attrs,
        price_basis_revision=price_basis_revision,
        issues=issues,
        source_boundary_exclusions=source_boundary_exclusions,
    )
    five_minute = _aggregate_intraday(one_minute, 5)
    thirty_minute = _aggregate_intraday(one_minute, 30)
    daily = _aggregate_daily(one_minute, complete_sessions)
    for frame, frequency in (
        (one_minute, "1m"),
        (five_minute, "5m"),
        (thirty_minute, "30m"),
        (daily, "d"),
    ):
        _attach_lineage(
            frame,
            input_attrs=input_attrs,
            base_revision=base_revision,
            derived_frequency=frequency,
        )

    if one_minute.empty:
        grade: QmtSameBaseGrade = "UNRESOLVED"
    elif blockers or source_boundary_exclusions:
        grade = "RESEARCH_ONLY"
    else:
        grade = "FULL_SYSTEM_ELIGIBLE"
    return QmtSameBaseStreamFrames(
        symbol=symbol,
        observed_at=decision,
        one_minute=one_minute,
        five_minute=five_minute,
        thirty_minute=thirty_minute,
        daily=daily,
        source_base_stream_revision=base_revision,
        price_basis_revision=price_basis_revision,
        complete_sessions=tuple(complete_sessions),
        partial_session=partial_session,
        session_issues=tuple(issues),
        grade=grade,
        blockers=tuple(blockers),
        source_boundary_exclusions=tuple(source_boundary_exclusions),
    )


__all__ = (
    "QMT_COMPLETED_ONE_MINUTE_GRID_REVISION",
    "QmtMinuteSessionIssue",
    "QmtMinuteSourceBoundaryExclusion",
    "QmtSameBaseStreamFrames",
    "build_qmt_same_base_stream_frames",
    "normalize_qmt_opening_event_for_completed_minutes",
    "normalize_qmt_opening_events_for_completed_minutes",
)
