"""实时选股页面的有界展示投影。

本模块只裁剪已经形成的审计信号，不参与信号判断、生命周期或执行许可。把它与决策
来源快照隔离后，纯页面字段调整不会让全市场决策缓存失效。
"""

from __future__ import annotations

from collections.abc import Mapping
import copy

from chanlun.decision_support.trading_system.five_minute_setup_state import (
    canonical_setup_state_document,
)
from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)


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
    "current_price",
    "chart_urls",
    "side",
    "point_type",
    "lifecycle_stage",
    "observed_at",
    "observation_lane",
    "monitor_observed_at",
    "realtime_observation",
    "structural_stop",
    "risk_multiplier",
    "position_recommendation",
    "execution_profile",
    "technical_entry_allowed",
    "selection_sources",
    "selection_path",
    "formal_selection_required",
    "sector_triggered",
    "monitor_only",
    "entry_allowed",
    "exit_allowed",
    "human_confirmation_required",
    "automated_order_authorized",
    "decision_reasons",
)
_PRESENTATION_SIGNAL_SECTOR_FIELDS = (
    "sector_id",
    "sector_name",
    "eligible",
    "hard_block",
    "reason_codes",
)
_PRESENTATION_CONTEXT_FIELDS = (
    "direction",
    "disposition",
    "dominant_point_type",
    "hard_block",
    "reason_codes",
)
_PRESENTATION_SETUP_FIELDS = (
    "state_contract",
    "status",
    "formation_state",
    "lock_state",
    "point_type",
    "center_ordinal",
    "contains_forming_segment",
    "contains_unlocked_segment",
    "contains_unfinished_segment",
    "actionable",
    "invalidation_price",
    "evidence_codes",
    "missing_conditions",
    "terminal_segment_role",
    "terminal_segment_level",
    "terminal_segment_id",
    "terminal_segment_source_kind",
    "terminal_segment_direction",
    "terminal_segment_state",
    "terminal_segment_start_at",
    "terminal_segment_end_at",
    "terminal_segment_available_at",
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


def presentation_signal_document(
    signal: Mapping[str, object],
) -> dict[str, object]:
    """构建单个审计信号的有界实时页面投影。"""

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
    raw_setup = signal.get("setup_5m")
    setup = (
        canonical_setup_state_document(raw_setup)
        if isinstance(raw_setup, Mapping)
        and raw_setup.get("status") in {"confirmed", "provisional"}
        else raw_setup
    )
    document["setup_5m"] = _presentation_fields(setup, _PRESENTATION_SETUP_FIELDS) or {}
    effective_stage = lifecycle_stage_from_signal(signal)
    if effective_stage is not None:
        document["lifecycle_stage"] = effective_stage
    raw_trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
    document["segment_difference_1m"] = (
        None
        if raw_trigger is None
        else _presentation_fields(raw_trigger, _PRESENTATION_TRIGGER_FIELDS) or {}
    )
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


__all__ = ("presentation_signal_document",)
