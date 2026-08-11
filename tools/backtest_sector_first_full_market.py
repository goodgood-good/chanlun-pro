#!/usr/bin/env python3
"""Causal sector-first direct-recursive strict strategy research replay.

The strict individual-stock route still requires signed three-program
adjudications.  This command runs a separately frozen QMT research proxy so
that the whole sector -> stock -> 30m/5m/1m -> order/fill/account chain can be
measured without pretending that the proxy is live-trading authority.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
from statistics import mean, median, stdev
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.decision_support.trading_system.decision_source_provenance import (  # noqa: E402
    current_replay_decision_source_snapshot,
    replay_decision_source_snapshot_id,
    replay_decision_source_snapshot_matches_current,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    load_qmt_daily_frame,
    load_qmt_frame,
    qmt_factor_frame,
)
from chanlun.decision_support.trading_system.backtest.current_sector import (  # noqa: E402
    CurrentQmtGics3CompositeReplaySource,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    PITMetadataIndex,
    QmtFactorAt,
    load_snapshot,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
)
from chanlun.decision_support.trading_system.models import StructuralPoint  # noqa: E402
from chanlun.decision_support.trading_system.higher_timeframe_gate import (  # noqa: E402
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    higher_timeframe_effectiveness_audit,
    higher_timeframe_gate_evidence_from_envelope,
    resolve_sector_higher_timeframe_gate,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.higher_timeframe_execution_attribution import (  # noqa: E402
    higher_timeframe_execution_attribution,
)
from chanlun.decision_support.trading_system.bar_execution import (  # noqa: E402
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.decision import (  # noqa: E402
    StrategicSignalFacts,
    SystemHealthFacts,
    TacticalSignalFacts,
    DecisionInput,
)
from chanlun.decision_support.trading_system.direct_recursive_structure import (  # noqa: E402
    DirectRecursiveEntryChain,
    direct_recursive_alignment_contract,
)
from chanlun.decision_support.trading_system.execution import (  # noqa: E402
    FeeModel,
    FeeRateAt,
)
from chanlun.decision_support.trading_system.human_review_screening import (  # noqa: E402
    HumanReviewAlert,
    ReviewPriceBar,
    evaluate_review_alert,
    human_review_alert_document,
    human_review_screening_parameters,
    review_priority,
    sector_ranking_review_evidence_from_candidate_audit,
    summarize_event_study,
)
from chanlun.decision_support.trading_system.multisymbol_replay import (  # noqa: E402
    ReplayBatch,
    ReplayCashDistributionFact,
    ReplayDecisionEvent,
    ReplayFactBindings,
    ReplayMandatoryShareActionFact,
    ReplayPriceFact,
    StrictMultiSymbolReplayEngine,
    INDIVIDUAL_REQUIRED_CANDIDATE_GATES,
    SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES,
    research_individual_direct_replay_contract,
    research_sector_technical_approx_replay_contract,
    research_sector_technical_direct_replay_contract,
)
from chanlun.decision_support.trading_system.parameters import (  # noqa: E402
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (  # noqa: E402
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
    build_qmt_higher_timeframe_risk,
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    QMT_SECTOR_LEDGER_SCHEMA,
    load_sector_ledger,
)
from chanlun.decision_support.trading_system.recent_year_research import (  # noqa: E402
    RECENT_YEAR_SELECTION_PATH,
    recent_year_research_parameters,
)
from chanlun.decision_support.trading_system.recent_year_provenance import (  # noqa: E402
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (  # noqa: E402
    build_qmt_same_base_stream_frames,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (  # noqa: E402
    QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
    QmtNativeDailyReconciliationError,
    build_qmt_native_daily_bridge,
)
from chanlun.decision_support.trading_system.research_approximation import (  # noqa: E402
    ResearchApproximationLedger,
)
from chanlun.decision_support.trading_system.sector_first_direct_facts import (  # noqa: E402
    SectorFirstDirectSymbolFacts,
)
from chanlun.decision_support.trading_system.sector_first_trigger_plan import (  # noqa: E402
    SectorFirstTriggerEvent,
    SectorFirstTriggerLedger,
)
from chanlun.decision_support.trading_system.selection import (  # noqa: E402
    CandidateDecision,
    GateCheck,
)
from chanlun.decision_support.trading_system.technical_approximation import (  # noqa: E402
    ApproximateChanlunEntryChain,
    TechnicalApproximationParameters,
    approximate_technical_entry_decision,
    bind_approximate_entry_chain,
    technical_approximation_alignment_contract,
    technical_approximation_parameters,
)

# Bind modules that supply objects directly used by this replay.  A delta of
# ``sys.modules`` is process-order dependent under pytest/web workers: package
# initializers can pull database and exchange adapters while
# this module is importing even though no replay decision references them.
# Those side effects have a separate full-integration provenance contract and
# must not contaminate the historical replay cohort.
_REPLAY_IMPORT_MODULE_NAMES = frozenset(
    {
        __name__,
        "chanlun.decision_support.trading_system.backtest.qmt_local_cache",
    }
    | {
        str(getattr(value, "__module__"))
        for value in tuple(globals().values())
        if isinstance(getattr(value, "__module__", None), str)
        and str(getattr(value, "__module__")).startswith("chanlun.")
    }
)


CN = ZoneInfo("Asia/Shanghai")
DEFAULT_ROOT = Path("audit/chanlun_trading_system_backtest/sector_first_full_market")
DEFAULT_RECENT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/recent_year_current_sector_no3p"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
INITIAL_CASH = Decimal("1000000")
_ZERO = Decimal("0")
FEE_SCHEDULE_ID = "A_SHARE_RESEARCH_2025"
BROKER_LATENCY = timedelta(seconds=2)
PRICE_TICK = Decimal("0.01")
LOT = 100


@dataclass(slots=True)
class SymbolContext:
    facts: SectorFirstDirectSymbolFacts
    frame: pd.DataFrame
    daily_frame: pd.DataFrame
    times: tuple[datetime, ...]
    executable_rows: pd.DataFrame
    executable_times: tuple[datetime, ...]
    sessions: tuple[date, ...]
    session_close: dict[date, Decimal]
    session_volume: dict[date, Decimal]


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    kind: str
    observed_at: datetime
    identity: str
    point: StructuralPoint | None = None
    chain: DirectRecursiveEntryChain | ApproximateChanlunEntryChain | None = None
    structure_snapshot_id: str | None = None
    selection: CandidateDecision | None = None
    selection_fact_ids: tuple[str, ...] = ()
    risk_fact_ids: tuple[str, ...] = ()
    frozen_fact_ids: tuple[str, ...] = ()
    q_plan: int = 0
    boundary: Decimal | None = None
    exact_risk_green: bool = False


def _symbol_daily_history_start(one_minute_start: datetime) -> datetime:
    """Return the independent native-daily warmup boundary for a symbol."""

    if one_minute_start.tzinfo is None:
        raise ValueError("symbol one-minute source start must be timezone-aware")
    return min(
        datetime.combine(one_minute_start.date(), time(0), tzinfo=CN),
        datetime.combine(
            recent_year_research_parameters().warmup_start,
            time(0),
            tzinfo=CN,
        ),
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument("--qmt-local-data-dir", type=Path, required=True)
    value.add_argument(
        "--no-three-program",
        action="store_true",
        help="run the frozen current-QMT-sector technical-only research variant",
    )
    value.add_argument(
        "--approximate-technical-points",
        action="store_true",
        help=(
            "use the separately frozen causal 30m/5m/1m point approximation "
            "instead of requiring an exact recursive parent/child proof"
        ),
    )
    value.add_argument(
        "--human-review-only",
        action="store_true",
        help=(
            "emit a ranked human review queue and 5/10/20-session event study; "
            "never build replay batches, orders, fills, positions or portfolio P&L"
        ),
    )
    value.add_argument(
        "--current-catalog-ledger",
        type=Path,
        default=Path(
            ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
        ),
    )
    value.add_argument("--initial-cash", type=Decimal, default=INITIAL_CASH)
    value.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ROOT / "full_market_research_backtest.json",
    )
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _decision_source_snapshot(
    project_root: Path = PROJECT_ROOT,
) -> dict[str, object]:
    """Identity of code that can alter this historical replay."""

    return current_replay_decision_source_snapshot(project_root)


def _decision_source_snapshot_matches_current(
    value: object,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    """Return whether result metadata matches the current source identity."""

    return replay_decision_source_snapshot_matches_current(value, project_root)


def _loaded_replay_source_paths(
    project_root: Path = PROJECT_ROOT,
    module_names: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return project Python sources actually loaded by this replay process."""

    root = project_root.resolve()
    selected = _REPLAY_IMPORT_MODULE_NAMES if module_names is None else module_names
    paths: set[str] = set()
    for name, module in tuple(sys.modules.items()):
        if name not in selected:
            continue
        raw = getattr(module, "__file__", None)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            relative = Path(raw).resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if relative.endswith(".py") and (
            relative.startswith("src/chanlun/")
            or relative == "tools/backtest_sector_first_full_market.py"
        ):
            paths.add(relative)
    return tuple(sorted(paths))


def _require_loaded_replay_sources_are_bound(
    snapshot: Mapping[str, object],
    module_names: frozenset[str] | None = None,
) -> None:
    """Fail closed if a lazy project dependency escaped the source manifest."""

    replay_decision_source_snapshot_id(snapshot)
    raw_files = snapshot["files"]
    if not isinstance(raw_files, (tuple, list)):
        raise RuntimeError("replay source snapshot files are malformed")
    bound = {
        str(row["path"])
        for row in raw_files
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    missing = tuple(
        path
        for path in _loaded_replay_source_paths(module_names=module_names)
        if path not in bound
    )
    if missing:
        raise RuntimeError(
            "loaded replay source is absent from provenance: " + ", ".join(missing)
        )


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    return value


def _fingerprint_value(value: object) -> object:
    """Make date-only facts canonical without weakening typed fingerprints.

    ``sha256_json`` deliberately distinguishes Decimal and datetime values.
    The sector-strength trigger added a date-only anchor, which that canonical
    encoder does not accept.  Convert only bare ``date`` values to ISO text;
    preserve Decimal and datetime identities exactly as before.
    """

    if is_dataclass(value):
        return _fingerprint_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _fingerprint_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_fingerprint_value(item) for item in value)
    if isinstance(value, list):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return value


def _atomic_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    expected_decision_source_snapshot: Mapping[str, object] | None = None,
    loaded_replay_module_names: frozenset[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if expected_decision_source_snapshot is not None:
        _require_loaded_replay_sources_are_bound(
            expected_decision_source_snapshot,
            loaded_replay_module_names,
        )
    temporary.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    if (
        expected_decision_source_snapshot is not None
        and not _decision_source_snapshot_matches_current(
            expected_decision_source_snapshot
        )
    ):
        temporary.unlink(missing_ok=True)
        current_snapshot = _decision_source_snapshot()
        expected_files = {
            str(row["path"]): str(row["sha256"])
            for row in expected_decision_source_snapshot.get("files", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str)
        }
        current_files = {
            str(row["path"]): str(row["sha256"])
            for row in current_snapshot.get("files", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str)
        }
        changed_paths = tuple(
            sorted(
                path
                for path in set(expected_files) | set(current_files)
                if expected_files.get(path) != current_files.get(path)
            )
        )
        raise RuntimeError(
            "decision source changed during replay; result was not published; "
            f"start={expected_decision_source_snapshot.get('aggregate_sha256')}; "
            f"current={current_snapshot.get('aggregate_sha256')}; "
            f"changed={','.join(changed_paths) or 'UNRESOLVED'}"
        )
    os.replace(temporary, path)


def _load_pickle(path: Path, expected: type):
    value = pickle.loads(path.read_bytes())
    if not isinstance(value, expected):
        raise ValueError(f"invalid checkpoint type: {path}")
    return value


def _load_bound_direct_pickle(
    path: Path,
    expected: type,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
):
    """Verify an extracted symbol checkpoint before unpickling it."""

    if (
        not isinstance(expected_sha256, str)
        or not expected_sha256.startswith("sha256:")
        or len(expected_sha256) != 71
        or not isinstance(expected_size_bytes, int)
        or isinstance(expected_size_bytes, bool)
        or expected_size_bytes <= 0
    ):
        raise ValueError("direct checkpoint binding is invalid")
    payload = path.read_bytes()
    if len(payload) != expected_size_bytes:
        raise ValueError(f"direct checkpoint size changed: {path}")
    actual_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f"direct checkpoint SHA256 changed: {path}")
    value = pickle.loads(payload)
    if not isinstance(value, expected):
        raise ValueError(f"invalid checkpoint type: {path}")
    return value


