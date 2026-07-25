#!/usr/bin/env python3
"""Download and audit a bounded QMT minute-history archive.

The QMT client owns the actual on-disk archive.  This command deliberately
downloads in small batches, verifies every batch by reading only the time
field, and writes an atomic resumable manifest.  It never fabricates bars or
falls back to a lower-frequency provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


CN = ZoneInfo("Asia/Shanghai")
CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
DEFAULT_SCOPE = "\u6caa\u6df1\u4eacA\u80a1"
SCHEMA = "chanlun-qmt-history-supplement/v1"


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


def _chunks(values: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    for offset in range(0, len(values), size):
        yield tuple(values[offset : offset + size])


def _native_timestamp(value: object) -> str:
    timestamp = pd.to_datetime(int(value), unit="ms", utc=True).tz_convert(CN)
    return timestamp.isoformat()


def _request_timestamp(day: date, *, closing: bool) -> str:
    observed = datetime.combine(
        day,
        datetime_time(15, 0) if closing else datetime_time(9, 30),
        tzinfo=CN,
    )
    return observed.strftime("%Y%m%d%H%M%S")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_resume(path: Path, request: Mapping[str, object]) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema") != SCHEMA or payload.get("request") != request:
        return {}
    return payload


def _parse_codes(raw: str | None, xtdata: object) -> tuple[str, ...]:
    if raw:
        candidates = (item.strip().upper() for item in raw.split(","))
    else:
        candidates = xtdata.get_stock_list_in_sector(DEFAULT_SCOPE)
    return tuple(
        sorted(
            {
                code
                for code in candidates
                if isinstance(code, str) and CODE_RE.fullmatch(code) is not None
            }
        )
    )


def _read_time_coverage(
    xtdata: object,
    *,
    codes: tuple[str, ...],
    period: str,
    start_text: str,
    end_text: str,
) -> dict[str, dict[str, object]]:
    raw = xtdata.get_market_data(
        field_list=["time"],
        stock_list=list(codes),
        period=period,
        start_time=start_text,
        end_time=end_text,
        count=-1,
        dividend_type="none",
        fill_data=False,
    )
    matrix = raw.get("time") if isinstance(raw, Mapping) else None
    output: dict[str, dict[str, object]] = {}
    for code in codes:
        if not isinstance(matrix, pd.DataFrame) or code not in matrix.index:
            output[code] = {"rows": 0, "earliest": None, "latest": None}
            continue
        values = pd.to_numeric(matrix.loc[code], errors="coerce").to_numpy()
        valid = values[pd.notna(values) & (values > 0) & np.isfinite(values)]
        if len(valid) == 0:
            output[code] = {"rows": 0, "earliest": None, "latest": None}
            continue
        output[code] = {
            "rows": int(len(valid)),
            "earliest": _native_timestamp(valid.min()),
            "latest": _native_timestamp(valid.max()),
        }
    return output


def _download_batch(
    xtdata: object,
    *,
    codes: tuple[str, ...],
    period: str,
    start_text: str,
    end_text: str,
    retries: int,
) -> dict[str, dict[str, object]]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            xtdata.download_history_data2(
                list(codes),
                period,
                start_time=start_text,
                end_time=end_text,
                incrementally=False,
            )
            return _read_time_coverage(
                xtdata,
                codes=codes,
                period=period,
                start_text=start_text,
                end_text=end_text,
            )
        except Exception as exc:  # QMT raises native client exceptions.
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 5))
    assert last_error is not None
    raise last_error


def _summary(records: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    populated = [row for row in records.values() if int(row.get("rows") or 0) > 0]
    earliest = [str(row["earliest"]) for row in populated if row.get("earliest")]
    latest = [str(row["latest"]) for row in populated if row.get("latest")]
    return {
        "codes_verified": len(records),
        "codes_with_rows": len(populated),
        "codes_without_rows": len(records) - len(populated),
        "total_rows": sum(int(row.get("rows") or 0) for row in records.values()),
        "earliest": min(earliest) if earliest else None,
        "latest": max(latest) if latest else None,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--start", type=_day, required=True)
    result.add_argument("--end", type=_day, required=True)
    result.add_argument("--period", choices=("1m", "5m", "30m"), default="1m")
    result.add_argument("--batch-size", type=_positive_int, default=50)
    result.add_argument("--retries", type=_positive_int, default=3)
    result.add_argument("--codes", help="optional comma-separated native QMT codes")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--force", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.start > args.end:
        raise ValueError("start cannot follow end")
    if args.batch_size > 100:
        raise ValueError("batch-size cannot exceed QMT's safe limit of 100")

    from xtquant import xtdata

    xtdata.enable_hello = False
    codes = _parse_codes(args.codes, xtdata)
    if not codes:
        raise RuntimeError("QMT returned no A-share codes")
    start_text = _request_timestamp(args.start, closing=False)
    end_text = _request_timestamp(args.end, closing=True)
    request: dict[str, object] = {
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "period": args.period,
        "codes": list(codes),
    }
    existing = {} if args.force else _load_resume(args.output, request)
    raw_records = existing.get("records") if isinstance(existing, dict) else None
    records: dict[str, dict[str, object]] = (
        dict(raw_records) if isinstance(raw_records, dict) else {}
    )
    # A failed batch stays resumable.  A legitimate zero-row result (for
    # example, a newly listed code outside the requested range) is complete.
    completed = {code for code, row in records.items() if not row.get("error")}
    pending = tuple(code for code in codes if code not in completed)
    started = time.perf_counter()

    for ordinal, chunk in enumerate(_chunks(pending, args.batch_size), start=1):
        batch_started = time.perf_counter()
        try:
            coverage = _download_batch(
                xtdata,
                codes=chunk,
                period=args.period,
                start_text=start_text,
                end_text=end_text,
                retries=args.retries,
            )
            records.update(coverage)
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            for code in chunk:
                records[code] = {
                    "rows": 0,
                    "earliest": None,
                    "latest": None,
                    "error": error,
                }
        summary = _summary(records)
        manifest: dict[str, object] = {
            "schema": SCHEMA,
            "generated_at": datetime.now().astimezone().isoformat(),
            "complete": all(
                code in records and not records[code].get("error") for code in codes
            ),
            "request": request,
            "summary": summary,
            "records": dict(sorted(records.items())),
        }
        _atomic_json(args.output, manifest)
        print(
            json.dumps(
                {
                    "batch": ordinal,
                    "batch_codes": len(chunk),
                    "verified": len(records),
                    "total": len(codes),
                    "rows": summary["total_rows"],
                    "seconds": round(time.perf_counter() - batch_started, 2),
                    "error": error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    complete = all(code in records and not records[code].get("error") for code in codes)
    final_summary = _summary(records)
    print(
        json.dumps(
            {
                "complete": complete,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                **final_summary,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
