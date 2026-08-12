#!/usr/bin/env python3
"""Read and normalize point-in-time data for the strict Chanlun strict strategy audit.

The module is deliberately strategy-neutral.  It does not create structural
points, selection facts, or trading signals.  It only validates cached source
rows, converts the provider's start-labelled minute rows to conservative bar
completion times, and applies dated cash-distribution adjustments forward
from their effective session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CN = ZoneInfo("Asia/Shanghai")
DEFAULT_MARKET_DATABASE = Path(
    ".cache/chanlun_available_data/financial_data_query_bars.sqlite3"
)
DEFAULT_PIT_DATABASE = Path(
    ".cache/chanlun_external_pit/etf_proxy_pit.sqlite3"
)
DEFAULT_PIT_MANIFEST = Path(
    "audit/chanlun_live_integration/external_etf_pit_manifest.json"
)
CANONICAL_ETF_CODE = "SH.510300"
PROVIDER_ETF_SYMBOL = "510300.SH"
BENCHMARK_SYMBOL = "000300.CSI"


def json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def content_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=json_default,
        )
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_cached_series(
    database: Path,
    *,
    symbol: str,
    period: str,
) -> pd.DataFrame:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        frame = pd.read_sql_query(
            """
            SELECT bar_time, begin_time, open, high, low, close,
                   previous_close, volume, amount
            FROM bars
            WHERE symbol = ? AND period = ? AND adj_type = 'S_Unsplit'
            ORDER BY bar_time
            """,
            connection,
            params=(symbol, period),
        )
    if frame.empty:
        raise RuntimeError(f"cached series is empty: {symbol}/{period}")
    parsed = pd.to_datetime(frame.pop("bar_time"), errors="raise")
    begin = pd.to_datetime(frame.pop("begin_time"), errors="raise")
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize(CN)
    else:
        parsed = parsed.dt.tz_convert(CN)
    if begin.dt.tz is None:
        begin = begin.dt.tz_localize(CN)
    else:
        begin = begin.dt.tz_convert(CN)
    frame.insert(0, "source_time", parsed)
    frame.insert(1, "source_begin_time", begin)
    for field in (
        "open",
        "high",
        "low",
        "close",
        "previous_close",
        "volume",
        "amount",
    ):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    required = ("open", "high", "low", "close", "volume")
    if frame.loc[:, list(required)].isna().any(axis=None):
        raise RuntimeError(f"cached series contains invalid OHLCV: {symbol}/{period}")
    if frame["source_time"].duplicated().any():
        raise RuntimeError(f"cached series contains duplicate timestamps: {symbol}/{period}")
    return frame.sort_values("source_time", kind="stable").reset_index(drop=True)


def _raw_session_times(session: date) -> tuple[time, ...]:
    morning = datetime.combine(session, time(9, 30))
    afternoon = datetime.combine(session, time(13, 0))
    return (
        *tuple((morning + timedelta(minutes=index)).time() for index in range(120)),
        *tuple((afternoon + timedelta(minutes=index)).time() for index in range(121)),
    )


def _completed_session_times(session: date) -> tuple[time, ...]:
    morning = datetime.combine(session, time(9, 31))
    afternoon = datetime.combine(session, time(13, 1))
    return (
        *tuple((morning + timedelta(minutes=index)).time() for index in range(120)),
        *tuple((afternoon + timedelta(minutes=index)).time() for index in range(120)),
    )


def normalize_completed_minute_sessions(
    raw: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Return only complete sessions with conservative completion timestamps.

    The cached provider rows are start-labelled: the regular rows run from
    09:30..11:29 and 13:00..14:59.  A separate 15:00 closing event may carry
    volume.  The provider also emits a deterministic zero-volume 11:30 lunch
    boundary event.  It is usually a zero-volume placeholder, but historical
    sessions can contain a real closing print.  It is therefore merged into
    the 11:29..11:30 bar rather than discarded.  Regular rows are shifted one
    minute forward; the 15:00 event is likewise merged into the final
    14:59..15:00 bar.  This produces exactly 240 unique, completed one-minute
    bars per accepted session.
    """

    required = {
        "source_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }
    missing = required.difference(raw.columns)
    if missing:
        raise ValueError(f"raw minute frame is missing columns: {sorted(missing)!r}")
    # 保持逐交易日校验明确可见，但把每个获准交易日组装成一个向量化数据帧。
    # 旧实现调用 ``iterrows``，每个交易日构建 240 个字典；在多年分钟缓存上，
    # 这个只读适配器会占据整个因果回放的大部分耗时，却不改变任何行情决策。
    output: list[pd.DataFrame] = []
    complete_sessions: list[date] = []
    rejected: list[dict[str, object]] = []
    merged_1130_boundary_events = 0
    merged_nonzero_1130_boundary_events = 0
    for session, rows in raw.groupby(raw["source_time"].dt.date, sort=True):
        ordered = rows.sort_values("source_time", kind="stable")
        lunch_mask = ordered["source_time"].dt.time == time(11, 30)
        lunch_rows = ordered[lunch_mask]
        if len(lunch_rows) > 1:
            rejected.append(
                {
                    "session": session,
                    "observed_rows": len(ordered),
                    "expected_rows": "241_OR_242",
                    "reason": "DUPLICATE_1130_BOUNDARY_EVENT",
                }
            )
            continue
        if len(lunch_rows) == 1:
            ordered = ordered[~lunch_mask]
            merged_1130_boundary_events += 1
            lunch_amount = lunch_rows.iloc[0]["amount"]
            if float(lunch_rows.iloc[0]["volume"]) != 0 or (
                not pd.isna(lunch_amount) and float(lunch_amount) != 0
            ):
                merged_nonzero_1130_boundary_events += 1
        observed = tuple(
            value.timetz().replace(tzinfo=None) for value in ordered["source_time"]
        )
        expected = _raw_session_times(session)
        if observed != expected:
            rejected.append(
                {
                    "session": session,
                    "observed_rows": len(ordered),
                    "expected_rows": len(expected),
                }
            )
            continue
        regular = ordered.iloc[:-1].copy().reset_index(drop=True)
        closing = ordered.iloc[-1]
        completed = pd.DataFrame(
            {
                "date": regular["source_time"] + pd.Timedelta(minutes=1),
                "open": pd.to_numeric(regular["open"], errors="raise").astype(float),
                "high": pd.to_numeric(regular["high"], errors="raise").astype(float),
                "low": pd.to_numeric(regular["low"], errors="raise").astype(float),
                "close": pd.to_numeric(regular["close"], errors="raise").astype(float),
                "previous_close": pd.to_numeric(
                    regular["previous_close"], errors="coerce"
                ).astype(float),
                "volume": pd.to_numeric(
                    regular["volume"], errors="raise"
                ).astype(float),
                "amount": pd.to_numeric(
                    regular["amount"], errors="coerce"
                ).fillna(0).astype(float),
                "source_row_count": 1,
                "source_first_time": regular["source_time"].to_numpy(copy=True),
                "source_last_time": regular["source_time"].to_numpy(copy=True),
            }
        )

        def merge_boundary(row_index: int, boundary: pd.Series) -> None:
            completed.at[row_index, "high"] = max(
                float(completed.at[row_index, "high"]),
                float(boundary["high"]),
            )
            completed.at[row_index, "low"] = min(
                float(completed.at[row_index, "low"]),
                float(boundary["low"]),
            )
            completed.at[row_index, "close"] = float(boundary["close"])
            completed.at[row_index, "volume"] = (
                float(completed.at[row_index, "volume"])
                + float(boundary["volume"])
            )
            boundary_amount = boundary["amount"]
            completed.at[row_index, "amount"] = (
                float(completed.at[row_index, "amount"])
                + (0.0 if pd.isna(boundary_amount) else float(boundary_amount))
            )
            completed.at[row_index, "source_row_count"] = 2
            completed.at[row_index, "source_last_time"] = boundary["source_time"]

        if len(lunch_rows) == 1:
            merge_boundary(119, lunch_rows.iloc[0])
        merge_boundary(len(completed) - 1, closing)
        output.append(completed)
        complete_sessions.append(session)
    frame = pd.concat(output, ignore_index=True) if output else pd.DataFrame()
    if not frame.empty:
        frame = frame.sort_values("date", kind="stable").reset_index(drop=True)
        if frame["date"].duplicated().any():
            raise RuntimeError("normalized minute completion times are not unique")
        for session, rows in frame.groupby(frame["date"].dt.date, sort=True):
            times = tuple(
                value.timetz().replace(tzinfo=None) for value in rows["date"]
            )
            if times != _completed_session_times(session):
                raise RuntimeError("normalized minute session grid is invalid")
    return frame, {
        "raw_rows": len(raw),
        "complete_source_sessions": len(complete_sessions),
        "completed_minute_rows": len(frame),
        "expected_regular_and_close_rows_per_session": 241,
        "accepted_source_rows_per_session": "241_OR_242_WITH_ONE_1130_EVENT",
        "expected_completed_bars_per_session": 240,
        "first_complete_session": complete_sessions[0] if complete_sessions else None,
        "last_complete_session": complete_sessions[-1] if complete_sessions else None,
        "rejected_sessions": tuple(rejected),
        "merged_1130_boundary_events": merged_1130_boundary_events,
        "merged_nonzero_1130_boundary_events": (
            merged_nonzero_1130_boundary_events
        ),
        "source_label_adapter": (
            "START_LABEL_PLUS_ONE_MINUTE_MERGE_1130_AND_1500_BOUNDARY_EVENTS"
        ),
    }


def longest_complete_interval(
    raw: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, object]]:
    normalized, coverage = normalize_completed_minute_sessions(raw)
    complete = frozenset(normalized["date"].dt.date)
    source_start = raw.iloc[0]["source_time"].date()
    source_end = raw.iloc[-1]["source_time"].date()
    benchmark_dates = benchmark[
        "source_time" if "source_time" in benchmark.columns else "date"
    ]
    expected = tuple(
        sorted(
            {
                pd.Timestamp(value).date()
                for value in benchmark_dates
                if source_start <= pd.Timestamp(value).date() <= source_end
            }
        )
    )
    runs: list[tuple[date, date, int]] = []
    current: list[date] = []
    for session in expected:
        if session in complete:
            current.append(session)
        elif current:
            runs.append((current[0], current[-1], len(current)))
            current = []
    if current:
        runs.append((current[0], current[-1], len(current)))
    if not runs:
        raise RuntimeError("no complete normalized minute interval is available")
    start, end, sessions = max(
        runs,
        key=lambda value: (value[2], -value[0].toordinal()),
    )
    selected = normalized[
        normalized["date"].dt.date.map(lambda value: start <= value <= end)
    ].copy().reset_index(drop=True)
    expected_selected = {value for value in expected if start <= value <= end}
    if set(selected["date"].dt.date) != expected_selected:
        raise RuntimeError("longest minute interval is not exchange-calendar complete")
    return selected, {
        "selection_rule": (
            "LONGEST_CONTIGUOUS_EXCHANGE_CALENDAR_INTERVAL_WITH_"
            "240_NORMALIZED_COMPLETED_MINUTE_BARS"
        ),
        "start": start,
        "end": end,
        "sessions": sessions,
        "all_complete_runs": tuple(
            {"start": left, "end": right, "sessions": size}
            for left, right, size in runs
        ),
        "normalization": coverage,
    }