def _direct_checkpoint_binding(
    manifest: Mapping[str, object],
    code: str,
) -> tuple[str, str, int]:
    if manifest.get("schema") != "chanlun-sector-first-direct-extract":
        raise RuntimeError("direct extraction manifest lacks checkpoint bindings")
    symbols = manifest.get("symbols")
    if not isinstance(symbols, Mapping):
        raise RuntimeError("direct extraction symbol manifest is invalid")
    row = symbols.get(code)
    if not isinstance(row, Mapping) or row.get("code") != code:
        raise RuntimeError(f"direct checkpoint manifest row is invalid: {code}")
    expected_path = f"direct_symbols/{code.replace('.', '_')}.pkl"
    path = row.get("checkpoint_path")
    sha256 = row.get("checkpoint_sha256")
    size = row.get("checkpoint_size_bytes")
    if path != expected_path:
        raise RuntimeError(f"direct checkpoint path changed: {code}")
    if (
        not isinstance(sha256, str)
        or not sha256.startswith("sha256:")
        or len(sha256) != 71
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        raise RuntimeError(f"direct checkpoint binding is invalid: {code}")
    return path, sha256, size


def _require_current_direct_algorithm(
    manifest: Mapping[str, object],
    trigger: SectorFirstTriggerLedger,
) -> None:
    """Reject mutually consistent checkpoints produced by old source code."""

    expected_hashes = recent_year_research_algorithm_hashes(PROJECT_ROOT)
    expected_revision = recent_year_research_algorithm_revision(expected_hashes)
    expected_rows = tuple(
        {"path": path, "sha256": digest}
        for path, digest in expected_hashes
    )
    algorithm = manifest.get("algorithm")
    if (
        not isinstance(algorithm, Mapping)
        or algorithm.get("scope") != RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE
        or algorithm.get("revision") != expected_revision
        or tuple(algorithm.get("hashes", ())) != expected_rows
        or trigger.algorithm_revision != expected_revision
    ):
        raise RuntimeError(
            "direct extraction algorithm differs from current decision source"
        )


def _load_context(facts: SectorFirstDirectSymbolFacts) -> SymbolContext:
    start_at = facts.source_start
    end_at = facts.source_end
    if start_at is None or end_at is None:
        raise ValueError(f"direct fact has no source range: {facts.code}")
    factors = qmt_factor_frame(facts.factors)
    frame = load_qmt_frame(
        facts.code,
        "1m",
        start_at=start_at,
        end_at=end_at,
        factors=factors,
    )
    if frame.empty:
        raise ValueError(f"QMT 1m frame disappeared: {facts.code}")
    daily_frame = load_qmt_daily_frame(
        facts.code,
        # The installed 1m cache is intentionally limited to the recent
        # research interval, while the frozen M/W/D gate needs its independent
        # 480-daily-bar warmup.  Never let the finite 1m left boundary silently
        # truncate the native-daily prefix.
        start_at=_symbol_daily_history_start(start_at),
        end_at=end_at,
        factors=factors,
    )
    if daily_frame.empty:
        raise ValueError(f"QMT native daily frame disappeared: {facts.code}")
    times = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    executable = frame[
        frame["date"].map(lambda value: pd.Timestamp(value).time() != time(9, 30))
    ].copy()
    executable_times = tuple(
        pd.Timestamp(value).to_pydatetime() for value in executable["date"]
    )
    sessions = tuple(sorted({value.date() for value in executable_times}))
    session_close: dict[date, Decimal] = {}
    session_volume: dict[date, Decimal] = {}
    for session, rows in executable.groupby(executable["date"].dt.date, sort=True):
        session_close[session] = Decimal(str(rows.iloc[-1]["raw_close"]))
        session_volume[session] = sum(
            (Decimal(str(value)) for value in rows["volume"]), Decimal("0")
        )
    return SymbolContext(
        facts=facts,
        frame=frame,
        daily_frame=daily_frame,
        times=times,
        executable_rows=executable.reset_index(drop=True),
        executable_times=executable_times,
        sessions=sessions,
        session_close=session_close,
        session_volume=session_volume,
    )


def _row_at_or_before(context: SymbolContext, observed_at: datetime):
    position = bisect_right(context.executable_times, observed_at) - 1
    if position < 0:
        return None
    return context.executable_rows.iloc[position]


def _raw_price_at(
    context: SymbolContext,
    observed_at: datetime,
    field: str,
) -> Decimal | None:
    row = _row_at_or_before(context, observed_at)
    return None if row is None else Decimal(str(row[field]))


def _execution_bars(
    context: SymbolContext,
    *,
    confirmed_at: datetime,
    valuation_at: datetime,
    optional: bool,
) -> tuple[HistoricalMinuteExecutionBar, ...]:
    rows = context.executable_rows[
        (context.executable_rows["date"] <= pd.Timestamp(valuation_at))
        & (
            context.executable_rows["date"]
            > pd.Timestamp(confirmed_at - timedelta(minutes=1))
        )
    ]
    output: list[HistoricalMinuteExecutionBar] = []
    for row in rows.itertuples(index=True):
        closed = pd.Timestamp(row.date).to_pydatetime()
        opened = closed - timedelta(minutes=1)
        if opened < confirmed_at:
            continue
        output.append(
            HistoricalMinuteExecutionBar(
                symbol=context.facts.code,
                opened_at=opened,
                closed_at=closed,
                sequence=int(row.Index),
                raw_open=Decimal(str(row.raw_open)),
                raw_high=Decimal(str(row.raw_high)),
                raw_low=Decimal(str(row.raw_low)),
                raw_close=Decimal(str(row.raw_close)),
                raw_volume=Decimal(str(row.volume)),
                source_id=(
                    f"{context.facts.source_revision}:1m:{closed.isoformat()}"
                ),
            )
        )
        if optional:
            break
    return tuple(output)


def _mark(context: SymbolContext, observed_at: datetime) -> ReplayPriceFact | None:
    row = _row_at_or_before(context, observed_at)
    if row is None:
        return None
    source_available = pd.Timestamp(row["date"]).to_pydatetime()
    # A suspended symbol is valued by the last exchange-observed raw close.
    # Stamp the valuation observation time (not a new trade price) so the
    # account curve remains complete while the source id preserves the carry.
    available = (
        observed_at
        if source_available < observed_at - timedelta(minutes=31)
        else source_available
    )
    return ReplayPriceFact(
        symbol=context.facts.code,
        available_at=available,
        raw_close=Decimal(str(row["raw_close"])),
        source_id=(
            f"{context.facts.source_revision}:mark:{source_available.isoformat()}"
            if available == source_available
            else (
                f"{context.facts.source_revision}:last-observation-carried-forward:"
                f"{source_available.isoformat()}:valued:{available.isoformat()}"
            )
        ),
    )


def _factor_on(context: SymbolContext, session: date) -> QmtFactorAt | None:
    matches = tuple(row for row in context.facts.factors if row.effective_on == session)
    if len(matches) > 1:
        raise ValueError("multiple QMT factor rows on one session")
    return None if not matches else matches[0]


def _board_limit_percent(context: SymbolContext) -> Decimal:
    code = context.facts.code
    name = context.facts.security_master.name.upper()
    if "ST" in name:
        return Decimal("0.05")
    if code.startswith("BJ."):
        return Decimal("0.30")
    if code.startswith("SH.688") or code.startswith(("SZ.300", "SZ.301")):
        return Decimal("0.20")
    return Decimal("0.10")


def _daily_limits(
    context: SymbolContext,
    session: date,
) -> tuple[Decimal, Decimal] | None:
    sessions = context.sessions
    position = bisect_left(sessions, session)
    if position <= 0:
        return None
    reference = context.session_close[sessions[position - 1]]
    factor = _factor_on(context, session)
    if factor is not None:
        reference /= factor.raw_price_divisor
    pct = _board_limit_percent(context)
    upper = (reference * (Decimal("1") + pct)).quantize(
        PRICE_TICK, rounding=ROUND_HALF_UP
    )
    lower = (reference * (Decimal("1") - pct)).quantize(
        PRICE_TICK, rounding=ROUND_HALF_UP
    )
    return upper, lower


def _status(
    context: SymbolContext,
    *,
    decision_at: datetime,
) -> BarProxyExecutionStatus:
    session = decision_at.date()
    limits = _daily_limits(context, session)
    visible = context.executable_rows[
        (context.executable_rows["date"].dt.date == session)
        & (context.executable_rows["date"] <= pd.Timestamp(decision_at))
    ]
    suspended = visible.empty or Decimal(str(visible["volume"].sum())) <= 0
    listed = context.facts.security_master.listed_on(session)
    complete = limits is not None and not visible.empty
    upper, lower = limits or (Decimal("999999"), Decimal("0.01"))
    return BarProxyExecutionStatus(
        known_at=decision_at,
        effective_session=session,
        listed=listed,
        suspended=suspended,
        continuity_active=listed and not suspended,
        point_in_time_state_complete=complete,
        corporate_action_state_complete=True,
        sellable_quantity=0,
        limit_up=upper,
        limit_down=lower,
        buy_quantity_increment=LOT,
        sell_quantity_increment=LOT,
        fee_schedule_id=FEE_SCHEDULE_ID,
    )


def _liquidity_cap(context: SymbolContext, observed_at: datetime) -> tuple[int, dict[str, object]]:
    prior_sessions = tuple(
        session for session in context.sessions if session < observed_at.date()
    )[-20:]
    daily = tuple(context.session_volume[session] for session in prior_sessions)
    clock = observed_at.time()
    same_clock: list[Decimal] = []
    for session in prior_sessions:
        matches = context.executable_rows[
            (context.executable_rows["date"].dt.date == session)
            & (context.executable_rows["date"].dt.time == clock)
        ]
        if len(matches) == 1:
            same_clock.append(Decimal(str(matches.iloc[0]["volume"])))
    cap = Decimal("0")
    if len(daily) == 20 and len(same_clock) == 20:
        cap = min(median(daily) * Decimal("0.01"), median(same_clock) * Decimal("0.05"))
    quantity = int((cap / Decimal(LOT)).to_integral_value(rounding=ROUND_DOWN)) * LOT
    return quantity, {
        "completed_daily_sessions": len(daily),
        "completed_same_clock_sessions": len(same_clock),
        "median_daily_volume": None if not daily else median(daily),
        "median_same_clock_volume": None if not same_clock else median(same_clock),
        "q_liquidity_cap": quantity,
    }


def _latest_trigger(
    ledger: SectorFirstTriggerLedger,
    observed_at: datetime,
) -> SectorFirstTriggerEvent | None:
    times = tuple(value.observed_at for value in ledger.events)
    position = bisect_right(times, observed_at) - 1
    return None if position < 0 else ledger.events[position]


def _membership_at(context: SymbolContext, observed_at: datetime):
    rows = tuple(row for row in context.facts.memberships if row.known_at <= observed_at)
    return None if not rows else rows[-1]


def _graph(context: SymbolContext) -> dict[str, tuple[str, ...]]:
    output: dict[str, tuple[str, ...]] = {}
    for unit in context.facts.completed_units:
        output[unit.unit_id] = tuple(unit.child_ids)
    for trend in context.facts.completed_trends:
        output[trend.trend_id] = tuple(
            unit.unit_id for unit in trend.constituent_units
        )
    return output


def _descendants(root: str, graph: Mapping[str, tuple[str, ...]]) -> frozenset[str]:
    seen: set[str] = set()
    pending = [root]
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        pending.extend(graph.get(identity, ()))
    return frozenset(seen)


def _exact_locator(
    context: SymbolContext,
    parent: StructuralPoint,
    *,
    side: str,
) -> StructuralPoint | None:
    anchor_map = dict(context.facts.point_anchor_unit_ids)
    parent_anchor = anchor_map.get(parent.point_id)
    if parent_anchor is None:
        return None
    descendants = _descendants(parent_anchor, _graph(context))
    point_types = {"1buy", "2buy"} if side == "buy" else {"1sell", "2sell"}
    eligible = tuple(
        point
        for point in context.facts.structural_points
        if point.recursive_level == 0
        and point.point_type in point_types
        and anchor_map.get(point.point_id) in descendants
        and point.available_at <= parent.available_at
    )
    return None if not eligible else max(eligible, key=lambda row: (row.available_at, row.point_id))


def _risk_gate(
    *,
    symbol: str,
    frame: pd.DataFrame,
    native_daily_frame: pd.DataFrame,
    observed_at: datetime,
    expected_sessions: Sequence[date],
    evaluation_not_before: date | None = None,
) -> tuple[
    str,
    str,
    tuple[str, ...],
    dict[str, object] | None,
    dict[str, object] | None,
]:
    try:
        visible_sessions = tuple(
            value for value in expected_sessions if value <= observed_at.date()
        )
        if not visible_sessions:
            raise ValueError("risk trading-calendar prefix is empty")
        native_dates = pd.to_datetime(native_daily_frame["date"], errors="raise")
        native_daily = native_daily_frame.loc[
            (native_dates.dt.date >= visible_sessions[0])
            & (native_dates.dt.date <= visible_sessions[-1])
        ].copy()
        native_daily.attrs = dict(native_daily_frame.attrs)
        same = build_qmt_same_base_stream_frames(
            symbol=symbol,
            one_minute_frame=frame,
            decision_time=observed_at,
            expected_sessions=expected_sessions,
            evaluation_not_before=evaluation_not_before,
        )
        bridge = build_qmt_native_daily_bridge(
            symbol=symbol,
            native_daily_frame=native_daily,
            same_base=same,
            decision_time=observed_at,
            trading_sessions=visible_sessions,
            max_price_difference_quanta=1,
        )
        inputs = qmt_higher_timeframe_inputs(
            symbol=symbol,
            daily_frame=bridge.daily,
            thirty_minute_frame=bridge.thirty_minute,
            decision_time=observed_at,
            required_base_frequency=QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
            native_daily_reconciliation_evidence=bridge.evidence,
            native_daily_calendar_coverage_evidence=(
                bridge.calendar_coverage_evidence
            ),
        )
        envelope = build_qmt_higher_timeframe_risk(
            inputs=inputs,
            trading_sessions=visible_sessions,
            calendar_coverage_end=visible_sessions[-1],
            snapshot_id=sha256_json(
                {
                    "schema": "full-market-risk",
                    "symbol": symbol,
                    "observed_at": observed_at,
                    "base": bridge.evidence.reconciled_source_revision,
                    "source_boundary_exclusions": tuple(
                        value.document()
                        for value in getattr(
                            same, "source_boundary_exclusions", ()
                        )
                    ),
                }
            ),
        )
        evidence = higher_timeframe_gate_evidence_from_envelope(envelope)
        return evidence.gate, evidence.snapshot_id, evidence.reason_codes, {
            "source_revision": evidence.source_revision,
            "monthly": evidence.monthly,
            "weekly": evidence.weekly,
            "daily": evidence.daily,
            "grade": evidence.grade,
            "period_diagnostics": tuple(
                value.document() for value in evidence.period_diagnostics
            ),
            "session_evidence": (
                None
                if evidence.session_evidence is None
                else evidence.session_evidence.document()
            ),
            "warmup": (
                None
                if evidence.warmup_evidence is None
                else evidence.warmup_evidence.document()
            ),
            "warmup_convergence": (
                None
                if evidence.warmup_convergence_evidence is None
                else evidence.warmup_convergence_evidence.document()
            ),
            "warmup_convergence_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.diagnostic is None
                else evidence.warmup_convergence_evidence.diagnostic.document()
            ),
            "warmup_mapping_supply_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.mapping_supply_diagnostic
                is None
                else evidence.warmup_convergence_evidence.mapping_supply_diagnostic.document()
            ),
            "warmup_structure_lineage_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.structure_lineage_diagnostic
                is None
                else evidence.warmup_convergence_evidence.structure_lineage_diagnostic.document()
            ),
            "native_daily_reconciliation": bridge.evidence.document(),
            "one_minute_source_alignment": {
                "evaluation_not_before": (
                    None
                    if evaluation_not_before is None
                    else evaluation_not_before.isoformat()
                ),
                "source_boundary_exclusions": tuple(
                    value.document()
                    for value in getattr(same, "source_boundary_exclusions", ())
                ),
            },
        }, bridge.calendar_coverage_evidence.document()
    except QmtNativeDailyReconciliationError as exc:
        coverage = exc.calendar_coverage_evidence
        source = sha256_json(
            {
                "schema": "native-daily-risk-error",
                "symbol": symbol,
                "at": observed_at,
                "reason_code": exc.code,
                "calendar_coverage_evidence": (
                    None if coverage is None else coverage.document()
                ),
            }
        )
        return (
            "UNRESOLVED",
            source,
            (exc.code,),
            None,
            None if coverage is None else coverage.document(),
        )
    except Exception as exc:
        return "UNRESOLVED", sha256_json(
            {"schema": "risk-error", "symbol": symbol, "at": observed_at}
        ), (f"{type(exc).__name__}:{exc}",), None, None


def _sector_risk_gate(
    *,
    sector_id: str | None,
    sector_members: tuple[str, ...] | None,
    observed_at: datetime,
    expected_sessions: Sequence[date],
    composite_source: CurrentQmtGics3CompositeReplaySource | None,
) -> tuple[str, str, tuple[str, ...], dict[str, object] | None]:
    """Use the page's exact 5m -> D/30m -> M/W/D sector risk core."""

    unresolved_identity = sha256_json(
        {
            "schema": "sector-risk-error",
            "sector_id": sector_id,
            "at": observed_at,
        }
    )
    if sector_id is None or not sector_members or composite_source is None:
        return (
            "UNRESOLVED",
            unresolved_identity,
            ("QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE",),
            None,
        )
    try:
        visible_sessions = tuple(
            value for value in expected_sessions if value <= observed_at.date()
        )
        if not visible_sessions:
            raise ValueError("sector risk trading-calendar prefix is empty")
        five_minute = composite_source.five_minute_prefix(
            sector_id=sector_id,
            member_codes=sector_members,
            observed_at=observed_at,
        )
        resolution = resolve_sector_higher_timeframe_gate(
            sector_id=sector_id,
            sector_members=sector_members,
            five_minute_frame=five_minute,
            observed_at=observed_at,
            trading_sessions=visible_sessions,
            calendar_coverage_end=visible_sessions[-1],
            native_daily_loader=lambda: composite_source.native_daily_prefix(
                sector_id=sector_id,
                member_codes=sector_members,
                observed_at=observed_at,
            ),
        )
        evidence = resolution.evidence
        source_mode = resolution.source_mode
        return evidence.gate, evidence.snapshot_id, evidence.reason_codes, {
            "source_revision": evidence.source_revision,
            "monthly": evidence.monthly,
            "weekly": evidence.weekly,
            "daily": evidence.daily,
            "grade": evidence.grade,
            "period_diagnostics": tuple(
                value.document() for value in evidence.period_diagnostics
            ),
            "session_evidence": evidence.session_evidence.document(),
            "warmup": (
                None
                if evidence.warmup_evidence is None
                else evidence.warmup_evidence.document()
            ),
            "warmup_convergence": (
                None
                if evidence.warmup_convergence_evidence is None
                else evidence.warmup_convergence_evidence.document()
            ),
            "warmup_convergence_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.diagnostic is None
                else evidence.warmup_convergence_evidence.diagnostic.document()
            ),
            "warmup_mapping_supply_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.mapping_supply_diagnostic
                is None
                else evidence.warmup_convergence_evidence.mapping_supply_diagnostic.document()
            ),
            "warmup_structure_lineage_diagnostic": (
                None
                if evidence.warmup_convergence_evidence is None
                or evidence.warmup_convergence_evidence.structure_lineage_diagnostic
                is None
                else evidence.warmup_convergence_evidence.structure_lineage_diagnostic.document()
            ),
            "membership_mode": (
                "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
            ),
            "source_mode": source_mode,
            "research_bridge_parameter_set_id": (
                sector_native_daily_research_bridge_contract()[
                    "parameter_set_id"
                ]
                if source_mode
                == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
                else None
            ),
            "strict_same_5m_warmup": (
                None
                if resolution.strict_warmup_evidence is None
                else resolution.strict_warmup_evidence.document()
            ),
            "strict_same_5m_warmup_convergence": (
                None
                if resolution.strict_warmup_convergence_evidence is None
                else resolution.strict_warmup_convergence_evidence.document()
            ),
            "strict_same_5m_warmup_convergence_diagnostic": (
                None
                if resolution.strict_warmup_convergence_evidence is None
                or resolution.strict_warmup_convergence_evidence.diagnostic
                is None
                else resolution.strict_warmup_convergence_evidence.diagnostic.document()
            ),
            "strict_same_5m_warmup_mapping_supply_diagnostic": (
                None
                if resolution.strict_warmup_convergence_evidence is None
                or resolution.strict_warmup_convergence_evidence.mapping_supply_diagnostic
                is None
                else resolution.strict_warmup_convergence_evidence.mapping_supply_diagnostic.document()
            ),
            "strict_same_5m_warmup_structure_lineage_diagnostic": (
                None
                if resolution.strict_warmup_convergence_evidence is None
                or resolution.strict_warmup_convergence_evidence.structure_lineage_diagnostic
                is None
                else resolution.strict_warmup_convergence_evidence.structure_lineage_diagnostic.document()
            ),
            "strict_same_5m_source_coverage": (
                None
                if resolution.strict_source_coverage_evidence is None
                else resolution.strict_source_coverage_evidence.document()
            ),
            "fallback_unavailable_reason_codes": (
                resolution.fallback_unavailable_reason_codes
            ),
        }
    except Exception as exc:
        return (
            "UNRESOLVED",
            unresolved_identity,
            (f"{type(exc).__name__}:{exc}",),
            None,
        )


