#!/usr/bin/env python3
"""Capture point-in-time data used by the external ETF research replay.

The cache deliberately keeps raw observations.  Adjusted prices are derived
later from the dated adjustment ledger so a future action can never rewrite an
older decision snapshot.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CN = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = Path(
    ".cache/chanlun_external_pit/etf_proxy_pit.sqlite3"
)
DEFAULT_MANIFEST = Path(
    "audit/chanlun_live_integration/external_etf_pit_manifest.json"
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    result.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    result.add_argument("--start", type=_date, default=date(2017, 1, 1))
    result.add_argument("--end", type=_date, default=date(2026, 7, 24))
    result.add_argument(
        "--candidate-date",
        action="append",
        type=_date,
        required=True,
        help="repeat for each precomputed ETF technical-candidate session",
    )
    result.add_argument("--force", action="store_true")
    return result


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS memberships (
            candidate_session TEXT NOT NULL,
            source_update_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (candidate_session, code)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS security_master (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            ipo_date TEXT NOT NULL,
            out_date TEXT,
            security_type TEXT NOT NULL,
            status TEXT NOT NULL,
            queried_at TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS daily_bars (
            code TEXT NOT NULL,
            session TEXT NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            previous_close TEXT NOT NULL,
            volume TEXT NOT NULL,
            amount TEXT NOT NULL,
            trade_status TEXT NOT NULL,
            is_st TEXT NOT NULL,
            PRIMARY KEY (code, session)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS adjustment_factors (
            code TEXT NOT NULL,
            effective_on TEXT NOT NULL,
            forward_factor TEXT NOT NULL,
            backward_factor TEXT NOT NULL,
            adjustment_factor TEXT NOT NULL,
            PRIMARY KEY (code, effective_on)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS etf_distributions (
            symbol TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            cumulative_cash_per_share TEXT NOT NULL,
            cash_per_share TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (symbol, ex_date)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS trading_calendar (
            calendar_date TEXT PRIMARY KEY,
            is_trading_day TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS query_log (
            query_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            queried_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            result_sha256 TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )


def _rows(result: object, *, operation: str) -> tuple[tuple[str, ...], ...]:
    error_code = str(getattr(result, "error_code", ""))
    if error_code != "0":
        raise RuntimeError(
            f"BaoStock {operation} failed: "
            f"{error_code} {getattr(result, 'error_msg', '')}"
        )
    output: list[tuple[str, ...]] = []
    while result.next():
        output.append(tuple(str(value) for value in result.get_row_data()))
    return tuple(output)


def _hash_rows(rows: Sequence[Sequence[str]]) -> str:
    payload = json.dumps(
        rows, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _query_id(
    provider: str,
    operation: str,
    parameters: dict[str, object],
) -> str:
    payload = json.dumps(
        (provider, operation, parameters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _record_query(
    connection: sqlite3.Connection,
    *,
    provider: str,
    operation: str,
    parameters: dict[str, object],
    queried_at: datetime,
    rows: Sequence[Sequence[str]],
) -> None:
    encoded = json.dumps(
        parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO query_log
        (query_id, provider, operation, parameters_json, queried_at,
         row_count, result_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _query_id(provider, operation, parameters),
            provider,
            operation,
            encoded,
            queried_at.isoformat(),
            len(rows),
            _hash_rows(rows),
        ),
    )


def _membership(
    connection: sqlite3.Connection,
    bs: object,
    candidate: date,
    *,
    queried_at: datetime,
    force: bool,
) -> tuple[str, ...]:
    key = candidate.isoformat()
    cached = tuple(
        row[0]
        for row in connection.execute(
            "SELECT code FROM memberships WHERE candidate_session=? ORDER BY code",
            (key,),
        )
    )
    if cached and not force:
        return cached
    result = bs.query_hs300_stocks(key)
    rows = _rows(result, operation="query_hs300_stocks")
    if len(rows) != 300 or any(len(row) != 3 for row in rows):
        raise RuntimeError(f"HS300 point-in-time basket is incomplete at {key}")
    update_dates = {row[0] for row in rows}
    if len(update_dates) != 1 or next(iter(update_dates)) > key:
        raise RuntimeError(f"HS300 source update date is invalid at {key}")
    connection.execute(
        "DELETE FROM memberships WHERE candidate_session=?", (key,)
    )
    connection.executemany(
        """
        INSERT INTO memberships
        (candidate_session, source_update_date, code, name)
        VALUES (?, ?, ?, ?)
        """,
        ((key, row[0], row[1], row[2]) for row in rows),
    )
    _record_query(
        connection,
        provider="BaoStock",
        operation="query_hs300_stocks",
        parameters={"date": key},
        queried_at=queried_at,
        rows=rows,
    )
    connection.commit()
    return tuple(sorted(row[1] for row in rows))


def _symbol_complete(
    connection: sqlite3.Connection,
    code: str,
    *,
    start: date,
    end: date,
) -> bool:
    master = connection.execute(
        "SELECT ipo_date, out_date FROM security_master WHERE code=?", (code,)
    ).fetchone()
    if master is None:
        return False
    bar_range = connection.execute(
        "SELECT MIN(session), MAX(session), COUNT(*) FROM daily_bars WHERE code=?",
        (code,),
    ).fetchone()
    if bar_range is None or int(bar_range[2]) == 0:
        return False
    expected_start = max(start, date.fromisoformat(master[0]))
    expected_end = min(
        end,
        date.fromisoformat(master[1]) if master[1] else end,
    )
    # The first/last calendar date can be a non-session.  A seven-day bound is
    # enough to prove the requested range was queried without requiring a
    # future-filled exchange calendar.
    return (
        date.fromisoformat(bar_range[0]).toordinal()
        <= expected_start.toordinal() + 7
        and date.fromisoformat(bar_range[1]).toordinal()
        >= expected_end.toordinal() - 7
    )


def _capture_symbol(
    connection: sqlite3.Connection,
    bs: object,
    code: str,
    *,
    start: date,
    end: date,
    queried_at: datetime,
    force: bool,
) -> None:
    if not force and _symbol_complete(connection, code, start=start, end=end):
        return

    basic_parameters = {"code": code}
    basic_rows = _rows(
        bs.query_stock_basic(code=code), operation="query_stock_basic"
    )
    if len(basic_rows) != 1 or len(basic_rows[0]) != 6:
        raise RuntimeError(f"security master is unavailable for {code}")
    basic = basic_rows[0]
    if basic[0] != code or not basic[2]:
        raise RuntimeError(f"security master identity is invalid for {code}")
    connection.execute(
        """
        INSERT OR REPLACE INTO security_master
        (code, name, ipo_date, out_date, security_type, status, queried_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (*basic, queried_at.isoformat()),
    )
    _record_query(
        connection,
        provider="BaoStock",
        operation="query_stock_basic",
        parameters=basic_parameters,
        queried_at=queried_at,
        rows=basic_rows,
    )

    fields = (
        "date,code,open,high,low,close,preclose,volume,amount,"
        "tradestatus,isST"
    )
    bar_parameters = {
        "code": code,
        "fields": fields,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "frequency": "d",
        "adjustflag": "3",
    }
    bar_rows = _rows(
        bs.query_history_k_data_plus(**bar_parameters),
        operation="query_history_k_data_plus",
    )
    if any(len(row) != 11 or row[1] != code for row in bar_rows):
        raise RuntimeError(f"daily bar identity is invalid for {code}")
    connection.execute("DELETE FROM daily_bars WHERE code=?", (code,))
    connection.executemany(
        """
        INSERT INTO daily_bars
        (session, code, open, high, low, close, previous_close, volume,
         amount, trade_status, is_st)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bar_rows,
    )
    _record_query(
        connection,
        provider="BaoStock",
        operation="query_history_k_data_plus",
        parameters=bar_parameters,
        queried_at=queried_at,
        rows=bar_rows,
    )

    factor_parameters = {
        "code": code,
        "start_date": "1990-01-01",
        "end_date": end.isoformat(),
    }
    factor_rows = _rows(
        bs.query_adjust_factor(**factor_parameters),
        operation="query_adjust_factor",
    )
    if any(len(row) != 5 or row[0] != code for row in factor_rows):
        raise RuntimeError(f"adjustment-factor identity is invalid for {code}")
    connection.execute("DELETE FROM adjustment_factors WHERE code=?", (code,))
    connection.executemany(
        """
        INSERT INTO adjustment_factors
        (code, effective_on, forward_factor, backward_factor,
         adjustment_factor)
        VALUES (?, ?, ?, ?, ?)
        """,
        factor_rows,
    )
    _record_query(
        connection,
        provider="BaoStock",
        operation="query_adjust_factor",
        parameters=factor_parameters,
        queried_at=queried_at,
        rows=factor_rows,
    )
    connection.commit()


def _capture_etf_distributions(
    connection: sqlite3.Connection,
    *,
    queried_at: datetime,
) -> None:
    import akshare as ak

    frame = ak.fund_etf_dividend_sina(symbol="sh510300")
    required = {"日期", "累计分红"}
    if set(frame.columns) != required or frame.empty:
        raise RuntimeError("AKShare ETF distribution ledger is unavailable")
    rows: list[tuple[str, str, str, str, str]] = []
    previous = 0.0
    for item in frame.sort_values("日期").itertuples(index=False, name=None):
        ex_date = str(item[0])[:10]
        cumulative = float(item[1])
        cash = cumulative - previous
        if cash <= 0:
            raise RuntimeError("ETF cumulative distribution ledger is not monotonic")
        rows.append(
            (
                "SH.510300",
                ex_date,
                format(cumulative, ".6f"),
                format(cash, ".6f"),
                "AKShare:fund_etf_dividend_sina:sh510300",
            )
        )
        previous = cumulative
    connection.execute(
        "DELETE FROM etf_distributions WHERE symbol='SH.510300'"
    )
    connection.executemany(
        """
        INSERT INTO etf_distributions
        (symbol, ex_date, cumulative_cash_per_share, cash_per_share, source)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    _record_query(
        connection,
        provider="AKShare/Sina",
        operation="fund_etf_dividend_sina",
        parameters={"symbol": "sh510300"},
        queried_at=queried_at,
        rows=rows,
    )
    connection.commit()


def _capture_trading_calendar(
    connection: sqlite3.Connection,
    bs: object,
    *,
    start: date,
    end: date,
    queried_at: datetime,
) -> None:
    parameters = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    rows = _rows(
        bs.query_trade_dates(**parameters),
        operation="query_trade_dates",
    )
    if not rows or any(len(row) != 2 for row in rows):
        raise RuntimeError("BaoStock trading calendar is unavailable")
    connection.execute("DELETE FROM trading_calendar")
    connection.executemany(
        """
        INSERT INTO trading_calendar (calendar_date, is_trading_day)
        VALUES (?, ?)
        """,
        rows,
    )
    _record_query(
        connection,
        provider="BaoStock",
        operation="query_trade_dates",
        parameters=parameters,
        queried_at=queried_at,
        rows=rows,
    )
    connection.commit()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "memberships",
            "security_master",
            "daily_bars",
            "adjustment_factors",
            "etf_distributions",
            "trading_calendar",
            "query_log",
        )
    }


