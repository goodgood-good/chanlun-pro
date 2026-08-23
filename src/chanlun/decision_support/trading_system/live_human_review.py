"""把共享实时决策文档因果转换为人工复核提醒。

分阶段扫描器和历史回放共同使用 ``HumanAssistedDecisionCore``。本模块是唯一可以
把规范决策文档转换为复核提醒的位置；它保留 30m 环境、5m 正式买卖与 1m 段差
的区别，且永远不会创建具有下单能力的对象。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping
from zoneinfo import ZoneInfo

from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    validate_signal_decision_document,
    validate_human_assisted_contract_document,
)
from chanlun.decision_support.trading_system.lifecycle import (
    STRUCTURE_INVALIDATED_REASON_CODE,
    is_one_minute_segment_difference_document,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HARD_HIGHER_TIMEFRAME_DATA_INTEGRITY_REASON_CODES,
    HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID,
    QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE,
    QMT_SECTOR_SAME_BASE_SOURCE_MODE,
    HigherTimeframePeriodDiagnostic,
    QmtSectorSameBaseCoverageEvidence,
    sector_native_daily_research_bridge_contract,
)
from chanlun.decision_support.trading_system.models import (
    CANONICAL_POINT_TYPE_SET,
    EntryExecutionBoundary,
    parse_entry_execution_boundary_document,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    BUY_SIGNAL_PROTECTION_REASON_CODES,
    build_position_recommendation,
    parse_position_recommendation_document,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE,
    WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION,
    canonical_setup_state_document,
    execution_recommendation_label,
    unconfirmed_setup_reason_code,
    unconfirmed_setup_recommendation,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_QMT_30M_FALLBACK_REASON_CODE,
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_FREQUENCIES,
    SCREENING_WARMUP_REQUIRED_BARS,
    expected_screening_warmup_suffix_bar_count,
    five_minute_warmup_converged,
    screening_warmup_reason_code,
)
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.human_review_screening import (
    HUMAN_REVIEW_SCREEN_SCHEMA,
    MONITOR_ONLY_WARNING_CODE,
    HumanReviewAlert,
    HUMAN_REVIEW_ALERT_TYPES,
    human_review_alert_document,
    human_review_screening_parameters,
    market_symbol_higher_timeframe_review_evidence_from_risk,
    review_priority,
    sector_higher_timeframe_review_evidence_from_risk,
    sector_ranking_review_evidence_from_live_sector,
)
from chanlun.decision_support.trading_system.execution_policy import (
    SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,
)
from chanlun.decision_support.trading_system.selection import (
    HIGHER_TIMEFRAME_RISK_STATES,
    evaluate_formal_selection_gate,
    higher_timeframe_risk_gate,
    selection_research_snapshot_from_document,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    QmtNativeDailyCalendarCoverageEvidence,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    WarmupConvergenceDiagnosticEnvelope,
    WarmupConvergenceEnvelope,
    WarmupMappingSupplyDiagnosticEnvelope,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
    WarmupStructureLineageDiagnosticEnvelope,
)


LIVE_SCREENING_SCHEMA = "chanlun-trading-screening"
SIGNAL_DOCUMENT_CONTRACT_ID = (
    "chanlun-strict-human-assisted-signal-document-v6-geometric-candidate"
)
_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})
_RISK_PERIODS = ("M", "W", "D")
_RISK_DECISION_FIELDS = frozenset(
    {
        "market_gate",
        "sector_gate",
        "symbol_gate",
        "market_states",
        "sector_states",
        "symbol_states",
        "market_reason_codes",
        "sector_reason_codes",
        "symbol_reason_codes",
        "reason_codes",
        "data_integrity_hard_block_reason_codes",
        "market_period_diagnostics",
        "sector_period_diagnostics",
        "symbol_period_diagnostics",
        "new_entry_requires_all_green",
    }
)
_RISK_CURRENT_EVIDENCE_FIELDS = frozenset(
    {
        "session_evidence_contract_id",
        "market_session_evidence",
        "sector_session_evidence",
        "symbol_session_evidence",
        "warmup_evidence_contract_id",
        "market_warmup_evidence",
        "sector_warmup_evidence",
        "symbol_warmup_evidence",
        "warmup_convergence_contract_id",
        "market_warmup_convergence_evidence",
        "sector_warmup_convergence_evidence",
        "symbol_warmup_convergence_evidence",
        "warmup_convergence_diagnostic_contract_id",
        "market_warmup_convergence_diagnostic_evidence",
        "sector_warmup_convergence_diagnostic_evidence",
        "symbol_warmup_convergence_diagnostic_evidence",
        "warmup_mapping_supply_diagnostic_contract_id",
        "market_warmup_mapping_supply_diagnostic_evidence",
        "sector_warmup_mapping_supply_diagnostic_evidence",
        "symbol_warmup_mapping_supply_diagnostic_evidence",
        "warmup_structure_lineage_diagnostic_contract_id",
        "market_warmup_structure_lineage_diagnostic_evidence",
        "sector_warmup_structure_lineage_diagnostic_evidence",
        "symbol_warmup_structure_lineage_diagnostic_evidence",
        "native_daily_reconciliation_contract_id",
        "market_native_daily_reconciliation_evidence",
        "sector_native_daily_reconciliation_evidence",
        "symbol_native_daily_reconciliation_evidence",
        "native_daily_calendar_coverage_contract_id",
        "market_native_daily_calendar_coverage_evidence",
        "sector_native_daily_calendar_coverage_evidence",
        "symbol_native_daily_calendar_coverage_evidence",
    }
)
_RISK_SECTOR_SOURCE_FIELDS = frozenset(
    {
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_strict_same_5m_source_coverage_evidence",
        "sector_research_bridge_parameter_set_id",
        "sector_strict_same_5m_warmup_convergence_evidence",
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence",
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence",
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence",
    }
)
_SESSION_ISSUE_CODES = frozenset(
    {
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
        "QMT_ONE_MINUTE_SESSION_GRID_INVALID",
    }
)
_QMT_MWD_WARMUP_BLOCKING_CODES = frozenset(
    {
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED",
    }
)
_LIFECYCLE_STAGES = frozenset(
    {"approaching", "formed", "armed", "observed", "triggered", "executable"}
)
_SNAPSHOT_AUDIT_STAGES = frozenset({"invalidated"})
_SELECTION_SOURCES = frozenset(
    {
        "QMT_SECTOR_TRIGGER",
        "QMT_SECTOR_ELIGIBLE_SCOPE",
        "ACTIVE_WATCHLIST_MONITOR",
        "MANUAL_ATTENTION_MONITOR",
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
        "PREVIOUS_SIGNAL_MONITOR",
        "DECISION_RULE_RECHECK",
        "PRESELECTION_CONTINUITY_RECHECK",
        "INCREMENTAL_SCAN_SCOPE",
    }
)
MIN_LIVE_REVIEW_STOCK_COVERAGE = Decimal("0.75")
COVERAGE_MANIFEST_SCHEMA = "chanlun-screening-coverage-manifest"
COVERAGE_STATE_CONTRACT_ID = "chanlun-screening-coverage-state"
COVERAGE_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "coverage_state_contract_id",
        "signal_document_contract_id",
        "coverage_epoch_id",
        "screening_policy_id",
        "source_cutoff",
        "market_data_as_of",
        "universe_revision",
        "sector_catalog_revision",
        "sector_strength_evidence_revision",
        "superseded_coverage_epoch_id",
        "superseded_market_data_as_of",
        "discovered_codes",
        "completed_codes",
        "excluded_codes",
        "failed_codes",
        "exclusions",
        "discarded_out_of_scope_retry_codes",
        "pending_frequencies",
        "backoff_frequencies",
        "deferred_frequencies",
        "complete",
        "batch_count",
    }
)
SECTOR_COVERAGE_CONTRACT_ID = "chanlun-screening-sector-coverage"
MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID = "chanlun-monitor-instrument-exclusion"
COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON = {
    "KLINE_MINIMUM_HISTORY_NOT_MET": "INSUFFICIENT_MINIMUM_HISTORY",
    "CURRENT_SESSION_SUSPENDED": "CURRENT_SESSION_SUSPENDED",
}
COVERAGE_EXCLUSION_REASON_CODES = frozenset(COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON)
_MONITOR_SELECTION_SOURCES = frozenset(
    {
        "ACTIVE_WATCHLIST_MONITOR",
        "MANUAL_ATTENTION_MONITOR",
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
        "PREVIOUS_SIGNAL_MONITOR",
        "DECISION_RULE_RECHECK",
    }
)
_LIVE_REVIEW_VALIDATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class _ValidatedLiveReviewSnapshot:
    """Process-local proof that one exact in-memory snapshot passed validation."""

    seal: object
    payload: Mapping[str, object]
    snapshot_content_sha256: str
    review_at: datetime
    signals: tuple[Mapping[str, object], ...]
    session: date | None


def _is_sha256_identity(value: object) -> bool:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _native_daily_reconciliation_evidence_is_consistent(
    raw: object,
    *,
    expected_symbol: str | None,
    evidence_cutoff: datetime,
) -> bool:
    """验证已认证原生日线桥接证据的展示副本。"""

    if not isinstance(raw, Mapping):
        return False
    required_fields = {
        "contract_id",
        "symbol",
        "observed_at",
        "native_daily_bar_count",
        "one_minute_daily_bar_count",
        "overlap_session_count",
        "first_overlap_session",
        "last_overlap_session",
        "native_daily_content_revision",
        "one_minute_base_revision",
        "price_basis_revision",
        "trading_calendar_revision",
        "price_tolerance_quanta",
        "price_difference_count",
        "price_difference_session_count",
        "price_difference_identities",
        "max_observed_price_difference_quanta",
        "reconciled_source_revision",
        "all_overlap_ohlcv_equal",
        "all_overlap_ohlcv_within_declared_tolerance",
        "native_daily_role",
        "intraday_role",
        "live_status",
    }
    if set(raw) != required_fields or raw.get("contract_id") != (
        QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
    ):
        return False
    symbol = raw.get("symbol")
    if (
        not isinstance(symbol, str)
        or re.fullmatch(r"(?:SH|SZ|BJ)\.[0-9]{6}", symbol) is None
        or (expected_symbol is not None and symbol != expected_symbol)
    ):
        return False
    try:
        observed_at = datetime.fromisoformat(str(raw.get("observed_at")))
        first = date.fromisoformat(str(raw.get("first_overlap_session")))
        last = date.fromisoformat(str(raw.get("last_overlap_session")))
    except (TypeError, ValueError):
        return False
    if (
        observed_at.tzinfo is None
        or observed_at != evidence_cutoff
        or first > last
        or last > evidence_cutoff.date()
    ):
        return False
    counts = tuple(
        raw.get(field)
        for field in (
            "native_daily_bar_count",
            "one_minute_daily_bar_count",
            "overlap_session_count",
        )
    )
    if any(type(value) is not int or value <= 0 for value in counts):
        return False
    native_count, minute_count, overlap_count = counts
    if overlap_count > min(native_count, minute_count) or (overlap_count == 1) != (
        first == last
    ):
        return False
    for field in (
        "native_daily_content_revision",
        "one_minute_base_revision",
        "price_basis_revision",
        "trading_calendar_revision",
        "reconciled_source_revision",
    ):
        if not _is_sha256_identity(raw.get(field)):
            return False
    tolerance = raw.get("price_tolerance_quanta")
    maximum = raw.get("max_observed_price_difference_quanta")
    identities = _unique_string_list(raw.get("price_difference_identities"))
    if (
        type(tolerance) is not int
        or tolerance not in {0, 1}
        or type(maximum) is not int
        or maximum < 0
        or maximum > tolerance
        or identities is None
        or raw.get("price_difference_count") != len(identities)
    ):
        return False
    difference_sessions: set[date] = set()
    for identity in identities:
        matched = re.fullmatch(
            r"([0-9]{4}-[0-9]{2}-[0-9]{2}):(open|high|low|close)",
            identity,
        )
        if matched is None:
            return False
        try:
            session = date.fromisoformat(matched.group(1))
        except ValueError:
            return False
        if not first <= session <= last:
            return False
        difference_sessions.add(session)
    if (
        raw.get("price_difference_session_count") != len(difference_sessions)
        or maximum != (1 if identities else 0)
        or raw.get("all_overlap_ohlcv_equal") is not (not identities)
        or raw.get("all_overlap_ohlcv_within_declared_tolerance") is not True
        or raw.get("native_daily_role") != "LEFT_HISTORY_BEFORE_ONE_MINUTE_BASE_ONLY"
        or raw.get("intraday_role") != "ONE_MINUTE_DERIVED_30M_AND_DAILY_TAIL"
        or raw.get("live_status") != "LIVE_DISABLED"
    ):
        return False
    return True


def _native_daily_calendar_coverage_evidence_is_consistent(
    raw: object,
    *,
    expected_symbol: str | None,
    evidence_cutoff: datetime,
    side_reasons: tuple[str, ...],
    side_gate: object,
) -> bool:
    """验证缺口证据，但不把缺失 K 线直接认定为停牌。"""

    if not isinstance(raw, Mapping):
        return False
    try:
        evidence = QmtNativeDailyCalendarCoverageEvidence.from_document(raw)
    except (TypeError, ValueError):
        return False
    if (
        evidence.observed_at != evidence_cutoff
        or (expected_symbol is not None and evidence.symbol != expected_symbol)
        or evidence.native_last_session > evidence_cutoff.date()
        or evidence.calendar_last_session > evidence_cutoff.date()
    ):
        return False
    mismatch = evidence.status != "EXACT"
    calendar_reason = "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in side_reasons
    return mismatch == (calendar_reason and side_gate == "UNRESOLVED")


def screening_coverage_epoch_id(
    *,
    market_data_as_of: datetime,
    universe_revision: str,
    sector_catalog_revision: str,
    sector_strength_evidence_revision: str | None,
    decision_core_id: str,
    screening_policy_id: str,
    structure_contract_id: str,
    parameter_set_id: str,
    signal_document_contract_id: str = SIGNAL_DOCUMENT_CONTRACT_ID,
) -> str:
    """返回选股覆盖批次的唯一规范身份。

    Web 生产路径与不可变前向验证器必须使用相同推导。尤其是，新计算的横向强度批次
    不能仅因收盘时间和 QMT 成员目录相同，就复用由另一组强度/排名输入采集的个股结果。

    ``None`` 仅保留给明确不做审计的测试或展示适配器；可进入前向流程的快照仍必须
    单独具备真实的强度证据修订号。
    """

    observed = normalize_datetime(market_data_as_of, "market_data_as_of")
    for field_name, value in (
        ("universe_revision", universe_revision),
        ("sector_catalog_revision", sector_catalog_revision),
        ("decision_core_id", decision_core_id),
        ("screening_policy_id", screening_policy_id),
    ):
        if not _is_sha256_identity(value):
            raise ValueError(f"{field_name} must be a sha256 identity")
    if sector_strength_evidence_revision is not None and not _is_sha256_identity(
        sector_strength_evidence_revision
    ):
        raise ValueError("sector_strength_evidence_revision must be a sha256 identity")
    for field_name, value in (
        ("structure_contract_id", structure_contract_id),
        ("parameter_set_id", parameter_set_id),
        ("signal_document_contract_id", signal_document_contract_id),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} must be a non-empty string")
    return sha256_json(
        {
            "schema": "chanlun-screening-coverage-epoch",
            "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
            "signal_document_contract_id": signal_document_contract_id,
            "market_data_as_of": observed,
            "universe_revision": universe_revision,
            "sector_catalog_revision": sector_catalog_revision,
            "sector_strength_evidence_revision": (sector_strength_evidence_revision),
            "decision_core_id": decision_core_id,
            "screening_policy_id": screening_policy_id,
            "structure_contract_id": structure_contract_id,
            "parameter_set_id": parameter_set_id,
        }
    )


def live_screening_semantic_snapshot_document(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Web 与每日归档共同使用的规范市场语义文档。

    墙钟耗时和节奏计数器不会形成新的市场决策；所有能改变候选、覆盖或来源谱系的字段
    都必须纳入。把实现保留在 ``src`` 中，可避免前向工具出现第二套较弱的哈希解释。
    """

    # 哈希只读取文档。全市场实时快照深拷贝可能超过 40 MiB，过去每次就绪探测都会执行；
    # 现在只复制实际需要规范化的两层映射，其余嵌套值作为 ``sha256_json`` 只读输入。
    stable = dict(payload)
    for field in (
        "generated_at",
        "scanned_at",
        "snapshot_content_sha256",
        # 通知资格属于运行墙钟闸门，只决定已认证市场转变能否发布，不会创建不同的
        # 市场决策或覆盖周期。
        "notification_context",
    ):
        stable.pop(field, None)
    raw_audit = stable.get("scan_audit")
    if isinstance(raw_audit, Mapping):
        audit = dict(raw_audit)
        stable["scan_audit"] = audit
        for field in (
            "batch_duration_ms",
            "sector_scan_duration_ms",
            "stock_scan_duration_ms",
            "coverage_cycle_elapsed_ms",
            "coverage_cycle_batch_count",
            "coverage_cycle_started_at",
            "planned_symbol_count",
            "completed_symbol_count",
            "excluded_symbol_count",
            "completion_ratio",
            "batch_resolution_ratio",
            "planned_frequencies",
            "background_full_refresh_required",
            "monitoring_only_refresh",
            "monitoring_symbol_count",
        ):
            audit.pop(field, None)
    raw_manifest = stable.get("coverage_manifest")
    if isinstance(raw_manifest, Mapping):
        manifest = dict(raw_manifest)
        stable["coverage_manifest"] = manifest
        manifest.pop("batch_count", None)
        manifest.pop("source_cutoff", None)
    return stable


def live_screening_snapshot_content_sha256(
    payload: Mapping[str, object],
) -> str:
    return sha256_json(live_screening_semantic_snapshot_document(payload))


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _entry_boundary_from_document(raw: object) -> EntryExecutionBoundary | None:
    if raw is None:
        return None
    try:
        return parse_entry_execution_boundary_document(raw)
    except ValueError as exc:
        if "identity changed" in str(exc):
            raise ValueError(
                "live screening entry execution boundary identity changed"
            ) from exc
        raise ValueError(
            "live screening entry execution boundary is malformed"
        ) from exc


def _source_screening_policy_id(payload: Mapping[str, object]) -> str:
    """返回强制认证的选股策略身份。"""

    policy = payload.get("screening_policy")
    declared = payload.get("screening_policy_id")
    if (
        isinstance(policy, Mapping)
        and isinstance(declared, str)
        and declared.startswith("sha256:")
        and declared == sha256_json(policy)
    ):
        return declared
    raise ValueError("live screening policy identity is unavailable or invalid")


def _canonical_code_list(raw: object, field: str) -> list[str]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(value, str) or not value for value in raw)
        or raw != sorted(set(raw))
    ):
        raise ValueError(f"live screening {field} is not canonical")
    return raw


def _frequency_code_set(raw: object, field: str) -> set[str]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"live screening {field} is malformed")
    codes: set[str] = set()
    for code, frequencies in raw.items():
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(frequencies, list)
            or not frequencies
            or any(not isinstance(value, str) or not value for value in frequencies)
            or frequencies != list(dict.fromkeys(frequencies))
        ):
            raise ValueError(f"live screening {field} is malformed")
        codes.add(code)
    return codes


def _coverage_exclusion_documents(raw: object) -> dict[str, str]:
    """把当前批次的资格排除项验证为带身份的事实。

    无法满足冻结最小历史要求的标的既不算分析成功，也不属于运行故障；它是当前行情
    批次对标的池的明确处置，待新批次能提供更多已完成 K 线时必须重试。清单保存完整
    规范化文档，可防止单纯的 ``excluded_codes`` 列表成为未经审计、任意隐藏扫描失败的
    通道。
    """

    if not isinstance(raw, list):
        raise ValueError("live screening exclusions are malformed")
    expected_keys = {
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "retry_policy",
        "deterministic_for_coverage_epoch",
        "remote_error_type",
        "reason",
    }
    output: dict[str, str] = {}
    ordered_codes: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            raise ValueError("live screening exclusion document is malformed")
        code = document.get("code")
        reason_code = document.get("reason_code")
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(reason_code, str)
            or reason_code not in COVERAGE_EXCLUSION_REASON_CODES
            or document.get("exclusion_type") != "stock_analysis_exclusion"
            or document.get("eligibility")
            != COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON[reason_code]
            or document.get("retry_policy") != "NEXT_MARKET_DATA_EPOCH"
            or document.get("deterministic_for_coverage_epoch") is not True
            or not isinstance(document.get("remote_error_type"), str)
            or not document.get("remote_error_type")
            or not isinstance(document.get("reason"), str)
            or not document.get("reason")
        ):
            raise ValueError("live screening exclusion document is malformed")
        ordered_codes.append(code)
        output[code] = reason_code
    if ordered_codes != sorted(set(ordered_codes)) or len(output) != len(raw):
        raise ValueError("live screening exclusions are not canonical")
    return output