def _reconciled_market_calendar(
    primary_daily: pd.DataFrame,
    secondary_daily: pd.DataFrame,
) -> tuple[tuple[date, ...], dict[str, object]]:
    """Cross-check two liquid QMT indices before using sessions as research calendar."""

    def sessions(frame: pd.DataFrame, label: str) -> tuple[date, ...]:
        if frame.empty or "date" not in frame:
            raise ValueError(f"{label} native daily calendar source is empty")
        values = pd.to_datetime(frame["date"], errors="raise")
        if values.dt.tz is None or any(value.time() != time(15) for value in values):
            raise ValueError(f"{label} native daily completion time is invalid")
        result = tuple(value.date() for value in values)
        if result != tuple(sorted(set(result))):
            raise ValueError(f"{label} native daily calendar is not chronological")
        return result

    primary = sessions(primary_daily, "primary")
    secondary = sessions(secondary_daily, "secondary")
    first = max(primary[0], secondary[0])
    last = min(primary[-1], secondary[-1])
    left = tuple(value for value in primary if first <= value <= last)
    right = tuple(value for value in secondary if first <= value <= last)
    if left != right:
        raise ValueError("QMT index daily calendars do not reconcile")
    if len(left) < QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS:
        raise ValueError(
            "reconciled QMT index calendar is shorter than the frozen M/W/D warmup"
        )
    revision = sha256_json(
        {
            "schema": "chanlun-qmt-two-index-realized-calendar",
            "primary_symbol": "SH.000001",
            "secondary_symbol": "SH.000300",
            "primary_source": primary_daily.attrs.get(
                "qmt_local_cache_source_sha256"
            ),
            "secondary_source": secondary_daily.attrs.get(
                "qmt_local_cache_source_sha256"
            ),
            "sessions": tuple(value.isoformat() for value in left),
        }
    )
    return left, {
        "contract_id": "chanlun-qmt-two-index-realized-calendar",
        "source_symbols": ("SH.000001", "SH.000300"),
        "session_count": len(left),
        "first_session": left[0].isoformat(),
        "last_session": left[-1].isoformat(),
        "source_revision": revision,
        "point_in_time_publication_proven": False,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


def _market_history_start(
    contexts: Mapping[str, SymbolContext],
) -> datetime:
    """Keep the frozen M/W/D warmup even when local 1m starts later.

    Symbol ``source_start`` describes the installed 1m cache.  Using it as the
    daily-calendar start silently discarded the older native-daily prefix and
    made the 480-day monthly/weekly/daily convergence gate impossible.  The
    native-daily bridge is specifically designed to certify that older prefix,
    so market history must always begin at the frozen research warmup.
    """

    frozen = datetime.combine(
        recent_year_research_parameters().warmup_start,
        time(9, 30),
        tzinfo=CN,
    )
    available = tuple(
        value.facts.source_start
        for value in contexts.values()
        if value.facts.source_start is not None
    )
    return min((frozen, *available))


def _check(gate: str, passed: bool, code: str, detail: object) -> GateCheck:
    return GateCheck(gate, passed, code, json.dumps(_jsonable(detail), ensure_ascii=False))


def _research_risk_disposition(
    *,
    market_gate: str,
    sector_gate: str,
    symbol_gate: str,
    triggered: bool,
    sector_hard_blocked: bool,
) -> tuple[bool, bool, bool, bool]:
    """Return market/sector/symbol proxy passes plus exact-GREEN status."""

    allowed = {"GREEN", "AMBER"}
    market_pass = market_gate in allowed
    sector_pass = sector_gate in allowed and not sector_hard_blocked
    symbol_pass = symbol_gate in allowed
    exact_green = (
        market_gate == sector_gate == symbol_gate == "GREEN"
        and triggered
        and not sector_hard_blocked
    )
    return market_pass, sector_pass, symbol_pass, exact_green


def _candidate(
    *,
    context: SymbolContext,
    signal_at: datetime,
    decision_at: datetime,
    chain: DirectRecursiveEntryChain | ApproximateChanlunEntryChain,
    trigger: SectorFirstTriggerLedger,
    research: ResearchApproximationLedger | None,
    current_sector_by_code: Mapping[str, str],
    current_sector_members_by_id: Mapping[str, tuple[str, ...]],
    current_catalog_entry_sha256: str | None,
    sector_composite_source: CurrentQmtGics3CompositeReplaySource | None,
    market_frame: pd.DataFrame,
    market_daily_frame: pd.DataFrame,
    market_sessions: Sequence[date],
    initial_cash: Decimal,
    risk_cache: dict[
        tuple[str, datetime],
        tuple[
            str,
            str,
            tuple[str, ...],
            dict[str, object] | None,
            dict[str, object] | None,
        ],
    ],
    sector_risk_cache: dict[
        tuple[str, datetime],
        tuple[str, str, tuple[str, ...], dict[str, object] | None],
    ],
) -> tuple[CandidateDecision, tuple[str, ...], tuple[str, ...], int, Decimal, bool, dict[str, object]]:
    params = individual_parameter_snapshot()
    current_mode = research is None
    research_row = (
        None
        if research is None
        else research.decision_at(context.facts.code, decision_at)
    )
    membership = _membership_at(context, decision_at)
    trigger_event = _latest_trigger(trigger, decision_at)
    ranked = () if trigger_event is None else trigger_event.ranked_sectors
    ranked_by_sector = {row.sector_id: row for row in ranked}
    ranked_ids = set(ranked_by_sector)
    sector_id = (
        current_sector_by_code.get(context.facts.code)
        if current_mode
        else (None if membership is None else membership.sector_id)
    )
    triggered = sector_id is not None and sector_id in ranked_ids
    hard_blocked = (
        trigger_event is not None
        and sector_id in set(trigger_event.hard_blocked_sector_ids)
    )
    liquidity_cap, liquidity = _liquidity_cap(context, decision_at)
    status = _status(context, decision_at=decision_at)
    latest = _row_at_or_before(context, decision_at)
    fresh_quote = (
        latest is not None
        and pd.Timestamp(latest["date"]).date() == decision_at.date()
        and pd.Timestamp(latest["date"]).time() != time(9, 30)
    )
    market_key = ("SH.000001", decision_at)
    if market_key not in risk_cache:
        risk_cache[market_key] = _risk_gate(
            symbol="SH.000001",
            frame=market_frame,
            native_daily_frame=market_daily_frame,
            observed_at=decision_at,
            expected_sessions=market_sessions,
            evaluation_not_before=context.facts.effective_start,
        )
    symbol_key = (context.facts.code, decision_at)
    if symbol_key not in risk_cache:
        risk_cache[symbol_key] = _risk_gate(
            symbol=context.facts.code,
            frame=context.frame,
            native_daily_frame=context.daily_frame,
            observed_at=decision_at,
            expected_sessions=market_sessions,
            evaluation_not_before=context.facts.effective_start,
        )
    (
        market_gate,
        market_risk_id,
        market_blockers,
        market_warmup,
        market_native_daily_calendar_coverage,
    ) = risk_cache[market_key]
    (
        symbol_gate,
        symbol_risk_id,
        symbol_blockers,
        symbol_warmup,
        symbol_native_daily_calendar_coverage,
    ) = risk_cache[symbol_key]
    sector_key = (sector_id or "UNRESOLVED", decision_at)
    if sector_key not in sector_risk_cache:
        sector_risk_cache[sector_key] = _sector_risk_gate(
            sector_id=sector_id,
            sector_members=(
                None
                if sector_id is None
                else current_sector_members_by_id.get(sector_id)
            ),
            observed_at=decision_at,
            expected_sessions=market_sessions,
            composite_source=sector_composite_source,
        )
    sector_gate, sector_risk_id, sector_blockers, sector_warmup = (
        sector_risk_cache[sector_key]
    )
    # Research-only relaxation: AMBER is allowed, but a confirmed RED or an
    # absent snapshot remains a hard reject.  The exact-GREEN ablation is
    # reported separately.
    market_pass, sector_risk_pass, symbol_pass, exact_green = (
        _research_risk_disposition(
            market_gate=market_gate,
            sector_gate=sector_gate,
            symbol_gate=symbol_gate,
            triggered=triggered,
            sector_hard_blocked=hard_blocked,
        )
    )
    research_same_sector = (
        research_row is not None and research_row.sector_id == sector_id
    )
    research_pass = (
        research_row is not None
        and research_row.accepted
        and research_same_sector
    )
    listed_days = sum(
        session >= context.facts.security_master.listed_from
        and session <= decision_at.date()
        for session in context.sessions
    )
    boundary = chain.l2_confirmation_bar_high
    q_slot = (
        0
        if boundary <= 0
        else int(
            ((initial_cash * Decimal("0.18") / boundary) / Decimal(LOT)).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        * LOT
    )
    q_plan = min(q_slot, liquidity_cap)

    required_gates = (
        SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
        if current_mode
        else INDIVIDUAL_REQUIRED_CANDIDATE_GATES
    )
    base: dict[str, tuple[bool, str, object]] = {
        gate: (True, f"PASS_{gate.upper()}", "frozen research replay gate")
        for gate in required_gates
    }
    base.update(
        {
            "sector_trigger": (
                triggered,
                (
                    "PASS_QMT_CURRENT_SECTOR_TRIGGER_RESEARCH_BACKFILL"
                    if current_mode and triggered
                    else (
                        "PASS_QMT_SECTOR_TRIGGER_POINT_IN_TIME"
                        if triggered
                        else "REJECT_SECTOR_TRIGGER"
                    )
                ),
                {
                    "sector_id": sector_id,
                    "trigger_at": (
                        None if trigger_event is None else trigger_event.observed_at
                    ),
                    "membership_mode": (
                        "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
                        if current_mode
                        else "POINT_IN_TIME"
                    ),
                },
            ),
            "market_risk": (
                market_pass,
                f"PASS_MARKET_RISK_NO_CONFIRMED_RED_RESEARCH_PROXY_{market_gate}" if market_pass else f"REJECT_MARKET_RISK_{market_gate}",
                {
                    "blocker_codes": market_blockers,
                    "warmup_evidence": market_warmup,
                    "native_daily_calendar_coverage_evidence": (
                        market_native_daily_calendar_coverage
                    ),
                },
            ),
            "sector_risk": (
                sector_risk_pass,
                (
                    f"PASS_SECTOR_RISK_NO_CONFIRMED_RED_RESEARCH_PROXY_{sector_gate}"
                    if sector_risk_pass
                    else (
                        "REJECT_SECTOR_RISK_TRIGGER_HARD_BLOCK"
                        if hard_blocked
                        else f"REJECT_SECTOR_RISK_{sector_gate}"
                    )
                ),
                {
                    "hard_blocked": hard_blocked,
                    "sector": sector_id,
                    "blocker_codes": sector_blockers,
                    "warmup_evidence": sector_warmup,
                },
            ),
            "symbol_risk": (
                symbol_pass,
                f"PASS_SYMBOL_RISK_NO_CONFIRMED_RED_RESEARCH_PROXY_{symbol_gate}" if symbol_pass else f"REJECT_SYMBOL_RISK_{symbol_gate}",
                {
                    "blocker_codes": symbol_blockers,
                    "warmup_evidence": symbol_warmup,
                    "native_daily_calendar_coverage_evidence": (
                        symbol_native_daily_calendar_coverage
                    ),
                },
            ),
            "listing": (status.listed, "PASS_LISTED" if status.listed else "REJECT_NOT_LISTED", listed_days),
            "st": ("ST" not in context.facts.security_master.name.upper(), "PASS_NOT_ST" if "ST" not in context.facts.security_master.name.upper() else "REJECT_ST", context.facts.security_master.name),
            "suspension": (not status.suspended, "PASS_NOT_SUSPENDED" if not status.suspended else "REJECT_SUSPENDED", decision_at.date()),
            "market_data": (status.point_in_time_state_complete, "PASS_COMPLETED_MARKET_DATA" if status.point_in_time_state_complete else "REJECT_MARKET_DATA_INCOMPLETE", decision_at),
            "runtime_market_rules": (listed_days > 5 and _daily_limits(context, decision_at.date()) is not None, "PASS_RUNTIME_RULES_PROXY" if listed_days > 5 else "REJECT_FIRST_FIVE_LISTING_SESSIONS", listed_days),
            "liquidity_history": (liquidity["completed_daily_sessions"] == 20 and liquidity["completed_same_clock_sessions"] == 20, "PASS_LIQUIDITY_HISTORY" if liquidity_cap > 0 else "REJECT_LIQUIDITY_HISTORY", liquidity),
            "liquidity_quantity": (liquidity_cap >= LOT, "PASS_LIQUIDITY_QUANTITY" if liquidity_cap >= LOT else "REJECT_ZERO_LIQUIDITY_CAP", liquidity_cap),
            "current_quote": (fresh_quote, "PASS_COMPLETED_1M_QUOTE_PROXY" if fresh_quote else "REJECT_STALE_COMPLETED_BAR", None if latest is None else latest["date"]),
            "quote_coverage": (fresh_quote, "PASS_COMPLETED_1M_QUOTE_PROXY_COVERAGE" if fresh_quote else "REJECT_QUOTE_PROXY_COVERAGE", "historical bid/ask unavailable"),
            "spread": (fresh_quote, "PASS_COMPLETED_1M_RANGE_PROXY" if fresh_quote else "REJECT_SPREAD_PROXY", "historical bid/ask unavailable"),
            "third_buy_boundary": (boundary > 0, "PASS_RAW_LOCATOR_CONFIRMATION_HIGH" if boundary > 0 else "REJECT_RAW_BOUNDARY_MISSING", boundary),
            "sector_strength": (
                sector_id in ranked_by_sector
                and ranked_by_sector[sector_id].horizontal_strength is not None,
                (
                    "PASS_HORIZONTAL_SECTOR_STRENGTH_RESOLVED"
                    if sector_id in ranked_by_sector
                    and ranked_by_sector[sector_id].horizontal_strength is not None
                    else "REJECT_HORIZONTAL_SECTOR_STRENGTH_UNRESOLVED"
                ),
                {
                    "sector_id": sector_id,
                    "strength": (
                        None
                        if sector_id not in ranked_by_sector
                        else ranked_by_sector[sector_id].horizontal_strength
                    ),
                    "rank": (
                        None
                        if sector_id not in ranked_by_sector
                        else ranked_by_sector[sector_id].horizontal_rank
                    ),
                    "evidence_revision": (
                        None
                        if trigger_event is None
                        else trigger_event.sector_strength_evidence_revision
                    ),
                },
            ),
        }
    )
    if not current_mode:
        base.update(
            {
                "industry_opportunity": (
                    research_row is not None
                    and research_row.industry_opportunity_status == "PASS",
                    "PASS_RESEARCH_PROXY_INDUSTRY_OPPORTUNITY"
                    if research_row is not None
                    and research_row.industry_opportunity_status == "PASS"
                    else "REJECT_RESEARCH_PROXY_INDUSTRY",
                    None
                    if research_row is None
                    else research_row.industry_opportunity_status,
                ),
                "fundamental_role": (
                    research_row is not None
                    and research_row.fundamental_role
                    in {"LEADER", "GROWTH_CHALLENGER"},
                    "PASS_RESEARCH_PROXY_FUNDAMENTAL_ROLE"
                    if research_row is not None
                    and research_row.fundamental_role
                    in {"LEADER", "GROWTH_CHALLENGER"}
                    else "REJECT_RESEARCH_PROXY_ROLE",
                    None if research_row is None else research_row.fundamental_role,
                ),
                "relative_value": (
                    research_row is not None
                    and research_row.relative_value_status in {"UNDERVALUED", "FAIR"},
                    "PASS_RESEARCH_PROXY_RELATIVE_VALUE"
                    if research_row is not None
                    and research_row.relative_value_status
                    in {"UNDERVALUED", "FAIR"}
                    else "REJECT_RESEARCH_PROXY_VALUE",
                    None
                    if research_row is None
                    else research_row.relative_value_status,
                ),
                "research_approximation": (
                    research_pass,
                    "PASS_QMT_RESEARCH_APPROXIMATION"
                    if research_pass
                    else "REJECT_QMT_RESEARCH_APPROXIMATION",
                    None
                    if research_row is None
                    else {
                        "reasons": research_row.reason_codes,
                        "snapshot_sector": research_row.sector_id,
                        "decision_sector": sector_id,
                    },
                ),
                "research_visibility": (
                    research_row is not None
                    and research_row.observed_at <= decision_at,
                    "PASS_RESEARCH_POINT_IN_TIME"
                    if research_row is not None
                    else "REJECT_RESEARCH_NOT_VISIBLE",
                    None if research_row is None else research_row.observed_at,
                ),
            }
        )
    checks = tuple(_check(gate, *base[gate]) for gate in sorted(base))
    accepted = all(row.passed for row in checks)
    role = "UNRESOLVED" if research_row is None else research_row.fundamental_role
    value = "UNRESOLVED" if research_row is None else research_row.relative_value_status
    sector_rank = ranked_by_sector.get(sector_id)
    sector_strength = (
        None if sector_rank is None else sector_rank.horizontal_strength
    )
    candidate = CandidateDecision(
        symbol=context.facts.code,
        parameter_set_id=params.parameter_set_id,
        selection_path=(
            RECENT_YEAR_SELECTION_PATH
            if current_mode
            else "INDIVIDUAL_THREE_PROGRAM"
        ),  # type: ignore[arg-type]
        accepted=accepted,
        checks=checks,
        fundamental_role=role,  # type: ignore[arg-type]
        relative_value_status=value,  # type: ignore[arg-type]
        sector_strength=sector_strength,
        confirmation_time=signal_at,
        higher_timeframe_risk_buyable=exact_green,
    )
    trigger_id = (
        sha256_json(_fingerprint_value(trigger_event))
        if trigger_event is not None
        else sha256_json({"missing_trigger": decision_at})
    )
    research_id = None if research_row is None else research_row.decision_id
    selection_ids = tuple(
        value
        for value in (
            current_catalog_entry_sha256 if current_mode else research_id,
            trigger_id,
            (
                None
                if sector_rank is None
                else sector_rank.strength_source_revision
            ),
        )
        if value is not None
    )
    risk_ids = (market_risk_id, sector_risk_id, symbol_risk_id)
    audit = {
        "symbol": context.facts.code,
        "signal_at": signal_at,
        "decision_at": decision_at,
        "sector_id": sector_id,
        "sector_ranking_available": sector_rank is not None,
        "sector_name": (
            None if sector_rank is None else sector_rank.sector_name
        ),
        "sector_eligible": sector_rank is not None,
        "sector_hard_block": sector_rank is None,
        "sector_regime": (
            "hostile" if sector_rank is None else sector_rank.regime
        ),
        "sector_ordinal": (
            None if sector_rank is None else sector_rank.ordinal
        ),
        "sector_rank_score": (
            0 if sector_rank is None else sector_rank.rank_score
        ),
        "sector_rank_components": (
            None if sector_rank is None else dict(sector_rank.rank_components)
        ),
        "sector_rank_reason_codes": (
            () if sector_rank is None else sector_rank.reason_codes
        ),
        "sector_catalog_revision": trigger.sector_scope_sha256,
        "sector_horizontal_strength": sector_strength,
        "sector_horizontal_rank": (
            None if sector_rank is None else sector_rank.horizontal_rank
        ),
        "sector_strength_observed_at": (
            None if sector_rank is None else sector_rank.strength_observed_at
        ),
        "sector_strength_source_revision": (
            None if sector_rank is None else sector_rank.strength_source_revision
        ),
        "sector_strength_evidence_revision": (
            None
            if trigger_event is None
            else trigger_event.sector_strength_evidence_revision
        ),
        "sector_strength_anchor_session": (
            None if sector_rank is None else sector_rank.strength_anchor_session
        ),
        "sector_strength_member_count": (
            0 if sector_rank is None else sector_rank.strength_member_count
        ),
        "sector_strength_reason_codes": (
            () if sector_rank is None else sector_rank.strength_reason_codes
        ),
        "research_decision_id": research_id,
        "selection_path": candidate.selection_path,
        "membership_mode": (
            "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
            if current_mode
            else "POINT_IN_TIME"
        ),
        "accepted": accepted,
        "passed_reason_codes": candidate.passed_reason_codes,
        "rejected_reason_codes": candidate.rejected_reason_codes,
        "market_risk_gate": market_gate,
        "sector_risk_gate": sector_gate,
        "symbol_risk_gate": symbol_gate,
        "market_risk_blocker_codes": market_blockers,
        "sector_risk_blocker_codes": sector_blockers,
        "symbol_risk_blocker_codes": symbol_blockers,
        "market_risk_warmup_evidence": market_warmup,
        "sector_risk_warmup_evidence": sector_warmup,
        "symbol_risk_warmup_evidence": symbol_warmup,
        "market_risk_native_daily_calendar_coverage_evidence": (
            market_native_daily_calendar_coverage
        ),
        "sector_risk_native_daily_calendar_coverage_evidence": None,
        "symbol_risk_native_daily_calendar_coverage_evidence": (
            symbol_native_daily_calendar_coverage
        ),
        "exact_green": exact_green,
        "liquidity": liquidity,
        "q_slot": q_slot,
        "q_plan": q_plan,
        "raw_entry_boundary": boundary,
    }
    return candidate, selection_ids, risk_ids, q_plan, boundary, exact_green, audit


def _market_grids(sessions: Sequence[date]) -> tuple[tuple[datetime, datetime], ...]:
    decisions = (time(9, 30), time(10), time(10, 30), time(11), time(11, 30), time(13, 30), time(14), time(14, 30))
    valuations = (time(10), time(10, 30), time(11), time(11, 30), time(13, 30), time(14), time(14, 30), time(15))
    return tuple(
        (
            datetime.combine(session, left, tzinfo=CN),
            datetime.combine(session, right, tzinfo=CN),
        )
        for session in sessions
        for left, right in zip(decisions, valuations, strict=True)
    )


def _dispatch_at(observed_at: datetime, decisions: Sequence[datetime]) -> datetime | None:
    position = bisect_left(decisions, observed_at)
    return None if position >= len(decisions) else decisions[position]


def _entry_dispatch_at(
    observed_at: datetime,
    decisions: Sequence[datetime],
) -> datetime | None:
    """Dispatch a new entry only after a completed continuous-auction 1m bar.

    The research variant explicitly excludes tick/order-book evidence and the
    QMT 09:30 opening event is not a completed one-minute bar.  A prior-session
    setup routed to the nominal 09:30 grid would therefore fail every current
    quote, suspension and same-clock liquidity gate by construction.  Defer
    only new entries to the next grid; persistent exits keep their ordinary
    exchange-open opportunity.
    """

    position = bisect_left(decisions, observed_at)
    while position < len(decisions) and decisions[position].time() == time(9, 30):
        position += 1
    return None if position >= len(decisions) else decisions[position]


def _entry_frozen_ids(
    fact,
    chain: DirectRecursiveEntryChain | ApproximateChanlunEntryChain,
) -> tuple[str, ...]:
    if isinstance(chain, ApproximateChanlunEntryChain):
        return tuple(
            dict.fromkeys(
                (
                    fact.structure_snapshot_id,
                    chain.technical_parameter_set_id,
                    *chain.provenance_fact_ids,
                )
            )
        )
    return tuple(
        dict.fromkeys(
            (
                fact.structure_snapshot_id,
                chain.l0_point_id,
                chain.l0_center_id,
                chain.l1_departure_unit_id,
                chain.l1_return_unit_id,
                chain.l2_locator_point_id,
                *chain.provenance_unit_ids,
                *chain.nine_segment_evidence_ids,
            )
        )
    )


def _first_adjusted_close_invalidation(
    context: SymbolContext,
    *,
    after: datetime,
    through: datetime | None,
    boundary: Decimal,
) -> datetime | None:
    rows = context.frame[context.frame["date"] > pd.Timestamp(after)]
    if through is not None:
        rows = rows[rows["date"] <= pd.Timestamp(through)]
    rows = rows[rows["close"] <= float(boundary)]
    if rows.empty:
        return None
    return pd.Timestamp(rows.iloc[0]["date"]).to_pydatetime()


def _approximate_tactical_locator(
    context: SymbolContext,
    parent: StructuralPoint,
    *,
    side: str,
    parameters: TechnicalApproximationParameters,
) -> StructuralPoint | None:
    allowed = {
        value
        for value in parameters.tactical_locator_point_types
        if value.endswith(side)
    }
    priority = {
        f"1{side}": 0,
        f"2{side}": 1,
        f"3{side}": 2,
    }
    positions = {value: index for index, value in enumerate(context.sessions)}
    parent_position = positions.get(parent.available_at.date())
    if parent_position is None:
        return None
    candidates = tuple(
        sorted(
            (
                point
                for point in context.facts.structural_points
                if point.recursive_level == parameters.locator_recursive_level
                and point.point_type in allowed
                and point.side == side
                and point.available_at >= parent.available_at
            ),
            key=lambda point: (
                point.available_at,
                priority.get(point.point_type, 9),
                point.point_id,
            ),
        )
    )
    if not candidates:
        return None
    locator = candidates[0]
    locator_position = positions.get(locator.available_at.date())
    if (
        locator_position is None
        or locator_position - parent_position
        > parameters.max_tactical_locator_wait_sessions
    ):
        return None
    return locator


def _build_signals(
    *,
    contexts: Mapping[str, SymbolContext],
    trigger: SectorFirstTriggerLedger,
    research: ResearchApproximationLedger | None,
    current_sector_by_code: Mapping[str, str],
    current_sector_members_by_id: Mapping[str, tuple[str, ...]],
    current_catalog_entry_sha256: str | None,
    sector_composite_source: CurrentQmtGics3CompositeReplaySource | None,
    market_frame: pd.DataFrame,
    market_daily_frame: pd.DataFrame,
    market_sessions: Sequence[date],
    grid_decisions: Sequence[datetime],
    initial_cash: Decimal,
    signal_not_before: datetime | None = None,
    technical_approximation: TechnicalApproximationParameters | None = None,
) -> tuple[dict[datetime, list[Signal]], tuple[dict[str, object], ...], dict[str, int]]:
    output: dict[datetime, list[Signal]] = defaultdict(list)
    audits: list[dict[str, object]] = []
    counters: Counter[str] = Counter()
    risk_cache: dict[
        tuple[str, datetime],
        tuple[
            str,
            str,
            tuple[str, ...],
            dict[str, object] | None,
            dict[str, object] | None,
        ],
    ] = {}
    sector_risk_cache: dict[
        tuple[str, datetime],
        tuple[str, str, tuple[str, ...], dict[str, object] | None],
    ] = {}
    alignment = (
        direct_recursive_alignment_contract()
        if technical_approximation is None
        else technical_approximation_alignment_contract()
    )
    for code, context in sorted(contexts.items()):
        points = {row.point_id: row for row in context.facts.structural_points}
        accepted_approximate_entries: list[
            tuple[
                datetime,
                ApproximateChanlunEntryChain,
                StructuralPoint,
            ]
        ] = []
        for fact in context.facts.direct_decisions:
            counters[f"direct_{fact.status.lower()}"] += 1
            approximation_audit: dict[str, object] | None = None
            signal_at = fact.first_seen_at
            if technical_approximation is None:
                dispatch = _entry_dispatch_at(signal_at, grid_decisions)
                if (
                    fact.status != "PASS"
                    or fact.aligned_entry_chain is None
                    or dispatch is None
                ):
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "TECHNICAL_REJECT_OR_OUTSIDE_REPLAY",
                            "reason_codes": fact.reason_codes
                            or ("NO_FUTURE_DISPATCH_GRID",),
                        }
                    )
                    continue
                chain: DirectRecursiveEntryChain | ApproximateChanlunEntryChain = (
                    fact.aligned_entry_chain
                )
                locator = points.get(chain.l2_locator_point_id)
                locator_at = (
                    None
                    if locator is None
                    else (locator.confirmed_at or locator.available_at)
                )
                raw_high = (
                    None
                    if locator_at is None
                    else _raw_price_at(context, locator_at, "raw_high")
                )
                if raw_high is None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "RAW_LOCATOR_BOUNDARY_MISSING",
                            "reason_codes": (
                                "REJECT_RAW_LOCATOR_BOUNDARY_MISSING",
                            ),
                        }
                    )
                    continue
                chain = replace(chain, l2_confirmation_bar_high=raw_high)
            else:
                strategic = points.get(fact.l0_point_id)
                if strategic is None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "APPROXIMATE_STRATEGIC_POINT_MISSING",
                            "reason_codes": (
                                "REJECT_APPROXIMATE_STRATEGIC_POINT_MISSING",
                            ),
                        }
                    )
                    counters["approximate_entry_rejected"] += 1
                    continue
                approximate = approximate_technical_entry_decision(
                    strict_decision=fact,
                    strategic_point=strategic,
                    structural_points=context.facts.structural_points,
                    trading_sessions=context.sessions,
                    parameters=technical_approximation,
                )
                approximation_audit = {
                    "technical_approximation_decision_id": approximate.decision_id,
                    "technical_approximation_parameter_set_id": (
                        approximate.parameter_set_id
                    ),
                    "technical_approximation_status": approximate.status,
                    "technical_approximation_confidence": approximate.confidence,
                    "technical_approximation_warning_codes": (
                        approximate.warning_codes
                    ),
                    "strict_reason_codes_reclassified_as_warnings": (
                        fact.reason_codes
                    ),
                }
                if approximate.status != "PASS" or approximate.locator_point_id is None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "APPROXIMATE_TECHNICAL_REJECT",
                            "reason_codes": approximate.reason_codes,
                            **approximation_audit,
                        }
                    )
                    counters["approximate_entry_rejected"] += 1
                    continue
                locator = points.get(approximate.locator_point_id)
                if locator is None or approximate.locator_at is None:
                    raise RuntimeError("passing approximate locator disappeared")
                signal_at = max(fact.first_seen_at, approximate.locator_at)
                dispatch = _entry_dispatch_at(signal_at, grid_decisions)
                if dispatch is None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "APPROXIMATE_OUTSIDE_REPLAY",
                            "reason_codes": ("NO_FUTURE_DISPATCH_GRID",),
                            **approximation_audit,
                        }
                    )
                    counters["approximate_entry_rejected"] += 1
                    continue
                invalidated_at = _first_adjusted_close_invalidation(
                    context,
                    after=fact.first_seen_at,
                    through=dispatch,
                    boundary=Decimal(str(strategic.structure_invalidation_price)),
                )
                if invalidated_at is not None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "decision_at": dispatch,
                            "status": "APPROXIMATE_SETUP_INVALIDATED_BEFORE_ORDER",
                            "reason_codes": (
                                "REJECT_APPROXIMATE_SETUP_INVALIDATED_BEFORE_ORDER",
                            ),
                            "invalidated_at": invalidated_at,
                            **approximation_audit,
                        }
                    )
                    counters["approximate_entry_invalidated_before_order"] += 1
                    continue
                locator_at = locator.confirmed_at or locator.available_at
                raw_high = _raw_price_at(context, locator_at, "raw_high")
                if raw_high is None:
                    audits.append(
                        {
                            "symbol": code,
                            "signal_at": signal_at,
                            "status": "RAW_APPROXIMATE_LOCATOR_BOUNDARY_MISSING",
                            "reason_codes": (
                                "REJECT_RAW_APPROXIMATE_LOCATOR_BOUNDARY_MISSING",
                            ),
                            **approximation_audit,
                        }
                    )
                    counters["approximate_entry_rejected"] += 1
                    continue
                chain = bind_approximate_entry_chain(
                    decision=approximate,
                    strategic_point=strategic,
                    locator_point=locator,
                    point_anchor_unit_ids=dict(
                        context.facts.point_anchor_unit_ids
                    ),
                    confirmation_bar_high=raw_high,
                )
                counters["approximate_entry_technical_pass"] += 1
            candidate, selection_ids, risk_ids, q_plan, boundary, exact_green, audit = _candidate(
                context=context,
                signal_at=signal_at,
                decision_at=dispatch,
                chain=chain,
                trigger=trigger,
                research=research,
                current_sector_by_code=current_sector_by_code,
                current_sector_members_by_id=current_sector_members_by_id,
                current_catalog_entry_sha256=current_catalog_entry_sha256,
                sector_composite_source=sector_composite_source,
                market_frame=market_frame,
                market_daily_frame=market_daily_frame,
                market_sessions=market_sessions,
                initial_cash=initial_cash,
                risk_cache=risk_cache,
                sector_risk_cache=sector_risk_cache,
            )
            audit["structure_snapshot_id"] = fact.structure_snapshot_id
            audit["l0_point_id"] = fact.l0_point_id
            if approximation_audit is not None:
                audit.update(approximation_audit)
            audits.append(audit)
            if not candidate.accepted:
                counters["candidate_rejected"] += 1
                continue
            counters["candidate_accepted"] += 1
            output[dispatch].append(
                Signal(
                    symbol=code,
                    kind="ENTRY",
                    observed_at=signal_at,
                    identity=fact.l0_point_id,
                    chain=chain,
                    structure_snapshot_id=fact.structure_snapshot_id,
                    selection=candidate,
                    selection_fact_ids=selection_ids,
                    risk_fact_ids=risk_ids,
                    frozen_fact_ids=_entry_frozen_ids(fact, chain),
                    q_plan=q_plan,
                    boundary=boundary,
                    exact_risk_green=exact_green,
                )
            )
            if isinstance(chain, ApproximateChanlunEntryChain):
                accepted_approximate_entries.append((dispatch, chain, strategic))

        # Strategic exits are never gated by sector selection.  The strict
        # path consumes its exact level-2 third sells.  The approximation also
        # accepts a completed 1m close through the 30m setup invalidation;
        # this is an observable price boundary, not a reconstructed sell point.
        if technical_approximation is None:
            for point in context.facts.strategic_sell_points:
                if point.recursive_level != 2 or point.point_type != "3sell":
                    continue
                dispatch = _dispatch_at(point.available_at, grid_decisions)
                if dispatch is not None:
                    output[dispatch].append(
                        Signal(
                            code,
                            "STRATEGIC_EXIT",
                            point.available_at,
                            point.point_id,
                            point=point,
                        )
                    )
                    counters["strategic_exit_signals"] += 1
        else:
            for entry_dispatch, chain, strategic in accepted_approximate_entries:
                exact_sells = tuple(
                    sorted(
                        (
                            point
                            for point in context.facts.structural_points
                            if point.recursive_level
                            == technical_approximation.strategic_recursive_level
                            and point.side == "sell"
                            and point.available_at > entry_dispatch
                        ),
                        key=lambda point: (point.available_at, point.point_id),
                    )
                )
                exact_sell = None if not exact_sells else exact_sells[0]
                invalidated_at = _first_adjusted_close_invalidation(
                    context,
                    after=entry_dispatch,
                    through=None,
                    boundary=chain.structural_invalidation_price,
                )
                exit_candidates = tuple(
                    value
                    for value in (
                        None if exact_sell is None else exact_sell.available_at,
                        invalidated_at,
                    )
                    if value is not None
                )
                if not exit_candidates:
                    counters["approximate_strategic_open_at_sample_end"] += 1
                    continue
                exit_at = min(exit_candidates)
                if signal_not_before is not None and exit_at < signal_not_before:
                    continue
                exit_dispatch = _dispatch_at(exit_at, grid_decisions)
                if exit_dispatch is None:
                    continue
                if exact_sell is not None and exact_sell.available_at == exit_at:
                    identity = exact_sell.point_id
                    point = exact_sell
                    frozen = (exact_sell.point_id, chain.chain_id)
                    counters["approximate_strategic_exit_by_level2_sell"] += 1
                else:
                    identity = sha256_json(
                        {
                            "schema": "approximate-invalidation-exit",
                            "symbol": code,
                            "entry_chain": chain.chain_id,
                            "invalidated_at": exit_at,
                            "boundary": chain.structural_invalidation_price,
                        }
                    )
                    point = strategic
                    frozen = (
                        identity,
                        chain.chain_id,
                        chain.strategic_point_id,
                        chain.technical_parameter_set_id,
                    )
                    counters["approximate_strategic_exit_by_invalidation"] += 1
                output[exit_dispatch].append(
                    Signal(
                        code,
                        "STRATEGIC_EXIT",
                        exit_at,
                        identity,
                        point=point,
                        frozen_fact_ids=frozen,
                    )
                )
                counters["strategic_exit_signals"] += 1

        # The strict path requires an exact descendant L2 1m first/second
        # point.  The approximation waits for a same-side confirmed level-0
        # point in a bounded three-session window and records both identities.
        approximate_entry_start = (
            None
            if not accepted_approximate_entries
            else min(value[0] for value in accepted_approximate_entries)
        )
        for point in context.facts.structural_points:
            if point.recursive_level != 1 or point.point_type not in {
                "1buy",
                "2buy",
                "3buy",
                "1sell",
                "2sell",
                "3sell",
            }:
                continue
            if (
                technical_approximation is not None
                and (
                    approximate_entry_start is None
                    or point.available_at < approximate_entry_start
                )
            ):
                continue
            side = "sell" if point.side == "sell" else "buy"
            locator = (
                _exact_locator(context, point, side=side)
                if technical_approximation is None
                else _approximate_tactical_locator(
                    context,
                    point,
                    side=side,
                    parameters=technical_approximation,
                )
            )
            if locator is None:
                counters[
                    "tactical_locator_rejected"
                    if technical_approximation is None
                    else "approximate_tactical_locator_rejected"
                ] += 1
                continue
            signal_at = max(point.available_at, locator.available_at)
            if signal_not_before is not None and signal_at < signal_not_before:
                continue
            dispatch = _dispatch_at(signal_at, grid_decisions)
            if dispatch is None:
                continue
            kind = "TACTICAL_SELL" if point.side == "sell" else "TACTICAL_BUYBACK"
            boundary = (
                None
                if kind == "TACTICAL_SELL"
                else _raw_price_at(context, locator.confirmed_at or locator.available_at, "raw_high")
            )
            if kind == "TACTICAL_BUYBACK" and boundary is None:
                counters["tactical_raw_boundary_rejected"] += 1
                continue
            output[dispatch].append(
                Signal(
                    code,
                    kind,
                    signal_at,
                    point.point_id,
                    point=point,
                    frozen_fact_ids=tuple(
                        dict.fromkeys(
                            (
                                point.point_id,
                                locator.point_id,
                                dict(context.facts.point_anchor_unit_ids)[
                                    point.point_id
                                ],
                                dict(context.facts.point_anchor_unit_ids)[
                                    locator.point_id
                                ],
                                *(
                                    ()
                                    if technical_approximation is None
                                    else (
                                        technical_approximation.parameter_set_id,
                                    )
                                ),
                            )
                        )
                    ),
                    risk_fact_ids=(sha256_json({"tactical_risk": point.point_id}),),
                    boundary=boundary,
                )
            )
            counters[kind.lower()] += 1
            if technical_approximation is not None:
                counters[f"approximate_{kind.lower()}"] += 1
    # Guard the mapping identity in the output even though the engine checks it again.
    counters["alignment_contract_bound"] = int(bool(alignment.parameter_set_id))
    return output, tuple(audits), dict(sorted(counters.items()))


