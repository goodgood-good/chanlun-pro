#!/usr/bin/env python3
"""Audit the strict 5-minute movement partition for an explicit symbol universe.

The command is read-only with respect to market data.  It uses the same QMT
front-ratio price basis and canonical 12,000-bar window as production screening,
then writes a compact per-symbol CSV plus a lossless JSON evidence summary.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import csv
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Iterable, Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for SOURCE_ROOT in (PROJECT_ROOT / "src", PROJECT_ROOT / "web" / "chanlun_chart"):
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun import config  # noqa: E402
from chanlun.decision_support.trading_system.screening_runtime import (  # noqa: E402
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.screening_warmup import (  # noqa: E402
    SCREENING_CANONICAL_REQUEST_BARS,
    SCREENING_MINIMUM_BARS_BY_FREQUENCY,
)
from chanlun.exchange.exchange_qmt import ExchangeQMT  # noqa: E402
from chanlun.exchange.price_basis import (  # noqa: E402
    QMT_STRUCTURE_DIVIDEND_TYPE,
)
from tools.research_data import atomic_json, content_sha256  # noqa: E402


SCHEMA = "chanlun-strict-5m-partition-audit-v1"
FREQUENCY = "5m"
DEFAULT_RUNTIME_ROOT = Path(config.get_data_path()) / "decision_support"
DEFAULT_UNIVERSE_SNAPSHOT = DEFAULT_RUNTIME_ROOT / "trading_screening_snapshot.json"
DEFAULT_OUTPUT = DEFAULT_RUNTIME_ROOT / "strict_5m_partition_audit.json"
DEFAULT_CSV_OUTPUT = DEFAULT_RUNTIME_ROOT / "strict_5m_partition_audit.csv"
_SYMBOL = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_WORKER_EXCHANGE: ExchangeQMT | None = None


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(item.strip().upper() for item in value.split(",") if item.strip())
    if not symbols or len(symbols) != len(set(symbols)):
        raise argparse.ArgumentTypeError("symbols must be non-empty and unique")
    if any(_SYMBOL.fullmatch(symbol) is None for symbol in symbols):
        raise argparse.ArgumentTypeError("symbols must use MARKET.NUMBER form")
    return symbols


def load_universe_snapshot(
    path: Path,
) -> tuple[tuple[str, ...], datetime, str | None, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("universe snapshot must be a JSON object")
    manifest = raw.get("coverage_manifest")
    if raw.get("screening_scope_mode") == "FULL_MARKET":
        if not isinstance(manifest, Mapping) or manifest.get("complete") is not True:
            raise ValueError("full-market universe snapshot is not complete")
        raw_symbols = manifest.get("discovered_codes", ())
        universe_source = "coverage_manifest.discovered_codes"
    else:
        raw_symbols = raw.get("admitted_universe_codes", ())
        universe_source = "admitted_universe_codes"
    symbols = tuple(str(code).strip().upper() for code in raw_symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("snapshot universe must be non-empty and unique")
    if any(_SYMBOL.fullmatch(symbol) is None for symbol in symbols):
        raise ValueError("snapshot contains a malformed symbol")
    as_of_value = raw.get("market_data_as_of") or raw.get("as_of")
    as_of = pd.Timestamp(as_of_value)
    if pd.isna(as_of):
        raise ValueError("snapshot market_data_as_of is unavailable")
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("Asia/Shanghai")
    else:
        as_of = as_of.tz_convert("Asia/Shanghai")
    universe_revision = (
        str(manifest.get("universe_revision"))
        if isinstance(manifest, Mapping) and manifest.get("universe_revision")
        else None
    )
    return symbols, as_of.to_pydatetime(), universe_revision, universe_source


def _board(code: str) -> str:
    number = code.split(".", 1)[1]
    if code == "SH.513100":
        return "reference_etf"
    if code.startswith("SH.68"):
        return "star"
    if code.startswith("SH.60"):
        return "sh_main"
    if code.startswith("BJ."):
        return "beijing"
    if number.startswith(("300", "301")):
        return "chinext"
    return "sz_main"


def stratified_symbols(symbols: Sequence[str], limit: int | None) -> tuple[str, ...]:
    """Return a deterministic board-balanced prefix, retaining the reference ETF."""

    unique = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
    if limit is None or limit >= len(unique):
        return unique
    if limit <= 0:
        raise ValueError("limit must be positive")
    groups: dict[str, list[str]] = defaultdict(list)
    for code in sorted(unique):
        groups[_board(code)].append(code)
    order = ("reference_etf", "sh_main", "star", "sz_main", "chinext", "beijing")
    selected: list[str] = []
    offset = 0
    while len(selected) < limit:
        advanced = False
        for group in order:
            values = groups.get(group, ())
            if offset < len(values):
                selected.append(values[offset])
                advanced = True
                if len(selected) == limit:
                    break
        if not advanced:
            break
        offset += 1
    return tuple(selected)


def _iso(value: object | None) -> str | None:
    return None if value is None else pd.Timestamp(value).isoformat()


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _indices(items: Iterable[object], unit_index: Mapping[str, int]) -> list[int]:
    return [unit_index[str(getattr(item, "unit_id"))] for item in items]


def summarize_level_zero(level: object) -> dict[str, object]:
    units = tuple(getattr(level, "units"))
    trends = tuple(getattr(level, "trend_types"))
    pending = tuple(getattr(level, "pending_movements"))
    centers = tuple(getattr(getattr(level, "center_result"), "centers"))
    boundaries = tuple(getattr(level, "decomposition_boundaries"))
    unit_index = {unit.unit_id: index for index, unit in enumerate(units)}
    if len(unit_index) != len(units):
        raise ValueError("level-zero unit identities are not unique")

    owners: Counter[str] = Counter()
    trend_rows: list[dict[str, object]] = []
    for trend in trends:
        owned_indices = _indices(trend.constituent_units, unit_index)
        witness_indices = _indices(trend.completion_witness_units, unit_index)
        first_unit_direction = trend.constituent_units[0].direction
        terminal_unit_direction = trend.constituent_units[-1].direction
        direction_aligned = (
            first_unit_direction == trend.direction
            and terminal_unit_direction == trend.direction
            and len(owned_indices) % 2 == 1
        )
        owners.update(unit.unit_id for unit in trend.constituent_units)
        divergence = trend.terminal_divergence
        completion_basis = (
            "divergence"
            if divergence is not None
            else "geometric_reversal"
            if trend.completion_witness_units
            else "forming"
            if _enum_value(trend.state) == "forming"
            else "center_lifecycle"
        )
        trend_rows.append(
            {
                "trend_id": trend.trend_id,
                "kind": _enum_value(trend.kind),
                "direction": trend.direction,
                "state": _enum_value(trend.state),
                "unit_start_index": min(owned_indices),
                "unit_end_index": max(owned_indices),
                "unit_count": len(owned_indices),
                "first_unit_direction": first_unit_direction,
                "terminal_unit_direction": terminal_unit_direction,
                "constituent_unit_count_is_odd": len(owned_indices) % 2 == 1,
                "direction_aligned": direction_aligned,
                "constituent_unit_indices": owned_indices,
                "center_count": len(trend.centers),
                "center_ids": [center.center_id for center in trend.centers],
                "market_start": _iso(trend.market_start),
                "market_end": _iso(trend.market_end),
                "confirmed_at": _iso(trend.confirmed_at),
                "available_at": _iso(trend.available_at),
                "completion_basis": completion_basis,
                "completion_witness_unit_indices": witness_indices,
                "terminal_divergence_id": (
                    None if divergence is None else divergence.divergence_id
                ),
                "terminal_divergence_kind": (
                    None if divergence is None else divergence.kind
                ),
            }
        )
    duplicated_owner_ids = sorted(
        unit_id for unit_id, count in owners.items() if count > 1
    )
    if duplicated_owner_ids:
        raise ValueError("formal trends own the same level-zero unit")

    pending_ids: set[str] = set()
    pending_rows: list[dict[str, object]] = []
    for partition in pending:
        indices = _indices(partition.constituent_units, unit_index)
        ids = {unit.unit_id for unit in partition.constituent_units}
        if pending_ids & ids or set(owners) & ids:
            raise ValueError("pending movements overlap another formal partition")
        pending_ids.update(ids)
        pending_rows.append(
            {
                "partition_id": partition.partition_id,
                "role": _enum_value(partition.role),
                "direction": partition.direction,
                "unit_start_index": min(indices),
                "unit_end_index": max(indices),
                "unit_count": len(indices),
                "constituent_unit_indices": indices,
                "available_at": _iso(partition.available_at),
                "left_trend_id": partition.left_trend_id,
                "right_trend_id": partition.right_trend_id,
            }
        )

    center_rows: list[dict[str, object]] = []
    physical_role_violations = 0
    for center in centers:
        establishment_indices = _indices(center.establishment_units, unit_index)
        has_minimum_roles = bool(center.has_minimum_physical_roles)
        if not has_minimum_roles:
            physical_role_violations += 1
        center_rows.append(
            {
                "center_id": center.center_id,
                "state": _enum_value(center.state),
                "zd_tick": center.zd_tick,
                "zg_tick": center.zg_tick,
                "establishment_unit_indices": establishment_indices,
                "establishment_role_count": len(establishment_indices),
                "has_entry_core_leave_roles": has_minimum_roles,
                "entry_unit_index": (
                    None
                    if center.entry_unit is None
                    else unit_index[center.entry_unit.unit_id]
                ),
                "establishment_leave_unit_index": (
                    None
                    if center.establishment_leave_unit is None
                    else unit_index[center.establishment_leave_unit.unit_id]
                ),
                "completion_leave_unit_index": (
                    None
                    if center.completion_leave_unit is None
                    else unit_index[center.completion_leave_unit.unit_id]
                ),
                "completion_return_unit_index": (
                    None
                    if center.completion_return_unit is None
                    else unit_index[center.completion_return_unit.unit_id]
                ),
                "body_unit_count": len(center.body_units),
                "market_start": _iso(center.body_start_market_time),
                "established_at": _iso(center.established_at),
                "completed_at": _iso(center.completed_at),
                "available_at": _iso(center.available_at),
            }
        )

    covered_ids = set(owners) | pending_ids
    missing_ids = sorted(set(unit_index) - covered_ids)
    extra_ids = sorted(covered_ids - set(unit_index))
    if extra_ids:
        raise ValueError("partition references an unknown level-zero unit")
    compact_parts = [
        (
            int(row["unit_start_index"]),
            f"T[{row['unit_start_index']}-{row['unit_end_index']}]"
            f":{row['direction']}/{row['kind']}/{row['state']}",
        )
        for row in trend_rows
    ] + [
        (
            int(row["unit_start_index"]),
            f"P[{row['unit_start_index']}-{row['unit_end_index']}]"
            f":{row['direction']}/{row['role']}",
        )
        for row in pending_rows
    ]
    compact_parts.sort(key=lambda item: item[0])
    return {
        "unit_count": len(units),
        "locked_unit_count": sum(bool(unit.locked) for unit in units),
        "forming_unit_count": sum(bool(unit.forming) for unit in units),
        "center_count": len(center_rows),
        "physical_center_role_violation_count": physical_role_violations,
        "movement_direction_alignment_violation_count": sum(
            not bool(row["direction_aligned"]) for row in trend_rows
        ),
        "formal_trend_count": len(trend_rows),
        "completed_trend_snapshot_count": len(getattr(level, "completed_trends")),
        "boundary_count": len(boundaries),
        "centerless_trend_count": sum(not row["center_count"] for row in trend_rows),
        "pending_partition_count": len(pending_rows),
        "pending_unit_count": len(pending_ids),
        "missing_partition_unit_indices": [
            unit_index[unit_id] for unit_id in missing_ids
        ],
        "all_units_partitioned": not missing_ids,
        "partition_text": ";".join(value for _index, value in compact_parts),
        "trends": trend_rows,
        "pending_movements": pending_rows,
        "centers": center_rows,
        "boundaries": [
            {
                "boundary_id": boundary.boundary_id,
                "kind": boundary.boundary_kind,
                "direction": boundary.divergence.direction,
                "terminal_unit_index": unit_index[boundary.anchor_unit_id],
                "confirmed_at": _iso(boundary.confirmed_at),
                "available_at": _iso(boundary.available_at),
            }
            for boundary in boundaries
        ],
    }


def summarize_evidence(
    *, code: str, frame: pd.DataFrame, evidence: object, elapsed_ms: float
) -> dict[str, object]:
    levels = tuple(getattr(getattr(evidence, "structure"), "levels"))
    level_zero = next((level for level in levels if level.structural_level == 0), None)
    if level_zero is None:
        raise ValueError("strict structure did not produce level zero")
    return {
        "code": code,
        "status": "ok",
        "board": _board(code),
        "bar_count": len(frame),
        "first_bar_at": _iso(frame["date"].iloc[0]),
        "last_bar_at": _iso(frame["date"].iloc[-1]),
        "price_basis_revision": evidence.price_basis_revision,
        "strict_config_revision": evidence.strict_config_revision,
        "structure_revision": evidence.structure_revision,
        "recursive_level_count": len(levels),
        "elapsed_ms": round(elapsed_ms, 2),
        "level_zero": summarize_level_zero(level_zero),
    }


def _worker_exchange() -> ExchangeQMT:
    global _WORKER_EXCHANGE
    if _WORKER_EXCHANGE is None:
        _WORKER_EXCHANGE = ExchangeQMT()
    return _WORKER_EXCHANGE


def audit_qmt_symbol(code: str, as_of_iso: str) -> dict[str, object]:
    started = perf_counter()
    try:
        requested_as_of = pd.Timestamp(as_of_iso)
        if requested_as_of.tzinfo is None:
            requested_as_of = requested_as_of.tz_localize("Asia/Shanghai")
        else:
            requested_as_of = requested_as_of.tz_convert("Asia/Shanghai")
        raw = _worker_exchange().klines(
            code,
            FREQUENCY,
            args={
                "req_counts": SCREENING_CANONICAL_REQUEST_BARS[FREQUENCY],
                "skip_download": True,
                "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
            },
        )
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise ValueError("QMT 5m frame is unavailable")
        attrs = dict(raw.attrs)
        dates = pd.to_datetime(raw["date"], errors="raise")
        if isinstance(dates.dtype, pd.DatetimeTZDtype):
            dates = dates.dt.tz_convert("Asia/Shanghai")
        else:
            dates = dates.dt.tz_localize("Asia/Shanghai")
        frame = raw.loc[dates <= requested_as_of].copy().reset_index(drop=True)
        frame.attrs = attrs
        if len(frame) < SCREENING_MINIMUM_BARS_BY_FREQUENCY[FREQUENCY]:
            raise ValueError("QMT 5m frame does not meet minimum history")
        closed_at = pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime()
        evidence = screening_evidence_from_frame(
            code=code,
            frequency=FREQUENCY,
            frame=frame,
            as_of=closed_at,
        )
        return summarize_evidence(
            code=code,
            frame=frame,
            evidence=evidence,
            elapsed_ms=(perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return {
            "code": code,
            "status": "error",
            "board": _board(code),
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _aggregate(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    successful = [row for row in rows if row.get("status") == "ok"]
    errors = [row for row in rows if row.get("status") != "ok"]
    levels = [row["level_zero"] for row in successful]
    trend_rows = [trend for level in levels for trend in level["trends"]]
    pending_rows = [item for level in levels for item in level["pending_movements"]]
    return {
        "requested_symbol_count": len(rows),
        "successful_symbol_count": len(successful),
        "error_symbol_count": len(errors),
        "all_units_partitioned_symbol_count": sum(
            bool(level["all_units_partitioned"]) for level in levels
        ),
        "symbols_with_pending_movements": sum(
            int(level["pending_partition_count"]) > 0 for level in levels
        ),
        "symbols_without_formal_trends": sum(
            int(level["formal_trend_count"]) == 0 for level in levels
        ),
        "total_level_zero_units": sum(int(level["unit_count"]) for level in levels),
        "total_centers": sum(int(level["center_count"]) for level in levels),
        "physical_center_role_violation_count": sum(
            int(level["physical_center_role_violation_count"]) for level in levels
        ),
        "movement_direction_alignment_violation_count": sum(
            int(level["movement_direction_alignment_violation_count"])
            for level in levels
        ),
        "total_formal_trends": len(trend_rows),
        "total_centerless_trends": sum(
            not trend["center_count"] for trend in trend_rows
        ),
        "total_pending_partitions": len(pending_rows),
        "total_pending_units": sum(int(item["unit_count"]) for item in pending_rows),
        "trend_kind_counts": dict(
            sorted(Counter(trend["kind"] for trend in trend_rows).items())
        ),
        "trend_state_counts": dict(
            sorted(Counter(trend["state"] for trend in trend_rows).items())
        ),
        "trend_completion_basis_counts": dict(
            sorted(Counter(trend["completion_basis"] for trend in trend_rows).items())
        ),
        "pending_role_counts": dict(
            sorted(Counter(item["role"] for item in pending_rows).items())
        ),
        "error_type_counts": dict(
            sorted(Counter(str(row.get("error_type")) for row in errors).items())
        ),
    }


def build_audit_document(
    *,
    rows: Sequence[Mapping[str, object]],
    as_of: datetime,
    universe_revision: str | None,
    universe_symbol_count: int,
    selected_by_limit: bool,
    universe_source: str = "explicit_rows",
) -> dict[str, object]:
    ordered = [dict(row) for row in sorted(rows, key=lambda item: str(item["code"]))]
    summary = _aggregate(ordered)
    document: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "market_data_as_of": as_of.isoformat(),
        "frequency": FREQUENCY,
        "price_basis": QMT_STRUCTURE_DIVIDEND_TYPE,
        "request_bars": SCREENING_CANONICAL_REQUEST_BARS[FREQUENCY],
        "qmt_skip_download": True,
        "read_only": True,
        "generic_rule": True,
        "ticker_specific_rules": False,
        "universe_revision": universe_revision,
        "universe_source": universe_source,
        "universe_symbol_count": universe_symbol_count,
        "scope": "STRATIFIED_SAMPLE" if selected_by_limit else "FULL_UNIVERSE",
        "summary": summary,
        "symbols": ordered,
    }
    document["status"] = (
        "COMPLETE"
        if summary["error_symbol_count"] == 0
        and summary["successful_symbol_count"] == summary["requested_symbol_count"]
        else "PARTIAL"
    )
    document["content_sha256"] = content_sha256(document)
    return document


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fields = (
        "code",
        "status",
        "board",
        "bar_count",
        "unit_count",
        "center_count",
        "formal_trend_count",
        "movement_direction_alignment_violation_count",
        "centerless_trend_count",
        "pending_partition_count",
        "pending_unit_count",
        "all_units_partitioned",
        "partition_text",
        "error_type",
        "error",
    )
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows, key=lambda item: str(item["code"])):
            level = row.get("level_zero")
            level = level if isinstance(level, Mapping) else {}
            writer.writerow(
                {
                    "code": row.get("code"),
                    "status": row.get("status"),
                    "board": row.get("board"),
                    "bar_count": row.get("bar_count"),
                    "unit_count": level.get("unit_count"),
                    "center_count": level.get("center_count"),
                    "formal_trend_count": level.get("formal_trend_count"),
                    "movement_direction_alignment_violation_count": level.get(
                        "movement_direction_alignment_violation_count"
                    ),
                    "centerless_trend_count": level.get("centerless_trend_count"),
                    "pending_partition_count": level.get("pending_partition_count"),
                    "pending_unit_count": level.get("pending_unit_count"),
                    "all_units_partitioned": level.get("all_units_partitioned"),
                    "partition_text": level.get("partition_text"),
                    "error_type": row.get("error_type"),
                    "error": row.get("error"),
                }
            )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _collect_rows(
    symbols: Sequence[str], *, as_of: datetime, workers: int, progress_every: int
) -> list[dict[str, object]]:
    as_of_iso = as_of.isoformat()
    rows: list[dict[str, object]] = []
    if workers == 1:
        for ordinal, code in enumerate(symbols, start=1):
            rows.append(audit_qmt_symbol(code, as_of_iso))
            if ordinal % progress_every == 0 or ordinal == len(symbols):
                print(f"partition-audit {ordinal}/{len(symbols)}", file=sys.stderr)
        return rows
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(audit_qmt_symbol, code, as_of_iso): code for code in symbols
        }
        for ordinal, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append(
                    {
                        "code": code,
                        "status": "error",
                        "board": _board(code),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            if ordinal % progress_every == 0 or ordinal == len(symbols):
                print(f"partition-audit {ordinal}/{len(symbols)}", file=sys.stderr)
    return rows


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=parse_symbols)
    parser.add_argument(
        "--universe-snapshot", type=Path, default=DEFAULT_UNIVERSE_SNAPSHOT
    )
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--workers", type=_positive_int, default=1)
    parser.add_argument("--progress-every", type=_positive_int, default=10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.symbols is not None and args.as_of is not None:
        universe_symbols = args.symbols
        as_of = args.as_of
        universe_revision = None
        universe_source = "explicit_symbols"
    else:
        (
            snapshot_symbols,
            snapshot_as_of,
            universe_revision,
            snapshot_source,
        ) = load_universe_snapshot(args.universe_snapshot)
        universe_symbols = args.symbols or snapshot_symbols
        as_of = args.as_of or snapshot_as_of
        universe_source = (
            "explicit_symbols" if args.symbols is not None else snapshot_source
        )
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of must be timezone-aware")
    selected = stratified_symbols(universe_symbols, args.limit)
    rows = _collect_rows(
        selected,
        as_of=as_of,
        workers=args.workers,
        progress_every=args.progress_every,
    )
    document = build_audit_document(
        rows=rows,
        as_of=as_of,
        universe_revision=universe_revision,
        universe_symbol_count=len(universe_symbols),
        selected_by_limit=len(selected) < len(universe_symbols),
        universe_source=universe_source,
    )
    atomic_json(args.output, document)
    _write_csv(args.csv_output, rows)
    print(json.dumps(document["summary"], ensure_ascii=False, sort_keys=True))
    print(f"json={args.output.resolve()}")
    print(f"csv={args.csv_output.resolve()}")
    return 0 if document["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
