#!/usr/bin/env python3
"""Snapshot raw QMT ETF corporate actions without constructing adjusted prices.

The snapshot is deliberately outside the frozen Chanlun structure and strict strategy
decision implementations.  It records QMT's effective-dated raw fields exactly
as returned.  An empty QMT frame is classified as unknown, not as proof that an
instrument has never had a corporate action.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


SCHEMA = "chanlun-qmt-etf-corporate-actions"
DEFAULT_CODES = (
    "510300.SH",
    "510050.SH",
    "510500.SH",
    "512100.SH",
    "512880.SH",
    "159915.SZ",
    "159919.SZ",
    "159949.SZ",
)
RAW_FIELDS = (
    "time",
    "interest",
    "stockBonus",
    "stockGift",
    "allotNum",
    "allotPrice",
    "gugai",
    "dr",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _snapshot_content_sha256(value: Mapping[str, object]) -> str:
    stable = {
        key: item
        for key, item in value.items()
        if key not in {"generated_at", "content_sha256"}
    }
    return _content_sha256(stable)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _tree_sha256(path: Path) -> str | None:
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _native(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _effective_date(value: object) -> str:
    rendered = str(value)
    if len(rendered) == 8 and rendered.isdigit():
        return f"{rendered[:4]}-{rendered[4:6]}-{rendered[6:]}"
    return rendered


def causal_events_at(
    events: Sequence[Mapping[str, object]],
    *,
    decision_on: date,
) -> tuple[Mapping[str, object], ...]:
    """Return events whose effective session is known at the decision date.

    Equality is intentionally included: the research policy makes an event
    available from the opening of its effective session, never before it.
    """

    output: list[Mapping[str, object]] = []
    for event in events:
        effective = date.fromisoformat(str(event["effective_on"]))
        if effective <= decision_on:
            output.append(event)
    return tuple(output)


def _instrument_snapshot(detail: Mapping[str, Any]) -> dict[str, object]:
    fields = (
        "ExchangeID",
        "InstrumentID",
        "InstrumentName",
        "OpenDate",
        "ExpireDate",
        "ProductID",
        "ProductName",
    )
    return {field: _native(detail.get(field)) for field in fields}


def _pit_510300_events(database: Path) -> tuple[dict[str, object], ...]:
    if not database.is_file():
        return ()
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT ex_date, cash_per_share, cumulative_cash_per_share, source
            FROM etf_distributions
            WHERE symbol='SH.510300'
            ORDER BY ex_date
            """
        ).fetchall()
    return tuple(
        {
            "effective_on": str(ex_date),
            "cash_per_share": str(cash_per_share),
            "cumulative_cash_per_share": str(cumulative),
            "source": str(source),
        }
        for ex_date, cash_per_share, cumulative, source in rows
    )