def _facts_for_signal(
    *,
    signal: Signal,
    decision_at: datetime,
    boundary: Decimal,
) -> DecisionInput:
    strategic = StrategicSignalFacts(l0_third_sell=signal.kind == "STRATEGIC_EXIT")
    tactical = TacticalSignalFacts(
        l1_phase="OSCILLATION",
        l1_third_sell=signal.kind == "TACTICAL_SELL",
        l1_third_buy=signal.kind == "TACTICAL_BUYBACK" and signal.point is not None and signal.point.point_type == "3buy",
        third_sell_recovery_first_or_second_buy=(signal.kind == "TACTICAL_BUYBACK" and signal.point is not None and signal.point.point_type in {"1buy", "2buy"}),
        higher_timeframe_allows_third_sell_recovery=True,
        broker_sellable_tactical_qty=10**9,
        q_liquidity_cap=10**9,
        cash_affordable_buyback_qty=10**9,
    )
    snapshot_id = signal.structure_snapshot_id or signal.identity
    return DecisionInput(
        symbol=signal.symbol,
        decision_time=decision_at,
        confirmation_time=signal.observed_at,
        structure_snapshot_id=snapshot_id,
        selection_snapshot_id=(
            None if signal.selection is None else signal.selection_fact_ids[0]
        ),
        account_snapshot_id=sha256_json({"event_sourced_account": decision_at}),
        strategic_state="S_ENTRY_READY" if signal.kind == "ENTRY" else "S_FLAT",
        health=SystemHealthFacts(True, True, True, True, True),
        strategic=strategic,
        tactical=tactical,
        cycle_ledger=None,
        candidate=signal.selection,
        q_plan=signal.q_plan,
        price_cap_or_floor=boundary,
    )


def _event(
    *,
    signal: Signal,
    context: SymbolContext,
    decision_at: datetime,
    valuation_at: datetime,
    technical_approximation: TechnicalApproximationParameters | None = None,
) -> ReplayDecisionEvent:
    status = _status(context, decision_at=decision_at)
    persistent = signal.kind in {"STRATEGIC_EXIT", "TACTICAL_SELL"}
    boundary = status.limit_down if persistent else signal.boundary or Decimal("0")
    confirmed = decision_at + BROKER_LATENCY
    bars = _execution_bars(
        context,
        confirmed_at=confirmed,
        valuation_at=valuation_at,
        optional=not persistent,
    )
    expires = None if persistent else (bars[0].closed_at if bars else valuation_at)
    alignment = (
        direct_recursive_alignment_contract()
        if technical_approximation is None
        else technical_approximation_alignment_contract()
    )
    frozen = signal.frozen_fact_ids or (signal.identity,)
    snapshot_id = signal.structure_snapshot_id or signal.identity
    if snapshot_id not in frozen:
        frozen = (snapshot_id, *frozen)
    return ReplayDecisionEvent(
        # Event identity is content-addressed by its source signal.  A former
        # trailing batch ordinal made an otherwise identical event/order id
        # change whenever an unrelated symbol entered or left the same grid.
        event_id=_signal_event_id(signal, decision_at),
        facts=_facts_for_signal(signal=signal, decision_at=decision_at, boundary=boundary),
        bindings=ReplayFactBindings(
            timeframe_override_parameter_set_id=None,
            alignment_contract_id=alignment.contract_id,
            alignment_parameter_set_id=alignment.parameter_set_id,
            frozen_structure_fact_ids=tuple(dict.fromkeys(frozen)),
            selection_fact_ids=signal.selection_fact_ids,
            risk_fact_ids=signal.risk_fact_ids or (sha256_json({"risk": signal.identity}),),
            aligned_entry_chain=signal.chain,
        ),
        created_at=decision_at,
        broker_confirmed_at=confirmed,
        expires_at=expires,
        execution_status=status,
        broker_position_quantity=None,
        bars=bars,
        persistent_intent_id=_persistent_signal_id(signal),
        account_position_source="EVENT_SOURCED_REPLAY",
        sellable_quantity_source="EVENT_SOURCED_REPLAY",
    )


def _persistent_signal_id(signal: Signal) -> str | None:
    """Return the stable lifecycle id shared by every retry grid."""

    if signal.kind not in {"STRATEGIC_EXIT", "TACTICAL_SELL"}:
        return None
    return f"persistent:{signal.symbol}:{signal.kind}:{signal.identity}"


def _signal_event_id(signal: Signal, decision_at: datetime) -> str:
    """Content identity independent of universe order or batch position."""

    return (
        f"FM:{decision_at.isoformat()}:{signal.kind}:"
        f"{signal.symbol}:{signal.identity}"
    )


def _corporate_actions(
    contexts: Mapping[str, SymbolContext],
    decision_at: datetime,
) -> tuple[tuple[ReplayCashDistributionFact, ...], tuple[ReplayMandatoryShareActionFact, ...]]:
    if decision_at.time() != time(9, 30):
        return (), ()
    cash: list[ReplayCashDistributionFact] = []
    shares: list[ReplayMandatoryShareActionFact] = []
    for context in contexts.values():
        factor = _factor_on(context, decision_at.date())
        if factor is None:
            continue
        source = sha256_json(_jsonable(asdict(factor)))
        if factor.interest > 0:
            cash.append(
                ReplayCashDistributionFact(
                    action_id=f"cash:{context.facts.code}:{factor.effective_on}",
                    symbol=context.facts.code,
                    effective_at=decision_at,
                    known_at=decision_at,
                    cash_per_share=factor.interest,
                    source_id=source,
                    source_ledger_sha256=context.facts.source_revision,
                )
            )
        multiplier = Decimal("1") + factor.stock_bonus + factor.stock_gift
        if multiplier != Decimal("1"):
            shares.append(
                ReplayMandatoryShareActionFact(
                    action_id=f"share:{context.facts.code}:{factor.effective_on}",
                    symbol=context.facts.code,
                    effective_at=decision_at,
                    known_at=decision_at,
                    share_multiplier=multiplier,
                    source_id=source,
                    source_ledger_sha256=context.facts.source_revision,
                )
            )
    return tuple(cash), tuple(shares)


def _build_batches_with_state(
    *,
    contexts: Mapping[str, SymbolContext],
    grids: Sequence[tuple[datetime, datetime]],
    signals: Mapping[datetime, Sequence[Signal]],
    initial_strategic_active: Mapping[str, Signal] | None = None,
    initial_tactical_active: Mapping[str, Signal] | None = None,
    technical_approximation: TechnicalApproximationParameters | None = None,
    retire_persistent_after_sessions: Mapping[str, date] | None = None,
) -> tuple[tuple[ReplayBatch, ...], dict[str, dict[str, Signal]]]:
    batches: list[ReplayBatch] = []
    strategic_active: dict[str, Signal] = dict(initial_strategic_active or {})
    tactical_active: dict[str, Signal] = dict(initial_tactical_active or {})
    missing_contexts = (
        set(strategic_active).union(tactical_active) - set(contexts)
    )
    if missing_contexts:
        raise ValueError(
            f"persistent paper signals lack current contexts: {sorted(missing_contexts)}"
        )
    resolution_sessions = dict(retire_persistent_after_sessions or {})
    for ordinal, (decision_at, valuation_at) in enumerate(grids):
        # Preserve the active dictionary through the whole trading session and
        # reconcile it only at the end-of-day checkpoint.  A lifecycle
        # resolved on D remains eligible for idempotent suppression on D, then
        # disappears before the first grid of the next session.
        active = _carry_active_into_session(
            {
                "strategic": strategic_active,
                "tactical": tactical_active,
            },
            current_session=decision_at.date(),
            resolution_sessions=resolution_sessions,
        )
        strategic_active = active["strategic"]
        tactical_active = active["tactical"]
        new = sorted(
            signals.get(decision_at, ()),
            key=lambda row: (row.observed_at, row.kind, row.symbol),
        )
        one_shot: dict[str, Signal] = {}
        for signal in new:
            if signal.kind == "ENTRY":
                # A later completed strategic buy starts a new structure cycle.
                strategic_active.pop(signal.symbol, None)
                one_shot[signal.symbol] = signal
            elif signal.kind == "STRATEGIC_EXIT":
                strategic_active[signal.symbol] = signal
            elif signal.kind == "TACTICAL_SELL":
                tactical_active[signal.symbol] = signal
            elif signal.kind == "TACTICAL_BUYBACK":
                tactical_active.pop(signal.symbol, None)
                one_shot[signal.symbol] = signal
        chosen: dict[str, Signal] = dict(one_shot)
        for symbol, signal in tactical_active.items():
            chosen[symbol] = signal
        for symbol, signal in strategic_active.items():
            chosen[symbol] = signal
        event_rows: list[ReplayDecisionEvent] = []
        for symbol, signal in sorted(chosen.items()):
            context = contexts[symbol]
            # At 09:30 QMT has only the auction event, not a completed
            # continuous one-minute status.  Persistent exits remain active
            # and are retried at 10:00; no unresolved status is fabricated.
            if not _status(context, decision_at=decision_at).point_in_time_state_complete:
                continue
            event_rows.append(
                _event(
                    signal=signal,
                    context=context,
                    decision_at=decision_at,
                    valuation_at=valuation_at,
                    technical_approximation=technical_approximation,
                )
            )
        events = tuple(event_rows)
        decision_marks = tuple(
            mark
            for context in contexts.values()
            for mark in (_mark(context, decision_at),)
            if mark is not None
        )
        valuation_marks = tuple(
            mark
            for context in contexts.values()
            for mark in (_mark(context, valuation_at),)
            if mark is not None
        )
        cash, shares = _corporate_actions(contexts, decision_at)
        batches.append(
            ReplayBatch(
                batch_id=f"FM:{ordinal:06d}:{decision_at.isoformat()}",
                decision_at=decision_at,
                valuation_at=valuation_at,
                events=events,
                decision_marks=decision_marks,
                valuation_marks=valuation_marks,
                cash_distributions=cash,
                mandatory_share_actions=shares,
            )
        )
    return tuple(batches), {
        "strategic": strategic_active,
        "tactical": tactical_active,
    }


