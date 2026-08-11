#!/usr/bin/env python3
"""Build QMT GICS3 triggers for the authorized recent-year research variant.

Current captured constituents are deliberately backfilled across the year.
Prices come only from completed local QMT 5m records, causally aggregated to
30m; no tick data, download, account connection, or order API is used.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.current_sector import (  # noqa: E402
    CURRENT_GICS3_COMPOSITE_PROVIDER,
    build_current_qmt_gics3_composite,
    reclassify_current_sector_facts,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    SectorResearchFacts,
    load_qmt_daily_frame,
    load_qmt_frame,
    qmt_factor_frame,
    sector_facts_from_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    PITMetadataSnapshot,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    load_sector_ledger,
)
from chanlun.decision_support.trading_system.recent_year_research import (  # noqa: E402
    recent_year_research_parameters,
)
from chanlun.decision_support.trading_system.recent_year_provenance import (  # noqa: E402
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)
from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.etf_proxy_facts import (  # noqa: E402
    DailyMarketBar,
)
from chanlun.decision_support.trading_system.sector_strength_replay import (  # noqa: E402
    ReplaySectorMemberDailySeries,
    build_replay_sector_strength_batches,
)
from chanlun.decision_support.trading_system.selection import (  # noqa: E402
    CompletedDailyClose,
)
from chanlun.decision_support.trading_system.sector_first_trigger_plan import (  # noqa: E402
    build_current_sector_trigger_ledger,
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/recent_year_current_sector_no3p"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_CATALOG = Path(
    ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)


_SNAPSHOT: PITMetadataSnapshot | None = None
_INDEX: PITMetadataIndex | None = None
_DATA_DIR: Path | None = None
_START_AT: datetime | None = None
_END_AT: datetime | None = None
_OBSERVED_TIMES: tuple[datetime, ...] = ()
_EXPECTED_CLOSES: tuple[datetime, ...] = ()
_ALGORITHM_REVISION: str | None = None
_CATALOG_ENTRY_SHA256: str | None = None
_OUTPUT_DIR: Path | None = None
_RECLASSIFY_EXISTING = False
_PIT_SOURCE_SHA256: str | None = None


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument("--catalog-ledger", type=Path, default=DEFAULT_CATALOG)
    value.add_argument("--qmt-local-data-dir", type=Path, required=True)
    value.add_argument("--output-dir", type=Path, default=DEFAULT_ROOT)
    value.add_argument("--workers", type=int, default=6)
    value.add_argument("--force", action="store_true")
    value.add_argument(
        "--reclassify-existing",
        action="store_true",
        help="reuse frozen causal contexts and only rerun the sector policy",
    )
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _normal(value: object) -> object:
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return _normal(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _normal(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normal(item) for item in value]
    return value


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
            json.dumps(
                _normal(payload),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _catalog_scope(
    *,
    catalog_entry: Mapping[str, object],
    snapshot: PITMetadataSnapshot,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], tuple[str, ...]]:
    known = {row.code: row for row in snapshot.securities}
    parameters = recent_year_research_parameters()
    members: dict[str, tuple[str, ...]] = {}
    names: dict[str, str] = {}
    catalog_codes: set[str] = set()
    selected_codes: set[str] = set()
    for raw in catalog_entry["sectors"]:
        row = dict(raw)
        sector_id = str(row["sector_id"])
        catalog_codes.update(row["member_codes"])
        selected = tuple(
            sorted(
                code
                for code in row["member_codes"]
                if code in known
                and known[code].intersects(
                    parameters.requested_start,
                    parameters.requested_end,
                )
            )
        )
        if len(selected) < 8:
            continue
        members[sector_id] = selected
        names[sector_id] = str(row["name"])
        selected_codes.update(selected)
    return members, names, tuple(sorted(catalog_codes - selected_codes))


def _pit_snapshot_available_at(
    *,
    snapshot: PITMetadataSnapshot,
) -> tuple[datetime, str]:
    """Return content-bound availability for historical release copies.

    Copying an immutable PIT JSON into a content-addressed release directory
    changes filesystem mtime without changing any signed fact.  Historical
    research therefore uses the content-bound ``captured_at``.
    """

    return snapshot.captured_at, "CONTENT_CAPTURED_AT_HISTORICAL"


def _initialize_worker(
    pit_path: str,
    data_dir: str,
    start_at: datetime,
    end_at: datetime,
    observed_times: tuple[datetime, ...],
    expected_closes: tuple[datetime, ...],
    algorithm_revision: str,
    catalog_entry_sha256: str,
    output_dir: str,
    reclassify_existing: bool,
) -> None:
    global _SNAPSHOT, _INDEX, _DATA_DIR, _START_AT, _END_AT
    global _OBSERVED_TIMES, _EXPECTED_CLOSES, _ALGORITHM_REVISION
    global _CATALOG_ENTRY_SHA256
    global _OUTPUT_DIR, _RECLASSIFY_EXISTING, _PIT_SOURCE_SHA256
    _SNAPSHOT = load_snapshot(Path(pit_path))
    _INDEX = PITMetadataIndex(_SNAPSHOT)
    _DATA_DIR = Path(data_dir)
    _START_AT = start_at
    _END_AT = end_at
    _OBSERVED_TIMES = observed_times
    _EXPECTED_CLOSES = expected_closes
    _ALGORITHM_REVISION = algorithm_revision
    _CATALOG_ENTRY_SHA256 = catalog_entry_sha256
    _OUTPUT_DIR = Path(output_dir)
    _RECLASSIFY_EXISTING = reclassify_existing
    _PIT_SOURCE_SHA256 = _sha256_file(Path(pit_path))
    os.environ[QMT_LOCAL_DATA_ENV] = data_dir


def _sector_source_revision(
    sector_id: str,
    members: Sequence[str],
    frame: pd.DataFrame,
) -> str:
    digest = hashlib.sha256()
    digest.update(str(_CATALOG_ENTRY_SHA256).encode("ascii"))
    digest.update(sector_id.encode("utf-8"))
    digest.update(repr(tuple(members)).encode("utf-8"))
    digest.update(
        pd.util.hash_pandas_object(
            frame.reset_index(drop=True), index=False, categorize=False
        ).to_numpy(dtype="uint64", copy=False).tobytes()
    )
    digest.update(repr(tuple(sorted(frame.attrs.items()))).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _build_sector(request: tuple[str, str, tuple[str, ...]]) -> SectorResearchFacts:
    if any(
        value is None
        for value in (
            _SNAPSHOT,
            _INDEX,
            _DATA_DIR,
            _START_AT,
            _END_AT,
            _ALGORITHM_REVISION,
            _CATALOG_ENTRY_SHA256,
            _OUTPUT_DIR,
        )
    ):
        raise RuntimeError("current-sector worker was not initialized")
    sector_id, sector_name, members = request
    assert _INDEX is not None
    assert _DATA_DIR is not None
    assert _START_AT is not None
    assert _END_AT is not None
    assert _ALGORITHM_REVISION is not None
    assert _OUTPUT_DIR is not None
    factors = {code: _INDEX.factors_for(code) for code in members}
    frame = build_current_qmt_gics3_composite(
        data_dir=_DATA_DIR,
        sector_id=sector_id,
        member_codes=members,
        factors_by_code=factors,
        start_at=_START_AT,
        end_at=_END_AT,
    )
    source_revision = _sector_source_revision(sector_id, members, frame)
    existing_path = (
        _OUTPUT_DIR
        / "sector_triggers"
        / f"{sector_id.rsplit(':', 1)[-1]}.pkl"
    )
    if _RECLASSIFY_EXISTING and existing_path.is_file():
        existing = pickle.loads(existing_path.read_bytes())
        if not isinstance(existing, SectorResearchFacts):
            raise ValueError("existing current-sector checkpoint is invalid")
        return reclassify_current_sector_facts(
            facts=existing,
            frame=frame,
            expected_closes=_EXPECTED_CLOSES,
            algorithm_revision=_ALGORITHM_REVISION,
            source_revision=source_revision,
        )
    return sector_facts_from_frame(
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=len(members),
        frame=frame,
        observed_times=_OBSERVED_TIMES,
        algorithm_revision=_ALGORITHM_REVISION,
        source_revision=source_revision,
        market_data_source=CURRENT_GICS3_COMPOSITE_PROVIDER,
        expected_closes=_EXPECTED_CLOSES,
    )


def _daily_market_bars(frame: pd.DataFrame) -> tuple[DailyMarketBar, ...]:
    return tuple(
        DailyMarketBar(
            session=pd.Timestamp(row.date).date(),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
            known_at=pd.Timestamp(row.date).to_pydatetime(),
        )
        for row in frame.itertuples(index=False)
    )


def _build_member_daily(code: str) -> ReplaySectorMemberDailySeries:
    if any(
        value is None
        for value in (
            _INDEX,
            _START_AT,
            _END_AT,
            _PIT_SOURCE_SHA256,
        )
    ):
        raise RuntimeError("sector-strength worker was not initialized")
    assert _INDEX is not None
    assert _START_AT is not None
    assert _END_AT is not None
    assert _PIT_SOURCE_SHA256 is not None
    factors = qmt_factor_frame(_INDEX.factors_for(code))
    frame = load_qmt_daily_frame(
        code,
        start_at=_START_AT,
        end_at=_END_AT,
        factors=factors,
    )
    closes = tuple(
        CompletedDailyClose(
            session=pd.Timestamp(row.date).date(),
            close=Decimal(str(row.close)),
            known_at=pd.Timestamp(row.date).to_pydatetime(),
        )
        for row in frame.itertuples(index=False)
    )
    source_revision = sha256_json(
        {
            "schema": "chanlun-sector-strength-member-daily-source",
            "symbol": code,
            "qmt_local_cache_source_sha256": frame.attrs.get(
                "qmt_local_cache_source_sha256",
                "MISSING",
            ),
            "price_basis_revision": frame.attrs.get(
                "price_basis_revision",
                "UNRESOLVED",
            ),
            "pit_snapshot_sha256": _PIT_SOURCE_SHA256,
            "selected_row_count": len(closes),
            "first_session": (
                None if not closes else closes[0].session.isoformat()
            ),
            "last_session": (
                None if not closes else closes[-1].session.isoformat()
            ),
        }
    )
    return ReplaySectorMemberDailySeries(
        symbol=code,
        security=_INDEX.security(code),
        closes=closes,
        source_revision=source_revision,
    )


def _strength_evaluation_times(
    *,
    benchmark_daily: Sequence[DailyMarketBar],
    observed_times: Sequence[datetime],
) -> tuple[datetime, ...]:
    available = tuple(benchmark_daily)
    output: set[datetime] = set()
    for observed_at in observed_times:
        visible = tuple(
            row
            for row in available
            if row.completed
            and row.known_at <= observed_at
            and row.session <= observed_at.date()
        )
        if visible:
            output.add(visible[-1].known_at)
    return tuple(sorted(output))


def _median_int(values: Sequence[int]) -> int:
    ordered = tuple(sorted(values))
    return 0 if not ordered else ordered[len(ordered) // 2]


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.workers <= 0 or args.workers > 16:
        raise ValueError("workers must be in [1, 16]")
    parameters = recent_year_research_parameters()
    pit_path = args.pit_snapshot.resolve()
    catalog_path = args.catalog_ledger.resolve()
    data_dir = args.qmt_local_data_dir.resolve()
    output_dir = args.output_dir.resolve()
    os.environ[QMT_LOCAL_DATA_ENV] = str(data_dir)
    snapshot = load_snapshot(pit_path)
    index = PITMetadataIndex(snapshot)
    pit_available_at, pit_availability_basis = _pit_snapshot_available_at(
        snapshot=snapshot,
    )
    ledger_document = load_sector_ledger(catalog_path)
    entries = tuple(ledger_document["entries"])
    if not entries:
        raise RuntimeError("current QMT sector capture ledger is empty")
    catalog_entry = entries[-1]
    members, names, excluded = _catalog_scope(
        catalog_entry=catalog_entry,
        snapshot=snapshot,
    )
    start_at = datetime.combine(parameters.warmup_start, time(9, 30), tzinfo=CN)
    end_at = datetime.combine(parameters.requested_end, time(15), tzinfo=CN)
    market = load_qmt_frame(
        "SH.000001",
        "30m",
        start_at=start_at,
        end_at=end_at,
    )
    expected_closes = tuple(
        pd.Timestamp(value).to_pydatetime() for value in market["date"]
    )
    benchmark_daily_frame = load_qmt_daily_frame(
        "SH.000300",
        start_at=start_at,
        end_at=end_at,
        factors=qmt_factor_frame(index.factors_for("SH.000300")),
    )
    calendar_daily_frame = load_qmt_daily_frame(
        "SH.000001",
        start_at=start_at,
        end_at=end_at,
        factors=qmt_factor_frame(index.factors_for("SH.000001")),
    )
    benchmark_sessions = tuple(
        pd.Timestamp(value).date() for value in benchmark_daily_frame["date"]
    )
    market_sessions = tuple(
        pd.Timestamp(value).date() for value in calendar_daily_frame["date"]
    )
    if (
        not benchmark_sessions
        or benchmark_sessions != market_sessions
        or benchmark_sessions != tuple(sorted(set(benchmark_sessions)))
    ):
        raise RuntimeError(
            "SH.000300 and SH.000001 native daily calendars do not reconcile"
        )
    benchmark_daily = _daily_market_bars(benchmark_daily_frame)
    effective_at = datetime.combine(
        parameters.effective_start,
        time(9, 30),
        tzinfo=CN,
    )
    observed_times = tuple(
        value
        for value in expected_closes
        if value >= effective_at
    )
    if not observed_times:
        raise RuntimeError("QMT 30m decision timeline is unavailable after capture")
    strength_evaluation_times = _strength_evaluation_times(
        benchmark_daily=benchmark_daily,
        observed_times=observed_times,
    )
    if not strength_evaluation_times:
        raise RuntimeError("completed benchmark daily cutoffs are unavailable")
    hashes = recent_year_research_algorithm_hashes(PROJECT_ROOT)
    revision = recent_year_research_algorithm_revision(hashes)
    requests = tuple(
        (sector_id, names[sector_id], members[sector_id])
        for sector_id in sorted(members)
    )
    if not requests:
        raise RuntimeError("current QMT GICS3 sector scope is empty")
    sector_facts: dict[str, SectorResearchFacts] = {}
    failures: dict[str, str] = {}
    with ProcessPoolExecutor(
        max_workers=min(args.workers, len(requests)),
        initializer=_initialize_worker,
        initargs=(
            str(pit_path),
            str(data_dir),
            start_at,
            end_at,
            observed_times,
            expected_closes,
            revision,
            str(catalog_entry["entry_sha256"]),
            str(output_dir),
            args.reclassify_existing,
        ),
    ) as executor:
        jobs = {executor.submit(_build_sector, row): row[0] for row in requests}
        for ordinal, future in enumerate(as_completed(jobs), start=1):
            sector_id = jobs[future]
            try:
                facts = future.result()
                sector_facts[sector_id] = facts
                _atomic_bytes(
                    output_dir
                    / "sector_triggers"
                    / f"{sector_id.rsplit(':', 1)[-1]}.pkl",
                    pickle.dumps(facts, protocol=pickle.HIGHEST_PROTOCOL),
                )
            except Exception as exc:
                failures[sector_id] = f"{type(exc).__name__}: {exc}"
            print(
                json.dumps(
                    {
                        "stage": "current_gics3_sector_trigger",
                        "completed": ordinal,
                        "total": len(jobs),
                        "failed": len(failures),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    if failures:
        _atomic_json(output_dir / "sector_trigger_failures.json", failures)
        raise RuntimeError(f"current sector construction failed: {len(failures)}")

    member_codes = tuple(
        sorted({code for values in members.values() for code in values})
    )
    member_series: dict[str, ReplaySectorMemberDailySeries] = {}
    daily_failures: dict[str, str] = {}
    with ProcessPoolExecutor(
        max_workers=min(args.workers, len(member_codes)),
        initializer=_initialize_worker,
        initargs=(
            str(pit_path),
            str(data_dir),
            start_at,
            end_at,
            observed_times,
            expected_closes,
            revision,
            str(catalog_entry["entry_sha256"]),
            str(output_dir),
            False,
        ),
    ) as executor:
        jobs = {executor.submit(_build_member_daily, code): code for code in member_codes}
        for ordinal, future in enumerate(as_completed(jobs), start=1):
            code = jobs[future]
            try:
                member_series[code] = future.result()
            except Exception as exc:
                daily_failures[code] = f"{type(exc).__name__}: {exc}"
            if ordinal % 100 == 0 or ordinal == len(jobs):
                print(
                    json.dumps(
                        {
                            "stage": "horizontal_sector_strength_daily",
                            "completed": ordinal,
                            "total": len(jobs),
                            "failed": len(daily_failures),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    if daily_failures:
        _atomic_json(
            output_dir / "sector_strength_daily_failures.json",
            daily_failures,
        )
        raise RuntimeError(
            f"sector strength member daily construction failed: {len(daily_failures)}"
        )
    member_source_stable = {
        "schema": "chanlun-sector-strength-member-source-manifest",
        "catalog_entry_sha256": catalog_entry["entry_sha256"],
        "pit_snapshot_sha256": _sha256_file(pit_path),
        "members": tuple(
            {
                "symbol": code,
                "source_revision": member_series[code].source_revision,
                "daily_row_count": len(member_series[code].closes),
                "first_session": (
                    None
                    if not member_series[code].closes
                    else member_series[code].closes[0].session.isoformat()
                ),
                "last_session": (
                    None
                    if not member_series[code].closes
                    else member_series[code].closes[-1].session.isoformat()
                ),
            }
            for code in member_codes
        ),
    }
    member_source_manifest = {
        **member_source_stable,
        "content_sha256": sha256_json(member_source_stable),
    }
    member_source_path = output_dir / "sector_strength_member_sources.json"
    _atomic_json(member_source_path, member_source_manifest)
    strength_batches = build_replay_sector_strength_batches(
        evaluation_times=strength_evaluation_times,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark_daily,
        members_by_sector=members,
        member_series=member_series,
        market_sessions=market_sessions,
        # Keep this causal identity stable when future daily rows are appended.
        # Raw file/PIT provenance is separately chained by the source manifest.
        input_revision=str(catalog_entry["entry_sha256"]),
    )
    strength_entries: list[dict[str, object]] = []
    resolved_counts: list[int] = []
    distinct_score_counts: list[int] = []
    largest_ties: list[int] = []
    evidence_directory = output_dir / "sector_strength_evidence"
    for batch in strength_batches:
        batch_time = next(iter(batch.values())).observed_at
        evidence_path = evidence_directory / (
            batch_time.strftime("%Y%m%dT%H%M%S%z") + ".json"
        )
        evidence_document = {
            **batch.evidence_document(),
            "evidence_revision": batch.evidence_revision,
        }
        _atomic_json(evidence_path, evidence_document)
        resolved = tuple(value for value in batch.values() if value.resolved)
        score_counts: dict[str, int] = {}
        for value in resolved:
            score = str(value.strength)
            score_counts[score] = score_counts.get(score, 0) + 1
        resolved_counts.append(len(resolved))
        distinct_score_counts.append(len(score_counts))
        largest_ties.append(max(score_counts.values(), default=0))
        strength_entries.append(
            {
                "observed_at": batch_time,
                "evidence_revision": batch.evidence_revision,
                "evidence_path": str(evidence_path),
                "evidence_file_sha256": _sha256_file(evidence_path),
                "resolved_sector_count": len(resolved),
                "unresolved_sector_count": len(batch) - len(resolved),
                "distinct_strength_count": len(score_counts),
                "largest_equal_strength_tie": max(
                    score_counts.values(),
                    default=0,
                ),
            }
        )
    strength_manifest_stable = {
        "schema": "chanlun-sector-strength-replay-manifest",
        "benchmark_symbol": "SH.000300",
        "catalog_entry_sha256": catalog_entry["entry_sha256"],
        "member_source_manifest_sha256": member_source_manifest["content_sha256"],
        "historical_suspension_facts_available": False,
        "missing_cutoff_policy": "UNEXPLAINED_GAP_FAIL_CLOSED",
        "entries": tuple(strength_entries),
    }
    strength_manifest = {
        **strength_manifest_stable,
        "content_sha256": sha256_json(strength_manifest_stable),
    }
    strength_manifest_path = output_dir / "sector_strength_replay_manifest.json"
    _atomic_json(strength_manifest_path, strength_manifest)
    # Do not publish a ledger that already became stale while the expensive
    # sector prefixes were being built.  Checkpoints are intentionally kept so
    # a verified reclassification can reuse them, but the signed aggregate must
    # only be written under the same source snapshot that produced it.
    if recent_year_research_algorithm_hashes(PROJECT_ROOT) != hashes:
        raise RuntimeError("source code changed while current sectors were built")
    trigger = build_current_sector_trigger_ledger(
        sector_facts=sector_facts,
        sector_members=members,
        securities=snapshot.securities,
        observed_times=observed_times,
        algorithm_revision=revision,
        catalog_entry_sha256=str(catalog_entry["entry_sha256"]),
        security_snapshot_sha256=_sha256_file(pit_path),
        sector_strength_batches=strength_batches,
    )
    checkpoint = output_dir / "sector_first_trigger_ledger.pkl"
    report_path = output_dir / "sector_first_trigger_ledger.json"
    _atomic_bytes(checkpoint, pickle.dumps(trigger, protocol=pickle.HIGHEST_PROTOCOL))
    report = trigger.document()
    report.update(
        {
            "parameter_snapshot": parameters.document(),
            "catalog_ledger": str(catalog_path),
            "catalog_ledger_sha256": _sha256_file(catalog_path),
            "catalog_entry_sha256": catalog_entry["entry_sha256"],
            "catalog_captured_at": catalog_entry["captured_at"],
            "pit_security_snapshot_sha256": _sha256_file(pit_path),
            "pit_snapshot_available_at": pit_available_at.isoformat(),
            "pit_snapshot_availability_basis": pit_availability_basis,
            "selected_current_member_count": len(member_codes),
            "excluded_current_member_count": len(excluded),
            "excluded_current_members": excluded,
            "current_membership_backfilled": True,
            "survivorship_bias_accepted_for_research": True,
            "tick_data_used": False,
            "minimum_market_data_frequency": "1m",
            "horizontal_sector_strength": {
                "status": "STRICT_AVAILABLE_WITH_UNRESOLVED_GAPS",
                "benchmark_symbol": "SH.000300",
                "common_anchor": "LATEST_COMPLETED_DAILY_BOTTOM_FRACTAL",
                "ma_periods": (5, 13, 21, 34, 55, 89, 144, 233),
                "strict_crossing": "COMPLETED_DAILY_CLOSE_GT_SMA",
                "all_current_members_kept_in_denominator": True,
                "historical_suspension_facts_available": False,
                "missing_cutoff_policy": "UNEXPLAINED_GAP_FAIL_CLOSED",
                "batch_count": len(strength_batches),
                "resolved_sector_count_min": min(resolved_counts, default=0),
                "resolved_sector_count_median": _median_int(resolved_counts),
                "resolved_sector_count_max": max(resolved_counts, default=0),
                "distinct_strength_count_min": min(
                    distinct_score_counts,
                    default=0,
                ),
                "distinct_strength_count_median": _median_int(
                    distinct_score_counts
                ),
                "distinct_strength_count_max": max(
                    distinct_score_counts,
                    default=0,
                ),
                "largest_equal_strength_tie_max": max(largest_ties, default=0),
                "member_source_manifest": str(member_source_path),
                "member_source_manifest_sha256": member_source_manifest[
                    "content_sha256"
                ],
                "replay_manifest": str(strength_manifest_path),
                "replay_manifest_sha256": strength_manifest["content_sha256"],
            },
            "algorithm_hashes": tuple(
                {"path": path, "sha256": digest} for path, digest in hashes
            ),
            "algorithm_hash_scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
        }
    )
    _atomic_json(report_path, report)
    # Guard the much smaller serialization window as well.  This second check
    # catches a source edit racing the final document writes.
    if recent_year_research_algorithm_hashes(PROJECT_ROOT) != hashes:
        raise RuntimeError("source code changed while current sectors were built")
    print(
        json.dumps(
            {
                "complete": True,
                "sectors": len(sector_facts),
                "events": len(trigger.events),
                "current_members": report["selected_current_member_count"],
                "strength_batches": len(strength_batches),
                "resolved_sectors_min": min(resolved_counts, default=0),
                "resolved_sectors_max": max(resolved_counts, default=0),
                "output": str(report_path),
                "checkpoint": str(checkpoint),
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
