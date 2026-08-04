#!/usr/bin/env python3
"""Build one immutable research cache for the fixed CSI300 broad-ETF pool."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for value in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from tools.chanlun_v3_research_data import (
    CN,
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_UNIVERSE = Path(
    "audit/chanlun_live_integration/csi300_broad_etf_universe_v1.json"
)
DEFAULT_TARGET = Path(
    ".cache/chanlun_v31_csi300_broad_pool/financial_data_query_bars.sqlite3"
)
DEFAULT_MANIFEST = Path(
    "audit/chanlun_live_integration/"
    "v31_csi300_broad_pool_market_database_manifest.json"
)
DEFAULT_SOURCES = {
    "159919.SZ": Path(
        ".cache/chanlun_v31_159919/financial_data_query_bars.sqlite3"
    ),
    "159925.SZ": Path(
        ".cache/chanlun_v31_159925/financial_data_query_bars.sqlite3"
    ),
    "510300.SH": Path(
        ".cache/chanlun_v3_available_data/financial_data_query_bars.sqlite3"
    ),
    "510310.SH": Path(
        ".cache/chanlun_v31_csi300_etfs/financial_data_query_bars.sqlite3"
    ),
    "510330.SH": Path(
        ".cache/chanlun_v31_510330/financial_data_query_bars.sqlite3"
    ),
    "510360.SH": Path(
        ".cache/chanlun_v31_510360/financial_data_query_bars.sqlite3"
    ),
    "510380.SH": Path(
        ".cache/chanlun_v31_510380/financial_data_query_bars.sqlite3"
    ),
    "510390.SH": Path(
        ".cache/chanlun_v31_510390/financial_data_query_bars.sqlite3"
    ),
}
BENCHMARK_SYMBOL = "000300.CSI"
BENCHMARK_SOURCE_SYMBOL = "510300.SH"


@dataclass(frozen=True, slots=True)
class SeriesSource:
    symbol: str
    period: str
    adj_type: str
    database: Path


BAR_SCHEMA = """
CREATE TABLE bars (
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

QUERY_WINDOW_SCHEMA = """
CREATE TABLE query_windows (
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


def _read_universe(path: Path) -> tuple[dict[str, object], tuple[str, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "chanlun-csi300-broad-etf-universe/v1":
        raise RuntimeError("CSI300 broad-ETF universe schema is invalid")
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    if payload.get("content_sha256") != content_sha256(stable):
        raise RuntimeError("CSI300 broad-ETF universe content hash is invalid")
    symbols = tuple(str(item["symbol"]) for item in payload["instruments"])
    if len(symbols) != 8 or len(set(symbols)) != len(symbols):
        raise RuntimeError("CSI300 broad-ETF universe must contain eight identities")
    return payload, symbols


def _series_stats(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    period: str,
    adj_type: str,
) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT COUNT(*), MIN(bar_time), MAX(bar_time)
        FROM bars
        WHERE symbol=? AND period=? AND adj_type=?
        """,
        (symbol, period, adj_type),
    ).fetchone()
    if row is None or int(row[0]) <= 0:
        raise RuntimeError(f"source series is empty: {symbol}/{period}/{adj_type}")
    return {"rows": int(row[0]), "first": str(row[1]), "last": str(row[2])}


