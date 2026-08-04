#!/usr/bin/env python3
"""Extract causal 30m/5m/1m facts after the historical sector trigger gate."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
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
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    load_snapshot,
)
from chanlun.decision_support.trading_system.v3_sector_first_direct_facts import (  # noqa: E402
    DIRECT_SYMBOL_FACT_SCHEMA,
    SectorFirstDirectSymbolFacts,
    build_sector_first_direct_symbol_facts,
)
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (  # noqa: E402
    load_sector_ledger,
)
from chanlun.decision_support.trading_system.v3_recent_year_research import (  # noqa: E402
    RECENT_YEAR_SELECTION_PATH,
    recent_year_research_parameters,
)
from chanlun.decision_support.trading_system.v3_recent_year_provenance import (  # noqa: E402
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)
from chanlun.decision_support.trading_system.v3_sector_first_scope import (  # noqa: E402
    build_sector_first_scope,
)
from chanlun.decision_support.trading_system.v3_sector_first_trigger_plan import (  # noqa: E402
    SectorFirstTriggerLedger,
)


DEFAULT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/sector_first_full_market"
)
DEFAULT_RECENT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/recent_year_current_sector_no3p"
)
DEFAULT_SNAPSHOT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    code: str
    warmup_start: date
    requested_start: date
    requested_end: date
    effective_start: date
    algorithm_revision: str
    producer_source_sha256: str
    fact_builder_source_sha256: str
    trigger_ledger_sha256: str
    target: str
    force: bool
    security_master: object
    memberships: tuple[object, ...]
    factors: tuple[object, ...]
    current_sector_id: str | None = None


_WORKER_TRIGGER_LEDGER: SectorFirstTriggerLedger | None = None


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
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--warmup-start",
        type=_parse_date,
        default=recent_year_research_parameters().warmup_start,
    )
    value.add_argument("--start", type=_parse_date, default=date(2025, 7, 25))
    value.add_argument(
        "--effective-start",
        type=_parse_date,
        default=date(2025, 8, 1),
    )
    value.add_argument("--end", type=_parse_date, default=date(2026, 7, 24))
    value.add_argument("--workers", type=_positive_int, default=12)
    value.add_argument("--limit", type=_positive_int)
    value.add_argument("--codes", help="optional comma-separated normalized codes")
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    value.add_argument(
        "--no-three-program",
        action="store_true",
        help=(
            "causally rescan only the frozen current-sector terminal query plan; "
            "the individual three-program is disabled"
        ),
    )
    value.add_argument(
        "--forward-paper-session",
        type=_parse_date,
        help="bind causal extraction to one forward-paper session",
    )
    value.add_argument(
        "--trigger-ledger",
        type=Path,
        default=DEFAULT_ROOT / "sector_first_trigger_ledger.pkl",
    )
    value.add_argument("--output-dir", type=Path, default=DEFAULT_ROOT)
    value.add_argument(
        "--query-plan",
        type=Path,
        default=DEFAULT_RECENT_ROOT / "terminal_query_plan.json",
    )
    value.add_argument(
        "--current-catalog-ledger",
        type=Path,
        default=Path(
            ".cache/chanlun_v3_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
        ),
    )
    value.add_argument("--force", action="store_true")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _checkpoint_binding(code: str, payload: bytes) -> dict[str, object]:
    """Bind the exact immutable pickle bytes consumed by a replay.

    The manifest used to describe only values parsed from each checkpoint.
    That allowed a pickle to be replaced after extraction without changing the
    manifest identity.  Hash the same byte snapshot that is written or parsed,
    and keep the canonical relative path so a later replay can verify bytes
    before unpickling them.
    """

    return {
        "checkpoint_path": (
            f"direct_symbols/{code.replace('.', '_')}.pkl"
        ),
        "checkpoint_sha256": _sha256_bytes(payload),
        "checkpoint_size_bytes": len(payload),
    }


def _load_query_plan(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("terminal query plan cannot be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("terminal query plan is invalid")
    content_sha256 = payload.get("content_sha256")
    stable = {key: value for key, value in payload.items() if key != "content_sha256"}
    computed = "sha256:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if content_sha256 != computed:
        raise ValueError("terminal query plan content hash changed")
    return payload


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
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8"),
    )


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "direct_symbols" / f"{code.replace('.', '_')}.pkl"


def _initialize_worker(trigger_path: str) -> None:
    global _WORKER_TRIGGER_LEDGER
    value = pickle.loads(Path(trigger_path).read_bytes())
    if not isinstance(value, SectorFirstTriggerLedger):
        raise ValueError("worker trigger checkpoint is invalid")
    _WORKER_TRIGGER_LEDGER = value


def _load_cached(
    request: WorkerRequest,
) -> tuple[SectorFirstDirectSymbolFacts, bytes] | None:
    try:
        payload = Path(request.target).read_bytes()
        value = pickle.loads(payload)
    except (OSError, EOFError, pickle.PickleError, ValueError, AttributeError):
        return None
    if (
        not isinstance(value, SectorFirstDirectSymbolFacts)
        or value.schema != DIRECT_SYMBOL_FACT_SCHEMA
        or value.algorithm_revision != request.algorithm_revision
        or value.trigger_ledger_sha256 != request.trigger_ledger_sha256
        or value.code != request.code
        or value.requested_start != request.requested_start
        or value.requested_end != request.requested_end
        or value.effective_start != request.effective_start
        or value.security_master != request.security_master
        or value.memberships != request.memberships
        or value.factors != request.factors
    ):
        return None
    return value, payload


def _summary(fact: SectorFirstDirectSymbolFacts) -> dict[str, object]:
    return {
        "code": fact.code,
        "source_revision": fact.source_revision,
        "source_start": None if fact.source_start is None else fact.source_start.isoformat(),
        "source_end": None if fact.source_end is None else fact.source_end.isoformat(),
        "rows_1m": fact.one_minute_row_count,
        "sector_trigger_window_count": len(fact.sector_trigger_windows),
        "direct_decision_count": len(fact.direct_decisions),
        "technical_entry_count": fact.technical_entry_count,
        "full_system_entry_count": fact.full_system_entry_count,
        "strategic_sell_point_count": len(fact.strategic_sell_points),
        "structural_point_count": len(fact.structural_points),
        "completed_unit_count": len(fact.completed_units),
        "completed_trend_count": len(fact.completed_trends),
        "point_anchor_count": len(fact.point_anchor_unit_ids),
        "point_counts": dict(fact.point_counts),
        "rejection_counts": dict(fact.rejection_counts),
        "three_program_status": fact.three_program_status,
        "data_grade": fact.data_grade,
        "live_status": fact.live_status,
    }


def _request_summary(
    fact: SectorFirstDirectSymbolFacts,
    request: WorkerRequest,
) -> dict[str, object]:
    value = _summary(fact)
    if request.current_sector_id is not None:
        value.update(
            {
                "current_sector_id": request.current_sector_id,
                "three_program_status": "DISABLED_USER_AUTHORIZED",
                "data_grade": "RESEARCH_ONLY",
            }
        )
    return value


def _worker(request: WorkerRequest) -> dict[str, object]:
    started = time.perf_counter()
    if _WORKER_TRIGGER_LEDGER is None:
        raise RuntimeError("worker trigger ledger was not initialized")
    if (
        recent_year_research_algorithm_revision(
            recent_year_research_algorithm_hashes(PROJECT_ROOT)
        )
        != request.algorithm_revision
        or _sha256_file(Path(__file__).resolve())
        != request.producer_source_sha256
        or _sha256_file(
            SOURCE_ROOT
            / "chanlun/decision_support/trading_system/v3_sector_first_direct_facts.py"
        )
        != request.fact_builder_source_sha256
    ):
        raise RuntimeError("worker algorithm differs from frozen extraction")
    cached = None if request.force else _load_cached(request)
    if cached is not None:
        fact, checkpoint_payload = cached
        return {
            **_request_summary(fact, request),
            **_checkpoint_binding(request.code, checkpoint_payload),
            "seconds": 0.0,
            "cached": True,
        }
    fact = build_sector_first_direct_symbol_facts(
        code=request.code,
        warmup_start=request.warmup_start,
        requested_start=request.requested_start,
        requested_end=request.requested_end,
        effective_start=request.effective_start,
        algorithm_revision=request.algorithm_revision,
        trigger_ledger=_WORKER_TRIGGER_LEDGER,
        trigger_ledger_sha256=request.trigger_ledger_sha256,
        security_master=request.security_master,  # type: ignore[arg-type]
        memberships=request.memberships,  # type: ignore[arg-type]
        qmt_factors=request.factors,  # type: ignore[arg-type]
        current_sector_id=request.current_sector_id,
    )
    if (
        recent_year_research_algorithm_revision(
            recent_year_research_algorithm_hashes(PROJECT_ROOT)
        )
        != request.algorithm_revision
        or _sha256_file(Path(__file__).resolve())
        != request.producer_source_sha256
        or _sha256_file(
            SOURCE_ROOT
            / "chanlun/decision_support/trading_system/v3_sector_first_direct_facts.py"
        )
        != request.fact_builder_source_sha256
    ):
        raise RuntimeError("algorithm changed while direct facts were built")
    checkpoint_payload = pickle.dumps(fact, protocol=pickle.HIGHEST_PROTOCOL)
    _atomic_bytes(Path(request.target), checkpoint_payload)
    return {
        **_request_summary(fact, request),
        **_checkpoint_binding(request.code, checkpoint_payload),
        "seconds": round(time.perf_counter() - started, 3),
        "cached": False,
    }


def _manifest(
    *,
    args: argparse.Namespace,
    algorithm_revision: str,
    algorithm_hashes: Sequence[tuple[str, str]],
    producer_source_sha256: str,
    fact_builder_source_sha256: str,
    trigger_ledger_sha256: str,
    scope_sha256: str,
    selected_count: int,
    completed: Mapping[str, Mapping[str, object]],
    failures: Mapping[str, str],
    started_at: float,
    selection_path: str,
    current_catalog_entry_sha256: str | None,
    current_catalog_ledger_sha256: str | None,
    query_plan_sha256: str | None,
) -> dict[str, object]:
    current_mode = selection_path == RECENT_YEAR_SELECTION_PATH
    return {
        "schema": "chanlun-v3-sector-first-direct-extract/v3",
        "generated_at": datetime_now(),
        "complete": len(completed) == selected_count and not failures,
        "selection_path": selection_path,
        "selection_order": (
            (
                *(
                    (
                        "QMT_PIT_GICS3_SECTOR_TRIGGER",
                        "SAME_SESSION_CAPTURED_SECTOR_MEMBERSHIP",
                    )
                    if args.forward_paper_session is not None
                    else (
                        "QMT_CURRENT_GICS3_SECTOR_TRIGGER",
                        "CURRENT_SECTOR_MEMBERSHIP_BACKFILLED_USER_AUTHORIZED",
                    )
                ),
                "DIRECT_RECURSIVE_30M_5M_1M_TECHNICAL_ENTRY",
                "LATER_COMPLETED_1M_BAR_EXECUTION_ONLY",
            )
            if current_mode
            else (
                "POINT_IN_TIME_SECTOR_TRIGGER",
                "POINT_IN_TIME_SECTOR_MEMBERS",
                "INDIVIDUAL_THREE_PROGRAM",
                "DIRECT_RECURSIVE_30M_5M_1M_TECHNICAL_ENTRY",
            )
        ),
        "algorithm": {
            "scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
            "revision": algorithm_revision,
            "hashes": tuple(
                {"path": path, "sha256": digest}
                for path, digest in algorithm_hashes
            ),
            "producer_source_sha256": producer_source_sha256,
            "fact_builder_source_sha256": fact_builder_source_sha256,
        },
        "inputs": {
            "pit_snapshot": str(args.pit_snapshot.resolve()),
            "pit_snapshot_sha256": _sha256_file(args.pit_snapshot.resolve()),
            "trigger_ledger": str(args.trigger_ledger.resolve()),
            "trigger_ledger_sha256": trigger_ledger_sha256,
            "sector_scope_sha256": scope_sha256,
            "query_plan_sha256": query_plan_sha256,
            "current_catalog_entry_sha256": current_catalog_entry_sha256,
            "current_catalog_ledger_sha256": current_catalog_ledger_sha256,
            "warmup_start": args.warmup_start.isoformat(),
            "start": args.start.isoformat(),
            "effective_start": args.effective_start.isoformat(),
            "end": args.end.isoformat(),
            "forward_paper_session": (
                None
                if args.forward_paper_session is None
                else args.forward_paper_session.isoformat()
            ),
        },
        "summary": {
            "selected_symbol_count": selected_count,
            "completed_symbol_count": len(completed),
            "failed_symbol_count": len(failures),
            "symbols_with_technical_entries": sum(
                int(row["technical_entry_count"]) > 0
                for row in completed.values()
            ),
            "technical_entry_count": sum(
                int(row["technical_entry_count"])
                for row in completed.values()
            ),
            "full_system_entry_count": (
                None
                if current_mode
                else 0
            ),
            "direct_decision_count": sum(
                int(row["direct_decision_count"])
                for row in completed.values()
            ),
            "one_minute_rows": sum(
                int(row["rows_1m"]) for row in completed.values()
            ),
            "elapsed_seconds": round(time.perf_counter() - started_at, 2),
        },
        "research_variant_gate": {
            "status": "USER_AUTHORIZED_RESEARCH_VARIANT" if current_mode else "UNRESOLVED",
            "three_program_mode": (
                "DISABLED_USER_AUTHORIZED" if current_mode else "UNRESOLVED"
            ),
            "current_membership_backfilled": (
                current_mode and args.forward_paper_session is None
            ),
            "same_session_membership_capture": (
                current_mode and args.forward_paper_session is not None
            ),
            "technical_replay_may_run": True,
            "formal_v3_or_survivor_bias_free_claim_allowed": False,
        },
        "failures": dict(sorted(failures.items())),
        "symbols": dict(sorted(completed.items())),
        "data_grade": "RESEARCH_ONLY" if current_mode else "COMPONENT_ONLY",
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def datetime_now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.no_three_program:
        if args.output_dir == DEFAULT_ROOT:
            args.output_dir = DEFAULT_RECENT_ROOT
        if args.trigger_ledger == DEFAULT_ROOT / "sector_first_trigger_ledger.pkl":
            args.trigger_ledger = DEFAULT_RECENT_ROOT / "sector_first_trigger_ledger.pkl"
    if not args.warmup_start <= args.start <= args.effective_start <= args.end:
        raise ValueError("expected warmup_start <= start <= effective_start <= end")
    snapshot = load_snapshot(args.pit_snapshot.resolve())
    trigger = pickle.loads(args.trigger_ledger.resolve().read_bytes())
    if not isinstance(trigger, SectorFirstTriggerLedger):
        raise ValueError("sector trigger ledger checkpoint is invalid")
    trigger_sha256 = _sha256_file(args.trigger_ledger.resolve())
    hashes = recent_year_research_algorithm_hashes(PROJECT_ROOT)
    revision = recent_year_research_algorithm_revision(hashes)
    producer_source_sha256 = _sha256_file(Path(__file__).resolve())
    fact_builder_source_sha256 = _sha256_file(
        SOURCE_ROOT
        / "chanlun/decision_support/trading_system/v3_sector_first_direct_facts.py"
    )
    if trigger.algorithm_revision != revision:
        raise RuntimeError("sector trigger ledger uses a different algorithm revision")
    index = PITMetadataIndex(snapshot)
    current_sector_by_code: dict[str, str] = {}
    current_catalog_entry_sha256: str | None = None
    current_catalog_ledger_sha256: str | None = None
    query_plan_sha256: str | None = None
    if args.no_three_program:
        parameters = recent_year_research_parameters()
        if args.forward_paper_session is None:
            if (
                args.warmup_start,
                args.start,
                args.effective_start,
                args.end,
            ) != (
                parameters.warmup_start,
                parameters.requested_start,
                parameters.effective_start,
                parameters.requested_end,
            ):
                raise ValueError(
                    "no-three-program extraction must use the frozen recent-year dates"
                )
        elif (
            args.warmup_start != parameters.warmup_start
            or args.start != args.forward_paper_session
            or args.effective_start != args.forward_paper_session
            or args.end != args.forward_paper_session
            or trigger.selection_order[:2]
            != (
                "QMT_PIT_SECTOR_TRIGGER",
                "QMT_PIT_MEMBERS_CAPTURED_SAME_SESSION",
            )
        ):
            raise ValueError("forward paper extraction dates or PIT trigger changed")
        if trigger.selection_path != RECENT_YEAR_SELECTION_PATH:
            raise ValueError("no-three-program extraction requires the current-sector trigger")
        catalog_path = args.current_catalog_ledger.resolve()
        catalog = load_sector_ledger(catalog_path)
        entries = tuple(catalog["entries"])
        if not entries:
            raise ValueError("current QMT sector ledger is empty")
        catalog_entry = entries[-1]
        if (
            args.forward_paper_session is not None
            and date.fromisoformat(str(catalog_entry["captured_at"])[:10])
            != args.forward_paper_session
        ):
            raise ValueError("forward paper extraction requires same-session catalog")
        current_catalog_entry_sha256 = str(catalog_entry["entry_sha256"])
        current_catalog_ledger_sha256 = _sha256_file(catalog_path)
        if trigger.sector_scope_sha256 != current_catalog_entry_sha256:
            raise RuntimeError("current-sector trigger uses a different catalog capture")
        for row in catalog_entry["sectors"]:
            sector_id = str(row["sector_id"])
            for code in row["member_codes"]:
                normalized = str(code)
                previous = current_sector_by_code.setdefault(normalized, sector_id)
                if previous != sector_id:
                    raise ValueError(f"current QMT member belongs to multiple GICS3 sectors: {normalized}")
        query_path = args.query_plan.resolve()
        query = _load_query_plan(query_path)
        query_plan_sha256 = _sha256_file(query_path)
        if (
            query.get("schema") != "chanlun-v3-sector-first-terminal-query-plan/v2"
            or query.get("authority") != "QUERY_PLANNER_ONLY_CAUSAL_RESCAN_REQUIRED"
            or query.get("selection_path") != RECENT_YEAR_SELECTION_PATH
            or query.get("three_program_mode") != "DISABLED_USER_AUTHORIZED"
            or query.get("algorithm_revision") != revision
            or query.get("algorithm_hash_scope")
            != RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE
            or query.get("trigger_ledger_sha256") != trigger_sha256
            or query.get("current_catalog_entry_sha256")
            != current_catalog_entry_sha256
            or query.get("current_catalog_ledger_sha256")
            != current_catalog_ledger_sha256
            or query.get("research_parameter_set_id") != parameters.parameter_set_id
            or query.get("forward_paper_session")
            != (
                None
                if args.forward_paper_session is None
                else args.forward_paper_session.isoformat()
            )
            or int(query.get("failed_symbol_count", -1)) != 0
            or query.get("failures") != {}
        ):
            raise ValueError("terminal query plan is not bound to this frozen research run")
        raw_available = query.get("potential_symbols")
        if not isinstance(raw_available, list):
            raise ValueError("terminal query plan potential symbols are unavailable")
        available = tuple(sorted(set(str(code) for code in raw_available)))
        if len(available) != int(query.get("potential_symbol_count", -1)):
            raise ValueError("terminal query plan potential symbol count changed")
        missing_current = tuple(
            code for code in available if code not in current_sector_by_code
        )
        if missing_current:
            raise ValueError(f"potential symbols are absent from current catalog: {missing_current}")
        scope_sha256 = current_catalog_entry_sha256
        selection_path = RECENT_YEAR_SELECTION_PATH
    else:
        scope = build_sector_first_scope(
            snapshot,
            requested_start=args.start,
            requested_end=args.end,
        )
        if trigger.sector_scope_sha256 != scope.content_sha256:
            raise RuntimeError("sector trigger ledger uses a different sector scope")
        available = scope.selected_symbols
        scope_sha256 = scope.content_sha256
        selection_path = "INDIVIDUAL_THREE_PROGRAM"
    if args.codes:
        requested = tuple(
            sorted(set(value.strip().upper() for value in args.codes.split(",") if value.strip()))
        )
        unknown = tuple(code for code in requested if code not in set(available))
        if unknown:
            raise ValueError(f"codes are outside sector-first scope: {unknown}")
        selected = requested
    else:
        selected = available
    if args.limit is not None:
        selected = selected[: args.limit]
    requests = tuple(
        WorkerRequest(
            code=code,
            warmup_start=args.warmup_start,
            requested_start=args.start,
            requested_end=args.end,
            effective_start=args.effective_start,
            algorithm_revision=revision,
            producer_source_sha256=producer_source_sha256,
            fact_builder_source_sha256=fact_builder_source_sha256,
            trigger_ledger_sha256=trigger_sha256,
            target=str(_fact_path(args.output_dir, code).resolve()),
            force=args.force,
            security_master=index.security(code),
            memberships=index.memberships_for(code),
            factors=index.factors_for(code),
            current_sector_id=current_sector_by_code.get(code),
        )
        for code in selected
    )
    completed: dict[str, dict[str, object]] = {}
    failures: dict[str, str] = {}
    started = time.perf_counter()
    manifest_path = args.output_dir / "direct_extract_manifest.json"
    workers = max(1, min(args.workers, len(requests)))
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_initialize_worker,
        initargs=(str(args.trigger_ledger.resolve()),),
    ) as executor:
        jobs = {
            executor.submit(_worker, request): request.code
            for request in requests
        }
        for position, future in enumerate(as_completed(jobs), start=1):
            code = jobs[future]
            try:
                completed[code] = future.result()
            except Exception as exc:  # fail closed but retain all checkpoints
                failures[code] = f"{type(exc).__name__}: {exc}"
            if position % 10 == 0 or position == len(requests):
                _atomic_json(
                    manifest_path,
                    _manifest(
                        args=args,
                        algorithm_revision=revision,
                        algorithm_hashes=hashes,
                        producer_source_sha256=producer_source_sha256,
                        fact_builder_source_sha256=fact_builder_source_sha256,
                        trigger_ledger_sha256=trigger_sha256,
                        scope_sha256=scope_sha256,
                        selected_count=len(selected),
                        completed=completed,
                        failures=failures,
                        started_at=started,
                        selection_path=selection_path,
                        current_catalog_entry_sha256=current_catalog_entry_sha256,
                        current_catalog_ledger_sha256=current_catalog_ledger_sha256,
                        query_plan_sha256=query_plan_sha256,
                    ),
                )
            print(
                f"[{position}/{len(requests)}] {code} "
                f"{'FAILED' if code in failures else 'complete'}",
                flush=True,
            )
    # An empty terminal query plan is a valid forward observation, not a
    # missing artifact.  Always persist the final manifest even when no worker
    # future existed.
    _atomic_json(
        manifest_path,
        _manifest(
            args=args,
            algorithm_revision=revision,
            algorithm_hashes=hashes,
            producer_source_sha256=producer_source_sha256,
            fact_builder_source_sha256=fact_builder_source_sha256,
            trigger_ledger_sha256=trigger_sha256,
            scope_sha256=scope_sha256,
            selected_count=len(selected),
            completed=completed,
            failures=failures,
            started_at=started,
            selection_path=selection_path,
            current_catalog_entry_sha256=current_catalog_entry_sha256,
            current_catalog_ledger_sha256=current_catalog_ledger_sha256,
            query_plan_sha256=query_plan_sha256,
        ),
    )
    if (
        recent_year_research_algorithm_hashes(PROJECT_ROOT) != hashes
        or _sha256_file(Path(__file__).resolve()) != producer_source_sha256
        or _sha256_file(
            SOURCE_ROOT
            / "chanlun/decision_support/trading_system/v3_sector_first_direct_facts.py"
        )
        != fact_builder_source_sha256
    ):
        raise RuntimeError("source code changed during direct extraction")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