@dataclass(frozen=True, slots=True)
class DistributionEvent:
    ex_date: date
    cash_per_share: Decimal
    cumulative_cash_per_share: Decimal
    source: str


def load_distributions(
    database: Path,
    *,
    symbol: str = CANONICAL_ETF_CODE,
) -> tuple[DistributionEvent, ...]:
    with sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True
    ) as connection:
        rows = connection.execute(
            """
            SELECT ex_date, cash_per_share, cumulative_cash_per_share, source
            FROM etf_distributions
            WHERE symbol=?
            ORDER BY ex_date
            """,
            (symbol,),
        ).fetchall()
    output = tuple(
        DistributionEvent(
            ex_date=date.fromisoformat(row[0]),
            cash_per_share=Decimal(row[1]),
            cumulative_cash_per_share=Decimal(row[2]),
            source=row[3],
        )
        for row in rows
    )
    if not output:
        raise RuntimeError(f"ETF distribution ledger is empty: {symbol}")
    return output


def causal_adjustment_ledger(
    one_minute: pd.DataFrame,
    distributions: Sequence[DistributionEvent],
) -> tuple[dict[str, object], ...]:
    session_close = {
        session: Decimal(str(rows.sort_values("date", kind="stable").iloc[-1]["close"]))
        for session, rows in one_minute.groupby(one_minute["date"].dt.date, sort=True)
    }
    sessions = tuple(sorted(session_close))
    previous = {
        sessions[index]: session_close[sessions[index - 1]]
        for index in range(1, len(sessions))
    }
    ledger: list[dict[str, object]] = []
    cumulative = Decimal("1")
    for event in distributions:
        reference = previous.get(event.ex_date)
        if reference is None:
            continue
        denominator = reference - event.cash_per_share
        if denominator <= 0:
            raise RuntimeError("ETF cash distribution produces an invalid divisor")
        multiplier = reference / denominator
        cumulative *= multiplier
        ledger.append(
            {
                "ex_date": event.ex_date,
                "cash_per_share": event.cash_per_share,
                "previous_raw_close": reference,
                "causal_forward_multiplier": multiplier,
                "cumulative_forward_multiplier": cumulative,
                "known_no_later_than": datetime.combine(
                    event.ex_date,
                    time(9, 30),
                    tzinfo=CN,
                ),
                "source": event.source,
            }
        )
    return tuple(ledger)


