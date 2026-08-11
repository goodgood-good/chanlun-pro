#!/usr/bin/env python3
"""Freeze the broad CSI300 ETF research universe from provider master data.

The filter accepts only funds whose legal Chinese name tracks the unmodified
CSI300 index.  Sector, style, ESG, enhanced, dividend and feeder/LOF products
therefore cannot enter merely because their name contains ``沪深300``.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping, Sequence


SCHEMA = "chanlun-csi300-broad-etf-universe"
SOURCE_ENDPOINT = "/api/common/symbol-by-cond"
SOURCE_PARAMETERS = {
    "asset_type": "基金",
    "asset_sub_type": "场内公募",
    "secu_category": "开放式基金",
}
_LEGAL_NAME = re.compile(
    r"^.+沪深300交易型开放式指数(?:发起式)?证券投资基金(?:\(已终止\))?$"
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _content_sha256(value: Mapping[str, object]) -> str:
    stable = {
        key: item
        for key, item in value.items()
        if key not in {"generated_at", "content_sha256"}
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def select_broad_csi300_etfs(
    fields: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    listed_no_later_than: date,
) -> tuple[dict[str, object], ...]:
    required = {
        "symbol",
        "secu_abbr",
        "chiname",
        "asset_type",
        "asset_sub_type",
        "secu_category",
        "listed_status",
        "trading_state",
        "market",
    }
    if required.difference(fields):
        raise ValueError("provider master fields are incomplete")
    index = {field: fields.index(field) for field in required}
    selected: list[dict[str, object]] = []
    for values in rows:
        if len(values) != len(fields):
            raise ValueError("provider master row width changed")
        name = str(values[index["chiname"]] or "")
        listed_raw = values[index["listed_status"]]
        if not _LEGAL_NAME.fullmatch(name) or not isinstance(listed_raw, str):
            continue
        listed_on = date.fromisoformat(listed_raw[:10])
        if listed_on > listed_no_later_than:
            continue
        selected.append(
            {
                "symbol": str(values[index["symbol"]]),
                "secu_abbr": str(values[index["secu_abbr"]]),
                "legal_name": name,
                "listed_on": listed_on.isoformat(),
                "trading_state_at_query_time": str(
                    values[index["trading_state"]]
                ),
                "market": str(values[index["market"]]),
            }
        )
    result = tuple(sorted(selected, key=lambda row: str(row["symbol"])))
    symbols = tuple(str(row["symbol"]) for row in result)
    if len(symbols) != len(set(symbols)):
        raise ValueError("broad CSI300 ETF identities are not unique")
    return result


def build_snapshot(source: Path, *, cutoff: date) -> dict[str, object]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("status") != "SUCCESS":
        raise ValueError("provider master response is not successful")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("provider master response must contain one aligned result")
    result = results[0]
    if not isinstance(result, Mapping) or result.get("url") != SOURCE_ENDPOINT:
        raise ValueError("provider master endpoint identity changed")
    meta = result.get("meta")
    fields = meta.get("fields") if isinstance(meta, Mapping) else None
    rows = result.get("data")
    if not isinstance(fields, list) or not all(isinstance(v, str) for v in fields):
        raise ValueError("provider master fields are invalid")
    if not isinstance(rows, list):
        raise ValueError("provider master rows are invalid")
    universe = select_broad_csi300_etfs(
        fields,
        rows,
        listed_no_later_than=cutoff,
    )
    snapshot: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_endpoint": SOURCE_ENDPOINT,
        "source_parameters": SOURCE_PARAMETERS,
        "source_response_sha256": _file_sha256(source),
        "listed_no_later_than": cutoff.isoformat(),
        "selection_rule": (
            "EXACT_LEGAL_NAME_UNMODIFIED_CSI300_ETF; "
            "NO_CURRENT_TRADING_STATE_SURVIVOR_FILTER"
        ),
        "instruments": universe,
        "instrument_count": len(universe),
        "result_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    snapshot["content_sha256"] = _content_sha256(snapshot)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cutoff", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = build_snapshot(args.source, cutoff=args.cutoff)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "instrument_count": snapshot["instrument_count"],
        "content_sha256": snapshot["content_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