def _copy_series(
    target: sqlite3.Connection,
    source: SeriesSource,
) -> tuple[dict[str, object], int, int]:
    uri = f"file:{source.database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as origin:
        expected = _series_stats(
            origin,
            symbol=source.symbol,
            period=source.period,
            adj_type=source.adj_type,
        )
        before = target.total_changes
        cursor = origin.execute(
            """
            SELECT symbol, period, adj_type, bar_time, begin_time,
                   open, high, low, close, previous_close, volume, amount
            FROM bars
            WHERE symbol=? AND period=? AND adj_type=?
            ORDER BY bar_time
            """,
            (source.symbol, source.period, source.adj_type),
        )
        attempted = 0
        while True:
            rows = cursor.fetchmany(10_000)
            if not rows:
                break
            attempted += len(rows)
            target.executemany(
                "INSERT OR IGNORE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        inserted = target.total_changes - before

        tables = {
            str(row[0])
            for row in origin.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "query_windows" in tables:
            windows = origin.execute(
                """
                SELECT symbol, period, split, start_at, end_at, status,
                       row_count, issues_json, queried_at
                FROM query_windows
                WHERE symbol=? AND period=? AND split=?
                ORDER BY start_at, end_at
                """,
                (source.symbol, source.period, source.adj_type),
            ).fetchall()
            target.executemany(
                """
                INSERT OR IGNORE INTO query_windows
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                windows,
            )
    return expected, attempted, inserted


def merge_pool_cache(
    *,
    target_path: Path,
    series: Iterable[SeriesSource],
) -> tuple[dict[str, object], ...]:
    """Create a new cache; existing targets are never replaced or mutated."""

    target = target_path.resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing pool cache: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise FileExistsError(f"temporary pool cache already exists: {temporary}")

    reports: list[dict[str, object]] = []
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA auto_vacuum=NONE")
            connection.execute(BAR_SCHEMA)
            connection.execute(QUERY_WINDOW_SCHEMA)
            for item in series:
                expected, attempted, inserted = _copy_series(connection, item)
                connection.commit()
                actual = _series_stats(
                    connection,
                    symbol=item.symbol,
                    period=item.period,
                    adj_type=item.adj_type,
                )
                if actual != expected:
                    raise RuntimeError(
                        f"merged series differs from source: {item.symbol}/{item.period}"
                    )
                reports.append(
                    {
                        "symbol": item.symbol,
                        "period": item.period,
                        "adj_type": item.adj_type,
                        "source_database": str(item.database.resolve()),
                        "source_database_sha256": sha256_file(item.database),
                        "source_stats": expected,
                        "target_stats": actual,
                        "attempted_rows": attempted,
                        "inserted_rows": inserted,
                        "duplicate_primary_keys_dropped": attempted - inserted,
                    }
                )
            connection.execute("VACUUM")
        finally:
            connection.close()
        os.replace(temporary, target)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    return tuple(reports)


def _parse_source(value: str) -> tuple[str, Path]:
    symbol, separator, raw_path = value.partition("=")
    if not separator or not symbol or not raw_path:
        raise argparse.ArgumentTypeError("source must be SYMBOL=DATABASE")
    return symbol.strip().upper(), Path(raw_path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge the fixed eight-symbol CSI300 broad-ETF research cache."
    )
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", action="append", type=_parse_source)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    universe, symbols = _read_universe(args.universe)
    sources = dict(args.source) if args.source else dict(DEFAULT_SOURCES)
    if set(sources) != set(symbols):
        raise RuntimeError("source identities do not exactly match the frozen universe")
    for path in sources.values():
        if not path.is_file():
            raise FileNotFoundError(path)

    series = tuple(
        SeriesSource(symbol, "P_Min1", "S_Unsplit", sources[symbol])
        for symbol in symbols
    ) + (
        SeriesSource(
            BENCHMARK_SYMBOL,
            "P_Day1",
            "S_Unsplit",
            sources[BENCHMARK_SOURCE_SYMBOL],
        ),
    )
    reports = merge_pool_cache(target_path=args.target, series=series)
    manifest: dict[str, object] = {
        "schema": "chanlun-v31-csi300-broad-pool-cache/v1",
        "generated_at": datetime.now(CN),
        "universe_artifact": str(args.universe.resolve()),
        "universe_artifact_sha256": sha256_file(args.universe),
        "universe_content_sha256": universe["content_sha256"],
        "universe_symbols": symbols,
        "benchmark_symbol": BENCHMARK_SYMBOL,
        "benchmark_source_symbol": BENCHMARK_SOURCE_SYMBOL,
        "target_database": str(args.target.resolve()),
        "target_database_sha256": sha256_file(args.target),
        "series": reports,
        "series_count": len(reports),
        "source_series_equal_target": all(
            item["source_stats"] == item["target_stats"] for item in reports
        ),
        "duplicate_primary_keys_dropped": sum(
            int(item["duplicate_primary_keys_dropped"]) for item in reports
        ),
        "source_databases_modified": False,
        "frozen_core_modified": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    stable = {key: value for key, value in manifest.items() if key != "generated_at"}
    manifest["content_sha256"] = content_sha256(stable)
    atomic_json(args.manifest, manifest)
    print(
        json.dumps(
            {
                "target": str(args.target.resolve()),
                "database_sha256": manifest["target_database_sha256"],
                "series_count": len(reports),
                "content_sha256": manifest["content_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