def capture(
    *,
    database: Path,
    manifest: Path,
    start: date,
    end: date,
    candidate_dates: Iterable[date],
    force: bool,
) -> dict[str, object]:
    candidates = tuple(sorted(set(candidate_dates)))
    if not candidates or candidates[0] < start or candidates[-1] > end:
        raise ValueError("candidate dates must fall inside the requested range")
    database.parent.mkdir(parents=True, exist_ok=True)
    queried_at = datetime.now(CN)

    import baostock as bs

    login = bs.login()
    if str(login.error_code) != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        with sqlite3.connect(database) as connection:
            _schema(connection)
            members: set[str] = set()
            for candidate in candidates:
                members.update(
                    _membership(
                        connection,
                        bs,
                        candidate,
                        queried_at=queried_at,
                        force=force,
                    )
                )
            for index, code in enumerate(sorted(members), start=1):
                # BaoStock sessions can expire during a long all-member
                # snapshot.  Refresh at deterministic boundaries; completed
                # symbols are already committed and remain immutable cache
                # inputs on a resumed run.
                if index > 1 and (index - 1) % 100 == 0:
                    bs.logout()
                    refreshed = bs.login()
                    if str(refreshed.error_code) != "0":
                        raise RuntimeError(
                            f"BaoStock session refresh failed: {refreshed.error_msg}"
                        )
                _capture_symbol(
                    connection,
                    bs,
                    code,
                    start=start,
                    end=end,
                    queried_at=queried_at,
                    force=force,
                )
                if index % 25 == 0:
                    print(f"captured {index}/{len(members)} basket members", flush=True)
            _capture_trading_calendar(
                connection,
                bs,
                start=start,
                end=end,
                queried_at=queried_at,
            )
            _capture_etf_distributions(connection, queried_at=queried_at)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            counts = _counts(connection)
    finally:
        bs.logout()

    payload: dict[str, object] = {
        "schema": "chanlun-external-etf-pit-snapshot",
        "captured_at": queried_at.isoformat(),
        "selection_path": "ETF_PROXY",
        "etf_symbol": "SH.510300",
        "tracked_index": "CSI.000300",
        "source_start": start.isoformat(),
        "source_end": end.isoformat(),
        "candidate_dates": [value.isoformat() for value in candidates],
        "candidate_dates_frozen_before_external_membership_queries": True,
        "sources": {
            "point_in_time_membership": "BaoStock.query_hs300_stocks",
            "member_master": "BaoStock.query_stock_basic",
            "member_daily_raw_bars": "BaoStock.query_history_k_data_plus(adjustflag=3)",
            "member_adjustment_factors": "BaoStock.query_adjust_factor",
            "trading_calendar": "BaoStock.query_trade_dates",
            "etf_distributions": "AKShare.fund_etf_dividend_sina",
        },
        "counts": counts,
        "database": str(database.resolve()),
        "database_sha256": _file_hash(database),
        "live_status": "LIVE_DISABLED",
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["content_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    _atomic_json(manifest, payload)
    return payload


def main() -> int:
    arguments = parser().parse_args()
    payload = capture(
        database=arguments.database,
        manifest=arguments.manifest,
        start=arguments.start,
        end=arguments.end,
        candidate_dates=arguments.candidate_date,
        force=arguments.force,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
