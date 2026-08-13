"""只由统一人工辅助决策核心驱动的只读增量选股服务。"""

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
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    HigherTimeframeGateBundle,
    HigherTimeframeSessionEvidence,
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
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
from cl_app.services.trading_screening_gateway import (
    CANONICAL_REQUEST_BARS_BY_FREQUENCY,
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

    收盘到下一交易日的选股在 15:05 后构建，并连续运行到次日盘前核对结束。连续
    交易期间，每分钟预算归独立优先通道所有；若此时继续处理数千个归档标的，可能
    让当前告警阻塞数分钟。
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


def _priority_monitor_delay_seconds(
    observed_at: datetime,
    last_at: datetime | None,
    *,
    interval_seconds: int,
) -> float:
    """返回两次监听启动时刻之间的剩余间隔。"""

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
    """按运行紧迫度返回非持仓买入候选。

    卖点只有在标的已持仓或被明确关注时才可操作，这些标的由调用方放入强制通道。
    若让数百个非持仓纯卖出文档占用有界的当前分钟通道，明确自选标的可能等待多个
    轮次。此函数只改变观察顺序，既不创建也不删除任何归档信号或交易决策。
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
    """在真实容量内选取最早到期的行情节奏候选。

    目标是限制最大观察年龄，并不承诺每个标的在调度器每次一分钟触发时都重算。两根
    已完成 5m 行情之间形态不会变化，因此调度器把该范围分摊到五个分钟触发点。若
    上一轮较慢并跨过多个触发点，本轮份额会按比例增大；硬上限会通过健康状态暴露
    容量缺口，而不是无限阻塞 1m 通道。
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
    """描述真实节奏覆盖，不把未观察状态冒充当前状态。"""

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
        "stroke_mode": STRICT_STROKE_MODE,
        "center_source": "physical_timeframe_recursive_segments",
        "recursive_structure_used": True,
        "stock_structure_request_bars": dict(CANONICAL_REQUEST_BARS_BY_FREQUENCY),
        "stock_structure_qmt_dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        "provisional_point_source": "strict_approaching_ledger",
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
            or self.stock_worker_count <= 0
            or (
                self.full_coverage_worker_count is not None
                and self.full_coverage_worker_count <= 0
            )
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

    @property
    def effective_full_coverage_worker_count(self) -> int:
        """返回盘后覆盖可占用的最大结构工作进程数。"""

        configured = self.full_coverage_worker_count
        return min(
            self.stock_worker_count,
            self.stock_worker_count if configured is None else configured,
        )


def _initial_snapshot(
    config: TradingScreeningConfig,
    *,
    selection_research_revision: str,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "algorithm_id": config.algorithm_id,
        "structure_contract_id": config.structure_contract_id,
        "parameter_set_id": config.parameter_set_id,
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
            "full_coverage_worker_limit": (config.effective_full_coverage_worker_count),
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
    """构建单个审计信号的有界实时页面投影。

    不可变选股发布物和人工复核详情接口保留完整证据树。实时列表只需身份、筛选字段、
    四周期摘要及决策相关原因码。复制全部审计字段会让 1664 行响应达到约 27 MiB，
    浏览器或扩展可能在渲染前放弃请求。这里采用显式允许列表，防止新增审计字段再次
    悄然膨胀每分钟轮询页面。
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
    """判断拒绝是否属于已审计的本周期资格事实。"""

    return bool(
        error.get("reason_code") in COVERAGE_EXCLUSION_REASON_CODES
        and error.get("retry_policy") == "NEXT_MARKET_DATA_EPOCH"
        and error.get("deterministic_for_coverage_epoch") is True
    )


def _stock_analysis_exclusion_document(
    error: Mapping[str, object],
) -> dict[str, object]:
    """把最短历史拒绝转换为非成功排除。

    这里不会降低历史阈值，也不会把标记记为已完成；它只负责区分预期且确定性的范围
    资格结论与传输或行情失败。
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


def _cache_contract_is_valid(
    value: object,
    config: TradingScreeningConfig,
    decision_core_id: str,
    selection_research_revision: str,
) -> bool:
    """校验快照语义契约，不重复计算已由本进程生成的内容哈希。"""

    return bool(
        isinstance(value, Mapping)
        and value.get("schema") == SCHEMA
        and value.get("algorithm_id") == config.algorithm_id
        and value.get("structure_contract_id") == config.structure_contract_id
        and value.get("parameter_set_id") == config.parameter_set_id
        and value.get("read_only") is True
        and value.get("no_order_execution") is True
        and value.get("decision_core_id") == decision_core_id
        and value.get("selection_research_revision") == selection_research_revision
        and value.get("screening_policy") == _screening_policy_document()
        and value.get("screening_policy_id") == _screening_policy_id()
        and value.get("signal_document_contract_id") == SIGNAL_DOCUMENT_CONTRACT_ID
        and isinstance(value.get("snapshot_content_sha256"), str)
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


def _cache_is_valid(
    value: object,
    config: TradingScreeningConfig,
    decision_core_id: str,
    selection_research_revision: str,
) -> bool:
    """校验外部或持久化快照的语义契约与内容身份。"""

    return bool(
        _cache_contract_is_valid(
            value,
            config,
            decision_core_id,
            selection_research_revision,
        )
        and isinstance(value, Mapping)
        and value.get("snapshot_content_sha256")
        == live_screening_snapshot_content_sha256(value)
    )


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
        self._human_review_decision_source_snapshot_id: str | None = None
        if self._human_review_archive_root is not None:
            try:
                project_root = Path(__file__).resolve().parents[4]
                self._human_review_decision_source_snapshot_id = (
                    _current_review_decision_source_id(str(project_root))
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # 缺失实现身份时绝不能把旧回执视为当前回执；隔离校验器仍应
                # 给出内存判定和可执行的原因。
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
        # 持久化文档跨重启保留生命周期和幂等性，但新进程仍须立即证明自身 QMT 路由。
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
        self._snapshot = loaded_snapshot or _initial_snapshot(
            config,
            selection_research_revision=self._selection_research_revision,
        )
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
            or payload.get("selection_research_revision")
            != self._selection_research_revision
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
        """发布精简监听状态，不修改覆盖状态。"""

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
        """清理持仓和自选范围外过期的实时叠加卖点。

        已认证归档快照保持不变，只删除精简的当前分钟叠加状态。既未持仓也未明确关注的
        标的卖点没有实时操作归属，优先通道也不会再对其采样。
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
        """观察当前已完成行情，但不改变冻结覆盖。

        此通道有意不构成全市场快照，也不计入覆盖率。它推进持仓、自选风险及当前有利
        QMT 板块轮换样本的人工复核生命周期通知。无论较慢的认证覆盖周期仍在处理，
        还是已经完成，该通道都保持运行；普通完整周期游标可能跨越数千标的，不能让
        持仓标的等待整轮扫描。
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
            # 日级预选会冻结板块排序和成员。盘中通道只监听更新后的已完成个股 K 线，
            # 不得每分钟暗中重选板块；否则既会在盘中改变决策范围，也会在观测一个
            # 优先标的前串行重建全部 QMT 板块。
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
        # 当前观测成功但未产生记录时同样具有权威性。代码级墓碑可防止日级归档中的
        # 旧待触发记录在设置消失后永久重返 1m 通道，也避免新形成记录输给旧归档排序。
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
        # 已有买入候选按可能改变决策的 5m 设置节奏观测。更宽的冻结支持板块范围
        # 属于发现通道，每个 30 分钟窗口接受一次当前 5m+30m 评估。若把全部板块
        # 成员都当作五分钟候选，会虚报容量并拖延真正待触发的 1m 通道。
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
            # 当前 1m 触发只有绑定最新已完成 5m 设置才有意义；二者同时刷新可防止
            # 过期的 5m 缓存结构跨越五分钟边界继续生效。
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
                    selection_research=visible_selection_research(
                        self._selection_research,
                        symbol=code,
                        selection_path=bundle.selection_path,
                        decision_time=bundle.as_of,
                    ),
                )
                if code in holding_codes and bundle.physical_timeframe_recursive:
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
        """防止实时观察污染冻结覆盖周期。"""

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
                # 内存健康结果仍可观测；状态文件失败绝不能中止已认证覆盖批次。
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

    def _load_valid_cache(self) -> dict[str, object] | None:
        generations = self._generation_paths()
        self._cache_generation_count = len(generations)
        self._cache_recovered_from_generation = None
        try:
            current_value = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # 主快照缺失、截断或不可读可能源于原子替换中断；只有这类物理失败
            # 才允许回退到不可变历史代。
            pass
        else:
            if isinstance(current_value, dict) and _cache_is_valid(
                current_value,
                self._config,
                self._decision_core_id,
                self._selection_research_revision,
            ):
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
            # 可解析主快照若未通过语义、策略或内容哈希校验，说明被篡改或已过期，
            # 而非写入中断；必须关闭失败，不能用旧备份掩盖。
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
        """返回完整审计发布物的精简实时页面视图。

        不可变归档保留全部预热和映射诊断；浏览器只需与决策相关的摘要，而不必为每行
        重复原始点证据。单独维护该投影既保留回放和前向捕获使用的可审计契约，也避免
        分钟轮询复制并传输超过 100 MiB 的 JSON 树。
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

        # 读取健康计数时不深拷贝全市场信号/证据树；下方仍针对完整不可变发布
        # 重算身份，因此不会削弱篡改检测。
        with self._state_lock:
            snapshot = self._snapshot
            validated_snapshot_sha256 = self._validated_snapshot_sha256
        scan_state = str(snapshot.get("scan_state") or "unknown")
        last_batch_state = str(snapshot.get("last_batch_state") or scan_state)
        snapshot_available = snapshot.get("available") is True
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
                        # 持久化与通知失败不属于刷新错误快照；保留工作线程存活，
                        # 同时避免无间隔重试。
                        self._record_background_exception(exc)
                        wake.wait(timeout=float(self._config.refresh_interval_seconds))
                        continue
                    self._record_background_result(refreshed)
                    if (
                        coverage_window_open
                        and refreshed.get("scan_state") == "complete"
                        and self._immediate_pending_symbol_count(refreshed) > 0
                    ):
                        # 按批次排空发现队列，使任务进度不依赖页面轮询。
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

                # 覆盖周期完成或批次失败后，从完成时刻开始安排下次尝试；慢批次超过
                # 名义刷新间隔时不得立即循环。
                wake.wait(timeout=float(self._config.refresh_interval_seconds))
        finally:
            with self._background_lock:
                if self._background_thread is current_thread():
                    self._background_thread = None

    def start_background(self) -> Thread:
        """幂等启动独立于页面的增量选股器。"""

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
        """通知后台选股器退出，并报告是否已停止。"""

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
        observed_at = normalize_datetime(self._clock(), "clock")
        coverage_window_open = bool(
            self._config.full_coverage_refresh_enabled
            and _full_coverage_refresh_window_open(observed_at)
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
                    self._selection_research_revision,
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
                        generations = self._generation_paths()
                        for expired in generations[_CACHE_GENERATION_RETENTION:]:
                            expired.unlink(missing_ok=True)
                        self._cache_generation_count = min(
                            len(generations), _CACHE_GENERATION_RETENTION
                        )
                        self._cache_generation_error = None
                    except OSError as exc:
                        # 备份失败必须可见，但不能阻止主快照的原子更新继续进行。
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
                        # 下方安全监听会发布精确到标的的原生失败，不替换已认证归档快照；
                        # 保持此标志为假，使下一分钟重试进程内路由恢复。
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
                        hydrated_batch = self._sector_catalog.native_sector_assessments(
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
        # 一个完整覆盖批次可能正常跨越数分钟；在原生 QMT 调用之间记录进度，使心跳
        # 检测单次卡住的调用，而不是给整个健康批次计时。
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
        ranked = rank_sectors(assessments)
        # 每个结构合格的 QMT 板块都贡献其成员；排序只用于解释和执行顺序，不是 Top-N 截断。
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
        # 标的主板块取包含它的首个（排名最高）合格板块。GICS3 成员通常唯一；确定性
        # 回退还能避免低排名的重复支持板块与页面展示的板块文档矛盾。
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
            if replacing_coverage_epoch:
                # 覆盖身份变化会清空完成账本。增量计划可只返回变化 K 线和监听标的，
                # 但该子集无法认证全新的完整板块清单；必须以同一冻结 d/30m/5m/1m
                # 结构输入重新纳入所有当前合格板块成员及显式监听标的。这是覆盖修复，
                # 不是参数或信号变化。
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
        if self._priority_monitor_due(observed_at):
            # 覆盖周期为保证因果完整而保持冻结；独立通道则用当前已完成分钟 K 线复查
            # 持仓、自选、活跃标的及轮换的支持板块样本。覆盖完成后它仍持续运行，
            # 因为常规数千标的游标无法为持仓风险提供一分钟时效。排除此批可避免重复
            # 原生 QMT 工作，且不改变两条通道的决策语义。
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
                    selection_research=visible_selection_research(
                        self._selection_research,
                        symbol=code,
                        selection_path=bundle.selection_path,
                        decision_time=bundle.as_of,
                    ),
                )
                if code in holding_codes and bundle.physical_timeframe_recursive:
                    # 外部持仓没有原始严格点位谱系；附加最保守的基础级别持仓身份，
                    # 使任一同频递归卖点都能进入完整退出复核。
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

        worker_limit = (
            self._config.stock_worker_count
            if monitoring_only_refresh
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
            "selection_research_revision": self._selection_research_revision,
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
                "full_coverage_worker_limit": (
                    self._config.effective_full_coverage_worker_count
                ),
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
