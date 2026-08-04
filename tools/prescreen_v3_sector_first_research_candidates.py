#!/usr/bin/env python3
"""Plan expensive causal scans after sector-first QMT research filtering.

This command is deliberately a *query planner*, not signal authority.  It
uses one terminal strict-structure snapshot to find symbols that may contain
a 30m strategic point while both the point-in-time sector window and the
frozen monthly QMT research approximation are active.  Every positive symbol
must subsequently be rebuilt by ``extract_v3_sector_first_direct_facts.py``;
only that prefix replay may emit a trade.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    load_qmt_frame,
    qmt_factor_frame,
    strict_state,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    PITMetadataSnapshot,
    SecurityMasterRecord,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
)
from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (  # noqa: E402
    build_v3_direct_recursive_structure_path,
)
from chanlun.decision_support.trading_system.v3_research_approximation import (  # noqa: E402
    ResearchApproximationLedger,
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
from chanlun.decision_support.trading_system.v3_sector_first_trigger_plan import (  # noqa: E402
    SectorFirstTriggerLedger,
    sector_trigger_windows_for_symbol,
)


DEFAULT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/sector_first_full_market_v2"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
QUERY_ROW_CHECKPOINT_SCHEMA = "chanlun-v3-terminal-query-row-checkpoint/v1"
_COMPATIBLE_SUPERSET_PLANNER_REVISIONS = frozenset(
    {
        # Full 5,126-symbol plan published before horizontal-strength ranking.
        # Its _scan implementation is unchanged; only the orchestration below
        # adds a machine-checked conservative-superset reuse path.
        "sha256:f79aec498c506e6a8eca9900b38ff900a3b3375f946981368fd639c68acfa4e8",
        # Formal v20 plan and the 47-symbol current-algorithm sample were both
        # produced by this exact source.  The only later changes add the two
        # exact structural-hash transition checks below; ``_scan`` is byte-for-
        # byte unchanged, so its signed rows remain admissible as the prior
        # wider-window plan.
        "sha256:92092c427c3d1fc760cab5705ec488f438dfd5bb8e4e405e131b39a770ac9784",
        # Formal v21 producer.  Its scan body is unchanged; the later source
        # edit only admits an exactly matching current producer automatically
        # so a freshly published plan does not require another hard-coded row.
        "sha256:c169d22b637c668395c4e88142e2f2e79878ec4c4023009ea12c7d845262fc44",
    }
)
_COMPATIBLE_CHECKPOINT_PLANNER_REVISIONS = frozenset(
    {
        # Same _scan body as this revision; only aggregate reuse accounting
        # was corrected afterwards.  Every row remains protected by its own
        # checkpoint content hash and all other identity fields.
        "sha256:f9361685eb203a21adf12bc31defca8057aa14f0e490402ae6cfdb7754947f72",
        # Same scan body after the resumed/computed aggregate counter fix.
        "sha256:6ee7209b87ec0b284e35a32e56ce13aeb2b957e890e6fbccf3a1e9b3addae23e",
    }
)
_STRUCTURAL_SUPERSET_PATHS = frozenset(
    {
        "src/chanlun/decision_support/fingerprints.py",
        "src/chanlun/decision_support/trading_system/backtest/fixed_year.py",
        "src/chanlun/decision_support/trading_system/backtest/pit_metadata.py",
        "src/chanlun/decision_support/trading_system/backtest/qmt_local_cache.py",
        "src/chanlun/decision_support/trading_system/qmt_causal_factor_adjustment.py",
        "src/chanlun/decision_support/trading_system/structure_adapter.py",
        "src/chanlun/decision_support/trading_system/v3_direct_recursive_structure.py",
        "src/chanlun/decision_support/trading_system/v3_structure_adapter.py",
        "src/chanlun/exchange/kline_precision.py",
        "src/chanlun/exchange/price_basis.py",
        "src/chanlun/tools/log_util.py",
    }
)
_COMPATIBLE_STRUCTURAL_HASH_TRANSITIONS = frozenset(
    {
        (
            "src/chanlun/core/beichi_calculator.py",
            "sha256:9e490b0c4f080c1446a7f9decfb518cf3df75978eb5677963a8b73cf6b4d0573",
            "sha256:cbd028dd33b5884ec36e6031b9c36031803a0ecc322313a3a3b831ddbe76c11d",
        ),
        (
            "src/chanlun/decision_support/trading_system/backtest/qmt_local_cache.py",
            "sha256:46bfb94aaf48c01dc8cc0b7752bdfd38018d24043a8c61c9904117fd63d82d45",
            "sha256:7fbaede3c7dabe9f3c01e7591af3692ae2861846f7ed158ba9395dfaa32d3edb",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _Request:
    code: str
    warmup_start: date
    effective_start: date
    requested_end: date


_TRIGGER: SectorFirstTriggerLedger | None = None
_RESEARCH: ResearchApproximationLedger | None = None
_SNAPSHOT: PITMetadataSnapshot | None = None
_INDEX: PITMetadataIndex | None = None
_CURRENT_SECTOR_BY_CODE: dict[str, str] | None = None
_CURRENT_SECTOR_INTERVALS: dict[
    str, tuple[tuple[datetime, datetime], ...]
] | None = None


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--warmup-start",
        type=_parse_date,
        default=recent_year_research_parameters().warmup_start,
    )
    value.add_argument("--effective-start", type=_parse_date, default=date(2025, 8, 1))
    value.add_argument("--end", type=_parse_date, default=date(2026, 7, 24))
    value.add_argument("--workers", type=int, default=12)
    value.add_argument("--limit", type=int)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument(
        "--trigger-ledger",
        type=Path,
        default=DEFAULT_ROOT / "sector_first_trigger_ledger.pkl",
    )
    value.add_argument(
        "--research-ledger",
        type=Path,
        default=DEFAULT_ROOT / "research_approximation_ledger.pkl",
    )
    value.add_argument(
        "--no-three-program",
        action="store_true",
        help="scan every captured current QMT sector member without three-program",
    )
    value.add_argument(
        "--forward-paper-session",
        type=_parse_date,
        help="bind this query plan to one same-session forward-paper capture",
    )
    value.add_argument(
        "--current-catalog-ledger",
        type=Path,
        default=Path(
            ".cache/chanlun_v3_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
        ),
    )
    value.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ROOT / "terminal_query_plan.json",
    )
    value.add_argument(
        "--checkpoint-dir",
        type=Path,
        help="idempotent per-symbol rows (default: <output-dir>/terminal_query_rows)",
    )
    value.add_argument(
        "--conservative-superset-query-plan",
        type=Path,
        help=(
            "reuse only negative rows from a fully completed prior plan after "
            "proving that every new sector window is a subset of the prior one"
        ),
    )
    value.add_argument(
        "--conservative-superset-trigger-ledger",
        type=Path,
        help="trigger ledger bound to --conservative-superset-query-plan",
    )
    value.add_argument("--qmt-local-data-dir", type=Path, required=True)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_signed_query_plan(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("conservative superset query plan must be an object")
    claimed = document.get("content_sha256")
    stable = {key: value for key, value in document.items() if key != "content_sha256"}
    actual = "sha256:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if claimed != actual:
        raise ValueError("conservative superset query plan content hash changed")
    return document


def _algorithm_hash_map(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError("conservative superset algorithm hashes are unavailable")
    output: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("conservative superset algorithm hash row is invalid")
        path = row.get("path")
        digest = row.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise ValueError("conservative superset algorithm hash row is invalid")
        if path in output:
            raise ValueError("conservative superset algorithm hash path is duplicated")
        output[path] = digest
    return output


def _is_structural_superset_path(path: str) -> bool:
    return path.startswith("src/chanlun/core/") or path in _STRUCTURAL_SUPERSET_PATHS


def _structural_hash_transition_is_compatible(
    path: str,
    prior_sha256: str | None,
    current_sha256: str,
) -> bool:
    """Permit only audited, exact non-planner-output source transitions.

    ``beichi_calculator`` supplies the legacy MMD path which ``strict_state``
    explicitly disables for terminal planning.  The QMT cache transition
    parses and hashes one immutable byte snapshot instead of memmapping and
    hashing the path afterwards; for stable bytes its selected K-line frame is
    identical.  Both transitions have targeted tests and a real SH.600066
    old/new fact comparison.  Hash triples are exact so any subsequent edit
    fails closed and requires a new review.
    """

    return prior_sha256 == current_sha256 or (
        path,
        prior_sha256,
        current_sha256,
    ) in _COMPATIBLE_STRUCTURAL_HASH_TRANSITIONS


def _conservative_superset_negative_rows(
    *,
    prior_plan_path: Path,
    prior_trigger_path: Path,
    current_trigger: SectorFirstTriggerLedger,
    current_codes: tuple[str, ...],
    current_sector_by_code: Mapping[str, str],
    current_hashes: tuple[tuple[str, str], ...],
    current_planner_source_sha256: str,
    catalog_entry_sha256: str,
    catalog_ledger_sha256: str,
    research_parameter_set_id: str,
    warmup_start: date,
    effective_start: date,
    end: date,
) -> tuple[dict[str, dict[str, object]], tuple[str, ...], dict[str, object]]:
    """Reuse only proven negatives from a wider historical trigger plan.

    A narrower sector window cannot create a strategic point that was absent
    from a wider window.  If a sector gained any new window, every current
    member of that sector is rescanned in addition to all prior positives.
    Reuse is safe only if universe, dates, PIT metadata, structural code, and
    every event timestamp are identical.  Any missing proof fails closed and
    forces the normal full scan.
    """

    prior = _load_signed_query_plan(prior_plan_path)
    prior_trigger_raw = prior_trigger_path.read_bytes()
    prior_trigger = pickle.loads(prior_trigger_raw)
    if not isinstance(prior_trigger, SectorFirstTriggerLedger):
        raise ValueError("conservative superset trigger ledger is invalid")
    if (
        prior.get("schema") != "chanlun-v3-sector-first-terminal-query-plan/v2"
        or prior.get("authority") != "QUERY_PLANNER_ONLY_CAUSAL_RESCAN_REQUIRED"
        or prior.get("selection_path") != RECENT_YEAR_SELECTION_PATH
        or prior.get("three_program_mode") != "DISABLED_USER_AUTHORIZED"
        or prior.get("live_status") != "LIVE_DISABLED"
        or prior.get("failed_symbol_count") != 0
        or prior.get("failures") != {}
        or (
            prior.get("producer_source_sha256")
            != current_planner_source_sha256
            and prior.get("producer_source_sha256")
            not in _COMPATIBLE_SUPERSET_PLANNER_REVISIONS
        )
        or prior.get("trigger_ledger_sha256") != _sha256_file(prior_trigger_path)
        or prior.get("current_catalog_entry_sha256") != catalog_entry_sha256
        or prior.get("current_catalog_ledger_sha256") != catalog_ledger_sha256
        or prior.get("research_parameter_set_id") != research_parameter_set_id
        or prior.get("forward_paper_session") is not None
        or prior.get("observation_range")
        != {
            "warmup_start": warmup_start.isoformat(),
            "effective_start": effective_start.isoformat(),
            "end": end.isoformat(),
        }
    ):
        raise ValueError("conservative superset query plan contract changed")
    if (
        prior_trigger.selection_path != current_trigger.selection_path
        or prior_trigger.taxonomy != current_trigger.taxonomy
        or prior_trigger.pit_snapshot_sha256 != current_trigger.pit_snapshot_sha256
        or len(prior_trigger.events) != len(current_trigger.events)
    ):
        raise ValueError("conservative superset trigger identity changed")

    prior_hashes = _algorithm_hash_map(prior.get("algorithm_hashes"))
    current_hash_map = dict(current_hashes)
    compared_paths = tuple(
        sorted(path for path in current_hash_map if _is_structural_superset_path(path))
    )
    if not compared_paths or any(
        not _structural_hash_transition_is_compatible(
            path,
            prior_hashes.get(path),
            current_hash_map[path],
        )
        for path in compared_paths
    ):
        raise ValueError("conservative superset structural algorithm changed")
    compatible_transition_paths = tuple(
        path
        for path in compared_paths
        if prior_hashes.get(path) != current_hash_map[path]
    )

    narrowed_event_count = 0
    expanded_event_count = 0
    added_sector_ids: set[str] = set()
    for prior_event, current_event in zip(
        prior_trigger.events,
        current_trigger.events,
    ):
        if prior_event.observed_at != current_event.observed_at:
            raise ValueError("conservative superset event timeline changed")
        prior_ids = {row.sector_id for row in prior_event.ranked_sectors}
        current_ids = {row.sector_id for row in current_event.ranked_sectors}
        added = current_ids - prior_ids
        if added:
            added_sector_ids.update(added)
            expanded_event_count += 1
        narrowed_event_count += current_ids != prior_ids

    raw_rows = prior.get("rows")
    raw_potential = prior.get("potential_symbols")
    if not isinstance(raw_rows, list) or not isinstance(raw_potential, list):
        raise ValueError("conservative superset plan rows are unavailable")
    rows_by_code: dict[str, dict[str, object]] = {}
    for row in raw_rows:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str):
            raise ValueError("conservative superset plan row is invalid")
        code = str(row["code"])
        if code in rows_by_code or not isinstance(row.get("potential"), bool):
            raise ValueError("conservative superset plan row identity is invalid")
        rows_by_code[code] = dict(row)
    current_set = set(current_codes)
    if (
        set(rows_by_code) != current_set
        or prior.get("requested_symbol_count") != len(current_codes)
        or prior.get("completed_symbol_count") != len(current_codes)
        or len(raw_rows) != len(current_codes)
    ):
        raise ValueError("conservative superset universe changed")
    potential = tuple(sorted(set(str(code) for code in raw_potential)))
    row_potential = tuple(
        sorted(code for code, row in rows_by_code.items() if row["potential"])
    )
    if (
        potential != row_potential
        or prior.get("potential_symbol_count") != len(potential)
    ):
        raise ValueError("conservative superset potential set changed")
    catalog_sector_ids = set(current_sector_by_code.values())
    if not added_sector_ids.issubset(catalog_sector_ids):
        raise ValueError("expanded sector is absent from the current catalog")
    rescan_codes = tuple(
        sorted(
            set(potential)
            | {
                code
                for code in current_codes
                if current_sector_by_code.get(code) in added_sector_ids
            }
        )
    )
    rescan_set = set(rescan_codes)
    negatives = {
        code: {
            **rows_by_code[code],
            "reuse_basis": "PRIOR_WIDER_SECTOR_WINDOW_NEGATIVE",
        }
        for code in current_codes
        if code not in rescan_set
    }
    proof = {
        "status": "PROVEN_CONSERVATIVE_RESCAN_COVERAGE",
        "prior_query_plan": str(prior_plan_path),
        "prior_query_plan_sha256": _sha256_file(prior_plan_path),
        "prior_trigger_ledger": str(prior_trigger_path),
        "prior_trigger_ledger_sha256": _sha256_file(prior_trigger_path),
        "prior_producer_source_sha256": prior["producer_source_sha256"],
        "structural_hash_path_count": len(compared_paths),
        "compatible_structural_transition_paths": compatible_transition_paths,
        "event_count": len(current_trigger.events),
        "narrowed_event_count": narrowed_event_count,
        "expanded_event_count": expanded_event_count,
        "added_sector_ids": tuple(sorted(added_sector_ids)),
        "added_sector_member_rescan_count": len(
            set(rescan_codes) - set(potential)
        ),
        "prior_potential_symbol_count": len(potential),
        "total_rescan_symbol_count": len(rescan_codes),
        "reused_negative_symbol_count": len(negatives),
        "logical_implication": (
            "PRIOR_NEGATIVE_OUTSIDE_ALL_ADDED_SECTORS_IMPLIES_NEW_NEGATIVE"
        ),
    }
    return negatives, rescan_codes, proof


def _current_sector_interval_index(
    ledger: SectorFirstTriggerLedger,
) -> dict[str, tuple[tuple[datetime, datetime], ...]]:
    """Index ranked-sector event intervals once per worker.

    The previous implementation rebuilt ``{row.sector_id ...}`` for every
    event and every symbol: roughly 5,000 * 1,900 repeated scans of the same
    immutable ledger.  This index preserves the exact event intervals and
    defers only the security listing-date filter to the per-symbol call.
    """

    if ledger.selection_path != RECENT_YEAR_SELECTION_PATH:
        raise ValueError("current-sector interval index requires recent-year ledger")
    values: dict[str, list[tuple[datetime, datetime]]] = {}
    microsecond = timedelta(microseconds=1)
    for position, event in enumerate(ledger.events):
        end = (
            ledger.events[position + 1].observed_at - microsecond
            if position + 1 < len(ledger.events)
            else event.observed_at
        )
        for row in event.ranked_sectors:
            values.setdefault(row.sector_id, []).append((event.observed_at, end))
    return {key: tuple(intervals) for key, intervals in values.items()}


def _listed_current_sector_windows(
    *,
    intervals: Sequence[tuple[datetime, datetime]],
    security: SecurityMasterRecord,
) -> tuple[tuple[datetime, datetime], ...]:
    """Apply the original listing filter and adjacent-window merge exactly."""

    windows: list[tuple[datetime, datetime]] = []
    microsecond = timedelta(microseconds=1)
    for start, end in intervals:
        if not security.listed_on(start.date()):
            continue
        if windows and start <= windows[-1][1] + microsecond:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return tuple(windows)


def _initialize_worker(
    trigger_path: str,
    research_path: str | None,
    pit_path: str,
    qmt_data_dir: str,
    current_catalog_path: str | None,
) -> None:
    global _TRIGGER, _RESEARCH, _SNAPSHOT, _INDEX, _CURRENT_SECTOR_BY_CODE
    global _CURRENT_SECTOR_INTERVALS
    os.environ[QMT_LOCAL_DATA_ENV] = qmt_data_dir
    _TRIGGER = pickle.loads(Path(trigger_path).read_bytes())
    _RESEARCH = (
        None
        if research_path is None
        else pickle.loads(Path(research_path).read_bytes())
    )
    _SNAPSHOT = load_snapshot(Path(pit_path))
    _INDEX = PITMetadataIndex(_SNAPSHOT)
    if not isinstance(_TRIGGER, SectorFirstTriggerLedger):
        raise ValueError("query planner trigger ledger is invalid")
    if _RESEARCH is not None and not isinstance(_RESEARCH, ResearchApproximationLedger):
        raise ValueError("query planner research ledger is invalid")
    _CURRENT_SECTOR_BY_CODE = None
    _CURRENT_SECTOR_INTERVALS = None
    if current_catalog_path is not None:
        ledger = load_sector_ledger(Path(current_catalog_path))
        entries = tuple(ledger["entries"])
        if not entries:
            raise ValueError("current QMT sector ledger is empty")
        rows = tuple(entries[-1]["sectors"])
        _CURRENT_SECTOR_BY_CODE = {
            code: str(row["sector_id"])
            for row in rows
            for code in row["member_codes"]
        }
        _CURRENT_SECTOR_INTERVALS = _current_sector_interval_index(_TRIGGER)


def _inside(
    observed_at: datetime,
    windows: tuple[tuple[datetime, datetime], ...],
) -> bool:
    return any(start <= observed_at <= end for start, end in windows)


def _scan(request: _Request) -> dict[str, object]:
    if _TRIGGER is None or _SNAPSHOT is None or _INDEX is None:
        raise RuntimeError("query planner worker was not initialized")
    timezone = _TRIGGER.events[0].observed_at.tzinfo
    if timezone is None:
        raise ValueError("query planner timezone is unavailable")
    factors = qmt_factor_frame(_INDEX.factors_for(request.code))
    frame = load_qmt_frame(
        request.code,
        "1m",
        start_at=datetime.combine(request.warmup_start, time(9, 30), tzinfo=timezone),
        end_at=datetime.combine(request.requested_end, time(15, 0), tzinfo=timezone),
        factors=factors,
    )
    if frame.empty:
        return {
            "code": request.code,
            "rows_1m": 0,
            "potential": False,
            "reason": "EMPTY_ONE_MINUTE_FRAME",
        }
    state = strict_state(request.code, "1m", frame)
    state.process_klines(frame)
    evidence = state.get_strict_evidence()
    path = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=request.code,
    )
    current_sector_id = (
        None
        if _CURRENT_SECTOR_BY_CODE is None
        else _CURRENT_SECTOR_BY_CODE.get(request.code)
    )
    windows = (
        _listed_current_sector_windows(
            intervals=(
                ()
                if _CURRENT_SECTOR_INTERVALS is None
                else _CURRENT_SECTOR_INTERVALS.get(current_sector_id, ())
            ),
            security=_INDEX.security(request.code),
        )
        if current_sector_id is not None
        else sector_trigger_windows_for_symbol(
            ledger=_TRIGGER,
            snapshot=_SNAPSHOT,
            code=request.code,
        )
    )
    effective_at = datetime.combine(
        request.effective_start, time(9, 30), tzinfo=timezone
    )
    point_times = {
        value.point_id: value.available_at
        for value in path.strategic_points
        if value.available_at >= effective_at
    }
    eligible_point_ids = []
    for decision in path.decisions:
        observed_at = point_times.get(decision.l0_point_id)
        if observed_at is None or not _inside(observed_at, windows):
            continue
        research = (
            None
            if _RESEARCH is None
            else _RESEARCH.decision_at(request.code, observed_at)
        )
        if _RESEARCH is None or (research is not None and research.accepted):
            eligible_point_ids.append(decision.l0_point_id)
    return {
        "code": request.code,
        "rows_1m": len(frame),
        "source_start": frame["date"].iloc[0].isoformat(),
        "source_end": frame["date"].iloc[-1].isoformat(),
        "structure_level_count": len(evidence.structure.levels),
        "strategic_point_count": len(path.strategic_points),
        "terminal_alignment_pass_count": path.aligned_entry_count,
        "sector_window_count": len(windows),
        "eligible_terminal_point_ids": tuple(sorted(eligible_point_ids)),
        "potential": bool(eligible_point_ids),
        "reason": (
            "TERMINAL_CANDIDATE_REQUIRES_CAUSAL_RESCAN"
            if eligible_point_ids
            else "NO_TERMINAL_SECTOR_ALIGNED_STRATEGIC_POINT"
        ),
    }


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _checkpoint_path(directory: Path, code: str) -> Path:
    identity = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return directory / f"{identity}.json"


def _checkpoint_payload(
    *,
    identity: Mapping[str, object],
    code: str,
    row: Mapping[str, object],
) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": QUERY_ROW_CHECKPOINT_SCHEMA,
        "identity": dict(identity),
        "code": code,
        "row": dict(row),
    }
    stable["content_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return stable


def _load_row_checkpoint(
    path: Path,
    *,
    identity: Mapping[str, object],
    code: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        claimed = document.pop("content_sha256")
        actual = "sha256:" + hashlib.sha256(
            json.dumps(document, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if (
            claimed != actual
            or document.get("schema") != QUERY_ROW_CHECKPOINT_SCHEMA
            or document.get("identity") != dict(identity)
            or document.get("code") != code
            or not isinstance(document.get("row"), dict)
        ):
            return None
        return dict(document["row"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.warmup_start <= args.effective_start <= args.end:
        raise ValueError("invalid query planner dates")
    if args.workers <= 0 or (args.limit is not None and args.limit <= 0):
        raise ValueError("workers and limit must be positive")
    superset_paths = (
        args.conservative_superset_query_plan,
        args.conservative_superset_trigger_ledger,
    )
    if (superset_paths[0] is None) != (superset_paths[1] is None):
        raise ValueError("both conservative superset paths are required")
    if superset_paths[0] is not None and (
        not args.no_three_program
        or args.forward_paper_session is not None
        or args.limit is not None
    ):
        raise ValueError(
            "conservative superset reuse requires the full historical "
            "no-three-program universe"
        )
    trigger_path = args.trigger_ledger.resolve()
    research_path = args.research_ledger.resolve()
    pit_path = args.pit_snapshot.resolve()
    trigger = pickle.loads(trigger_path.read_bytes())
    research = (
        None
        if args.no_three_program
        else pickle.loads(research_path.read_bytes())
    )
    if not isinstance(trigger, SectorFirstTriggerLedger):
        raise ValueError("query planner trigger ledger is invalid")
    if research is not None and not isinstance(research, ResearchApproximationLedger):
        raise ValueError("query planner research ledger is invalid")
    if (
        research is not None
        and research.trigger_ledger_sha256 != _sha256_file(trigger_path)
    ):
        raise ValueError("research proxy is not bound to this trigger ledger")
    hashes = recent_year_research_algorithm_hashes(PROJECT_ROOT)
    revision = recent_year_research_algorithm_revision(hashes)
    planner_source_sha256 = _sha256_file(Path(__file__).resolve())
    if trigger.algorithm_revision != revision:
        raise ValueError("query planner source differs from trigger algorithm")
    catalog_path: Path | None = None
    catalog_entry: Mapping[str, object] | None = None
    if args.no_three_program:
        if trigger.selection_path != RECENT_YEAR_SELECTION_PATH:
            raise ValueError("no-three-program scan requires the current-sector ledger")
        catalog_path = args.current_catalog_ledger.resolve()
        catalog = load_sector_ledger(catalog_path)
        entries = tuple(catalog["entries"])
        if not entries:
            raise ValueError("current QMT sector ledger is empty")
        catalog_entry = entries[-1]
        forward_session = args.forward_paper_session
        if forward_session is not None:
            if (
                args.warmup_start != recent_year_research_parameters().warmup_start
                or args.effective_start != forward_session
                or args.end != forward_session
            ):
                raise ValueError("forward paper query dates changed frozen parameters")
            if datetime.fromisoformat(str(catalog_entry["captured_at"])).date() != forward_session:
                raise ValueError("forward paper query requires a same-session catalog")
            if trigger.selection_order[:2] != (
                "QMT_PIT_SECTOR_TRIGGER",
                "QMT_PIT_MEMBERS_CAPTURED_SAME_SESSION",
            ):
                raise ValueError("forward paper query requires PIT sector expansion")
        by_code = {
            code: str(row["sector_id"])
            for row in catalog_entry["sectors"]
            for code in row["member_codes"]
        }
        trigger_sector_ids = {
            sector_id for sector_id, _revision in trigger.sector_source_revisions
        }
        snapshot = load_snapshot(pit_path)
        index = PITMetadataIndex(snapshot)
        known_codes = {row.code for row in snapshot.securities}
        parameters = recent_year_research_parameters()
        scope_start = forward_session or parameters.requested_start
        scope_end = forward_session or parameters.requested_end
        codes = tuple(
            sorted(
                code
                for code in by_code
                if code in known_codes
                and by_code[code] in trigger_sector_ids
                and index.security(code).intersects(
                    scope_start,
                    scope_end,
                )
            )
        )
    else:
        assert research is not None
        codes = research.ever_accepted_symbols
    if args.limit is not None:
        codes = codes[: args.limit]
    all_requests = tuple(
        _Request(code, args.warmup_start, args.effective_start, args.end)
        for code in codes
    )
    trigger_sha256 = _sha256_file(trigger_path)
    pit_sha256 = _sha256_file(pit_path)
    catalog_sha256 = None if catalog_path is None else _sha256_file(catalog_path)
    checkpoint_identity: dict[str, object] = {
        "algorithm_revision": revision,
        "algorithm_hash_scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
        "planner_source_sha256": planner_source_sha256,
        "trigger_ledger_sha256": trigger_sha256,
        "pit_snapshot_sha256": pit_sha256,
        "current_catalog_ledger_sha256": catalog_sha256,
        "warmup_start": args.warmup_start.isoformat(),
        "effective_start": args.effective_start.isoformat(),
        "end": args.end.isoformat(),
        "three_program_disabled": research is None,
    }
    checkpoint_dir = (
        args.checkpoint_dir.resolve()
        if args.checkpoint_dir is not None
        else args.output.resolve().parent / "terminal_query_rows"
    )
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict[str, object]] = {}
    superset_proof: dict[str, object] | None = None
    superset_pruned_count = 0
    if args.conservative_superset_query_plan is not None:
        if catalog_entry is None or catalog_sha256 is None:
            raise ValueError("conservative superset catalog identity is unavailable")
        rows, rescan_codes, superset_proof = _conservative_superset_negative_rows(
            prior_plan_path=args.conservative_superset_query_plan.resolve(),
            prior_trigger_path=args.conservative_superset_trigger_ledger.resolve(),
            current_trigger=trigger,
            current_codes=codes,
            current_sector_by_code=by_code,
            current_hashes=hashes,
            current_planner_source_sha256=planner_source_sha256,
            catalog_entry_sha256=str(catalog_entry["entry_sha256"]),
            catalog_ledger_sha256=catalog_sha256,
            research_parameter_set_id=recent_year_research_parameters().parameter_set_id,
            warmup_start=args.warmup_start,
            effective_start=args.effective_start,
            end=args.end,
        )
        superset_pruned_count = len(rows)
        rescan_set = set(rescan_codes)
        all_requests = tuple(
            request for request in all_requests if request.code in rescan_set
        )
    failures: dict[str, str] = {}
    requests: list[_Request] = []
    for request in all_requests:
        checkpoint_path = _checkpoint_path(checkpoint_dir, request.code)
        cached = _load_row_checkpoint(
            checkpoint_path,
            identity=checkpoint_identity,
            code=request.code,
        )
        if cached is None and superset_proof is not None:
            for compatible_revision in sorted(
                _COMPATIBLE_CHECKPOINT_PLANNER_REVISIONS
            ):
                cached = _load_row_checkpoint(
                    checkpoint_path,
                    identity={
                        **checkpoint_identity,
                        "planner_source_sha256": compatible_revision,
                    },
                    code=request.code,
                )
                if cached is not None:
                    break
        if cached is None:
            requests.append(request)
        else:
            rows[request.code] = cached
    resumed_count = len(rows) - superset_pruned_count
    workers = max(1, min(args.workers, len(requests))) if requests else 1
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_initialize_worker,
        initargs=(
            str(trigger_path),
            None if research is None else str(research_path),
            str(pit_path),
            str(args.qmt_local_data_dir.resolve()),
            None if catalog_path is None else str(catalog_path),
        ),
    ) as executor:
        jobs = {executor.submit(_scan, request): request.code for request in requests}
        for ordinal, future in enumerate(as_completed(jobs), start=1):
            code = jobs[future]
            try:
                rows[code] = future.result()
                _atomic_json(
                    _checkpoint_path(checkpoint_dir, code),
                    _checkpoint_payload(
                        identity=checkpoint_identity,
                        code=code,
                        row=rows[code],
                    ),
                )
            except Exception as exc:  # preserve every failed universe member
                failures[code] = f"{type(exc).__name__}: {exc}"
            if ordinal % 25 == 0 or ordinal == len(jobs):
                print(
                    f"terminal query plan computed={ordinal}/{len(jobs)} "
                    f"resumed={resumed_count} "
                    f"potential={sum(bool(row['potential']) for row in rows.values())} "
                    f"failed={len(failures)}",
                    flush=True,
                )
    potential = tuple(
        sorted(code for code, row in rows.items() if bool(row["potential"]))
    )
    document: dict[str, object] = {
        "schema": "chanlun-v3-sector-first-terminal-query-plan/v2",
        "algorithm_revision": revision,
        "algorithm_hash_scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
        "algorithm_hashes": tuple(
            {"path": path, "sha256": digest} for path, digest in hashes
        ),
        "producer_source_sha256": planner_source_sha256,
        "trigger_ledger_sha256": trigger_sha256,
        "research_ledger_sha256": (
            None if research is None else _sha256_file(research_path)
        ),
        "research_parameter_set_id": (
            recent_year_research_parameters().parameter_set_id
            if research is None
            else research.parameters.parameter_set_id
        ),
        "forward_paper_session": (
            None
            if args.forward_paper_session is None
            else args.forward_paper_session.isoformat()
        ),
        "observation_range": {
            "warmup_start": args.warmup_start.isoformat(),
            "effective_start": args.effective_start.isoformat(),
            "end": args.end.isoformat(),
        },
        "selection_path": trigger.selection_path,
        "three_program_mode": (
            "DISABLED_USER_AUTHORIZED" if research is None else "RESEARCH_PROXY"
        ),
        "current_catalog_entry_sha256": (
            None if catalog_entry is None else catalog_entry["entry_sha256"]
        ),
        "current_catalog_ledger_sha256": (
            catalog_sha256
        ),
        "requested_symbol_count": len(codes),
        "completed_symbol_count": len(rows),
        "failed_symbol_count": len(failures),
        "resumed_symbol_count": resumed_count,
        "computed_symbol_count": len(rows) - resumed_count - superset_pruned_count,
        "conservative_superset_pruned_symbol_count": superset_pruned_count,
        "conservative_superset_proof": superset_proof,
        "checkpoint_directory": str(checkpoint_dir),
        "potential_symbol_count": len(potential),
        "potential_symbols": potential,
        "potential_symbols_sha256": "sha256:"
        + hashlib.sha256(repr(potential).encode("utf-8")).hexdigest(),
        "rows": tuple(rows[code] for code in sorted(rows)),
        "failures": dict(sorted(failures.items())),
        "authority": "QUERY_PLANNER_ONLY_CAUSAL_RESCAN_REQUIRED",
        "data_grade": "RESEARCH_ONLY",
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    document["content_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if (
        recent_year_research_algorithm_hashes(PROJECT_ROOT) != hashes
        or _sha256_file(Path(__file__).resolve()) != planner_source_sha256
    ):
        raise RuntimeError("source code changed while terminal query plan was built")
    _atomic_json(args.output.resolve(), document)
    if (
        recent_year_research_algorithm_hashes(PROJECT_ROOT) != hashes
        or _sha256_file(Path(__file__).resolve()) != planner_source_sha256
    ):
        raise RuntimeError("source code changed while terminal query plan was published")
    print(json.dumps({key: value for key, value in document.items() if key != "rows"}, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