def _carry_active_into_session(
    active_state: Mapping[str, Mapping[str, Signal]],
    *,
    current_session: date,
    resolution_sessions: Mapping[str, date],
) -> dict[str, dict[str, Signal]]:
    """Apply an end-of-session lifecycle checkpoint to the next session.

    A same-session retry must remain visible to the event-sourced engine so it
    can be suppressed under the stable persistent identity.  Removing only
    identities resolved on a *strictly earlier* session exactly mirrors the
    daily session checkpoint and prevents a stale strategic exit from masking
    a later entry or tactical signal for the same symbol.
    """

    return {
        name: {
            symbol: signal
            for symbol, signal in values.items()
            if (
                (resolved_on := resolution_sessions.get(
                    _persistent_signal_id(signal) or ""
                ))
                is None
                or resolved_on >= current_session
            )
        }
        for name, values in active_state.items()
    }


def _persistent_resolution_sessions(
    batches: Sequence[ReplayBatch],
    replay_result: object,
) -> dict[str, date]:
    """Recover each resolved lifecycle's exact causal checkpoint session.

    The engine records an intent for every adjudicated retry and suppresses
    all later retries immediately after resolution.  Consequently the last
    recorded intent for a final ``resolved_persistent_intent_id`` is the exact
    session on which that lifecycle became completed or proven to be a no-op.
    No decision semantics are reconstructed here.
    """

    batch_sessions: dict[str, date] = {}
    for batch in batches:
        if batch.batch_id in batch_sessions:
            raise RuntimeError(
                f"historical replay batch id is duplicated: {batch.batch_id}"
            )
        batch_sessions[batch.batch_id] = batch.decision_at.date()

    resolved = tuple(
        sorted(set(getattr(replay_result, "resolved_persistent_intent_ids", ())))
    )
    adjudicated: dict[str, list[date]] = defaultdict(list)
    for record in getattr(replay_result, "intents", ()):
        identity = getattr(record, "persistent_intent_id", None)
        if identity not in resolved:
            continue
        try:
            session = batch_sessions[str(record.batch_id)]
        except KeyError as exc:
            raise RuntimeError(
                "resolved persistent intent references an unknown replay batch: "
                f"{record.batch_id}"
            ) from exc
        adjudicated[str(identity)].append(session)

    missing = tuple(identity for identity in resolved if not adjudicated[identity])
    if missing:
        raise RuntimeError(
            "resolved persistent lifecycle lacks an adjudicated intent: "
            + ", ".join(missing)
        )
    return {
        identity: max(adjudicated[identity])
        for identity in resolved
    }


