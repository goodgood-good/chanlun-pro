"""只读买卖点生命周期通知。

通知负责报告已经确认的结构事实，不把“买卖点已出现”偷换成“已经具备下单
资格”。三程序、月周日风险门、板块门和执行边界仍决定正式准入状态，并在
通知正文中明确披露。

正文固定采用结论、标的、判断、执行、失效、风险、时效、背景的决策优先顺序；
同一价格或结构证据只在最接近其用途的位置披露，避免重复信息淹没操作边界。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import logging
from math import isfinite
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.lifecycle import (
    is_one_minute_segment_difference_document,
    lifecycle_stage_from_signal,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    validate_signal_decision_document,
)
from chanlun.decision_support.trading_system.models import (
    POINT_REVIEW_ORDER,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_BASIS,
    ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_REASON,
    build_position_recommendation,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION,
    WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    five_minute_warmup_converged,
)

from .realtime_review_inbox import (
    a_share_notification_event,
    segment_difference_boundary_status,
)


SCHEMA = "chanlun-signal-notifications"
STRATEGY_ID = STRICT_STRATEGY_ID
CN = ZoneInfo("Asia/Shanghai")
_NOTIFIABLE_TRANSITIONS = {
    (None, "triggered"),
    (None, "executable"),
    ("observed", "triggered"),
    ("approaching", "triggered"),
    ("formed", "triggered"),
    ("armed", "triggered"),
    ("triggered", "executable"),
    ("armed", "invalidated"),
    ("triggered", "invalidated"),
    ("executable", "invalidated"),
    ("active", "closed"),
}
_NOTIFICATION_RETRY_TTL = timedelta(minutes=10)
_APPROACHING_DIGEST_COOLDOWN = timedelta(minutes=15)
_APPROACHING_DIGEST_MAX_ITEMS = 8
_APPROACHING_OCCURRENCE_LIMIT = 16_384
_PRECONFIRMATION_DIVERGENCE_DIGEST_MAX_ITEMS = 8
_PRECONFIRMATION_DIVERGENCE_OCCURRENCE_LIMIT = 16_384
_PRECONFIRMATION_DIVERGENCE_FIELD = "preconfirmation_divergences_1m"
_NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD = (
    "notification_preconfirmation_divergence_1m"
)
_SEGMENT_ENRICHED_STAGE = "segment_enriched"
_SEGMENT_ATTACHABLE_STAGES = frozenset({"triggered", "executable", "active"})
_EVIDENCE_NOTIFICATION_STAGES = frozenset(
    {"triggered", "executable", _SEGMENT_ENRICHED_STAGE}
)
# Confirmed occurrences remain pending until transport acknowledgement.  Their
# original detection time is retained so a delayed delivery is never presented
# as a newly formed signal.
_PENDING_TRIGGER_MAX_AGE = timedelta.max
_AUDIT_RECORD_LIMIT = 500
_SUPPRESSED_FINGERPRINT_LIMIT = 16_384
_SCREENING_COMPLETION_RECORD_LIMIT = 400
_SCREENING_COMPLETION_CLOSE_HOUR = 15
_SCREENING_COMPLETION_EVENT_SCHEMA = "chanlun-daily-screening-completion-v1"
_STAGE_LABELS = {
    "observed": "结构观察",
    "approaching": "即将确认",
    "formed": "5分钟几何候选待锁定确认",
    "armed": "等待操作确认",
    "triggered": "5分钟操作确认",
    "executable": "强提示待人工复核",
    "active": "结构持续跟踪",
    "invalidated": "结构已失效",
    "closed": "跟踪已结束",
    _SEGMENT_ENRICHED_STAGE: "1分钟区间套定位补充",
}
_POINT_LABELS = {
    "1buy": "一类买点",
    "2buy": "二类买点",
    "3buy": "三类买点",
    "1sell": "一类卖点",
    "2sell": "二类卖点",
    "3sell": "三类卖点",
}
_DIRECTION_LABELS = {
    "up": "向上",
    "down": "向下",
    "neutral": "震荡待定",
}
_DIVERGENCE_LABELS = {
    "trend": "趋势背驰",
    "consolidation": "盘整背驰",
}
_DISPOSITION_LABELS = {
    "supportive": "有利",
    "neutral": "中性",
    "hostile": "不利",
}
_MANUAL_ATTENTION_SOURCES = frozenset(
    {
        "MANUAL_ATTENTION_MONITOR",
        # 旧通知/归档兼容；当前生产文档只生成上面的人工关注来源。
        "HOLDING_MONITOR",
        "VIRTUAL_HOLDING_MONITOR",
    }
)
_SETUP_POINT_ORDER = {
    point_type: index for index, point_type in enumerate(POINT_REVIEW_ORDER)
}


def _signals_by_id(snapshot: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = snapshot.get("signals", ())
    if not isinstance(rows, (list, tuple)):
        return {}
    output: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        signal_id = row.get("signal_id")
        if isinstance(signal_id, str) and signal_id:
            output[signal_id] = row
    return output


def _stage(signal: Mapping[str, object] | None) -> str | None:
    if signal is None:
        return None
    return lifecycle_stage_from_signal(signal)


def _recorded_segment(document: Mapping[str, object] | None) -> Mapping[str, object]:
    if not isinstance(document, Mapping):
        return {}
    raw = document.get("segment_difference_1m")
    return raw if isinstance(raw, Mapping) else {}


def _new_segment_attached(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> bool:
    """Return true for a new causal 1m occurrence on an existing 5m setup.

    The first attachment and every later, semantically distinct re-armed
    occurrence are notification events.  Rebuilt internal IDs for the same
    completed 1m bar remain deduplicated by the occurrence key.
    """

    if not isinstance(previous, Mapping):
        return False
    current_segment = _recorded_segment(current)
    if not current_segment:
        return False
    previous_stage = _stage(previous)
    current_stage = _stage(current)
    previous_occurrence = _segment_occurrence_key(
        previous,
        _SEGMENT_ENRICHED_STAGE,
    )
    current_occurrence = _segment_occurrence_key(
        current,
        _SEGMENT_ENRICHED_STAGE,
    )
    return bool(
        previous_stage in _SEGMENT_ATTACHABLE_STAGES
        and current_stage in _SEGMENT_ATTACHABLE_STAGES
        and _signal_semantic_key(previous) == _signal_semantic_key(current)
        and current_occurrence is not None
        and current_occurrence != previous_occurrence
    )


def _stage_label(stage: str | None) -> str:
    """只在展示边界翻译稳定的生命周期枚举。"""

    if stage is None or stage == "None":
        return "首次发现"
    return _STAGE_LABELS.get(stage, "未知状态")


def notification_event_id(signal_id: str, old_stage: str, new_stage: str) -> str:
    payload = json.dumps(
        {
            "schema": SCHEMA,
            "signal_id": signal_id,
            "old_stage": old_stage,
            "new_stage": new_stage,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _defense_price_value(
    setup: Mapping[str, object],
) -> object:
    return setup.get("invalidation_price")


def _trigger_occurrence_key(
    signal: Mapping[str, object],
    new_stage: str,
) -> tuple[str, ...] | None:
    """以 5 分钟正式点识别一次买卖信号，不依赖可选 1 分钟区间套定位。"""

    if new_stage not in {"triggered", "executable"}:
        return None
    setup = _mapping(signal.get("setup_5m"))
    point_type = str(setup.get("point_type") or signal.get("point_type") or "")
    source_frequency = str(setup.get("source_frequency") or "")
    recursive_level = setup.get("recursive_level", signal.get("recursive_level"))
    anchor_at = _time_identity(setup.get("anchor_at"))
    available_at = _time_identity(
        setup.get("available_at") or setup.get("confirmed_at")
    )
    if (
        not point_type
        or not source_frequency
        or isinstance(recursive_level, bool)
        or not isinstance(recursive_level, int)
        or not anchor_at
        or not available_at
    ):
        return None
    if not is_five_minute_trade_level(source_frequency, recursive_level):
        return None
    side = str(signal.get("side") or setup.get("side") or "")
    return (
        str(signal.get("code") or ""),
        side,
        source_frequency,
        str(recursive_level),
        point_type,
        anchor_at,
        available_at,
    )


def _trigger_occurrence_event_id(key: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-notification-trigger-occurrence",
            "strategy_id": STRATEGY_ID,
            "key": key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _approaching_occurrence_key(
    signal: Mapping[str, object],
) -> tuple[str, ...] | None:
    """Identify one forming 5m structure without using its rolling bar time.

    A provisional point is rebuilt on every completed 5-minute bar.  Its
    ``point_id`` and ``available_at`` therefore change even while the same
    unfinished terminal segment is being observed.  The terminal-segment
    identity is the durable user-facing occurrence and prevents a reminder on
    every bar.
    """

    if _stage(signal) != "approaching":
        return None
    setup = _mapping(signal.get("setup_5m"))
    point_type = str(setup.get("point_type") or signal.get("point_type") or "")
    side = str(signal.get("side") or setup.get("side") or "")
    source_frequency = str(setup.get("source_frequency") or "")
    recursive_level = setup.get("recursive_level", signal.get("recursive_level"))
    terminal_segment_id = str(setup.get("terminal_segment_id") or "").strip()
    terminal_segment_start_at = _time_identity(
        setup.get("terminal_segment_start_at")
    )
    terminal_segment_end_at = _time_identity(setup.get("terminal_segment_end_at"))
    if (
        not str(signal.get("code") or "").strip()
        or side not in {"buy", "sell"}
        or not point_type
        or source_frequency != "5m"
        or isinstance(recursive_level, bool)
        or not isinstance(recursive_level, int)
        or not is_five_minute_trade_level(source_frequency, recursive_level)
        or not terminal_segment_id
        or not terminal_segment_start_at
        or not terminal_segment_end_at
    ):
        return None
    return (
        str(signal.get("code")),
        side,
        source_frequency,
        str(recursive_level),
        point_type,
        terminal_segment_id,
        terminal_segment_start_at,
        terminal_segment_end_at,
    )


def _approaching_occurrence_event_id(key: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-approaching-occurrence",
            "strategy_id": STRATEGY_ID,
            "key": key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _approaching_digest_event_id(
    occurrence_ids: Sequence[str],
    *,
    created_at: datetime,
) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-approaching-digest",
            "strategy_id": STRATEGY_ID,
            "created_at": created_at.isoformat(timespec="minutes"),
            "occurrence_ids": sorted(occurrence_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _preconfirmation_divergence_rows(
    signal: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    raw = signal.get(_PRECONFIRMATION_DIVERGENCE_FIELD)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(value for value in raw if isinstance(value, Mapping))


def _preconfirmation_divergence_occurrence_key(
    signal: Mapping[str, object],
    divergence: Mapping[str, object],
) -> tuple[str, ...] | None:
    setup = _mapping(signal.get("setup_5m"))
    side = str(signal.get("side") or setup.get("side") or "")
    setup_segment_id = str(setup.get("terminal_segment_id") or "").strip()
    setup_segment_start = _time_identity(setup.get("terminal_segment_start_at"))
    divergence_level = divergence.get("recursive_level")
    divergence_segment_id = str(
        divergence.get("terminal_segment_id") or ""
    ).strip()
    divergence_segment_start = _time_identity(
        divergence.get("terminal_segment_start_at")
    )
    divergence_segment_end = _time_identity(
        divergence.get("terminal_segment_end_at")
    )
    divergence_anchor = _time_identity(divergence.get("anchor_at"))
    divergence_available = _time_identity(
        divergence.get("available_at") or divergence.get("confirmed_at")
    )
    if (
        _stage(signal) not in {"approaching", "formed"}
        or not str(signal.get("code") or "").strip()
        or side not in {"buy", "sell"}
        or not setup_segment_id
        or not setup_segment_start
        or divergence.get("source_frequency") != "1m"
        or isinstance(divergence_level, bool)
        or not isinstance(divergence_level, int)
        or divergence_level < 0
        or not divergence_segment_id
        or not divergence_segment_start
        or not divergence_segment_end
        or not divergence_anchor
        or not divergence_available
    ):
        return None
    return (
        str(signal.get("code")),
        side,
        str(setup.get("price_basis_revision") or ""),
        setup_segment_id,
        setup_segment_start,
        str(divergence.get("point_type") or ""),
        str(divergence.get("divergence_kind") or ""),
        str(divergence_level),
        divergence_segment_id,
        divergence_segment_start,
        divergence_segment_end,
        divergence_anchor,
        divergence_available,
    )


def _preconfirmation_divergence_occurrence_event_id(
    key: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-preconfirmation-divergence-occurrence",
            "strategy_id": STRATEGY_ID,
            "key": key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _preconfirmation_divergence_digest_event_id(
    occurrence_ids: Sequence[str],
    *,
    created_at: datetime,
) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-preconfirmation-divergence-digest",
            "strategy_id": STRATEGY_ID,
            "created_at": created_at.isoformat(timespec="minutes"),
            "occurrence_ids": sorted(occurrence_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _segment_occurrence_key(
    signal: Mapping[str, object],
    new_stage: str,
) -> tuple[str, ...] | None:
    """Identify a newly attached 1m fact independently of internal IDs."""

    if new_stage != _SEGMENT_ENRICHED_STAGE:
        return None
    setup_key = _signal_semantic_key(signal)
    trigger = _mapping(signal.get("segment_difference_1m"))
    trigger_type = str(trigger.get("point_type") or "")
    trigger_side = str(trigger.get("side") or "")
    trigger_frequency = str(trigger.get("source_frequency") or "")
    trigger_level = trigger.get("recursive_level")
    trigger_anchor = _time_identity(trigger.get("anchor_at"))
    trigger_available = _time_identity(
        trigger.get("available_at") or trigger.get("confirmed_at")
    )
    if (
        any(not value for value in setup_key)
        or trigger_type == ""
        or trigger_side not in {"buy", "sell"}
        or trigger_frequency != "1m"
        or isinstance(trigger_level, bool)
        or not isinstance(trigger_level, int)
        or trigger_level < 0
        or not trigger_anchor
        or not trigger_available
    ):
        return None
    return (
        *setup_key,
        trigger_side,
        trigger_frequency,
        str(trigger_level),
        trigger_type,
        trigger_anchor,
        trigger_available,
    )


def _segment_occurrence_event_id(key: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-notification-segment-occurrence",
            "strategy_id": STRATEGY_ID,
            "key": key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CN)


def _time_identity(value: object) -> str:
    """Canonicalize equivalent timezone spellings for durable identities."""

    parsed = _parse_time(value)
    return (
        parsed.isoformat(timespec="seconds")
        if parsed is not None
        else str(value or "").strip()
    )


def _signal_time(signal: Mapping[str, object]) -> datetime | None:
    setup = _mapping(signal.get("setup_5m"))
    return next(
        (
            parsed
            for key in ("available_at", "confirmed_at")
            if (parsed := _parse_time(setup.get(key))) is not None
        ),
        None,
    )


def _segment_time(signal: Mapping[str, object]) -> datetime | None:
    trigger = _mapping(signal.get("segment_difference_1m"))
    return next(
        (
            parsed
            for key in ("available_at", "confirmed_at")
            if (parsed := _parse_time(trigger.get(key))) is not None
        ),
        None,
    )


def _notification_evidence_time(
    signal: Mapping[str, object],
    *,
    new_stage: str,
) -> datetime | None:
    """Return when every fact required by this notification was observable.

    A 1m locator can form inside the terminal 5m segment before the 5m point is
    formally available.  In that case the enrichment/confluence is new at the
    5m availability time, not stale from the earlier 1m timestamp.  When the
    1m locator arrives later, its own availability remains the event time.
    """

    signal_at = _signal_time(signal)
    preconfirmation_divergence = _mapping(
        signal.get(_NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD)
    )
    if preconfirmation_divergence:
        divergence_at = next(
            (
                parsed
                for key in ("available_at", "confirmed_at")
                if (
                    parsed := _parse_time(preconfirmation_divergence.get(key))
                )
                is not None
            ),
            None,
        )
        if signal_at is None:
            return divergence_at
        if divergence_at is None:
            return signal_at
        return max(signal_at, divergence_at)
    if new_stage != _SEGMENT_ENRICHED_STAGE:
        return signal_at
    segment_at = _segment_time(signal)
    if signal_at is None:
        return segment_at
    if segment_at is None:
        return signal_at
    return max(signal_at, segment_at)


def _notification_expires_at(
    signal: Mapping[str, object],
    *,
    new_stage: str,
    detected_at: object | None = None,
) -> str | None:
    """Return the bounded retry deadline for external message transport.

    The deadline starts when the monitor discovers the event. It is deliberately
    independent from the 5m setup time and never changes structural validity.
    """

    if new_stage not in _EVIDENCE_NOTIFICATION_STAGES:
        return None
    event_at = _notification_evidence_time(
        signal,
        new_stage=new_stage,
    )
    if event_at is None:
        return None
    discovered = _parse_time(detected_at)
    retry_started_at = event_at if discovered is None else discovered
    expires_at = retry_started_at + _NOTIFICATION_RETRY_TTL
    return expires_at.isoformat()


def _notification_eligibility_reason(
    signal: Mapping[str, object],
    *,
    old_stage: str | None,
    new_stage: str,
    require_decision_identity: bool = False,
    evaluated_at: object | None = None,
) -> str | None:
    """只允许当前、可验证的 5 分钟正式买卖点进入通知通道。

    ``entry_allowed`` 和 ``exit_allowed`` 是正式操作资格，不是买卖点是否
    存在的判据。风险门、三程序或持仓身份未通过时，结构点仍需通知，但正文
    必须标明仅供观察；结构权威、预热收敛和时效性仍然失败关闭。1 分钟
    缺失时不能压掉 5 分钟通知，但必须保持“等待区间套”、不得生成执行比例。
    """

    if new_stage in {"invalidated", "closed"}:
        return None
    if new_stage not in _EVIDENCE_NOTIFICATION_STAGES:
        return "UNSUPPORTED_NOTIFICATION_STAGE"

    side = str(signal.get("side") or "")
    if side not in {"buy", "sell"}:
        return "SIGNAL_SIDE_INVALID"
    carries_decision_identity = bool(
        signal.get("decision_document_schema") or signal.get("decision_document_id")
    )
    if require_decision_identity or carries_decision_identity:
        try:
            validate_signal_decision_document(signal)
        except (TypeError, ValueError):
            return "SIGNAL_DECISION_DOCUMENT_INVALID"

    setup = _mapping(signal.get("setup_5m"))
    if (
        setup.get("status") != "confirmed"
        or setup.get("source_frequency") != "5m"
        or setup.get("side") != side
        or setup.get("actionable") is not True
        or setup.get("point_id") != signal.get("point_id")
    ):
        return "FIVE_MINUTE_STRUCTURE_NOT_CONFIRMED"
    recursive_level = setup.get("recursive_level")
    if type(recursive_level) is not int or not is_five_minute_trade_level(
        "5m", recursive_level
    ):
        return "FIVE_MINUTE_OPERATION_LEVEL_INVALID"
    if carries_decision_identity:
        terminal_level = setup.get("terminal_segment_level")
        terminal_id = setup.get("terminal_segment_id")
        terminal_end = _parse_time(setup.get("terminal_segment_end_at"))
        setup_anchor = _parse_time(setup.get("anchor_at"))
        if (
            setup.get("terminal_segment_role") != "latest_completed"
            or setup.get("terminal_segment_state") not in {"formed", "locked"}
            or setup.get("terminal_segment_source_kind") != "segment"
            or setup.get("terminal_segment_direction")
            != ("down" if side == "buy" else "up")
            or terminal_level != recursive_level
            or not isinstance(terminal_id, str)
            or not terminal_id
            or terminal_end is None
            or setup_anchor is None
            or terminal_end != setup_anchor
        ):
            return "TERMINAL_SEGMENT_LINEAGE_INVALID"

    execution_profile = _mapping(signal.get("execution_profile"))
    if execution_profile:
        if execution_profile.get("structure_signal_confirmed") is not True:
            return "FIVE_MINUTE_STRUCTURE_NOT_CONFIRMED"
        if execution_profile.get("one_minute_required_for_trade_signal") is True:
            return "ONE_MINUTE_ROLE_CONTRACT_INVALID"
        if (
            execution_profile.get("one_minute_required_for_precise_execution")
            is not True
        ):
            return "ONE_MINUTE_PRECISE_EXECUTION_CONTRACT_INVALID"
        # ``hard_blocked`` is an execution/sizing verdict, not a structural
        # existence verdict.  Confirmed points still need an observation alert;
        # the formatter exposes the block reasons and keeps action at 0%.

    if signal.get("physical_timeframe_recursive") is not True:
        return "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING"
    warmup = _mapping(signal.get("warmup"))
    if five_minute_warmup_converged(warmup) is not True:
        return "WARMUP_NOT_CONVERGED"
    segment = _mapping(signal.get("segment_difference_1m"))
    if new_stage == _SEGMENT_ENRICHED_STAGE and not segment:
        return "ONE_MINUTE_SEGMENT_EVIDENCE_MISSING"
    if segment and not is_one_minute_segment_difference_document(
        segment,
        expected_side=str(signal.get("side")),
    ):
        return "ONE_MINUTE_SEGMENT_EVIDENCE_INVALID"
    if new_stage == _SEGMENT_ENRICHED_STAGE and _stage(signal) not in {
        "triggered",
        "executable",
        "active",
    }:
        return "FIVE_MINUTE_SIGNAL_NOT_ACTIVE_FOR_SEGMENT_ENRICHMENT"
    event_at = _notification_evidence_time(
        signal,
        new_stage=new_stage,
    )
    observed_at = _parse_time(
        signal.get("monitor_observed_at") or signal.get("observed_at")
    )
    if event_at is None or observed_at is None:
        return "SIGNAL_TIME_UNAVAILABLE"
    if observed_at < event_at:
        return "SIGNAL_FROM_FUTURE"
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        checked_at = _parse_time(evaluated_at)
        if checked_at is None or checked_at < observed_at:
            checked_at = observed_at
        boundary_status = segment_difference_boundary_status(
            signal,
            trigger=segment,
            evaluated_at=checked_at,
        )
        # A sell locator has no buy-entry TTL and is intentionally reported as
        # ``not_applicable``. Buy-side enrichment is a realtime notification,
        # not a historical-fact replay: once its short execution boundary has
        # expired (or cannot be attested), retain it in the structural snapshot
        # but never enqueue DingTalk delivery or strict chart rendering.
        if boundary_status not in {"current", "not_applicable"}:
            return (
                "ONE_MINUTE_SEGMENT_EVIDENCE_EXPIRED"
                if boundary_status == "expired"
                else "ONE_MINUTE_SEGMENT_EVIDENCE_NOT_CURRENT"
            )

    return None


def _approaching_digest_eligibility_reason(
    signal: Mapping[str, object],
    *,
    observed_at: datetime,
) -> str | None:
    """Validate a current forming structure without promoting it to a signal."""

    if _stage(signal) != "approaching":
        return "NOT_APPROACHING"
    side = str(signal.get("side") or "")
    if side not in {"buy", "sell"}:
        return "SIGNAL_SIDE_INVALID"
    setup = _mapping(signal.get("setup_5m"))
    if (
        setup.get("status") != "provisional"
        or setup.get("actionable") is not False
        or setup.get("source_frequency") != "5m"
        or setup.get("side") != side
        or setup.get("point_id") != signal.get("point_id")
        or setup.get("terminal_segment_role") != "latest_unfinished"
        or setup.get("terminal_segment_state") != "forming"
    ):
        return "FIVE_MINUTE_APPROACHING_STRUCTURE_INVALID"
    recursive_level = setup.get("recursive_level")
    if type(recursive_level) is not int or not is_five_minute_trade_level(
        "5m", recursive_level
    ):
        return "FIVE_MINUTE_OPERATION_LEVEL_INVALID"
    if signal.get("physical_timeframe_recursive") is not True:
        return "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING"
    if five_minute_warmup_converged(_mapping(signal.get("warmup"))) is not True:
        return "WARMUP_NOT_CONVERGED"
    event_at = _parse_time(
        setup.get("available_at")
        or setup.get("confirmed_at")
        or signal.get("observed_at")
    )
    monitor_observed_at = _parse_time(
        signal.get("monitor_observed_at") or signal.get("observed_at")
    )
    if event_at is None or monitor_observed_at is None:
        return "SIGNAL_TIME_UNAVAILABLE"
    local_now = observed_at.astimezone(CN)
    if (
        event_at.date() != local_now.date()
        or monitor_observed_at.date() != local_now.date()
    ):
        return "APPROACHING_SIGNAL_NOT_CURRENT_SESSION"
    if monitor_observed_at < event_at or local_now < event_at:
        return "SIGNAL_FROM_FUTURE"
    if _approaching_occurrence_key(signal) is None:
        return "APPROACHING_OCCURRENCE_IDENTITY_INVALID"
    return None


def _preconfirmation_divergence_eligibility_reason(
    signal: Mapping[str, object],
    divergence: Mapping[str, object],
    *,
    observed_at: datetime,
) -> str | None:
    """Validate a 1m divergence without promoting its unfinished 5m parent."""

    stage = _stage(signal)
    if stage not in {"approaching", "formed"}:
        return "FIVE_MINUTE_STRUCTURE_ALREADY_CONFIRMED_OR_TERMINAL"
    side = str(signal.get("side") or "")
    if side not in {"buy", "sell"}:
        return "SIGNAL_SIDE_INVALID"
    setup = _mapping(signal.get("setup_5m"))
    expected_terminal_states = {
        "approaching": ("latest_unfinished", frozenset({"forming"})),
        "formed": ("latest_completed", frozenset({"formed", "locked"})),
    }
    expected_role, expected_states = expected_terminal_states[stage]
    if (
        setup.get("status") != "provisional"
        or setup.get("actionable") is not False
        or setup.get("source_frequency") != "5m"
        or setup.get("side") != side
        or setup.get("point_id") != signal.get("point_id")
        or setup.get("terminal_segment_role") != expected_role
        or setup.get("terminal_segment_state") not in expected_states
        or setup.get("terminal_segment_source_kind") != "segment"
        or isinstance(signal.get("segment_difference_1m"), Mapping)
    ):
        return "FIVE_MINUTE_PRECONFIRMATION_STRUCTURE_INVALID"
    recursive_level = setup.get("recursive_level")
    if type(recursive_level) is not int or not is_five_minute_trade_level(
        "5m", recursive_level
    ):
        return "FIVE_MINUTE_OPERATION_LEVEL_INVALID"
    if signal.get("physical_timeframe_recursive") is not True:
        return "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING"
    if five_minute_warmup_converged(_mapping(signal.get("warmup"))) is not True:
        return "WARMUP_NOT_CONVERGED"
    execution_profile = _mapping(signal.get("execution_profile"))
    if execution_profile and (
        execution_profile.get("structure_signal_confirmed") is not False
        or execution_profile.get("segment_difference_status") != "STRUCTURE_PENDING"
        or execution_profile.get("segment_difference_ready") is not False
        or execution_profile.get("precise_execution_ready") is not False
    ):
        return "PRECONFIRMATION_EXECUTION_CONTRACT_INVALID"
    if (
        not is_one_minute_segment_difference_document(
            divergence,
            expected_side=side,
        )
        or divergence.get("divergence_kind") not in {"trend", "consolidation"}
        or divergence.get("terminal_segment_role") != "latest_completed"
        or divergence.get("terminal_segment_source_kind") != "segment"
        or divergence.get("terminal_segment_state") not in {"formed", "locked"}
        or divergence.get("price_basis_revision")
        != setup.get("price_basis_revision")
    ):
        return "ONE_MINUTE_PRECONFIRMATION_DIVERGENCE_INVALID"
    setup_start = _parse_time(setup.get("terminal_segment_start_at"))
    setup_end = _parse_time(setup.get("terminal_segment_end_at"))
    divergence_start = _parse_time(divergence.get("terminal_segment_start_at"))
    divergence_end = _parse_time(divergence.get("terminal_segment_end_at"))
    if (
        setup_start is None
        or setup_end is None
        or divergence_start is None
        or divergence_end is None
        or setup_start - timedelta(minutes=5)
        > divergence_start - timedelta(minutes=1)
        or divergence_end > setup_end
    ):
        return "ONE_MINUTE_PRECONFIRMATION_LINEAGE_NOT_NESTED"
    setup_available_at = _parse_time(setup.get("available_at"))
    divergence_available_at = _parse_time(
        divergence.get("available_at") or divergence.get("confirmed_at")
    )
    monitor_observed_at = _parse_time(
        signal.get("monitor_observed_at") or signal.get("observed_at")
    )
    if (
        setup_available_at is None
        or divergence_available_at is None
        or monitor_observed_at is None
    ):
        return "SIGNAL_TIME_UNAVAILABLE"
    local_now = observed_at.astimezone(CN)
    if (
        setup_available_at.date() != local_now.date()
        or divergence_available_at.date() != local_now.date()
        or monitor_observed_at.date() != local_now.date()
    ):
        return "PRECONFIRMATION_DIVERGENCE_NOT_CURRENT_SESSION"
    jointly_known_at = max(setup_available_at, divergence_available_at)
    if monitor_observed_at < jointly_known_at or local_now < jointly_known_at:
        return "SIGNAL_FROM_FUTURE"
    if _preconfirmation_divergence_occurrence_key(signal, divergence) is None:
        return "PRECONFIRMATION_DIVERGENCE_IDENTITY_INVALID"
    return None


def _signal_semantic_key(signal: Mapping[str, object]) -> tuple[str, ...]:
    setup = _mapping(signal.get("setup_5m"))
    return (
        str(signal.get("code") or ""),
        str(signal.get("side") or ""),
        str(setup.get("source_frequency") or ""),
        str(setup.get("point_type") or signal.get("point_type") or ""),
        str(
            setup.get("recursive_level")
            if setup.get("recursive_level") is not None
            else signal.get("recursive_level")
            if signal.get("recursive_level") is not None
            else ""
        ),
        _time_identity(setup.get("anchor_at") or setup.get("available_at")),
        _time_identity(setup.get("available_at") or setup.get("confirmed_at")),
    )


def _terminal_occurrence_key(
    signal: Mapping[str, object],
    new_stage: str,
) -> tuple[str, ...] | None:
    """Identify one terminal transition independently of a rebuilt signal id."""

    if new_stage not in {"invalidated", "closed"}:
        return None
    semantic_key = _signal_semantic_key(signal)
    # 终态只绑定正式 5 分钟发生事实；可选 1 分钟区间套定位不改变信号身份。
    if any(not value for value in semantic_key):
        return None
    return (new_stage, *semantic_key)


def _terminal_occurrence_event_id(key: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema": "chanlun-signal-notification-terminal-occurrence",
            "strategy_id": STRATEGY_ID,
            "key": key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _notification_group_key(
    signal: Mapping[str, object],
    new_stage: str,
    fallback_event_id: str,
) -> tuple[str, ...]:
    segment_key = _segment_occurrence_key(signal, new_stage)
    if segment_key is not None:
        return ("segment", *segment_key)
    trigger_key = _trigger_occurrence_key(signal, new_stage)
    if trigger_key is not None:
        return ("trigger", *trigger_key)
    terminal_key = _terminal_occurrence_key(signal, new_stage)
    if terminal_key is not None:
        return ("terminal", *terminal_key)
    return ("signal", fallback_event_id)


def _notification_group_event_id(group_key: tuple[str, ...]) -> str:
    if group_key[0] == "segment":
        return _segment_occurrence_event_id(group_key[1:])
    if group_key[0] == "trigger":
        return _trigger_occurrence_event_id(group_key[1:])
    if group_key[0] == "terminal":
        return _terminal_occurrence_event_id(group_key[1:])
    return group_key[1]


def _notification_document_delivery_priority(
    document: Mapping[str, object],
    new_stage: str,
) -> int:
    """Return a stable risk rank shared by dispatch and the durable outbox."""

    sources = document.get("selection_sources")
    source_values = (
        sources if isinstance(sources, (list, tuple, set, frozenset)) else ()
    )
    manual_attention = bool(
        {str(value) for value in source_values} & _MANUAL_ATTENTION_SOURCES
    )
    side = str(
        document.get("side") or _mapping(document.get("setup_5m")).get("side") or ""
    )
    profile = _mapping(document.get("execution_profile"))
    position = _mapping(document.get("position_recommendation"))
    hard_blocked = bool(
        profile.get("hard_blocked") is True
        or profile.get("recommendation") == "BLOCKED"
        or position.get("status") == "BLOCKED"
    )
    actionable = bool(
        not hard_blocked
        and (
            document.get("entry_allowed") is True
            or document.get("exit_allowed") is True
        )
    )
    if side == "sell" and manual_attention:
        risk_rank = 0
    elif side == "sell":
        risk_rank = 1
    elif side == "buy" and manual_attention:
        risk_rank = 2
    elif side == "buy" and actionable:
        risk_rank = 3
    elif side == "buy":
        risk_rank = 4
    else:
        risk_rank = 5
    if new_stage in {"triggered", "executable"}:
        return risk_rank
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        return 6 + risk_rank
    return 10 + risk_rank


def _notification_group_dispatch_priority(
    item: tuple[
        tuple[str, ...],
        list[tuple[str, str | None, str, Mapping[str, object]]],
    ],
) -> tuple[object, ...]:
    """Order durable alerts by user risk instead of symbol/hash identity.

    A busy scan can discover several independent occurrences in one refresh.
    Transport failure or rate limiting must not let a lexically earlier buy
    consume the delivery opportunity before a sell signal.  Stable group keys
    keep ordering deterministic inside the same risk class.
    """

    group_key, candidates = item
    return (
        min(
            _notification_document_delivery_priority(document, new_stage)
            for _signal_id, _old_stage, new_stage, document in candidates
        ),
        group_key,
    )


def _suppression_fingerprint(event_id: str, reason: str) -> str:
    """Return the persistent idempotency key for one suppressed occurrence."""

    payload = json.dumps(
        {
            "schema": "chanlun-notification-suppression-fingerprint",
            "event_id": event_id,
            "reason": reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _text(value: object, default: str = "—") -> str:
    rendered = str(value).strip() if value is not None else ""
    return rendered if rendered else default


def _notification_time_text(value: object) -> str:
    """把结构或通知链路时刻固定渲染为上海时区的秒级时间。"""

    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return "暂不可用"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=CN)
    return parsed.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")


def _notification_timeline_parts(
    items: tuple[tuple[str, object], ...],
) -> list[str]:
    """同一交易日只展示一次日期，跨日时保留每个完整时刻。"""

    parsed = [_parse_time(value) for _label, value in items]
    if all(value is not None for value in parsed):
        localized = [value.astimezone(CN) for value in parsed if value is not None]
        dates = {value.date() for value in localized}
        if len(dates) == 1:
            return [
                f"日期 {localized[0].strftime('%Y-%m-%d')}",
                *[
                    f"{label} {value.strftime('%H:%M:%S')}"
                    for (label, _raw), value in zip(items, localized, strict=True)
                ],
            ]
    return [
        f"{label} {_notification_time_text(value)}"
        for label, value in items
    ]


def _notification_deadline_text(value: object, *, reference: object) -> str:
    deadline = _parse_time(value)
    reference_time = _parse_time(reference)
    if deadline is None:
        return "暂不可用"
    deadline = deadline.astimezone(CN)
    if (
        reference_time is not None
        and reference_time.astimezone(CN).date() == deadline.date()
    ):
        return f"当日 {deadline.strftime('%H:%M:%S')}"
    return deadline.strftime("%Y-%m-%d %H:%M:%S")


def _terminal_segment_text(setup: Mapping[str, object]) -> str:
    role = str(setup.get("terminal_segment_role") or "")
    if role not in {"latest_unfinished", "latest_completed"}:
        return "末端线段：血缘暂不可用"
    state_code = str(setup.get("terminal_segment_state") or "")
    role_label = (
        "最新形成中线段"
        if role == "latest_unfinished"
        else "最新已锁定线段"
        if state_code == "locked"
        else "最新几何成形线段"
    )
    direction = _DIRECTION_LABELS.get(
        str(setup.get("terminal_segment_direction") or ""),
        "方向待定",
    )
    state = {
        "forming": "形成中",
        "formed": "几何已成形、末端仍会随新K更新",
        "locked": "末端结构已封存",
    }.get(state_code, "状态待定")
    start_at = _notification_time_text(setup.get("terminal_segment_start_at"))
    end_at = _notification_time_text(setup.get("terminal_segment_end_at"))
    return f"末端线段：{role_label}｜{direction}｜{state}｜{start_at}→{end_at}"


def _current_price_text(value: object) -> str:
    """展示真实随行情进入结构快照的价格，缺失时不回退到结构锚点。"""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "暂不可用"
    if parsed <= 0 or not isfinite(parsed):
        return "暂不可用"
    rendered = f"{parsed:.6f}".rstrip("0").rstrip(".")
    return rendered or "暂不可用"


def _current_price_label(source: object) -> str:
    return {
        "realtime_tick": "当前价",
        "latest_completed_1m_close": "最近1分钟收盘价",
        "latest_completed_5m_close": "最近5分钟收盘价",
        "latest_completed_bar_close": "最近已完成K线收盘价",
        "legacy_completed_bar_close": "历史已完成K线收盘价",
    }.get(str(source or ""), "最近已完成K线收盘价")


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and isfinite(parsed) else None


def _structure_anchor_value(setup: Mapping[str, object]) -> object:
    # 新版标准文档使用 ``anchor_price``；旧通知档案可能仍保留兼容别名。
    return setup.get("anchor_price") or setup.get("structure_anchor_price")


def _entry_execution_boundary(
    signal: Mapping[str, object],
) -> Mapping[str, object]:
    return _mapping(signal.get("entry_execution_boundary"))


def _buy_entry_guidance_state(
    signal: Mapping[str, object],
    *,
    detected_at: object | None = None,
) -> tuple[str, float | None]:
    """Project the short-lived 1m locator into a fail-closed display state.

    The 5m anchor is structural evidence and must never be substituted for the
    raw high of the 1m confirmation bar.  This helper only affects notification
    guidance; the canonical decision document remains unchanged.
    """

    side = str(
        signal.get("side")
        or _mapping(signal.get("setup_5m")).get("side")
        or ""
    )
    if side != "buy":
        return "not_applicable", None
    boundary_status = segment_difference_boundary_status(
        signal,
        evaluated_at=detected_at,
    )
    if boundary_status == "absent":
        return "waiting", None
    if boundary_status == "expired":
        return "expired", _positive_float(
            _entry_execution_boundary(signal).get("raw_high")
        )
    if boundary_status != "current":
        return "unavailable", None
    boundary = _entry_execution_boundary(signal)
    price_cap = _positive_float(boundary.get("raw_high"))
    if (
        price_cap is None
        or _parse_time(boundary.get("confirmation_bar_closed_at")) is None
        or _parse_time(boundary.get("entry_valid_until")) is None
    ):
        return "unavailable", None
    current_price = _positive_float(signal.get("current_price"))
    if current_price is not None and current_price > price_cap:
        return "price_above_cap", price_cap
    if str(signal.get("realtime_quote_status") or "") == "unavailable":
        return "price_unavailable", price_cap
    return "current", price_cap


def _risk_gate_summary(signal: Mapping[str, object]) -> str:
    risk = _mapping(signal.get("higher_timeframe_risk"))
    labels = {
        "GREEN": "通过",
        "AMBER": "谨慎",
        "RED": "阻断",
        "UNRESOLVED": "待核验",
    }
    values = (
        ("市场", risk.get("market_gate")),
        ("板块", risk.get("sector_gate")),
        ("个股", risk.get("symbol_gate")),
    )
    if not any(value not in (None, "") for _label, value in values):
        return "风险门待核验"
    if all(str(value or "") == "GREEN" for _label, value in values):
        return "全部通过"
    return "／".join(
        f"{label}{labels.get(str(value or ''), '待核验')}"
        for label, value in values
    )


def _judgment_checklist_line(
    signal: Mapping[str, object],
    *,
    setup_point: str,
    trigger_evidence: str,
    new_stage: str,
    detected_at: object | None = None,
) -> str:
    setup = _mapping(signal.get("setup_5m"))
    side = str(
        signal.get("side")
        or setup.get("side")
        or ""
    )
    setup_level = _recursive_level_text(
        setup.get("recursive_level")
        if "recursive_level" in setup
        else signal.get("recursive_level")
    )
    if new_stage == "invalidated":
        five_minute = f"{setup_point}（{setup_level}）已失效"
    elif new_stage == "closed":
        five_minute = f"{setup_point}（{setup_level}）跟踪结束"
    else:
        five_minute = f"{setup_point}（{setup_level}）已确认"

    terminal_state = (
        "原结构已失效"
        if new_stage == "invalidated"
        else "跟踪已结束"
        if new_stage == "closed"
        else "已封存"
        if setup.get("lock_state") == "locked"
        else "未封存，仍随新K更新"
    )

    trigger = _mapping(signal.get("segment_difference_1m"))
    boundary_status = segment_difference_boundary_status(
        signal,
        trigger=trigger,
        evaluated_at=detected_at,
    )
    if new_stage == "invalidated":
        one_minute = "原定位已作废"
    elif new_stage == "closed":
        one_minute = "跟踪已结束"
    elif not trigger:
        one_minute = "待出现"
    elif side == "sell":
        one_minute = f"{trigger_evidence}已确认（仅精确定位）"
    elif boundary_status == "current":
        one_minute = f"{trigger_evidence}已确认（窗口有效）"
    elif boundary_status == "expired":
        one_minute = f"{trigger_evidence}历史已确认（窗口已过）"
    else:
        one_minute = f"{trigger_evidence}已确认（执行边界缺失）"
    risk_gate = (
        "不再适用"
        if new_stage in {"invalidated", "closed"}
        else _risk_gate_summary(signal)
    )
    return (
        f"判断：5分钟={five_minute}｜末端结构={terminal_state}｜"
        f"1分钟区间套定位：{one_minute}｜风险门={risk_gate}"
    )


def _execution_price_snapshot(
    signal: Mapping[str, object],
    setup: Mapping[str, object],
) -> str:
    """把执行所需的行情来源、价格时点和结构偏离压缩到一个短语。"""

    current = _positive_float(signal.get("current_price"))
    anchor = _positive_float(_structure_anchor_value(setup))
    source_label = _current_price_label(signal.get("current_price_source"))
    rendered = f"{source_label}：{_current_price_text(signal.get('current_price'))}"
    price_at = _notification_time_text(signal.get("current_price_at"))
    if price_at != "暂不可用":
        rendered += f"（获取 {price_at}）"
    if current is not None and anchor is not None:
        drift = (current - anchor) / anchor * 100
        rendered += f"｜较5分钟锚点 {drift:+.2f}%"
    return rendered


def _execution_defense_snapshot(
    signal: Mapping[str, object],
    setup: Mapping[str, object],
) -> str:
    current = _positive_float(signal.get("current_price"))
    defense = _positive_float(_defense_price_value(setup))
    rendered = f"失效：5分钟失效价 {_defense_price_text(signal, setup)}"
    raw_values = signal.get("notification_defense_prices")
    distinct_values = (
        {
            str(value).strip()
            for value in raw_values
            if value is not None and str(value).strip()
        }
        if isinstance(raw_values, (list, tuple, set, frozenset))
        else set()
    )
    # 共振通知可能同时披露多个结构边界；这时单一距离没有明确归属，宁可省略。
    if len(distinct_values) > 1:
        return rendered
    if current is None or defense is None:
        return rendered
    side = str(signal.get("side") or setup.get("side") or "")
    if side not in {"buy", "sell"}:
        return rendered
    if side == "buy":
        distance = (current - defense) / current * 100
        distance_text = (
            f"距向下失效 {distance:.2f}%"
            if distance >= 0
            else f"已跌破失效价 {abs(distance):.2f}%"
        )
    else:
        distance = (defense - current) / current * 100
        distance_text = (
            f"距向上失效 {distance:.2f}%"
            if distance >= 0
            else f"已突破失效价 {abs(distance):.2f}%"
        )
    return f"{rendered}｜{distance_text}"


def _execution_boundary_line(
    signal: Mapping[str, object],
    setup: Mapping[str, object],
    *,
    new_stage: str,
    detected_at: object | None = None,
) -> str:
    side = str(signal.get("side") or setup.get("side") or "")
    if new_stage == "invalidated":
        price = _execution_price_snapshot(signal, setup)
        return (
            f"执行：停止使用原结构｜{price}｜"
            "旧1分钟定位与旧模型比例同时作废"
        )
    if new_stage == "closed":
        return "执行：本次结构跟踪已结束；旧边界不得继续使用"
    price = _execution_price_snapshot(signal, setup)
    if side == "sell":
        return (
            f"执行：{price}｜1分钟只定位卖出时点｜"
            "退出规则由卖点与持有结构级别关系决定"
        )
    if side != "buy":
        return "执行：待人工核对"

    state, price_cap = _buy_entry_guidance_state(
        signal,
        detected_at=detected_at,
    )
    if state == "waiting":
        return (
            f"执行：等待同向1分钟区间套，未定位前不执行｜{price}"
        )
    if state == "expired":
        old_cap = (
            f" { _current_price_text(price_cap) }"
            if price_cap is not None
            else ""
        )
        return (
            f"执行：旧1分钟买入上限{old_cap}已过期，本次比例0%；"
            f"等待新的1分钟区间套｜{price}"
        )
    if state == "unavailable":
        return (
            f"执行：1分钟确认K最高价缺失，买入上限不可用；"
            f"5分钟锚点不得替代｜{price}"
        )

    boundary = _entry_execution_boundary(signal)
    valid_until = _notification_deadline_text(
        boundary.get("entry_valid_until"),
        reference=(
            detected_at
            or signal.get("monitor_observed_at")
            or signal.get("observed_at")
            or boundary.get("confirmation_bar_closed_at")
        ),
    )
    current_price = _positive_float(signal.get("current_price"))
    source = str(signal.get("current_price_source") or "")
    if current_price is None:
        price_condition = "实时价待核对"
    else:
        relation = "≤" if price_cap is not None and current_price <= price_cap else ">"
        price_condition = f"{relation}上限"
        if state == "price_above_cap":
            price_condition += "，禁止追价"
        elif source == "realtime_tick":
            price_condition += "，价格条件通过"
        else:
            price_condition += "，仅作预检；执行前须核实时价"
    return (
        f"执行：{price}｜1分钟买入上限 {_current_price_text(price_cap)}｜"
        f"有效至 {valid_until}｜{price_condition}"
    )


def _notification_position_recommendation(
    signal: Mapping[str, object],
    *,
    detected_at: object | None = None,
    new_stage: str = "triggered",
) -> Mapping[str, object]:
    """按通知最终可见价格重算人工仓位上限，不修改规范决策文档。"""

    setup = _mapping(signal.get("setup_5m"))
    side = str(signal.get("side") or setup.get("side") or "")
    realtime_quote_unavailable = bool(
        side == "buy"
        and str(signal.get("realtime_quote_status") or "") == "unavailable"
    )
    entry_guidance_state, _entry_price_cap = _buy_entry_guidance_state(
        signal,
        detected_at=detected_at,
    )
    boundary_blocked = entry_guidance_state in {
        "expired",
        "unavailable",
        "price_above_cap",
    }
    projected = _mapping(signal.get("notification_position_recommendation"))
    if (
        projected
        and not realtime_quote_unavailable
        and not boundary_blocked
        and entry_guidance_state != "waiting"
    ):
        return projected

    canonical = _mapping(signal.get("position_recommendation"))
    profile = _mapping(signal.get("execution_profile"))
    # A hard execution/sizing gate does not erase the structural point, but it
    # is authoritative for the displayed action. Keep the alert and force its
    # operational projection to 0% even if a partially migrated producer left
    # an inconsistent READY recommendation behind.
    recommendation = (
        "BLOCKED"
        if profile.get("hard_blocked") is True or boundary_blocked
        else WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
        if entry_guidance_state == "waiting"
        else str(profile.get("recommendation") or "")
    )
    if not recommendation:
        recommendation = {
            "BLOCKED": "BLOCKED",
            "NOT_ACTIONABLE": (
                WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
                if canonical.get("basis")
                == ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_BASIS
                else GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION
                if canonical.get("basis")
                == "GEOMETRIC_5M_CANDIDATE_AWAITING_CONFIRMATION"
                else "WAITING_STRUCTURE"
            ),
        }.get(str(canonical.get("status") or ""), "READY")

    anchor = _structure_anchor_value(setup)
    current_price = _positive_float(signal.get("current_price"))
    # 已完成K线价格仍可用于保留“不追价/已破位”等保守的 0% 保护，但实时
    # 询价失败时不能据此发布新的非零买入比例。卖点不依赖买入比例，仍按
    # 结构级别关系正常通知。
    entry_price: object = current_price if current_price is not None else anchor
    try:
        result = build_position_recommendation(
            side=side,
            recommendation=recommendation,
            risk_multiplier=signal.get("risk_multiplier", "1"),
            context_risk_scale=profile.get("context_risk_scale", "1"),
            entry_price=entry_price,
            structural_stop=_defense_price_value(setup),
            exit_action=str(signal.get("exit_action") or "none"),
            structure_anchor_price=anchor,
        ).document()
        if realtime_quote_unavailable and result.get("status") in {
            "RECOMMENDED",
            "UNRESOLVED",
        }:
            # ``POSITION_RATIO_INPUT_UNRESOLVED`` is also used by historical
            # account-coupled migrations and is therefore intentionally
            # rejected by the durable review inbox.  Quote failure is a
            # different, purely structural evidence state: name it explicitly
            # so the alert can be retained while sizing remains fail-closed.
            result.update(
                {
                    "status": "UNRESOLVED",
                    "basis": "REALTIME_PRICE_UNAVAILABLE",
                    "recommended_ratio": None,
                    "recommended_percent": None,
                    "label": "结构风险参考：实时价格未取得，暂不生成买入比例",
                    "reason_codes": ["REALTIME_PRICE_UNAVAILABLE"],
                }
            )
        return result
    except (TypeError, ValueError):
        return canonical


def _elapsed_text(start: object, end: object) -> str:
    started = _parse_time(start)
    ended = _parse_time(end)
    if started is None or ended is None:
        return "暂不可用"
    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        return "时序异常"
    minutes, remainder = divmod(seconds, 60)
    if minutes == 0:
        return f"{remainder}秒"
    if remainder == 0:
        return f"{minutes}分钟"
    return f"{minutes}分{remainder}秒"


def _display_percent(value: object) -> tuple[str, str | None]:
    """把模型比例安全向下显示到一位小数，同时保留可审计原值。"""

    raw = str(value or "").strip()
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return raw, None
    if not parsed.is_finite() or parsed < 0:
        return raw, None
    if Decimal("0") < parsed < Decimal("0.1"):
        normalized_raw = format(parsed, "f").rstrip("0").rstrip(".")
        return normalized_raw, None
    displayed = parsed.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    rendered = format(displayed, "f").rstrip("0").rstrip(".") or "0"
    normalized_raw = format(parsed, "f").rstrip("0").rstrip(".") or "0"
    return rendered, normalized_raw if rendered != normalized_raw else None


def _recursive_level_text(value: object) -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return f"递归层级：L{max(parsed, 0)}"


def _point_label(value: object, default: str = "未触发") -> str:
    point = str(value or "").strip()
    if not point:
        return default
    return _POINT_LABELS.get(point, point)


def _localized(value: object, labels: Mapping[str, str], default: str = "未知") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return labels.get(text, text)


def _scope_label(signal: Mapping[str, object]) -> str:
    raw = signal.get("selection_sources")
    values = raw if isinstance(raw, (list, tuple, set, frozenset)) else ()
    sources = {str(value) for value in values}
    return "人工关注" if sources & _MANUAL_ATTENTION_SOURCES else "候选"


def _signal_market(signal: Mapping[str, object]) -> str:
    explicit = str(signal.get("market") or "").strip().lower()
    if explicit:
        return explicit
    code = str(signal.get("code") or "").strip().upper()
    if code.endswith(".US"):
        return "us"
    if code.startswith("HK.") or code.endswith(".HK"):
        return "hk"
    return "a"


def _defense_price_text(
    signal: Mapping[str, object],
    setup: Mapping[str, object],
) -> str:
    """展示操作级结构防守位，不臆造价格。

    五分钟 setup 是通知结构的直接来源，因此其失效价同时约束买点与卖点。
    卖点防守位是向上的失效边界而非止损价，通知中必须明确方向。
    """

    raw_values = signal.get("notification_defense_prices")
    values = (
        tuple(
            dict.fromkeys(
                str(value).strip()
                for value in raw_values
                if value is not None and str(value).strip()
            )
        )
        if isinstance(raw_values, (list, tuple, set, frozenset))
        else ()
    )
    value = _defense_price_value(setup)
    rendered = "、".join(values) if values else _text(value, "待结构确认")
    side = str(signal.get("side") or "").strip()
    if not side:
        point = str(setup.get("point_type") or signal.get("point_type") or "")
        side = (
            "buy" if point.endswith("buy") else "sell" if point.endswith("sell") else ""
        )
    if side == "buy":
        return f"{rendered}（跌破买入结构失效）"
    if side == "sell":
        return f"{rendered}（突破卖出结构失效）"
    return rendered


def _position_reason_codes(
    signal: Mapping[str, object],
    recommendation: Mapping[str, object],
) -> tuple[str, ...]:
    profile = _mapping(signal.get("execution_profile"))
    values: list[object] = []
    for raw in (
        recommendation.get("reason_codes"),
        profile.get("hard_block_reason_codes"),
        signal.get("decision_reasons"),
    ):
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    return tuple(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def _blocked_position_reason_text(
    signal: Mapping[str, object],
    recommendation: Mapping[str, object],
) -> str:
    reasons = set(_position_reason_codes(signal, recommendation))
    side = str(signal.get("side") or recommendation.get("side") or "")
    if (
        lifecycle_stage_from_signal(signal) == "invalidated"
        or "structure_invalidated" in reasons
        or "STRUCTURE_INVALIDATED" in reasons
    ):
        return (
            "本条5分钟卖点结构已失效，结束本结构跟踪"
            if side == "sell"
            else "本条5分钟买点结构已失效，等待新的5分钟结构"
        )
    if "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR" in reasons:
        return "当前价已超过结构锚点的5%追价保护线，等待新的5分钟结构"
    if "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP" in reasons:
        return "当前价已触及或跌破5分钟结构防守位"
    if "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in reasons:
        return "1分钟区间套定位窗口已过，等待新的1分钟区间套"
    if "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING" in reasons:
        return "1分钟区间套已出现，但精确执行边界不可用"
    if "WARMUP_CONVERGENCE_GATE_FAILED" in reasons:
        return "5分钟完整历史与对照窗口的活动买卖点不一致，等待重新收敛"
    if "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH" in reasons:
        return "原生日线与交易日历覆盖不一致，等待数据校验通过"
    if "QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH" in reasons:
        return "原生日线与1分钟派生日线的开高低收量不一致，等待数据校验通过"
    if "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED" in reasons:
        return "高周期同源行情完整性校验未通过"
    if reasons & {"same_or_higher_structure_conflict", "structure_conflict"}:
        return "同级或更高级别存在反向结构冲突"
    if "three_buy_lacks_tick_clearance" in reasons:
        return "三买离开中枢的价格空间不足一个最小价位"
    return "具体限制原因未完整保存，请核对诊断证据"


def _action_advice(
    signal: Mapping[str, object],
    *,
    point_type: object,
    scope: str,
    new_stage: str,
    detected_at: object | None = None,
) -> str:
    if new_stage == "invalidated":
        return "操作：取消该结构计划"
    if new_stage == "closed":
        return "操作：结束跟踪"
    side = str(signal.get("side") or "").strip()
    entry_state, entry_price_cap = _buy_entry_guidance_state(
        signal,
        detected_at=detected_at,
    )
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        boundary_status = segment_difference_boundary_status(
            signal,
            evaluated_at=detected_at,
        )
        if side == "buy" and entry_state == "price_above_cap":
            return (
                "操作：禁止追价；当前价已超过1分钟买入上限 "
                f"{_current_price_text(entry_price_cap)}，等待价格重新满足边界"
                "或新的1分钟定位"
            )
        if side == "buy" and entry_state == "unavailable":
            return (
                "操作：暂不执行；1分钟定位已确认，但买入上限缺失，"
                "5分钟锚点不能替代"
            )
        if boundary_status == "current":
            if side == "sell":
                return (
                    "操作：复核卖出；1分钟精确定位已确认，"
                    "须先核对卖点与持有结构级别"
                )
            return (
                "操作：可人工复核买入；1分钟定位窗口有效，"
                "须满足下方全部执行与风险边界"
            )
        if boundary_status == "expired":
            return (
                "操作：暂不执行；1分钟定位窗口已过，不追价，等待新的1分钟区间套"
            )
        if boundary_status == "unavailable":
            return (
                "操作：暂不执行；1分钟定位已确认但执行边界缺失，"
                "等待边界恢复或新的1分钟定位"
            )
        if boundary_status == "not_applicable":
            return (
                "操作：复核卖出；1分钟定位已确认，先核对5分钟卖点与持有结构级别"
            )
        return "操作：暂不执行；1分钟定位状态待核对"

    point = str(point_type or "").strip()
    if not side:
        side = "buy" if "buy" in point else "sell" if "sell" in point else ""
    profile = _mapping(signal.get("execution_profile"))
    recommendation = (
        "BLOCKED"
        if profile.get("hard_blocked") is True
        else str(profile.get("recommendation") or "")
    )
    operational = _notification_position_recommendation(
        signal,
        detected_at=detected_at,
        new_stage=new_stage,
    )
    operational_reasons = {
        str(value)
        for value in operational.get("reason_codes", ())
        if isinstance(value, str)
    }
    if side == "buy":
        if entry_state == "waiting":
            return "操作：暂不执行；5分钟买点已确认，等待同向1分钟区间套定位"
        if entry_state == "expired":
            return (
                "操作：暂不执行；旧1分钟定位已过期，不追价，等待新的1分钟区间套"
            )
        if entry_state == "unavailable":
            return (
                "操作：暂不执行；1分钟定位已确认但买入上限缺失，"
                "5分钟锚点不能替代"
            )
        if entry_state == "price_above_cap":
            return (
                "操作：禁止追价；当前价已超过1分钟买入上限 "
                f"{_current_price_text(entry_price_cap)}，等待价格重新满足边界"
                "或新的1分钟定位"
            )
        if (
            operational.get("status") == "NOT_ACTIONABLE"
            and ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_REASON
            in operational_reasons
        ):
            return (
                "操作：等待1分钟定位；5分钟买点已确认，当前不生成买入比例"
            )
        if operational.get("status") == "BLOCKED":
            if operational_reasons & {
                "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",
                "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",
            }:
                return (
                    "操作：禁止买入；当前价格已触发0%保护，不追价，"
                    "等待新的5分钟结构"
                )
            return "操作：本条买入不纳入操作计划；" + _blocked_position_reason_text(
                signal,
                operational,
            )
        if operational.get("status") == "UNRESOLVED":
            if signal.get("realtime_quote_status") == "unavailable":
                return (
                    "操作：仅观察；实时价格未取得，不使用已完成K线价格生成买入比例"
                )
            return (
                "操作：仅观察；结构价格或防守信息不足，补齐证据后再复核"
            )
        if recommendation == "CAUTION" or (
            not recommendation and signal.get("entry_allowed") is not True
        ):
            return (
                "操作：仅观察；5分钟买点已确认，需手工复核逆风环境和提示证据"
            )
        if recommendation == "BLOCKED":
            return "操作：本条买入不纳入操作计划；" + _blocked_position_reason_text(
                signal,
                operational,
            )
        if point.startswith("1buy"):
            condition = "一买反转已确认"
        elif point.startswith("2buy"):
            condition = "二买回踩不破已确认"
        elif point.startswith("3buy"):
            condition = "三买回抽已确认"
        else:
            condition = "5分钟买点已确认"
        return (
            f"操作：可人工复核分批买入；{condition}，"
            "须满足下方全部执行与风险边界"
        )
    if side == "sell":
        if (
            operational.get("status") == "NOT_ACTIONABLE"
            and ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_REASON
            in operational_reasons
        ):
            return (
                "操作：等待1分钟定位；5分钟卖点已确认，当前不生成退出比例"
            )
        if signal.get("exit_allowed") is not True:
            return (
                "操作：结构卖出提醒已确认；请核对卖点级别与结构仍然有效"
            )
        if "SAME_OR_HIGHER_STRUCTURE_FULL_EXIT" in operational_reasons:
            return (
                "操作：优先按完整退出规则人工复核；卖点与持有结构为同级或更高级别，"
                "并确认5分钟卖出结构未向上失效"
            )
        if "LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION" in operational_reasons:
            return (
                "操作：仅复核段差减仓；当前卖点属于低级别或不同结构，"
                "不得作为完整退出"
            )
        if operational.get("status") == "CONDITIONAL":
            return (
                "操作：暂不执行；先核对卖点与持有结构级别，"
                "再决定完整退出或段差减仓"
            )
        if point.startswith("3sell"):
            return "操作：复核退出；优先检查三卖退出条件"
        if point.startswith("2sell"):
            return "操作：复核卖出；反弹未转强时优先检查退出条件"
        return "操作：复核卖出或退出条件"
    return "操作：人工复核后再操作"


def _position_recommendation_line(
    signal: Mapping[str, object],
    *,
    detected_at: object | None = None,
    new_stage: str = "triggered",
) -> str:
    entry_state, entry_price_cap = _buy_entry_guidance_state(
        signal,
        detected_at=detected_at,
    )
    if entry_state == "waiting":
        return "风险参考：暂不计算（等待1分钟区间套给出精确买入位置）"
    if entry_state == "expired":
        return "风险参考：本次执行比例 0%（旧1分钟定位窗口已过）"
    if entry_state == "unavailable":
        return "风险参考：本次执行比例 0%（1分钟确认K最高价缺失）"
    if entry_state == "price_above_cap":
        return (
            "风险参考：本次执行比例 0%（当前可见价格超过1分钟买入上限 "
            f"{_current_price_text(entry_price_cap)}；结构模型上限不构成执行许可）"
        )
    recommendation = _notification_position_recommendation(
        signal,
        detected_at=detected_at,
        new_stage=new_stage,
    )
    status = str(recommendation.get("status") or "")
    side = str(signal.get("side") or recommendation.get("side") or "")
    percent = str(recommendation.get("recommended_percent") or "").strip()
    if status == "RECOMMENDED" and percent:
        displayed, model_value = _display_percent(percent)
        model_note = f"；精确测算 {model_value}%" if model_value else ""
        if side == "buy":
            return (
                f"风险参考：结构模型比例上限 {displayed}%"
                f"（按当前价至5分钟失效价{model_note}；"
                "非仓位建议）"
            )
        if side == "sell":
            reasons = {
                str(value)
                for value in recommendation.get("reason_codes", ())
                if isinstance(value, str)
            }
            relation = (
                "按5分钟同级或更高级别卖点完整退出规则"
                if "SAME_OR_HIGHER_STRUCTURE_FULL_EXIT" in reasons
                else "按5分钟低级别或不同结构卖点段差规则"
                if "LOWER_STRUCTURE_SEGMENT_DIFFERENCE_REDUCTION" in reasons
                else "按已核对的5分钟结构级别规则"
            )
            return (
                f"风险参考：结构退出比例 {displayed}%"
                + (f"（精确模型值 {model_value}%）" if model_value else "")
                + f"（{relation}；仅作结构模型比较）"
            )
    if status == "CONDITIONAL" and side == "sell":
        return "风险参考：卖点与目标结构级别待核对；关系未确认前不生成退出比例"
    if status == "BLOCKED" and side == "buy":
        return (
            "风险参考：本条买入不纳入操作计划（"
            f"{_blocked_position_reason_text(signal, recommendation)}）"
        )
    if status == "BLOCKED" and side == "sell":
        return (
            "风险参考：本条信号不再计算退出比例（"
            f"{_blocked_position_reason_text(signal, recommendation)}）"
        )
    if status == "NOT_ACTIONABLE":
        basis = str(recommendation.get("basis") or "")
        if basis == ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_BASIS:
            return (
                "风险参考：暂不计算（5分钟买卖点已确认，"
                "等待1分钟区间套精确定位）"
            )
        if side == "buy":
            state = (
                "5分钟买点仅为几何候选，尚未达到操作确认"
                if basis == "GEOMETRIC_5M_CANDIDATE_AWAITING_CONFIRMATION"
                else "5分钟买点仍在形成"
            )
            return f"风险参考：暂不计算（{state}）"
        return "风险参考：暂不计算（5分钟卖点尚未达到操作确认）"
    if status == "UNRESOLVED":
        if side == "buy" and signal.get("realtime_quote_status") == "unavailable":
            return (
                "风险参考：暂不计算（实时价格未取得；"
                "不使用已完成K线价格生成买入比例）"
            )
        return (
            "风险参考：暂不计算（结构价格或风险参数不足，待人工核对）"
            if side == "buy"
            else "风险参考：退出比例待核对结构级别"
        )
    label = str(recommendation.get("label") or "").strip()
    if label:
        if any(
            term in label
            for term in (
                "账户",
                "权益",
                "资金",
                "现金",
                "持仓",
                "仓位",
                "持有数量",
                "组合热度",
            )
        ):
            return "风险参考：待人工核对结构价格与风险参数"
        return "风险参考：" + label.removeprefix("建议：")
    return (
        "风险参考：买入比例待计算（需核对结构价格与风险参数）"
        if side == "buy"
        else "风险参考：退出比例待核对结构级别"
        if side == "sell"
        else "风险参考：待人工复核"
    )


def _same_period_context_text(
    context: Mapping[str, object],
    label: str,
) -> str:
    evidence = _mapping(context.get("same_period_technical_evidence"))
    if not evidence:
        return f"{label}：证据暂不可用"
    relation_labels = {
        "ma5_above_ma10": "MA5>MA10",
        "ma5_below_ma10": "MA5<MA10",
        "equal": "MA5=MA10",
        "unresolved": "均线关系待判定",
    }
    fractal_labels = {"top": "顶分型", "bottom": "底分型", "none": "分型待判定"}
    fractal_state_labels = {
        "forming": "形成中",
        "confirmed": "已确认",
        "pen_endpoint_pending_lock": "笔端点待锁定",
        "pen_locked": "已延伸为锁定笔",
        "continuation": "延续中",
        "unresolved": "待判定",
    }
    ma5 = _text(evidence.get("ma5"), "不足")
    ma10 = _text(evidence.get("ma10"), "不足")
    fractal = fractal_labels.get(str(evidence.get("fractal_type")), "分型待判定")
    state = fractal_state_labels.get(
        str(evidence.get("fractal_state")),
        "待判定",
    )
    return (
        f"{label}：MA5 {ma5}｜MA10 {ma10}｜"
        f"{relation_labels.get(str(evidence.get('ma5_vs_ma10')), '均线关系待判定')}｜"
        f"{fractal}{state}"
    )


def _operation_status_text(
    signal: Mapping[str, object],
    *,
    side: str,
    new_stage: str,
    operational_status: str,
    operational_reason_codes: set[str],
    recommendation: str,
    detected_at: object | None = None,
) -> str:
    if new_stage == "invalidated":
        return "已失效，不再按原结构操作"
    if new_stage == "closed":
        return "跟踪结束"
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        entry_state, _entry_price_cap = _buy_entry_guidance_state(
            signal,
            detected_at=detected_at,
        )
        if entry_state == "price_above_cap":
            return "1分钟定位有效，但价格超过买入上限"
        if entry_state == "unavailable":
            return "1分钟定位已确认，但执行上限缺失"
        boundary_status = segment_difference_boundary_status(
            signal,
            evaluated_at=detected_at,
        )
        if boundary_status == "current":
            return "1分钟区间套已确认，精确执行候选已解锁"
        if boundary_status == "expired":
            return "1分钟区间套已确认，但定位窗口已过"
        if boundary_status == "not_applicable":
            return "1分钟卖出区间套已确认，等待人工复核"
        return "1分钟区间套已确认，定位边界待人工核对"
    if side == "buy":
        entry_state, _entry_price_cap = _buy_entry_guidance_state(
            signal,
            detected_at=detected_at,
        )
        if entry_state == "waiting":
            return "5分钟信号已确认，等待1分钟区间套"
        if entry_state == "expired":
            return "5分钟信号保留，1分钟定位窗口已过"
        if entry_state == "unavailable":
            return "5分钟信号保留，1分钟执行上限缺失"
        if entry_state == "price_above_cap":
            return "禁止追价（超过1分钟买入上限）"
        if operational_status == "BLOCKED":
            return (
                "禁止买入（0%保护）"
                if operational_reason_codes
                else "禁止买入（具体限制原因待核对）"
            )
        if operational_status in {"UNRESOLVED", "NOT_ACTIONABLE"}:
            return (
                "5分钟信号已确认，等待1分钟区间套"
                if operational_status == "NOT_ACTIONABLE"
                else "仅观察，结构风险待核对"
            )
        if recommendation == "CAUTION" or (
            not recommendation and signal.get("entry_allowed") is not True
        ):
            return "仅观察，待人工复核"
        return "可人工复核执行"
    if side == "sell":
        if operational_status == "BLOCKED":
            return "卖出结构保留，当前精确执行已关闭"
        if operational_status == "NOT_ACTIONABLE":
            return "5分钟卖点已确认，等待1分钟区间套"
        return "卖出或退出复核"
    return "待人工复核"


def _approaching_digest_sort_key(
    signal: Mapping[str, object],
) -> tuple[object, ...]:
    sources = signal.get("selection_sources")
    source_values = (
        sources if isinstance(sources, (list, tuple, set, frozenset)) else ()
    )
    manual_attention = bool(
        {str(value) for value in source_values} & _MANUAL_ATTENTION_SOURCES
    )
    side = str(
        signal.get("side") or _mapping(signal.get("setup_5m")).get("side") or ""
    )
    point_type = str(
        _mapping(signal.get("setup_5m")).get("point_type")
        or signal.get("point_type")
        or ""
    )
    event_at = _parse_time(
        _mapping(signal.get("setup_5m")).get("available_at")
        or signal.get("observed_at")
    )
    return (
        not manual_attention,
        side != "sell",
        _SETUP_POINT_ORDER.get(point_type, 99),
        -(event_at.timestamp() if event_at is not None else 0.0),
        str(signal.get("code") or ""),
    )


def _preconfirmation_divergence_sort_key(
    signal: Mapping[str, object],
) -> tuple[object, ...]:
    divergence = _mapping(
        signal.get(_NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD)
    )
    side = str(
        signal.get("side") or _mapping(signal.get("setup_5m")).get("side") or ""
    )
    point_type = str(divergence.get("point_type") or "")
    available_at = _parse_time(
        divergence.get("available_at") or divergence.get("confirmed_at")
    )
    return (
        side != "sell",
        _SETUP_POINT_ORDER.get(point_type, 99),
        -(available_at.timestamp() if available_at is not None else 0.0),
        str(signal.get("code") or ""),
        str(divergence.get("point_id") or ""),
    )


def format_preconfirmation_divergence_digest(
    signals: Sequence[Mapping[str, object]],
    *,
    total_count: int | None = None,
    buy_count: int | None = None,
    sell_count: int | None = None,
    detected_at: object | None = None,
) -> tuple[str, list[str]]:
    """Render confirmed 1m divergences whose 5m parent is not yet confirmed."""

    candidates = tuple(
        sorted(signals, key=_preconfirmation_divergence_sort_key)
    )
    if not candidates:
        raise ValueError("preconfirmation divergence digest requires candidates")
    total = len(candidates) if total_count is None else int(total_count)
    if total < len(candidates):
        raise ValueError(
            "preconfirmation divergence total cannot be smaller than its rows"
        )
    computed_buy = sum(
        str(value.get("side") or _mapping(value.get("setup_5m")).get("side"))
        == "buy"
        for value in candidates
    )
    computed_sell = sum(
        str(value.get("side") or _mapping(value.get("setup_5m")).get("side"))
        == "sell"
        for value in candidates
    )
    buys = computed_buy if buy_count is None else int(buy_count)
    sells = computed_sell if sell_count is None else int(sell_count)
    if buys < 0 or sells < 0 or buys + sells != total:
        raise ValueError("preconfirmation divergence side counts are invalid")

    visible = candidates[:_PRECONFIRMATION_DIVERGENCE_DIGEST_MAX_ITEMS]
    title = f"买卖通知｜1分钟背驰预警·5分钟未确认｜{total}个"
    lines = [
        (
            f"结论：发现 {total} 个已确认的1分钟背驰；对应5分钟结构仍未确认，"
            "这是提前观察，不是正式买卖点，不可据此操作"
        ),
        (
            f"范围：买入方向 {buys}｜卖出方向 {sells}｜"
            f"本条展示 {len(visible)} 个"
        ),
    ]
    for index, signal in enumerate(visible, start=1):
        setup = _mapping(signal.get("setup_5m"))
        divergence = _mapping(
            signal.get(_NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD)
        )
        code = _text(signal.get("code"), "代码待核对")
        name = _text(signal.get("name"), code)
        identity = code if name == code else f"{name}（{code}）"
        divergence_label = _point_label(
            divergence.get("point_type"),
            "背驰点",
        )
        divergence_kind = _DIVERGENCE_LABELS.get(
            str(divergence.get("divergence_kind") or ""),
            "背驰",
        )
        setup_label = _point_label(
            setup.get("point_type") or signal.get("point_type"),
            "结构候选",
        )
        available_at = _parse_time(
            divergence.get("available_at") or divergence.get("confirmed_at")
        )
        available_text = (
            available_at.astimezone(CN).strftime("%H:%M")
            if available_at is not None
            else "时间待核对"
        )
        five_minute_state = (
            "几何候选待确认"
            if _stage(signal) == "formed"
            else "形成中"
        )
        lines.append(
            f"预警{index}：{identity}｜1分钟{divergence_label}（{divergence_kind}）"
            f"｜确认 {available_text}｜对应5分钟{setup_label}{five_minute_state}"
        )
    omitted = total - len(visible)
    if omitted:
        lines.append(
            f"其余：还有 {omitted} 个1分钟背驰预警，请在早盘筛选页查看"
        )
    detected = _parse_time(detected_at) or _parse_time(
        candidates[0].get("monitor_observed_at")
        or candidates[0].get("observed_at")
    )
    lines.extend(
        [
            (
                "等待：5分钟结构正式确认后，系统会另发正式买卖通知；"
                "若5分钟结构失效，本预警不构成买卖点"
            ),
            (
                "时效：汇总于 "
                + (
                    detected.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")
                    if detected is not None
                    else "暂不可用"
                )
                + "｜同一1分钟背驰发生点只通知一次"
            ),
            "说明：仅供人工观察；系统不会自动下单",
        ]
    )
    return title, lines


def format_approaching_digest(
    signals: Sequence[Mapping[str, object]],
    *,
    total_count: int | None = None,
    buy_count: int | None = None,
    sell_count: int | None = None,
    detected_at: object | None = None,
) -> tuple[str, list[str]]:
    """Render one bounded, explicitly non-actionable forming-structure digest."""

    candidates = tuple(sorted(signals, key=_approaching_digest_sort_key))
    if not candidates:
        raise ValueError("approaching digest requires at least one candidate")
    total = len(candidates) if total_count is None else int(total_count)
    if total < len(candidates):
        raise ValueError("approaching digest total cannot be smaller than its rows")
    computed_buy = sum(
        str(value.get("side") or _mapping(value.get("setup_5m")).get("side"))
        == "buy"
        for value in candidates
    )
    computed_sell = sum(
        str(value.get("side") or _mapping(value.get("setup_5m")).get("side"))
        == "sell"
        for value in candidates
    )
    buys = computed_buy if buy_count is None else int(buy_count)
    sells = computed_sell if sell_count is None else int(sell_count)
    if buys < 0 or sells < 0 or buys + sells != total:
        raise ValueError("approaching digest side counts are invalid")

    visible = candidates[:_APPROACHING_DIGEST_MAX_ITEMS]
    title = f"买卖通知｜结构预警·尚未确认｜{total}个候选"
    lines = [
        (
            f"结论：发现 {total} 个5分钟结构接近形成；"
            "全部尚未确认，不是正式买卖点，不可据此操作"
        ),
        (
            f"范围：买入候选 {buys}｜卖出候选 {sells}｜"
            f"本条展示 {len(visible)} 个"
        ),
    ]
    for index, signal in enumerate(visible, start=1):
        setup = _mapping(signal.get("setup_5m"))
        code = _text(signal.get("code"), "代码待核对")
        name = _text(signal.get("name"), code)
        identity = code if name == code else f"{name}（{code}）"
        point = _point_label(
            setup.get("point_type") or signal.get("point_type"),
            "结构候选",
        )
        event_at = _parse_time(
            setup.get("available_at")
            or setup.get("confirmed_at")
            or signal.get("observed_at")
        )
        observed_text = (
            event_at.astimezone(CN).strftime("%H:%M")
            if event_at is not None
            else "时间待核对"
        )
        sector = _text(_mapping(signal.get("sector")).get("sector_name"), "")
        sector_text = f"｜{sector}" if sector else ""
        lines.append(
            f"候选{index}：{identity}｜5分钟{point}｜形成中｜{observed_text}"
            f"{sector_text}"
        )
    omitted = total - len(visible)
    if omitted:
        lines.append(
            f"其余：还有 {omitted} 个候选，请在早盘筛选页查看；"
            "摘要不替代正式通知"
        )
    detected = _parse_time(detected_at) or _parse_time(
        candidates[0].get("monitor_observed_at")
        or candidates[0].get("observed_at")
    )
    lines.extend(
        [
            (
                "等待：只有5分钟结构正式确认后，系统才会另发正式买卖通知；"
                "候选失效不代表卖点"
            ),
            (
                "时效：汇总于 "
                + (
                    detected.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")
                    if detected is not None
                    else "暂不可用"
                )
                + "｜同一结构只预警一次｜15分钟内最多一条摘要"
            ),
            "说明：仅用于确认实时监控正在工作；系统不会自动下单",
        ]
    )
    return title, lines


def format_notification(
    signal: Mapping[str, object],
    old_stage: str,
    new_stage: str,
    *,
    detected_at: object | None = None,
) -> tuple[str, list[str]]:
    context = _mapping(signal.get("context_30m"))
    daily_context = _mapping(signal.get("context_d"))
    execution_profile = _mapping(signal.get("execution_profile"))
    recommendation = str(execution_profile.get("recommendation") or "")
    setup = _mapping(signal.get("setup_5m"))
    trigger = _mapping(signal.get("segment_difference_1m"))
    sector = _mapping(signal.get("sector"))
    code = _text(signal.get("code"))
    name = _text(signal.get("name"), code)
    raw_setup_points = signal.get("notification_setup_point_types")
    setup_points = (
        tuple(
            sorted(
                {
                    str(value)
                    for value in raw_setup_points
                    if isinstance(value, str) and value
                },
                key=lambda value: (_SETUP_POINT_ORDER.get(value, 99), value),
            )
        )
        if isinstance(raw_setup_points, (list, tuple, set, frozenset))
        else ()
    )
    setup_point = (
        "、".join(_point_label(value) for value in setup_points) + "共振"
        if len(setup_points) > 1
        else _point_label(
            setup.get("point_type") or signal.get("point_type"),
            "结构信号",
        )
    )
    trigger_point = _point_label(trigger.get("point_type"))
    trigger_divergence = _DIVERGENCE_LABELS.get(
        str(trigger.get("divergence_kind") or "")
    )
    trigger_evidence = (
        f"{trigger_point}（{trigger_divergence}）"
        if trigger_divergence
        else trigger_point
    )
    scope = _scope_label(signal)
    effective_point_type = setup.get("point_type") or signal.get("point_type")
    old_stage_label = _stage_label(old_stage)
    new_stage_label = _stage_label(new_stage)
    side = str(signal.get("side") or setup.get("side") or "")
    operational_position = _notification_position_recommendation(
        signal,
        detected_at=detected_at,
        new_stage=new_stage,
    )
    operational_status = str(operational_position.get("status") or "")
    operational_reason_codes = {
        str(value)
        for value in operational_position.get("reason_codes", ())
        if isinstance(value, str)
    }
    entry_guidance_state, _entry_price_cap = _buy_entry_guidance_state(
        signal,
        detected_at=detected_at,
    )
    if new_stage in {"invalidated", "closed"}:
        headline = new_stage_label
        notification_kind = "信号失效" if new_stage == "invalidated" else "跟踪结束"
    elif new_stage == _SEGMENT_ENRICHED_STAGE:
        headline = f"5分钟{setup_point}＋1分钟区间套{trigger_evidence}"
        notification_kind = (
            "1分钟定位已过期"
            if side == "buy" and entry_guidance_state == "expired"
            else "1分钟执行边界缺失"
            if side == "buy" and entry_guidance_state == "unavailable"
            else "1分钟定位·禁止追价"
            if side == "buy" and entry_guidance_state == "price_above_cap"
            else "1分钟精确定位新出现"
        )
    else:
        headline = f"5分钟{setup_point}"
        notification_kind = (
            "买点确认·等待1分钟定位"
            if side == "buy" and entry_guidance_state == "waiting"
            else "买点确认·1分钟定位过期"
            if side == "buy" and entry_guidance_state == "expired"
            else "买点确认·执行边界缺失"
            if side == "buy" and entry_guidance_state == "unavailable"
            else "买点确认·禁止追价"
            if side == "buy" and entry_guidance_state == "price_above_cap"
            else "买点确认·0%保护"
            if side == "buy"
            and operational_status == "BLOCKED"
            and operational_reason_codes
            & {
                "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",
                "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",
            }
            else "新买点·待人工确认"
            if side == "buy"
            and operational_status == "RECOMMENDED"
            and (
                recommendation == "READY"
                or not recommendation
                and signal.get("entry_allowed") is True
            )
            else "买点观察·待人工复核"
            if side == "buy"
            else "新卖点·退出复核"
            if side == "sell"
            else "结构信号"
        )
    title = f"买卖通知｜{notification_kind}｜{scope}｜{code}｜{headline}"

    direction = _localized(context.get("direction"), _DIRECTION_LABELS)
    disposition = _localized(
        context.get("disposition"),
        _DISPOSITION_LABELS,
        "",
    )
    context_text = f"30分钟{direction}"
    if disposition:
        context_text += f"（{disposition}）"
    available_at_value = (
        setup.get("available_at")
        or setup.get("confirmed_at")
        or signal.get("observed_at")
    )
    event_available_at_value = available_at_value
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        event_available_at_value = _notification_evidence_time(
            signal,
            new_stage=new_stage,
        )
    confirmed_at_value = setup.get("confirmed_at") or available_at_value
    detected_at_value = (
        detected_at or signal.get("monitor_observed_at") or signal.get("observed_at")
    )
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        timeline_items = [
            ("1分钟定位确认", _segment_time(signal)),
            ("原5分钟确认", confirmed_at_value),
        ]
    else:
        confirmation_label = (
            "原5分钟确认"
            if new_stage in {"invalidated", "closed"}
            else "5分钟确认"
        )
        timeline_items = [(confirmation_label, confirmed_at_value)]
        if _time_identity(available_at_value) != _time_identity(confirmed_at_value):
            timeline_items.append(("信号可用", available_at_value))
    timeline_items.append(("监听发现", detected_at_value or available_at_value))
    time_parts = _notification_timeline_parts(tuple(timeline_items))
    time_parts[-1] += (
        f"（延迟 {_elapsed_text(event_available_at_value, detected_at_value)}）"
    )
    operation_status = _operation_status_text(
        signal,
        side=side,
        new_stage=new_stage,
        operational_status=operational_status,
        operational_reason_codes=operational_reason_codes,
        recommendation=recommendation,
        detected_at=detected_at,
    )
    action_advice = _action_advice(
        signal,
        point_type=effective_point_type,
        scope=scope,
        new_stage=new_stage,
        detected_at=detected_at,
    )
    identity = code if name == code else f"{name}（{code}）"
    lines = [
        "结论：" + action_advice.removeprefix("操作："),
        (
            f"标的：{identity}｜状态：{operation_status}｜"
            f"进度：{old_stage_label}→{new_stage_label}"
        ),
        _judgment_checklist_line(
            signal,
            setup_point=setup_point,
            trigger_evidence=trigger_evidence,
            new_stage=new_stage,
            detected_at=detected_at,
        ),
        _execution_boundary_line(
            signal,
            setup,
            new_stage=new_stage,
            detected_at=detected_at,
        ),
    ]
    if new_stage != "closed":
        lines.append(_execution_defense_snapshot(signal, setup))
    lines.extend(
        [
            _position_recommendation_line(
                signal,
                detected_at=detected_at,
                new_stage=new_stage,
            ),
            "时效：" + "｜".join(time_parts),
            (
                f"背景：{context_text}｜"
                f"环境：{_text(execution_profile.get('context_grade_label'), '待判定')}｜"
                f"板块：{_text(sector.get('sector_name'))}"
            ),
        ]
    )
    terminal_segment = _terminal_segment_text(setup)
    if terminal_segment != "末端线段：血缘暂不可用":
        lines.append(terminal_segment)
    technical_parts = [
        _same_period_context_text(source, label).replace("：", " ", 1)
        for source, label in ((daily_context, "日线"), (context, "30分钟"))
        if _mapping(source.get("same_period_technical_evidence"))
    ]
    if technical_parts:
        lines.append("技术：" + "｜".join(technical_parts))
    lines.append(
        "说明：仅供人工复核；如需操作，请在其他交易软件手工决定并完成；"
        "系统不会自动下单"
    )
    return title, lines


def _valid_screening_session(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def screening_completion_event_id(market_data_session: str) -> str:
    """Return the stable once-per-market-session completion identity."""

    if not _valid_screening_session(market_data_session):
        raise ValueError("market_data_session must be an ISO date")
    payload = json.dumps(
        {
            "schema": _SCREENING_COMPLETION_EVENT_SCHEMA,
            "strategy_id": STRATEGY_ID,
            "market_data_session": market_data_session,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _screening_deferred_exclusions_are_complete(
    manifest: Mapping[str, object],
    audit: Mapping[str, object],
    *,
    discovered_count: int,
    completed_count: int,
    excluded_count: int,
    next_epoch_retry_count: int,
) -> bool:
    """Authenticate deterministic exclusions carried into the next data epoch.

    A completed coverage cycle may legitimately exclude a suspended symbol or
    one without the frozen minimum history.  Those symbols remain in the
    next-market-data-epoch queue, but they are resolved dispositions for the
    current cycle rather than failed or pending work.  Accept that state only
    when the canonical manifest proves the relationship one-for-one.
    """

    if next_epoch_retry_count == 0:
        return True
    raw_discovered = manifest.get("discovered_codes")
    raw_completed = manifest.get("completed_codes")
    raw_excluded = manifest.get("excluded_codes")
    raw_failed = manifest.get("failed_codes")
    raw_exclusions = manifest.get("exclusions")
    raw_pending = manifest.get("pending_frequencies")
    raw_backoff = manifest.get("backoff_frequencies")
    raw_deferred = manifest.get("deferred_frequencies")
    if (
        not isinstance(raw_discovered, list)
        or not isinstance(raw_completed, list)
        or not isinstance(raw_excluded, list)
        or raw_failed != []
        or not isinstance(raw_exclusions, list)
        or raw_pending != {}
        or raw_backoff != {}
        or not isinstance(raw_deferred, Mapping)
        or audit.get("retry_symbol_count") != next_epoch_retry_count
    ):
        return False

    def canonical_codes(values: list[object]) -> tuple[str, ...] | None:
        if any(not isinstance(value, str) or not value for value in values):
            return None
        codes = tuple(str(value) for value in values)
        return codes if codes == tuple(sorted(set(codes))) else None

    discovered = canonical_codes(raw_discovered)
    completed = canonical_codes(raw_completed)
    excluded = canonical_codes(raw_excluded)
    if (
        discovered is None
        or completed is None
        or excluded is None
        or len(discovered) != discovered_count
        or len(completed) != completed_count
        or len(excluded) != excluded_count
        or len(excluded) != next_epoch_retry_count
        or set(discovered) != set(completed) | set(excluded)
        or set(completed) & set(excluded)
        or set(raw_deferred) != set(excluded)
    ):
        return False

    valid_frequencies = {"d", "30m", "5m", "1m"}
    for code, frequencies in raw_deferred.items():
        if (
            not isinstance(code, str)
            or not isinstance(frequencies, list)
            or not frequencies
            or any(
                not isinstance(value, str) or value not in valid_frequencies
                for value in frequencies
            )
            or frequencies != list(dict.fromkeys(frequencies))
        ):
            return False

    exclusion_fields = {
        "code",
        "exclusion_type",
        "eligibility",
        "reason_code",
        "retry_policy",
        "deterministic_for_coverage_epoch",
        "remote_error_type",
        "reason",
    }
    exclusion_codes: list[str] = []
    for document in raw_exclusions:
        if not isinstance(document, Mapping) or set(document) != exclusion_fields:
            return False
        code = document.get("code")
        if (
            not isinstance(code, str)
            or not code
            or document.get("exclusion_type") != "stock_analysis_exclusion"
            or not isinstance(document.get("eligibility"), str)
            or not document.get("eligibility")
            or not isinstance(document.get("reason_code"), str)
            or not document.get("reason_code")
            or document.get("retry_policy") != "NEXT_MARKET_DATA_EPOCH"
            or document.get("deterministic_for_coverage_epoch") is not True
            or not isinstance(document.get("remote_error_type"), str)
            or not document.get("remote_error_type")
            or not isinstance(document.get("reason"), str)
            or not document.get("reason")
        ):
            return False
        exclusion_codes.append(code)
    return tuple(exclusion_codes) == excluded


def _screening_completion_details(
    snapshot: Mapping[str, object],
) -> dict[str, object] | None:
    """Validate and summarize one genuinely completed daily selection cycle."""

    audit = _mapping(snapshot.get("scan_audit"))
    quality = _mapping(snapshot.get("data_quality"))
    manifest = _mapping(snapshot.get("coverage_manifest"))
    market_data_as_of = _parse_time(snapshot.get("market_data_as_of"))
    coverage_epoch_id = snapshot.get("coverage_epoch_id")
    raw_signals = snapshot.get("signals")
    failure_codes = quality.get("failure_codes")
    errors = snapshot.get("errors")
    if (
        snapshot.get("available") is not True
        or snapshot.get("scan_state") != "complete"
        or snapshot.get("last_batch_state") != "complete"
        or snapshot.get("full_coverage_state") != "complete"
        or snapshot.get("read_only") is not True
        or snapshot.get("no_order_execution") is not True
        or manifest.get("complete") is not True
        or audit.get("coverage_cycle_complete") is not True
        or audit.get("monitoring_only_refresh") is not False
        or quality.get("complete") is not True
        or quality.get("stale") is not False
        or not isinstance(failure_codes, (list, tuple))
        or bool(failure_codes)
        or not isinstance(errors, (list, tuple))
        or bool(errors)
        or market_data_as_of is None
        or market_data_as_of.hour < _SCREENING_COMPLETION_CLOSE_HOUR
        or not isinstance(coverage_epoch_id, str)
        or not coverage_epoch_id
        or not isinstance(raw_signals, list)
        or any(not isinstance(value, Mapping) for value in raw_signals)
    ):
        return None

    count_fields = {
        "discovered_symbol_count": audit.get("discovered_symbol_count"),
        "completed_symbol_count": audit.get(
            "coverage_cycle_completed_symbol_count"
        ),
        "excluded_symbol_count": audit.get(
            "coverage_cycle_excluded_symbol_count"
        ),
        "failed_symbol_count": audit.get("coverage_cycle_failed_symbol_count"),
        "pending_symbol_count": audit.get("pending_symbol_count"),
        "immediate_pending_symbol_count": audit.get(
            "immediate_pending_symbol_count"
        ),
        "backoff_retry_symbol_count": audit.get("backoff_retry_symbol_count"),
        "next_epoch_retry_symbol_count": audit.get(
            "next_epoch_retry_symbol_count"
        ),
    }
    if any(type(value) is not int or value < 0 for value in count_fields.values()):
        return None
    if (
        count_fields["failed_symbol_count"] != 0
        or count_fields["pending_symbol_count"] != 0
        or count_fields["immediate_pending_symbol_count"] != 0
        or count_fields["backoff_retry_symbol_count"] != 0
        or count_fields["discovered_symbol_count"]
        != count_fields["completed_symbol_count"]
        + count_fields["excluded_symbol_count"]
        or not _screening_deferred_exclusions_are_complete(
            manifest,
            audit,
            discovered_count=count_fields["discovered_symbol_count"],
            completed_count=count_fields["completed_symbol_count"],
            excluded_count=count_fields["excluded_symbol_count"],
            next_epoch_retry_count=count_fields["next_epoch_retry_symbol_count"],
        )
    ):
        return None

    signals = tuple(dict(value) for value in raw_signals)
    sides = tuple(
        str(value.get("side") or _mapping(value.get("setup_5m")).get("side"))
        for value in signals
    )
    if any(value not in {"buy", "sell"} for value in sides):
        return None
    return {
        "market_data_session": market_data_as_of.date().isoformat(),
        "market_data_as_of": market_data_as_of,
        "coverage_epoch_id": coverage_epoch_id,
        **count_fields,
        "selected_symbol_count": len(
            {
                str(value.get("code"))
                for value in signals
                if isinstance(value.get("code"), str) and value.get("code")
            }
        ),
        "signal_count": len(signals),
        "buy_count": sides.count("buy"),
        "sell_count": sides.count("sell"),
    }


def format_screening_completion(
    snapshot: Mapping[str, object],
    *,
    completed_at: object,
) -> tuple[str, list[str]]:
    """Render a concise, explicitly non-actionable daily completion receipt."""

    details = _screening_completion_details(snapshot)
    completion_time = _parse_time(completed_at)
    if details is None:
        raise ValueError("screening snapshot is not a complete daily selection")
    if completion_time is None:
        raise ValueError("completed_at must be timezone-aware")
    session = str(details["market_data_session"])
    title = f"买卖通知｜每日选股完成｜{session}"
    next_epoch_retry_text = (
        f"｜下周期复查 {details['next_epoch_retry_symbol_count']} 只"
        if details["next_epoch_retry_symbol_count"]
        else ""
    )
    lines = [
        f"结论：{session} 日终选股已完成，本轮完整结果已生成。",
        (
            "范围："
            f"发现 {details['discovered_symbol_count']} 只｜"
            f"完成 {details['completed_symbol_count']} 只｜"
            f"排除 {details['excluded_symbol_count']} 只｜"
            f"失败 {details['failed_symbol_count']} 只｜待处理 0 只"
            f"{next_epoch_retry_text}"
        ),
        (
            "结果："
            f"入选标的 {details['selected_symbol_count']} 只｜"
            f"结构 {details['signal_count']} 个｜"
            f"买入方向 {details['buy_count']} 个｜"
            f"卖出方向 {details['sell_count']} 个"
        ),
        (
            "时间："
            f"行情截止 {_notification_time_text(details['market_data_as_of'])}｜"
            f"任务完成 {_notification_time_text(completion_time)}"
        ),
        (
            "说明：这是选股任务完成回执，不是买卖建议；具体买卖点仍以独立实时通知为准；"
            "系统不会自动下单"
        ),
    ]
    return title, lines


class SignalNotificationDispatcher:
    def __init__(
        self,
        notifier: object | None,
        *,
        state_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        review_inbox: object | None = None,
        quote_provider: Callable[[str], object] | None = None,
    ) -> None:
        send = getattr(notifier, "send", None)
        if notifier is not None and not callable(send):
            raise TypeError("notifier must expose send")
        if notifier is None and review_inbox is None:
            raise TypeError("notifier or review_inbox is required")
        self._notifier = notifier
        self._state_path = None if state_path is None else Path(state_path)
        self._clock = clock or (lambda: datetime.now(CN))
        if review_inbox is not None and not callable(
            getattr(review_inbox, "record", None)
        ):
            raise TypeError("review_inbox must expose record")
        self._review_inbox = review_inbox
        if quote_provider is not None and not callable(quote_provider):
            raise TypeError("quote_provider must be callable")
        self._quote_provider = quote_provider
        self._log = logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._state_load_error: str | None = None
        state = self._load_state()
        self._delivered = set(state["delivered_event_ids"])
        self._delivered_segment_evidence = set(
            state["delivered_segment_evidence_ids"]
        )
        self._success_count = int(state["success_count"])
        self._failure_count = int(state["failure_count"])
        self._last_success_at = state["last_success_at"]
        self._last_success_event_id = state["last_success_event_id"]
        self._last_failure_at = state["last_failure_at"]
        self._last_failure_reason = state["last_failure_reason"]
        self._suppressed_count = int(state["suppressed_count"])
        self._last_suppressed_at = state["last_suppressed_at"]
        self._last_suppressed_reason = state["last_suppressed_reason"]
        self._event_audit = list(state["event_audit"])
        self._suppressed_fingerprints = dict.fromkeys(state["suppressed_fingerprints"])
        self._pending_trigger_events = dict(state["pending_trigger_events"])
        self._approaching_alerted_occurrences = dict(
            state["approaching_alerted_occurrences"]
        )
        self._last_approaching_digest_at = state["last_approaching_digest_at"]
        pending_approaching_digest = state["pending_approaching_digest"]
        self._pending_approaching_digest = (
            None
            if pending_approaching_digest is None
            else dict(pending_approaching_digest)
        )
        self._preconfirmation_divergence_alerted_occurrences = dict(
            state["preconfirmation_divergence_alerted_occurrences"]
        )
        pending_preconfirmation_divergence_digest = state[
            "pending_preconfirmation_divergence_digest"
        ]
        self._pending_preconfirmation_divergence_digest = (
            None
            if pending_preconfirmation_divergence_digest is None
            else dict(pending_preconfirmation_divergence_digest)
        )
        self._screening_completion_sessions = dict(
            state["screening_completion_sessions"]
        )
        self._pending_screening_completions = {
            str(session): dict(value)
            for session, value in state["pending_screening_completions"].items()
        }

    def set_quote_provider(
        self,
        quote_provider: Callable[[str], object] | None,
    ) -> None:
        if quote_provider is not None and not callable(quote_provider):
            raise TypeError("quote_provider must be callable")
        with self._lock:
            self._quote_provider = quote_provider

    def _with_realtime_price(
        self,
        document: Mapping[str, object],
        *,
        detected_at: object | None = None,
        new_stage: str = "triggered",
    ) -> dict[str, object]:
        enriched = dict(document)
        # 每次尝试都以本次最终可见价格重算，不沿用失败重试中缓存的通知投影。
        enriched.pop("notification_position_recommendation", None)
        # The screening bundle carries the latest completed price but not the
        # winning source frequency.  If the realtime quote fails, do not
        # invent a 1-minute provenance: the bundle may have fallen back to a
        # completed 5-minute bar.
        enriched.setdefault("current_price_source", "latest_completed_bar_close")
        code = str(enriched.get("code") or "").strip()
        if self._quote_provider is not None and code:
            enriched["realtime_quote_status"] = "unavailable"
            try:
                quote = self._quote_provider(code)
                raw_price = (
                    quote.get("last")
                    if isinstance(quote, Mapping)
                    else getattr(quote, "last")
                )
                live_price = float(raw_price)
                if live_price <= 0 or not isfinite(live_price):
                    raise ValueError("realtime quote price is invalid")
            except Exception as exc:
                self._log.warning(
                    "A-share notification realtime quote unavailable code=%s: %s: %s",
                    code,
                    type(exc).__name__,
                    str(exc)[:120],
                )
            else:
                enriched["current_price"] = live_price
                enriched["current_price_source"] = "realtime_tick"
                enriched["realtime_quote_status"] = "verified"
                enriched["current_price_at"] = (
                    detected_at.isoformat()
                    if isinstance(detected_at, datetime)
                    else str(detected_at or "")
                )
        enriched["notification_position_recommendation"] = dict(
            _notification_position_recommendation(
                enriched,
                detected_at=detected_at,
                new_stage=new_stage,
            )
        )
        return enriched

    def _record_review_notification(
        self,
        *,
        event_id: str,
        document: Mapping[str, object],
        old_stage: str,
        new_stage: str,
        delivery_status: str,
        detected_at: datetime | str | None = None,
        delivery_reason: str | None = None,
    ) -> bool:
        if self._review_inbox is None:
            return True
        try:
            event = a_share_notification_event(
                event_id=event_id,
                document=document,
                old_stage=old_stage,
                new_stage=new_stage,
                delivery_status=delivery_status,
                delivery_reason=delivery_reason,
                recorded_at=self._now(),
                detected_at=detected_at,
            )
            self._review_inbox.record(event)
            return True
        except Exception:
            # The review record is part of the local durability boundary.  The
            # caller retains this signal in its retry ledger and must not send
            # externally until the record is durable.
            self._log.exception("failed to record realtime review notification")
            return False

    def _load_state(self) -> dict[str, object]:
        empty = {
            "delivered_event_ids": (),
            "delivered_segment_evidence_ids": (),
            "success_count": 0,
            "failure_count": 0,
            "last_success_at": None,
            "last_success_event_id": None,
            "last_failure_at": None,
            "last_failure_reason": None,
            "suppressed_count": 0,
            "last_suppressed_at": None,
            "last_suppressed_reason": None,
            "event_audit": (),
            "suppressed_fingerprints": (),
            "pending_trigger_events": {},
            "approaching_alerted_occurrences": {},
            "last_approaching_digest_at": None,
            "pending_approaching_digest": None,
            "preconfirmation_divergence_alerted_occurrences": {},
            "pending_preconfirmation_divergence_digest": None,
            "screening_completion_sessions": {},
            "pending_screening_completions": {},
        }

        def invalid(reason: str) -> dict[str, object]:
            self._state_load_error = reason
            return empty

        if self._state_path is None:
            return empty
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return invalid(f"STATE_READ_{type(exc).__name__.upper()}")
        required_state_fields = frozenset(
            {
                "schema",
                "delivered_event_ids",
                "success_count",
                "failure_count",
                "last_success_at",
                "last_success_event_id",
                "last_failure_at",
                "last_failure_reason",
                "suppressed_count",
                "last_suppressed_at",
                "last_suppressed_reason",
                "event_audit",
                "pending_trigger_events",
            }
        )
        optional_state_fields = frozenset(
            {
                "suppressed_fingerprints",
                "delivered_segment_evidence_ids",
                "approaching_alerted_occurrences",
                "last_approaching_digest_at",
                "pending_approaching_digest",
                "preconfirmation_divergence_alerted_occurrences",
                "pending_preconfirmation_divergence_digest",
                "screening_completion_sessions",
                "pending_screening_completions",
            }
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("delivered_event_ids"), list)
            or not required_state_fields.issubset(payload)
            or not frozenset(payload).issubset(
                required_state_fields | optional_state_fields
            )
        ):
            return invalid("STATE_SCHEMA_OR_FIELDS_INVALID")
        values = payload["delivered_event_ids"]
        if not all(
            isinstance(value, str) and value.startswith("sha256:") and len(value) == 71
            for value in values
        ):
            return invalid("DELIVERED_EVENT_IDS_INVALID")
        delivered = tuple(values)
        if delivered != tuple(sorted(set(delivered))):
            return invalid("DELIVERED_EVENT_IDS_NOT_CANONICAL")
        raw_delivered_segment_evidence = payload.get(
            "delivered_segment_evidence_ids",
            [],
        )
        if not isinstance(raw_delivered_segment_evidence, list) or not all(
            isinstance(value, str)
            and value.startswith("sha256:")
            and len(value) == 71
            for value in raw_delivered_segment_evidence
        ):
            return invalid("DELIVERED_SEGMENT_EVIDENCE_IDS_INVALID")
        delivered_segment_evidence = tuple(raw_delivered_segment_evidence)
        if delivered_segment_evidence != tuple(
            sorted(set(delivered_segment_evidence))
        ):
            return invalid("DELIVERED_SEGMENT_EVIDENCE_IDS_NOT_CANONICAL")
        for key in ("success_count", "failure_count", "suppressed_count"):
            if type(payload[key]) is not int or payload[key] < 0:
                return invalid("STATE_COUNTER_INVALID")
        for key in (
            "last_success_at",
            "last_failure_at",
            "last_suppressed_at",
        ):
            value = payload[key]
            if value is not None and _parse_time(value) is None:
                return invalid("STATE_TIMESTAMP_INVALID")
        for key in (
            "last_success_event_id",
            "last_failure_reason",
            "last_suppressed_reason",
        ):
            value = payload[key]
            if value is not None and (not isinstance(value, str) or not value):
                return invalid("STATE_DIAGNOSTIC_FIELD_INVALID")
        last_success_event_id = payload["last_success_event_id"]
        if last_success_event_id is not None and (
            not last_success_event_id.startswith("sha256:")
            or len(last_success_event_id) != 71
        ):
            return invalid("LAST_SUCCESS_EVENT_ID_INVALID")
        raw_audit = payload["event_audit"]
        if not isinstance(raw_audit, list) or not all(
            isinstance(value, Mapping) for value in raw_audit
        ):
            return invalid("EVENT_AUDIT_INVALID")
        event_audit = tuple(dict(value) for value in raw_audit[-_AUDIT_RECORD_LIMIT:])
        raw_suppressed_fingerprints = payload.get("suppressed_fingerprints")
        if raw_suppressed_fingerprints is None:
            # Migrate the legacy state without discarding delivered-event
            # dedupe. The retained tail seeds as much suppression history as
            # the old bounded format can prove.
            suppressed_fingerprints = tuple(
                dict.fromkeys(
                    _suppression_fingerprint(
                        str(value.get("event_id") or ""),
                        str(value.get("reason") or ""),
                    )
                    for value in event_audit
                    if value.get("status") == "suppressed"
                    and isinstance(value.get("event_id"), str)
                    and value.get("event_id")
                    and isinstance(value.get("reason"), str)
                    and value.get("reason")
                )
            )
        elif (
            not isinstance(raw_suppressed_fingerprints, list)
            or len(raw_suppressed_fingerprints) > _SUPPRESSED_FINGERPRINT_LIMIT
            or not all(
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                for value in raw_suppressed_fingerprints
            )
            or len(raw_suppressed_fingerprints) != len(set(raw_suppressed_fingerprints))
        ):
            return invalid("SUPPRESSED_FINGERPRINTS_INVALID")
        else:
            suppressed_fingerprints = tuple(raw_suppressed_fingerprints)
        raw_pending = payload["pending_trigger_events"]
        if not isinstance(raw_pending, Mapping):
            return invalid("PENDING_TRIGGER_EVENTS_INVALID")
        pending_trigger_events: dict[str, dict[str, object]] = {}
        for event_id, value in raw_pending.items():
            if (
                not isinstance(event_id, str)
                or not event_id.startswith("sha256:")
                or len(event_id) != 71
                or not isinstance(value, Mapping)
                or not {"old_stage", "new_stage", "queued_at"}.issubset(value)
                or not isinstance(value.get("old_stage"), str)
                or not value.get("old_stage")
                or value.get("new_stage")
                not in {
                    "triggered",
                    "executable",
                    "invalidated",
                    "closed",
                    _SEGMENT_ENRICHED_STAGE,
                }
                or _parse_time(value.get("queued_at")) is None
                or (
                    value.get("detected_at") is not None
                    and _parse_time(value.get("detected_at")) is None
                )
                or (
                    value.get("document") is not None
                    and not isinstance(value.get("document"), Mapping)
                )
            ):
                return invalid("PENDING_TRIGGER_EVENT_INVALID")
            normalized_pending: dict[str, object] = {
                "old_stage": str(value["old_stage"]),
                "new_stage": str(value["new_stage"]),
                "queued_at": str(value["queued_at"]),
            }
            if value.get("detected_at") is not None:
                normalized_pending["detected_at"] = str(value["detected_at"])
            if isinstance(value.get("document"), Mapping):
                normalized_pending["document"] = dict(value["document"])
            pending_trigger_events[event_id] = normalized_pending

        raw_approaching_alerted = payload.get(
            "approaching_alerted_occurrences",
            {},
        )
        if (
            not isinstance(raw_approaching_alerted, Mapping)
            or len(raw_approaching_alerted) > _APPROACHING_OCCURRENCE_LIMIT
            or any(
                not isinstance(event_id, str)
                or not event_id.startswith("sha256:")
                or len(event_id) != 71
                or _parse_time(alerted_at) is None
                for event_id, alerted_at in raw_approaching_alerted.items()
            )
        ):
            return invalid("APPROACHING_ALERTED_OCCURRENCES_INVALID")
        approaching_alerted_occurrences = {
            str(event_id): str(alerted_at)
            for event_id, alerted_at in raw_approaching_alerted.items()
        }
        last_approaching_digest_at = payload.get("last_approaching_digest_at")
        if (
            last_approaching_digest_at is not None
            and _parse_time(last_approaching_digest_at) is None
        ):
            return invalid("LAST_APPROACHING_DIGEST_AT_INVALID")
        raw_pending_approaching_digest = payload.get("pending_approaching_digest")
        pending_approaching_digest: dict[str, object] | None = None
        if raw_pending_approaching_digest is not None:
            required_pending_digest_fields = {
                "event_id",
                "occurrence_ids",
                "created_at",
                "documents",
                "total_count",
                "buy_count",
                "sell_count",
            }
            raw_occurrence_ids = (
                raw_pending_approaching_digest.get("occurrence_ids")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            raw_documents = (
                raw_pending_approaching_digest.get("documents")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            digest_event_id = (
                raw_pending_approaching_digest.get("event_id")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            total_count = (
                raw_pending_approaching_digest.get("total_count")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            buy_count = (
                raw_pending_approaching_digest.get("buy_count")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            sell_count = (
                raw_pending_approaching_digest.get("sell_count")
                if isinstance(raw_pending_approaching_digest, Mapping)
                else None
            )
            if (
                not isinstance(raw_pending_approaching_digest, Mapping)
                or set(raw_pending_approaching_digest)
                != required_pending_digest_fields
                or not isinstance(digest_event_id, str)
                or not digest_event_id.startswith("sha256:")
                or len(digest_event_id) != 71
                or _parse_time(raw_pending_approaching_digest.get("created_at"))
                is None
                or not isinstance(raw_occurrence_ids, list)
                or not raw_occurrence_ids
                or len(raw_occurrence_ids) > _APPROACHING_OCCURRENCE_LIMIT
                or any(
                    not isinstance(value, str)
                    or not value.startswith("sha256:")
                    or len(value) != 71
                    for value in raw_occurrence_ids
                )
                or raw_occurrence_ids != sorted(set(raw_occurrence_ids))
                or not isinstance(raw_documents, list)
                or not raw_documents
                or len(raw_documents) > _APPROACHING_DIGEST_MAX_ITEMS
                or any(not isinstance(value, Mapping) for value in raw_documents)
                or type(total_count) is not int
                or total_count != len(raw_occurrence_ids)
                or type(buy_count) is not int
                or type(sell_count) is not int
                or buy_count < 0
                or sell_count < 0
                or buy_count + sell_count != total_count
            ):
                return invalid("PENDING_APPROACHING_DIGEST_INVALID")
            pending_approaching_digest = {
                "event_id": digest_event_id,
                "occurrence_ids": list(raw_occurrence_ids),
                "created_at": str(raw_pending_approaching_digest["created_at"]),
                "documents": [dict(value) for value in raw_documents],
                "total_count": total_count,
                "buy_count": buy_count,
                "sell_count": sell_count,
            }

        raw_preconfirmation_divergence_alerted = payload.get(
            "preconfirmation_divergence_alerted_occurrences",
            {},
        )
        if (
            not isinstance(raw_preconfirmation_divergence_alerted, Mapping)
            or len(raw_preconfirmation_divergence_alerted)
            > _PRECONFIRMATION_DIVERGENCE_OCCURRENCE_LIMIT
            or any(
                not isinstance(event_id, str)
                or not event_id.startswith("sha256:")
                or len(event_id) != 71
                or _parse_time(alerted_at) is None
                for event_id, alerted_at in (
                    raw_preconfirmation_divergence_alerted.items()
                )
            )
        ):
            return invalid(
                "PRECONFIRMATION_DIVERGENCE_ALERTED_OCCURRENCES_INVALID"
            )
        preconfirmation_divergence_alerted_occurrences = {
            str(event_id): str(alerted_at)
            for event_id, alerted_at in (
                raw_preconfirmation_divergence_alerted.items()
            )
        }
        raw_pending_preconfirmation_divergence_digest = payload.get(
            "pending_preconfirmation_divergence_digest"
        )
        pending_preconfirmation_divergence_digest: dict[str, object] | None = None
        if raw_pending_preconfirmation_divergence_digest is not None:
            required_pending_divergence_fields = {
                "event_id",
                "occurrence_ids",
                "created_at",
                "documents",
                "total_count",
                "buy_count",
                "sell_count",
            }
            raw_divergence_occurrence_ids = (
                raw_pending_preconfirmation_divergence_digest.get("occurrence_ids")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            raw_divergence_documents = (
                raw_pending_preconfirmation_divergence_digest.get("documents")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            divergence_digest_event_id = (
                raw_pending_preconfirmation_divergence_digest.get("event_id")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            divergence_total_count = (
                raw_pending_preconfirmation_divergence_digest.get("total_count")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            divergence_buy_count = (
                raw_pending_preconfirmation_divergence_digest.get("buy_count")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            divergence_sell_count = (
                raw_pending_preconfirmation_divergence_digest.get("sell_count")
                if isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                else None
            )
            if (
                not isinstance(
                    raw_pending_preconfirmation_divergence_digest,
                    Mapping,
                )
                or set(raw_pending_preconfirmation_divergence_digest)
                != required_pending_divergence_fields
                or not isinstance(divergence_digest_event_id, str)
                or not divergence_digest_event_id.startswith("sha256:")
                or len(divergence_digest_event_id) != 71
                or _parse_time(
                    raw_pending_preconfirmation_divergence_digest.get("created_at")
                )
                is None
                or not isinstance(raw_divergence_occurrence_ids, list)
                or not raw_divergence_occurrence_ids
                or len(raw_divergence_occurrence_ids)
                > _PRECONFIRMATION_DIVERGENCE_OCCURRENCE_LIMIT
                or any(
                    not isinstance(value, str)
                    or not value.startswith("sha256:")
                    or len(value) != 71
                    for value in raw_divergence_occurrence_ids
                )
                or raw_divergence_occurrence_ids
                != sorted(set(raw_divergence_occurrence_ids))
                or not isinstance(raw_divergence_documents, list)
                or not raw_divergence_documents
                or len(raw_divergence_documents)
                > _PRECONFIRMATION_DIVERGENCE_DIGEST_MAX_ITEMS
                or any(
                    not isinstance(value, Mapping)
                    for value in raw_divergence_documents
                )
                or type(divergence_total_count) is not int
                or divergence_total_count != len(raw_divergence_occurrence_ids)
                or type(divergence_buy_count) is not int
                or type(divergence_sell_count) is not int
                or divergence_buy_count < 0
                or divergence_sell_count < 0
                or divergence_buy_count + divergence_sell_count
                != divergence_total_count
            ):
                return invalid(
                    "PENDING_PRECONFIRMATION_DIVERGENCE_DIGEST_INVALID"
                )
            pending_preconfirmation_divergence_digest = {
                "event_id": divergence_digest_event_id,
                "occurrence_ids": list(raw_divergence_occurrence_ids),
                "created_at": str(
                    raw_pending_preconfirmation_divergence_digest["created_at"]
                ),
                "documents": [
                    dict(value) for value in raw_divergence_documents
                ],
                "total_count": divergence_total_count,
                "buy_count": divergence_buy_count,
                "sell_count": divergence_sell_count,
            }

        raw_screening_completion_sessions = payload.get(
            "screening_completion_sessions",
            {},
        )
        if (
            not isinstance(raw_screening_completion_sessions, Mapping)
            or len(raw_screening_completion_sessions)
            > _SCREENING_COMPLETION_RECORD_LIMIT
            or any(
                not _valid_screening_session(session)
                or _parse_time(accepted_at) is None
                for session, accepted_at in raw_screening_completion_sessions.items()
            )
        ):
            return invalid("SCREENING_COMPLETION_SESSIONS_INVALID")
        screening_completion_sessions = {
            str(session): str(accepted_at)
            for session, accepted_at in raw_screening_completion_sessions.items()
        }

        raw_pending_screening_completions = payload.get(
            "pending_screening_completions",
            {},
        )
        if (
            not isinstance(raw_pending_screening_completions, Mapping)
            or len(raw_pending_screening_completions)
            > _SCREENING_COMPLETION_RECORD_LIMIT
        ):
            return invalid("PENDING_SCREENING_COMPLETIONS_INVALID")
        pending_screening_completions: dict[str, dict[str, object]] = {}
        pending_completion_fields = {
            "event_id",
            "created_at",
            "market_data_as_of",
            "coverage_epoch_id",
            "title",
            "lines",
        }
        for session, raw_completion in raw_pending_screening_completions.items():
            market_data_as_of = (
                _parse_time(raw_completion.get("market_data_as_of"))
                if isinstance(raw_completion, Mapping)
                else None
            )
            event_id = (
                raw_completion.get("event_id")
                if isinstance(raw_completion, Mapping)
                else None
            )
            lines = (
                raw_completion.get("lines")
                if isinstance(raw_completion, Mapping)
                else None
            )
            if (
                not _valid_screening_session(session)
                or session in screening_completion_sessions
                or not isinstance(raw_completion, Mapping)
                or set(raw_completion) != pending_completion_fields
                or event_id != screening_completion_event_id(str(session))
                or _parse_time(raw_completion.get("created_at")) is None
                or market_data_as_of is None
                or market_data_as_of.date().isoformat() != session
                or market_data_as_of.hour < _SCREENING_COMPLETION_CLOSE_HOUR
                or not isinstance(raw_completion.get("coverage_epoch_id"), str)
                or not raw_completion.get("coverage_epoch_id")
                or not isinstance(raw_completion.get("title"), str)
                or not raw_completion.get("title")
                or not isinstance(lines, list)
                or not lines
                or any(not isinstance(value, str) or not value for value in lines)
            ):
                return invalid("PENDING_SCREENING_COMPLETION_INVALID")
            pending_screening_completions[str(session)] = {
                "event_id": str(event_id),
                "created_at": str(raw_completion["created_at"]),
                "market_data_as_of": str(raw_completion["market_data_as_of"]),
                "coverage_epoch_id": str(raw_completion["coverage_epoch_id"]),
                "title": str(raw_completion["title"]),
                "lines": list(lines),
            }

        return {
            "delivered_event_ids": delivered,
            "delivered_segment_evidence_ids": delivered_segment_evidence,
            "success_count": payload["success_count"],
            "failure_count": payload["failure_count"],
            "last_success_at": payload["last_success_at"],
            "last_success_event_id": last_success_event_id,
            "last_failure_at": payload["last_failure_at"],
            "last_failure_reason": payload["last_failure_reason"],
            "suppressed_count": payload["suppressed_count"],
            "last_suppressed_at": payload["last_suppressed_at"],
            "last_suppressed_reason": payload["last_suppressed_reason"],
            "event_audit": event_audit,
            "suppressed_fingerprints": suppressed_fingerprints,
            "pending_trigger_events": pending_trigger_events,
            "approaching_alerted_occurrences": approaching_alerted_occurrences,
            "last_approaching_digest_at": last_approaching_digest_at,
            "pending_approaching_digest": pending_approaching_digest,
            "preconfirmation_divergence_alerted_occurrences": (
                preconfirmation_divergence_alerted_occurrences
            ),
            "pending_preconfirmation_divergence_digest": (
                pending_preconfirmation_divergence_digest
            ),
            "screening_completion_sessions": screening_completion_sessions,
            "pending_screening_completions": pending_screening_completions,
        }

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("notification clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("notification clock must be timezone-aware")
        return value.astimezone(CN)

    def _persist(self) -> None:
        if self._state_path is None or self._state_load_error is not None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "delivered_event_ids": sorted(self._delivered),
                    "delivered_segment_evidence_ids": sorted(
                        self._delivered_segment_evidence
                    ),
                    "success_count": self._success_count,
                    "failure_count": self._failure_count,
                    "last_success_at": self._last_success_at,
                    "last_success_event_id": self._last_success_event_id,
                    "last_failure_at": self._last_failure_at,
                    "last_failure_reason": self._last_failure_reason,
                    "suppressed_count": self._suppressed_count,
                    "last_suppressed_at": self._last_suppressed_at,
                    "last_suppressed_reason": self._last_suppressed_reason,
                    "event_audit": self._event_audit[-_AUDIT_RECORD_LIMIT:],
                    "suppressed_fingerprints": list(self._suppressed_fingerprints),
                    "pending_trigger_events": self._pending_trigger_events,
                    "approaching_alerted_occurrences": dict(
                        sorted(self._approaching_alerted_occurrences.items())
                    ),
                    "last_approaching_digest_at": self._last_approaching_digest_at,
                    "pending_approaching_digest": self._pending_approaching_digest,
                    "preconfirmation_divergence_alerted_occurrences": dict(
                        sorted(
                            self._preconfirmation_divergence_alerted_occurrences.items()
                        )
                    ),
                    "pending_preconfirmation_divergence_digest": (
                        self._pending_preconfirmation_divergence_digest
                    ),
                    "screening_completion_sessions": dict(
                        sorted(self._screening_completion_sessions.items())
                    ),
                    "pending_screening_completions": dict(
                        sorted(self._pending_screening_completions.items())
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._state_path)

    def health_snapshot(self) -> dict[str, object]:
        """公开持久投递证据，但不泄露任何凭据。"""

        with self._lock:
            deferred_delivery = bool(
                self._notifier is not None
                and getattr(self._notifier, "delivery_deferred", False) is True
            )
            deferred_health: dict[str, object] | None = None
            if deferred_delivery:
                health_provider = getattr(self._notifier, "health_snapshot", None)
                if callable(health_provider):
                    try:
                        raw_health = health_provider()
                        if isinstance(raw_health, Mapping):
                            deferred_health = dict(raw_health)
                    except Exception as exc:
                        deferred_health = {
                            "configured": True,
                            "operationally_verified": False,
                            "status": "unavailable",
                            "reason_code": "NOTIFICATION_OUTBOX_HEALTH_UNAVAILABLE",
                            "last_failure_reason": type(exc).__name__,
                        }
            transport_configured = bool(
                self._notifier is not None
                and getattr(self._notifier, "available", True)
            )
            degraded = bool(
                self._last_failure_at is not None
                and (
                    self._last_success_at is None
                    or self._last_failure_at > self._last_success_at
                )
            )
            state_valid = self._state_load_error is None
            verified = (
                deferred_health.get("operationally_verified") is True
                if deferred_health is not None
                else bool(self._delivered)
            ) and state_valid
            if not state_valid:
                status = "unavailable"
                reason = "NOTIFICATION_DISPATCH_STATE_INVALID"
            elif deferred_health is not None:
                transport_configured = deferred_health.get("configured") is True
                status = str(deferred_health.get("status") or "unavailable")
                reason = str(
                    deferred_health.get("reason_code")
                    or "NOTIFICATION_OUTBOX_HEALTH_UNAVAILABLE"
                )
            elif not transport_configured:
                status = "unavailable"
                reason = "EXTERNAL_NOTIFICATION_TRANSPORT_NOT_CONFIGURED"
            elif degraded:
                status = "degraded"
                reason = "LATEST_NOTIFICATION_DELIVERY_FAILED"
            elif verified:
                status = "verified"
                reason = "DELIVERY_SUCCESS_PROVEN"
            else:
                status = "awaiting_first_delivery"
                reason = "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"
            result = {
                "schema": "chanlun-signal-notification-readiness",
                "configured": transport_configured,
                "review_inbox_configured": self._review_inbox is not None,
                "operationally_verified": verified,
                "status": status,
                "reason_code": reason,
                "delivery_mode": (
                    "DURABLE_BACKGROUND_OUTBOX" if deferred_delivery else "INLINE"
                ),
                "accepted_event_count": len(self._delivered),
                "delivered_event_count": (
                    int(deferred_health.get("delivered_event_count", 0))
                    if deferred_health is not None
                    else len(self._delivered)
                ),
                "success_count": (
                    int(deferred_health.get("success_count", 0))
                    if deferred_health is not None
                    else self._success_count
                ),
                "failure_count": (
                    int(deferred_health.get("failure_count", 0))
                    if deferred_health is not None
                    else self._failure_count
                ),
                "last_success_at": (
                    deferred_health.get("last_success_at")
                    if deferred_health is not None
                    else self._last_success_at
                ),
                "last_success_event_id": (
                    deferred_health.get("last_success_event_id")
                    if deferred_health is not None
                    else self._last_success_event_id
                ),
                "last_failure_at": (
                    deferred_health.get("last_failure_at")
                    if deferred_health is not None
                    else self._last_failure_at
                ),
                "last_failure_reason": (
                    deferred_health.get("last_failure_reason")
                    if deferred_health is not None
                    else self._last_failure_reason
                ),
                "suppressed_count": self._suppressed_count,
                "last_suppressed_at": self._last_suppressed_at,
                "last_suppressed_reason": self._last_suppressed_reason,
                "suppressed_fingerprint_count": len(self._suppressed_fingerprints),
                "event_audit_record_count": len(self._event_audit),
                "pending_trigger_event_count": len(self._pending_trigger_events),
                "approaching_alerted_occurrence_count": len(
                    self._approaching_alerted_occurrences
                ),
                "approaching_digest_pending": (
                    self._pending_approaching_digest is not None
                ),
                "last_approaching_digest_at": self._last_approaching_digest_at,
                "approaching_digest_cooldown_seconds": int(
                    _APPROACHING_DIGEST_COOLDOWN.total_seconds()
                ),
                "preconfirmation_divergence_alerted_occurrence_count": len(
                    self._preconfirmation_divergence_alerted_occurrences
                ),
                "preconfirmation_divergence_digest_pending": (
                    self._pending_preconfirmation_divergence_digest is not None
                ),
                "screening_completion_session_count": len(
                    self._screening_completion_sessions
                ),
                "last_screening_completion_session": (
                    max(self._screening_completion_sessions)
                    if self._screening_completion_sessions
                    else None
                ),
                "last_screening_completion_at": (
                    self._screening_completion_sessions[
                        max(self._screening_completion_sessions)
                    ]
                    if self._screening_completion_sessions
                    else None
                ),
                "pending_screening_completion_count": len(
                    self._pending_screening_completions
                ),
                "pending_screening_completion_sessions": sorted(
                    self._pending_screening_completions
                ),
                "credentials_exposed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
            if deferred_health is not None:
                result.update(
                    {
                        "outbox_worker_alive": deferred_health.get("worker_alive"),
                        "outbox_pending_event_count": int(
                            deferred_health.get("pending_event_count", 0)
                        ),
                        "outbox_retrying_event_count": int(
                            deferred_health.get("retrying_event_count", 0)
                        ),
                        "outbox_review_projection_pending_event_count": int(
                            deferred_health.get(
                                "review_projection_pending_event_count", 0
                            )
                        ),
                        "outbox_delivery_observer_configured": (
                            deferred_health.get("delivery_observer_configured") is True
                        ),
                        "outbox_oldest_pending_at": deferred_health.get(
                            "oldest_pending_at"
                        ),
                        "outbox_oldest_pending_age_seconds": deferred_health.get(
                            "oldest_pending_age_seconds"
                        ),
                        "outbox_in_flight_event_id": deferred_health.get(
                            "in_flight_event_id"
                        ),
                    }
                )
            return result

    def _record_audit(
        self,
        *,
        status: str,
        event_id: str,
        old_stage: str,
        new_stage: str,
        document: Mapping[str, object],
        reason: str | None = None,
    ) -> None:
        setup = _mapping(document.get("setup_5m"))
        trigger = _mapping(document.get("segment_difference_1m")) or _mapping(
            document.get(_NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD)
        )
        raw_reasons = document.get("decision_reasons")
        reasons = (
            raw_reasons
            if isinstance(raw_reasons, (list, tuple, set, frozenset))
            else ()
        )
        recorded_at = self._now().isoformat()
        notification_evidence_at = _notification_evidence_time(
            document,
            new_stage=new_stage,
        )
        self._event_audit.append(
            {
                "status": status,
                "event_id": event_id,
                "recorded_at": recorded_at,
                "reason": reason,
                "code": str(document.get("code") or ""),
                "side": str(document.get("side") or ""),
                "old_stage": old_stage,
                "new_stage": new_stage,
                "setup_point_type": str(setup.get("point_type") or ""),
                "setup_point_id": str(setup.get("point_id") or ""),
                "terminal_segment_role": str(setup.get("terminal_segment_role") or ""),
                "terminal_segment_id": str(setup.get("terminal_segment_id") or ""),
                "terminal_segment_direction": str(
                    setup.get("terminal_segment_direction") or ""
                ),
                "terminal_segment_state": str(
                    setup.get("terminal_segment_state") or ""
                ),
                "terminal_segment_start_at": str(
                    setup.get("terminal_segment_start_at") or ""
                ),
                "terminal_segment_end_at": str(
                    setup.get("terminal_segment_end_at") or ""
                ),
                "trigger_point_type": str(trigger.get("point_type") or ""),
                "trigger_divergence_kind": str(
                    trigger.get("divergence_kind") or ""
                ),
                "trigger_point_id": str(trigger.get("point_id") or ""),
                "trigger_anchor_at": str(trigger.get("anchor_at") or ""),
                "trigger_confirmed_at": str(
                    trigger.get("confirmed_at") or trigger.get("available_at") or ""
                ),
                "trigger_available_at": str(
                    trigger.get("available_at") or trigger.get("confirmed_at") or ""
                ),
                "trigger_recursive_level": trigger.get("recursive_level"),
                "notification_evidence_at": (
                    ""
                    if notification_evidence_at is None
                    else notification_evidence_at.isoformat(timespec="seconds")
                ),
                "one_minute_role": "SEGMENT_DIFFERENCE_ONLY",
                "entry_allowed": document.get("entry_allowed") is True,
                "exit_allowed": document.get("exit_allowed") is True,
                "warmup_converged": (
                    _mapping(document.get("warmup")).get("converged") is True
                ),
                "decision_reasons": [str(value) for value in reasons],
            }
        )
        if len(self._event_audit) > _AUDIT_RECORD_LIMIT:
            del self._event_audit[:-_AUDIT_RECORD_LIMIT]

    def _record_suppressed(
        self,
        *,
        event_id: str,
        old_stage: str,
        new_stage: str,
        document: Mapping[str, object],
        reason: str,
    ) -> None:
        # The 500-row audit tail is observability, not an idempotency ledger.
        # A full-market refresh can suppress more rows than that in one pass.
        fingerprint = _suppression_fingerprint(event_id, reason)
        if fingerprint in self._suppressed_fingerprints:
            self._suppressed_fingerprints.pop(fingerprint)
            self._suppressed_fingerprints[fingerprint] = None
            return
        self._suppressed_fingerprints[fingerprint] = None
        while len(self._suppressed_fingerprints) > _SUPPRESSED_FINGERPRINT_LIMIT:
            del self._suppressed_fingerprints[next(iter(self._suppressed_fingerprints))]
        self._suppressed_count += 1
        self._last_suppressed_at = self._now().isoformat()
        self._last_suppressed_reason = reason
        self._record_audit(
            status="suppressed",
            event_id=event_id,
            old_stage=old_stage,
            new_stage=new_stage,
            document=document,
            reason=reason,
        )

    def _delivered_trigger_exists(
        self,
        document: Mapping[str, object],
    ) -> bool:
        occurrence = _trigger_occurrence_key(document, "triggered")
        return bool(
            occurrence is not None
            and _trigger_occurrence_event_id(occurrence) in self._delivered
        )

    def _delivered_segment_evidence_exists(
        self,
        document: Mapping[str, object],
    ) -> bool:
        occurrence = _segment_occurrence_key(
            document,
            _SEGMENT_ENRICHED_STAGE,
        )
        return bool(
            occurrence is not None
            and _segment_occurrence_event_id(occurrence)
            in self._delivered_segment_evidence
        )

    def dispatch_screening_completion(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        """Queue one durable completion receipt per closed market-data session."""

        with self._lock:
            if self._state_load_error is not None or self._notifier is None:
                return
            now = self._now()
            current_details = _screening_completion_details(current)
            previous_details = _screening_completion_details(previous)
            if current_details is not None:
                session = str(current_details["market_data_session"])
                previous_session = (
                    str(previous_details["market_data_session"])
                    if previous_details is not None
                    else None
                )
                event_id = screening_completion_event_id(session)
                if (
                    previous_session != session
                    and session not in self._screening_completion_sessions
                    and event_id not in self._delivered
                    and session not in self._pending_screening_completions
                ):
                    title, lines = format_screening_completion(
                        current,
                        completed_at=now,
                    )
                    self._pending_screening_completions[session] = {
                        "event_id": event_id,
                        "created_at": now.isoformat(),
                        "market_data_as_of": _time_identity(
                            current_details["market_data_as_of"]
                        ),
                        "coverage_epoch_id": str(
                            current_details["coverage_epoch_id"]
                        ),
                        "title": title,
                        "lines": lines,
                    }
                    try:
                        # Write the exact receipt before crossing the transport
                        # boundary. A crash can then only replay the same event
                        # id and message through the durable outbox.
                        self._persist()
                    except OSError:
                        self._pending_screening_completions.pop(session, None)
                        raise

            for session in sorted(self._pending_screening_completions):
                pending = self._pending_screening_completions.get(session)
                if pending is None:
                    continue
                event_id = str(pending["event_id"])
                if (
                    event_id in self._delivered
                    or session in self._screening_completion_sessions
                ):
                    self._delivered.add(event_id)
                    self._screening_completion_sessions.setdefault(
                        session,
                        now.isoformat(),
                    )
                    self._pending_screening_completions.pop(session, None)
                    self._persist()
                    continue
                try:
                    send_rich = getattr(self._notifier, "send_rich", None)
                    if callable(send_rich):
                        sent = bool(
                            send_rich(
                                str(pending["title"]),
                                list(pending["lines"]),
                                {
                                    "artifact_key": event_id,
                                    "require_evidence_match": False,
                                    "delivery_priority": 80,
                                    "charts": [],
                                    "notification_kind": (
                                        "daily_screening_completion"
                                    ),
                                    "market_data_session": session,
                                },
                            )
                        )
                    else:
                        sent = bool(
                            self._notifier.send(
                                str(pending["title"]),
                                list(pending["lines"]),
                            )
                        )
                except Exception as exc:
                    sent = False
                    failure_reason = type(exc).__name__
                else:
                    failure_reason = None if sent else "NOTIFIER_RETURNED_FALSE"
                if not sent:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = failure_reason
                    self._persist()
                    return

                accepted_at = self._now().isoformat()
                self._delivered.add(event_id)
                self._screening_completion_sessions[session] = accepted_at
                self._pending_screening_completions.pop(session, None)
                while (
                    len(self._screening_completion_sessions)
                    > _SCREENING_COMPLETION_RECORD_LIMIT
                ):
                    self._screening_completion_sessions.pop(
                        min(self._screening_completion_sessions)
                    )
                self._success_count += 1
                self._last_success_at = accepted_at
                self._last_success_event_id = event_id
                self._persist()

    def _dispatch_preconfirmation_divergence_digest_locked(
        self,
        current: Mapping[str, object],
    ) -> None:
        """Durably deliver new 1m divergences under unconfirmed 5m setups."""

        now = self._now()
        pending = self._pending_preconfirmation_divergence_digest
        if pending is None:
            raw_rows = current.get("signals", ())
            rows = raw_rows if isinstance(raw_rows, (list, tuple)) else ()
            candidates_by_occurrence: dict[str, dict[str, object]] = {}
            for document in rows:
                if not isinstance(document, Mapping):
                    continue
                for divergence in _preconfirmation_divergence_rows(document):
                    if (
                        _preconfirmation_divergence_eligibility_reason(
                            document,
                            divergence,
                            observed_at=now,
                        )
                        is not None
                    ):
                        continue
                    occurrence = _preconfirmation_divergence_occurrence_key(
                        document,
                        divergence,
                    )
                    if occurrence is None:
                        continue
                    occurrence_id = (
                        _preconfirmation_divergence_occurrence_event_id(occurrence)
                    )
                    if (
                        occurrence_id
                        in self._preconfirmation_divergence_alerted_occurrences
                    ):
                        continue
                    notification_document = dict(document)
                    notification_document[
                        _NOTIFICATION_PRECONFIRMATION_DIVERGENCE_FIELD
                    ] = dict(divergence)
                    candidates_by_occurrence.setdefault(
                        occurrence_id,
                        notification_document,
                    )
            if not candidates_by_occurrence:
                return
            ordered = tuple(
                sorted(
                    candidates_by_occurrence.items(),
                    key=lambda item: _preconfirmation_divergence_sort_key(item[1]),
                )
            )
            occurrence_ids = sorted(candidates_by_occurrence)
            documents = [
                dict(document)
                for _occurrence_id, document in ordered[
                    :_PRECONFIRMATION_DIVERGENCE_DIGEST_MAX_ITEMS
                ]
            ]
            buy_count = sum(
                str(
                    document.get("side")
                    or _mapping(document.get("setup_5m")).get("side")
                )
                == "buy"
                for document in candidates_by_occurrence.values()
            )
            sell_count = len(occurrence_ids) - buy_count
            event_id = _preconfirmation_divergence_digest_event_id(
                occurrence_ids,
                created_at=now,
            )
            pending = {
                "event_id": event_id,
                "occurrence_ids": occurrence_ids,
                "created_at": now.isoformat(),
                "documents": documents,
                "total_count": len(occurrence_ids),
                "buy_count": buy_count,
                "sell_count": sell_count,
            }
            self._pending_preconfirmation_divergence_digest = pending
            try:
                self._persist()
            except OSError:
                self._pending_preconfirmation_divergence_digest = None
                raise

        event_id = str(pending["event_id"])
        occurrence_ids = tuple(str(value) for value in pending["occurrence_ids"])
        documents = tuple(
            dict(value)
            for value in pending["documents"]
            if isinstance(value, Mapping)
        )
        created_at = _parse_time(pending["created_at"])
        if created_at is None or not documents:
            self._state_load_error = (
                "PENDING_PRECONFIRMATION_DIVERGENCE_DIGEST_INVALID"
            )
            return
        representative = dict(documents[0])
        representative["notification_digest_count"] = int(pending["total_count"])
        representative["notification_digest_kind"] = (
            "preconfirmation_divergence"
        )
        title, lines = format_preconfirmation_divergence_digest(
            documents,
            total_count=int(pending["total_count"]),
            buy_count=int(pending["buy_count"]),
            sell_count=int(pending["sell_count"]),
            detected_at=created_at,
        )
        review_recorded = self._record_review_notification(
            event_id=event_id,
            document=representative,
            old_stage="approaching",
            new_stage="approaching",
            delivery_status="pending",
            detected_at=created_at,
        )
        if not review_recorded:
            self._failure_count += 1
            self._last_failure_at = now.isoformat()
            self._last_failure_reason = "REVIEW_INBOX_RECORD_FAILED"
            self._record_audit(
                status="failed",
                event_id=event_id,
                old_stage="approaching",
                new_stage="approaching",
                document=representative,
                reason="REVIEW_INBOX_RECORD_FAILED",
            )
            self._persist()
            return

        expires_at = now + _NOTIFICATION_RETRY_TTL
        try:
            send_rich = getattr(self._notifier, "send_rich", None)
            if callable(send_rich):
                sent = bool(
                    send_rich(
                        title,
                        lines,
                        {
                            "artifact_key": event_id,
                            "require_evidence_match": False,
                            "delivery_priority": 8,
                            "expires_at": expires_at.isoformat(),
                            "charts": [],
                        },
                    )
                )
            elif self._notifier is not None:
                sent = bool(self._notifier.send(title, lines))
            else:
                sent = False
        except Exception as exc:
            sent = False
            failure_reason = type(exc).__name__
        else:
            failure_reason = None if sent else "NOTIFIER_RETURNED_FALSE"
        if not sent:
            self._failure_count += 1
            self._last_failure_at = now.isoformat()
            self._last_failure_reason = failure_reason
            self._record_audit(
                status="failed",
                event_id=event_id,
                old_stage="approaching",
                new_stage="approaching",
                document=representative,
                reason=failure_reason,
            )
            self._record_review_notification(
                event_id=event_id,
                document=representative,
                old_stage="approaching",
                new_stage="approaching",
                delivery_status="failed",
                detected_at=created_at,
                delivery_reason=failure_reason,
            )
            self._persist()
            return

        delivered_at = now.isoformat()
        self._delivered.add(event_id)
        for occurrence_id in occurrence_ids:
            self._preconfirmation_divergence_alerted_occurrences[
                occurrence_id
            ] = delivered_at
        if (
            len(self._preconfirmation_divergence_alerted_occurrences)
            > _PRECONFIRMATION_DIVERGENCE_OCCURRENCE_LIMIT
        ):
            retained = sorted(
                self._preconfirmation_divergence_alerted_occurrences.items(),
                key=lambda item: (item[1], item[0]),
            )[-_PRECONFIRMATION_DIVERGENCE_OCCURRENCE_LIMIT:]
            self._preconfirmation_divergence_alerted_occurrences = dict(retained)
        self._pending_preconfirmation_divergence_digest = None
        self._success_count += 1
        self._last_success_at = delivered_at
        self._last_success_event_id = event_id
        deferred_delivery = bool(
            getattr(self._notifier, "delivery_deferred", False) is True
        )
        self._record_audit(
            status="queued" if deferred_delivery else "delivered",
            event_id=event_id,
            old_stage="approaching",
            new_stage="approaching",
            document=representative,
        )
        if not deferred_delivery:
            self._record_review_notification(
                event_id=event_id,
                document=representative,
                old_stage="approaching",
                new_stage="approaching",
                delivery_status=(
                    "simulated"
                    if getattr(self._notifier, "dry_run", False) is True
                    else "delivered"
                ),
                detected_at=created_at,
            )
        self._persist()

    def dispatch_approaching_digest(
        self,
        current: Mapping[str, object],
    ) -> None:
        """Send one durable, rate-limited digest for current forming structures.

        This channel deliberately does not reuse ``dispatch_changes``.  A
        rolling provisional point is not a buy/sell event and must never enter
        the formal trigger/invalidated lifecycle.  The full current monitor
        snapshot is supplied once per completed monitoring round so all newly
        visible structures can be coalesced into one bounded message.
        """

        with self._lock:
            if self._state_load_error is not None:
                return
            self._dispatch_preconfirmation_divergence_digest_locked(current)
            if self._pending_preconfirmation_divergence_digest is not None:
                return
            now = self._now()
            pending = self._pending_approaching_digest
            if pending is None:
                last_digest_at = _parse_time(self._last_approaching_digest_at)
                if (
                    last_digest_at is not None
                    and timedelta(0) <= now - last_digest_at
                    < _APPROACHING_DIGEST_COOLDOWN
                ):
                    return
                raw_rows = current.get("signals", ())
                rows = (
                    raw_rows
                    if isinstance(raw_rows, (list, tuple))
                    else ()
                )
                candidates_by_occurrence: dict[
                    str,
                    Mapping[str, object],
                ] = {}
                for document in rows:
                    if not isinstance(document, Mapping):
                        continue
                    if any(
                        _preconfirmation_divergence_eligibility_reason(
                            document,
                            divergence,
                            observed_at=now,
                        )
                        is None
                        for divergence in _preconfirmation_divergence_rows(document)
                    ):
                        # The stronger 1m-divergence warning already explains
                        # that this 5m structure is unconfirmed.  Do not follow
                        # it with a weaker generic approaching reminder.
                        continue
                    if (
                        _approaching_digest_eligibility_reason(
                            document,
                            observed_at=now,
                        )
                        is not None
                    ):
                        continue
                    occurrence = _approaching_occurrence_key(document)
                    if occurrence is None:
                        continue
                    occurrence_id = _approaching_occurrence_event_id(occurrence)
                    if occurrence_id in self._approaching_alerted_occurrences:
                        continue
                    candidates_by_occurrence.setdefault(occurrence_id, document)
                if not candidates_by_occurrence:
                    return
                ordered = tuple(
                    sorted(
                        candidates_by_occurrence.items(),
                        key=lambda item: _approaching_digest_sort_key(item[1]),
                    )
                )
                occurrence_ids = sorted(candidates_by_occurrence)
                documents = [
                    dict(document)
                    for _occurrence_id, document in ordered[
                        :_APPROACHING_DIGEST_MAX_ITEMS
                    ]
                ]
                buy_count = sum(
                    str(
                        document.get("side")
                        or _mapping(document.get("setup_5m")).get("side")
                    )
                    == "buy"
                    for document in candidates_by_occurrence.values()
                )
                sell_count = len(occurrence_ids) - buy_count
                event_id = _approaching_digest_event_id(
                    occurrence_ids,
                    created_at=now,
                )
                pending = {
                    "event_id": event_id,
                    "occurrence_ids": occurrence_ids,
                    "created_at": now.isoformat(),
                    "documents": documents,
                    "total_count": len(occurrence_ids),
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                }
                # Persist the exact digest before crossing the external outbox
                # boundary.  A crash can then only replay the same event id;
                # the durable outbox deduplicates that retry.
                self._pending_approaching_digest = pending
                try:
                    self._persist()
                except OSError:
                    self._pending_approaching_digest = None
                    raise

            event_id = str(pending["event_id"])
            occurrence_ids = tuple(str(value) for value in pending["occurrence_ids"])
            documents = tuple(
                dict(value)
                for value in pending["documents"]
                if isinstance(value, Mapping)
            )
            created_at = _parse_time(pending["created_at"])
            if created_at is None or not documents:
                # This is unreachable after state validation, but preserves the
                # fail-closed boundary if in-process state is corrupted.
                self._state_load_error = "PENDING_APPROACHING_DIGEST_INVALID"
                return
            representative = dict(documents[0])
            representative["notification_digest_count"] = int(
                pending["total_count"]
            )
            representative["notification_digest_kind"] = "approaching"
            title, lines = format_approaching_digest(
                documents,
                total_count=int(pending["total_count"]),
                buy_count=int(pending["buy_count"]),
                sell_count=int(pending["sell_count"]),
                detected_at=created_at,
            )
            review_recorded = self._record_review_notification(
                event_id=event_id,
                document=representative,
                old_stage="observed",
                new_stage="approaching",
                delivery_status="pending",
                detected_at=created_at,
            )
            if not review_recorded:
                self._failure_count += 1
                self._last_failure_at = now.isoformat()
                self._last_failure_reason = "REVIEW_INBOX_RECORD_FAILED"
                self._record_audit(
                    status="failed",
                    event_id=event_id,
                    old_stage="observed",
                    new_stage="approaching",
                    document=representative,
                    reason="REVIEW_INBOX_RECORD_FAILED",
                )
                self._persist()
                return
            expires_at = now + _NOTIFICATION_RETRY_TTL
            try:
                send_rich = getattr(self._notifier, "send_rich", None)
                if callable(send_rich):
                    sent = bool(
                        send_rich(
                            title,
                            lines,
                            {
                                "artifact_key": event_id,
                                "require_evidence_match": False,
                                "delivery_priority": 20,
                                "expires_at": expires_at.isoformat(),
                                "charts": [],
                            },
                        )
                    )
                elif self._notifier is not None:
                    sent = bool(self._notifier.send(title, lines))
                else:
                    sent = False
            except Exception as exc:
                sent = False
                failure_reason = type(exc).__name__
            else:
                failure_reason = None if sent else "NOTIFIER_RETURNED_FALSE"
            if not sent:
                self._failure_count += 1
                self._last_failure_at = now.isoformat()
                self._last_failure_reason = failure_reason
                self._record_audit(
                    status="failed",
                    event_id=event_id,
                    old_stage="observed",
                    new_stage="approaching",
                    document=representative,
                    reason=failure_reason,
                )
                self._record_review_notification(
                    event_id=event_id,
                    document=representative,
                    old_stage="observed",
                    new_stage="approaching",
                    delivery_status="failed",
                    detected_at=created_at,
                    delivery_reason=failure_reason,
                )
                self._persist()
                return

            delivered_at = now.isoformat()
            self._delivered.add(event_id)
            for occurrence_id in occurrence_ids:
                self._approaching_alerted_occurrences[occurrence_id] = delivered_at
            if (
                len(self._approaching_alerted_occurrences)
                > _APPROACHING_OCCURRENCE_LIMIT
            ):
                retained = sorted(
                    self._approaching_alerted_occurrences.items(),
                    key=lambda item: (item[1], item[0]),
                )[-_APPROACHING_OCCURRENCE_LIMIT:]
                self._approaching_alerted_occurrences = dict(retained)
            self._pending_approaching_digest = None
            self._last_approaching_digest_at = delivered_at
            self._success_count += 1
            self._last_success_at = delivered_at
            self._last_success_event_id = event_id
            deferred_delivery = bool(
                getattr(self._notifier, "delivery_deferred", False) is True
            )
            self._record_audit(
                status="queued" if deferred_delivery else "delivered",
                event_id=event_id,
                old_stage="observed",
                new_stage="approaching",
                document=representative,
            )
            if not deferred_delivery:
                self._record_review_notification(
                    event_id=event_id,
                    document=representative,
                    old_stage="observed",
                    new_stage="approaching",
                    delivery_status=(
                        "simulated"
                        if getattr(self._notifier, "dry_run", False) is True
                        else "delivered"
                    ),
                    detected_at=created_at,
                )
            self._persist()

    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        with self._lock:
            # Losing the durable dedupe ledger makes it impossible to prove that
            # a structurally identical notification was not already delivered.
            # Preserve the invalid file for recovery and fail closed instead of
            # replaying old signals or silently replacing the evidence.
            if self._state_load_error is not None:
                return
            before = _signals_by_id(previous)
            after = _signals_by_id(current)
            before_by_semantic = {
                _signal_semantic_key(document): document for document in before.values()
            }
            require_decision_identity = bool(
                current.get("decision_core_id")
                or current.get("signal_document_contract_id")
                or current.get("decision_core")
            )
            grouped: dict[
                tuple[str, ...],
                list[tuple[str, str, str, Mapping[str, object]]],
            ] = {}
            dirty_state = False
            dispatch_now = self._now()
            for event_id, pending in tuple(self._pending_trigger_events.items()):
                pending_document = pending.get("document")
                if not isinstance(pending_document, Mapping):
                    # Legacy state files did not persist the payload.  They can
                    # still be retried below while the occurrence is present in
                    # the current snapshot.
                    continue
                pending_stage = str(pending.get("new_stage") or "triggered")
                pending_group_key = _notification_group_key(
                    pending_document,
                    pending_stage,
                    event_id,
                )
                canonical_event_id = _notification_group_event_id(pending_group_key)
                if canonical_event_id != event_id:
                    self._pending_trigger_events.pop(event_id, None)
                    self._pending_trigger_events.setdefault(canonical_event_id, pending)
                    dirty_state = True
                grouped[pending_group_key] = [
                    (
                        str(pending_document.get("signal_id") or event_id),
                        str(pending.get("old_stage") or "None"),
                        pending_stage,
                        pending_document,
                    )
                ]
            for event_id, pending in tuple(self._pending_trigger_events.items()):
                queued_at = _parse_time(pending.get("queued_at"))
                if (
                    queued_at is not None
                    and timedelta(0)
                    <= dispatch_now - queued_at
                    <= _PENDING_TRIGGER_MAX_AGE
                ):
                    continue
                self._pending_trigger_events.pop(event_id, None)
                self._suppressed_count += 1
                self._last_suppressed_at = dispatch_now.isoformat()
                self._last_suppressed_reason = "PENDING_TRIGGER_EXPIRED"
                self._record_audit(
                    status="suppressed",
                    event_id=event_id,
                    old_stage=str(pending.get("old_stage") or ""),
                    new_stage=str(pending.get("new_stage") or ""),
                    document={},
                    reason="PENDING_TRIGGER_EXPIRED",
                )
                dirty_state = True
            for signal_id, document in sorted(after.items()):
                previous_document = before.get(signal_id)
                if previous_document is None:
                    previous_document = before_by_semantic.get(
                        _signal_semantic_key(document)
                    )
                actual_old_stage = _stage(previous_document)
                actual_new_stage = _stage(document)
                transition = (actual_old_stage, actual_new_stage)
                pending_event_id = None
                pending = None
                current_occurrence = _trigger_occurrence_key(
                    document,
                    str(actual_new_stage),
                )
                if current_occurrence is not None:
                    pending_event_id = _trigger_occurrence_event_id(current_occurrence)
                    pending = self._pending_trigger_events.get(pending_event_id)
                segment_attached = _new_segment_attached(
                    previous_document,
                    document,
                )
                # A stage upgrade and a new 1m locator can land in the same
                # refresh.  If the 5m occurrence was already delivered, its
                # stage event will dedupe and must not swallow the new locator.
                # An undelivered/pending 5m event instead keeps priority and
                # carries the locator in that single retried notification.
                if (
                    segment_attached
                    and self._delivered_trigger_exists(document)
                    and not self._delivered_segment_evidence_exists(document)
                ):
                    old_stage = str(actual_old_stage)
                    new_stage = _SEGMENT_ENRICHED_STAGE
                elif transition in _NOTIFIABLE_TRANSITIONS:
                    old_stage = str(actual_old_stage)
                    new_stage = str(actual_new_stage)
                elif pending is not None:
                    old_stage = str(pending["old_stage"])
                    new_stage = str(pending["new_stage"])
                elif segment_attached:
                    old_stage = str(actual_old_stage)
                    new_stage = _SEGMENT_ENRICHED_STAGE
                else:
                    continue
                signal_event_id = notification_event_id(
                    signal_id,
                    old_stage,
                    new_stage,
                )
                group_key = _notification_group_key(
                    document,
                    new_stage,
                    signal_event_id,
                )
                canonical_event_id = _notification_group_event_id(group_key)
                if pending is None:
                    pending_event_id = canonical_event_id
                    pending = self._pending_trigger_events.get(pending_event_id)
                if new_stage == "invalidated" and not self._delivered_trigger_exists(
                    document
                ):
                    self._record_suppressed(
                        event_id=canonical_event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=document,
                        reason="ORPHAN_INVALIDATION_WITHOUT_DELIVERED_TRIGGER",
                    )
                    dirty_state = True
                    continue
                # 重试也重新验证结构身份；绝对过期时间还会在真正发送前按当前
                # 时钟再检查一次，避免损坏队列或过期边界绕过实时通知闸门。
                rejection = _notification_eligibility_reason(
                    document,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    require_decision_identity=require_decision_identity,
                    evaluated_at=dispatch_now,
                )
                if rejection is not None:
                    if pending_event_id is not None:
                        self._pending_trigger_events.pop(pending_event_id, None)
                    self._record_suppressed(
                        event_id=(
                            pending_event_id
                            if pending_event_id is not None
                            else signal_event_id
                        ),
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=document,
                        reason=rejection,
                    )
                    dirty_state = True
                    continue
                grouped.setdefault(group_key, []).append(
                    (
                        signal_id,
                        old_stage,
                        new_stage,
                        document,
                    )
                )

            authoritative_raw = current.get("notification_authoritative_codes", ())
            authoritative_codes = (
                {
                    str(value)
                    for value in authoritative_raw
                    if isinstance(value, str) and value
                }
                if isinstance(authoritative_raw, (list, tuple, set, frozenset))
                else set()
            )
            current_semantic_keys = {
                _signal_semantic_key(value) for value in after.values()
            }
            for signal_id, old_document in sorted(before.items()):
                if (
                    signal_id in after
                    or str(old_document.get("code") or "") not in authoritative_codes
                    or _stage(old_document)
                    not in {
                        "armed",
                        "triggered",
                        "executable",
                    }
                    or _signal_semantic_key(old_document) in current_semantic_keys
                ):
                    continue
                old_stage = str(_stage(old_document))
                document = dict(old_document)
                document["lifecycle_stage"] = "invalidated"
                document["decision_reasons"] = list(
                    dict.fromkeys(
                        (
                            *(document.get("decision_reasons") or ()),
                            "STRUCTURE_REMOVED_ON_AUTHORITATIVE_REFRESH",
                        )
                    )
                )
                signal_event_id = notification_event_id(
                    signal_id,
                    old_stage,
                    "invalidated",
                )
                group_key = _notification_group_key(
                    document,
                    "invalidated",
                    signal_event_id,
                )
                canonical_event_id = _notification_group_event_id(group_key)
                if not self._delivered_trigger_exists(document):
                    self._record_suppressed(
                        event_id=canonical_event_id,
                        old_stage=old_stage,
                        new_stage="invalidated",
                        document=document,
                        reason="ORPHAN_INVALIDATION_WITHOUT_DELIVERED_TRIGGER",
                    )
                    dirty_state = True
                    continue
                grouped.setdefault(group_key, []).append(
                    (
                        signal_id,
                        old_stage,
                        "invalidated",
                        document,
                    )
                )

            for group_key, candidates in sorted(
                grouped.items(),
                key=_notification_group_dispatch_priority,
            ):
                event_id = _notification_group_event_id(group_key)
                legacy_event_ids = {
                    notification_event_id(candidate[0], candidate[1], candidate[2])
                    for candidate in candidates
                }
                if event_id in self._delivered or bool(
                    legacy_event_ids & self._delivered
                ):
                    if event_id not in self._delivered:
                        self._delivered.add(event_id)
                        dirty_state = True
                    if self._pending_trigger_events.pop(event_id, None) is not None:
                        dirty_state = True
                    continue
                persisted_pending = self._pending_trigger_events.get(event_id)
                candidates.sort(
                    key=lambda value: (
                        not bool(
                            set(value[3].get("selection_sources") or ())
                            & _MANUAL_ATTENTION_SOURCES
                        ),
                        not bool(
                            value[3].get("entry_allowed")
                            or value[3].get("exit_allowed")
                        ),
                        not bool(_recorded_segment(value[3])),
                        value[0],
                    )
                )
                _signal_id, old_stage, new_stage, document = candidates[0]
                # Pending payloads are restored into ``grouped`` before the
                # current snapshot is traversed. Revalidate the selected
                # payload at the irreversible send boundary as well: otherwise
                # an enrichment which expired while waiting for a webhook retry
                # could survive the current-row rejection and still render/send
                # from the already-populated pending group.
                rejection = _notification_eligibility_reason(
                    document,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    require_decision_identity=require_decision_identity,
                    evaluated_at=dispatch_now,
                )
                if rejection is not None:
                    self._pending_trigger_events.pop(event_id, None)
                    self._record_suppressed(
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=document,
                        reason=rejection,
                    )
                    dirty_state = True
                    continue
                setup_point_types = tuple(
                    sorted(
                        {
                            str(_mapping(value[3].get("setup_5m")).get("point_type"))
                            for value in candidates
                            if _mapping(value[3].get("setup_5m")).get("point_type")
                        },
                        key=lambda value: (_SETUP_POINT_ORDER.get(value, 99), value),
                    )
                )
                notification_document = dict(document)
                if len(setup_point_types) > 1:
                    notification_document["notification_setup_point_types"] = list(
                        setup_point_types
                    )
                defense_prices = tuple(
                    dict.fromkeys(
                        str(value).strip()
                        for value in (
                            _defense_price_value(_mapping(candidate[3].get("setup_5m")))
                            for candidate in candidates
                        )
                        if value is not None and str(value).strip()
                    )
                )
                if len(defense_prices) > 1:
                    notification_document["notification_defense_prices"] = list(
                        defense_prices
                    )
                detected_time = (
                    _parse_time(
                        persisted_pending.get("detected_at")
                        if isinstance(persisted_pending, Mapping)
                        else None
                    )
                    or _parse_time(notification_document.get("monitor_observed_at"))
                    or dispatch_now
                )
                expires_at = _parse_time(
                    _notification_expires_at(
                        notification_document,
                        new_stage=new_stage,
                        detected_at=detected_time,
                    )
                )
                if (
                    new_stage in _EVIDENCE_NOTIFICATION_STAGES
                    and expires_at is not None
                    and dispatch_now > expires_at
                ):
                    expired_recorded = self._record_review_notification(
                        event_id=event_id,
                        document=notification_document,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        delivery_status="expired",
                        detected_at=detected_time,
                        delivery_reason="NOTIFICATION_DELIVERY_EXPIRED",
                    )
                    if not expired_recorded:
                        self._failure_count += 1
                        self._last_failure_at = dispatch_now.isoformat()
                        self._last_failure_reason = "REVIEW_INBOX_RECORD_FAILED"
                        self._pending_trigger_events[event_id] = {
                            "old_stage": old_stage,
                            "new_stage": new_stage,
                            "queued_at": (
                                str(persisted_pending.get("queued_at"))
                                if isinstance(persisted_pending, Mapping)
                                and persisted_pending.get("queued_at")
                                else dispatch_now.isoformat()
                            ),
                            "detected_at": detected_time.isoformat(),
                            "document": notification_document,
                        }
                        self._record_audit(
                            status="failed",
                            event_id=event_id,
                            old_stage=old_stage,
                            new_stage=new_stage,
                            document=notification_document,
                            reason="REVIEW_INBOX_RECORD_FAILED",
                        )
                        self._persist()
                        continue
                    self._pending_trigger_events.pop(event_id, None)
                    self._record_suppressed(
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason="NOTIFICATION_DELIVERY_EXPIRED",
                    )
                    dirty_state = True
                    continue
                # 先完成不可逆的时效闸门，再读取逐笔价格。冷启动追赶可能一次发现大量
                # 已经过期的历史结构；这些事件只进入过期审计，不应占用页面行情进程。
                notification_document = self._with_realtime_price(
                    notification_document,
                    detected_at=dispatch_now,
                    new_stage=new_stage,
                )
                title, lines = format_notification(
                    notification_document,
                    old_stage,
                    new_stage,
                    detected_at=detected_time,
                )
                review_recorded = self._record_review_notification(
                    event_id=event_id,
                    document=notification_document,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    delivery_status="pending",
                    detected_at=detected_time,
                )
                if not review_recorded:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = "REVIEW_INBOX_RECORD_FAILED"
                    self._pending_trigger_events[event_id] = {
                        "old_stage": old_stage,
                        "new_stage": new_stage,
                        "queued_at": (
                            str(persisted_pending.get("queued_at"))
                            if isinstance(persisted_pending, Mapping)
                            and persisted_pending.get("queued_at")
                            else dispatch_now.isoformat()
                        ),
                        "detected_at": detected_time.isoformat(),
                        "document": notification_document,
                    }
                    self._record_audit(
                        status="failed",
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason="REVIEW_INBOX_RECORD_FAILED",
                    )
                    self._persist()
                    continue
                try:
                    send_rich = getattr(self._notifier, "send_rich", None)
                    if callable(send_rich):
                        code = _text(notification_document.get("code"), "")
                        name = _text(notification_document.get("name"), code)
                        setup = _mapping(notification_document.get("setup_5m"))
                        segment = _mapping(
                            notification_document.get("segment_difference_1m")
                        )
                        chart_evidence = (
                            segment if new_stage == _SEGMENT_ENRICHED_STAGE else setup
                        )
                        chart_frequency = (
                            "1m" if new_stage == _SEGMENT_ENRICHED_STAGE else "5m"
                        )
                        context = {
                            "require_evidence_match": new_stage
                            in _EVIDENCE_NOTIFICATION_STAGES,
                            "delivery_priority": (
                                _notification_document_delivery_priority(
                                    notification_document,
                                    new_stage,
                                )
                            ),
                            "expires_at": (
                                None if expires_at is None else expires_at.isoformat()
                            ),
                            "charts": [
                                {
                                    "market": _signal_market(notification_document),
                                    "code": code,
                                    "name": name,
                                    "artifact_key": event_id,
                                    "observed_at": _text(
                                        notification_document.get("observed_at"),
                                        "",
                                    ),
                                    "point_type": _text(
                                        chart_evidence.get("point_type"),
                                        "",
                                    ),
                                    "signal_time": _text(
                                        chart_evidence.get("available_at")
                                        or chart_evidence.get("confirmed_at"),
                                        "",
                                    ),
                                    "evidence_id": _text(
                                        chart_evidence.get("point_id"),
                                        "",
                                    ),
                                    "recursive_level": chart_evidence.get(
                                        "recursive_level"
                                    ),
                                    "anchor_time": _text(
                                        chart_evidence.get("anchor_at"),
                                        "",
                                    ),
                                    "frequency": chart_frequency,
                                    "evidence_required": new_stage
                                    in _EVIDENCE_NOTIFICATION_STAGES,
                                }
                            ],
                        }
                        sent = bool(send_rich(title, lines, context))
                    elif self._notifier is not None:
                        sent = bool(self._notifier.send(title, lines))
                    else:
                        sent = False
                except Exception as exc:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = type(exc).__name__
                    self._pending_trigger_events[event_id] = {
                        "old_stage": old_stage,
                        "new_stage": new_stage,
                        "queued_at": (
                            str(persisted_pending.get("queued_at"))
                            if isinstance(persisted_pending, Mapping)
                            and persisted_pending.get("queued_at")
                            else dispatch_now.isoformat()
                        ),
                        "detected_at": detected_time.isoformat(),
                        "document": notification_document,
                    }
                    self._record_audit(
                        status="failed",
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason=type(exc).__name__,
                    )
                    self._record_review_notification(
                        event_id=event_id,
                        document=notification_document,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        delivery_status="failed",
                        detected_at=detected_time,
                        delivery_reason=type(exc).__name__,
                    )
                    self._persist()
                    # Persist this occurrence and continue with independent
                    # groups; one broken symbol/renderer must not abort the
                    # remainder of the notification batch.
                    continue
                if not sent:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = "NOTIFIER_RETURNED_FALSE"
                    self._pending_trigger_events[event_id] = {
                        "old_stage": old_stage,
                        "new_stage": new_stage,
                        "queued_at": (
                            str(persisted_pending.get("queued_at"))
                            if isinstance(persisted_pending, Mapping)
                            and persisted_pending.get("queued_at")
                            else dispatch_now.isoformat()
                        ),
                        "detected_at": detected_time.isoformat(),
                        "document": notification_document,
                    }
                    self._record_audit(
                        status="failed",
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason="NOTIFIER_RETURNED_FALSE",
                    )
                    self._record_review_notification(
                        event_id=event_id,
                        document=notification_document,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        delivery_status="failed",
                        detected_at=detected_time,
                        delivery_reason="NOTIFIER_RETURNED_FALSE",
                    )
                    self._persist()
                    continue
                self._delivered.add(event_id)
                if new_stage in _EVIDENCE_NOTIFICATION_STAGES:
                    segment_occurrence = _segment_occurrence_key(
                        notification_document,
                        _SEGMENT_ENRICHED_STAGE,
                    )
                    if segment_occurrence is not None:
                        self._delivered_segment_evidence.add(
                            _segment_occurrence_event_id(segment_occurrence)
                        )
                self._pending_trigger_events.pop(event_id, None)
                self._success_count += 1
                self._last_success_at = self._now().isoformat()
                self._last_success_event_id = event_id
                deferred_delivery = bool(
                    getattr(self._notifier, "delivery_deferred", False) is True
                )
                self._record_audit(
                    status="queued" if deferred_delivery else "delivered",
                    event_id=event_id,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    document=notification_document,
                )
                # A durable outbox acknowledgement means the event is safely
                # queued, not that DingTalk has already accepted it.  The
                # outbox delivery observer updates the review inbox after the
                # actual transport result.
                if not deferred_delivery:
                    self._record_review_notification(
                        event_id=event_id,
                        document=notification_document,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        delivery_status=(
                            "simulated"
                            if getattr(self._notifier, "dry_run", False) is True
                            else "delivered"
                        ),
                        detected_at=detected_time,
                    )
                self._persist()
                dirty_state = False
            if dirty_state:
                self._persist()


__all__ = [
    "SCHEMA",
    "STRATEGY_ID",
    "SignalNotificationDispatcher",
    "format_approaching_digest",
    "format_preconfirmation_divergence_digest",
    "format_screening_completion",
    "format_notification",
    "notification_event_id",
    "screening_completion_event_id",
]
