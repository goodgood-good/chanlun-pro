"""只由统一人工辅助决策核心驱动的只读增量选股服务。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
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
from chanlun.core.strict_structure.base_profile import (
    STRICT_STROKE_MODE,
    strict_base_config_revision,
)
from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot,
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
    apply_formal_selection_scope as _apply_selection_scope,
    sector_decision_document,
    serialize_evaluated_signal,
    signal_decision_projection,
    validate_signal_decision_document,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    canonical_setup_state_document,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframeGateBundle,
    HigherTimeframeSessionEvidence,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    selection_research_ledger_document,
    visible_selection_research,
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
    CANONICAL_POINT_TYPES,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    POINT_REVIEW_ORDER,
    RankedSector,
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.live_review_materialization import (
    resolve_live_review_materialization_receipt,
)
from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_state_from_signal_document,
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
    COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON,
    COVERAGE_EXCLUSION_REASON_CODES,
    COVERAGE_MANIFEST_FIELDS,
    COVERAGE_MANIFEST_SCHEMA,
    COVERAGE_STATE_CONTRACT_ID,
    MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID,
    SECTOR_COVERAGE_CONTRACT_ID,
    SIGNAL_DOCUMENT_CONTRACT_ID,
    coverage_manifest_dispositions_are_consistent,
    live_screening_snapshot_content_sha256,
    monitor_instrument_exclusions_are_consistent,
    screening_coverage_epoch_id,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS,
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
from cl_app.services.realtime_quotes import (
    AShareInstrumentSessionStatusBatch,
    validated_instrument_session_status_batch,
)
from cl_app.services.trading_screening_scope import (
    DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS,
    DEFAULT_VALIDATION_COHORT_SIZE,
    ScreeningScopeAuthorizationError,
    admit_screening_universe,
    configured_screening_allowlist,
    project_configured_screening_codes,
    require_configured_screening_codes,
    validate_screening_scope_configuration,
)
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT,
    QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT,
    QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE,
    QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT,
    QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
    QMT_GICS3_COMPOSITE_PROVIDER,
    QMT_GICS_HIERARCHY_CATALOG_SOURCE,
    QMT_SECTOR_STRENGTH_ADJUSTMENT,
    QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT,
    QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE,
)
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
from cl_app.services.trading_screening_gateway import (
    CANONICAL_REQUEST_BARS_BY_FREQUENCY,
    CachedSectorSnapshot,
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
    _sector_exclusion_document,
    _sector_failure_document,
)
from cl_app.services.trading_screening_presentation import (
    presentation_signal_document as _presentation_signal_document,
)
from cl_app.services.live_review_runtime_contract import (
    validate_live_review_snapshot,
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
CN = ZoneInfo("Asia/Shanghai")
# 次日候选池的重计算属于收盘后任务。15:05 为 QMT 写入 15:00 已完成分钟线
# 预留一个很小的落盘缓冲；全市场覆盖一旦开始，收盘后必须连续运行到次日盘前，
# 不能在 23:00 人为停顿一小时。盘中窗口仍只运行有界实时监听，不让几千只股票
# 的主扫描挤占每分钟候选判断。
POST_CLOSE_PRESELECTION_START = datetime_time(15, 5)
POST_CLOSE_PRESELECTION_END = datetime_time.max
PREOPEN_RECONCILIATION_START = datetime_time(8, 45)
PREOPEN_RECONCILIATION_END = datetime_time(9, 10)
OVERNIGHT_COVERAGE_CONTINUATION_START = datetime_time(0, 0)
OVERNIGHT_COVERAGE_CONTINUATION_END = PREOPEN_RECONCILIATION_START
PRIORITY_MONITOR_MORNING_START = datetime_time(9, 31)
PRIORITY_MONITOR_MORNING_END = datetime_time(11, 31)
PRIORITY_MONITOR_AFTERNOON_START = datetime_time(13, 1)
PRIORITY_MONITOR_AFTERNOON_END = datetime_time(15, 1)
# QMT 的分钟线在整分钟闭合后才可读取。固定落在闭合后 2 秒，既避免随机
# 进程启动相位读取上一根缓存，也给原生行情落盘留出一个小缓冲。
PRIORITY_MONITOR_BAR_READY_OFFSET_SECONDS = 2
# 买入区间套边界只保留到下一根合法 1m K 线，任何多轮轮转都无法满足它。
ONE_MINUTE_LOCATOR_SLA_SECONDS = 60
PRIORITY_MONITOR_SCHEMA = "chanlun-priority-signal-monitor-v2-continuation"
CANDIDATE_MONITOR_CONTRACT_ID = (
    "bar-cadence-live-candidate-monitor-v4-epoch-symbol-exclusions"
)
CANDIDATE_MONITOR_SYMBOL_EXCLUSION_SCHEMA = (
    "chanlun-candidate-monitor-symbol-exclusion"
)
CANDIDATE_MONITOR_LANE_1M = "CURRENT_1M"
CANDIDATE_MONITOR_LANE_5M = "CURRENT_5M"
CANDIDATE_MONITOR_LANE_30M = "CURRENT_30M"
PRIORITY_MONITOR_PUBLISH_BATCH_SIZE = 8
# Candidate discovery owns two native workers in production.  Publishing every
# two waves bounds the notification handoff delay without turning a large
# supportive universe into one state-file transaction per symbol.
CANDIDATE_NOTIFICATION_PUBLISH_BATCH_SIZE = 4
MONITOR_ADMISSION_MIN_GUARD_SECONDS = 5.0
PRIORITY_MONITOR_PERSIST_BATCH_SIZE = 64
_CANDIDATE_MONITOR_LANES = frozenset(
    {
        CANDIDATE_MONITOR_LANE_1M,
        CANDIDATE_MONITOR_LANE_5M,
        CANDIDATE_MONITOR_LANE_30M,
    }
)


def _remove_orphan_atomic_temporaries(target: Path) -> None:
    """Remove interrupted atomic-write files while the target lock is held."""

    for temporary in target.parent.glob(f".{target.name}.*.tmp"):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best-effort; inability to remove an old temporary must
            # not prevent the next canonical state write from completing.
            continue


_CANDIDATE_MONITOR_PRESENTATION_LANES = {
    CANDIDATE_MONITOR_LANE_1M: "PRIORITY_CURRENT_1M",
    CANDIDATE_MONITOR_LANE_5M: "CANDIDATE_CURRENT_5M",
    CANDIDATE_MONITOR_LANE_30M: "CANDIDATE_CURRENT_30M",
}
MARKET_CLOSE_CUTOFF = datetime_time(15)
COMPLETE_CLOSE_IDLE_REASON = "COMPLETE_CLOSE_SNAPSHOT_OUTSIDE_ACTIVE_WINDOW"
FULL_COVERAGE_PAUSE_REASON = "OUTSIDE_FULL_COVERAGE_REFRESH_WINDOW"
_CACHE_GENERATION_RETENTION = 3
_CACHE_GENERATION_FILE = re.compile(r"^[0-9a-f]{64}\.json$")
_CACHE_SCOPE_SIDECAR_SCHEMA = (
    "chanlun-trading-screening-cache-scope-v2-exact-cohort"
)
_CACHE_SCOPE_SIDECAR_MAX_BYTES = 64 * 1024
_LARGE_INCOMPLETE_SNAPSHOT_BYTES = 16 * 1024 * 1024
_DECISION_SOURCE_UNSPECIFIED = object()


@lru_cache(maxsize=4)
def _current_review_decision_source_id(project_root: str) -> str:
    """为当前仓库冻结一次应用进程实现身份。"""

    snapshot = current_decision_source_snapshot(Path(project_root))
    return decision_source_snapshot_id(snapshot)


@lru_cache(maxsize=32)
def _official_calendar_for_observed_day(
    observed_day: date,
) -> tuple[date, date, frozenset[date], str] | None:
    """每个观察日只加载一次已固定的上交所年度日历。

    调度不能把工作日假期误当成交易日。前向链路已固定并校验上交所年度公告，因此
    选股直接复用同一证据，不再创造第二套日历。日期超出证据覆盖时返回 ``None``，
    继续采用保守的工作日兜底；这可能增加计算，但不会漏过可能的交易日。
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
    """把收盘快照绑定到其可操作的交易日。"""

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
    """判断已证明完整的收盘快照能否继续只读静置。

    这是运行节奏门槛，不是放宽缓存。它不会把一个板块快照用于不同决策时点：当前
    Web 进程成功刷新一次后，已全部处理的收盘快照可静置到两个边界之一；15:05 用
    完整收盘数据构建下一交易日候选池，08:45 则在 09:10 时点捕获前做有界盘前核对。
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
        # 不能因诊断刷新发生在今日收盘后，就接受昨日 15:00 的截止点。
        # 行情终端 QMT 可能需要短暂时间才暴露最后一分钟，因此必须重试到今日完整收盘
        # 真正成为决策截止点。
        if cutoff.date() != local_now.date():
            return False
        phase_start = datetime.combine(
            local_now.date(),
            POST_CLOSE_PRESELECTION_START,
            tzinfo=CN,
        )
    else:
        return True

    # 当前阶段完成一次刷新即可。若它开启新的全市场周期，待处理队列会绕过
    # 空闲闸门并持续排空；若 QMT 尚未暴露收盘数据，上面的截止点检查会继续重试。
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
    """判断完整冻结周期是否必须再次查询 QMT 板块状态。

    除两个明确的每日边界外，进程重启不代表新决策时点。仅因 Python 进程变化就重算
    同一板块结构，可能改变结构点身份，并在一个覆盖周期中混入两种证据身份。只有
    盘后和盘前窗口会为完整周期探测新的市场或目录修订。
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
    """解析下一个归档窗口，包括跨夜续算。"""

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
    """把高成本全市场任务限制在明确的每日窗口内。

    收盘到下一交易日的选股在 15:05 后构建，并连续运行到下一交易日盘前核对结束。
    周末和法定休市日属于同一个跨夜周期，不能在午夜把尚未完成的覆盖队列暂停到
    下一个交易日。完整快照仍由 ``_complete_close_snapshot_can_idle`` 抑制重复计算。
    连续交易期间，每分钟预算归独立优先通道所有。
    """

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return True
    current = local_now.time()
    return bool(
        OVERNIGHT_COVERAGE_CONTINUATION_START
        <= current
        < OVERNIGHT_COVERAGE_CONTINUATION_END
        or PREOPEN_RECONCILIATION_START <= current < PREOPEN_RECONCILIATION_END
        or POST_CLOSE_PRESELECTION_START <= current < POST_CLOSE_PRESELECTION_END
    )


def _priority_monitor_session_open(observed_at: datetime) -> bool:
    """只使用已完成的 A 股分钟行情，绝不使用进行中的行情。"""

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


def _candidate_monitor_lunch_catchup_open(observed_at: datetime) -> bool:
    """允许午休补齐候选缓存，但不赋予 1m 实时通知资格。"""

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return False
    current = local_now.time()
    return bool(
        PRIORITY_MONITOR_MORNING_END <= current < PRIORITY_MONITOR_AFTERNOON_START
    )


def _priority_monitor_compute_window_open(observed_at: datetime) -> bool:
    """包含盘前预热和午休候选补齐；两者都不具备实时通知资格。"""

    local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    local_is_trading, _calendar_source = _scheduled_trading_day(
        local_now.date(),
        observed_at=local_now,
    )
    if not local_is_trading:
        return False
    current = local_now.time()
    return bool(
        (PREOPEN_RECONCILIATION_START <= current < PRIORITY_MONITOR_MORNING_START)
        or _candidate_monitor_lunch_catchup_open(local_now)
        or _priority_monitor_session_open(local_now)
    )


def _candidate_trading_elapsed_seconds(
    previous_at: datetime,
    observed_at: datetime,
) -> float:
    """累计两次观察之间真正可能产生新 A 股分钟 K 线的秒数。

    候选结构只读取已完成分钟行情。午休、夜间、周末和休市日没有新事实，不能把这些
    自然时间计入 5m/30m 监听 SLA，否则午后与次日开盘会把仍然最新的状态全部误判为
    过期，并用有限结构进程重算完全相同的 K 线前缀。
    """

    previous = normalize_datetime(previous_at, "candidate previous_at").astimezone(CN)
    observed = normalize_datetime(observed_at, "candidate observed_at").astimezone(CN)
    if observed <= previous:
        return 0.0
    # 持久状态正常只覆盖数日。异常的超长跨度无需逐日扫描；它一定超过当前最长
    # 30m SLA，直接返回自然时间即可保持失败关闭和有界健康请求。
    if (observed.date() - previous.date()).days > 370:
        return (observed - previous).total_seconds()

    elapsed = 0.0
    session_windows = (
        (PRIORITY_MONITOR_MORNING_START, PRIORITY_MONITOR_MORNING_END),
        (PRIORITY_MONITOR_AFTERNOON_START, PRIORITY_MONITOR_AFTERNOON_END),
    )
    session = previous.date()
    while session <= observed.date():
        is_trading, _calendar_source = _scheduled_trading_day(
            session,
            observed_at=observed,
        )
        if is_trading:
            for started_at, ended_at in session_windows:
                window_start = datetime.combine(session, started_at, tzinfo=CN)
                window_end = datetime.combine(session, ended_at, tzinfo=CN)
                overlap_start = max(previous, window_start)
                overlap_end = min(observed, window_end)
                if overlap_end > overlap_start:
                    elapsed += (overlap_end - overlap_start).total_seconds()
        session += timedelta(days=1)
    return elapsed


def _previous_scheduled_trading_day(
    value: date,
    *,
    observed_at: datetime,
) -> date | None:
    candidate = value - timedelta(days=1)
    for _ in range(370):
        is_trading, _source = _scheduled_trading_day(
            candidate,
            observed_at=observed_at,
        )
        if is_trading:
            return candidate
        candidate -= timedelta(days=1)
    return None


def _latest_expected_a_share_minute_cutoff(
    observed_at: datetime,
) -> datetime | None:
    """返回观察时刻应当可见的最后一根已完成 A 股分钟线。

    新鲜度必须按交易时钟计算。当天 15:00 的完成行情在盘后和次日盘前仍是当前
    决策事实；直接使用墙钟时差会在 16:00 后把所有合法收盘数据误判为过期。
    """

    observed = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    is_trading, _source = _scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    current = observed.time()
    if is_trading and current >= PRIORITY_MONITOR_MORNING_START:
        if current < PRIORITY_MONITOR_MORNING_END:
            return observed.replace(second=0, microsecond=0) - timedelta(minutes=1)
        if current < PRIORITY_MONITOR_AFTERNOON_START:
            return datetime.combine(
                observed.date(),
                datetime_time(11, 30),
                tzinfo=CN,
            )
        if current < PRIORITY_MONITOR_AFTERNOON_END:
            return observed.replace(second=0, microsecond=0) - timedelta(minutes=1)
        return datetime.combine(
            observed.date(),
            MARKET_CLOSE_CUTOFF,
            tzinfo=CN,
        )

    previous = _previous_scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    if previous is None:
        return None
    return datetime.combine(previous, MARKET_CLOSE_CUTOFF, tzinfo=CN)


def _latest_expected_a_share_five_minute_cutoff(
    observed_at: datetime,
) -> datetime | None:
    """Return the latest completed physical 5m bar expected from QMT.

    QMT labels A-share 5m bars by their close time (09:35, ..., 11:30 and
    13:05, ..., 15:00).  Between 13:01 and 13:04 the latest valid 5m fact is
    therefore still the 11:30 bar.  Comparing a 5m-only candidate bundle with
    the latest completed *one-minute* cutoff incorrectly marks the entire
    candidate lane stale for the first four minutes after each session break.
    """

    observed = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    is_trading, _source = _scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    current = observed.time()
    if is_trading:
        morning_open = datetime.combine(
            observed.date(),
            datetime_time(9, 30),
            tzinfo=CN,
        )
        afternoon_open = datetime.combine(
            observed.date(),
            datetime_time(13, 0),
            tzinfo=CN,
        )
        morning_first_close = morning_open + timedelta(minutes=5)
        afternoon_first_close = afternoon_open + timedelta(minutes=5)
        if morning_first_close.time() <= current < PRIORITY_MONITOR_MORNING_END:
            completed = int((observed - morning_open).total_seconds() // 300) * 5
            return morning_open + timedelta(minutes=completed)
        if PRIORITY_MONITOR_MORNING_END <= current < afternoon_first_close.time():
            return datetime.combine(
                observed.date(),
                datetime_time(11, 30),
                tzinfo=CN,
            )
        if afternoon_first_close.time() <= current < PRIORITY_MONITOR_AFTERNOON_END:
            completed = int((observed - afternoon_open).total_seconds() // 300) * 5
            return afternoon_open + timedelta(minutes=completed)
        if current >= PRIORITY_MONITOR_AFTERNOON_END:
            return datetime.combine(
                observed.date(),
                MARKET_CLOSE_CUTOFF,
                tzinfo=CN,
            )

    previous = _previous_scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    if previous is None:
        return None
    return datetime.combine(previous, MARKET_CLOSE_CUTOFF, tzinfo=CN)


def _structure_bundle_is_current(
    *,
    observed_at: datetime,
    bundle_as_of: datetime,
    max_age_seconds: int,
    expected_frequency: str = "1m",
) -> bool:
    """按最近应有的完成分钟线验证结构，而不是按休市墙钟流逝验证。"""

    if expected_frequency not in {"1m", "5m"}:
        raise ValueError("structure freshness frequency must be 1m or 5m")
    observed = normalize_datetime(observed_at, "observed_at")
    bundle = normalize_datetime(bundle_as_of, "bundle_as_of")
    if bundle > observed:
        return False
    expected = (
        _latest_expected_a_share_five_minute_cutoff(observed)
        if expected_frequency == "5m"
        else _latest_expected_a_share_minute_cutoff(observed)
    )
    reference = observed if expected is None else expected
    return reference - bundle <= timedelta(seconds=max_age_seconds)


def _structure_bundle_intraday_freshness_evidence(
    bundle: SymbolStructureBundle,
    *,
    requested_frequencies: Sequence[str],
) -> tuple[tuple[str, datetime], ...] | None:
    """Return independent 5m trade and optional 1m precision timestamps.

    A full coverage plan requests 1m up front, but the native gateway correctly
    skips that precision-only lane when there is no current 5m setup.  Treating
    the request itself as proof of a 1m result makes a valid 5m-only bundle look
    stale around session boundaries.  Conversely, a fresh 1m result must never
    hide a stale 5m trade-level result, so native bundles expose both timestamps
    and both are checked independently.  The scalar bundle timestamp remains a
    fail-closed compatibility path for synthetic and legacy providers.
    """

    materialized = {
        frequency for frequency, _converged, _full, _suffix in bundle.warmup_by_frequency
    }
    closed_at_by_frequency = dict(bundle.analysis_closed_at_by_frequency)
    if closed_at_by_frequency:
        materialized.update(closed_at_by_frequency)
        if "5m" not in materialized:
            return None
        required = tuple(
            frequency for frequency in ("5m", "1m") if frequency in materialized
        )
        if any(frequency not in closed_at_by_frequency for frequency in required):
            return None
        return tuple(
            (frequency, closed_at_by_frequency[frequency]) for frequency in required
        )
    if materialized:
        if materialized != {"5m"}:
            return None
        return (("5m", bundle.as_of),)
    frequency = "1m" if "1m" in requested_frequencies else "5m"
    return ((frequency, bundle.as_of),)


def _structure_bundle_is_current_for_intraday_evidence(
    bundle: SymbolStructureBundle,
    *,
    observed_at: datetime,
    max_age_seconds: int,
    requested_frequencies: Sequence[str],
) -> bool:
    if bundle.as_of > normalize_datetime(observed_at, "observed_at"):
        return False
    evidence = _structure_bundle_intraday_freshness_evidence(
        bundle,
        requested_frequencies=requested_frequencies,
    )
    return evidence is not None and all(
        _structure_bundle_is_current(
            observed_at=observed_at,
            bundle_as_of=closed_at,
            max_age_seconds=max_age_seconds,
            expected_frequency=frequency,
        )
        for frequency, closed_at in evidence
    )


def _structure_bundle_is_current_for_zero_trade_session(
    *,
    observed_at: datetime,
    bundle_as_of: datetime,
    max_age_seconds: int,
) -> bool:
    """Accept the previous close only when today's quote proves no trade.

    A suspended or not-yet-traded instrument has no completed intraday bar even
    while the exchange as a whole is open.  The ordinary freshness check must
    therefore fail closed first.  A zero-trade quote may explain only the
    current session; it cannot excuse an arbitrarily old structure, so the
    bundle must still be current at the immediately preceding scheduled close.
    """

    observed = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    bundle = normalize_datetime(bundle_as_of, "bundle_as_of").astimezone(CN)
    if bundle > observed:
        return False
    is_trading, _source = _scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    if not is_trading or not _priority_monitor_session_open(observed):
        return False
    previous = _previous_scheduled_trading_day(
        observed.date(),
        observed_at=observed,
    )
    if previous is None:
        return False
    previous_close = datetime.combine(previous, MARKET_CLOSE_CUTOFF, tzinfo=CN)
    return _structure_bundle_is_current(
        observed_at=previous_close,
        bundle_as_of=bundle,
        max_age_seconds=max_age_seconds,
    )


def _structure_bundle_is_current_for_zero_trade_intraday_evidence(
    bundle: SymbolStructureBundle,
    *,
    observed_at: datetime,
    max_age_seconds: int,
    requested_frequencies: Sequence[str],
) -> bool:
    if bundle.as_of > normalize_datetime(observed_at, "observed_at"):
        return False
    evidence = _structure_bundle_intraday_freshness_evidence(
        bundle,
        requested_frequencies=requested_frequencies,
    )
    return evidence is not None and all(
        _structure_bundle_is_current_for_zero_trade_session(
            observed_at=observed_at,
            bundle_as_of=closed_at,
            max_age_seconds=max_age_seconds,
        )
        for _frequency, closed_at in evidence
    )


def _current_session_zero_trade_codes(
    value: object,
    *,
    requested_codes: tuple[str, ...],
) -> frozenset[str]:
    """Return codes whose authenticated current quote proves zero trading.

    The evidence is deliberately narrow: the market must be open, the batch
    must remain read-only, and price discovery, order book and cumulative
    volume must all still be zero while a valid previous close is present.
    Missing or malformed evidence returns an empty set and preserves the normal
    stale-data failure.
    """

    if (
        getattr(value, "market_open", None) is not True
        or getattr(value, "real_account_access", None) is not False
        or getattr(value, "real_order_transport", None) is not False
    ):
        return frozenset()
    ticks_provider = getattr(value, "ticks", None)
    if not callable(ticks_provider):
        return frozenset()
    try:
        ticks = ticks_provider()
    except Exception:
        return frozenset()
    if not isinstance(ticks, Mapping):
        return frozenset()
    output: set[str] = set()
    requested = set(requested_codes)
    for code, tick in ticks.items():
        if code not in requested:
            continue
        try:
            last = float(getattr(tick, "last"))
            values = tuple(
                float(getattr(tick, name))
                for name in ("buy1", "sell1", "high", "low", "open", "volume")
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if last > 0 and all(number == 0 for number in values):
            output.add(str(code))
    return frozenset(output)


def _current_session_quote_diagnostics(
    value: object,
    *,
    requested_codes: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """Describe quote completeness without exposing prices or native values."""

    ticks_provider = getattr(value, "ticks", None)
    if not callable(ticks_provider):
        return ()
    try:
        ticks = ticks_provider()
    except Exception:
        return ()
    if not isinstance(ticks, Mapping):
        return ()
    rows: list[dict[str, object]] = []
    for code in requested_codes:
        tick = ticks.get(code)
        if tick is None:
            rows.append({"code": code, "present": False, "nonzero_fields": []})
            continue
        nonzero_fields: list[str] = []
        malformed = False
        try:
            last_positive = float(getattr(tick, "last")) > 0
        except (AttributeError, TypeError, ValueError, OverflowError):
            last_positive = False
            malformed = True
        for name in ("buy1", "sell1", "high", "low", "open", "volume"):
            try:
                if float(getattr(tick, name)) != 0:
                    nonzero_fields.append(name)
            except (AttributeError, TypeError, ValueError, OverflowError):
                malformed = True
        rows.append(
            {
                "code": code,
                "present": True,
                "last_positive": last_positive,
                "nonzero_fields": nonzero_fields,
                "malformed": malformed,
            }
        )
    return tuple(rows)


def _current_session_suspended_codes(
    value: object,
    *,
    requested_codes: tuple[str, ...],
    session: date,
) -> frozenset[str]:
    """Return only exact same-session QMT ``InstrumentStatus >= 1`` facts."""

    try:
        batch = validated_instrument_session_status_batch(
            value,
            requested_codes=requested_codes,
            session=session,
        )
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(fact.code for fact in batch.facts if fact.suspended)


def _priority_monitor_delay_seconds(
    observed_at: datetime,
    last_at: datetime | None,
    *,
    interval_seconds: int,
) -> float:
    """返回下一个已完成分钟 K 线固定读取相位之前的剩余时间。"""

    if last_at is None:
        return 0.0
    observed = normalize_datetime(observed_at, "observed_at")
    previous = normalize_datetime(last_at, "priority monitor last_at")
    if observed < previous:
        return 0.0
    regular_delay = max(
        0.0,
        interval_seconds - (observed - previous).total_seconds(),
    )
    local_observed = observed.astimezone(CN)
    local_previous = previous.astimezone(CN)
    is_trading, _source = _scheduled_trading_day(
        local_observed.date(),
        observed_at=local_observed,
    )
    if not is_trading:
        return regular_delay
    session_bounds = (
        (PRIORITY_MONITOR_MORNING_START, PRIORITY_MONITOR_MORNING_END),
        (PRIORITY_MONITOR_AFTERNOON_START, PRIORITY_MONITOR_AFTERNOON_END),
    )
    for start_time, end_time in session_bounds:
        session_start = datetime.combine(
            local_observed.date(),
            start_time,
            tzinfo=CN,
        ) + timedelta(seconds=PRIORITY_MONITOR_BAR_READY_OFFSET_SECONDS)
        session_end = datetime.combine(
            local_observed.date(),
            end_time,
            tzinfo=CN,
        )
        if local_observed < session_start:
            regular_delay = min(
                regular_delay,
                max(0.0, (session_start - local_observed).total_seconds()),
            )
            continue
        if local_observed >= session_end:
            continue
        elapsed = (local_observed - session_start).total_seconds()
        slot_index = int(elapsed // interval_seconds)
        current_slot = session_start + timedelta(
            seconds=slot_index * interval_seconds
        )
        if local_previous < current_slot <= local_observed:
            return 0.0
        next_slot = current_slot + timedelta(seconds=interval_seconds)
        if next_slot < session_end:
            return max(0.0, (next_slot - local_observed).total_seconds())
    return regular_delay


_PRIORITY_SIGNAL_STAGE_RANK = {
    "executable": 0,
    "triggered": 1,
    "armed": 2,
    "formed": 3,
    "approaching": 4,
    "observed": 5,
    "active": 6,
}

_ONE_MINUTE_SEGMENT_IMMEDIATE_STAGES = frozenset({"triggered", "executable", "active"})
_CURRENT_SELECTION_LIFECYCLE_STAGES = frozenset(
    {
        "observed",
        "approaching",
        "triggered",
        "executable",
        "active",
    }
)


def _current_five_minute_setup_requires_segment_monitor(
    signal: Mapping[str, object],
    observed_at: datetime,
) -> bool:
    """Return whether an execution-fresh 5m setup belongs in the 1m lane."""

    normalize_datetime(observed_at, "segment monitor observed_at")
    stage = lifecycle_stage_from_signal(signal)
    if stage in {"observed", "approaching", "formed", "armed"}:
        # These rows are either still forming or come from the bounded legacy
        # migration path.  They need another 5m observation before the service
        # can prove that the structure was replaced; wall-clock age is not that
        # proof.  Only confirmed current rows enter the 1m lane below.
        return True
    setup = signal.get("setup_5m")
    raw_anchor = setup.get("anchor_at") if isinstance(setup, Mapping) else None
    try:
        anchor_at = (
            datetime.fromisoformat(raw_anchor)
            if isinstance(raw_anchor, str)
            else raw_anchor
        )
        if not isinstance(anchor_at, datetime):
            return False
        if normalize_datetime(
            observed_at,
            "segment monitor observed_at",
        ) > normalize_datetime(anchor_at, "5m setup anchor_at") + timedelta(
            seconds=MAX_FIVE_MINUTE_SETUP_AGE_SECONDS
        ):
            return False
    except (TypeError, ValueError):
        return False
    return _is_current_selection_signal(signal)


def _signal_side(signal: Mapping[str, object]) -> str | None:
    """Return the canonical side, with a read-only legacy point-type fallback."""

    side = signal.get("side")
    if side in {"buy", "sell"}:
        return str(side)
    point_type = signal.get("point_type")
    if isinstance(point_type, str):
        if point_type.endswith("buy"):
            return "buy"
        if point_type.endswith("sell"):
            return "sell"
    return None


_MONITOR_CONTINUATION_FALLBACK_FIELDS = (
    "decision_document_schema",
    "decision_core_id",
    "decision_document_id",
    "setup_id",
    "point_id",
    "tower",
    "recursive_level",
    "structure_scope",
    "structure_frequencies",
    "stroke_mode",
    "recursive_structure_used",
    "physical_timeframe_recursive",
    "setup_5m",
    "segment_difference_1m",
    "entry_execution_boundary",
)


def _priority_monitor_continuation_document(
    signal: Mapping[str, object],
) -> dict[str, object]:
    """Return bounded state that can safely drive the next monitor decision.

    The browser projection intentionally omits immutable setup/lifecycle identity
    and exact 5m/1m occurrence timestamps.  Reusing that projection as internal
    state therefore drops confirmed setups from the 1m locator lane and breaks
    cross-session segment-enrichment notifications.  A canonical decision
    projection is still compact, but retains every field covered by the decision
    hash.  Minimal fixtures and bounded legacy imports use the explicit fallback
    fields below without weakening production decision-document validation.
    """

    document = _presentation_signal_document(signal)
    if signal.get("decision_document_schema") is not None:
        validate_signal_decision_document(signal)
        decision = signal_decision_projection(signal)
        decision_schema = decision.pop("schema")
        decision_risk = decision.pop("higher_timeframe_risk")
        document.update(copy.deepcopy(decision))
        presentation_risk = document.get("higher_timeframe_risk")
        if not isinstance(presentation_risk, Mapping):
            raise TypeError("priority monitor presentation risk is invalid")
        # ``signal_decision_projection`` intentionally keeps only fields that
        # participate in the immutable decision hash. Replacing the complete
        # presentation risk mapping with it discards authenticated sector-source
        # provenance and makes the browser reject the whole live overlay.
        document["higher_timeframe_risk"] = {
            **copy.deepcopy(presentation_risk),
            **copy.deepcopy(decision_risk),
        }
        document["decision_document_schema"] = decision_schema
        document["decision_document_id"] = copy.deepcopy(
            signal["decision_document_id"]
        )
    else:
        for field in _MONITOR_CONTINUATION_FALLBACK_FIELDS:
            if field in signal:
                document[field] = copy.deepcopy(signal[field])
    document["monitor_continuation"] = True
    document["presentation_projection"] = False
    document["full_audit_evidence_embedded"] = False
    return document


def _one_minute_segment_requires_monitor(
    signal: Mapping[str, object],
    observed_at: datetime,
) -> bool:
    """Return whether a buy setup still awaits its first exact 1m witness.

    Once a segment-difference witness exists, its first jointly-known boundary
    is immutable.  Expiry or missing execution metadata must not put the setup
    back into discovery and let a later witness replace it.
    """

    # Optional discovery capacity is only useful for buy execution.  Sell
    # sell witnesses matter when a symbol is actually held (or explicitly watched),
    # and those symbols already enter the independent mandatory lane.  A
    # non-held sell without 1m evidence must not displace a pending buy setup.
    if _signal_side(signal) != "buy":
        return False
    segment = signal.get("segment_difference_1m")
    return not isinstance(segment, Mapping)


def _is_current_selection_signal(signal: Mapping[str, object]) -> bool:
    """Return whether a lifecycle row may appear in the current shortlist.

    Terminal lifecycle rows remain available to the notification dispatcher and
    immutable audit snapshots, but they are historical facts rather than current
    selections.  Keeping this gate at the presentation boundary also lets a fresh
    monitor tombstone remove the superseded full-snapshot row without displaying
    the tombstone itself.
    """

    stage = lifecycle_stage_from_signal(signal)
    if stage not in _CURRENT_SELECTION_LIFECYCLE_STAGES:
        return False
    setup = signal.get("setup_5m")
    if not isinstance(setup, Mapping) or setup.get("terminal_segment_role") is None:
        # Compatibility for bounded test fixtures and imported research rows.
        # The production gateway always supplies exact terminal lineage.
        return True
    try:
        formation_state = canonical_setup_state_document(setup)["formation_state"]
    except (TypeError, ValueError):
        return False
    expected_stages = {
        "forming": frozenset({"approaching"}),
        "confirmed": frozenset({"triggered", "executable", "active"}),
    }
    return stage in expected_stages.get(str(formation_state), frozenset())


def _priority_signal_candidate_codes(
    *signal_groups: tuple[Mapping[str, object], ...],
    excluded_codes: frozenset[str] = frozenset(),
    allowed_stages: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """按运行紧迫度返回仍需跟踪的买卖点候选。

    六类买卖点共用同一套生命周期。盘中可选扩展通道优先跟踪买入候选；持仓与
    自选标的由强制通道继续跟踪卖点，未持仓卖点仍保留在完整覆盖和审计快照中。
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
                or point_type not in CANONICAL_POINT_TYPES
                or not isinstance(stage, str)
                or stage in {"closed", "invalidated"}
                or (allowed_stages is not None and stage not in allowed_stages)
            ):
                continue
            rank = (_PRIORITY_SIGNAL_STAGE_RANK.get(stage, 10**6), code)
            previous = best_rank.get(code)
            if previous is None or rank < previous:
                best_rank[code] = rank
    return tuple(sorted(best_rank, key=best_rank.__getitem__))


def _merge_authoritative_monitor_documents(
    main_documents: tuple[Mapping[str, object], ...],
    monitor_documents: tuple[Mapping[str, object], ...],
    monitor_code_observations: Mapping[str, tuple[datetime, str]],
    *,
    snapshot_market_data_as_of: object,
) -> tuple[
    tuple[Mapping[str, object], ...],
    tuple[Mapping[str, object], ...],
    frozenset[str],
    frozenset[str],
]:
    """合并完整快照和增量监听，并让“无记录”墓碑持续到下一完整快照。

    监听成功本身就是代码级权威事实：即使没有生成信号文档，也必须压住旧完整
    快照中的记录。时间年龄不能使事实自动失效；只有数据截止点不早于该观测的
    新完整快照才能取代它。
    """

    snapshot_cutoff: datetime | None = None
    if isinstance(snapshot_market_data_as_of, datetime):
        try:
            snapshot_cutoff = normalize_datetime(
                snapshot_market_data_as_of,
                "snapshot_market_data_as_of",
            )
        except ValueError:
            snapshot_cutoff = None
    elif isinstance(snapshot_market_data_as_of, str):
        try:
            snapshot_cutoff = normalize_datetime(
                datetime.fromisoformat(snapshot_market_data_as_of),
                "snapshot_market_data_as_of",
            )
        except ValueError:
            snapshot_cutoff = None

    main_times_by_code: dict[str, list[datetime]] = {}
    if snapshot_cutoff is None:
        for row in main_documents:
            code = row.get("code")
            if not isinstance(code, str) or not code:
                continue
            for field in ("monitor_observed_at", "observed_at"):
                raw = row.get(field)
                if not isinstance(raw, str):
                    continue
                try:
                    parsed = normalize_datetime(
                        datetime.fromisoformat(raw),
                        f"main document {field}",
                    )
                except ValueError:
                    continue
                main_times_by_code.setdefault(code, []).append(parsed)

    authoritative_codes: set[str] = set()
    superseded_codes: set[str] = set()
    for code, raw_observation in monitor_code_observations.items():
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(raw_observation, tuple)
            or len(raw_observation) != 2
            or not isinstance(raw_observation[0], datetime)
        ):
            continue
        try:
            observation = normalize_datetime(
                raw_observation[0],
                "priority monitor code observation",
            )
        except ValueError:
            continue
        code_cutoff = snapshot_cutoff
        if code_cutoff is None and main_times_by_code.get(code):
            code_cutoff = max(main_times_by_code[code])
        if code_cutoff is None:
            # 没有可比较的完整快照时，成功增量观测是唯一权威来源。
            authoritative_codes.add(code)
        elif observation > code_cutoff:
            authoritative_codes.add(code)
        else:
            # 完整快照已覆盖同一或更新的数据截止点，可以安全回收墓碑。
            superseded_codes.add(code)

    overlay_documents = tuple(
        row
        for row in monitor_documents
        if isinstance(row.get("code"), str) and row.get("code") in authoritative_codes
    )
    merged_documents = (
        tuple(
            row for row in main_documents if row.get("code") not in authoritative_codes
        )
        + overlay_documents
    )
    return (
        merged_documents,
        overlay_documents,
        frozenset(authoritative_codes),
        frozenset(superseded_codes),
    )


def _take_due_candidate_batch(
    universe: tuple[str, ...],
    *,
    last_success_at: Mapping[str, datetime],
    observed_at: datetime,
    target_seconds: int,
    monitor_interval_seconds: int,
    max_symbols: int,
    execution_grace_seconds: float = 0.0,
    excluded_codes: frozenset[str] = frozenset(),
    previous_monitor_at: datetime | None = None,
) -> tuple[str, ...]:
    """在真实容量内选取最早到期的行情节奏候选。

    目标是限制最大观察年龄，并不承诺每个标的在调度器每次一分钟触发时都重算。两根
    已完成 5m 行情之间形态不会变化，因此调度器把该范围分摊到五个分钟触发点。若
    上一轮较慢并跨过多个触发点，本轮份额会按比例增大；硬上限会通过健康状态暴露
    容量缺口，而不是无限阻塞 1m 通道。
    """

    if target_seconds <= 0 or monitor_interval_seconds <= 0 or max_symbols <= 0:
        raise ValueError("candidate cadence limits must be positive")
    if (
        isinstance(execution_grace_seconds, bool)
        or not isinstance(execution_grace_seconds, (int, float))
        or execution_grace_seconds < 0
    ):
        raise ValueError("candidate execution grace must be non-negative")
    observed = normalize_datetime(observed_at, "observed_at")
    maximum_age_seconds = target_seconds + float(execution_grace_seconds)
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
                int(_candidate_trading_elapsed_seconds(previous, observed)),
            )
    planned = max(
        1,
        (len(values) * min(elapsed_seconds, target_seconds) + target_seconds - 1)
        // target_seconds,
    )
    # ``universe`` 已由生命周期紧迫度排序。未观察标的以及同一轮完成的标的会有
    # 完全相同的时间键；此时必须保留该业务顺序，不能再按股票代码重排，否则冷启动
    # 和容量退化时会把已触发/已形成信号随机淹没在普通候选中。
    # Preserve lifecycle urgency for missing/equal-time rows, while otherwise
    # scheduling by earliest observation deadline. Looking only for
    # ``age >= target`` loses a row whenever consecutive minute rounds differ
    # by a second (for example 14:55:03 -> 15:00:02). It also wastes spare
    # capacity before a lumpy deadline group becomes impossible to drain. For
    # every deadline prefix, admit exactly the rows that cannot fit into the
    # remaining future physical waves.
    missing: list[tuple[int, str]] = []
    observed_rows: list[tuple[datetime, int, str, float, bool]] = []
    for priority_index, code in enumerate(values):
        last_at = last_success_at.get(code)
        if last_at is None:
            missing.append((priority_index, code))
            continue
        last = normalize_datetime(last_at, f"{code} candidate last_success_at")
        clock_invalid = observed < last
        observed_rows.append(
            (
                last,
                priority_index,
                code,
                _candidate_trading_elapsed_seconds(last, observed),
                clock_invalid,
            )
        )
    observed_rows.sort(key=lambda value: (value[0], value[1]))

    hard_due_count = sum(
        1
        for _last, _index, _code, age_seconds, clock_invalid in observed_rows
        if clock_invalid or age_seconds >= target_seconds
    )
    deadline_required_count = 0
    for ordinal, (_last, _index, _code, age_seconds, clock_invalid) in enumerate(
        observed_rows,
        start=1,
    ):
        remaining_seconds = (
            -1.0 if clock_invalid else maximum_age_seconds - age_seconds
        )
        future_round_count = max(
            0,
            int(max(0.0, remaining_seconds) // monitor_interval_seconds),
        )
        deadline_required_count = max(
            deadline_required_count,
            ordinal - future_round_count * max_symbols,
        )

    required_count = min(
        max_symbols,
        max(
            min(len(missing), planned),
            hard_due_count,
            deadline_required_count,
        ),
    )
    if required_count <= 0:
        return ()
    ordered_codes = tuple(code for _index, code in missing) + tuple(
        code for _last, _index, code, _age, _invalid in observed_rows
    )
    return ordered_codes[:required_count]


def _group_candidate_batch_by_sector(
    codes: tuple[str, ...],
    *,
    sector_by_code: Mapping[str, SectorAssessment],
) -> tuple[str, ...]:
    """Keep the due set unchanged while making its expensive sector facts local.

    The candidate lane normally owns one isolated structure worker until the 1m
    phase finishes.  Lexicographic symbol order can therefore pay the cold
    34,608-bar sector build for every request in a 50-second window.  Draining
    each encountered sector together lets subsequent symbols reuse the exact
    authenticated sector gate.  First-seen sector order and in-sector symbol
    order remain stable, so the scheduler still advances deterministically.
    """

    groups: dict[tuple[str, str], list[str]] = {}
    for code in dict.fromkeys(codes):
        assessment = sector_by_code.get(code)
        # Missing membership has no shareable authenticated sector identity;
        # retain its original per-symbol position instead of coalescing all
        # unclassified failures into a new priority group.
        key = (
            ("sector", assessment.sector_id)
            if assessment is not None
            else ("symbol", code)
        )
        groups.setdefault(key, []).append(code)
    return tuple(code for group in groups.values() for code in group)


def _take_rotating_priority_batch(
    universe: tuple[str, ...],
    *,
    previous_codes: tuple[str, ...],
    max_symbols: int,
) -> tuple[str, ...]:
    """Bound and rotate non-mandatory 1m work without starving the tail."""

    if max_symbols <= 0:
        return ()
    candidates = tuple(dict.fromkeys(universe))
    previous_set = set(previous_codes).intersection(candidates)
    if previous_set:
        # ``universe`` is already ordered by lifecycle urgency.  Keep every
        # unattempted (including newly arrived) candidate in that business order,
        # then move only the previous physical wave to the back.  Rotating around
        # the previous tail would let an old triggered row jump ahead of a newly
        # executable row whenever the time budget ended mid-universe.
        candidates = tuple(
            code for code in candidates if code not in previous_set
        ) + tuple(
            code for code in candidates if code in previous_set
        )
    return candidates[:max_symbols]


def _rotating_signal_candidate_admission_order(
    universe: tuple[str, ...],
    *,
    pinned_codes: tuple[str, ...],
    previous_universe: tuple[str, ...],
    last_success_at: Mapping[str, datetime],
) -> tuple[str, ...]:
    """Pin confirmed setups and fairly advance the lower-priority overflow.

    The admitted candidate window is intentionally bounded by physical 5m
    capacity.  Previously it always took the lexicographically first approaching
    rows, so an overflow tail could never be observed.  Keep an incomplete prior
    window until every retained row has one successful observation; after that,
    advance from its tail.  Confirmed/current setups remain ahead of this
    discovery rotation on every pass.
    """

    candidates = tuple(dict.fromkeys(universe))
    if not candidates:
        return ()
    candidate_set = set(candidates)
    pinned_set = set(pinned_codes).intersection(candidate_set)
    pinned = tuple(code for code in candidates if code in pinned_set)
    optional = tuple(code for code in candidates if code not in pinned_set)

    def rotate_completed_window(values: tuple[str, ...]) -> tuple[str, ...]:
        value_set = set(values)
        previous_values = tuple(
            code
            for code in dict.fromkeys(previous_universe)
            if code in value_set
        )
        if not values or not previous_values:
            return values
        previous_incomplete = any(
            code not in last_success_at for code in previous_values
        )
        tail_index = values.index(previous_values[-1])
        after_tail = values[tail_index + 1 :] + values[: tail_index + 1]
        if not previous_incomplete:
            return after_tail
        retained = set(previous_values)
        return previous_values + tuple(
            code for code in after_tail if code not in retained
        )

    # Pinned setups always remain ahead of discovery rows, but a physical
    # admission ceiling can still be smaller than the pinned set.  Rotate a
    # fully observed pinned wave as well, otherwise its lexicographic tail is
    # permanently starved exactly when locator capacity is already degraded.
    return rotate_completed_window(pinned) + rotate_completed_window(optional)


def _take_rule_recheck_batch(
    pending_codes: tuple[str, ...],
    *,
    scheduled_codes: tuple[str, ...],
    previous_codes: tuple[str, ...] = (),
    max_symbols: int,
) -> tuple[str, ...]:
    """用普通候选剩余的固定容量排空规则变更重检队列。

    普通 5m 候选继续遵守五分钟覆盖节奏；规则变更队列则是一次性积压，不能按
    “当前剩余数量的五分之一”反复缩小批次，否则队尾会指数式拖延。已经进入普通
    候选批次的代码会在同一次成功评估后自然出队，不重复占用迁移容量。
    """

    if max_symbols <= 0:
        raise ValueError("rule recheck batch capacity must be positive")
    scheduled = set(scheduled_codes)
    remaining_capacity = max(0, max_symbols - len(scheduled))
    if remaining_capacity == 0:
        return ()
    candidates = tuple(
        code for code in sorted(dict.fromkeys(pending_codes)) if code not in scheduled
    )
    if not candidates:
        return ()
    candidate_set = set(candidates)
    previous_pending = tuple(code for code in previous_codes if code in candidate_set)
    if previous_pending:
        start = candidates.index(previous_pending[-1]) + 1
        candidates = candidates[start:] + candidates[:start]
    return candidates[:remaining_capacity]


def _candidate_lane_coverage(
    universe: tuple[str, ...],
    *,
    last_success_at: Mapping[str, datetime],
    observed_at: datetime,
    target_seconds: int,
    execution_grace_seconds: float = 0.0,
) -> dict[str, object]:
    """描述真实节奏覆盖，不把未观察状态冒充当前状态。"""

    if (
        isinstance(execution_grace_seconds, bool)
        or not isinstance(execution_grace_seconds, (int, float))
        or execution_grace_seconds < 0
    ):
        raise ValueError("candidate execution grace must be non-negative")
    observed = normalize_datetime(observed_at, "observed_at")
    maximum_age_seconds = target_seconds + float(execution_grace_seconds)
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
        age = _candidate_trading_elapsed_seconds(last, observed)
        oldest_age = age if oldest_age is None else max(oldest_age, age)
        if observed < last or age > maximum_age_seconds:
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
        "execution_grace_seconds": float(execution_grace_seconds),
        "maximum_age_seconds": maximum_age_seconds,
        "age_basis": "A_SHARE_COMPLETED_MINUTE_SESSION_SECONDS",
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
    """描述归档扫描是否可以发出实时告警。

    全市场覆盖允许断点续算，耗时可能远超一分钟。队尾才发现的标的仍属于冻结覆盖
    截止点；若当作实时转换，会把隔夜或补算结果伪装成盘中告警。覆盖处理期间，独立
    优先监听器才是正常实时通道。只有完整发布本身仍属当前状态且 A 股分钟交易时段
    已开启，归档通道才可通知。
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


@dataclass(frozen=True, slots=True)
class _SectorMemberRouting:
    ranked: tuple[RankedSector, ...]
    eligible_sector_by_code: dict[str, SectorAssessment]
    context_sector_by_code: dict[str, SectorAssessment]
    effective_members_by_sector: dict[str, tuple[str, ...]]
    ranked_scan_codes: tuple[str, ...]
    audit: dict[str, object]


def _affinity_worker_slot(affinity_key: str, worker_count: int) -> int:
    """Mirror the native process proxy's stable worker-affinity contract."""

    if type(worker_count) is not int or worker_count <= 0:
        raise ValueError("worker_count must be a positive integer")
    digest = hashlib.sha256(affinity_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % worker_count


def _ranked_sector_round_robin_codes(
    eligible_by_code: Mapping[str, SectorAssessment],
    rank_by_id: Mapping[str, int],
    *,
    affinity_worker_count: int = 1,
) -> tuple[str, ...]:
    """Interleave ranked sectors so one affinity shard cannot serialize a batch.

    Sector order remains deterministic and rank-first.  Only members within the
    same 64-symbol coverage window are striped across sectors; each symbol still
    reaches its stable sector-affinity worker and keeps the shared sector cache.
    """

    codes_by_sector: dict[str, list[str]] = {}
    for code, assessment in eligible_by_code.items():
        codes_by_sector.setdefault(assessment.sector_id, []).append(code)
    ordered_sector_ids = sorted(
        codes_by_sector,
        key=lambda sector_id: (
            0 if sector_id.startswith("qmt-gics4:") else 1,
            rank_by_id.get(sector_id, 10**9),
            sector_id,
        ),
    )
    if affinity_worker_count > 1:
        worker_buckets: dict[int, list[str]] = {
            index: [] for index in range(affinity_worker_count)
        }
        for sector_id in ordered_sector_ids:
            slot = _affinity_worker_slot(
                f"sector:{sector_id}",
                affinity_worker_count,
            )
            worker_buckets[slot].append(sector_id)
        ordered_sector_ids = [
            worker_buckets[slot][offset]
            for offset in range(
                max((len(values) for values in worker_buckets.values()), default=0)
            )
            for slot in range(affinity_worker_count)
            if offset < len(worker_buckets[slot])
        ]
    for codes in codes_by_sector.values():
        codes.sort()
    return tuple(
        codes_by_sector[sector_id][offset]
        for offset in range(
            max((len(codes) for codes in codes_by_sector.values()), default=0)
        )
        for sector_id in ordered_sector_ids
        if offset < len(codes_by_sector[sector_id])
    )


def _priority_affinity_striped_codes(
    codes: tuple[str, ...],
    *,
    sector_by_code: Mapping[str, SectorAssessment],
    worker_count: int,
    candidate_symbol_striping: bool = False,
) -> tuple[str, ...]:
    """Fill each urgent-monitor wave from distinct native worker shards.

    The order inside every shard stays stable.  Calling this helper separately
    for mandatory, immediate and waiting-trigger cohorts therefore improves
    physical parallelism without allowing a lower-priority cohort to jump the
    monitor queue.
    """

    canonical = tuple(dict.fromkeys(codes))
    if worker_count <= 1 or len(canonical) <= 1:
        return canonical
    buckets: dict[int, list[str]] = {index: [] for index in range(worker_count)}
    for code in canonical:
        sector = sector_by_code.get(code)
        affinity_key = (
            f"sector:{sector.sector_id}"
            if sector is not None and sector.sector_id != "unclassified"
            else f"symbol:{code}"
        )
        if candidate_symbol_striping and affinity_key.startswith("sector:"):
            # Candidate workers each cache the same bounded sector composite.
            # Add stable symbol entropy so one large supportive sector cannot
            # pin an entire 5m batch to one process while the other sits idle.
            affinity_key = f"{affinity_key}|symbol:{code}"
        buckets[_affinity_worker_slot(affinity_key, worker_count)].append(code)
    return tuple(
        buckets[slot][offset]
        for offset in range(
            max((len(values) for values in buckets.values()), default=0)
        )
        for slot in range(worker_count)
        if offset < len(buckets[slot])
    )


def _sector_member_routing(
    *,
    assessments: tuple[SectorAssessment, ...],
    members_by_sector: Mapping[str, tuple[str, ...]],
    parent_relations: tuple[tuple[str, str], ...],
    unavailable_sector_ids: frozenset[str],
    affinity_worker_count: int = 1,
) -> _SectorMemberRouting:
    """Resolve one effective sector per stock, with an explicit hierarchy gate."""

    assessment_by_id = {item.sector_id: item for item in assessments}
    rankable = tuple(
        item for item in assessments if item.sector_id not in unavailable_sector_ids
    )
    ranked = rank_sectors(rankable)
    rank_by_id = {row.assessment.sector_id: row.ordinal for row in ranked}

    if not parent_relations:
        eligible_by_code: dict[str, SectorAssessment] = {}
        context_by_code: dict[str, SectorAssessment] = {}
        effective_members: dict[str, list[str]] = {}
        for ranked_sector in ranked:
            assessment = ranked_sector.assessment
            for member in sorted(members_by_sector.get(assessment.sector_id, ())):
                if member not in eligible_by_code:
                    eligible_by_code[member] = assessment
                    effective_members.setdefault(assessment.sector_id, []).append(
                        member
                    )
                context_by_code.setdefault(member, assessment)
        ranked_ids = {row.assessment.sector_id for row in ranked}
        remaining = sorted(
            (item for item in rankable if item.sector_id not in ranked_ids),
            key=lambda item: (
                not item.eligible,
                -item.rank_score,
                item.sector_id,
            ),
        )
        for assessment in remaining:
            for member in sorted(members_by_sector.get(assessment.sector_id, ())):
                context_by_code.setdefault(member, assessment)
        scan_codes = _ranked_sector_round_robin_codes(
            eligible_by_code,
            rank_by_id,
            affinity_worker_count=affinity_worker_count,
        )
        return _SectorMemberRouting(
            ranked=ranked,
            eligible_sector_by_code=eligible_by_code,
            context_sector_by_code=context_by_code,
            effective_members_by_sector={
                sector_id: tuple(codes)
                for sector_id, codes in sorted(effective_members.items())
            },
            ranked_scan_codes=scan_codes,
            audit={
                "sector_hierarchy_enabled": False,
                "gics4_primary_symbol_count": 0,
                "gics3_fallback_symbol_count": 0,
                "gics4_structural_blocked_symbol_count": 0,
                "gics3_parent_blocked_symbol_count": 0,
                "scan_order_contract": "RANKED_SECTOR_ROUND_ROBIN_V1",
                "scan_affinity_worker_count": affinity_worker_count,
            },
        )

    child_to_parent = dict(parent_relations)
    parent_ids = {
        item.sector_id
        for item in assessments
        if item.sector_id.startswith("qmt-gics3:")
    }.union(child_to_parent.values())
    parent_by_code: dict[str, str] = {}
    for parent_id in sorted(parent_ids):
        for member in sorted(members_by_sector.get(parent_id, ())):
            previous = parent_by_code.setdefault(member, parent_id)
            if previous != parent_id:
                raise ValueError("stock belongs to multiple GICS3 parents")
    child_by_code: dict[str, str] = {}
    for child_id, _parent_id in parent_relations:
        for member in sorted(members_by_sector.get(child_id, ())):
            previous = child_by_code.setdefault(member, child_id)
            if previous != child_id:
                raise ValueError("stock belongs to multiple GICS4 children")

    eligible_by_code = {}
    context_by_code = {}
    effective_members: dict[str, list[str]] = {}
    gics4_primary_count = 0
    gics3_fallback_count = 0
    gics4_structural_blocked_count = 0
    gics3_parent_blocked_count = 0
    for code in sorted(set(parent_by_code).union(child_by_code)):
        parent_id = parent_by_code.get(code)
        child_id = child_by_code.get(code)
        parent = assessment_by_id.get(parent_id or "")
        child = assessment_by_id.get(child_id or "")
        parent_available = bool(
            parent is not None
            and parent.sector_id not in unavailable_sector_ids
            and parent.eligible
            and not parent.hard_block
        )
        if not parent_available:
            if parent is not None:
                context_by_code[code] = parent
            elif child is not None:
                context_by_code[code] = child
            gics3_parent_blocked_count += 1
            continue

        assert parent is not None
        effective = parent
        used_fallback = True
        if child_id is not None:
            if child_id in unavailable_sector_ids or child is None:
                # 成员数不足或本轮数据失败是“未知”，由已确认可用的宽行业兜底。
                pass
            elif child.eligible and not child.hard_block:
                effective = child
                used_fallback = False
            else:
                # 子行业已有完整且明确的不利结构时，不能退回更宽的父行业放行。
                context_by_code[code] = child
                gics4_structural_blocked_count += 1
                continue
        context_by_code[code] = effective
        eligible_by_code[code] = effective
        effective_members.setdefault(effective.sector_id, []).append(code)
        if used_fallback:
            gics3_fallback_count += 1
        else:
            gics4_primary_count += 1

    scan_codes = _ranked_sector_round_robin_codes(
        eligible_by_code,
        rank_by_id,
        affinity_worker_count=affinity_worker_count,
    )
    return _SectorMemberRouting(
        ranked=ranked,
        eligible_sector_by_code=eligible_by_code,
        context_sector_by_code=context_by_code,
        effective_members_by_sector={
            sector_id: tuple(codes)
            for sector_id, codes in sorted(effective_members.items())
        },
        ranked_scan_codes=scan_codes,
        audit={
            "sector_hierarchy_enabled": True,
            "gics3_parent_sector_count": len(parent_ids),
            "gics4_child_sector_count": len(child_to_parent),
            "gics4_primary_symbol_count": gics4_primary_count,
            "gics3_fallback_symbol_count": gics3_fallback_count,
            "gics4_structural_blocked_symbol_count": (gics4_structural_blocked_count),
            "gics3_parent_blocked_symbol_count": gics3_parent_blocked_count,
            "scan_order_contract": "RANKED_SECTOR_ROUND_ROBIN_V1",
            "scan_affinity_worker_count": affinity_worker_count,
        },
    )


def _screening_policy_document() -> dict[str, object]:
    return {
        "latest_per_independent_lane": True,
        "confirmed_and_provisional_lanes_independent": True,
        "recent_confirmed_setup_ledger_retained": True,
        "external_holding_structure_policy": ("UNKNOWN_UNTIL_MANUALLY_CLASSIFIED"),
        "sell_only_higher_timeframe_evidence_policy": (
            "SCHEMA_COMPLETE_UNRESOLVED_WITHOUT_PROVIDER_CALL"
        ),
        "max_five_minute_setup_age_seconds": (MAX_FIVE_MINUTE_SETUP_AGE_SECONDS),
        "sector_catalog_source": QMT_GICS_HIERARCHY_CATALOG_SOURCE,
        "sector_price_source": "qmt_gics_hierarchy_component_composite",
        "sector_taxonomy_levels": ["GICS3", "GICS4"],
        "sector_hierarchy_gate": "GICS3_PARENT_REQUIRED",
        "sector_primary_route": "GICS4_CHILD_WHEN_AVAILABLE",
        "sector_child_unavailable_fallback": "GICS3_PARENT",
        "sector_child_structural_block_fallback": "NONE",
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
        "sector_scope": "all_parent_gated_eligible_gics3_and_gics4",
        "stock_scope": "one_effective_sector_per_symbol",
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
        "stroke_mode": STRICT_STROKE_MODE,
        "center_source": "physical_timeframe_recursive_segments",
        "recursive_structure_used": True,
        "stock_structure_request_bars": dict(CANONICAL_REQUEST_BARS_BY_FREQUENCY),
        "stock_structure_qmt_dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        "provisional_point_source": "strict_approaching_ledger",
        "stock_trade_frequency": "5m",
        "stock_segment_difference_frequency": "1m",
        "stock_segment_difference_required_for_trade_signal": False,
        "stock_segment_difference_required_for_precise_execution": True,
        "minimum_market_data_frequency": "1m",
        "qmt_one_minute_grid_revision": (QMT_COMPLETED_ONE_MINUTE_GRID_REVISION),
        "tick_data_used": False,
        "selection_universe_source": "qmt_gics3_gics4_current_hierarchy",
        "monitor_instrument_eligibility": ("qmt_native_stock_or_etf_fail_closed"),
        "isolated_structure_instrument_type_contract": (
            "shared_qmt_catalog_explicit_stock_or_etf"
        ),
        "etf_selection_path": "ETF_PROXY_WITHOUT_INDIVIDUAL_SECTOR_GATE",
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
        "higher_timeframe_warmup_physical_daily_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS
        ),
        "sector_higher_timeframe_physical_five_minute_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS * 48
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
    """返回选股规则与决策输入适配语义的身份。"""

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

    def realtime_ticks(self, codes: tuple[str, ...]) -> object: ...

    def priority_realtime_ticks(self, codes: tuple[str, ...]) -> object: ...

    def current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch: ...

    def priority_current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch: ...

    def prepare_local_history(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
    ) -> Mapping[str, object]: ...

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
        admitted_codes: tuple[str, ...] | None = None,
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
    max_symbols_per_refresh: int = DEFAULT_VALIDATION_COHORT_SIZE
    # This is an admission-list ceiling, not a promise to run every symbol.
    # The absolute per-round deadline remains the physical capacity guard.
    max_monitor_symbols_per_refresh: int = DEFAULT_VALIDATION_COHORT_SIZE
    max_total_symbols_per_refresh: int = DEFAULT_VALIDATION_COHORT_SIZE
    priority_monitoring_enabled: bool = False
    full_coverage_refresh_enabled: bool = False
    # 运维显式要求立即重建时，本进程可暂时绕过盘中全量窗口；一旦当前逻辑的
    # 完整快照发布成功，覆盖通道会自动恢复常规时段闸门。
    force_full_coverage_until_complete: bool = False
    priority_monitor_interval_seconds: int = 60
    priority_monitor_time_budget_seconds: float = 50.0
    candidate_monitor_time_budget_seconds: float = 50.0
    max_five_minute_candidate_symbols_per_refresh: int = (
        DEFAULT_VALIDATION_COHORT_SIZE
    )
    max_thirty_minute_candidate_symbols_per_refresh: int = (
        DEFAULT_VALIDATION_COHORT_SIZE
    )
    supportive_discovery_max_sector_rank: int = DEFAULT_VALIDATION_COHORT_SIZE
    validation_cohort_size: int = DEFAULT_VALIDATION_COHORT_SIZE
    max_admitted_universe_symbols: int = DEFAULT_MAX_ADMITTED_UNIVERSE_SYMBOLS
    large_scope_authorized: bool = False
    admitted_universe_codes: tuple[str, ...] = ()
    five_minute_candidate_target_seconds: int = 300
    thirty_minute_candidate_target_seconds: int = 1800
    incomplete_checkpoint_interval_seconds: int = 120
    stock_worker_count: int = 1
    full_coverage_worker_count: int | None = None
    min_scan_completion_ratio: Decimal = Decimal("0.80")
    max_structure_age_seconds: int = 3600
    algorithm_id: str = STRICT_STRATEGY_ID
    structure_contract_id: str = "physical-timeframe-recursive"
    parameter_set_id: str = strict_base_config_revision()

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
            or self.supportive_discovery_max_sector_rank <= 0
            or self.stock_worker_count <= 0
            or (
                self.full_coverage_worker_count is not None
                and self.full_coverage_worker_count <= 0
            )
        ):
            raise ValueError("screening limits must be positive")
        if self.priority_monitor_interval_seconds <= 0:
            raise ValueError("priority monitor interval must be positive")
        if self.incomplete_checkpoint_interval_seconds <= 0:
            raise ValueError("incomplete checkpoint interval must be positive")
        for label, budget in (
            ("priority", self.priority_monitor_time_budget_seconds),
            ("candidate", self.candidate_monitor_time_budget_seconds),
        ):
            if (
                isinstance(budget, bool)
                or not isinstance(budget, (int, float))
                or not (0 < budget < self.priority_monitor_interval_seconds)
            ):
                raise ValueError(
                    f"{label} monitor time budget must be inside the priority interval"
                )
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
        validate_screening_scope_configuration(
            validation_cohort_size=self.validation_cohort_size,
            max_admitted_universe_symbols=self.max_admitted_universe_symbols,
            large_scope_authorized=self.large_scope_authorized,
            full_coverage_enabled=self.full_coverage_refresh_enabled,
            force_full_coverage_until_complete=(
                self.force_full_coverage_until_complete
            ),
            per_refresh_limits={
                "max_symbols_per_refresh": self.max_symbols_per_refresh,
                "max_monitor_symbols_per_refresh": (
                    self.max_monitor_symbols_per_refresh
                ),
                "max_total_symbols_per_refresh": self.max_total_symbols_per_refresh,
                "max_five_minute_candidate_symbols_per_refresh": (
                    self.max_five_minute_candidate_symbols_per_refresh
                ),
                "max_thirty_minute_candidate_symbols_per_refresh": (
                    self.max_thirty_minute_candidate_symbols_per_refresh
                ),
            },
        )
        if (
            not isinstance(self.admitted_universe_codes, tuple)
            or len(self.admitted_universe_codes)
            != len(set(self.admitted_universe_codes))
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in self.admitted_universe_codes
            )
            or len(self.admitted_universe_codes)
            > self.effective_monitor_universe_limit
        ):
            raise ValueError("admitted_universe_codes must be a unique bounded tuple")

    @property
    def effective_monitor_universe_limit(self) -> int:
        """Return the explicitly authorized union limit for Web monitoring."""

        return (
            self.max_admitted_universe_symbols
            if self.large_scope_authorized
            else self.validation_cohort_size
        )

    @property
    def screening_scope_mode(self) -> str:
        """Return the user-visible operational scope, independent of cache state."""

        if self.full_coverage_refresh_enabled:
            return "FULL_MARKET"
        if self.large_scope_authorized:
            return "LARGE_SCOPE"
        return "VALIDATION_COHORT"

    @property
    def effective_full_coverage_worker_count(self) -> int:
        """返回盘后覆盖可占用的最大结构工作进程数。"""

        configured = self.full_coverage_worker_count
        return min(
            self.stock_worker_count,
            self.stock_worker_count if configured is None else configured,
        )

    @property
    def effective_candidate_worker_count(self) -> int:
        """为正式 5m 候选返回不包含 1m 保障分片的并行数。"""

        return max(1, self.stock_worker_count - 1)

    @property
    def effective_priority_worker_count(self) -> int:
        """为精确执行所需的 1m 区间套保留一个独立分片。"""

        return 1


def _configured_scope_allowlist(
    config: TradingScreeningConfig,
) -> frozenset[str] | None:
    return configured_screening_allowlist(
        scope_mode=config.screening_scope_mode,
        admitted_codes=config.admitted_universe_codes,
    )


def _configured_validation_cohort_codes(
    config: TradingScreeningConfig,
) -> tuple[str, ...]:
    """Return the exact cohort that may build a bounded validation snapshot."""

    if config.screening_scope_mode != "VALIDATION_COHORT":
        return ()
    if _configured_scope_allowlist(config) is None:
        return ()
    return config.admitted_universe_codes


def _configured_sector_assessment_codes(
    config: TradingScreeningConfig,
) -> tuple[str, ...] | None:
    """Bind every bounded native sector request to its exact admission list."""

    if config.screening_scope_mode == "FULL_MARKET":
        return None
    # The exact empty tuple is retained for low-level/test configurations.  Real
    # native gateways reject it before provider I/O, rather than interpreting a
    # missing keyword as permission to enumerate the full market.
    return config.admitted_universe_codes


def _project_codes_to_configured_scope(
    values: Sequence[str],
    config: TradingScreeningConfig,
) -> tuple[str, ...]:
    return project_configured_screening_codes(
        values,
        scope_mode=config.screening_scope_mode,
        admitted_codes=config.admitted_universe_codes,
    )


def _require_codes_in_configured_scope(
    values: Sequence[str],
    config: TradingScreeningConfig,
    *,
    subject: str,
) -> tuple[str, ...]:
    return require_configured_screening_codes(
        values,
        scope_mode=config.screening_scope_mode,
        admitted_codes=config.admitted_universe_codes,
        subject=subject,
    )


def _project_scan_plan_to_configured_scope(
    plan: ScanPlan,
    config: TradingScreeningConfig,
) -> ScanPlan:
    """Remove every optional planner subject outside the exact Web cohort."""

    allowlist = _configured_scope_allowlist(config)
    if allowlist is None:
        return plan
    symbols = tuple(code for code in plan.symbols if code in allowlist)
    return ScanPlan(
        sectors=plan.sectors,
        symbols=symbols,
        symbol_frequencies=tuple(
            (code, frequencies)
            for code, frequencies in plan.symbol_frequencies
            if code in allowlist
        ),
        full_market_history_scan=False,
        background_full_refresh_required=plan.background_full_refresh_required,
    )


def _initial_snapshot(
    config: TradingScreeningConfig,
    *,
    selection_research_revision: str,
    decision_source_snapshot_id: str | None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "algorithm_id": config.algorithm_id,
        "structure_contract_id": config.structure_contract_id,
        "parameter_set_id": config.parameter_set_id,
        "screening_scope_mode": config.screening_scope_mode,
        "effective_monitor_universe_limit": config.effective_monitor_universe_limit,
        "admitted_universe_codes": [],
        "decision_source_snapshot_id": decision_source_snapshot_id,
        "selection_research_revision": selection_research_revision,
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
        "counts_by_point_type": {point_type: 0 for point_type in CANONICAL_POINT_TYPES},
        "screening_policy": _screening_policy_document(),
        "screening_policy_id": _screening_policy_id(),
        "sectors": [],
        "sector_parent_relations": [],
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
            "sector_publishability_ratio": "0",
            "sector_publishability_basis": "completed_only",
            "sector_failure_counts": {},
            "sector_exclusion_counts": {},
            "sector_hierarchy_enabled": False,
            "gics4_primary_symbol_count": 0,
            "gics3_fallback_symbol_count": 0,
            "gics4_structural_blocked_symbol_count": 0,
            "gics3_parent_blocked_symbol_count": 0,
            "planned_symbol_count": 0,
            "completed_symbol_count": 0,
            "completion_ratio": "0",
            "full_market_history_scan": False,
            "background_full_refresh_required": True,
            "batch_duration_ms": 0,
            "sector_scan_duration_ms": 0,
            "stock_scan_duration_ms": 0,
            "stock_worker_count": config.stock_worker_count,
            "full_coverage_worker_limit": (config.effective_full_coverage_worker_count),
            "coverage_cycle_elapsed_ms": 0,
            "coverage_cycle_runtime_stock_scan_elapsed_ms": 0,
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
        and dominant_point_type not in CANONICAL_POINT_TYPES
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


def _snapshot_sector_routing_allowlist(
    snapshot: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
) -> frozenset[str] | None:
    """Return exact bounded routing subjects, excluding analysis-only peers."""

    scope_mode = snapshot.get("screening_scope_mode")
    raw_limit = snapshot.get("effective_monitor_universe_limit")
    raw_admitted = snapshot.get("admitted_universe_codes")
    raw_configured_admitted = snapshot.get("configured_admitted_codes")
    if scope_mode is None and raw_limit is None and raw_admitted is None:
        return None
    if (
        scope_mode
        not in {"FULL_MARKET", "LARGE_SCOPE", "VALIDATION_COHORT"}
        or type(raw_limit) is not int
        or raw_limit <= 0
        or not isinstance(raw_admitted, list)
        or any(
            not isinstance(code, str)
            or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
            for code in raw_admitted
        )
        or len(raw_admitted) != len(set(raw_admitted))
    ):
        raise ValueError("cached coverage routing admission is invalid")
    if scope_mode == "FULL_MARKET":
        return None
    discovered = manifest.get("discovered_codes")
    admitted = frozenset(raw_admitted)
    if (
        len(admitted) > raw_limit
        or not isinstance(discovered, list)
        or any(not isinstance(code, str) for code in discovered)
        or not set(discovered).issubset(admitted)
        or (
            raw_configured_admitted not in (None, [])
            and (
                not isinstance(raw_configured_admitted, list)
                or any(
                    not isinstance(code, str)
                    or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                    for code in raw_configured_admitted
                )
                or len(raw_configured_admitted)
                != len(set(raw_configured_admitted))
                or frozenset(raw_configured_admitted) != admitted
            )
        )
    ):
        raise ValueError("cached coverage routing admission is invalid")
    return admitted


def _coverage_sector_state_from_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[SectorAssessmentBatch, dict[str, tuple[str, ...]] | None]:
    """恢复为可续算覆盖周期冻结的精确板块证据。

    个股信号内嵌实际影响决策的板块评估。因此多批次扫描在进程重启后必须复用同一批
    评估文档；在相同行情截止点重新运行板块分析器也可能产生不同结构点身份，进而在
    其他内容未变的周期中悄然混入两种证据身份。
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
    routing_allowlist = _snapshot_sector_routing_allowlist(
        snapshot,
        manifest=manifest,
    )
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
    raw_parent_relations = snapshot.get("sector_parent_relations", [])
    if not isinstance(raw_parent_relations, list):
        raise ValueError("cached sector parent relations are invalid")
    parent_relations: list[tuple[str, str]] = []
    for raw_relation in raw_parent_relations:
        if (
            not isinstance(raw_relation, list)
            or len(raw_relation) != 2
            or not all(isinstance(value, str) for value in raw_relation)
        ):
            raise ValueError("cached sector parent relation is invalid")
        parent_relations.append((raw_relation[0], raw_relation[1]))
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
        parent_relations=tuple(parent_relations),
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
        if routing_allowlist is not None:
            admitted_members = {
                sector_id: tuple(
                    code for code in codes if code in routing_allowlist
                )
                for sector_id, codes in members.items()
            }
            discovered = frozenset(manifest["discovered_codes"])
            if any(
                code not in discovered
                for codes in admitted_members.values()
                for code in codes
            ):
                raise ValueError(
                    "cached sector routing membership escaped coverage discovery"
                )
            members = admitted_members
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
    current_price: float | None = None,
    decision_core_id: str,
    selection_sources: tuple[str, ...],
    formal_selection_required: bool = True,
    higher_timeframe_gates: HigherTimeframeGateBundle | None = None,
) -> dict[str, object]:
    document = serialize_evaluated_signal(
        item,
        previous_stage=previous_stage,
        name=name,
        current_price=current_price,
        decision_core_id=decision_core_id,
        selection_sources=selection_sources,
        formal_selection_required=formal_selection_required,
    )
    document["chart_urls"] = _chart_urls(str(document["code"]))
    if higher_timeframe_gates is None:
        # Keep the transport/review schema total even for callers that did not
        # attach an entry-only M/W/D provider.  Production sell-only bundles
        # carry the more specific intentional-skip reason from the gateway.
        higher_timeframe_gates = unresolved_higher_timeframe_gates(
            symbol=str(document["code"]),
            observed_at=item.lifecycle.observed_at,
            reason_code="HIGHER_TIMEFRAME_EVIDENCE_UNAVAILABLE",
            sector_subject=item.setup.sector.sector_id,
        )
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
        # 这些事实只用于展示和解释已关闭失败的月/周/日结果，不参与决策身份。
        # 扩展拥有独立契约，因此无需为了补充说明而重放已完成标的。
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
        # 可选的板块原生日线链有意保持未对账，最高只允许 AMBER，不能作为
        # 对账证据，也不能误标为已通过标的/基准重叠认证的桥接结果。
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
        # 板块月/周/日由成分 5m 合成序列派生，并使用独立的严格同源覆盖契约。
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


def _previous_lifecycle_bundle_state(
    rows: object,
    *,
    code: str,
    as_of: datetime,
    decision_core_id: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """恢复同一决策契约下该标的已发布的单调生命周期证据。"""

    if isinstance(rows, Mapping):
        rows = tuple(rows.values())
    elif not isinstance(rows, (list, tuple)):
        try:
            rows = tuple(rows)  # type: ignore[arg-type]
        except TypeError:
            rows = ()
    lifecycles: list[object] = []
    triggers: dict[str, object] = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or row.get("code") != code
            or row.get("decision_core_id") != decision_core_id
        ):
            continue
        lifecycle, trigger = lifecycle_state_from_signal_document(row)
        if lifecycle.observed_at > as_of:
            raise ValueError("persisted lifecycle is newer than structure bundle")
        lifecycles.append(lifecycle)
        if trigger is not None:
            triggers[trigger.point_id] = trigger
    return tuple(lifecycles), tuple(triggers.values())


def _stock_analysis_error_document(
    code: str,
    error: Exception,
) -> dict[str, object]:
    """规范化个股失败，不依赖界面解析异常文本。

    隔离工作进程通过属性公开远端异常类型与原始消息；测试和研究工具使用的直接内存
    网关仍通过普通异常名与消息工作。已知行情失败在冻结覆盖截止点下是确定性的；运行
    传输失败则在工作进程退避后明确允许重试。
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
        ("current_session_suspended", "CURRENT_SESSION_SUSPENDED"),
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
            "NativeScreeningWorkerDeadlineExceeded": (
                "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED"
            ),
            "NativePriorityScreeningWorkerDeadlineExceeded": (
                "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED"
            ),
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
    """判断拒绝是否属于已审计的本周期资格事实。"""

    return bool(
        error.get("reason_code") in COVERAGE_EXCLUSION_REASON_CODES
        and error.get("retry_policy") == "NEXT_MARKET_DATA_EPOCH"
        and error.get("deterministic_for_coverage_epoch") is True
    )


_CANDIDATE_MONITOR_SYMBOL_EXCLUSION_FIELDS = frozenset(
    {
        "schema",
        "code",
        "error_type",
        "reason_code",
        "failure_class",
        "retry_policy",
        "deterministic_for_coverage_epoch",
        "remote_error_type",
        "reason",
        "observation_lane",
        "market_data_cutoff",
        "excluded_at",
    }
)


def _candidate_monitor_symbol_exclusion_document(
    error: Mapping[str, object],
    *,
    observation_lane: str,
    observed_at: datetime,
) -> dict[str, object] | None:
    """Convert a deterministic optional-symbol rejection into an epoch fact.

    A single unsupported instrument must not report the shared candidate lane as
    unavailable.  The rejection is nevertheless retained, and the symbol is not
    retried against the same completed market-data bar.
    """

    if observation_lane not in _CANDIDATE_MONITOR_LANES:
        raise ValueError("candidate symbol exclusion lane is invalid")
    if not _is_coverage_exclusion(error):
        return None
    cutoff = (
        _latest_expected_a_share_minute_cutoff(observed_at)
        if observation_lane == CANDIDATE_MONITOR_LANE_1M
        else _latest_expected_a_share_five_minute_cutoff(observed_at)
    )
    if cutoff is None:
        return None
    return {
        "schema": CANDIDATE_MONITOR_SYMBOL_EXCLUSION_SCHEMA,
        "code": str(error["code"]),
        "error_type": "stock_analysis_error",
        "reason_code": str(error["reason_code"]),
        "failure_class": "MARKET_DATA_REJECTION",
        "retry_policy": "NEXT_MARKET_DATA_EPOCH",
        "deterministic_for_coverage_epoch": True,
        "remote_error_type": str(error["remote_error_type"]),
        "reason": str(error["reason"]),
        "observation_lane": observation_lane,
        "market_data_cutoff": cutoff.isoformat(),
        "excluded_at": normalize_datetime(observed_at, "observed_at").isoformat(),
    }


def _candidate_monitor_symbol_exclusion_is_valid(
    value: object,
) -> bool:
    if (
        not isinstance(value, Mapping)
        or set(value) != _CANDIDATE_MONITOR_SYMBOL_EXCLUSION_FIELDS
        or value.get("schema") != CANDIDATE_MONITOR_SYMBOL_EXCLUSION_SCHEMA
        or value.get("observation_lane") not in _CANDIDATE_MONITOR_LANES
        or value.get("error_type") != "stock_analysis_error"
        or value.get("failure_class") != "MARKET_DATA_REJECTION"
        or not isinstance(value.get("code"), str)
        or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", str(value.get("code"))) is None
        or not isinstance(value.get("remote_error_type"), str)
        or not value.get("remote_error_type")
        or not isinstance(value.get("reason"), str)
        or not value.get("reason")
        or not _is_coverage_exclusion(value)
    ):
        return False
    try:
        cutoff = normalize_datetime(
            datetime.fromisoformat(str(value["market_data_cutoff"])),
            "candidate symbol exclusion cutoff",
        )
        excluded_at = normalize_datetime(
            datetime.fromisoformat(str(value["excluded_at"])),
            "candidate symbol exclusion excluded_at",
        )
    except (KeyError, TypeError, ValueError):
        return False
    return cutoff <= excluded_at


def _candidate_monitor_symbol_exclusion_is_active(
    value: Mapping[str, object],
    *,
    observed_at: datetime,
) -> bool:
    if not _candidate_monitor_symbol_exclusion_is_valid(value):
        return False
    expected = (
        _latest_expected_a_share_minute_cutoff(observed_at)
        if value["observation_lane"] == CANDIDATE_MONITOR_LANE_1M
        else _latest_expected_a_share_five_minute_cutoff(observed_at)
    )
    if expected is None:
        return False
    cutoff = normalize_datetime(
        datetime.fromisoformat(str(value["market_data_cutoff"])),
        "candidate symbol exclusion cutoff",
    )
    return cutoff == expected


def _stock_analysis_exclusion_document(
    error: Mapping[str, object],
) -> dict[str, object]:
    """把确定性的本周期资格拒绝转换为非成功排除。

    这里不会把标的伪装成已完成；它只负责区分预期且确定性的范围资格结论与传输或
    行情失败。停牌状态与最短历史不足都只在当前行情周期内有效。
    """

    if not _is_coverage_exclusion(error):
        raise ValueError("stock analysis error is not a coverage exclusion")
    reason_code = str(error["reason_code"])
    return {
        "code": str(error["code"]),
        "exclusion_type": "stock_analysis_exclusion",
        "eligibility": COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON[reason_code],
        "reason_code": reason_code,
        "retry_policy": "NEXT_MARKET_DATA_EPOCH",
        "deterministic_for_coverage_epoch": True,
        "remote_error_type": str(error["remote_error_type"]),
        "reason": str(error["reason"]),
    }


def _sector_coverage_contract_is_valid(
    snapshot: Mapping[str, object],
) -> bool:
    """根据精确文档重新计算当前板块处置状态。"""

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
    """判断每个缓存板块文档是否都带有明确来源证据。

    当前契约始终包含 ``strength_source_revision``；值为 ``None`` 会明确记录当时没有
    可独立证明的横向强度来源。
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


def _sector_parent_relations_are_valid(snapshot: Mapping[str, object]) -> bool:
    raw_relations = snapshot.get("sector_parent_relations", [])
    raw_sectors = snapshot.get("sectors")
    if not isinstance(raw_relations, list) or not isinstance(raw_sectors, list):
        return False
    sector_ids = {
        str(row.get("sector_id"))
        for row in raw_sectors
        if isinstance(row, Mapping) and isinstance(row.get("sector_id"), str)
    }
    relations: list[tuple[str, str]] = []
    for raw in raw_relations:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not isinstance(raw[0], str)
            or not raw[0].startswith("qmt-gics4:")
            or not isinstance(raw[1], str)
            or not raw[1].startswith("qmt-gics3:")
            or raw[0] not in sector_ids
            or raw[1] not in sector_ids
        ):
            return False
        relations.append((raw[0], raw[1]))
    return bool(
        relations == sorted(relations)
        and len({child for child, _parent in relations}) == len(relations)
    )


def _cache_contract_is_valid(
    value: object,
    config: TradingScreeningConfig,
    decision_core_id: str,
    selection_research_revision: str,
    decision_source_snapshot_id: object = _DECISION_SOURCE_UNSPECIFIED,
) -> bool:
    """校验快照语义契约，不重复计算已由本进程生成的内容哈希。"""

    signals = value.get("signals") if isinstance(value, Mapping) else None
    manifest = value.get("coverage_manifest") if isinstance(value, Mapping) else None
    signal_documents_current = bool(
        isinstance(signals, list)
        and all(
            isinstance(signal, Mapping)
            and signal.get("decision_core_id") == decision_core_id
            and isinstance(signal.get("execution_profile"), Mapping)
            and _cached_signal_decision_identity_is_valid(signal)
            for signal in signals
        )
    )
    decision_source_current = bool(
        decision_source_snapshot_id is _DECISION_SOURCE_UNSPECIFIED
        or (
            isinstance(value, Mapping)
            and isinstance(decision_source_snapshot_id, str)
            and value.get("decision_source_snapshot_id") == decision_source_snapshot_id
        )
    )
    return bool(
        isinstance(value, Mapping)
        and value.get("schema") == SCHEMA
        and value.get("algorithm_id") == config.algorithm_id
        and value.get("structure_contract_id") == config.structure_contract_id
        and value.get("parameter_set_id") == config.parameter_set_id
        and value.get("read_only") is True
        and value.get("no_order_execution") is True
        and value.get("decision_core_id") == decision_core_id
        and decision_source_current
        and value.get("selection_research_revision") == selection_research_revision
        and value.get("screening_policy") == _screening_policy_document()
        and value.get("screening_policy_id") == _screening_policy_id()
        and value.get("signal_document_contract_id") == SIGNAL_DOCUMENT_CONTRACT_ID
        and isinstance(value.get("snapshot_content_sha256"), str)
        and monitor_instrument_exclusions_are_consistent(value)
        and isinstance(manifest, Mapping)
        # A cache accepted here is subsequently restored by
        # ``_restore_coverage_state``.  Accepting a looser manifest shape or a
        # different top-level evidence identity creates a valid-looking file
        # whose queue cannot be resumed after restart.
        and set(manifest) == COVERAGE_MANIFEST_FIELDS
        and manifest.get("coverage_epoch_id") == value.get("coverage_epoch_id")
        and manifest.get("screening_policy_id") == value.get("screening_policy_id")
        and manifest.get("signal_document_contract_id")
        == value.get("signal_document_contract_id")
        and manifest.get("sector_strength_evidence_revision")
        == value.get("sector_strength_evidence_revision")
        and coverage_manifest_dispositions_are_consistent(
            manifest, value.get("errors")
        )
        and manifest.get("schema") == COVERAGE_MANIFEST_SCHEMA
        and manifest.get("coverage_state_contract_id")
        == COVERAGE_STATE_CONTRACT_ID
        and isinstance(value.get("sectors"), list)
        and isinstance(value.get("signals"), list)
        and signal_documents_current
        and isinstance(value.get("data_quality"), Mapping)
        and value.get("sector_coverage_contract_id") == SECTOR_COVERAGE_CONTRACT_ID
        and _sector_coverage_contract_is_valid(value)
        and _sector_source_evidence_complete(value)
        and _sector_parent_relations_are_valid(value)
    )


def _cached_signal_decision_identity_is_valid(
    signal: Mapping[str, object],
) -> bool:
    try:
        validate_signal_decision_document(signal)
    except (TypeError, ValueError):
        return False
    return True


def _cache_is_valid(
    value: object,
    config: TradingScreeningConfig,
    decision_core_id: str,
    selection_research_revision: str,
    decision_source_snapshot_id: object = _DECISION_SOURCE_UNSPECIFIED,
) -> bool:
    """校验外部或持久化快照的语义契约与内容身份。"""

    return bool(
        _restored_snapshot_scope_is_valid(value, config)
        and _cache_contract_is_valid(
            value,
            config,
            decision_core_id,
            selection_research_revision,
            decision_source_snapshot_id,
        )
        and isinstance(value, Mapping)
        and value.get("snapshot_content_sha256")
        == live_screening_snapshot_content_sha256(value)
    )


def _restored_snapshot_scope_is_valid(
    value: object,
    config: TradingScreeningConfig,
) -> bool:
    """Prove a persisted snapshot belongs to the currently authorized scope.

    Every restore, including a full-market restore, must retain the scope that
    produced the immutable snapshot.  A validation or explicitly bounded
    large-scope process may only restore a snapshot whose complete
    strategy/routing state (publication, coverage ledger and retry queues) fits
    inside the current admission ceiling.  The complete sector-strength peer
    basket remains analysis context and is independently hashed, but cannot
    widen stock routing.  Refusing the entire immutable snapshot avoids
    presenting a clipped archive as authoritative full-market coverage.
    """

    if (
        not isinstance(value, Mapping)
        or value.get("screening_scope_mode") != config.screening_scope_mode
        or value.get("effective_monitor_universe_limit")
        != config.effective_monitor_universe_limit
    ):
        return False
    ordered_codes = _restored_snapshot_scope_codes(value)
    if ordered_codes is None:
        return False
    raw_admitted_codes = value.get("admitted_universe_codes")
    raw_configured_admitted_codes = value.get("configured_admitted_codes")
    if config.screening_scope_mode == "FULL_MARKET":
        return bool(
            isinstance(raw_admitted_codes, list)
            and all(
                isinstance(code, str)
                and re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is not None
                for code in raw_admitted_codes
            )
            and len(raw_admitted_codes) == len(set(raw_admitted_codes))
            and frozenset(raw_admitted_codes) == frozenset(ordered_codes)
        )
    nested_member_codes = _restored_snapshot_nested_member_codes(value)
    if nested_member_codes is None:
        return False
    if (
        not _bounded_snapshot_admission_is_valid(
            raw_admitted_codes,
            raw_configured_admitted_codes=raw_configured_admitted_codes,
            strategy_subject_codes=ordered_codes,
            config=config,
        )
    ):
        return False
    manifest = value.get("coverage_manifest")
    if not isinstance(manifest, Mapping):
        return False
    raw_discovered_codes = manifest.get("discovered_codes")
    if not isinstance(raw_discovered_codes, list):
        return False
    admitted = admit_screening_universe(
        # Include code-bearing strategy failures as well as coverage subjects.
        # Monitor exclusions are authenticated diagnostic-only records and are
        # deliberately excluded from routing/admission identity.
        signal_codes=ordered_codes,
        max_symbols=config.effective_monitor_universe_limit,
        large_scope_authorized=config.large_scope_authorized,
    )
    return not admitted.deferred_signal_codes


def _bounded_snapshot_admission_is_valid(
    raw_admitted_codes: object,
    *,
    raw_configured_admitted_codes: object,
    strategy_subject_codes: tuple[str, ...],
    config: TradingScreeningConfig,
) -> bool:
    """Bind a bounded snapshot to one exact configured cohort identity."""

    if (
        not isinstance(raw_admitted_codes, list)
        or any(
            not isinstance(code, str)
            or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
            for code in raw_admitted_codes
        )
        or len(raw_admitted_codes) != len(set(raw_admitted_codes))
        or len(raw_admitted_codes) > config.effective_monitor_universe_limit
        or not set(strategy_subject_codes).issubset(raw_admitted_codes)
    ):
        return False
    configured = frozenset(config.admitted_universe_codes)
    admitted = frozenset(raw_admitted_codes)
    if configured:
        return bool(
            isinstance(raw_configured_admitted_codes, list)
            and all(
                isinstance(code, str)
                and re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is not None
                for code in raw_configured_admitted_codes
            )
            and len(raw_configured_admitted_codes)
            == len(set(raw_configured_admitted_codes))
            and frozenset(raw_configured_admitted_codes) == configured
            and admitted == configured
        )
    return bool(
        raw_configured_admitted_codes in (None, [])
        and admitted == frozenset(strategy_subject_codes)
    )


def _restored_snapshot_scope_codes(value: object) -> tuple[str, ...] | None:
    """Return every strategy-subject identity carried by one snapshot."""

    if not isinstance(value, Mapping):
        return None
    manifest = value.get("coverage_manifest")
    signals = value.get("signals")
    if not isinstance(manifest, Mapping) or not isinstance(signals, list):
        return None

    ordered_codes: list[str] = []
    for field in (
        "discovered_codes",
        "completed_codes",
        "excluded_codes",
        "failed_codes",
        "discarded_out_of_scope_retry_codes",
    ):
        raw_codes = manifest.get(field)
        if not isinstance(raw_codes, list) or any(
            not isinstance(code, str) or not code for code in raw_codes
        ):
            return None
        ordered_codes.extend(raw_codes)
    for field in (
        "pending_frequencies",
        "backoff_frequencies",
        "deferred_frequencies",
    ):
        raw_frequency_map = manifest.get(field)
        if not isinstance(raw_frequency_map, Mapping) or any(
            not isinstance(code, str) or not code for code in raw_frequency_map
        ):
            return None
        ordered_codes.extend(str(code) for code in raw_frequency_map)
    for signal in signals:
        if not isinstance(signal, Mapping):
            return None
        code = signal.get("code")
        if not isinstance(code, str) or not code:
            return None
        ordered_codes.append(code)
    errors = value.get("errors")
    if not isinstance(errors, list):
        return None
    for row in errors:
        if not isinstance(row, Mapping):
            return None
        ordered_codes.extend(_explicit_strategy_subject_codes(row))

    # Monitor exclusions describe instruments that were rejected before any
    # strategy structure request.  Their contract is diagnostic-only and is
    # content-hash authenticated separately; treating those codes as strategy
    # subjects makes a valid exact cohort appear to escape merely because an
    # unrelated watchlist contains an index or unresolved instrument.
    monitor_exclusions = value.get("monitor_instrument_exclusions")
    if not isinstance(monitor_exclusions, list):
        return None
    for row in monitor_exclusions:
        code = row.get("code") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or row.get("diagnostic_only") is not True
            or not isinstance(code, str)
            or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
        ):
            return None
    return tuple(dict.fromkeys(ordered_codes))


def _restored_snapshot_nested_member_codes(value: object) -> tuple[str, ...] | None:
    """Return every sector/runtime member identity embedded in a snapshot."""

    if not isinstance(value, Mapping):
        return None
    evidence = value.get("sector_strength_evidence")
    if evidence is None:
        return ()
    if not isinstance(evidence, Mapping):
        return None
    sectors = evidence.get("sectors")
    if not isinstance(sectors, list):
        return None
    codes: list[str] = []
    for row in sectors:
        if not isinstance(row, Mapping):
            return None
        members = row.get("member_symbols")
        if not isinstance(members, list) or any(
            not isinstance(code, str)
            or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
            for code in members
        ):
            return None
        codes.extend(members)
    return tuple(dict.fromkeys(codes))


def _explicit_strategy_subject_codes(value: object) -> tuple[str, ...]:
    """Collect identities explicitly labelled as code/symbol in diagnostics."""

    codes: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in {"code", "symbol"} and isinstance(nested, str) and nested:
                codes.append(nested)
            else:
                codes.extend(_explicit_strategy_subject_codes(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            codes.extend(_explicit_strategy_subject_codes(nested))
    return tuple(dict.fromkeys(codes))


def _screening_review_readiness(
    snapshot: Mapping[str, object],
    *,
    identity_valid: bool,
) -> tuple[bool, str]:
    """证明不可变选股页面能否进入人工复核。

    运行就绪与研究样本就绪有意分离。覆盖周期未完成时页面仍可用于查看，但不能放行
    每日前向评估器。机械前置条件齐全后，精确的复核边界校验器是唯一最终决策核心。
    此结论不评价每日前向归档另行要求的同交易日 QMT 捕获。
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
        engine: HumanAssistedDecisionCore,
        scan_planner: Callable[..., ScanPlan] = build_scan_plan,
        cache_path: Path,
        human_review_archive_root: Path | None = None,
        selection_research: tuple[SelectionResearchSnapshot, ...] = (),
        clock: Callable[[], datetime],
        notifier: NotificationDispatcher | None,
        config: TradingScreeningConfig = TradingScreeningConfig(),
        risk_limits: RiskLimits = RiskLimits(),
        backtest_verdict: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(engine, HumanAssistedDecisionCore):
            raise TypeError("实时选股服务必须使用唯一人工辅助决策核心")
        self._market_data = market_data
        self._sector_catalog = sector_catalog
        self._engine = engine
        self._formal_selection_required = bool(
            getattr(
                getattr(engine, "contract", None),
                "formal_selection_required",
                True,
            )
        )
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
        self._selection_research = tuple(selection_research)
        self._selection_research_revision = sha256_json(
            selection_research_ledger_document(self._selection_research)
        )
        self._decision_source_snapshot_id: str | None = None
        try:
            project_root = Path(__file__).resolve().parents[4]
            self._decision_source_snapshot_id = _current_review_decision_source_id(
                str(project_root)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # 缺少实现身份时不能恢复持久化决策结论；服务仍可从空状态重算。
            self._decision_source_snapshot_id = None
        self._human_review_decision_source_snapshot_id: str | None = (
            self._decision_source_snapshot_id
            if self._human_review_archive_root is not None
            else None
        )
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
        self._last_incomplete_checkpoint_at: datetime | None = None
        self._state_lock = RLock()
        self._scan_lock = Lock()
        # 全量覆盖与实时优先监听使用不同的互斥边界。实时分支只更新紧凑监听状态，
        # 不替换全市场发布物，因此可在候选/板块进程执行长任务时安全并行。
        self._priority_scan_lock = Lock()
        # 文件锁保护路径，但不能阻止同一进程中较旧的内存快照后写覆盖较新的
        # 检查点；把“捕获状态 + 原子替换”按调用顺序串行化。
        self._priority_monitor_persist_lock = Lock()
        self._priority_progress_launch_lock = Lock()
        self._priority_progress_thread: Thread | None = None
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
        # 仅监听观测发生在完整覆盖周期已认证之后；其瞬时失败属于运行诊断，
        # 不是覆盖失败。二者分离可避免一次复查失败永久污染不可变周期。
        self._last_monitoring_at: datetime | None = None
        self._last_monitoring_errors: tuple[dict[str, object], ...] = ()
        self._pending_frequencies: dict[str, set[str]] = {}
        # 工作进程或传输的瞬时失败在同一冻结行情周期内按节奏重试；确定性数据
        # 拒绝保留在 ``_deferred_frequencies``，直到出现真正的新行情周期。
        self._backoff_frequencies: dict[str, set[str]] = {}
        self._deferred_frequencies: dict[str, set[str]] = {}
        self._monitor_offset = 0
        # 可恢复队列排空期间，全市场覆盖周期有意保持冻结。另用紧凑状态追踪
        # 当前 K 线的优先观测，避免持仓、自选和活跃信号在历史覆盖任务后等待数小时。
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
        self._priority_monitor_mandatory_count = 0
        self._priority_monitor_immediate_universe_count = 0
        self._priority_monitor_tracking_universe_count = 0
        self._priority_monitor_scheduled_count = 0
        self._priority_monitor_configured_rotation_seconds: int | None = None
        self._priority_monitor_current_session_zero_trade_codes: tuple[str, ...] = ()
        self._priority_monitor_zero_trade_quote_status = "not_observed"
        self._priority_monitor_zero_trade_quote_error: str | None = None
        self._priority_monitor_zero_trade_quote_diagnostics: tuple[
            dict[str, object], ...
        ] = ()
        self._priority_monitor_current_session_suspended_codes: tuple[str, ...] = ()
        self._priority_monitor_instrument_status_probe_status = "not_observed"
        self._priority_monitor_instrument_status_probe_error: str | None = None
        self._candidate_monitor_last_errors: tuple[dict[str, object], ...] = ()
        self._candidate_monitor_symbol_exclusions: dict[
            str, dict[str, object]
        ] = {}
        self._candidate_monitor_started_at: datetime | None = None
        self._candidate_monitor_five_universe: tuple[str, ...] = ()
        self._candidate_monitor_thirty_universe: tuple[str, ...] = ()
        self._candidate_monitor_five_last_success_at: dict[str, datetime] = {}
        self._candidate_monitor_thirty_last_success_at: dict[str, datetime] = {}
        self._candidate_monitor_last_five_codes: tuple[str, ...] = ()
        self._candidate_monitor_last_thirty_codes: tuple[str, ...] = ()
        self._candidate_monitor_last_deferred_codes: tuple[str, ...] = ()
        self._candidate_monitor_signal_pool_count = 0
        self._candidate_monitor_signal_admitted_count = 0
        self._candidate_monitor_signal_deferred_count = 0
        self._candidate_monitor_supportive_eligible_count = 0
        self._candidate_monitor_supportive_admitted_count = 0
        self._candidate_monitor_supportive_capacity = 0
        self._priority_monitor_immediate_pool_count = 0
        self._priority_monitor_immediate_deferred_count = 0
        self._priority_monitor_locator_pool_count = 0
        self._priority_monitor_locator_admission_deferred_count = 0
        self._candidate_monitor_suspended_session: date | None = None
        self._candidate_monitor_current_session_suspended_codes: tuple[str, ...] = ()
        self._candidate_monitor_suspension_probe_status = "not_observed"
        self._candidate_monitor_suspension_probe_error: str | None = None
        self._priority_monitor_last_round_elapsed_seconds: float | None = None
        self._priority_monitor_locator_last_observed_at: datetime | None = None
        self._priority_monitor_locator_last_elapsed_seconds: float | None = None
        self._priority_monitor_locator_last_scheduled_count = 0
        self._priority_monitor_locator_last_attempted_count = 0
        self._priority_monitor_locator_last_completed_count = 0
        self._priority_monitor_locator_runtime_verified = False
        self._priority_monitor_sector_source_mode: str | None = None
        self._priority_monitor_sector_as_of: datetime | None = None
        self._priority_monitor_sector_coverage_epoch_id: str | None = None
        # 决策规则改变时，旧归档中的结论不能继续展示或直接改签。这里只保留旧归档
        # 曾经命中的代码，交给当前唯一决策核心按有界 5m 节奏重新计算。
        self._decision_rule_recheck_source_snapshot_sha256: str | None = None
        self._decision_rule_recheck_source_core_id: str | None = None
        self._decision_rule_recheck_pending_codes: set[str] = set()
        self._decision_rule_recheck_last_attempted_codes: tuple[str, ...] = ()
        self._decision_rule_recheck_last_deferred_codes: tuple[str, ...] = ()
        self._decision_rule_recheck_last_errors: tuple[dict[str, object], ...] = ()
        # A source-only deployment revision must invalidate every persisted signal
        # conclusion, but it must not silently collapse the current session's
        # candidate discovery scope.  A fully authenticated previous-close
        # snapshot may therefore contribute only its frozen sector membership and
        # code identities while the current source rebuilds the full publication.
        self._preselection_continuity_sector_batch: SectorAssessmentBatch | None = None
        self._preselection_continuity_sector_members: (
            dict[str, tuple[str, ...]] | None
        ) = None
        self._preselection_continuity_market_data_as_of: datetime | None = None
        self._preselection_continuity_coverage_epoch_id: str | None = None
        self._preselection_continuity_sector_catalog_revision: str | None = None
        self._preselection_continuity_source_snapshot_sha256: str | None = None
        self._preselection_continuity_source_name: str | None = None
        self._preselection_continuity_target_session: str | None = None
        self._preselection_continuity_signal_codes: tuple[str, ...] = ()
        self._preselection_continuity_supportive_code_count = 0
        self._preselection_continuity_sector_runtime_hydrated = False
        self._quarantined_priority_monitor_decision_core_id: str | None = None
        self._quarantined_priority_monitor_reason: str | None = None
        self._quarantined_priority_monitor_recheck_code_count = 0
        # 持久化文档跨重启保留生命周期和幂等性，但新进程仍须立即证明自身 QMT 路由。
        self._priority_monitor_runtime_verified = False
        self._coverage_cycle_started_at: datetime | None = None
        self._coverage_cycle_started_perf: float | None = None
        self._coverage_runtime_baseline_finalized_count = 0
        self._coverage_runtime_stock_scan_elapsed_seconds = 0.0
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
        self._presentation_cached_sector_snapshot: CachedSectorSnapshot | None = None
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
        self._snapshot = loaded_snapshot or _initial_snapshot(
            config,
            selection_research_revision=self._selection_research_revision,
            decision_source_snapshot_id=self._decision_source_snapshot_id,
        )
        if (
            loaded_snapshot is not None
            and isinstance(loaded_snapshot.get("coverage_manifest"), Mapping)
            and loaded_snapshot["coverage_manifest"].get("complete") is not True
        ):
            self._last_incomplete_checkpoint_at = normalize_datetime(
                self._clock(), "clock"
            )
        # 主快照可能先提供旧规则命中代码；实时状态更接近停机时刻，并保存当前规则
        # 已经排空后的准确剩余集合，因此必须最后恢复。旧核心状态只会进入代码重检
        # 迁移分支，绝不会覆盖这里安装的当前核心快照或恢复旧信号结论。
        self._load_priority_monitor_state()
        continuity_recheck_reseeded = (
            self._restore_preselection_continuity_recheck_seed()
        )
        if (
            loaded_snapshot is not None
            and self._reconcile_rule_recheck_after_current_snapshot(self._snapshot)
        ) or continuity_recheck_reseeded:
            self._persist_priority_monitor_state()
        # 已加载快照已通过完整语义与内容哈希闸门；健康检查可按身份认证这份
        # 不可变发布，无需每个 HTTP 请求都重新哈希超过 100 MiB 的信号树。
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
        self._review_readiness_error: str | None = None
        # 大型全市场发布通过最深层人工复核边界可能耗时数分钟。健康请求不能
        # 重复这项 CPU 工作；单个守护线程校验指定不可变哈希，在结果缓存前
        # /readyz 保持响应并报告 PENDING。
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
        self._snapshot["decision_source_snapshot_id"] = (
            self._decision_source_snapshot_id
        )
        self._snapshot["selection_research_revision"] = (
            self._selection_research_revision
        )
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
                # 绝不能把只能部分恢复的板块状态混入已认证的当前覆盖周期。
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
            progress_registrar(self._record_native_progress)

    def _native_sector_assessments(
        self,
        *,
        as_of: datetime,
    ) -> SectorAssessmentBatch:
        admitted_codes = _configured_sector_assessment_codes(self._config)
        if admitted_codes is None:
            return self._sector_catalog.native_sector_assessments(as_of=as_of)
        return self._sector_catalog.native_sector_assessments(
            as_of=as_of,
            admitted_codes=admitted_codes,
        )

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
            or payload.get("screening_policy_id") != _screening_policy_id()
            or not isinstance(payload.get("decision_core_id"), str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(payload.get("decision_core_id")),
            )
            is None
            or not isinstance(self._decision_source_snapshot_id, str)
            or payload.get("decision_source_snapshot_id")
            != self._decision_source_snapshot_id
            or payload.get("selection_research_revision")
            != self._selection_research_revision
            or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
            or payload.get("read_only") is not True
            or payload.get("automated_order_authorized") is not False
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
            # Continuation rows retain exact decision/scheduling identity but
            # exclude audit-only evidence.  Browser projections are forbidden
            # here because they cannot safely drive the next monitor decision.
            or value.get("monitor_continuation") is not True
            or value.get("presentation_projection") is not False
            or value.get("full_audit_evidence_embedded") is not False
            or value.get("observation_lane")
            not in _CANDIDATE_MONITOR_PRESENTATION_LANES.values()
            or not isinstance(value.get("monitor_observed_at"), str)
            or not value.get("monitor_observed_at")
            or not isinstance(value.get("realtime_observation"), bool)
            for value in raw_documents
        ):
            return
        latest_documents = {
            str(value["signal_id"]): copy.deepcopy(dict(value))
            for value in raw_documents
        }
        try:
            for document in latest_documents.values():
                if document.get("decision_document_schema") is not None:
                    validate_signal_decision_document(document)
        except (KeyError, TypeError, ValueError):
            return
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
        raw_candidate_symbol_exclusions = payload.get(
            "candidate_symbol_exclusions",
            [],
        )
        raw_five_universe = payload.get("five_minute_universe", [])
        raw_thirty_universe = payload.get("thirty_minute_universe", [])
        raw_last_five_codes = payload.get("last_five_minute_codes", [])
        raw_last_thirty_codes = payload.get("last_thirty_minute_codes", [])
        raw_last_deferred_codes = payload.get("last_deferred_candidate_codes", [])
        raw_recheck_pending_codes = payload.get(
            "decision_rule_recheck_pending_codes",
            [],
        )
        raw_recheck_last_attempted_codes = payload.get(
            "decision_rule_recheck_last_attempted_codes",
            [],
        )
        raw_recheck_last_deferred_codes = payload.get(
            "decision_rule_recheck_last_deferred_codes",
            [],
        )
        raw_recheck_last_errors = payload.get(
            "decision_rule_recheck_last_errors",
            [],
        )
        string_lists = (
            raw_last_codes,
            raw_five_universe,
            raw_thirty_universe,
            raw_last_five_codes,
            raw_last_thirty_codes,
            raw_last_deferred_codes,
            raw_recheck_pending_codes,
            raw_recheck_last_attempted_codes,
            raw_recheck_last_deferred_codes,
        )
        if any(
            not isinstance(values, list)
            or len(values) != len(set(values))
            or any(not isinstance(value, str) or not value for value in values)
            for values in string_lists
        ):
            return
        if (
            not isinstance(raw_candidate_symbol_exclusions, list)
            or any(
                not _candidate_monitor_symbol_exclusion_is_valid(value)
                for value in raw_candidate_symbol_exclusions
            )
            or len(raw_candidate_symbol_exclusions)
            != len(
                {
                    str(value["code"])
                    for value in raw_candidate_symbol_exclusions
                    if isinstance(value, Mapping)
                }
            )
        ):
            return
        candidate_symbol_exclusions = {
            str(value["code"]): copy.deepcopy(dict(value))
            for value in raw_candidate_symbol_exclusions
            if isinstance(value, Mapping)
        }
        if any(
            not isinstance(values, list)
            or any(not isinstance(value, Mapping) for value in values)
            for values in (
                raw_last_errors,
                raw_candidate_errors,
                raw_recheck_last_errors,
            )
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
            raw_last_at = payload.get("last_at")
            last_at = (
                None
                if raw_last_at is None
                else normalize_datetime(
                    datetime.fromisoformat(str(raw_last_at)),
                    "priority monitor last_at",
                )
            )
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
                "CURRENT_CACHED_SECTOR_SNAPSHOT",
                "FROZEN_COVERAGE_EPOCH",
                "PRESELECTION_CONTINUITY",
                "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED",
                "UNCLASSIFIED_SECTOR_FAIL_CLOSED",
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
            raw_recheck_source_sha256 = payload.get(
                "decision_rule_recheck_source_snapshot_sha256"
            )
            raw_recheck_source_core_id = payload.get(
                "decision_rule_recheck_source_core_id"
            )
            if (raw_recheck_source_sha256 is None) != (
                raw_recheck_source_core_id is None
            ):
                return
            if raw_recheck_source_sha256 is None:
                if raw_recheck_pending_codes:
                    return
            elif (
                not isinstance(raw_recheck_source_sha256, str)
                or not raw_recheck_source_sha256.startswith("sha256:")
                or not isinstance(raw_recheck_source_core_id, str)
                or not raw_recheck_source_core_id.startswith("sha256:")
                or any(
                    re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                    for code in raw_recheck_pending_codes
                )
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        if self._config.screening_scope_mode != "FULL_MARKET":
            raw_scope_mode = payload.get("screening_scope_mode")
            raw_scope_limit = payload.get("effective_monitor_universe_limit")
            raw_admitted_codes = payload.get("admitted_universe_codes")
            if (
                raw_scope_mode != self._config.screening_scope_mode
                or raw_scope_limit
                != self._config.effective_monitor_universe_limit
                or not isinstance(raw_admitted_codes, list)
                or any(
                    not isinstance(code, str) or not code
                    for code in raw_admitted_codes
                )
                or len(raw_admitted_codes) != len(set(raw_admitted_codes))
            ):
                # Older compact files did not carry an immutable admission
                # proof.  A bounded process must fail closed instead of treating
                # the conclusions themselves as evidence of their own scope.
                return
            # Every code-bearing field participates in the proof.  In
            # particular, older files may have an empty candidate universe but
            # dozens of ``latest_documents``; checking only five/thirty-minute
            # queues would restore those stale conclusions onto the page.
            restored_signal_codes = tuple(
                dict.fromkeys(
                    (
                        *raw_five_universe,
                        *raw_last_codes,
                        *raw_last_five_codes,
                        *raw_last_deferred_codes,
                        *five_last_success_at,
                        *code_observations,
                        *(str(value) for value in raw_codes.values()),
                        *candidate_symbol_exclusions,
                        *(
                            code
                            for error in (*raw_last_errors, *raw_candidate_errors)
                            for code in _explicit_strategy_subject_codes(error)
                        ),
                    )
                )
            )
            restored_supportive_codes = tuple(
                dict.fromkeys(
                    (
                        *raw_thirty_universe,
                        *raw_last_thirty_codes,
                        *thirty_last_success_at,
                    )
                )
            )
            restored_recheck_codes = tuple(
                dict.fromkeys(
                    (
                        *raw_recheck_pending_codes,
                        *raw_recheck_last_attempted_codes,
                        *raw_recheck_last_deferred_codes,
                        *(
                            code
                            for error in raw_recheck_last_errors
                            for code in _explicit_strategy_subject_codes(error)
                        ),
                    )
                )
            )
            restored_codes = tuple(
                dict.fromkeys(
                    (
                        *restored_signal_codes,
                        *restored_supportive_codes,
                        *restored_recheck_codes,
                    )
                )
            )
            configured_allowlist = _configured_scope_allowlist(self._config)
            if (
                set(restored_codes) != set(raw_admitted_codes)
                or (
                    configured_allowlist is not None
                    and not set(restored_codes).issubset(configured_allowlist)
                )
            ):
                return
            with self._state_lock:
                current_snapshot = self._snapshot
            current_snapshot_codes = (
                _restored_snapshot_scope_codes(current_snapshot)
                if _restored_snapshot_scope_is_valid(
                    current_snapshot,
                    self._config,
                )
                else ()
            )
            restored_admission = admit_screening_universe(
                signal_codes=(
                    *(current_snapshot_codes or ()),
                    *restored_signal_codes,
                ),
                supportive_codes=restored_supportive_codes,
                recheck_codes=restored_recheck_codes,
                max_symbols=self._config.effective_monitor_universe_limit,
                large_scope_authorized=self._config.large_scope_authorized,
            )
            if (
                restored_admission.deferred_signal_codes
                or restored_admission.deferred_supportive_codes
                or restored_admission.deferred_recheck_codes
            ):
                # A broad state file from a previous production run cannot
                # silently repopulate a bounded validation process.  Ignore it
                # rather than publishing a clipped, non-authoritative queue.
                return
        if not set(five_last_success_at).issubset(set(raw_five_universe)) or not set(
            thirty_last_success_at
        ).issubset(set(raw_thirty_universe)):
            return
        for document in latest_documents.values():
            code = str(document["code"])
            observation = code_observations.get(code)
            if observation is None:
                return
            observation_at, lane = observation
            presentation_lane = _CANDIDATE_MONITOR_PRESENTATION_LANES[lane]
            try:
                monitor_observed_at = normalize_datetime(
                    datetime.fromisoformat(str(document["monitor_observed_at"])),
                    f"{code} monitor document observed_at",
                )
            except ValueError:
                return
            if (
                document.get("observation_lane") != presentation_lane
                or monitor_observed_at != observation_at
                or document.get("realtime_observation")
                is not (lane == CANDIDATE_MONITOR_LANE_1M)
            ):
                return
        cached_core_id = str(payload["decision_core_id"])
        if cached_core_id != self._decision_core_id:
            # 旧实时状态已经通过与当前状态相同的完整结构、因果时间和内容哈希校验。
            # 这里只提取代码；信号阶段、买卖点、板块结论和上次成功时间全部隔离。
            recheck_codes = {
                str(document["code"])
                for document in latest_documents.values()
                if re.fullmatch(
                    r"^(?:SH|SZ|BJ)\.\d{6}$",
                    str(document["code"]),
                )
                is not None
            }
            recheck_codes.update(raw_recheck_pending_codes)
            recheck_codes = set(
                _project_codes_to_configured_scope(
                    tuple(sorted(recheck_codes)),
                    self._config,
                )
            )
            recheck_admission = admit_screening_universe(
                recheck_codes=sorted(recheck_codes),
                max_symbols=self._config.effective_monitor_universe_limit,
                large_scope_authorized=self._config.large_scope_authorized,
            )
            recheck_codes = set(recheck_admission.recheck_codes)
            with self._background_lock:
                self._decision_rule_recheck_source_snapshot_sha256 = str(
                    payload["content_sha256"]
                )
                self._decision_rule_recheck_source_core_id = cached_core_id
                self._decision_rule_recheck_pending_codes.update(recheck_codes)
            self._quarantined_priority_monitor_decision_core_id = cached_core_id
            self._quarantined_priority_monitor_reason = (
                "DECISION_CORE_IDENTITY_MISMATCH"
            )
            self._quarantined_priority_monitor_recheck_code_count = len(recheck_codes)
            return
        effective_five_universe = list(raw_five_universe)
        effective_last_five_codes = list(raw_last_five_codes)
        effective_last_deferred_codes = list(raw_last_deferred_codes)
        effective_candidate_errors = list(raw_candidate_errors)
        effective_recheck_attempted_codes = list(raw_recheck_last_attempted_codes)
        effective_recheck_deferred_codes = list(raw_recheck_last_deferred_codes)
        effective_recheck_errors = list(raw_recheck_last_errors)
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
            copy.deepcopy(dict(value)) for value in effective_candidate_errors
        )
        self._candidate_monitor_symbol_exclusions = candidate_symbol_exclusions
        self._candidate_monitor_started_at = candidate_started_at
        self._candidate_monitor_five_universe = tuple(effective_five_universe)
        self._candidate_monitor_thirty_universe = tuple(raw_thirty_universe)
        self._candidate_monitor_five_last_success_at = five_last_success_at
        self._candidate_monitor_thirty_last_success_at = thirty_last_success_at
        self._candidate_monitor_last_five_codes = tuple(effective_last_five_codes)
        self._candidate_monitor_last_thirty_codes = tuple(raw_last_thirty_codes)
        self._candidate_monitor_last_deferred_codes = tuple(
            effective_last_deferred_codes
        )
        self._priority_monitor_sector_source_mode = raw_sector_source_mode
        self._priority_monitor_sector_as_of = sector_as_of
        self._priority_monitor_sector_coverage_epoch_id = raw_sector_epoch_id
        self._decision_rule_recheck_source_snapshot_sha256 = raw_recheck_source_sha256
        self._decision_rule_recheck_source_core_id = raw_recheck_source_core_id
        self._decision_rule_recheck_pending_codes = set(raw_recheck_pending_codes)
        self._decision_rule_recheck_last_attempted_codes = tuple(
            effective_recheck_attempted_codes
        )
        self._decision_rule_recheck_last_deferred_codes = tuple(
            effective_recheck_deferred_codes
        )
        self._decision_rule_recheck_last_errors = tuple(
            copy.deepcopy(dict(value)) for value in effective_recheck_errors
        )

    def _persist_priority_monitor_state(self) -> None:
        if not self._config.priority_monitoring_enabled:
            return
        with self._priority_monitor_persist_lock:
            self._persist_priority_monitor_state_serialized()

    def _persist_priority_monitor_state_serialized(self) -> None:
        """Persist after obtaining the in-process capture/write ordering lock."""

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
            candidate_symbol_exclusions = tuple(
                copy.deepcopy(value)
                for _, value in sorted(
                    self._candidate_monitor_symbol_exclusions.items()
                )
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
            last_deferred_codes = tuple(self._candidate_monitor_last_deferred_codes)
            sector_source_mode = self._priority_monitor_sector_source_mode
            sector_as_of = self._priority_monitor_sector_as_of
            sector_coverage_epoch_id = self._priority_monitor_sector_coverage_epoch_id
            recheck_source_sha256 = self._decision_rule_recheck_source_snapshot_sha256
            recheck_source_core_id = self._decision_rule_recheck_source_core_id
            recheck_pending_codes = tuple(
                sorted(self._decision_rule_recheck_pending_codes)
            )
            recheck_last_attempted_codes = tuple(
                self._decision_rule_recheck_last_attempted_codes
            )
            recheck_last_deferred_codes = tuple(
                self._decision_rule_recheck_last_deferred_codes
            )
            recheck_last_errors = tuple(
                copy.deepcopy(value)
                for value in self._decision_rule_recheck_last_errors
            )
        admitted_universe_codes = tuple(
            dict.fromkeys(
                (
                    *five_universe,
                    *last_codes,
                    *last_five_codes,
                    *last_deferred_codes,
                    *five_last_success_at,
                    *code_observations,
                    *signal_codes.values(),
                    *(
                        str(value["code"])
                        for value in candidate_symbol_exclusions
                    ),
                    *(
                        code
                        for error in (*last_errors, *candidate_last_errors)
                        for code in _explicit_strategy_subject_codes(error)
                    ),
                    *thirty_universe,
                    *last_thirty_codes,
                    *thirty_last_success_at,
                    *recheck_pending_codes,
                    *recheck_last_attempted_codes,
                    *recheck_last_deferred_codes,
                    *(
                        code
                        for error in recheck_last_errors
                        for code in _explicit_strategy_subject_codes(error)
                    ),
                )
            )
        )
        payload: dict[str, object] = {
            "schema": PRIORITY_MONITOR_SCHEMA,
            "candidate_monitor_contract_id": CANDIDATE_MONITOR_CONTRACT_ID,
            "screening_policy_id": _screening_policy_id(),
            "screening_scope_mode": self._config.screening_scope_mode,
            "effective_monitor_universe_limit": (
                self._config.effective_monitor_universe_limit
            ),
            "admitted_universe_codes": list(admitted_universe_codes),
            "decision_core_id": self._decision_core_id,
            "decision_source_snapshot_id": self._decision_source_snapshot_id,
            "selection_research_revision": self._selection_research_revision,
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
            "candidate_symbol_exclusions": [
                copy.deepcopy(value) for value in candidate_symbol_exclusions
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
            "last_deferred_candidate_codes": list(last_deferred_codes),
            "sector_source_mode": sector_source_mode,
            "sector_as_of": (
                None if sector_as_of is None else sector_as_of.isoformat()
            ),
            "sector_coverage_epoch_id": sector_coverage_epoch_id,
            "decision_rule_recheck_source_snapshot_sha256": (recheck_source_sha256),
            "decision_rule_recheck_source_core_id": recheck_source_core_id,
            "decision_rule_recheck_pending_codes": list(recheck_pending_codes),
            "decision_rule_recheck_last_attempted_codes": list(
                recheck_last_attempted_codes
            ),
            "decision_rule_recheck_last_deferred_codes": list(
                recheck_last_deferred_codes
            ),
            "decision_rule_recheck_last_errors": [
                copy.deepcopy(value) for value in recheck_last_errors
            ],
            "read_only": True,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }
        payload["content_sha256"] = self._priority_monitor_state_sha256(payload)
        path = self._priority_monitor_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with interprocess_file_lock(lock_path, timeout_seconds=10.0):
            _remove_orphan_atomic_temporaries(path)
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
            or not _priority_monitor_compute_window_open(observed_at)
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

    def _startup_priority_bootstrap_required(
        self,
    ) -> bool:
        """全量快照不可用时先用当前进程复核显式监听标的。"""

        with self._background_lock:
            runtime_verified = self._priority_monitor_runtime_verified
            locator_verification_required = bool(
                self._priority_monitor_locator_last_scheduled_count > 0
            )
            locator_runtime_verified = (
                self._priority_monitor_locator_runtime_verified
            )
        return bool(
            self._config.priority_monitoring_enabled
            and (
                not runtime_verified
                or (
                    locator_verification_required
                    and not locator_runtime_verified
                )
            )
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
        candidate_symbol_exclusions: tuple[dict[str, object], ...] | None = None,
        five_universe: tuple[str, ...] | None = None,
        thirty_universe: tuple[str, ...] | None = None,
        five_codes: tuple[str, ...] = (),
        thirty_codes: tuple[str, ...] = (),
        successful_five_codes: tuple[str, ...] = (),
        successful_thirty_codes: tuple[str, ...] = (),
        deferred_candidate_codes: tuple[str, ...] = (),
        decision_rule_recheck_attempted_codes: tuple[str, ...] | None = None,
        decision_rule_recheck_deferred_codes: tuple[str, ...] | None = None,
        decision_rule_recheck_errors: tuple[dict[str, object], ...] | None = None,
        locator_elapsed_seconds: float | None = None,
        locator_scheduled_count: int | None = None,
        locator_attempted_count: int | None = None,
        locator_completed_count: int | None = None,
        locator_runtime_verified: bool | None = None,
        round_complete: bool = True,
        round_failed: bool = False,
    ) -> None:
        """发布精简监听状态，不修改覆盖状态。"""

        if round_complete and round_failed:
            raise ValueError("priority monitor round cannot complete and fail together")
        lane_map = dict(lanes_by_code or {})
        if any(value not in _CANDIDATE_MONITOR_LANES for value in lane_map.values()):
            raise ValueError("candidate monitor lane is invalid")
        symbol_exclusion_map: dict[str, dict[str, object]] | None = None
        if candidate_symbol_exclusions is not None:
            if any(
                not _candidate_monitor_symbol_exclusion_is_valid(value)
                for value in candidate_symbol_exclusions
            ):
                raise ValueError("candidate symbol exclusion is invalid")
            symbol_exclusion_map = {
                str(value["code"]): copy.deepcopy(value)
                for value in candidate_symbol_exclusions
            }
            if len(symbol_exclusion_map) != len(candidate_symbol_exclusions):
                raise ValueError("candidate symbol exclusion codes must be unique")
        locator_metrics = (
            locator_elapsed_seconds,
            locator_scheduled_count,
            locator_attempted_count,
            locator_completed_count,
            locator_runtime_verified,
        )
        if any(value is not None for value in locator_metrics):
            if any(value is None for value in locator_metrics):
                raise ValueError("priority locator metrics must be supplied together")
            if (
                not isinstance(locator_elapsed_seconds, (int, float))
                or isinstance(locator_elapsed_seconds, bool)
                or locator_elapsed_seconds < 0
                or any(
                    type(value) is not int or value < 0
                    for value in (
                        locator_scheduled_count,
                        locator_attempted_count,
                        locator_completed_count,
                    )
                )
                or type(locator_runtime_verified) is not bool
                or locator_attempted_count > locator_scheduled_count
                or locator_completed_count > locator_attempted_count
            ):
                raise ValueError("priority locator metrics are invalid")
        compact_documents = None
        if documents is not None:
            compact_documents = tuple(
                {
                    **_priority_monitor_continuation_document(document),
                    "observation_lane": _CANDIDATE_MONITOR_PRESENTATION_LANES[
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
            self._decision_rule_recheck_pending_codes.difference_update(
                successful_codes
            )
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
                self._candidate_monitor_last_deferred_codes = tuple(
                    deferred_candidate_codes
                )
                self._candidate_monitor_last_errors = tuple(
                    copy.deepcopy(value) for value in candidate_errors
                )
            if symbol_exclusion_map is not None:
                self._candidate_monitor_symbol_exclusions = symbol_exclusion_map
                symbol_exclusion_codes = set(symbol_exclusion_map)
                for signal_id, document in tuple(
                    self._priority_monitor_latest_documents.items()
                ):
                    if document.get("code") not in symbol_exclusion_codes:
                        continue
                    self._priority_monitor_latest_documents.pop(signal_id, None)
                    self._priority_monitor_signal_stages.pop(signal_id, None)
                    self._priority_monitor_signal_codes.pop(signal_id, None)
                # An audited per-symbol rejection is also an authoritative
                # tombstone: it must suppress a previously published live result
                # until a new market-data epoch is successfully evaluated.
                for code, exclusion in symbol_exclusion_map.items():
                    self._priority_monitor_code_observations[code] = (
                        observed_at,
                        str(exclusion["observation_lane"]),
                    )
            if decision_rule_recheck_attempted_codes is not None:
                self._decision_rule_recheck_last_attempted_codes = tuple(
                    decision_rule_recheck_attempted_codes
                )
            if decision_rule_recheck_deferred_codes is not None:
                self._decision_rule_recheck_last_deferred_codes = tuple(
                    decision_rule_recheck_deferred_codes
                )
            if decision_rule_recheck_errors is not None:
                self._decision_rule_recheck_last_errors = tuple(
                    copy.deepcopy(value) for value in decision_rule_recheck_errors
                )
            if round_complete:
                self._priority_monitor_last_at = observed_at
                self._priority_monitor_last_codes = tuple(codes)
                self._priority_monitor_last_errors = tuple(
                    copy.deepcopy(value) for value in errors
                )
                self._priority_monitor_runtime_verified = True
            elif round_failed:
                # 未完整结束的轮次必须保持立即到期，不能因已经发布了部分标的而被
                # 误记成该分钟已完成；错误仍单独暴露给健康检查。
                self._priority_monitor_last_errors = tuple(
                    copy.deepcopy(value) for value in errors
                )
                self._priority_monitor_runtime_verified = False
                self._priority_monitor_locator_runtime_verified = False
            if locator_elapsed_seconds is not None:
                self._priority_monitor_locator_last_observed_at = observed_at
                self._priority_monitor_locator_last_elapsed_seconds = round(
                    float(locator_elapsed_seconds),
                    6,
                )
                self._priority_monitor_locator_last_scheduled_count = int(
                    locator_scheduled_count
                )
                self._priority_monitor_locator_last_attempted_count = int(
                    locator_attempted_count
                )
                self._priority_monitor_locator_last_completed_count = int(
                    locator_completed_count
                )
                self._priority_monitor_locator_runtime_verified = bool(
                    locator_runtime_verified
                )
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
        frozen_sector_source_mode: str = "FROZEN_COVERAGE_EPOCH",
        preselection_continuity_codes: tuple[str, ...] = (),
        force_startup_bootstrap: bool = False,
    ) -> None:
        """观察当前已完成行情，但不改变冻结覆盖。

        此通道有意不构成全市场快照，也不计入覆盖率。它推进持仓、自选风险及当前有利
        QMT 板块轮换样本的人工复核生命周期通知。无论较慢的认证覆盖周期仍在处理，
        还是已经完成，该通道都保持运行；普通完整周期游标可能跨越数千标的，不能让
        持仓标的等待整轮扫描。
        """

        if type(force_startup_bootstrap) is not bool:
            raise TypeError("force_startup_bootstrap must be an exact bool")
        if frozen_sector_source_mode not in {
            "FROZEN_COVERAGE_EPOCH",
            "PRESELECTION_CONTINUITY",
        }:
            raise ValueError("frozen priority sector source mode is invalid")
        continuity_codes = _project_codes_to_configured_scope(
            tuple(
                code
                for code in dict.fromkeys(preselection_continuity_codes)
                if re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is not None
                and code not in excluded_codes
            ),
            self._config,
        )
        if not force_startup_bootstrap and not self._priority_monitor_due(observed_at):
            return
        priority_round_started_perf = time.perf_counter()
        realtime_notification_eligible = _priority_monitor_session_open(observed_at)
        candidate_lunch_catchup = _candidate_monitor_lunch_catchup_open(observed_at)
        monitor_session = observed_at.astimezone(CN).date()
        with self._background_lock:
            configured_allowlist = _configured_scope_allowlist(self._config)
            active_candidate_symbol_exclusions = {
                code: copy.deepcopy(value)
                for code, value in self._candidate_monitor_symbol_exclusions.items()
                if _candidate_monitor_symbol_exclusion_is_active(
                    value,
                    observed_at=observed_at,
                )
                and (
                    configured_allowlist is None or code in configured_allowlist
                )
            }
            # Epoch changes are the retry trigger.  Pruning here prevents a
            # deterministic individual rejection from being retried every minute
            # against the same completed 5m fact.
            self._candidate_monitor_symbol_exclusions = copy.deepcopy(
                active_candidate_symbol_exclusions
            )
            if self._candidate_monitor_suspended_session == monitor_session:
                candidate_suspended_codes = set(
                    _project_codes_to_configured_scope(
                        self._candidate_monitor_current_session_suspended_codes,
                        self._config,
                    )
                )
            else:
                candidate_suspended_codes = set()
                self._candidate_monitor_suspended_session = monitor_session
                self._candidate_monitor_current_session_suspended_codes = ()
                self._candidate_monitor_suspension_probe_status = "not_required"
                self._candidate_monitor_suspension_probe_error = None
        priority_deadline_perf = (
            priority_round_started_perf
            + self._config.priority_monitor_time_budget_seconds
        )
        if (frozen_sector_batch is None) != (frozen_sector_members is None):
            raise ValueError(
                "frozen priority sector batch and members must be supplied together"
            )
        if frozen_sector_batch is None:
            cached_provider = getattr(
                self._sector_catalog,
                "cached_sector_snapshot_for_priority",
                None,
            )
            cached_snapshot = (
                cached_provider(as_of=observed_at)
                if callable(cached_provider)
                else None
            )
            if cached_snapshot is not None and not isinstance(
                cached_snapshot,
                CachedSectorSnapshot,
            ):
                raise TypeError("priority sector cache provider returned invalid data")
            if cached_snapshot is not None:
                sector_batch: SectorAssessmentBatch | None = cached_snapshot.batch
                all_members = dict(cached_snapshot.members)
                sector_source_mode = (
                    "CURRENT_CACHED_SECTOR_SNAPSHOT"
                    if cached_snapshot.current_decision_epoch
                    else "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED"
                )
                sector_as_of = self._sector_market_data_as_of(
                    sector_batch.assessments,
                    cached_snapshot.requested_as_of,
                )
                sector_coverage_epoch_id = None
            elif callable(cached_provider) or not (
                self._config.full_coverage_refresh_enabled
            ):
                # 生产代理即使没有缓存也不能在 1m 通道中启动数分钟的全板块重建。
                # 未分类上下文仍会计算个股结构和全部卖点；买入因板块风险缺失而关闭。
                sector_batch = None
                all_members = {}
                sector_source_mode = "UNCLASSIFIED_SECTOR_FAIL_CLOSED"
                sector_as_of = observed_at
                sector_coverage_epoch_id = None
            else:
                # 仅保留给不具备只读缓存接口的进程内测试/嵌入式网关。正式隔离代理
                # 必然走上方分支，盘中不会调用原生板块生产器。
                sector_batch = self._native_sector_assessments(as_of=observed_at)
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
            # 日级预选会冻结板块排序和成员。盘中通道只监听更新后的已完成个股 K 线，
            # 不得每分钟暗中重选板块；否则既会在盘中改变决策范围，也会在观测一个
            # 优先标的前串行重建全部 QMT 板块。
            sector_batch = frozen_sector_batch
            all_members = dict(frozen_sector_members or {})
            sector_source_mode = frozen_sector_source_mode
            sector_as_of = normalize_datetime(
                frozen_sector_as_of,
                "frozen priority sector as_of",
            )
            sector_coverage_epoch_id = frozen_coverage_epoch_id
        with self._background_lock:
            self._priority_monitor_sector_source_mode = sector_source_mode
            self._priority_monitor_sector_as_of = sector_as_of
            self._priority_monitor_sector_coverage_epoch_id = sector_coverage_epoch_id
        if sector_batch is None:
            assessments: tuple[SectorAssessment, ...] = ()
            failed_sector_ids: set[str] = set()
            parent_relations: tuple[tuple[str, str], ...] = ()
        else:
            failed_sector_ids = {
                item.sector_id
                for item in (*sector_batch.errors, *sector_batch.exclusions)
            }
            assessments = tuple(sector_batch.assessments)
            parent_relations = sector_batch.parent_relations
            priority_sector_ratio = (
                sector_batch.resolution_ratio
                if parent_relations
                else sector_batch.completion_ratio
            )
            fail_closed_reason = (
                "priority_sector_snapshot_stale"
                if sector_source_mode == "STALE_CACHED_SECTOR_SNAPSHOT_FAIL_CLOSED"
                else "priority_sector_coverage_incomplete"
                if priority_sector_ratio < self._config.min_scan_completion_ratio
                else None
            )
            if fail_closed_reason is not None:
                assessments = tuple(
                    replace(
                        assessment,
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        reason_codes=tuple(
                            dict.fromkeys(
                                (*assessment.reason_codes, fail_closed_reason)
                            )
                        ),
                    )
                    for assessment in assessments
                )
        routing = _sector_member_routing(
            assessments=assessments,
            members_by_sector=all_members,
            parent_relations=parent_relations,
            unavailable_sector_ids=frozenset(failed_sector_ids),
        )
        sector_by_code = dict(routing.context_sector_by_code)
        # 即使板块不利或快照已过期，也保留真实成员上下文供卖点和风险展示使用；
        # 只有经父级门控后的当前支持性有效板块才可扩大候选发现范围。
        # Rank remains a hard policy ceiling, while the code-level admission is
        # calculated below from the configured 5m cadence capacity.  This lets
        # every genuinely supportive sector participate when the lane has room,
        # without silently promising a universe the five-minute lane cannot turn.
        live_supportive_sector_ordinals = {
            row.assessment.sector_id: row.ordinal
            for row in routing.ranked
            if row.ordinal <= self._config.supportive_discovery_max_sector_rank
            and row.assessment.regime == "supportive"
        }
        supportive_eligible_codes = _project_codes_to_configured_scope(
            tuple(
                code
                for code, assessment in sorted(
                    routing.eligible_sector_by_code.items(),
                    key=lambda item: (
                        live_supportive_sector_ordinals.get(
                            item[1].sector_id,
                            10**9,
                        ),
                        item[0],
                    ),
                )
                if assessment.sector_id in live_supportive_sector_ordinals
            ),
            self._config,
        )
        eligible_sector_codes = set(routing.eligible_sector_by_code)

        watchlist, _rejected_watchlist = _validated_monitor_instrument_scope(
            self._market_data.active_watchlist_scope(),
            "active_watchlist_scope",
        )
        holdings, _rejected_holdings = _validated_monitor_instrument_scope(
            self._market_data.holdings_scope(),
            "holdings_scope",
        )
        mandatory_scope = _require_codes_in_configured_scope(
            tuple(dict.fromkeys((*holdings, *watchlist))),
            self._config,
            subject="mandatory holdings/watchlist scope",
        )
        mandatory_codes = tuple(
            code for code in mandatory_scope if code not in excluded_codes
        )
        # Fail before the first realtime quote/status or structure request.  A
        # large manually maintained holdings/watchlist scope must never turn a
        # validation restart into an implicit broad-market run.
        mandatory_admission = admit_screening_universe(
            mandatory_codes=mandatory_codes,
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        mandatory_codes = mandatory_admission.mandatory_codes
        mandatory_code_set = set(mandatory_codes)
        candidate_cadence_epoch_excluded_codes = {
            code
            for code, value in active_candidate_symbol_exclusions.items()
            if value.get("observation_lane") != CANDIDATE_MONITOR_LANE_1M
            and code not in mandatory_code_set
        }
        candidate_locator_epoch_excluded_codes = set(
            active_candidate_symbol_exclusions
        ).difference(mandatory_code_set)
        current_session_zero_trade_codes = frozenset()
        zero_trade_quote_status = "not_required"
        zero_trade_quote_error: str | None = None
        zero_trade_quote_diagnostics: tuple[dict[str, object], ...] = ()
        current_session_suspended_codes = frozenset()
        raw_current_session_suspended_codes = frozenset()
        instrument_status_probe_status = "not_required"
        instrument_status_probe_error: str | None = None
        if realtime_notification_eligible and mandatory_codes:
            quote_provider = getattr(
                self._market_data,
                "priority_realtime_ticks",
                None,
            )
            if not callable(quote_provider):
                quote_provider = getattr(self._market_data, "realtime_ticks", None)
            if callable(quote_provider):
                zero_trade_quote_status = "failed"
                try:
                    quote_batch = quote_provider(tuple(sorted(mandatory_codes)))
                except Exception as exc:
                    # Quote evidence only explains a missing completed bar.  It
                    # is never required to calculate a signal, and any failure
                    # preserves the ordinary stale-data fail-closed path.
                    zero_trade_quote_error = f"{type(exc).__name__}: {str(exc)[:140]}"
                else:
                    current_session_zero_trade_codes = (
                        _current_session_zero_trade_codes(
                            quote_batch,
                            requested_codes=tuple(sorted(mandatory_codes)),
                        )
                    )
                    zero_trade_quote_diagnostics = _current_session_quote_diagnostics(
                        quote_batch,
                        requested_codes=tuple(sorted(mandatory_codes)),
                    )
                    zero_trade_quote_status = (
                        "verified_zero_trade"
                        if current_session_zero_trade_codes
                        else "verified_no_zero_trade"
                    )
            else:
                zero_trade_quote_status = "provider_unavailable"
            status_provider = getattr(
                self._market_data,
                "priority_current_session_instrument_statuses",
                None,
            )
            if not callable(status_provider):
                status_provider = getattr(
                    self._market_data,
                    "current_session_instrument_statuses",
                    None,
                )
            if callable(status_provider):
                instrument_status_probe_status = "failed"
                status_session = observed_at.astimezone(CN).date()
                try:
                    status_batch = status_provider(
                        tuple(sorted(mandatory_codes)),
                        session=status_session,
                    )
                except Exception as exc:
                    instrument_status_probe_error = (
                        f"{type(exc).__name__}: {str(exc)[:140]}"
                    )
                else:
                    raw_current_session_suspended_codes = (
                        _current_session_suspended_codes(
                            status_batch,
                            requested_codes=tuple(sorted(mandatory_codes)),
                            session=status_session,
                        )
                    )
                    # QMT ``InstrumentStatus`` is not a stable boolean across
                    # instrument classes: an actively trading ETF can expose a
                    # positive value.  Exclude a live symbol only when the same
                    # session quote independently proves that it has made no
                    # trade.  If either source is absent we keep calculating and
                    # let the ordinary stale-bar check fail closed.
                    current_session_suspended_codes = frozenset(
                        raw_current_session_suspended_codes.intersection(
                            current_session_zero_trade_codes
                        )
                    )
                    instrument_status_probe_status = (
                        "verified_suspended"
                        if current_session_suspended_codes
                        else "verified_no_suspension"
                    )
            else:
                instrument_status_probe_status = "provider_unavailable"
        current_session_no_bar_codes = frozenset(
            (*current_session_zero_trade_codes, *current_session_suspended_codes)
        )
        # 只有“同日状态异常 + 同日零成交”交叉确认的停牌标的才移出本轮结构调度。
        # 它仍保留在健康诊断中，但不会反复读取数日前的结构；单独一个正数状态或一份
        # 零成交报价都不足以把仍在交易的标的静默排除。
        monitorable_mandatory_codes = tuple(
            code
            for code in mandatory_codes
            if code not in current_session_suspended_codes
        )
        with self._background_lock:
            self._priority_monitor_current_session_zero_trade_codes = tuple(
                sorted(current_session_zero_trade_codes)
            )
            self._priority_monitor_zero_trade_quote_status = zero_trade_quote_status
            self._priority_monitor_zero_trade_quote_error = zero_trade_quote_error
            self._priority_monitor_zero_trade_quote_diagnostics = (
                zero_trade_quote_diagnostics
            )
            self._priority_monitor_current_session_suspended_codes = tuple(
                sorted(current_session_suspended_codes)
            )
            self._priority_monitor_instrument_status_probe_status = (
                instrument_status_probe_status
            )
            self._priority_monitor_instrument_status_probe_error = (
                instrument_status_probe_error
            )
        configured_allowlist = _configured_scope_allowlist(self._config)
        main_signal_documents = tuple(
            row
            for row in previous.get("signals", ())
            if isinstance(row, Mapping)
            and (
                configured_allowlist is None
                or row.get("code") in configured_allowlist
            )
        )
        with self._background_lock:
            monitor_signal_documents = tuple(
                copy.deepcopy(row)
                for row in self._priority_monitor_latest_documents.values()
                if configured_allowlist is None
                or row.get("code") in configured_allowlist
            )
            monitor_code_observations = {
                code: value
                for code, value in self._priority_monitor_code_observations.items()
                if configured_allowlist is None or code in configured_allowlist
            }
            decision_rule_recheck_codes = _project_codes_to_configured_scope(
                tuple(sorted(self._decision_rule_recheck_pending_codes)),
                self._config,
            )
            if configured_allowlist is not None:
                self._decision_rule_recheck_pending_codes.intersection_update(
                    configured_allowlist
                )
            previous_priority_codes = _project_codes_to_configured_scope(
                self._priority_monitor_last_codes,
                self._config,
            )
            previous_candidate_five_universe = _project_codes_to_configured_scope(
                self._candidate_monitor_five_universe,
                self._config,
            )
            candidate_five_last_success_at = dict(
                self._candidate_monitor_five_last_success_at
            )
        decision_rule_recheck_code_set = set(decision_rule_recheck_codes)
        # Continuity identifies the bounded one-off recheck queue; it is not by
        # itself evidence that every archived code still belongs to the live 5m
        # SLA.  A genuinely recurring setup is already retained independently by
        # ``signal_candidate_codes``.  Keeping all continuity rows in the formal
        # universe makes a source-only deployment inflate that universe by
        # hundreds of stale conclusions and starve current supportive discovery.
        continuity_pending_codes = tuple(
            code for code in continuity_codes if code in decision_rule_recheck_code_set
        )
        continuity_pending_code_set = set(continuity_pending_codes)
        (
            current_signal_documents,
            current_monitor_signal_documents,
            _authoritative_monitor_codes,
            superseded_monitor_codes,
        ) = _merge_authoritative_monitor_documents(
            main_signal_documents,
            monitor_signal_documents,
            monitor_code_observations,
            snapshot_market_data_as_of=previous.get("market_data_as_of"),
        )
        if superseded_monitor_codes:
            # 只回收本轮复制时仍未被更新的记录，避免并发完成的新监听结果被误删。
            with self._background_lock:
                for code in superseded_monitor_codes:
                    if self._priority_monitor_code_observations.get(
                        code
                    ) != monitor_code_observations.get(code):
                        continue
                    self._priority_monitor_code_observations.pop(code, None)
                    for signal_id, document in tuple(
                        self._priority_monitor_latest_documents.items()
                    ):
                        if document.get("code") != code:
                            continue
                        self._priority_monitor_latest_documents.pop(signal_id, None)
                        self._priority_monitor_signal_stages.pop(signal_id, None)
                        self._priority_monitor_signal_codes.pop(signal_id, None)
        # A current 5m setup remains in the recurring lane until a newer strict
        # structure replaces it.  Its original notification may be old, but a
        # later 1m locator is a distinct structural event.
        recurring_signal_documents = tuple(
            row
            for row in current_signal_documents
            if _signal_side(row) == "buy"
            and _current_five_minute_setup_requires_segment_monitor(
                row,
                observed_at,
            )
        )
        signal_candidate_codes = _priority_signal_candidate_codes(
            recurring_signal_documents,
            excluded_codes=excluded_codes,
        )
        signal_candidate_codes = tuple(
            code
            for code in signal_candidate_codes
            if code not in current_session_suspended_codes
            and code not in candidate_suspended_codes
            and code not in candidate_cadence_epoch_excluded_codes
        )
        pinned_signal_candidate_codes = _priority_signal_candidate_codes(
            tuple(
                row
                for row in recurring_signal_documents
                if lifecycle_stage_from_signal(row)
                in _ONE_MINUTE_SEGMENT_IMMEDIATE_STAGES
            ),
            excluded_codes=frozenset(
                (
                    *excluded_codes,
                    *current_session_suspended_codes,
                    *candidate_suspended_codes,
                    *candidate_cadence_epoch_excluded_codes,
                )
            ),
        )
        signal_candidate_codes = _rotating_signal_candidate_admission_order(
            signal_candidate_codes,
            pinned_codes=pinned_signal_candidate_codes,
            previous_universe=previous_candidate_five_universe,
            last_success_at=candidate_five_last_success_at,
        )
        signal_candidate_pool_count = len(signal_candidate_codes)
        five_cadence_rounds = max(
            1,
            (
                self._config.five_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
                - 1
            )
            // self._config.priority_monitor_interval_seconds,
        )
        thirty_cadence_rounds = max(
            1,
            (
                self._config.thirty_minute_candidate_target_seconds
                + self._config.priority_monitor_interval_seconds
                - 1
            )
            // self._config.priority_monitor_interval_seconds,
        )
        configured_candidate_universe_capacity = min(
            self._config.max_five_minute_candidate_symbols_per_refresh
            * five_cadence_rounds,
            self._config.max_thirty_minute_candidate_symbols_per_refresh
            * thirty_cadence_rounds,
            self._config.effective_monitor_universe_limit,
        )
        reserved_candidate_codes = set(monitorable_mandatory_codes)
        reserved_candidate_codes.update(
            code
            for code in signal_candidate_codes
            if code not in candidate_cadence_epoch_excluded_codes
        )
        eligible_supportive_codes = tuple(
            code
            for code in supportive_eligible_codes
            if code not in excluded_codes
            and code not in current_session_suspended_codes
            and code not in candidate_suspended_codes
            and code not in candidate_cadence_epoch_excluded_codes
            and code not in reserved_candidate_codes
        )
        supportive_admission_capacity = max(
            0,
            configured_candidate_universe_capacity - len(reserved_candidate_codes),
        )
        universe_admission = admit_screening_universe(
            mandatory_codes=monitorable_mandatory_codes,
            signal_codes=signal_candidate_codes,
            supportive_codes=(
                eligible_supportive_codes[:supportive_admission_capacity]
            ),
            recheck_codes=decision_rule_recheck_codes,
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        monitorable_mandatory_codes = universe_admission.mandatory_codes
        signal_candidate_codes = universe_admission.signal_codes
        admitted_signal_code_set = set(signal_candidate_codes)
        supportive_codes = list(universe_admission.supportive_codes)
        decision_rule_recheck_codes = universe_admission.recheck_codes
        with self._background_lock:
            self._candidate_monitor_supportive_eligible_count = len(
                eligible_supportive_codes
            )
            self._candidate_monitor_supportive_admitted_count = len(supportive_codes)
            self._candidate_monitor_supportive_capacity = supportive_admission_capacity
            self._candidate_monitor_signal_pool_count = signal_candidate_pool_count
            self._candidate_monitor_signal_admitted_count = len(signal_candidate_codes)
            self._candidate_monitor_signal_deferred_count = len(
                universe_admission.deferred_signal_codes
            )
            self._priority_monitor_immediate_pool_count = len(
                pinned_signal_candidate_codes
            )
            self._priority_monitor_immediate_deferred_count = len(
                set(pinned_signal_candidate_codes).difference(
                    admitted_signal_code_set,
                )
            )
        # A confirmed 5m buy setup enters the exact 1m locator lane until its
        # first causal segment-difference witness is attached.  That witness is
        # immutable; an expired execution boundary must not silently replace it
        # with a later occurrence.  Holdings and explicit watchlist symbols keep
        # their independent mandatory 1m observation lane.
        pending_segment_documents = tuple(
            row
            for row in current_signal_documents
            if _current_five_minute_setup_requires_segment_monitor(row, observed_at)
            and _one_minute_segment_requires_monitor(row, observed_at)
        )
        locator_signal_pool = _priority_signal_candidate_codes(
            pending_segment_documents,
            excluded_codes=frozenset(
                (
                    *excluded_codes,
                    *mandatory_scope,
                    *current_session_suspended_codes,
                    *candidate_suspended_codes,
                    *candidate_cadence_epoch_excluded_codes,
                    *candidate_locator_epoch_excluded_codes,
                )
            ),
            allowed_stages=_ONE_MINUTE_SEGMENT_IMMEDIATE_STAGES,
        )
        immediate_signal_universe = tuple(
            code
            for code in locator_signal_pool
            if code in admitted_signal_code_set
        )
        locator_admission_deferred_codes = tuple(
            code
            for code in locator_signal_pool
            if code not in admitted_signal_code_set
        )
        urgent_signal_universe = immediate_signal_universe
        urgent_signal_codes = _take_rotating_priority_batch(
            immediate_signal_universe,
            previous_codes=previous_priority_codes,
            max_symbols=max(
                0,
                self._config.max_monitor_symbols_per_refresh
                - len(monitorable_mandatory_codes),
            ),
        )
        selected_immediate_codes = urgent_signal_codes
        priority_worker_count = self._config.effective_priority_worker_count
        minute_codes = (
            ()
            if candidate_lunch_catchup
            else tuple(
                dict.fromkeys(
                    (
                        *_priority_affinity_striped_codes(
                            monitorable_mandatory_codes,
                            sector_by_code=sector_by_code,
                            worker_count=priority_worker_count,
                        ),
                        *_priority_affinity_striped_codes(
                            selected_immediate_codes,
                            sector_by_code=sector_by_code,
                            worker_count=priority_worker_count,
                        ),
                    )
                )
            )
        )
        optional_segment_capacity = max(
            0,
            self._config.max_monitor_symbols_per_refresh
            - len(monitorable_mandatory_codes),
        )
        configured_rotation_seconds = (
            0
            if not immediate_signal_universe
            else None
            if optional_segment_capacity <= 0
            else (
                (len(immediate_signal_universe) + optional_segment_capacity - 1)
                // optional_segment_capacity
            )
            * self._config.priority_monitor_interval_seconds
        )
        with self._background_lock:
            self._priority_monitor_mandatory_count = len(mandatory_codes)
            self._priority_monitor_immediate_universe_count = len(
                immediate_signal_universe
            )
            self._priority_monitor_tracking_universe_count = len(urgent_signal_universe)
            self._priority_monitor_scheduled_count = len(minute_codes)
            self._priority_monitor_configured_rotation_seconds = (
                configured_rotation_seconds
            )
            self._priority_monitor_locator_pool_count = len(locator_signal_pool)
            self._priority_monitor_locator_admission_deferred_count = len(
                locator_admission_deferred_codes
            )
        # 持仓、自选和当前仍有效的买卖点候选始终排在最前；冻结的支持性板块成员也必须进入
        # 5 分钟发现轮转。只在 30 分钟通道观察它们会让刚形成的正式 5m 点晚于通知
        # 新鲜窗口才被发现。候选调度仍受独立时间预算和硬容量约束，容量不足会在
        # health 中失败关闭，不能静默退回半小时发现。
        regular_five_universe = tuple(
            code
            for code in dict.fromkeys(
                (
                    *monitorable_mandatory_codes,
                    *signal_candidate_codes,
                    *supportive_codes,
                )
            )
            if code not in excluded_codes
            and code not in current_session_suspended_codes
            and code not in candidate_suspended_codes
            and code not in candidate_cadence_epoch_excluded_codes
        )
        # 规则迁移复核是一次性排空队列，不属于正式候选的五分钟 SLA。两者可以共享
        # 剩余计算容量，但覆盖分母、错误和延期状态必须完全分开。
        five_universe = regular_five_universe
        thirty_universe = tuple(
            code
            for code in dict.fromkeys((*regular_five_universe, *supportive_codes))
            if code not in current_session_suspended_codes
            and code not in candidate_suspended_codes
            and code not in candidate_cadence_epoch_excluded_codes
        )
        with self._background_lock:
            previous_monitor_at = self._priority_monitor_last_at
            five_last_success_at = dict(self._candidate_monitor_five_last_success_at)
            thirty_last_success_at = dict(
                self._candidate_monitor_thirty_last_success_at
            )
            previous_five_codes = tuple(self._candidate_monitor_last_five_codes)
        regular_five_codes = _take_due_candidate_batch(
            regular_five_universe,
            last_success_at=five_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.five_minute_candidate_target_seconds,
            monitor_interval_seconds=self._config.priority_monitor_interval_seconds,
            max_symbols=(self._config.max_five_minute_candidate_symbols_per_refresh),
            execution_grace_seconds=(
                self._config.candidate_monitor_time_budget_seconds
            ),
            excluded_codes=excluded_codes,
            previous_monitor_at=previous_monitor_at,
        )
        # 规则迁移积压只使用正式候选完成后的一个物理波次。它不承担盘中 SLA，
        # 不能把数百个代码一次塞进 50 秒分钟预算并反复回收原生进程；完整覆盖会
        # 在当前核心快照发布后权威清空已经处理的积压。未开启完整覆盖时，该队列
        # 仍按固定物理容量逐分钟推进，不会饿死。
        rule_recheck_batch_limit = min(
            self._config.max_five_minute_candidate_symbols_per_refresh,
            len(set(regular_five_codes)) + max(1, self._config.stock_worker_count - 1),
        )
        rule_recheck_codes = _take_rule_recheck_batch(
            decision_rule_recheck_codes,
            scheduled_codes=regular_five_codes,
            previous_codes=previous_five_codes,
            max_symbols=rule_recheck_batch_limit,
        )
        five_codes = tuple(dict.fromkeys((*regular_five_codes, *rule_recheck_codes)))
        scheduled_thirty_codes = _take_due_candidate_batch(
            thirty_universe,
            last_success_at=thirty_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.thirty_minute_candidate_target_seconds,
            monitor_interval_seconds=self._config.priority_monitor_interval_seconds,
            max_symbols=(self._config.max_thirty_minute_candidate_symbols_per_refresh),
            execution_grace_seconds=(
                self._config.candidate_monitor_time_budget_seconds
            ),
            excluded_codes=excluded_codes,
            previous_monitor_at=previous_monitor_at,
        )
        # A cold process already pays for each due formal 5m observation.  Merge
        # its missing 30m cadence fact into the same native request instead of
        # putting that symbol at the back of a second queue for up to 30 minutes.
        # The native bundle prunes 30m work when there is no current 5m setup;
        # when a setup exists, this also guarantees that its 30m context is fresh.
        # Keep the configured 30m hard bound and preserve independently overdue
        # 30m work ahead of opportunistic cold-start coalescing.
        thirty_universe_set = set(thirty_universe)
        cold_start_thirty_codes = tuple(
            code
            for code in regular_five_codes
            if code in thirty_universe_set and code not in thirty_last_success_at
        )
        thirty_codes = tuple(
            dict.fromkeys((*scheduled_thirty_codes, *cold_start_thirty_codes))
        )[: self._config.max_thirty_minute_candidate_symbols_per_refresh]
        frequencies_by_code: dict[str, set[str]] = {}
        for code in minute_codes:
            # 当前 1m 段差只有绑定最新已完成 5m 正式点才有意义；二者同时刷新可
            # 防止过期的 5m 缓存结构跨越五分钟边界继续生效。
            frequencies_by_code.setdefault(code, set()).update(("5m", "1m"))
        for code in five_codes:
            frequencies_by_code.setdefault(code, set()).add("5m")
        for code in thirty_codes:
            frequencies_by_code.setdefault(code, set()).update(("5m", "30m"))
        # 正式 5m/30m 候选必须先于一次性规则复核进入候选通道。规则复核批次可能接近
        # 上限；若把它插在 30m 候选之前，后者会在每轮预算耗尽时永久饥饿，健康状态
        # 也会持续误报正式候选延期。重叠标的仍通过去重合并全部所需频率。
        codes = tuple(
            dict.fromkeys(
                (
                    *minute_codes,
                    *regular_five_codes,
                    *thirty_codes,
                    *rule_recheck_codes,
                )
            )
        )
        minute_code_set = set(minute_codes)
        five_code_set = set(five_codes)
        five_universe_set = set(regular_five_universe)
        regular_candidate_scope_set = five_universe_set | thirty_universe_set
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
                candidate_symbol_exclusions=tuple(
                    active_candidate_symbol_exclusions[code]
                    for code in sorted(active_candidate_symbol_exclusions)
                ),
                five_universe=five_universe,
                thirty_universe=thirty_universe,
                locator_elapsed_seconds=0.0,
                locator_scheduled_count=0,
                locator_attempted_count=0,
                locator_completed_count=0,
                locator_runtime_verified=False,
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
            for row in current_signal_documents
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
            for row in current_signal_documents
            if isinstance(row.get("signal_id"), str)
            and isinstance(row.get("code"), str)
        }

        def selection_sources_for(code: str) -> tuple[str, ...]:
            sources: list[str] = []
            if code in supportive_code_set:
                sources.append("QMT_SECTOR_TRIGGER")
            elif code in eligible_sector_codes:
                sources.append("QMT_SECTOR_ELIGIBLE_SCOPE")
            if code in watchlist_codes:
                sources.append("ACTIVE_WATCHLIST_MONITOR")
            if code in holding_codes:
                sources.append("MANUAL_ATTENTION_MONITOR")
            if code in decision_rule_recheck_code_set:
                sources.append("DECISION_RULE_RECHECK")
            if code in continuity_pending_code_set:
                sources.append("PRESELECTION_CONTINUITY_RECHECK")
            if code in signal_candidate_codes and not sources:
                sources.append("PREVIOUS_SIGNAL_MONITOR")
            return tuple(sources or ("INCREMENTAL_SCAN_SCOPE",))

        def evaluate(code: str, *, work_lane_override: str | None = None):
            sector = sector_by_code.get(
                code,
                SectorAssessment(
                    sector_id="unclassified",
                    sector_name="未匹配 QMT GICS3/GICS4 行业",
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_membership_missing",),
                ),
            )
            try:
                requested_frequencies = tuple(
                    frequency
                    for frequency in SCREENING_STRUCTURE_FREQUENCIES
                    if frequency in frequencies_by_code[code]
                )
                bundle = self._structure_bundle_with_causal_risk(
                    code,
                    as_of=observed_at,
                    sector=sector,
                    frequencies=requested_frequencies,
                    risk_evidence_cutoff=sector_as_of,
                    deadline_monotonic=(
                        priority_deadline_perf
                        if code in minute_code_set
                        else candidate_deadline_perf
                    ),
                    work_lane=(
                        work_lane_override
                        or ("priority" if code in minute_code_set else "candidate")
                    ),
                )
                bundle = replace(
                    bundle,
                    selection_sources=selection_sources_for(code),
                    selection_research=visible_selection_research(
                        self._selection_research,
                        symbol=code,
                        selection_path=bundle.selection_path,
                        decision_time=bundle.as_of,
                    ),
                )
                previous_lifecycles, previous_triggers = (
                    _previous_lifecycle_bundle_state(
                        previous_documents_by_id.values(),
                        code=code,
                        as_of=bundle.as_of,
                        decision_core_id=self._decision_core_id,
                    )
                )
                bundle = replace(
                    bundle,
                    previous_lifecycles=previous_lifecycles,
                    previous_trigger_points=previous_triggers,
                )
                bundle_is_current = _structure_bundle_is_current_for_intraday_evidence(
                    bundle,
                    observed_at=observed_at,
                    max_age_seconds=self._config.max_structure_age_seconds,
                    requested_frequencies=requested_frequencies,
                )
                if not bundle_is_current and code in current_session_no_bar_codes:
                    bundle_is_current = (
                        _structure_bundle_is_current_for_zero_trade_intraday_evidence(
                            bundle,
                            observed_at=observed_at,
                            max_age_seconds=self._config.max_structure_age_seconds,
                            requested_frequencies=requested_frequencies,
                        )
                    )
                if not bundle_is_current:
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
                        current_price=bundle.latest_price,
                        decision_core_id=self._decision_core_id,
                        selection_sources=bundle.selection_sources,
                        formal_selection_required=(self._formal_selection_required),
                        higher_timeframe_gates=bundle.higher_timeframe_gates,
                    )
                    for item in evaluated
                )
                return code, documents, None
            except Exception as exc:
                return code, (), exc

        priority_preparation_errors: list[dict[str, object]] = []
        candidate_preparation_errors: list[dict[str, object]] = []
        decision_rule_recheck_preparation_errors: list[dict[str, object]] = []
        # 1分钟区间套不参与5分钟主信号成立，却是精确执行的必要证据。容量
        # 故障不能抹掉5分钟信号或首报链路，但必须关闭精确执行并在健康状态中
        # 明确失败；静默轮转到下一分钟时，原定位窗口已经没有执行意义。
        priority_capacity_insufficient = bool(
            len(monitorable_mandatory_codes)
            > self._config.max_monitor_symbols_per_refresh
        )
        if realtime_notification_eligible and priority_capacity_insufficient:
            priority_preparation_errors.append(
                {
                    "error_type": "priority_monitor_capacity_error",
                    "reason_code": "PRIORITY_MONITOR_CAPACITY_INSUFFICIENT",
                    "reason": (
                        f"tracking_universe={len(urgent_signal_universe)} "
                        f"scheduled={len(urgent_signal_codes)} "
                        f"mandatory={len(mandatory_codes)} "
                        f"rotation_seconds={configured_rotation_seconds}"
                    ),
                }
            )
        if realtime_notification_eligible and locator_admission_deferred_codes:
            priority_preparation_errors.append(
                {
                    "error_type": "priority_monitor_locator_admission_error",
                    "reason_code": (
                        "ONE_MINUTE_LOCATOR_ADMISSION_CAPACITY_INSUFFICIENT"
                    ),
                    "reason": (
                        f"locator_pool={len(locator_signal_pool)} "
                        f"admitted={len(immediate_signal_universe)} "
                        f"deferred={len(locator_admission_deferred_codes)}"
                    ),
                    "deferred_codes": list(locator_admission_deferred_codes),
                }
            )
        history_preparer = getattr(self._market_data, "prepare_local_history", None)
        candidate_deadline_perf: float | None = None

        def prepare_history(
            phase_codes: tuple[str, ...],
            *,
            phase: str,
        ) -> None:
            if not phase_codes:
                return
            try:
                bounded_preparer = None
                if phase == "priority_1m":
                    priority_preparer = getattr(
                        self._market_data,
                        "prepare_priority_local_history",
                        None,
                    )
                    prepare = (
                        priority_preparer
                        if callable(priority_preparer)
                        else history_preparer
                    )
                else:
                    bounded_preparer = getattr(
                        self._market_data,
                        "prepare_candidate_local_history_until",
                        None,
                    )
                    if not callable(bounded_preparer):
                        bounded_preparer = getattr(
                            self._market_data,
                            "prepare_local_history_until",
                            None,
                        )
                    prepare = (
                        bounded_preparer
                        if candidate_deadline_perf is not None
                        and callable(bounded_preparer)
                        else history_preparer
                    )
                if not callable(prepare):
                    return
                # 每分钟优先队列必须一次准备完整决策周期，避免持仓、观察池和待
                # 触发标的退回逐只下载。普通候选轮换则只准备必要的 5m 设置层；
                # 确认存在当前设置后，结构包才按需读取其余周期，避免大候选池
                # 无条件占用原生行情工作进程。
                frequency_requests = tuple(
                    (
                        code,
                        tuple(
                            frequency
                            for frequency in SCREENING_STRUCTURE_FREQUENCIES
                            if frequency in frequencies_by_code[code]
                            or frequency in {"d", "30m", "5m"}
                        ),
                    )
                    if phase == "priority_1m"
                    else (code, ("5m",))
                    for code in sorted(phase_codes)
                )
                kwargs: dict[str, object] = {
                    "frequency_requests": frequency_requests,
                    "as_of": observed_at,
                }
                if bounded_preparer is not None and prepare is bounded_preparer:
                    kwargs["deadline_monotonic"] = candidate_deadline_perf
                prepare(
                    **kwargs,
                )
            except Exception as exc:
                if (
                    phase != "priority_1m"
                    and candidate_deadline_perf is not None
                    and (
                        getattr(exc, "reason_code", None)
                        == "CANDIDATE_MONITOR_TIME_BUDGET_EXHAUSTED"
                        or time.perf_counter() >= candidate_deadline_perf
                    )
                ):
                    return
                # 批量准备只是传输优化。失败后各结构请求会沿用原有逐只下载路径；
                # 仍保留明确告警，避免健康页在 SLA 已退化时误报容量充足。
                error = {
                    "error_type": "priority_monitor_history_preparation_error",
                    "reason_code": "PRIORITY_MONITOR_BATCH_HISTORY_PREPARATION_FAILED",
                    "reason": (f"phase={phase} {type(exc).__name__}: {str(exc)[:140]}"),
                }
                if phase == "priority_1m":
                    priority_preparation_errors.append(error)
                elif any(code in regular_candidate_scope_set for code in phase_codes):
                    candidate_preparation_errors.append(error)
                else:
                    decision_rule_recheck_preparation_errors.append(error)

        results: list[tuple[str, tuple[dict[str, object], ...], Exception | None]] = []
        results_lock = Lock()
        phase_metrics: dict[str, tuple[int, float]] = {}
        notification_dispatch_lock = Lock()
        partial_results: list[
            tuple[str, tuple[dict[str, object], ...], Exception | None]
        ] = []
        candidate_notification_results: list[
            tuple[str, tuple[dict[str, object], ...], Exception | None]
        ] = []
        candidate_notification_published_codes: set[str] = set()
        partial_state_persisted = False
        successful_since_partial_persist = 0

        def publish_completed_minute_results() -> None:
            nonlocal partial_state_persisted, successful_since_partial_persist
            if not partial_results:
                return
            batch = tuple(partial_results)
            partial_results.clear()
            successful = tuple(code for code, _rows, exc in batch if exc is None)
            if not successful:
                return
            batch_documents = tuple(
                document
                for _code, rows, exc in batch
                if exc is None
                for document in rows
            )
            self._record_priority_monitor_result(
                observed_at=observed_at,
                codes=(),
                errors=(),
                documents=batch_documents,
                successful_codes=successful,
                lanes_by_code={code: lanes_by_code[code] for code in successful},
                round_complete=False,
            )
            successful_since_partial_persist += len(successful)
            # 先让事件进入分发器的持久去重/待发送队列，再推进增量监听检查点。
            # 若进程在两步之间退出，重启后最多重放一次并由事件身份去重；反向
            # 顺序会让新段差已经写入监听状态、却永远失去“首次附着”通知。
            if self._notifier is not None and realtime_notification_eligible:
                successful_set = set(successful)
                partial_previous = {
                    "signals": [
                        document
                        for signal_id, document in sorted(
                            previous_documents_by_id.items()
                        )
                        if signal_id in prior_stages
                        and document.get("code") in successful_set
                    ]
                }
                partial_current = {
                    "signals": list(batch_documents),
                    "notification_authoritative_codes": list(successful),
                }
                try:
                    with notification_dispatch_lock:
                        self._notifier.dispatch_changes(
                            partial_previous,
                            partial_current,
                        )
                except Exception as exc:
                    # 整轮末尾仍会用同一轮开始时的前态重试；检查点继续落盘供
                    # 页面展示，通知分发器自身负责持久化已受理但尚未送达的事件。
                    errors_during_publish.append(
                        {
                            "error_type": "priority_monitor_notification_error",
                            "reason_code": "PRIORITY_MONITOR_NOTIFICATION_FAILED",
                            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                        }
                    )
            if (
                not partial_state_persisted
                or successful_since_partial_persist
                >= PRIORITY_MONITOR_PERSIST_BATCH_SIZE
            ):
                try:
                    self._persist_priority_monitor_state()
                except Exception as exc:
                    # 分发器有独立的幂等事件账本；实时状态文件瞬时失败时仍应继续
                    # 通知本批标的，整轮结尾会再次尝试原子落盘。
                    errors_during_publish.append(
                        {
                            "error_type": "priority_monitor_state_persistence_error",
                            "reason_code": "PRIORITY_MONITOR_STATE_PERSISTENCE_FAILED",
                            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                        }
                    )
                else:
                    partial_state_persisted = True
                    successful_since_partial_persist = 0

        def publish_completed_candidate_results() -> None:
            """Hand completed 5m discovery to notifications before the slow tail.

            The final round publication remains authoritative for cadence and
            presentation state.  This earlier handoff only crosses the durable
            notification boundary; the dispatcher's semantic event identity
            makes the final retry idempotent.
            """

            if not candidate_notification_results:
                return
            batch = tuple(candidate_notification_results)
            candidate_notification_results.clear()
            successful = tuple(
                code
                for code, _rows, exc in batch
                if exc is None
                and code in regular_candidate_scope_set
                and "5m" in frequencies_by_code[code]
            )
            if (
                not successful
                or self._notifier is None
                or not realtime_notification_eligible
            ):
                return
            successful_set = set(successful)
            batch_documents = tuple(
                document
                for code, rows, exc in batch
                if exc is None and code in successful_set
                for document in rows
            )
            partial_previous = {
                "signals": [
                    document
                    for signal_id, document in sorted(
                        previous_documents_by_id.items()
                    )
                    if signal_id in prior_stages
                    and document.get("code") in successful_set
                ]
            }
            partial_current = {
                "signals": list(batch_documents),
                "notification_authoritative_codes": list(successful),
            }
            try:
                with notification_dispatch_lock:
                    self._notifier.dispatch_changes(
                        partial_previous,
                        partial_current,
                    )
                candidate_notification_published_codes.update(successful)
            except Exception as exc:
                with results_lock:
                    errors_during_publish.append(
                        {
                            "error_type": "priority_monitor_notification_error",
                            "reason_code": "PRIORITY_MONITOR_NOTIFICATION_FAILED",
                            "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                        }
                    )

        errors_during_publish: list[dict[str, object]] = []

        def consume_result(
            result: tuple[
                str,
                tuple[dict[str, object], ...],
                Exception | None,
            ],
        ) -> None:
            with results_lock:
                results.append(result)
            if result[0] in minute_code_set:
                partial_results.append(result)
                if len(partial_results) >= PRIORITY_MONITOR_PUBLISH_BATCH_SIZE:
                    publish_completed_minute_results()
                return
            if (
                result[2] is None
                and result[0] in regular_candidate_scope_set
                and "5m" in frequencies_by_code[result[0]]
            ):
                candidate_notification_results.append(result)
                if (
                    len(candidate_notification_results)
                    >= CANDIDATE_NOTIFICATION_PUBLISH_BATCH_SIZE
                ):
                    publish_completed_candidate_results()

        def evaluate_phase(
            phase_codes: tuple[str, ...],
            *,
            phase: str,
            admission_deadline_perf: float | None = None,
        ) -> tuple[str, ...]:
            phase_started_perf = time.perf_counter()
            if not phase_codes:
                with results_lock:
                    phase_metrics[phase] = (0, 0.0)
                return ()
            attempted: list[str] = []
            start = 0
            previous_wave_elapsed_seconds: float | None = None
            while start < len(phase_codes):
                phase_budget_seconds = (
                    self._config.priority_monitor_time_budget_seconds
                    if phase == "priority_1m"
                    else self._config.candidate_monitor_time_budget_seconds
                )
                admission_guard_seconds = min(
                    MONITOR_ADMISSION_MIN_GUARD_SECONDS,
                    phase_budget_seconds / 10,
                )
                if previous_wave_elapsed_seconds is not None:
                    admission_guard_seconds = max(
                        admission_guard_seconds,
                        previous_wave_elapsed_seconds * 1.25,
                    )
                if admission_deadline_perf is not None and (
                    time.perf_counter() >= admission_deadline_perf
                    or (
                        attempted
                        and admission_deadline_perf - time.perf_counter()
                        <= admission_guard_seconds
                    )
                ):
                    # 首波用于证明本轮监听链路；后续波次必须给原生结构计算留下
                    # 安全窗口。否则在绝对截止点前接纳一个不可能完成的新波次，
                    # 会回收仍可复用的增量工作进程，并把后续每分钟都变成冷启动。
                    break
                phase_worker_limit = (
                    self._config.effective_priority_worker_count
                    if phase == "priority_1m"
                    else self._config.effective_candidate_worker_count
                )
                wave_size = (
                    len(phase_codes) - start
                    if admission_deadline_perf is None
                    else min(phase_worker_limit, len(phase_codes) - start)
                )
                wave = phase_codes[start : start + wave_size]
                start += len(wave)
                wave_started_perf = time.perf_counter()
                prepare_history(wave, phase=phase)
                if (
                    admission_deadline_perf is not None
                    and time.perf_counter() >= admission_deadline_perf
                ):
                    break
                attempted.extend(wave)
                worker_count = min(phase_worker_limit, len(wave))
                if worker_count == 1:
                    for code in wave:
                        consume_result(evaluate(code))
                    previous_wave_elapsed_seconds = (
                        time.perf_counter() - wave_started_perf
                    )
                    continue
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix=(
                        "TradingPriority1m"
                        if phase == "priority_1m"
                        else "TradingCandidateCadence"
                    ),
                ) as executor:
                    futures = {
                        executor.submit(
                            evaluate,
                            code,
                        ): code
                        for code in wave
                    }
                    for future in as_completed(futures):
                        consume_result(future.result())
                previous_wave_elapsed_seconds = time.perf_counter() - wave_started_perf
            elapsed_seconds = max(0.0, time.perf_counter() - phase_started_perf)
            with results_lock:
                phase_metrics[phase] = (len(attempted), elapsed_seconds)
            return tuple(attempted)

        # Preserve lane precedence (formal 5m, then 30m, then one-off rule
        # rechecks), group each lane by its authenticated sector, and then stripe
        # the groups across the candidate shards.  This keeps the shared sector
        # build local while allowing the formal 5m lane to use every non-priority
        # worker.
        candidate_codes = _priority_affinity_striped_codes(
            tuple(
                dict.fromkeys(
                    code
                    for phase_codes in (
                        regular_five_codes,
                        thirty_codes,
                        rule_recheck_codes,
                    )
                    for code in _group_candidate_batch_by_sector(
                        tuple(
                            value
                            for value in phase_codes
                            if value not in minute_code_set
                        ),
                        sector_by_code=sector_by_code,
                    )
                )
            ),
            sector_by_code=sector_by_code,
            worker_count=self._config.effective_candidate_worker_count,
            candidate_symbol_striping=True,
        )
        regular_candidate_code_set = (
            set(regular_five_codes) | set(thirty_codes)
        ).difference(minute_code_set)
        scheduled_recheck_code_set = set(decision_rule_recheck_codes).intersection(
            codes
        )
        # 低频轮换按结构工作进程数分波，只在本分钟预算内接纳新波次。达到绝对
        # 截止点时只回收对应隔离分片；其余代码保持到期并在下一轮继续，且绝不能
        # 被记成已观察。上方物理波次上限确保一次性规则积压不会常态触碰该边界。
        # Candidate preparation happens after sector routing, suspension probes,
        # cadence selection and other priority-only work.  Charging that setup
        # time to the candidate lane can leave the workers with only a fraction
        # of their configured budget and repeatedly abort their first cold
        # request.  Start the lane budget at admission time; the phase loop still
        # refuses new waves at this deadline, while an already admitted native
        # request has its own bounded execution deadline in the process gateway.
        candidate_deadline_perf = (
            time.perf_counter()
            + self._config.candidate_monitor_time_budget_seconds
        )
        # 1m 与普通候选始终绑定到互不重叠的原生分片并行。优先分片保留其 1m
        # 递归状态直至下一轮，避免候选大工作集污染缓存并触发回收后的冷重建。
        if candidate_codes and minute_codes:
            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="TradingCandidateCoordinator",
            ) as coordinator:
                candidate_future = coordinator.submit(
                    evaluate_phase,
                    candidate_codes,
                    phase="candidate_cadence",
                    admission_deadline_perf=candidate_deadline_perf,
                )
                attempted_minute_codes = evaluate_phase(
                    minute_codes,
                    phase="priority_1m",
                    admission_deadline_perf=priority_deadline_perf,
                )
                publish_completed_minute_results()
                attempted_candidate_codes = candidate_future.result()
                publish_completed_candidate_results()
        else:
            attempted_minute_codes = evaluate_phase(
                minute_codes,
                phase="priority_1m",
                admission_deadline_perf=priority_deadline_perf,
            )
            publish_completed_minute_results()
            attempted_candidate_codes = evaluate_phase(
                candidate_codes,
                phase="candidate_cadence",
                admission_deadline_perf=candidate_deadline_perf,
            )
            publish_completed_candidate_results()
        attempted_minute_code_set = set(attempted_minute_codes)
        configured_locator_deferred_codes = tuple(
            code
            for code in immediate_signal_universe
            if code not in set(selected_immediate_codes)
        )
        deadline_locator_deferred_codes = tuple(
            code
            for code in selected_immediate_codes
            if code not in attempted_minute_code_set
        )
        if realtime_notification_eligible and configured_locator_deferred_codes:
            priority_preparation_errors.append(
                {
                    "error_type": "priority_monitor_locator_capacity_error",
                    "reason_code": (
                        "ONE_MINUTE_LOCATOR_CONFIGURED_CAPACITY_INSUFFICIENT"
                    ),
                    "reason": (
                        f"locator_universe={len(immediate_signal_universe)} "
                        f"selected={len(selected_immediate_codes)} "
                        f"deferred={len(configured_locator_deferred_codes)} "
                        f"rotation_seconds={configured_rotation_seconds} "
                        f"sla_seconds={ONE_MINUTE_LOCATOR_SLA_SECONDS}"
                    ),
                    "deferred_codes": list(configured_locator_deferred_codes),
                }
            )
        if realtime_notification_eligible and deadline_locator_deferred_codes:
            priority_preparation_errors.append(
                {
                    "error_type": "priority_monitor_locator_time_budget_error",
                    "reason_code": "ONE_MINUTE_LOCATOR_TIME_BUDGET_EXHAUSTED",
                    "reason": (
                        f"selected={len(selected_immediate_codes)} "
                        f"attempted={len(selected_immediate_codes) - len(deadline_locator_deferred_codes)} "
                        f"deferred={len(deadline_locator_deferred_codes)} "
                        f"budget_seconds={self._config.priority_monitor_time_budget_seconds} "
                        f"sla_seconds={ONE_MINUTE_LOCATOR_SLA_SECONDS}"
                    ),
                    "deferred_codes": list(deadline_locator_deferred_codes),
                }
            )
        deferred_mandatory_codes = tuple(
            code
            for code in monitorable_mandatory_codes
            if code not in attempted_minute_code_set
        )
        if realtime_notification_eligible and deferred_mandatory_codes:
            priority_preparation_errors.append(
                {
                    "error_type": "priority_monitor_time_budget_error",
                    "reason_code": "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED",
                    "reason": (
                        f"mandatory={len(mandatory_codes)} "
                        f"monitorable_mandatory={len(monitorable_mandatory_codes)} "
                        f"attempted_mandatory="
                        f"{len(monitorable_mandatory_codes) - len(deferred_mandatory_codes)} "
                        f"deferred_mandatory={len(deferred_mandatory_codes)}"
                    ),
                }
            )
        results.sort(key=lambda value: value[0])
        deadline_deferred_codes = {
            code
            for code, _rows, exc in results
            if code not in minute_code_set
            and exc is not None
            and getattr(exc, "reason_code", None)
            == "CANDIDATE_MONITOR_TIME_BUDGET_EXHAUSTED"
        }
        attempted_candidate_set = set(attempted_candidate_codes).difference(
            deadline_deferred_codes
        )
        all_deferred_codes = tuple(
            code for code in candidate_codes if code not in attempted_candidate_set
        )
        deferred_candidate_codes = tuple(
            code for code in all_deferred_codes if code in regular_candidate_code_set
        )
        # 迁移健康状态展示完整未接纳积压，而不只展示本轮物理波次中的延期代码。
        # 已实际返回（成功或确定性失败）的代码属于“已尝试”；绝对截止超时以及尚未
        # 接纳的代码继续列为延期，便于运维直接看见真实剩余量。
        completed_recheck_attempts = {
            code
            for code, _rows, _exc in results
            if code in decision_rule_recheck_codes
            and code not in deadline_deferred_codes
        }
        deferred_recheck_codes = tuple(
            code
            for code in decision_rule_recheck_codes
            if code not in completed_recheck_attempts
        )

        documents: list[dict[str, object]] = []
        errors: list[dict[str, object]] = [
            *errors_during_publish,
            *priority_preparation_errors,
        ]
        candidate_errors: list[dict[str, object]] = list(candidate_preparation_errors)
        minute_stock_errors: list[dict[str, object]] = []
        decision_rule_recheck_errors: list[dict[str, object]] = list(
            decision_rule_recheck_preparation_errors
        )
        for code, rows, exc in results:
            if exc is None:
                documents.extend(rows)
                continue
            error = _stock_analysis_error_document(code, exc)
            if code in minute_code_set:
                minute_stock_errors.append(error)
            if code in minute_code_set and code in mandatory_code_set:
                errors.append(error)
            elif code in minute_code_set:
                # 1m is mandatory for precise execution, but independent from
                # 5m signal formation and its initial alert.  A fetch failure
                # closes precise execution and remains visible in diagnostics;
                # it cannot erase the 5m signal/notification lane itself.
                candidate_errors.append(error)
            elif code in deadline_deferred_codes:
                continue
            elif (
                code in scheduled_recheck_code_set
                and code not in regular_candidate_code_set
            ):
                decision_rule_recheck_errors.append(error)
            else:
                candidate_errors.append(error)
        candidate_stale_codes = tuple(
            sorted(
                {
                    str(error["code"])
                    for error in candidate_errors
                    if error.get("reason_code") == "STRUCTURE_BUNDLE_STALE"
                    and error.get("deterministic_for_coverage_epoch") is True
                    and isinstance(error.get("code"), str)
                    and error["code"] in regular_candidate_scope_set
                }
            )
        )
        candidate_suspension_probe_status = "not_required"
        candidate_suspension_probe_error: str | None = None
        verified_candidate_suspended_codes: frozenset[str] = frozenset()
        if realtime_notification_eligible and candidate_stale_codes:
            candidate_quote_provider = getattr(
                self._market_data,
                "priority_realtime_ticks",
                None,
            )
            if not callable(candidate_quote_provider):
                candidate_quote_provider = getattr(
                    self._market_data,
                    "realtime_ticks",
                    None,
                )
            candidate_status_provider = getattr(
                self._market_data,
                "priority_current_session_instrument_statuses",
                None,
            )
            if not callable(candidate_status_provider):
                candidate_status_provider = getattr(
                    self._market_data,
                    "current_session_instrument_statuses",
                    None,
                )
            if not callable(candidate_quote_provider) or not callable(
                candidate_status_provider
            ):
                candidate_suspension_probe_status = "provider_unavailable"
            else:
                candidate_suspension_probe_status = "failed"
                try:
                    candidate_quote_batch = candidate_quote_provider(
                        candidate_stale_codes
                    )
                    candidate_status_batch = candidate_status_provider(
                        candidate_stale_codes,
                        session=monitor_session,
                    )
                except Exception as exc:
                    candidate_suspension_probe_error = (
                        f"{type(exc).__name__}: {str(exc)[:140]}"
                    )
                else:
                    candidate_zero_trade_codes = _current_session_zero_trade_codes(
                        candidate_quote_batch,
                        requested_codes=candidate_stale_codes,
                    )
                    candidate_status_suspended_codes = (
                        _current_session_suspended_codes(
                            candidate_status_batch,
                            requested_codes=candidate_stale_codes,
                            session=monitor_session,
                        )
                    )
                    verified_candidate_suspended_codes = frozenset(
                        candidate_zero_trade_codes.intersection(
                            candidate_status_suspended_codes
                        )
                    )
                    candidate_suspension_probe_status = (
                        "verified_suspended"
                        if verified_candidate_suspended_codes
                        else "verified_no_suspension"
                    )
        if verified_candidate_suspended_codes:
            candidate_suspended_codes.update(verified_candidate_suspended_codes)
            candidate_errors = [
                error
                for error in candidate_errors
                if error.get("code") not in verified_candidate_suspended_codes
            ]
            five_universe = tuple(
                code
                for code in five_universe
                if code not in verified_candidate_suspended_codes
            )
            thirty_universe = tuple(
                code
                for code in thirty_universe
                if code not in verified_candidate_suspended_codes
            )
        candidate_symbol_exclusions = copy.deepcopy(
            active_candidate_symbol_exclusions
        )
        retained_candidate_errors: list[dict[str, object]] = []
        newly_excluded_candidate_codes: set[str] = set()
        for error in candidate_errors:
            code = error.get("code")
            lane = lanes_by_code.get(str(code)) if isinstance(code, str) else None
            exclusion = (
                _candidate_monitor_symbol_exclusion_document(
                    error,
                    observation_lane=lane,
                    observed_at=observed_at,
                )
                if lane is not None and code not in mandatory_code_set
                else None
            )
            if exclusion is None:
                retained_candidate_errors.append(error)
                continue
            candidate_symbol_exclusions[str(code)] = exclusion
            newly_excluded_candidate_codes.add(str(code))
        candidate_errors = retained_candidate_errors
        if newly_excluded_candidate_codes:
            five_universe = tuple(
                code for code in five_universe if code not in newly_excluded_candidate_codes
            )
            thirty_universe = tuple(
                code
                for code in thirty_universe
                if code not in newly_excluded_candidate_codes
            )
        with self._background_lock:
            if self._candidate_monitor_suspended_session == monitor_session:
                self._candidate_monitor_current_session_suspended_codes = tuple(
                    sorted(candidate_suspended_codes)
                )
                self._candidate_monitor_suspension_probe_status = (
                    candidate_suspension_probe_status
                )
                self._candidate_monitor_suspension_probe_error = (
                    candidate_suspension_probe_error
                )
        documents.sort(
            key=lambda row: (
                str(row.get("code")),
                str(row.get("signal_id")),
            )
        )
        authoritative_codes = tuple(
            sorted(code for code, _rows, exc in results if exc is None)
        )
        for code in authoritative_codes:
            candidate_symbol_exclusions.pop(code, None)
        # 5分钟是正式买卖级别，1分钟区间套负责解锁精确执行。凡本轮已成功读取
        # 当前5m结构的普通候选/发现标的，都可成为局部首报权威来源；分发器仍会按结构
        # ``available_at`` 严格拒绝迟到事件。一次性规则迁移若不在正常候选范围内，
        # 只用于重建当前规则身份，不能借此发送“实时”通知。
        notification_authoritative_codes = tuple(
            code
            for code in authoritative_codes
            if code not in candidate_notification_published_codes
            and (
                code in minute_code_set
                or (
                    code in regular_candidate_scope_set
                    and "5m" in frequencies_by_code[code]
                )
            )
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
            # 信号缺失只对本次局部通道中成功重算的标的有意义；分发器据此撤回信号，
            # 而不会使轮换出本批次的标的失效。
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
        attempted_codes = set(authoritative_codes).union(
            code for code, _rows, exc in results if exc is not None
        )
        attempted_five_codes = tuple(
            code for code in regular_five_codes if code in attempted_codes
        )
        attempted_thirty_codes = tuple(
            code for code in thirty_codes if code in attempted_codes
        )
        attempted_recheck_codes = tuple(
            code
            for code in codes
            if code in scheduled_recheck_code_set and code in attempted_codes
        )
        _locator_metric_attempted, locator_elapsed_seconds = phase_metrics.get(
            "priority_1m",
            (0, 0.0),
        )
        locator_completed_count = sum(
            1
            for code, _rows, exc in results
            if code in minute_code_set and exc is None
        )
        locator_runtime_failures = tuple(
            error
            for error in minute_stock_errors
            if error.get("failure_class") != "MARKET_DATA_REJECTION"
        )
        locator_runtime_verified = bool(
            minute_codes
            and attempted_minute_code_set == minute_code_set
            and not locator_admission_deferred_codes
            and not configured_locator_deferred_codes
            and not deadline_locator_deferred_codes
            and not deferred_mandatory_codes
            and not priority_capacity_insufficient
            and locator_elapsed_seconds <= ONE_MINUTE_LOCATOR_SLA_SECONDS
            and not locator_runtime_failures
        )
        self._record_priority_monitor_result(
            observed_at=observed_at,
            codes=attempted_minute_codes,
            errors=tuple(errors),
            documents=tuple(documents),
            successful_codes=authoritative_codes,
            lanes_by_code=lanes_by_code,
            candidate_errors=tuple(candidate_errors),
            candidate_symbol_exclusions=tuple(
                candidate_symbol_exclusions[code]
                for code in sorted(candidate_symbol_exclusions)
            ),
            five_universe=five_universe,
            thirty_universe=thirty_universe,
            five_codes=attempted_five_codes,
            thirty_codes=attempted_thirty_codes,
            successful_five_codes=successful_five_codes,
            successful_thirty_codes=successful_thirty_codes,
            deferred_candidate_codes=deferred_candidate_codes,
            decision_rule_recheck_attempted_codes=attempted_recheck_codes,
            decision_rule_recheck_deferred_codes=deferred_recheck_codes,
            decision_rule_recheck_errors=tuple(decision_rule_recheck_errors),
            locator_elapsed_seconds=locator_elapsed_seconds,
            locator_scheduled_count=len(minute_codes),
            locator_attempted_count=len(attempted_minute_code_set),
            locator_completed_count=locator_completed_count,
            locator_runtime_verified=locator_runtime_verified,
        )
        if (
            self._notifier is not None
            and realtime_notification_eligible
            and notification_authoritative_codes
        ):
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
        # 完整轮与分钟内部分批次遵循同一提交顺序：通知先持久受理，监听状态
        # 后推进。正常重复调用由分发器的语义事件键去重。
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
        frozen_sector_source_mode: str = "FROZEN_COVERAGE_EPOCH",
        preselection_continuity_codes: tuple[str, ...] = (),
        force_startup_bootstrap: bool = False,
    ) -> None:
        """防止实时观察污染冻结覆盖周期。"""

        round_started_perf = time.perf_counter()
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
                frozen_sector_source_mode=frozen_sector_source_mode,
                preselection_continuity_codes=preselection_continuity_codes,
                force_startup_bootstrap=force_startup_bootstrap,
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
                "reason_code": (
                    exc.reason_code
                    if isinstance(exc, ScreeningScopeAuthorizationError)
                    else "PRIORITY_MONITOR_FAILED"
                ),
                "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            }
            self._record_priority_monitor_result(
                observed_at=observed_at,
                codes=completed_codes,
                errors=(error,),
                round_complete=completed_this_observation,
                round_failed=not completed_this_observation,
            )
            try:
                self._persist_priority_monitor_state()
            except Exception:
                # 内存健康结果仍可观测；状态文件失败绝不能中止已认证覆盖批次。
                pass
        finally:
            with self._background_lock:
                self._priority_monitor_last_round_elapsed_seconds = round(
                    max(0.0, time.perf_counter() - round_started_perf),
                    6,
                )
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
        """独立重算共享严格策略的覆盖周期身份。"""

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
        if not _restored_snapshot_scope_is_valid(snapshot, self._config):
            return False
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
                or not isinstance(exclusion.get("reason_code"), str)
                or exclusion.get("eligibility")
                != COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON.get(
                    str(exclusion["reason_code"])
                )
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
        excluded = set(canonical_lists["excluded_codes"])
        if (
            exclusion_codes != sorted(set(exclusion_codes))
            or set(exclusion_codes) != excluded
            or not coverage_manifest_dispositions_are_consistent(
                manifest,
                snapshot.get("errors"),
            )
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
        errors = snapshot.get("errors")
        stock_errors = (
            tuple(value for value in errors if isinstance(value, Mapping))
            if isinstance(errors, list)
            else ()
        )
        # 确定性行情拒绝必须等到下一行情周期；非确定性计算异常则可能已随代码
        # 修复或原生工作进程重建而恢复。每次进程启动只把这类标的从“下一周期”
        # 提升为一次有退避的当前周期重试，失败后仍会重新落回延后队列，避免热循环。
        restart_retry_codes = {
            str(value["code"])
            for value in stock_errors
            if value.get("error_type") == "stock_analysis_error"
            and value.get("deterministic_for_coverage_epoch") is False
            and value.get("retry_policy") == "NEXT_COVERAGE_CYCLE"
            and isinstance(value.get("code"), str)
            and value.get("code") in self._coverage_cycle_failed_codes
        }
        for code in restart_retry_codes:
            frequencies = self._deferred_frequencies.pop(code, None)
            if frequencies:
                self._pending_frequencies.setdefault(code, set()).update(frequencies)
        audit = snapshot.get("scan_audit")
        if isinstance(audit, Mapping):
            self._coverage_cycle_full_market_history_scan = bool(
                audit.get("full_market_history_scan")
            )
            self._coverage_cycle_background_refresh_required = bool(
                audit.get("background_full_refresh_required")
            )
        if stock_errors:
            self._record_cycle_errors(stock_errors)
        return True

    def _cache_generation_directory(self) -> Path:
        return self._cache_path.with_name(f".{self._cache_path.name}.generations")

    @staticmethod
    def _cache_scope_sidecar_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.scope")

    def _cache_scope_sidecar_document(
        self,
        path: Path,
        payload: Mapping[str, object],
    ) -> dict[str, object] | None:
        codes = _restored_snapshot_scope_codes(payload)
        nested_member_codes = _restored_snapshot_nested_member_codes(payload)
        raw_admitted_codes = payload.get("admitted_universe_codes")
        raw_configured_admitted_codes = payload.get("configured_admitted_codes")
        if (
            codes is None
            or nested_member_codes is None
            or payload.get("screening_scope_mode")
            != self._config.screening_scope_mode
            or payload.get("effective_monitor_universe_limit")
            != self._config.effective_monitor_universe_limit
            or not _bounded_snapshot_admission_is_valid(
                raw_admitted_codes,
                raw_configured_admitted_codes=(
                    raw_configured_admitted_codes
                ),
                strategy_subject_codes=codes,
                config=self._config,
            )
        ):
            return None
        try:
            payload_stat = path.stat()
        except OSError:
            return None
        return {
            "schema": _CACHE_SCOPE_SIDECAR_SCHEMA,
            "screening_scope_mode": payload.get("screening_scope_mode"),
            "effective_monitor_universe_limit": payload.get(
                "effective_monitor_universe_limit"
            ),
            "configured_admitted_codes": list(
                self._config.admitted_universe_codes
            ),
            "snapshot_admitted_codes": list(raw_admitted_codes),
            "strategy_subject_codes": list(codes),
            "analysis_context_member_count": len(nested_member_codes),
            "analysis_context_member_codes_sha256": sha256_json(
                nested_member_codes
            ),
            "payload_name": path.name,
            "payload_size_bytes": payload_stat.st_size,
            "payload_mtime_ns": payload_stat.st_mtime_ns,
            "snapshot_content_sha256": payload.get("snapshot_content_sha256"),
        }

    def _persist_cache_scope_sidecar(
        self,
        path: Path,
        payload: Mapping[str, object],
    ) -> bool:
        """Persist the small admission proof after the payload is durable."""

        if self._config.screening_scope_mode == "FULL_MARKET":
            # A sidecar is an exact bounded-admission proof.  Full snapshots are
            # authenticated by their payload scope and must never retain a stale
            # validation proof beside a content-addressed generation.
            self._cache_scope_sidecar_path(path).unlink(missing_ok=True)
            return True
        sidecar = self._cache_scope_sidecar_path(path)
        document = self._cache_scope_sidecar_document(path, payload)
        if document is None:
            sidecar.unlink(missing_ok=True)
            return False
        temporary = sidecar.with_name(
            f".{sidecar.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    document,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, sidecar)
        except OSError:
            sidecar.unlink(missing_ok=True)
            return False
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def _cache_scope_sidecar_allows_payload(self, path: Path) -> bool:
        """Admit a bounded cache without opening its potentially huge payload."""

        if self._config.screening_scope_mode == "FULL_MARKET":
            # Full writers deliberately do not create bounded scope sidecars.
            # Their presence proves this path came from another scope, so reject
            # it before reading a potentially huge validation payload.
            return not self._cache_scope_sidecar_path(path).exists()
        sidecar = self._cache_scope_sidecar_path(path)
        try:
            if sidecar.stat().st_size > _CACHE_SCOPE_SIDECAR_MAX_BYTES:
                return False
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            payload_stat = path.stat()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(document, Mapping):
            return False
        codes = document.get("strategy_subject_codes")
        raw_admitted_codes = document.get("snapshot_admitted_codes")
        raw_configured_admitted_codes = document.get(
            "configured_admitted_codes"
        )
        if (
            document.get("schema") != _CACHE_SCOPE_SIDECAR_SCHEMA
            or document.get("screening_scope_mode")
            != self._config.screening_scope_mode
            or document.get("effective_monitor_universe_limit")
            != self._config.effective_monitor_universe_limit
            or document.get("payload_name") != path.name
            or document.get("payload_size_bytes") != payload_stat.st_size
            or document.get("payload_mtime_ns") != payload_stat.st_mtime_ns
            or not isinstance(document.get("snapshot_content_sha256"), str)
            or not isinstance(codes, list)
            or type(document.get("analysis_context_member_count")) is not int
            or document.get("analysis_context_member_count", -1) < 0
            or not isinstance(
                document.get("analysis_context_member_codes_sha256"), str
            )
            or not str(
                document.get("analysis_context_member_codes_sha256")
            ).startswith("sha256:")
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in codes
            )
            or len(codes) != len(set(codes))
            or not _bounded_snapshot_admission_is_valid(
                raw_admitted_codes,
                raw_configured_admitted_codes=(
                    raw_configured_admitted_codes
                ),
                strategy_subject_codes=tuple(codes),
                config=self._config,
            )
        ):
            return False
        return True

    def _cache_scope_sidecar_matches_loaded_payload(
        self,
        path: Path,
        payload: Mapping[str, object],
    ) -> bool:
        if self._config.screening_scope_mode == "FULL_MARKET":
            # Detailed payload scope and contract validation happens immediately
            # after decoding.  Keep this boundary limited to proving that no
            # bounded-admission sidecar was attached to the full payload so a
            # malformed full document retains the precise contract error class.
            return not self._cache_scope_sidecar_path(path).exists()
        try:
            document = json.loads(
                self._cache_scope_sidecar_path(path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        nested_member_codes = _restored_snapshot_nested_member_codes(payload)
        return bool(
            isinstance(document, Mapping)
            and nested_member_codes is not None
            and document.get("snapshot_content_sha256")
            == payload.get("snapshot_content_sha256")
            and document.get("snapshot_admitted_codes")
            == payload.get("admitted_universe_codes")
            and isinstance(document.get("configured_admitted_codes"), list)
            and all(
                isinstance(code, str)
                for code in document["configured_admitted_codes"]
            )
            and frozenset(document["configured_admitted_codes"])
            == frozenset(self._config.admitted_universe_codes)
            and document.get("analysis_context_member_count")
            == len(nested_member_codes)
            and document.get("analysis_context_member_codes_sha256")
            == sha256_json(nested_member_codes)
        )

    def _valid_cache_from_path(self, path: Path) -> dict[str, object] | None:
        if not self._cache_scope_sidecar_allows_payload(path):
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping) or not self._cache_scope_sidecar_matches_loaded_payload(
            path, value
        ):
            return None
        # ``json.loads`` 已生成全新对象树；此处深拷贝超过 100 MiB 的已校验快照只会
        # 让启动内存翻倍，不会增加隔离性，因为返回树本身即服务私有的不可变发布。
        return (
            value
            if isinstance(value, dict)
            and _cache_is_valid(
                value,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
                self._decision_source_snapshot_id,
            )
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

    def _seed_decision_rule_recheck(
        self,
        snapshot: Mapping[str, object],
        *,
        cached_core_id: str,
    ) -> None:
        """从已验真的旧规则快照提取仍需盘中复核的买入代码。

        旧结论一律不继承。迁移队列只负责恢复当前买入候选：形成中的 5m
        设置以及仍在通知窗口内的正式买点。历史买点和非持仓卖点不会阻塞
        新规则启动；持仓/关注卖点由每轮强制监听范围独立重算，完整覆盖仍
        负责收盘审计。
        """

        if not self._config.priority_monitoring_enabled:
            return
        source_sha256 = snapshot.get("snapshot_content_sha256")
        raw_signals = snapshot.get("signals")
        if (
            not isinstance(source_sha256, str)
            or not source_sha256.startswith("sha256:")
            or not isinstance(raw_signals, list)
        ):
            return
        with self._background_lock:
            if (
                self._decision_rule_recheck_source_snapshot_sha256 == source_sha256
                and self._decision_rule_recheck_source_core_id == cached_core_id
            ):
                # 当前规则的监听状态已记录同一来源，包含已经出队后的准确剩余集合。
                return
        raw_cutoff = snapshot.get("market_data_as_of")
        if raw_cutoff is None and isinstance(
            snapshot.get("coverage_manifest"), Mapping
        ):
            raw_cutoff = snapshot["coverage_manifest"].get("market_data_as_of")
        try:
            snapshot_cutoff = normalize_datetime(
                datetime.fromisoformat(str(raw_cutoff)),
                "rule recheck snapshot cutoff",
            )
        except (TypeError, ValueError):
            snapshot_cutoff = None
        codes = {
            str(signal["code"])
            for signal in raw_signals
            if isinstance(signal, Mapping)
            and isinstance(signal.get("code"), str)
            and re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", str(signal["code"])) is not None
            and _signal_side(signal) == "buy"
            and _is_current_selection_signal(signal)
            and (
                lifecycle_stage_from_signal(signal) == "approaching"
                or snapshot_cutoff is None
                or _current_five_minute_setup_requires_segment_monitor(
                    signal,
                    snapshot_cutoff,
                )
            )
        }
        codes = set(
            _project_codes_to_configured_scope(
                tuple(sorted(codes)),
                self._config,
            )
        )
        recheck_admission = admit_screening_universe(
            recheck_codes=sorted(codes),
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        codes = set(recheck_admission.recheck_codes)
        with self._background_lock:
            self._decision_rule_recheck_source_snapshot_sha256 = source_sha256
            self._decision_rule_recheck_source_core_id = cached_core_id
            self._decision_rule_recheck_pending_codes = codes
            self._decision_rule_recheck_last_attempted_codes = ()
            self._decision_rule_recheck_last_deferred_codes = ()
            self._decision_rule_recheck_last_errors = ()

    def _valid_previous_core_snapshot(
        self,
        snapshot: Mapping[str, object],
        *,
        cached_core_id: str,
    ) -> bool:
        """验证旧核心快照自身完整，防止受损缓存污染复核范围。"""

        decision_core = snapshot.get("decision_core")
        return bool(
            isinstance(decision_core, Mapping)
            and decision_core.get("contract_id") == cached_core_id
            and decision_core.get("live_status") == "LIVE_DISABLED"
            and _cache_is_valid(
                snapshot,
                self._config,
                cached_core_id,
                self._selection_research_revision,
            )
        )

    def _install_preselection_continuity(
        self,
        snapshot: Mapping[str, object],
        *,
        source_path: Path,
    ) -> bool:
        """Install code-only previous-close scope without restoring conclusions.

        This bridge is deliberately narrower than cache recovery.  The source
        must be a complete, immutable publication for the session being traded,
        and every decision contract except the implementation-source revision
        must still match.  Only typed sector evidence, membership, and symbol
        identities survive; all signals are recalculated by the current engine.
        """

        if not self._config.priority_monitoring_enabled:
            return False
        cached_core_id = snapshot.get("decision_core_id")
        source_snapshot_id = snapshot.get("decision_source_snapshot_id")
        manifest = snapshot.get("coverage_manifest")
        audit = snapshot.get("scan_audit")
        data_quality = snapshot.get("data_quality")
        source_sha256 = snapshot.get("snapshot_content_sha256")
        if (
            cached_core_id != self._decision_core_id
            or not isinstance(source_snapshot_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", source_snapshot_id) is None
            or snapshot.get("selection_research_revision")
            != self._selection_research_revision
            or snapshot.get("available") is not True
            or snapshot.get("scan_state") != "complete"
            or snapshot.get("full_coverage_state") != "complete"
            or not isinstance(manifest, Mapping)
            or manifest.get("complete") is not True
            or not isinstance(audit, Mapping)
            or audit.get("coverage_cycle_complete") is not True
            or not isinstance(data_quality, Mapping)
            or data_quality.get("complete") is not True
            or not isinstance(source_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", source_sha256) is None
            or not self._valid_previous_core_snapshot(
                snapshot,
                cached_core_id=self._decision_core_id,
            )
            or not self._coverage_epoch_identity_valid(snapshot)
        ):
            return False
        try:
            continuity_discovered_count = int(
                audit.get("discovered_symbol_count", 0)
            )
        except (TypeError, ValueError):
            return False
        if (
            continuity_discovered_count
            > self._config.effective_monitor_universe_limit
            and self._config.screening_scope_mode != "FULL_MARKET"
        ):
            # A complete production snapshot is not a bounded validation or
            # large-scope cohort.  Do not use it to repopulate sector/supportive
            # or recheck state after a source-only code change.
            return False
        target = _preselection_target_session(snapshot, self._clock())
        if target.get("aligned") is not True:
            return False
        target_session = target.get("target_session")
        coverage_epoch_id = manifest.get("coverage_epoch_id")
        catalog_revision = manifest.get("sector_catalog_revision")
        raw_market_data_as_of = snapshot.get("market_data_as_of")
        if (
            not isinstance(target_session, str)
            or not target_session
            or not isinstance(coverage_epoch_id, str)
            or not coverage_epoch_id
            or not isinstance(catalog_revision, str)
            or not catalog_revision.startswith("sha256:")
            or not isinstance(raw_market_data_as_of, str)
        ):
            return False
        try:
            market_data_as_of = normalize_datetime(
                datetime.fromisoformat(raw_market_data_as_of),
                "preselection continuity market_data_as_of",
            )
            sector_batch, sector_members = _coverage_sector_state_from_snapshot(
                snapshot
            )
        except (TypeError, ValueError):
            return False
        sector_publishability_ratio = (
            sector_batch.resolution_ratio
            if sector_batch.parent_relations
            else sector_batch.completion_ratio
        )
        if (
            sector_members is None
            or sector_batch.strength_evidence is None
            or sector_publishability_ratio < self._config.min_scan_completion_ratio
        ):
            return False
        raw_signals = snapshot.get("signals")
        if not isinstance(raw_signals, list):
            return False
        configured_allowlist = _configured_scope_allowlist(self._config)
        if configured_allowlist is not None:
            sector_members = {
                sector_id: tuple(
                    code for code in members if code in configured_allowlist
                )
                for sector_id, members in sector_members.items()
            }
        signal_documents = tuple(row for row in raw_signals if isinstance(row, Mapping))
        signal_admission = admit_screening_universe(
            signal_codes=_project_codes_to_configured_scope(
                _priority_signal_candidate_codes(signal_documents),
                self._config,
            ),
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        signal_codes = signal_admission.signal_codes
        try:
            routing = _sector_member_routing(
                assessments=sector_batch.assessments,
                members_by_sector=sector_members,
                parent_relations=sector_batch.parent_relations,
                unavailable_sector_ids=frozenset(
                    item.sector_id
                    for item in (*sector_batch.errors, *sector_batch.exclusions)
                ),
            )
        except (TypeError, ValueError):
            return False
        supportive_code_count = sum(
            1
            for assessment in routing.eligible_sector_by_code.values()
            if assessment.regime == "supportive"
        )
        with self._background_lock:
            self._preselection_continuity_sector_batch = sector_batch
            self._preselection_continuity_sector_members = dict(sector_members)
            self._preselection_continuity_market_data_as_of = market_data_as_of
            self._preselection_continuity_coverage_epoch_id = coverage_epoch_id
            self._preselection_continuity_sector_catalog_revision = catalog_revision
            self._preselection_continuity_source_snapshot_sha256 = source_sha256
            self._preselection_continuity_source_name = source_path.name
            self._preselection_continuity_target_session = target_session
            self._preselection_continuity_signal_codes = signal_codes
            self._preselection_continuity_supportive_code_count = supportive_code_count
            self._preselection_continuity_sector_runtime_hydrated = False
        self._seed_decision_rule_recheck(
            snapshot,
            cached_core_id=self._decision_core_id,
        )
        return True

    def _restore_preselection_continuity_recheck_seed(self) -> bool:
        """Prevent an unrelated compact monitor state from hiding the seed.

        The compact priority state is loaded after the large snapshot because it
        normally contains the most recent remaining queue.  During a source-only
        transition, however, an older compact state may describe a different
        snapshot and must not overwrite the newly authenticated continuity
        codes.  Once the compact state names this exact source, its smaller queue
        is authoritative and is preserved across later restarts.
        """

        with self._background_lock:
            continuity_source = (
                self._preselection_continuity_source_snapshot_sha256
            )
            if continuity_source is None:
                return False
            if (
                self._decision_rule_recheck_source_snapshot_sha256
                == continuity_source
                and self._decision_rule_recheck_source_core_id
                == self._decision_core_id
            ):
                return False
            self._decision_rule_recheck_source_snapshot_sha256 = continuity_source
            self._decision_rule_recheck_source_core_id = self._decision_core_id
            recheck_admission = admit_screening_universe(
                recheck_codes=_project_codes_to_configured_scope(
                    self._preselection_continuity_signal_codes,
                    self._config,
                ),
                max_symbols=self._config.effective_monitor_universe_limit,
                large_scope_authorized=self._config.large_scope_authorized,
            )
            self._decision_rule_recheck_pending_codes = set(
                recheck_admission.recheck_codes
            )
            self._decision_rule_recheck_last_attempted_codes = ()
            self._decision_rule_recheck_last_deferred_codes = ()
            self._decision_rule_recheck_last_errors = ()
        return True

    def _clear_preselection_continuity(self) -> bool:
        with self._background_lock:
            active = self._preselection_continuity_source_snapshot_sha256 is not None
            self._preselection_continuity_sector_batch = None
            self._preselection_continuity_sector_members = None
            self._preselection_continuity_market_data_as_of = None
            self._preselection_continuity_coverage_epoch_id = None
            self._preselection_continuity_sector_catalog_revision = None
            self._preselection_continuity_source_snapshot_sha256 = None
            self._preselection_continuity_source_name = None
            self._preselection_continuity_target_session = None
            self._preselection_continuity_signal_codes = ()
            self._preselection_continuity_supportive_code_count = 0
            self._preselection_continuity_sector_runtime_hydrated = False
        return active

    def _cache_document_from_path(self, path: Path) -> dict[str, object] | None:
        if not self._cache_scope_sidecar_allows_payload(path):
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping) or not self._cache_scope_sidecar_matches_loaded_payload(
            path, value
        ):
            return None
        return value if isinstance(value, dict) else None

    def _recover_cache_or_preselection_from_generations(
        self,
        generations: tuple[Path, ...],
        *,
        recover_current_cache: bool,
    ) -> dict[str, object] | None:
        continuity_installed = False
        for path in generations:
            candidate = self._cache_document_from_path(path)
            if candidate is None:
                continue
            if recover_current_cache and _cache_is_valid(
                candidate,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
                self._decision_source_snapshot_id,
            ):
                if continuity_installed:
                    self._clear_preselection_continuity()
                self._cache_recovered_from_generation = str(path)
                return candidate
            if not continuity_installed:
                continuity_installed = self._install_preselection_continuity(
                    candidate,
                    source_path=path,
                )
        return None

    def _load_valid_cache(self) -> dict[str, object] | None:
        generations = self._generation_paths()
        self._cache_generation_count = len(generations)
        self._cache_recovered_from_generation = None
        if not self._cache_scope_sidecar_allows_payload(self._cache_path):
            if self._cache_path.exists():
                self._quarantined_cache_reason = "CACHE_SCOPE_PROOF_MISSING_OR_INVALID"
                return None
            return self._recover_cache_or_preselection_from_generations(
                generations, recover_current_cache=True
            )
        try:
            current_value = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # 主快照缺失、截断或不可读可能源于原子替换中断；只有这类物理失败
            # 才允许回退到不可变历史代。
            pass
        else:
            if not isinstance(current_value, Mapping) or not (
                self._cache_scope_sidecar_matches_loaded_payload(
                    self._cache_path, current_value
                )
            ):
                self._quarantined_cache_reason = (
                    "CACHE_SCOPE_PAYLOAD_IDENTITY_MISMATCH"
                )
                return None
            if isinstance(current_value, dict):
                current_manifest = current_value.get("coverage_manifest")
                current_epoch_id = current_value.get("coverage_epoch_id")
                current_content_identity_valid = bool(
                    isinstance(current_value.get("snapshot_content_sha256"), str)
                    and current_value.get("snapshot_content_sha256")
                    == live_screening_snapshot_content_sha256(current_value)
                )
                current_scope_valid = _restored_snapshot_scope_is_valid(
                    current_value,
                    self._config,
                )
                current_contract_valid = bool(
                    current_scope_valid
                    and _cache_contract_is_valid(
                        current_value,
                        self._config,
                        self._decision_core_id,
                        self._selection_research_revision,
                        self._decision_source_snapshot_id,
                    )
                )
                recoverable_checkpoint_identity = bool(
                    current_scope_valid
                    and current_content_identity_valid
                    and current_value.get("schema") == SCHEMA
                    and current_value.get("algorithm_id") == self._config.algorithm_id
                    and current_value.get("structure_contract_id")
                    == self._config.structure_contract_id
                    and current_value.get("parameter_set_id")
                    == self._config.parameter_set_id
                    and current_value.get("decision_core_id") == self._decision_core_id
                    and current_value.get("decision_source_snapshot_id")
                    == self._decision_source_snapshot_id
                    and isinstance(self._decision_source_snapshot_id, str)
                    and current_value.get("selection_research_revision")
                    == self._selection_research_revision
                    and current_value.get("read_only") is True
                    and current_value.get("no_order_execution") is True
                    and current_value.get("scan_state")
                    in {"in_progress", "incomplete_not_published"}
                    and current_value.get("full_coverage_state") == "in_progress"
                    and isinstance(current_manifest, Mapping)
                    and current_manifest.get("schema") == COVERAGE_MANIFEST_SCHEMA
                    and current_manifest.get("coverage_state_contract_id")
                    == COVERAGE_STATE_CONTRACT_ID
                    and current_manifest.get("coverage_epoch_id") == current_epoch_id
                )
                continuity_checkpoint_identity = bool(
                    current_scope_valid
                    and current_content_identity_valid
                    and current_value.get("schema") == SCHEMA
                    and current_value.get("algorithm_id") == self._config.algorithm_id
                    and current_value.get("structure_contract_id")
                    == self._config.structure_contract_id
                    and current_value.get("parameter_set_id")
                    == self._config.parameter_set_id
                    and current_value.get("decision_core_id") == self._decision_core_id
                    and current_value.get("decision_source_snapshot_id")
                    == self._decision_source_snapshot_id
                    and isinstance(self._decision_source_snapshot_id, str)
                    and current_value.get("selection_research_revision")
                    == self._selection_research_revision
                    and current_value.get("read_only") is True
                    and current_value.get("no_order_execution") is True
                    and isinstance(current_manifest, Mapping)
                    and current_manifest.get("complete") is False
                    and (
                        current_value.get("available") is False
                        or current_value.get("scan_state")
                        in {
                            "not_started",
                            "coverage_epoch_invalidated",
                            "in_progress",
                            "incomplete_not_published",
                        }
                    )
                )
                if (
                    isinstance(current_manifest, Mapping)
                    and current_manifest.get("complete") is False
                    and isinstance(current_epoch_id, str)
                    and current_epoch_id
                    and recoverable_checkpoint_identity
                ):
                    # 同一覆盖纪元曾经完整发布后，局部重试产生的未完成检查点不能
                    # 让页面退回“全量构建中”。优先恢复不可变完整代；恢复出的失败
                    # 标的仍会由启动重试队列按当前代码单独复算。
                    for path in generations:
                        recovered = self._valid_cache_from_path(path)
                        recovered_manifest = (
                            recovered.get("coverage_manifest")
                            if isinstance(recovered, Mapping)
                            else None
                        )
                        if (
                            isinstance(recovered_manifest, Mapping)
                            and recovered_manifest.get("complete") is True
                            and recovered.get("coverage_epoch_id") == current_epoch_id
                        ):
                            self._cache_recovered_from_generation = str(path)
                            return recovered
                if current_contract_valid and current_content_identity_valid:
                    if continuity_checkpoint_identity:
                        self._recover_cache_or_preselection_from_generations(
                            generations,
                            recover_current_cache=False,
                        )
                    return current_value
            if isinstance(current_value, Mapping):
                cached_core_id = current_value.get("decision_core_id")
                cached_research_revision = current_value.get(
                    "selection_research_revision"
                )
                if (
                    isinstance(cached_core_id, str)
                    and cached_core_id
                    and cached_core_id != self._decision_core_id
                ):
                    self._quarantined_cache_decision_core_id = cached_core_id
                    self._quarantined_cache_reason = "DECISION_CORE_IDENTITY_MISMATCH"
                    if self._valid_previous_core_snapshot(
                        current_value,
                        cached_core_id=cached_core_id,
                    ):
                        self._seed_decision_rule_recheck(
                            current_value,
                            cached_core_id=cached_core_id,
                        )
                elif current_value.get("decision_source_snapshot_id") != (
                    self._decision_source_snapshot_id
                ):
                    self._quarantined_cache_decision_core_id = (
                        cached_core_id if isinstance(cached_core_id, str) else None
                    )
                    self._quarantined_cache_reason = "DECISION_SOURCE_REVISION_MISMATCH"
                    previous_source_snapshot_valid = bool(
                        isinstance(cached_core_id, str)
                        and cached_core_id == self._decision_core_id
                        and self._valid_previous_core_snapshot(
                            current_value,
                            cached_core_id=cached_core_id,
                        )
                    )
                    if previous_source_snapshot_valid:
                        self._seed_decision_rule_recheck(
                            current_value,
                            cached_core_id=cached_core_id,
                        )
                        continuity_installed = self._install_preselection_continuity(
                            current_value,
                            source_path=self._cache_path,
                        )
                        if not continuity_installed:
                            self._recover_cache_or_preselection_from_generations(
                                generations,
                                recover_current_cache=False,
                            )
                elif cached_research_revision != self._selection_research_revision:
                    self._quarantined_cache_decision_core_id = (
                        cached_core_id if isinstance(cached_core_id, str) else None
                    )
                    self._quarantined_cache_reason = (
                        "SELECTION_RESEARCH_REVISION_MISMATCH"
                    )
                else:
                    self._quarantined_cache_decision_core_id = (
                        cached_core_id if isinstance(cached_core_id, str) else None
                    )
                    self._quarantined_cache_reason = "CURRENT_CACHE_CONTRACT_INVALID"
                    if continuity_checkpoint_identity:
                        self._recover_cache_or_preselection_from_generations(
                            generations,
                            recover_current_cache=False,
                        )
            # 可解析主快照若未通过语义、策略或内容哈希校验，说明被篡改或已过期，
            # 而非写入中断；必须关闭失败，不能用旧备份掩盖。
            return None
        return self._recover_cache_or_preselection_from_generations(
            generations,
            recover_current_cache=True,
        )

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return copy.deepcopy(self._snapshot)

    def admitted_universe_codes(self) -> tuple[str, ...] | None:
        """Return the bounded runtime subjects, or ``None`` for FULL_MARKET.

        This is a read-only projection used by Web presentation and review
        archives.  It never consults QMT and therefore cannot turn a page read
        into discovery work.
        """

        if self._config.screening_scope_mode == "FULL_MARKET":
            return None
        if _configured_scope_allowlist(self._config) is not None:
            # The configured cohort is the authorization boundary, not a
            # snapshot of whichever lanes happen to be active this minute.
            # Suspended, excluded, or not-yet-due symbols must not disappear
            # from Web/cache admission merely because they consumed no work.
            return self._config.admitted_universe_codes
        with self._state_lock:
            snapshot = self._snapshot
            snapshot_codes = (
                _restored_snapshot_scope_codes(snapshot)
                if _restored_snapshot_scope_is_valid(snapshot, self._config)
                else None
            )
        with self._background_lock:
            priority_document_codes = tuple(
                str(value["code"])
                for value in self._priority_monitor_latest_documents.values()
                if isinstance(value.get("code"), str) and value.get("code")
            )
            priority_state_codes = tuple(
                dict.fromkeys(
                    (
                        *priority_document_codes,
                        *self._candidate_monitor_five_universe,
                        *self._priority_monitor_last_codes,
                        *self._candidate_monitor_last_five_codes,
                        *self._candidate_monitor_last_deferred_codes,
                        *self._candidate_monitor_five_last_success_at,
                        *self._priority_monitor_code_observations,
                        *self._priority_monitor_signal_codes.values(),
                        *self._candidate_monitor_symbol_exclusions,
                        *(
                            code
                            for error in (
                                *self._priority_monitor_last_errors,
                                *self._candidate_monitor_last_errors,
                            )
                            for code in _explicit_strategy_subject_codes(error)
                        ),
                    )
                )
            )
            supportive_state_codes = tuple(
                dict.fromkeys(
                    (
                        *self._candidate_monitor_thirty_universe,
                        *self._candidate_monitor_last_thirty_codes,
                        *self._candidate_monitor_thirty_last_success_at,
                    )
                )
            )
            recheck_codes = tuple(
                dict.fromkeys(
                    (
                        *sorted(self._decision_rule_recheck_pending_codes),
                        *self._decision_rule_recheck_last_attempted_codes,
                        *self._decision_rule_recheck_last_deferred_codes,
                        *(
                            code
                            for error in self._decision_rule_recheck_last_errors
                            for code in _explicit_strategy_subject_codes(error)
                        ),
                    )
                )
            )
        admission = admit_screening_universe(
            signal_codes=(
                *priority_state_codes,
                *(snapshot_codes or ()),
            ),
            supportive_codes=supportive_state_codes,
            recheck_codes=recheck_codes,
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        return admission.admitted_codes

    def admit_archive_universe_codes(
        self,
        mandatory_codes: Sequence[str],
    ) -> tuple[str, ...] | None:
        """Admit archive-owned positions/attention before optional subjects."""

        if self._config.screening_scope_mode == "FULL_MARKET":
            return None
        current_codes = self.admitted_universe_codes() or ()
        admission = admit_screening_universe(
            mandatory_codes=mandatory_codes,
            signal_codes=current_codes,
            max_symbols=self._config.effective_monitor_universe_limit,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        return admission.admitted_codes

    def _load_presentation_cached_sector_snapshot(
        self,
        *,
        observed_at: datetime,
    ) -> CachedSectorSnapshot | None:
        with self._state_lock:
            cached = self._presentation_cached_sector_snapshot
        if cached is not None:
            return cached
        cached_provider = getattr(
            self._sector_catalog,
            "cached_sector_snapshot_for_priority",
            None,
        )
        if not callable(cached_provider):
            return None
        scope_configurer = getattr(
            self._sector_catalog,
            "configure_sector_cache_restore_scope",
            None,
        )
        if callable(scope_configurer):
            try:
                scope_configurer(
                    scope_mode=self._config.screening_scope_mode,
                    max_symbols=self._config.effective_monitor_universe_limit,
                    admitted_codes=(
                        self._config.admitted_universe_codes
                        or (self.admitted_universe_codes() or ())
                    ),
                )
            except (TypeError, ValueError):
                return None
        try:
            candidate = cached_provider(as_of=observed_at)
        except (OSError, TypeError, ValueError, RuntimeError):
            return None
        if not isinstance(candidate, CachedSectorSnapshot):
            return None
        with self._state_lock:
            if self._presentation_cached_sector_snapshot is None:
                self._presentation_cached_sector_snapshot = candidate
            return self._presentation_cached_sector_snapshot

    def presentation_snapshot(self, *, isolated: bool = True) -> dict[str, object]:
        """返回完整审计发布物的精简实时页面视图。

        不可变归档保留全部预热和映射诊断；浏览器只需与决策相关的摘要，而不必为每行
        重复原始点证据。单独维护该投影既保留回放和前向捕获使用的可审计契约，也避免
        分钟轮询复制并传输超过 100 MiB 的 JSON 树。
        """

        if type(isolated) is not bool:
            raise TypeError("isolated must be an exact bool")

        observed_at = normalize_datetime(self._clock(), "clock")
        with self._state_lock:
            snapshot_available = self._snapshot.get("available") is True
            cached_sector_snapshot = self._presentation_cached_sector_snapshot
        # 全覆盖扫描会先完成并冻结板块批次，再进入耗时更长的个股扫描。这里读取的
        # 是不可变 dataclass 引用，不获取长时间持有的扫描锁，以免页面请求被首轮
        # 全市场扫描阻塞。它只可用于页面目录预览，绝不能冒充已发布决策快照。
        coverage_sector_batch = self._coverage_cycle_sector_batch
        coverage_sector_batch_usable = bool(
            coverage_sector_batch is not None
            and coverage_sector_batch.completion_ratio
            >= self._config.min_scan_completion_ratio
        )
        if not coverage_sector_batch_usable and cached_sector_snapshot is None:
            with self._background_lock:
                background_running = bool(
                    self._background_thread is not None
                    and self._background_thread.is_alive()
                )
            if not background_running:
                cached_sector_snapshot = self._load_presentation_cached_sector_snapshot(
                    observed_at=observed_at
                )
        presentation_sector_batch = (
            coverage_sector_batch
            if coverage_sector_batch_usable
            else (
                cached_sector_snapshot.batch
                if cached_sector_snapshot is not None
                and cached_sector_snapshot.batch.completion_ratio
                >= self._config.min_scan_completion_ratio
                else None
            )
        )
        presentation_sector_source = (
            "CURRENT_COVERAGE_CYCLE"
            if coverage_sector_batch_usable
            else (
                "CACHED_SECTOR_SNAPSHOT"
                if presentation_sector_batch is not None
                else "UNAVAILABLE"
            )
        )
        with self._background_lock:
            priority_last_at = self._priority_monitor_last_at
            priority_errors = tuple(self._priority_monitor_last_errors)
            priority_runtime_verified = self._priority_monitor_runtime_verified
            priority_locator_runtime_verified = (
                self._priority_monitor_locator_runtime_verified
            )
            priority_locator_scheduled_count = (
                self._priority_monitor_locator_last_scheduled_count
            )
            priority_documents = tuple(
                copy.deepcopy(value)
                for _, value in sorted(self._priority_monitor_latest_documents.items())
            )
            code_observations = dict(self._priority_monitor_code_observations)
            candidate_errors = tuple(self._candidate_monitor_last_errors)
            candidate_symbol_exclusions = tuple(
                copy.deepcopy(value)
                for _, value in sorted(
                    self._candidate_monitor_symbol_exclusions.items()
                )
                if _candidate_monitor_symbol_exclusion_is_active(
                    value,
                    observed_at=observed_at,
                )
            )
            priority_revision = self._priority_monitor_presentation_revision
        admitted_universe = self.admitted_universe_codes()
        admitted_code_set = (
            None if admitted_universe is None else set(admitted_universe)
        )
        if admitted_code_set is not None:
            priority_documents = tuple(
                value
                for value in priority_documents
                if value.get("code") in admitted_code_set
            )
            code_observations = {
                code: value
                for code, value in code_observations.items()
                if code in admitted_code_set
            }
            candidate_symbol_exclusions = tuple(
                value
                for value in candidate_symbol_exclusions
                if value.get("code") in admitted_code_set
            )
            priority_errors = tuple(
                value
                for value in priority_errors
                if set(_explicit_strategy_subject_codes(value)).issubset(
                    admitted_code_set
                )
            )
            candidate_errors = tuple(
                value
                for value in candidate_errors
                if set(_explicit_strategy_subject_codes(value)).issubset(
                    admitted_code_set
                )
            )
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
        startup_bootstrap_overlay = bool(
            self._config.priority_monitoring_enabled
            and not snapshot_available
            and priority_last_at is not None
            and priority_last_at <= observed_at
            and priority_runtime_verified
            and (
                priority_locator_scheduled_count == 0
                or priority_locator_runtime_verified
            )
            and not priority_errors
        )
        overlay_active = bool(
            self._config.priority_monitoring_enabled
            and (
                _priority_monitor_session_open(observed_at) or startup_bootstrap_overlay
            )
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
            value
            for value in priority_documents
            if value.get("code") in fresh_codes and _is_current_selection_signal(value)
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
            snapshot_scope_valid = _restored_snapshot_scope_is_valid(
                snapshot,
                self._config,
            )
            snapshot_scope_codes = (
                _restored_snapshot_scope_codes(snapshot)
                if snapshot_scope_valid
                else None
            )
            if (
                snapshot_scope_valid
                and admitted_code_set is not None
                and snapshot_scope_codes is not None
                and not set(snapshot_scope_codes).issubset(admitted_code_set)
            ):
                snapshot_scope_valid = False
            if not snapshot_scope_valid:
                # Do not copy full-market status, evidence or progress into a
                # bounded page projection.  Current compact monitor documents
                # are overlaid below after the same admission intersection.
                snapshot = _initial_snapshot(
                    self._config,
                    selection_research_revision=(
                        self._selection_research_revision
                    ),
                    decision_source_snapshot_id=(
                        self._decision_source_snapshot_id
                    ),
                )
                snapshot_available = False
            source_sha256 = snapshot.get("snapshot_content_sha256")
            validated_source_sha256 = (
                self._validated_snapshot_sha256 if snapshot_scope_valid else None
            )
            presentation_revision = (
                f"{source_sha256}|{priority_revision}|live={priority_live}"
                f"|fresh={sha256_json(tuple(sorted(fresh_code_observations)))}"
                f"|sector_batch={id(presentation_sector_batch)}"
                f"|validated={validated_source_sha256}"
                f"|scope={sha256_json(admitted_universe)}"
            )
            if (
                isinstance(source_sha256, str)
                and self._presentation_cache_sha256 == presentation_revision
                and self._presentation_cache is not None
            ):
                return (
                    copy.deepcopy(self._presentation_cache)
                    if isolated
                    else self._presentation_cache
                )
        document = {
            key: copy.deepcopy(value)
            for key, value in snapshot.items()
            if key != "signals"
        }
        document["screening_scope"] = {
            "schema": "chanlun-screening-scope-v1",
            "mode": self._config.screening_scope_mode,
            "validation_cohort_size": self._config.validation_cohort_size,
            "effective_monitor_universe_limit": (
                self._config.effective_monitor_universe_limit
            ),
            "configured_max_admitted_universe_symbols": (
                self._config.max_admitted_universe_symbols
            ),
            "large_scope_authorized": self._config.large_scope_authorized,
            "full_coverage_enabled": self._config.full_coverage_refresh_enabled,
        }
        document_snapshot_authoritative = bool(
            document.get("available") is True
            and isinstance(source_sha256, str)
            and source_sha256 == validated_source_sha256
        )
        sector_catalog_overlay_active = False
        existing_sector_documents = document.get("sectors")
        if (
            not document_snapshot_authoritative
            and isinstance(existing_sector_documents, list)
            and presentation_sector_batch is not None
        ):
            failed_sector_ids = {
                item.sector_id
                for item in (
                    *presentation_sector_batch.errors,
                    *presentation_sector_batch.exclusions,
                )
            }
            preview_assessments = tuple(
                assessment
                for assessment in presentation_sector_batch.assessments
                if assessment.sector_id not in failed_sector_ids
            )
            ranked_ordinals = {
                row.assessment.sector_id: row.ordinal
                for row in rank_sectors(preview_assessments)
            }
            document["sectors"] = [
                _sector_document(
                    assessment,
                    ordinal=ranked_ordinals.get(assessment.sector_id),
                )
                for assessment in sorted(
                    preview_assessments,
                    key=lambda row: (
                        ranked_ordinals.get(row.sector_id, 10**9),
                        row.sector_id,
                    ),
                )
            ]
            sector_catalog_overlay_active = bool(document["sectors"])
        visible_sector_documents = document.get("sectors")
        visible_sector_count = (
            len(visible_sector_documents)
            if isinstance(visible_sector_documents, list)
            else 0
        )
        sector_catalog_fallback_active = bool(
            not document_snapshot_authoritative and visible_sector_count > 0
        )
        document["sector_catalog_overlay"] = {
            "schema": "chanlun-sector-catalog-page-overlay",
            "active": sector_catalog_fallback_active,
            "source": (
                presentation_sector_source
                if sector_catalog_overlay_active
                else (
                    "PUBLISHED_SNAPSHOT"
                    if document_snapshot_authoritative
                    else (
                        "LAST_INVALIDATED_SNAPSHOT"
                        if visible_sector_count > 0
                        else "UNAVAILABLE"
                    )
                )
            ),
            "provisional": sector_catalog_fallback_active,
            "decision_authoritative": document_snapshot_authoritative,
            "display_only": sector_catalog_fallback_active,
            "sector_count": visible_sector_count,
            "completion_ratio": (
                str(presentation_sector_batch.completion_ratio)
                if sector_catalog_overlay_active
                and presentation_sector_batch is not None
                else None
            ),
            "archival_snapshot_unchanged": True,
        }
        raw_signals = snapshot.get("signals")
        projected_signals = (
            [
                _presentation_signal_document(value)
                for value in raw_signals
                if isinstance(value, Mapping)
                and _is_current_selection_signal(value)
                and (
                    admitted_code_set is None
                    or value.get("code") in admitted_code_set
                )
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
                    POINT_REVIEW_ORDER.index(str(value["point_type"])),
                    str(value["code"]),
                    str(value["signal_id"]),
                ),
            )
        document["signals"] = projected_signals
        document["counts_by_stage"] = {}
        document["counts_by_point_type"] = {
            point_type: 0 for point_type in CANONICAL_POINT_TYPES
        }
        for value in projected_signals:
            stage = str(value.get("lifecycle_stage") or "unknown")
            document["counts_by_stage"][stage] = (
                document["counts_by_stage"].get(stage, 0) + 1
            )
            point_type = str(value.get("point_type") or "")
            if point_type in document["counts_by_point_type"]:
                document["counts_by_point_type"][point_type] += 1
        document["presentation_schema"] = "chanlun-trading-screening-presentation"
        document["presentation_revision"] = sha256_json(
            {
                "schema": "chanlun-trading-screening-presentation-revision",
                "revision": presentation_revision,
            }
        )
        document["source_snapshot_content_sha256"] = source_sha256
        document["full_audit_evidence_embedded"] = False
        document["priority_live_overlay"] = {
            "schema": "chanlun-priority-live-page-overlay",
            "enabled": self._config.priority_monitoring_enabled,
            "live": priority_live,
            "startup_bootstrap": startup_bootstrap_overlay,
            "observed_at": (
                None if priority_last_at is None else priority_last_at.isoformat()
            ),
            "age_seconds": priority_age_seconds,
            "max_age_seconds": priority_max_age_seconds,
            "signal_count": (
                len(minute_documents)
                if priority_live or startup_bootstrap_overlay
                else 0
            ),
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
            "symbol_exclusion_count": len(candidate_symbol_exclusions),
            "symbol_exclusion_codes": [
                str(value["code"]) for value in candidate_symbol_exclusions
            ],
            "archival_snapshot_unchanged": True,
            "realtime_notification_authorized": False,
            "fresh_five_minute_notification_authorized": True,
            "notification_scope": ("CURRENT_5M_REGULAR_CANDIDATE_OR_DISCOVERY_ONLY"),
        }
        with self._state_lock:
            if self._snapshot is snapshot and isinstance(source_sha256, str):
                self._presentation_cache_sha256 = presentation_revision
                self._presentation_cache = document
        return copy.deepcopy(document) if isolated else document

    def presentation_snapshot_reference(self) -> Mapping[str, object]:
        """供同步 HTTP 序列化使用的不可变页面投影引用。

        发布缓存只通过整棵替换更新，路由层只做顶层投影并立即序列化，不会修改这里
        返回的对象。普通调用仍默认获得深拷贝，保留原有隔离契约。
        """

        return self.presentation_snapshot(isolated=False)

    def _snapshot_reference(self) -> Mapping[str, object]:
        """返回当前不可变发布物，而不复制整棵数据树。

        刷新会构建独立载荷，并在 ``_state_lock`` 下原子替换 ``self._snapshot``，从不
        原地修改已发布快照。因此安装新发布物期间，健康请求可以安全持有旧映射；公开
        页面调用方仍使用 :meth:`snapshot` 并获得隔离的深拷贝。
        """

        with self._state_lock:
            return self._snapshot

    def _record_background_heartbeat(self) -> None:
        with self._background_lock:
            # 原生进程进度由请求线程回报；并行个股扫描中它虽不是后台所有者线程，
            # 仍能证明所属刷新存活。仅在没有后台所有者时忽略回调。
            if self._background_thread is None:
                return
            self._background_heartbeat_at = normalize_datetime(self._clock(), "clock")

    def _record_native_progress(self) -> None:
        """记录长任务进度，并在独立分片按时唤醒实时监听。"""

        self._record_background_heartbeat()
        observed_at = normalize_datetime(self._clock(), "clock")
        if self._priority_scan_lock.locked() or not self._priority_monitor_due(
            observed_at
        ):
            return
        with self._background_lock:
            background_running = bool(
                self._background_thread is not None
                and self._background_thread.is_alive()
                and not self._background_stop.is_set()
            )
        if not background_running:
            return
        with self._priority_progress_launch_lock:
            existing = self._priority_progress_thread
            if existing is not None and existing.is_alive():
                return
            worker = Thread(
                target=self._refresh_priority_from_native_progress,
                daemon=True,
                name="trading-screening-priority-progress",
            )
            self._priority_progress_thread = worker
            worker.start()

    def _refresh_priority_from_native_progress(self) -> None:
        try:
            self.refresh_now(copy_result=False, priority_only=True)
        finally:
            with self._priority_progress_launch_lock:
                if self._priority_progress_thread is current_thread():
                    self._priority_progress_thread = None

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
                        self._review_readiness_error = None
                return result
            with self._review_readiness_validation_lock:
                if self._review_readiness_validation_sha256 != snapshot_sha256:
                    self._review_readiness_validation_sha256 = snapshot_sha256
                    with self._state_lock:
                        if self._snapshot is snapshot:
                            self._review_readiness_error = None
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
        """应用重启后只复用精确匹配的子进程结论，绝不复用过期结论。"""

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
        """在不阻塞 HTTP 的情况下计算大型不可变复核结论。"""

        validation_error: str | None = None
        try:
            result = _screening_review_readiness(snapshot, identity_valid=True)
        except Exception as exc:
            result = (False, "REVIEW_BOUNDARY_VALIDATION_FAILED")
            validation_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        with self._state_lock:
            if self._snapshot is snapshot:
                self._review_readiness_cache_sha256 = snapshot_sha256
                self._review_readiness_cache = result
                self._review_readiness_error = validation_error
        with self._review_readiness_validation_lock:
            if self._review_readiness_validation_sha256 == snapshot_sha256:
                self._review_readiness_validation_sha256 = None
                self._review_readiness_validation_thread = None

    def _validate_review_readiness_file_in_background(
        self,
        snapshot: Mapping[str, object],
        snapshot_sha256: str,
    ) -> None:
        """在 Web 解释器之外校验大型持久快照。

        ``validate_live_review_snapshot`` 会遍历超过 100 MiB 的完整证据树。Python
        守护线程虽然让 HTTP 逻辑异步，但其计算仍占用全局解释器锁，可能使所有页面
        停顿数分钟。发布物在安装前已原子持久化，因此短生命周期只读子进程可以校验
        精确文件与哈希，并只返回精简结论。
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
        validation_error: str | None = None
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
            raw_error = document.get("error")
            validation_error = (
                str(raw_error)[:240]
                if isinstance(raw_error, str) and raw_error.strip()
                else None
            )
        except (
            OSError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            result = False, "REVIEW_BOUNDARY_VALIDATION_FAILED"
            validation_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        with self._state_lock:
            if self._snapshot is snapshot:
                self._review_readiness_cache_sha256 = snapshot_sha256
                self._review_readiness_cache = result
                self._review_readiness_error = validation_error
        with self._review_readiness_validation_lock:
            if self._review_readiness_validation_sha256 == snapshot_sha256:
                self._review_readiness_validation_sha256 = None
                self._review_readiness_validation_thread = None

    def health_snapshot(self) -> dict[str, object]:
        """返回无副作用的选股运行证明。

        Flask 进程存活不足以证明 QMT 选股器可用：选股器运行在守护线程中，原生
        ``xtquant`` 崩溃也无法由 Python 捕获。此文档让就绪检查在不调用 QMT 的情况
        下校验工作进程、心跳和最后一个可发布的不可变快照。
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
            priority_monitor_current_session_zero_trade_codes = tuple(
                self._priority_monitor_current_session_zero_trade_codes
            )
            priority_monitor_zero_trade_quote_status = (
                self._priority_monitor_zero_trade_quote_status
            )
            priority_monitor_zero_trade_quote_error = (
                self._priority_monitor_zero_trade_quote_error
            )
            priority_monitor_zero_trade_quote_diagnostics = tuple(
                copy.deepcopy(self._priority_monitor_zero_trade_quote_diagnostics)
            )
            priority_monitor_current_session_suspended_codes = tuple(
                self._priority_monitor_current_session_suspended_codes
            )
            priority_monitor_instrument_status_probe_status = (
                self._priority_monitor_instrument_status_probe_status
            )
            priority_monitor_instrument_status_probe_error = (
                self._priority_monitor_instrument_status_probe_error
            )
            priority_monitor_last_errors = tuple(
                copy.deepcopy(self._priority_monitor_last_errors)
            )
            priority_monitor_mandatory_count = self._priority_monitor_mandatory_count
            priority_monitor_immediate_universe_count = (
                self._priority_monitor_immediate_universe_count
            )
            priority_monitor_tracking_universe_count = (
                self._priority_monitor_tracking_universe_count
            )
            priority_monitor_scheduled_count = self._priority_monitor_scheduled_count
            priority_monitor_configured_rotation_seconds = (
                self._priority_monitor_configured_rotation_seconds
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
            candidate_monitor_symbol_exclusions = tuple(
                copy.deepcopy(value)
                for _, value in sorted(
                    self._candidate_monitor_symbol_exclusions.items()
                )
                if _candidate_monitor_symbol_exclusion_is_active(
                    value,
                    observed_at=observed_at,
                )
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
            candidate_monitor_last_deferred_codes = tuple(
                self._candidate_monitor_last_deferred_codes
            )
            candidate_monitor_signal_pool_count = (
                self._candidate_monitor_signal_pool_count
            )
            candidate_monitor_signal_admitted_count = (
                self._candidate_monitor_signal_admitted_count
            )
            candidate_monitor_signal_deferred_count = (
                self._candidate_monitor_signal_deferred_count
            )
            candidate_monitor_supportive_eligible_count = (
                self._candidate_monitor_supportive_eligible_count
            )
            candidate_monitor_supportive_admitted_count = (
                self._candidate_monitor_supportive_admitted_count
            )
            candidate_monitor_supportive_capacity = (
                self._candidate_monitor_supportive_capacity
            )
            priority_monitor_immediate_pool_count = (
                self._priority_monitor_immediate_pool_count
            )
            priority_monitor_immediate_deferred_count = (
                self._priority_monitor_immediate_deferred_count
            )
            priority_monitor_locator_pool_count = (
                self._priority_monitor_locator_pool_count
            )
            priority_monitor_locator_admission_deferred_count = (
                self._priority_monitor_locator_admission_deferred_count
            )
            candidate_monitor_suspended_session = (
                self._candidate_monitor_suspended_session
            )
            candidate_monitor_current_session_suspended_codes = tuple(
                self._candidate_monitor_current_session_suspended_codes
            )
            candidate_monitor_suspension_probe_status = (
                self._candidate_monitor_suspension_probe_status
            )
            candidate_monitor_suspension_probe_error = (
                self._candidate_monitor_suspension_probe_error
            )
            priority_monitor_last_round_elapsed_seconds = (
                self._priority_monitor_last_round_elapsed_seconds
            )
            priority_monitor_locator_last_observed_at = (
                self._priority_monitor_locator_last_observed_at
            )
            priority_monitor_locator_last_elapsed_seconds = (
                self._priority_monitor_locator_last_elapsed_seconds
            )
            priority_monitor_locator_last_scheduled_count = (
                self._priority_monitor_locator_last_scheduled_count
            )
            priority_monitor_locator_last_attempted_count = (
                self._priority_monitor_locator_last_attempted_count
            )
            priority_monitor_locator_last_completed_count = (
                self._priority_monitor_locator_last_completed_count
            )
            priority_monitor_locator_runtime_verified = (
                self._priority_monitor_locator_runtime_verified
            )
            decision_rule_recheck_source_sha256 = (
                self._decision_rule_recheck_source_snapshot_sha256
            )
            decision_rule_recheck_source_core_id = (
                self._decision_rule_recheck_source_core_id
            )
            decision_rule_recheck_pending_codes = tuple(
                sorted(self._decision_rule_recheck_pending_codes)
            )
            decision_rule_recheck_last_attempted_codes = tuple(
                self._decision_rule_recheck_last_attempted_codes
            )
            decision_rule_recheck_last_deferred_codes = tuple(
                self._decision_rule_recheck_last_deferred_codes
            )
            decision_rule_recheck_last_errors = tuple(
                copy.deepcopy(value)
                for value in self._decision_rule_recheck_last_errors
            )
            preselection_continuity_source_sha256 = (
                self._preselection_continuity_source_snapshot_sha256
            )
            preselection_continuity_source_name = (
                self._preselection_continuity_source_name
            )
            preselection_continuity_target_session = (
                self._preselection_continuity_target_session
            )
            preselection_continuity_market_data_as_of = (
                self._preselection_continuity_market_data_as_of
            )
            preselection_continuity_coverage_epoch_id = (
                self._preselection_continuity_coverage_epoch_id
            )
            preselection_continuity_signal_codes = tuple(
                self._preselection_continuity_signal_codes
            )
            preselection_continuity_supportive_code_count = (
                self._preselection_continuity_supportive_code_count
            )
            preselection_continuity_sector_runtime_hydrated = (
                self._preselection_continuity_sector_runtime_hydrated
            )

        # 读取健康计数时不深拷贝全市场信号/证据树；下方仍针对完整不可变发布
        # 重算身份，因此不会削弱篡改检测。
        with self._state_lock:
            snapshot = self._snapshot
            validated_snapshot_sha256 = self._validated_snapshot_sha256
        admitted_universe = self.admitted_universe_codes()
        if admitted_universe is not None:
            admitted_code_set = set(admitted_universe)

            def admitted_codes(values: Sequence[str]) -> tuple[str, ...]:
                return tuple(code for code in values if code in admitted_code_set)

            def admitted_errors(
                values: Sequence[Mapping[str, object]],
            ) -> tuple[dict[str, object], ...]:
                return tuple(
                    copy.deepcopy(dict(value))
                    for value in values
                    if set(_explicit_strategy_subject_codes(value)).issubset(
                        admitted_code_set
                    )
                )

            priority_monitor_last_codes = admitted_codes(priority_monitor_last_codes)
            last_monitoring_errors = admitted_errors(last_monitoring_errors)
            priority_monitor_current_session_zero_trade_codes = admitted_codes(
                priority_monitor_current_session_zero_trade_codes
            )
            priority_monitor_current_session_suspended_codes = admitted_codes(
                priority_monitor_current_session_suspended_codes
            )
            priority_monitor_zero_trade_quote_diagnostics = tuple(
                value
                for value in priority_monitor_zero_trade_quote_diagnostics
                if set(_explicit_strategy_subject_codes(value)).issubset(
                    admitted_code_set
                )
            )
            priority_monitor_last_errors = admitted_errors(
                priority_monitor_last_errors
            )
            candidate_monitor_last_errors = admitted_errors(
                candidate_monitor_last_errors
            )
            candidate_monitor_symbol_exclusions = tuple(
                value
                for value in candidate_monitor_symbol_exclusions
                if value.get("code") in admitted_code_set
            )
            candidate_monitor_five_universe = admitted_codes(
                candidate_monitor_five_universe
            )
            candidate_monitor_thirty_universe = admitted_codes(
                candidate_monitor_thirty_universe
            )
            candidate_monitor_five_last_success_at = {
                code: value
                for code, value in candidate_monitor_five_last_success_at.items()
                if code in admitted_code_set
            }
            candidate_monitor_thirty_last_success_at = {
                code: value
                for code, value in candidate_monitor_thirty_last_success_at.items()
                if code in admitted_code_set
            }
            candidate_monitor_last_five_codes = admitted_codes(
                candidate_monitor_last_five_codes
            )
            candidate_monitor_last_thirty_codes = admitted_codes(
                candidate_monitor_last_thirty_codes
            )
            candidate_monitor_last_deferred_codes = admitted_codes(
                candidate_monitor_last_deferred_codes
            )
            candidate_monitor_current_session_suspended_codes = admitted_codes(
                candidate_monitor_current_session_suspended_codes
            )
            decision_rule_recheck_pending_codes = admitted_codes(
                decision_rule_recheck_pending_codes
            )
            decision_rule_recheck_last_attempted_codes = admitted_codes(
                decision_rule_recheck_last_attempted_codes
            )
            decision_rule_recheck_last_deferred_codes = admitted_codes(
                decision_rule_recheck_last_deferred_codes
            )
            decision_rule_recheck_last_errors = admitted_errors(
                decision_rule_recheck_last_errors
            )
            preselection_continuity_signal_codes = admitted_codes(
                preselection_continuity_signal_codes
            )
            priority_monitor_latest_signal_count = min(
                priority_monitor_latest_signal_count,
                len(admitted_universe),
            )
            priority_monitor_mandatory_count = min(
                priority_monitor_mandatory_count,
                len(admitted_universe),
            )
            priority_monitor_immediate_universe_count = min(
                priority_monitor_immediate_universe_count,
                len(admitted_universe),
            )
            priority_monitor_tracking_universe_count = min(
                priority_monitor_tracking_universe_count,
                len(admitted_universe),
            )
            priority_monitor_scheduled_count = min(
                priority_monitor_scheduled_count,
                len(admitted_universe),
            )
            candidate_monitor_supportive_eligible_count = min(
                candidate_monitor_supportive_eligible_count,
                len(admitted_universe),
            )
            candidate_monitor_supportive_admitted_count = min(
                candidate_monitor_supportive_admitted_count,
                len(admitted_universe),
            )
            preselection_continuity_supportive_code_count = min(
                preselection_continuity_supportive_code_count,
                len(admitted_universe),
            )
        snapshot_scope_valid = _restored_snapshot_scope_is_valid(
            snapshot,
            self._config,
        )
        snapshot_scope_codes = (
            _restored_snapshot_scope_codes(snapshot)
            if snapshot_scope_valid
            else None
        )
        if (
            snapshot_scope_valid
            and admitted_universe is not None
            and snapshot_scope_codes is not None
            and not set(snapshot_scope_codes).issubset(set(admitted_universe))
        ):
            snapshot_scope_valid = False
        if not snapshot_scope_valid:
            snapshot = _initial_snapshot(
                self._config,
                selection_research_revision=self._selection_research_revision,
                decision_source_snapshot_id=self._decision_source_snapshot_id,
            )
            validated_snapshot_sha256 = None
        scan_state = str(snapshot.get("scan_state") or "unknown")
        last_batch_state = str(snapshot.get("last_batch_state") or scan_state)
        snapshot_available = snapshot.get("available") is True
        startup_priority_bootstrap_ready = bool(
            self._config.priority_monitoring_enabled
            and not snapshot_available
            and priority_monitor_runtime_verified
            and (
                priority_monitor_locator_last_scheduled_count == 0
                or priority_monitor_locator_runtime_verified
            )
            and priority_monitor_last_at is not None
            and priority_monitor_last_at <= observed_at
            and not priority_monitor_last_errors
        )
        snapshot_sha256 = snapshot.get("snapshot_content_sha256")
        try:
            # 发布在原子安装前校验一次；安装后的对象树私有且不原地修改，因此匹配
            # 声明身份即可避免每次 /readyz 轮询都哈希超过 100 MiB 的快照。启动缓存
            # 同样先校验再安装；未知发布首次通过完整闸门后再按身份缓存。
            identity_valid = bool(
                isinstance(snapshot_sha256, str)
                and snapshot_sha256 == validated_snapshot_sha256
            )
            if not identity_valid:
                identity_valid = _cache_is_valid(
                    snapshot,
                    self._config,
                    self._decision_core_id,
                    self._selection_research_revision,
                    self._decision_source_snapshot_id,
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
        selection_operational_reason_codes: list[str] = []
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
        # 首份全量快照属于选股发布物就绪，不属于 Web/QMT 进程运行就绪。后台线程、
        # 心跳和原生网关健康时，/readyz 必须保持可服务，并通过 selection_* 明示
        # “等待首份快照”，避免守护脚本反复重启一个正在正常构建快照的进程。
        snapshot_rebuild_in_progress = bool(
            not identity_valid
            and current_snapshot_required
            and (
                not snapshot_available
                or scan_state
                in {"coverage_epoch_invalidated", "incomplete_not_published"}
            )
            and refresh_started_at is not None
            and worker_alive
        )
        if (
            snapshot_available
            and not identity_valid
            and not snapshot_rebuild_in_progress
        ):
            selection_operational_reason_codes.append(
                "screening_snapshot_identity_missing"
            )
        if scan_state == "refresh_failed":
            selection_operational_reason_codes.append("screening_refresh_failed")
        elif (
            scan_state
            in {
                "coverage_epoch_invalidated",
                "incomplete_not_published",
            }
            and not startup_priority_bootstrap_ready
            and not snapshot_rebuild_in_progress
        ):
            selection_operational_reason_codes.append(
                "screening_snapshot_not_publishable"
            )
        if last_error is not None and scan_state != "refresh_failed":
            selection_operational_reason_codes.append(
                "screening_background_error"
            )
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
        daily_preselection_sell_candidate_count = sum(
            1
            for value in signal_rows
            if isinstance(value, Mapping)
            and str(value.get("point_type") or "").endswith("sell")
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
        continuity_defers_full_coverage = (
            self._preselection_continuity_defers_full_coverage(observed_at)
        )
        full_coverage_force_active = self._forced_full_coverage_active(
            snapshot,
            observed_at=observed_at,
        )
        full_coverage_auto_recovery_reason = (
            self._quarantined_cache_reason or "PRESELECTION_SNAPSHOT_MISSING"
            if full_coverage_force_active
            and not self._config.force_full_coverage_until_complete
            else None
        )
        full_coverage_scheduled_window_open = bool(
            full_coverage_refresh_enabled
            and _full_coverage_refresh_window_open(observed_at)
        )
        full_coverage_refresh_window_open = bool(
            full_coverage_force_active
            or (
                full_coverage_scheduled_window_open
                and not continuity_defers_full_coverage
            )
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
        priority_monitor_compute_window_open = _priority_monitor_compute_window_open(
            observed_at
        )
        candidate_monitor_lunch_catchup_active = _candidate_monitor_lunch_catchup_open(
            observed_at
        )
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
            # Keep the previous run's failure counters for diagnosis, but a
            # closed exchange has no currently-due one-minute observation.
            priority_monitor_status = "not_due"
            priority_monitor_ready = True
            if priority_monitor_last_errors:
                priority_monitor_reason_codes.append(
                    "PRIORITY_MONITOR_LAST_RUN_DEGRADED"
                )
        elif priority_monitor_last_errors:
            # 明确运行故障比“尚未完成本轮验证”更有诊断价值；失败轮次仍保持
            # runtime_verified=False，因此调度器会立即重试而不是等待下一周期。
            priority_monitor_status = "degraded"
            priority_monitor_ready = False
            priority_monitor_reason_codes.append("PRIORITY_MONITOR_DEGRADED")
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
        else:
            priority_monitor_status = "verified"
            priority_monitor_ready = True
        priority_monitor_failure_reason_counts: dict[str, int] = {}
        for error in priority_monitor_last_errors:
            reason = str(error.get("reason_code") or "PRIORITY_MONITOR_UNCLASSIFIED")
            priority_monitor_failure_reason_counts[reason] = (
                priority_monitor_failure_reason_counts.get(reason, 0) + 1
            )
        priority_monitor_locator_deferred_codes = tuple(
            dict.fromkeys(
                code
                for error in priority_monitor_last_errors
                if error.get("reason_code")
                in {
                    "ONE_MINUTE_LOCATOR_ADMISSION_CAPACITY_INSUFFICIENT",
                    "ONE_MINUTE_LOCATOR_CONFIGURED_CAPACITY_INSUFFICIENT",
                    "ONE_MINUTE_LOCATOR_TIME_BUDGET_EXHAUSTED",
                }
                for code in (
                    error.get("deferred_codes")
                    if isinstance(error.get("deferred_codes"), list)
                    else []
                )
                if isinstance(code, str)
            )
        )
        if priority_monitor_locator_last_observed_at is None:
            priority_monitor_locator_runtime_status = "awaiting_observation"
        elif priority_monitor_locator_last_scheduled_count == 0:
            priority_monitor_locator_runtime_status = "not_required"
        elif priority_monitor_locator_runtime_verified:
            priority_monitor_locator_runtime_status = (
                "verified_with_symbol_rejections"
                if priority_monitor_locator_last_completed_count
                < priority_monitor_locator_last_attempted_count
                else "verified"
            )
        elif (
            priority_monitor_locator_last_attempted_count
            < priority_monitor_locator_last_scheduled_count
        ):
            priority_monitor_locator_runtime_status = "capacity_insufficient"
        else:
            priority_monitor_locator_runtime_status = "failed"
        priority_monitor_locator_observed_symbols_per_second = (
            priority_monitor_locator_last_attempted_count
            / priority_monitor_locator_last_elapsed_seconds
            if priority_monitor_locator_last_elapsed_seconds is not None
            and priority_monitor_locator_last_elapsed_seconds > 0
            and priority_monitor_locator_last_attempted_count > 0
            else None
        )
        five_candidate_coverage = _candidate_lane_coverage(
            candidate_monitor_five_universe,
            last_success_at=candidate_monitor_five_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.five_minute_candidate_target_seconds,
            execution_grace_seconds=(
                self._config.candidate_monitor_time_budget_seconds
            ),
        )
        thirty_candidate_coverage = _candidate_lane_coverage(
            candidate_monitor_thirty_universe,
            last_success_at=candidate_monitor_thirty_last_success_at,
            observed_at=observed_at,
            target_seconds=self._config.thirty_minute_candidate_target_seconds,
            execution_grace_seconds=(
                self._config.candidate_monitor_time_budget_seconds
            ),
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
        candidate_configured_capacity_sufficient = bool(
            five_required_per_refresh
            <= self._config.max_five_minute_candidate_symbols_per_refresh
            and thirty_required_per_refresh
            <= self._config.max_thirty_minute_candidate_symbols_per_refresh
        )
        candidate_coverage_overdue = bool(
            five_candidate_coverage["overdue_count"]
            or thirty_candidate_coverage["overdue_count"]
        )
        candidate_required_symbols_per_second = (
            len(candidate_monitor_five_universe)
            / self._config.five_minute_candidate_target_seconds
            + len(candidate_monitor_thirty_universe)
            / self._config.thirty_minute_candidate_target_seconds
        )
        candidate_attempted_codes = set(candidate_monitor_last_five_codes).union(
            candidate_monitor_last_thirty_codes
        )
        candidate_observed_symbols_per_second: float | None = None
        if (
            candidate_monitor_last_deferred_codes
            and priority_monitor_last_round_elapsed_seconds is not None
            and priority_monitor_last_round_elapsed_seconds > 0
            and candidate_attempted_codes
        ):
            # A deferred round consumed the physical lane budget, so its actual
            # end-to-end throughput is stronger evidence than the configured
            # admission-list ceiling.  Include preparation/publish overhead:
            # that time is part of the one-minute production cadence too.
            candidate_observed_symbols_per_second = (
                len(candidate_attempted_codes)
                / priority_monitor_last_round_elapsed_seconds
            )
        if priority_monitor_last_at is None:
            candidate_observed_capacity_sufficient: bool | None = None
        elif candidate_monitor_last_errors:
            candidate_observed_capacity_sufficient = False
        elif candidate_monitor_last_deferred_codes:
            candidate_observed_capacity_sufficient = (
                False
                if candidate_coverage_overdue
                or (
                    candidate_observed_symbols_per_second is not None
                    and candidate_observed_symbols_per_second
                    < candidate_required_symbols_per_second
                )
                else None
            )
        else:
            candidate_observed_capacity_sufficient = True
        candidate_capacity_sufficient = bool(
            candidate_configured_capacity_sufficient
            and candidate_observed_capacity_sufficient is not False
        )
        candidate_monitor_failure_reason_counts: dict[str, int] = {}
        for error in candidate_monitor_last_errors:
            reason = str(error.get("reason_code") or "CANDIDATE_MONITOR_UNCLASSIFIED")
            candidate_monitor_failure_reason_counts[reason] = (
                candidate_monitor_failure_reason_counts.get(reason, 0) + 1
            )
        candidate_monitor_symbol_exclusion_reason_counts: dict[str, int] = {}
        for exclusion in candidate_monitor_symbol_exclusions:
            reason = str(
                exclusion.get("reason_code") or "CANDIDATE_SYMBOL_UNCLASSIFIED"
            )
            candidate_monitor_symbol_exclusion_reason_counts[reason] = (
                candidate_monitor_symbol_exclusion_reason_counts.get(reason, 0) + 1
            )
        candidate_monitor_reason_codes: list[str] = []
        if not priority_monitor_enabled:
            candidate_monitor_status = "disabled"
            candidate_monitor_ready = True
        elif candidate_monitor_lunch_catchup_active:
            # 午休没有新的完成分钟事实，实时 SLA 不到期；后台仍利用空窗补齐
            # 冷候选与磁盘增量态。历史容量缺口继续展示，但不把可用页面宣告失败。
            candidate_monitor_status = "catching_up"
            candidate_monitor_ready = True
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_LUNCH_CATCHUP_ACTIVE"
            )
            if candidate_monitor_last_errors:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_LAST_RUN_ERRORS"
                )
            elif not candidate_configured_capacity_sufficient:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
                )
            elif candidate_observed_capacity_sufficient is False:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_LAST_RUN_CAPACITY_INSUFFICIENT"
                )
        elif not priority_monitor_session_open:
            candidate_monitor_status = "not_due"
            candidate_monitor_ready = True
            if candidate_monitor_last_errors:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_LAST_RUN_ERRORS"
                )
            elif not candidate_configured_capacity_sufficient:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
                )
            elif candidate_observed_capacity_sufficient is False:
                candidate_monitor_reason_codes.append(
                    "CANDIDATE_MONITOR_LAST_RUN_CAPACITY_INSUFFICIENT"
                )
        elif candidate_monitor_last_errors:
            candidate_monitor_status = "degraded"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_ERRORS")
        elif not priority_monitor_runtime_verified:
            candidate_monitor_status = "awaiting_runtime_verification"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_RUNTIME_UNVERIFIED"
            )
        elif not candidate_configured_capacity_sufficient:
            candidate_monitor_status = "capacity_insufficient"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_CONFIGURED_CAPACITY_INSUFFICIENT"
            )
        elif candidate_observed_capacity_sufficient is False:
            candidate_monitor_status = "capacity_insufficient"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_OBSERVED_CAPACITY_INSUFFICIENT"
            )
        elif (
            five_candidate_coverage["ready"] is True
            and thirty_candidate_coverage["ready"] is True
        ):
            candidate_monitor_status = "verified"
            candidate_monitor_ready = True
        elif (
            five_candidate_coverage["overdue_count"] == 0
            and thirty_candidate_coverage["overdue_count"] == 0
        ):
            candidate_monitor_status = "warming"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_WARMING")
        else:
            candidate_monitor_status = "cadence_overdue"
            candidate_monitor_ready = False
            candidate_monitor_reason_codes.append("CANDIDATE_MONITOR_CADENCE_OVERDUE")
        if candidate_monitor_symbol_exclusions and priority_monitor_enabled:
            candidate_monitor_reason_codes.append(
                "CANDIDATE_MONITOR_SYMBOL_EXCLUSIONS"
            )
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
        notification_delivery_verified = bool(
            notification_delivery is not None
            and notification_delivery.get("configured") is True
            and notification_delivery.get("operationally_verified") is True
        )
        if not notification_dispatcher_configured:
            realtime_alert_ready = False
            realtime_alert_status = "notification_not_configured"
            realtime_alert_reason_code = "REALTIME_NOTIFICATION_NOT_CONFIGURED"
        elif notification_delivery_degraded:
            realtime_alert_ready = False
            realtime_alert_status = "notification_degraded"
            realtime_alert_reason_code = str(
                notification_delivery.get("reason_code")
                if notification_delivery is not None
                else "REALTIME_NOTIFICATION_DELIVERY_DEGRADED"
            )
        elif (
            not priority_monitor_session_open
            and notification_delivery is not None
            and notification_delivery.get("status") == "awaiting_first_delivery"
            and notification_delivery.get("reason_code")
            == "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"
        ):
            # 休市期间没有到期事件可用于证明首次送达。此状态不是当前告警
            # 时效故障；通知器配置和后台 worker 仍由独立字段完整暴露。
            realtime_alert_ready = True
            realtime_alert_status = "not_due"
            realtime_alert_reason_code = "NON_TRADING_SESSION_NOT_DUE"
        elif not notification_delivery_verified:
            # “已创建通知器对象”不能冒充“已有成功送达证据”。在首次真实或演练
            # 送达完成前，识别链仍可继续运行，但总预警状态必须明确为尚未验证。
            realtime_alert_ready = False
            realtime_alert_status = "notification_unverified"
            realtime_alert_reason_code = str(
                notification_delivery.get("reason_code")
                if notification_delivery is not None
                else "REALTIME_NOTIFICATION_DELIVERY_UNVERIFIED"
            )
        elif not priority_monitor_session_open:
            realtime_alert_ready = True
            realtime_alert_status = "not_due"
            realtime_alert_reason_code = "NON_TRADING_SESSION_NOT_DUE"
        elif not priority_monitor_ready:
            realtime_alert_ready = False
            realtime_alert_status = "priority_monitor_degraded"
            realtime_alert_reason_code = (
                priority_monitor_reason_codes[0]
                if priority_monitor_reason_codes
                else "PRIORITY_MONITOR_DEGRADED"
            )
        elif not candidate_monitor_ready:
            # 立即持仓/自选复查正常，并不证明支持板块候选的 5m 轮换满足时效。
            # 总预警状态同时约束两条监听车道，避免页面在候选容量不足或逾期时
            # 仍显示“正常”。
            realtime_alert_ready = False
            realtime_alert_status = "candidate_monitor_degraded"
            realtime_alert_reason_code = (
                candidate_monitor_reason_codes[0]
                if candidate_monitor_reason_codes
                else "CANDIDATE_MONITOR_DEGRADED"
            )
        else:
            realtime_alert_ready = True
            realtime_alert_status = "ready"
            realtime_alert_reason_code = "READY"

        member_history_diagnostics = snapshot.get("sector_member_history_diagnostics")
        if not isinstance(member_history_diagnostics, Mapping):
            member_history_diagnostics = None

        runtime_ready = not reasons
        return {
            # ``ready`` 保留为进程运行就绪，供 /readyz 和服务守护使用；选股发布物是否
            # 完整由下方独立字段表达，二者不得再混为一个布尔值。
            "ready": runtime_ready,
            "runtime_ready": runtime_ready,
            "runtime_status": "ready" if runtime_ready else "not_ready",
            "selection_ready": daily_preselection_ready,
            "selection_status": daily_preselection_status,
            "selection_reason_code": daily_preselection_reason_code,
            "selection_operational_reason_codes": list(
                selection_operational_reason_codes
            ),
            "status": "ready" if runtime_ready else "not_ready",
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
            "incomplete_checkpoint_interval_seconds": (
                self._config.incomplete_checkpoint_interval_seconds
            ),
            "last_incomplete_checkpoint_at": (
                None
                if self._last_incomplete_checkpoint_at is None
                else self._last_incomplete_checkpoint_at.isoformat()
            ),
            "quarantined_cache_decision_core_id": (
                self._quarantined_cache_decision_core_id
            ),
            "quarantined_cache_reason": self._quarantined_cache_reason,
            "preselection_continuity_active": (
                preselection_continuity_source_sha256 is not None
            ),
            "preselection_continuity_source_snapshot_sha256": (
                preselection_continuity_source_sha256
            ),
            "preselection_continuity_source_name": (
                preselection_continuity_source_name
            ),
            "preselection_continuity_target_session": (
                preselection_continuity_target_session
            ),
            "preselection_continuity_market_data_as_of": (
                None
                if preselection_continuity_market_data_as_of is None
                else preselection_continuity_market_data_as_of.isoformat()
            ),
            "preselection_continuity_coverage_epoch_id": (
                preselection_continuity_coverage_epoch_id
            ),
            "preselection_continuity_signal_code_count": len(
                preselection_continuity_signal_codes
            ),
            "preselection_continuity_supportive_code_count": (
                preselection_continuity_supportive_code_count
            ),
            "preselection_continuity_sector_runtime_hydrated": (
                preselection_continuity_sector_runtime_hydrated
            ),
            "quarantined_priority_monitor_decision_core_id": (
                self._quarantined_priority_monitor_decision_core_id
            ),
            "quarantined_priority_monitor_reason": (
                self._quarantined_priority_monitor_reason
            ),
            "quarantined_priority_monitor_recheck_code_count": (
                self._quarantined_priority_monitor_recheck_code_count
            ),
            "decision_rule_recheck_source_snapshot_sha256": (
                decision_rule_recheck_source_sha256
            ),
            "decision_rule_recheck_source_core_id": (
                decision_rule_recheck_source_core_id
            ),
            "decision_rule_recheck_pending_count": len(
                decision_rule_recheck_pending_codes
            ),
            "decision_rule_recheck_pending_codes": list(
                decision_rule_recheck_pending_codes
            ),
            "decision_rule_recheck_last_attempted_count": len(
                decision_rule_recheck_last_attempted_codes
            ),
            "decision_rule_recheck_last_attempted_codes": list(
                decision_rule_recheck_last_attempted_codes
            ),
            "decision_rule_recheck_last_deferred_count": len(
                decision_rule_recheck_last_deferred_codes
            ),
            "decision_rule_recheck_last_deferred_codes": list(
                decision_rule_recheck_last_deferred_codes
            ),
            "decision_rule_recheck_last_error_count": len(
                decision_rule_recheck_last_errors
            ),
            "decision_rule_recheck_last_errors": [
                copy.deepcopy(value) for value in decision_rule_recheck_last_errors
            ],
            "decision_rule_recheck_status": (
                "pending"
                if decision_rule_recheck_pending_codes
                else "complete"
                if decision_rule_recheck_source_sha256 is not None
                else "not_required"
            ),
            "current_logic_snapshot_required": current_snapshot_required,
            "snapshot_rebuild_in_progress": snapshot_rebuild_in_progress,
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
            "startup_priority_bootstrap_ready": (startup_priority_bootstrap_ready),
            "priority_monitor_runtime_verified": (priority_monitor_runtime_verified),
            "priority_monitor_ready": priority_monitor_ready,
            "priority_monitor_status": priority_monitor_status,
            "priority_monitor_reason_codes": priority_monitor_reason_codes,
            "priority_monitor_session_open": priority_monitor_session_open,
            "priority_monitor_compute_window_open": (
                priority_monitor_compute_window_open
            ),
            "priority_monitor_preopen_warmup_active": bool(
                priority_monitor_compute_window_open
                and not priority_monitor_session_open
                and not candidate_monitor_lunch_catchup_active
            ),
            "candidate_monitor_lunch_catchup_active": (
                candidate_monitor_lunch_catchup_active
            ),
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
            "priority_monitor_current_session_zero_trade_code_count": len(
                priority_monitor_current_session_zero_trade_codes
            ),
            "priority_monitor_current_session_zero_trade_codes": list(
                priority_monitor_current_session_zero_trade_codes
            ),
            "priority_monitor_zero_trade_quote_status": (
                priority_monitor_zero_trade_quote_status
            ),
            "priority_monitor_zero_trade_quote_error": (
                priority_monitor_zero_trade_quote_error
            ),
            "priority_monitor_zero_trade_quote_diagnostics": list(
                priority_monitor_zero_trade_quote_diagnostics
            ),
            "priority_monitor_current_session_suspended_code_count": len(
                priority_monitor_current_session_suspended_codes
            ),
            "priority_monitor_current_session_suspended_codes": list(
                priority_monitor_current_session_suspended_codes
            ),
            "priority_monitor_instrument_status_probe_status": (
                priority_monitor_instrument_status_probe_status
            ),
            "priority_monitor_instrument_status_probe_error": (
                priority_monitor_instrument_status_probe_error
            ),
            "priority_monitor_mandatory_count": priority_monitor_mandatory_count,
            "priority_monitor_immediate_universe_count": (
                priority_monitor_immediate_universe_count
            ),
            "priority_monitor_tracking_universe_count": (
                priority_monitor_tracking_universe_count
            ),
            "priority_monitor_scheduled_count": priority_monitor_scheduled_count,
            "priority_monitor_configured_rotation_seconds": (
                priority_monitor_configured_rotation_seconds
            ),
            "priority_monitor_locator_sla_seconds": ONE_MINUTE_LOCATOR_SLA_SECONDS,
            "priority_monitor_locator_capacity_sufficient": bool(
                priority_monitor_locator_admission_deferred_count == 0
                and priority_monitor_configured_rotation_seconds is not None
                and priority_monitor_configured_rotation_seconds
                <= ONE_MINUTE_LOCATOR_SLA_SECONDS
            ),
            "priority_monitor_locator_pool_count": (
                priority_monitor_locator_pool_count
            ),
            "priority_monitor_locator_admission_deferred_count": (
                priority_monitor_locator_admission_deferred_count
            ),
            "priority_monitor_locator_runtime_verified": (
                priority_monitor_locator_runtime_verified
            ),
            "priority_monitor_locator_runtime_status": (
                priority_monitor_locator_runtime_status
            ),
            "priority_monitor_locator_last_observed_at": (
                None
                if priority_monitor_locator_last_observed_at is None
                else priority_monitor_locator_last_observed_at.isoformat()
            ),
            "priority_monitor_locator_last_elapsed_seconds": (
                priority_monitor_locator_last_elapsed_seconds
            ),
            "priority_monitor_locator_last_scheduled_count": (
                priority_monitor_locator_last_scheduled_count
            ),
            "priority_monitor_locator_last_attempted_count": (
                priority_monitor_locator_last_attempted_count
            ),
            "priority_monitor_locator_last_completed_count": (
                priority_monitor_locator_last_completed_count
            ),
            "priority_monitor_locator_observed_symbols_per_second": (
                None
                if priority_monitor_locator_observed_symbols_per_second is None
                else round(
                    priority_monitor_locator_observed_symbols_per_second,
                    6,
                )
            ),
            "priority_monitor_locator_deferred_count": len(
                priority_monitor_locator_deferred_codes
            ),
            "priority_monitor_locator_deferred_codes": list(
                priority_monitor_locator_deferred_codes
            ),
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
            "priority_monitor_last_round_elapsed_seconds": (
                priority_monitor_last_round_elapsed_seconds
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
            "candidate_monitor_configured_capacity_sufficient": (
                candidate_configured_capacity_sufficient
            ),
            "candidate_monitor_observed_capacity_sufficient": (
                candidate_observed_capacity_sufficient
            ),
            "candidate_monitor_required_symbols_per_second": round(
                candidate_required_symbols_per_second,
                6,
            ),
            "candidate_monitor_observed_symbols_per_second": (
                None
                if candidate_observed_symbols_per_second is None
                else round(candidate_observed_symbols_per_second, 6)
            ),
            "candidate_monitor_last_run_status": (
                "not_run"
                if priority_monitor_last_at is None
                else "degraded"
                if candidate_monitor_last_errors
                else "deferred"
                if candidate_monitor_last_deferred_codes
                else "complete_with_symbol_exclusions"
                if candidate_monitor_symbol_exclusions
                else "complete"
            ),
            "candidate_monitor_last_error_count": len(candidate_monitor_last_errors),
            "candidate_monitor_symbol_exclusion_count": len(
                candidate_monitor_symbol_exclusions
            ),
            "candidate_monitor_symbol_exclusion_codes": [
                str(value["code"]) for value in candidate_monitor_symbol_exclusions
            ],
            "candidate_monitor_symbol_exclusion_reason_counts": dict(
                sorted(candidate_monitor_symbol_exclusion_reason_counts.items())
            ),
            "candidate_monitor_symbol_exclusions": [
                copy.deepcopy(value) for value in candidate_monitor_symbol_exclusions
            ],
            "candidate_monitor_suspended_session": (
                None
                if candidate_monitor_suspended_session is None
                else candidate_monitor_suspended_session.isoformat()
            ),
            "candidate_monitor_current_session_suspended_code_count": len(
                candidate_monitor_current_session_suspended_codes
            ),
            "candidate_monitor_current_session_suspended_codes": list(
                candidate_monitor_current_session_suspended_codes
            ),
            "candidate_monitor_suspension_probe_status": (
                candidate_monitor_suspension_probe_status
            ),
            "candidate_monitor_suspension_probe_error": (
                candidate_monitor_suspension_probe_error
            ),
            "candidate_monitor_time_budget_seconds": (
                self._config.candidate_monitor_time_budget_seconds
            ),
            "candidate_monitor_supportive_discovery_max_sector_rank": (
                self._config.supportive_discovery_max_sector_rank
            ),
            "candidate_monitor_signal_pool_count": (
                candidate_monitor_signal_pool_count
            ),
            "candidate_monitor_signal_admitted_count": (
                candidate_monitor_signal_admitted_count
            ),
            "candidate_monitor_signal_deferred_count": (
                candidate_monitor_signal_deferred_count
            ),
            "candidate_monitor_signal_rotation_active": bool(
                candidate_monitor_signal_deferred_count
            ),
            "candidate_monitor_supportive_eligible_count": (
                candidate_monitor_supportive_eligible_count
            ),
            "candidate_monitor_supportive_admitted_count": (
                candidate_monitor_supportive_admitted_count
            ),
            "candidate_monitor_supportive_capacity": (
                candidate_monitor_supportive_capacity
            ),
            "candidate_notification_streaming_enabled": True,
            "candidate_notification_publish_batch_size": (
                CANDIDATE_NOTIFICATION_PUBLISH_BATCH_SIZE
            ),
            "priority_monitor_time_budget_seconds": (
                self._config.priority_monitor_time_budget_seconds
            ),
            "priority_monitor_bar_ready_offset_seconds": (
                PRIORITY_MONITOR_BAR_READY_OFFSET_SECONDS
            ),
            "priority_monitor_immediate_pool_count": (
                priority_monitor_immediate_pool_count
            ),
            "priority_monitor_immediate_deferred_count": (
                priority_monitor_immediate_deferred_count
            ),
            "candidate_monitor_last_deferred_count": len(
                candidate_monitor_last_deferred_codes
            ),
            "candidate_monitor_last_deferred_codes": list(
                candidate_monitor_last_deferred_codes
            ),
            "candidate_monitor_last_failure_reason_counts": dict(
                sorted(candidate_monitor_failure_reason_counts.items())
            ),
            "candidate_monitor_five_minute": {
                **five_candidate_coverage,
                "scope": (
                    "OWNED_WATCHED_EXISTING_AND_SUPPORTIVE_SECTOR_DISCOVERY"
                ),
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
            "screening_scope_mode": self._config.screening_scope_mode,
            "validation_cohort_size": self._config.validation_cohort_size,
            "effective_monitor_universe_limit": (
                self._config.effective_monitor_universe_limit
            ),
            "max_admitted_universe_symbols": (
                self._config.max_admitted_universe_symbols
            ),
            "large_scope_authorized": self._config.large_scope_authorized,
            "full_coverage_refresh_enabled": full_coverage_refresh_enabled,
            "full_coverage_force_until_complete_enabled": (
                self._config.force_full_coverage_until_complete
            ),
            "full_coverage_force_active": full_coverage_force_active,
            "full_coverage_auto_recovery_active": (
                full_coverage_auto_recovery_reason is not None
            ),
            "full_coverage_auto_recovery_reason": (
                full_coverage_auto_recovery_reason
            ),
            "full_coverage_scheduled_window_open": (
                full_coverage_scheduled_window_open
            ),
            "full_coverage_deferred_for_preselection_continuity": (
                continuity_defers_full_coverage
            ),
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
            "screening_review_error": getattr(
                self,
                "_review_readiness_error",
                None,
            ),
            "daily_preselection_ready": daily_preselection_ready,
            "daily_preselection_status": daily_preselection_status,
            "daily_preselection_reason_code": (daily_preselection_reason_code),
            "daily_preselection_candidate_count": (daily_preselection_candidate_count),
            "daily_preselection_buy_candidate_count": (
                daily_preselection_buy_candidate_count
            ),
            "daily_preselection_sell_candidate_count": (
                daily_preselection_sell_candidate_count
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
            "coverage_cycle_runtime_stock_scan_elapsed_ms": scan_audit.get(
                "coverage_cycle_runtime_stock_scan_elapsed_ms", 0
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
                # 深层复核契约校验期间，已通过哈希校验的完整发布仍作为页面来源。
                # 此时另启全量扫描只会与有界校验器争用资源，无法改善当前判定。
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

    def _preselection_continuity_defers_full_coverage(
        self,
        observed_at: datetime,
    ) -> bool:
        """Reserve structure workers for current signals during the live day.

        The authenticated previous-close continuity state already supplies the
        sector/member discovery route.  Starting a new sector snapshot between
        pre-open reconciliation and the post-close window would occupy a
        candidate worker for tens of minutes and make current 5m observations
        miss their freshness SLA.  An explicit operator-requested rebuild still
        takes precedence, and overnight/post-close work remains unchanged.
        """

        if self._config.force_full_coverage_until_complete:
            return False
        local_now = normalize_datetime(observed_at, "observed_at").astimezone(CN)
        with self._background_lock:
            continuity_active = (
                self._preselection_continuity_source_snapshot_sha256 is not None
            )
            target_session = self._preselection_continuity_target_session
        return bool(
            continuity_active
            and target_session == local_now.date().isoformat()
            and PREOPEN_RECONCILIATION_START
            <= local_now.time()
            < POST_CLOSE_PRESELECTION_START
        )

    def _forced_full_coverage_active(
        self,
        snapshot: Mapping[str, object],
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        """判断显式重建或隔离旧快照后的自动恢复是否仍未完成。

        显式重建、隔离旧快照和首份快照缺失都只改变调度时段，不降低缓存身份、
        覆盖完整性或人工复核发布门槛。若没有可认证的过渡预选，自动恢复仍会立即
        进入受限覆盖通道；已有过渡预选时，交易关键时段优先保障当前信号计算，收盘
        后再重建完整快照。使用本进程已经认证的快照哈希做常数时间判断，避免后台
        每分钟重新哈希大型快照。
        """

        automatic_recovery_reason = self._quarantined_cache_reason
        automatic_recovery = (
            automatic_recovery_reason
            in {
                "DECISION_CORE_IDENTITY_MISMATCH",
                "DECISION_SOURCE_REVISION_MISMATCH",
                "SELECTION_RESEARCH_REVISION_MISMATCH",
                "CURRENT_CACHE_CONTRACT_INVALID",
            }
            or snapshot.get("available") is not True
        )
        if automatic_recovery and self._preselection_continuity_defers_full_coverage(
            normalize_datetime(observed_at or self._clock(), "clock")
        ):
            automatic_recovery = False
        if not self._config.full_coverage_refresh_enabled or not (
            self._config.force_full_coverage_until_complete
            or automatic_recovery
        ):
            return False
        with self._state_lock:
            validated_snapshot_sha256 = self._validated_snapshot_sha256
        audit = snapshot.get("scan_audit")
        manifest = snapshot.get("coverage_manifest")
        quality = snapshot.get("data_quality")
        return not bool(
            snapshot.get("available") is True
            and isinstance(snapshot.get("snapshot_content_sha256"), str)
            and snapshot.get("snapshot_content_sha256") == validated_snapshot_sha256
            and isinstance(audit, Mapping)
            and audit.get("coverage_cycle_complete") is True
            and self._pending_symbol_count(snapshot) == 0
            and isinstance(manifest, Mapping)
            and manifest.get("complete") is True
            and isinstance(quality, Mapping)
            and quality.get("complete") is True
        )

    def _full_coverage_execution_window_open(
        self,
        snapshot: Mapping[str, object],
        observed_at: datetime,
    ) -> bool:
        # A fixed validation cohort is already the complete authorized universe.
        # Let it build a first snapshot immediately; this path remains impossible
        # without a non-empty exact allowlist and never enables full-market mode.
        if _configured_validation_cohort_codes(self._config):
            return True
        if not self._config.full_coverage_refresh_enabled:
            return False
        forced = self._forced_full_coverage_active(
            snapshot,
            observed_at=observed_at,
        )
        scheduled = _full_coverage_refresh_window_open(observed_at)
        continuity_deferred = self._preselection_continuity_defers_full_coverage(
            observed_at
        )
        return bool(forced or (scheduled and not continuity_deferred))

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
                # 检查状态前先清除事件，避免此处至 ``wait`` 之间的并发唤醒丢失。
                wake.clear()
                if stop.is_set():
                    break
                snapshot = self._snapshot_reference()
                has_pending = (
                    bool(self._pending_frequencies)
                    or self._immediate_pending_symbol_count(snapshot) > 0
                )
                has_backoff_retry = (
                    bool(self._backoff_frequencies)
                    or self._backoff_retry_symbol_count(snapshot) > 0
                )
                observed_at = normalize_datetime(self._clock(), "clock")
                forced_full_coverage_active = self._forced_full_coverage_active(
                    snapshot,
                    observed_at=observed_at,
                )
                coverage_window_open = self._full_coverage_execution_window_open(
                    snapshot,
                    observed_at,
                )
                priority_monitor_due = self._priority_monitor_due(observed_at)
                # 决策合同升级会隔离旧信号快照。全量行业重建可能持续数分钟，因此先用
                # 当前核心复核显式关注/持仓，页面和通知通道即可获得一份不含旧结论的
                # 快速观测；有认证过渡预选时，完整覆盖延后到非交易关键窗口。
                if coverage_window_open and self._startup_priority_bootstrap_required():
                    if self._scan_lock.locked():
                        wake.wait(timeout=1.0)
                        continue
                    self._record_background_refresh_start()
                    try:
                        bootstrapped = self.refresh_now(
                            copy_result=False,
                            priority_only=True,
                        )
                    except Exception as exc:
                        self._record_background_exception(exc)
                    else:
                        self._record_background_result(bootstrapped)
                    if stop.is_set():
                        break
                    self._record_background_heartbeat()
                    snapshot = self._snapshot_reference()
                    observed_at = normalize_datetime(self._clock(), "clock")
                    priority_monitor_due = self._priority_monitor_due(observed_at)
                if (
                    has_pending
                    or has_backoff_retry
                    or priority_monitor_due
                    or self._needs_refresh()
                    or forced_full_coverage_active
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
                        # 持久化与通知失败不属于刷新错误快照；保留工作线程存活，
                        # 同时避免无间隔重试。
                        self._record_background_exception(exc)
                        wake.wait(timeout=float(self._config.refresh_interval_seconds))
                        continue
                    self._record_background_result(refreshed)
                    if (
                        coverage_window_open
                        and refreshed.get("last_batch_state") == "complete"
                        and self._immediate_pending_symbol_count(refreshed) > 0
                    ):
                        # 按批次排空发现队列，使任务进度不依赖页面轮询。
                        continue
                    if stop.is_set():
                        break
                    if (
                        not coverage_window_open
                        and self._config.priority_monitoring_enabled
                        and _priority_monitor_compute_window_open(
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

                # 覆盖周期完成或批次失败后，从完成时刻开始安排下次尝试；慢批次超过
                # 名义刷新间隔时不得立即循环。
                wake.wait(timeout=float(self._config.refresh_interval_seconds))
        finally:
            with self._background_lock:
                if self._background_thread is current_thread():
                    self._background_thread = None

    def start_background(self) -> Thread:
        """幂等启动独立于页面的增量选股器。"""

        started_at = normalize_datetime(self._clock(), "clock")
        self._load_presentation_cached_sector_snapshot(observed_at=started_at)
        with self._background_lock:
            existing = self._background_thread
            if existing is not None and existing.is_alive():
                return existing
            self._background_stop = Event()
            self._background_wake = Event()
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
        """通知后台选股器退出，并报告是否已停止。"""

        with self._background_lock:
            worker = self._background_thread
            stop = self._background_stop
            wake = self._background_wake
        with self._priority_progress_launch_lock:
            priority_worker = self._priority_progress_thread
        if worker is None and priority_worker is None:
            return True
        stop.set()
        wake.set()
        if wait and worker is not None and worker is not current_thread():
            worker.join(timeout=timeout)
        if (
            wait
            and priority_worker is not None
            and priority_worker is not current_thread()
        ):
            priority_worker.join(timeout=timeout)
        stopped = bool(
            (worker is None or not worker.is_alive())
            and (priority_worker is None or not priority_worker.is_alive())
        )
        if stopped:
            with self._background_lock:
                if self._background_thread is worker:
                    self._background_thread = None
            with self._priority_progress_launch_lock:
                if self._priority_progress_thread is priority_worker:
                    self._priority_progress_thread = None
        return stopped

    def ensure_refresh(self) -> bool:
        snapshot = self._snapshot_reference()
        observed_at = normalize_datetime(self._clock(), "clock")
        forced_full_coverage_active = self._forced_full_coverage_active(
            snapshot,
            observed_at=observed_at,
        )
        if (
            not self._needs_refresh()
            and self._immediate_pending_symbol_count(snapshot) == 0
            and not forced_full_coverage_active
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
        coverage_window_open = self._full_coverage_execution_window_open(
            snapshot,
            observed_at,
        )
        Thread(
            # QMT 启动回调可能早于正式后台线程执行。该临时唤醒路径必须与后台循环
            # 使用完全相同的时段闸门，否则盘中会绕过优先通道并串行重建全部板块。
            target=lambda: self.refresh_now(
                copy_result=False,
                priority_only=not coverage_window_open,
            ),
            daemon=True,
            name="trading-screening",
        ).start()
        return True

    def notify_instrument_scope_changed(self) -> bool:
        """自选或持仓成员变化后唤醒优先通道。

        每轮优先扫描都会重新读取提供者，因此没有成员缓存需要失效。把运行观察标记为
        未校验，会让下一轮后台循环立即到期；唤醒事件则消除原本可能出现的一分钟延迟。
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
        """拒绝同一覆盖周期中任何已完成标的回退。"""

        previous = self._snapshot_reference()
        if cache_valid is None:
            cache_valid = _cache_is_valid(
                payload,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
                self._decision_source_snapshot_id,
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
            _remove_orphan_atomic_temporaries(self._cache_path)
            if cache_valid is None:
                cache_valid = _cache_is_valid(
                    payload,
                    self._config,
                    self._decision_core_id,
                    self._selection_research_revision,
                    self._decision_source_snapshot_id,
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
                    # 直接流式写入原子替换文件，避免每个覆盖批次再构造一份超过
                    # 100 MiB 的 ``json.dumps`` 字符串，引发内存峰值和 GC 停顿。
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
                        self._persist_cache_scope_sidecar(generation_path, payload)
                        generations = self._generation_paths()
                        for expired in generations[_CACHE_GENERATION_RETENTION:]:
                            expired.unlink(missing_ok=True)
                            self._cache_scope_sidecar_path(expired).unlink(
                                missing_ok=True
                            )
                        self._cache_generation_count = min(
                            len(generations), _CACHE_GENERATION_RETENTION
                        )
                        self._cache_generation_error = None
                    except OSError as exc:
                        # 备份失败必须可见，但不能阻止主快照的原子更新继续进行。
                        self._cache_generation_error = (
                            f"{type(exc).__name__}: {str(exc)[:160]}"
                        )
                self._cache_scope_sidecar_path(self._cache_path).unlink(
                    missing_ok=True
                )
                os.replace(temporary, self._cache_path)
                self._persist_cache_scope_sidecar(self._cache_path, payload)
                if not (
                    isinstance(manifest, Mapping) and manifest.get("complete") is True
                ):
                    self._last_incomplete_checkpoint_at = normalize_datetime(
                        self._clock(), "clock"
                    )
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _incomplete_checkpoint_due(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Throttle only large in-progress snapshots; final publication is exact."""

        manifest = payload.get("coverage_manifest")
        if isinstance(manifest, Mapping) and manifest.get("complete") is True:
            return True
        try:
            cache_size = self._cache_path.stat().st_size
        except OSError:
            return True
        if cache_size < _LARGE_INCOMPLETE_SNAPSHOT_BYTES:
            return True
        last = self._last_incomplete_checkpoint_at
        if last is None:
            return True
        observed_at = normalize_datetime(self._clock(), "clock")
        return (
            observed_at < last
            or (observed_at - last).total_seconds()
            >= self._config.incomplete_checkpoint_interval_seconds
        )

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

    def _reconcile_rule_recheck_after_current_snapshot(
        self,
        payload: Mapping[str, object],
    ) -> bool:
        """Let each current-core checkpoint retire work it already finalized.

        Completed and explicitly excluded symbols are authoritative even while a
        long full-market rebuild is still running.  Waiting for the final symbol
        before removing them from the migration queue wastes candidate capacity.
        Continuity itself is cleared only by a complete, audited snapshot.
        """

        manifest = payload.get("coverage_manifest")
        audit = payload.get("scan_audit")
        if (
            payload.get("decision_core_id") != self._decision_core_id
            or payload.get("decision_source_snapshot_id")
            != self._decision_source_snapshot_id
            or payload.get("selection_research_revision")
            != self._selection_research_revision
            or not isinstance(manifest, Mapping)
            or not isinstance(audit, Mapping)
        ):
            return False
        raw_completed_codes = manifest.get("completed_codes")
        raw_excluded_codes = manifest.get("excluded_codes")
        raw_failed_codes = manifest.get("failed_codes")
        if any(
            not isinstance(values, list)
            or any(not isinstance(code, str) for code in values)
            for values in (
                raw_completed_codes,
                raw_excluded_codes,
                raw_failed_codes,
            )
        ):
            return False
        complete = bool(
            manifest.get("complete") is True
            and audit.get("coverage_cycle_complete") is True
        )
        continuity_cleared = (
            self._clear_preselection_continuity() if complete else False
        )
        finalized_codes = set(raw_completed_codes).union(raw_excluded_codes)
        failed_codes = set(raw_failed_codes)
        with self._background_lock:
            retained = (
                self._decision_rule_recheck_pending_codes.intersection(failed_codes)
                if complete
                else self._decision_rule_recheck_pending_codes.difference(
                    finalized_codes
                )
            )
            if retained == self._decision_rule_recheck_pending_codes:
                return continuity_cleared
            self._decision_rule_recheck_pending_codes = retained
            self._decision_rule_recheck_last_attempted_codes = tuple(
                code
                for code in self._decision_rule_recheck_last_attempted_codes
                if code in retained
            )
            self._decision_rule_recheck_last_deferred_codes = tuple(
                code
                for code in self._decision_rule_recheck_last_deferred_codes
                if code in retained
            )
            self._decision_rule_recheck_last_errors = tuple(
                copy.deepcopy(error)
                for error in self._decision_rule_recheck_last_errors
                if error.get("code") in retained
            )
        return True

    def _finalize_snapshot_identity(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        scope_codes = _restored_snapshot_scope_codes(payload)
        payload["screening_scope_mode"] = self._config.screening_scope_mode
        payload["effective_monitor_universe_limit"] = (
            self._config.effective_monitor_universe_limit
        )
        payload["configured_admitted_codes"] = list(
            self._config.admitted_universe_codes
        )
        payload["admitted_universe_codes"] = list(
            self._config.admitted_universe_codes
            if self._config.screening_scope_mode != "FULL_MARKET"
            and self._config.admitted_universe_codes
            else (scope_codes or ())
        )
        payload["decision_source_snapshot_id"] = self._decision_source_snapshot_id
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
        deadline_monotonic: float | None = None,
        work_lane: str = "coverage",
    ) -> SymbolStructureBundle:
        """加载带有因果冻结月周日事实的当前低级别结构包。

        ``market_data_as_of`` 是原子板块和覆盖截止点。当前 1m 行情可能在该截止点之后、
        扫描时钟之前收盘；低级别信号保留该精度，而月周日证据时点必须等于
        ``min(bundle.as_of, market_data_as_of)``。每个提供者都必须公开明确感知截止点的
        方法。
        """

        observed = normalize_datetime(as_of, "as_of")
        cutoff = normalize_datetime(
            risk_evidence_cutoff,
            "risk_evidence_cutoff",
        )
        if cutoff > observed:
            raise ValueError("risk evidence cutoff cannot be after scan as_of")
        bounded_provider = None
        unbounded_lane_provider = None
        if work_lane not in {
            "coverage",
            "priority",
            "candidate",
            "candidate_overflow",
        }:
            raise ValueError("structure work lane is invalid")
        if work_lane == "priority":
            priority_provider = None
            if deadline_monotonic is not None:
                priority_provider = getattr(
                    self._market_data,
                    "priority_structure_bundle_with_risk_cutoff_until",
                    None,
                )
            if callable(priority_provider):
                bundle = priority_provider(
                    code,
                    as_of=observed,
                    sector=sector,
                    frequencies=frequencies,
                    risk_evidence_cutoff=cutoff,
                    deadline_monotonic=deadline_monotonic,
                )
            else:
                priority_provider = getattr(
                    self._market_data,
                    "priority_structure_bundle_with_risk_cutoff",
                    None,
                )
                provider = (
                    priority_provider
                    if callable(priority_provider)
                    else self._market_data.structure_bundle_with_risk_cutoff
                )
                bundle = provider(
                    code,
                    as_of=observed,
                    sector=sector,
                    frequencies=frequencies,
                    risk_evidence_cutoff=cutoff,
                )
        else:
            bounded_method = {
                "candidate": "candidate_structure_bundle_with_risk_cutoff_until",
                "candidate_overflow": (
                    "candidate_overflow_structure_bundle_with_risk_cutoff_until"
                ),
                "coverage": "structure_bundle_with_risk_cutoff_until",
            }[work_lane]
            bounded_provider = getattr(self._market_data, bounded_method, None)
            if not callable(bounded_provider) and work_lane in {
                "candidate",
                "candidate_overflow",
            }:
                bounded_provider = getattr(
                    self._market_data,
                    "candidate_structure_bundle_with_risk_cutoff_until",
                    None,
                )
            if not callable(bounded_provider) and work_lane in {
                "candidate",
                "candidate_overflow",
            }:
                bounded_provider = getattr(
                    self._market_data,
                    "structure_bundle_with_risk_cutoff_until",
                    None,
                )
            if deadline_monotonic is None and work_lane in {
                "candidate",
                "candidate_overflow",
            }:
                unbounded_lane_provider = getattr(
                    self._market_data,
                    "candidate_structure_bundle_with_risk_cutoff",
                    None,
                )
        if (
            work_lane != "priority"
            and deadline_monotonic is not None
            and callable(bounded_provider)
        ):
            bundle = bounded_provider(
                code,
                as_of=observed,
                sector=sector,
                frequencies=frequencies,
                risk_evidence_cutoff=cutoff,
                deadline_monotonic=deadline_monotonic,
            )
        elif work_lane != "priority" and callable(unbounded_lane_provider):
            bundle = unbounded_lane_provider(
                code,
                as_of=observed,
                sector=sector,
                frequencies=frequencies,
                risk_evidence_cutoff=cutoff,
            )
        elif work_lane != "priority":
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
        # ``scan_order_codes`` 只表示运行优先级，来源于已冻结的板块排序，绝不会
        # 从完整合格板块覆盖范围中删除标的。
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
        """在不重开覆盖的情况下提取有界同周期监听批次。

        市场截止点和范围周期完成后，重复观察活动信号仍可推进复核生命周期，但这不
        表示范围覆盖缺失，绝不能把它们放回 ``_pending_frequencies``。否则持久清单会
        每隔几分钟在完成与未完成之间震荡，并永久重复扫描未变化的收盘数据。
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
        self._coverage_runtime_stock_scan_elapsed_seconds = 0.0
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
        """判断 ``payload`` 是否为独立有效的最后正常页面。

        刷新诊断是运行事实，不是替代决策快照。完整周期原子发布后，网关整体故障必须
        保留该页面及其缓存，由后台健康路径报告失败并重试。
        """

        manifest = payload.get("coverage_manifest")
        audit = payload.get("scan_audit")
        quality = payload.get("data_quality")
        return bool(
            _cache_is_valid(
                payload,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
                self._decision_source_snapshot_id,
            )
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

        scan_lock = self._priority_scan_lock if priority_only else self._scan_lock
        if not scan_lock.acquire(blocking=False):
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
                    # 将诊断返回后台循环以记录故障，但不替换独立有效的页面数据
                    # 及其磁盘原子缓存。
                    return result(payload)
                # 身份刚由本进程对该私有对象树计算完成；此处只需校验语义契约。
                # 外部缓存加载与未知发布仍使用 ``_cache_is_valid`` 完整重算哈希。
                payload_valid = _cache_contract_is_valid(
                    payload,
                    self._config,
                    self._decision_core_id,
                    self._selection_research_revision,
                    self._decision_source_snapshot_id,
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
            payload_valid = _cache_contract_is_valid(
                payload,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
                self._decision_source_snapshot_id,
            )
            notification_context = payload.get("notification_context")
            notification_eligible = bool(
                isinstance(notification_context, Mapping)
                and notification_context.get("realtime_eligible") is True
            )
            # 先把新的实时事件交给分发器的持久去重/待发送账本，再推进选股
            # 快照。若进程在两步之间退出，旧快照会令事件在重启后重放，
            # 分发器按语义身份去重；反向顺序则可能永久丢掉首次通知。
            if self._notifier is not None and notification_eligible:
                self._notifier.dispatch_changes(previous, payload)
            if self._incomplete_checkpoint_due(payload):
                self._persist_atomic(
                    payload,
                    cache_valid=payload_valid,
                )
            else:
                self._assert_coverage_progress_non_regression(
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
            if self._reconcile_rule_recheck_after_current_snapshot(payload):
                self._persist_priority_monitor_state()
            return result(payload)
        finally:
            scan_lock.release()

    def _perform_incremental_refresh(
        self,
        previous: Mapping[str, object],
        *,
        priority_only: bool = False,
    ) -> dict[str, object]:
        batch_started_perf = time.perf_counter()
        observed_at = normalize_datetime(self._clock(), "clock")
        configured_allowlist = _configured_scope_allowlist(self._config)
        if configured_allowlist is not None:
            for frequencies in (
                self._pending_frequencies,
                self._backoff_frequencies,
                self._deferred_frequencies,
            ):
                rejected = set(frequencies).difference(configured_allowlist)
                self._coverage_cycle_discarded_retry_codes.update(rejected)
                for code in rejected:
                    frequencies.pop(code, None)
        if priority_only:
            force_startup_bootstrap = self._startup_priority_bootstrap_required()
            with self._background_lock:
                continuity_sector_batch = self._preselection_continuity_sector_batch
                continuity_sector_members = (
                    None
                    if self._preselection_continuity_sector_members is None
                    else dict(self._preselection_continuity_sector_members)
                )
                continuity_market_data_as_of = (
                    self._preselection_continuity_market_data_as_of
                )
                continuity_coverage_epoch_id = (
                    self._preselection_continuity_coverage_epoch_id
                )
                continuity_catalog_revision = (
                    self._preselection_continuity_sector_catalog_revision
                )
                continuity_signal_codes = tuple(
                    self._preselection_continuity_signal_codes
                )
                continuity_runtime_hydrated = (
                    self._preselection_continuity_sector_runtime_hydrated
                )
            continuity_sector_ready = bool(
                continuity_sector_batch is not None
                and continuity_sector_members is not None
                and continuity_market_data_as_of is not None
                and continuity_coverage_epoch_id
                and continuity_catalog_revision
            )
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
                        # 下方安全监听会发布精确到标的的原生失败，不替换已认证归档快照；
                        # 保持此标志为假，使下一分钟重试进程内路由恢复。
                        pass
                    else:
                        self._coverage_cycle_sector_runtime_hydrated = True
            if (
                not frozen_sector_ready
                and continuity_sector_ready
                and not continuity_runtime_hydrated
            ):
                restore_members = getattr(
                    self._sector_catalog,
                    "restore_authenticated_sector_members",
                    None,
                )
                if callable(restore_members):
                    try:
                        restore_members(
                            members=dict(continuity_sector_members or {}),
                            as_of=continuity_market_data_as_of,
                            catalog_revision=(continuity_catalog_revision or ""),
                        )
                    except Exception:
                        # Keep the authenticated application evidence intact and
                        # retry process-local routing hydration next minute.
                        pass
                    else:
                        with self._background_lock:
                            if (
                                self._preselection_continuity_coverage_epoch_id
                                == continuity_coverage_epoch_id
                            ):
                                self._preselection_continuity_sector_runtime_hydrated = True
            use_continuity_sectors = bool(
                not frozen_sector_ready and continuity_sector_ready
            )
            self._run_priority_monitor_safely(
                previous=previous,
                observed_at=observed_at,
                frozen_sector_batch=(
                    self._coverage_cycle_sector_batch
                    if frozen_sector_ready
                    else continuity_sector_batch
                    if use_continuity_sectors
                    else None
                ),
                frozen_sector_members=(
                    self._coverage_cycle_sector_members
                    if frozen_sector_ready
                    else continuity_sector_members
                    if use_continuity_sectors
                    else None
                ),
                frozen_sector_as_of=(
                    self._coverage_market_data_as_of
                    if frozen_sector_ready
                    else continuity_market_data_as_of
                    if use_continuity_sectors
                    else None
                ),
                frozen_coverage_epoch_id=(
                    self._coverage_epoch_id
                    if frozen_sector_ready
                    else continuity_coverage_epoch_id
                    if use_continuity_sectors
                    else None
                ),
                frozen_sector_source_mode=(
                    "PRESELECTION_CONTINUITY"
                    if use_continuity_sectors
                    else "FROZEN_COVERAGE_EPOCH"
                ),
                preselection_continuity_codes=continuity_signal_codes,
                force_startup_bootstrap=force_startup_bootstrap,
            )
            # 实时通道持久化自身紧凑且已认证的状态；返回原样归档快照可保证分钟观测
            # 不会消费、重排或重新发布覆盖任务。
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
            # 多批次盘中周期可能在收盘后仍未排空；稍后完成也不能把 14:35 的事实
            # 变成 15:00 收盘快照。下一批次前替换该待处理周期，确保日级前向样本
            # 只能来自新的完整收盘覆盖计划。
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
            # 一个覆盖周期对应一个行情快照。每个个股批次都重读全部板块合成数据
            # 不仅浪费数十秒，还会让后续批次使用不同板块状态和截止时刻。
            #
            # 完成周期在原进程和应用重启后都保持冻结。若只允许使用“重启恢复”的缓存，
            # 实时进程会在覆盖完成后的每次空闲刷新都重算板块，可能在保留旧个股文档时
            # 改变结构点身份，把两套证据混进同一快照。只有上方明确的盘前/盘后探测
            # 才允许完整周期查询新版板块目录。
            #
            # 原生进程代理在内存中保存成员路由。应用重启后可由已认证筛选快照直接预热；
            # 通用网关则回退到自身冻结时刻缓存或评估调用。成员精确一致时，以冻结评估为准。
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
                        # 通用传输必须按冻结覆盖时刻恢复，而非重启墙钟时刻。板块缓存
                        # 以因果 5m 周期为键；使用 ``observed_at`` 会错误拒绝生成本周期的缓存。
                        hydrated_batch = self._native_sector_assessments(
                            as_of=self._coverage_cycle_started_at
                        )
                        runtime_sector_members = dict(self._sector_catalog.members())
                self._coverage_cycle_sector_runtime_hydrated = True
            if runtime_sector_members == dict(cached_sector_members):
                as_of = self._coverage_cycle_started_at
                sector_batch = cached_sector_batch
            else:
                # 真实成员变化代表新标的池，不是可恢复的同周期传输细节；继续使用新认证
                # 批次，让常规周期替换闸门重放完整当前范围。
                reuse_cycle_sectors = False
                as_of = observed_at
                sector_batch = (
                    hydrated_batch
                    if hydrated_batch is not None
                    else self._native_sector_assessments(as_of=as_of)
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
            sector_batch = self._native_sector_assessments(as_of=as_of)
            sector_scan_duration_ms = round(
                (time.perf_counter() - sector_started_perf) * 1000,
                2,
            )
        # 一个完整覆盖批次可能正常跨越数分钟；在原生 QMT 调用之间记录进度，使心跳
        # 检测单次卡住的调用，而不是给整个健康批次计时。
        self._record_background_heartbeat()
        sector_ratio = sector_batch.completion_ratio
        sector_resolution_ratio = sector_batch.resolution_ratio
        sector_publishability_ratio = (
            sector_resolution_ratio if sector_batch.parent_relations else sector_ratio
        )
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
            "sector_publishability_ratio": str(sector_publishability_ratio),
            "sector_publishability_basis": (
                "completed_or_deterministically_excluded"
                if sector_batch.parent_relations
                else "completed_only"
            ),
            "sector_failure_counts": dict(sector_batch.failure_counts),
            "sector_exclusion_counts": dict(sector_batch.exclusion_counts),
        }
        sector_errors = [_sector_failure_document(item) for item in sector_batch.errors]
        sector_exclusions = [
            _sector_exclusion_document(item) for item in sector_batch.exclusions
        ]
        if sector_publishability_ratio < self._config.min_scan_completion_ratio:
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
                    "stock_worker_count": (
                        self._config.effective_full_coverage_worker_count
                    ),
                    "full_coverage_worker_limit": (
                        self._config.effective_full_coverage_worker_count
                    ),
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
        if reuse_cycle_sectors and cached_sector_members is not None:
            all_members = dict(cached_sector_members)
        else:
            all_members = dict(self._sector_catalog.members())
            self._coverage_cycle_sector_runtime_hydrated = True
        if configured_allowlist is not None:
            all_members = {
                sector_id: tuple(
                    code for code in members if code in configured_allowlist
                )
                for sector_id, members in all_members.items()
            }
        self._record_background_heartbeat()
        routing = _sector_member_routing(
            assessments=sector_batch.assessments,
            members_by_sector=all_members,
            parent_relations=sector_batch.parent_relations,
            unavailable_sector_ids=frozenset(failed_sector_ids),
            affinity_worker_count=(self._config.effective_full_coverage_worker_count),
        )
        ranked = routing.ranked
        ranked_ordinals = {row.assessment.sector_id: row.ordinal for row in ranked}
        # 所有结构合格板块都保留在解释层；标的执行范围则严格收敛为一个有效子行业
        # 或一个父行业回退，避免 GICS3/GICS4 重复扫描和重复信号。
        selected = ranked
        sector_members = dict(routing.effective_members_by_sector)
        selected_sector_by_code = dict(routing.eligible_sector_by_code)
        ranked_scan_codes = routing.ranked_scan_codes
        sector_audit.update(routing.audit)
        watchlist, rejected_watchlist = _validated_monitor_instrument_scope(
            self._market_data.active_watchlist_scope(),
            "active_watchlist_scope",
        )
        holdings, rejected_holdings = _validated_monitor_instrument_scope(
            self._market_data.holdings_scope(),
            "holdings_scope",
        )
        watchlist = _require_codes_in_configured_scope(
            watchlist,
            self._config,
            subject="coverage active watchlist scope",
        )
        holdings = _require_codes_in_configured_scope(
            holdings,
            self._config,
            subject="coverage holdings scope",
        )
        selected_member_codes = set(selected_sector_by_code)
        triggered_member_codes = {
            code
            for code, assessment in selected_sector_by_code.items()
            if assessment.regime == "supportive"
        }
        raw_previous_active_codes = set(
            _project_codes_to_configured_scope(
                tuple(
                    str(row.get("code"))
                    for row in previous.get("signals", ())
                    if isinstance(row, Mapping)
                    and isinstance(row.get("code"), str)
                    and row.get("lifecycle_stage")
                    not in {"closed", "invalidated"}
                ),
                self._config,
            )
        )
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
            ("MANUAL_ATTENTION_MONITOR", rejected_holdings),
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
                sources.append("MANUAL_ATTENTION_MONITOR")
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

            # 只有此时才能安全替换旧待处理计划：新板块快照已证明自身行情截止点包含收盘；
            # 若在证明前清理，QMT 尚未更新完本地历史时会丢失可恢复任务。
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
                "selection_research_revision": self._selection_research_revision,
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
            plan = _project_scan_plan_to_configured_scope(plan, self._config)
            if replacing_coverage_epoch:
                # 覆盖身份变化会清空完成账本。增量计划可只返回变化 K 线和监听标的，
                # 但该子集无法认证全新的完整板块清单；必须以同一冻结 d/30m/5m/1m
                # 结构输入重新纳入所有当前合格板块成员及显式监听标的。这是覆盖修复，
                # 不是参数或信号变化。
                full_scope = set(priority_codes).union(plan.symbols)
                validation_codes = _configured_validation_cohort_codes(self._config)
                if validation_codes:
                    full_scope.update(validation_codes)
                else:
                    full_scope.update(
                        member
                        for members in sector_members.values()
                        for member in members
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
                plan = _project_scan_plan_to_configured_scope(plan, self._config)
            # 即使板块合成截止点和目录版本未变，计划器仍可能发现真正的新代码；它属于
            # 发现任务而非同周期监听，必须进入常规可恢复覆盖队列。
            if monitoring_only_refresh and any(
                code not in self._coverage_cycle_discovered_codes
                for code in plan.symbols
            ):
                monitoring_only_refresh = False
            if not monitoring_only_refresh:
                # 重试队列属于生成它的标的池。新行情周期可重试仍有效成员，但目录或
                # 自选变化绝不能复活已移除/退市标的。``plan.symbols`` 还包含虽非板块
                # 成员或显式监听、但仍有效的自定义增量发现。
                retry_scope = selected_member_codes.union(
                    priority_codes,
                    plan.symbols,
                )
                backoff = self._backoff_frequencies
                self._backoff_frequencies = {}
                if configured_allowlist is not None:
                    rejected = set(backoff).difference(configured_allowlist)
                    self._coverage_cycle_discarded_retry_codes.update(rejected)
                    backoff = {
                        code: frequencies
                        for code, frequencies in backoff.items()
                        if code in configured_allowlist
                    }
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
                    if configured_allowlist is not None:
                        rejected = set(deferred).difference(configured_allowlist)
                        self._coverage_cycle_discarded_retry_codes.update(rejected)
                        deferred = {
                            code: frequencies
                            for code, frequencies in deferred.items()
                            if code in configured_allowlist
                        }
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
            # 待处理队列排空期间覆盖计划不可变；每批次重新规划会反复加入活跃自选，
            # 导致存在监听标的时周期永远无法完成。
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
        if self._priority_monitor_due(observed_at) and self._priority_scan_lock.acquire(
            blocking=False
        ):
            # 覆盖周期为保证因果完整而保持冻结；独立通道则用当前已完成分钟 K 线复查
            # 持仓、自选、活跃标的及轮换的支持板块样本。覆盖完成后它仍持续运行，
            # 因为常规数千标的游标无法为持仓风险提供一分钟时效。排除此批可避免重复
            # 原生 QMT 工作，且不改变两条通道的决策语义。
            try:
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
            finally:
                self._priority_scan_lock.release()
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
        sector_by_code: dict[str, SectorAssessment] = dict(
            routing.context_sector_by_code
        )
        coverage_work_lane = (
            "candidate"
            if not monitoring_only_refresh
            and _priority_monitor_compute_window_open(observed_at)
            else "coverage"
        )
        stock_instrument_status_probe_status = "not_required"
        stock_instrument_status_probe_error: str | None = None
        suspended_codes: frozenset[str] = frozenset()
        if symbols:
            status_provider = getattr(
                self._market_data,
                "current_session_instrument_statuses",
                None,
            )
            if callable(status_provider):
                status_session = (
                    (self._coverage_market_data_as_of or market_data_as_of)
                    .astimezone(CN)
                    .date()
                )
                try:
                    status_batch = status_provider(
                        tuple(sorted(set(symbols))),
                        session=status_session,
                    )
                    if not isinstance(
                        status_batch,
                        AShareInstrumentSessionStatusBatch,
                    ):
                        raise TypeError(
                            "instrument status provider returned an invalid batch"
                        )
                    suspended_codes = frozenset(
                        fact.code for fact in status_batch.facts if fact.suspended
                    )
                    stock_instrument_status_probe_status = "completed"
                except Exception as exc:
                    # 停牌探针不可用时不能猜测；保留原来的结构计算与陈旧检查，确保
                    # 行情传输故障仍按失败关闭，而不是把未知标的错误排除。
                    stock_instrument_status_probe_status = "failed"
                    stock_instrument_status_probe_error = (
                        f"{type(exc).__name__}: {exc}"[:400]
                    )
            else:
                stock_instrument_status_probe_status = "provider_unavailable"
        sector_audit.update(
            {
                "stock_instrument_status_probe_status": (
                    stock_instrument_status_probe_status
                ),
                "stock_instrument_status_probe_error": (
                    stock_instrument_status_probe_error
                ),
                "stock_current_session_suspended_code_count": len(suspended_codes),
            }
        )

        def evaluate_stock(code: str):
            sector = sector_by_code.get(
                code,
                SectorAssessment(
                    sector_id="unclassified",
                    sector_name="未匹配 QMT GICS3/GICS4 行业",
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_membership_missing",),
                ),
            )
            try:
                if code in suspended_codes:
                    raise ValueError("current_session_suspended")
                selection_sources = selection_sources_for(code)
                requested_frequencies = batch_frequencies.get(code, ())
                bundle = self._structure_bundle_with_causal_risk(
                    code,
                    as_of=as_of,
                    sector=sector,
                    frequencies=requested_frequencies,
                    risk_evidence_cutoff=(
                        self._coverage_market_data_as_of or market_data_as_of
                    ),
                    work_lane=coverage_work_lane,
                )
                bundle = replace(
                    bundle,
                    selection_sources=selection_sources,
                    selection_research=visible_selection_research(
                        self._selection_research,
                        symbol=code,
                        selection_path=bundle.selection_path,
                        decision_time=bundle.as_of,
                    ),
                )
                previous_lifecycles, previous_triggers = (
                    _previous_lifecycle_bundle_state(
                        previous_signals.values(),
                        code=code,
                        as_of=bundle.as_of,
                        decision_core_id=self._decision_core_id,
                    )
                )
                bundle = replace(
                    bundle,
                    previous_lifecycles=previous_lifecycles,
                    previous_trigger_points=previous_triggers,
                )
                if not _structure_bundle_is_current_for_intraday_evidence(
                    bundle,
                    observed_at=as_of,
                    max_age_seconds=self._config.max_structure_age_seconds,
                    requested_frequencies=requested_frequencies,
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
                            current_price=bundle.latest_price,
                            decision_core_id=self._decision_core_id,
                            selection_sources=bundle.selection_sources,
                            formal_selection_required=(self._formal_selection_required),
                            higher_timeframe_gates=(bundle.higher_timeframe_gates),
                        )
                    )
                completed += 1
                completed_codes.add(code)
            else:
                error = _stock_analysis_error_document(code, exc)
                if _is_coverage_exclusion(error):
                    exclusions.append(_stock_analysis_exclusion_document(error))
                    excluded_codes.add(code)
                else:
                    errors.append(error)
            self._record_background_heartbeat()

        worker_limit = (
            self._config.stock_worker_count
            if monitoring_only_refresh
            else self._config.effective_candidate_worker_count
            if coverage_work_lane == "candidate"
            else self._config.effective_full_coverage_worker_count
        )
        worker_count = min(worker_limit, max(1, len(symbols)))
        if worker_count == 1:
            for code in symbols:
                self._record_background_heartbeat()
                consume_stock_result(evaluate_stock(code))
        else:
            # 行情代理将每个标的确定性分配给一个隔离 QMT 工作进程。此处线程只协调
            # 已认证 IPC；CPU 密集结构计算在独立 Python 进程中使用物理核心，不争用
            # 全局解释器锁。``executor.map`` 保持标的顺序，使信号与拒绝文档逐字节确定。
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="TradingScreeningStock",
            ) as executor:
                for result in executor.map(evaluate_stock, symbols):
                    consume_stock_result(result)
        stock_scan_elapsed_seconds = max(
            0.0,
            time.perf_counter() - stock_started_perf,
        )
        stock_scan_duration_ms = round(stock_scan_elapsed_seconds * 1000, 2)
        if not monitoring_only_refresh:
            # 板块目录/强度证据可能在新周期首批耗时数十分钟，但那是一次性的覆盖
            # 初始化，不代表逐标的结构计算速度。吞吐与 ETA 只累计个股扫描墙钟；
            # 完整周期总耗时仍由 ``coverage_cycle_elapsed_ms`` 原样保留供审计。
            self._coverage_runtime_stock_scan_elapsed_seconds += (
                stock_scan_elapsed_seconds
            )
        failed_codes = tuple(
            code
            for code in symbols
            if code not in completed_codes and code not in excluded_codes
        )
        stock_batch_errors = errors[len(sector_errors) :]
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
                self._coverage_cycle_completed_codes.discard(code)
                self._coverage_cycle_failed_codes.discard(code)
                self._coverage_cycle_errors.pop(
                    f"stock_analysis_error:{code}",
                    None,
                )
            for code in failed_codes:
                self._coverage_cycle_excluded_codes.discard(code)
                self._coverage_cycle_exclusions.pop(code, None)
            self._record_cycle_exclusions(exclusions)
        if monitoring_only_refresh:
            # 全市场覆盖账本已经完成。同截止点复查失败时必须保留最近有效信号，仅作为
            # 运行健康问题展示；若加入 ``errors``，标的错误码会与清单失败码不一致，
            # 后续成功监听也无法恢复该周期。
            with self._background_lock:
                self._last_monitoring_at = as_of
                self._last_monitoring_errors = tuple(copy.deepcopy(errors))
            # 已完成覆盖发布不可变。上方板块探测可能在相同冻结截止点生成不同结构点身份，
            # 而有界监听只重评部分标的；此处重建页面会把保留的旧信号与新版块文档混合。
            # 优先监听已通过独立状态通道持久化并通知当前结果，因此日级预选快照必须
            # 保持逐字节不变。
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
        # 在批次完成闸门前先分类失败。若把低完成率批次整体重新入队，排序靠前的一簇
        # 确定性行情拒绝会垄断每次刷新，使冻结计划中后续有效标的永远无法访问。
        # 确定性失败在本行情周期内终止，传输失败进入按节奏退避队列；二者都完整记录
        # 在不可变清单和错误账本中。
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
            coverage_runtime_stock_scan_elapsed_ms = float(
                previous_scan_audit.get("coverage_cycle_runtime_stock_scan_elapsed_ms")
                or 0
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
            coverage_runtime_stock_scan_elapsed_ms = round(
                self._coverage_runtime_stock_scan_elapsed_seconds * 1000,
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
            elapsed_seconds = self._coverage_runtime_stock_scan_elapsed_seconds
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
            and previous.get("scan_state")
            in {"complete", "in_progress", "incomplete_not_published"}
            and coverage_completion >= self._config.min_scan_completion_ratio
        )
        sector_member_history_diagnostics = (
            None
            if sector_batch.strength_evidence is None
            else build_sector_member_history_diagnostics(sector_batch.strength_evidence)
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
            failed["generated_at"] = as_of.isoformat()
            failed["as_of"] = (
                self._coverage_market_data_as_of or market_data_as_of
            ).isoformat()
            failed["market_data_as_of"] = (
                self._coverage_market_data_as_of or market_data_as_of
            ).isoformat()
            failed["coverage_epoch_id"] = self._coverage_epoch_id
            failed["coverage_manifest"] = self._coverage_manifest(complete=False)
            # The checkpoint is a resumable state artifact even when its
            # partial stock decisions are not publishable.  Persist the exact
            # frozen sector batch and all of its provenance instead of pairing
            # a new manifest with the previous snapshot's top-level evidence.
            failed["sectors"] = [
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
            ]
            failed["sector_parent_relations"] = [
                list(value) for value in sector_batch.parent_relations
            ]
            failed["sector_strength_evidence"] = (
                None
                if sector_batch.strength_evidence is None
                else sector_batch.strength_evidence.evidence_document()
            )
            failed["sector_strength_evidence_revision"] = (
                None
                if sector_batch.strength_evidence is None
                else sector_batch.strength_evidence.evidence_revision
            )
            failed["sector_member_history_diagnostics"] = (
                sector_member_history_diagnostics
            )
            failed["monitor_instrument_exclusion_contract_id"] = (
                MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
            )
            failed["monitor_instrument_exclusions"] = monitor_instrument_exclusions
            failed["risk_limits"] = _risk_limits_document(self._risk_limits)
            failed["notification_context"] = _main_notification_context(
                observed_at=observed_at,
                market_data_as_of=(
                    self._coverage_market_data_as_of or market_data_as_of
                ),
                coverage_complete=False,
                monitoring_only_refresh=False,
                max_age_seconds=max(
                    180,
                    self._config.refresh_interval_seconds * 3,
                ),
            )
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
                    "full_coverage_worker_limit": (
                        self._config.effective_full_coverage_worker_count
                    ),
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
                    "coverage_cycle_runtime_stock_scan_elapsed_ms": (
                        coverage_runtime_stock_scan_elapsed_ms
                    ),
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
                not _is_current_selection_signal(row)
                or code in completed_codes
                or code in excluded_codes
                or code not in self._coverage_cycle_completed_codes
                or code not in retained_scope
            ):
                continue
            retained_row = copy.deepcopy(dict(row))
            # Retained rows must be projected with the same production policy as
            # freshly evaluated rows.  The helper's default intentionally keeps
            # offline research strict, so omitting this argument silently turned
            # the formal-research ledger back on after the first coverage batch.
            _apply_selection_scope(
                retained_row,
                selection_sources_for(code),
                formal_selection_required=self._formal_selection_required,
            )
            retained.append(retained_row)
        signals = retained + signals

        signals.sort(
            key=lambda row: (
                POINT_REVIEW_ORDER.index(str(row["point_type"])),
                str(row["code"]),
                str(row["signal_id"]),
            )
        )
        counts_by_stage: dict[str, int] = {}
        counts_by_point = {point_type: 0 for point_type in CANONICAL_POINT_TYPES}
        for row in signals:
            stage = str(row["lifecycle_stage"])
            counts_by_stage[stage] = counts_by_stage.get(stage, 0) + 1
            counts_by_point[str(row["point_type"])] += 1
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
        payload = {
            "schema": SCHEMA,
            "algorithm_id": self._config.algorithm_id,
            "structure_contract_id": self._config.structure_contract_id,
            "parameter_set_id": self._config.parameter_set_id,
            "decision_core_id": self._decision_core_id,
            "decision_core": copy.deepcopy(self._decision_core_document),
            "decision_source_snapshot_id": self._decision_source_snapshot_id,
            "selection_research_revision": self._selection_research_revision,
            "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
            "sector_coverage_contract_id": SECTOR_COVERAGE_CONTRACT_ID,
            "available": True,
            # ``scan_state`` describes the whole published scan, while
            # ``last_batch_state`` describes only this bounded batch.  Reporting
            # both as complete during a multi-hour coverage cycle caused API
            # consumers to treat a partial market snapshot as a completed one.
            "scan_state": ("complete" if coverage_cycle_complete else "in_progress"),
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
            "sector_parent_relations": [
                list(value) for value in sector_batch.parent_relations
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
                "full_coverage_worker_limit": (
                    self._config.effective_full_coverage_worker_count
                ),
                "coverage_cycle_elapsed_ms": coverage_cycle_elapsed_ms,
                "coverage_cycle_runtime_stock_scan_elapsed_ms": (
                    coverage_runtime_stock_scan_elapsed_ms
                ),
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
    "SCHEMA",
    "SIGNAL_DOCUMENT_CONTRACT_ID",
    "SectorCatalogGateway",
    "TradingScreeningConfig",
    "TradingScreeningService",
]