def _crosscheck_510300(
    qmt_events: Sequence[Mapping[str, object]],
    pit_events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not pit_events:
        return {"status": "REFERENCE_UNAVAILABLE"}
    qmt = {
        str(event["effective_on"]): float(event["raw"]["interest"])  # type: ignore[index]
        for event in qmt_events
    }
    pit = {
        str(event["effective_on"]): float(event["cash_per_share"])
        for event in pit_events
    }
    common = sorted(qmt.keys() & pit.keys())
    mismatches = tuple(
        {
            "effective_on": effective_on,
            "qmt_interest": qmt[effective_on],
            "pit_cash_per_share": pit[effective_on],
        }
        for effective_on in common
        if abs(qmt[effective_on] - pit[effective_on]) > 1e-10
    )
    return {
        "status": (
            "EXACT_EFFECTIVE_DATE_AND_CASH_MATCH"
            if set(qmt) == set(pit) and not mismatches
            else "PARTIAL_OR_MISMATCH"
        ),
        "qmt_events": len(qmt),
        "reference_events": len(pit),
        "common_events": len(common),
        "qmt_only_dates": sorted(qmt.keys() - pit.keys()),
        "reference_only_dates": sorted(pit.keys() - qmt.keys()),
        "cash_mismatches": mismatches,
    }


def build_snapshot(
    codes: Sequence[str],
    *,
    pit_database: Path,
) -> dict[str, object]:
    from xtquant import xtdata

    xtdata.enable_hello = False
    data_dir = Path(xtdata.get_data_dir())
    source_store = data_dir / "DividData"
    instruments: list[dict[str, object]] = []
    for code in codes:
        detail = xtdata.get_instrument_detail(code, False)
        if not isinstance(detail, Mapping) or not detail:
            instruments.append(
                {
                    "code": code,
                    "status": "INSTRUMENT_UNAVAILABLE",
                    "causal_application": "NOT_ALLOWED",
                    "events": [],
                }
            )
            continue
        frame = xtdata.get_divid_factors(code)
        if frame is None or not hasattr(frame, "columns"):
            instruments.append(
                {
                    "code": code,
                    "instrument": _instrument_snapshot(detail),
                    "status": "PROVIDER_RESPONSE_UNSUPPORTED",
                    "causal_application": "NOT_ALLOWED",
                    "events": [],
                }
            )
            continue
        columns = tuple(str(column) for column in frame.columns)
        if len(frame) == 0:
            instruments.append(
                {
                    "code": code,
                    "instrument": _instrument_snapshot(detail),
                    "status": "NO_ROWS_UNKNOWN_NOT_CERTIFIED_NO_EVENT",
                    "causal_application": "NOT_ALLOWED_UNTIL_CROSS_SOURCE_CERTIFIED",
                    "provider_columns": list(columns),
                    "events": [],
                    "events_sha256": _content_sha256([]),
                }
            )
            continue
        if columns != RAW_FIELDS:
            instruments.append(
                {
                    "code": code,
                    "instrument": _instrument_snapshot(detail),
                    "status": "UNEXPECTED_PROVIDER_SCHEMA",
                    "causal_application": "NOT_ALLOWED",
                    "provider_columns": list(columns),
                    "events": [],
                }
            )
            continue
        events: list[dict[str, object]] = []
        for effective_on, row in frame.sort_index().iterrows():
            raw = {field: _native(row[field]) for field in RAW_FIELDS}
            events.append(
                {
                    "effective_on": _effective_date(effective_on),
                    "availability_policy": "EFFECTIVE_SESSION_OPEN_RESEARCH_ASSUMPTION",
                    "raw": raw,
                }
            )
        instruments.append(
            {
                "code": code,
                "instrument": _instrument_snapshot(detail),
                "status": "EFFECTIVE_DATED_EVENTS_AVAILABLE",
                "causal_application": (
                    "ALLOWED_FROM_EFFECTIVE_SESSION_ONLY_RESEARCH_NO_FINAL_FORWARD_SERIES"
                ),
                "provider_columns": list(columns),
                "event_count": len(events),
                "first_effective_on": events[0]["effective_on"],
                "last_effective_on": events[-1]["effective_on"],
                "events": events,
                "events_sha256": _content_sha256(events),
            }
        )
    pit_events = _pit_510300_events(pit_database)
    qmt_510300 = next(
        (
            item.get("events", [])
            for item in instruments
            if item.get("code") == "510300.SH"
        ),
        [],
    )
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": "QMT.xtdata.get_divid_factors",
        "source_store_sha256": _tree_sha256(source_store),
        "raw_provider_fields": list(RAW_FIELDS),
        "price_adjustment_output": "NONE",
        "policy": (
            "KEEP_RAW_EVENTS_AND_EFFECTIVE_DATES; NEVER_BUILD_A_FINAL_FORWARD_"
            "ADJUSTED_SERIES; EMPTY_PROVIDER_ROWS_REMAIN_UNKNOWN"
        ),
        "instruments": instruments,
        "reference_510300": {
            "source": "existing read-only etf_distributions ledger",
            "events": pit_events,
            "events_sha256": _content_sha256(pit_events),
            "crosscheck": _crosscheck_510300(qmt_510300, pit_events),
        },
        "live_status": "LIVE_DISABLED",
    }
    payload["content_sha256"] = _snapshot_content_sha256(payload)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codes",
        default=",".join(DEFAULT_CODES),
        help="comma-separated native QMT codes",
    )
    parser.add_argument(
        "--pit-database",
        type=Path,
        default=Path(".cache/chanlun_external_pit/etf_proxy_pit.sqlite3"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    codes = tuple(
        dict.fromkeys(code.strip().upper() for code in args.codes.split(",") if code.strip())
    )
    if not codes:
        raise ValueError("at least one code is required")
    payload = build_snapshot(codes, pit_database=args.pit_database)
    _atomic_json(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "content_sha256": payload["content_sha256"],
                "instrument_statuses": {
                    str(item["code"]): item["status"]
                    for item in payload["instruments"]  # type: ignore[index]
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
