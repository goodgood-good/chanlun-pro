#!/usr/bin/env python3
"""Audit QMT 1m coverage for the complete sector-first stock universe.

The command is deliberately read-only: it never downloads or fills bars.  It
queries the timestamp field in bounded batches and writes an atomic resumable
ledger, so missing warm-up history cannot be hidden behind a small pilot.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import sys
import time as wall_time
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    sha256_file,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
    read_qmt_local_kline,
    resolve_qmt_local_data_dir,
)


CN = ZoneInfo("Asia/Shanghai")
SCHEMA = "chanlun-sector-first-minute-coverage"
DEFAULT_SCOPE = Path(
    "audit/chanlun_live_integration/sector_first_full_market_scope.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/sector_first_full_market_1m_coverage.json"
)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    converted = int(value)
    if converted <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return converted


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--scope", type=Path, default=DEFAULT_SCOPE)
    value.add_argument("--start", type=_day, default=date(2018, 1, 1))
    value.add_argument("--end", type=_day, default=date(2026, 7, 24))
    value.add_argument("--batch-size", type=_positive_int, default=100)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument(
        "--qmt-local-data-dir",
        type=Path,
        help=(
            "read QMT's immutable local fixed-record cache instead of RPC; "
            f"defaults to {QMT_LOCAL_DATA_ENV}"
        ),
    )
    value.add_argument("--force", action="store_true")
    return value


def _normal_to_native(code: str) -> str:
    market, digits = code.split(".", 1)
    if market not in {"SH", "SZ", "BJ"} or len(digits) != 6:
        raise ValueError(f"unsupported normalized A-share code: {code!r}")
    return f"{digits}.{market}"


def _native_time(value: object) -> str:
    return (
        pd.to_datetime(int(value), unit="ms", utc=True)
        .tz_convert(CN)
        .isoformat()
    )


def _scope_rows(scope: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    if scope.get("schema") != "chanlun-sector-first-scope":
        raise ValueError("unsupported sector-first scope")
    raw = scope.get("symbols")
    if not isinstance(raw, list):
        raise ValueError("sector-first scope symbols are unavailable")
    selected: list[dict[str, object]] = []
    for row in raw:
        if not isinstance(row, Mapping) or not row.get(
            "classified_for_requested_range"
        ):
            continue
        selected.append(
            {
                "code": str(row["code"]),
                "listed_from": str(row["listed_from"]),
            }
        )
    selected.sort(key=lambda row: str(row["code"]))
    if len(selected) != len({str(row["code"]) for row in selected}):
        raise ValueError("sector-first selected symbols are not unique")
    return tuple(selected)


def _request(
    *,
    scope_sha256: str,
    scope_file_sha256: str,
    start: date,
    end: date,
    symbols: Sequence[Mapping[str, object]],
    transport: str,
    local_data_dir: Path | None,
) -> dict[str, object]:
    return {
        "scope_sha256": scope_sha256,
        "scope_file_sha256": scope_file_sha256,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "period": "1m",
        "symbol_count": len(symbols),
        "symbols_sha256": content_sha256(tuple(str(row["code"]) for row in symbols)),
        "download_performed": False,
        "fill_performed": False,
        "transport": transport,
        "local_data_dir": None if local_data_dir is None else str(local_data_dir),
    }


def _resume(
    path: Path,
    request: Mapping[str, object],
    *,
    force: bool,
) -> dict[str, dict[str, object]]:
    if force or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if payload.get("schema") != SCHEMA or payload.get("request") != request:
        return {}
    records = payload.get("records")
    return dict(records) if isinstance(records, dict) else {}


def _coverage_row(
    matrix: pd.DataFrame | None,
    *,
    native: str,
    expected_start: date,
    requested_end: date,
) -> dict[str, object]:
    if matrix is None or native not in matrix.index:
        values = pd.Series(dtype="float64")
    else:
        values = pd.to_numeric(matrix.loc[native], errors="coerce")
        values = values[values.notna() & (values > 0)]
    if values.empty:
        earliest = latest = None
    else:
        earliest = _native_time(values.min())
        latest = _native_time(values.max())
    earliest_day = None if earliest is None else datetime.fromisoformat(earliest).date()
    latest_day = None if latest is None else datetime.fromisoformat(latest).date()
    return {
        "rows": int(len(values)),
        "earliest": earliest,
        "latest": latest,
        "expected_start": expected_start.isoformat(),
        "start_covered": earliest_day is not None and earliest_day <= expected_start,
        "end_covered": latest_day is not None and latest_day >= requested_end,
    }


def _local_coverage_row(
    *,
    data_dir: Path,
    code: str,
    start_at: datetime,
    end_at: datetime,
    expected_start: date,
    requested_end: date,
) -> dict[str, object]:
    frame, audit = read_qmt_local_kline(
        data_dir=data_dir,
        code=code,
        frequency="1m",
        start_at=start_at,
        end_at=end_at,
    )
    earliest = None if audit.first_at is None else audit.first_at.isoformat()
    latest = None if audit.last_at is None else audit.last_at.isoformat()
    earliest_day = None if audit.first_at is None else audit.first_at.date()
    latest_day = None if audit.last_at is None else audit.last_at.date()
    return {
        "rows": int(len(frame)),
        "earliest": earliest,
        "latest": latest,
        "expected_start": expected_start.isoformat(),
        "start_covered": earliest_day is not None and earliest_day <= expected_start,
        "end_covered": latest_day is not None and latest_day >= requested_end,
        "source_sha256": audit.source_sha256,
        "source_audit_id": audit.audit_id,
    }


def _summary(records: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    populated = tuple(row for row in records.values() if int(row["rows"]) > 0)
    full = tuple(
        row
        for row in populated
        if bool(row["start_covered"]) and bool(row["end_covered"])
    )
    earliest = tuple(str(row["earliest"]) for row in populated if row["earliest"])
    latest = tuple(str(row["latest"]) for row in populated if row["latest"])
    return {
        "audited_symbol_count": len(records),
        "symbols_with_rows": len(populated),
        "symbols_without_rows": len(records) - len(populated),
        "symbols_with_complete_requested_history": len(full),
        "symbols_missing_requested_warmup": sum(
            not bool(row["start_covered"]) for row in records.values()
        ),
        "symbols_missing_requested_end": sum(
            not bool(row["end_covered"]) for row in records.values()
        ),
        "total_rows": sum(int(row["rows"]) for row in records.values()),
        "earliest_observed": min(earliest) if earliest else None,
        "latest_observed": max(latest) if latest else None,
    }


def _document(
    request: Mapping[str, object],
    records: Mapping[str, Mapping[str, object]],
    *,
    complete: bool,
    runtime_failure: Mapping[str, object] | None = None,
) -> dict[str, object]:
    summary = _summary(records)
    stable: dict[str, object] = {
        "schema": SCHEMA,
        "request": dict(request),
        "complete": complete,
        "summary": summary,
        "records": dict(sorted(records.items())),
        "runtime_failure": (
            None if runtime_failure is None else dict(runtime_failure)
        ),
        "data_grade": "COMPONENT_ONLY",
        "full_system_minute_history_gate": (
            "PASS"
            if complete
            and summary["symbols_with_complete_requested_history"]
            == request["symbol_count"]
            else "FAIL_INCOMPLETE_1M_WARMUP"
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": content_sha256(stable)}


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.start > args.end:
        raise ValueError("start cannot follow end")
    if args.batch_size > 100:
        raise ValueError("batch-size cannot exceed 100")
    scope_path = args.scope.resolve()
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    symbols = _scope_rows(scope)
    explicit_local = args.qmt_local_data_dir
    if explicit_local is not None:
        os.environ[QMT_LOCAL_DATA_ENV] = str(explicit_local.resolve())
    local_data_dir = resolve_qmt_local_data_dir()
    transport = "LOCAL_FIXED_RECORD_READ_ONLY" if local_data_dir else "RPC"
    request = _request(
        scope_sha256=str(scope["content_sha256"]),
        scope_file_sha256=sha256_file(scope_path),
        start=args.start,
        end=args.end,
        symbols=symbols,
        transport=transport,
        local_data_dir=local_data_dir,
    )
    records = _resume(args.output, request, force=args.force)
    pending = tuple(row for row in symbols if str(row["code"]) not in records)
    start_text = datetime.combine(args.start, time(9, 30), tzinfo=CN).strftime(
        "%Y%m%d%H%M%S"
    )
    end_text = datetime.combine(args.end, time(15, 0), tzinfo=CN).strftime(
        "%Y%m%d%H%M%S"
    )

    started = wall_time.perf_counter()
    if local_data_dir is not None:
        start_at = datetime.combine(args.start, time(9, 30), tzinfo=CN)
        end_at = datetime.combine(args.end, time(15, 0), tzinfo=CN)
        for offset, row in enumerate(pending, start=1):
            code = str(row["code"])
            listed_from = date.fromisoformat(str(row["listed_from"]))
            records[code] = _local_coverage_row(
                data_dir=local_data_dir,
                code=code,
                start_at=start_at,
                end_at=end_at,
                expected_start=max(args.start, listed_from),
                requested_end=args.end,
            )
            if offset % args.batch_size == 0 or offset == len(pending):
                complete = len(records) == len(symbols)
                atomic_json(args.output, _document(request, records, complete=complete))
                print(
                    json.dumps(
                        {
                            "audited": len(records),
                            "total": len(symbols),
                            "batch": min(args.batch_size, offset),
                            "seconds": round(wall_time.perf_counter() - started, 2),
                            "transport": transport,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        document = _document(request, records, complete=len(records) == len(symbols))
        atomic_json(args.output, document)
        print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
        return 0

    from xtquant import xtdata

    xtdata.enable_hello = False
    for offset in range(0, len(pending), args.batch_size):
        chunk = pending[offset : offset + args.batch_size]
        native_to_row = {_normal_to_native(str(row["code"])): row for row in chunk}
        try:
            raw = xtdata.get_market_data(
                field_list=["time"],
                stock_list=list(native_to_row),
                period="1m",
                start_time=start_text,
                end_time=end_text,
                count=-1,
                dividend_type="none",
                fill_data=False,
            )
        except Exception as exc:  # QMT 会抛出原生客户端异常。
            failure = {
                "first_code": str(chunk[0]["code"]),
                "batch_size": len(chunk),
                "error": f"{type(exc).__name__}:{exc}",
            }
            atomic_json(
                args.output,
                _document(
                    request,
                    records,
                    complete=False,
                    runtime_failure=failure,
                ),
            )
            print(json.dumps(failure, ensure_ascii=False), flush=True)
            return 2
        matrix = raw.get("time") if isinstance(raw, Mapping) else None
        if matrix is not None and not isinstance(matrix, pd.DataFrame):
            matrix = None
        for native, row in native_to_row.items():
            listed_from = date.fromisoformat(str(row["listed_from"]))
            records[str(row["code"])] = _coverage_row(
                matrix,
                native=native,
                expected_start=max(args.start, listed_from),
                requested_end=args.end,
            )
        complete = len(records) == len(symbols)
        atomic_json(args.output, _document(request, records, complete=complete))
        print(
            json.dumps(
                {
                    "audited": len(records),
                    "total": len(symbols),
                    "batch": len(chunk),
                    "seconds": round(wall_time.perf_counter() - started, 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    document = _document(request, records, complete=len(records) == len(symbols))
    atomic_json(args.output, document)
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
