#!/usr/bin/env python3
"""Recompute every signal-producing symbol on a truncated QMT prefix."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import time as wall_time
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


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
    SecurityMasterRecord,
)
from tools import qmt_research_contract


CN = ZoneInfo("Asia/Shanghai")
AUDIT_SCHEMA = "chanlun-prefix-invariance-audit"


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="explicit sample extraction directory; no full-market default",
    )
    result.add_argument("--workers", type=_positive_int, default=2)
    result.add_argument("--force", action="store_true")
    return result


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _algorithm_revision(hashes: Sequence[tuple[str, str]]) -> str:
    encoded = json.dumps(
        tuple(hashes),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def _current_fact_algorithm_revision() -> str:
    return _algorithm_revision(qmt_research_contract.fact_algorithm_hashes())


def _frozen_algorithm_entry(
    manifest: Mapping[str, object],
    *,
    key: str,
    current_hashes: Sequence[tuple[str, str]],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    raw = manifest.get(key)
    if not isinstance(raw, Mapping):
        raise ValueError(f"extract manifest has no frozen {key}")
    revision = raw.get("revision")
    rows = raw.get("hashes")
    if not isinstance(revision, str) or not isinstance(rows, list):
        raise ValueError(f"extract manifest {key} is malformed")
    hashes = tuple(
        (str(row["path"]), str(row["sha256"]))
        for row in rows
        if isinstance(row, Mapping)
    )
    if len(hashes) != len(rows) or _algorithm_revision(hashes) != revision:
        raise ValueError(f"extract manifest {key} revision is inconsistent")
    if tuple(current_hashes) != hashes:
        raise RuntimeError(f"source code changed after frozen {key}")
    return revision, hashes


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "symbols" / f"{code.replace('.', '_')}.pkl"


def _audit_path(directory: Path, code: str) -> Path:
    return directory / "prefix_audit" / f"{code.replace('.', '_')}.json"


def _semantic_hash(value: object) -> str:
    encoded = repr(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _prefix_end(facts: SymbolResearchFacts) -> date | None:
    candidates = sorted(
        {
            row.observed_at.date()
            for row in facts.evaluations
            if facts.effective_start <= row.observed_at.date() < facts.requested_end
        }
    )
    return None if not candidates else candidates[len(candidates) // 2]


def _prefix_master(
    master: SecurityMasterRecord,
    prefix_end: date,
) -> SecurityMasterRecord:
    return SecurityMasterRecord(
        code=master.code,
        name=master.name,
        listed_from=master.listed_from,
        listed_through=(
            master.listed_through
            if master.listed_through is not None and master.listed_through <= prefix_end
            else None
        ),
    )


def _projection(
    facts: SymbolResearchFacts,
    cutoff: datetime,
) -> tuple[object, ...]:
    decision_times = tuple(
        row.observed_at for row in facts.evaluations if row.observed_at <= cutoff
    )
    semantic_cutoff = max(decision_times, default=cutoff)

    def clipped_visibility(intervals):
        return tuple(
            replace(
                interval,
                visible_until=(
                    None
                    if interval.visible_until is None
                    or interval.visible_until > semantic_cutoff
                    else interval.visible_until
                ),
            )
            for interval in intervals
            if interval.visible_from <= semantic_cutoff
        )

    daily_visibility = clipped_visibility(facts.daily_point_visibility)
    daily_point_ids = {interval.point_id for interval in daily_visibility}
    return (
        tuple(
            point for point in facts.daily_points if point.point_id in daily_point_ids
        ),
        daily_visibility,
        tuple(
            point
            for point in facts.thirty_points
            if point.available_at <= semantic_cutoff
        ),
        clipped_visibility(facts.thirty_point_visibility),
        tuple(
            point
            for point in facts.five_points
            if point.available_at <= semantic_cutoff
        ),
        clipped_visibility(facts.five_point_visibility),
        tuple(
            point for point in facts.one_points if point.available_at <= semantic_cutoff
        ),
        clipped_visibility(facts.one_point_visibility),
        tuple(
            row
            for row in facts.five_minute_warmup
            if row.observed_at <= semantic_cutoff
        ),
        tuple(row for row in facts.evaluations if row.observed_at <= semantic_cutoff),
    )


@dataclass(frozen=True, slots=True)
class Request:
    fact_path: str
    target: str
    warmup_start: date
    algorithm_revision: str


def _worker(request: Request) -> dict[str, object]:
    if _current_fact_algorithm_revision() != request.algorithm_revision:
        raise RuntimeError("worker algorithm differs from frozen extraction")
    path = Path(request.fact_path)
    full = pickle.loads(path.read_bytes())
    if not isinstance(full, SymbolResearchFacts) or full.schema != FACT_SCHEMA:
        raise ValueError("invalid full symbol fact")
    prefix_end = _prefix_end(full)
    if prefix_end is None:
        result = {
            "schema": AUDIT_SCHEMA,
            "code": full.code,
            "algorithm_revision": request.algorithm_revision,
            "full_source_revision": full.source_revision,
            "status": "passed",
            "reason": "no_preterminal_decision_prefix",
            "prefix_end": None,
            "full_fact_sha256": _sha256(path),
        }
        _atomic_json(Path(request.target), result)
        return result
    if full.security_master is None:
        raise ValueError("PIT security master is missing")
    cutoff = datetime.combine(prefix_end, time(15, 0), tzinfo=CN)
    prefix = build_symbol_facts(
        code=full.code,
        sector_id=full.sector_id,
        warmup_start=request.warmup_start,
        requested_start=full.requested_start,
        requested_end=prefix_end,
        effective_start=full.effective_start,
        algorithm_revision=request.algorithm_revision,
        security_master=_prefix_master(full.security_master, prefix_end),
        memberships=tuple(row for row in full.memberships if row.known_at <= cutoff),
        qmt_factors=tuple(
            row for row in full.factors if row.effective_on <= prefix_end
        ),
    )
    expected = _projection(full, cutoff)
    actual = _projection(prefix, cutoff)
    passed = expected == actual
    result = {
        "schema": AUDIT_SCHEMA,
        "code": full.code,
        "algorithm_revision": request.algorithm_revision,
        "full_source_revision": full.source_revision,
        "status": "passed" if passed else "failed",
        "reason": "semantic_prefix_equal" if passed else "semantic_prefix_changed",
        "prefix_end": prefix_end.isoformat(),
        "expected_sha256": _semantic_hash(expected),
        "actual_sha256": _semantic_hash(actual),
        "expected_counts": [len(value) for value in expected],
        "actual_counts": [len(value) for value in actual],
        "full_fact_sha256": _sha256(path),
    }
    _atomic_json(Path(request.target), result)
    return result


def _valid_existing(path: Path, request: Request) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("schema") != AUDIT_SCHEMA
        or raw.get("algorithm_revision") != request.algorithm_revision
        or raw.get("full_fact_sha256") != _sha256(Path(request.fact_path))
    ):
        return None
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.workers > 12:
        raise ValueError("workers cannot exceed 12")
    directory = args.input_dir.resolve()
    manifest_path = directory / "extract_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or not manifest.get("complete"):
        raise RuntimeError("symbol extraction is incomplete")
    request_info = manifest.get("request")
    symbols = manifest.get("symbols")
    if not all(isinstance(value, Mapping) for value in (request_info, symbols)):
        raise ValueError("extract manifest is malformed")
    fact_key = "fact_algorithm" if "fact_algorithm" in manifest else "algorithm"
    fact_current_hashes = (
        qmt_research_contract.fact_algorithm_hashes()
        if fact_key == "fact_algorithm"
        else qmt_research_contract.algorithm_hashes()
    )
    fact_algorithm_revision, fact_algorithm_hashes = _frozen_algorithm_entry(
        manifest,
        key=fact_key,
        current_hashes=fact_current_hashes,
    )
    prefix_algorithm_hashes = qmt_research_contract.prefix_algorithm_hashes()
    prefix_algorithm_revision = _algorithm_revision(prefix_algorithm_hashes)
    warmup_start = date.fromisoformat(str(request_info["warmup_start"]))
    requests: list[Request] = []
    skipped_no_evaluations = 0
    for code, summary in sorted(symbols.items()):
        if not isinstance(summary, Mapping):
            raise ValueError("symbol summary is malformed")
        if int(summary.get("evaluation_count", 0)) <= 0:
            skipped_no_evaluations += 1
            continue
        requests.append(
            Request(
                fact_path=str(_fact_path(directory, str(code))),
                target=str(_audit_path(directory, str(code))),
                warmup_start=warmup_start,
                algorithm_revision=fact_algorithm_revision,
            )
        )
    completed: dict[str, dict[str, object]] = {}
    pending: list[Request] = []
    for request in requests:
        existing = (
            None if args.force else _valid_existing(Path(request.target), request)
        )
        if existing is None:
            pending.append(request)
        else:
            completed[str(existing["code"])] = existing
    started = wall_time.perf_counter()
    if pending:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_worker, row): row for row in pending}
            for ordinal, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                completed[str(row["code"])] = row
                if ordinal % 25 == 0 or ordinal == len(pending):
                    print(
                        json.dumps(
                            {
                                "stage": "prefix_invariance",
                                "finished": ordinal,
                                "pending": len(pending),
                                "passed": sum(
                                    item["status"] == "passed"
                                    for item in completed.values()
                                ),
                                "failed": sum(
                                    item["status"] != "passed"
                                    for item in completed.values()
                                ),
                                "seconds": round(wall_time.perf_counter() - started, 1),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    failures = tuple(
        sorted(code for code, row in completed.items() if row["status"] != "passed")
    )
    output = {
        "schema": AUDIT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": "passed"
        if not failures and len(completed) == len(requests)
        else "failed",
        # ``algorithm_revision`` remains as a compatibility alias for readers
        # of the earlier audit schema.  It now identifies this stage alone,
        # rather than coupling Prefix to report/finalizer implementation.
        "algorithm_revision": prefix_algorithm_revision,
        "prefix_algorithm_revision": prefix_algorithm_revision,
        "fact_algorithm_revision": fact_algorithm_revision,
        "pit_snapshot_sha256": request_info["pit_snapshot_sha256"],
        "extract_manifest_sha256": _sha256(manifest_path),
        "signal_producing_symbol_count": len(requests),
        "audited_symbol_count": len(completed),
        "skipped_no_evaluation_symbol_count": skipped_no_evaluations,
        "failed_codes": failures,
        "checkpoint_tree_sha256": _semantic_hash(
            tuple((code, completed[code]) for code in sorted(completed))
        ),
    }
    output_path = directory / "prefix_invariance_audit.json"
    if qmt_research_contract.prefix_algorithm_hashes() != prefix_algorithm_hashes:
        raise RuntimeError("prefix algorithm changed during prefix audit")
    current_fact_hashes = (
        qmt_research_contract.fact_algorithm_hashes()
        if fact_key == "fact_algorithm"
        else qmt_research_contract.algorithm_hashes()
    )
    if current_fact_hashes != fact_algorithm_hashes:
        raise RuntimeError("fact algorithm changed during prefix audit")
    _atomic_json(output_path, output)
    print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
    return 0 if output["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