def apply_causal_forward_adjustments(
    frame: pd.DataFrame,
    ledger: Sequence[Mapping[str, object]],
) -> pd.DataFrame:
    adjusted = frame.copy()
    for event in ledger:
        effective_on = event["ex_date"]
        if not isinstance(effective_on, date):
            raise TypeError("adjustment effective date is invalid")
        multiplier = Decimal(str(event["causal_forward_multiplier"]))
        mask = adjusted["date"].dt.date >= effective_on
        for field in ("open", "high", "low", "close"):
            adjusted.loc[mask, field] = adjusted.loc[mask, field] * float(multiplier)
    return adjusted


def aggregate_completed_bars(
    one_minute: pd.DataFrame,
    *,
    minutes: int,
) -> pd.DataFrame:
    if minutes not in {5, 30}:
        raise ValueError("strict strategy intraday aggregation only permits 5 or 30 minutes")
    if one_minute.empty:
        return pd.DataFrame(
            columns=("date", "open", "high", "low", "close", "volume")
        )
    ordered = one_minute.sort_values("date", kind="stable").reset_index(drop=True)
    sessions = ordered["date"].dt.normalize()
    positions = ordered.groupby(sessions, sort=False).cumcount()
    counts = ordered.groupby(sessions, sort=False)["date"].transform("size")
    expected_minute = positions + (9 * 60 + 31)
    afternoon = positions >= 120
    expected_minute.loc[afternoon] = (
        positions.loc[afternoon] - 120 + (13 * 60 + 1)
    )
    actual_minute = ordered["date"].dt.hour * 60 + ordered["date"].dt.minute
    if (
        (counts != 240).any()
        or (actual_minute != expected_minute).any()
        or ordered["date"].dt.second.ne(0).any()
    ):
        raise ValueError("cannot aggregate an incomplete normalized session")

    bucket = positions // minutes
    aggregated = (
        ordered.groupby([sessions, bucket], sort=True, observed=True)
        .agg(
            date=("date", "last"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index(drop=True)
    )
    for field in ("open", "high", "low", "close", "volume"):
        aggregated[field] = pd.to_numeric(
            aggregated[field], errors="raise"
        ).astype(float)
    return aggregated.sort_values("date", kind="stable").reset_index(drop=True)


__all__ = [
    "BENCHMARK_SYMBOL",
    "CANONICAL_ETF_CODE",
    "CN",
    "DEFAULT_MARKET_DATABASE",
    "DEFAULT_PIT_DATABASE",
    "DEFAULT_PIT_MANIFEST",
    "DistributionEvent",
    "PROVIDER_ETF_SYMBOL",
    "aggregate_completed_bars",
    "apply_causal_forward_adjustments",
    "atomic_json",
    "causal_adjustment_ledger",
    "content_sha256",
    "json_default",
    "load_distributions",
    "longest_complete_interval",
    "normalize_completed_minute_sessions",
    "read_cached_series",
    "sha256_file",
]
