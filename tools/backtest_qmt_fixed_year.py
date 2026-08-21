#!/usr/bin/env python3
"""Run the current fixed Chanlun policy over one bounded QMT year.

The command is intentionally resumable.  Stage one writes one compact causal
fact file per symbol instead of materialising the full 318-million-row market
in memory.  Later stages consume only symbols and timestamps at which the
production entry/exit policy can change.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import time
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FACT_SCHEMA,
    SymbolResearchFacts,
    build_symbol_facts,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataIndex,
    PITMetadataSnapshot,
    QmtFactorAt,
    SecurityMasterRecord,
    SectorMembershipChange,
    load_snapshot,
)
from chanlun.decision_support.trading_system.sector_first_scope import (
    build_sector_first_scope,
)
from chanlun.decision_support.trading_system.signal_alignment import (
    UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID,
)
from tools import qmt_research_contract


RUN_SCHEMA = "chanlun-fixed-year-qmt-run"
DEFAULT_WARMUP_START = date(2025, 5, 1)
DEFAULT_REQUESTED_START = date(2025, 7, 25)
DEFAULT_EFFECTIVE_START = date(2025, 8, 1)
DEFAULT_END = date(2026, 7, 24)
DEFAULT_PIT_SNAPSHOT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    code: str
    sector_id: str
    warmup_start: date
    requested_start: date
    requested_end: date
    effective_start: date
    algorithm_revision: str
    target: str
    security_master: SecurityMasterRecord
    memberships: tuple[SectorMembershipChange, ...]
    factors: tuple[QmtFactorAt, ...]


def _algorithm_revision(
    hashes: Sequence[tuple[str, str]] | None = None,
) -> str:
    values = tuple(
        qmt_research_contract.algorithm_hashes() if hashes is None else hashes
    )
    payload = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fact_algorithm_revision(
    hashes: Sequence[tuple[str, str]] | None = None,
) -> str:
    values = tuple(
        qmt_research_contract.fact_algorithm_hashes() if hashes is None else hashes
    )
    return _algorithm_revision(values)


@lru_cache(maxsize=1)
def _current_fact_algorithm_revision() -> str:
    """Validate once per long-lived worker process, not once per symbol."""

    return _fact_algorithm_revision()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--warmup-start", type=_parse_date, default=DEFAULT_WARMUP_START
    )
    result.add_argument(
        "--start",
        type=_parse_date,
        default=DEFAULT_REQUESTED_START,
        help="requested performance range start",
    )
    result.add_argument(
        "--effective-start",
        type=_parse_date,
        default=DEFAULT_EFFECTIVE_START,
        help="first tradable session after the uniform 1m warm-up",
    )
    result.add_argument("--end", type=_parse_date, default=DEFAULT_END)
    result.add_argument("--workers", type=_positive_int, default=6)
    result.add_argument("--limit", type=_positive_int)
    result.add_argument("--codes", help="optional comma-separated normalized codes")
    result.add_argument(
        "--pit-snapshot",
        type=Path,
        default=DEFAULT_PIT_SNAPSHOT,
        help="immutable effective-dated QMT/CNInfo metadata snapshot",
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=Path("audit/chanlun_trading_system_backtest/fixed_year_2025_2026"),
    )
    result.add_argument("--force", action="store_true")
    return result


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "symbols" / f"{code.replace('.', '_')}.pkl"


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # 已完成标的清单会增长到数千条；工作进程池在内存中持有结构状态时，
    # 不要同时物化一份巨大的 Unicode 字符串及其编码字节。
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_fact(path: Path, request: WorkerRequest) -> SymbolResearchFacts | None:
    try:
        value = pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError):
        return None
    if not isinstance(value, SymbolResearchFacts):
        return None
    if (
        value.schema != FACT_SCHEMA
        or value.algorithm_revision != request.algorithm_revision
        or value.code != request.code
        or value.sector_id != request.sector_id
        or value.requested_start != request.requested_start
        or value.requested_end != request.requested_end
        or value.effective_start != request.effective_start
        or value.security_master != request.security_master
        or value.memberships != request.memberships
        or value.factors != request.factors
    ):
        return None
    return value


def _fact_summary(fact: SymbolResearchFacts) -> dict[str, object]:
    return {
        "code": fact.code,
        "sector_id": fact.sector_id,
        "algorithm_revision": fact.algorithm_revision,
        "source_revision": fact.source_revision,
        "row_counts": dict(fact.row_counts),
        "point_counts": {
            "30m": len(fact.thirty_points),
            "5m": len(fact.five_points),
            "1m": len(fact.one_points),
        },
        "evaluation_count": len(fact.evaluations),
        "direction_unavailable_count": fact.direction_unavailable_count,
        "membership_change_count": len(fact.memberships),
        "corporate_action_count": len(fact.factors),
    }


def _worker(request: WorkerRequest) -> dict[str, object]:
    started = time.perf_counter()
    if _current_fact_algorithm_revision() != request.algorithm_revision:
        raise RuntimeError("worker algorithm differs from the frozen run revision")
    target = Path(request.target)
    fact = build_symbol_facts(
        code=request.code,
        sector_id=request.sector_id,
        warmup_start=request.warmup_start,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        effective_start=request.effective_start,
        algorithm_revision=request.algorithm_revision,
        security_master=request.security_master,
        memberships=request.memberships,
        qmt_factors=request.factors,
    )
    if _current_fact_algorithm_revision() != request.algorithm_revision:
        raise RuntimeError("algorithm changed while the symbol fact was built")
    _atomic_bytes(target, pickle.dumps(fact, protocol=pickle.HIGHEST_PROTOCOL))
    return {
        **_fact_summary(fact),
        "seconds": round(time.perf_counter() - started, 3),
    }


def _catalog_scope(
    snapshot: PITMetadataSnapshot,
    *,
    requested_start: date,
    requested_end: date,
) -> tuple[tuple[tuple[str, str], ...], dict[str, object]]:
    # 回测与实时选股共用同一个板块优先范围合同，不能在命令行中再构造另一套
    # 细节不同的个股优先股票池。
    sector_first = build_sector_first_scope(
        snapshot,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    index = PITMetadataIndex(snapshot)
    selected = set(sector_first.selected_symbols)
    scope = tuple(
        (
            row.code,
            (
                index.memberships_for(row.code)[0].sector_id
                if index.memberships_for(row.code)
                else "qmt-sw1:unclassified"
            ),
        )
        for row in snapshot.securities
        if row.code in selected
    )
    return tuple(sorted(scope)), {
        "catalog_revision": "sha256:" + hashlib.sha256(
            repr(snapshot.qmt_sw1_sector_names).encode("utf-8")
        ).hexdigest(),
        "catalog_source": "qmt_sw1_with_cninfo_effective_dates",
        "catalog_sector_count": len(snapshot.qmt_sw1_sector_names),
        "eligible_sector_count": len(snapshot.qmt_sw1_sector_names),
        "membership_edge_count": len(snapshot.memberships),
        "archived_intersecting_symbol_count": len(sector_first.symbols),
        "unique_symbol_count": len(scope),
        "classified_symbol_count": len(scope),
        "unclassified_symbol_count": len(sector_first.rejected_symbols),
        "duplicate_membership_count": 0,
        "selection_path": sector_first.selection_path,
        "selection_order": (
            "POINT_IN_TIME_SECTOR_TRIGGER",
            "POINT_IN_TIME_SECTOR_MEMBERS",
            "INDIVIDUAL_THREE_PROGRAM",
            UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID,
        ),
        "sector_first_scope_sha256": sector_first.content_sha256,
        "etf_proxy_role": sector_first.etf_proxy_role,
        "pit_metadata_schema": snapshot.schema,
        "pit_source_hashes": dict(snapshot.source_hashes),
    }


def _manifest(
    *,
    args: argparse.Namespace,
    catalog: Mapping[str, object],
    selected: tuple[tuple[str, str], ...],
    completed: Mapping[str, Mapping[str, object]],
    failures: Mapping[str, str],
    started_at: datetime,
    algorithm_hashes: Sequence[tuple[str, str]],
    algorithm_revision: str,
    fact_algorithm_hashes: Sequence[tuple[str, str]],
    fact_algorithm_revision: str,
) -> dict[str, object]:
    symbols_with_market_data = sum(
        any(int(value) > 0 for value in dict(row.get("row_counts", {})).values())
        for row in completed.values()
    )
    return {
        "schema": RUN_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "started_at": started_at.isoformat(),
        "complete": len(completed) == len(selected) and not failures,
        "algorithm": {
            "revision": algorithm_revision,
            "hashes": [
                {"path": path, "sha256": digest}
                for path, digest in algorithm_hashes
            ],
        },
        "fact_algorithm": {
            "revision": fact_algorithm_revision,
            "hashes": [
                {"path": path, "sha256": digest}
                for path, digest in fact_algorithm_hashes
            ],
        },
        "request": {
            "warmup_start": args.warmup_start.isoformat(),
            "requested_start": args.start.isoformat(),
            "effective_start": args.effective_start.isoformat(),
            "requested_end": args.end.isoformat(),
            "workers": args.workers,
            "pit_snapshot": str(args.pit_snapshot.resolve()),
            "pit_snapshot_sha256": _file_sha256(args.pit_snapshot.resolve()),
        },
        "catalog": dict(catalog),
        "summary": {
            "selected_symbol_count": len(selected),
            "completed_symbol_count": len(completed),
            "failed_symbol_count": len(failures),
            "evaluation_count": sum(
                int(row.get("evaluation_count", 0)) for row in completed.values()
            ),
            "symbols_with_evaluations": sum(
                int(row.get("evaluation_count", 0)) > 0 for row in completed.values()
            ),
            "symbols_with_market_data": symbols_with_market_data,
            "symbols_without_market_data": (
                len(completed) - symbols_with_market_data
            ),
            "rows_by_frequency": {
                frequency: sum(
                    int(dict(row.get("row_counts", {})).get(frequency, 0))
                    for row in completed.values()
                )
                for frequency in ("30m", "5m", "1m")
            },
        },
        "failures": dict(sorted(failures.items())),
        "symbols": dict(sorted(completed.items())),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.warmup_start <= args.start <= args.effective_start <= args.end:
        raise ValueError("expected warmup_start <= start <= effective_start <= end")
    if args.workers > 16:
        raise ValueError("workers cannot exceed 16")
    algorithm_hashes = qmt_research_contract.algorithm_hashes()
    algorithm_revision = _algorithm_revision(algorithm_hashes)
    fact_algorithm_hashes = qmt_research_contract.fact_algorithm_hashes()
    fact_algorithm_revision = _fact_algorithm_revision(fact_algorithm_hashes)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pit_snapshot_path = args.pit_snapshot.resolve()
    snapshot = load_snapshot(pit_snapshot_path)
    snapshot_index = PITMetadataIndex(snapshot)
    if snapshot.source_start > args.warmup_start or snapshot.source_end < args.end:
        raise ValueError("PIT metadata does not cover the requested replay range")
    scope, catalog_summary = _catalog_scope(
        snapshot,
        requested_start=args.start,
        requested_end=args.end,
    )
    catalog_summary["pit_snapshot_sha256"] = _file_sha256(pit_snapshot_path)
    if args.codes:
        requested = {
            value.strip().upper() for value in args.codes.split(",") if value.strip()
        }
        known = {code for code, _sector_id in scope}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(
                f"codes are outside eligible QMT GICS3 scope: {','.join(unknown)}"
            )
        scope = tuple(row for row in scope if row[0] in requested)
    if args.limit is not None:
        scope = scope[: args.limit]
    if not scope:
        raise RuntimeError("fixed-year QMT scope is empty")
    selected = tuple(scope)
    requests = {
        code: WorkerRequest(
            code=code,
            sector_id=sector_id,
            warmup_start=args.warmup_start,
            requested_start=args.start,
            requested_end=args.end,
            effective_start=args.effective_start,
            algorithm_revision=fact_algorithm_revision,
            target=str(_fact_path(output_dir, code)),
            security_master=snapshot_index.security(code),
            memberships=snapshot_index.memberships_for(code),
            factors=snapshot_index.factors_for(code),
        )
        for code, sector_id in selected
    }
    completed: dict[str, dict[str, object]] = {}
    failures: dict[str, str] = {}
    pending: list[WorkerRequest] = []
    for code, request in requests.items():
        existing = None if args.force else _load_fact(Path(request.target), request)
        if existing is None:
            pending.append(request)
        else:
            completed[code] = _fact_summary(existing)
    started_at = datetime.now().astimezone()
    manifest_path = output_dir / "extract_manifest.json"
    _atomic_json(
        manifest_path,
        _manifest(
            args=args,
            catalog=catalog_summary,
            selected=selected,
            completed=completed,
            failures=failures,
            started_at=started_at,
            algorithm_hashes=algorithm_hashes,
            algorithm_revision=algorithm_revision,
            fact_algorithm_hashes=fact_algorithm_hashes,
            fact_algorithm_revision=fact_algorithm_revision,
        ),
    )
    started = time.perf_counter()
    if pending:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_worker, request): request for request in pending
            }
            for ordinal, future in enumerate(as_completed(futures), start=1):
                request = futures[future]
                try:
                    summary = future.result()
                    completed[request.code] = summary
                    failures.pop(request.code, None)
                except Exception as exc:
                    failures[request.code] = f"{type(exc).__name__}:{exc}"
            # 每个标的已有各自完成落盘同步的事实检查点。每得到一个结果就完整重写清单，
            # 复杂度会增长到 O(N²)，最终压过实际结构计算；重启时可直接发现更新的
            # 单标的文件。
                publish = (
                    ordinal % 25 == 0
                    or ordinal == len(pending)
                    or request.code in failures
                )
                if publish:
                    payload = _manifest(
                        args=args,
                        catalog=catalog_summary,
                        selected=selected,
                        completed=completed,
                        failures=failures,
                        started_at=started_at,
                        algorithm_hashes=algorithm_hashes,
                        algorithm_revision=algorithm_revision,
                        fact_algorithm_hashes=fact_algorithm_hashes,
                        fact_algorithm_revision=fact_algorithm_revision,
                    )
                    _atomic_json(manifest_path, payload)
                    print(
                        json.dumps(
                            {
                                "finished_this_run": ordinal,
                                "pending_this_run": len(pending),
                                "completed_total": len(completed),
                                "selected_total": len(selected),
                                "failed_total": len(failures),
                                "evaluation_count": payload["summary"][
                                    "evaluation_count"
                                ],
                                "elapsed_seconds": round(
                                    time.perf_counter() - started, 1
                                ),
                                "latest_code": request.code,
                                "latest_error": failures.get(request.code),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    final = _manifest(
        args=args,
        catalog=catalog_summary,
        selected=selected,
        completed=completed,
        failures=failures,
        started_at=started_at,
        algorithm_hashes=algorithm_hashes,
        algorithm_revision=algorithm_revision,
        fact_algorithm_hashes=fact_algorithm_hashes,
        fact_algorithm_revision=fact_algorithm_revision,
    )
    if _algorithm_revision() != algorithm_revision:
        raise RuntimeError("algorithm changed during the extraction run")
    if _fact_algorithm_revision() != fact_algorithm_revision:
        raise RuntimeError("fact algorithm changed during the extraction run")
    _atomic_json(manifest_path, final)
    print(json.dumps(final["summary"], ensure_ascii=False, indent=2), flush=True)
    return 0 if final["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
