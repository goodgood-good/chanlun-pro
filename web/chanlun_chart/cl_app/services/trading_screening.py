"""Incremental, read-only screening service driven only by ``TradingEngine``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Event, Lock, RLock, Thread, current_thread
import time
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
    TradingEngine,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot,
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
    apply_sector_selection_scope as _apply_selection_scope,
    sector_decision_document,
    serialize_evaluated_signal,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframeGateBundle,
    HigherTimeframeSessionEvidence,
)
from chanlun.decision_support.trading_system.file_lock import (
    interprocess_file_lock,
)
from chanlun.decision_support.trading_system.incremental_scan import (
    BarKey,
    ScanCursor,
    ScanPlan,
    build_scan_plan,
)
from chanlun.decision_support.trading_system.models import (
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.live_review_materialization import (
    resolve_live_review_materialization_receipt,
)
from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)
from chanlun.decision_support.trading_system.portfolio_risk import RiskLimits
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_FREQUENCIES,
)
from chanlun.decision_support.trading_system.sector_policy import rank_sectors
from chanlun.decision_support.trading_system.sector_strength import (
    MIN_MEMBER_HISTORY_COVERAGE,
    build_sector_member_history_diagnostics,
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.live_human_review import (
    COVERAGE_EXCLUSION_REASON_CODES,
    COVERAGE_MANIFEST_FIELDS,
    COVERAGE_MANIFEST_SCHEMA,
    COVERAGE_STATE_CONTRACT_ID,
    MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID,
    SECTOR_COVERAGE_CONTRACT_ID,
    SIGNAL_DOCUMENT_CONTRACT_ID,
    live_screening_snapshot_content_sha256,
    monitor_instrument_exclusions_are_consistent,
    screening_coverage_epoch_id,
    validate_live_review_snapshot,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.trading_session import (
    official_trading_session_evidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
)
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT,
    QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT,
    QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE,
    QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT,
    QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
    QMT_GICS3_COMPOSITE_PROVIDER,
    QMT_SECTOR_STRENGTH_ADJUSTMENT,
    QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT,
    QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE,
)
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
    _sector_exclusion_document,
    _sector_failure_document,
)


_KNOWN_MONITOR_INSTRUMENT_TYPES = frozenset(
    {
        "stock_cn",
        "etf_cn",
        "index_cn",
        "fund_cn",
        "unsupported_cn",
        "unresolved_cn",
    }
)
_TRADABLE_MONITOR_INSTRUMENT_TYPES = frozenset({"stock_cn", "etf_cn"})


SCHEMA = "chanlun-trading-screening"
POINT_TYPES = ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")
CN = ZoneInfo("Asia/Shanghai")
# 次日候选池的重计算属于收盘后任务。15:05 为 QMT 写入 15:00 已完成分钟线
# 预留一个很小的落盘缓冲；全市场覆盖一旦开始，即使超过窗口也会由 pending
# 队列继续排空。盘前窗口只重新读取板块目录/成分与最新缓存，完成轻量复核，
# 不再把几千只股票的主扫描压到开盘前。
POST_CLOSE_PRESELECTION_START = datetime_time(15, 5)
POST_CLOSE_PRESELECTION_END = datetime_time(23, 0)
PREOPEN_RECONCILIATION_START = datetime_time(8, 45)
PREOPEN_RECONCILIATION_END = datetime_time(9, 10)
OVERNIGHT_COVERAGE_CONTINUATION_START = datetime_time(0, 0)
OVERNIGHT_COVERAGE_CONTINUATION_END = PREOPEN_RECONCILIATION_START
PRIORITY_MONITOR_MORNING_START = datetime_time(9, 31)
PRIORITY_MONITOR_MORNING_END = datetime_time(11, 31)
PRIORITY_MONITOR_AFTERNOON_START = datetime_time(13, 1)
PRIORITY_MONITOR_AFTERNOON_END = datetime_time(15, 1)
PRIORITY_MONITOR_SCHEMA = "chanlun-priority-signal-monitor"
CANDIDATE_MONITOR_CONTRACT_ID = "bar-cadence-live-candidate-monitor"
CANDIDATE_MONITOR_LANE_1M = "CURRENT_1M"
CANDIDATE_MONITOR_LANE_5M = "CURRENT_5M"
CANDIDATE_MONITOR_LANE_30M = "CURRENT_30M"
_CANDIDATE_MONITOR_LANES = frozenset(
    {
        CANDIDATE_MONITOR_LANE_1M,
        CANDIDATE_MONITOR_LANE_5M,
        CANDIDATE_MONITOR_LANE_30M,
    }
)
MARKET_CLOSE_CUTOFF = datetime_time(15)
COMPLETE_CLOSE_IDLE_REASON = "COMPLETE_CLOSE_SNAPSHOT_OUTSIDE_ACTIVE_WINDOW"
FULL_COVERAGE_PAUSE_REASON = "OUTSIDE_FULL_COVERAGE_REFRESH_WINDOW"
_CACHE_GENERATION_RETENTION = 3
_CACHE_GENERATION_FILE = re.compile(r"^[0-9a-f]{64}\.json$")


@lru_cache(maxsize=4)
def _current_review_decision_source_id(project_root: str) -> str:
    """Freeze the app process implementation identity once per repository."""

    snapshot = current_decision_source_snapshot(Path(project_root))
    return decision_source_snapshot_id(snapshot)


@lru_cache(maxsize=32)
def _official_calendar_for_observed_day(
    observed_day: date,
) -> tuple[date, date, frozenset[date], str] | None:
    """Load the pinned SSE annual calendar once per observed day.

    Scheduling must never turn a weekday holiday into a trading session.  The
    existing forward pipeline already pins and validates the SSE annual
    announcement, so screening consumes the same evidence instead of inventing
    a second calendar.  Returning ``None`` keeps a conservative weekday
    fallback outside the artifact's coverage; it can cause extra work, but can
    never skip a possible market session.
    """

    observed_at = datetime.combine(
        observed_day,
        datetime_time(23, 59, 59),
        tzinfo=CN,
    )
    try:
        evidence = official_trading_session_evidence(
            session=observed_day,
            observed_at=observed_at,
        )
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(evidence, Mapping):
        return None
    calendar = evidence.get("calendar_document")
    if not isinstance(calendar, Mapping):
        return None
    raw_days = calendar.get("trading_days")
    if not isinstance(raw_days, list):
        return None
    try:
        coverage_start = date.fromisoformat(str(calendar["coverage_start"]))
        coverage_end = date.fromisoformat(str(calendar["coverage_end"]))
        trading_days = frozenset(date.fromisoformat(str(value)) for value in raw_days)
    except (KeyError, TypeError, ValueError):
        return None
    fingerprint = calendar.get("calendar_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith("sha256:"):
        return None
    return coverage_start, coverage_end, trading_days, fingerprint


def _scheduled_trading_day(
    value: date,
    *,
    observed_at: datetime,
) -> tuple[bool, str]:
    observed = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    calendar = _official_calendar_for_observed_day(observed.date())
    if calendar is not None:
        coverage_start, coverage_end, trading_days, _fingerprint = calendar
        if coverage_start <= value <= coverage_end:
            return value in trading_days, "SSE_OFFICIAL_ANNUAL_CALENDAR"
    return value.weekday() < 5, "CONSERVATIVE_WEEKDAY_FALLBACK"


def _next_scheduled_trading_day(
    value: date,
    *,
    observed_at: datetime,
    include_value: bool,
) -> tuple[date | None, str]:
    candidate = value if include_value else value + timedelta(days=1)
    source = "UNRESOLVED"
    for _ in range(370):
        is_trading, source = _scheduled_trading_day(
            candidate,
            observed_at=observed_at,
        )
        if is_trading:
            return candidate, source
        candidate += timedelta(days=1)
    return None, source


def _preselection_target_session(
    snapshot: Mapping[str, object],
    observed_at: datetime,
) -> dict[str, object]:
    """Bind a close snapshot to the session for which it is actionable."""

    observed = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    raw_cutoff = snapshot.get("market_data_as_of")
    try:
        cutoff = normalize_datetime(
            datetime.fromisoformat(str(raw_cutoff)),
            "market_data_as_of",
        ).astimezone(CN)
    except (TypeError, ValueError):
        return {
            "target_session": None,
            "expected_session": None,
            "aligned": False,
            "reason_code": "PRESELECTION_MARKET_DATA_CUTOFF_INVALID",
            "calendar_source": "UNRESOLVED",
        }
    if cutoff.time() < MARKET_CLOSE_CUTOFF:
        return {
            "target_session": None,
            "expected_session": None,
            "aligned": False,
            "reason_code": "PRESELECTION_CLOSE_CUTOFF_INCOMPLETE",
            "calendar_source": "UNRESOLVED",
        }

    target, target_source = _next_scheduled_trading_day(
        cutoff.date(),
        observed_at=observed,
        include_value=False,
    )
    today_is_trading, today_source = _scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    if today_is_trading and observed.time() < MARKET_CLOSE_CUTOFF:
        expected = observed.date()
        expected_source = today_source
    else:
        expected, expected_source = _next_scheduled_trading_day(
            observed.date(),
            observed_at=observed,
            include_value=not today_is_trading,
        )
    aligned = target is not None and expected is not None and target == expected
    return {
        "target_session": None if target is None else target.isoformat(),
        "expected_session": None if expected is None else expected.isoformat(),
        "aligned": aligned,
        "reason_code": ("READY" if aligned else "PRESELECTION_TARGET_SESSION_MISMATCH"),
        "calendar_source": (
            target_source
            if target_source == expected_source
            else f"{target_source}|{expected_source}"
        ),
    }


def _complete_close_snapshot_can_idle(
    snapshot: Mapping[str, object],
    observed_at: datetime,
    *,
    review_boundary_ready: bool | None = None,
    phase_refresh_at: datetime | None = None,
) -> bool:
    """Return whether a proven close-complete snapshot may remain read-only.

    This is an operational pacing gate, not a cache relaxation.  It never reuses
    one sector snapshot for a different decision time: after one successful
    refresh in the current Web process, a fully drained close snapshot may idle
    until one of two boundaries: 15:05 builds the next-session candidate pool
    from the completed close, while 08:45 performs a bounded pre-open
    reconciliation before the 09:10 point-in-time capture.
    """

    if (
        snapshot.get("available") is not True
        or snapshot.get("scan_state") != "complete"
    ):
        return False
    audit = snapshot.get("scan_audit")
    manifest = snapshot.get("coverage_manifest")
    if not isinstance(audit, Mapping) or not isinstance(manifest, Mapping):
        return False
    try:
        pending = int(audit.get("pending_symbol_count", 0))
        immediate = int(audit.get("immediate_pending_symbol_count", pending))
        backoff = int(audit.get("backoff_retry_symbol_count", 0))
    except (TypeError, ValueError):
        return False
    if (
        audit.get("coverage_cycle_complete") is not True
        or manifest.get("complete") is not True
        or pending != 0
        or immediate != 0
        or backoff != 0
    ):
        return False
    if review_boundary_ready is None:
        try:
            validate_live_review_snapshot(snapshot)
        except (TypeError, ValueError):
            return False
    elif not review_boundary_ready:
        return False
    raw_cutoff = snapshot.get("market_data_as_of")
    if not isinstance(raw_cutoff, str):
        return False
    try:
        cutoff = normalize_datetime(
            datetime.fromisoformat(raw_cutoff),
            "market_data_as_of",
        ).astimezone(CN)
    except ValueError:
        return False
    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    if cutoff > local_now or cutoff.time() < MARKET_CLOSE_CUTOFF:
        return False
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return True

    current = local_now.time()
    if PREOPEN_RECONCILIATION_START <= current < PREOPEN_RECONCILIATION_END:
        phase_start = datetime.combine(
            local_now.date(),
            PREOPEN_RECONCILIATION_START,
            tzinfo=CN,
        )
    elif POST_CLOSE_PRESELECTION_START <= current < POST_CLOSE_PRESELECTION_END:
        # Do not accept yesterday's 15:00 cutoff merely because a diagnostic
        # refresh ran after today's close.  QMT can need a short period to
        # expose the final minute; keep retrying until today's completed close
        # is the actual decision cutoff.
        if cutoff.date() != local_now.date():
            return False
        phase_start = datetime.combine(
            local_now.date(),
            POST_CLOSE_PRESELECTION_START,
            tzinfo=CN,
        )
    else:
        return True

    # One complete refresh inside the current phase is enough.  If it opens a
    # new full-market epoch, the pending queue bypasses this idle gate and keeps
    # draining until complete; if QMT has not exposed the close yet, the
    # pre-close cutoff check above keeps the service retrying.
    if phase_refresh_at is not None:
        refreshed_at = normalize_datetime(
            phase_refresh_at,
            "phase_refresh_at",
        ).astimezone(CN)
    else:
        scanned_at = snapshot.get("scanned_at") or snapshot.get("generated_at")
        if not isinstance(scanned_at, str):
            return False
        try:
            refreshed_at = normalize_datetime(
                datetime.fromisoformat(scanned_at),
                "screening scanned_at",
            ).astimezone(CN)
        except ValueError:
            return False
    return refreshed_at >= phase_start


def _coverage_sector_probe_required(
    snapshot: Mapping[str, object],
    observed_at: datetime,
) -> bool:
    """Return whether a complete frozen epoch must query QMT sector state again.

    Outside the two deliberate daily boundaries, a restart is not a new
    decision time.  Recomputing the same sector structures merely because the
    Python process changed can alter structural point identities and mix two
    evidence identities in one coverage epoch. The post-close and pre-open
    windows remain the only places where a complete epoch is probed for a new
    market/catalog revision.
    """

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    is_trading_day, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not is_trading_day:
        return False
    current = local_now.time()
    if PREOPEN_RECONCILIATION_START <= current < PREOPEN_RECONCILIATION_END:
        phase_start = datetime.combine(
            local_now.date(),
            PREOPEN_RECONCILIATION_START,
            tzinfo=CN,
        )
    elif POST_CLOSE_PRESELECTION_START <= current < POST_CLOSE_PRESELECTION_END:
        phase_start = datetime.combine(
            local_now.date(),
            POST_CLOSE_PRESELECTION_START,
            tzinfo=CN,
        )
        raw_cutoff = snapshot.get("market_data_as_of")
        if not isinstance(raw_cutoff, str):
            return True
        try:
            cutoff = normalize_datetime(
                datetime.fromisoformat(raw_cutoff),
                "market_data_as_of",
            ).astimezone(CN)
        except ValueError:
            return True
        if cutoff.date() != local_now.date():
            return True
    else:
        return False
    scanned_at = snapshot.get("scanned_at") or snapshot.get("generated_at")
    if not isinstance(scanned_at, str):
        return True
    try:
        scanned = normalize_datetime(
            datetime.fromisoformat(scanned_at),
            "screening scanned_at",
        ).astimezone(CN)
    except ValueError:
        return True
    return scanned < phase_start


def _next_background_active_start(observed_at: datetime) -> datetime:
    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    for day_offset in range(32):
        candidate = local_now.date() + timedelta(days=day_offset)
        candidate_is_trading, _calendar_source = _scheduled_trading_day(
            candidate,
            observed_at=local_now,
        )
        if not candidate_is_trading:
            continue
        for boundary in (
            PREOPEN_RECONCILIATION_START,
            POST_CLOSE_PRESELECTION_START,
        ):
            value = datetime.combine(candidate, boundary, tzinfo=CN)
            if value > local_now:
                return value
    raise RuntimeError("unable to resolve next screening schedule boundary")


def _next_full_coverage_active_start(observed_at: datetime) -> datetime:
    """Resolve the next archival window, including overnight continuation."""

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    for day_offset in range(32):
        candidate = local_now.date() + timedelta(days=day_offset)
        candidate_is_trading, _calendar_source = _scheduled_trading_day(
            candidate,
            observed_at=local_now,
        )
        if not candidate_is_trading:
            continue
        for boundary in (
            OVERNIGHT_COVERAGE_CONTINUATION_START,
            POST_CLOSE_PRESELECTION_START,
        ):
            value = datetime.combine(candidate, boundary, tzinfo=CN)
            if value > local_now:
                return value
    raise RuntimeError("unable to resolve next full-coverage schedule boundary")


def _full_coverage_refresh_window_open(observed_at: datetime) -> bool:
    """Limit expensive full-universe work to deliberate daily windows.

    The close-to-next-session selection is built after 15:05.  An unfinished
    authenticated epoch may resume from midnight through the short pre-open
    reconciliation window.  During continuous trading the independent priority
    lane owns the minute-by-minute budget; draining thousands of archival
    symbols there can otherwise block a current alert for minutes.
    """

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return False
    current = local_now.time()
    return bool(
        OVERNIGHT_COVERAGE_CONTINUATION_START
        <= current
        < OVERNIGHT_COVERAGE_CONTINUATION_END
        or PREOPEN_RECONCILIATION_START <= current < PREOPEN_RECONCILIATION_END
        or POST_CLOSE_PRESELECTION_START <= current < POST_CLOSE_PRESELECTION_END
    )


def _priority_monitor_session_open(observed_at: datetime) -> bool:
    """Use only completed A-share minute bars, never an in-progress bar."""

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return False
    current = local_now.time()
    return bool(
        PRIORITY_MONITOR_MORNING_START <= current < PRIORITY_MONITOR_MORNING_END
        or PRIORITY_MONITOR_AFTERNOON_START <= current < PRIORITY_MONITOR_AFTERNOON_END
    )


def _priority_monitor_delay_seconds(
    observed_at: datetime,
    last_at: datetime | None,
    *,
    interval_seconds: int,
) -> float:
    """Return the remaining start-to-start monitor interval."""

    if last_at is None:
        return 0.0
    observed = normalize_datetime(observed_at, "observed_at")
    previous = normalize_datetime(last_at, "priority monitor last_at")
    if observed < previous:
        return 0.0
    return max(0.0, interval_seconds - (observed - previous).total_seconds())


_PRIORITY_BUY_STAGE_RANK = {
    "executable": 0,
    "triggered": 1,
    "armed": 2,
    "formed": 3,
    "approaching": 4,
    "observed": 5,
    "active": 6,
}

_CURRENT_MINUTE_BUY_STAGES = frozenset({"armed", "triggered", "executable", "active"})


def _priority_buy_candidate_codes(
    *signal_groups: tuple[Mapping[str, object], ...],
    excluded_codes: frozenset[str] = frozenset(),
    allowed_stages: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return non-owned buy candidates in operational urgency order.

    A sell point is actionable only for a held or explicitly watched symbol;
    those names live in the mandatory lane assembled by the caller.  Letting
    hundreds of unowned sell-only documents consume the bounded current-minute
    lane can otherwise make every explicit watchlist name wait many rotations.
    This helper changes observation order only: it neither creates nor removes
    any archived signal or trading decision.
    """

    best_rank: dict[str, tuple[int, str]] = {}
    for group in signal_groups:
        for row in group:
            code = row.get("code")
            point_type = row.get("point_type")
            stage = lifecycle_stage_from_signal(row)
            if (
                not isinstance(code, str)
                or not code
                or code in excluded_codes
                or not isinstance(point_type, str)
                or point_type not in POINT_TYPES
                or not point_type.endswith("buy")
                or not isinstance(stage, str)
                or stage in {"closed", "invalidated"}
                or (allowed_stages is not None and stage not in allowed_stages)
            ):
                continue
            rank = (_PRIORITY_BUY_STAGE_RANK.get(stage, 10**6), code)
            previous = best_rank.get(code)
            if previous is None or rank < previous:
                best_rank[code] = rank
    return tuple(sorted(best_rank, key=best_rank.__getitem__))


def _take_due_candidate_batch(
    universe: tuple[str, ...],
    *,
    last_success_at: Mapping[str, datetime],
    observed_at: datetime,
    target_seconds: int,
    monitor_interval_seconds: int,
    max_symbols: int,
    excluded_codes: frozenset[str] = frozenset(),
    previous_monitor_at: datetime | None = None,
) -> tuple[str, ...]:
    """Take the oldest due bar-cadence candidates within honest capacity.

    The target is a maximum observation age, not a promise that every symbol
    is recomputed on every one-minute scheduler tick.  A 5m setup cannot
    change between completed 5m bars, so the scheduler spreads that universe
    across the five minute ticks.  If a slow prior pass consumed multiple
    ticks, the requested share grows proportionally and the hard cap makes any
    capacity shortfall visible through health instead of blocking the 1m lane
    indefinitely.
    """

    if target_seconds <= 0 or monitor_interval_seconds <= 0 or max_symbols <= 0:
        raise ValueError("candidate cadence limits must be positive")
    observed = normalize_datetime(observed_at, "observed_at")
    values = tuple(
        code for code in dict.fromkeys(universe) if code not in excluded_codes
    )
    if not values:
        return ()
    elapsed_seconds = monitor_interval_seconds
    if previous_monitor_at is not None:
        previous = normalize_datetime(
            previous_monitor_at,
            "previous candidate monitor at",
        )
        if observed >= previous:
            elapsed_seconds = max(
                monitor_interval_seconds,
                int((observed - previous).total_seconds()),
            )
    planned = max(
        1,
        (len(values) * min(elapsed_seconds, target_seconds) + target_seconds - 1)
        // target_seconds,
    )
    due: list[tuple[bool, datetime, str]] = []
    minimum = datetime.min.replace(tzinfo=observed.tzinfo)
    for code in values:
        last_at = last_success_at.get(code)
        if last_at is None:
            due.append((False, minimum, code))
            continue
        last = normalize_datetime(last_at, f"{code} candidate last_success_at")
        if observed < last or (observed - last).total_seconds() >= target_seconds:
            due.append((True, last, code))
    due.sort()
    return tuple(value[2] for value in due[: min(max_symbols, planned)])


def _candidate_lane_coverage(
    universe: tuple[str, ...],
    *,
    last_success_at: Mapping[str, datetime],
    observed_at: datetime,
    target_seconds: int,
) -> dict[str, object]:
    """Describe actual cadence coverage without treating unseen as current."""

    observed = normalize_datetime(observed_at, "observed_at")
    unique = tuple(dict.fromkeys(universe))
    missing = 0
    overdue = 0
    oldest_age: float | None = None
    for code in unique:
        last_at = last_success_at.get(code)
        if last_at is None:
            missing += 1
            continue
        last = normalize_datetime(last_at, f"{code} candidate last_success_at")
        age = max(0.0, (observed - last).total_seconds())
        oldest_age = age if oldest_age is None else max(oldest_age, age)
        if observed < last or age > target_seconds:
            overdue += 1
    current = max(0, len(unique) - missing - overdue)
    return {
        "universe_count": len(unique),
        "current_count": current,
        "missing_count": missing,
        "overdue_count": overdue,
        "coverage_ratio": (
            "1" if not unique else str(Decimal(current) / Decimal(len(unique)))
        ),
        "oldest_observation_age_seconds": oldest_age,
        "target_seconds": target_seconds,
        "ready": missing == 0 and overdue == 0,
    }


