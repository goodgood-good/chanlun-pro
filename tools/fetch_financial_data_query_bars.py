from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Iterable, Sequence


KLINE_URL = "/api/quote/kline-batch"
EXPECTED_FIELDS = (
    "symbol",
    "period",
    "adj_type",
    "date",
    "begin_date",
    "open",
    "high",
    "low",
    "close",
    "previous_close",
    "volume",
    "amount",
)


@dataclass(frozen=True, slots=True)
class QueryWindow:
    symbol: str
    period: str
    split: str
    start_at: datetime
    end_at: datetime

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.symbol,
            self.period,
            self.split,
            self.start_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.end_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def request(self) -> dict[str, object]:
        return {
            "url": KLINE_URL,
            "params": {
                "symbols": [self.symbol],
                "period": self.period,
                "split": self.split,
                "start_date": self.start_at.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": self.end_at.strftime("%Y-%m-%d %H:%M:%S"),
                "count": 1000,
            },
        }


def natural_day_windows(
    *,
    symbol: str,
    period: str,
    split: str,
    start: date,
    end: date,
    maximum_days: int,
) -> tuple[QueryWindow, ...]:
    if maximum_days <= 0:
        raise ValueError("maximum_days must be positive")
    if start > end:
        raise ValueError("start cannot follow end")
    output: list[QueryWindow] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=maximum_days - 1))
        output.append(
            QueryWindow(
                symbol=symbol,
                period=period,
                split=split,
                start_at=datetime.combine(cursor, time.min),
                end_at=datetime.combine(window_end, time(23, 59, 59)),
            )
        )
        cursor = window_end + timedelta(days=1)
    return tuple(output)