def _scheduler_event_signature(
    batches: Sequence[ReplayBatch],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Stable identity of the decision schedule, excluding invariant marks."""

    return tuple(
        (
            batch.batch_id,
            tuple(event.event_id for event in batch.events),
        )
        for batch in batches
    )


def _tactical_execution_audit(
    *,
    signals: Mapping[datetime, Sequence[Signal]],
    replay_result: object,
) -> dict[str, object]:
    """Explain every tactical source signal through intent, order and fill.

    Signal counters alone made six raw tactical observations look like a
    missing execution path.  This audit binds each source identity to the
    shared decision-core record and distinguishes a true scheduler omission
    from a legal zero-lot/no-position decision.
    """

    generated = sorted(
        (
            signal
            for values in signals.values()
            for signal in values
            if signal.kind in {"TACTICAL_SELL", "TACTICAL_BUYBACK"}
        ),
        key=lambda value: (
            value.observed_at,
            value.kind,
            value.symbol,
            value.identity,
        ),
    )
    intent_records = tuple(getattr(replay_result, "intents", ()))
    order_records = tuple(getattr(replay_result, "orders", ()))
    suppressed = dict(
        getattr(replay_result, "suppressed_persistent_event_counts", ())
    )
    rows: list[dict[str, object]] = []
    for signal in generated:
        persistent_id = _persistent_signal_id(signal)
        snapshot_id = signal.structure_snapshot_id or signal.identity
        matched_intents = tuple(
            record
            for record in intent_records
            if (
                record.persistent_intent_id == persistent_id
                if persistent_id is not None
                else (
                    record.intent.symbol == signal.symbol
                    and record.intent.confirmation_time == signal.observed_at
                    and record.intent.structure_snapshot_id == snapshot_id
                    and f":{signal.kind}:" in record.event_id
                )
            )
        )
        event_ids = {record.event_id for record in matched_intents}
        matched_orders = tuple(
            record for record in order_records if record.event_id in event_ids
        )
        fill_count = sum(len(record.match.fills) for record in matched_orders)
        reason_codes = tuple(
            dict.fromkeys(
                reason
                for record in matched_intents
                for reason in record.intent.reason_codes
            )
        )
        if fill_count:
            disposition = "EXECUTED"
        elif matched_orders:
            disposition = "ORDERED_UNFILLED"
        elif not matched_intents:
            disposition = "NOT_DISPATCHED_BY_PRIORITY_OR_REPLACEMENT"
        elif "NO_SELLABLE_TACTICAL_INVENTORY" in reason_codes:
            disposition = "NO_EXECUTABLE_TACTICAL_LOT"
        elif "NO_ACTIONABLE_COMPLETED_SIGNAL" in reason_codes:
            disposition = "NO_ACTIVE_RESTORE_OR_POSITION"
        else:
            disposition = "DECISION_CORE_NO_ORDER"
        rows.append(
            {
                "signal_identity": signal.identity,
                "kind": signal.kind,
                "symbol": signal.symbol,
                "observed_at": signal.observed_at,
                "structure_snapshot_id": snapshot_id,
                "persistent_intent_id": persistent_id,
                "decision_record_count": len(matched_intents),
                "decision_actions": tuple(
                    dict.fromkeys(
                        record.intent.action for record in matched_intents
                    )
                ),
                "reason_codes": reason_codes,
                "order_count": len(matched_orders),
                "fill_count": fill_count,
                "suppressed_retry_count": (
                    0 if persistent_id is None else suppressed.get(persistent_id, 0)
                ),
                "disposition": disposition,
            }
        )
    dispositions = Counter(str(row["disposition"]) for row in rows)
    tactical_persistent_ids = {
        value
        for value in (_persistent_signal_id(signal) for signal in generated)
        if value is not None
    }
    order_count = sum(int(row["order_count"]) for row in rows)
    fill_count = sum(int(row["fill_count"]) for row in rows)
    if not generated:
        adjudication = "NO_TACTICAL_SOURCE_SIGNAL"
    elif fill_count:
        adjudication = "TACTICAL_EXECUTION_PRESENT"
    elif order_count:
        adjudication = "TACTICAL_ORDERS_UNFILLED"
    elif dispositions.get("NO_EXECUTABLE_TACTICAL_LOT"):
        adjudication = (
            "TACTICAL_SIGNALS_PRESENT_BUT_NO_LEGAL_LOT_UNDER_FROZEN_PARAMETERS"
        )
    else:
        adjudication = "TACTICAL_SIGNALS_PRESENT_BUT_NOT_ACTIONABLE"
    return {
        "schema": "chanlun-tactical-execution-audit",
        "generated_signal_count": len(generated),
        "dispatched_source_signal_count": sum(
            bool(row["decision_record_count"]) for row in rows
        ),
        "decision_record_count": sum(
            int(row["decision_record_count"]) for row in rows
        ),
        "order_count": order_count,
        "fill_count": fill_count,
        "completed_tactical_cycle_count": getattr(
            getattr(replay_result, "metrics", None),
            "tactical_cycle_count",
            0,
        ),
        "suppressed_retry_count": sum(
            count
            for identity, count in suppressed.items()
            if identity in tactical_persistent_ids
        ),
        "disposition_counts": dict(sorted(dispositions.items())),
        "adjudication": adjudication,
        "signals": tuple(rows),
    }


def _fee_model(start: date) -> FeeModel:
    return FeeModel(
        schedule_id=FEE_SCHEDULE_ID,
        rates=(
            FeeRateAt(
                effective_from=start,
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stock_sell_stamp_rate=Decimal("0.0005"),
                transfer_rate=Decimal("0.00001"),
            ),
        ),
    )


def _run(
    *,
    batches: tuple[ReplayBatch, ...],
    research: ResearchApproximationLedger | None,
    initial_cash: Decimal,
    technical_approximation: TechnicalApproximationParameters | None = None,
) -> object:
    if not batches:
        raise ValueError("replay requires market batches")
    return StrictMultiSymbolReplayEngine(
        initial_cash=initial_cash,
        started_at=batches[0].decision_at,
        fee_model=_fee_model(batches[0].decision_at.date()),
        contract=(
            research_sector_technical_approx_replay_contract()
            if technical_approximation is not None
            else research_sector_technical_direct_replay_contract()
            if research is None
            else research_individual_direct_replay_contract(
                research.parameters.parameter_set_id
            )
        ),
    ).replay(batches)


def _run_historical_session_checkpoint_replay(
    *,
    contexts: Mapping[str, SymbolContext],
    grids: Sequence[tuple[datetime, datetime]],
    signals: Mapping[datetime, Sequence[Signal]],
    research: ResearchApproximationLedger | None,
    initial_cash: Decimal,
    technical_approximation: TechnicalApproximationParameters | None,
    initial_strategic_active: Mapping[str, Signal] | None = None,
    initial_tactical_active: Mapping[str, Signal] | None = None,
) -> tuple[
    tuple[ReplayBatch, ...],
    dict[str, dict[str, Signal]],
    object,
    dict[str, object],
]:
    """Converge a full-history schedule to daily causal checkpoints.

    The replay engine is the only authority that may declare a persistent
    lifecycle resolved.  A first pass obtains those causal sessions; the next
    build retires each identity only from the following session onward.  That
    can expose a later same-symbol signal, so build/replay repeats until the
    event schedule is unchanged.  The operation is causal: a retirement can
    affect only sessions after its adjudication session.
    """

    source_signals = tuple(
        signal for values in signals.values() for signal in values
    ) + tuple((initial_strategic_active or {}).values()) + tuple(
        (initial_tactical_active or {}).values()
    )
    source_signal_ids = {
        (signal.symbol, signal.kind, signal.observed_at, signal.identity)
        for signal in source_signals
    }
    # Each non-converged pass must alter at least one source lifecycle or its
    # dispatch.  The data-derived bound fails closed instead of silently
    # accepting an arbitrary iteration cap.
    maximum_build_passes = max(3, len(source_signal_ids) * 2 + 3)
    resolution_sessions: dict[str, date] = {}
    previous_signature: tuple[tuple[str, tuple[str, ...]], ...] | None = None
    previous_result: object | None = None
    seen_signatures: dict[
        tuple[tuple[str, tuple[str, ...]], ...], int
    ] = {}
    replay_pass_count = 0
    initial_event_ids: set[str] | None = None

    for build_pass_count in range(1, maximum_build_passes + 1):
        batches, active_state = _build_batches_with_state(
            contexts=contexts,
            grids=grids,
            signals=signals,
            initial_strategic_active=initial_strategic_active,
            initial_tactical_active=initial_tactical_active,
            technical_approximation=technical_approximation,
            retire_persistent_after_sessions=resolution_sessions,
        )
        signature = _scheduler_event_signature(batches)
        event_ids = {
            event.event_id for batch in batches for event in batch.events
        }
        if initial_event_ids is None:
            initial_event_ids = set(event_ids)

        if signature == previous_signature:
            if previous_result is None:
                raise RuntimeError("scheduler converged without a replay result")
            initial_ids = initial_event_ids or set()
            audit = {
                "schema": "chanlun-session-checkpoint-scheduler-audit",
                "mode": "HISTORICAL_SESSION_CHECKPOINT_FIXED_POINT",
                "converged": True,
                "build_pass_count": build_pass_count,
                "replay_pass_count": replay_pass_count,
                "maximum_build_passes": maximum_build_passes,
                "source_signal_count": len(source_signal_ids),
                "initial_event_count": len(initial_ids),
                "final_event_count": len(event_ids),
                "newly_exposed_event_count": len(event_ids - initial_ids),
                "removed_stale_retry_event_count": len(initial_ids - event_ids),
                "resolved_persistent_signal_count": len(resolution_sessions),
                "resolution_sessions": tuple(
                    {
                        "persistent_intent_id": identity,
                        "resolved_on": resolved_on,
                        "retirement_effective_after": resolved_on,
                    }
                    for identity, resolved_on in sorted(
                        resolution_sessions.items()
                    )
                ),
                "event_schedule_sha256": sha256_json(
                    {"batch_events": signature}
                ),
                "retirement_boundary": (
                    "RESOLUTION_SESSION_END_EFFECTIVE_NEXT_SESSION"
                ),
                "parameters_changed": False,
                "live_status": "LIVE_DISABLED",
            }
            return batches, active_state, previous_result, audit

        repeated_at = seen_signatures.get(signature)
        if repeated_at is not None:
            raise RuntimeError(
                "historical session-checkpoint scheduler entered a cycle: "
                f"pass {build_pass_count} repeats pass {repeated_at}"
            )
        seen_signatures[signature] = build_pass_count
        result = _run(
            batches=batches,
            research=research,
            initial_cash=initial_cash,
            technical_approximation=technical_approximation,
        )
        replay_pass_count += 1
        resolution_sessions = _persistent_resolution_sessions(batches, result)
        previous_signature = signature
        previous_result = result

    raise RuntimeError(
        "historical session-checkpoint scheduler did not converge within its "
        f"data-derived bound of {maximum_build_passes} build passes"
    )


def _source_signal_projection(
    signals: Mapping[datetime, Sequence[Signal]],
    predicate,
) -> dict[datetime, tuple[Signal, ...]]:
    """Project source signals before scheduling, omitting empty dispatches."""

    return {
        decision_at: retained
        for decision_at, values in signals.items()
        if (retained := tuple(signal for signal in values if predicate(signal)))
    }


def _run_historical_causal_ablations(
    *,
    contexts: Mapping[str, SymbolContext],
    grids: Sequence[tuple[datetime, datetime]],
    signals: Mapping[datetime, Sequence[Signal]],
    research: ResearchApproximationLedger | None,
    initial_cash: Decimal,
    technical_approximation: TechnicalApproximationParameters | None,
    initial_strategic_active: Mapping[str, Signal] | None = None,
    initial_tactical_active: Mapping[str, Signal] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Rebuild every historical ablation from its own source-signal stream.

    Filtering an already scheduled primary batch tuple is not causal: removing
    a tactical or non-GREEN entry can change which later same-symbol signal is
    selected after a persistent lifecycle retires.  Each variant therefore
    receives an independent fixed-point schedule and the same replay engine.
    """

    variants = (
        (
            "NO_TACTICAL",
            _source_signal_projection(
                signals,
                lambda signal: signal.kind
                not in {"TACTICAL_SELL", "TACTICAL_BUYBACK"},
            ),
            {},
        ),
        (
            "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY",
            _source_signal_projection(
                signals,
                lambda signal: (
                    signal.kind != "ENTRY" or signal.exact_risk_green
                ),
            ),
            dict(initial_tactical_active or {}),
        ),
    )
    results: dict[str, object] = {}
    audits: dict[str, dict[str, object]] = {}
    for name, variant_signals, tactical_active in variants:
        _batches, _active, result, audit = (
            _run_historical_session_checkpoint_replay(
                contexts=contexts,
                grids=grids,
                signals=variant_signals,
                research=research,
                initial_cash=initial_cash,
                technical_approximation=technical_approximation,
                initial_strategic_active=initial_strategic_active,
                initial_tactical_active=tactical_active,
            )
        )
        results[name] = result
        audits[name] = audit
    return results, audits


def _series_metrics(points: Sequence[tuple[date, Decimal]]) -> dict[str, object]:
    if len(points) < 2 or any(value <= 0 for _session, value in points):
        return {"status": "NOT_EVALUABLE", "observations": len(points)}
    values = tuple(value for _session, value in points)
    total = values[-1] / values[0] - Decimal("1")
    peak = values[0]
    drawdown = Decimal("0")
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        returns.append(float(current / previous - Decimal("1")))
        peak = max(peak, current)
        drawdown = max(drawdown, (peak - current) / peak)
    span = max(1, (points[-1][0] - points[0][0]).days)
    annualized = Decimal(str((1 + float(total)) ** (365 / span) - 1)) if total > -1 else None
    sharpe = None
    if len(returns) >= 2 and stdev(returns) > 0:
        sharpe = Decimal(str(mean(returns) / stdev(returns) * math.sqrt(252)))
    return {
        "status": "EVALUATED",
        "start": points[0][0],
        "end": points[-1][0],
        "observations": len(points),
        "net_return": total,
        "annualized_return": annualized,
        "max_drawdown": drawdown,
        "sharpe": sharpe,
    }


def _daily_equity(result) -> tuple[tuple[date, Decimal], ...]:
    daily: dict[date, Decimal] = {}
    for point in result.equity_curve:
        daily[point.observed_at.date()] = point.equity
    return tuple(sorted(daily.items()))


def _terminal_accounting_attribution(
    result: object,
    *,
    sector_by_symbol: Mapping[str, str],
    sector_name_by_id: Mapping[str, str],
    sector_membership_mode: str,
) -> dict[str, object]:
    """Explain terminal P&L without inventing a second accounting ledger.

    Closed cycles have exact realised net P&L.  An open cycle can already
    contain dividends or tactical cash flows, so its exact remainder is named
    *marked open-cycle P&L*, not pure unrealised P&L.  The latter would require
    an additional cost-basis ledger and is deliberately left unresolved.
    """

    equity_curve = tuple(getattr(result, "equity_curve", ()))
    if not equity_curve:
        return {
            "schema": "chanlun-terminal-accounting-attribution",
            "status": "NOT_EVALUABLE",
            "reason_codes": ("TERMINAL_EQUITY_POINT_MISSING",),
            "sector_membership_mode": sector_membership_mode,
        }
    terminal = equity_curve[-1]
    initial_cash = Decimal(getattr(result, "initial_cash"))
    final_cash = Decimal(getattr(result, "final_cash"))
    terminal_cash = Decimal(getattr(terminal, "cash"))
    terminal_market = Decimal(getattr(terminal, "market_value"))
    terminal_equity = Decimal(getattr(terminal, "equity"))
    total_net_pnl = terminal_equity - initial_cash
    reasons: list[str] = []
    if not bool(getattr(terminal, "complete", False)):
        reasons.extend(str(value) for value in terminal.reason_codes)

    sector_rows: dict[str, dict[str, object]] = {}

    def sector_row(symbol: str) -> dict[str, object]:
        sector_id = sector_by_symbol.get(symbol)
        if sector_id is None:
            reasons.append(f"UNRESOLVED_CURRENT_SECTOR:{symbol}")
            sector_id = "UNRESOLVED"
        row = sector_rows.setdefault(
            sector_id,
            {
                "sector_id": sector_id,
                "sector_name": sector_name_by_id.get(sector_id),
                "closed_cycle_count": 0,
                "closed_cycle_realized_net_pnl": _ZERO,
                "open_position_count": 0,
                "open_market_value": _ZERO,
                "open_cycle_marked_net_pnl": _ZERO,
            },
        )
        return row

    closed_cycles = tuple(getattr(result, "closed_cycles", ()))
    closed_rows: list[dict[str, object]] = []
    closed_realized = _ZERO
    for cycle in closed_cycles:
        net_pnl = Decimal(cycle.net_pnl)
        closed_realized += net_pnl
        row = sector_row(str(cycle.symbol))
        row["closed_cycle_count"] = int(row["closed_cycle_count"]) + 1
        row["closed_cycle_realized_net_pnl"] = Decimal(
            row["closed_cycle_realized_net_pnl"]
        ) + net_pnl
        closed_rows.append(
            {
                "symbol": str(cycle.symbol),
                "sector_id": row["sector_id"],
                "sector_name": row["sector_name"],
                "cycle_id": str(cycle.cycle_id),
                "slot_number": int(cycle.slot_number),
                "opened_at": cycle.opened_at,
                "closed_at": cycle.closed_at,
                "entry_cash": Decimal(cycle.entry_cash),
                "realized_net_pnl": net_pnl,
            }
        )

    position_rows: list[dict[str, object]] = []
    open_marked = _ZERO
    marked_market_value = _ZERO
    positions = tuple(getattr(result, "positions", ()))
    for position in positions:
        symbol = str(position.symbol)
        market_value = getattr(position, "market_value", None)
        if (
            market_value is None
            or getattr(position, "last_price", None) is None
            or getattr(position, "marked_at", None) is None
            or not bool(getattr(position, "mark_complete", False))
        ):
            reasons.append(f"UNRESOLVED_TERMINAL_MARK:{symbol}")
            continue
        market = Decimal(market_value)
        marked_pnl = Decimal(position.cumulative_cash_flow) + market
        marked_market_value += market
        open_marked += marked_pnl
        row = sector_row(symbol)
        row["open_position_count"] = int(row["open_position_count"]) + 1
        row["open_market_value"] = Decimal(row["open_market_value"]) + market
        row["open_cycle_marked_net_pnl"] = Decimal(
            row["open_cycle_marked_net_pnl"]
        ) + marked_pnl
        position_rows.append(
            {
                "symbol": symbol,
                "sector_id": row["sector_id"],
                "sector_name": row["sector_name"],
                "cycle_id": str(position.cycle_id),
                "slot_number": int(position.slot_number),
                "opened_at": position.opened_at,
                "quantity": int(position.quantity),
                "entry_cash": Decimal(position.entry_cash),
                "cumulative_cash_flow": Decimal(position.cumulative_cash_flow),
                "cumulative_fees": Decimal(position.cumulative_fees),
                "turnover_notional": Decimal(position.turnover_notional),
                "tactical_cycles_completed": int(
                    position.tactical_cycles_completed
                ),
                "last_price": Decimal(position.last_price),
                "market_value": market,
                "marked_at": position.marked_at,
                "marked_net_pnl": marked_pnl,
                "account_equity_fraction": (
                    _ZERO if terminal_equity == 0 else market / terminal_equity
                ),
                "invested_market_value_fraction": (
                    _ZERO if terminal_market == 0 else market / terminal_market
                ),
            }
        )

    pnl_identity_difference = total_net_pnl - closed_realized - open_marked
    cash_market_equity_difference = (
        terminal_equity - final_cash - terminal_market
    )
    terminal_cash_difference = terminal_cash - final_cash
    position_market_value_difference = terminal_market - marked_market_value
    for code, difference in (
        ("PNL_IDENTITY_MISMATCH", pnl_identity_difference),
        ("CASH_MARKET_EQUITY_MISMATCH", cash_market_equity_difference),
        ("FINAL_CASH_MISMATCH", terminal_cash_difference),
        ("POSITION_MARKET_VALUE_MISMATCH", position_market_value_difference),
    ):
        if difference != 0:
            reasons.append(code)

    sector_output: list[dict[str, object]] = []
    for sector_id, row in sorted(sector_rows.items()):
        total_attributed = Decimal(
            row["closed_cycle_realized_net_pnl"]
        ) + Decimal(row["open_cycle_marked_net_pnl"])
        sector_output.append(
            {
                **row,
                "total_attributed_net_pnl": total_attributed,
                "open_market_value_account_equity_fraction": (
                    _ZERO
                    if terminal_equity == 0
                    else Decimal(row["open_market_value"]) / terminal_equity
                ),
                "open_market_value_invested_fraction": (
                    _ZERO
                    if terminal_market == 0
                    else Decimal(row["open_market_value"]) / terminal_market
                ),
            }
        )

    by_symbol = sorted(
        position_rows,
        key=lambda row: (-Decimal(row["market_value"]), str(row["symbol"])),
    )
    by_sector = sorted(
        sector_output,
        key=lambda row: (
            -Decimal(row["open_market_value"]),
            str(row["sector_id"]),
        ),
    )
    invested_hhi = sum(
        (
            Decimal(row["invested_market_value_fraction"]) ** 2
            for row in position_rows
        ),
        _ZERO,
    )
    sector_invested_hhi = sum(
        (
            Decimal(row["open_market_value_invested_fraction"]) ** 2
            for row in sector_output
        ),
        _ZERO,
    )
    return {
        "schema": "chanlun-terminal-accounting-attribution",
        "status": "EVALUATED" if not reasons else "NOT_EVALUABLE",
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "sector_membership_mode": sector_membership_mode,
        "terminal": {
            "observed_at": terminal.observed_at,
            "initial_cash": initial_cash,
            "final_cash": final_cash,
            "cash": terminal_cash,
            "market_value": terminal_market,
            "equity": terminal_equity,
            "total_net_pnl": total_net_pnl,
        },
        "pnl_decomposition": {
            "closed_cycle_realized_net_pnl": closed_realized,
            "open_cycle_marked_net_pnl": open_marked,
            "pure_unrealized_net_pnl": None,
            "pure_unrealized_reason": (
                "OPEN_CYCLE_TACTICAL_AND_CORPORATE_CASH_FLOWS_REQUIRE_"
                "A_SEPARATE_COST_BASIS_LEDGER"
            ),
            "identity_difference": pnl_identity_difference,
        },
        "accounting_identity": {
            "cash_market_equity_difference": cash_market_equity_difference,
            "terminal_cash_difference": terminal_cash_difference,
            "position_market_value_difference": (
                position_market_value_difference
            ),
        },
        "concentration": {
            "open_position_count": len(position_rows),
            "max_symbol": None if not by_symbol else by_symbol[0]["symbol"],
            "max_symbol_equity_fraction": (
                _ZERO
                if not by_symbol
                else by_symbol[0]["account_equity_fraction"]
            ),
            "max_symbol_invested_fraction": (
                _ZERO
                if not by_symbol
                else by_symbol[0]["invested_market_value_fraction"]
            ),
            "symbol_invested_hhi": invested_hhi,
            "max_sector_id": (
                None if not by_sector else by_sector[0]["sector_id"]
            ),
            "max_sector_equity_fraction": (
                _ZERO
                if not by_sector
                else by_sector[0]["open_market_value_account_equity_fraction"]
            ),
            "max_sector_invested_fraction": (
                _ZERO
                if not by_sector
                else by_sector[0]["open_market_value_invested_fraction"]
            ),
            "sector_invested_hhi": sector_invested_hhi,
        },
        "closed_cycles": tuple(closed_rows),
        "open_positions": tuple(position_rows),
        "sector_attribution": tuple(sector_output),
        "disclosures": (
            "closed-cycle P&L is realised net cash after fees",
            "open-cycle marked P&L equals cumulative cycle cash flow plus terminal market value",
            "pure unrealised P&L is unresolved without a separate cost-basis ledger",
            "sector attribution uses the explicitly authorised current-member backfill",
        ),
    }


def _terminal_accounting_headline(
    attribution: Mapping[str, object],
) -> dict[str, object]:
    """Expose the terminal P&L dependency in the CLI completion receipt.

    The formal artifact already carries the complete attribution document.
    Historically the command-line receipt printed only aggregate metrics, so
    a positive mark-to-market return could be read without noticing that every
    closed strategic cycle had lost money.  This is a read-only projection of
    the same accounting identity; it never changes a signal, order or fill.
    """

    status = str(attribution.get("status", "NOT_EVALUABLE"))
    reason_codes = tuple(
        str(value) for value in attribution.get("reason_codes", ())
    )
    if status != "EVALUATED":
        return {
            "schema": "chanlun-terminal-accounting-headline",
            "status": "NOT_EVALUABLE",
            "reason_codes": reason_codes,
            "return_dependency_status": "NOT_EVALUABLE",
            "positive_total_depends_on_open_cycle_marks": None,
            "diagnostic_only": True,
            "decisions_unchanged": True,
            "live_status": "LIVE_DISABLED",
        }

    terminal = attribution.get("terminal")
    decomposition = attribution.get("pnl_decomposition")
    concentration = attribution.get("concentration")
    if not isinstance(terminal, Mapping):
        raise ValueError("evaluated terminal attribution is missing terminal")
    if not isinstance(decomposition, Mapping):
        raise ValueError(
            "evaluated terminal attribution is missing pnl decomposition"
        )
    if not isinstance(concentration, Mapping):
        raise ValueError(
            "evaluated terminal attribution is missing concentration"
        )

    initial_cash = Decimal(terminal["initial_cash"])
    total_net_pnl = Decimal(terminal["total_net_pnl"])
    closed_realized = Decimal(
        decomposition["closed_cycle_realized_net_pnl"]
    )
    open_marked = Decimal(decomposition["open_cycle_marked_net_pnl"])
    identity_difference = Decimal(decomposition["identity_difference"])
    if initial_cash <= 0:
        raise ValueError("terminal attribution initial cash must be positive")
    if identity_difference != 0 or total_net_pnl != closed_realized + open_marked:
        raise ValueError("terminal attribution P&L identity is inconsistent")

    depends_on_open_marks = (
        total_net_pnl > 0 and closed_realized <= 0 and open_marked > 0
    )
    if depends_on_open_marks:
        dependency_status = "POSITIVE_TOTAL_DEPENDS_ON_OPEN_CYCLE_MARKS"
    elif total_net_pnl > 0:
        dependency_status = "POSITIVE_TOTAL_SUPPORTED_BY_REALIZED_CYCLES"
    else:
        dependency_status = "NON_POSITIVE_TOTAL"

    return {
        "schema": "chanlun-terminal-accounting-headline",
        "status": "EVALUATED",
        "reason_codes": reason_codes,
        "return_dependency_status": dependency_status,
        "positive_total_depends_on_open_cycle_marks": depends_on_open_marks,
        "total_net_pnl": total_net_pnl,
        "total_net_return_on_initial_cash": total_net_pnl / initial_cash,
        "closed_cycle_realized_net_pnl": closed_realized,
        "closed_cycle_realized_return_on_initial_cash": (
            closed_realized / initial_cash
        ),
        "open_cycle_marked_net_pnl": open_marked,
        "open_cycle_marked_return_on_initial_cash": open_marked / initial_cash,
        "open_position_count": int(concentration["open_position_count"]),
        "max_open_symbol": concentration.get("max_symbol"),
        "max_open_symbol_equity_fraction": Decimal(
            concentration["max_symbol_equity_fraction"]
        ),
        "max_open_symbol_invested_fraction": Decimal(
            concentration["max_symbol_invested_fraction"]
        ),
        "pure_unrealized_net_pnl": None,
        "pure_unrealized_reason": decomposition.get(
            "pure_unrealized_reason"
        ),
        "diagnostic_only": True,
        "decisions_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


def _performance_headline(
    research_result: Mapping[str, object],
    benchmarks: Sequence[Mapping[str, object]],
    *,
    terminal_headline: Mapping[str, object],
) -> dict[str, object]:
    """Make holdout, benchmark and annualisation caveats unavoidable in CLI.

    This projection consumes only the already-built formal report fields.  It
    deliberately does not create another equity curve or benchmark series.
    """

    performance_status = str(
        research_result.get("performance_status", "NOT_EVALUABLE")
    )
    performance_reason = research_result.get("performance_reason")
    if not performance_status.startswith("EVALUATED"):
        return {
            "schema": "chanlun-performance-headline",
            "status": "NOT_EVALUABLE",
            "reason": performance_reason,
            "relative_performance_status": "NOT_EVALUABLE",
            "diagnostic_only": True,
            "decisions_unchanged": True,
            "live_status": "LIVE_DISABLED",
        }

    daily = research_result.get("daily_metrics")
    replay = research_result.get("replay")
    periods = research_result.get("periods")
    if not isinstance(daily, Mapping):
        raise ValueError("evaluated research result is missing daily metrics")
    if not isinstance(replay, Mapping):
        raise ValueError("evaluated research result is missing replay")
    replay_metrics = replay.get("metrics")
    if not isinstance(replay_metrics, Mapping):
        raise ValueError("evaluated research result is missing replay metrics")
    if not isinstance(periods, Mapping):
        raise ValueError("evaluated research result is missing periods")
    split_periods = periods.get("train_validation_holdout")
    if not isinstance(split_periods, Mapping):
        raise ValueError("evaluated research result is missing split periods")
    holdout = split_periods.get("FINAL_HOLDOUT_20")
    if not isinstance(holdout, Mapping) or holdout.get("status") != "EVALUATED":
        raise ValueError("evaluated research result is missing final holdout")

    def session_date(value: object) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    strategy_start = session_date(daily["start"])
    strategy_end = session_date(daily["end"])
    strategy_observations = int(daily["observations"])
    strategy_return = Decimal(daily["net_return"])
    strategy_drawdown = Decimal(daily["max_drawdown"])
    strategy_sharpe = (
        None
        if daily.get("sharpe") is None
        else Decimal(daily["sharpe"])
    )
    daily_annualized = (
        None
        if daily.get("annualized_return") is None
        else Decimal(daily["annualized_return"])
    )
    event_annualized = (
        None
        if replay_metrics.get("annualized_return") is None
        else Decimal(replay_metrics["annualized_return"])
    )
    warnings = tuple(str(value) for value in replay_metrics.get("warnings", ()))
    calendar_span_days = (strategy_end - strategy_start).days
    insufficient_span = (
        "INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION" in warnings
    )
    if insufficient_span:
        if calendar_span_days >= 365 or event_annualized is not None:
            raise ValueError("annualization warning contradicts observed span")
        annualization_status = (
            "MATHEMATICAL_ESTIMATE_BELOW_FULL_CALENDAR_YEAR"
        )
    else:
        if calendar_span_days < 365:
            raise ValueError("sub-year result is missing annualization warning")
        annualization_status = "FULL_CALENDAR_YEAR_OBSERVED"

    holdout_start = session_date(holdout["start"])
    holdout_end = session_date(holdout["end"])
    holdout_return = Decimal(holdout["net_return"])
    if not (
        strategy_start <= holdout_start <= holdout_end == strategy_end
    ):
        raise ValueError("final holdout range is inconsistent with strategy range")

    if not benchmarks:
        raise ValueError("relative performance requires at least one benchmark")
    benchmark_rows: list[dict[str, object]] = []
    for raw in benchmarks:
        if not isinstance(raw, Mapping):
            raise ValueError("benchmark row must be a mapping")
        metrics = raw.get("metrics")
        if not isinstance(metrics, Mapping) or metrics.get("status") != "EVALUATED":
            raise ValueError("benchmark metrics are not evaluable")
        if (
            session_date(metrics["start"]) != strategy_start
            or session_date(metrics["end"]) != strategy_end
            or int(metrics["observations"]) != strategy_observations
        ):
            raise ValueError("benchmark range does not match strategy range")
        benchmark_return = Decimal(metrics["net_return"])
        benchmark_drawdown = Decimal(metrics["max_drawdown"])
        benchmark_sharpe = (
            None
            if metrics.get("sharpe") is None
            else Decimal(metrics["sharpe"])
        )
        benchmark_rows.append(
            {
                "symbol": str(raw["symbol"]),
                "definition": str(raw.get("definition", "")),
                "net_return": benchmark_return,
                "strategy_excess_net_return": (
                    strategy_return - benchmark_return
                ),
                "max_drawdown": benchmark_drawdown,
                "strategy_minus_benchmark_max_drawdown": (
                    strategy_drawdown - benchmark_drawdown
                ),
                "sharpe": benchmark_sharpe,
                "strategy_minus_benchmark_sharpe": (
                    None
                    if strategy_sharpe is None or benchmark_sharpe is None
                    else strategy_sharpe - benchmark_sharpe
                ),
            }
        )

    underperformed_all = all(
        Decimal(row["strategy_excess_net_return"]) < 0
        for row in benchmark_rows
    )
    holdout_negative = holdout_return < 0
    if strategy_return > 0 and holdout_negative and underperformed_all:
        relative_status = (
            "POSITIVE_FULL_SAMPLE_BUT_NEGATIVE_HOLDOUT_AND_"
            "UNDERPERFORMS_ALL_BENCHMARKS"
        )
    elif strategy_return > 0 and underperformed_all:
        relative_status = "POSITIVE_FULL_SAMPLE_BUT_UNDERPERFORMS_ALL_BENCHMARKS"
    elif strategy_return <= 0:
        relative_status = "NON_POSITIVE_FULL_SAMPLE"
    else:
        relative_status = "MIXED_OR_POSITIVE_RELATIVE_PERFORMANCE"

    return {
        "schema": "chanlun-performance-headline",
        "status": performance_status,
        "reason": performance_reason,
        "relative_performance_status": relative_status,
        "strategy_net_return": strategy_return,
        "final_holdout_net_return": holdout_return,
        "final_holdout_negative": holdout_negative,
        "strategy_underperformed_all_benchmarks": underperformed_all,
        "benchmarks": tuple(benchmark_rows),
        "annualization_status": annualization_status,
        "calendar_span_days": calendar_span_days,
        "daily_annualized_return_estimate": daily_annualized,
        "event_annualized_return": event_annualized,
        "return_dependency_status": terminal_headline.get(
            "return_dependency_status"
        ),
        "diagnostic_only": True,
        "decisions_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


def _performance_adjudication(result) -> tuple[str, str | None]:
    """Name the actual failed gate instead of calling every failure 'empty'."""

    metrics = result.metrics
    if metrics.performance_evaluable:
        sample_reasons = []
        if getattr(metrics, "strategic_sample_insufficient", False):
            sample_reasons.append("STRATEGIC_SAMPLE_BELOW_100")
        if getattr(metrics, "tactical_sample_insufficient", False):
            sample_reasons.append("TACTICAL_SAMPLE_BELOW_200")
        if sample_reasons:
            return "EVALUATED_SAMPLE_INSUFFICIENT", "+".join(sample_reasons)
        return "EVALUATED", None
    if metrics.empty_replay:
        return "NOT_EVALUABLE_EMPTY_REPLAY", "NO_SPEC_COMPLIANT_ORDER_OR_FILL"
    if not metrics.ledger_valid:
        return (
            "NOT_EVALUABLE_UNRESOLVED_LEDGER",
            "UNRESOLVED_FACTS_OR_VALUATIONS_PRESENT",
        )
    if metrics.strategic_cycle_count == 0:
        return (
            "NOT_EVALUABLE_NO_CLOSED_STRATEGIC_CYCLE",
            "NO_CLOSED_STRATEGIC_CYCLE",
        )
    return "NOT_EVALUABLE", "PERFORMANCE_GATE_FAILED"


def _periods(points: Sequence[tuple[date, Decimal]]) -> dict[str, object]:
    if len(points) < 3:
        return {}
    size = len(points)
    train = max(2, int(size * 0.6))
    validation = max(train + 1, int(size * 0.8))
    slices = {
        "TRAIN_60": points[:train],
        "VALIDATION_20": points[train - 1 : validation],
        "FINAL_HOLDOUT_20": points[validation - 1 :],
    }
    annual = {
        str(year): tuple(row for row in points if row[0].year == year)
        for year in sorted({row[0].year for row in points})
    }
    quarterly: dict[str, tuple[tuple[date, Decimal], ...]] = {}
    for row in points:
        key = f"{row[0].year}-Q{(row[0].month - 1) // 3 + 1}"
        quarterly.setdefault(key, ())
        quarterly[key] = (*quarterly[key], row)
    return {
        "split_policy": "CHRONOLOGICAL_60_20_20_NO_REFIT",
        "train_validation_holdout": {
            key: _series_metrics(value) for key, value in slices.items()
        },
        "calendar_years": {key: _series_metrics(value) for key, value in annual.items()},
        "walk_forward_quarters": {
            key: _series_metrics(value) for key, value in quarterly.items()
        },
    }


def _benchmark(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    frame = load_qmt_frame(symbol, "1m", start_at=start, end_at=end)
    if frame.empty:
        return {"symbol": symbol, "status": "NO_LOCAL_DATA"}
    executable = frame[frame["date"].dt.time != time(9, 30)]
    daily = tuple(
        (
            session,
            Decimal(str(rows.iloc[-1]["raw_close"])),
        )
        for session, rows in executable.groupby(executable["date"].dt.date, sort=True)
    )
    return {
        "symbol": symbol,
        "definition": "RAW_CLOSE_PRICE_RETURN_NO_DIVIDEND_REINVESTMENT",
        "metrics": _series_metrics(daily),
    }


def _human_review_alerts(
    *,
    signals: Mapping[datetime, Sequence[Signal]],
    candidate_audit: Sequence[Mapping[str, object]],
    contexts: Mapping[str, SymbolContext],
) -> tuple[HumanReviewAlert, ...]:
    """Convert machine hints into a zero-authority human review queue."""

    parameters = human_review_screening_parameters()
    accepted_audits = {
        (str(row["symbol"]), str(_jsonable(row["decision_at"]))): row
        for row in candidate_audit
        if row.get("accepted") is True and row.get("decision_at") is not None
    }
    latest_entry: dict[str, HumanReviewAlert] = {}
    alert_types = {
        "ENTRY": "POSSIBLE_30M_BUY",
        "STRATEGIC_EXIT": "POSSIBLE_30M_EXIT",
        "TACTICAL_SELL": "POSSIBLE_5M_TACTICAL_SELL",
        "TACTICAL_BUYBACK": "POSSIBLE_5M_TACTICAL_BUYBACK",
    }
    output: list[HumanReviewAlert] = []
    ordered = sorted(
        (
            (decision_at, signal)
            for decision_at, rows in signals.items()
            for signal in rows
            if signal.kind in alert_types
        ),
        key=lambda value: (
            value[0],
            value[1].symbol,
            value[1].kind,
            value[1].identity,
        ),
    )
    for decision_at, signal in ordered:
        audit = (
            accepted_audits.get((signal.symbol, decision_at.isoformat()))
            if signal.kind == "ENTRY"
            else None
        )
        prior = latest_entry.get(signal.symbol)
        if signal.kind == "ENTRY" and audit is None:
            raise RuntimeError("accepted entry signal has no candidate audit row")
        raw_confidence = (
            "UNRESOLVED"
            if audit is None
            else str(audit.get("technical_approximation_confidence") or "UNRESOLVED")
        )
        confidence = (
            raw_confidence
            if raw_confidence in {"HIGH", "MEDIUM", "LOW", "UNRESOLVED"}
            else "UNRESOLVED"
        )
        market_gate = (
            str(audit.get("market_risk_gate") or "UNRESOLVED")
            if audit is not None
            else ("UNRESOLVED" if prior is None else prior.market_risk_gate)
        )
        symbol_gate = (
            str(audit.get("symbol_risk_gate") or "UNRESOLVED")
            if audit is not None
            else ("UNRESOLVED" if prior is None else prior.symbol_risk_gate)
        )
        sector_gate = (
            str(audit.get("sector_risk_gate") or "UNRESOLVED")
            if audit is not None
            else "UNRESOLVED"
        )
        exact_green = bool(audit is not None and audit.get("exact_green"))
        warning_codes = list(
            ()
            if audit is None
            else tuple(audit.get("technical_approximation_warning_codes") or ())
        )
        if audit is not None:
            warning_codes.extend(
                tuple(audit.get("strict_reason_codes_reclassified_as_warnings") or ())
            )
        else:
            warning_codes.append("HUMAN_MUST_CONFIRM_NON_ENTRY_STRUCTURE_HINT")
        if market_gate != "GREEN":
            warning_codes.append(f"MARKET_RISK_{market_gate}")
        if sector_gate != "GREEN":
            warning_codes.append(f"SECTOR_RISK_{sector_gate}")
        if symbol_gate != "GREEN":
            warning_codes.append(f"SYMBOL_RISK_{symbol_gate}")
        warnings = tuple(dict.fromkeys(str(value) for value in warning_codes))
        context = contexts[signal.symbol]
        row = _row_at_or_before(context, decision_at)
        reference_price = None if row is None else Decimal(str(row["close"]))
        invalidation = (
            signal.chain.structural_invalidation_price
            if isinstance(signal.chain, ApproximateChanlunEntryChain)
            else (None if prior is None else prior.structural_invalidation_price)
        )
        structure_snapshot_id = (
            signal.structure_snapshot_id
            or (None if prior is None else prior.structure_snapshot_id)
            or signal.identity
        )
        sector_id = (
            None if audit is None else str(audit.get("sector_id") or "") or None
        )
        if sector_id is None and prior is not None:
            sector_id = prior.sector_id
        sector_ranking_evidence = (
            sector_ranking_review_evidence_from_candidate_audit(
                audit,
                observed_at=decision_at,
            )
            if audit is not None
            else (None if prior is None else prior.sector_ranking_evidence)
        )
        source_ids = tuple(
            dict.fromkeys(
                str(value)
                for value in (
                    signal.identity,
                    structure_snapshot_id,
                    *signal.frozen_fact_ids,
                    *signal.selection_fact_ids,
                    *signal.risk_fact_ids,
                    parameters.parameter_set_id,
                    (
                        None
                        if sector_ranking_evidence is None
                        else sector_ranking_evidence.evidence_id
                    ),
                )
                if value
            )
        )
        alert = HumanReviewAlert(
            symbol=signal.symbol,
            alert_type=alert_types[signal.kind],  # type: ignore[arg-type]
            signal_at=signal.observed_at,
            review_available_at=decision_at,
            source_point_id=signal.identity,
            structure_snapshot_id=structure_snapshot_id,
            sector_id=sector_id,
            confidence=confidence,  # type: ignore[arg-type]
            review_priority=review_priority(
                confidence=confidence,  # type: ignore[arg-type]
                exact_green=exact_green,
                market_risk_gate=market_gate,
                sector_risk_gate=sector_gate,
                symbol_risk_gate=symbol_gate,
                warning_count=len(warnings),
                parameters=parameters,
            ),
            reference_price=reference_price,
            structural_invalidation_price=invalidation,
            market_risk_gate=market_gate,
            sector_risk_gate=sector_gate,
            symbol_risk_gate=symbol_gate,
            warning_codes=warnings,
            source_fact_ids=source_ids,
            screening_parameter_set_id=parameters.parameter_set_id,
            technical_approximation_parameter_set_id=(
                parameters.technical_approximation_parameter_set_id
            ),
            sector_ranking_evidence=sector_ranking_evidence,
        )
        output.append(alert)
        if signal.kind == "ENTRY":
            latest_entry[signal.symbol] = alert
    return tuple(
        sorted(
            output,
            key=lambda value: (
                -value.review_priority,
                value.review_available_at,
                value.symbol,
                value.alert_type,
                value.candidate_id,
            ),
        )
    )


def _review_price_bars(context: SymbolContext) -> tuple[ReviewPriceBar, ...]:
    return tuple(
        ReviewPriceBar(
            observed_at=pd.Timestamp(row.date).to_pydatetime(),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
        )
        for row in context.executable_rows.itertuples(index=False)
    )


def _human_review_alert_payload(alert: HumanReviewAlert) -> dict[str, object]:
    """Serialize nested evidence with its independently verifiable identity."""

    return {
        **human_review_alert_document(alert),
        "candidate_id": alert.candidate_id,
        "signal_lifecycle_id": alert.signal_lifecycle_id,
    }


def _human_review_report(
    *,
    args: argparse.Namespace,
    root: Path,
    contexts: Mapping[str, SymbolContext],
    trigger: SectorFirstTriggerLedger,
    query_plan: Mapping[str, object],
    manifest: Mapping[str, object],
    trigger_path: Path,
    query_plan_file_sha256: str | None,
    manifest_file_sha256: str,
    current_catalog_entry_sha256: str | None,
    current_catalog_ledger_sha256: str | None,
    signals: Mapping[datetime, Sequence[Signal]],
    candidate_audit: Sequence[Mapping[str, object]],
    signal_counts: Mapping[str, int],
    effective_start: date,
    requested_end: date,
    decision_source_snapshot: Mapping[str, object],
) -> dict[str, object]:
    parameters = human_review_screening_parameters()
    alerts = _human_review_alerts(
        signals=signals,
        candidate_audit=candidate_audit,
        contexts=contexts,
    )
    entry_alerts = tuple(
        alert for alert in alerts if alert.alert_type == "POSSIBLE_30M_BUY"
    )
    bars_by_symbol = {
        symbol: _review_price_bars(contexts[symbol])
        for symbol in sorted({alert.symbol for alert in entry_alerts})
    }
    observations = tuple(
        observation
        for alert in entry_alerts
        for observation in evaluate_review_alert(alert, bars_by_symbol[alert.symbol])
    )
    hard_rejections = tuple(
        row for row in candidate_audit if row.get("accepted") is not True
    )
    report: dict[str, object] = {
        "schema": "chanlun-human-review-screen",
        "result_label": "RECENT_YEAR_HUMAN_REVIEW_SCREEN_EVENT_STUDY",
        "sample": {
            "effective_start": effective_start,
            "requested_end": requested_end,
            "minimum_bar_period": "1m",
        },
        "division_of_responsibility": {
            "program": (
                "QMT sector trigger and current/PIT membership screening",
                "tradeability, liquidity and higher-timeframe risk hints",
                "approximate 30m/5m/1m structural location and evidence ranking",
                "candidate rejection reasons and 5/10/20-session event study",
            ),
            "human": (
                "confirm center and recursive level",
                "combine same-level and center decomposition",
                "confirm trend type and exact buy/sell point",
                "define invalidation and decide whether to paper-observe",
            ),
        },
        "screening_contract": parameters.document(),
        "review_queue": tuple(_human_review_alert_payload(alert) for alert in alerts),
        "candidate_audit": tuple(candidate_audit),
        "hard_rejections": hard_rejections,
        "event_study": {
            "evaluated_alert_type": "POSSIBLE_30M_BUY",
            "horizon_definition": (
                "the 5th/10th/20th complete trading session after the review date; "
                "the signal session is excluded"
            ),
            "false_positive_proxy_definition": (
                "structural invalidation observed or horizon close return <= 0; "
                "this is a screening diagnostic, not a trade outcome"
            ),
            "observations": tuple(asdict(value) for value in observations),
            "summary": summarize_event_study(observations),
        },
        "signal_counts": dict(signal_counts),
        "candidate_funnel": {
            "all_market_sector_classified_symbols": query_plan.get(
                "requested_symbol_count", 5201
            ),
            "terminal_recursive_potential_symbols": query_plan.get(
                "potential_symbol_count"
            ),
            "causally_rescanned_symbols": len(manifest["symbols"]),
            "technical_hint_pass_count": signal_counts.get(
                "approximate_entry_technical_pass", 0
            ),
            "review_candidate_count": len(entry_alerts),
            "all_review_alert_count": len(alerts),
            "hard_rejection_count": len(hard_rejections),
            "orders_created": 0,
            "fills_created": 0,
        },
        "scope": {
            "root": root,
            "symbols": tuple(sorted(contexts)),
            "sector_count": len(trigger.sector_source_revisions),
            "selection_order": trigger.selection_order,
            "three_program_mode": "DISABLED_USER_AUTHORIZED",
        },
        "input_hashes": {
            "trigger_ledger": _sha256_file(trigger_path),
            "current_catalog_entry": current_catalog_entry_sha256,
            "current_catalog_ledger": current_catalog_ledger_sha256,
            "terminal_query_plan": (
                query_plan_file_sha256
            ),
            "direct_manifest": manifest_file_sha256,
            "pit_snapshot": _sha256_file(args.pit_snapshot.resolve()),
        },
        "decision_source_snapshot": decision_source_snapshot,
        "data_caveats": (
            "historical QMT sector membership is unavailable; the captured current membership is backfilled over the recent-year study only",
            "the individual three-program is disabled by user instruction",
            "technical structure is an approximate pointer for human review and is not asserted as the unique Chanlun decomposition",
            "only completed 1m-or-higher bars are used; tick, order book and invented fill data are excluded",
        ),
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "automated_order_authorized": False,
        "human_confirmation_required": True,
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    report["content_sha256"] = sha256_json(_jsonable(report))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    # Capture the identity of the implementation loaded for this run.  The
    # atomic publisher verifies the same identity immediately before replace,
    # so a long replay cannot accidentally attest source written mid-run.
    run_decision_source_snapshot = _decision_source_snapshot()
    run_start_module_names = frozenset(sys.modules)
    if args.initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    if args.approximate_technical_points and not args.no_three_program:
        raise ValueError(
            "technical point approximation is available only in the explicit "
            "no-three-program research variant"
        )
    if args.human_review_only:
        if not args.no_three_program or not args.approximate_technical_points:
            raise ValueError(
                "human review mode requires --no-three-program and "
                "--approximate-technical-points"
            )
    if args.root == DEFAULT_ROOT and args.no_three_program:
        args.root = DEFAULT_RECENT_ROOT
    if args.output == DEFAULT_ROOT / "full_market_research_backtest.json":
        args.output = args.root / (
            "human_review_screen.json"
            if args.human_review_only
            else "approximate_technical_backtest.json"
            if args.approximate_technical_points
            else "full_market_research_backtest.json"
        )
    technical_approximation = (
        technical_approximation_parameters()
        if args.approximate_technical_points
        else None
    )
    os.environ[QMT_LOCAL_DATA_ENV] = str(args.qmt_local_data_dir.resolve())
    root = args.root.resolve()
    trigger_path = root / "sector_first_trigger_ledger.pkl"
    research_path = root / "research_approximation_ledger.pkl"
    manifest_path = root / "direct_extract_manifest.json"
    trigger = _load_pickle(trigger_path, SectorFirstTriggerLedger)
    research = (
        None
        if args.no_three_program
        else _load_pickle(research_path, ResearchApproximationLedger)
    )
    manifest_payload = manifest_path.read_bytes()
    manifest_file_sha256 = (
        "sha256:" + hashlib.sha256(manifest_payload).hexdigest()
    )
    manifest = json.loads(manifest_payload.decode("utf-8"))
    query_plan_path = root / "terminal_query_plan.json"
    query_plan_payload = (
        query_plan_path.read_bytes() if query_plan_path.is_file() else None
    )
    query_plan_file_sha256 = (
        None
        if query_plan_payload is None
        else "sha256:" + hashlib.sha256(query_plan_payload).hexdigest()
    )
    query_plan = (
        {}
        if query_plan_payload is None
        else json.loads(query_plan_payload.decode("utf-8"))
    )
    if not manifest.get("complete"):
        raise RuntimeError("direct causal extraction is incomplete")
    if manifest.get("schema") != "chanlun-sector-first-direct-extract":
        raise RuntimeError("direct causal extraction lacks checkpoint bindings")
    _require_current_direct_algorithm(manifest, trigger)
    if (
        research is not None
        and research.trigger_ledger_sha256 != _sha256_file(trigger_path)
    ):
        raise RuntimeError("research and sector ledgers are not bound")
    current_sector_by_code: dict[str, str] = {}
    current_sector_members_by_id: dict[str, tuple[str, ...]] = {}
    current_sector_name_by_id: dict[str, str] = {}
    current_catalog_entry_sha256: str | None = None
    current_catalog_ledger_sha256: str | None = None
    if args.no_three_program:
        parameters = recent_year_research_parameters()
        trigger_sha256 = _sha256_file(trigger_path)
        query_plan_sha256 = query_plan_file_sha256
        if query_plan_sha256 is None:
            raise RuntimeError("recent-year terminal query plan is unavailable")
        if (
            trigger.selection_path != RECENT_YEAR_SELECTION_PATH
            or manifest.get("selection_path") != RECENT_YEAR_SELECTION_PATH
            or manifest.get("data_grade") != "RESEARCH_ONLY"
            or query_plan.get("selection_path") != RECENT_YEAR_SELECTION_PATH
            or query_plan.get("three_program_mode")
            != "DISABLED_USER_AUTHORIZED"
            or query_plan.get("research_parameter_set_id")
            != parameters.parameter_set_id
            or query_plan.get("schema")
            != "chanlun-sector-first-terminal-query-plan"
            or manifest.get("algorithm", {}).get("revision")
            != trigger.algorithm_revision
            or query_plan.get("algorithm_revision")
            != trigger.algorithm_revision
            or query_plan.get("trigger_ledger_sha256") != trigger_sha256
            or manifest.get("inputs", {}).get("trigger_ledger_sha256")
            != trigger_sha256
            or manifest.get("inputs", {}).get("query_plan_sha256")
            != query_plan_sha256
        ):
            raise RuntimeError("recent-year current-sector artifacts are not bound")
        catalog_path = args.current_catalog_ledger.resolve()
        catalog = load_sector_ledger(catalog_path)
        entries = tuple(catalog["entries"])
        if not entries:
            raise RuntimeError("current QMT sector ledger is empty")
        expected_entry_sha256 = str(query_plan.get("current_catalog_entry_sha256"))
        matching_entries = tuple(
            (index, value)
            for index, value in enumerate(entries)
            if value.get("entry_sha256") == expected_entry_sha256
        )
        if len(matching_entries) != 1:
            raise RuntimeError("bound QMT catalog entry is absent or non-unique")
        entry_index, entry = matching_entries[0]
        if trigger.selection_order[:2] != (
            "QMT_CURRENT_SECTOR_TRIGGER",
            "QMT_CURRENT_MEMBERS_BACKFILLED_USER_AUTHORIZED",
        ):
            raise RuntimeError("current-sector trigger is not historical backfill")
        current_catalog_entry_sha256 = str(entry["entry_sha256"])
        current_catalog_ledger_sha256 = _sha256_file(catalog_path)
        # An append-only catalog legitimately gains later daily captures.
        # Bind historical artifacts to the validated ledger prefix ending at
        # their exact entry, instead of requiring the mutable file-tail hash.
        stable_bound_catalog = {
            "schema": QMT_SECTOR_LEDGER_SCHEMA,
            "entries": tuple(entries[: entry_index + 1]),
        }
        bound_catalog_document = {
            **stable_bound_catalog,
            "content_sha256": sha256_json(stable_bound_catalog),
        }
        bound_catalog_bytes = (
            json.dumps(
                _jsonable(bound_catalog_document),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        bound_catalog_ledger_sha256 = (
            "sha256:" + hashlib.sha256(bound_catalog_bytes).hexdigest()
        )
        if (
            current_catalog_entry_sha256 != trigger.sector_scope_sha256
            or current_catalog_entry_sha256
            != query_plan.get("current_catalog_entry_sha256")
            or bound_catalog_ledger_sha256
            != query_plan.get("current_catalog_ledger_sha256")
            or manifest.get("inputs", {}).get("current_catalog_entry_sha256")
            != current_catalog_entry_sha256
            or manifest.get("inputs", {}).get("current_catalog_ledger_sha256")
            != bound_catalog_ledger_sha256
        ):
            raise RuntimeError("current QMT catalog bindings changed")
        for row in entry["sectors"]:
            sector_id = str(row["sector_id"])
            sector_name = str(row.get("name") or sector_id)
            member_codes = tuple(
                sorted({str(value) for value in row["member_codes"]})
            )
            if not member_codes:
                # The captured QMT taxonomy may contain named empty groups.
                # They were already excluded from the trigger ledger and can
                # never own a stock candidate; keep that local exclusion
                # instead of turning one empty group into a market-wide halt.
                continue
            if sector_id in current_sector_members_by_id:
                raise RuntimeError(f"current QMT sector is duplicated: {sector_id}")
            current_sector_members_by_id[sector_id] = member_codes
            current_sector_name_by_id[sector_id] = sector_name
            for code in member_codes:
                previous = current_sector_by_code.setdefault(code, sector_id)
                if previous != sector_id:
                    raise RuntimeError(
                        f"current QMT member belongs to multiple sectors: {code}"
                    )
    elif manifest.get("selection_path") != "INDIVIDUAL_THREE_PROGRAM":
        raise RuntimeError("direct manifest selection path changed")
    contexts: dict[str, SymbolContext] = {}
    for code in sorted(manifest["symbols"]):
        relative_path, checkpoint_sha256, checkpoint_size = (
            _direct_checkpoint_binding(manifest, code)
        )
        path = root / relative_path
        facts = _load_bound_direct_pickle(
            path,
            SectorFirstDirectSymbolFacts,
            expected_sha256=checkpoint_sha256,
            expected_size_bytes=checkpoint_size,
        )
        contexts[code] = _load_context(facts)
    if not contexts:
        raise RuntimeError("causal extraction supplied no auditable symbol")
    effective_start = min(
        value.facts.effective_start for value in contexts.values()
    )
    requested_end = max(
        value.facts.requested_end for value in contexts.values()
    )
    market_start = _market_history_start(contexts)
    market_end = datetime.combine(requested_end, time(15), tzinfo=CN)
    sector_composite_source: CurrentQmtGics3CompositeReplaySource | None = None
    if args.no_three_program:
        pit_index = PITMetadataIndex(load_snapshot(args.pit_snapshot.resolve()))
        sector_member_codes = tuple(
            sorted(
                {
                    code
                    for members in current_sector_members_by_id.values()
                    for code in members
                }
            )
        )
        sector_composite_source = CurrentQmtGics3CompositeReplaySource(
            data_dir=args.qmt_local_data_dir,
            start_at=datetime.combine(
                recent_year_research_parameters().warmup_start,
                time(9, 30),
                tzinfo=CN,
            ),
            end_at=market_end,
            factors_by_code={
                code: pit_index.factors_for(code) for code in sector_member_codes
            },
        )
    market_frame = load_qmt_frame("SH.000001", "1m", start_at=market_start, end_at=market_end)
    market_daily_frame = load_qmt_daily_frame(
        "SH.000001",
        start_at=datetime.combine(market_start.date(), time(0), tzinfo=CN),
        end_at=market_end,
    )
    secondary_market_daily = load_qmt_daily_frame(
        "SH.000300",
        start_at=datetime.combine(market_start.date(), time(0), tzinfo=CN),
        end_at=market_end,
    )
    market_calendar_sessions, market_calendar_evidence = (
        _reconciled_market_calendar(
            market_daily_frame,
            secondary_market_daily,
        )
    )
    replay_sessions = tuple(
        sorted(
            {
                pd.Timestamp(value).date()
                for value in market_frame["date"]
                if effective_start <= pd.Timestamp(value).date() <= requested_end
            }
        )
    )
    grids = _market_grids(replay_sessions)
    grid_decisions = tuple(value[0] for value in grids)
    signals, candidate_audit, signal_counts = _build_signals(
        contexts=contexts,
        trigger=trigger,
        research=research,
        current_sector_by_code=current_sector_by_code,
        current_sector_members_by_id=current_sector_members_by_id,
        current_catalog_entry_sha256=current_catalog_entry_sha256,
        sector_composite_source=sector_composite_source,
        market_frame=market_frame,
        market_daily_frame=market_daily_frame,
        market_sessions=market_calendar_sessions,
        grid_decisions=grid_decisions,
        initial_cash=args.initial_cash,
        signal_not_before=None,
        technical_approximation=technical_approximation,
    )
    if args.human_review_only:
        report = _human_review_report(
            args=args,
            root=root,
            contexts=contexts,
            trigger=trigger,
            query_plan=query_plan,
            manifest=manifest,
            trigger_path=trigger_path,
            query_plan_file_sha256=query_plan_file_sha256,
            manifest_file_sha256=manifest_file_sha256,
            current_catalog_entry_sha256=current_catalog_entry_sha256,
            current_catalog_ledger_sha256=current_catalog_ledger_sha256,
            signals=signals,
            candidate_audit=candidate_audit,
            signal_counts=signal_counts,
            effective_start=effective_start,
            requested_end=requested_end,
            decision_source_snapshot=run_decision_source_snapshot,
        )
        _atomic_json(
            args.output.resolve(),
            report,
            expected_decision_source_snapshot=run_decision_source_snapshot,
            loaded_replay_module_names=(
                _REPLAY_IMPORT_MODULE_NAMES
                | (frozenset(sys.modules) - run_start_module_names)
            ),
        )
        print(
            json.dumps(
                _jsonable(
                    {
                        "output": args.output.resolve(),
                        "result_label": report["result_label"],
                        "candidate_funnel": report["candidate_funnel"],
                        "event_study_summary": report["event_study"]["summary"],
                        "orders_created": 0,
                        "fills_created": 0,
                        "highest_status": "REVIEW_REQUIRED",
                        "live_status": "LIVE_DISABLED",
                        "content_sha256": report["content_sha256"],
                    }
                ),
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 0
    active_state: dict[str, dict[str, Signal]] = {
        "strategic": {},
        "tactical": {},
    }
    (
        batches,
        _next_active_state,
        result,
        scheduler_causality_audit,
    ) = _run_historical_session_checkpoint_replay(
        contexts=contexts,
        grids=grids,
        signals=signals,
        research=research,
        initial_cash=args.initial_cash,
        technical_approximation=technical_approximation,
        initial_strategic_active=active_state["strategic"],
        initial_tactical_active=active_state["tactical"],
    )
    tactical_execution_audit = _tactical_execution_audit(
        signals=signals,
        replay_result=result,
    )
    ablation_results, ablation_scheduler_causality_audits = (
        _run_historical_causal_ablations(
            contexts=contexts,
            grids=grids,
            signals=signals,
            research=research,
            initial_cash=args.initial_cash,
            technical_approximation=technical_approximation,
            initial_strategic_active=active_state["strategic"],
            initial_tactical_active=active_state["tactical"],
        )
    )
    no_tactical = ablation_results["NO_TACTICAL"]
    exact_green = ablation_results["EXACT_GREEN_HIGHER_TIMEFRAME_ONLY"]
    daily = _daily_equity(result)
    accounting_curve_metrics = _series_metrics(daily)
    performance_status, performance_reason = _performance_adjudication(result)
    strategy_curve_metrics: dict[str, object] = (
        {
            **accounting_curve_metrics,
            "status": performance_status,
            "adjudication_reason": performance_reason,
        }
        if result.metrics.performance_evaluable
        else {
            "status": performance_status,
            "reason": performance_reason,
            "observations": len(daily),
            "accounting_identity_net_return": accounting_curve_metrics.get(
                "net_return"
            ),
            "accounting_identity_max_drawdown": accounting_curve_metrics.get(
                "max_drawdown"
            ),
        }
    )
    strict_empty = StrictMultiSymbolReplayEngine(
        initial_cash=args.initial_cash,
        started_at=grids[0][0],
        fee_model=_fee_model(grids[0][0].date()),
        contract=(
            research_sector_technical_direct_replay_contract()
            if research is None
            else research_individual_direct_replay_contract(
                research.parameters.parameter_set_id
            )
        ),
    ).replay(())
    current_mode = research is None
    approximate_mode = technical_approximation is not None
    replay_document = asdict(result)
    terminal_accounting_attribution = _terminal_accounting_attribution(
        result,
        sector_by_symbol=current_sector_by_code,
        sector_name_by_id=current_sector_name_by_id,
        sector_membership_mode=(
            "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
            if current_mode
            else "POINT_IN_TIME_MEMBERSHIP_NOT_EXPORTED_FOR_ATTRIBUTION"
        ),
    )
    risk_execution_attribution = (
        higher_timeframe_execution_attribution(
            candidate_audit,
            replay_document,
            terminal_accounting_attribution,
        )
        if current_mode and approximate_mode
        else None
    )
    higher_timeframe_effectiveness = higher_timeframe_effectiveness_audit(
        candidate_audit
    )
    variant_parameters = recent_year_research_parameters() if current_mode else None
    technical_disclosures = (
        (
            "raw recursive level 2 third buys are treated as approximate 30m setups rather than a proof of unique decomposition",
            "the first confirmed raw level 0 buy within ten trading sessions is the approximate 1m locator",
            "active center expansion and unresolved nine-segment evidence lower confidence but are not hard blockers",
            "a setup is rejected if a completed adjusted 1m close breaches its structural invalidation before order dispatch",
            "full strategic exits use a confirmed raw level 2 sell or completed 1m invalidation; level 1 points remain tactical",
        )
        if approximate_mode
        else ()
    )
    benchmark_results = (
        _benchmark("SH.000001", start=grids[0][0], end=grids[-1][1]),
        _benchmark("SH.000300", start=grids[0][0], end=grids[-1][1]),
        _benchmark("SH.510300", start=grids[0][0], end=grids[-1][1]),
    )
    report: dict[str, object] = {
        "schema": "chanlun-sector-first-full-market-research-backtest",
        "result_label": (
            "RECENT_YEAR_APPROXIMATE_CHANLUN_POINT_RESEARCH_BACKTEST"
            if approximate_mode
            else "RECENT_YEAR_CURRENT_SECTOR_TECHNICAL_RESEARCH_BACKTEST"
            if current_mode
            else "RESEARCH_APPROXIMATION_COMPONENT_BACKTEST"
        ),
        "strict_full_system_result": {
            "status": "NOT_EVALUABLE",
            "reason": (
                "TECHNICAL_POINTS_ARE_EXPLICIT_RESEARCH_APPROXIMATIONS"
                if approximate_mode
                else "USER_AUTHORIZED_VARIANT_BACKFILLS_CURRENT_SECTOR_MEMBERSHIP_AND_DISABLES_THREE_PROGRAM"
                if current_mode
                else "SIGNED_POINT_IN_TIME_THREE_PROGRAM_SERVICE_UNAVAILABLE"
            ),
            "replay": asdict(strict_empty),
        },
        "research_variant_result": {
            "replay": replay_document,
            "terminal_accounting_attribution": terminal_accounting_attribution,
            "performance_status": performance_status,
            "performance_reason": performance_reason,
            "daily_metrics": strategy_curve_metrics,
            "accounting_curve_metrics": accounting_curve_metrics,
            "periods": (
                _periods(daily)
                if result.metrics.performance_evaluable
                else {
                    "status": performance_status,
                    "reason": performance_reason,
                }
            ),
        },
        "ablations": {
            "NO_TACTICAL": asdict(no_tactical),
            "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY": asdict(exact_green),
        },
        "ablation_scheduler_causality_audits": (
            ablation_scheduler_causality_audits
        ),
        "benchmarks": benchmark_results,
        "candidate_audit": candidate_audit,
        "structural_rejections": tuple(
            {
                "symbol": context.facts.code,
                "strategic_point_id": decision.l0_point_id,
                "status": decision.status,
                "reason_codes": decision.reason_codes,
                "relevant_expansion_ids": decision.relevant_expansion_ids,
                "unresolved_nine_segment_ids": decision.unresolved_nine_segment_ids,
            }
            for context in contexts.values()
            for decision in context.facts.direct_decisions
            if decision.status != "PASS"
        ),
        "signal_counts": signal_counts,
        "scheduler_causality_audit": scheduler_causality_audit,
        "tactical_execution_audit": tactical_execution_audit,
        "candidate_funnel": {
            "all_market_sector_classified_symbols": query_plan.get(
                "requested_symbol_count", 5201
            ),
            "three_program_prefiltered_symbols": (
                None
                if current_mode
                else len(research.ever_accepted_symbols)  # type: ignore[union-attr]
            ),
            "terminal_recursive_potential_symbols": query_plan.get(
                "potential_symbol_count"
            ),
            "causally_rescanned_symbols": len(manifest["symbols"]),
            "causal_technical_entry_count": (
                signal_counts.get("approximate_entry_technical_pass", 0)
                if approximate_mode
                else manifest.get("summary", {}).get("technical_entry_count")
            ),
            "accepted_candidate_count": sum(
                bool(row.get("accepted")) for row in candidate_audit
            ),
            "order_count": result.metrics.order_count,
            "fill_count": result.metrics.fill_count,
        },
        "higher_timeframe_gate_distribution": {
            subject: dict(
                sorted(
                    Counter(
                        str(row.get(field) or "UNRESOLVED")
                        for row in candidate_audit
                        if row.get("decision_at") is not None
                        and field in row
                    ).items()
                )
            )
            for subject, field in (
                ("market", "market_risk_gate"),
                ("sector", "sector_risk_gate"),
                ("symbol", "symbol_risk_gate"),
            )
        },
        "higher_timeframe_effectiveness_audit": (
            higher_timeframe_effectiveness
        ),
        "higher_timeframe_execution_attribution": (
            risk_execution_attribution
        ),
        "sector_higher_timeframe_source_distribution": dict(
            sorted(
                Counter(
                    str(
                        (
                            row.get("sector_risk_warmup_evidence") or {}
                        ).get("source_mode", "UNRESOLVED")
                    )
                    for row in candidate_audit
                    if row.get("sector_risk_gate") is not None
                ).items()
            )
        ),
        "scope": {
            "all_market_scope_symbols": query_plan.get(
                "requested_symbol_count", 5201
            ),
            "causal_extracted_symbols": len(manifest["symbols"]),
            "audited_direct_symbols": len(contexts),
            "signal_source_symbols": (
                len(
                    {
                        str(row["symbol"])
                        for row in candidate_audit
                        if row.get("accepted")
                    }
                )
                if approximate_mode
                else sum(
                    bool(value.facts.technical_entry_count)
                    or bool(value.facts.strategic_sell_points)
                    for value in contexts.values()
                )
            ),
            "replay_symbols": tuple(sorted(contexts)),
            "sector_count": len(trigger.sector_source_revisions),
            "selection_order": trigger.selection_order,
        },
        "higher_timeframe_data_provenance": {
            "market_calendar": market_calendar_evidence,
            "daily_source": (
                "QMT_NATIVE_DAILY_PREFIX_RECONCILED_AGAINST_COMPLETED_1M_OVERLAP"
            ),
            "thirty_minute_source": "COMPLETED_QMT_1M_AGGREGATION_ONLY",
            "sector_daily_source": (
                "QMT_CURRENT_MEMBER_NATIVE_DAILY_MEDIAN_RETURN_CHAIN_"
                "RESEARCH_ONLY"
                if current_mode
                else "UNRESOLVED"
            ),
            "sector_thirty_minute_source": (
                "COMPLETED_QMT_CURRENT_MEMBER_5M_COMPOSITE_AGGREGATION"
                if current_mode
                else "UNRESOLVED"
            ),
            "sector_cross_frequency_reconciliation": (
                "UNRECONCILED_NONLINEAR_MEDIAN_AGGREGATION_GREEN_CAPPED_AMBER"
                if current_mode
                else "UNRESOLVED"
            ),
            "calendar_limitation": (
                "two realized QMT index daily session paths agree, but their "
                "historical point-in-time publication is not independently proven"
            ),
        },
        "input_hashes": {
            "trigger_ledger": _sha256_file(trigger_path),
            "research_ledger": (
                None if current_mode else _sha256_file(research_path)
            ),
            "current_catalog_entry": current_catalog_entry_sha256,
            "current_catalog_ledger": current_catalog_ledger_sha256,
            "terminal_query_plan": (
                query_plan_file_sha256
            ),
            "direct_manifest": manifest_file_sha256,
            "pit_snapshot": _sha256_file(args.pit_snapshot.resolve()),
        },
        "decision_source_snapshot": run_decision_source_snapshot,
        "parameter_snapshots": {
            "strategy_parameter_set_id": individual_parameter_snapshot().parameter_set_id,
            "research_parameter_set_id": (
                variant_parameters.parameter_set_id
                if variant_parameters is not None
                else research.parameters.parameter_set_id  # type: ignore[union-attr]
            ),
            "research_variant": (
                None
                if variant_parameters is None
                else variant_parameters.document()
            ),
            "technical_approximation": (
                None
                if technical_approximation is None
                else technical_approximation.document()
            ),
            "technical_alignment_parameter_set_id": (
                None
                if technical_approximation is None
                else technical_approximation_alignment_contract().parameter_set_id
            ),
            "sector_higher_timeframe_research_bridge": (
                sector_native_daily_research_bridge_contract()
                if current_mode
                else None
            ),
            "replay_contract_parameter_set_id": result.contract.parameter_set_id,
            "fee_schedule": asdict(_fee_model(grids[0][0].date())),
            "initial_cash": args.initial_cash,
        },
        "approximation_disclosures": (
            (*technical_disclosures,
                "current QMT GICS3 membership is backfilled over the whole test year with explicit user authorization",
                "the individual three-program is disabled and is not evaluated by this research variant",
                "historical bid/ask and tick data are unavailable; only later completed 1m bars may match orders",
                "market/symbol native QMT daily history is used only after overlap reconciliation against the authoritative completed 1m prefix; 30m remains 1m-derived",
                "sector native-daily and 5m median composites are nonlinear and unreconciled; the M/W/D fallback is research-only and any GREEN is capped to AMBER",
                "the historical trading calendar is a two-index realized-session reconciliation, not a point-in-time publication archive",
                "historical ST names use the captured security-master name and may not reproduce every past rename",
                "AMBER higher-timeframe states pass the separate research path; confirmed RED and unresolved snapshots fail",
                "ordinary L2 oscillation short-difference remains disabled until 20 causal adaptation pairs exist",
                "rights subscriptions are excluded; mandatory bonus/split and cash dividends are event-sourced",
                "this result is not formal strict strategy, not survivor-bias-free, and can never enable live trading",
            )
            if current_mode
            else (*technical_disclosures,
                "SW1 membership uses the captured PIT/current approximation where historical changes are incomplete",
                "QMT disclosure-dated finance and liquidity proxy is not a signed three-program adjudication",
                "historical bid/ask is unavailable; completed 1m bars are used only as a conservative quote/spread proxy",
                "market/symbol native QMT daily history is used only after overlap reconciliation against the authoritative completed 1m prefix; 30m remains 1m-derived",
                "the historical trading calendar is a two-index realized-session reconciliation, not a point-in-time publication archive",
                "historical ST names use the captured security-master name and may not reproduce every past rename",
                "AMBER higher-timeframe states pass the separate research path; confirmed RED and unresolved snapshots fail",
                "ordinary L2 oscillation short-difference remains disabled until 20 causal adaptation pairs exist",
                "rights subscriptions are excluded; mandatory bonus/split and cash dividends are event-sourced",
            )
        ),
        "causality_guards": (
            "completed bars only",
            "signals dispatch at the next 30m decision grid",
            "broker latency two seconds",
            "no signal-bar fill",
            "strict whole-bar limit crossing",
            "T+1 event-sourced sellable quantity",
            "point-in-time factors and corporate actions",
        ),
        "sample_warnings": (
            "STRATEGIC_SAMPLE_BELOW_100" if result.metrics.strategic_sample_insufficient else "STRATEGIC_SAMPLE_ADEQUATE",
            "TACTICAL_SAMPLE_BELOW_200" if result.metrics.tactical_sample_insufficient else "TACTICAL_SAMPLE_ADEQUATE",
        ),
        "data_grade": (
            "RESEARCH_APPROXIMATION"
            if approximate_mode
            else "RESEARCH_ONLY"
            if current_mode
            else "RESEARCH_APPROXIMATION"
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    terminal_accounting_headline = _terminal_accounting_headline(
        terminal_accounting_attribution
    )
    performance_headline = _performance_headline(
        report["research_variant_result"],
        benchmark_results,
        terminal_headline=terminal_accounting_headline,
    )
    risk_execution_headline = (
        None
        if risk_execution_attribution is None
        else {
            "schema": (
                "chanlun-higher-timeframe-execution-headline"
            ),
            "status": risk_execution_attribution["status"],
            "causal_identity_status": risk_execution_attribution[
                "causal_identity_status"
            ],
            "risk_evidenced_candidate_count": risk_execution_attribution[
                "risk_evidenced_candidate_count"
            ],
            "accepted_candidate_count": risk_execution_attribution[
                "accepted_candidate_count"
            ],
            "entry_order_count": risk_execution_attribution[
                "entry_order_count"
            ],
            "entry_filled_candidate_count": risk_execution_attribution[
                "entry_filled_candidate_count"
            ],
            "entry_unfilled_candidate_count": risk_execution_attribution[
                "entry_unfilled_candidate_count"
            ],
            "strict_green": risk_execution_attribution["cohorts"][
                "STRICT_GREEN"
            ],
            "research_amber_only": risk_execution_attribution["cohorts"][
                "RESEARCH_AMBER_ONLY"
            ],
            "all_filled_entries_are_research_amber_only": (
                risk_execution_attribution[
                    "all_filled_entries_are_research_amber_only"
                ]
            ),
            "terminal_total_net_pnl": risk_execution_attribution[
                "terminal_total_net_pnl"
            ],
            "diagnostic_only": True,
            "decisions_unchanged": True,
            "live_status": "LIVE_DISABLED",
        }
    )
    report["content_sha256"] = sha256_json(_jsonable(report))
    _atomic_json(
        args.output.resolve(),
        report,
        expected_decision_source_snapshot=run_decision_source_snapshot,
        loaded_replay_module_names=(
            _REPLAY_IMPORT_MODULE_NAMES
            | (frozenset(sys.modules) - run_start_module_names)
        ),
    )
    print(
        json.dumps(
            _jsonable(
                {
                    "output": args.output.resolve(),
                    "metrics": asdict(result.metrics),
                    "terminal_accounting_headline": (
                        terminal_accounting_headline
                    ),
                    "performance_headline": performance_headline,
                    "higher_timeframe_execution_headline": (
                        risk_execution_headline
                    ),
                    "scope": report["scope"],
                    "signal_counts": signal_counts,
                    "content_sha256": report["content_sha256"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
