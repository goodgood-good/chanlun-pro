#!/usr/bin/env python3
"""Certify, execute, and publish the point-in-time fixed-year QMT replay."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import pickle
import sys
import time as wall_time
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.backtest.data_audit import DataEvidence
from chanlun.decision_support.trading_system.backtest.causality_gate_contract import (
    CAUSALITY_GATE_PROVEN_CONTROLS,
    CAUSALITY_GATE_SCHEMA,
    causality_gate_state_is_consistent,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FACT_SCHEMA,
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
    SparseEvaluationFact,
    SymbolResearchFacts,
    load_qmt_frame,
    run_sparse_portfolio,
    sector_facts_from_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataIndex,
    PITMetadataSnapshot,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.pit_scope import (
    validate_scope_proof,
)
from chanlun.decision_support.trading_system.backtest.pit_sector import (
    PIT_SW1_COMPOSITE_PROVIDER,
    build_pit_sw1_composite,
    candidate_codes_for_pit_sector,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    qmt_local_kline_path,
    resolve_qmt_local_data_dir,
)
from chanlun.decision_support.trading_system.backtest.report import (
    BacktestEvaluationResult,
    build_report,
)
from chanlun.decision_support.trading_system.lifecycle import (
    current_five_minute_setup_points,
    five_minute_setup_is_executable,
    five_minute_setup_is_in_policy_scope,
    match_one_minute_nesting_witness_for_point,
    structural_point_occurrence_id,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_MINIMUM_BARS_BY_FREQUENCY,
)
from chanlun.decision_support.fingerprints import sha256_json
from tools import qmt_research_contract


_SECTOR_CACHE_METADATA_SCHEMA = "chanlun-pit-sector-cache-metadata-v1"
_SECTOR_COMPOSITE_CACHE_METADATA_SCHEMA = (
    "chanlun-pit-sector-composite-cache-metadata-v1"
)


def _positive_decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("value must be a positive decimal")
    return result


def _positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="explicit sample extraction directory; no full-market default",
    )
    result.add_argument(
        "--report",
        type=Path,
        required=True,
        help="explicit report target paired with the selected input directory",
    )
    result.add_argument(
        "--initial-cash", type=_positive_decimal, default=Decimal("1000000")
    )
    result.add_argument("--bootstrap-repetitions", type=int, default=2000)
    result.add_argument("--sector-workers", type=_positive_int, default=3)
    result.add_argument("--max-sector-count", type=_positive_int, default=12)
    result.add_argument(
        "--max-sector-closure",
        type=_positive_int,
        default=2000,
        help="maximum unique all-member sector reference symbols before QMT I/O",
    )
    result.add_argument(
        "--confirm-large-sector-scope",
        action="store_true",
        help="independently authorize a sector reference scope above its budgets",
    )
    result.add_argument("--force-sectors", action="store_true")
    result.add_argument(
        "--reuse-sector-cache",
        action="store_true",
        help=(
            "reuse content-addressed sector facts when their PIT, timeline, "
            "implementation, and local QMT file inventory are unchanged"
        ),
    )
    return result


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_bytes(
        path,
        (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _checkpoint_tree(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _algorithm_revision(hashes: Sequence[tuple[str, str]]) -> str:
    encoded = json.dumps(
        tuple(hashes), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sector_algorithm_revision(
    fact_algorithm_hashes: Sequence[tuple[str, str]],
    sector_composite_algorithm_revision: str,
) -> str:
    """Bind sector checkpoints without coupling them to report/UI changes."""

    pit_sector_path = (
        PROJECT_ROOT
        / "src/chanlun/decision_support/trading_system/backtest/pit_sector.py"
    )
    orchestration_source = "\n".join(
        inspect.getsource(function)
        for function in (
            _sector_revision,
            _build_one_sector_fact,
        )
    ).encode("utf-8")
    extra = (
        (
            "src/chanlun/decision_support/trading_system/backtest/pit_sector.py",
            _sha256(pit_sector_path),
        ),
        (
            "tools/finalize_qmt_pit_fixed_year.py#sector_fact_orchestration",
            "sha256:" + hashlib.sha256(orchestration_source).hexdigest(),
        ),
        (
            "sector-composite-algorithm-revision",
            sector_composite_algorithm_revision,
        ),
    )
    return _algorithm_revision(tuple(sorted((*fact_algorithm_hashes, *extra))))


def _sector_composite_algorithm_revision() -> str:
    """Identify only code that can change raw all-member sector prices."""

    return _algorithm_revision(
        qmt_research_contract.sector_composite_algorithm_hashes()
    )


def _frozen_fact_algorithm(
    manifest: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    raw = manifest.get("fact_algorithm")
    if raw is None:
        raise ValueError("extract manifest has no frozen fact algorithm")
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest fact algorithm is malformed")
    revision = raw.get("revision")
    rows = raw.get("hashes")
    if not isinstance(revision, str) or not isinstance(rows, list):
        raise ValueError("extract manifest fact algorithm is malformed")
    hashes = tuple(
        (str(row["path"]), str(row["sha256"]))
        for row in rows
        if isinstance(row, Mapping)
    )
    if len(hashes) != len(rows) or _algorithm_revision(hashes) != revision:
        raise ValueError("extract manifest fact algorithm revision is inconsistent")
    if qmt_research_contract.fact_algorithm_hashes() != hashes:
        raise RuntimeError("symbol-fact source code changed after extraction")
    return revision, hashes


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "symbols" / f"{code.replace('.', '_')}.pkl"


def _load_symbols(
    directory: Path,
    manifest: Mapping[str, object],
    algorithm_revision: str,
) -> tuple[SymbolResearchFacts, ...]:
    raw = manifest.get("symbols")
    if not isinstance(raw, Mapping):
        raise ValueError("extract manifest has no symbol map")
    output: list[SymbolResearchFacts] = []
    for code in sorted(raw):
        path = _fact_path(directory, str(code))
        value = pickle.loads(path.read_bytes())
        if (
            not isinstance(value, SymbolResearchFacts)
            or value.schema != FACT_SCHEMA
            or value.algorithm_revision != algorithm_revision
            or value.code != code
        ):
            raise ValueError(f"invalid symbol checkpoint: {code}")
        output.append(value)
    return tuple(output)


def _sector_path(directory: Path, sector_id: str) -> Path:
    return directory / "pit_sectors" / f"{sector_id.rsplit(':', 1)[-1]}.pkl"


def _sector_cache_metadata_path(directory: Path, sector_id: str) -> Path:
    return directory / "pit_sectors" / f"{sector_id.rsplit(':', 1)[-1]}.cache.json"


def _sector_composite_path(directory: Path, sector_id: str) -> Path:
    return directory / "pit_sector_composites" / f"{sector_id.rsplit(':', 1)[-1]}.pkl"


def _sector_composite_metadata_path(directory: Path, sector_id: str) -> Path:
    return (
        directory
        / "pit_sector_composites"
        / f"{sector_id.rsplit(':', 1)[-1]}.cache.json"
    )


def _sector_source_inventory_revision(
    snapshot: PITMetadataSnapshot,
    sector_id: str,
    *,
    warmup_start: date,
    requested_end: date,
) -> str | None:
    """Fingerprint cheap immutable-file facts before trusting a sector cache.

    Per-symbol facts already treat their completed checkpoint as immutable until
    an explicit force run.  The fast sector path follows the same contract while
    additionally invalidating when the selected QMT data root or a contributing
    local 5m file changes in path, size, or nanosecond modification time.
    """

    data_dir = resolve_qmt_local_data_dir()
    if data_dir is None:
        return None
    member_codes = candidate_codes_for_pit_sector(
        snapshot,
        sector_id,
        start_at=datetime.combine(
            warmup_start,
            time(9, 30),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        end_at=datetime.combine(
            requested_end,
            time(15, 0),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
    )
    rows: list[dict[str, object]] = []
    for code in dict.fromkeys(("SH.000001", *member_codes)):
        path = qmt_local_kline_path(data_dir, code, "5m")
        try:
            relative_path = path.relative_to(data_dir).as_posix()
        except ValueError:
            relative_path = str(path.resolve())
        try:
            status = path.stat()
        except FileNotFoundError:
            rows.append(
                {
                    "code": code,
                    "path": relative_path,
                    "state": "missing",
                }
            )
        else:
            rows.append(
                {
                    "code": code,
                    "path": relative_path,
                    "state": "present",
                    "size": status.st_size,
                    "mtime_ns": status.st_mtime_ns,
                }
            )
    return sha256_json(
        {
            "schema": "chanlun-qmt-sector-source-inventory-v1",
            "data_dir": str(data_dir),
            "sector_id": sector_id,
            "files": rows,
        }
    )


def _sector_composite_cache_context_revision(
    *,
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    sector_id: str,
    warmup_start: date,
    requested_end: date,
    sector_composite_algorithm_revision: str,
) -> str | None:
    source_inventory_revision = _sector_source_inventory_revision(
        snapshot,
        sector_id,
        warmup_start=warmup_start,
        requested_end=requested_end,
    )
    if source_inventory_revision is None:
        return None
    return sha256_json(
        {
            "schema": _SECTOR_COMPOSITE_CACHE_METADATA_SCHEMA,
            "snapshot_sha256": snapshot_hash,
            "sector_id": sector_id,
            "warmup_start": warmup_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "sector_composite_algorithm_revision": (
                sector_composite_algorithm_revision
            ),
            "source_inventory_revision": source_inventory_revision,
        }
    )


def _load_fast_cached_sector_composite(
    path: Path,
    metadata_path: Path,
    *,
    sector_id: str,
    sector_composite_algorithm_revision: str,
    cache_context_revision: str,
) -> pd.DataFrame | None:
    if not path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("schema") != _SECTOR_COMPOSITE_CACHE_METADATA_SCHEMA
        or metadata.get("sector_id") != sector_id
        or metadata.get("sector_composite_algorithm_revision")
        != sector_composite_algorithm_revision
        or metadata.get("cache_context_revision") != cache_context_revision
        or metadata.get("artifact_sha256") != _sha256(path)
    ):
        return None
    try:
        frame = pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.PickleError, ValueError, AttributeError):
        return None
    required = {"code", "date", "open", "high", "low", "close", "volume"}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
        return None
    return frame


def _write_sector_composite_cache(
    *,
    path: Path,
    metadata_path: Path,
    frame: pd.DataFrame,
    sector_id: str,
    sector_composite_algorithm_revision: str,
    cache_context_revision: str,
) -> None:
    _atomic_bytes(path, pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL))
    _atomic_json(
        metadata_path,
        {
            "schema": _SECTOR_COMPOSITE_CACHE_METADATA_SCHEMA,
            "sector_id": sector_id,
            "sector_composite_algorithm_revision": (
                sector_composite_algorithm_revision
            ),
            "cache_context_revision": cache_context_revision,
            "artifact_sha256": _sha256(path),
        },
    )


def _load_or_build_sector_composite(
    *,
    directory: Path,
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    sector_id: str,
    warmup_start: date,
    requested_end: date,
    sector_composite_algorithm_revision: str,
    force: bool,
    reuse_cache: bool,
) -> tuple[pd.DataFrame, str]:
    path = _sector_composite_path(directory, sector_id)
    metadata_path = _sector_composite_metadata_path(directory, sector_id)
    context_revision = _sector_composite_cache_context_revision(
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        sector_id=sector_id,
        warmup_start=warmup_start,
        requested_end=requested_end,
        sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
    )
    cached = None
    if reuse_cache and not force and context_revision is not None:
        cached = _load_fast_cached_sector_composite(
            path,
            metadata_path,
            sector_id=sector_id,
            sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
            cache_context_revision=context_revision,
        )
    if cached is not None:
        return cached, "fast_hit"
    frame = build_pit_sw1_composite(
        snapshot=snapshot,
        sector_id=sector_id,
        start_at=datetime.combine(
            warmup_start,
            time(9, 30),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        end_at=datetime.combine(
            requested_end,
            time(15, 0),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
    )
    if reuse_cache and context_revision is not None:
        _write_sector_composite_cache(
            path=path,
            metadata_path=metadata_path,
            frame=frame,
            sector_id=sector_id,
            sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
            cache_context_revision=context_revision,
        )
    return frame, "rebuilt"


def _sector_cache_context_revision(
    *,
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    sector_id: str,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    warmup_start: date,
    requested_end: date,
    sector_algorithm_revision: str,
) -> str | None:
    source_inventory_revision = _sector_source_inventory_revision(
        snapshot,
        sector_id,
        warmup_start=warmup_start,
        requested_end=requested_end,
    )
    if source_inventory_revision is None:
        return None
    return sha256_json(
        {
            "schema": _SECTOR_CACHE_METADATA_SCHEMA,
            "snapshot_sha256": snapshot_hash,
            "sector_id": sector_id,
            "warmup_start": warmup_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "observed_times": [value.isoformat() for value in observed_times],
            "expected_closes": [value.isoformat() for value in expected_closes],
            "sector_algorithm_revision": sector_algorithm_revision,
            "source_inventory_revision": source_inventory_revision,
        }
    )


def _load_fast_cached_sector(
    path: Path,
    metadata_path: Path,
    *,
    sector_id: str,
    sector_algorithm_revision: str,
    cache_context_revision: str,
    observed_times: Sequence[datetime],
) -> SectorResearchFacts | None:
    if not path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("schema") != _SECTOR_CACHE_METADATA_SCHEMA
        or metadata.get("sector_id") != sector_id
        or metadata.get("sector_algorithm_revision") != sector_algorithm_revision
        or metadata.get("cache_context_revision") != cache_context_revision
        or metadata.get("artifact_sha256") != _sha256(path)
    ):
        return None
    source_revision = metadata.get("source_revision")
    if not isinstance(source_revision, str):
        return None
    cached = _load_cached_sector(
        path,
        algorithm_revision=sector_algorithm_revision,
        source_revision=source_revision,
    )
    if cached is None or cached.sector_id != sector_id:
        return None
    cached_times = tuple(value for value, _assessment in cached.assessments)
    return cached if cached_times == tuple(observed_times) else None


def _write_sector_cache_metadata(
    metadata_path: Path,
    artifact_path: Path,
    *,
    facts: SectorResearchFacts,
    sector_algorithm_revision: str,
    cache_context_revision: str,
) -> None:
    _atomic_json(
        metadata_path,
        {
            "schema": _SECTOR_CACHE_METADATA_SCHEMA,
            "sector_id": facts.sector_id,
            "sector_algorithm_revision": sector_algorithm_revision,
            "cache_context_revision": cache_context_revision,
            "source_revision": facts.source_revision,
            "artifact_sha256": _sha256(artifact_path),
        },
    )


def _sector_revision(
    *,
    snapshot_hash: str,
    sector_id: str,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    frame: pd.DataFrame,
) -> str:
    digest = hashlib.sha256()
    digest.update(snapshot_hash.encode("ascii"))
    digest.update(sector_id.encode("utf-8"))
    digest.update(repr(tuple(observed_times)).encode("utf-8"))
    digest.update(repr(tuple(expected_closes)).encode("utf-8"))
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True), index=False, categorize=False
        )
        .to_numpy(dtype="uint64", copy=False)
        .tobytes()
    )
    return "sha256:" + digest.hexdigest()


def _load_cached_sector(
    path: Path,
    *,
    algorithm_revision: str,
    source_revision: str,
) -> SectorResearchFacts | None:
    try:
        value = pickle.loads(path.read_bytes())
    except (OSError, EOFError, pickle.PickleError, ValueError, AttributeError):
        return None
    if (
        not isinstance(value, SectorResearchFacts)
        or value.schema != SECTOR_FACT_SCHEMA
        or value.algorithm_revision != algorithm_revision
        or value.source_revision != source_revision
    ):
        return None
    return value


def _build_one_sector_fact(
    *,
    directory: Path,
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    names: Mapping[str, str],
    sector_id: str,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    warmup_start: date,
    requested_end: date,
    sector_algorithm_revision: str,
    sector_composite_algorithm_revision: str,
    force: bool,
    reuse_cache: bool,
) -> tuple[SectorResearchFacts, dict[str, object]]:
    path = _sector_path(directory, sector_id)
    metadata_path = _sector_cache_metadata_path(directory, sector_id)
    cache_context_revision = _sector_cache_context_revision(
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        sector_id=sector_id,
        observed_times=observed_times,
        expected_closes=expected_closes,
        warmup_start=warmup_start,
        requested_end=requested_end,
        sector_algorithm_revision=sector_algorithm_revision,
    )
    cached = None
    if reuse_cache and not force and cache_context_revision is not None:
        cached = _load_fast_cached_sector(
            path,
            metadata_path,
            sector_id=sector_id,
            sector_algorithm_revision=sector_algorithm_revision,
            cache_context_revision=cache_context_revision,
            observed_times=observed_times,
        )
    cache_state = "fast_hit"
    composite_cache_state = "not_needed"
    if cached is not None:
        facts = cached
    else:
        frame, composite_cache_state = _load_or_build_sector_composite(
            directory=directory,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            sector_id=sector_id,
            warmup_start=warmup_start,
            requested_end=requested_end,
            sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
            force=force,
            reuse_cache=reuse_cache,
        )
        revision = _sector_revision(
            snapshot_hash=snapshot_hash,
            sector_id=sector_id,
            observed_times=observed_times,
            expected_closes=expected_closes,
            frame=frame,
        )
        cached = (
            None
            if force
            else _load_cached_sector(
                path,
                algorithm_revision=sector_algorithm_revision,
                source_revision=revision,
            )
        )
        if cached is None:
            member_count = len(
                candidate_codes_for_pit_sector(
                    snapshot,
                    sector_id,
                    start_at=datetime.combine(
                        warmup_start,
                        time(9, 30),
                        tzinfo=snapshot.captured_at.tzinfo,
                    ),
                    end_at=datetime.combine(
                        requested_end,
                        time(15, 0),
                        tzinfo=snapshot.captured_at.tzinfo,
                    ),
                )
            )
            facts = sector_facts_from_frame(
                sector_id=sector_id,
                sector_name=names[sector_id],
                member_count=member_count,
                frame=frame,
                observed_times=observed_times,
                algorithm_revision=sector_algorithm_revision,
                source_revision=revision,
                market_data_source=PIT_SW1_COMPOSITE_PROVIDER,
                expected_closes=expected_closes,
            )
            _atomic_bytes(
                path,
                pickle.dumps(facts, protocol=pickle.HIGHEST_PROTOCOL),
            )
            cache_state = "rebuilt"
        else:
            facts = cached
            cache_state = "verified_hit"
        if cache_context_revision is not None:
            _write_sector_cache_metadata(
                metadata_path,
                path,
                facts=facts,
                sector_algorithm_revision=sector_algorithm_revision,
                cache_context_revision=cache_context_revision,
            )
    return facts, {
        "stage": "pit_sector",
        "sector": sector_id,
        "events": len(observed_times),
        "rows": facts.row_count,
        "members": facts.member_count,
        "error": facts.error,
        "cache": cache_state,
        "composite_cache": composite_cache_state,
    }


def _build_sector_facts(
    *,
    directory: Path,
    symbols: Sequence[SymbolResearchFacts],
    snapshot: PITMetadataSnapshot,
    snapshot_hash: str,
    warmup_start: date,
    requested_end: date,
    sector_algorithm_revision: str,
    sector_composite_algorithm_revision: str,
    force: bool,
    reuse_cache: bool,
    workers: int,
) -> dict[str, SectorResearchFacts]:
    times_by_sector: dict[str, set[datetime]] = {}
    for facts in symbols:
        for evaluation in facts.evaluations:
            if evaluation.sector_id is not None:
                times_by_sector.setdefault(evaluation.sector_id, set()).add(
                    evaluation.observed_at
                )
    names = dict(snapshot.qmt_sw1_sector_names)
    market_frame = load_qmt_frame(
        "SH.000001",
        "30m",
        start_at=datetime.combine(
            warmup_start,
            time(9, 30),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        end_at=datetime.combine(
            requested_end,
            time(15, 0),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        factors=pd.DataFrame(),
    )
    expected_closes = tuple(
        pd.Timestamp(value).to_pydatetime() for value in market_frame["date"]
    )
    if not expected_closes:
        raise RuntimeError("QMT market 30m reference timeline is unavailable")
    sector_ids = tuple(sorted(times_by_sector))
    unknown = tuple(value for value in sector_ids if value not in names)
    if unknown:
        raise ValueError(f"unknown PIT sector at evaluation: {unknown[0]}")

    def build(sector_id: str) -> tuple[SectorResearchFacts, dict[str, object]]:
        facts, diagnostic = _build_one_sector_fact(
            directory=directory,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            names=names,
            sector_id=sector_id,
            observed_times=tuple(sorted(times_by_sector[sector_id])),
            expected_closes=expected_closes,
            warmup_start=warmup_start,
            requested_end=requested_end,
            sector_algorithm_revision=sector_algorithm_revision,
            sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
            force=force,
            reuse_cache=reuse_cache,
        )
        return facts, diagnostic

    completed: dict[str, SectorResearchFacts] = {}
    if workers == 1 or len(sector_ids) <= 1:
        for sector_id in sector_ids:
            facts, diagnostic = build(sector_id)
            completed[sector_id] = facts
            print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(sector_ids))) as executor:
            futures = {
                executor.submit(
                    _build_one_sector_fact,
                    directory=directory,
                    snapshot=snapshot,
                    snapshot_hash=snapshot_hash,
                    names=names,
                    sector_id=sector_id,
                    observed_times=tuple(sorted(times_by_sector[sector_id])),
                    expected_closes=expected_closes,
                    warmup_start=warmup_start,
                    requested_end=requested_end,
                    sector_algorithm_revision=sector_algorithm_revision,
                    sector_composite_algorithm_revision=(
                        sector_composite_algorithm_revision
                    ),
                    force=force,
                    reuse_cache=reuse_cache,
                ): sector_id
                for sector_id in sector_ids
            }
            for future in as_completed(futures):
                sector_id = futures[future]
                facts, diagnostic = future.result()
                completed[sector_id] = facts
                print(json.dumps(diagnostic, ensure_ascii=False), flush=True)
    return {sector_id: completed[sector_id] for sector_id in sector_ids}


def _sector_reference_scope(
    *,
    symbols: Sequence[SymbolResearchFacts],
    snapshot: PITMetadataSnapshot,
    warmup_start: date,
    requested_end: date,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    sector_ids = tuple(
        sorted(
            {
                evaluation.sector_id
                for facts in symbols
                for evaluation in facts.evaluations
                if evaluation.sector_id is not None
            }
        )
    )
    closure_codes = tuple(
        sorted(
            {
                code
                for sector_id in sector_ids
                for code in candidate_codes_for_pit_sector(
                    snapshot,
                    sector_id,
                    start_at=datetime.combine(
                        warmup_start,
                        time(9, 30),
                        tzinfo=snapshot.captured_at.tzinfo,
                    ),
                    end_at=datetime.combine(
                        requested_end,
                        time(15, 0),
                        tzinfo=snapshot.captured_at.tzinfo,
                    ),
                )
            }
        )
    )
    return sector_ids, closure_codes


def _validate_sector_reference_budget(
    *,
    sector_ids: Sequence[str],
    closure_codes: Sequence[str],
    max_sector_count: int,
    max_sector_closure: int,
    confirmed_large_scope: bool,
) -> None:
    if confirmed_large_scope:
        return
    failures: list[str] = []
    if len(sector_ids) > max_sector_count:
        failures.append(f"{len(sector_ids)} sectors exceed budget {max_sector_count}")
    if len(closure_codes) > max_sector_closure:
        failures.append(
            f"{len(closure_codes)} reference symbols exceed budget {max_sector_closure}"
        )
    if failures:
        raise ValueError(
            "sector reference scope requires explicit large-scope confirmation: "
            + "; ".join(failures)
        )


def _new_exact_buy_nesting_pairs(
    facts: SymbolResearchFacts,
    evaluation: SparseEvaluationFact,
    *,
    setup_points: Sequence[StructuralPoint] | None = None,
) -> tuple[tuple[StructuralPoint, StructuralPoint], ...]:
    """Return canonical 5m/1m pairs born at this evaluation close."""

    observed_at = evaluation.observed_at
    if setup_points is None:
        visible_setup_ids = {
            interval.point_id
            for interval in facts.five_point_visibility
            if interval.contains(observed_at)
        }
        candidate_setups = tuple(
            point for point in facts.five_points if point.point_id in visible_setup_ids
        )
    else:
        candidate_setups = tuple(setup_points)
    visible_setups = tuple(
        point
        for point in candidate_setups
        if is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
        and five_minute_setup_is_in_policy_scope(point)
    )
    current_setups = current_five_minute_setup_points(
        visible_setups,
        as_of=observed_at,
    )
    output: list[tuple[StructuralPoint, StructuralPoint]] = []
    for setup in current_setups:
        if setup.side != "buy" or not five_minute_setup_is_executable(
            setup,
            as_of=observed_at,
        ):
            continue
        witness = match_one_minute_nesting_witness_for_point(
            setup,
            facts.one_points,
            as_of=observed_at,
        )
        if (
            witness is not None
            and max(
                setup.available_at,
                witness.available_at,
            )
            == observed_at
        ):
            output.append((setup, witness))
    return tuple(output)


def _decision_funnel_diagnostics(
    *,
    symbols: Sequence[SymbolResearchFacts],
    sectors: Mapping[str, SectorResearchFacts],
) -> dict[str, object]:
    """Summarize the causal 5m-to-1m funnel without replaying market orders.

    Counts are taken from the same current-state ledgers and exact joint-
    knowledge boundaries consumed by ``run_sparse_portfolio``.  Keeping this
    structural funnel next to the PnL report prevents a sparse trade count from
    being mistaken for a missing first/second/third-class classifier.
    """

    point_types = ("1buy", "1sell", "2buy", "2sell", "3buy", "3sell")

    def count_document(values: Counter[str]) -> dict[str, int]:
        return {point_type: int(values[point_type]) for point_type in point_types}

    signal_events: Counter[str] = Counter()
    unique_setups: dict[tuple[str, str], str] = {}
    exact_boundary_events: Counter[str] = Counter()
    exact_boundary_setups: dict[tuple[str, str], str] = {}
    sector_regimes: Counter[str] = Counter()
    market_gates: Counter[str] = Counter()
    sector_gates: Counter[str] = Counter()
    symbol_gates: Counter[str] = Counter()
    evaluations_without_signal = 0
    boundary_events_without_gate = 0
    sector_assessment_missing = 0
    sector_assessments = {
        sector_id: dict(facts.assessments)
        for sector_id, facts in sectors.items()
    }

    for facts in symbols:
        points_by_id = {point.point_id: point for point in facts.five_points}
        for evaluation in facts.evaluations:
            if facts.five_point_visibility:
                visible_ids = {
                    interval.point_id
                    for interval in facts.five_point_visibility
                    if interval.contains(evaluation.observed_at)
                }
                visible_points = tuple(
                    points_by_id[point_id]
                    for point_id in sorted(visible_ids)
                    if point_id in points_by_id
                )
            else:
                visible_points = tuple(
                    point
                    for point in facts.five_points
                    if point.available_at <= evaluation.observed_at
                )
            current_points = tuple(
                point
                for point in current_five_minute_setup_points(
                    visible_points,
                    as_of=evaluation.observed_at,
                )
                if five_minute_setup_is_executable(
                    point,
                    as_of=evaluation.observed_at,
                )
            )
            if not current_points:
                evaluations_without_signal += 1
            for point in current_points:
                signal_events[point.point_type] += 1
                occurrence = structural_point_occurrence_id(point)
                key = (facts.code, occurrence)
                previous_type = unique_setups.setdefault(key, point.point_type)
                if previous_type != point.point_type:
                    raise ValueError(
                        "one 5m setup occurrence changed canonical point type"
                    )

            resolved_sector_id = evaluation.sector_id or facts.sector_id
            sector = sector_assessments.get(resolved_sector_id, {}).get(
                evaluation.observed_at
            )
            if sector is None:
                sector_assessment_missing += 1
            else:
                sector_regimes[sector.regime] += 1

            exact_pairs = _new_exact_buy_nesting_pairs(facts, evaluation)
            for setup, _witness in exact_pairs:
                exact_boundary_events[setup.point_type] += 1
                occurrence = structural_point_occurrence_id(setup)
                key = (facts.code, occurrence)
                previous_type = exact_boundary_setups.setdefault(
                    key,
                    setup.point_type,
                )
                if previous_type != setup.point_type:
                    raise ValueError(
                        "one exact nesting setup changed canonical point type"
                    )
            if exact_pairs:
                gates = evaluation.higher_timeframe_gates
                if gates is None:
                    boundary_events_without_gate += 1
                else:
                    market_gates[gates.market.gate] += 1
                    sector_gates[gates.sector.gate] += 1
                    symbol_gates[gates.symbol.gate] += 1

    unique_setup_counts = Counter(unique_setups.values())
    exact_setup_counts = Counter(exact_boundary_setups.values())
    evaluation_count = sum(len(facts.evaluations) for facts in symbols)
    buy_signal_events = sum(
        signal_events[point_type] for point_type in ("1buy", "2buy", "3buy")
    )
    buy_setups = sum(
        unique_setup_counts[point_type]
        for point_type in ("1buy", "2buy", "3buy")
    )
    exact_events = sum(exact_boundary_events.values())
    exact_setups = len(exact_boundary_setups)
    return {
        "schema": "chanlun-fixed-year-decision-funnel-v1",
        "causal_evaluation_count": evaluation_count,
        "evaluation_without_current_5m_signal_count": (
            evaluations_without_signal
        ),
        "five_minute_signal_event_count": sum(signal_events.values()),
        "five_minute_signal_events_by_point_type": count_document(signal_events),
        "unique_five_minute_setup_count": len(unique_setups),
        "unique_five_minute_setups_by_point_type": count_document(
            unique_setup_counts
        ),
        "buy_signal_event_count": buy_signal_events,
        "unique_buy_setup_count": buy_setups,
        "exact_one_minute_nesting_boundary_event_count": exact_events,
        "exact_one_minute_nesting_boundary_events_by_five_minute_point_type": (
            count_document(exact_boundary_events)
        ),
        "unique_five_minute_setups_with_exact_one_minute_boundary_count": (
            exact_setups
        ),
        "unique_five_minute_setups_with_exact_one_minute_boundary_by_point_type": (
            count_document(exact_setup_counts)
        ),
        "boundary_event_without_higher_timeframe_gate_count": (
            boundary_events_without_gate
        ),
        "higher_timeframe_market_gates_at_boundary": dict(
            sorted(market_gates.items())
        ),
        "higher_timeframe_sector_gates_at_boundary": dict(
            sorted(sector_gates.items())
        ),
        "higher_timeframe_symbol_gates_at_boundary": dict(
            sorted(symbol_gates.items())
        ),
        "sector_regimes_at_causal_evaluation": dict(
            sorted(sector_regimes.items())
        ),
        "sector_assessment_missing_count": sector_assessment_missing,
    }


def _production_snapshot_pair_mismatch_is_unsafe(
    *,
    expected_pair_keys: set[tuple[str, str]],
    snapshot_pair_keys: set[tuple[str, str]],
    snapshot_converged: bool,
) -> bool:
    """Return whether a production/full-ledger disagreement can reach an order.

    A converged production snapshot is required to agree exactly with the causal
    full-history ledger.  A non-converged snapshot is deliberately different:
    no production pair means no execution boundary, while a production pair
    makes ``build_symbol_bundle`` enforce the non-overridable 5m warmup gate.
    Either branch is unable to enqueue an entry, so it is a proven blocked
    candidate rather than a future-function failure for the portfolio replay.
    """

    return expected_pair_keys != snapshot_pair_keys and snapshot_converged


def _causality_failures(
    *,
    symbols: Sequence[SymbolResearchFacts],
    sectors: Mapping[str, SectorResearchFacts],
    snapshot: PITMetadataSnapshot,
) -> tuple[str, ...]:
    failures: list[str] = []
    index = PITMetadataIndex(snapshot)
    for facts in symbols:
        try:
            master = index.security(facts.code)
        except KeyError:
            failures.append("security_master_missing")
            continue
        if facts.security_master != master:
            failures.append("security_master_checkpoint_mismatch")
        if facts.memberships != index.memberships_for(facts.code):
            failures.append("membership_checkpoint_mismatch")
        if facts.factors != index.factors_for(facts.code):
            failures.append("corporate_action_checkpoint_mismatch")
        if any(
            not point.confirmed
            or point.confirmed_at is None
            or point.available_at < point.confirmed_at
            for point in (
                *facts.daily_points,
                *facts.thirty_points,
                *facts.five_points,
                *facts.one_points,
            )
        ):
            failures.append("noncausal_structural_point_registry")
        for frequency, points, visibility in (
            ("daily", facts.daily_points, facts.daily_point_visibility),
            ("thirty_minute", facts.thirty_points, facts.thirty_point_visibility),
            ("five_minute", facts.five_points, facts.five_point_visibility),
            ("one_minute", facts.one_points, facts.one_point_visibility),
        ):
            points_by_id = {point.point_id: point for point in points}
            visibility_point_ids = {interval.point_id for interval in visibility}
            if visibility_point_ids != points_by_id.keys():
                failures.append(f"{frequency}_current_state_ledger_incomplete")
            if any(
                interval.visible_from < points_by_id[interval.point_id].available_at
                for interval in visibility
                if interval.point_id in points_by_id
            ):
                failures.append(f"{frequency}_state_visible_before_point")
        operation_point_ids = {
            point.point_id
            for point in facts.five_points
            if is_five_minute_trade_level(
                point.source_frequency,
                point.recursive_level,
            )
            and five_minute_setup_is_in_policy_scope(point)
        }
        warmup_by_time = {row.observed_at: row for row in facts.five_minute_warmup}
        for evaluation in facts.evaluations:
            if not any(
                interval.point_id in operation_point_ids
                and interval.contains(evaluation.observed_at)
                for interval in facts.five_point_visibility
            ):
                failures.append("decision_without_current_five_minute_setup")
            if evaluation.bar.closed_at != evaluation.observed_at:
                failures.append("decision_bar_not_closed")
            exact_buy_pairs = _new_exact_buy_nesting_pairs(facts, evaluation)
            has_exact_new_pair = bool(exact_buy_pairs)
            execution_snapshot = warmup_by_time.get(evaluation.observed_at)
            snapshot_pairs: tuple[tuple[StructuralPoint, StructuralPoint], ...] = ()
            if has_exact_new_pair and execution_snapshot is None:
                failures.append(
                    "exact_nesting_pair_without_production_execution_snapshot"
                )
            if execution_snapshot is not None and not has_exact_new_pair:
                failures.append(
                    "production_execution_snapshot_without_exact_nesting_pair"
                )
            if execution_snapshot is not None:
                snapshot_pairs = _new_exact_buy_nesting_pairs(
                    facts,
                    evaluation,
                    setup_points=execution_snapshot.production_five_points,
                )
                expected_pair_keys = {
                    (structural_point_occurrence_id(setup), witness.point_id)
                    for setup, witness in exact_buy_pairs
                }
                snapshot_pair_keys = {
                    (structural_point_occurrence_id(setup), witness.point_id)
                    for setup, witness in snapshot_pairs
                }
                if _production_snapshot_pair_mismatch_is_unsafe(
                    expected_pair_keys=expected_pair_keys,
                    snapshot_pair_keys=snapshot_pair_keys,
                    snapshot_converged=execution_snapshot.converged,
                ):
                    failures.append(
                        "production_execution_snapshot_nesting_pair_mismatch"
                    )
                if (
                    execution_snapshot.one_minute_bar_count
                    != evaluation.one_minute_bar_count
                ):
                    failures.append("production_one_minute_history_count_changed")
                if (
                    execution_snapshot.one_minute_bar_count
                    < SCREENING_MINIMUM_BARS_BY_FREQUENCY["1m"]
                ):
                    failures.append("production_one_minute_history_insufficient")
            if (
                snapshot_pairs
                and execution_snapshot is not None
                and evaluation.higher_timeframe_gates is None
            ):
                failures.append(
                    "buy_nesting_pair_without_higher_timeframe_integrity_gate"
                )
            expected = index.membership_at(facts.code, evaluation.observed_at)
            expected_sector = None if expected is None else expected.sector_id
            if evaluation.sector_id != expected_sector:
                failures.append("future_or_stale_sector_membership")
            if not master.listed_on(evaluation.observed_at.date()):
                failures.append("unlisted_decision_event")
            if evaluation.bar.adjustment_known_at > evaluation.observed_at:
                failures.append("future_adjustment_timestamp")
            expected_divisor = Decimal("1")
            for factor in facts.factors:
                if factor.effective_on <= evaluation.observed_at.date():
                    expected_divisor *= factor.raw_price_divisor
            for raw_value, analysis_value in (
                (evaluation.bar.raw_open, evaluation.bar.analysis_open),
                (evaluation.bar.raw_high, evaluation.bar.analysis_high),
                (evaluation.bar.raw_low, evaluation.bar.analysis_low),
                (evaluation.bar.raw_close, evaluation.bar.analysis_close),
            ):
                expected_value = raw_value * expected_divisor
                tolerance = max(
                    Decimal("0.0000001"), abs(expected_value) * Decimal("1e-10")
                )
                if abs(analysis_value - expected_value) > tolerance:
                    failures.append("noncausal_price_adjustment")
    if any(row.allot_num > 0 for row in snapshot.factors):
        failures.append("rights_issue_accounting_outside_certified_contract")
    if any(row.gugai > 0 for row in snapshot.factors):
        failures.append("share_reform_outside_certified_contract")
    if any(facts.error is not None for facts in sectors.values()):
        failures.append("sector_composite_incomplete")
    for facts in sectors.values():
        point_ids = {point.point_id for point in facts.thirty_points}
        visibility_ids = {
            interval.point_id for interval in facts.thirty_point_visibility
        }
        if visibility_ids != point_ids:
            failures.append("sector_current_state_ledger_incomplete")
        for observed_at, assessment in facts.assessments:
            context = assessment.thirty_context
            if context is None or context.dominant_point_id is None:
                continue
            if not any(
                interval.point_id == context.dominant_point_id
                and interval.contains(observed_at)
                for interval in facts.thirty_point_visibility
            ):
                failures.append("stale_sector_point_at_decision")
    for facts in symbols:
        for evaluation in facts.evaluations:
            if evaluation.sector_id is not None and (
                evaluation.sector_id not in sectors
                or evaluation.observed_at
                not in dict(sectors[evaluation.sector_id].assessments)
            ):
                failures.append("sector_assessment_missing_at_decision")
    return tuple(dict.fromkeys(failures))


def _write_gate(
    *,
    path: Path,
    status: str,
    pnl_generated: bool,
    algorithm_revision: str,
    snapshot_hash: str,
    symbols: int,
    evaluations: int,
    failures: Sequence[str],
    report: Path | None = None,
) -> None:
    if not causality_gate_state_is_consistent(
        status=status,
        pnl_generated=pnl_generated,
        failures=failures,
        report=report,
    ):
        raise ValueError("inconsistent causality gate state")
    _atomic_json(
        path,
        {
            "schema": CAUSALITY_GATE_SCHEMA,
            "checked_at": datetime.now().astimezone().isoformat(),
            "status": status,
            "pnl_generated": pnl_generated,
            "algorithm_revision": algorithm_revision,
            "pit_snapshot_sha256": snapshot_hash,
            "validated_symbol_fact_count": symbols,
            "validated_decision_count": evaluations,
            "proven_controls": CAUSALITY_GATE_PROVEN_CONTROLS,
            "failures": list(failures),
            "report": None if report is None else str(report.resolve()),
        },
    )


def _prefix_audit_failures(
    *,
    path: Path,
    manifest_path: Path,
    prefix_algorithm_revision: str,
    fact_algorithm_revision: str,
    snapshot_hash: str,
    symbols: Sequence[SymbolResearchFacts],
) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ("prefix_invariance_audit_missing",)
    if not isinstance(raw, Mapping):
        return ("prefix_invariance_audit_malformed",)
    failures: list[str] = []
    expected_count = sum(bool(row.evaluations) for row in symbols)
    if raw.get("schema") != "chanlun-prefix-invariance-audit":
        failures.append("prefix_invariance_audit_schema_mismatch")
    if raw.get("status") != "passed" or raw.get("failed_codes"):
        failures.append("prefix_invariance_changed")
    recorded_prefix_revision = raw.get(
        "prefix_algorithm_revision",
        raw.get("algorithm_revision"),
    )
    if recorded_prefix_revision != prefix_algorithm_revision:
        failures.append("prefix_invariance_algorithm_mismatch")
    if raw.get("fact_algorithm_revision", raw.get("algorithm_revision")) != (
        fact_algorithm_revision
    ):
        failures.append("prefix_invariance_fact_algorithm_mismatch")
    if raw.get("pit_snapshot_sha256") != snapshot_hash:
        failures.append("prefix_invariance_snapshot_mismatch")
    if raw.get("extract_manifest_sha256") != _sha256(manifest_path):
        failures.append("prefix_invariance_manifest_mismatch")
    if (
        int(raw.get("signal_producing_symbol_count", -1)) != expected_count
        or int(raw.get("audited_symbol_count", -1)) != expected_count
    ):
        failures.append("prefix_invariance_coverage_missing")
    return tuple(dict.fromkeys(failures))


def _pit_scope_failures(
    *,
    path: Path,
    snapshot: PITMetadataSnapshot,
    replay_codes: Sequence[str],
) -> tuple[str, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ("pit_scope_proof_unreadable",)
    if not isinstance(raw, Mapping):
        return ("pit_scope_proof_malformed",)
    audit = raw.get("audit")
    scope = audit.get("scope") if isinstance(audit, Mapping) else None
    if not isinstance(scope, Mapping):
        return ("pit_scope_proof_missing",)
    return validate_scope_proof(
        snapshot=snapshot,
        scope=scope,
        replay_codes=replay_codes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.bootstrap_repetitions <= 0:
        raise ValueError("bootstrap repetitions must be positive")
    started = wall_time.perf_counter()
    directory = args.input_dir.resolve()
    manifest_path = directory / "extract_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or not manifest.get("complete"):
        raise RuntimeError("symbol extraction is incomplete")
    algorithm_hashes = qmt_research_contract.algorithm_hashes()
    algorithm_revision = _algorithm_revision(algorithm_hashes)
    fact_algorithm_revision, fact_algorithm_hashes = _frozen_fact_algorithm(manifest)
    sector_composite_algorithm_revision = _sector_composite_algorithm_revision()
    sector_algorithm_revision = _sector_algorithm_revision(
        fact_algorithm_hashes,
        sector_composite_algorithm_revision,
    )
    prefix_algorithm_revision = _algorithm_revision(
        qmt_research_contract.prefix_algorithm_hashes()
    )
    symbols = _load_symbols(directory, manifest, fact_algorithm_revision)
    request = manifest.get("request")
    catalog = manifest.get("catalog")
    if not isinstance(request, Mapping) or not isinstance(catalog, Mapping):
        raise ValueError("extract request or catalog is missing")
    archived_intersecting = int(
        catalog.get("archived_intersecting_symbol_count", len(symbols))
    )
    unclassified_contracts = int(catalog.get("unclassified_symbol_count", 0))
    if archived_intersecting < len(symbols) or unclassified_contracts < 0:
        raise ValueError("extract catalog coverage is inconsistent")
    archived_membership_coverage = (
        Decimal("1")
        if archived_intersecting == 0
        else Decimal(len(symbols)) / Decimal(archived_intersecting)
    )
    snapshot_path = Path(str(request["pit_snapshot"]))
    snapshot_hash = _sha256(snapshot_path)
    if snapshot_hash != request.get("pit_snapshot_sha256"):
        raise ValueError("PIT metadata changed after symbol extraction")
    snapshot = load_snapshot(snapshot_path)
    scope_failures = _pit_scope_failures(
        path=snapshot_path,
        snapshot=snapshot,
        replay_codes=tuple(row.code for row in symbols),
    )
    if scope_failures:
        raise ValueError(
            "PIT historical sector closure proof failed: " + ",".join(scope_failures)
        )
    requested_start = date.fromisoformat(str(request["requested_start"]))
    effective_start = date.fromisoformat(str(request["effective_start"]))
    requested_end = date.fromisoformat(str(request["requested_end"]))
    warmup_start = date.fromisoformat(str(request["warmup_start"]))
    sector_ids, sector_closure_codes = _sector_reference_scope(
        symbols=symbols,
        snapshot=snapshot,
        warmup_start=warmup_start,
        requested_end=requested_end,
    )
    _validate_sector_reference_budget(
        sector_ids=sector_ids,
        closure_codes=sector_closure_codes,
        max_sector_count=args.max_sector_count,
        max_sector_closure=args.max_sector_closure,
        confirmed_large_scope=args.confirm_large_sector_scope,
    )
    print(
        json.dumps(
            {
                "stage": "sector_scope",
                "selected_symbols": len(symbols),
                "sectors": len(sector_ids),
                "reference_symbols": len(sector_closure_codes),
                "large_scope_confirmed": args.confirm_large_sector_scope,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    prefix_path = directory / "prefix_invariance_audit.json"
    prefix_failures = _prefix_audit_failures(
        path=prefix_path,
        manifest_path=manifest_path,
        prefix_algorithm_revision=prefix_algorithm_revision,
        fact_algorithm_revision=fact_algorithm_revision,
        snapshot_hash=snapshot_hash,
        symbols=symbols,
    )
    gate_path = args.report.resolve().parent / "causality_gate.json"
    evaluations = sum(len(row.evaluations) for row in symbols)
    if prefix_failures:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=prefix_failures,
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_prefix_invariance_gate",
                    "failures": prefix_failures,
                    "gate": str(gate_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    sectors = _build_sector_facts(
        directory=directory,
        symbols=symbols,
        snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        warmup_start=warmup_start,
        requested_end=requested_end,
        sector_algorithm_revision=sector_algorithm_revision,
        sector_composite_algorithm_revision=(sector_composite_algorithm_revision),
        force=args.force_sectors,
        reuse_cache=args.reuse_sector_cache,
        workers=args.sector_workers,
    )
    failures = _causality_failures(
        symbols=symbols,
        sectors=sectors,
        snapshot=snapshot,
    )
    if failures:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=failures,
        )
        print(
            json.dumps(
                {
                    "complete": False,
                    "status": "blocked_by_no_future_function_gate",
                    "failures": failures,
                    "gate": str(gate_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3
    run = run_sparse_portfolio(
        symbols,
        sectors,
        initial_cash=args.initial_cash,
        formal_selection_required=False,
    )
    decision_funnel = _decision_funnel_diagnostics(
        symbols=symbols,
        sectors=sectors,
    )
    terminal_same_bar = tuple(
        trade.code
        for trade in run.trades
        if trade.exit_reason == "forced_liquidation_sensitivity"
    )
    if terminal_same_bar:
        _write_gate(
            path=gate_path,
            status="blocked",
            pnl_generated=False,
            algorithm_revision=algorithm_revision,
            snapshot_hash=snapshot_hash,
            symbols=len(symbols),
            evaluations=evaluations,
            failures=("terminal_same_bar_liquidation_detected",),
        )
        raise RuntimeError("certified replay generated a terminal same-bar fill")
    run_path = directory / "certified_portfolio_run.pkl"
    _atomic_bytes(run_path, pickle.dumps(run, protocol=pickle.HIGHEST_PROTOCOL))
    sector_assessments = tuple(
        assessment
        for facts in sectors.values()
        for _observed_at, assessment in facts.assessments
    )
    complete_sector_events = sum(
        "sector_data_incomplete" not in row.reason_codes for row in sector_assessments
    )
    sector_event_coverage = (
        Decimal("1")
        if not sector_assessments
        else Decimal(complete_sector_events) / Decimal(len(sector_assessments))
    )
    evidence = DataEvidence(
        grade="certified",
        failures=(),
        warnings=(
            "fixed_policy_single_year_no_parameter_search",
            "malformed_qmt_expiry_sentinels_resolved_by_status_and_observed_bars",
            "gross_cash_dividends_before_investor_specific_holding_period_tax",
            *(
                ("terminal_open_positions_marked_to_market_not_same_bar_liquidated",)
                if run.open_positions
                else ()
            ),
            *(
                ("unclassified_archived_contracts_excluded",)
                if unclassified_contracts
                else ()
            ),
            *(
                ("incomplete_sector_bars_hard_blocked",)
                if complete_sector_events < len(sector_assessments)
                else ()
            ),
        ),
        coverage=(
            ("symbol_extraction", Decimal("1")),
            ("historical_membership", Decimal("1")),
            ("archived_universe_membership_coverage", archived_membership_coverage),
            ("point_in_time_adjustment", Decimal("1")),
            ("historical_security_status", Decimal("1")),
            ("corporate_action_accounting", Decimal("1")),
            ("sector_event_coverage", sector_event_coverage),
            ("causal_current_structure_ledgers", Decimal("1")),
            ("exact_one_minute_nesting_pair_scheduling", Decimal("1")),
            ("entry_higher_timeframe_integrity_evidence", Decimal("1")),
        ),
    )
    result = BacktestEvaluationResult(
        aggregate_run=run,
        bootstrap_repetitions=args.bootstrap_repetitions,
    )
    sector_paths = tuple(_sector_path(directory, sector_id) for sector_id in sectors)
    symbol_paths = tuple(_fact_path(directory, facts.code) for facts in symbols)
    report = build_report(
        evidence=evidence,
        result=result,
        ablations=qmt_research_contract.unavailable_ablations(
            "fixed_policy_ablation_not_run"
        ),
        benchmarks=qmt_research_contract.unavailable_benchmarks(),
        generated_at=datetime.now().astimezone(),
        algorithm_hashes=algorithm_hashes,
        limitations=(
            "fixed_policy_single_year_no_parameter_search",
            "required_ablations_not_run",
            "required_benchmarks_not_run",
            "investor_specific_dividend_withholding_tax_not_modelled",
            *(("terminal_open_positions_are_unrealised",) if run.open_positions else ()),
            "formal_selection_research_ledger_not_used_by_current_production_policy",
            *(
                ("historical_sw1_membership_unavailable_for_some_archived_contracts",)
                if unclassified_contracts
                else ()
            ),
        ),
        requested_range=(requested_start, requested_end),
        effective_range=(effective_start, requested_end),
        evaluation_mode="fixed_policy_one_year",
        sector_price_source=PIT_SW1_COMPOSITE_PROVIDER,
        formal_selection_required=False,
        universe_summary={
            "catalog_source": "qmt_sw1_with_cninfo_effective_dates",
            "selected_symbol_count": len(symbols),
            "archived_intersecting_symbol_count": archived_intersecting,
            "unclassified_excluded_symbol_count": unclassified_contracts,
            "eligible_sector_count": len(snapshot.qmt_sw1_sector_names),
            "sector_composite_member_limit": None,
            "corporate_action_count": len(snapshot.factors),
            "causal_evaluation_count": evaluations,
            "formal_selection_required": False,
            "decision_funnel": decision_funnel,
        },
        data_source_hashes=(
            ("pit_metadata_snapshot", snapshot_hash),
            ("qmt_extract_manifest", _sha256(manifest_path)),
            ("prefix_invariance_audit", _sha256(prefix_path)),
            (
                "symbol_fact_checkpoint_tree",
                _checkpoint_tree(symbol_paths, root=directory),
            ),
            (
                "sector_fact_checkpoint_tree",
                _checkpoint_tree(sector_paths, root=directory),
            ),
            ("certified_portfolio_run", _sha256(run_path)),
        ),
    )
    if qmt_research_contract.algorithm_hashes() != algorithm_hashes:
        raise RuntimeError("source code changed during certified finalization")
    if (
        "fact_algorithm" in manifest
        and qmt_research_contract.fact_algorithm_hashes() != fact_algorithm_hashes
    ):
        raise RuntimeError("symbol-fact source changed during certified finalization")
    qmt_research_contract.write_report_atomic(args.report, report)
    _write_gate(
        path=gate_path,
        status="passed",
        pnl_generated=bool(run.fills),
        algorithm_revision=algorithm_revision,
        snapshot_hash=snapshot_hash,
        symbols=len(symbols),
        evaluations=evaluations,
        failures=(),
        report=args.report,
    )
    print(
        json.dumps(
            {
                "complete": True,
                "grade": "certified",
                "symbols": len(symbols),
                "sectors": len(sectors),
                "evaluations": evaluations,
                "trades": len(run.trades),
                "fills": len(run.fills),
                "open_positions": len(run.open_positions),
                "elapsed_seconds": round(wall_time.perf_counter() - started, 2),
                "report": str(args.report.resolve()),
                "gate": str(gate_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