def _chunks(values: Sequence[QueryWindow], size: int) -> Iterable[Sequence[QueryWindow]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            adj_type TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            begin_time TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            previous_close REAL,
            volume REAL NOT NULL,
            amount REAL,
            PRIMARY KEY (symbol, period, adj_type, bar_time)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS query_windows (
            symbol TEXT NOT NULL,
            period TEXT NOT NULL,
            split TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            issues_json TEXT NOT NULL,
            queried_at TEXT NOT NULL,
            PRIMARY KEY (symbol, period, split, start_at, end_at)
        ) WITHOUT ROWID
        """
    )
    connection.commit()
    return connection


def _completed_keys(connection: sqlite3.Connection) -> set[tuple[str, ...]]:
    rows = connection.execute(
        """
        SELECT symbol, period, split, start_at, end_at
        FROM query_windows
        WHERE status = 'SUCCESS'
        """
    )
    return {tuple(str(value) for value in row) for row in rows}


def _query_batch(query_script: Path, windows: Sequence[QueryWindow]) -> dict[str, object]:
    if not os.environ.get("FINANCIAL_DATA_API_KEY", "").strip():
        raise RuntimeError("FINANCIAL_DATA_API_KEY is unavailable")
    payload = json.dumps(
        [window.request() for window in windows],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    process = subprocess.run(
        [sys.executable, str(query_script), payload],
        check=False,
        capture_output=True,
        timeout=330,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"financial-data query process failed with exit {process.returncode}"
        )
    output: str | None = None
    for encoding in ("utf-8", "gb18030"):
        try:
            output = process.stdout.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if output is None:
        raise RuntimeError(
            "financial-data query output is neither UTF-8 nor GB18030"
        )
    try:
        response = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("financial-data query returned invalid JSON") from exc
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        issues = response.get("issues") if isinstance(response, dict) else None
        raise RuntimeError(f"financial-data query failed: {issues!r}")
    return response


def _store_result(
    connection: sqlite3.Connection,
    *,
    window: QueryWindow,
    result: object,
) -> int:
    if not isinstance(result, dict):
        raise RuntimeError("query result must be an object")
    meta = result.get("meta")
    fields = tuple(meta.get("fields", ())) if isinstance(meta, dict) else ()
    if fields != EXPECTED_FIELDS:
        raise RuntimeError(f"unexpected kline fields: {fields!r}")
    data = result.get("data")
    if not isinstance(data, list):
        raise RuntimeError("kline result data must be a list")
    records: list[tuple[object, ...]] = []
    for raw in data:
        if not isinstance(raw, list) or len(raw) != len(EXPECTED_FIELDS):
            raise RuntimeError("malformed kline row")
        row = dict(zip(EXPECTED_FIELDS, raw, strict=True))
        if (
            row["symbol"] != window.symbol
            or row["period"] != window.period
            or row["adj_type"] != window.split
        ):
            raise RuntimeError("kline row does not match requested identity")
        records.append(tuple(row[field] for field in EXPECTED_FIELDS))
    connection.executemany(
        """
        INSERT INTO bars (
            symbol, period, adj_type, bar_time, begin_time,
            open, high, low, close, previous_close, volume, amount
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, period, adj_type, bar_time) DO UPDATE SET
            begin_time=excluded.begin_time,
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            previous_close=excluded.previous_close,
            volume=excluded.volume,
            amount=excluded.amount
        """,
        records,
    )
    issues = result.get("issues", [])
    connection.execute(
        """
        INSERT INTO query_windows (
            symbol, period, split, start_at, end_at,
            status, row_count, issues_json, queried_at
        ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?)
        ON CONFLICT(symbol, period, split, start_at, end_at) DO UPDATE SET
            status=excluded.status,
            row_count=excluded.row_count,
            issues_json=excluded.issues_json,
            queried_at=excluded.queried_at
        """,
        (
            *window.key,
            len(records),
            json.dumps(issues, ensure_ascii=False, sort_keys=True),
            datetime.now().astimezone().isoformat(),
        ),
    )
    return len(records)


def acquire(
    *,
    database: Path,
    query_script: Path,
    windows: Sequence[QueryWindow],
    batch_size: int,
) -> dict[str, object]:
    connection = _connect(database)
    queried = 0
    returned_rows = 0
    try:
        complete = _completed_keys(connection)
        pending = tuple(window for window in windows if window.key not in complete)
        total_batches = (len(pending) + batch_size - 1) // batch_size
        for batch_number, batch in enumerate(_chunks(pending, batch_size), start=1):
            response = _query_batch(query_script, batch)
            results = response.get("results")
            if not isinstance(results, list) or len(results) != len(batch):
                raise RuntimeError("query results are not aligned to requests")
            for window, result in zip(batch, results, strict=True):
                returned_rows += _store_result(
                    connection,
                    window=window,
                    result=result,
                )
            connection.commit()
            queried += len(batch)
            print(
                json.dumps(
                    {
                        "batch": batch_number,
                        "batches": total_batches,
                        "windows_completed": queried,
                        "windows_pending_at_start": len(pending),
                        "rows_this_run": returned_rows,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        counts = connection.execute(
            """
            SELECT symbol, period, adj_type, COUNT(*), MIN(bar_time), MAX(bar_time)
            FROM bars
            GROUP BY symbol, period, adj_type
            ORDER BY symbol, period, adj_type
            """
        ).fetchall()
        window_counts = connection.execute(
            """
            SELECT status, COUNT(*) FROM query_windows GROUP BY status ORDER BY status
            """
        ).fetchall()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return {
        "schema": "chanlun-financial-data-query-cache",
        "database": str(database.resolve()),
        "database_sha256": _sha256_file(database),
        "queried_windows_this_run": queried,
        "returned_rows_this_run": returned_rows,
        "series": [
            {
                "symbol": row[0],
                "period": row[1],
                "adj_type": row[2],
                "rows": row[3],
                "first": row[4],
                "last": row[5],
            }
            for row in counts
        ],
        "window_status_counts": dict(window_counts),
        "credential_persisted": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cache kline data returned by the financial-data-query skill."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--query-script", type=Path, required=True)
    parser.add_argument("--etf-symbol", required=True)
    parser.add_argument("--benchmark-symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--summary", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.query_script.is_file():
        raise FileNotFoundError(args.query_script)
    windows = (
        *natural_day_windows(
            symbol=args.etf_symbol,
            period="P_Min1",
            split="S_Unsplit",
            start=args.start,
            end=args.end,
            maximum_days=2,
        ),
        *natural_day_windows(
            symbol=args.benchmark_symbol,
            period="P_Day1",
            split="S_Unsplit",
            start=args.start,
            end=args.end,
            maximum_days=365,
        ),
    )
    summary = acquire(
        database=args.database,
        query_script=args.query_script,
        windows=windows,
        batch_size=args.batch_size,
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.summary is not None:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
