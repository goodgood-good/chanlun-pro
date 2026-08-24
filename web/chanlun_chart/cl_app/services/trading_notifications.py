"""只读买卖点生命周期通知。

通知负责报告已经确认的结构事实，不把“买卖点已出现”偷换成“已经具备下单
资格”。三程序、月周日风险门、板块门和执行边界仍决定正式准入状态，并在
通知正文中明确披露。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
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
    segment_difference_evidence_status,
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
_STAGE_LABELS = {
    "observed": "结构观察",
    "approaching": "即将确认",
    "formed": "5分钟几何候选待锁定确认",
    "armed": "旧版等待态",
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
    boundary_expired = bool(
        side == "buy"
        and segment_difference_boundary_status(
            signal,
            evaluated_at=detected_at,
        )
        == "expired"
    )
    projected = _mapping(signal.get("notification_position_recommendation"))
    if projected and not realtime_quote_unavailable and not boundary_expired:
        return projected

    canonical = _mapping(signal.get("position_recommendation"))
    profile = _mapping(signal.get("execution_profile"))
    # A hard execution/sizing gate does not erase the structural point, but it
    # is authoritative for the displayed action. Keep the alert and force its
    # operational projection to 0% even if a partially migrated producer left
    # an inconsistent READY recommendation behind.
    recommendation = (
        "BLOCKED"
        if profile.get("hard_blocked") is True or boundary_expired
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


def _price_risk_line(
    signal: Mapping[str, object],
    setup: Mapping[str, object],
) -> str:
    current = _positive_float(signal.get("current_price"))
    anchor = _positive_float(_structure_anchor_value(setup))
    defense = _positive_float(_defense_price_value(setup))
    source_label = _current_price_label(signal.get("current_price_source"))
    price_at = _notification_time_text(signal.get("current_price_at"))
    price_time = f"（获取 {price_at}）" if price_at != "暂不可用" else ""
    parts = [
        f"{source_label}：{_current_price_text(signal.get('current_price'))}{price_time}",
        f"结构锚点：{_current_price_text(_structure_anchor_value(setup))}",
    ]
    if current is not None and anchor is not None:
        drift = (current - anchor) / anchor * 100
        parts[1] += f"（{drift:+.2f}%）"
    parts.append(f"防守价：{_defense_price_text(signal, setup)}")
    if current is not None and defense is not None:
        side = str(signal.get("side") or setup.get("side") or "")
        distance = (
            (current - defense) / current * 100
            if side == "buy"
            else (defense - current) / current * 100
        )
        parts.append(f"距防守位：{distance:+.2f}%")
    return "价格：" + "｜".join(parts)


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
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        boundary_status = segment_difference_boundary_status(
            signal,
            evaluated_at=detected_at,
        )
        if boundary_status == "current":
            return (
                "操作：1分钟区间套已完成且定位窗口有效，现已升级为精确执行候选；"
                "核对原5分钟结构与风险比例后，在其他交易软件手工决定"
            )
        if boundary_status == "expired":
            return (
                "操作：1分钟区间套证据已出现，但定位窗口已过，精确执行资格关闭；"
                "不追价，等待新的1分钟区间套"
            )
        if boundary_status == "unavailable":
            return (
                "操作：1分钟区间套证据已出现，但精确执行边界缺失；"
                "暂不生成执行比例，等待边界恢复或新的1分钟区间套"
            )
        if boundary_status == "not_applicable":
            return (
                "操作：1分钟卖出区间套已完成；核对原5分钟卖点与持有结构级别后，"
                "在其他交易软件手工决定"
            )
        return "操作：1分钟区间套状态待核对；未确认前不生成精确执行比例"

    point = str(point_type or "").strip()
    side = str(signal.get("side") or "").strip()
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
        if (
            operational.get("status") == "NOT_ACTIONABLE"
            and ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_REASON
            in operational_reasons
        ):
            return (
                "操作：5分钟买点已确认并已提醒；等待1分钟区间套精确定位，"
                "当前不生成买入比例"
            )
        if operational.get("status") == "BLOCKED":
            if operational_reasons & {
                "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",
                "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",
            }:
                return (
                    "操作：5分钟买点已达到操作确认，但当前价格已触发0%保护；"
                    "不追价，等待新的5分钟结构，仅在其他交易软件手工复核"
                )
            return (
                "操作：本条买入不纳入操作计划；"
                f"{_blocked_position_reason_text(signal, operational)}"
            )
        if operational.get("status") == "UNRESOLVED":
            if signal.get("realtime_quote_status") == "unavailable":
                return (
                    "操作：5分钟买点已达到操作确认，但实时价格未取得；"
                    "不使用已完成K线价格生成买入比例，等待实时价格证据后再复核"
                )
            return (
                "操作：5分钟买点已达到操作确认，但结构价格或防守信息不足；"
                "暂不生成买入比例，补齐证据后再复核"
            )
        if recommendation == "CAUTION" or (
            not recommendation and signal.get("entry_allowed") is not True
        ):
            return (
                "操作：5分钟技术买点已达到操作确认，需手工复核逆风环境和提示证据后，"
                "在其他交易软件手工决定；本系统不会自动下单"
            )
        if recommendation == "BLOCKED":
            return (
                "操作：本条买入不纳入操作计划；"
                f"{_blocked_position_reason_text(signal, operational)}"
            )
        action = "买入"
        if point.startswith("1buy"):
            condition = "确认反转后"
        elif point.startswith("2buy"):
            condition = "回踩不破后"
        elif point.startswith("3buy"):
            condition = "回抽确认后"
        else:
            condition = "人工确认后"
        return (
            f"操作：{condition}在其他交易软件手工确认并分批{action}；本系统不会自动下单"
        )
    if side == "sell":
        if (
            operational.get("status") == "NOT_ACTIONABLE"
            and ONE_MINUTE_SEGMENT_DIFFERENCE_REQUIRED_REASON
            in operational_reasons
        ):
            return (
                "操作：5分钟卖点已确认并已提醒；等待1分钟区间套精确定位，"
                "当前不生成退出比例"
            )
        if signal.get("exit_allowed") is not True:
            return (
                "操作：结构卖出提醒已达到操作确认；请核对卖点级别与结构仍然有效，"
                "再在其他交易软件手工决定；本系统不会自动下单"
            )
        if point.startswith("3sell"):
            return "操作：优先检查退出条件"
        if point.startswith("2sell"):
            return "操作：反弹未转强时优先复核卖出条件"
        return "操作：优先复核卖出或退出条件"
    return "操作：人工复核后再操作"


def _position_recommendation_line(
    signal: Mapping[str, object],
    *,
    detected_at: object | None = None,
    new_stage: str = "triggered",
) -> str:
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
                f"（按当前价至5分钟防守位{model_note}；"
                "仅作结构模型比较）"
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
        return (
            "风险参考：卖点与目标结构的级别关系待人工核对；同级或更高级别卖点"
            "按完整退出规则复核，低级别或不同结构仅作段差处理；"
            "关系未确认前不生成退出比例"
        )
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
    segment_evidence_status = segment_difference_evidence_status(
        signal,
        trigger=trigger,
    )
    segment_boundary_status = segment_difference_boundary_status(
        signal,
        trigger=trigger,
        evaluated_at=detected_at,
    )
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
    if new_stage in {"invalidated", "closed"}:
        headline = new_stage_label
        notification_kind = "信号失效" if new_stage == "invalidated" else "跟踪结束"
    elif new_stage == _SEGMENT_ENRICHED_STAGE:
        headline = f"5分钟{setup_point}＋1分钟区间套{trigger_evidence}"
        notification_kind = "1分钟精确定位新出现"
    else:
        headline = f"5分钟{setup_point}"
        notification_kind = (
            "买点确认·0%保护"
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
    confirmed_at = _notification_time_text(confirmed_at_value)
    available_at = _notification_time_text(available_at_value)
    discovered_at = _notification_time_text(
        detected_at or signal.get("observed_at") or available_at_value
    )
    setup_level = _recursive_level_text(
        setup.get("recursive_level")
        if "recursive_level" in setup
        else signal.get("recursive_level")
    )
    trigger_structure = (
        (f"1分钟区间套定位：{trigger_evidence}已确认；精确买入位置仍有效")
        if trigger.get("point_type")
        and segment_evidence_status == "present"
        and segment_boundary_status == "current"
        else (
            f"1分钟区间套定位：{trigger_evidence}历史证据保留；"
            "买入精确定位窗口已过"
        )
        if trigger.get("point_type")
        and segment_evidence_status == "present"
        and segment_boundary_status == "expired"
        else (
            f"1分钟区间套定位：{trigger_evidence}历史证据保留；"
            "买入精确定位边界不可用"
        )
        if trigger.get("point_type")
        and segment_evidence_status == "present"
        and segment_boundary_status == "unavailable"
        else (
            f"1分钟区间套定位：{trigger_evidence}已确认；"
            "卖出精确位置已确认"
        )
        if trigger.get("point_type")
        and segment_evidence_status == "present"
        and segment_boundary_status == "not_applicable"
        else (f"1分钟区间套定位：{trigger_evidence}证据已记录；精确定位边界待核对")
        if trigger.get("point_type") and segment_evidence_status == "present"
        else "1分钟区间套：暂未出现（5分钟信号保留，精确执行尚未解锁）"
    )
    detected_at_value = (
        detected_at or signal.get("monitor_observed_at") or signal.get("observed_at")
    )
    confirmation_caption = (
        "操作确认（末端结构已封存）"
        if setup.get("lock_state") == "locked"
        else "操作确认（末端结构仍会随新K更新）"
    )
    if new_stage == _SEGMENT_ENRICHED_STAGE:
        time_parts = [
            f"1分钟定位确认 {_notification_time_text(_segment_time(signal))}",
            f"原5分钟{confirmation_caption} {confirmed_at}",
        ]
    else:
        time_parts = [f"{confirmation_caption} {confirmed_at}"]
        if available_at != confirmed_at:
            time_parts.append(f"信号可用 {available_at}")
    time_parts.append(
        f"监听发现：{discovered_at}（延迟 "
        f"{_elapsed_text(event_available_at_value, detected_at_value)}）"
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
    lines = [
        (
            f"股票：{name}｜状态：{operation_status}｜"
            f"进度：{old_stage_label}→{new_stage_label}"
        ),
        _price_risk_line(signal, setup),
        _position_recommendation_line(
            signal,
            detected_at=detected_at,
            new_stage=new_stage,
        ),
        "时间：" + "｜".join(time_parts),
        _terminal_segment_text(setup),
        (
            f"依据：5分钟{setup_point}（{setup_level}）｜{context_text}｜"
            f"{trigger_structure}"
        ),
        (
            f"环境：{_text(execution_profile.get('context_grade_label'), '待判定')}｜"
            f"板块：{_text(sector.get('sector_name'))}"
        ),
        (
            "技术："
            + _same_period_context_text(daily_context, "日线").replace("：", " ", 1)
            + "｜"
            + _same_period_context_text(context, "30分钟").replace("：", " ", 1)
        ),
        _action_advice(
            signal,
            point_type=effective_point_type,
            scope=scope,
            new_stage=new_stage,
            detected_at=detected_at,
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
            {"suppressed_fingerprints", "delivered_segment_evidence_ids"}
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
        trigger = _mapping(document.get("segment_difference_1m"))
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
    "format_notification",
    "notification_event_id",
]
