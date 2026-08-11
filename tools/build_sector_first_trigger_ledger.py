#!/usr/bin/env python3
"""Build the historical SW1 trigger ledger before any individual-stock scan.

The command consumes the immutable QMT/CNInfo point-in-time metadata snapshot,
builds one all-member causal 30-minute composite for every SW1 sector, and
freezes the ranked eligible sectors at each completed market 30-minute close.
It is research-only and never connects to an account or creates an order.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
    load_qmt_frame,
    sector_facts_from_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataSnapshot,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.pit_sector import (  # noqa: E402
    PIT_SW1_COMPOSITE_PROVIDER,
    build_pit_sw1_composite,
)
from chanlun.decision_support.trading_system.sector_first_scope import (  # noqa: E402
    build_sector_first_scope,
)
from chanlun.decision_support.trading_system.sector_first_trigger_plan import (  # noqa: E402
    build_sector_first_trigger_ledger,
)
from tools import qmt_research_contract  # noqa: E402


DEFAULT_SNAPSHOT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_OUTPUT_DIR = Path(
    "audit/chanlun_trading_system_backtest/sector_first_full_market"
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    value.add_argument("--warmup-start", type=_parse_date, default=date(2025, 5, 1))
    value.add_argument("--start", type=_parse_date, default=date(2025, 7, 25))
    value.add_argument(
        "--effective-start",
        type=_parse_date,
        default=date(2025, 8, 1),
    )
    value.add_argument("--end", type=_parse_date, default=date(2026, 7, 24))
    value.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    value.add_argument("--workers", type=int, default=4)
    value.add_argument("--force", action="store_true")
    return value


def _sha256_file(path: Path) -> str:
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


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _normal(value: object) -> object:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _normal(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_bytes(
        path,
        (
            json.dumps(
                _normal(payload),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _sector_path(directory: Path, sector_id: str) -> Path:
    return directory / "sector_triggers" / f"{sector_id.rsplit(':', 1)[-1]}.pkl"


def _sector_revision(
    *,
    snapshot_sha256: str,
    sector_id: str,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    frame: pd.DataFrame,
) -> str:
    digest = hashlib.sha256()
    digest.update(snapshot_sha256.encode("ascii"))
    digest.update(sector_id.encode("utf-8"))
    digest.update(repr(tuple(observed_times)).encode("utf-8"))
    digest.update(repr(tuple(expected_closes)).encode("utf-8"))
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True),
            index=False,
            categorize=False,
        ).to_numpy(dtype="uint64", copy=False).tobytes()
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


def _build_sector_facts(
    *,
    directory: Path,
    snapshot: PITMetadataSnapshot,
    snapshot_sha256: str,
    warmup_start: date,
    end: date,
    observed_times: Sequence[datetime],
    expected_closes: Sequence[datetime],
    algorithm_revision: str,
    force: bool,
    workers: int,
) -> dict[str, SectorResearchFacts]:
    names = dict(snapshot.qmt_sw1_sector_names)
    output: dict[str, SectorResearchFacts] = {}
    start_at = datetime.combine(
        warmup_start,
        time(9, 30),
        tzinfo=snapshot.captured_at.tzinfo,
    )
    end_at = datetime.combine(
        end,
        time(15, 0),
        tzinfo=snapshot.captured_at.tzinfo,
    )
    maximum_workers = max(1, min(workers, len(names)))
    with ProcessPoolExecutor(max_workers=maximum_workers) as executor:
        jobs = {
            executor.submit(
                _build_one_sector,
                directory=directory,
                snapshot=snapshot,
                snapshot_sha256=snapshot_sha256,
                sector_id=sector_id,
                sector_name=names[sector_id],
                start_at=start_at,
                end_at=end_at,
                observed_times=tuple(observed_times),
                expected_closes=tuple(expected_closes),
                algorithm_revision=algorithm_revision,
                force=force,
            ): sector_id
            for sector_id in sorted(names)
        }
        for completed, future in enumerate(as_completed(jobs), start=1):
            sector_id = jobs[future]
            facts = future.result()
            output[sector_id] = facts
            print(
                json.dumps(
                    {
                        "stage": "sector_first_trigger",
                        "completed": completed,
                        "total": len(names),
                        "sector": sector_id,
                        "rows": facts.row_count,
                        "events": len(facts.assessments),
                        "error": facts.error,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return output


def _build_one_sector(
    *,
    directory: Path,
    snapshot: PITMetadataSnapshot,
    snapshot_sha256: str,
    sector_id: str,
    sector_name: str,
    start_at: datetime,
    end_at: datetime,
    observed_times: tuple[datetime, ...],
    expected_closes: tuple[datetime, ...],
    algorithm_revision: str,
    force: bool,
) -> SectorResearchFacts:
    frame = build_pit_sw1_composite(
        snapshot=snapshot,
        sector_id=sector_id,
        start_at=start_at,
        end_at=end_at,
    )
    revision = _sector_revision(
        snapshot_sha256=snapshot_sha256,
        sector_id=sector_id,
        observed_times=observed_times,
        expected_closes=expected_closes,
        frame=frame,
    )
    path = _sector_path(directory, sector_id)
    cached = None if force else _load_cached_sector(
        path,
        algorithm_revision=algorithm_revision,
        source_revision=revision,
    )
    if cached is not None:
        return cached
    member_count = len(
        {
            row.code
            for row in snapshot.memberships
            if row.sector_id == sector_id
        }
    )
    facts = sector_facts_from_frame(
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=member_count,
        frame=frame,
        observed_times=observed_times,
        algorithm_revision=algorithm_revision,
        source_revision=revision,
        market_data_source=PIT_SW1_COMPOSITE_PROVIDER,
        expected_closes=expected_closes,
    )
    _atomic_bytes(
        path,
        pickle.dumps(facts, protocol=pickle.HIGHEST_PROTOCOL),
    )
    return facts


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.warmup_start <= args.start <= args.effective_start <= args.end:
        raise ValueError("expected warmup_start <= start <= effective_start <= end")
    if args.workers <= 0:
        raise ValueError("workers must be positive")
    snapshot_path = args.pit_snapshot.resolve()
    snapshot = load_snapshot(snapshot_path)
    if not snapshot.source_start <= args.start <= args.end <= snapshot.source_end:
        raise ValueError("requested trigger range is outside the PIT snapshot")
    snapshot_hash = _sha256_file(snapshot_path)
    algorithm_hashes = qmt_research_contract.algorithm_hashes()
    algorithm_revision = _algorithm_revision(algorithm_hashes)
    scope = build_sector_first_scope(
        snapshot,
        requested_start=args.start,
        requested_end=args.end,
    )
    market_frame = load_qmt_frame(
        "SH.000001",
        "30m",
        start_at=datetime.combine(
            args.warmup_start,
            time(9, 30),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
        end_at=datetime.combine(
            args.end,
            time(15, 0),
            tzinfo=snapshot.captured_at.tzinfo,
        ),
    )
    expected_closes = tuple(
        pd.Timestamp(value).to_pydatetime() for value in market_frame["date"]
    )
    effective_at = datetime.combine(
        args.effective_start,
        time(9, 30),
        tzinfo=snapshot.captured_at.tzinfo,
    )
    observed_times = tuple(
        value for value in expected_closes if value >= effective_at
    )
    if not observed_times:
        raise RuntimeError("QMT market 30m trigger timeline is unavailable")
    sectors = _build_sector_facts(
        directory=args.output_dir,
        snapshot=snapshot,
        snapshot_sha256=snapshot_hash,
        warmup_start=args.warmup_start,
        end=args.end,
        observed_times=observed_times,
        expected_closes=expected_closes,
        algorithm_revision=algorithm_revision,
        force=args.force,
        workers=args.workers,
    )
    ledger = build_sector_first_trigger_ledger(
        snapshot=snapshot,
        scope=scope,
        sector_facts=sectors,
        observed_times=observed_times,
        algorithm_revision=algorithm_revision,
        pit_snapshot_sha256=snapshot_hash,
    )
    document = ledger.document()
    document["algorithm_hashes"] = tuple(
        {"path": path, "sha256": digest} for path, digest in algorithm_hashes
    )
    document["requested_range"] = {
        "start": args.start,
        "effective_start": args.effective_start,
        "end": args.end,
    }
    document["scope_counts"] = scope.document()["counts"]
    output = args.output_dir / "sector_first_trigger_ledger.json"
    checkpoint = args.output_dir / "sector_first_trigger_ledger.pkl"
    _atomic_bytes(
        checkpoint,
        pickle.dumps(ledger, protocol=pickle.HIGHEST_PROTOCOL),
    )
    _atomic_json(output, document)
    if qmt_research_contract.algorithm_hashes() != algorithm_hashes:
        raise RuntimeError("source code changed while building sector triggers")
    print(
        json.dumps(
            {
                "complete": True,
                "sectors": len(sectors),
                "events": len(ledger.events),
                "maximum_candidates": document["counts"][
                    "maximum_candidate_symbol_count"
                ],
                "output": str(output.resolve()),
                "checkpoint": str(checkpoint.resolve()),
                "live_status": "LIVE_DISABLED",
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