def _main_notification_context(
    *,
    observed_at: datetime,
    market_data_as_of: datetime,
    coverage_complete: bool,
    monitoring_only_refresh: bool,
    max_age_seconds: int,
) -> dict[str, object]:
    """Describe whether the archival scan may emit a real-time alert.

    Full-universe coverage is deliberately resumable and can take much longer
    than one minute.  A symbol discovered near the end of that queue still
    belongs to the frozen coverage cutoff; treating it as a live transition
    would make an overnight/backfill result look like an intraday warning.
    The independent priority monitor is the normal live lane while coverage is
    draining.  The archival lane may notify only when its complete publication
    is itself current and the A-share minute session is open.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    cutoff = normalize_datetime(market_data_as_of, "market_data_as_of")
    age_seconds = (observed - cutoff).total_seconds()
    realtime_eligible = True
    reason_code = "READY"
    if not coverage_complete:
        realtime_eligible = False
        reason_code = "COVERAGE_IN_PROGRESS"
    elif not _priority_monitor_session_open(observed):
        realtime_eligible = False
        reason_code = "OUTSIDE_COMPLETED_MINUTE_SESSION"
    elif age_seconds < 0:
        realtime_eligible = False
        reason_code = "MARKET_DATA_CUTOFF_IN_FUTURE"
    elif age_seconds > max_age_seconds:
        realtime_eligible = False
        reason_code = "FROZEN_COVERAGE_CUTOFF_STALE"
    return {
        "schema": "chanlun-realtime-notification-context",
        "realtime_eligible": realtime_eligible,
        "reason_code": reason_code,
        "source": (
            "CURRENT_COMPLETE_MONITORING"
            if monitoring_only_refresh
            else "CURRENT_COMPLETE_COVERAGE"
            if realtime_eligible
            else "FROZEN_COVERAGE"
        ),
        "observed_at": observed.isoformat(),
        "market_data_as_of": cutoff.isoformat(),
        "market_data_age_seconds": round(age_seconds, 3),
        "max_age_seconds": max_age_seconds,
        "uses_completed_minute_bars_only": True,
    }


def _screening_policy_document() -> dict[str, object]:
    return {
        "latest_per_independent_lane": True,
        "max_five_minute_setup_age_seconds": (MAX_FIVE_MINUTE_SETUP_AGE_SECONDS),
        "sector_catalog_source": "qmt_gics3_components",
        "sector_price_source": "qmt_gics3_component_composite",
        "sector_composite_provider": QMT_GICS3_COMPOSITE_PROVIDER,
        "sector_composite_adjustment": QMT_GICS3_COMPOSITE_ADJUSTMENT,
        "sector_composite_factor_adjustment_contract": (
            QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
        ),
        "sector_composite_factor_cutoff": "decision_date_only",
        "sector_composite_factor_failure_policy": "fail_closed",
        "sector_composite_structure_price_quantum": "0.000001",
        "sector_composite_member_limit": QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
        "sector_composite_minimum_member_count": (
            QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT
        ),
        "sector_composite_minimum_bar_coverage": str(
            QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE
        ),
        "sector_composite_coverage_denominator": (
            "frozen_deterministic_representative_sample"
        ),
        "sector_composite_calendar_grid_contract": (
            QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT
        ),
        "sector_composite_member_mask_contract": (
            QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT
        ),
        "sector_scope": "all_eligible",
        "stock_scope": "all_members_of_all_eligible_sectors",
        "sector_frequencies": ["30m", "5m"],
        "sector_higher_timeframe_base_frequency": "5m",
        "sector_thirty_minute_derivation_contract": (
            QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT
        ),
        "sector_higher_timeframe_frequencies": ["M", "W", "D"],
        "sector_higher_timeframe_membership_provenance": (
            "exact_members_sample_coverage_price_grid_and_path"
        ),
        "stock_structure_frequencies": ["d", "30m", "5m", "1m"],
        "stroke_mode": "old",
        "center_source": "physical_timeframe_level_zero_segments",
        "recursive_structure_used": False,
        "unfinished_segment_candidates": True,
        "stock_trigger_frequency": "1m",
        "minimum_market_data_frequency": "1m",
        "qmt_one_minute_grid_revision": (QMT_COMPLETED_ONE_MINUTE_GRID_REVISION),
        "tick_data_used": False,
        "selection_universe_source": "qmt_gics3_current_components",
        "monitor_instrument_eligibility": ("qmt_native_stock_or_etf_fail_closed"),
        "sector_strength_qmt_dividend_type": (QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE),
        "sector_strength_adjustment": QMT_SECTOR_STRENGTH_ADJUSTMENT,
        "sector_strength_price_basis_contract": (
            QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT
        ),
        "sector_strength_min_member_history_coverage": str(MIN_MEMBER_HISTORY_COVERAGE),
        "higher_timeframe_partial_evidence_policy": (
            "preserve_independent_market_sector_symbol_gates_fail_closed"
        ),
        "higher_timeframe_warmup_required_daily_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        ),
        "higher_timeframe_warmup_required_thirty_minute_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 8
        ),
        "higher_timeframe_warmup_convergence_contract": (
            "drop_oldest_third_compare_mwd_state_mapping_and_ma5"
        ),
        "higher_timeframe_warmup_entry_policy": (
            "fail_closed_on_insufficient_or_diverged"
        ),
        "stock_failure_protocol": "stable_reason_code_epoch_scoped_retry",
    }


def _screening_policy_id() -> str:
    """Identity for selection rules and decision-input adapter semantics."""

    return sha256_json(_screening_policy_document())


def _validated_monitor_instrument_scope(
    value: object,
    field_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(value) is not tuple or len(value) != 2:
        raise TypeError(f"{field_name} must return eligible and excluded tuples")
    eligible, excluded = value
    for label, codes in (("eligible", eligible), ("excluded", excluded)):
        if (
            type(codes) is not tuple
            or any(
                type(code) is not str
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in codes
            )
            or tuple(sorted(set(codes))) != codes
        ):
            raise ValueError(f"{field_name}.{label} codes are invalid")
    if set(eligible) & set(excluded):
        raise ValueError(f"{field_name} scope dispositions overlap")
    return eligible, excluded


def _monitor_instrument_exclusion_documents(
    *scopes: tuple[str, tuple[str, ...]],
    instrument_types: Mapping[str, str],
) -> list[dict[str, object]]:
    sources_by_code: dict[str, set[str]] = {}
    for source, codes in scopes:
        for code in codes:
            sources_by_code.setdefault(code, set()).add(source)
    if set(instrument_types) != set(sources_by_code) or any(
        type(code) is not str
        or type(kind) is not str
        or kind not in _KNOWN_MONITOR_INSTRUMENT_TYPES
        or kind in _TRADABLE_MONITOR_INSTRUMENT_TYPES
        for code, kind in instrument_types.items()
    ):
        raise ValueError("monitor instrument type dispositions are inconsistent")
    documents: list[dict[str, object]] = []
    for code, sources in sorted(sources_by_code.items()):
        instrument_type = instrument_types[code]
        unresolved = instrument_type == "unresolved_cn"
        documents.append(
            {
                "code": code,
                "eligibility": (
                    "UNRESOLVED_FROM_TRADING_SCREENING"
                    if unresolved
                    else "EXCLUDED_FROM_TRADING_SCREENING"
                ),
                "reason_code": (
                    "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED"
                    if unresolved
                    else "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
                ),
                "selection_sources": sorted(sources),
                "evidence_source": "QMT_GET_INSTRUMENT_TYPE",
                "qmt_instrument_type": instrument_type,
                "diagnostic_only": True,
                "tick_data_used": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        )
    return documents


class MarketDataGateway(Protocol):
    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]: ...

    def active_watchlist(self) -> tuple[str, ...]: ...

    def active_watchlist_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]: ...

    def holdings(self) -> tuple[str, ...]: ...

    def holdings_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]: ...

    def tradable_instrument_codes(
        self,
        codes: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, str]: ...

    def symbol_name(self, code: str) -> str | None: ...

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
    ) -> SymbolStructureBundle: ...


class SectorCatalogGateway(Protocol):
    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
    ) -> SectorAssessmentBatch: ...

    def members(self) -> Mapping[str, tuple[str, ...]]: ...


class NotificationDispatcher(Protocol):
    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TradingScreeningConfig:
    refresh_interval_seconds: int = 60
    max_visible_symbols: int = 500
    max_symbols_per_refresh: int = 32
    max_monitor_symbols_per_refresh: int = 64
    max_total_symbols_per_refresh: int = 32
    priority_monitoring_enabled: bool = False
    full_coverage_refresh_enabled: bool = True
    priority_monitor_interval_seconds: int = 60
    max_five_minute_candidate_symbols_per_refresh: int = 256
    max_thirty_minute_candidate_symbols_per_refresh: int = 96
    five_minute_candidate_target_seconds: int = 300
    thirty_minute_candidate_target_seconds: int = 1800
    stock_worker_count: int = 1
    min_scan_completion_ratio: Decimal = Decimal("0.80")
    max_structure_age_seconds: int = 3600
    algorithm_id: str = STRICT_STRATEGY_ID
    structure_contract_id: str = "physical-timeframe-l0"
    parameter_set_id: str = "old-pen"

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if (
            self.max_visible_symbols <= 0
            or self.max_symbols_per_refresh <= 0
            or self.max_monitor_symbols_per_refresh <= 0
            or self.max_total_symbols_per_refresh <= 0
            or self.max_five_minute_candidate_symbols_per_refresh <= 0
            or self.max_thirty_minute_candidate_symbols_per_refresh <= 0
            or self.stock_worker_count <= 0
        ):
            raise ValueError("screening limits must be positive")
        if self.priority_monitor_interval_seconds <= 0:
            raise ValueError("priority monitor interval must be positive")
        if (
            self.five_minute_candidate_target_seconds
            < self.priority_monitor_interval_seconds
            or self.thirty_minute_candidate_target_seconds
            < self.five_minute_candidate_target_seconds
        ):
            raise ValueError("candidate cadence targets are inconsistent")
        if not Decimal("0") < self.min_scan_completion_ratio <= Decimal("1"):
            raise ValueError("min_scan_completion_ratio must be in (0, 1]")
        if self.max_structure_age_seconds <= 0:
            raise ValueError("max_structure_age_seconds must be positive")
        if not self.algorithm_id:
            raise ValueError("algorithm_id cannot be empty")


def _initial_snapshot(config: TradingScreeningConfig) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "algorithm_id": config.algorithm_id,
        "structure_contract_id": config.structure_contract_id,
        "parameter_set_id": config.parameter_set_id,
        "available": False,
        "scan_state": "not_started",
        "last_batch_state": "not_started",
        "full_coverage_state": "not_started",
        "generated_at": None,
        "scanned_at": None,
        "as_of": None,
        "market_data_as_of": None,
        "coverage_epoch_id": None,
        "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
        "sector_coverage_contract_id": SECTOR_COVERAGE_CONTRACT_ID,
        "monitor_instrument_exclusion_contract_id": (
            MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
        ),
        "sector_first": True,
        "read_only": True,
        "research_only": True,
        "no_order_execution": True,
        "counts_by_stage": {},
        "counts_by_point_type": {point_type: 0 for point_type in POINT_TYPES},
        "screening_policy": _screening_policy_document(),
        "screening_policy_id": _screening_policy_id(),
        "sectors": [],
        "sector_strength_evidence": None,
        "sector_strength_evidence_revision": None,
        "sector_member_history_diagnostics": None,
        "monitor_instrument_exclusions": [],
        "signals": [],
        "risk_limits": _risk_limits_document(RiskLimits()),
        "scan_audit": {
            "sector_discovered_count": 0,
            "sector_completed_count": 0,
            "sector_excluded_count": 0,
            "sector_failed_count": 0,
            "sector_resolved_count": 0,
            "sector_completion_ratio": "0",
            "sector_resolution_ratio": "0",
            "sector_failure_counts": {},
            "sector_exclusion_counts": {},
            "planned_symbol_count": 0,
            "completed_symbol_count": 0,
            "completion_ratio": "0",
            "full_market_history_scan": False,
            "background_full_refresh_required": True,
            "batch_duration_ms": 0,
            "sector_scan_duration_ms": 0,
            "stock_scan_duration_ms": 0,
            "stock_worker_count": config.stock_worker_count,
            "coverage_cycle_elapsed_ms": 0,
            "coverage_cycle_batch_count": 0,
            "coverage_cycle_started_at": None,
            "discovered_symbol_count": 0,
            "coverage_cycle_attempted_symbol_count": 0,
            "coverage_cycle_completed_symbol_count": 0,
            "coverage_cycle_excluded_symbol_count": 0,
            "coverage_cycle_failed_symbol_count": 0,
            "coverage_cycle_resolved_symbol_count": 0,
            "coverage_cycle_completion_ratio": "0",
            "coverage_cycle_resolution_ratio": "0",
            "coverage_cycle_progress_ratio": "0",
            "coverage_cycle_finalized_symbol_count": 0,
            "coverage_cycle_throughput_symbols_per_minute": None,
            "coverage_cycle_estimated_remaining_seconds": None,
            "stock_failure_counts": {},
            "stock_exclusion_counts": {},
            "monitor_instrument_exclusion_count": 0,
        },
        "data_quality": {
            "complete": False,
            "stale": True,
            "failure_codes": ["not_scanned"],
        },
        "backtest_verdict": {
            "live_ready": False,
            "status": "evidence_unavailable",
        },
        "errors": [],
        "sector_exclusions": [],
        "coverage_manifest": {
            "schema": COVERAGE_MANIFEST_SCHEMA,
            "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "coverage_epoch_id": None,
            "screening_policy_id": _screening_policy_id(),
            "source_cutoff": None,
            "market_data_as_of": None,
            "universe_revision": None,
            "sector_catalog_revision": None,
            "sector_strength_evidence_revision": None,
            "superseded_coverage_epoch_id": None,
            "superseded_market_data_as_of": None,
            "discovered_codes": [],
            "completed_codes": [],
            "excluded_codes": [],
            "failed_codes": [],
            "exclusions": [],
            "discarded_out_of_scope_retry_codes": [],
            "pending_frequencies": {},
            "backoff_frequencies": {},
            "deferred_frequencies": {},
            "complete": False,
            "batch_count": 0,
        },
        "snapshot_content_sha256": None,
    }


def _risk_limits_document(limits: RiskLimits) -> dict[str, object]:
    return {
        "base_trade_risk": str(limits.base_trade_risk),
        "max_symbol_fraction": str(limits.max_symbol_fraction),
        "max_sector_fraction": str(limits.max_sector_fraction),
        "max_portfolio_heat": str(limits.max_portfolio_heat),
        "first_drawdown": str(limits.first_drawdown),
        "second_drawdown": str(limits.second_drawdown),
        "stop_drawdown": str(limits.stop_drawdown),
    }


def _sector_document(
    assessment: SectorAssessment,
    *,
    ordinal: int | None,
) -> dict[str, object]:
    return sector_decision_document(assessment, ordinal=ordinal)


_SECTOR_CONTEXT_DOCUMENT_FIELDS = frozenset(
    {
        "frequency",
        "direction",
        "disposition",
        "hard_block",
        "dominant_point_id",
        "dominant_point_type",
        "reason_codes",
        "observed_at",
    }
)
_SECTOR_DOCUMENT_FIELDS = frozenset(
    {
        "sector_id",
        "sector_name",
        "eligible",
        "hard_block",
        "regime",
        "rank",
        "rank_score",
        "rank_components",
        "reason_codes",
        "horizontal_strength",
        "horizontal_rank",
        "strength_anchor_session",
        "strength_member_count",
        "strength_source_revision",
        "strength_reason_codes",
        "context_30m",
        "context_5m",
        "context_1m",
    }
)


def _sector_context_from_document(
    value: object,
    *,
    frequency: str,
) -> TimeframeContext | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _SECTOR_CONTEXT_DOCUMENT_FIELDS:
        raise ValueError("cached sector context document is invalid")
    raw_reasons = value.get("reason_codes")
    direction = value.get("direction")
    disposition = value.get("disposition")
    dominant_point_id = value.get("dominant_point_id")
    dominant_point_type = value.get("dominant_point_type")
    if (
        value.get("frequency") != frequency
        or direction not in {"up", "down", "neutral"}
        or disposition not in {"supportive", "neutral", "hostile"}
        or type(value.get("hard_block")) is not bool
        or dominant_point_id is not None
        and (not isinstance(dominant_point_id, str) or not dominant_point_id)
        or dominant_point_type is not None
        and dominant_point_type
        not in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
        or not isinstance(raw_reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in raw_reasons)
        or len(raw_reasons) != len(set(raw_reasons))
    ):
        raise ValueError("cached sector context document is invalid")
    try:
        observed_at = normalize_datetime(
            datetime.fromisoformat(str(value["observed_at"])),
            "cached sector context observed_at",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cached sector context document is invalid") from exc
    return TimeframeContext(
        frequency=frequency,
        direction=direction,
        disposition=disposition,
        hard_block=bool(value["hard_block"]),
        dominant_point_id=dominant_point_id,
        dominant_point_type=dominant_point_type,
        reason_codes=tuple(raw_reasons),
        observed_at=observed_at,
    )


def _sector_assessment_from_document(
    value: object,
) -> tuple[SectorAssessment, int | None]:
    if not isinstance(value, Mapping) or set(value) != _SECTOR_DOCUMENT_FIELDS:
        raise ValueError("cached sector assessment document is invalid")
    raw_components = value.get("rank_components")
    raw_reasons = value.get("reason_codes")
    raw_strength_reasons = value.get("strength_reason_codes")
    rank = value.get("rank")
    horizontal_rank = value.get("horizontal_rank")
    if (
        not isinstance(value.get("sector_id"), str)
        or not value.get("sector_id")
        or not isinstance(value.get("sector_name"), str)
        or not value.get("sector_name")
        or type(value.get("eligible")) is not bool
        or type(value.get("hard_block")) is not bool
        or value.get("regime") not in {"supportive", "neutral", "hostile"}
        or rank is not None
        and (type(rank) is not int or rank <= 0)
        or type(value.get("rank_score")) is not int
        or not isinstance(raw_components, Mapping)
        or any(
            not isinstance(name, str) or not name or type(component) is not int
            for name, component in raw_components.items()
        )
        or not isinstance(raw_reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in raw_reasons)
        or len(raw_reasons) != len(set(raw_reasons))
        or horizontal_rank is not None
        and (type(horizontal_rank) is not int or horizontal_rank <= 0)
        or type(value.get("strength_member_count")) is not int
        or int(value.get("strength_member_count")) < 0
        or not isinstance(raw_strength_reasons, list)
        or any(
            not isinstance(reason, str) or not reason for reason in raw_strength_reasons
        )
        or len(raw_strength_reasons) != len(set(raw_strength_reasons))
    ):
        raise ValueError("cached sector assessment document is invalid")
    raw_strength = value.get("horizontal_strength")
    raw_anchor = value.get("strength_anchor_session")
    raw_revision = value.get("strength_source_revision")
    if raw_revision is not None and (
        not isinstance(raw_revision, str) or not raw_revision
    ):
        raise ValueError("cached sector strength identity is invalid")
    try:
        strength = None if raw_strength is None else Decimal(str(raw_strength))
        anchor = None if raw_anchor is None else date.fromisoformat(str(raw_anchor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("cached sector strength evidence is invalid") from exc
    assessment = SectorAssessment(
        sector_id=str(value["sector_id"]),
        sector_name=str(value["sector_name"]),
        eligible=bool(value["eligible"]),
        hard_block=bool(value["hard_block"]),
        regime=value["regime"],
        rank_components=tuple(
            (str(name), int(component)) for name, component in raw_components.items()
        ),
        reason_codes=tuple(raw_reasons),
        thirty_context=_sector_context_from_document(
            value.get("context_30m"),
            frequency="30m",
        ),
        five_context=_sector_context_from_document(
            value.get("context_5m"),
            frequency="5m",
        ),
        one_context=_sector_context_from_document(
            value.get("context_1m"),
            frequency="1m",
        ),
        horizontal_strength=strength,
        horizontal_rank=horizontal_rank,
        strength_anchor_session=anchor,
        strength_member_count=int(value["strength_member_count"]),
        strength_source_revision=raw_revision,
        strength_reason_codes=tuple(raw_strength_reasons),
    )
    if assessment.rank_score != value.get("rank_score"):
        raise ValueError("cached sector rank score is invalid")
    return assessment, rank


def _sector_failure_from_document(value: Mapping[str, object]) -> SectorAnalysisFailure:
    allowed = {
        "sector_id",
        "code",
        "error_type",
        "reason",
        "detail_code",
        "catalog_member_count",
        "universe_member_count",
    }
    if set(value) - allowed:
        raise ValueError("cached sector failure document is invalid")
    try:
        failure = SectorAnalysisFailure(
            sector_id=str(value["sector_id"]),
            code=str(value["code"]),
            error_type=str(value["error_type"]),
            reason=str(value["reason"]),
            detail_code=value.get("detail_code"),
            catalog_member_count=value.get("catalog_member_count"),
            universe_member_count=value.get("universe_member_count"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cached sector failure document is invalid") from exc
    if _sector_failure_document(failure) != dict(value):
        raise ValueError("cached sector failure document changed on restoration")
    return failure


def _sector_exclusion_from_document(
    value: Mapping[str, object],
) -> SectorAnalysisExclusion:
    expected_fields = {
        "sector_id",
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "reason",
        "detail_code",
        "catalog_member_count",
        "universe_member_count",
        "required_member_count",
        "deterministic_for_catalog_revision",
        "retry_policy",
    }
    if (
        set(value) != expected_fields
        or value.get("exclusion_type") != "sector_analysis_exclusion"
        or value.get("eligibility") != "MINIMUM_SECTOR_MEMBERS_NOT_MET"
        or value.get("deterministic_for_catalog_revision") is not True
        or value.get("retry_policy") != "NEXT_SECTOR_CATALOG_REVISION"
    ):
        raise ValueError("cached sector exclusion document is invalid")
    try:
        exclusion = SectorAnalysisExclusion(
            sector_id=str(value["sector_id"]),
            code=str(value["code"]),
            reason_code=str(value["reason_code"]),
            reason=str(value["reason"]),
            detail_code=str(value["detail_code"]),
            catalog_member_count=value["catalog_member_count"],
            universe_member_count=value["universe_member_count"],
            required_member_count=value["required_member_count"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cached sector exclusion document is invalid") from exc
    if _sector_exclusion_document(exclusion) != dict(value):
        raise ValueError("cached sector exclusion document changed on restoration")
    return exclusion


def _coverage_sector_state_from_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[SectorAssessmentBatch, dict[str, tuple[str, ...]] | None]:
    """Restore the exact sector evidence frozen for one resumable coverage epoch.

    A stock signal embeds the sector assessment that affected its decision.  A
    multi-batch scan must therefore reuse the same assessment documents after a
    process restart; re-running the sector analyzer at the same bar cutoff can
    produce a different structural point identity and silently mix two evidence
    identities in one otherwise unchanged epoch.
    """

    raw_sectors = snapshot.get("sectors")
    raw_errors = snapshot.get("errors")
    raw_exclusions = snapshot.get("sector_exclusions")
    audit = snapshot.get("scan_audit")
    manifest = snapshot.get("coverage_manifest")
    if (
        not isinstance(raw_sectors, list)
        or not isinstance(raw_errors, list)
        or not isinstance(raw_exclusions, list)
        or not isinstance(audit, Mapping)
        or not isinstance(manifest, Mapping)
    ):
        raise ValueError("cached coverage sector state is unavailable")
    restored = [_sector_assessment_from_document(value) for value in raw_sectors]
    assessments = tuple(
        sorted(
            (assessment for assessment, _rank in restored),
            key=lambda row: row.sector_id,
        )
    )
    if len({assessment.sector_id for assessment in assessments}) != len(assessments):
        raise ValueError("cached sector assessments are not unique")
    failures = tuple(
        _sector_failure_from_document(value)
        for value in raw_errors
        if isinstance(value, Mapping) and "sector_id" in value
    )
    if any(not isinstance(value, Mapping) for value in raw_exclusions):
        raise ValueError("cached sector exclusions are invalid")
    exclusions = tuple(
        _sector_exclusion_from_document(value)
        for value in raw_exclusions
        if isinstance(value, Mapping)
    )
    raw_strength = snapshot.get("sector_strength_evidence")
    strength_evidence = (
        None
        if raw_strength is None
        else sector_strength_batch_from_evidence_document(raw_strength)
    )
    strength_revision = snapshot.get("sector_strength_evidence_revision")
    if (
        strength_evidence is None
        and strength_revision is not None
        or strength_evidence is not None
        and strength_revision != strength_evidence.evidence_revision
    ):
        raise ValueError("cached sector strength evidence identity is invalid")
    try:
        discovered_count = int(audit["sector_discovered_count"])
        completed_count = int(audit["sector_completed_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cached sector completion counts are invalid") from exc
    failure_counts: dict[str, int] = {}
    for failure in failures:
        failure_counts[failure.error_type] = (
            failure_counts.get(failure.error_type, 0) + 1
        )
    exclusion_counts: dict[str, int] = {}
    for exclusion in exclusions:
        exclusion_counts[exclusion.reason_code] = (
            exclusion_counts.get(exclusion.reason_code, 0) + 1
        )
    if audit.get("sector_failure_counts") != dict(
        sorted(failure_counts.items())
    ) or audit.get("sector_exclusion_counts") != dict(sorted(exclusion_counts.items())):
        raise ValueError("cached sector disposition counts are invalid")
    catalog_revision = manifest.get("sector_catalog_revision")
    if not isinstance(catalog_revision, str) or not catalog_revision.startswith(
        "sha256:"
    ):
        raise ValueError("cached sector catalog identity is invalid")
    batch = SectorAssessmentBatch(
        assessments=assessments,
        discovered_count=discovered_count,
        completed_count=completed_count,
        failure_counts=tuple(sorted(failure_counts.items())),
        errors=failures,
        exclusion_counts=tuple(sorted(exclusion_counts.items())),
        exclusions=exclusions,
        catalog_revision=catalog_revision,
        strength_evidence=strength_evidence,
    )
    failed_ids = {value.sector_id for value in (*failures, *exclusions)}
    ranked_ordinals = {
        row.assessment.sector_id: row.ordinal
        for row in rank_sectors(
            tuple(
                assessment
                for assessment in assessments
                if assessment.sector_id not in failed_ids
            )
        )
    }
    restored_by_id = {assessment.sector_id: assessment for assessment in assessments}
    for raw, (_assessment, raw_rank) in zip(raw_sectors, restored):
        assert isinstance(raw, Mapping)
        sector_id = str(raw["sector_id"])
        expected_rank = ranked_ordinals.get(sector_id)
        if raw_rank != expected_rank or _sector_document(
            restored_by_id[sector_id],
            ordinal=expected_rank,
        ) != dict(raw):
            raise ValueError("cached sector assessment changed on restoration")

    members: dict[str, tuple[str, ...]] | None = None
    if strength_evidence is not None:
        evidence_document = strength_evidence.evidence_document()
        members = {
            str(row["sector_id"]): tuple(str(code) for code in row["member_symbols"])
            for row in evidence_document["sectors"]
        }
        if set(members) != {assessment.sector_id for assessment in assessments}:
            raise ValueError("cached sector membership coverage is invalid")
    return batch, members


def _chart_urls(code: str) -> dict[str, str]:
    intervals = {"d": "D", "30m": "30", "5m": "5", "1m": "1"}
    return {
        frequency: (f"/?market=a&code={code}&layout=single&intervals={interval}")
        for frequency, interval in intervals.items()
    }


def _signal_document(
    item: EvaluatedSignal,
    *,
    previous_stage: str | None,
    name: str | None,
    decision_core_id: str,
    selection_sources: tuple[str, ...],
    higher_timeframe_gates: HigherTimeframeGateBundle | None = None,
) -> dict[str, object]:
    document = serialize_evaluated_signal(
        item,
        previous_stage=previous_stage,
        name=name,
        decision_core_id=decision_core_id,
        selection_sources=selection_sources,
    )
    document["chart_urls"] = _chart_urls(str(document["code"]))
    if higher_timeframe_gates is not None and (
        higher_timeframe_gates.market.session_evidence is not None
        or (higher_timeframe_gates.sector.session_evidence is not None)
        or higher_timeframe_gates.symbol.session_evidence is not None
    ):
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        market_evidence = (
            higher_timeframe_gates.market.session_evidence
            or HigherTimeframeSessionEvidence.unavailable()
        )
        symbol_evidence = (
            higher_timeframe_gates.symbol.session_evidence
            or HigherTimeframeSessionEvidence.unavailable()
        )
        sector_evidence = (
            higher_timeframe_gates.sector.session_evidence
        ) or HigherTimeframeSessionEvidence.unavailable()
        # Presentation-only extension: these facts explain an already
        # fail-closed M/W/D result and never participate in decision identity.
        # The extension carries its own contract so coverage can continue
        # without replaying already-completed symbols merely to add prose.
        risk["session_evidence_contract_id"] = (
            HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
        )
        risk["market_session_evidence"] = market_evidence.document()
        risk["sector_session_evidence"] = sector_evidence.document()
        risk["symbol_session_evidence"] = symbol_evidence.document()
    if higher_timeframe_gates is not None and (
        higher_timeframe_gates.market.warmup_evidence is not None
        or (higher_timeframe_gates.sector.warmup_evidence is not None)
        or higher_timeframe_gates.symbol.warmup_evidence is not None
    ):
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        risk["warmup_evidence_contract_id"] = (
            QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
        )
        risk["market_warmup_evidence"] = (
            None
            if higher_timeframe_gates.market.warmup_evidence is None
            else higher_timeframe_gates.market.warmup_evidence.document()
        )
        risk["sector_warmup_evidence"] = (
            None
            if higher_timeframe_gates.sector.warmup_evidence is None
            else higher_timeframe_gates.sector.warmup_evidence.document()
        )
        risk["symbol_warmup_evidence"] = (
            None
            if higher_timeframe_gates.symbol.warmup_evidence is None
            else higher_timeframe_gates.symbol.warmup_evidence.document()
        )
        sector_gate = higher_timeframe_gates.sector
        if sector_gate is not None and sector_gate.sector_source_mode is not None:
            risk["sector_higher_timeframe_source_mode"] = sector_gate.sector_source_mode
            risk["sector_strict_same_5m_warmup_evidence"] = (
                None
                if sector_gate.sector_strict_same_base_warmup_evidence is None
                else sector_gate.sector_strict_same_base_warmup_evidence.document()
            )
            risk["sector_strict_same_5m_source_coverage_evidence"] = (
                None
                if sector_gate.sector_strict_same_base_source_coverage_evidence is None
                else sector_gate.sector_strict_same_base_source_coverage_evidence.document()
            )
            risk["sector_research_bridge_parameter_set_id"] = (
                sector_gate.sector_research_bridge_parameter_set_id
            )
    if higher_timeframe_gates is not None and (
        higher_timeframe_gates.market.warmup_convergence_evidence is not None
        or (higher_timeframe_gates.sector.warmup_convergence_evidence is not None)
        or higher_timeframe_gates.symbol.warmup_convergence_evidence is not None
    ):
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        risk["warmup_convergence_contract_id"] = WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        risk["warmup_convergence_diagnostic_contract_id"] = (
            WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
        )
        risk["warmup_mapping_supply_diagnostic_contract_id"] = (
            WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
        )
        risk["warmup_structure_lineage_diagnostic_contract_id"] = (
            WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
        )
        risk["market_warmup_convergence_evidence"] = (
            None
            if higher_timeframe_gates.market.warmup_convergence_evidence is None
            else higher_timeframe_gates.market.warmup_convergence_evidence.document()
        )
        risk["sector_warmup_convergence_evidence"] = (
            None
            if higher_timeframe_gates.sector.warmup_convergence_evidence is None
            else higher_timeframe_gates.sector.warmup_convergence_evidence.document()
        )
        risk["symbol_warmup_convergence_evidence"] = (
            None
            if higher_timeframe_gates.symbol.warmup_convergence_evidence is None
            else higher_timeframe_gates.symbol.warmup_convergence_evidence.document()
        )
        for subject, gate in (
            ("market", higher_timeframe_gates.market),
            ("sector", higher_timeframe_gates.sector),
            ("symbol", higher_timeframe_gates.symbol),
        ):
            convergence = None if gate is None else gate.warmup_convergence_evidence
            risk[f"{subject}_warmup_convergence_diagnostic_evidence"] = (
                None
                if convergence is None or convergence.diagnostic is None
                else convergence.diagnostic.document()
            )
            risk[f"{subject}_warmup_mapping_supply_diagnostic_evidence"] = (
                None
                if convergence is None or convergence.mapping_supply_diagnostic is None
                else convergence.mapping_supply_diagnostic.document()
            )
            risk[f"{subject}_warmup_structure_lineage_diagnostic_evidence"] = (
                None
                if convergence is None
                or convergence.structure_lineage_diagnostic is None
                else convergence.structure_lineage_diagnostic.document()
            )
        sector_gate = higher_timeframe_gates.sector
        if sector_gate is not None and sector_gate.sector_source_mode is not None:
            risk["sector_strict_same_5m_warmup_convergence_evidence"] = (
                None
                if sector_gate.sector_strict_same_base_warmup_convergence_evidence
                is None
                else sector_gate.sector_strict_same_base_warmup_convergence_evidence.document()
            )
            strict_convergence = (
                sector_gate.sector_strict_same_base_warmup_convergence_evidence
            )
            risk["sector_strict_same_5m_warmup_convergence_diagnostic_evidence"] = (
                None
                if strict_convergence is None or strict_convergence.diagnostic is None
                else strict_convergence.diagnostic.document()
            )
            risk["sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence"] = (
                None
                if strict_convergence is None
                or strict_convergence.mapping_supply_diagnostic is None
                else strict_convergence.mapping_supply_diagnostic.document()
            )
            risk[
                "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence"
            ] = (
                None
                if strict_convergence is None
                or strict_convergence.structure_lineage_diagnostic is None
                else strict_convergence.structure_lineage_diagnostic.document()
            )
    if higher_timeframe_gates is not None and (
        higher_timeframe_gates.market.native_daily_reconciliation_evidence is not None
        or higher_timeframe_gates.symbol.native_daily_reconciliation_evidence
        is not None
    ):
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        risk["native_daily_reconciliation_contract_id"] = (
            QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
        )
        risk["market_native_daily_reconciliation_evidence"] = (
            None
            if higher_timeframe_gates.market.native_daily_reconciliation_evidence
            is None
            else higher_timeframe_gates.market.native_daily_reconciliation_evidence.document()
        )
        # The optional sector native-daily path is intentionally unreconciled,
        # capped at AMBER and therefore is not a *reconciliation* evidence.
        # Never mislabel it as the symbol/benchmark overlap-certified bridge.
        risk["sector_native_daily_reconciliation_evidence"] = None
        risk["symbol_native_daily_reconciliation_evidence"] = (
            None
            if higher_timeframe_gates.symbol.native_daily_reconciliation_evidence
            is None
            else higher_timeframe_gates.symbol.native_daily_reconciliation_evidence.document()
        )
    if higher_timeframe_gates is not None and (
        higher_timeframe_gates.market.native_daily_calendar_coverage_evidence
        is not None
        or higher_timeframe_gates.symbol.native_daily_calendar_coverage_evidence
        is not None
    ):
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        risk["native_daily_calendar_coverage_contract_id"] = (
            QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
        )
        risk["market_native_daily_calendar_coverage_evidence"] = (
            None
            if higher_timeframe_gates.market.native_daily_calendar_coverage_evidence
            is None
            else higher_timeframe_gates.market.native_daily_calendar_coverage_evidence.document()
        )
        # Sector M/W/D is derived from its component 5m composite and has a
        # separate strict same-base coverage contract.
        risk["sector_native_daily_calendar_coverage_evidence"] = None
        risk["symbol_native_daily_calendar_coverage_evidence"] = (
            None
            if higher_timeframe_gates.symbol.native_daily_calendar_coverage_evidence
            is None
            else higher_timeframe_gates.symbol.native_daily_calendar_coverage_evidence.document()
        )
    if higher_timeframe_gates is not None:
        risk = document.get("higher_timeframe_risk")
        if not isinstance(risk, dict):
            raise TypeError("serialized higher-timeframe risk document is invalid")
        risk.setdefault(
            "session_evidence_contract_id",
            HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
        )
        for subject in ("market", "sector", "symbol"):
            risk.setdefault(
                f"{subject}_session_evidence",
                HigherTimeframeSessionEvidence.unavailable().document(),
            )
        risk.setdefault(
            "warmup_evidence_contract_id",
            QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
        )
        risk.setdefault(
            "warmup_convergence_contract_id",
            WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
        )
        risk.setdefault(
            "warmup_convergence_diagnostic_contract_id",
            WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
        )
        risk.setdefault(
            "warmup_mapping_supply_diagnostic_contract_id",
            WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
        )
        risk.setdefault(
            "warmup_structure_lineage_diagnostic_contract_id",
            WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
        )
        risk.setdefault(
            "native_daily_reconciliation_contract_id",
            QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
        )
        risk.setdefault(
            "native_daily_calendar_coverage_contract_id",
            QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
        )
        for subject in ("market", "sector", "symbol"):
            for suffix in (
                "warmup_evidence",
                "warmup_convergence_evidence",
                "warmup_convergence_diagnostic_evidence",
                "warmup_mapping_supply_diagnostic_evidence",
                "warmup_structure_lineage_diagnostic_evidence",
                "native_daily_reconciliation_evidence",
                "native_daily_calendar_coverage_evidence",
            ):
                risk.setdefault(f"{subject}_{suffix}", None)
        sector_gate = higher_timeframe_gates.sector
        if sector_gate.sector_source_mode is not None:
            risk.setdefault(
                "sector_higher_timeframe_source_mode",
                sector_gate.sector_source_mode,
            )
            risk.setdefault(
                "sector_strict_same_5m_warmup_evidence",
                None
                if sector_gate.sector_strict_same_base_warmup_evidence is None
                else sector_gate.sector_strict_same_base_warmup_evidence.document(),
            )
            risk.setdefault(
                "sector_strict_same_5m_source_coverage_evidence",
                None
                if sector_gate.sector_strict_same_base_source_coverage_evidence is None
                else sector_gate.sector_strict_same_base_source_coverage_evidence.document(),
            )
            risk.setdefault(
                "sector_research_bridge_parameter_set_id",
                sector_gate.sector_research_bridge_parameter_set_id,
            )
            strict_convergence = (
                sector_gate.sector_strict_same_base_warmup_convergence_evidence
            )
            risk.setdefault(
                "sector_strict_same_5m_warmup_convergence_evidence",
                None if strict_convergence is None else strict_convergence.document(),
            )
            risk.setdefault(
                "sector_strict_same_5m_warmup_convergence_diagnostic_evidence",
                None
                if strict_convergence is None or strict_convergence.diagnostic is None
                else strict_convergence.diagnostic.document(),
            )
            risk.setdefault(
                "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence",
                None
                if strict_convergence is None
                or strict_convergence.mapping_supply_diagnostic is None
                else strict_convergence.mapping_supply_diagnostic.document(),
            )
            risk.setdefault(
                "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence",
                None
                if strict_convergence is None
                or strict_convergence.structure_lineage_diagnostic is None
                else strict_convergence.structure_lineage_diagnostic.document(),
            )
    return document


_PRESENTATION_RISK_FIELDS = (
    "market_gate",
    "sector_gate",
    "symbol_gate",
    "new_entry_requires_all_green",
    "reason_codes",
    "market_reason_codes",
    "sector_reason_codes",
    "symbol_reason_codes",
    "sector_higher_timeframe_source_mode",
    "sector_research_bridge_parameter_set_id",
)
_PRESENTATION_SIGNAL_FIELDS = (
    "signal_id",
    "decision_document_id",
    "code",
    "name",
    "side",
    "point_type",
    "lifecycle_stage",
    "observed_at",
    "observation_lane",
    "monitor_observed_at",
    "realtime_observation",
    "structural_stop",
    "risk_multiplier",
    "selection_sources",
    "sector_triggered",
    "entry_allowed",
    "exit_allowed",
    "decision_reasons",
)
_PRESENTATION_SIGNAL_SECTOR_FIELDS = (
    "sector_id",
    "sector_name",
)
_PRESENTATION_CONTEXT_FIELDS = (
    "direction",
    "disposition",
    "dominant_point_type",
    "hard_block",
    "reason_codes",
)
_PRESENTATION_SETUP_FIELDS = (
    "status",
    "point_type",
    "center_ordinal",
    "contains_unfinished_segment",
    "invalidation_price",
    "evidence_codes",
    "missing_conditions",
)
_PRESENTATION_TRIGGER_FIELDS = (
    "status",
    "point_type",
    "evidence_codes",
    "missing_conditions",
)
_PRESENTATION_SIGNAL_WARMUP_FIELDS = (
    "converged",
    "reason_codes",
)
_PRESENTATION_SIGNAL_WARMUP_ROW_FIELDS = (
    "frequency",
    "converged",
    "full_bar_count",
    "suffix_bar_count",
)
_PRESENTATION_SIGNAL_WARMUP_DIFFERENCE_FIELDS = (
    "frequency",
    "difference_codes",
)
_PRESENTATION_WARMUP_FIELDS = (
    "contract_id",
    "converged",
    "full_daily_bar_count",
    "suffix_daily_bar_count",
    "required_daily_bar_count",
    "reason_code",
    "live_status",
)
_PRESENTATION_NATIVE_DAILY_FIELDS = (
    "contract_id",
    "symbol",
    "native_daily_bar_count",
    "one_minute_daily_bar_count",
    "overlap_session_count",
    "first_overlap_session",
    "last_overlap_session",
    "price_tolerance_quanta",
    "max_observed_price_difference_quanta",
    "all_overlap_ohlcv_within_declared_tolerance",
    "live_status",
)
_PRESENTATION_NATIVE_CALENDAR_FIELDS = (
    "contract_id",
    "symbol",
    "status",
    "native_daily_bar_count",
    "expected_calendar_session_count",
    "native_first_session",
    "native_last_session",
    "unexplained_calendar_only_sessions",
    "native_only_sessions",
    "live_status",
)
_PRESENTATION_SOURCE_COVERAGE_FIELDS = (
    "contract_id",
    "base_frequency",
    "prefix_only",
    "live_status",
    "completed_daily_bar_count",
    "required_daily_bar_count",
    "warmup_reason_code",
    "first_completed_session",
    "last_completed_session",
    "remaining_daily_bar_count",
    "missing_leading_calendar_session_count",
    "boundary_status",
)


def _presentation_fields(
    value: object,
    fields: tuple[str, ...],
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {field: copy.deepcopy(value[field]) for field in fields if field in value}


def _presentation_rows(
    value: object,
    fields: tuple[str, ...],
) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict[str, object]] = []
    for raw in value:
        row = _presentation_fields(raw, fields)
        if row is not None:
            rows.append(row)
    return rows


def _presentation_signal_document(
    signal: Mapping[str, object],
) -> dict[str, object]:
    """Build the bounded live-page projection of one audit signal.

    The immutable screening publication and the human-review detail endpoint
    retain the complete evidence tree.  The live list only needs identity,
    filtering, the four-period summary and decision-relevant reason codes.
    Copying every audit field made a 1,664-row response roughly 27 MiB and
    caused browsers/extensions to abandon the request before rendering any
    result.  Keep this projection as an explicit allow-list so new audit fields
    cannot silently inflate the minute-polled page again.
    """

    document = _presentation_fields(signal, _PRESENTATION_SIGNAL_FIELDS) or {}
    document["sector"] = (
        _presentation_fields(
            signal.get("sector"),
            _PRESENTATION_SIGNAL_SECTOR_FIELDS,
        )
        or {}
    )
    for key in ("context_d", "context_30m"):
        document[key] = (
            _presentation_fields(signal.get(key), _PRESENTATION_CONTEXT_FIELDS) or {}
        )
    document["setup_5m"] = (
        _presentation_fields(signal.get("setup_5m"), _PRESENTATION_SETUP_FIELDS) or {}
    )
    effective_stage = lifecycle_stage_from_signal(signal)
    if effective_stage is not None:
        document["lifecycle_stage"] = effective_stage
    raw_trigger = signal.get("trigger_1m")
    document["trigger_1m"] = (
        None
        if raw_trigger is None
        else _presentation_fields(raw_trigger, _PRESENTATION_TRIGGER_FIELDS) or {}
    )
    raw_warmup = signal.get("warmup")
    warmup = _presentation_fields(raw_warmup, _PRESENTATION_SIGNAL_WARMUP_FIELDS) or {}
    if isinstance(raw_warmup, Mapping):
        warmup["by_frequency"] = _presentation_rows(
            raw_warmup.get("by_frequency"),
            _PRESENTATION_SIGNAL_WARMUP_ROW_FIELDS,
        )
        warmup["difference_codes_by_frequency"] = _presentation_rows(
            raw_warmup.get("difference_codes_by_frequency"),
            _PRESENTATION_SIGNAL_WARMUP_DIFFERENCE_FIELDS,
        )
    document["warmup"] = warmup
    raw_risk = signal.get("higher_timeframe_risk")
    if isinstance(raw_risk, Mapping):
        risk = {
            field: copy.deepcopy(raw_risk[field])
            for field in _PRESENTATION_RISK_FIELDS
            if field in raw_risk
        }
        strict_warmup_key = "sector_strict_same_5m_warmup_evidence"
        if strict_warmup_key in raw_risk:
            risk[strict_warmup_key] = _presentation_fields(
                raw_risk[strict_warmup_key],
                _PRESENTATION_WARMUP_FIELDS,
            )
        strict_coverage_key = "sector_strict_same_5m_source_coverage_evidence"
        if strict_coverage_key in raw_risk:
            risk[strict_coverage_key] = _presentation_fields(
                raw_risk[strict_coverage_key],
                _PRESENTATION_SOURCE_COVERAGE_FIELDS,
            )
        document["higher_timeframe_risk"] = risk
    document["presentation_projection"] = True
    document["full_audit_evidence_embedded"] = False
    return document


def _stock_analysis_error_document(
    code: str,
    error: Exception,
) -> dict[str, object]:
    """Normalize stock failures without relying on UI parsing exception text.

    The isolated worker exposes its remote exception type and original message
    as attributes.  Direct/in-memory gateways used by tests and research tools
    still work through the ordinary exception name and message.  Known market
    data failures are deterministic for the frozen coverage cutoff; runtime
    transport failures remain explicitly retryable after worker backoff.
    """

    remote_error_type = getattr(error, "remote_error_type", type(error).__name__)
    remote_message = str(getattr(error, "remote_message", str(error)))[:400]
    reason = str(error)[:400]
    known_data_reasons = (
        ("kline frame is unavailable", "KLINE_FRAME_UNAVAILABLE"),
        (
            "kline frame contains invalid market facts",
            "KLINE_FRAME_INVALID_MARKET_FACTS",
        ),
        (
            "kline frame does not meet minimum history",
            "KLINE_MINIMUM_HISTORY_NOT_MET",
        ),
        ("structure_bundle_stale", "STRUCTURE_BUNDLE_STALE"),
    )
    reason_code = next(
        (
            value
            for fragment, value in known_data_reasons
            if fragment in remote_message or fragment in reason
        ),
        None,
    )
    if reason_code is not None:
        failure_class = "MARKET_DATA_REJECTION"
        retry_policy = "NEXT_MARKET_DATA_EPOCH"
        deterministic_for_epoch = True
    else:
        runtime_codes = {
            "NativeScreeningWorkerTimeout": "NATIVE_WORKER_TIMEOUT",
            "NativeScreeningWorkerUnavailable": "NATIVE_WORKER_UNAVAILABLE",
            "NativeScreeningWorkerProtocolError": "NATIVE_WORKER_PROTOCOL_ERROR",
        }
        reason_code = runtime_codes.get(type(error).__name__)
        if reason_code is not None:
            failure_class = "RUNTIME_FAILURE"
            retry_policy = "NEXT_REFRESH_AFTER_BACKOFF"
        else:
            reason_code = "STOCK_ANALYSIS_UNCLASSIFIED"
            failure_class = "UNCLASSIFIED_FAILURE"
            retry_policy = "NEXT_COVERAGE_CYCLE"
        deterministic_for_epoch = False
    return {
        "code": code,
        "error_type": "stock_analysis_error",
        "reason_code": reason_code,
        "failure_class": failure_class,
        "retry_policy": retry_policy,
        "deterministic_for_coverage_epoch": deterministic_for_epoch,
        "remote_error_type": str(remote_error_type),
        "reason": reason,
    }


def _is_coverage_exclusion(error: Mapping[str, object]) -> bool:
    """Return whether a rejection is an audited epoch-local eligibility fact."""

    return bool(
        error.get("reason_code") in COVERAGE_EXCLUSION_REASON_CODES
        and error.get("retry_policy") == "NEXT_MARKET_DATA_EPOCH"
        and error.get("deterministic_for_coverage_epoch") is True
    )


def _stock_analysis_exclusion_document(
    error: Mapping[str, object],
) -> dict[str, object]:
    """Convert minimum-history rejection into a non-success exclusion.

    This deliberately does not lower the history threshold and does not mark
    the symbol completed.  It only distinguishes an expected, deterministic
    universe eligibility outcome from a transport or market-data failure.
    """

    if not _is_coverage_exclusion(error):
        raise ValueError("stock analysis error is not a coverage exclusion")
    return {
        "code": str(error["code"]),
        "exclusion_type": "stock_analysis_exclusion",
        "eligibility": "INSUFFICIENT_MINIMUM_HISTORY",
        "reason_code": str(error["reason_code"]),
        "retry_policy": "NEXT_MARKET_DATA_EPOCH",
        "deterministic_for_coverage_epoch": True,
        "remote_error_type": str(error["remote_error_type"]),
        "reason": str(error["reason"]),
    }


def _sector_coverage_contract_is_valid(
    snapshot: Mapping[str, object],
) -> bool:
    """Recompute current sector dispositions from their exact documents."""

    if snapshot.get("sector_coverage_contract_id") != SECTOR_COVERAGE_CONTRACT_ID:
        return False
    audit = snapshot.get("scan_audit")
    raw_sectors = snapshot.get("sectors")
    raw_errors = snapshot.get("errors")
    raw_exclusions = snapshot.get("sector_exclusions")
    if (
        not isinstance(audit, Mapping)
        or not isinstance(raw_sectors, list)
        or not isinstance(raw_errors, list)
        or not isinstance(raw_exclusions, list)
    ):
        return False
    sector_ids: list[str] = []
    for sector in raw_sectors:
        if (
            not isinstance(sector, Mapping)
            or not isinstance(sector.get("sector_id"), str)
            or not sector.get("sector_id")
        ):
            return False
        sector_ids.append(str(sector["sector_id"]))
    if len(sector_ids) != len(set(sector_ids)):
        return False

    exclusion_ids: list[str] = []
    exclusion_counts: dict[str, int] = {}
    expected_exclusion_keys = {
        "sector_id",
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "reason",
        "detail_code",
        "catalog_member_count",
        "universe_member_count",
        "required_member_count",
        "deterministic_for_catalog_revision",
        "retry_policy",
    }
    try:
        for raw in raw_exclusions:
            if not isinstance(raw, Mapping) or set(raw) != expected_exclusion_keys:
                return False
            exclusion = SectorAnalysisExclusion(
                sector_id=str(raw.get("sector_id") or ""),
                code=str(raw.get("code") or ""),
                reason_code=str(raw.get("reason_code") or ""),
                reason=str(raw.get("reason") or ""),
                detail_code=str(raw.get("detail_code") or ""),
                catalog_member_count=raw.get("catalog_member_count"),
                universe_member_count=raw.get("universe_member_count"),
                required_member_count=raw.get("required_member_count"),
            )
            if dict(raw) != _sector_exclusion_document(exclusion):
                return False
            expected_detail = (
                "sector_catalog_members_missing"
                if exclusion.catalog_member_count == 0
                else (
                    "sector_constituent_count_below_minimum"
                    if exclusion.catalog_member_count < exclusion.required_member_count
                    else "sector_universe_member_coverage_insufficient"
                )
            )
            expected_reason = (
                f"catalog_members={exclusion.catalog_member_count}; "
                f"universe_members={exclusion.universe_member_count}; "
                f"required={exclusion.required_member_count}"
            )
            if (
                exclusion.detail_code != expected_detail
                or exclusion.reason != expected_reason
            ):
                return False
            exclusion_ids.append(exclusion.sector_id)
            exclusion_counts[exclusion.reason_code] = (
                exclusion_counts.get(exclusion.reason_code, 0) + 1
            )
    except (TypeError, ValueError):
        return False
    if exclusion_ids != sorted(set(exclusion_ids)):
        return False

    failure_ids: list[str] = []
    failure_counts: dict[str, int] = {}
    for raw in raw_errors:
        if not isinstance(raw, Mapping) or "sector_id" not in raw:
            continue
        sector_id = raw.get("sector_id")
        error_type = raw.get("error_type")
        reason = raw.get("reason")
        if (
            not isinstance(sector_id, str)
            or not sector_id
            or not isinstance(error_type, str)
            or not error_type
            or error_type == "sector_member_coverage_insufficient"
            or not isinstance(reason, str)
            or not reason
        ):
            return False
        failure_ids.append(sector_id)
        failure_counts[error_type] = failure_counts.get(error_type, 0) + 1
    sector_set = set(sector_ids)
    exclusion_set = set(exclusion_ids)
    failure_set = set(failure_ids)
    retained_snapshot_during_failed_attempt = (
        snapshot.get("scan_state") == "incomplete_not_published"
    )
    if (
        len(failure_ids) != len(failure_set)
        or exclusion_set & failure_set
        or (
            not retained_snapshot_during_failed_attempt
            and not exclusion_set.issubset(sector_set)
        )
        or (
            not retained_snapshot_during_failed_attempt
            and not failure_set.issubset(sector_set)
        )
    ):
        return False
    try:
        discovered = int(audit.get("sector_discovered_count"))
        completed = int(audit.get("sector_completed_count"))
        resolved = completed + len(exclusion_set)
        return bool(
            discovered >= 0
            and completed >= 0
            and (
                not retained_snapshot_during_failed_attempt
                or (
                    isinstance(snapshot.get("data_quality"), Mapping)
                    and snapshot["data_quality"].get("complete") is False
                )
            )
            and completed + len(exclusion_set) + len(failure_set) == discovered
            and (
                retained_snapshot_during_failed_attempt or discovered == len(sector_ids)
            )
            and int(audit.get("sector_excluded_count")) == len(exclusion_set)
            and int(audit.get("sector_failed_count")) == len(failure_set)
            and int(audit.get("sector_resolved_count")) == resolved
            and Decimal(str(audit.get("sector_completion_ratio")))
            == (
                Decimal("0")
                if discovered == 0
                else Decimal(completed) / Decimal(discovered)
            )
            and Decimal(str(audit.get("sector_resolution_ratio")))
            == (
                Decimal("0")
                if discovered == 0
                else Decimal(resolved) / Decimal(discovered)
            )
            and audit.get("sector_failure_counts")
            == dict(sorted(failure_counts.items()))
            and audit.get("sector_exclusion_counts")
            == dict(sorted(exclusion_counts.items()))
        )
    except (ArithmeticError, TypeError, ValueError):
        return False


def _sector_source_evidence_complete(snapshot: Mapping[str, object]) -> bool:
    """Return whether every cached sector document has explicit source evidence.

    The current contract always includes ``strength_source_revision``.  A
    ``None`` value explicitly records that no independently attested
    horizontal-strength source was available.
    """

    sectors = snapshot.get("sectors")
    signals = snapshot.get("signals")
    if (
        not isinstance(sectors, list)
        or not isinstance(signals, list)
        or "sector_strength_evidence" not in snapshot
        or "sector_strength_evidence_revision" not in snapshot
        or "sector_member_history_diagnostics" not in snapshot
    ):
        return False
    if not all(
        isinstance(value, Mapping) and "strength_source_revision" in value
        for value in sectors
    ):
        return False
    if not all(
        isinstance(value, Mapping)
        and isinstance(value.get("sector"), Mapping)
        and "strength_source_revision" in value["sector"]
        for value in signals
    ):
        return False
    raw_evidence = snapshot.get("sector_strength_evidence")
    evidence_revision = snapshot.get("sector_strength_evidence_revision")
    diagnostics = snapshot.get("sector_member_history_diagnostics")
    if raw_evidence is None:
        return evidence_revision is None and diagnostics is None
    if not isinstance(raw_evidence, Mapping):
        return False
    try:
        batch = sector_strength_batch_from_evidence_document(raw_evidence)
        expected_diagnostics = build_sector_member_history_diagnostics(batch)
    except (TypeError, ValueError):
        return False
    return bool(
        evidence_revision == batch.evidence_revision
        and diagnostics == expected_diagnostics
    )


def _cache_is_valid(
    value: object,
    config: TradingScreeningConfig,
    decision_core_id: str,
) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("schema") == SCHEMA
        and value.get("algorithm_id") == config.algorithm_id
        and value.get("structure_contract_id") == config.structure_contract_id
        and value.get("parameter_set_id") == config.parameter_set_id
        and value.get("read_only") is True
        and value.get("no_order_execution") is True
        and value.get("decision_core_id") == decision_core_id
        and value.get("screening_policy") == _screening_policy_document()
        and value.get("screening_policy_id") == _screening_policy_id()
        and value.get("signal_document_contract_id") == SIGNAL_DOCUMENT_CONTRACT_ID
        and isinstance(value.get("snapshot_content_sha256"), str)
        and value.get("snapshot_content_sha256")
        == live_screening_snapshot_content_sha256(value)
        and monitor_instrument_exclusions_are_consistent(value)
        and isinstance(value.get("coverage_manifest"), Mapping)
        and value["coverage_manifest"].get("schema") == COVERAGE_MANIFEST_SCHEMA
        and value["coverage_manifest"].get("coverage_state_contract_id")
        == COVERAGE_STATE_CONTRACT_ID
        and isinstance(value.get("sectors"), list)
        and isinstance(value.get("signals"), list)
        and isinstance(value.get("data_quality"), Mapping)
        and value.get("sector_coverage_contract_id") == SECTOR_COVERAGE_CONTRACT_ID
        and _sector_coverage_contract_is_valid(value)
        and _sector_source_evidence_complete(value)
    )


def _screening_review_readiness(
    snapshot: Mapping[str, object],
    *,
    identity_valid: bool,
) -> tuple[bool, str]:
    """Attest whether the immutable screening page can enter human review.

    Operational readiness and research-sample readiness are deliberately
    separate.  An incomplete coverage epoch is still a usable screening page,
    but it must not release the daily forward evaluator.  Once the mechanical
    prerequisites are complete, the exact review-boundary validator is the
    sole final decision core.  This verdict intentionally says nothing about
    the separate same-session QMT Capture required by the daily forward
    archive.
    """

    if snapshot.get("available") is not True:
        return False, "SNAPSHOT_UNAVAILABLE"
    if not identity_valid:
        return False, "SNAPSHOT_IDENTITY_INVALID"
    audit = snapshot.get("scan_audit")
    manifest = snapshot.get("coverage_manifest")
    try:
        pending = (
            int(audit.get("pending_symbol_count") or 0)
            if isinstance(audit, Mapping)
            else -1
        )
    except (TypeError, ValueError):
        pending = -1
    if (
        snapshot.get("scan_state") != "complete"
        or not isinstance(audit, Mapping)
        or audit.get("coverage_cycle_complete") is not True
        or pending != 0
        or not isinstance(manifest, Mapping)
        or manifest.get("complete") is not True
    ):
        return False, "COVERAGE_INCOMPLETE"
    try:
        validate_live_review_snapshot(snapshot)
    except (TypeError, ValueError):
        return False, "REVIEW_BOUNDARY_INVALID"
    return True, "READY"


class TradingScreeningService:
    def __init__(
        self,
        *,
        market_data: MarketDataGateway,
        sector_catalog: SectorCatalogGateway,
        engine: TradingEngine | HumanAssistedDecisionCore,
        scan_planner: Callable[..., ScanPlan] = build_scan_plan,
        cache_path: Path,
        human_review_archive_root: Path | None = None,
        clock: Callable[[], datetime],
        notifier: NotificationDispatcher | None,
        config: TradingScreeningConfig = TradingScreeningConfig(),
        risk_limits: RiskLimits = RiskLimits(),
        backtest_verdict: Mapping[str, object] | None = None,
    ) -> None:
        self._market_data = market_data
        self._sector_catalog = sector_catalog
        self._engine = engine
        raw_core_id = getattr(engine, "contract_id", None)
        self._decision_core_id = (
            raw_core_id
            if isinstance(raw_core_id, str) and raw_core_id.startswith("sha256:")
            else sha256_json(
                {
                    "schema": "chanlun-screening-engine-adapter",
                    "engine_type": f"{type(engine).__module__}.{type(engine).__qualname__}",
                }
            )
        )
        contract = getattr(engine, "contract", None)
        contract_document = getattr(contract, "document", None)
        self._decision_core_document = (
            contract_document()
            if callable(contract_document)
            else {
                "schema": "chanlun-screening-engine-adapter",
                "contract_id": self._decision_core_id,
                "live_status": "LIVE_DISABLED",
            }
        )
        self._scan_planner = scan_planner
        self._cache_path = Path(cache_path)
        self._human_review_archive_root = (
            None
            if human_review_archive_root is None
            else Path(human_review_archive_root)
        )
        self._human_review_decision_source_snapshot_id: str | None = None
        if self._human_review_archive_root is not None:
            try:
                project_root = Path(__file__).resolve().parents[4]
                self._human_review_decision_source_snapshot_id = (
                    _current_review_decision_source_id(str(project_root))
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A missing implementation identity may never make an old
                # receipt look current.  The isolated validator remains able
                # to produce the in-memory verdict and an actionable reason.
                self._human_review_decision_source_snapshot_id = None
        self._clock = clock
        self._notifier = notifier
        self._config = config
        self._risk_limits = risk_limits
        self._backtest_verdict = dict(
            backtest_verdict or {"live_ready": False, "status": "evidence_unavailable"}
        )
        self._cache_recovered_from_generation: str | None = None
        self._cache_generation_count = 0
        self._cache_generation_error: str | None = None
        self._state_lock = RLock()
        self._scan_lock = Lock()
        self._background_lock = Lock()
        self._background_stop = Event()
        self._background_wake = Event()
        self._background_thread: Thread | None = None
        self._background_started_at: datetime | None = None
        self._background_heartbeat_at: datetime | None = None
        self._background_refresh_started_at: datetime | None = None
        self._background_last_result_at: datetime | None = None
        self._background_last_error: str | None = None
        self._background_iteration_count = 0
        # Monitor-only observations run after a full coverage epoch has already
        # been attested.  Their transient failures are operational diagnostics,
        # not coverage failures; keeping them separate prevents one failed
        # re-observation from poisoning the immutable epoch forever.
        self._last_monitoring_at: datetime | None = None
        self._last_monitoring_errors: tuple[dict[str, object], ...] = ()
        self._pending_frequencies: dict[str, set[str]] = {}
        # Transient worker/transport failures retry after one paced refresh in
        # the same frozen market-data epoch. Deterministic data rejections stay
        # in ``_deferred_frequencies`` until a genuinely new market-data epoch.
        self._backoff_frequencies: dict[str, set[str]] = {}
        self._deferred_frequencies: dict[str, set[str]] = {}
        self._monitor_offset = 0
        # The full-universe coverage epoch is deliberately frozen while its
        # resumable queue drains.  A separate compact state tracks current-bar
        # priority observations so holdings/watchlists/active signals do not
        # wait hours behind that historical coverage work.
        self._priority_monitor_state_path = self._cache_path.with_name(
            "trading_priority_monitor_state.json"
        )
        self._priority_monitor_signal_stages: dict[str, str] = {}
        self._priority_monitor_signal_codes: dict[str, str] = {}
        self._priority_monitor_latest_documents: dict[str, dict[str, object]] = {}
        self._priority_monitor_code_observations: dict[str, tuple[datetime, str]] = {}
        self._priority_monitor_presentation_revision: str | None = None
        self._priority_monitor_last_at: datetime | None = None
        self._priority_monitor_last_codes: tuple[str, ...] = ()
        self._priority_monitor_last_errors: tuple[dict[str, object], ...] = ()
        self._candidate_monitor_last_errors: tuple[dict[str, object], ...] = ()
        self._candidate_monitor_started_at: datetime | None = None
        self._candidate_monitor_five_universe: tuple[str, ...] = ()
        self._candidate_monitor_thirty_universe: tuple[str, ...] = ()
        self._candidate_monitor_five_last_success_at: dict[str, datetime] = {}
        self._candidate_monitor_thirty_last_success_at: dict[str, datetime] = {}
        self._candidate_monitor_last_five_codes: tuple[str, ...] = ()
        self._candidate_monitor_last_thirty_codes: tuple[str, ...] = ()
        self._priority_monitor_sector_source_mode: str | None = None
        self._priority_monitor_sector_as_of: datetime | None = None
        self._priority_monitor_sector_coverage_epoch_id: str | None = None
        # Persisted documents survive restart for lifecycle/idempotence, but
        # the new process must still prove its own QMT routing immediately.
        self._priority_monitor_runtime_verified = False
        self._load_priority_monitor_state()
        self._coverage_cycle_started_at: datetime | None = None
        self._coverage_cycle_started_perf: float | None = None
        self._coverage_runtime_baseline_finalized_count = 0
        self._coverage_cycle_batch_count = 0
        self._coverage_cycle_discovered_codes: set[str] = set()
        self._coverage_cycle_completed_codes: set[str] = set()
        self._coverage_cycle_excluded_codes: set[str] = set()
        self._coverage_cycle_failed_codes: set[str] = set()
        self._coverage_cycle_exclusions: dict[str, dict[str, object]] = {}
        self._coverage_cycle_discarded_retry_codes: set[str] = set()
        self._coverage_cycle_errors: dict[str, dict[str, object]] = {}
        self._coverage_sector_restore_error: str | None = None
        self._coverage_cycle_full_market_history_scan = False
        self._coverage_cycle_background_refresh_required = False
        self._coverage_cycle_sector_batch: SectorAssessmentBatch | None = None
        self._coverage_cycle_sector_members: dict[str, tuple[str, ...]] | None = None
        self._coverage_cycle_sector_restored = False
        self._coverage_cycle_sector_runtime_hydrated = False
        self._coverage_cycle_superseded_epoch_id: str | None = None
        self._coverage_cycle_superseded_market_data_as_of: datetime | None = None
        self._coverage_epoch_id: str | None = None
        self._coverage_universe_revision: str | None = None
        self._coverage_sector_catalog_revision: str | None = None
        self._coverage_sector_strength_evidence_revision: str | None = None
        self._coverage_market_data_as_of: datetime | None = None
        self._quarantined_cache_decision_core_id: str | None = None
        self._quarantined_cache_reason: str | None = None
        loaded_snapshot = self._load_valid_cache()
        self._snapshot = loaded_snapshot or _initial_snapshot(config)
        # Loaded snapshots have already passed the full semantic/content hash
        # gate.  Health reads can attest this immutable publication by identity
        # instead of re-hashing a 100+ MiB signal tree on every HTTP request.
        self._validated_snapshot_sha256: str | None = (
            str(self._snapshot.get("snapshot_content_sha256"))
            if loaded_snapshot is not None
            and isinstance(self._snapshot.get("snapshot_content_sha256"), str)
            else None
        )
        self._presentation_cache_sha256: str | None = None
        self._presentation_cache: dict[str, object] | None = None
        self._review_readiness_cache_sha256: str | None = None
        self._review_readiness_cache: tuple[bool, str] | None = None
        # Large full-market publications can take minutes to pass the deepest
        # human-review boundary validator.  Health requests must never each
        # repeat that CPU work.  One daemon validates a given immutable hash;
        # /readyz remains responsive and reports PENDING until it is cached.
        self._review_readiness_validation_lock = Lock()
        self._review_readiness_validation_sha256: str | None = None
        self._review_readiness_validation_thread: Thread | None = None
        cached_manifest = self._snapshot.get("coverage_manifest")
        cached_sector_catalog_revision = (
            cached_manifest.get("sector_catalog_revision")
            if isinstance(cached_manifest, Mapping)
            else None
        )
        self._snapshot_rebuild_required = (
            self._snapshot.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
            or not isinstance(cached_sector_catalog_revision, str)
            or not cached_sector_catalog_revision.startswith("sha256:")
            or not _sector_source_evidence_complete(self._snapshot)
        )
        self._snapshot["decision_core_id"] = self._decision_core_id
        self._snapshot["decision_core"] = copy.deepcopy(self._decision_core_document)
        coverage_state_restored = (
            False
            if self._snapshot_rebuild_required
            else self._restore_coverage_state(self._snapshot)
        )
        if coverage_state_restored:
            try:
                restored_sector_batch, restored_sector_members = (
                    _coverage_sector_state_from_snapshot(self._snapshot)
                )
            except (TypeError, ValueError) as exc:
                # Never mix a partially restorable sector state with the
                # authenticated current coverage epoch.
                self._coverage_sector_restore_error = (
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                )
                self._snapshot_rebuild_required = True
                coverage_state_restored = False
            else:
                self._coverage_cycle_sector_batch = restored_sector_batch
                self._coverage_cycle_sector_members = restored_sector_members
                self._coverage_cycle_sector_restored = True
        if coverage_state_restored:
            runtime_pending = (
                set(self._pending_frequencies) | set(self._backoff_frequencies)
            ).intersection(self._coverage_cycle_discovered_codes)
            self._coverage_runtime_baseline_finalized_count = max(
                0,
                len(self._coverage_cycle_discovered_codes) - len(runtime_pending),
            )
        self._last_as_of = (
            None
            if self._snapshot_rebuild_required or not coverage_state_restored
            else self._cached_as_of(self._snapshot)
        )
        self._cursor = (
            ScanCursor.current(
                structure_contract_id=config.structure_contract_id,
                parameter_set_id=config.parameter_set_id,
            )
            if self._last_as_of is not None
            else ScanCursor.empty()
        )
        progress_registrar = getattr(
            self._market_data,
            "set_progress_callback",
            None,
        )
        if callable(progress_registrar):
            progress_registrar(self._record_background_heartbeat)

    @staticmethod
    def _cached_as_of(snapshot: Mapping[str, object]) -> datetime | None:
        value = snapshot.get("as_of")
        if not isinstance(value, str):
            return None
        try:
            return normalize_datetime(datetime.fromisoformat(value), "cached as_of")
        except ValueError:
            return None

    @staticmethod
    def _cached_scanned_at(snapshot: Mapping[str, object]) -> datetime | None:
        value = snapshot.get("scanned_at") or snapshot.get("generated_at")
        if not isinstance(value, str):
            return None
        try:
            return normalize_datetime(
                datetime.fromisoformat(value),
                "cached scanned_at",
            )
        except ValueError:
            return None

    @staticmethod
    def _priority_monitor_state_sha256(
        payload: Mapping[str, object],
    ) -> str:
        return sha256_json(
            {key: value for key, value in payload.items() if key != "content_sha256"}
        )

    def _load_priority_monitor_state(self) -> None:
        if not self._config.priority_monitoring_enabled:
            return
        try:
            payload = json.loads(
                self._priority_monitor_state_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != PRIORITY_MONITOR_SCHEMA
            or payload.get("candidate_monitor_contract_id")
            != CANDIDATE_MONITOR_CONTRACT_ID
            or payload.get("decision_core_id") != self._decision_core_id
            or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
            or payload.get("live_status") != "LIVE_DISABLED"
            or payload.get("content_sha256")
            != self._priority_monitor_state_sha256(payload)
        ):
            return
        raw_stages = payload.get("signal_stages")
        raw_codes = payload.get("signal_codes")
        raw_documents = payload.get("latest_documents", [])
        if not isinstance(raw_stages, Mapping) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or not value
            for key, value in raw_stages.items()
        ):
            return
        if (
            not isinstance(raw_codes, Mapping)
            or set(raw_codes) != set(raw_stages)
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in raw_codes.items()
            )
        ):
            return
        if not isinstance(raw_documents, list) or any(
            not isinstance(value, Mapping)
            or not isinstance(value.get("signal_id"), str)
            or not value.get("signal_id")
            or not isinstance(value.get("code"), str)
            or not value.get("code")
            or not isinstance(value.get("lifecycle_stage"), str)
            or value.get("decision_core_id") != self._decision_core_id
            for value in raw_documents
        ):
            return
        latest_documents = {
            str(value["signal_id"]): copy.deepcopy(dict(value))
            for value in raw_documents
        }
        if len(latest_documents) != len(raw_documents) or any(
            signal_id not in raw_stages
            or raw_stages[signal_id] != document.get("lifecycle_stage")
            or raw_codes.get(signal_id) != document.get("code")
            for signal_id, document in latest_documents.items()
        ):
            return
        raw_last_codes = payload.get("last_codes", [])
        raw_last_errors = payload.get("last_errors", [])
        raw_candidate_errors = payload.get("candidate_last_errors", [])
        raw_five_universe = payload.get("five_minute_universe", [])
        raw_thirty_universe = payload.get("thirty_minute_universe", [])
        raw_last_five_codes = payload.get("last_five_minute_codes", [])
        raw_last_thirty_codes = payload.get("last_thirty_minute_codes", [])
        string_lists = (
            raw_last_codes,
            raw_five_universe,
            raw_thirty_universe,
            raw_last_five_codes,
            raw_last_thirty_codes,
        )
        if any(
            not isinstance(values, list)
            or len(values) != len(set(values))
            or any(not isinstance(value, str) or not value for value in values)
            for values in string_lists
        ):
            return
        if any(
            not isinstance(values, list)
            or any(not isinstance(value, Mapping) for value in values)
            for values in (raw_last_errors, raw_candidate_errors)
        ):
            return

        def parse_datetime_map(
            raw: object,
            *,
            label: str,
        ) -> dict[str, datetime]:
            if not isinstance(raw, Mapping):
                raise TypeError(f"{label} must be a mapping")
            result: dict[str, datetime] = {}
            for key, value in raw.items():
                if not isinstance(key, str) or not key or not isinstance(value, str):
                    raise TypeError(f"{label} contains an invalid row")
                result[key] = normalize_datetime(
                    datetime.fromisoformat(value),
                    f"{label} {key}",
                )
            return result

        try:
            last_at = datetime.fromisoformat(str(payload["last_at"]))
            last_at = normalize_datetime(last_at, "priority monitor last_at")
            raw_started_at = payload.get("candidate_monitor_started_at")
            candidate_started_at = (
                None
                if raw_started_at is None
                else normalize_datetime(
                    datetime.fromisoformat(str(raw_started_at)),
                    "candidate monitor started_at",
                )
            )
            five_last_success_at = parse_datetime_map(
                payload.get("five_minute_last_success_at"),
                label="five minute candidate last_success_at",
            )
            thirty_last_success_at = parse_datetime_map(
                payload.get("thirty_minute_last_success_at"),
                label="thirty minute candidate last_success_at",
            )
            raw_code_observations = payload.get("code_observations")
            if not isinstance(raw_code_observations, Mapping):
                return
            code_observations: dict[str, tuple[datetime, str]] = {}
            for code, raw_observation in raw_code_observations.items():
                if (
                    not isinstance(code, str)
                    or not code
                    or not isinstance(raw_observation, Mapping)
                    or raw_observation.get("lane") not in _CANDIDATE_MONITOR_LANES
                ):
                    return
                code_observations[code] = (
                    normalize_datetime(
                        datetime.fromisoformat(str(raw_observation["observed_at"])),
                        f"{code} monitor observation",
                    ),
                    str(raw_observation["lane"]),
                )
            raw_sector_source_mode = payload.get("sector_source_mode")
            if raw_sector_source_mode not in {
                None,
                "CURRENT_NATIVE",
                "FROZEN_COVERAGE_EPOCH",
            }:
                return
            raw_sector_as_of = payload.get("sector_as_of")
            sector_as_of = (
                None
                if raw_sector_as_of is None
                else normalize_datetime(
                    datetime.fromisoformat(str(raw_sector_as_of)),
                    "priority monitor sector_as_of",
                )
            )
            raw_sector_epoch_id = payload.get("sector_coverage_epoch_id")
            if raw_sector_epoch_id is not None and (
                not isinstance(raw_sector_epoch_id, str) or not raw_sector_epoch_id
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        if not set(five_last_success_at).issubset(set(raw_five_universe)) or not set(
            thirty_last_success_at
        ).issubset(set(raw_thirty_universe)):
            return
        effective_stages: dict[str, str] = {}
        for key, value in raw_stages.items():
            signal_id = str(key)
            document = latest_documents.get(signal_id)
            stage = (
                lifecycle_stage_from_signal(document)
                if isinstance(document, Mapping)
                else None
            )
            if stage is not None and isinstance(document, dict):
                document["lifecycle_stage"] = stage
            effective_stages[signal_id] = stage or str(value)
        self._priority_monitor_signal_stages = effective_stages
        self._priority_monitor_signal_codes = {
            str(key): str(value) for key, value in raw_codes.items()
        }
        self._priority_monitor_latest_documents = latest_documents
        self._priority_monitor_code_observations = code_observations
        self._priority_monitor_presentation_revision = str(payload["content_sha256"])
        self._priority_monitor_last_at = last_at
        self._priority_monitor_last_codes = tuple(raw_last_codes)
        self._priority_monitor_last_errors = tuple(
            copy.deepcopy(dict(value)) for value in raw_last_errors
        )
        self._candidate_monitor_last_errors = tuple(
            copy.deepcopy(dict(value)) for value in raw_candidate_errors
        )
        self._candidate_monitor_started_at = candidate_started_at
        self._candidate_monitor_five_universe = tuple(raw_five_universe)
        self._candidate_monitor_thirty_universe = tuple(raw_thirty_universe)
        self._candidate_monitor_five_last_success_at = five_last_success_at
        self._candidate_monitor_thirty_last_success_at = thirty_last_success_at
        self._candidate_monitor_last_five_codes = tuple(raw_last_five_codes)
        self._candidate_monitor_last_thirty_codes = tuple(raw_last_thirty_codes)
        self._priority_monitor_sector_source_mode = raw_sector_source_mode
        self._priority_monitor_sector_as_of = sector_as_of
        self._priority_monitor_sector_coverage_epoch_id = raw_sector_epoch_id

    def _persist_priority_monitor_state(self) -> None:
        if not self._config.priority_monitoring_enabled:
            return
        with self._background_lock:
            last_at = self._priority_monitor_last_at
            signal_stages = dict(self._priority_monitor_signal_stages)
            signal_codes = dict(self._priority_monitor_signal_codes)
            latest_documents = tuple(
                copy.deepcopy(value)
                for _, value in sorted(self._priority_monitor_latest_documents.items())
            )
            last_codes = tuple(self._priority_monitor_last_codes)
            last_errors = tuple(
                copy.deepcopy(value) for value in self._priority_monitor_last_errors
            )
            candidate_last_errors = tuple(
                copy.deepcopy(value) for value in self._candidate_monitor_last_errors
            )
            code_observations = dict(self._priority_monitor_code_observations)
            candidate_started_at = self._candidate_monitor_started_at
            five_universe = tuple(self._candidate_monitor_five_universe)
            thirty_universe = tuple(self._candidate_monitor_thirty_universe)
            five_last_success_at = dict(self._candidate_monitor_five_last_success_at)
            thirty_last_success_at = dict(
                self._candidate_monitor_thirty_last_success_at
            )
            last_five_codes = tuple(self._candidate_monitor_last_five_codes)
            last_thirty_codes = tuple(self._candidate_monitor_last_thirty_codes)
            sector_source_mode = self._priority_monitor_sector_source_mode
            sector_as_of = self._priority_monitor_sector_as_of
            sector_coverage_epoch_id = self._priority_monitor_sector_coverage_epoch_id
        payload: dict[str, object] = {
            "schema": PRIORITY_MONITOR_SCHEMA,
            "candidate_monitor_contract_id": CANDIDATE_MONITOR_CONTRACT_ID,
            "decision_core_id": self._decision_core_id,
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "last_at": (None if last_at is None else last_at.isoformat()),
            "signal_stages": dict(sorted(signal_stages.items())),
            "signal_codes": dict(sorted(signal_codes.items())),
            "latest_documents": list(latest_documents),
            "code_observations": {
                code: {"observed_at": value[0].isoformat(), "lane": value[1]}
                for code, value in sorted(code_observations.items())
            },
            "last_codes": list(last_codes),
            "last_errors": [copy.deepcopy(value) for value in last_errors],
            "candidate_last_errors": [
                copy.deepcopy(value) for value in candidate_last_errors
            ],
            "candidate_monitor_started_at": (
                None
                if candidate_started_at is None
                else candidate_started_at.isoformat()
            ),
            "five_minute_universe": list(five_universe),
            "thirty_minute_universe": list(thirty_universe),
            "five_minute_last_success_at": {
                code: value.isoformat()
                for code, value in sorted(five_last_success_at.items())
            },
            "thirty_minute_last_success_at": {
                code: value.isoformat()
                for code, value in sorted(thirty_last_success_at.items())
            },
            "last_five_minute_codes": list(last_five_codes),
            "last_thirty_minute_codes": list(last_thirty_codes),
            "sector_source_mode": sector_source_mode,
            "sector_as_of": (
                None if sector_as_of is None else sector_as_of.isoformat()
            ),
            "sector_coverage_epoch_id": sector_coverage_epoch_id,
            "read_only": True,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }
        payload["content_sha256"] = self._priority_monitor_state_sha256(payload)
        path = self._priority_monitor_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with interprocess_file_lock(lock_path, timeout_seconds=10.0):
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _priority_monitor_due(self, observed_at: datetime) -> bool:
        if (
            not self._config.priority_monitoring_enabled
            or not _priority_monitor_session_open(observed_at)
        ):
            return False
        return bool(
            not self._priority_monitor_runtime_verified
            or _priority_monitor_delay_seconds(
                observed_at,
                self._priority_monitor_last_at,
                interval_seconds=(self._config.priority_monitor_interval_seconds),
            )
            <= 0
        )

    def _record_priority_monitor_result(
        self,
        *,
        observed_at: datetime,
        codes: tuple[str, ...],
        errors: tuple[dict[str, object], ...],
        documents: tuple[Mapping[str, object], ...] | None = None,
        successful_codes: tuple[str, ...] = (),
        lanes_by_code: Mapping[str, str] | None = None,
        candidate_errors: tuple[dict[str, object], ...] = (),
        five_universe: tuple[str, ...] | None = None,
        thirty_universe: tuple[str, ...] | None = None,
        five_codes: tuple[str, ...] = (),
        thirty_codes: tuple[str, ...] = (),
        successful_five_codes: tuple[str, ...] = (),
        successful_thirty_codes: tuple[str, ...] = (),
    ) -> None:
        """Publish compact monitor state without touching coverage state."""

        lane_map = dict(lanes_by_code or {})
        if any(value not in _CANDIDATE_MONITOR_LANES for value in lane_map.values()):
            raise ValueError("candidate monitor lane is invalid")
        presentation_lane = {
            CANDIDATE_MONITOR_LANE_1M: "PRIORITY_CURRENT_1M",
            CANDIDATE_MONITOR_LANE_5M: "CANDIDATE_CURRENT_5M",
            CANDIDATE_MONITOR_LANE_30M: "CANDIDATE_CURRENT_30M",
        }
        compact_documents = None
        if documents is not None:
            compact_documents = tuple(
                {
                    **_presentation_signal_document(document),
                    "observation_lane": presentation_lane[
                        lane_map[str(document["code"])]
                    ],
                    "monitor_observed_at": observed_at.isoformat(),
                    "realtime_observation": (
                        lane_map[str(document["code"])] == CANDIDATE_MONITOR_LANE_1M
                    ),
                }
                for document in documents
            )
        with self._background_lock:
            if compact_documents is not None:
                completed_codes = set(successful_codes)
                for signal_id, document in tuple(
                    self._priority_monitor_latest_documents.items()
                ):
                    if document.get("code") in completed_codes:
                        self._priority_monitor_latest_documents.pop(
                            signal_id,
                            None,
                        )
                        self._priority_monitor_signal_stages.pop(signal_id, None)
                        self._priority_monitor_signal_codes.pop(signal_id, None)
                for document in compact_documents:
                    signal_id = str(document["signal_id"])
                    self._priority_monitor_latest_documents[signal_id] = copy.deepcopy(
                        document
                    )
                    self._priority_monitor_signal_stages[signal_id] = str(
                        lifecycle_stage_from_signal(document)
                        or document["lifecycle_stage"]
                    )
                    self._priority_monitor_signal_codes[signal_id] = str(
                        document["code"]
                    )
                for code in successful_codes:
                    self._priority_monitor_code_observations[code] = (
                        observed_at,
                        lane_map[code],
                    )
            if five_universe is not None and thirty_universe is not None:
                if self._candidate_monitor_started_at is None:
                    self._candidate_monitor_started_at = observed_at
                self._candidate_monitor_five_universe = tuple(
                    dict.fromkeys(five_universe)
                )
                self._candidate_monitor_thirty_universe = tuple(
                    dict.fromkeys(thirty_universe)
                )
                five_scope = set(self._candidate_monitor_five_universe)
                thirty_scope = set(self._candidate_monitor_thirty_universe)
                self._candidate_monitor_five_last_success_at = {
                    code: value
                    for code, value in self._candidate_monitor_five_last_success_at.items()
                    if code in five_scope
                }
                self._candidate_monitor_thirty_last_success_at = {
                    code: value
                    for code, value in self._candidate_monitor_thirty_last_success_at.items()
                    if code in thirty_scope
                }
                self._candidate_monitor_five_last_success_at.update(
                    {code: observed_at for code in successful_five_codes}
                )
                self._candidate_monitor_thirty_last_success_at.update(
                    {code: observed_at for code in successful_thirty_codes}
                )
                self._candidate_monitor_last_five_codes = tuple(five_codes)
                self._candidate_monitor_last_thirty_codes = tuple(thirty_codes)
                self._candidate_monitor_last_errors = tuple(
                    copy.deepcopy(value) for value in candidate_errors
                )
            self._priority_monitor_last_at = observed_at
            self._priority_monitor_last_codes = tuple(codes)
            self._priority_monitor_last_errors = tuple(
                copy.deepcopy(value) for value in errors
            )
            self._priority_monitor_runtime_verified = True
            self._priority_monitor_presentation_revision = sha256_json(
                {
                    "observed_at": observed_at,
                    "documents": tuple(
                        self._priority_monitor_latest_documents[key]
                        for key in sorted(self._priority_monitor_latest_documents)
                    ),
                    "code_observations": tuple(
                        (code, value[0], value[1])
                        for code, value in sorted(
                            self._priority_monitor_code_observations.items()
                        )
                    ),
                }
            )

    def _prune_unowned_sell_priority_state(
        self,
        *,
        mandatory_codes: frozenset[str],
    ) -> None:
        """Drop stale live-overlay sells outside holdings/watchlist scope.

        The authenticated archival snapshot remains untouched.  Only compact
        current-minute overlay state is removed, because a sell point for a
        symbol that is neither held nor explicitly watched has no live action
        owner and will no longer be sampled by the priority lane.
        """

        with self._background_lock:
            removable = tuple(
                signal_id
                for signal_id, document in self._priority_monitor_latest_documents.items()
                if isinstance(document.get("code"), str)
                and document.get("code") not in mandatory_codes
                and isinstance(document.get("point_type"), str)
                and str(document["point_type"]).endswith("sell")
            )
            for signal_id in removable:
                code = self._priority_monitor_signal_codes.get(signal_id)
                self._priority_monitor_latest_documents.pop(signal_id, None)
                self._priority_monitor_signal_stages.pop(signal_id, None)
                self._priority_monitor_signal_codes.pop(signal_id, None)
                if (
                    isinstance(code, str)
                    and code not in mandatory_codes
                    and all(
                        document.get("code") != code
                        for document in self._priority_monitor_latest_documents.values()
                    )
                ):
                    self._priority_monitor_code_observations.pop(code, None)

    def _run_priority_monitor(
        self,
        *,
        previous: Mapping[str, object],
        observed_at: datetime,
        excluded_codes: frozenset[str] = frozenset(),
        frozen_sector_batch: SectorAssessmentBatch | None = None,
        frozen_sector_members: Mapping[str, tuple[str, ...]] | None = None,
        frozen_sector_as_of: datetime | None = None,
        frozen_coverage_epoch_id: str | None = None,
    ) -> None:
        """Observe current completed bars without changing the frozen coverage.

        This lane is intentionally *not* a full-universe snapshot and never
        contributes to coverage ratios.  It advances human-review lifecycle
        notifications for owned/selected risk and a rotating sample of
        currently supportive QMT sectors.  It remains active both while the
        slower authenticated coverage epoch drains and after that epoch becomes
        complete: the ordinary complete-epoch cursor can span thousands of
        symbols and must not make a holding wait one full rotation.
        """

        if not self._priority_monitor_due(observed_at):
            return
        if (frozen_sector_batch is None) != (frozen_sector_members is None):
            raise ValueError(
                "frozen priority sector batch and members must be supplied together"
            )
        if frozen_sector_batch is None:
            sector_batch = self._sector_catalog.native_sector_assessments(
                as_of=observed_at
            )
            all_members = dict(self._sector_catalog.members())
            sector_source_mode = "CURRENT_NATIVE"
            sector_as_of = self._sector_market_data_as_of(
                sector_batch.assessments,
                observed_at,
            )
            sector_coverage_epoch_id = None
        else:
            if frozen_sector_as_of is None or not frozen_coverage_epoch_id:
                raise ValueError("frozen priority sector provenance is required")
            # Daily preselection freezes sector ranking and membership.  The
            # intraday lane must monitor newer completed stock bars without
            # silently reselecting sectors every minute; doing so both changes
            # the decision scope mid-session and serially rebuilds every QMT
            # sector before one priority symbol can be observed.
            sector_batch = frozen_sector_batch
            all_members = dict(frozen_sector_members or {})
            sector_source_mode = "FROZEN_COVERAGE_EPOCH"
            sector_as_of = normalize_datetime(
                frozen_sector_as_of,
                "frozen priority sector as_of",
            )
            sector_coverage_epoch_id = frozen_coverage_epoch_id
        with self._background_lock:
            self._priority_monitor_sector_source_mode = sector_source_mode
            self._priority_monitor_sector_as_of = sector_as_of
            self._priority_monitor_sector_coverage_epoch_id = sector_coverage_epoch_id
        if sector_batch.completion_ratio < self._config.min_scan_completion_ratio:
            raise RuntimeError("priority monitor sector coverage is incomplete")
        failed_sector_ids = {
            item.sector_id for item in (*sector_batch.errors, *sector_batch.exclusions)
        }
        assessments = tuple(
            assessment
            for assessment in sector_batch.assessments
            if assessment.sector_id not in failed_sector_ids
        )
        ranked = rank_sectors(assessments)
        sector_by_code: dict[str, SectorAssessment] = {}
        supportive_codes: list[str] = []
        for ranked_sector in ranked:
            assessment = ranked_sector.assessment
            for member in sorted(all_members.get(assessment.sector_id, ())):
                sector_by_code.setdefault(member, assessment)
                if assessment.regime == "supportive":
                    supportive_codes.append(member)

        watchlist, _rejected_watchlist = _validated_monitor_instrument_scope(
            self._market_data.active_watchlist_scope(),
            "active_watchlist_scope",
        )
        holdings, _rejected_holdings = _validated_monitor_instrument_scope(
            self._market_data.holdings_scope(),
            "holdings_scope",
        )
        mandatory_scope = tuple(dict.fromkeys((*holdings, *watchlist)))
        mandatory_codes = tuple(
            code for code in mandatory_scope if code not in excluded_codes
        )
        self._prune_unowned_sell_priority_state(
            mandatory_codes=frozenset(mandatory_codes),
        )
        main_signal_documents = tuple(
            row for row in previous.get("signals", ()) if isinstance(row, Mapping)
        )
        with self._background_lock:
            monitor_signal_documents = tuple(
                copy.deepcopy(row)
                for row in self._priority_monitor_latest_documents.values()
            )
            monitor_code_observations = dict(self._priority_monitor_code_observations)
        observation_max_age_seconds = {
            CANDIDATE_MONITOR_LANE_1M: max(
                180,
                self._config.priority_monitor_interval_seconds * 3,
            ),
            CANDIDATE_MONITOR_LANE_5M: (
                self._config.five_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
            ),
            CANDIDATE_MONITOR_LANE_30M: (
                self._config.thirty_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
            ),
        }
        fresh_monitor_codes = {
            code
            for code, (last_at, lane) in monitor_code_observations.items()
            if observed_at >= last_at
            and (observed_at - last_at).total_seconds()
            <= observation_max_age_seconds[lane]
        }
        current_monitor_signal_documents = tuple(
            row
            for row in monitor_signal_documents
            if row.get("code") in fresh_monitor_codes
        )
        # A successful current observation is authoritative even when it emits
        # no row.  Without this code-level tombstone, an armed row in the daily
        # archive would re-enter the 1m lane forever after its setup vanished;
        # a newer formed row could likewise lose to the archive's older rank.
        current_signal_documents = (
            tuple(
                row
                for row in main_signal_documents
                if row.get("code") not in fresh_monitor_codes
            )
            + current_monitor_signal_documents
        )
        buy_candidate_codes = _priority_buy_candidate_codes(
            main_signal_documents,
            current_monitor_signal_documents,
            excluded_codes=excluded_codes,
        )
        urgent_buy_codes = _priority_buy_candidate_codes(
            current_signal_documents,
            excluded_codes=frozenset((*excluded_codes, *mandatory_scope)),
            allowed_stages=_CURRENT_MINUTE_BUY_STAGES,
        )
        minute_codes = tuple(dict.fromkeys((*mandatory_codes, *urgent_buy_codes)))
        # Existing buy candidates are observed on the cadence of the 5m setup
        # that can change their decision.  The much broader frozen supportive
        # sector scope is a discovery lane: it receives a current 5m+30m
        # evaluation over each 30-minute window.  Treating all sector members
        # as five-minute candidates would overclaim capacity and delay the
        # genuinely armed 1m lane on the measured production host.
        five_universe = tuple(dict.fromkeys((*mandatory_scope, *buy_candidate_codes)))
        thirty_universe = tuple(dict.fromkeys((*five_universe, *supportive_codes)))
        with self._background_lock:
            previous_monitor_at = self._priority_monitor_last_at
            five_last_success_at = dict(self._candidate_monitor_five_last_success_at)
            thirty_last_success_at = dict(
                self._candidate_monitor_thirty_last_success_at
            )
        five_codes = _take_due_candidate_batch(
            five_universe,
            last_success_at=five_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.five_minute_candidate_target_seconds,
            monitor_interval_seconds=self._config.priority_monitor_interval_seconds,
            max_symbols=(self._config.max_five_minute_candidate_symbols_per_refresh),
            excluded_codes=excluded_codes,
            previous_monitor_at=previous_monitor_at,
        )
        thirty_codes = _take_due_candidate_batch(
            thirty_universe,
            last_success_at=thirty_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.thirty_minute_candidate_target_seconds,
            monitor_interval_seconds=self._config.priority_monitor_interval_seconds,
            max_symbols=(self._config.max_thirty_minute_candidate_symbols_per_refresh),
            excluded_codes=excluded_codes,
            previous_monitor_at=previous_monitor_at,
        )
        frequencies_by_code: dict[str, set[str]] = {}
        for code in minute_codes:
            # A current 1m trigger is only meaningful against the latest
            # completed 5m setup; refreshing both prevents a stale cached 5m
            # structure from surviving a five-minute boundary.
            frequencies_by_code.setdefault(code, set()).update(("5m", "1m"))
        for code in five_codes:
            frequencies_by_code.setdefault(code, set()).add("5m")
        for code in thirty_codes:
            frequencies_by_code.setdefault(code, set()).update(("5m", "30m"))
        codes = tuple(dict.fromkeys((*minute_codes, *five_codes, *thirty_codes)))
        minute_code_set = set(minute_codes)
        five_code_set = set(five_codes)
        five_universe_set = set(five_universe)
        thirty_universe_set = set(thirty_universe)
        lanes_by_code = {
            code: (
                CANDIDATE_MONITOR_LANE_1M
                if code in minute_code_set
                else CANDIDATE_MONITOR_LANE_5M
                if code in five_code_set
                else CANDIDATE_MONITOR_LANE_30M
            )
            for code in codes
        }
        if not codes:
            self._record_priority_monitor_result(
                observed_at=observed_at,
                codes=(),
                errors=(),
                five_universe=five_universe,
                thirty_universe=thirty_universe,
            )
            self._persist_priority_monitor_state()
            return

        watchlist_codes = set(watchlist)
        holding_codes = set(holdings)
        supportive_code_set = set(supportive_codes)
        main_previous_stages = {
            str(row["signal_id"]): str(
                lifecycle_stage_from_signal(row) or row["lifecycle_stage"]
            )
            for row in previous.get("signals", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("signal_id"), str)
            and isinstance(row.get("lifecycle_stage"), str)
        }
        monitor_previous_stages: dict[str, str] = {}
        for row in current_monitor_signal_documents:
            signal_id = row.get("signal_id")
            stage = lifecycle_stage_from_signal(row)
            if isinstance(signal_id, str) and stage is not None:
                monitor_previous_stages[signal_id] = stage
        prior_stages = {
            **main_previous_stages,
            **monitor_previous_stages,
        }
        previous_documents_by_id = {
            str(row["signal_id"]): copy.deepcopy(dict(row))
            for row in (*main_signal_documents, *current_monitor_signal_documents)
            if isinstance(row.get("signal_id"), str)
            and isinstance(row.get("code"), str)
        }

        def selection_sources_for(code: str) -> tuple[str, ...]:
            sources: list[str] = []
            if code in supportive_code_set:
                sources.append("QMT_SECTOR_TRIGGER")
            elif code in sector_by_code:
                sources.append("QMT_SECTOR_ELIGIBLE_SCOPE")
            if code in watchlist_codes:
                sources.append("ACTIVE_WATCHLIST_MONITOR")
            if code in holding_codes:
                sources.append("HOLDING_MONITOR")
            if code in buy_candidate_codes and not sources:
                sources.append("PREVIOUS_SIGNAL_MONITOR")
            return tuple(sources or ("INCREMENTAL_SCAN_SCOPE",))

        def evaluate(code: str):
            sector = sector_by_code.get(
                code,
                SectorAssessment(
                    sector_id="unclassified",
                    sector_name="未匹配 QMT GICS3 行业",
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_membership_missing",),
                ),
            )
            try:
                bundle = self._structure_bundle_with_causal_risk(
                    code,
                    as_of=observed_at,
                    sector=sector,
                    frequencies=tuple(
                        frequency
                        for frequency in SCREENING_STRUCTURE_FREQUENCIES
                        if frequency in frequencies_by_code[code]
                    ),
                    risk_evidence_cutoff=sector_as_of,
                )
                bundle = replace(
                    bundle,
                    selection_sources=selection_sources_for(code),
                )
                if code in holding_codes and bundle.physical_timeframe_level_zero:
                    bundle = replace(bundle, held_tower="formal", held_level=0)
                age = observed_at - bundle.as_of
                if age < timedelta(0) or age > timedelta(
                    seconds=self._config.max_structure_age_seconds
                ):
                    raise ValueError("priority_monitor_structure_bundle_stale")
                evaluated = self._engine.evaluate_symbol(bundle)
                name_provider = getattr(self._market_data, "symbol_name", None)
                symbol_name = (
                    name_provider(code)
                    if evaluated and callable(name_provider)
                    else None
                )
                documents = tuple(
                    _signal_document(
                        item,
                        previous_stage=prior_stages.get(item.lifecycle.signal_id),
                        name=symbol_name,
                        decision_core_id=self._decision_core_id,
                        selection_sources=bundle.selection_sources,
                        higher_timeframe_gates=bundle.higher_timeframe_gates,
                    )
                    for item in evaluated
                )
                return code, documents, None
            except Exception as exc:
                return code, (), exc

        results = []
        worker_count = min(
            self._config.stock_worker_count,
            max(1, len(codes)),
        )
        if worker_count == 1:
            results = [evaluate(code) for code in codes]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="TradingPriorityMonitor",
            ) as executor:
                results = list(executor.map(evaluate, codes))

        documents: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        candidate_errors: list[dict[str, object]] = []
        for code, rows, exc in results:
            if exc is None:
                documents.extend(rows)
                continue
            error = _stock_analysis_error_document(code, exc)
            if code in minute_code_set:
                errors.append(error)
            else:
                candidate_errors.append(error)
        documents.sort(
            key=lambda row: (
                str(row.get("code")),
                str(row.get("signal_id")),
            )
        )
        authoritative_codes = tuple(
            sorted(code for code, _rows, exc in results if exc is None)
        )
        notification_authoritative_codes = tuple(
            code for code in authoritative_codes if code in minute_code_set
        )
        notification_authoritative_code_set = set(notification_authoritative_codes)
        previous_notification = {
            "signals": [
                document
                for signal_id, document in sorted(previous_documents_by_id.items())
                if signal_id in prior_stages
                and document.get("code") in notification_authoritative_code_set
            ]
        }
        current_notification = {
            "signals": [
                document
                for document in documents
                if document.get("code") in notification_authoritative_code_set
            ],
            # A missing signal is meaningful only for symbols successfully
            # recomputed in this partial lane.  The dispatcher uses this scope
            # to emit a retraction without invalidating rotated-out symbols.
            "notification_authoritative_codes": list(notification_authoritative_codes),
        }
        successful_five_codes = tuple(
            code
            for code in authoritative_codes
            if code in five_universe_set and "5m" in frequencies_by_code[code]
        )
        successful_thirty_codes = tuple(
            code
            for code in authoritative_codes
            if code in thirty_universe_set and "30m" in frequencies_by_code[code]
        )
        self._record_priority_monitor_result(
            observed_at=observed_at,
            codes=minute_codes,
            errors=tuple(errors),
            documents=tuple(documents),
            successful_codes=authoritative_codes,
            lanes_by_code=lanes_by_code,
            candidate_errors=tuple(candidate_errors),
            five_universe=five_universe,
            thirty_universe=thirty_universe,
            five_codes=five_codes,
            thirty_codes=thirty_codes,
            successful_five_codes=successful_five_codes,
            successful_thirty_codes=successful_thirty_codes,
        )
        self._persist_priority_monitor_state()
        if self._notifier is not None:
            try:
                self._notifier.dispatch_changes(
                    previous_notification,
                    current_notification,
                )
            except Exception as exc:
                notification_error = {
                    "error_type": "priority_monitor_notification_error",
                    "reason_code": "PRIORITY_MONITOR_NOTIFICATION_FAILED",
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
                errors.append(notification_error)
                self._record_priority_monitor_result(
                    observed_at=observed_at,
                    codes=minute_codes,
                    errors=tuple(errors),
                )
                self._persist_priority_monitor_state()

    def _run_priority_monitor_safely(
        self,
        *,
        previous: Mapping[str, object],
        observed_at: datetime,
        excluded_codes: frozenset[str] = frozenset(),
        frozen_sector_batch: SectorAssessmentBatch | None = None,
        frozen_sector_members: Mapping[str, tuple[str, ...]] | None = None,
        frozen_sector_as_of: datetime | None = None,
        frozen_coverage_epoch_id: str | None = None,
    ) -> None:
        """Keep live observations from poisoning a frozen coverage epoch."""

        try:
            self._record_background_heartbeat()
            self._run_priority_monitor(
                previous=previous,
                observed_at=observed_at,
                excluded_codes=excluded_codes,
                frozen_sector_batch=frozen_sector_batch,
                frozen_sector_members=frozen_sector_members,
                frozen_sector_as_of=frozen_sector_as_of,
                frozen_coverage_epoch_id=frozen_coverage_epoch_id,
            )
        except Exception as exc:
            with self._background_lock:
                completed_this_observation = (
                    self._priority_monitor_last_at == observed_at
                )
                completed_codes = (
                    self._priority_monitor_last_codes
                    if completed_this_observation
                    else ()
                )
            error = {
                "error_type": "priority_monitor_error",
                "reason_code": "PRIORITY_MONITOR_FAILED",
                "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            }
            self._record_priority_monitor_result(
                observed_at=observed_at,
                codes=completed_codes,
                errors=(error,),
            )
            try:
                self._persist_priority_monitor_state()
            except Exception:
                # The in-memory health result remains observable.  A state-file
                # failure must never abort the authenticated coverage batch.
                pass
        finally:
            self._record_background_heartbeat()

    @staticmethod
    def _frequency_map(value: object) -> dict[str, set[str]]:
        if not isinstance(value, Mapping):
            return {}
        output: dict[str, set[str]] = {}
        for code, raw in value.items():
            if not isinstance(code, str) or not isinstance(raw, list):
                continue
            frequencies = {
                str(item) for item in raw if item in {"1m", "5m", "30m", "d"}
            }
            if frequencies:
                output[code] = frequencies
        return output

    def _coverage_epoch_identity_valid(
        self,
        snapshot: Mapping[str, object],
    ) -> bool:
        """Independently recompute the shared strict strategy coverage-epoch identity."""

        manifest = snapshot.get("coverage_manifest")
        if not isinstance(manifest, Mapping):
            return False
        epoch_id = manifest.get("coverage_epoch_id")
        market_data_as_of = manifest.get("market_data_as_of")
        universe_revision = manifest.get("universe_revision")
        sector_catalog_revision = manifest.get("sector_catalog_revision")
        strength_revision = manifest.get("sector_strength_evidence_revision")
        screening_policy_id = manifest.get("screening_policy_id")
        if not all(
            isinstance(value, str) and value
            for value in (
                epoch_id,
                market_data_as_of,
                universe_revision,
                sector_catalog_revision,
                screening_policy_id,
            )
        ):
            return False
        if (
            snapshot.get("coverage_epoch_id") != epoch_id
            or manifest.get("schema") != COVERAGE_MANIFEST_SCHEMA
            or manifest.get("coverage_state_contract_id") != COVERAGE_STATE_CONTRACT_ID
            or manifest.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
            or screening_policy_id != _screening_policy_id()
            or strength_revision != snapshot.get("sector_strength_evidence_revision")
            or (
                strength_revision is not None and not isinstance(strength_revision, str)
            )
        ):
            return False
        try:
            expected = screening_coverage_epoch_id(
                market_data_as_of=normalize_datetime(
                    datetime.fromisoformat(market_data_as_of),
                    "coverage market data as_of",
                ),
                universe_revision=universe_revision,
                sector_catalog_revision=sector_catalog_revision,
                sector_strength_evidence_revision=strength_revision,
                decision_core_id=self._decision_core_id,
                screening_policy_id=screening_policy_id,
                structure_contract_id=self._config.structure_contract_id,
                parameter_set_id=self._config.parameter_set_id,
            )
        except (TypeError, ValueError):
            return False
        return epoch_id == expected

    def _restore_coverage_state(self, snapshot: Mapping[str, object]) -> bool:
        manifest = snapshot.get("coverage_manifest")
        if not isinstance(manifest, Mapping):
            return False
        epoch_id = manifest.get("coverage_epoch_id")
        if (
            set(manifest) != COVERAGE_MANIFEST_FIELDS
            or manifest.get("schema") != COVERAGE_MANIFEST_SCHEMA
            or manifest.get("coverage_state_contract_id") != COVERAGE_STATE_CONTRACT_ID
            or manifest.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
            or snapshot.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
        ):
            return False
        canonical_lists: dict[str, list[str]] = {}
        for field in (
            "discovered_codes",
            "completed_codes",
            "excluded_codes",
            "failed_codes",
            "discarded_out_of_scope_retry_codes",
        ):
            raw = manifest.get(field)
            if (
                not isinstance(raw, list)
                or any(not isinstance(value, str) or not value for value in raw)
                or raw != sorted(set(raw))
            ):
                return False
            canonical_lists[field] = raw
        if (
            type(manifest.get("complete")) is not bool
            or type(manifest.get("batch_count")) is not int
            or manifest.get("batch_count", -1) < 0
        ):
            return False
        parsed_frequency_maps: dict[str, dict[str, set[str]]] = {}
        for field in (
            "pending_frequencies",
            "backoff_frequencies",
            "deferred_frequencies",
        ):
            parsed = self._frequency_map(manifest.get(field))
            if self._frequency_document(parsed) != manifest.get(field):
                return False
            parsed_frequency_maps[field] = parsed
        exclusion_keys = {
            "code",
            "exclusion_type",
            "eligibility",
            "reason_code",
            "retry_policy",
            "deterministic_for_coverage_epoch",
            "remote_error_type",
            "reason",
        }
        raw_exclusions = manifest.get("exclusions")
        if not isinstance(raw_exclusions, list):
            return False
        exclusion_codes: list[str] = []
        for exclusion in raw_exclusions:
            if (
                not isinstance(exclusion, Mapping)
                or set(exclusion) != exclusion_keys
                or exclusion.get("exclusion_type") != "stock_analysis_exclusion"
                or exclusion.get("eligibility") != "INSUFFICIENT_MINIMUM_HISTORY"
                or not _is_coverage_exclusion(exclusion)
                or not isinstance(exclusion.get("code"), str)
                or not exclusion.get("code")
                or not isinstance(exclusion.get("remote_error_type"), str)
                or not exclusion.get("remote_error_type")
                or not isinstance(exclusion.get("reason"), str)
                or not exclusion.get("reason")
            ):
                return False
            exclusion_codes.append(str(exclusion["code"]))
        discovered = set(canonical_lists["discovered_codes"])
        completed = set(canonical_lists["completed_codes"])
        excluded = set(canonical_lists["excluded_codes"])
        failed = set(canonical_lists["failed_codes"])
        if (
            exclusion_codes != sorted(set(exclusion_codes))
            or set(exclusion_codes) != excluded
            or completed & excluded
            or completed & failed
            or excluded & failed
            or (completed | excluded | failed) - discovered
        ):
            return False
        screening_policy_id = manifest.get("screening_policy_id")
        source_cutoff = manifest.get("source_cutoff")
        market_data_as_of = manifest.get("market_data_as_of")
        universe_revision = manifest.get("universe_revision")
        sector_catalog_revision = manifest.get("sector_catalog_revision")
        sector_strength_evidence_revision = manifest.get(
            "sector_strength_evidence_revision"
        )
        if not all(
            isinstance(value, str) and value
            for value in (
                epoch_id,
                screening_policy_id,
                source_cutoff,
                market_data_as_of,
                universe_revision,
                sector_catalog_revision,
            )
        ):
            return False
        if (
            screening_policy_id != _screening_policy_id()
            or sector_strength_evidence_revision
            != snapshot.get("sector_strength_evidence_revision")
            or (
                sector_strength_evidence_revision is not None
                and not isinstance(sector_strength_evidence_revision, str)
            )
        ):
            return False
        try:
            started = normalize_datetime(
                datetime.fromisoformat(source_cutoff),
                "coverage source cutoff",
            )
            market_time = normalize_datetime(
                datetime.fromisoformat(market_data_as_of),
                "coverage market data as_of",
            )
            expected_epoch_id = screening_coverage_epoch_id(
                market_data_as_of=market_time,
                universe_revision=universe_revision,
                sector_catalog_revision=sector_catalog_revision,
                sector_strength_evidence_revision=(sector_strength_evidence_revision),
                decision_core_id=self._decision_core_id,
                screening_policy_id=_screening_policy_id(),
                structure_contract_id=self._config.structure_contract_id,
                parameter_set_id=self._config.parameter_set_id,
            )
        except ValueError:
            return False
        if epoch_id != expected_epoch_id:
            return False
        self._coverage_epoch_id = epoch_id
        self._coverage_universe_revision = universe_revision
        self._coverage_sector_catalog_revision = sector_catalog_revision
        self._coverage_sector_strength_evidence_revision = (
            sector_strength_evidence_revision
        )
        self._coverage_cycle_started_at = started
        self._coverage_market_data_as_of = market_time
        superseded_epoch_id = manifest.get("superseded_coverage_epoch_id")
        superseded_cutoff = manifest.get("superseded_market_data_as_of")
        if (superseded_epoch_id is None) != (superseded_cutoff is None):
            return False
        if superseded_epoch_id is None:
            self._coverage_cycle_superseded_epoch_id = None
            self._coverage_cycle_superseded_market_data_as_of = None
        elif (
            not isinstance(superseded_epoch_id, str)
            or not superseded_epoch_id.startswith("sha256:")
            or not isinstance(superseded_cutoff, str)
            or not superseded_cutoff
        ):
            return False
        else:
            try:
                self._coverage_cycle_superseded_market_data_as_of = normalize_datetime(
                    datetime.fromisoformat(superseded_cutoff),
                    "superseded coverage market data as_of",
                )
            except ValueError:
                return False
            self._coverage_cycle_superseded_epoch_id = superseded_epoch_id
        self._coverage_cycle_started_perf = time.perf_counter()
        self._coverage_cycle_batch_count = int(manifest.get("batch_count") or 0)
        self._coverage_cycle_discovered_codes = set(canonical_lists["discovered_codes"])
        self._coverage_cycle_completed_codes = set(canonical_lists["completed_codes"])
        self._coverage_cycle_excluded_codes = set(canonical_lists["excluded_codes"])
        self._coverage_cycle_failed_codes = set(canonical_lists["failed_codes"])
        raw_exclusions = manifest.get("exclusions")
        self._coverage_cycle_exclusions = {}
        if isinstance(raw_exclusions, list):
            self._coverage_cycle_exclusions = {
                str(value["code"]): copy.deepcopy(dict(value))
                for value in raw_exclusions
                if isinstance(value, Mapping) and isinstance(value.get("code"), str)
            }
        self._coverage_cycle_discarded_retry_codes = set(
            canonical_lists["discarded_out_of_scope_retry_codes"]
        )
        self._pending_frequencies = parsed_frequency_maps["pending_frequencies"]
        self._backoff_frequencies = parsed_frequency_maps["backoff_frequencies"]
        self._deferred_frequencies = parsed_frequency_maps["deferred_frequencies"]
        audit = snapshot.get("scan_audit")
        if isinstance(audit, Mapping):
            self._coverage_cycle_full_market_history_scan = bool(
                audit.get("full_market_history_scan")
            )
            self._coverage_cycle_background_refresh_required = bool(
                audit.get("background_full_refresh_required")
            )
        errors = snapshot.get("errors")
        if isinstance(errors, list):
            self._record_cycle_errors(
                [value for value in errors if isinstance(value, Mapping)]
            )
        return True

    def _cache_generation_directory(self) -> Path:
        return self._cache_path.with_name(f".{self._cache_path.name}.generations")

    def _valid_cache_from_path(self, path: Path) -> dict[str, object] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        # ``json.loads`` already owns a fresh tree.  Deep-copying a 100+ MiB
        # validated snapshot here doubled startup memory and added no isolation:
        # the returned tree becomes the service's private immutable publication.
        return (
            value
            if isinstance(value, dict)
            and _cache_is_valid(value, self._config, self._decision_core_id)
            else None
        )

    def _generation_paths(self) -> tuple[Path, ...]:
        directory = self._cache_generation_directory()
        try:
            stamped = tuple(
                (value.stat().st_mtime_ns, value)
                for value in directory.iterdir()
                if value.is_file() and _CACHE_GENERATION_FILE.fullmatch(value.name)
            )
        except OSError:
            return ()
        return tuple(
            value
            for _, value in sorted(
                stamped,
                key=lambda item: (item[0], item[1].name),
                reverse=True,
            )
        )

    def _load_valid_cache(self) -> dict[str, object] | None:
        generations = self._generation_paths()
        self._cache_generation_count = len(generations)
        self._cache_recovered_from_generation = None
        try:
            current_value = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # A missing, truncated or otherwise unreadable primary snapshot can
            # be the residue of an interrupted replacement.  Only that physical
            # failure class may roll back to an immutable generation.
            pass
        else:
            if isinstance(current_value, dict) and _cache_is_valid(
                current_value, self._config, self._decision_core_id
            ):
                return current_value
            if isinstance(current_value, Mapping):
                cached_core_id = current_value.get("decision_core_id")
                if (
                    isinstance(cached_core_id, str)
                    and cached_core_id
                    and cached_core_id != self._decision_core_id
                ):
                    self._quarantined_cache_decision_core_id = cached_core_id
                    self._quarantined_cache_reason = "DECISION_CORE_IDENTITY_MISMATCH"
                else:
                    self._quarantined_cache_decision_core_id = (
                        cached_core_id if isinstance(cached_core_id, str) else None
                    )
                    self._quarantined_cache_reason = "CURRENT_CACHE_CONTRACT_INVALID"
            # A parseable primary that fails semantic, policy or content-hash
            # validation is evidence of tampering/staleness, not an interrupted
            # write.  Fail closed instead of hiding it behind an older backup.
            return None
        for path in generations:
            recovered = self._valid_cache_from_path(path)
            if recovered is None:
                continue
            self._cache_recovered_from_generation = str(path)
            return recovered
        return None

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return copy.deepcopy(self._snapshot)

    def presentation_snapshot(self) -> dict[str, object]:
        """Return a compact live-page view of the full audit publication.

        The immutable archive retains every warmup/mapping diagnostic.  The
        browser needs their decision-relevant summary, not repeated raw point
        evidence for every row.  Keeping this projection separate preserves
        the auditable contract used by replay/forward capture while preventing
        minute polling from copying and transferring a 100+ MiB JSON tree.
        """

        observed_at = normalize_datetime(self._clock(), "clock")
        with self._background_lock:
            priority_last_at = self._priority_monitor_last_at
            priority_errors = tuple(self._priority_monitor_last_errors)
            priority_documents = tuple(
                copy.deepcopy(value)
                for _, value in sorted(self._priority_monitor_latest_documents.items())
            )
            code_observations = dict(self._priority_monitor_code_observations)
            candidate_errors = tuple(self._candidate_monitor_last_errors)
            priority_revision = self._priority_monitor_presentation_revision
        priority_max_age_seconds = max(
            180,
            self._config.priority_monitor_interval_seconds * 3,
        )
        priority_age_seconds = (
            None
            if priority_last_at is None
            else max(0.0, (observed_at - priority_last_at).total_seconds())
        )
        priority_live = bool(
            self._config.priority_monitoring_enabled
            and priority_last_at is not None
            and _priority_monitor_session_open(observed_at)
            and priority_age_seconds is not None
            and priority_age_seconds <= priority_max_age_seconds
        )
        overlay_active = bool(
            self._config.priority_monitoring_enabled
            and _priority_monitor_session_open(observed_at)
        )
        lane_max_age_seconds = {
            CANDIDATE_MONITOR_LANE_1M: priority_max_age_seconds,
            CANDIDATE_MONITOR_LANE_5M: (
                self._config.five_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
            ),
            CANDIDATE_MONITOR_LANE_30M: (
                self._config.thirty_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
            ),
        }
        fresh_code_observations = {
            code: (value[0], value[1])
            for code, value in code_observations.items()
            if overlay_active
            and observed_at >= value[0]
            and (observed_at - value[0]).total_seconds()
            <= lane_max_age_seconds[value[1]]
        }
        fresh_codes = set(fresh_code_observations)
        priority_documents = tuple(
            value for value in priority_documents if value.get("code") in fresh_codes
        )
        minute_documents = tuple(
            value
            for value in priority_documents
            if value.get("realtime_observation") is True
        )
        candidate_documents = tuple(
            value
            for value in priority_documents
            if value.get("realtime_observation") is not True
        )
        with self._state_lock:
            snapshot = self._snapshot
            source_sha256 = snapshot.get("snapshot_content_sha256")
            presentation_revision = (
                f"{source_sha256}|{priority_revision}|live={priority_live}"
                f"|fresh={sha256_json(tuple(sorted(fresh_code_observations)))}"
            )
            if (
                isinstance(source_sha256, str)
                and self._presentation_cache_sha256 == presentation_revision
                and self._presentation_cache is not None
            ):
                return copy.deepcopy(self._presentation_cache)
        document = {
            key: copy.deepcopy(value)
            for key, value in snapshot.items()
            if key != "signals"
        }
        raw_signals = snapshot.get("signals")
        projected_signals = (
            [
                _presentation_signal_document(value)
                for value in raw_signals
                if isinstance(value, Mapping)
            ]
            if isinstance(raw_signals, (list, tuple))
            else []
        )
        if overlay_active and fresh_codes:
            projected_signals = [
                value
                for value in projected_signals
                if value.get("code") not in fresh_codes
            ]
            signals_by_id = {
                str(value["signal_id"]): value
                for value in projected_signals
                if isinstance(value.get("signal_id"), str)
            }
            for value in priority_documents:
                projected = _presentation_signal_document(value)
                signals_by_id[str(projected["signal_id"])] = projected
            projected_signals = sorted(
                signals_by_id.values(),
                key=lambda value: (
                    POINT_TYPES.index(str(value["point_type"])),
                    str(value["code"]),
                    str(value["signal_id"]),
                ),
            )
        document["signals"] = projected_signals
        document["counts_by_stage"] = {}
        document["counts_by_point_type"] = {point_type: 0 for point_type in POINT_TYPES}
        for value in projected_signals:
            stage = str(value.get("lifecycle_stage") or "unknown")
            document["counts_by_stage"][stage] = (
                document["counts_by_stage"].get(stage, 0) + 1
            )
            point_type = str(value.get("point_type") or "")
            if point_type in document["counts_by_point_type"]:
                document["counts_by_point_type"][point_type] += 1
        document["presentation_schema"] = "chanlun-trading-screening-presentation"
        document["source_snapshot_content_sha256"] = source_sha256
        document["full_audit_evidence_embedded"] = False
        document["priority_live_overlay"] = {
            "schema": "chanlun-priority-live-page-overlay",
            "enabled": self._config.priority_monitoring_enabled,
            "live": priority_live,
            "observed_at": (
                None if priority_last_at is None else priority_last_at.isoformat()
            ),
            "age_seconds": priority_age_seconds,
            "max_age_seconds": priority_max_age_seconds,
            "signal_count": (len(minute_documents) if priority_live else 0),
            "error_count": len(priority_errors),
            "notification_dispatcher_configured": self._notifier is not None,
            "archival_snapshot_unchanged": True,
        }
        document["candidate_live_overlay"] = {
            "schema": "chanlun-candidate-live-page-overlay",
            "contract_id": CANDIDATE_MONITOR_CONTRACT_ID,
            "enabled": self._config.priority_monitoring_enabled,
            "live": bool(overlay_active and fresh_code_observations),
            "fresh_code_count": len(fresh_code_observations),
            "signal_count": len(candidate_documents),
            "error_count": len(candidate_errors),
            "archival_snapshot_unchanged": True,
            "realtime_notification_authorized": False,
        }
        with self._state_lock:
            if self._snapshot is snapshot and isinstance(source_sha256, str):
                self._presentation_cache_sha256 = presentation_revision
                self._presentation_cache = document
        return copy.deepcopy(document)

    def _snapshot_reference(self) -> Mapping[str, object]:
        """Return the current immutable publication without copying its tree.

        Refresh builds a separate payload and atomically replaces
        ``self._snapshot`` under ``_state_lock``; it never mutates a published
        snapshot in place.  A health request may therefore retain the old
        mapping safely while a newer publication is installed.  Public page
        callers still use :meth:`snapshot` and receive an isolated deep copy.
        """

        with self._state_lock:
            return self._snapshot

    def _record_background_heartbeat(self) -> None:
        with self._background_lock:
            # Native process progress is delivered by the request thread.  In
            # the parallel stock scanner that is intentionally not the owner
            # background thread, but it still proves the owned refresh is
            # alive.  Ignore callbacks only when no background owner exists.
            if self._background_thread is None:
                return
            self._background_heartbeat_at = normalize_datetime(self._clock(), "clock")

    def _record_background_refresh_start(self) -> None:
        observed_at = normalize_datetime(self._clock(), "clock")
        with self._background_lock:
            if self._background_thread is not current_thread():
                return
            self._background_heartbeat_at = observed_at
            self._background_refresh_started_at = observed_at

    def _record_background_result(
        self,
        payload: Mapping[str, object],
    ) -> None:
        observed_at = normalize_datetime(self._clock(), "clock")
        error = None
        if payload.get("scan_state") == "refresh_failed":
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, Mapping):
                    error_type = str(first.get("error") or "refresh_failed")
                    reason = str(first.get("reason") or "")[:160]
                    error = f"{error_type}: {reason}" if reason else error_type
            if error is None:
                error = "refresh_failed"
        with self._background_lock:
            self._background_heartbeat_at = observed_at
            self._background_refresh_started_at = None
            self._background_last_result_at = observed_at
            self._background_last_error = error
            self._background_iteration_count += 1

    def _record_background_exception(self, exc: Exception) -> None:
        observed_at = normalize_datetime(self._clock(), "clock")
        message = str(exc)[:160]
        error = type(exc).__name__
        if message:
            error = f"{error}: {message}"
        with self._background_lock:
            self._background_heartbeat_at = observed_at
            self._background_refresh_started_at = None
            self._background_last_result_at = observed_at
            self._background_last_error = error
            self._background_iteration_count += 1

    def _review_readiness_for_publication(
        self,
        snapshot: Mapping[str, object],
        *,
        identity_valid: bool,
    ) -> tuple[bool, str]:
        snapshot_sha256 = snapshot.get("snapshot_content_sha256")
        if identity_valid and isinstance(snapshot_sha256, str):
            with self._state_lock:
                if (
                    self._review_readiness_cache_sha256 == snapshot_sha256
                    and self._review_readiness_cache is not None
                ):
                    return self._review_readiness_cache
        raw_signals = snapshot.get("signals")
        large_publication = bool(
            identity_valid
            and isinstance(snapshot_sha256, str)
            and isinstance(raw_signals, list)
            and len(raw_signals) > 256
        )
        if large_publication:
            if self._materialized_review_receipt_is_current(snapshot_sha256):
                result = (True, "READY")
                with self._state_lock:
                    if self._snapshot is snapshot:
                        self._review_readiness_cache_sha256 = snapshot_sha256
                        self._review_readiness_cache = result
                return result
            with self._review_readiness_validation_lock:
                if self._review_readiness_validation_sha256 != snapshot_sha256:
                    self._review_readiness_validation_sha256 = snapshot_sha256
                    file_backed = bool(
                        hasattr(self, "_cache_path") and self._cache_path.is_file()
                    )
                    worker = Thread(
                        target=(
                            self._validate_review_readiness_file_in_background
                            if file_backed
                            else self._validate_review_readiness_in_background
                        ),
                        args=(snapshot, snapshot_sha256),
                        name="screening-review-boundary-validator",
                        daemon=True,
                    )
                    self._review_readiness_validation_thread = worker
                    worker.start()
            return False, "REVIEW_BOUNDARY_VALIDATION_PENDING"
        result = _screening_review_readiness(
            snapshot,
            identity_valid=identity_valid,
        )
        if identity_valid and isinstance(snapshot_sha256, str):
            with self._state_lock:
                if self._snapshot is snapshot:
                    self._review_readiness_cache_sha256 = snapshot_sha256
                    self._review_readiness_cache = result
        return result

    def _materialized_review_receipt_is_current(
        self,
        snapshot_sha256: str,
    ) -> bool:
        """Reuse an exact child verdict after app restart, never a stale one."""

        archive_root = getattr(self, "_human_review_archive_root", None)
        decision_source_id = getattr(
            self,
            "_human_review_decision_source_snapshot_id",
            None,
        )
        if archive_root is None or decision_source_id is None:
            return False
        report = resolve_live_review_materialization_receipt(
            source_path=self._cache_path,
            archive_root=archive_root,
            expected_source_snapshot_content_sha256=snapshot_sha256,
            expected_decision_source_snapshot_id=decision_source_id,
        )
        return report is not None

    def _validate_review_readiness_in_background(
        self,
        snapshot: Mapping[str, object],
        snapshot_sha256: str,
    ) -> None:
        """Compute one large immutable review verdict without blocking HTTP."""

        try:
            result = _screening_review_readiness(snapshot, identity_valid=True)
        except Exception:
            result = (False, "REVIEW_BOUNDARY_VALIDATION_FAILED")
        with self._state_lock:
            if self._snapshot is snapshot:
                self._review_readiness_cache_sha256 = snapshot_sha256
                self._review_readiness_cache = result
        with self._review_readiness_validation_lock:
            if self._review_readiness_validation_sha256 == snapshot_sha256:
                self._review_readiness_validation_sha256 = None
                self._review_readiness_validation_thread = None

    def _validate_review_readiness_file_in_background(
        self,
        snapshot: Mapping[str, object],
        snapshot_sha256: str,
    ) -> None:
        """Validate a large persisted snapshot outside the Web interpreter.

        ``validate_live_review_snapshot`` walks the complete 100+ MiB evidence
        tree.  A Python daemon thread keeps HTTP logically asynchronous but its
        CPU work still owns the GIL and can stall every page for minutes.  The
        publication has already been atomically persisted before installation,
        so a short-lived read-only child can validate that exact file/hash and
        return only the compact verdict.
        """

        project_root = Path(__file__).resolve().parents[4]
        validator = project_root / "tools" / "validate_trading_screening_review.py"
        command = [
            sys.executable,
            str(validator),
            "--input",
            str(self._cache_path),
            "--expected-sha256",
            snapshot_sha256,
        ]
        human_review_archive_root = getattr(
            self,
            "_human_review_archive_root",
            None,
        )
        if human_review_archive_root is not None:
            command.extend(
                [
                    "--archive-root",
                    str(human_review_archive_root),
                    "--repository-root",
                    str(project_root),
                ]
            )
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
                creationflags=creationflags,
            )
            document = json.loads(completed.stdout)
            if (
                not isinstance(document, Mapping)
                or document.get("snapshot_content_sha256") != snapshot_sha256
                or not isinstance(document.get("ready"), bool)
                or not isinstance(document.get("reason_code"), str)
                or completed.returncode not in {0, 3}
            ):
                raise ValueError("review validator returned an invalid verdict")
            result = bool(document["ready"]), str(document["reason_code"])
        except (
            OSError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            result = False, "REVIEW_BOUNDARY_VALIDATION_FAILED"
        with self._state_lock:
            if self._snapshot is snapshot:
                self._review_readiness_cache_sha256 = snapshot_sha256
                self._review_readiness_cache = result
        with self._review_readiness_validation_lock:
            if self._review_readiness_validation_sha256 == snapshot_sha256:
                self._review_readiness_validation_sha256 = None
                self._review_readiness_validation_thread = None

    def health_snapshot(self) -> dict[str, object]:
        """Return a side-effect-free operational attestation for screening.

        A live Flask process is not sufficient evidence that the QMT-backed
        scanner is usable: the scanner runs in a daemon thread and native
        ``xtquant`` failures cannot be caught by Python.  This document lets
        readiness verify the worker, its heartbeat, and the last publishable
        immutable snapshot without making any QMT call itself.
        """

        observed_at = normalize_datetime(self._clock(), "clock")
        with self._background_lock:
            worker = self._background_thread
            worker_alive = worker is not None and worker.is_alive()
            started_at = self._background_started_at
            heartbeat_at = self._background_heartbeat_at
            refresh_started_at = self._background_refresh_started_at
            last_result_at = self._background_last_result_at
            last_error = self._background_last_error
            iteration_count = self._background_iteration_count
            last_monitoring_at = self._last_monitoring_at
            last_monitoring_errors = tuple(copy.deepcopy(self._last_monitoring_errors))
            priority_monitor_last_at = self._priority_monitor_last_at
            priority_monitor_last_codes = tuple(self._priority_monitor_last_codes)
            priority_monitor_last_errors = tuple(
                copy.deepcopy(self._priority_monitor_last_errors)
            )
            priority_monitor_latest_signal_count = len(
                self._priority_monitor_latest_documents
            )
            priority_monitor_sector_source_mode = (
                self._priority_monitor_sector_source_mode
            )
            priority_monitor_sector_as_of = self._priority_monitor_sector_as_of
            priority_monitor_sector_coverage_epoch_id = (
                self._priority_monitor_sector_coverage_epoch_id
            )
            priority_monitor_runtime_verified = self._priority_monitor_runtime_verified
            candidate_monitor_started_at = self._candidate_monitor_started_at
            candidate_monitor_last_errors = tuple(
                copy.deepcopy(self._candidate_monitor_last_errors)
            )
            candidate_monitor_five_universe = tuple(
                self._candidate_monitor_five_universe
            )
            candidate_monitor_thirty_universe = tuple(
                self._candidate_monitor_thirty_universe
            )
            candidate_monitor_five_last_success_at = dict(
                self._candidate_monitor_five_last_success_at
            )
            candidate_monitor_thirty_last_success_at = dict(
                self._candidate_monitor_thirty_last_success_at
            )
            candidate_monitor_last_five_codes = tuple(
                self._candidate_monitor_last_five_codes
            )
            candidate_monitor_last_thirty_codes = tuple(
                self._candidate_monitor_last_thirty_codes
            )

        # Do not deep-copy the full-universe signal/evidence tree merely to
        # read health counters.  Identity is still recomputed against the
        # entire immutable publication below, so tamper detection is not
        # weakened.
        with self._state_lock:
            snapshot = self._snapshot
            validated_snapshot_sha256 = self._validated_snapshot_sha256
        scan_state = str(snapshot.get("scan_state") or "unknown")
        last_batch_state = str(snapshot.get("last_batch_state") or scan_state)
        snapshot_available = snapshot.get("available") is True
        snapshot_sha256 = snapshot.get("snapshot_content_sha256")
        try:
            # Publication is validated once before atomic installation.  The
            # installed tree is private and never mutated in place, so matching
            # its declared content identity avoids hashing a 100+ MiB snapshot
            # on every /readyz poll.  A cache loaded at startup is likewise
            # validated before installation; unknown publications still take
            # the full gate once and then become identity-cached.
            identity_valid = bool(
                isinstance(snapshot_sha256, str)
                and snapshot_sha256 == validated_snapshot_sha256
            )
            if not identity_valid:
                identity_valid = _cache_is_valid(
                    snapshot,
                    self._config,
                    self._decision_core_id,
                )
                if identity_valid:
                    with self._state_lock:
                        if self._snapshot is snapshot:
                            self._validated_snapshot_sha256 = str(snapshot_sha256)
            identity_valid = bool(
                identity_valid
                and snapshot.get("signal_document_contract_id")
                == SIGNAL_DOCUMENT_CONTRACT_ID
            )
            manifest_identity = snapshot.get("coverage_manifest")
            identity_valid = bool(
                identity_valid
                and isinstance(manifest_identity, Mapping)
                and manifest_identity.get("signal_document_contract_id")
                == SIGNAL_DOCUMENT_CONTRACT_ID
                and self._coverage_epoch_identity_valid(snapshot)
            )
        except (TypeError, ValueError):
            identity_valid = False
        heartbeat_age_seconds = (
            None
            if heartbeat_at is None
            else max(0.0, (observed_at - heartbeat_at).total_seconds())
        )
        heartbeat_max_age_seconds = max(
            180,
            self._config.refresh_interval_seconds * 3,
        )

        reasons: list[str] = []
        native_gateway_health: dict[str, object] | None = None
        native_health_provider = getattr(
            self._market_data,
            "health_snapshot",
            None,
        )
        if callable(native_health_provider):
            try:
                raw_native_health = native_health_provider()
                if not isinstance(raw_native_health, Mapping):
                    raise TypeError("native gateway health must be a mapping")
                native_gateway_health = dict(raw_native_health)
                if (
                    native_gateway_health.get("required") is True
                    and native_gateway_health.get("ready") is not True
                ):
                    reasons.append("screening_native_gateway_not_ready")
            except Exception as exc:
                native_gateway_health = {
                    "required": True,
                    "ready": False,
                    "status": "health_failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                }
                reasons.append("screening_native_gateway_health_failed")
        if not worker_alive:
            reasons.append("screening_worker_not_running")
        if heartbeat_at is None:
            reasons.append("screening_heartbeat_missing")
        elif heartbeat_age_seconds is not None and (
            heartbeat_age_seconds > heartbeat_max_age_seconds
        ):
            reasons.append("screening_heartbeat_stale")
        current_snapshot_required = self._config.full_coverage_refresh_enabled
        if not snapshot_available and current_snapshot_required:
            reasons.append("screening_snapshot_unavailable")
        elif snapshot_available and not identity_valid:
            reasons.append("screening_snapshot_identity_missing")
        if scan_state == "refresh_failed":
            reasons.append("screening_refresh_failed")
        elif scan_state in {
            "coverage_epoch_invalidated",
            "incomplete_not_published",
        }:
            reasons.append("screening_snapshot_not_publishable")
        if last_error is not None and scan_state != "refresh_failed":
            reasons.append("screening_background_error")
        scan_audit = snapshot.get("scan_audit")
        coverage_complete = None
        if not isinstance(scan_audit, Mapping):
            scan_audit = {}
        else:
            coverage_complete = scan_audit.get("coverage_cycle_complete")
        raw_full_coverage_state = snapshot.get("full_coverage_state")
        full_coverage_state = (
            str(raw_full_coverage_state)
            if isinstance(raw_full_coverage_state, str) and raw_full_coverage_state
            else "complete"
            if coverage_complete is True
            else "in_progress"
            if snapshot_available
            else "not_started"
        )
        coverage_manifest = snapshot.get("coverage_manifest")
        if not isinstance(coverage_manifest, Mapping):
            coverage_manifest = {}
        screening_review_ready, screening_review_reason_code = (
            self._review_readiness_for_publication(
                snapshot,
                identity_valid=identity_valid,
            )
        )
        snapshot_signals = snapshot.get("signals")
        signal_rows = (
            snapshot_signals if isinstance(snapshot_signals, (list, tuple)) else ()
        )
        daily_preselection_candidate_count = sum(
            1 for value in signal_rows if isinstance(value, Mapping)
        )
        daily_preselection_buy_candidate_count = sum(
            1
            for value in signal_rows
            if isinstance(value, Mapping)
            and str(value.get("point_type") or "").endswith("buy")
        )
        preselection_session = _preselection_target_session(
            snapshot,
            observed_at,
        )
        preselection_session_aligned = bool(preselection_session.get("aligned") is True)
        if (
            screening_review_ready
            and coverage_complete is True
            and preselection_session_aligned
        ):
            daily_preselection_ready = True
            daily_preselection_status = "ready"
            daily_preselection_reason_code = "READY"
        elif not snapshot_available:
            daily_preselection_ready = False
            daily_preselection_status = "awaiting_first_snapshot"
            daily_preselection_reason_code = "PRESELECTION_SNAPSHOT_MISSING"
        elif coverage_complete is not True:
            daily_preselection_ready = False
            daily_preselection_status = "coverage_in_progress"
            daily_preselection_reason_code = "PRESELECTION_COVERAGE_INCOMPLETE"
        elif not preselection_session_aligned:
            daily_preselection_ready = False
            daily_preselection_status = "target_session_stale"
            daily_preselection_reason_code = str(
                preselection_session.get("reason_code")
                or "PRESELECTION_TARGET_SESSION_MISMATCH"
            )
        else:
            daily_preselection_ready = False
            daily_preselection_status = "review_blocked"
            daily_preselection_reason_code = screening_review_reason_code
        refresh_suppressed = (
            last_result_at is not None
            and last_error is None
            and _complete_close_snapshot_can_idle(
                snapshot,
                observed_at,
                review_boundary_ready=screening_review_ready,
                phase_refresh_at=last_result_at,
            )
        )
        full_coverage_refresh_enabled = self._config.full_coverage_refresh_enabled
        full_coverage_refresh_window_open = bool(
            full_coverage_refresh_enabled
            and _full_coverage_refresh_window_open(observed_at)
        )
        monitoring_failure_codes = sorted(
            {
                str(error["code"])
                for error in last_monitoring_errors
                if isinstance(error.get("code"), str)
            }
        )
        monitoring_failure_reason_counts: dict[str, int] = {}
        for error in last_monitoring_errors:
            reason = str(error.get("reason_code") or "STOCK_ANALYSIS_UNCLASSIFIED")
            monitoring_failure_reason_counts[reason] = (
                monitoring_failure_reason_counts.get(reason, 0) + 1
            )

        priority_monitor_enabled = self._config.priority_monitoring_enabled
        priority_monitor_session_open = _priority_monitor_session_open(observed_at)
        priority_monitor_age_seconds = (
            None
            if priority_monitor_last_at is None
            else max(
                0.0,
                (observed_at - priority_monitor_last_at).total_seconds(),
            )
        )
        priority_monitor_max_age_seconds = max(
            180,
            self._config.priority_monitor_interval_seconds * 3,
        )
        priority_monitor_reason_codes: list[str] = []
        if not priority_monitor_enabled:
            priority_monitor_status = "disabled"
            priority_monitor_ready = True
        elif not priority_monitor_session_open:
            priority_monitor_status = "not_due"
            priority_monitor_ready = True
        elif not priority_monitor_runtime_verified:
            priority_monitor_status = "awaiting_runtime_verification"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_RUNTIME_UNVERIFIED")
        elif priority_monitor_last_at is None:
            priority_monitor_status = "awaiting_first_run"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_AWAITING_FIRST_RUN")
        elif priority_monitor_last_at > observed_at:
            priority_monitor_status = "clock_regressed"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_CLOCK_REGRESSED")
        elif (
            priority_monitor_age_seconds is not None
            and priority_monitor_age_seconds > priority_monitor_max_age_seconds
        ):
            priority_monitor_status = "stale"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_STALE")
        elif priority_monitor_last_errors:
            priority_monitor_status = "degraded"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_DEGRADED")
        else:
            priority_monitor_status = "verified"
            priority_monitor_ready = True
        priority_monitor_failure_reason_counts: dict[str, int] = {}
        for error in priority_monitor_last_errors:
            reason = str(error.get("reason_code") or "PRIORITY_MONITOR_UNCLASSIFIED")
            priority_monitor_failure_reason_counts[reason] = (
                priority_monitor_failure_reason_counts.get(reason, 0) + 1
            )
        five_candidate_coverage = _candidate_lane_coverage(
            candidate_monitor_five_universe,
            last_success_at=candidate_monitor_five_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.five_minute_candidate_target_seconds,
        )
        thirty_candidate_coverage = _candidate_lane_coverage(
            candidate_monitor_thirty_universe,
            last_success_at=candidate_monitor_thirty_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.thirty_minute_candidate_target_seconds,
        )
        five_required_per_refresh = (
            len(candidate_monitor_five_universe)
            * self._config.priority_monitor_interval_seconds
            + self._config.five_minute_candidate_target_seconds
            - 1
        ) // self._config.five_minute_candidate_target_seconds
        thirty_required_per_refresh = (
            len(candidate_monitor_thirty_universe)
            * self._config.priority_monitor_interval_seconds
            + self._config.thirty_minute_candidate_target_seconds
            - 1
        ) // self._config.thirty_minute_candidate_target_seconds
        candidate_capacity_sufficient = bool(
            five_required_per_refresh
            <= self._config.max_five_minute_candidate_symbols_per_refresh
            and thirty_required_per_refresh
            <= self._config.max_thirty_minute_candidate_symbols_per_refresh
        )
        candidate_monitor_failure_reason_counts: dict[str, int] = {}
        for error in candidate_monitor_last_errors:
            reason = str(error.get("reason_code") or "CANDIDATE_MONITOR_UNCLASSIFIED")
            candidate_monitor_failure_reason_counts[reason] = (
                candidate_monitor_failure_reason_counts.get(reason, 0) + 1
            )
        candidate_warmup_age_seconds = (
            None
            if candidate_monitor_started_at is None
            else max(
                0.0,
                (observed_at - candidate_monitor_started_at).total_seconds(),
            )
        )
        candidate_monitor_reason_codes: list[str] = []
        if not priority_monitor_enabled:
            candidate_monitor_status = "disabled"
            candidate_monitor_ready = True
        elif not priority_monitor_session_open:
            candidate_monitor_status = "not_due"
            candidate_monitor_ready = True
        elif not priority_monitor_runtime_verified:
            candidate_monitor_status = "awaiting_runtime_verification"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_RUNTIME_UNVERIFIED"
            )
        elif candidate_monitor_last_errors:
            candidate_monitor_status = "degraded"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_ERRORS")
        elif not candidate_capacity_sufficient:
            candidate_monitor_status = "capacity_insufficient"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
            )
        elif (
            five_candidate_coverage["ready"] is True
            and thirty_candidate_coverage["ready"] is True
        ):
            candidate_monitor_status = "verified"
            candidate_monitor_ready = True
        elif (
            candidate_warmup_age_seconds is not None
            and candidate_warmup_age_seconds
            <= self._config.thirty_minute_candidate_target_seconds
        ):
            candidate_monitor_status = "warming"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_WARMING")
        else:
            candidate_monitor_status = "cadence_overdue"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_CADENCE_OVERDUE")
        notification_dispatcher_configured = self._notifier is not None
        notification_delivery: dict[str, object] | None = None
        if notification_dispatcher_configured:
            health_provider = getattr(self._notifier, "health_snapshot", None)
            if callable(health_provider):
                try:
                    raw_notification_delivery = health_provider()
                    if isinstance(raw_notification_delivery, Mapping):
                        notification_delivery = dict(raw_notification_delivery)
                except Exception as exc:
                    notification_delivery = {
                        "schema": "chanlun-signal-notification-readiness",
                        "configured": True,
                        "operationally_verified": False,
                        "status": "unavailable",
                        "reason_code": "NOTIFICATION_HEALTH_UNAVAILABLE",
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                        "credentials_exposed": False,
                        "real_account_accessed": False,
                        "real_order_transport_enabled": False,
                        "live_status": "LIVE_DISABLED",
                    }
        notification_delivery_degraded = bool(
            notification_delivery is not None
            and notification_delivery.get("status") in {"degraded", "unavailable"}
        )
        if not notification_dispatcher_configured:
            realtime_alert_ready = False
            realtime_alert_status = "notification_not_configured"
            realtime_alert_reason_code = "REALTIME_NOTIFICATION_NOT_CONFIGURED"
        elif not priority_monitor_session_open:
            realtime_alert_ready = True
            realtime_alert_status = "not_due"
            realtime_alert_reason_code = "NON_TRADING_SESSION_NOT_DUE"
        elif notification_delivery_degraded:
            realtime_alert_ready = False
            realtime_alert_status = "notification_degraded"
            realtime_alert_reason_code = str(
                notification_delivery.get("reason_code")
                if notification_delivery is not None
                else "REALTIME_NOTIFICATION_DELIVERY_DEGRADED"
            )
        elif priority_monitor_ready:
            realtime_alert_ready = True
            realtime_alert_status = "ready"
            realtime_alert_reason_code = "READY"
        else:
            realtime_alert_ready = False
            realtime_alert_status = "monitor_degraded"
            realtime_alert_reason_code = (
                priority_monitor_reason_codes[0]
                if priority_monitor_reason_codes
                else "PRIORITY_MONITOR_DEGRADED"
            )

        member_history_diagnostics = snapshot.get("sector_member_history_diagnostics")
        if not isinstance(member_history_diagnostics, Mapping):
            member_history_diagnostics = None

        ready = not reasons
        return {
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "worker_alive": worker_alive,
            "background_started_at": (
                None if started_at is None else started_at.isoformat()
            ),
            "heartbeat_at": (
                None if heartbeat_at is None else heartbeat_at.isoformat()
            ),
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "heartbeat_max_age_seconds": heartbeat_max_age_seconds,
            "refresh_in_progress": refresh_started_at is not None,
            "refresh_started_at": (
                None if refresh_started_at is None else refresh_started_at.isoformat()
            ),
            "refresh_elapsed_seconds": (
                None
                if refresh_started_at is None
                else max(
                    0.0,
                    (observed_at - refresh_started_at).total_seconds(),
                )
            ),
            "last_result_at": (
                None if last_result_at is None else last_result_at.isoformat()
            ),
            "refresh_attempt_count": iteration_count,
            "last_error": last_error,
            "cache_recovered_from_generation": (self._cache_recovered_from_generation),
            "cache_generation_count": self._cache_generation_count,
            "cache_generation_error": self._cache_generation_error,
            "quarantined_cache_decision_core_id": (
                self._quarantined_cache_decision_core_id
            ),
            "quarantined_cache_reason": self._quarantined_cache_reason,
            "current_logic_snapshot_required": current_snapshot_required,
            "coverage_sector_restore_error": (self._coverage_sector_restore_error),
            "last_monitoring_at": (
                None if last_monitoring_at is None else last_monitoring_at.isoformat()
            ),
            "last_monitoring_failure_count": len(last_monitoring_errors),
            "last_monitoring_failure_codes": monitoring_failure_codes,
            "last_monitoring_failure_reason_counts": dict(
                sorted(monitoring_failure_reason_counts.items())
            ),
            "priority_monitoring_enabled": priority_monitor_enabled,
            "priority_monitor_runtime_verified": (priority_monitor_runtime_verified),
            "priority_monitor_ready": priority_monitor_ready,
            "priority_monitor_status": priority_monitor_status,
            "priority_monitor_reason_codes": priority_monitor_reason_codes,
            "priority_monitor_session_open": priority_monitor_session_open,
            "priority_monitor_due": self._priority_monitor_due(observed_at),
            "priority_monitor_last_at": (
                None
                if priority_monitor_last_at is None
                else priority_monitor_last_at.isoformat()
            ),
            "priority_monitor_age_seconds": priority_monitor_age_seconds,
            "priority_monitor_max_age_seconds": (priority_monitor_max_age_seconds),
            "priority_monitor_last_code_count": len(priority_monitor_last_codes),
            "priority_monitor_last_codes": list(priority_monitor_last_codes),
            "priority_monitor_last_error_count": len(priority_monitor_last_errors),
            "priority_monitor_sector_source_mode": (
                priority_monitor_sector_source_mode
            ),
            "priority_monitor_sector_as_of": (
                None
                if priority_monitor_sector_as_of is None
                else priority_monitor_sector_as_of.isoformat()
            ),
            "priority_monitor_sector_coverage_epoch_id": (
                priority_monitor_sector_coverage_epoch_id
            ),
            "priority_monitor_latest_signal_count": (
                priority_monitor_latest_signal_count
            ),
            "priority_monitor_last_failure_reason_counts": dict(
                sorted(priority_monitor_failure_reason_counts.items())
            ),
            "candidate_monitor_contract_id": CANDIDATE_MONITOR_CONTRACT_ID,
            "candidate_monitor_ready": candidate_monitor_ready,
            "candidate_monitor_status": candidate_monitor_status,
            "candidate_monitor_reason_codes": candidate_monitor_reason_codes,
            "candidate_monitor_started_at": (
                None
                if candidate_monitor_started_at is None
                else candidate_monitor_started_at.isoformat()
            ),
            "candidate_monitor_capacity_sufficient": (candidate_capacity_sufficient),
            "candidate_monitor_last_error_count": len(candidate_monitor_last_errors),
            "candidate_monitor_last_failure_reason_counts": dict(
                sorted(candidate_monitor_failure_reason_counts.items())
            ),
            "candidate_monitor_five_minute": {
                **five_candidate_coverage,
                "scope": "OWNED_WATCHED_AND_EXISTING_BUY_CANDIDATES",
                "required_symbols_per_refresh": five_required_per_refresh,
                "max_symbols_per_refresh": (
                    self._config.max_five_minute_candidate_symbols_per_refresh
                ),
                "last_batch_count": len(candidate_monitor_last_five_codes),
                "last_batch_codes": list(candidate_monitor_last_five_codes),
                "requested_frequencies": ["5m"],
            },
            "candidate_monitor_thirty_minute": {
                **thirty_candidate_coverage,
                "scope": "SUPPORTIVE_SECTOR_DISCOVERY_AND_EXISTING_CANDIDATES",
                "required_symbols_per_refresh": thirty_required_per_refresh,
                "max_symbols_per_refresh": (
                    self._config.max_thirty_minute_candidate_symbols_per_refresh
                ),
                "last_batch_count": len(candidate_monitor_last_thirty_codes),
                "last_batch_codes": list(candidate_monitor_last_thirty_codes),
                "requested_frequencies": ["5m", "30m"],
            },
            "notification_dispatcher_configured": (notification_dispatcher_configured),
            "notification_delivery": copy.deepcopy(notification_delivery),
            "notification_operationally_verified": bool(
                notification_delivery is not None
                and notification_delivery.get("operationally_verified") is True
            ),
            "notification_delivered_event_count": (
                int(notification_delivery.get("delivered_event_count", 0))
                if notification_delivery is not None
                else 0
            ),
            "notification_last_success_at": (
                notification_delivery.get("last_success_at")
                if notification_delivery is not None
                else None
            ),
            "notification_last_failure_at": (
                notification_delivery.get("last_failure_at")
                if notification_delivery is not None
                else None
            ),
            "realtime_alert_ready": realtime_alert_ready,
            "realtime_alert_status": realtime_alert_status,
            "realtime_alert_reason_code": realtime_alert_reason_code,
            "realtime_alert_delivery_mode": (
                "ACTIVE_NOTIFICATION_DISPATCHER"
                if notification_dispatcher_configured
                else "PAGE_ONLY"
            ),
            "refresh_suppressed": refresh_suppressed,
            "refresh_suppression_reason": (
                COMPLETE_CLOSE_IDLE_REASON if refresh_suppressed else None
            ),
            "full_coverage_refresh_enabled": full_coverage_refresh_enabled,
            "full_coverage_refresh_window_open": (full_coverage_refresh_window_open),
            "full_coverage_refresh_paused": (not full_coverage_refresh_window_open),
            "full_coverage_refresh_pause_reason": (
                None
                if full_coverage_refresh_window_open
                else (
                    FULL_COVERAGE_PAUSE_REASON
                    if full_coverage_refresh_enabled
                    else "FULL_COVERAGE_REFRESH_DISABLED"
                )
            ),
            "full_coverage_next_active_at": (
                None
                if (
                    full_coverage_refresh_window_open
                    or not full_coverage_refresh_enabled
                )
                else _next_full_coverage_active_start(observed_at).isoformat()
            ),
            "next_background_active_at": (
                _next_background_active_start(observed_at).isoformat()
                if refresh_suppressed
                else None
            ),
            "background_active_windows": [
                {
                    "phase": "POST_CLOSE_PRESELECTION",
                    "timezone": "Asia/Shanghai",
                    "weekdays": [0, 1, 2, 3, 4],
                    "start": POST_CLOSE_PRESELECTION_START.isoformat(),
                    "end": POST_CLOSE_PRESELECTION_END.isoformat(),
                },
                {
                    "phase": "OVERNIGHT_COVERAGE_CONTINUATION",
                    "timezone": "Asia/Shanghai",
                    "weekdays": [0, 1, 2, 3, 4],
                    "start": OVERNIGHT_COVERAGE_CONTINUATION_START.isoformat(),
                    "end": OVERNIGHT_COVERAGE_CONTINUATION_END.isoformat(),
                },
                {
                    "phase": "PREOPEN_RECONCILIATION",
                    "timezone": "Asia/Shanghai",
                    "weekdays": [0, 1, 2, 3, 4],
                    "start": PREOPEN_RECONCILIATION_START.isoformat(),
                    "end": PREOPEN_RECONCILIATION_END.isoformat(),
                },
            ],
            "scan_state": scan_state,
            "last_batch_state": last_batch_state,
            "full_coverage_state": full_coverage_state,
            "snapshot_available": snapshot_available,
            "snapshot_content_sha256": (snapshot_sha256 if identity_valid else None),
            "as_of": snapshot.get("as_of"),
            "market_data_as_of": snapshot.get("market_data_as_of"),
            "scanned_at": snapshot.get("scanned_at"),
            "coverage_epoch_id": snapshot.get("coverage_epoch_id"),
            "screening_policy_id": snapshot.get("screening_policy_id"),
            "sector_coverage_contract_id": snapshot.get("sector_coverage_contract_id"),
            "sector_discovered_count": scan_audit.get("sector_discovered_count", 0),
            "sector_completed_count": scan_audit.get("sector_completed_count", 0),
            "sector_excluded_count": scan_audit.get("sector_excluded_count", 0),
            "sector_failed_count": scan_audit.get("sector_failed_count", 0),
            "sector_resolved_count": scan_audit.get("sector_resolved_count", 0),
            "sector_completion_ratio": scan_audit.get("sector_completion_ratio", "0"),
            "sector_resolution_ratio": scan_audit.get("sector_resolution_ratio", "0"),
            "sector_failure_counts": scan_audit.get("sector_failure_counts", {}),
            "sector_exclusion_counts": scan_audit.get("sector_exclusion_counts", {}),
            "sector_exclusions": snapshot.get("sector_exclusions", []),
            "sector_member_history_diagnostics": (
                None
                if member_history_diagnostics is None
                else copy.deepcopy(dict(member_history_diagnostics))
            ),
            "coverage_cycle_complete": coverage_complete,
            "screening_review_ready": screening_review_ready,
            "screening_review_reason_code": screening_review_reason_code,
            "daily_preselection_ready": daily_preselection_ready,
            "daily_preselection_status": daily_preselection_status,
            "daily_preselection_reason_code": (daily_preselection_reason_code),
            "daily_preselection_candidate_count": (daily_preselection_candidate_count),
            "daily_preselection_buy_candidate_count": (
                daily_preselection_buy_candidate_count
            ),
            "daily_preselection_market_data_as_of": snapshot.get("market_data_as_of"),
            "daily_preselection_target_session": preselection_session.get(
                "target_session"
            ),
            "daily_preselection_expected_session": preselection_session.get(
                "expected_session"
            ),
            "daily_preselection_session_aligned": (preselection_session_aligned),
            "daily_preselection_calendar_source": preselection_session.get(
                "calendar_source"
            ),
            "daily_preselection_refresh_schedule": (
                "MON-FRI 15:05 Asia/Shanghai FOR_NEXT_SESSION"
            ),
            "daily_preselection_reconcile_schedule": ("MON-FRI 08:45 Asia/Shanghai"),
            "daily_preselection_capture_schedule": ("MON-FRI 09:10 Asia/Shanghai"),
            "pending_symbol_count": self._pending_symbol_count(snapshot),
            "immediate_pending_symbol_count": scan_audit.get(
                "immediate_pending_symbol_count",
                scan_audit.get("pending_symbol_count", 0),
            ),
            "retry_symbol_count": scan_audit.get("retry_symbol_count", 0),
            "backoff_retry_symbol_count": scan_audit.get(
                "backoff_retry_symbol_count", 0
            ),
            "next_epoch_retry_symbol_count": scan_audit.get(
                "next_epoch_retry_symbol_count", 0
            ),
            "coverage_cycle_batch_count": scan_audit.get(
                "coverage_cycle_batch_count", 0
            ),
            "coverage_cycle_started_at": scan_audit.get("coverage_cycle_started_at"),
            "discovered_symbol_count": scan_audit.get("discovered_symbol_count", 0),
            "coverage_cycle_attempted_symbol_count": scan_audit.get(
                "coverage_cycle_attempted_symbol_count", 0
            ),
            "coverage_cycle_completed_symbol_count": scan_audit.get(
                "coverage_cycle_completed_symbol_count", 0
            ),
            "coverage_cycle_excluded_symbol_count": scan_audit.get(
                "coverage_cycle_excluded_symbol_count", 0
            ),
            "coverage_cycle_failed_symbol_count": scan_audit.get(
                "coverage_cycle_failed_symbol_count", 0
            ),
            "coverage_cycle_resolved_symbol_count": scan_audit.get(
                "coverage_cycle_resolved_symbol_count", 0
            ),
            "coverage_cycle_completion_ratio": scan_audit.get(
                "coverage_cycle_completion_ratio", "0"
            ),
            "coverage_cycle_resolution_ratio": scan_audit.get(
                "coverage_cycle_resolution_ratio", "0"
            ),
            "coverage_cycle_progress_ratio": scan_audit.get(
                "coverage_cycle_progress_ratio", "0"
            ),
            "coverage_cycle_finalized_symbol_count": scan_audit.get(
                "coverage_cycle_finalized_symbol_count", 0
            ),
            "coverage_cycle_runtime_baseline_finalized_symbol_count": (
                scan_audit.get(
                    "coverage_cycle_runtime_baseline_finalized_symbol_count",
                    0,
                )
            ),
            "coverage_cycle_runtime_finalized_symbol_count": scan_audit.get(
                "coverage_cycle_runtime_finalized_symbol_count", 0
            ),
            "coverage_cycle_throughput_symbols_per_minute": scan_audit.get(
                "coverage_cycle_throughput_symbols_per_minute"
            ),
            "coverage_cycle_estimated_remaining_seconds": scan_audit.get(
                "coverage_cycle_estimated_remaining_seconds"
            ),
            "stock_exclusion_counts": scan_audit.get("stock_exclusion_counts", {}),
            "coverage_excluded_codes": coverage_manifest.get("excluded_codes", []),
            "coverage_exclusions": coverage_manifest.get("exclusions", []),
            "superseded_coverage_epoch_id": coverage_manifest.get(
                "superseded_coverage_epoch_id"
            ),
            "superseded_market_data_as_of": coverage_manifest.get(
                "superseded_market_data_as_of"
            ),
            "native_gateway": native_gateway_health,
            "reasons": reasons,
        }

    def _needs_refresh(self) -> bool:
        with self._state_lock:
            snapshot = self._snapshot
            validated_snapshot_sha256 = self._validated_snapshot_sha256
        observed_at = normalize_datetime(self._clock(), "clock")
        with self._background_lock:
            last_result_at = self._background_last_result_at
            has_successful_process_refresh = (
                last_result_at is not None and self._background_last_error is None
            )
        if has_successful_process_refresh:
            identity_valid = bool(
                isinstance(snapshot.get("snapshot_content_sha256"), str)
                and snapshot.get("snapshot_content_sha256") == validated_snapshot_sha256
            )
            review_ready, _reason = self._review_readiness_for_publication(
                snapshot,
                identity_valid=identity_valid,
            )
            if _reason == "REVIEW_BOUNDARY_VALIDATION_PENDING":
                # The already hash-validated complete publication remains the
                # page source while its deeper review contract is checked.
                # Starting another full scan here would compete with the one
                # bounded validator and cannot improve the current verdict.
                return False
            if _complete_close_snapshot_can_idle(
                snapshot,
                observed_at,
                review_boundary_ready=review_ready,
                phase_refresh_at=last_result_at,
            ):
                return False
        generated = self._cached_scanned_at(snapshot)
        if generated is None:
            return True
        return observed_at - generated >= timedelta(
            seconds=self._config.refresh_interval_seconds
        )

    @staticmethod
    def _pending_symbol_count(snapshot: Mapping[str, object]) -> int:
        audit = snapshot.get("scan_audit")
        if not isinstance(audit, Mapping):
            return 0
        try:
            return max(0, int(audit.get("pending_symbol_count", 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _immediate_pending_symbol_count(snapshot: Mapping[str, object]) -> int:
        audit = snapshot.get("scan_audit")
        if not isinstance(audit, Mapping):
            return 0
        raw = audit.get(
            "immediate_pending_symbol_count",
            audit.get("pending_symbol_count", 0),
        )
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _backoff_retry_symbol_count(snapshot: Mapping[str, object]) -> int:
        audit = snapshot.get("scan_audit")
        if not isinstance(audit, Mapping):
            return 0
        try:
            return max(0, int(audit.get("backoff_retry_symbol_count", 0)))
        except (TypeError, ValueError):
            return 0

    def _background_loop(self, stop: Event, wake: Event) -> None:
        try:
            while not stop.is_set():
                self._record_background_heartbeat()
                # Clear the event before inspecting state so a concurrent wake-up
                # between this point and ``wait`` cannot be lost.
                wake.clear()
                if stop.is_set():
                    break
                snapshot = self._snapshot_reference()
                has_pending = self._immediate_pending_symbol_count(snapshot) > 0
                has_backoff_retry = self._backoff_retry_symbol_count(snapshot) > 0
                observed_at = normalize_datetime(self._clock(), "clock")
                coverage_window_open = bool(
                    self._config.full_coverage_refresh_enabled
                    and _full_coverage_refresh_window_open(observed_at)
                )
                priority_monitor_due = self._priority_monitor_due(observed_at)
                if (
                    has_pending
                    or has_backoff_retry
                    or priority_monitor_due
                    or self._needs_refresh()
                ):
                    if self._scan_lock.locked():
                        wake.wait(timeout=1.0)
                        continue
                    self._record_background_refresh_start()
                    try:
                        refreshed = self.refresh_now(
                            copy_result=False,
                            priority_only=not coverage_window_open,
                        )
                    except Exception as exc:
                        # Persistence and notification failures live outside the
                        # refresh error snapshot. Keep the worker alive, but avoid
                        # a tight retry loop.
                        self._record_background_exception(exc)
                        wake.wait(timeout=float(self._config.refresh_interval_seconds))
                        continue
                    self._record_background_result(refreshed)
                    if (
                        coverage_window_open
                        and refreshed.get("scan_state") == "complete"
                        and self._immediate_pending_symbol_count(refreshed) > 0
                    ):
                        # Drain the discovery queue batch by batch. This is what
                        # makes progress independent of page polling.
                        continue
                    if stop.is_set():
                        break
                    if (
                        not coverage_window_open
                        and self._config.priority_monitoring_enabled
                        and _priority_monitor_session_open(
                            normalize_datetime(self._clock(), "clock")
                        )
                    ):
                        completed_at = normalize_datetime(self._clock(), "clock")
                        with self._background_lock:
                            priority_last_at = self._priority_monitor_last_at
                        delay = _priority_monitor_delay_seconds(
                            completed_at,
                            priority_last_at,
                            interval_seconds=(
                                self._config.priority_monitor_interval_seconds
                            ),
                        )
                        if delay <= 0:
                            continue
                        wake.wait(
                            timeout=min(
                                float(self._config.refresh_interval_seconds),
                                delay,
                            )
                        )
                        continue

                # Once a coverage cycle is complete (or a batch failed), pace the
                # next attempt from completion time instead of immediately looping
                # when a slow batch took longer than the nominal refresh interval.
                wake.wait(timeout=float(self._config.refresh_interval_seconds))
        finally:
            with self._background_lock:
                if self._background_thread is current_thread():
                    self._background_thread = None

    def start_background(self) -> Thread:
        """Start the page-independent incremental scanner, idempotently."""

        with self._background_lock:
            existing = self._background_thread
            if existing is not None and existing.is_alive():
                return existing
            self._background_stop = Event()
            self._background_wake = Event()
            started_at = normalize_datetime(self._clock(), "clock")
            self._background_started_at = started_at
            self._background_heartbeat_at = started_at
            self._background_refresh_started_at = None
            self._background_last_result_at = None
            self._background_last_error = None
            self._background_iteration_count = 0
            worker = Thread(
                target=self._background_loop,
                args=(self._background_stop, self._background_wake),
                daemon=True,
                name="trading-screening-background",
            )
            self._background_thread = worker
            worker.start()
            return worker

    def shutdown_background(
        self,
        *,
        wait: bool = True,
        timeout: float | None = 1.0,
    ) -> bool:
        """Signal the background scanner and report whether it has stopped."""

        with self._background_lock:
            worker = self._background_thread
            stop = self._background_stop
            wake = self._background_wake
        if worker is None:
            return True
        stop.set()
        wake.set()
        if wait and worker is not current_thread():
            worker.join(timeout=timeout)
        stopped = not worker.is_alive()
        if stopped:
            with self._background_lock:
                if self._background_thread is worker:
                    self._background_thread = None
        return stopped

    def ensure_refresh(self) -> bool:
        snapshot = self._snapshot_reference()
        if (
            not self._needs_refresh()
            and self._immediate_pending_symbol_count(snapshot) == 0
        ) or self._scan_lock.locked():
            return False
        with self._background_lock:
            background_running = (
                self._background_thread is not None
                and self._background_thread.is_alive()
            )
            if background_running:
                self._background_wake.set()
                return True
        Thread(
            target=lambda: self.refresh_now(copy_result=False),
            daemon=True,
            name="trading-screening",
        ).start()
        return True

    def notify_instrument_scope_changed(self) -> bool:
        """Wake the priority lane after a watchlist/holding membership edit.

        Providers are evaluated inside every priority scan, so there is no
        membership cache to invalidate. Marking the runtime observation
        unverified makes the next background-loop iteration due immediately;
        the wake event removes the otherwise possible one-minute delay.
        """

        with self._background_lock:
            self._priority_monitor_runtime_verified = False
            worker = self._background_thread
            if worker is None or not worker.is_alive():
                return False
            self._background_wake.set()
            return True

    def _assert_coverage_progress_non_regression(
        self,
        payload: Mapping[str, object],
        *,
        cache_valid: bool | None = None,
    ) -> None:
        """Reject any loss of completed symbols in one coverage epoch."""

        previous = self._snapshot_reference()
        if cache_valid is None:
            cache_valid = _cache_is_valid(
                payload,
                self._config,
                self._decision_core_id,
            )
        if not cache_valid:
            return
        if previous.get("coverage_epoch_id") != payload.get("coverage_epoch_id"):
            return
        old_manifest = previous.get("coverage_manifest")
        new_manifest = payload.get("coverage_manifest")
        if not isinstance(old_manifest, Mapping) or not isinstance(
            new_manifest, Mapping
        ):
            return
        old_discovered = {
            value
            for value in old_manifest.get("discovered_codes", ())
            if isinstance(value, str)
        }
        new_discovered = {
            value
            for value in new_manifest.get("discovered_codes", ())
            if isinstance(value, str)
        }
        lost_discovered = old_discovered - new_discovered
        if lost_discovered:
            raise ValueError(
                "same coverage epoch lost discovered symbols: "
                f"{','.join(sorted(lost_discovered)[:8])}"
            )
        old_completed = {
            value
            for value in old_manifest.get("completed_codes", ())
            if isinstance(value, str)
        }
        new_completed = {
            value
            for value in new_manifest.get("completed_codes", ())
            if isinstance(value, str)
        }
        new_excluded = {
            value
            for value in new_manifest.get("excluded_codes", ())
            if isinstance(value, str)
        }
        unexplained = old_completed - new_completed - new_excluded
        if unexplained:
            raise ValueError(
                "same coverage epoch lost completed symbols: "
                f"{','.join(sorted(unexplained)[:8])}"
            )

    def _persist_atomic(
        self,
        payload: Mapping[str, object],
        *,
        cache_valid: bool | None = None,
    ) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._cache_path.with_suffix(self._cache_path.suffix + ".lock")
        with interprocess_file_lock(lock_path, timeout_seconds=30.0):
            if cache_valid is None:
                cache_valid = _cache_is_valid(
                    payload,
                    self._config,
                    self._decision_core_id,
                )
            self._assert_coverage_progress_non_regression(
                payload,
                cache_valid=cache_valid,
            )
            temporary = self._cache_path.with_name(
                f".{self._cache_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
            )
            manifest = payload.get("coverage_manifest")
            create_generation = bool(
                cache_valid
                and isinstance(manifest, Mapping)
                and manifest.get("complete") is True
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    # Stream directly into the atomic replacement.  Building a
                    # second 100+ MiB ``json.dumps`` string on every coverage
                    # batch caused avoidable memory spikes and GC pauses.
                    json.dump(
                        payload,
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                if create_generation:
                    generation_sha256 = str(payload["snapshot_content_sha256"])
                    generation_name = generation_sha256.removeprefix("sha256:")
                    generation_directory = self._cache_generation_directory()
                    generation_path = generation_directory / f"{generation_name}.json"
                    try:
                        generation_directory.mkdir(parents=True, exist_ok=True)
                        if not generation_path.exists():
                            generation_temporary = generation_directory / (
                                f".{generation_path.name}.{os.getpid()}."
                                f"{uuid4().hex}.tmp"
                            )
                            try:
                                with (
                                    temporary.open("rb") as source,
                                    generation_temporary.open("xb") as destination,
                                ):
                                    shutil.copyfileobj(
                                        source,
                                        destination,
                                        length=1024 * 1024,
                                    )
                                    destination.flush()
                                    os.fsync(destination.fileno())
                                os.replace(generation_temporary, generation_path)
                            finally:
                                generation_temporary.unlink(missing_ok=True)
                        generations = self._generation_paths()
                        for expired in generations[_CACHE_GENERATION_RETENTION:]:
                            expired.unlink(missing_ok=True)
                        self._cache_generation_count = min(
                            len(generations), _CACHE_GENERATION_RETENTION
                        )
                        self._cache_generation_error = None
                    except OSError as exc:
                        # A backup failure must remain visible but must not
                        # prevent the atomic primary snapshot from progressing.
                        self._cache_generation_error = (
                            f"{type(exc).__name__}: {str(exc)[:160]}"
                        )
                os.replace(temporary, self._cache_path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _frequency_document(
        value: Mapping[str, set[str]],
    ) -> dict[str, list[str]]:
        order = {"d": 0, "30m": 1, "5m": 2, "1m": 3}
        return {
            code: sorted(frequencies, key=lambda item: order[item])
            for code, frequencies in sorted(value.items())
            if frequencies
        }

    def _coverage_manifest(self, *, complete: bool) -> dict[str, object]:
        return {
            "schema": COVERAGE_MANIFEST_SCHEMA,
            "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "coverage_epoch_id": self._coverage_epoch_id,
            "screening_policy_id": _screening_policy_id(),
            "source_cutoff": (
                None
                if self._coverage_cycle_started_at is None
                else self._coverage_cycle_started_at.isoformat()
            ),
            "market_data_as_of": (
                None
                if self._coverage_market_data_as_of is None
                else self._coverage_market_data_as_of.isoformat()
            ),
            "universe_revision": self._coverage_universe_revision,
            "sector_catalog_revision": (self._coverage_sector_catalog_revision),
            "sector_strength_evidence_revision": (
                self._coverage_sector_strength_evidence_revision
            ),
            "superseded_coverage_epoch_id": (self._coverage_cycle_superseded_epoch_id),
            "superseded_market_data_as_of": (
                None
                if self._coverage_cycle_superseded_market_data_as_of is None
                else self._coverage_cycle_superseded_market_data_as_of.isoformat()
            ),
            "discovered_codes": sorted(self._coverage_cycle_discovered_codes),
            "completed_codes": sorted(self._coverage_cycle_completed_codes),
            "excluded_codes": sorted(self._coverage_cycle_excluded_codes),
            "failed_codes": sorted(self._coverage_cycle_failed_codes),
            "exclusions": [
                copy.deepcopy(self._coverage_cycle_exclusions[code])
                for code in sorted(self._coverage_cycle_exclusions)
            ],
            "discarded_out_of_scope_retry_codes": sorted(
                self._coverage_cycle_discarded_retry_codes
            ),
            "pending_frequencies": self._frequency_document(self._pending_frequencies),
            "backoff_frequencies": self._frequency_document(self._backoff_frequencies),
            "deferred_frequencies": self._frequency_document(
                self._deferred_frequencies
            ),
            "complete": complete,
            "batch_count": self._coverage_cycle_batch_count,
        }

    def _finalize_snapshot_identity(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        payload["snapshot_content_sha256"] = live_screening_snapshot_content_sha256(
            payload
        )
        return payload

    @staticmethod
    def _sector_market_data_as_of(
        assessments: tuple[SectorAssessment, ...],
        fallback: datetime,
    ) -> datetime:
        observed = tuple(
            context.observed_at
            for assessment in assessments
            for context in (
                assessment.thirty_context,
                assessment.five_context,
                assessment.one_context,
            )
            if context is not None
        )
        return max(observed) if observed else fallback

    def _structure_bundle_with_causal_risk(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        """Load a current low-level bundle with causally frozen M/W/D facts.

        ``market_data_as_of`` is the atomic sector/coverage cutoff.  A current
        1m bar may close after that cutoff but before the scan wall clock; the
        low-level signal keeps that precision while M/W/D evidence must equal
        ``min(bundle.as_of, market_data_as_of)``.  Every provider must expose
        the explicit cutoff-aware method.
        """

        observed = normalize_datetime(as_of, "as_of")
        cutoff = normalize_datetime(
            risk_evidence_cutoff,
            "risk_evidence_cutoff",
        )
        if cutoff > observed:
            raise ValueError("risk evidence cutoff cannot be after scan as_of")
        bundle = self._market_data.structure_bundle_with_risk_cutoff(
            code,
            as_of=observed,
            sector=sector,
            frequencies=frequencies,
            risk_evidence_cutoff=cutoff,
        )
        if not isinstance(bundle, SymbolStructureBundle):
            raise TypeError("structure bundle provider returned an invalid result")
        gates = bundle.higher_timeframe_gates
        if gates is not None:
            expected = min(bundle.as_of, cutoff)
            evidence = (
                gates.market,
                gates.sector,
                gates.symbol,
            )
            if any(item.observed_at != expected for item in evidence):
                raise ValueError("higher_timeframe_evidence_cutoff_mismatch")
        return bundle

    def _take_scan_batch(
        self,
        plan: ScanPlan,
        *,
        priority_codes: tuple[str, ...],
        scan_order_codes: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
        frequency_order = ("1m", "5m", "30m", "d")
        for code in plan.symbols:
            self._pending_frequencies.setdefault(code, set()).update(
                plan.frequencies_for(code)
            )
        priority = tuple(
            code
            for code in sorted(set(priority_codes))
            if code in self._pending_frequencies
        )
        if priority:
            start = self._monitor_offset % len(priority)
            rotated = priority[start:] + priority[:start]
            monitors = rotated[
                : min(
                    self._config.max_monitor_symbols_per_refresh,
                    self._config.max_total_symbols_per_refresh,
                )
            ]
            self._monitor_offset = (start + len(monitors)) % len(priority)
        else:
            monitors = ()
            self._monitor_offset = 0
        priority_set = set(priority)
        ordered_remaining: list[str] = []
        seen = set(priority_set)
        # ``scan_order_codes`` is an operational priority only.  It is derived
        # from the already frozen sector ranking and never removes a symbol from
        # the complete eligible-sector coverage scope.
        for code in scan_order_codes:
            if code in self._pending_frequencies and code not in seen:
                ordered_remaining.append(code)
                seen.add(code)
        ordered_remaining.extend(
            code for code in sorted(self._pending_frequencies) if code not in seen
        )
        remaining = tuple(ordered_remaining)
        remaining_capacity = max(
            0,
            self._config.max_total_symbols_per_refresh - len(monitors),
        )
        discovery = remaining[
            : min(
                self._config.max_symbols_per_refresh,
                remaining_capacity,
            )
        ]
        symbols = monitors + discovery
        frequencies = {
            code: tuple(
                frequency
                for frequency in frequency_order
                if frequency in self._pending_frequencies[code]
            )
            for code in symbols
        }
        for code in symbols:
            self._pending_frequencies.pop(code, None)
        return symbols, frequencies

    def _take_monitoring_batch(
        self,
        plan: ScanPlan,
        *,
        priority_codes: tuple[str, ...],
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
        """Take a bounded same-epoch monitor batch without reopening coverage.

        Once a market cutoff/universe epoch is complete, repeated observations
        of active signals may still advance their review lifecycle.  They are
        not missing universe coverage, however, and must never be put back into
        ``_pending_frequencies``.  Otherwise the persisted manifest oscillates
        from complete to incomplete every few minutes and an unchanged close is
        rescanned forever.
        """

        candidates = tuple(sorted(set(plan.symbols)))
        if not candidates:
            self._monitor_offset = 0
            return (), {}
        priority_set = set(priority_codes)
        priority = tuple(code for code in candidates if code in priority_set)
        ordinary = tuple(code for code in candidates if code not in priority_set)
        ordered = priority + ordinary
        start = self._monitor_offset % len(ordered)
        rotated = ordered[start:] + ordered[:start]
        limit = max(
            self._config.max_monitor_symbols_per_refresh,
            self._config.max_symbols_per_refresh,
        )
        symbols = rotated[:limit]
        self._monitor_offset = (start + len(symbols)) % len(ordered)
        return symbols, {code: plan.frequencies_for(code) for code in symbols}

    def _defer_symbols_to_next_cycle(
        self,
        symbols: tuple[str, ...],
        frequencies: Mapping[str, tuple[str, ...]],
    ) -> None:
        for code in symbols:
            self._deferred_frequencies.setdefault(code, set()).update(
                frequencies.get(code, ())
            )

    def _defer_symbols_to_backoff_refresh(
        self,
        symbols: tuple[str, ...],
        frequencies: Mapping[str, tuple[str, ...]],
    ) -> None:
        for code in symbols:
            self._backoff_frequencies.setdefault(code, set()).update(
                frequencies.get(code, ())
            )

    @staticmethod
    def _error_identity(error: Mapping[str, object]) -> str:
        subject = error.get("code") or error.get("sector_id") or "unknown"
        return f"{error.get('error_type', 'error')}:{subject}"

    def _begin_coverage_cycle(
        self,
        *,
        as_of: datetime,
        market_data_as_of: datetime,
        universe_revision: str,
        sector_catalog_revision: str,
        sector_strength_evidence_revision: str | None,
        started_perf: float,
        superseded_epoch_id: str | None = None,
        superseded_market_data_as_of: datetime | None = None,
    ) -> None:
        self._coverage_cycle_started_at = as_of
        self._coverage_market_data_as_of = market_data_as_of
        self._coverage_universe_revision = universe_revision
        self._coverage_sector_catalog_revision = sector_catalog_revision
        self._coverage_sector_strength_evidence_revision = (
            sector_strength_evidence_revision
        )
        self._coverage_epoch_id = screening_coverage_epoch_id(
            market_data_as_of=market_data_as_of,
            universe_revision=universe_revision,
            sector_catalog_revision=sector_catalog_revision,
            sector_strength_evidence_revision=(sector_strength_evidence_revision),
            decision_core_id=self._decision_core_id,
            screening_policy_id=_screening_policy_id(),
            structure_contract_id=self._config.structure_contract_id,
            parameter_set_id=self._config.parameter_set_id,
        )
        self._coverage_cycle_started_perf = started_perf
        self._coverage_runtime_baseline_finalized_count = 0
        self._coverage_cycle_batch_count = 0
        self._coverage_cycle_discovered_codes.clear()
        self._coverage_cycle_completed_codes.clear()
        self._coverage_cycle_excluded_codes.clear()
        self._coverage_cycle_failed_codes.clear()
        self._coverage_cycle_exclusions.clear()
        self._coverage_cycle_discarded_retry_codes.clear()
        self._coverage_cycle_errors.clear()
        self._coverage_cycle_full_market_history_scan = False
        self._coverage_cycle_background_refresh_required = False
        with self._background_lock:
            self._last_monitoring_at = None
            self._last_monitoring_errors = ()
        self._coverage_cycle_superseded_epoch_id = superseded_epoch_id
        self._coverage_cycle_superseded_market_data_as_of = superseded_market_data_as_of

    def _record_cycle_errors(
        self,
        errors: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    ) -> None:
        for error in errors:
            normalized = {
                str(key): (
                    value if isinstance(value, (str, bool, int, float)) else str(value)
                )
                for key, value in error.items()
                if value is not None
            }
            self._coverage_cycle_errors[self._error_identity(error)] = normalized

    def _record_cycle_exclusions(
        self,
        exclusions: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    ) -> None:
        for exclusion in exclusions:
            code = exclusion.get("code")
            if not isinstance(code, str) or not code:
                raise ValueError("coverage exclusion requires code")
            self._coverage_cycle_exclusions[code] = copy.deepcopy(dict(exclusion))

    def _can_retain_complete_snapshot(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Return whether ``payload`` is an independently valid last-good page.

        Refresh diagnostics are operational facts, not a replacement decision
        snapshot.  Once a complete epoch has been atomically published, a
        gateway-wide outage must leave that page and its cache intact while the
        background health path reports the failure and retries.
        """

        manifest = payload.get("coverage_manifest")
        audit = payload.get("scan_audit")
        quality = payload.get("data_quality")
        return bool(
            _cache_is_valid(payload, self._config, self._decision_core_id)
            and payload.get("available") is True
            and isinstance(manifest, Mapping)
            and manifest.get("complete") is True
            and isinstance(audit, Mapping)
            and audit.get("coverage_cycle_complete") is True
            and self._pending_symbol_count(payload) == 0
            and isinstance(quality, Mapping)
            and quality.get("complete") is True
        )

    def refresh_now(
        self,
        *,
        copy_result: bool = True,
        priority_only: bool = False,
    ) -> dict[str, object]:
        def result(value: dict[str, object]) -> dict[str, object]:
            return copy.deepcopy(value) if copy_result else value

        if not self._scan_lock.acquire(blocking=False):
            if copy_result:
                return self.snapshot()
            return dict(self._snapshot_reference())
        try:
            previous = self._snapshot_reference()
            try:
                payload = self._perform_incremental_refresh(
                    previous,
                    priority_only=priority_only,
                )
            except Exception as exc:
                payload = copy.deepcopy(dict(previous))
                payload["scan_state"] = "refresh_failed"
                payload["last_batch_state"] = "refresh_failed"
                payload["full_coverage_state"] = (
                    previous.get("full_coverage_state") or "blocked"
                )
                payload["scanned_at"] = normalize_datetime(
                    self._clock(), "clock"
                ).isoformat()
                payload["data_quality"] = {
                    "complete": False,
                    "stale": True,
                    "failure_codes": ["refresh_failed"],
                }
                payload["errors"] = [
                    {
                        "error": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                ]
                self._finalize_snapshot_identity(payload)
                if self._can_retain_complete_snapshot(previous):
                    # Return the diagnostic to the background loop so
                    # readiness records the outage, but do not replace the
                    # independently valid page or its atomic on-disk cache.
                    return result(payload)
                payload_valid = _cache_is_valid(
                    payload,
                    self._config,
                    self._decision_core_id,
                )
                try:
                    self._persist_atomic(
                        payload,
                        cache_valid=payload_valid,
                    )
                except OSError:
                    pass
                with self._state_lock:
                    self._snapshot = payload
                    self._validated_snapshot_sha256 = (
                        str(payload.get("snapshot_content_sha256"))
                        if payload_valid
                        else None
                    )
                return result(payload)
            self._finalize_snapshot_identity(payload)
            if payload.get("snapshot_content_sha256") == previous.get(
                "snapshot_content_sha256"
            ):
                return result(dict(previous))
            payload_valid = _cache_is_valid(
                payload,
                self._config,
                self._decision_core_id,
            )
            self._persist_atomic(
                payload,
                cache_valid=payload_valid,
            )
            with self._state_lock:
                self._snapshot = payload
                self._validated_snapshot_sha256 = (
                    str(payload.get("snapshot_content_sha256"))
                    if payload_valid
                    else None
                )
            notification_context = payload.get("notification_context")
            notification_eligible = bool(
                isinstance(notification_context, Mapping)
                and notification_context.get("realtime_eligible") is True
            )
            if self._notifier is not None and notification_eligible:
                self._notifier.dispatch_changes(previous, payload)
            return result(payload)
        finally:
            self._scan_lock.release()

    def _perform_incremental_refresh(
        self,
        previous: Mapping[str, object],
        *,
        priority_only: bool = False,
    ) -> dict[str, object]:
        batch_started_perf = time.perf_counter()
        observed_at = normalize_datetime(self._clock(), "clock")
        if priority_only:
            frozen_sector_ready = bool(
                self._coverage_cycle_sector_batch is not None
                and self._coverage_cycle_sector_members is not None
                and self._coverage_cycle_started_at is not None
                and self._coverage_sector_catalog_revision is not None
                and self._coverage_market_data_as_of is not None
                and self._coverage_epoch_id
            )
            if frozen_sector_ready and not self._coverage_cycle_sector_runtime_hydrated:
                restore_members = getattr(
                    self._sector_catalog,
                    "restore_authenticated_sector_members",
                    None,
                )
                if callable(restore_members):
                    try:
                        restore_members(
                            members=dict(self._coverage_cycle_sector_members or {}),
                            as_of=self._coverage_cycle_started_at,
                            catalog_revision=(
                                self._coverage_sector_catalog_revision or ""
                            ),
                        )
                    except Exception:
                        # The safe monitor below publishes the exact per-symbol
                        # native failures without replacing the authenticated
                        # archival snapshot.  Leave this flag false so the next
                        # minute retries process-local routing restoration.
                        pass
                    else:
                        self._coverage_cycle_sector_runtime_hydrated = True
            self._run_priority_monitor_safely(
                previous=previous,
                observed_at=observed_at,
                frozen_sector_batch=(
                    self._coverage_cycle_sector_batch if frozen_sector_ready else None
                ),
                frozen_sector_members=(
                    self._coverage_cycle_sector_members if frozen_sector_ready else None
                ),
                frozen_sector_as_of=(
                    self._coverage_market_data_as_of if frozen_sector_ready else None
                ),
                frozen_coverage_epoch_id=(
                    self._coverage_epoch_id if frozen_sector_ready else None
                ),
            )
            # The live lane persists its own compact, authenticated state.
            # Returning the exact archival snapshot guarantees that a minute
            # observation cannot consume, reorder, or republish coverage work.
            return dict(previous)
        cycle_started = not self._pending_frequencies
        superseded_epoch_id: str | None = None
        superseded_market_data_as_of: datetime | None = None
        preclose_epoch_refresh_required = False
        current_cutoff = self._coverage_market_data_as_of
        if not cycle_started and current_cutoff is not None:
            cutoff_local = current_cutoff.astimezone(CN)
            market_close = datetime.combine(
                cutoff_local.date(),
                datetime_time(15),
                tzinfo=CN,
            )
            # A multi-batch intraday cycle can still be draining after the
            # market has closed.  Finishing it later does not turn its 14:35
            # facts into a 15:00 end-of-session snapshot.  Supersede that
            # pending epoch before the next batch so the daily forward sample
            # can only come from a fresh close-complete coverage plan.
            if cutoff_local < market_close <= observed_at.astimezone(CN):
                superseded_epoch_id = self._coverage_epoch_id
                superseded_market_data_as_of = current_cutoff
                preclose_epoch_refresh_required = True
                cycle_started = True
        cached_sector_batch = self._coverage_cycle_sector_batch
        cached_sector_members = self._coverage_cycle_sector_members
        same_epoch_backoff_retry = bool(self._backoff_frequencies)
        previous_manifest = previous.get("coverage_manifest")
        frozen_complete_epoch = bool(
            isinstance(previous_manifest, Mapping)
            and previous_manifest.get("complete") is True
        )
        complete_epoch_probe_required = bool(
            cycle_started
            and frozen_complete_epoch
            and _coverage_sector_probe_required(previous, observed_at)
        )
        reuse_cycle_sectors = (
            not preclose_epoch_refresh_required
            and (
                not cycle_started
                or same_epoch_backoff_retry
                or (frozen_complete_epoch and not complete_epoch_probe_required)
            )
            and self._coverage_cycle_started_at is not None
            and cached_sector_batch is not None
            and cached_sector_members is not None
        )
        if reuse_cycle_sectors:
            # A coverage cycle is one market-data snapshot. Re-reading all sector
            # composites for every stock batch both wasted tens of seconds and
            # allowed later batches to use a different sector state/as-of time.
            #
            # A completed epoch is frozen both in the process that produced it
            # and after an app restart. Requiring the cache to have come from
            # restart restoration made the live process recompute sectors on
            # every idle refresh after coverage completed. That could change
            # structural point identities while retaining the already-built
            # symbol documents, mixing two evidence states in one snapshot.
            # The deliberate pre-open/post-close probes above are the only
            # times a complete epoch may query a new sector catalog revision.
            #
            # The native process proxy keeps member routing in memory. After
            # an app restart the authenticated screening snapshot can prime
            # that routing directly; generic gateways fall back to their own
            # frozen-time cache or assessment call. The frozen assessment
            # remains authoritative when membership is exact.
            sector_started_perf = time.perf_counter()
            hydrated_batch: SectorAssessmentBatch | None = None
            if self._coverage_cycle_sector_runtime_hydrated:
                runtime_sector_members = dict(cached_sector_members)
            else:
                restore_members = getattr(
                    self._sector_catalog,
                    "restore_authenticated_sector_members",
                    None,
                )
                if callable(restore_members):
                    restore_members(
                        members=dict(cached_sector_members),
                        as_of=self._coverage_cycle_started_at,
                        catalog_revision=(self._coverage_sector_catalog_revision or ""),
                    )
                    runtime_sector_members = dict(self._sector_catalog.members())
                else:
                    try:
                        runtime_sector_members = dict(self._sector_catalog.members())
                    except RuntimeError:
                        # Hydrate a generic transport at the frozen coverage
                        # time, not at the restart wall clock.  Its sector
                        # cache is keyed by causal 5m epoch; using
                        # ``observed_at`` would reject the exact cache that
                        # produced this coverage epoch.
                        hydrated_batch = self._sector_catalog.native_sector_assessments(
                            as_of=self._coverage_cycle_started_at
                        )
                        runtime_sector_members = dict(self._sector_catalog.members())
                self._coverage_cycle_sector_runtime_hydrated = True
            if runtime_sector_members == dict(cached_sector_members):
                as_of = self._coverage_cycle_started_at
                sector_batch = cached_sector_batch
            else:
                # A real membership change is a new universe, not a restorable
                # same-epoch transport detail.  Continue with the freshly
                # authenticated batch so the ordinary epoch-replacement gate
                # replays the complete current scope.
                reuse_cycle_sectors = False
                as_of = observed_at
                sector_batch = (
                    hydrated_batch
                    if hydrated_batch is not None
                    else self._sector_catalog.native_sector_assessments(as_of=as_of)
                )
            sector_scan_duration_ms = round(
                (time.perf_counter() - sector_started_perf) * 1000,
                2,
            )
        else:
            as_of = (
                self._coverage_cycle_started_at
                if not cycle_started and self._coverage_cycle_started_at is not None
                else observed_at
            )
            sector_started_perf = time.perf_counter()
            sector_batch = self._sector_catalog.native_sector_assessments(as_of=as_of)
            sector_scan_duration_ms = round(
                (time.perf_counter() - sector_started_perf) * 1000,
                2,
            )
        # One complete coverage batch can legitimately span many minutes.
        # Record progress between native QMT calls so the heartbeat detects a
        # single stuck call instead of timing the whole healthy batch.
        self._record_background_heartbeat()
        sector_ratio = sector_batch.completion_ratio
        sector_resolution_ratio = sector_batch.resolution_ratio
        sector_audit: dict[str, object] = {
            "sector_discovered_count": sector_batch.discovered_count,
            "sector_completed_count": sector_batch.completed_count,
            "sector_excluded_count": len(sector_batch.exclusions),
            "sector_failed_count": len(sector_batch.errors),
            "sector_resolved_count": (
                sector_batch.completed_count + len(sector_batch.exclusions)
            ),
            "sector_completion_ratio": str(sector_ratio),
            "sector_resolution_ratio": str(sector_resolution_ratio),
            "sector_failure_counts": dict(sector_batch.failure_counts),
            "sector_exclusion_counts": dict(sector_batch.exclusion_counts),
        }
        sector_errors = [_sector_failure_document(item) for item in sector_batch.errors]
        sector_exclusions = [
            _sector_exclusion_document(item) for item in sector_batch.exclusions
        ]
        if sector_ratio < self._config.min_scan_completion_ratio:
            failed = copy.deepcopy(dict(previous))
            failed["scan_state"] = "incomplete_not_published"
            failed["last_batch_state"] = "incomplete_not_published"
            failed["full_coverage_state"] = "in_progress"
            previous_audit = failed.get("scan_audit")
            scan_audit = (
                dict(previous_audit) if isinstance(previous_audit, Mapping) else {}
            )
            scan_audit.update(sector_audit)
            scan_audit.update(
                {
                    "batch_duration_ms": round(
                        (time.perf_counter() - batch_started_perf) * 1000,
                        2,
                    ),
                    "sector_scan_duration_ms": sector_scan_duration_ms,
                    "stock_scan_duration_ms": 0,
                    "stock_worker_count": self._config.stock_worker_count,
                }
            )
            failed["scan_audit"] = scan_audit
            failed["data_quality"] = {
                "complete": False,
                "stale": True,
                "failure_codes": ["sector_scan_completion_below_threshold"],
            }
            failed["errors"] = sector_errors
            failed["sector_exclusions"] = sector_exclusions
            failed["sector_coverage_contract_id"] = SECTOR_COVERAGE_CONTRACT_ID
            return failed

        failed_sector_ids = {
            item.sector_id for item in (*sector_batch.errors, *sector_batch.exclusions)
        }
        assessments = tuple(
            assessment
            for assessment in sector_batch.assessments
            if assessment.sector_id not in failed_sector_ids
        )
        ranked = rank_sectors(assessments)
        # Every structurally eligible QMT sector contributes its members. Ranking
        # remains an explanation/order field; it is not a top-N cutoff.
        selected = ranked
        selected_by_id = {row.assessment.sector_id: row.assessment for row in selected}
        if reuse_cycle_sectors and cached_sector_members is not None:
            all_members = dict(cached_sector_members)
        else:
            all_members = dict(self._sector_catalog.members())
            self._coverage_cycle_sector_runtime_hydrated = True
        self._record_background_heartbeat()
        sector_members = {
            sector_id: tuple(all_members.get(sector_id, ()))
            for sector_id in selected_by_id
        }
        # A symbol's primary sector is the first (best-ranked) eligible sector
        # containing it.  GICS3 membership should normally be unique, while
        # this deterministic fallback also prevents a lower-ranked supportive
        # duplicate from contradicting the sector document shown on the page.
        selected_sector_by_code: dict[str, SectorAssessment] = {}
        for ranked_sector in selected:
            for member in sorted(
                sector_members.get(ranked_sector.assessment.sector_id, ())
            ):
                selected_sector_by_code.setdefault(
                    member,
                    ranked_sector.assessment,
                )
        ranked_scan_codes = tuple(
            dict.fromkeys(
                member
                for ranked_sector in selected
                for member in sorted(
                    sector_members.get(ranked_sector.assessment.sector_id, ())
                )
            )
        )
        watchlist, rejected_watchlist = _validated_monitor_instrument_scope(
            self._market_data.active_watchlist_scope(),
            "active_watchlist_scope",
        )
        holdings, rejected_holdings = _validated_monitor_instrument_scope(
            self._market_data.holdings_scope(),
            "holdings_scope",
        )
        selected_member_codes = set(selected_sector_by_code)
        triggered_member_codes = {
            code
            for code, assessment in selected_sector_by_code.items()
            if assessment.regime == "supportive"
        }
        raw_previous_active_codes = {
            str(row.get("code"))
            for row in previous.get("signals", ())
            if isinstance(row, Mapping)
            and isinstance(row.get("code"), str)
            and row.get("lifecycle_stage") not in {"closed", "invalidated"}
        }
        previous_nonmember_codes = tuple(
            sorted(raw_previous_active_codes.difference(selected_member_codes))
        )
        qualified_previous_nonmembers = self._market_data.tradable_instrument_codes(
            previous_nonmember_codes
        )
        rejected_previous_nonmembers = tuple(
            code
            for code in previous_nonmember_codes
            if code not in qualified_previous_nonmembers
        )
        rejected_monitor_codes = tuple(
            sorted(
                set(rejected_watchlist)
                | set(rejected_holdings)
                | set(rejected_previous_nonmembers)
            )
        )
        monitor_instrument_types = (
            {}
            if not rejected_monitor_codes
            else self._market_data.screening_instrument_types(rejected_monitor_codes)
        )
        monitor_instrument_exclusions = _monitor_instrument_exclusion_documents(
            ("ACTIVE_WATCHLIST_MONITOR", rejected_watchlist),
            ("HOLDING_MONITOR", rejected_holdings),
            ("PREVIOUS_SIGNAL_MONITOR", rejected_previous_nonmembers),
            instrument_types=monitor_instrument_types,
        )
        self._record_background_heartbeat()
        previous_active_codes = tuple(
            sorted(
                raw_previous_active_codes.intersection(selected_member_codes)
                | set(qualified_previous_nonmembers)
            )
        )
        priority_codes = tuple(
            sorted(set((*watchlist, *holdings, *previous_active_codes)))
        )
        watchlist_codes = set(watchlist)
        holding_codes = set(holdings)
        previous_signal_codes = set(previous_active_codes)

        def selection_sources_for(code: str) -> tuple[str, ...]:
            sources: list[str] = []
            if code in triggered_member_codes:
                sources.append("QMT_SECTOR_TRIGGER")
            elif code in selected_member_codes:
                sources.append("QMT_SECTOR_ELIGIBLE_SCOPE")
            if code in watchlist_codes:
                sources.append("ACTIVE_WATCHLIST_MONITOR")
            if code in holding_codes:
                sources.append("HOLDING_MONITOR")
            if code in previous_signal_codes and not sources:
                sources.append("PREVIOUS_SIGNAL_MONITOR")
            if not sources:
                sources.append("INCREMENTAL_SCAN_SCOPE")
            return tuple(sources)

        market_data_as_of = self._sector_market_data_as_of(
            sector_batch.assessments,
            self._cached_as_of(previous) or as_of,
        )
        if preclose_epoch_refresh_required:
            required_close = datetime.combine(
                (superseded_market_data_as_of or market_data_as_of)
                .astimezone(CN)
                .date(),
                datetime_time(15),
                tzinfo=CN,
            )
            if market_data_as_of.astimezone(CN) < required_close:
                blocked = copy.deepcopy(dict(previous))
                blocked["scan_state"] = "postclose_market_data_incomplete"
                blocked["last_batch_state"] = "postclose_market_data_incomplete"
                blocked["full_coverage_state"] = "in_progress"
                blocked["scanned_at"] = observed_at.isoformat()
                previous_audit = blocked.get("scan_audit")
                scan_audit = (
                    dict(previous_audit) if isinstance(previous_audit, Mapping) else {}
                )
                scan_audit.update(sector_audit)
                scan_audit.update(
                    {
                        "postclose_market_data_refresh_required": True,
                        "required_market_data_as_of": required_close.isoformat(),
                        "observed_market_data_as_of": (market_data_as_of.isoformat()),
                    }
                )
                blocked["scan_audit"] = scan_audit
                blocked["data_quality"] = {
                    "complete": False,
                    "stale": True,
                    "failure_codes": ["postclose_market_data_incomplete"],
                }
                blocked["errors"] = sector_errors + [
                    {
                        "error_type": "postclose_market_data_incomplete",
                        "reason": (
                            f"required {required_close.isoformat()}, got "
                            f"{market_data_as_of.isoformat()}"
                        ),
                    }
                ]
                return blocked

            # Only now is it safe to replace the old pending plan.  The fresh
            # sector snapshot proves that its own market-data cutoff includes
            # the close; clearing before this proof would lose resumable work
            # whenever QMT had not finished updating its local history.
            self._pending_frequencies.clear()
            self._backoff_frequencies.clear()
            self._deferred_frequencies.clear()
            self._coverage_cycle_sector_batch = None
            self._coverage_cycle_sector_members = None
            self._coverage_cycle_sector_restored = False
            self._coverage_cycle_sector_runtime_hydrated = False

        effective_superseded_epoch_id = (
            superseded_epoch_id
            if superseded_epoch_id is not None
            else (
                self._coverage_cycle_superseded_epoch_id if not cycle_started else None
            )
        )
        effective_superseded_cutoff = (
            superseded_market_data_as_of
            if superseded_market_data_as_of is not None
            else (
                self._coverage_cycle_superseded_market_data_as_of
                if not cycle_started
                else None
            )
        )
        if effective_superseded_epoch_id is not None:
            sector_audit.update(
                {
                    "preclose_epoch_superseded": True,
                    "superseded_coverage_epoch_id": (effective_superseded_epoch_id),
                    "superseded_market_data_as_of": (
                        None
                        if effective_superseded_cutoff is None
                        else effective_superseded_cutoff.isoformat()
                    ),
                }
            )
        universe_revision = sha256_json(
            {
                "schema": "chanlun-screening-universe",
                "sector_members": {
                    sector_id: tuple(members)
                    for sector_id, members in sorted(sector_members.items())
                },
                "watchlist": watchlist,
                "holdings": holdings,
                "decision_core_id": self._decision_core_id,
            }
        )
        sector_catalog_revision = sector_batch.catalog_revision or sha256_json(
            {
                "schema": "chanlun-live-sector-membership",
                "members": {
                    sector_id: tuple(members)
                    for sector_id, members in sorted(all_members.items())
                },
            }
        )
        sector_strength_evidence_revision = (
            None
            if sector_batch.strength_evidence is None
            else sector_batch.strength_evidence.evidence_revision
        )
        expected_epoch_id = screening_coverage_epoch_id(
            market_data_as_of=market_data_as_of,
            universe_revision=universe_revision,
            sector_catalog_revision=sector_catalog_revision,
            sector_strength_evidence_revision=(sector_strength_evidence_revision),
            decision_core_id=self._decision_core_id,
            screening_policy_id=_screening_policy_id(),
            structure_contract_id=self._config.structure_contract_id,
            parameter_set_id=self._config.parameter_set_id,
        )
        same_coverage_epoch = (
            self._coverage_epoch_id == expected_epoch_id
            and self._coverage_universe_revision == universe_revision
            and self._coverage_sector_catalog_revision == sector_catalog_revision
            and self._coverage_sector_strength_evidence_revision
            == sector_strength_evidence_revision
            and self._coverage_market_data_as_of == market_data_as_of
        )
        monitoring_only_refresh = bool(
            cycle_started
            and same_coverage_epoch
            and not self._backoff_frequencies
            and isinstance(previous_manifest, Mapping)
            and previous_manifest.get("complete") is True
            and not preclose_epoch_refresh_required
        )
        if (
            not cycle_started
            and self._coverage_universe_revision is not None
            and (
                universe_revision != self._coverage_universe_revision
                or sector_catalog_revision != self._coverage_sector_catalog_revision
                or sector_strength_evidence_revision
                != self._coverage_sector_strength_evidence_revision
            )
        ):
            self._pending_frequencies.clear()
            self._backoff_frequencies.clear()
            self._deferred_frequencies.clear()
            invalidated = copy.deepcopy(dict(previous))
            invalidated["scan_state"] = "coverage_epoch_invalidated"
            invalidated["last_batch_state"] = "coverage_epoch_invalidated"
            invalidated["full_coverage_state"] = "invalidated"
            invalidated["scanned_at"] = normalize_datetime(
                self._clock(), "clock"
            ).isoformat()
            invalidated["data_quality"] = {
                "complete": False,
                "stale": True,
                "failure_codes": [
                    (
                        "coverage_universe_revision_changed"
                        if universe_revision != self._coverage_universe_revision
                        else (
                            "coverage_sector_catalog_revision_changed"
                            if sector_catalog_revision
                            != self._coverage_sector_catalog_revision
                            else "coverage_sector_strength_evidence_changed"
                        )
                    )
                ],
            }
            invalidated["coverage_manifest"] = {
                **self._coverage_manifest(complete=True),
                "invalidated": True,
                "replacement_required": True,
            }
            return invalidated
        if same_coverage_epoch and self._coverage_cycle_sector_batch is None:
            self._coverage_cycle_sector_batch = sector_batch
            self._coverage_cycle_sector_members = dict(all_members)
            self._coverage_cycle_sector_restored = False
        replacing_coverage_epoch = False
        if cycle_started:
            replacing_coverage_epoch = (
                preclose_epoch_refresh_required or not same_coverage_epoch
            )
            if replacing_coverage_epoch:
                self._begin_coverage_cycle(
                    as_of=as_of,
                    market_data_as_of=market_data_as_of,
                    universe_revision=universe_revision,
                    sector_catalog_revision=sector_catalog_revision,
                    sector_strength_evidence_revision=(
                        sector_strength_evidence_revision
                    ),
                    started_perf=batch_started_perf,
                    superseded_epoch_id=superseded_epoch_id,
                    superseded_market_data_as_of=(superseded_market_data_as_of),
                )
            self._coverage_cycle_sector_batch = sector_batch
            self._coverage_cycle_sector_members = dict(all_members)
            if not reuse_cycle_sectors:
                self._coverage_cycle_sector_restored = False
            planner_since = None if replacing_coverage_epoch else self._last_as_of
            planner_cursor = (
                ScanCursor.empty() if replacing_coverage_epoch else self._cursor
            )
            plan = self._scan_planner(
                changed_bars=self._market_data.changed_bars(planner_since),
                sector_members=sector_members,
                known_sector_ids=tuple(
                    sorted(assessment.sector_id for assessment in assessments)
                ),
                active_watchlist=priority_codes,
                holdings=holdings,
                previous=planner_cursor,
                structure_contract_id=self._config.structure_contract_id,
                parameter_set_id=self._config.parameter_set_id,
            )
            if replacing_coverage_epoch:
                # A coverage identity change clears the completed ledger.  An
                # incremental planner may legitimately return only changed bars
                # and monitored symbols, but that subset cannot authenticate a
                # brand-new full-sector coverage manifest.  Re-enter every
                # member of every currently eligible QMT sector, plus explicit
                # monitors, with the same frozen d/30m/5m/1m structure inputs.
                # This is coverage repair, not a parameter or signal change.
                full_scope = set(priority_codes).union(plan.symbols)
                full_scope.update(
                    member for members in sector_members.values() for member in members
                )
                plan = ScanPlan(
                    sectors=tuple(sorted(set(plan.sectors).union(sector_members))),
                    symbols=tuple(sorted(full_scope)),
                    symbol_frequencies=tuple(
                        (code, SCREENING_STRUCTURE_FREQUENCIES)
                        for code in sorted(full_scope)
                    ),
                    full_market_history_scan=plan.full_market_history_scan,
                    background_full_refresh_required=True,
                )
            # A planner may surface a genuinely new code even when the sector
            # composite cutoff and catalog revision are unchanged.  Such a code
            # is discovery work, not same-epoch monitoring, and must enter the
            # ordinary resumable coverage queue.
            if monitoring_only_refresh and any(
                code not in self._coverage_cycle_discovered_codes
                for code in plan.symbols
            ):
                monitoring_only_refresh = False
            if not monitoring_only_refresh:
                # Retry queues belong to the universe that produced them.  A
                # new market-data epoch may retry a still-current member, but
                # a catalog/watchlist change must never resurrect a removed or
                # delisted symbol.  ``plan.symbols`` is included for custom
                # incremental discoveries that are valid even when they are
                # not sector members or explicit monitors.
                retry_scope = selected_member_codes.union(
                    priority_codes,
                    plan.symbols,
                )
                backoff = self._backoff_frequencies
                self._backoff_frequencies = {}
                if replacing_coverage_epoch:
                    discarded = set(backoff).difference(retry_scope)
                    self._coverage_cycle_discarded_retry_codes.update(discarded)
                    backoff = {
                        code: frequencies
                        for code, frequencies in backoff.items()
                        if code in retry_scope
                    }
                for code, frequencies in backoff.items():
                    self._pending_frequencies.setdefault(code, set()).update(
                        frequencies
                    )
                deferred: dict[str, set[str]] = {}
                if replacing_coverage_epoch:
                    deferred = self._deferred_frequencies
                    self._deferred_frequencies = {}
                    discarded = set(deferred).difference(retry_scope)
                    self._coverage_cycle_discarded_retry_codes.update(discarded)
                    deferred = {
                        code: frequencies
                        for code, frequencies in deferred.items()
                        if code in retry_scope
                    }
                    for code, frequencies in deferred.items():
                        self._pending_frequencies.setdefault(code, set()).update(
                            frequencies
                        )
                self._coverage_cycle_discovered_codes.update(plan.symbols)
                self._coverage_cycle_discovered_codes.update(backoff)
                self._coverage_cycle_discovered_codes.update(deferred)
                self._coverage_cycle_full_market_history_scan = (
                    plan.full_market_history_scan
                )
                self._coverage_cycle_background_refresh_required = (
                    plan.background_full_refresh_required
                )
        else:
            # A coverage plan is immutable while its pending queue drains. Replanning
            # every batch re-added the active watchlist and made a cycle impossible
            # to finish whenever monitored symbols existed.
            plan = ScanPlan(
                sectors=(),
                symbols=(),
                symbol_frequencies=(),
                full_market_history_scan=(
                    self._coverage_cycle_full_market_history_scan
                ),
                background_full_refresh_required=(
                    self._coverage_cycle_background_refresh_required
                ),
            )
        if not monitoring_only_refresh:
            self._record_cycle_errors(sector_errors)
        if monitoring_only_refresh:
            symbols, batch_frequencies = self._take_monitoring_batch(
                plan,
                priority_codes=priority_codes,
            )
        else:
            symbols, batch_frequencies = self._take_scan_batch(
                plan,
                priority_codes=priority_codes,
                scan_order_codes=ranked_scan_codes,
            )
        if self._priority_monitor_due(observed_at):
            # The coverage epoch stays frozen for causal completeness, while
            # this independent lane re-observes owned/watchlisted/active names
            # and a rotating supportive-sector sample on current completed
            # minute bars.  It also stays active after coverage completes,
            # because the ordinary multi-thousand-symbol monitor cursor cannot
            # provide a one-minute SLA for owned risk.  Excluding this batch
            # prevents duplicate native QMT work without changing either
            # lane's decision semantics.
            self._run_priority_monitor_safely(
                previous=previous,
                observed_at=observed_at,
                excluded_codes=frozenset(symbols),
                frozen_sector_batch=sector_batch,
                frozen_sector_members=all_members,
                frozen_sector_as_of=(
                    self._coverage_market_data_as_of or market_data_as_of
                ),
                frozen_coverage_epoch_id=self._coverage_epoch_id,
            )
        previous_signal_rows = (
            () if replacing_coverage_epoch else previous.get("signals", ())
        )
        previous_signals = {
            str(row.get("signal_id")): row
            for row in previous_signal_rows
            if isinstance(row, Mapping) and isinstance(row.get("signal_id"), str)
        }
        signals: list[dict[str, object]] = []
        errors: list[dict[str, str]] = list(sector_errors)
        exclusions: list[dict[str, object]] = []
        completed = 0
        completed_codes: set[str] = set()
        excluded_codes: set[str] = set()
        stock_started_perf = time.perf_counter()
        sector_by_code: dict[str, SectorAssessment] = dict(selected_sector_by_code)
        selected_assessments = tuple(row.assessment for row in selected)
        selected_ids = {assessment.sector_id for assessment in selected_assessments}
        assessment_order = selected_assessments + tuple(
            sorted(
                (
                    assessment
                    for assessment in assessments
                    if assessment.sector_id not in selected_ids
                ),
                key=lambda assessment: (
                    not assessment.eligible,
                    -assessment.rank_score,
                    assessment.sector_id,
                ),
            )
        )
        for assessment in assessment_order:
            for member in all_members.get(assessment.sector_id, ()):
                sector_by_code.setdefault(member, assessment)

        def evaluate_stock(code: str):
            sector = sector_by_code.get(
                code,
                SectorAssessment(
                    sector_id="unclassified",
                    sector_name="未匹配 QMT GICS3 行业",
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_membership_missing",),
                ),
            )
            try:
                selection_sources = selection_sources_for(code)
                bundle = self._structure_bundle_with_causal_risk(
                    code,
                    as_of=as_of,
                    sector=sector,
                    frequencies=batch_frequencies.get(code, ()),
                    risk_evidence_cutoff=(
                        self._coverage_market_data_as_of or market_data_as_of
                    ),
                )
                bundle = replace(
                    bundle,
                    selection_sources=selection_sources,
                )
                if code in holding_codes and bundle.physical_timeframe_level_zero:
                    # The active human-paper path uses independent physical
                    # timeframe level-zero structures.  ``formal / 0`` is the
                    # only legal identity in that contract, so attaching it
                    # proves position existence without guessing whether the
                    # current 5m sell clue is a strategic 30m exit or a
                    # tactical short-diff.  That role remains an explicit
                    # human judgement in the review adapter.
                    bundle = replace(
                        bundle,
                        held_tower="formal",
                        held_level=0,
                    )
                age = as_of - bundle.as_of
                if age < timedelta(0) or age > timedelta(
                    seconds=self._config.max_structure_age_seconds
                ):
                    raise ValueError("structure_bundle_stale")
                evaluated = self._engine.evaluate_symbol(bundle)
                name_provider = getattr(self._market_data, "symbol_name", None)
                symbol_name = (
                    name_provider(code)
                    if evaluated and callable(name_provider)
                    else None
                )
                return code, bundle, evaluated, symbol_name, None
            except Exception as exc:
                return code, None, (), None, exc

        def consume_stock_result(result) -> None:
            nonlocal completed
            code, bundle, evaluated, symbol_name, exc = result
            if exc is None:
                assert isinstance(bundle, SymbolStructureBundle)
                for item in evaluated:
                    previous_stage = None
                    previous_row = previous_signals.get(item.lifecycle.signal_id)
                    if isinstance(previous_row, Mapping):
                        stage = lifecycle_stage_from_signal(previous_row)
                        previous_stage = stage if isinstance(stage, str) else None
                    signals.append(
                        _signal_document(
                            item,
                            previous_stage=previous_stage,
                            name=symbol_name,
                            decision_core_id=self._decision_core_id,
                            selection_sources=bundle.selection_sources,
                            higher_timeframe_gates=(bundle.higher_timeframe_gates),
                        )
                    )
                completed += 1
                completed_codes.add(code)
            else:
                error = _stock_analysis_error_document(code, exc)
                if not monitoring_only_refresh and _is_coverage_exclusion(error):
                    exclusions.append(_stock_analysis_exclusion_document(error))
                    excluded_codes.add(code)
                else:
                    errors.append(error)
            self._record_background_heartbeat()

        worker_count = min(self._config.stock_worker_count, max(1, len(symbols)))
        if worker_count == 1:
            for code in symbols:
                self._record_background_heartbeat()
                consume_stock_result(evaluate_stock(code))
        else:
            # Market-data proxies assign each symbol deterministically to one
            # isolated QMT worker.  Threads here only coordinate authenticated
            # IPC; the CPU-heavy structure calculations run in separate Python
            # processes and therefore use physical cores rather than contending
            # on the GIL. `executor.map` preserves symbol order so signal and
            # rejection documents remain byte-for-byte deterministic.
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="TradingScreeningStock",
            ) as executor:
                for result in executor.map(evaluate_stock, symbols):
                    consume_stock_result(result)
        stock_scan_duration_ms = round(
            (time.perf_counter() - stock_started_perf) * 1000,
            2,
        )
        failed_codes = tuple(
            code
            for code in symbols
            if code not in completed_codes and code not in excluded_codes
        )
        if not monitoring_only_refresh:
            self._coverage_cycle_batch_count += 1
            self._coverage_cycle_completed_codes.update(completed_codes)
            self._coverage_cycle_excluded_codes.update(excluded_codes)
            self._coverage_cycle_failed_codes.update(failed_codes)
            for code in completed_codes:
                self._coverage_cycle_excluded_codes.discard(code)
                self._coverage_cycle_failed_codes.discard(code)
                self._coverage_cycle_exclusions.pop(code, None)
                self._coverage_cycle_errors.pop(
                    f"stock_analysis_error:{code}",
                    None,
                )
            for code in excluded_codes:
                self._coverage_cycle_failed_codes.discard(code)
                self._coverage_cycle_errors.pop(
                    f"stock_analysis_error:{code}",
                    None,
                )
            self._record_cycle_exclusions(exclusions)
        stock_batch_errors = errors[len(sector_errors) :]
        if monitoring_only_refresh:
            # The full-universe coverage ledger is already complete.  A failed
            # same-cutoff re-observation must retain the last-good signal and be
            # visible only as operational health; adding it to ``errors`` would
            # make stock error codes disagree with manifest.failed_codes and no
            # later successful monitor could recover the epoch.
            with self._background_lock:
                self._last_monitoring_at = as_of
                self._last_monitoring_errors = tuple(copy.deepcopy(errors))
            # A completed coverage publication is immutable.  The sector probe
            # above may legitimately produce a different structural point
            # identity for the same frozen market cutoff, while this bounded
            # monitor only re-evaluates a subset of symbols.  Rebuilding the
            # page here would therefore combine retained old signals with new
            # sector documents.  The priority monitor already persists and
            # notifies its current results through its independent state lane;
            # keep the daily preselection snapshot byte-for-byte unchanged.
            return dict(previous)
        else:
            self._record_cycle_errors(stock_batch_errors)
        cycle_errors = list(self._coverage_cycle_errors.values())
        stock_failure_counts: dict[str, int] = {}
        for error in cycle_errors:
            if error.get("error_type") != "stock_analysis_error":
                continue
            reason_code = str(error.get("reason_code") or "STOCK_ANALYSIS_UNCLASSIFIED")
            stock_failure_counts[reason_code] = (
                stock_failure_counts.get(reason_code, 0) + 1
            )
        stock_exclusion_counts: dict[str, int] = {}
        for exclusion in self._coverage_cycle_exclusions.values():
            reason_code = str(exclusion["reason_code"])
            stock_exclusion_counts[reason_code] = (
                stock_exclusion_counts.get(reason_code, 0) + 1
            )
        # Classify failures before the batch completion gate.  Requeueing an
        # entire low-completion batch made a sorted cluster of deterministic
        # market-data rejections monopolize every refresh, so untouched valid
        # symbols later in the frozen coverage plan were never visited.
        # Deterministic failures are terminal for this market-data epoch;
        # transport failures use the paced backoff queue.  Both remain fully
        # represented in the immutable manifest and error ledger.
        if not monitoring_only_refresh:
            stock_errors_by_code = {
                str(error["code"]): error
                for error in cycle_errors
                if error.get("error_type") == "stock_analysis_error"
                and isinstance(error.get("code"), str)
            }
            backoff_codes = tuple(
                code
                for code in failed_codes
                if stock_errors_by_code.get(code, {}).get("retry_policy")
                == "NEXT_REFRESH_AFTER_BACKOFF"
            )
            backoff_code_set = set(backoff_codes)
            next_epoch_codes = tuple(
                code for code in failed_codes if code not in backoff_code_set
            )
            self._defer_symbols_to_backoff_refresh(
                backoff_codes,
                batch_frequencies,
            )
            self._defer_symbols_to_next_cycle(
                next_epoch_codes,
                batch_frequencies,
            )
            self._defer_symbols_to_next_cycle(
                tuple(sorted(excluded_codes)),
                batch_frequencies,
            )
        planned_count = len(symbols)
        completion = (
            Decimal("1")
            if planned_count == 0
            else Decimal(completed) / Decimal(planned_count)
        )
        batch_resolution = (
            Decimal("1")
            if planned_count == 0
            else Decimal(completed + len(excluded_codes)) / Decimal(planned_count)
        )
        coverage_attempted_count = len(
            self._coverage_cycle_completed_codes
            | self._coverage_cycle_excluded_codes
            | self._coverage_cycle_failed_codes
        )
        coverage_completion = (
            Decimal("0")
            if coverage_attempted_count == 0
            else Decimal(len(self._coverage_cycle_completed_codes))
            / Decimal(coverage_attempted_count)
        )
        coverage_resolved_count = len(
            self._coverage_cycle_completed_codes | self._coverage_cycle_excluded_codes
        )
        coverage_resolution = (
            Decimal("0")
            if not self._coverage_cycle_discovered_codes
            else Decimal(coverage_resolved_count)
            / Decimal(len(self._coverage_cycle_discovered_codes))
        )
        previous_scan_audit = previous.get("scan_audit")
        if monitoring_only_refresh and isinstance(previous_scan_audit, Mapping):
            coverage_cycle_elapsed_ms = float(
                previous_scan_audit.get("coverage_cycle_elapsed_ms") or 0
            )
            coverage_finalized_count = int(
                previous_scan_audit.get(
                    "coverage_cycle_finalized_symbol_count",
                    len(self._coverage_cycle_discovered_codes),
                )
            )
            coverage_progress = Decimal(
                str(previous_scan_audit.get("coverage_cycle_progress_ratio") or "1")
            )
            coverage_throughput = previous_scan_audit.get(
                "coverage_cycle_throughput_symbols_per_minute"
            )
            runtime_finalized_count = int(
                previous_scan_audit.get(
                    "coverage_cycle_runtime_finalized_symbol_count", 0
                )
            )
            coverage_eta_seconds = previous_scan_audit.get(
                "coverage_cycle_estimated_remaining_seconds", 0
            )
        else:
            coverage_started_perf = self._coverage_cycle_started_perf
            coverage_cycle_elapsed_ms = round(
                0
                if coverage_started_perf is None
                else (time.perf_counter() - coverage_started_perf) * 1000,
                2,
            )
            remaining_codes = (
                set(self._pending_frequencies) | set(self._backoff_frequencies)
            ).intersection(self._coverage_cycle_discovered_codes)
            discovered_count = len(self._coverage_cycle_discovered_codes)
            coverage_finalized_count = max(
                0,
                discovered_count - len(remaining_codes),
            )
            coverage_progress = (
                Decimal("0")
                if discovered_count == 0
                else Decimal(coverage_finalized_count) / Decimal(discovered_count)
            )
            runtime_finalized_count = max(
                0,
                coverage_finalized_count
                - self._coverage_runtime_baseline_finalized_count,
            )
            elapsed_seconds = coverage_cycle_elapsed_ms / 1000
            coverage_throughput = (
                None
                if elapsed_seconds <= 0 or runtime_finalized_count == 0
                else round(runtime_finalized_count * 60 / elapsed_seconds, 3)
            )
            if not remaining_codes:
                coverage_eta_seconds = 0
            elif coverage_throughput is None or coverage_throughput <= 0:
                coverage_eta_seconds = None
            else:
                coverage_eta_seconds = round(
                    len(remaining_codes) * 60 / coverage_throughput,
                    1,
                )
        cumulative_coverage_publishable = (
            previous.get("available") is True
            and previous.get("scan_state") in {"complete", "incomplete_not_published"}
            and coverage_completion >= self._config.min_scan_completion_ratio
        )
        if (
            batch_resolution < self._config.min_scan_completion_ratio
            and not monitoring_only_refresh
            and not cumulative_coverage_publishable
        ):
            failed = copy.deepcopy(dict(previous))
            failed["scan_state"] = "incomplete_not_published"
            failed["last_batch_state"] = "incomplete_not_published"
            failed["full_coverage_state"] = "in_progress"
            failed["scanned_at"] = as_of.isoformat()
            failed["coverage_epoch_id"] = self._coverage_epoch_id
            failed["coverage_manifest"] = self._coverage_manifest(complete=False)
            failed["errors"] = cycle_errors
            failed["sector_exclusions"] = sector_exclusions
            failed["sector_coverage_contract_id"] = SECTOR_COVERAGE_CONTRACT_ID
            previous_audit = failed.get("scan_audit")
            scan_audit = (
                dict(previous_audit) if isinstance(previous_audit, Mapping) else {}
            )
            scan_audit.update(sector_audit)
            scan_audit.update(
                {
                    "planned_symbol_count": planned_count,
                    "completed_symbol_count": completed,
                    "completion_ratio": str(completion),
                    "batch_resolution_ratio": str(batch_resolution),
                    "discovered_symbol_count": len(
                        self._coverage_cycle_discovered_codes
                    ),
                    "coverage_cycle_completion_ratio": str(coverage_completion),
                    "coverage_cycle_resolution_ratio": str(coverage_resolution),
                    "coverage_cycle_progress_ratio": str(coverage_progress),
                    "coverage_cycle_finalized_symbol_count": (coverage_finalized_count),
                    "coverage_cycle_runtime_baseline_finalized_symbol_count": (
                        self._coverage_runtime_baseline_finalized_count
                    ),
                    "coverage_cycle_runtime_finalized_symbol_count": (
                        runtime_finalized_count
                    ),
                    "coverage_cycle_throughput_symbols_per_minute": (
                        coverage_throughput
                    ),
                    "coverage_cycle_estimated_remaining_seconds": (
                        coverage_eta_seconds
                    ),
                    "immediate_pending_symbol_count": len(self._pending_frequencies),
                    "pending_symbol_count": len(self._pending_frequencies)
                    + len(self._backoff_frequencies),
                    "retry_symbol_count": len(self._backoff_frequencies)
                    + len(self._deferred_frequencies),
                    "backoff_retry_symbol_count": len(self._backoff_frequencies),
                    "next_epoch_retry_symbol_count": len(self._deferred_frequencies),
                    "coverage_cycle_complete": False,
                    "batch_duration_ms": round(
                        (time.perf_counter() - batch_started_perf) * 1000,
                        2,
                    ),
                    "sector_scan_duration_ms": sector_scan_duration_ms,
                    "stock_scan_duration_ms": stock_scan_duration_ms,
                    "stock_worker_count": worker_count,
                    "coverage_cycle_batch_count": (self._coverage_cycle_batch_count),
                    "coverage_cycle_started_at": (
                        None
                        if self._coverage_cycle_started_at is None
                        else self._coverage_cycle_started_at.isoformat()
                    ),
                    "coverage_cycle_attempted_symbol_count": len(
                        self._coverage_cycle_completed_codes
                        | self._coverage_cycle_excluded_codes
                        | self._coverage_cycle_failed_codes
                    ),
                    "coverage_cycle_completed_symbol_count": len(
                        self._coverage_cycle_completed_codes
                    ),
                    "coverage_cycle_excluded_symbol_count": len(
                        self._coverage_cycle_excluded_codes
                    ),
                    "coverage_cycle_failed_symbol_count": len(
                        self._coverage_cycle_failed_codes
                    ),
                    "coverage_cycle_resolved_symbol_count": coverage_resolved_count,
                    "coverage_cycle_elapsed_ms": coverage_cycle_elapsed_ms,
                    "stock_failure_counts": dict(sorted(stock_failure_counts.items())),
                    "stock_exclusion_counts": dict(
                        sorted(stock_exclusion_counts.items())
                    ),
                }
            )
            failed["scan_audit"] = scan_audit
            failed["data_quality"] = {
                "complete": False,
                "stale": True,
                "failure_codes": ["scan_completion_below_threshold"],
            }
            return failed

        failure_codes = []
        if any("sector_id" in error for error in cycle_errors):
            failure_codes.append("sector_scan_partial")
        if self._coverage_cycle_failed_codes:
            failure_codes.append("stock_scan_partial")
        retained_scope = selected_member_codes.union(watchlist, holdings)
        retained: list[dict[str, object]] = []
        for row in previous_signal_rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("code"), str):
                continue
            code = str(row["code"])
            if (
                code in completed_codes
                or code in excluded_codes
                or code not in retained_scope
            ):
                continue
            retained_row = copy.deepcopy(dict(row))
            _apply_selection_scope(retained_row, selection_sources_for(code))
            retained.append(retained_row)
        signals = retained + signals

        signals.sort(
            key=lambda row: (
                POINT_TYPES.index(str(row["point_type"])),
                str(row["code"]),
                str(row["signal_id"]),
            )
        )
        counts_by_stage: dict[str, int] = {}
        counts_by_point = {point_type: 0 for point_type in POINT_TYPES}
        for row in signals:
            stage = str(row["lifecycle_stage"])
            counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
            counts_by_point[str(row["point_type"])] += 1
        ranked_ordinals = {row.assessment.sector_id: row.ordinal for row in ranked}
        dispositioned_codes = (
            self._coverage_cycle_completed_codes
            | self._coverage_cycle_excluded_codes
            | self._coverage_cycle_failed_codes
        )
        coverage_cycle_complete = (
            True
            if monitoring_only_refresh
            else (
                not self._pending_frequencies
                and not self._backoff_frequencies
                and dispositioned_codes == self._coverage_cycle_discovered_codes
            )
        )
        if not coverage_cycle_complete:
            failure_codes.append("coverage_cycle_incomplete")
        batch_duration_ms = round(
            (time.perf_counter() - batch_started_perf) * 1000,
            2,
        )
        sector_member_history_diagnostics = (
            None
            if sector_batch.strength_evidence is None
            else build_sector_member_history_diagnostics(sector_batch.strength_evidence)
        )
        payload = {
            "schema": SCHEMA,
            "algorithm_id": self._config.algorithm_id,
            "structure_contract_id": self._config.structure_contract_id,
            "parameter_set_id": self._config.parameter_set_id,
            "decision_core_id": self._decision_core_id,
            "decision_core": copy.deepcopy(self._decision_core_document),
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "sector_coverage_contract_id": SECTOR_COVERAGE_CONTRACT_ID,
            "available": True,
            "scan_state": "complete",
            "last_batch_state": "complete",
            "full_coverage_state": (
                "complete" if coverage_cycle_complete else "in_progress"
            ),
            "generated_at": as_of.isoformat(),
            "scanned_at": as_of.isoformat(),
            "as_of": (
                self._coverage_market_data_as_of or market_data_as_of
            ).isoformat(),
            "market_data_as_of": (
                self._coverage_market_data_as_of or market_data_as_of
            ).isoformat(),
            "coverage_epoch_id": self._coverage_epoch_id,
            "sector_first": True,
            "read_only": True,
            "research_only": True,
            "no_order_execution": True,
            "notification_context": _main_notification_context(
                observed_at=observed_at,
                market_data_as_of=(
                    self._coverage_market_data_as_of or market_data_as_of
                ),
                coverage_complete=coverage_cycle_complete,
                monitoring_only_refresh=monitoring_only_refresh,
                max_age_seconds=max(
                    180,
                    self._config.refresh_interval_seconds * 3,
                ),
            ),
            "counts_by_stage": dict(sorted(counts_by_stage.items())),
            "counts_by_point_type": counts_by_point,
            "screening_policy": _screening_policy_document(),
            "screening_policy_id": _screening_policy_id(),
            "sectors": [
                _sector_document(
                    assessment,
                    ordinal=ranked_ordinals.get(assessment.sector_id),
                )
                for assessment in sorted(
                    sector_batch.assessments,
                    key=lambda row: (
                        ranked_ordinals.get(row.sector_id, 10**9),
                        row.sector_id,
                    ),
                )
            ],
            "sector_strength_evidence": (
                None
                if sector_batch.strength_evidence is None
                else sector_batch.strength_evidence.evidence_document()
            ),
            "sector_strength_evidence_revision": (
                None
                if sector_batch.strength_evidence is None
                else sector_batch.strength_evidence.evidence_revision
            ),
            "sector_member_history_diagnostics": (sector_member_history_diagnostics),
            "monitor_instrument_exclusion_contract_id": (
                MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
            ),
            "monitor_instrument_exclusions": monitor_instrument_exclusions,
            "signals": signals,
            "risk_limits": _risk_limits_document(self._risk_limits),
            "scan_audit": {
                **sector_audit,
                "planned_symbol_count": planned_count,
                "discovered_symbol_count": len(self._coverage_cycle_discovered_codes),
                "completed_symbol_count": completed,
                "excluded_symbol_count": len(excluded_codes),
                "immediate_pending_symbol_count": len(self._pending_frequencies),
                "pending_symbol_count": len(self._pending_frequencies)
                + len(self._backoff_frequencies),
                "retry_symbol_count": len(self._backoff_frequencies)
                + len(self._deferred_frequencies),
                "backoff_retry_symbol_count": len(self._backoff_frequencies),
                "next_epoch_retry_symbol_count": len(self._deferred_frequencies),
                "coverage_cycle_complete": coverage_cycle_complete,
                "monitoring_only_refresh": monitoring_only_refresh,
                "monitoring_symbol_count": (
                    planned_count if monitoring_only_refresh else 0
                ),
                "completion_ratio": str(completion),
                "batch_resolution_ratio": str(batch_resolution),
                "coverage_cycle_completion_ratio": str(coverage_completion),
                "coverage_cycle_resolution_ratio": str(coverage_resolution),
                "coverage_cycle_progress_ratio": str(coverage_progress),
                "coverage_cycle_finalized_symbol_count": coverage_finalized_count,
                "coverage_cycle_runtime_baseline_finalized_symbol_count": (
                    self._coverage_runtime_baseline_finalized_count
                ),
                "coverage_cycle_runtime_finalized_symbol_count": (
                    runtime_finalized_count
                ),
                "coverage_cycle_throughput_symbols_per_minute": (coverage_throughput),
                "coverage_cycle_estimated_remaining_seconds": (coverage_eta_seconds),
                "full_market_history_scan": (
                    self._coverage_cycle_full_market_history_scan
                ),
                "background_full_refresh_required": (
                    self._coverage_cycle_background_refresh_required
                ),
                "selected_sector_count": len(selected),
                "monitor_instrument_exclusion_count": len(
                    monitor_instrument_exclusions
                ),
                "batch_duration_ms": batch_duration_ms,
                "sector_scan_duration_ms": sector_scan_duration_ms,
                "stock_scan_duration_ms": stock_scan_duration_ms,
                "stock_worker_count": worker_count,
                "coverage_cycle_elapsed_ms": coverage_cycle_elapsed_ms,
                "coverage_cycle_batch_count": self._coverage_cycle_batch_count,
                "coverage_cycle_started_at": (
                    None
                    if self._coverage_cycle_started_at is None
                    else self._coverage_cycle_started_at.isoformat()
                ),
                "coverage_cycle_attempted_symbol_count": len(
                    self._coverage_cycle_completed_codes
                    | self._coverage_cycle_excluded_codes
                    | self._coverage_cycle_failed_codes
                ),
                "coverage_cycle_completed_symbol_count": len(
                    self._coverage_cycle_completed_codes
                ),
                "coverage_cycle_excluded_symbol_count": len(
                    self._coverage_cycle_excluded_codes
                ),
                "coverage_cycle_failed_symbol_count": len(
                    self._coverage_cycle_failed_codes
                ),
                "coverage_cycle_resolved_symbol_count": coverage_resolved_count,
                "stock_failure_counts": dict(sorted(stock_failure_counts.items())),
                "stock_exclusion_counts": dict(sorted(stock_exclusion_counts.items())),
                "planned_frequencies": {
                    code: list(batch_frequencies.get(code, ())) for code in symbols
                },
            },
            "data_quality": {
                "complete": coverage_cycle_complete and not cycle_errors,
                "stale": False,
                "failure_codes": failure_codes,
            },
            "backtest_verdict": copy.deepcopy(self._backtest_verdict),
            "errors": cycle_errors,
            "sector_exclusions": sector_exclusions,
            "coverage_manifest": self._coverage_manifest(
                complete=coverage_cycle_complete
            ),
        }
        if coverage_cycle_complete:
            self._last_as_of = self._coverage_market_data_as_of or market_data_as_of
            self._cursor = ScanCursor.current(
                structure_contract_id=self._config.structure_contract_id,
                parameter_set_id=self._config.parameter_set_id,
            )
        return payload


__all__ = [
    "MarketDataGateway",
    "NotificationDispatcher",
    "POINT_TYPES",
    "SCHEMA",
    "SIGNAL_DOCUMENT_CONTRACT_ID",
    "SectorCatalogGateway",
    "TradingScreeningConfig",
    "TradingScreeningService",
]