def _monitor_instrument_exclusions_are_consistent(
    payload: Mapping[str, object],
    *,
    audit: Mapping[str, object],
    manifest: Mapping[str, object],
) -> bool:
    """认证只读的 QMT 标的类型排除项。

    这些记录不参与信号生成，但用于解释明确自选股、虚拟持仓或历史信号为何未进入选股
    标的池。只有外层快照哈希无法区分真实文档与伪造后重新哈希的文档，因此实时水位线
    和不可变复核边界都会强制执行这份精确语义契约。
    """

    raw = payload.get("monitor_instrument_exclusions")
    raw_count = audit.get("monitor_instrument_exclusion_count")
    if (
        payload.get("monitor_instrument_exclusion_contract_id")
        != MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
        or not isinstance(raw, list)
        or type(raw_count) is not int
        or raw_count != len(raw)
    ):
        return False
    expected_keys = {
        "code",
        "eligibility",
        "reason_code",
        "selection_sources",
        "evidence_source",
        "qmt_instrument_type",
        "diagnostic_only",
        "tick_data_used",
        "real_account_accessed",
        "real_order_transport_enabled",
        "live_status",
    }
    discovered_sets: list[set[str]] = []
    for field in (
        "discovered_codes",
        "completed_codes",
        "excluded_codes",
        "failed_codes",
    ):
        values = manifest.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            return False
        discovered_sets.append(set(values))
    covered_codes = set().union(*discovered_sets)
    ordered_codes: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            return False
        code = document.get("code")
        sources = document.get("selection_sources")
        instrument_type = document.get("qmt_instrument_type")
        unresolved = instrument_type == "unresolved_cn"
        if (
            not isinstance(code, str)
            or re.fullmatch(r"(?:SH|SZ|BJ)\.\d{6}", code) is None
            or code in covered_codes
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) for source in sources)
            or sources != sorted(set(sources))
            or not set(sources).issubset(_MONITOR_SELECTION_SOURCES)
            or instrument_type
            not in {"index_cn", "fund_cn", "unsupported_cn", "unresolved_cn"}
            or document.get("eligibility")
            != (
                "UNRESOLVED_FROM_TRADING_SCREENING"
                if unresolved
                else "EXCLUDED_FROM_TRADING_SCREENING"
            )
            or document.get("reason_code")
            != (
                "QMT_NATIVE_INSTRUMENT_TYPE_UNRESOLVED"
                if unresolved
                else "QMT_NATIVE_STOCK_OR_ETF_REQUIRED"
            )
            or document.get("evidence_source") != "QMT_GET_INSTRUMENT_TYPE"
            or document.get("diagnostic_only") is not True
            or document.get("tick_data_used") is not False
            or document.get("real_account_accessed") is not False
            or document.get("real_order_transport_enabled") is not False
            or document.get("live_status") != "LIVE_DISABLED"
        ):
            return False
        ordered_codes.append(code)
    return ordered_codes == sorted(set(ordered_codes))


def monitor_instrument_exclusions_are_consistent(
    payload: Mapping[str, object],
) -> bool:
    """返回页面与归档的监听诊断契约是否完全一致。"""

    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    return bool(
        isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and _monitor_instrument_exclusions_are_consistent(
            payload,
            audit=audit,
            manifest=manifest,
        )
    )


def coverage_manifest_dispositions_are_consistent(
    manifest: Mapping[str, object],
    errors: object,
) -> bool:
    """统一校验覆盖结果、重试队列与错误证据的关系。

    ``completed_codes`` 表示仍有可复用的最近成功证据，``failed_codes`` 表示最近一次
    尝试失败。因此只要失败文档与即时、退避或下一周期队列精确匹配，二者可以重叠；
    确定性排除始终与成功、失败互斥。该规则由生成、恢复和人工复核共同使用，防止同一
    份快照在写入端有效、重启端却无法恢复。
    """

    if not isinstance(errors, list):
        return False
    try:
        discovered = set(
            _canonical_code_list(manifest.get("discovered_codes"), "discovered codes")
        )
        completed = set(
            _canonical_code_list(manifest.get("completed_codes"), "completed codes")
        )
        failed = set(_canonical_code_list(manifest.get("failed_codes"), "failed codes"))
        excluded = set(
            _canonical_code_list(manifest.get("excluded_codes"), "excluded codes")
        )
        pending = _frequency_code_set(
            manifest.get("pending_frequencies"), "pending frequencies"
        )
        backoff = _frequency_code_set(
            manifest.get("backoff_frequencies"), "backoff frequencies"
        )
        deferred = _frequency_code_set(
            manifest.get("deferred_frequencies"), "deferred frequencies"
        )
    except (TypeError, ValueError):
        return False

    stock_errors: dict[str, Mapping[str, object]] = {}
    stock_error_fields = {
        "code",
        "error_type",
        "reason_code",
        "failure_class",
        "retry_policy",
        "deterministic_for_coverage_epoch",
        "remote_error_type",
        "reason",
    }
    for raw in errors:
        if not isinstance(raw, Mapping):
            return False
        if raw.get("error_type") != "stock_analysis_error":
            continue
        code = raw.get("code")
        if (
            set(raw) != stock_error_fields
            or not isinstance(code, str)
            or not code
            or code in stock_errors
            or not isinstance(raw.get("reason_code"), str)
            or not raw.get("reason_code")
            or not isinstance(raw.get("remote_error_type"), str)
            or not raw.get("remote_error_type")
            or not isinstance(raw.get("reason"), str)
            or not raw.get("reason")
        ):
            return False
        stock_errors[code] = raw
    immediate_retry = pending | backoff
    retained_failures = completed & failed

    def retry_evidence_matches_queue(code: str) -> bool:
        error = stock_errors.get(code)
        if error is None:
            return False
        failure_class = error.get("failure_class")
        retry_policy = error.get("retry_policy")
        deterministic = error.get("deterministic_for_coverage_epoch")
        if retry_policy == "NEXT_REFRESH_AFTER_BACKOFF":
            return bool(
                failure_class == "RUNTIME_FAILURE"
                and deterministic is False
                and code in immediate_retry
                and code not in deferred
            )
        if retry_policy == "NEXT_MARKET_DATA_EPOCH":
            return bool(
                failure_class == "MARKET_DATA_REJECTION"
                and deterministic is True
                and code in deferred
                and code not in immediate_retry
            )
        if retry_policy == "NEXT_COVERAGE_CYCLE":
            return bool(
                failure_class == "UNCLASSIFIED_FAILURE"
                and deterministic is False
                and code in deferred
                and code not in immediate_retry
            )
        return False

    return bool(
        set(stock_errors) == failed
        and not completed & excluded
        and not excluded & failed
        and not (completed | excluded | failed) - discovered
        and not pending - discovered
        and not backoff - discovered
        and not pending & excluded
        and not backoff & excluded
        and not pending & backoff
        and not backoff & deferred
        and not deferred - (failed | excluded)
        and not excluded - deferred
        and not backoff - failed
        and not failed - (immediate_retry | deferred)
        and not ((pending & completed) - failed)
        and not retained_failures - (immediate_retry | deferred)
        and all(retry_evidence_matches_queue(code) for code in failed)
    )


def _coverage_manifest_is_consistent(
    payload: Mapping[str, object],
    *,
    audit: Mapping[str, object],
    manifest: Mapping[str, object],
    errors: list[object],
) -> bool:
    """重新计算覆盖声明，不信任文档自行报告的汇总数。"""

    try:
        discovered = set(
            _canonical_code_list(manifest.get("discovered_codes"), "discovered codes")
        )
        completed = set(
            _canonical_code_list(manifest.get("completed_codes"), "completed codes")
        )
        failed = set(_canonical_code_list(manifest.get("failed_codes"), "failed codes"))
        excluded = set(
            _canonical_code_list(manifest.get("excluded_codes"), "excluded codes")
        )
        exclusion_reasons = _coverage_exclusion_documents(manifest.get("exclusions"))
        discarded = set(
            _canonical_code_list(
                manifest.get("discarded_out_of_scope_retry_codes"),
                "discarded retry codes",
            )
        )
        pending = _frequency_code_set(
            manifest.get("pending_frequencies"), "pending frequencies"
        )
        backoff = _frequency_code_set(
            manifest.get("backoff_frequencies"), "backoff frequencies"
        )
        deferred = _frequency_code_set(
            manifest.get("deferred_frequencies"), "deferred frequencies"
        )
        attempted = completed | excluded | failed
        resolved = completed | excluded
        complete = manifest.get("complete") is True
        market_data_as_of = datetime.fromisoformat(
            str(manifest.get("market_data_as_of"))
        )
        expected_epoch_id = screening_coverage_epoch_id(
            market_data_as_of=market_data_as_of,
            universe_revision=str(manifest.get("universe_revision")),
            sector_catalog_revision=str(manifest.get("sector_catalog_revision")),
            sector_strength_evidence_revision=(
                manifest.get("sector_strength_evidence_revision")
                if isinstance(manifest.get("sector_strength_evidence_revision"), str)
                else None
            ),
            decision_core_id=str(payload.get("decision_core_id")),
            screening_policy_id=_source_screening_policy_id(payload),
            structure_contract_id=str(payload.get("structure_contract_id")),
            parameter_set_id=str(payload.get("parameter_set_id")),
        )
        if (
            not coverage_manifest_dispositions_are_consistent(manifest, errors)
            or set(manifest) != COVERAGE_MANIFEST_FIELDS
            or manifest.get("schema") != COVERAGE_MANIFEST_SCHEMA
            or manifest.get("coverage_state_contract_id") != COVERAGE_STATE_CONTRACT_ID
            or manifest.get("signal_document_contract_id")
            != SIGNAL_DOCUMENT_CONTRACT_ID
            or manifest.get("coverage_epoch_id") != payload.get("coverage_epoch_id")
            or manifest.get("coverage_epoch_id") != expected_epoch_id
            or manifest.get("screening_policy_id") != payload.get("screening_policy_id")
            or manifest.get("market_data_as_of") != payload.get("market_data_as_of")
            or manifest.get("sector_strength_evidence_revision")
            != payload.get("sector_strength_evidence_revision")
            or not _is_sha256_identity(manifest.get("sector_catalog_revision"))
            or excluded != set(exclusion_reasons)
            or discarded & discovered
            or (complete and attempted != discovered)
            or (complete and (pending or backoff))
            or audit.get("coverage_cycle_complete") is not complete
            or int(audit.get("discovered_symbol_count")) != len(discovered)
            or int(audit.get("coverage_cycle_attempted_symbol_count")) != len(attempted)
            or int(audit.get("coverage_cycle_completed_symbol_count")) != len(completed)
            or int(audit.get("coverage_cycle_excluded_symbol_count")) != len(excluded)
            or int(audit.get("coverage_cycle_failed_symbol_count")) != len(failed)
            or int(audit.get("coverage_cycle_resolved_symbol_count")) != len(resolved)
            or int(audit.get("immediate_pending_symbol_count")) != len(pending)
            or int(audit.get("backoff_retry_symbol_count")) != len(backoff)
            or int(audit.get("pending_symbol_count")) != len(pending) + len(backoff)
            or int(audit.get("next_epoch_retry_symbol_count")) != len(deferred)
            or int(audit.get("retry_symbol_count")) != len(backoff) + len(deferred)
        ):
            return False
        expected_ratio = (
            Decimal("0")
            if not attempted
            else Decimal(len(completed)) / Decimal(len(attempted))
        )
        if Decimal(str(audit.get("coverage_cycle_completion_ratio"))) != expected_ratio:
            return False
        expected_resolution_ratio = (
            Decimal("0")
            if not discovered
            else Decimal(len(resolved)) / Decimal(len(discovered))
        )
        if (
            Decimal(str(audit.get("coverage_cycle_resolution_ratio")))
            != expected_resolution_ratio
        ):
            return False

        stock_error_codes: list[str] = []
        failure_counts: dict[str, int] = {}
        for raw in errors:
            if not isinstance(raw, Mapping):
                return False
            if raw.get("error_type") != "stock_analysis_error":
                continue
            code = raw.get("code")
            reason = raw.get("reason_code")
            if not isinstance(code, str) or not code or not isinstance(reason, str):
                return False
            stock_error_codes.append(code)
            failure_counts[reason] = failure_counts.get(reason, 0) + 1
        if (
            len(stock_error_codes) != len(set(stock_error_codes))
            or set(stock_error_codes) != failed
            or audit.get("stock_failure_counts") != dict(sorted(failure_counts.items()))
        ):
            return False
        exclusion_counts: dict[str, int] = {}
        for reason in exclusion_reasons.values():
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        if audit.get("stock_exclusion_counts") != dict(
            sorted(exclusion_counts.items())
        ):
            return False
        raw_signals = payload.get("signals")
        if not isinstance(raw_signals, list) or any(
            not isinstance(signal, Mapping) or not isinstance(signal.get("code"), str)
            for signal in raw_signals
        ):
            return False
        signal_codes = {
            str(signal.get("code"))
            for signal in raw_signals
            if isinstance(signal, Mapping)
        }
        if signal_codes - completed:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False
    return True


def validate_live_screening_market_watermark(
    payload: Mapping[str, object],
) -> datetime:
    """验证当前扫描的市场截止点，但不提升其中任何信号。

    进行中的覆盖批次不能提升到人工复核，但其已认证市场截止点仍是判断旧不可变复核
    报告能否创建新模拟意图的权威水位线。因此本验证器只证明只读信封、语义身份和覆盖
    清单身份；它有意不要求 ``coverage_cycle_complete``，也绝不返回候选信号。
    """

    try:
        review_at = datetime.fromisoformat(str(payload["as_of"]))
        market_data_as_of = datetime.fromisoformat(str(payload["market_data_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening market watermark is invalid") from exc
    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    errors = payload.get("errors")
    coverage_consistent = (
        _coverage_manifest_is_consistent(
            payload,
            audit=audit,
            manifest=manifest,
            errors=errors,
        )
        if isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and isinstance(errors, list)
        else False
    )
    monitor_exclusions_consistent = monitor_instrument_exclusions_are_consistent(
        payload
    )
    if (
        payload.get("schema") != LIVE_SCREENING_SCHEMA
        or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or payload.get("sector_coverage_contract_id") != SECTOR_COVERAGE_CONTRACT_ID
        or payload.get("available") is not True
        or payload.get("scan_state") not in {"scanning", "complete"}
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or review_at.tzinfo is None
        or market_data_as_of.tzinfo is None
        or market_data_as_of > review_at
        or market_data_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
        != review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        or not coverage_consistent
        or not monitor_exclusions_consistent
        or not isinstance(payload.get("snapshot_content_sha256"), str)
        or payload.get("snapshot_content_sha256")
        != live_screening_snapshot_content_sha256(payload)
    ):
        raise ValueError("live screening market watermark is unverified")
    return market_data_as_of


def _sector_context_is_consistent(
    raw: object,
    *,
    frequency: str,
    evidence_cutoff: datetime,
) -> bool:
    if not isinstance(raw, Mapping):
        return False
    try:
        observed_at = datetime.fromisoformat(str(raw["observed_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    # 行情终端 QMT 的板块合成数据有意止于 5m；序列化 1m 记录是中性哨兵，
    # 定位。它并非 ``classify_context`` 的结果，故使用独立精确契约；30m/5m 板块记录
    # 使用常规背景语义。
    context_consistent = _decision_context_is_consistent(raw)
    if frequency == "1m":
        context_consistent = bool(
            raw.get("direction") == "neutral"
            and raw.get("disposition") == "neutral"
            and raw.get("hard_block") is False
            and raw.get("dominant_point_id") is None
            and raw.get("dominant_point_type") is None
            and raw.get("reason_codes") == ["stock_one_minute_trigger_only"]
        )
    return bool(
        raw.get("frequency") == frequency
        and context_consistent
        and observed_at.tzinfo is not None
        and observed_at <= evidence_cutoff
    )


def _decision_context_is_consistent(raw: object) -> bool:
    """根据主导买卖点摘要重新计算一个序列化上下文。"""

    if not isinstance(raw, Mapping):
        return False
    direction = raw.get("direction")
    dominant_id = raw.get("dominant_point_id")
    dominant_type = raw.get("dominant_point_type")
    if direction not in {"up", "down", "neutral"}:
        return False
    if dominant_type is None:
        if dominant_id is not None:
            return False
        disposition = "neutral"
        # The producer distinguishes a genuinely empty context from one whose
        # visible directional points have all aged out of the active window.
        # Both states correctly have no dominant point and remain neutral.
        valid_reason_codes = (
            ["no_active_directional_point"],
            ["directional_points_expired"],
        )
    else:
        if dominant_type not in CANONICAL_POINT_TYPE_SET or not _is_sha256_identity(
            dominant_id
        ):
            return False
        if str(dominant_type).endswith("buy"):
            disposition = "supportive"
            reason = "confirmed_buy_structure"
        elif direction == "down":
            disposition = "hostile"
            reason = "confirmed_sell_with_down_structure"
        else:
            disposition = "neutral"
            reason = "mixed_or_transition_structure"
        valid_reason_codes = ([reason],)
    return bool(
        raw.get("disposition") == disposition
        and raw.get("hard_block") is (disposition == "hostile")
        and raw.get("reason_codes") in valid_reason_codes
    )


def _etf_proxy_sector_is_consistent(raw: object, *, code: object) -> bool:
    """Validate the synthetic sector carried by the ETF no-sector path."""

    if not isinstance(raw, Mapping) or not isinstance(code, str) or not code:
        return False
    return dict(raw) == {
        "sector_id": f"etf-proxy:{code}",
        "sector_name": "ETF代理路径（不要求个股行业）",
        "eligible": True,
        "hard_block": False,
        "regime": "neutral",
        "rank": None,
        "rank_score": 0,
        "rank_components": {},
        "reason_codes": ["ETF_PROXY_SECTOR_NOT_REQUIRED"],
        "horizontal_strength": None,
        "horizontal_rank": None,
        "strength_anchor_session": None,
        "strength_member_count": 0,
        "strength_source_revision": None,
        "strength_reason_codes": [],
        "context_30m": None,
        "context_5m": None,
        "context_1m": None,
    }


def _point_document_is_causal(
    raw: object,
    *,
    frequency: str,
    evidence_cutoff: datetime,
) -> bool:
    """校验一份结构点文档的可移植因果时序。

    语义快照哈希只能证明页面声明的内容未变化；这些时间还必须证明 5 分钟设置或
    1 分钟定位点在声明的行情截止点前已经可见。正式点仅有 ``confirmed`` 状态，
    盘中候选仅有 ``provisional`` 状态；结构失效属于信号生命周期，不是点位状态。
    """

    if not isinstance(raw, Mapping):
        return False
    try:
        anchor_at = datetime.fromisoformat(str(raw["anchor_at"]))
        available_at = datetime.fromisoformat(str(raw["available_at"]))
        confirmed_at = (
            None
            if raw.get("confirmed_at") is None
            else datetime.fromisoformat(str(raw["confirmed_at"]))
        )
    except (KeyError, TypeError, ValueError):
        return False
    status = raw.get("status")
    return bool(
        raw.get("source_frequency") == frequency
        and status in {"provisional", "confirmed"}
        and anchor_at.tzinfo is not None
        and available_at.tzinfo is not None
        and anchor_at <= available_at <= evidence_cutoff
        and (status == "confirmed") == (confirmed_at is not None)
        and (
            confirmed_at is None
            or confirmed_at.tzinfo is not None
            and anchor_at <= confirmed_at <= available_at
        )
    )


def _one_minute_locator_follows_five_minute_setup(
    setup: Mapping[str, object],
    locator: Mapping[str, object],
) -> bool:
    """Require execution evidence to be observable after the formal 5m setup."""

    try:
        setup_available_at = datetime.fromisoformat(str(setup["available_at"]))
        locator_available_at = datetime.fromisoformat(str(locator["available_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        setup_available_at.tzinfo is not None
        and locator_available_at.tzinfo is not None
        and setup_available_at <= locator_available_at
    )


def _warmup_evidence_is_consistent(raw: object) -> bool:
    """根据冻结记录重新计算当前成对预热结论。"""

    if not isinstance(raw, Mapping):
        return False
    rows = raw.get("by_frequency")
    reasons = raw.get("reason_codes")
    difference_rows = raw.get("difference_codes_by_frequency")
    if (
        raw.get("required_for_new_entry") is not True
        or type(raw.get("converged")) is not bool
        or not isinstance(rows, list)
        or len(rows) != len(SCREENING_WARMUP_FREQUENCIES)
        or not isinstance(reasons, list)
        or any(not isinstance(value, str) or not value for value in reasons)
        or len(reasons) != len(set(reasons))
        or not isinstance(difference_rows, list)
    ):
        return False
    expected_frequencies = SCREENING_WARMUP_FREQUENCIES
    actual_frequencies: list[str] = []
    converged_values: list[bool] = []
    expected_reasons: list[str] = []
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                return False
            frequency = row.get("frequency")
            converged = row.get("converged")
            full_count = row.get("full_bar_count")
            suffix_count = row.get("suffix_bar_count")
            if not isinstance(frequency, str) or type(converged) is not bool:
                return False
            reason = screening_warmup_reason_code(
                frequency=frequency,
                converged=converged,
                full_bar_count=full_count,  # type: ignore[arg-type]
                suffix_bar_count=suffix_count,  # type: ignore[arg-type]
            )
            actual_frequencies.append(frequency)
            converged_values.append(converged)
            expected_reasons.append(f"{frequency.upper()}:{reason}")
    except (TypeError, ValueError):
        return False
    if len(difference_rows) != len(rows):
        return False
    for warmup_row, diagnostic_row in zip(rows, difference_rows):
        if not isinstance(diagnostic_row, Mapping):
            return False
        codes = diagnostic_row.get("difference_codes")
        if (
            diagnostic_row.get("frequency") != warmup_row.get("frequency")
            or not isinstance(codes, list)
            or any(
                not isinstance(value, str)
                or value not in SCREENING_WARMUP_DIFFERENCE_CODES
                for value in codes
            )
            or len(codes) != len(set(codes))
            or (warmup_row.get("converged") is True and codes)
            or (
                warmup_row.get("converged") is False
                and warmup_row.get("full_bar_count", 0)
                >= SCREENING_WARMUP_REQUIRED_BARS[str(warmup_row.get("frequency"))]
                and not codes
            )
        ):
            return False
    accepted_reasons = [expected_reasons]
    if "30m" in actual_frequencies:
        fallback_reasons = list(expected_reasons)
        thirty_index = actual_frequencies.index("30m")
        fallback_reasons.insert(
            thirty_index + 1,
            f"30M:{SCREENING_QMT_30M_FALLBACK_REASON_CODE}",
        )
        accepted_reasons.append(fallback_reasons)
    return bool(
        tuple(actual_frequencies) == expected_frequencies
        and raw.get("converged") is all(converged_values)
        and reasons in accepted_reasons
    )


def _warmup_execution_partition(
    raw: Mapping[str, object],
) -> tuple[bool, tuple[str, ...], tuple[str, ...]] | None:
    """Split warmup evidence into the 5m gate and context-only diagnostics."""

    rows = raw.get("by_frequency")
    reasons = _unique_string_list(raw.get("reason_codes"))
    if not isinstance(rows, list) or reasons is None:
        return None
    # Hand-built legacy bundles may omit per-period rows.  Match the core's
    # fail-closed aggregate fallback; production snapshots still require all
    # four rows through ``_warmup_evidence_is_consistent``.
    if not rows:
        aggregate = raw.get("converged")
        if type(aggregate) is not bool:
            return None
        return (
            aggregate,
            tuple(reason for reason in reasons if reason.startswith("5M:")),
            tuple(
                reason
                for reason in reasons
                if reason.split(":", 1)[0] in {"D", "30M", "1M"}
                and reason.split(":", 1)[-1]
                in {"WARMUP_HISTORY_INSUFFICIENT", "WARMUP_TAIL_DIVERGED"}
            ),
        )
    five_minute_rows = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("frequency") == "5m"
    ]
    if len(five_minute_rows) != 1:
        return None
    five_minute_converged = five_minute_rows[0].get("converged")
    if type(five_minute_converged) is not bool:
        return None
    diagnostic_failures = {
        "WARMUP_HISTORY_INSUFFICIENT",
        "WARMUP_TAIL_DIVERGED",
    }
    trade_level_reasons = tuple(
        reason for reason in reasons if reason.startswith("5M:")
    )
    context_advisories = tuple(
        reason
        for reason in reasons
        if reason.split(":", 1)[0] in {"D", "30M", "1M"}
        and reason.split(":", 1)[-1] in diagnostic_failures
    )
    return five_minute_converged, trade_level_reasons, context_advisories


def _unique_string_list(raw: object) -> tuple[str, ...] | None:
    if (
        not isinstance(raw, list)
        or any(not isinstance(value, str) or not value for value in raw)
        or len(raw) != len(set(raw))
    ):
        return None
    return tuple(raw)


def _risk_period_diagnostics(
    raw: object,
    *,
    evidence_cutoff: datetime,
) -> tuple[HigherTimeframePeriodDiagnostic, ...] | None:
    """解析并因果验证可移植的月/周/日诊断文档。"""

    if not isinstance(raw, list) or len(raw) not in {0, 3}:
        return None
    output: list[HigherTimeframePeriodDiagnostic] = []
    for expected_period, value in zip(_RISK_PERIODS, raw):
        if not isinstance(value, Mapping):
            return None
        period = value.get("period")
        state = value.get("state")
        completed_count = value.get("completed_bar_count")
        mapping_unique = value.get("mapping_unique")
        mapped_center_id = value.get("mapped_center_id")
        candidates = _unique_string_list(value.get("mapping_candidate_ids"))
        blockers = _unique_string_list(value.get("blocker_codes"))
        warnings = _unique_string_list(value.get("warning_codes"))
        source_revision = value.get("source_revision")
        has_mapping_supply = "mapping_supply" in value
        mapping_supply = None
        if has_mapping_supply:
            try:
                mapping_supply = RiskMappingSupplyFacts.from_document(
                    value.get("mapping_supply")
                )
            except (TypeError, ValueError):
                return None
        if (
            period != expected_period
            or not isinstance(state, str)
            or state not in HIGHER_TIMEFRAME_RISK_STATES
            or type(completed_count) is not int
            or completed_count < 0
            or type(mapping_unique) is not bool
            or (
                mapped_center_id is not None
                and (not isinstance(mapped_center_id, str) or not mapped_center_id)
            )
            or candidates is None
            or blockers is None
            or warnings is None
            or not _is_sha256_identity(source_revision)
        ):
            return None
        evidence_bar_end = None
        if value.get("evidence_bar_end") is not None:
            try:
                evidence_bar_end = normalize_datetime(
                    datetime.fromisoformat(str(value["evidence_bar_end"])),
                    "higher_timeframe.evidence_bar_end",
                )
            except (TypeError, ValueError):
                return None
            if evidence_bar_end > evidence_cutoff:
                return None
        active_interval = None
        raw_interval = value.get("active_top_interval")
        if raw_interval is not None:
            if not isinstance(raw_interval, list) or len(raw_interval) != 2:
                return None
            try:
                active_interval = tuple(
                    normalize_datetime(
                        datetime.fromisoformat(str(item)),
                        "higher_timeframe.active_top_interval",
                    )
                    for item in raw_interval
                )
            except (TypeError, ValueError):
                return None
            if (
                active_interval[0] > active_interval[1]
                or active_interval[1] > evidence_cutoff
                or evidence_bar_end is None
                or active_interval[1] > evidence_bar_end
            ):
                return None
        if state == "NONE":
            if (
                active_interval is not None
                or mapping_unique is not True
                or mapped_center_id is not None
                or candidates
                or blockers
            ):
                return None
        elif (
            active_interval is None or evidence_bar_end is None or completed_count == 0
        ):
            return None
        if (
            mapping_unique is True
            and state != "NONE"
            and not isinstance(mapped_center_id, str)
        ):
            return None
        if mapping_unique is False and (mapped_center_id is not None or not blockers):
            return None
        if (state == "NONE" and has_mapping_supply) or (
            state != "NONE" and mapping_supply is None
        ):
            return None
        try:
            output.append(
                HigherTimeframePeriodDiagnostic(
                    period=period,  # type: ignore[arg-type]
                    state=state,  # type: ignore[arg-type]
                    completed_bar_count=completed_count,
                    evidence_bar_end=evidence_bar_end,
                    active_top_interval=active_interval,  # type: ignore[arg-type]
                    mapping_unique=mapping_unique,
                    mapped_center_id=mapped_center_id,  # type: ignore[arg-type]
                    mapping_candidate_ids=candidates,
                    blocker_codes=blockers,
                    warning_codes=warnings,
                    source_revision=source_revision,  # type: ignore[arg-type]
                    mapping_supply=mapping_supply,
                )
            )
        except (TypeError, ValueError):
            return None
    return tuple(output)


def _session_evidence_is_consistent(
    raw: object,
    *,
    side_reasons: tuple[str, ...],
    side_gate: object,
    evidence_cutoff: datetime,
) -> bool:
    """验证可选且只用于展示的 QMT 交易日说明。

    除非独立的时点交易状态来源证明停牌，否则缺失交易日仍保持未分类。因此当前契约要求
    精确记录来源问题、``historical_trade_status_proven=false`` 和关闭失败处置；重新哈希
    的叙述不能把缺失偷换成停牌，也不能抹去日期。
    """

    if not isinstance(raw, Mapping) or set(raw) != {
        "contract_id",
        "status",
        "issue_count",
        "issues",
        "entry_disposition",
    }:
        return False
    status = raw.get("status")
    issues = raw.get("issues")
    issue_count = raw.get("issue_count")
    if (
        raw.get("contract_id") != HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
        or status not in {"EXACT", "UNAVAILABLE"}
        or type(issue_count) is not int
        or not isinstance(issues, list)
        or issue_count != len(issues)
        or (status == "UNAVAILABLE" and issues)
    ):
        return False
    expected_disposition = (
        "FAIL_CLOSED" if status == "UNAVAILABLE" or issues else "NO_SESSION_BLOCKER"
    )
    if raw.get("entry_disposition") != expected_disposition:
        return False

    canonical: list[dict[str, object]] = []
    identities: list[tuple[date, str]] = []
    for issue in issues:
        if not isinstance(issue, Mapping) or set(issue) != {
            "session",
            "code",
            "observed_rows",
            "classification",
            "detail",
            "historical_trade_status_proven",
            "entry_disposition",
        }:
            return False
        raw_session = issue.get("session")
        raw_code = issue.get("code")
        raw_rows = issue.get("observed_rows")
        raw_detail = issue.get("detail")
        if (
            not isinstance(raw_session, str)
            or raw_code not in _SESSION_ISSUE_CODES
            or type(raw_rows) is not int
            or not isinstance(raw_detail, str)
            or issue.get("historical_trade_status_proven") is not False
            or issue.get("entry_disposition") != "FAIL_CLOSED"
        ):
            return False
        try:
            session = date.fromisoformat(raw_session)
            reconstructed = QmtMinuteSessionIssue(
                session=session,
                code=raw_code,
                observed_rows=raw_rows,
                detail=raw_detail,
            ).document()
        except (TypeError, ValueError):
            return False
        if dict(issue) != reconstructed or session > evidence_cutoff.date():
            return False
        identities.append((session, raw_code))
        canonical.append(reconstructed)
    if identities != sorted(set(identities)):
        return False
    issue_codes = {str(value["code"]) for value in canonical}
    reason_issue_codes = _SESSION_ISSUE_CODES.intersection(side_reasons)
    if not issue_codes.issubset(set(side_reasons)):
        return False
    if status == "EXACT" and issue_codes != reason_issue_codes:
        return False
    if status == "UNAVAILABLE" and reason_issue_codes:
        return False
    if (status == "UNAVAILABLE" or issues) and side_gate != "UNRESOLVED":
        return False
    return True


def _mwd_warmup_evidence_is_consistent(
    raw: object,
    *,
    side_reasons: tuple[str, ...],
    side_gate: object,
) -> bool:
    """重新计算一条月/周/日预热记录中冻结的数量与结论关系。"""

    blocking_reasons = _QMT_MWD_WARMUP_BLOCKING_CODES.intersection(side_reasons)
    if raw is None:
        return not blocking_reasons
    if not isinstance(raw, Mapping) or set(raw) != {
        "contract_id",
        "required_daily_bar_count",
        "full_daily_bar_count",
        "suffix_daily_bar_count",
        "converged",
        "reason_code",
        "full_signature",
        "suffix_signature",
        "entry_disposition",
    }:
        return False
    required = raw.get("required_daily_bar_count")
    full_count = raw.get("full_daily_bar_count")
    suffix_count = raw.get("suffix_daily_bar_count")
    converged = raw.get("converged")
    reason = raw.get("reason_code")
    full_signature = raw.get("full_signature")
    suffix_signature = raw.get("suffix_signature")
    if (
        raw.get("contract_id") != QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
        or required != QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        or type(full_count) is not int
        or full_count < 0
        or type(suffix_count) is not int
        or suffix_count < 0
        or type(converged) is not bool
        or not _is_sha256_identity(full_signature)
    ):
        return False
    if reason == "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT":
        consistent = (
            full_count < required
            and suffix_count == 0
            and converged is False
            and suffix_signature is None
        )
    elif reason in {
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
    }:
        consistent = (
            full_count >= required
            and suffix_count == expected_screening_warmup_suffix_bar_count(full_count)
            and _is_sha256_identity(suffix_signature)
            and converged == (reason == "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE")
            and (full_signature == suffix_signature) == converged
        )
    else:
        return False
    if not consistent:
        return False
    blocked = converged is False
    expected_disposition = "FAIL_CLOSED" if blocked else "NO_WARMUP_BLOCKER"
    if raw.get("entry_disposition") != expected_disposition:
        return False
    if blocked:
        return reason in side_reasons and side_gate == "UNRESOLVED"
    return not blocking_reasons and reason not in side_reasons


def _mwd_warmup_convergence_evidence_is_consistent(
    raw: object,
    *,
    evidence_cutoff: datetime,
    side_gate: object,
) -> bool:
    if raw is None:
        return side_gate == "UNRESOLVED"
    if not isinstance(raw, Mapping):
        return False
    try:
        evidence = WarmupConvergenceEnvelope.from_document(raw)
    except (TypeError, ValueError):
        return False
    return bool(
        evidence.as_of == evidence_cutoff
        and evidence.frequency == "d"
        and evidence.parameter_set_id
        == QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        and evidence.diagnostic_only is True
        and evidence.active_gate_unchanged is True
        and evidence.live_status == "LIVE_DISABLED"
    )


def _mwd_warmup_diagnostic_chain_is_consistent(
    *,
    envelope_raw: object,
    semantic_raw: object,
    supply_raw: object,
    lineage_raw: object,
    evidence_cutoff: datetime,
    memo: dict[tuple[str, str], bool] | None = None,
) -> bool:
    """每份不同文档只验证一次完整诊断谱系。

    旧调用方分别执行语义、映射供给和谱系验证；每个更深层验证器都会重建并再次验证此前
    各层，导致同一信号重复解析同一信封三次，且市场和板块文档还会被数百个信号共享。
    本次验证范围内使用以重新计算的规范内容哈希为键的缓存，既保留完整防篡改检查，又
    避免二次方工作量；文档自报哈希有意不作为缓存键。
    """

    cache_key: tuple[str, str] | None = None
    if memo is not None:
        try:
            cache_key = (
                evidence_cutoff.isoformat(),
                sha256_json(
                    {
                        "envelope": envelope_raw,
                        "semantic": semantic_raw,
                        "mapping_supply": supply_raw,
                        "structure_lineage": lineage_raw,
                    }
                ),
            )
        except (OverflowError, TypeError, ValueError):
            cache_key = None
        if cache_key is not None and cache_key in memo:
            return memo[cache_key]

    def current(value: object) -> bool:
        return bool(
            value.as_of == evidence_cutoff
            and value.frequency == "d"
            and value.parameter_set_id
            == QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
            and value.diagnostic_only is True
            and value.active_gate_unchanged is True
            and value.live_status == "LIVE_DISABLED"
        )

    result = False
    if envelope_raw is None:
        result = semantic_raw is None and supply_raw is None and lineage_raw is None
    elif all(
        isinstance(value, Mapping)
        for value in (envelope_raw, semantic_raw, supply_raw, lineage_raw)
    ):
        try:
            envelope = WarmupConvergenceEnvelope.from_document(envelope_raw)
            semantic = WarmupConvergenceDiagnosticEnvelope.from_document(semantic_raw)
            semantic.validate_against(envelope)
            bound = replace(envelope, diagnostic=semantic)
            supply = WarmupMappingSupplyDiagnosticEnvelope.from_document(supply_raw)
            supply.validate_against(bound)
            lineage = WarmupStructureLineageDiagnosticEnvelope.from_document(
                lineage_raw
            )
            lineage.validate_against(replace(bound, mapping_supply_diagnostic=supply))
            result = current(semantic) and current(supply) and current(lineage)
        except (TypeError, ValueError):
            result = False

    if memo is not None and cache_key is not None:
        memo[cache_key] = result
    return result


def _sector_source_extension_mode(
    risk: Mapping[str, object],
    *,
    sector_reasons: tuple[str, ...],
) -> tuple[bool, str | None]:
    """认证页面与回放条件板块来源的谱系。

    声明原生日线研究阻断的记录必须携带完整谱系。预检查有意独立于风险状态重算，只有
    身份完整的研究桥接才能触发下方由绿色限制为黄色的安全上限。
    """

    fields = (
        "sector_higher_timeframe_source_mode",
        "sector_strict_same_5m_warmup_evidence",
        "sector_strict_same_5m_source_coverage_evidence",
        "sector_research_bridge_parameter_set_id",
    )
    present = tuple(field in risk for field in fields)
    blocker = "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
    if not any(present):
        return blocker not in sector_reasons, None
    if not all(present):
        return False, None

    mode = risk.get("sector_higher_timeframe_source_mode")
    bridge_id = risk.get("sector_research_bridge_parameter_set_id")
    if mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE:
        return bridge_id is None and blocker not in sector_reasons, mode
    if mode != QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
        return False, None
    expected_bridge_id = sector_native_daily_research_bridge_contract()[
        "parameter_set_id"
    ]
    valid = bool(
        bridge_id == expected_bridge_id
        and blocker in sector_reasons
        and risk.get("sector_gate") in {"AMBER", "RED", "UNRESOLVED"}
    )
    return valid, mode if valid else None


def _sector_source_extension_is_consistent(
    risk: Mapping[str, object],
    *,
    sector_reasons: tuple[str, ...],
    mode: str | None,
    evidence_cutoff: datetime,
) -> bool:
    """把来源模式同时绑定到严格预热证据和最终选用的预热证据。"""

    if mode is None:
        return True
    strict_warmup = risk.get("sector_strict_same_5m_warmup_evidence")
    raw_coverage = risk.get("sector_strict_same_5m_source_coverage_evidence")
    selected_warmup = risk.get("sector_warmup_evidence")
    if not isinstance(strict_warmup, Mapping) or not isinstance(
        raw_coverage,
        Mapping,
    ):
        return False
    try:
        coverage = QmtSectorSameBaseCoverageEvidence.from_document(raw_coverage)
    except ValueError:
        return False
    if (
        coverage.observed_at != evidence_cutoff
        or coverage.completed_daily_bar_count
        < strict_warmup.get("full_daily_bar_count")
        or coverage.required_daily_bar_count
        != strict_warmup.get("required_daily_bar_count")
        or coverage.warmup_converged != strict_warmup.get("converged")
        or coverage.warmup_reason_code != strict_warmup.get("reason_code")
    ):
        return False
    if mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE:
        # 严格链路中选中证据就是严格证据；第二份有分歧副本会让页面与回放来源含糊。
        return strict_warmup == selected_warmup
    if mode != QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE:
        return False
    insufficient = "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
    return _mwd_warmup_evidence_is_consistent(
        strict_warmup,
        side_reasons=(insufficient,),
        side_gate="UNRESOLVED",
    )


def _risk_side_evidence_is_consistent(
    risk: Mapping[str, object],
    *,
    prefix: str,
    evidence_cutoff: datetime,
    allow_green_to_amber_cap: bool = False,
) -> bool:
    gate = risk.get(f"{prefix}_gate")
    raw_states = risk.get(f"{prefix}_states")
    reasons = _unique_string_list(risk.get(f"{prefix}_reason_codes"))
    diagnostics = _risk_period_diagnostics(
        risk.get(f"{prefix}_period_diagnostics"),
        evidence_cutoff=evidence_cutoff,
    )
    if (
        gate not in _GATES
        or not isinstance(raw_states, Mapping)
        or set(raw_states) != set(_RISK_PERIODS)
        or reasons is None
        or diagnostics is None
    ):
        return False
    states = tuple(raw_states[period] for period in _RISK_PERIODS)
    unresolved_snapshot = all(value == "UNRESOLVED" for value in states)
    if unresolved_snapshot:
        if gate != "UNRESOLVED" or not reasons:
            return False
    else:
        if any(
            not isinstance(value, str) or value not in HIGHER_TIMEFRAME_RISK_STATES
            for value in states
        ):
            return False
        if (
            len(diagnostics) != 3
            or tuple(value.state for value in diagnostics) != states
        ):
            return False
        expected_gate = higher_timeframe_risk_gate(
            states=states,  # type: ignore[arg-type]
            completed_ma5_available=True,
            mapping_unique=all(value.mapping_unique for value in diagnostics),
        )
        capped_research_green = bool(
            allow_green_to_amber_cap
            and expected_gate == "GREEN"
            and gate == "AMBER"
            and (
                "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE" in reasons
            )
        )
        if gate != expected_gate and not capped_research_green:
            return False
    for diagnostic in diagnostics:
        if any(code not in reasons for code in diagnostic.blocker_codes):
            return False
        if (
            not diagnostic.mapping_unique
            and f"{diagnostic.period}_CENTER_MAPPING_UNRESOLVED" not in reasons
        ):
            return False
    return True


def _risk_evidence_is_consistent(
    raw: object,
    *,
    evidence_cutoff: datetime,
    expected_symbol: str | None = None,
    diagnostic_memo: dict[tuple[str, str], bool] | None = None,
) -> bool:
    if not isinstance(raw, Mapping):
        return False
    market_reasons = _unique_string_list(raw.get("market_reason_codes"))
    sector_reasons = _unique_string_list(raw.get("sector_reason_codes"))
    symbol_reasons = _unique_string_list(raw.get("symbol_reason_codes"))
    merged_reasons = _unique_string_list(raw.get("reason_codes"))
    if (
        raw.get("new_entry_requires_all_green") is not False
        or market_reasons is None
        or sector_reasons is None
        or symbol_reasons is None
        or merged_reasons is None
        or merged_reasons
        != tuple(dict.fromkeys((*market_reasons, *sector_reasons, *symbol_reasons)))
    ):
        return False
    sector_extension_valid, sector_source_mode = _sector_source_extension_mode(
        raw,
        sector_reasons=sector_reasons,
    )
    if not sector_extension_valid:
        return False
    expected_fields = _RISK_DECISION_FIELDS | _RISK_CURRENT_EVIDENCE_FIELDS
    if sector_source_mode is not None:
        expected_fields |= _RISK_SECTOR_SOURCE_FIELDS
    if set(raw) != expected_fields:
        return False
    side_evidence_valid = (
        _risk_side_evidence_is_consistent(
            raw,
            prefix="market",
            evidence_cutoff=evidence_cutoff,
        )
        and _risk_side_evidence_is_consistent(
            raw,
            prefix="sector",
            evidence_cutoff=evidence_cutoff,
            allow_green_to_amber_cap=(
                sector_source_mode == QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_MODE
            ),
        )
        and _risk_side_evidence_is_consistent(
            raw,
            prefix="symbol",
            evidence_cutoff=evidence_cutoff,
        )
    )
    if not side_evidence_valid:
        return False

    extension_fields = (
        "session_evidence_contract_id",
        "market_session_evidence",
        "sector_session_evidence",
        "symbol_session_evidence",
    )
    if (
        set(extension_fields) - set(raw)
        or raw.get("session_evidence_contract_id")
        != HIGHER_TIMEFRAME_SESSION_EVIDENCE_CONTRACT_ID
    ):
        return False
    session_valid = (
        _session_evidence_is_consistent(
            raw.get("market_session_evidence"),
            side_reasons=market_reasons,
            side_gate=raw.get("market_gate"),
            evidence_cutoff=evidence_cutoff,
        )
        and _session_evidence_is_consistent(
            raw.get("sector_session_evidence"),
            side_reasons=sector_reasons,
            side_gate=raw.get("sector_gate"),
            evidence_cutoff=evidence_cutoff,
        )
        and _session_evidence_is_consistent(
            raw.get("symbol_session_evidence"),
            side_reasons=symbol_reasons,
            side_gate=raw.get("symbol_gate"),
            evidence_cutoff=evidence_cutoff,
        )
    )
    if not session_valid:
        return False

    warmup_fields = (
        "warmup_evidence_contract_id",
        "market_warmup_evidence",
        "sector_warmup_evidence",
        "symbol_warmup_evidence",
    )
    if (
        set(warmup_fields) - set(raw)
        or raw.get("warmup_evidence_contract_id")
        != QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID
    ):
        return False
    warmup_valid = (
        _mwd_warmup_evidence_is_consistent(
            raw.get("market_warmup_evidence"),
            side_reasons=market_reasons,
            side_gate=raw.get("market_gate"),
        )
        and _mwd_warmup_evidence_is_consistent(
            raw.get("sector_warmup_evidence"),
            side_reasons=sector_reasons,
            side_gate=raw.get("sector_gate"),
        )
        and _mwd_warmup_evidence_is_consistent(
            raw.get("symbol_warmup_evidence"),
            side_reasons=symbol_reasons,
            side_gate=raw.get("symbol_gate"),
        )
    )
    if not warmup_valid:
        return False
    convergence_fields = (
        "warmup_convergence_contract_id",
        "market_warmup_convergence_evidence",
        "sector_warmup_convergence_evidence",
        "symbol_warmup_convergence_evidence",
    )
    strict_convergence_field = "sector_strict_same_5m_warmup_convergence_evidence"
    if (
        set(convergence_fields) - set(raw)
        or raw.get("warmup_convergence_contract_id")
        != WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("market_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("market_gate"),
        )
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("sector_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("sector_gate"),
        )
        or not _mwd_warmup_convergence_evidence_is_consistent(
            raw.get("symbol_warmup_convergence_evidence"),
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("symbol_gate"),
        )
    ):
        return False
    if sector_source_mode is None:
        if strict_convergence_field in raw:
            return False
    else:
        if strict_convergence_field not in raw:
            return False
        strict_convergence = raw.get(strict_convergence_field)
        if not _mwd_warmup_convergence_evidence_is_consistent(
            strict_convergence,
            evidence_cutoff=evidence_cutoff,
            side_gate=raw.get("sector_gate"),
        ):
            return False
        if (
            sector_source_mode == QMT_SECTOR_SAME_BASE_SOURCE_MODE
            and strict_convergence != raw.get("sector_warmup_convergence_evidence")
        ):
            return False
    diagnostic_fields = (
        "warmup_convergence_diagnostic_contract_id",
        "market_warmup_convergence_diagnostic_evidence",
        "sector_warmup_convergence_diagnostic_evidence",
        "symbol_warmup_convergence_diagnostic_evidence",
    )
    strict_diagnostic_field = (
        "sector_strict_same_5m_warmup_convergence_diagnostic_evidence"
    )
    if (
        set(diagnostic_fields) - set(raw)
        or raw.get("warmup_convergence_diagnostic_contract_id")
        != WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_diagnostic_field in raw:
            return False
    elif strict_diagnostic_field not in raw:
        return False
    supply_diagnostic_fields = (
        "warmup_mapping_supply_diagnostic_contract_id",
        "market_warmup_mapping_supply_diagnostic_evidence",
        "sector_warmup_mapping_supply_diagnostic_evidence",
        "symbol_warmup_mapping_supply_diagnostic_evidence",
    )
    strict_supply_diagnostic_field = (
        "sector_strict_same_5m_warmup_mapping_supply_diagnostic_evidence"
    )
    if (
        set(supply_diagnostic_fields) - set(raw)
        or raw.get("warmup_mapping_supply_diagnostic_contract_id")
        != WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_supply_diagnostic_field in raw:
            return False
    elif strict_supply_diagnostic_field not in raw:
        return False
    lineage_diagnostic_fields = (
        "warmup_structure_lineage_diagnostic_contract_id",
        "market_warmup_structure_lineage_diagnostic_evidence",
        "sector_warmup_structure_lineage_diagnostic_evidence",
        "symbol_warmup_structure_lineage_diagnostic_evidence",
    )
    strict_lineage_diagnostic_field = (
        "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence"
    )
    if (
        set(lineage_diagnostic_fields) - set(raw)
        or raw.get("warmup_structure_lineage_diagnostic_contract_id")
        != WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
    ):
        return False
    if sector_source_mode is None:
        if strict_lineage_diagnostic_field in raw:
            return False
    elif strict_lineage_diagnostic_field not in raw:
        return False

    for prefix in ("market", "sector", "symbol"):
        if not _mwd_warmup_diagnostic_chain_is_consistent(
            envelope_raw=raw.get(f"{prefix}_warmup_convergence_evidence"),
            semantic_raw=raw.get(f"{prefix}_warmup_convergence_diagnostic_evidence"),
            supply_raw=raw.get(f"{prefix}_warmup_mapping_supply_diagnostic_evidence"),
            lineage_raw=raw.get(
                f"{prefix}_warmup_structure_lineage_diagnostic_evidence"
            ),
            evidence_cutoff=evidence_cutoff,
            memo=diagnostic_memo,
        ):
            return False
    if sector_source_mode is not None and not (
        _mwd_warmup_diagnostic_chain_is_consistent(
            envelope_raw=raw.get(strict_convergence_field),
            semantic_raw=raw.get(strict_diagnostic_field),
            supply_raw=raw.get(strict_supply_diagnostic_field),
            lineage_raw=raw.get(strict_lineage_diagnostic_field),
            evidence_cutoff=evidence_cutoff,
            memo=diagnostic_memo,
        )
    ):
        return False
    if not _sector_source_extension_is_consistent(
        raw,
        sector_reasons=sector_reasons,
        mode=sector_source_mode,
        evidence_cutoff=evidence_cutoff,
    ):
        return False

    native_fields = (
        "native_daily_reconciliation_contract_id",
        "market_native_daily_reconciliation_evidence",
        "sector_native_daily_reconciliation_evidence",
        "symbol_native_daily_reconciliation_evidence",
    )
    if (
        set(native_fields) - set(raw)
        or raw.get("native_daily_reconciliation_contract_id")
        != QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
        or raw.get("sector_native_daily_reconciliation_evidence") is not None
    ):
        return False

    def native_side_is_consistent(prefix: str, expected: str | None) -> bool:
        evidence = raw.get(f"{prefix}_native_daily_reconciliation_evidence")
        if evidence is None:
            return raw.get(f"{prefix}_gate") == "UNRESOLVED"
        return _native_daily_reconciliation_evidence_is_consistent(
            evidence,
            expected_symbol=expected,
            evidence_cutoff=evidence_cutoff,
        )

    native_valid = native_side_is_consistent(
        "market", None
    ) and native_side_is_consistent("symbol", expected_symbol)
    if not native_valid:
        return False

    calendar_fields = (
        "native_daily_calendar_coverage_contract_id",
        "market_native_daily_calendar_coverage_evidence",
        "sector_native_daily_calendar_coverage_evidence",
        "symbol_native_daily_calendar_coverage_evidence",
    )
    if (
        set(calendar_fields) - set(raw)
        or raw.get("native_daily_calendar_coverage_contract_id")
        != QMT_NATIVE_DAILY_CALENDAR_COVERAGE_EVIDENCE_CONTRACT_ID
        or raw.get("sector_native_daily_calendar_coverage_evidence") is not None
    ):
        return False

    def calendar_side_is_consistent(
        prefix: str,
        expected: str | None,
        reasons: tuple[str, ...],
    ) -> bool:
        evidence = raw.get(f"{prefix}_native_daily_calendar_coverage_evidence")
        if evidence is None:
            return "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" not in reasons
        if not _native_daily_calendar_coverage_evidence_is_consistent(
            evidence,
            expected_symbol=expected,
            evidence_cutoff=evidence_cutoff,
            side_reasons=reasons,
            side_gate=raw.get(f"{prefix}_gate"),
        ):
            return False
        try:
            coverage = QmtNativeDailyCalendarCoverageEvidence.from_document(evidence)
        except (TypeError, ValueError):
            return False
        reconciliation = raw.get(f"{prefix}_native_daily_reconciliation_evidence")
        return (coverage.status == "EXACT") == (reconciliation is not None)

    calendar_valid = calendar_side_is_consistent(
        "market", None, market_reasons
    ) and calendar_side_is_consistent("symbol", expected_symbol, symbol_reasons)
    return bool(
        calendar_valid
        and _higher_timeframe_data_integrity_reason_codes_from_risk(raw) is not None
    )


def _formal_selection_gate_is_consistent(
    signal: Mapping[str, object],
) -> tuple[bool, tuple[str, ...], bool]:
    """从签名研究快照重新计算正式候选资格，拒绝自报布尔值。"""

    selection_path = signal.get("selection_path")
    raw_sources = signal.get("selection_sources")
    raw_gate = signal.get("formal_selection")
    raw_research = signal.get("selection_research")
    formal_selection_required = signal.get("formal_selection_required")
    if (
        selection_path not in {"INDIVIDUAL_THREE_PROGRAM", "ETF_PROXY"}
        or not isinstance(raw_sources, list)
        or any(not isinstance(value, str) for value in raw_sources)
        or not isinstance(raw_gate, Mapping)
        or type(formal_selection_required) is not bool
    ):
        return False, (), False
    try:
        observed_at = datetime.fromisoformat(str(signal["observed_at"]))
        research = (
            None
            if raw_research is None
            else selection_research_snapshot_from_document(dict(raw_research))
        )
        expected = evaluate_formal_selection_gate(
            research,
            symbol=str(signal.get("code") or ""),
            decision_time=observed_at,
            selection_path=selection_path,
            sector_triggered="QMT_SECTOR_TRIGGER" in raw_sources,
        )
    except (KeyError, TypeError, ValueError):
        return False, (), False
    consistent = bool(
        dict(raw_gate) == expected.document()
        and signal.get("sector_triggered") is expected.sector_triggered
        and signal.get("monitor_only")
        is (formal_selection_required and not expected.accepted)
    )
    return (
        consistent,
        expected.reason_codes,
        not formal_selection_required or expected.accepted,
    )


def _higher_timeframe_data_integrity_reason_codes_from_risk(
    risk: Mapping[str, object],
) -> tuple[str, ...] | None:
    """复算方向分级之外、真正能够硬关闭新买入的数据矛盾。"""

    declared = _unique_string_list(risk.get("data_integrity_hard_block_reason_codes"))
    if declared is None:
        return None
    reasons: list[str] = []
    for subject in ("market", "symbol"):
        subject_reasons = _unique_string_list(risk.get(f"{subject}_reason_codes"))
        if subject_reasons is None:
            return None
        reasons.extend(
            value
            for value in subject_reasons
            if value in HARD_HIGHER_TIMEFRAME_DATA_INTEGRITY_REASON_CODES
        )

        coverage = risk.get(f"{subject}_native_daily_calendar_coverage_evidence")
        if isinstance(coverage, Mapping) and coverage.get("status") != "EXACT":
            reasons.append("QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH")

    expected = tuple(dict.fromkeys(reasons))
    return expected if declared == expected else None


def _entry_gate_is_consistent(
    signal: Mapping[str, object],
    *,
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
) -> bool:
    """校验结构执行硬条件；环境与研究范围由执行画像单独校验。"""

    side = signal.get("side")
    technical = signal.get("technical_entry_allowed")
    entry_allowed = signal.get("entry_allowed")
    exit_allowed = signal.get("exit_allowed")
    if (
        side not in {"buy", "sell"}
        or type(technical) is not bool
        or type(entry_allowed) is not bool
        or type(exit_allowed) is not bool
    ):
        return False
    if side == "sell":
        return technical is False and entry_allowed is False
    formal_consistent, _formal_reasons, formal_accepted = (
        _formal_selection_gate_is_consistent(signal)
    )
    if isinstance(signal.get("execution_profile"), Mapping):
        data_integrity_reasons = (
            _higher_timeframe_data_integrity_reason_codes_from_risk(risk)
        )
        warmup_partition = _warmup_execution_partition(warmup)
        if data_integrity_reasons is None or warmup_partition is None:
            return False
        five_minute_warmup_converged, _trade_reasons, _context_reasons = (
            warmup_partition
        )
        # 月/周/日方向、板块环境和研究范围只生成提示；物理预热与能够证明
        # 时间穿越、同源破坏或日历矛盾的数据事实仍然硬关闭新买入。
        decision_reasons = _unique_string_list(signal.get("decision_reasons"))
        if decision_reasons is None:
            return False
        operationally_blocked = bool(
            {
                "one_minute_not_confirmed",
                "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING",
                "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
                *BUY_SIGNAL_PROTECTION_REASON_CODES,
            }.intersection(decision_reasons)
        )
        return bool(
            formal_consistent
            and entry_allowed
            is (
                technical
                and five_minute_warmup_converged
                and not data_integrity_reasons
                and not operationally_blocked
            )
            and exit_allowed is False
        )
    sector_required = signal.get("selection_path") == ("INDIVIDUAL_THREE_PROGRAM")
    expected_entry = bool(
        technical
        and warmup.get("converged") is True
        and risk.get("market_gate") == "GREEN"
        and (not sector_required or risk.get("sector_gate") == "GREEN")
        and risk.get("symbol_gate") == "GREEN"
        and formal_accepted
    )
    return bool(
        formal_consistent and entry_allowed is expected_entry and exit_allowed is False
    )


def _conflict_reason_codes(raw: object) -> tuple[str, ...] | None:
    if not isinstance(raw, Mapping) or type(raw.get("hard_block")) is not bool:
        return None
    blocking = _unique_string_list(raw.get("blocking_point_ids"))
    risk_only = _unique_string_list(raw.get("risk_only_point_ids"))
    if (
        blocking is None
        or risk_only is None
        or any(not _is_sha256_identity(value) for value in (*blocking, *risk_only))
        or blocking != tuple(sorted(blocking))
        or risk_only != tuple(sorted(risk_only))
        or set(blocking).intersection(risk_only)
        or raw.get("hard_block") is not bool(blocking)
    ):
        return None
    if blocking:
        return ("same_or_higher_structure_conflict",)
    if risk_only:
        return ("lower_or_unrelated_structure_risk",)
    return ()


def _decision_decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _confirmed_five_minute_operation_setup(
    setup: object,
    *,
    side: str | None = None,
) -> bool:
    """Fail closed unless review evidence is a confirmed physical 5m/L0 setup."""

    if not isinstance(setup, Mapping):
        return False
    recursive_level = setup.get("recursive_level")
    return bool(
        isinstance(setup.get("price_basis_revision"), str)
        and setup.get("price_basis_revision")
        and setup.get("status") == "confirmed"
        and setup.get("source_frequency") == "5m"
        and type(recursive_level) is int
        and is_five_minute_trade_level("5m", recursive_level)
        and (side is None or setup.get("side") == side)
    )


def _canonical_setup_formation_state(setup: Mapping[str, object]) -> str | None:
    """Recompute the setup state instead of trusting a display field."""

    try:
        value = canonical_setup_state_document(setup).get("formation_state")
    except (TypeError, ValueError):
        return None
    return str(value) if value in {"forming", "geometry_ready", "confirmed"} else None


def _separated_buy_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
    conflict_reasons: tuple[str, ...],
) -> bool:
    """复算“5分钟结构事实 / 1分钟精确执行 / 环境分级”的买入决策。"""

    profile = signal.get("execution_profile")
    setup = signal.get("setup_5m")
    trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
    sector = signal.get("sector")
    context = signal.get("context_30m")
    daily_context = signal.get("context_d")
    decision_reasons = _unique_string_list(signal.get("decision_reasons"))
    multiplier = _decision_decimal(signal.get("risk_multiplier"))
    if (
        not isinstance(profile, Mapping)
        or not all(
            isinstance(value, Mapping)
            for value in (setup, sector, context, daily_context)
        )
        or (trigger is not None and not isinstance(trigger, Mapping))
        or decision_reasons is None
        or multiplier is None
    ):
        return False
    warmup_partition = _warmup_execution_partition(warmup)
    if warmup_partition is None:
        return False
    (
        five_minute_warmup_converged,
        trade_level_warmup_reasons,
        context_warmup_advisories,
    ) = warmup_partition
    confirmed_point = _confirmed_five_minute_operation_setup(
        setup,
        side="buy",
    )
    formation_state = _canonical_setup_formation_state(setup)
    if formation_state is None:
        return False
    minimum_tick = _decision_decimal(policy.get("minimum_tick"))
    trigger_confirmed = bool(
        confirmed_point
        and isinstance(trigger, Mapping)
        and minimum_tick is not None
        and is_one_minute_segment_difference_document(
            trigger,
            minimum_tick=minimum_tick,
            expected_side="buy",
        )
        and _one_minute_locator_follows_five_minute_setup(setup, trigger)
        and signal.get("lifecycle_stage")
        in {"triggered", "executable", "active"}
    )
    core_reasons: list[str] = []
    if policy.get("require_confirmed_five_minute") is True and not confirmed_point:
        core_reasons.append(
            unconfirmed_setup_reason_code(
                formation_state,
                forming_reason_code="five_minute_not_confirmed",
            )
        )
    if signal.get("lifecycle_stage") not in {"triggered", "executable", "active"}:
        core_reasons.append("lifecycle_not_actionable")
    if (
        policy.get("require_confirmed_one_minute") is True
        and confirmed_point
        and not trigger_confirmed
    ):
        core_reasons.append("one_minute_not_confirmed")
    conflict = signal.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("hard_block") is True:
        core_reasons.append("structure_conflict")
    if confirmed_point and setup.get("point_type") == "3buy":
        try:
            clearance = Decimal(str(setup["anchor_price"])) - Decimal(
                str(setup["center_zg"])
            )
            minimum_tick = Decimal(str(policy["minimum_tick"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            clearance = None
            minimum_tick = Decimal("0")
        if (
            setup.get("variant") == "boundary_touch"
            or clearance is None
            or not clearance.is_finite()
            or clearance < minimum_tick
        ):
            core_reasons.append("three_buy_lacks_tick_clearance")
    # 技术候选只回答 5 分钟正式结构是否成立；1 分钟区间套及其瞬时边界
    # 可以关闭精确执行资格，但不能反向抹掉 5 分钟主信号。
    technical_entry_allowed = not any(
        reason != "one_minute_not_confirmed" for reason in core_reasons
    )
    data_integrity_reasons = _higher_timeframe_data_integrity_reason_codes_from_risk(
        risk
    )
    if data_integrity_reasons is None:
        return False
    data_integrity_blocked = bool(data_integrity_reasons)
    warmup_blocked = not five_minute_warmup_converged
    entry_reasons = list(core_reasons)
    if data_integrity_blocked:
        entry_reasons.extend(
            (
                "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
                *data_integrity_reasons,
            )
        )
    if warmup_blocked:
        entry_reasons.extend(
            (
                "WARMUP_CONVERGENCE_GATE_FAILED",
                *trade_level_warmup_reasons,
            )
        )

    advisory_reasons: list[str] = []
    if context.get("hard_block") is True:
        advisory_reasons.append("thirty_minute_hostile")
    if daily_context.get("hard_block") is True:
        advisory_reasons.append("daily_structure_hostile")
    if (
        signal.get("selection_path") == "INDIVIDUAL_THREE_PROGRAM"
        and sector.get("hard_block") is True
    ):
        advisory_reasons.append("sector_hostile")
    raw_profile_advisories = _unique_string_list(profile.get("advisory_reason_codes"))
    if raw_profile_advisories is None:
        return False
    if "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN" in raw_profile_advisories:
        sector_required = signal.get("selection_path") == "INDIVIDUAL_THREE_PROGRAM"
        if not (
            risk.get("market_gate") != "GREEN"
            or sector_required
            and risk.get("sector_gate") != "GREEN"
            or risk.get("symbol_gate") != "GREEN"
        ):
            return False
        advisory_reasons.extend(
            (
                "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN",
                f"MARKET_GATE_{risk.get('market_gate')}",
                *(
                    (f"SECTOR_GATE_{risk.get('sector_gate')}",)
                    if sector_required
                    else ()
                ),
                f"SYMBOL_GATE_{risk.get('symbol_gate')}",
                *(str(value) for value in risk.get("reason_codes") or ()),
            )
        )
    assessment = context.get("signal_context_assessment")
    if not isinstance(assessment, Mapping):
        return False
    grade = assessment.get("grade")
    if grade not in {"A", "B", "C", "UNRESOLVED"}:
        return False
    if grade != "A":
        advisory_reasons.append(f"SAME_PERIOD_CONTEXT_GRADE_{grade}")
    advisory_reasons.extend(context_warmup_advisories)
    entry_boundary_reason: str | None = None
    if (
        trigger_confirmed
        and signal.get("physical_timeframe_recursive") is True
        and str(signal.get("code") or "").startswith(("SH.", "SZ.", "BJ."))
    ):
        raw_boundary = signal.get("entry_execution_boundary")
        if raw_boundary is None:
            try:
                trigger_available_at = datetime.fromisoformat(
                    str(trigger["available_at"])
                )
                observed_at = datetime.fromisoformat(str(signal["observed_at"]))
                inferred_valid_until = a_share_optional_entry_valid_until(
                    trigger_available_at
                )
            except (KeyError, TypeError, ValueError):
                return False
            if observed_at.tzinfo is None:
                return False
            entry_boundary_reason = (
                "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
                if inferred_valid_until <= observed_at
                else "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING"
            )
        else:
            try:
                boundary = parse_entry_execution_boundary_document(raw_boundary)
                observed_at = datetime.fromisoformat(str(signal["observed_at"]))
            except (TypeError, ValueError):
                return False
            if observed_at.tzinfo is None:
                return False
            if boundary.entry_valid_until <= observed_at:
                entry_boundary_reason = "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
    if entry_boundary_reason is not None:
        # 生产核心先追加 1 分钟边界原因，再追加外层数据完整性/暖机原因。
        entry_reasons = [
            *core_reasons,
            entry_boundary_reason,
            *entry_reasons[len(core_reasons) :],
        ]
    conflict = signal.get("conflict")
    conflict_hard = bool(
        isinstance(conflict, Mapping) and conflict.get("hard_block") is True
    )
    if conflict_reasons and not conflict_hard:
        advisory_reasons.extend(conflict_reasons)
    formal_consistent, formal_reasons, formal_accepted = (
        _formal_selection_gate_is_consistent(signal)
    )
    if not formal_consistent:
        return False
    if not formal_accepted:
        advisory_reasons.extend(formal_reasons)
    expected_advisories = tuple(dict.fromkeys(advisory_reasons))
    setup_conflict_reasons = (
        (
            unconfirmed_setup_reason_code(
                formation_state,
                forming_reason_code="setup_not_confirmed",
            ),
        )
        if setup.get("status") == "provisional"
        else ()
    )
    base_expected_decision_reasons = tuple(
        dict.fromkeys(
            (
                *entry_reasons,
                *expected_advisories,
                *(conflict_reasons if conflict_hard else ()),
                *setup_conflict_reasons,
                *(
                    (STRUCTURE_INVALIDATED_REASON_CODE,)
                    if signal.get("lifecycle_stage") == "invalidated"
                    else ()
                ),
            )
        )
    )
    multiplier_field = {
        "1buy": "first_buy_risk_multiplier",
        "2buy": "second_buy_risk_multiplier",
        "3buy": "third_buy_risk_multiplier",
    }.get(str(setup.get("point_type")))
    try:
        expected_multiplier = (
            Decimal(str(policy[multiplier_field]))
            if confirmed_point and multiplier_field is not None
            else Decimal("0")
        )
        expected_stop = (
            Decimal(str(setup["invalidation_price"])) if confirmed_point else None
        )
        actual_stop = (
            None
            if signal.get("structural_stop") is None
            else Decimal(str(signal["structural_stop"]))
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if data_integrity_blocked or warmup_blocked:
        expected_multiplier *= 0
    hard_profile_reasons = tuple(
        dict.fromkeys(
            (
                *(
                    reason
                    for reason in entry_reasons
                    if reason
                    not in {
                        "five_minute_not_confirmed",
                        "lifecycle_not_actionable",
                        "one_minute_not_confirmed",
                        GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE,
                    }
                ),
                *(conflict_reasons if conflict_hard else ()),
                *(
                    (STRUCTURE_INVALIDATED_REASON_CODE,)
                    if signal.get("lifecycle_stage") == "invalidated"
                    else ()
                ),
            )
        )
    )
    if signal.get("lifecycle_stage") == "invalidated":
        recommendation = "BLOCKED"
    elif hard_profile_reasons:
        recommendation = "BLOCKED"
    elif not confirmed_point:
        recommendation = unconfirmed_setup_recommendation(formation_state)
    elif not trigger_confirmed:
        recommendation = WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
    elif expected_advisories:
        recommendation = "CAUTION"
    else:
        recommendation = "READY"
    context_scale = {
        "A": "1.00",
        "B": "0.75",
        "C": "0.50",
        "UNRESOLVED": "0.50",
    }[grade]
    expected_position_recommendation = build_position_recommendation(
        side="buy",
        recommendation=recommendation,
        risk_multiplier=expected_multiplier,
        context_risk_scale=context_scale,
        entry_price=(
            signal.get("current_price")
            if signal.get("current_price") is not None
            else setup.get("anchor_price")
        ),
        structural_stop=setup.get("invalidation_price"),
        exit_action="none",
        structure_anchor_price=setup.get("anchor_price"),
    ).document()
    operational_buy_protections = tuple(
        reason
        for reason in expected_position_recommendation["reason_codes"]
        if reason in BUY_SIGNAL_PROTECTION_REASON_CODES
    )
    if operational_buy_protections:
        recommendation = "BLOCKED"
        hard_profile_reasons = tuple(
            dict.fromkeys(
                (*hard_profile_reasons, *operational_buy_protections)
            )
        )
    expected_decision_reasons = tuple(
        dict.fromkeys(
            (*base_expected_decision_reasons, *operational_buy_protections)
        )
    )
    if not confirmed_point:
        precision_locator_status = "STRUCTURE_PENDING"
    elif not trigger_confirmed:
        precision_locator_status = "WAITING_ONE_MINUTE"
    elif entry_boundary_reason == "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED":
        precision_locator_status = "BOUNDARY_EXPIRED"
    elif entry_boundary_reason == "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING":
        precision_locator_status = "BOUNDARY_MISSING"
    else:
        precision_locator_status = "READY"
    precision_locator_ready = precision_locator_status == "READY"
    return bool(
        profile.get("structure_signal_confirmed") is confirmed_point
        and profile.get("execution_trigger_confirmed") is trigger_confirmed
        and profile.get("one_minute_role") == "SEGMENT_DIFFERENCE_ONLY"
        and profile.get("one_minute_required_for_trade_signal") is False
        and profile.get("one_minute_required_for_precise_execution") is True
        and profile.get("one_minute_segment_difference_present") is trigger_confirmed
        and profile.get("precision_locator_status") == precision_locator_status
        and profile.get("precision_locator_ready") is precision_locator_ready
        and profile.get("precise_execution_ready")
        is bool(
            precision_locator_ready
            and not entry_reasons
            and not operational_buy_protections
        )
        and profile.get("recommendation") == recommendation
        and profile.get("recommendation_label")
        == execution_recommendation_label(recommendation)
        and profile.get("hard_blocked") is (recommendation == "BLOCKED")
        and _unique_string_list(profile.get("hard_block_reason_codes"))
        == hard_profile_reasons
        and raw_profile_advisories == expected_advisories
        and profile.get("context_grade") == grade
        and profile.get("context_risk_scale") == context_scale
        and profile.get("context_risk_scale_role") == "MANUAL_POSITION_SIZING_ONLY"
        and signal.get("position_recommendation") == expected_position_recommendation
        and profile.get("position_recommendation") == expected_position_recommendation
        and profile.get("manual_confirmation_required") is True
        and profile.get("automated_order_authorized") is False
        and signal.get("technical_entry_allowed") is technical_entry_allowed
        and signal.get("entry_allowed")
        is (not entry_reasons and not operational_buy_protections)
        and signal.get("exit_allowed") is False
        and signal.get("exit_action") == "none"
        and multiplier == expected_multiplier
        and actual_stop == expected_stop
        and decision_reasons == expected_decision_reasons
    )


def _buy_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
    conflict_reasons: tuple[str, ...],
) -> bool:
    if isinstance(signal.get("execution_profile"), Mapping):
        return _separated_buy_decision_evidence_is_consistent(
            signal,
            policy=policy,
            risk=risk,
            warmup=warmup,
            conflict_reasons=conflict_reasons,
        )
    setup = signal.get("setup_5m")
    trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
    sector = signal.get("sector")
    context = signal.get("context_30m")
    daily_context = signal.get("context_d")
    decision_reasons = _unique_string_list(signal.get("decision_reasons"))
    multiplier = _decision_decimal(signal.get("risk_multiplier"))
    if (
        not all(
            isinstance(value, Mapping)
            for value in (setup, sector, context, daily_context)
        )
        or (trigger is not None and not isinstance(trigger, Mapping))
        or decision_reasons is None
        or multiplier is None
    ):
        return False
    has_structural_lineage = bool(
        isinstance(setup.get("price_basis_revision"), str)
        and str(setup.get("price_basis_revision"))
    )
    confirmed_structural_point = bool(
        has_structural_lineage and setup.get("status") == "confirmed"
    )
    confirmed_buy = _confirmed_five_minute_operation_setup(
        setup,
        side="buy",
    )
    minimum_tick = _decision_decimal(policy.get("minimum_tick"))
    trigger_confirmed = bool(
        confirmed_structural_point
        and isinstance(trigger, Mapping)
        and minimum_tick is not None
        and is_one_minute_segment_difference_document(
            trigger,
            minimum_tick=minimum_tick,
            expected_side=str(setup.get("side")),
        )
        and _one_minute_locator_follows_five_minute_setup(setup, trigger)
        and signal.get("lifecycle_stage")
        in {"triggered", "executable", "active"}
    )
    entry_reasons: list[str] = []
    if policy.get("require_confirmed_five_minute") is True and not confirmed_buy:
        entry_reasons.append("five_minute_not_confirmed")
    if policy.get("require_confirmed_one_minute") is True and not trigger_confirmed:
        entry_reasons.append("one_minute_not_confirmed")
    if (
        policy.get("require_sector_eligibility") is True
        and signal.get("selection_path") == "INDIVIDUAL_THREE_PROGRAM"
        and sector.get("hard_block") is True
    ):
        entry_reasons.append("sector_hostile")
    if (
        policy.get("require_thirty_minute_context") is True
        and context.get("hard_block") is True
    ):
        entry_reasons.append("thirty_minute_hostile")
    conflict = signal.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("hard_block") is True:
        entry_reasons.append("structure_conflict")
    if confirmed_structural_point and setup.get("point_type") == "3buy":
        try:
            clearance = Decimal(str(setup["anchor_price"])) - Decimal(
                str(setup["center_zg"])
            )
            minimum_tick = Decimal(str(policy["minimum_tick"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            clearance = None
            minimum_tick = Decimal("0")
        if (
            setup.get("variant") == "boundary_touch"
            or clearance is None
            or not clearance.is_finite()
            or clearance < minimum_tick
        ):
            entry_reasons.append("three_buy_lacks_tick_clearance")
    multiplier_field = {
        "1buy": "first_buy_risk_multiplier",
        "2buy": "second_buy_risk_multiplier",
        "3buy": "third_buy_risk_multiplier",
    }.get(str(setup.get("point_type")))
    try:
        expected_multiplier = (
            Decimal(str(policy[multiplier_field]))
            if confirmed_structural_point and multiplier_field is not None
            else Decimal("0")
        )
        expected_stop = (
            Decimal(str(setup["invalidation_price"]))
            if confirmed_structural_point
            else None
        )
        actual_stop = (
            None
            if signal.get("structural_stop") is None
            else Decimal(str(signal["structural_stop"]))
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if (
        not expected_multiplier.is_finite()
        or expected_multiplier < 0
        or expected_stop is not None
        and (not expected_stop.is_finite() or expected_stop <= 0)
        or actual_stop is not None
        and (not actual_stop.is_finite() or actual_stop <= 0)
    ):
        return False
    if not entry_reasons and daily_context.get("hard_block") is True:
        entry_reasons.append("daily_structure_hostile")
        expected_multiplier *= 0
    technical_entry_allowed = not entry_reasons
    sector_required = signal.get("selection_path") == ("INDIVIDUAL_THREE_PROGRAM")
    risk_blocked = bool(
        risk.get("market_gate") != "GREEN"
        or (sector_required and risk.get("sector_gate") != "GREEN")
        or risk.get("symbol_gate") != "GREEN"
    )
    warmup_blocked = warmup.get("converged") is not True
    if technical_entry_allowed and (risk_blocked or warmup_blocked):
        if risk_blocked:
            entry_reasons.extend(
                (
                    "HIGHER_TIMEFRAME_GATE_NOT_GREEN",
                    f"MARKET_GATE_{risk.get('market_gate')}",
                    *(
                        (f"SECTOR_GATE_{risk.get('sector_gate')}",)
                        if sector_required
                        else ()
                    ),
                    f"SYMBOL_GATE_{risk.get('symbol_gate')}",
                    *(str(value) for value in risk.get("reason_codes") or ()),
                )
            )
        if warmup_blocked:
            entry_reasons.extend(
                (
                    "WARMUP_CONVERGENCE_GATE_FAILED",
                    *(str(value) for value in warmup.get("reason_codes") or ()),
                )
            )
        expected_multiplier *= 0
    # 当 setup 仍是 ProvisionalCandidate 时，``resolve_conflict`` 会给出一条明确的
    # 来源原因。候选点尚无可进入冲突账本的正式点身份，因此这里必须依据 setup
    # 状态重建该原因，不能从空的冲突点列表反推。
    setup_conflict_reasons = (
        ("setup_not_confirmed",) if setup.get("status") == "provisional" else ()
    )
    formal_consistent, formal_reasons, formal_accepted = (
        _formal_selection_gate_is_consistent(signal)
    )
    if not formal_consistent:
        return False
    selection_reasons = () if formal_accepted else formal_reasons
    if selection_reasons:
        # 决策核心先应用正式选股门，再由序列化过程追加 setup 冲突原因；这里必须
        # 保持相同顺序，才能从归档文档独立重建规范原因序列。
        entry_reasons.extend(selection_reasons)
        expected_multiplier *= 0
    expected_entry_reasons = tuple(dict.fromkeys(entry_reasons))
    expected_decision_reasons = tuple(
        dict.fromkeys(
            (
                *expected_entry_reasons,
                *conflict_reasons,
                *setup_conflict_reasons,
                *(
                    (STRUCTURE_INVALIDATED_REASON_CODE,)
                    if signal.get("lifecycle_stage") == "invalidated"
                    else ()
                ),
            )
        )
    )
    return bool(
        signal.get("technical_entry_allowed") is technical_entry_allowed
        and signal.get("entry_allowed")
        is (not expected_entry_reasons and not selection_reasons)
        and signal.get("exit_allowed") is False
        and signal.get("exit_action") == "none"
        and multiplier == expected_multiplier
        and actual_stop == expected_stop
        and decision_reasons == expected_decision_reasons
    )


def _displayed_decision_evidence_is_consistent(
    signal: Mapping[str, object],
    *,
    policy: object,
    risk: Mapping[str, object],
    warmup: Mapping[str, object],
) -> bool:
    context = signal.get("context_30m")
    decision_reasons = _unique_string_list(signal.get("decision_reasons"))
    conflict_reasons = _conflict_reason_codes(signal.get("conflict"))
    if (
        not isinstance(policy, Mapping)
        or not _decision_context_is_consistent(context)
        or decision_reasons is None
        or conflict_reasons is None
    ):
        return False
    if signal.get("side") == "buy":
        return _buy_decision_evidence_is_consistent(
            signal,
            policy=policy,
            risk=risk,
            warmup=warmup,
            conflict_reasons=conflict_reasons,
        )
    if isinstance(signal.get("execution_profile"), Mapping):
        profile = signal["execution_profile"]
        setup = signal.get("setup_5m")
        trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
        sector = signal.get("sector")
        daily_context = signal.get("context_d")
        if not all(
            isinstance(value, Mapping)
            for value in (profile, setup, context, sector, daily_context)
        ) or (trigger is not None and not isinstance(trigger, Mapping)):
            return False
        confirmed_sell = _confirmed_five_minute_operation_setup(
            setup,
            side="sell",
        )
        formation_state = _canonical_setup_formation_state(setup)
        if formation_state is None:
            return False
        minimum_tick = _decision_decimal(policy.get("minimum_tick"))
        trigger_confirmed = bool(
            confirmed_sell
            and isinstance(trigger, Mapping)
            and minimum_tick is not None
            and is_one_minute_segment_difference_document(
                trigger,
                minimum_tick=minimum_tick,
                expected_side="sell",
            )
            and _one_minute_locator_follows_five_minute_setup(setup, trigger)
            and signal.get("lifecycle_stage")
            in {"triggered", "executable", "active"}
        )
        exit_action = signal.get("exit_action")
        lifecycle_actionable = signal.get("lifecycle_stage") in {
            "triggered",
            "executable",
            "active",
        }
        if not confirmed_sell:
            exit_reasons = (
                unconfirmed_setup_reason_code(
                    formation_state,
                    forming_reason_code="sell_not_confirmed",
                ),
            )
        elif not lifecycle_actionable:
            exit_reasons = ("lifecycle_not_actionable",)
        elif (
            policy.get("require_confirmed_one_minute") is True and not trigger_confirmed
        ):
            exit_reasons = ("one_minute_sell_not_confirmed",)
        elif exit_action == "none":
            exit_reasons = (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,)
        elif exit_action == "exit_full":
            exit_reasons = ("same_or_higher_sell",)
        elif exit_action == "reduce_tactical":
            exit_reasons = ("lower_or_different_structure_sell",)
        else:
            return False
        context_advisories: list[str] = []
        if context.get("hard_block") is True:
            context_advisories.append("thirty_minute_hostile")
        if daily_context.get("hard_block") is True:
            context_advisories.append("daily_structure_hostile")
        if (
            signal.get("selection_path") == "INDIVIDUAL_THREE_PROGRAM"
            and sector.get("hard_block") is True
        ):
            context_advisories.append("sector_hostile")
        assessment = context.get("signal_context_assessment")
        if not isinstance(assessment, Mapping) or assessment.get("grade") not in {
            "A",
            "B",
            "C",
            "UNRESOLVED",
        }:
            return False
        grade = str(assessment["grade"])
        if grade != "A":
            context_advisories.append(f"SAME_PERIOD_CONTEXT_GRADE_{grade}")
        expected_profile_advisories = tuple(
            dict.fromkeys(
                (
                    *context_advisories,
                    *(
                        (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,)
                        if exit_reasons
                        == (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,)
                        else ()
                    ),
                )
            )
        )
        expected_decision_reasons = tuple(
            dict.fromkeys(
                (
                    *exit_reasons,
                    *context_advisories,
                    *(
                        (STRUCTURE_INVALIDATED_REASON_CODE,)
                        if signal.get("lifecycle_stage") == "invalidated"
                        else ()
                    ),
                )
            )
        )
        hard_profile_reasons = tuple(
            reason
            for reason in expected_decision_reasons
            if reason
            not in {
                "sell_not_confirmed",
                "lifecycle_not_actionable",
                "one_minute_sell_not_confirmed",
                GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE,
                SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,
                *expected_profile_advisories,
            }
        )
        if signal.get("lifecycle_stage") == "invalidated":
            recommendation = "BLOCKED"
        elif hard_profile_reasons:
            recommendation = "BLOCKED"
        elif not confirmed_sell:
            recommendation = unconfirmed_setup_recommendation(formation_state)
        elif not trigger_confirmed:
            recommendation = WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
        elif expected_profile_advisories:
            recommendation = "CAUTION"
        else:
            recommendation = "READY"
        context_scale = {
            "A": "1.00",
            "B": "0.75",
            "C": "0.50",
            "UNRESOLVED": "0.50",
        }[grade]
        expected_position_recommendation = build_position_recommendation(
            side="sell",
            recommendation=recommendation,
            risk_multiplier="0",
            context_risk_scale=context_scale,
            entry_price=(
                signal.get("current_price")
                if signal.get("current_price") is not None
                else setup.get("anchor_price")
            ),
            structural_stop=setup.get("invalidation_price"),
            exit_action=str(exit_action),
            structure_anchor_price=setup.get("anchor_price"),
        ).document()
        precision_locator_status = (
            "STRUCTURE_PENDING"
            if not confirmed_sell
            else "WAITING_ONE_MINUTE"
            if not trigger_confirmed
            else "READY"
        )
        precision_locator_ready = precision_locator_status == "READY"
        return bool(
            signal.get("side") == "sell"
            and _decision_decimal(signal.get("risk_multiplier")) == Decimal("0")
            and signal.get("structural_stop") is None
            and signal.get("technical_entry_allowed") is False
            and signal.get("entry_allowed") is False
            and type(signal.get("exit_allowed")) is bool
            and signal.get("exit_allowed") is (exit_action != "none")
            and not conflict_reasons
            and decision_reasons == expected_decision_reasons
            and profile.get("structure_signal_confirmed") is confirmed_sell
            and profile.get("execution_trigger_confirmed") is trigger_confirmed
            and profile.get("one_minute_role") == "SEGMENT_DIFFERENCE_ONLY"
            and profile.get("one_minute_required_for_trade_signal") is False
            and profile.get("one_minute_required_for_precise_execution") is True
            and profile.get("one_minute_segment_difference_present")
            is trigger_confirmed
            and profile.get("precision_locator_status") == precision_locator_status
            and profile.get("precision_locator_ready") is precision_locator_ready
            and profile.get("precise_execution_ready")
            is bool(precision_locator_ready and exit_action != "none")
            and profile.get("recommendation") == recommendation
            and profile.get("recommendation_label")
            == execution_recommendation_label(recommendation)
            and profile.get("hard_blocked") is (recommendation == "BLOCKED")
            and _unique_string_list(profile.get("hard_block_reason_codes"))
            == hard_profile_reasons
            and _unique_string_list(profile.get("advisory_reason_codes"))
            == expected_profile_advisories
            and profile.get("context_grade") == grade
            and profile.get("context_risk_scale") == context_scale
            and profile.get("context_risk_scale_role") == "MANUAL_POSITION_SIZING_ONLY"
            and signal.get("position_recommendation")
            == expected_position_recommendation
            and profile.get("position_recommendation")
            == expected_position_recommendation
            and profile.get("manual_confirmation_required") is True
            and profile.get("automated_order_authorized") is False
        )
    multiplier = _decision_decimal(signal.get("risk_multiplier"))
    setup = signal.get("setup_5m")
    trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
    if not isinstance(setup, Mapping):
        return False
    confirmed_sell = _confirmed_five_minute_operation_setup(
        setup,
        side="sell",
    )
    minimum_tick = _decision_decimal(policy.get("minimum_tick"))
    trigger_confirmed = bool(
        isinstance(trigger, Mapping)
        and minimum_tick is not None
        and is_one_minute_segment_difference_document(
            trigger,
            minimum_tick=minimum_tick,
            expected_side="sell",
        )
        and _one_minute_locator_follows_five_minute_setup(setup, trigger)
        and signal.get("lifecycle_stage")
        in {"triggered", "executable", "active"}
    )
    exit_action = signal.get("exit_action")
    lifecycle_actionable = signal.get("lifecycle_stage") in {
        "triggered",
        "executable",
        "active",
    }
    if not confirmed_sell:
        expected_sell_reasons = ("sell_not_confirmed",)
    elif not lifecycle_actionable:
        expected_sell_reasons = ("lifecycle_not_actionable",)
    elif policy.get("require_confirmed_one_minute") is True and not trigger_confirmed:
        expected_sell_reasons = ("one_minute_sell_not_confirmed",)
    elif exit_action == "none":
        expected_sell_reasons = (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,)
    elif exit_action == "exit_full":
        expected_sell_reasons = ("same_or_higher_sell",)
    elif exit_action == "reduce_tactical":
        expected_sell_reasons = ("lower_or_different_structure_sell",)
    else:
        return False
    expected_sell_reasons = tuple(
        dict.fromkeys(
            (
                *expected_sell_reasons,
                *(
                    (STRUCTURE_INVALIDATED_REASON_CODE,)
                    if signal.get("lifecycle_stage") == "invalidated"
                    else ()
                ),
            )
        )
    )
    return bool(
        signal.get("side") == "sell"
        and multiplier == Decimal("0")
        and signal.get("structural_stop") is None
        and signal.get("technical_entry_allowed") is False
        and signal.get("entry_allowed") is False
        and type(signal.get("exit_allowed")) is bool
        and exit_action in {"none", "reduce_tactical", "exit_full"}
        and signal.get("exit_allowed") is (exit_action != "none")
        and not conflict_reasons
        and decision_reasons == expected_sell_reasons
    )


def _validated_sector_strength_evidence(
    payload: Mapping[str, object],
    *,
    review_at: datetime,
) -> SectorStrengthBatch:
    raw = payload.get("sector_strength_evidence")
    revision = payload.get("sector_strength_evidence_revision")
    manifest = payload.get("coverage_manifest")
    try:
        batch = sector_strength_batch_from_evidence_document(raw)
        scanned_at = datetime.fromisoformat(str(payload["scanned_at"]))
        evidence_at = datetime.fromisoformat(
            str(batch.evidence_document()["decision_time"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening sector strength evidence is invalid") from exc
    document = batch.evidence_document()
    if (
        not _is_sha256_identity(revision)
        or revision != batch.evidence_revision
        or not isinstance(manifest, Mapping)
        or document.get("membership_revision")
        != manifest.get("sector_catalog_revision")
        or scanned_at.tzinfo is None
        or evidence_at.tzinfo is None
        or evidence_at.date() != review_at.date()
        or evidence_at < review_at
        or evidence_at > scanned_at
    ):
        raise ValueError("live screening sector strength evidence is invalid")
    return batch


def _authenticated_sector_members(
    strength_evidence: SectorStrengthBatch,
) -> dict[str, frozenset[str]]:
    """从已验证批次中恢复当前 QMT 板块成员关系。

    精简强度对象有意省略完整成员列表，但规范批次证据会保留。仅凭声明板块具备资格且有
    排名，不能证明板块触发；信号标的还必须出现在该板块已认证的当前 QMT 成分篮子中。
    调用本函数前，解析器已检查标的唯一性、排序、数量和批次证据身份。
    """

    document = strength_evidence.evidence_document()
    raw_sectors = document.get("sectors")
    if not isinstance(raw_sectors, list):  # pragma: no cover - parser guards it
        raise ValueError("live screening sector membership is invalid")
    output: dict[str, frozenset[str]] = {}
    for raw in raw_sectors:
        if not isinstance(raw, Mapping):  # pragma: no cover - parser guards it
            raise ValueError("live screening sector membership is invalid")
        sector_id = raw.get("sector_id")
        members = raw.get("member_symbols")
        if not isinstance(sector_id, str) or not isinstance(members, list):
            raise ValueError("live screening sector membership is invalid")
        output[sector_id] = frozenset(str(value) for value in members)
    return output


def _sector_exclusion_documents(
    raw: object,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("live screening sector exclusions are unavailable")
    expected_keys = {
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
    allowed_details = {
        "sector_catalog_members_missing",
        "sector_constituent_count_below_minimum",
        "sector_universe_member_coverage_insufficient",
    }
    output: dict[str, Mapping[str, object]] = {}
    ordered_ids: list[str] = []
    for document in raw:
        if not isinstance(document, Mapping) or set(document) != expected_keys:
            raise ValueError("live screening sector exclusion is malformed")
        sector_id = document.get("sector_id")
        code = document.get("code")
        detail_code = document.get("detail_code")
        catalog_count = document.get("catalog_member_count")
        universe_count = document.get("universe_member_count")
        required_count = document.get("required_member_count")
        if (
            not isinstance(sector_id, str)
            or not sector_id
            or not isinstance(code, str)
            or not code
            or document.get("exclusion_type") != "sector_analysis_exclusion"
            or document.get("eligibility") != "MINIMUM_SECTOR_MEMBERS_NOT_MET"
            or document.get("reason_code") != "sector_member_coverage_insufficient"
            or not isinstance(document.get("reason"), str)
            or not document.get("reason")
            or detail_code not in allowed_details
            or type(catalog_count) is not int
            or catalog_count < 0
            or type(universe_count) is not int
            or universe_count < 0
            or type(required_count) is not int
            or required_count <= 0
            or universe_count >= required_count
            or document.get("deterministic_for_catalog_revision") is not True
            or document.get("retry_policy") != "NEXT_SECTOR_CATALOG_REVISION"
        ):
            raise ValueError("live screening sector exclusion is malformed")
        expected_detail = (
            "sector_catalog_members_missing"
            if catalog_count == 0
            else (
                "sector_constituent_count_below_minimum"
                if catalog_count < required_count
                else "sector_universe_member_coverage_insufficient"
            )
        )
        expected_reason = (
            f"catalog_members={catalog_count}; "
            f"universe_members={universe_count}; required={required_count}"
        )
        if detail_code != expected_detail or document.get("reason") != expected_reason:
            raise ValueError("live screening sector exclusion is inconsistent")
        ordered_ids.append(sector_id)
        output[sector_id] = document
    if ordered_ids != sorted(set(ordered_ids)) or len(output) != len(raw):
        raise ValueError("live screening sector exclusions are not canonical")
    return output


def _validated_sector_documents(
    payload: Mapping[str, object],
    *,
    review_at: datetime,
    market_data_as_of: datetime,
    strength_evidence: SectorStrengthBatch,
) -> dict[str, Mapping[str, object]]:
    """重新计算完整板块评估集合及其中具备资格的排名子集。

    页面有意发布每个完成或失败的板块评估：合格板块获得排名，结构不利或明确失败的板块
    保持无排名，使仅监听标的仍保留真实板块上下文。若把列表误当成仅含合格板块的表，
    所有包含高级别板块阻断的生产快照都会被错误拒绝。
    """

    raw_sectors = payload.get("sectors")
    audit = payload.get("scan_audit")
    raw_errors = payload.get("errors")
    raw_exclusions = payload.get("sector_exclusions")
    if (
        not isinstance(raw_sectors, list)
        or not isinstance(audit, Mapping)
        or not isinstance(raw_errors, list)
    ):
        raise ValueError("live screening sector ranking is unavailable")
    sector_exclusions = _sector_exclusion_documents(raw_exclusions)
    sector_failures: dict[str, Mapping[str, object]] = {}
    failure_counts: dict[str, int] = {}
    for error in raw_errors:
        if not isinstance(error, Mapping) or "sector_id" not in error:
            continue
        failed_sector_id = error.get("sector_id")
        error_type = error.get("error_type")
        reason = error.get("reason")
        detail_code = error.get("detail_code")
        if (
            not isinstance(failed_sector_id, str)
            or not failed_sector_id
            or failed_sector_id in sector_failures
            or not isinstance(error_type, str)
            or not error_type
            or error_type == "sector_member_coverage_insufficient"
            or not isinstance(reason, str)
            or not reason
            or detail_code is not None
            and (not isinstance(detail_code, str) or not detail_code)
        ):
            raise ValueError("live screening sector failure evidence is invalid")
        sector_failures[failed_sector_id] = error
        failure_counts[error_type] = failure_counts.get(error_type, 0) + 1
    if set(sector_failures) & set(sector_exclusions):
        raise ValueError("live screening sector dispositions overlap")
    sectors: dict[str, Mapping[str, object]] = {}
    sortable: list[tuple[bool, Decimal, int, str]] = []
    ordered_ids: list[str] = []
    unranked_ids: list[str] = []
    for raw in raw_sectors:
        if not isinstance(raw, Mapping):
            raise ValueError("live screening sector ranking is invalid")
        required_fields = {
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
        sector_id = raw.get("sector_id")
        sector_name = raw.get("sector_name")
        contexts = {
            "30m": raw.get("context_30m"),
            "5m": raw.get("context_5m"),
            "1m": raw.get("context_1m"),
        }
        if (
            not isinstance(sector_id, str)
            or required_fields - set(raw)
            or not sector_id
            or sector_id in sectors
            or not isinstance(sector_name, str)
            or not sector_name
        ):
            raise ValueError("live screening sector ranking is invalid")
        failure = sector_failures.get(sector_id)
        exclusion = sector_exclusions.get(sector_id)
        if failure is not None or exclusion is not None:
            if any(context is not None for context in contexts.values()):
                raise ValueError("live screening unavailable sector context is invalid")
            hard_block = True
            expected_components: dict[str, int] = {}
            expected_score = 0
            expected_regime = "hostile"
            if failure is not None:
                expected_reasons = [str(failure["error_type"])]
                if failure.get("detail_code") is not None:
                    expected_reasons.append(str(failure["detail_code"]))
            else:
                assert exclusion is not None
                expected_reasons = [
                    str(exclusion["reason_code"]),
                    str(exclusion["detail_code"]),
                ]
            eligible_for_ranking = False
        else:
            if any(
                not _sector_context_is_consistent(
                    context,
                    frequency=frequency,
                    evidence_cutoff=market_data_as_of,
                )
                for frequency, context in contexts.items()
            ):
                raise ValueError("live screening sector ranking is invalid")
            thirty = contexts["30m"]
            five = contexts["5m"]
            one = contexts["1m"]
            assert isinstance(thirty, Mapping)
            assert isinstance(five, Mapping)
            assert isinstance(one, Mapping)
            hard_block = thirty.get("hard_block") is True
            expected_components = {
                "thirty_support": (
                    40 if thirty.get("disposition") == "supportive" else 0
                ),
                "five_support": (30 if five.get("disposition") == "supportive" else 0),
                "neutral_access": 0 if hard_block else 5,
            }
            expected_score = sum(expected_components.values())
            expected_regime = (
                "hostile"
                if hard_block
                else "supportive"
                if any(
                    expected_components[name] > 0
                    for name in (
                        "thirty_support",
                        "five_support",
                    )
                )
                else "neutral"
            )
            expected_reasons = (
                ["higher_structure_sell_risk"]
                if hard_block
                else ["structural_ranking_only"]
            )
            eligible_for_ranking = not hard_block
        raw_rank = raw.get("rank")
        raw_strength = raw.get("horizontal_strength")
        horizontal_rank = raw.get("horizontal_rank")
        source_revision = raw.get("strength_source_revision")
        strength_reasons = raw.get("strength_reason_codes")
        try:
            strength = None if raw_strength is None else Decimal(str(raw_strength))
            anchor = (
                None
                if raw.get("strength_anchor_session") is None
                else date.fromisoformat(str(raw["strength_anchor_session"]))
            )
            member_count = int(raw.get("strength_member_count"))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("live screening sector strength is invalid") from None
        strength_resolved = strength is not None
        expected_strength = strength_evidence.get(sector_id)
        if (
            raw.get("eligible") is not (not hard_block)
            or raw.get("hard_block") is not hard_block
            or (
                type(raw_rank) is not int
                if eligible_for_ranking
                else raw_rank is not None
            )
            or eligible_for_ranking
            and raw_rank <= 0
            or raw.get("rank_components") != expected_components
            or raw.get("rank_score") != expected_score
            or raw.get("regime") != expected_regime
            or raw.get("reason_codes") != expected_reasons
            or strength is not None
            and not strength.is_finite()
            or strength_resolved
            != (type(horizontal_rank) is int and horizontal_rank > 0)
            or strength_resolved
            and (
                anchor is None
                or anchor > review_at.date()
                or member_count <= 0
                or not _is_sha256_identity(source_revision)
            )
            or not strength_resolved
            and (
                horizontal_rank is not None
                or source_revision is None
                and (anchor is not None or member_count != 0)
                or source_revision is not None
                and (
                    not _is_sha256_identity(source_revision)
                    or anchor is not None
                    and anchor > review_at.date()
                )
            )
            or not isinstance(strength_reasons, list)
            or any(
                not isinstance(value, str) or not value for value in strength_reasons
            )
            or len(strength_reasons) != len(set(strength_reasons))
            or expected_strength is None
            or strength != expected_strength.strength
            or horizontal_rank != expected_strength.rank
            or anchor != expected_strength.anchor_session
            or member_count != expected_strength.member_count
            or source_revision != expected_strength.source_revision
            or strength_reasons != list(expected_strength.reason_codes)
        ):
            raise ValueError("live screening sector ranking is invalid")
        sectors[sector_id] = raw
        ordered_ids.append(sector_id)
        if eligible_for_ranking:
            sortable.append(
                (
                    strength is None,
                    -(strength if strength is not None else Decimal("0")),
                    -expected_score,
                    sector_id,
                )
            )
        else:
            unranked_ids.append(sector_id)
    expected_ranked_order = [
        sector_id for _missing, _strength, _score, sector_id in sorted(sortable)
    ]
    expected_order = expected_ranked_order + sorted(unranked_ids)
    if ordered_ids != expected_order:
        raise ValueError("live screening sector ranking order is invalid")
    if any(
        sectors[sector_id].get("rank") != ordinal
        for ordinal, sector_id in enumerate(expected_ranked_order, start=1)
    ):
        raise ValueError("live screening sector ranking is invalid")
    try:
        discovered_count = int(audit.get("sector_discovered_count"))
        completed_count = int(audit.get("sector_completed_count"))
        excluded_count = int(audit.get("sector_excluded_count"))
        failed_count = int(audit.get("sector_failed_count"))
        resolved_count = int(audit.get("sector_resolved_count"))
        selected_count = int(audit.get("selected_sector_count"))
        completion_ratio = Decimal(str(audit.get("sector_completion_ratio")))
        resolution_ratio = Decimal(str(audit.get("sector_resolution_ratio")))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("live screening sector coverage is invalid") from None
    expected_completed = len(sectors) - len(sector_failures) - len(sector_exclusions)
    expected_resolved = expected_completed + len(sector_exclusions)
    expected_ratio = (
        Decimal("0")
        if not sectors
        else Decimal(expected_completed) / Decimal(len(sectors))
    )
    expected_resolution_ratio = (
        Decimal("0")
        if not sectors
        else Decimal(expected_resolved) / Decimal(len(sectors))
    )
    exclusion_counts: dict[str, int] = {}
    for exclusion in sector_exclusions.values():
        reason_code = str(exclusion["reason_code"])
        exclusion_counts[reason_code] = exclusion_counts.get(reason_code, 0) + 1
    if (
        discovered_count != len(sectors)
        or completed_count != expected_completed
        or excluded_count != len(sector_exclusions)
        or failed_count != len(sector_failures)
        or resolved_count != expected_resolved
        or selected_count != len(expected_ranked_order)
        or completion_ratio != expected_ratio
        or resolution_ratio != expected_resolution_ratio
        or audit.get("sector_failure_counts") != dict(sorted(failure_counts.items()))
        or audit.get("sector_exclusion_counts")
        != dict(sorted(exclusion_counts.items()))
        or set(sector_failures).difference(sectors)
        or set(sector_exclusions).difference(sectors)
        or set(strength_evidence) != set(sectors)
    ):
        raise ValueError("live screening sector coverage is invalid")
    return sectors


def validate_live_review_snapshot(
    payload: Mapping[str, object],
    *,
    session: date | None = None,
) -> tuple[datetime, tuple[Mapping[str, object], ...]]:
    """验证安全性、覆盖范围以及 30 分钟/5 分钟/1 分钟来源谱系边界。"""

    try:
        review_at = datetime.fromisoformat(str(payload["as_of"]))
        market_data_as_of = datetime.fromisoformat(str(payload["market_data_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening review cutoff is invalid") from exc
    audit = payload.get("scan_audit")
    manifest = payload.get("coverage_manifest")
    raw_signals = payload.get("signals")
    errors = payload.get("errors")
    decision_core = payload.get("decision_core")
    decision_core_id = payload.get("decision_core_id")
    verified_decision_core_id: str | None = None
    if isinstance(decision_core, Mapping):
        try:
            verified_decision_core_id = validate_human_assisted_contract_document(
                decision_core
            )
        except (TypeError, ValueError):
            verified_decision_core_id = None
    coverage_manifest_consistent = (
        _coverage_manifest_is_consistent(
            payload,
            audit=audit,
            manifest=manifest,
            errors=errors,
        )
        if isinstance(audit, Mapping)
        and isinstance(manifest, Mapping)
        and isinstance(errors, list)
        else False
    )
    monitor_exclusions_consistent = monitor_instrument_exclusions_are_consistent(
        payload
    )
    try:
        sector_completion = (
            Decimal(str(audit.get("sector_completion_ratio")))
            if isinstance(audit, Mapping)
            else Decimal("NaN")
        )
        sector_resolution = (
            Decimal(str(audit.get("sector_resolution_ratio")))
            if isinstance(audit, Mapping)
            else Decimal("NaN")
        )
        stock_coverage = (
            Decimal(str(audit.get("coverage_cycle_completion_ratio")))
            if isinstance(audit, Mapping)
            else Decimal("NaN")
        )
    except (InvalidOperation, TypeError, ValueError):
        sector_completion = Decimal("NaN")
        sector_resolution = Decimal("NaN")
        stock_coverage = Decimal("NaN")
    if (
        payload.get("schema") != LIVE_SCREENING_SCHEMA
        or payload.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or payload.get("sector_coverage_contract_id") != SECTOR_COVERAGE_CONTRACT_ID
        or payload.get("available") is not True
        or payload.get("scan_state") != "complete"
        or payload.get("sector_first") is not True
        or payload.get("read_only") is not True
        or payload.get("research_only") is not True
        or payload.get("no_order_execution") is not True
        or review_at.tzinfo is None
        or (
            session is not None
            and review_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != session
        )
        or not isinstance(audit, Mapping)
        or not isinstance(manifest, Mapping)
        or manifest.get("signal_document_contract_id") != SIGNAL_DOCUMENT_CONTRACT_ID
        or not isinstance(raw_signals, list)
        or not isinstance(errors, list)
        or not coverage_manifest_consistent
        or not monitor_exclusions_consistent
        or not isinstance(decision_core, Mapping)
        or not isinstance(decision_core_id, str)
        or not decision_core_id.startswith("sha256:")
        or verified_decision_core_id != decision_core_id
        or decision_core.get("contract_id") != decision_core_id
        or decision_core.get("context_frequency") != "30m"
        or decision_core.get("trade_frequency") != "5m"
        or decision_core.get("segment_difference_frequency") != "1m"
        or decision_core.get("strategic_frequency") != "30m"
        or decision_core.get("tactical_frequency") != "5m"
        or decision_core.get("locator_frequency") != "1m"
        or decision_core.get("human_confirmation_required") is not True
        or decision_core.get("automated_order_authorized") is not False
        or decision_core.get("live_status") != "LIVE_DISABLED"
        # 覆盖周期认证声明的行情截止点，但不能独立证明该截止点对当前决策页面具有因果性。
        # 未来截止点也能以新有效周期和语义哈希重新标识；因此在此绑定两个时钟，使
        # /readyz 与归档共用一个时间边界，而非依赖日级工具稍后的重复校验。
        or market_data_as_of.tzinfo is None
        or market_data_as_of > review_at
        or market_data_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
        != review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        or audit.get("coverage_cycle_complete") is not True
        or not sector_completion.is_finite()
        or not sector_resolution.is_finite()
        # GICS4 + GICS3 会包含不少成员数不足的小板块。经下方强校验认证的
        # “确定性排除”同样属于已解析覆盖，不能再按直接完成率误判为缺失。
        # 对仍有瞬时失败的旧快照则继续保留至少 80% 的直接完成门槛。
        or (sector_completion < Decimal("0.80") and sector_resolution != Decimal("1"))
        or not stock_coverage.is_finite()
        or stock_coverage < MIN_LIVE_REVIEW_STOCK_COVERAGE
        or int(audit.get("pending_symbol_count") or 0) != 0
        or manifest.get("complete") is not True
        or not isinstance(payload.get("snapshot_content_sha256"), str)
        or payload.get("snapshot_content_sha256")
        != live_screening_snapshot_content_sha256(payload)
        or audit.get("full_market_history_scan") is not False
    ):
        raise ValueError("live screening snapshot review boundary is incomplete")

    strength_evidence = _validated_sector_strength_evidence(
        payload,
        review_at=review_at,
    )
    sector_members = _authenticated_sector_members(strength_evidence)
    sector_documents = _validated_sector_documents(
        payload,
        review_at=review_at,
        market_data_as_of=market_data_as_of,
        strength_evidence=strength_evidence,
    )
    discovered_codes = manifest.get("discovered_codes")
    if not isinstance(discovered_codes, list):  # 由覆盖范围解析器保护
        raise ValueError("live screening eligible sector member coverage is invalid")
    required_sector_members = {
        symbol
        for sector_id, document in sector_documents.items()
        if document.get("eligible") is True
        for symbol in sector_members.get(sector_id, frozenset())
    }
    missing_sector_members = required_sector_members.difference(discovered_codes)
    if missing_sector_members:
        # ``universe_revision`` 用于认证服务声明的标的范围。
        # ``universe_revision`` 可认证服务声明的标的池，但不透明哈希无法独立证明声明
        # 包含每个当前成员。已校验横向强度批次携带规范 QMT 成员列表，因此任何信号获准前
        # 都要把已完成个股账本绑定到该证据。显式自选/持仓监听可增加代码，但绝不能让
        # 合格板块成员变成可选项。
        raise ValueError("live screening eligible sector member coverage is incomplete")

    decision_policy = decision_core.get("policy")
    decision_minimum_tick = (
        _decision_decimal(decision_policy.get("minimum_tick"))
        if isinstance(decision_policy, Mapping)
        else None
    )
    if decision_minimum_tick is None or decision_minimum_tick <= 0:
        raise ValueError("live screening decision minimum tick is invalid")

    signals: list[Mapping[str, object]] = []
    diagnostic_memo: dict[tuple[str, str], bool] = {}
    for raw in raw_signals:
        if not isinstance(raw, Mapping):
            raise ValueError("live screening signal is invalid")
        try:
            observed_at = datetime.fromisoformat(str(raw["observed_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("live screening signal time is invalid") from exc
        # 页面统一行情截止点只是外层上限；每个嵌套决策事实还必须在该信号自身生命周期
        # 观测时已经存在，否则旧信号可能静默使用更晚的设置、定位点、日级背景或月/周/日诊断。
        signal_evidence_cutoff = (
            observed_at
            if observed_at.tzinfo is not None and observed_at <= market_data_as_of
            else market_data_as_of
        )
        setup = raw.get("setup_5m")
        if (
            "segment_difference_1m" in raw
            and "trigger_1m" in raw
            and raw.get("segment_difference_1m") != raw.get("trigger_1m")
        ):
            raise ValueError("live screening signal timeframe provenance is invalid")
        trigger = raw.get("segment_difference_1m", raw.get("trigger_1m"))
        daily_context = raw.get("context_d")
        entry_boundary = _entry_boundary_from_document(
            raw.get("entry_execution_boundary")
        )
        context = raw.get("context_30m")
        risk = raw.get("higher_timeframe_risk")
        warmup = raw.get("warmup")
        market_reasons = (
            risk.get("market_reason_codes") if isinstance(risk, Mapping) else None
        )
        symbol_reasons = (
            risk.get("symbol_reason_codes") if isinstance(risk, Mapping) else None
        )
        sector_reasons = (
            risk.get("sector_reason_codes") if isinstance(risk, Mapping) else None
        )
        merged_reasons = risk.get("reason_codes") if isinstance(risk, Mapping) else None
        market_diagnostics = (
            risk.get("market_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        symbol_diagnostics = (
            risk.get("symbol_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        sector_diagnostics = (
            risk.get("sector_period_diagnostics") if isinstance(risk, Mapping) else None
        )
        reason_contract_valid = (
            isinstance(market_reasons, list)
            and all(isinstance(value, str) for value in market_reasons)
            and isinstance(symbol_reasons, list)
            and all(isinstance(value, str) for value in symbol_reasons)
            and isinstance(sector_reasons, list)
            and all(isinstance(value, str) for value in sector_reasons)
            and isinstance(merged_reasons, list)
            and all(isinstance(value, str) for value in merged_reasons)
            and merged_reasons
            == list(dict.fromkeys((*market_reasons, *sector_reasons, *symbol_reasons)))
        )
        diagnostic_contract_valid = (
            isinstance(market_diagnostics, list)
            and all(isinstance(value, Mapping) for value in market_diagnostics)
            and isinstance(symbol_diagnostics, list)
            and all(isinstance(value, Mapping) for value in symbol_diagnostics)
            and isinstance(sector_diagnostics, list)
            and all(isinstance(value, Mapping) for value in sector_diagnostics)
        )
        if not _warmup_evidence_is_consistent(warmup):
            raise ValueError("live screening signal warmup evidence is invalid")
        if not _risk_evidence_is_consistent(
            risk,
            evidence_cutoff=signal_evidence_cutoff,
            expected_symbol=str(raw.get("code")),
            diagnostic_memo=diagnostic_memo,
        ):
            raise ValueError("live screening signal risk evidence is invalid")
        if (
            not isinstance(risk, Mapping)
            or not isinstance(warmup, Mapping)
            or not _entry_gate_is_consistent(raw, risk=risk, warmup=warmup)
        ):
            raise ValueError("live screening signal entry gate is invalid")
        setup_is_causal = _point_document_is_causal(
            setup,
            frequency="5m",
            evidence_cutoff=signal_evidence_cutoff,
        )
        trigger_is_causal = trigger is None or _point_document_is_causal(
            trigger,
            frequency="1m",
            evidence_cutoff=signal_evidence_cutoff,
        )
        trigger_is_execution_point = bool(
            trigger is None
            or isinstance(trigger, Mapping)
            and is_one_minute_segment_difference_document(
                trigger,
                minimum_tick=decision_minimum_tick,
                expected_side=str(raw.get("side")),
            )
        )
        trigger_available_at = None
        if isinstance(trigger, Mapping):
            try:
                trigger_available_at = datetime.fromisoformat(
                    str(trigger["available_at"])
                )
            except (KeyError, TypeError, ValueError):
                trigger_available_at = None
        raw_selection_sources = raw.get("selection_sources")
        selection_sources = (
            tuple(raw_selection_sources)
            if isinstance(raw_selection_sources, list)
            else None
        )
        signal_sector = raw.get("sector")
        sector_document_consistent = False
        if isinstance(signal_sector, Mapping) and selection_sources:
            signal_sector_id = signal_sector.get("sector_id")
            expected_sector = sector_documents.get(str(signal_sector_id))
            if expected_sector is not None:
                expected_signal_sector = dict(expected_sector)
                expected_signal_sector["rank"] = None
                sector_document_consistent = (
                    dict(signal_sector) == expected_signal_sector
                    and (
                        "QMT_SECTOR_TRIGGER" not in selection_sources
                        or (
                            expected_sector.get("eligible") is True
                            and expected_sector.get("regime") == "supportive"
                            and type(expected_sector.get("rank")) is int
                            and isinstance(raw.get("code"), str)
                            and raw.get("code")
                            in sector_members.get(str(signal_sector_id), frozenset())
                        )
                    )
                    and (
                        "QMT_SECTOR_ELIGIBLE_SCOPE" not in selection_sources
                        or (
                            expected_sector.get("eligible") is True
                            and expected_sector.get("regime") != "supportive"
                            and type(expected_sector.get("rank")) is int
                            and isinstance(raw.get("code"), str)
                            and raw.get("code")
                            in sector_members.get(
                                str(signal_sector_id),
                                frozenset(),
                            )
                        )
                    )
                )
            elif raw.get("selection_path") == "ETF_PROXY":
                sector_document_consistent = _etf_proxy_sector_is_consistent(
                    signal_sector,
                    code=raw.get("code"),
                )
            elif "QMT_SECTOR_TRIGGER" not in selection_sources:
                sector_document_consistent = signal_sector_id == "unclassified"
        signal_identity_consistent = False
        if isinstance(setup, Mapping) and isinstance(signal_sector, Mapping):
            setup_point_id = setup.get("point_id")
            sector_id = signal_sector.get("sector_id")
            if isinstance(setup_point_id, str) and isinstance(sector_id, str):
                expected_setup_id = sha256_json(
                    {
                        "schema": "chanlun-trade-setup",
                        "point_id": setup_point_id,
                        "sector_id": sector_id,
                        "sector_required": raw.get("selection_path")
                        == "INDIVIDUAL_THREE_PROGRAM",
                    }
                )
                expected_signal_id = sha256_json(
                    {
                        "schema": "chanlun-signal-lifecycle",
                        "setup_id": expected_setup_id,
                        "side": setup.get("side"),
                    }
                )
                signal_identity_consistent = bool(
                    raw.get("point_id") == setup_point_id
                    and raw.get("point_type") == setup.get("point_type")
                    and raw.get("side") == setup.get("side")
                    and raw.get("tower") == setup.get("tower")
                    and raw.get("recursive_level") == setup.get("recursive_level")
                    and raw.get("setup_id") == expected_setup_id
                    and raw.get("signal_id") == expected_signal_id
                )
        if (
            observed_at.tzinfo is None
            or observed_at > market_data_as_of
            or raw.get("decision_core_id") != decision_core_id
            or raw.get("human_confirmation_required") is not True
            or raw.get("automated_order_authorized") is not False
            or raw.get("live_status") != "LIVE_DISABLED"
            or raw.get("structure_scope") != "physical-timeframe-recursive"
            or raw.get("structure_frequencies") != ["d", "30m", "5m", "1m"]
            or raw.get("stroke_mode") != STRICT_STROKE_MODE
            or raw.get("recursive_structure_used") is not True
            or raw.get("physical_timeframe_recursive") is not True
            or not signal_identity_consistent
            or not isinstance(context, Mapping)
            or not isinstance(setup, Mapping)
            or not _sector_context_is_consistent(
                daily_context,
                frequency="d",
                evidence_cutoff=signal_evidence_cutoff,
            )
            or not isinstance(risk, Mapping)
            or not reason_contract_valid
            or not diagnostic_contract_valid
            or not setup_is_causal
            or (trigger is not None and not isinstance(trigger, Mapping))
            or not trigger_is_causal
            or not trigger_is_execution_point
            or raw.get("lifecycle_stage")
            not in (_LIFECYCLE_STAGES | _SNAPSHOT_AUDIT_STAGES)
            or isinstance(trigger, Mapping)
            and (
                trigger.get("side") != raw.get("side")
                or trigger.get("point_id") == raw.get("point_id")
            )
            or (
                entry_boundary is not None
                and (
                    not isinstance(trigger, Mapping)
                    or entry_boundary.point_id != trigger.get("point_id")
                    or entry_boundary.symbol != raw.get("code")
                    or raw.get("side") != "buy"
                    or trigger_available_at is None
                    or entry_boundary.confirmation_bar_closed_at != trigger_available_at
                    or entry_boundary.confirmation_bar_closed_at
                    > signal_evidence_cutoff
                )
            )
            or selection_sources is None
            or not selection_sources
            or any(
                not isinstance(value, str) or value not in _SELECTION_SOURCES
                for value in selection_sources
            )
            or len(selection_sources) != len(set(selection_sources))
            or not _formal_selection_gate_is_consistent(raw)[0]
            or not sector_document_consistent
        ):
            raise ValueError("live screening signal timeframe provenance is invalid")
        signals.append(raw)
    try:
        # 归档就绪必须覆盖完整只读转换，而不只是外层快照检查；否则非法生命周期、风险闸门、
        # 预热文档或结构价格会让 /readyz 报告就绪，却在日级链构造提醒时才失败。构造器没有
        # 输入输出或下单权限，在此调用可让该校验器的所有消费者共用同一语义边界。
        for signal in signals:
            # Invalidated rows remain in the immutable screening snapshot for
            # lifecycle/audit visibility, but they are no longer actionable
            # review candidates.  Reconstructing a REVIEW_REQUIRED alert from
            # such a terminal row would incorrectly revive a cancelled setup.
            if signal.get("lifecycle_stage") in _SNAPSHOT_AUDIT_STAGES:
                continue
            signal_sector = signal.get("sector")
            sector_id = (
                signal_sector.get("sector_id")
                if isinstance(signal_sector, Mapping)
                else None
            )
            live_signal_human_review_alert(
                signal,
                review_available_at=review_at,
                source_snapshot_sha256=str(payload["snapshot_content_sha256"]),
                sector_ranking_document=sector_documents.get(str(sector_id)),
                sector_ranking_observed_at=datetime.fromisoformat(
                    str(strength_evidence.evidence_document()["decision_time"])
                ),
                sector_strength_evidence_revision=(
                    str(payload["sector_strength_evidence_revision"])
                    if isinstance(
                        payload.get("sector_strength_evidence_revision"),
                        str,
                    )
                    else None
                ),
                sector_catalog_revision=str(manifest["sector_catalog_revision"]),
            )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("live screening signal review conversion is invalid") from exc
    # 只有低层来源与提醒转换契约通过后，才重算主要人工复核字段。这样既保留更精确诊断，
    # 也避免非法生命周期或周期标签仅表现为派生策略不匹配。
    for signal in signals:
        risk = signal.get("higher_timeframe_risk")
        warmup = signal.get("warmup")
        if (
            not isinstance(risk, Mapping)
            or not isinstance(warmup, Mapping)
            or not _displayed_decision_evidence_is_consistent(
                signal,
                policy=decision_core.get("policy"),
                risk=risk,
                warmup=warmup,
            )
        ):
            raise ValueError("live screening signal decision evidence is invalid")
    # 可移植决策身份是逐信号最终防篡改闸门；它有意放在所有更具体、可独立重算的领域
    # 检查之后，使调用方保留可执行诊断。
    for signal in signals:
        try:
            validate_signal_decision_document(signal)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "live screening signal decision identity is invalid"
            ) from exc
    expected_stage_counts: dict[str, int] = {}
    expected_point_counts: dict[str, int] = {}
    for signal in signals:
        stage = str(signal.get("lifecycle_stage"))
        point_type = str(signal.get("point_type"))
        expected_stage_counts[stage] = expected_stage_counts.get(stage, 0) + 1
        expected_point_counts[point_type] = expected_point_counts.get(point_type, 0) + 1

    def normalized_declared_counts(
        raw: object,
        *,
        allowed: frozenset[str],
    ) -> dict[str, int] | None:
        if not isinstance(raw, Mapping):
            return None
        normalized: dict[str, int] = {}
        for key, value in raw.items():
            if (
                not isinstance(key, str)
                or key not in allowed
                or type(value) is not int
                or value < 0
            ):
                return None
            if value:
                normalized[key] = value
        return normalized

    if (
        normalized_declared_counts(
            payload.get("counts_by_stage"),
            allowed=_LIFECYCLE_STAGES | _SNAPSHOT_AUDIT_STAGES,
        )
        != expected_stage_counts
        or normalized_declared_counts(
            payload.get("counts_by_point_type"),
            allowed=CANONICAL_POINT_TYPE_SET,
        )
        != expected_point_counts
    ):
        raise ValueError("live screening signal aggregates are invalid")
    return review_at, tuple(signals)


def _validated_live_review_snapshot(
    payload: Mapping[str, object],
    *,
    session: date | None = None,
) -> _ValidatedLiveReviewSnapshot:
    """Validate once and seal the exact mapping for an immediate report build.

    The token is intentionally process-local and identity-bound.  It is used only
    by the isolated materializer, synchronously, so the expensive 150 MiB semantic
    hash does not run once before and once again inside the report builder.
    """

    review_at, signals = validate_live_review_snapshot(payload, session=session)
    snapshot_content_sha256 = payload.get("snapshot_content_sha256")
    if not isinstance(snapshot_content_sha256, str):
        raise ValueError("live screening snapshot identity is unavailable")
    return _ValidatedLiveReviewSnapshot(
        seal=_LIVE_REVIEW_VALIDATION_SEAL,
        payload=payload,
        snapshot_content_sha256=snapshot_content_sha256,
        review_at=review_at,
        signals=signals,
        session=session,
    )


def _alert_type(signal: Mapping[str, object]) -> str:
    side = signal.get("side")
    # 生产决策合同只把 5 分钟正式点当作买卖信号。旧 30m/战术枚举仅用于读取
    # 历史研究档案，新的实时人工复核记录不得继续写入这些含义冲突的类型。
    if side == "buy":
        return "POSSIBLE_5M_TRADE_BUY"
    if side == "sell":
        return "POSSIBLE_5M_TRADE_SELL"
    raise ValueError("live screening signal side is invalid")


def live_signal_human_review_alert(
    signal: Mapping[str, object],
    *,
    review_available_at: datetime,
    source_snapshot_sha256: str,
    sector_ranking_document: Mapping[str, object] | None = None,
    sector_ranking_observed_at: datetime | None = None,
    sector_strength_evidence_revision: str | None = None,
    sector_catalog_revision: str | None = None,
) -> HumanReviewAlert:
    """转换一个规范的30m环境/5m主信号/1m精确执行决策。"""

    symbol = signal.get("code")
    signal_id = signal.get("signal_id")
    point_id = signal.get("point_id")
    stage = signal.get("lifecycle_stage")
    if (
        not isinstance(symbol, str)
        or not isinstance(signal_id, str)
        or not isinstance(point_id, str)
        or stage not in _LIFECYCLE_STAGES
    ):
        raise ValueError("live screening signal cannot enter human review")
    try:
        signal_at = datetime.fromisoformat(str(signal["observed_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening signal review time is invalid") from exc
    if signal_at.tzinfo is None or signal_at > review_available_at:
        raise ValueError("live screening review would expose a future signal")
    risk = signal.get("higher_timeframe_risk")
    warmup = signal.get("warmup")
    sector = signal.get("sector")
    setup = signal.get("setup_5m")
    trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
    context = signal.get("context_30m")
    entry_boundary = _entry_boundary_from_document(
        signal.get("entry_execution_boundary")
    )
    if not all(
        isinstance(value, Mapping) for value in (risk, warmup, sector, setup, context)
    ) or (trigger is not None and not isinstance(trigger, Mapping)):
        raise ValueError("live screening review evidence is incomplete")
    market_gate = str(risk.get("market_gate") or "UNRESOLVED")
    sector_gate = str(risk.get("sector_gate") or "UNRESOLVED")
    symbol_gate = str(risk.get("symbol_gate") or "UNRESOLVED")
    if (
        market_gate not in _GATES
        or sector_gate not in _GATES
        or symbol_gate not in _GATES
    ):
        raise ValueError("live screening review risk gate is invalid")
    sector_source_evidence = sector_higher_timeframe_review_evidence_from_risk(
        risk,
        sector_id=(
            str(sector["sector_id"])
            if isinstance(sector.get("sector_id"), str)
            else None
        ),
        observed_at=signal_at,
    )
    if sector_ranking_document is not None and sector_ranking_observed_at is None:
        raise ValueError("live sector ranking observation time is unavailable")
    if sector_ranking_document is None:
        sector_ranking_evidence = None
    else:
        assert sector_ranking_observed_at is not None
        sector_ranking_evidence = sector_ranking_review_evidence_from_live_sector(
            sector_ranking_document,
            observed_at=sector_ranking_observed_at,
            strength_evidence_revision=sector_strength_evidence_revision,
            sector_catalog_revision=sector_catalog_revision,
        )
    market_symbol_source_evidence = (
        market_symbol_higher_timeframe_review_evidence_from_risk(
            risk,
            symbol=symbol,
            observed_at=signal_at,
        )
    )
    try:
        # 5 分钟是正式买卖级别，人工复核的结构价格必须锚定正式 5m 点；
        # 必需的 1m 精确执行证据也不得悄悄替换主信号价格。
        reference_price = Decimal(str(setup["anchor_price"]))
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening structure anchor price is invalid") from exc
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("live screening structure anchor price is invalid")
    execution_profile = signal.get("execution_profile")
    recommendation = (
        str(execution_profile.get("recommendation") or "")
        if isinstance(execution_profile, Mapping)
        else ""
    )
    profile_context_grade = (
        str(execution_profile.get("context_grade") or "UNRESOLVED")
        if isinstance(execution_profile, Mapping)
        else "UNRESOLVED"
    )
    allowed = bool(
        recommendation == "READY"
        or not recommendation
        and (signal.get("entry_allowed") or signal.get("exit_allowed"))
    )
    confidence = (
        "HIGH"
        if allowed
        else "LOW"
        if profile_context_grade in {"C", "UNRESOLVED"}
        else "MEDIUM"
        if recommendation == "CAUTION"
        or stage in {"observed", "triggered", "executable"}
        else "LOW"
        if stage in {"formed", "armed"}
        else "UNRESOLVED"
    )
    alert_type = _alert_type(signal)
    if entry_boundary is not None and (
        not isinstance(trigger, Mapping)
        or entry_boundary.point_id != trigger.get("point_id")
        or entry_boundary.symbol != symbol
        or signal.get("side") != "buy"
    ):
        raise ValueError("live screening entry boundary does not match its signal")
    selection_sources = tuple(
        str(value) for value in signal.get("selection_sources") or ()
    )
    warnings = tuple(
        dict.fromkeys(
            (
                "STAGED_LIVE_SCREEN_REQUIRES_HUMAN_CONFIRMATION",
                "CONTEXT_30M",
                "TRADE_SIGNAL_5M",
                (
                    "SEGMENT_DIFFERENCE_1M_PRESENT"
                    if trigger is not None
                    else "SEGMENT_DIFFERENCE_1M_NOT_PRESENT_OPTIONAL"
                ),
                f"LIFECYCLE_{str(stage).upper()}",
                *((f"EXECUTION_RECOMMENDATION_{recommendation}",) if recommendation else ()),
                *(
                    (
                        "UNADJUSTED_1M_CONFIRMATION_BAR_BOUNDARY_PRESENT",
                        (
                            "BUY_ENTRY_BOUNDARY_ALREADY_EXPIRED"
                            if entry_boundary.entry_valid_until <= review_available_at
                            else "BUY_ENTRY_BOUNDARY_ACTIVE"
                        ),
                    )
                    if signal.get("side") == "buy" and entry_boundary is not None
                    else (
                        ("BUY_EXECUTION_BOUNDARY_MISSING_REVIEW_ONLY",)
                        if signal.get("side") == "buy"
                        else ()
                    )
                ),
                *(f"SELECTION_SOURCE_{value}" for value in selection_sources),
                *((MONITOR_ONLY_WARNING_CODE,) if signal.get("monitor_only") is True else ()),
                *(str(value) for value in signal.get("decision_reasons") or ()),
                *(str(value) for value in risk.get("reason_codes") or ()),
                *(str(value) for value in warmup.get("reason_codes") or ()),
                *(
                    ()
                    if five_minute_warmup_converged(warmup) is True
                    else ("WARMUP_NOT_CONVERGED",)
                ),
            )
        )
    )
    structure_snapshot_id = sha256_json(
        {
            "schema": "chanlun-live-human-review-structure",
            "source_snapshot_sha256": source_snapshot_sha256,
            "context_30m": dict(context),
            "trade_signal_5m": dict(setup),
            "segment_difference_1m": None if trigger is None else dict(trigger),
            "signal": dict(signal),
        }
    )
    facts = tuple(
        dict.fromkeys(
            str(value)
            for value in (
                point_id,
                signal.get("setup_id"),
                signal_id,
                signal.get("decision_core_id"),
                structure_snapshot_id,
                source_snapshot_sha256,
                (
                    None
                    if sector_source_evidence is None
                    else sector_source_evidence.evidence_id
                ),
                market_symbol_source_evidence.evidence_id,
                (
                    None
                    if sector_ranking_evidence is None
                    else sector_ranking_evidence.evidence_id
                ),
                None if entry_boundary is None else entry_boundary.evidence_id,
            )
            if isinstance(value, str) and value
        )
    )
    invalidation = signal.get("structural_stop")
    parameters = human_review_screening_parameters()
    position_recommendation = parse_position_recommendation_document(
        signal.get("position_recommendation")
    )
    return HumanReviewAlert(
        symbol=symbol,
        alert_type=alert_type,  # type: ignore[arg-type]
        signal_at=signal_at,
        review_available_at=review_available_at,
        source_point_id=point_id,
        structure_snapshot_id=structure_snapshot_id,
        sector_id=(
            str(sector["sector_id"])
            if isinstance(sector.get("sector_id"), str)
            else None
        ),
        confidence=confidence,  # type: ignore[arg-type]
        review_priority=review_priority(
            confidence=confidence,  # type: ignore[arg-type]
            exact_green=allowed,
            market_risk_gate=market_gate,
            sector_risk_gate=sector_gate,
            symbol_risk_gate=symbol_gate,
            warning_count=len(warnings),
            position_status=position_recommendation.status,
            side=str(signal.get("side")),
            selection_sources=selection_sources,
            lifecycle_stage=str(stage),
            monitor_only=signal.get("monitor_only") is True,
            parameters=parameters,
        ),
        # 这是 5 分钟正式买卖点的因果结构锚点，不是行情报价或成交承诺。
        reference_price=reference_price,
        structural_invalidation_price=(
            None if invalidation is None else Decimal(str(invalidation))
        ),
        market_risk_gate=market_gate,
        sector_risk_gate=sector_gate,
        symbol_risk_gate=symbol_gate,
        warning_codes=warnings,
        source_fact_ids=facts,
        screening_parameter_set_id=parameters.parameter_set_id,
        signal_alignment_parameter_set_id=parameters.signal_alignment_parameter_set_id,
        sector_higher_timeframe_evidence=sector_source_evidence,
        market_symbol_higher_timeframe_evidence=(market_symbol_source_evidence),
        sector_ranking_evidence=sector_ranking_evidence,
        entry_confirmation_bar_closed_at=(
            None
            if entry_boundary is None
            else entry_boundary.confirmation_bar_closed_at
        ),
        entry_price_cap=(None if entry_boundary is None else entry_boundary.raw_high),
        entry_valid_until=(
            None if entry_boundary is None else entry_boundary.entry_valid_until
        ),
        entry_boundary_evidence_id=(
            None if entry_boundary is None else entry_boundary.evidence_id
        ),
        entry_execution_boundary=entry_boundary,
        position_recommendation=position_recommendation,
    )


def live_human_review_document(
    *,
    live_snapshot: Mapping[str, object],
    source_snapshot_sha256: str,
    session: date,
    result_label: str = "LIVE_INTRADAY_HUMAN_REVIEW_QUEUE",
    decision_source_snapshot: Mapping[str, object] | None = None,
    _validated_snapshot: _ValidatedLiveReviewSnapshot | None = None,
) -> dict[str, object]:
    """构建适合不可变实时归档、仅供复核的报告。"""

    source_implementation_id = (
        None
        if decision_source_snapshot is None
        else decision_source_snapshot_id(decision_source_snapshot)
    )
    if _validated_snapshot is None:
        review_at, signals = validate_live_review_snapshot(
            live_snapshot,
            session=session,
        )
    else:
        validation = _validated_snapshot
        if (
            type(validation) is not _ValidatedLiveReviewSnapshot
            or validation.seal is not _LIVE_REVIEW_VALIDATION_SEAL
            or validation.payload is not live_snapshot
            or validation.snapshot_content_sha256 != source_snapshot_sha256
            or live_snapshot.get("snapshot_content_sha256")
            != validation.snapshot_content_sha256
            or (validation.session is not None and validation.session != session)
            or validation.review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            != session
        ):
            raise ValueError("live screening validation token is invalid")
        review_at, signals = validation.review_at, validation.signals
    ranking_documents = {
        str(value["sector_id"]): value
        for value in live_snapshot.get("sectors") or ()
        if isinstance(value, Mapping) and isinstance(value.get("sector_id"), str)
    }
    strength_document = live_snapshot.get("sector_strength_evidence")
    if not isinstance(strength_document, Mapping):
        raise ValueError("live screening sector strength evidence is unavailable")
    ranking_observed_at = datetime.fromisoformat(
        str(strength_document["decision_time"])
    )
    alerts = tuple(
        sorted(
            (
                live_signal_human_review_alert(
                    signal,
                    review_available_at=review_at,
                    source_snapshot_sha256=source_snapshot_sha256,
                    sector_ranking_document=ranking_documents.get(
                        str(
                            signal.get("sector", {}).get("sector_id")
                            if isinstance(signal.get("sector"), Mapping)
                            else ""
                        )
                    ),
                    sector_ranking_observed_at=ranking_observed_at,
                    sector_strength_evidence_revision=(
                        str(live_snapshot["sector_strength_evidence_revision"])
                        if isinstance(
                            live_snapshot.get("sector_strength_evidence_revision"),
                            str,
                        )
                        else None
                    ),
                    sector_catalog_revision=str(
                        live_snapshot["coverage_manifest"]["sector_catalog_revision"]
                    ),
                )
                for signal in signals
                if signal.get("lifecycle_stage") not in _SNAPSHOT_AUDIT_STAGES
            ),
            key=lambda value: (
                -value.review_priority,
                -int(value.review_available_at.timestamp()),
                value.symbol,
                value.candidate_id,
            ),
        )
    )
    parameters = human_review_screening_parameters()
    stable: dict[str, object] = {
        "schema": HUMAN_REVIEW_SCREEN_SCHEMA,
        "forward_paper_session": session.isoformat(),
        "result_label": result_label,
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "live_status": "LIVE_DISABLED",
        "sample": {
            "forward_session": session.isoformat(),
            "market_data_as_of": review_at.isoformat(),
            "minimum_bar_period": "1m",
        },
        "scope": {
            "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
            "three_program_mode": "DISABLED_USER_AUTHORIZED",
            "context_frequency": "30m",
            "trade_frequency": "5m",
            "segment_difference_frequency": "1m",
            "segment_difference_required_for_trade_signal": False,
            "segment_difference_required_for_precise_execution": True,
            # Compatibility aliases retained for previously archived reports.
            "strategic_frequency": "30m",
            "tactical_frequency": "5m",
            "locator_frequency": "1m",
            "candidate_symbol_count": len({value.symbol for value in alerts}),
        },
        "screening_contract": _jsonable(parameters.document()),
        **(
            {}
            if decision_source_snapshot is None
            else {"decision_source_snapshot": _jsonable(decision_source_snapshot)}
        ),
        "input_hashes": {
            "live_screening_snapshot": source_snapshot_sha256,
            "declared_snapshot_content_sha256": live_snapshot.get(
                "snapshot_content_sha256"
            ),
            "decision_core_id": live_snapshot.get("decision_core_id"),
            "screening_policy_id": _source_screening_policy_id(live_snapshot),
            **(
                {}
                if source_implementation_id is None
                else {"decision_source_snapshot_id": source_implementation_id}
            ),
        },
        "candidate_funnel": {
            "live_screen_candidate_count": len(signals),
            "review_candidate_count": len(alerts),
        },
        "signal_counts": {
            "by_stage": dict(live_snapshot.get("counts_by_stage") or {}),
            "by_point_type": dict(live_snapshot.get("counts_by_point_type") or {}),
            "by_alert_type": {
                alert_type: sum(value.alert_type == alert_type for value in alerts)
                for alert_type in HUMAN_REVIEW_ALERT_TYPES
            },
        },
        "review_queue": [
            {
                **_jsonable(human_review_alert_document(alert)),
                "candidate_id": alert.candidate_id,
                "signal_lifecycle_id": alert.signal_lifecycle_id,
            }
            for alert in alerts
        ],
        "event_study": {"summary": {}, "observations": []},
        "data_caveats": [
            "CURRENT_QMT_MEMBERSHIP_HAS_FORWARD_POINT_IN_TIME_MEANING_ONLY",
            "PROGRAM_OUTPUT_IS_A_REVIEW_CANDIDATE_NOT_A_TRADE_SIGNAL",
            "REFERENCE_PRICE_IS_STRUCTURE_ANCHOR_NOT_EXECUTION_QUOTE",
            "NO_TICK_DATA_AND_NO_PORTFOLIO_PERFORMANCE",
        ],
        "division_of_responsibility": {
            "program": (
                "sector ranking, 30m context, 5m formal trade point, optional "
                "1m segment difference, causal chart "
                "lock, risk and warmup evidence"
            ),
            "human": (
                "center, trend type, recursive level, position ownership, "
                "recommended ratio and any manual trade decision"
            ),
        },
        "hard_rejections": list(live_snapshot.get("errors") or ()),
        "candidate_audit": [
            {
                "signal_id": signal.get("signal_id"),
                "symbol": signal.get("code"),
                "context_frequency": "30m",
                "trade_frequency": "5m",
                "segment_difference_frequency": (
                    "1m"
                    if signal.get(
                        "segment_difference_1m",
                        signal.get("trigger_1m"),
                    )
                    is not None
                    else None
                ),
                "position_recommendation": signal.get("position_recommendation"),
                "decision_reasons": list(signal.get("decision_reasons") or ()),
                "entry_allowed": bool(signal.get("entry_allowed")),
                "exit_allowed": bool(signal.get("exit_allowed")),
                "exit_action": signal.get("exit_action"),
            }
            for signal in signals
        ],
    }
    portable = _jsonable(stable)
    if not isinstance(portable, dict):
        raise TypeError("live human review document must be a mapping")
    return {**portable, "content_sha256": sha256_json(portable)}


__all__ = (
    "COVERAGE_EXCLUSION_ELIGIBILITY_BY_REASON",
    "COVERAGE_EXCLUSION_REASON_CODES",
    "COVERAGE_MANIFEST_SCHEMA",
    "COVERAGE_MANIFEST_FIELDS",
    "COVERAGE_STATE_CONTRACT_ID",
    "HUMAN_REVIEW_SCREEN_SCHEMA",
    "LIVE_SCREENING_SCHEMA",
    "MIN_LIVE_REVIEW_STOCK_COVERAGE",
    "MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID",
    "SECTOR_COVERAGE_CONTRACT_ID",
    "live_screening_semantic_snapshot_document",
    "live_screening_snapshot_content_sha256",
    "coverage_manifest_dispositions_are_consistent",
    "monitor_instrument_exclusions_are_consistent",
    "live_human_review_document",
    "live_signal_human_review_alert",
    "screening_coverage_epoch_id",
    "validate_live_screening_market_watermark",
    "validate_live_review_snapshot",
)
