"""Durable, read-only inbox for realtime structure notifications.

The DingTalk transports are intentionally independent from this inbox: a valid
structure event must remain available for human review even when delivery is
temporarily unavailable.  The inbox stores only compact structure facts and
never contains credentials, account data or order instructions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import threading
from typing import Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.models import (
    CANONICAL_POINT_TYPE_SET,
)
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
    is_one_minute_segment_level,
)


SCHEMA = "chanlun-realtime-review-inbox"
EVENT_SCHEMA = "chanlun-realtime-review-notification"
CN = ZoneInfo("Asia/Shanghai")
_MAX_EVENTS = 500
_DELIVERY_STATUSES = frozenset(
    {
        "pending",
        "delivered",
        "simulated",
        "failed",
        "expired",
    }
)
_SOURCES = frozenset(
    {
        "A_SHARE_STRICT_DECISION_CORE",
        "CROSS_MARKET_ATTENTION_MONITOR",
    }
)
_SEGMENT_DIFFERENCE_STATUSES = frozenset(
    {
        "absent",
        "current",
        "expired",
        "unavailable",
        "unknown",
    }
)
_SEGMENT_DIFFERENCE_EVIDENCE_STATUSES = frozenset(
    {
        "absent",
        "present",
        "unknown",
    }
)
_SEGMENT_DIFFERENCE_BOUNDARY_STATUSES = frozenset(
    {
        "absent",
        "current",
        "expired",
        "unavailable",
        "unknown",
        "not_applicable",
    }
)
_SETUP_LOCK_STATES = frozenset({"pending", "locked", "unknown"})
_DIVERGENCE_KINDS = frozenset({"trend", "consolidation"})
_EXPIRED_SEGMENT_BOUNDARY = "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
_MISSING_SEGMENT_BOUNDARY = "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING"
_ACCOUNT_COUPLED_POSITION_CODE_TERMS = (
    "ACCOUNT",
    "EQUITY",
    "PORTFOLIO",
    "CASH",
    "CURRENT_POSITION",
    "POSITION_RATIO",
    "POSITION_STRUCTURE",
)
_ACCOUNT_COUPLED_POSITION_LABEL_TERMS = (
    "账户",
    "持仓",
    "仓位",
    "权益",
    "资金",
    "现金",
    "持有数量",
    "组合热度",
)


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=CN)
    return value.astimezone(CN)


def _aware_iso(value: object, *, fallback: datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            parsed = fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=CN)
    return parsed.astimezone(CN).isoformat()


def _optional_aware_iso(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=CN)
    return parsed.astimezone(CN).isoformat()


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CN)


def _reason_code_set(document: Mapping[str, object]) -> set[str]:
    values: list[object] = []
    raw_reasons = document.get("decision_reasons")
    if isinstance(raw_reasons, Sequence) and not isinstance(
        raw_reasons, (str, bytes, bytearray)
    ):
        values.extend(raw_reasons)
    profile = document.get("execution_profile")
    if isinstance(profile, Mapping):
        for field in ("advisory_reason_codes", "hard_block_reason_codes"):
            raw_profile_reasons = profile.get(field)
            if isinstance(raw_profile_reasons, Sequence) and not isinstance(
                raw_profile_reasons, (str, bytes, bytearray)
            ):
                values.extend(raw_profile_reasons)
    return {str(value) for value in values if str(value).strip()}


def segment_difference_review_status(
    document: Mapping[str, object],
    *,
    trigger: Mapping[str, object] | None = None,
    evaluated_at: object | None = None,
) -> str:
    """Classify recorded 1m evidence without overstating its current validity."""

    if trigger is None:
        raw_trigger = document.get("segment_difference_1m") or document.get(
            "trigger_1m"
        )
        trigger = raw_trigger if isinstance(raw_trigger, Mapping) else {}
    if not trigger:
        return "absent"

    reasons = _reason_code_set(document)
    if _EXPIRED_SEGMENT_BOUNDARY in reasons:
        return "expired"

    boundary = document.get("entry_execution_boundary")
    if isinstance(boundary, Mapping):
        raw_valid_until = boundary.get("entry_valid_until")
        if raw_valid_until not in (None, ""):
            valid_until = _aware_datetime(raw_valid_until)
            observed_at = _aware_datetime(
                document.get("observed_at")
                if evaluated_at is None
                else evaluated_at
            )
            if valid_until is None or observed_at is None:
                return "unavailable"
            if valid_until <= observed_at:
                return "expired"

    if _MISSING_SEGMENT_BOUNDARY in reasons:
        return "unavailable"
    return "current"


def segment_difference_evidence_status(
    document: Mapping[str, object],
    *,
    trigger: Mapping[str, object] | None = None,
) -> str:
    """Report whether a confirmed 1m segment-difference fact was recorded.

    A structural fact and the short-lived entry locator are different axes.
    Expiring the locator must never erase the fact that the 1m point existed.
    """

    if trigger is None:
        raw_trigger = document.get("segment_difference_1m") or document.get(
            "trigger_1m"
        )
        trigger = raw_trigger if isinstance(raw_trigger, Mapping) else {}
    return "present" if trigger else "absent"


def segment_difference_boundary_status(
    document: Mapping[str, object],
    *,
    trigger: Mapping[str, object] | None = None,
    evaluated_at: object | None = None,
) -> str:
    """Classify only the optional buy-entry locator boundary.

    Sell-side segment evidence has no buy-entry boundary by construction, so
    it is ``not_applicable`` rather than silently being labelled ``current``.
    """

    if trigger is None:
        raw_trigger = document.get("segment_difference_1m") or document.get(
            "trigger_1m"
        )
        trigger = raw_trigger if isinstance(raw_trigger, Mapping) else {}
    if not trigger:
        return "absent"
    side = str(document.get("side") or trigger.get("side") or "")
    if side != "buy":
        return "not_applicable"

    legacy = segment_difference_review_status(
        document,
        trigger=trigger,
        evaluated_at=evaluated_at,
    )
    if legacy in {"current", "expired", "unavailable", "unknown"}:
        return legacy
    return "unknown"


def _is_delivered_status(value: object) -> bool:
    return value in {"delivered", "simulated"}


def _account_coupled_position_recommendation(value: object) -> bool:
    """Return whether a recommendation still depends on unavailable account facts."""

    if not isinstance(value, Mapping):
        return False
    label = str(value.get("label") or "")
    if any(term in label for term in _ACCOUNT_COUPLED_POSITION_LABEL_TERMS):
        return True
    code_values = [value.get("basis")]
    reasons = value.get("reason_codes")
    if isinstance(reasons, Sequence) and not isinstance(
        reasons, (str, bytes, bytearray)
    ):
        code_values.extend(reasons)
    rendered_codes = "|".join(str(item or "").upper() for item in code_values)
    return any(term in rendered_codes for term in _ACCOUNT_COUPLED_POSITION_CODE_TERMS)


def _legacy_structural_position_recommendation(
    value: Mapping[str, object],
    *,
    side: str,
) -> dict[str, object]:
    """Project old account-coupled guidance onto structure facts only.

    Historical delivery and signal evidence remain unchanged.  A sell ratio
    cannot be selected without knowing its relation to the target structure,
    while a valid legacy buy ratio is retained only as a structure-model
    comparison value.  Malformed or unresolved buy guidance stays unresolved.
    """

    common: dict[str, object] = {
        "side": side,
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
        "segment_difference_max_ratio": "0.25",
        "segment_difference_max_percent": "25",
    }
    if side == "sell":
        return {
            **common,
            "status": "CONDITIONAL",
            "basis": "STRUCTURAL_EXIT_LEVEL_REQUIRED",
            "recommended_ratio": None,
            "recommended_percent": None,
            "label": (
                "结构退出参考：需先人工核对卖点与目标结构的级别关系；"
                "5分钟同级或更高级别卖点按完整退出规则处理，低级别或不同"
                "结构卖点只按段差规则处理，参考上限 25%（仅表达结构规则）"
            ),
            "reason_codes": ["SELL_STRUCTURE_RELATION_REQUIRED"],
            "conditional_options": [
                {
                    "condition": "FIVE_MINUTE_SAME_OR_HIGHER_LEVEL_EXIT",
                    "recommended_ratio": "1",
                    "recommended_percent": "100",
                },
                {
                    "condition": "FIVE_MINUTE_LOWER_OR_DIFFERENT_STRUCTURE_REDUCTION",
                    "recommended_ratio": "0.25",
                    "recommended_percent": "25",
                },
            ],
        }

    status = str(value.get("status") or "")
    raw_ratio = value.get("recommended_ratio")
    raw_percent = value.get("recommended_percent")
    try:
        ratio = Decimal(str(raw_ratio))
        percent = Decimal(str(raw_percent))
    except (InvalidOperation, TypeError, ValueError):
        ratio = Decimal("-1")
        percent = Decimal("-1")
    if (
        status == "RECOMMENDED"
        and ratio.is_finite()
        and percent.is_finite()
        and Decimal("0") <= ratio <= Decimal("1")
        and percent == ratio * Decimal("100")
    ):
        return {
            **common,
            "status": "RECOMMENDED",
            "basis": "STRUCTURAL_RISK_MODEL_UPPER_BOUND",
            "recommended_ratio": str(raw_ratio),
            "recommended_percent": str(raw_percent),
            "label": (
                f"结构风险参考比例：{raw_percent}% 以内（历史通知仅保留结构模型值）"
            ),
            "reason_codes": ["LEGACY_STRUCTURAL_RISK_MODEL_RATIO"],
            "conditional_options": [],
        }
    if status == "BLOCKED":
        return {
            **common,
            "status": "BLOCKED",
            "basis": "NO_TRADE",
            "recommended_ratio": "0",
            "recommended_percent": "0",
            "label": ("结构风险参考：本条买入不纳入操作计划（历史限制原因需重新核对）"),
            "reason_codes": ["LEGACY_BUY_RESTRICTION_REQUIRES_REVIEW"],
            "conditional_options": [],
        }
    return {
        **common,
        "status": "UNRESOLVED",
        "basis": "LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED",
        "recommended_ratio": None,
        "recommended_percent": None,
        "label": "历史通知缺少完整结构价格或风险参数，结构风险参考待重新核对",
        "reason_codes": ["LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED"],
        "conditional_options": [],
    }


def _normalize_event(
    raw: object,
    *,
    migrate_account_coupled_position: bool = False,
    migrate_non_strict_segment: bool = False,
) -> dict[str, object]:
    """Fill the explicit time contract for records written before this schema.

    Legacy fields stay in place for older front ends.  Missing semantic fields
    are derived only from already validated legacy timestamps; supplied new
    fields are never silently repaired.
    """

    if not isinstance(raw, Mapping):
        return {}
    normalized = dict(raw)
    legacy_cross_market_attention = (
        normalized.get("source") == "CROSS_MARKET_HOLDING_MONITOR"
    )
    signal_time = normalized.get("signal_time")
    recorded_at = normalized.get("recorded_at")
    observed_at = normalized.get("observed_at") or recorded_at
    if not normalized.get("signal_available_at"):
        normalized["signal_available_at"] = signal_time
    if not normalized.get("structure_confirmed_at"):
        normalized["structure_confirmed_at"] = signal_time
    if "structure_anchor_time" not in normalized:
        normalized["structure_anchor_time"] = _optional_aware_iso(
            normalized.get("anchor_time")
        )
    if not normalized.get("detected_at"):
        # 旧跨市场记录把 ``observed_at`` 写成了信号可用时刻；其真正的
        # 首次监听入箱时间只能由 ``first_recorded_at`` 恢复。A 股旧记录
        # 的 observed_at 本来就是决策核心发现该结构的时刻。
        normalized["detected_at"] = (
            (normalized.get("first_recorded_at") or recorded_at)
            if legacy_cross_market_attention
            else (observed_at or normalized.get("first_recorded_at") or recorded_at)
        )
    if not normalized.get("delivery_updated_at"):
        normalized["delivery_updated_at"] = recorded_at
    if "delivered_at" not in normalized:
        normalized["delivered_at"] = (
            recorded_at
            if _is_delivered_status(normalized.get("delivery_status"))
            else None
        )
    if normalized.get("current_price") is not None and not normalized.get(
        "current_price_source"
    ):
        normalized["current_price_source"] = "latest_completed_bar_close"
    if legacy_cross_market_attention and normalized.get("current_price") is None:
        # Early cross-market records kept the completed-bar price only under
        # ``reference_price``. Restore it explicitly without mislabelling it as
        # a realtime quote.
        try:
            legacy_price = float(normalized.get("reference_price"))
        except (TypeError, ValueError):
            legacy_price = 0.0
        if legacy_price > 0 and isfinite(legacy_price):
            normalized["current_price"] = normalized["reference_price"]
            normalized["current_price_source"] = "legacy_completed_bar_close"
    if legacy_cross_market_attention:
        normalized["source"] = "CROSS_MARKET_ATTENTION_MONITOR"
    if "is_manual_attention" not in normalized:
        normalized["is_manual_attention"] = normalized.get("is_holding") is True
    normalized.pop("is_holding", None)
    raw_selection_sources = normalized.get("selection_sources")
    if isinstance(raw_selection_sources, list):
        normalized["selection_sources"] = list(
            dict.fromkeys(
                "MANUAL_ATTENTION_MONITOR"
                if isinstance(value, str)
                and value in {"HOLDING_MONITOR", "VIRTUAL_HOLDING_MONITOR"}
                else value
                for value in raw_selection_sources
            )
        )
    if normalized.get("source_frequency") == "5m":
        normalized.setdefault("recursive_level", 0)
    # Older notification rows did not preserve whether the operation-confirmed
    # point had also completed the later anti-repaint audit lock.  Keep that
    # absence explicit instead of silently presenting an old row as locked.
    normalized.setdefault("setup_lock_state", "unknown")
    if not isinstance(normalized.get("position_recommendation"), Mapping):
        side = _text(normalized.get("side"))
        if side in {"buy", "sell"}:
            normalized["position_recommendation"] = {
                "side": side,
                "status": "UNRESOLVED",
                "basis": "LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED",
                "recommended_ratio": None,
                "recommended_percent": None,
                "label": (
                    f"历史通知未记录建议{'买入' if side == 'buy' else '卖出'}比例；"
                    "请按当前结构与操作级别人工核对"
                ),
                "reason_codes": ["LEGACY_STRUCTURAL_RISK_INPUT_UNRESOLVED"],
                "conditional_options": [],
                "manual_confirmation_required": True,
                "automated_order_authorized": False,
            }
    elif migrate_account_coupled_position and (
        _account_coupled_position_recommendation(normalized["position_recommendation"])
    ):
        side = _text(normalized.get("side"))
        if side in {"buy", "sell"}:
            normalized["position_recommendation"] = (
                _legacy_structural_position_recommendation(
                    normalized["position_recommendation"],
                    side=side,
                )
            )
    historical_recommendation = normalized.get("position_recommendation_at_detection")
    if (
        migrate_account_coupled_position
        and isinstance(historical_recommendation, Mapping)
        and _account_coupled_position_recommendation(historical_recommendation)
    ):
        side = _text(normalized.get("side"))
        if side in {"buy", "sell"}:
            normalized["position_recommendation_at_detection"] = (
                _legacy_structural_position_recommendation(
                    historical_recommendation,
                    side=side,
                )
            )
    segment_present = normalized.get("segment_difference_present") is True
    segment_recursive_level = normalized.get("segment_difference_recursive_level")
    if (
        migrate_non_strict_segment
        and segment_present
        and not is_one_minute_segment_level(
            "1m",
            segment_recursive_level,
        )
    ):
        # Older builds incorrectly advertised physical 1m/L1 (effective 5m)
        # as subordinate one-minute segment-difference evidence.  Preserve the
        # original fact for audit, but remove it from the strict segment lane.
        normalized["legacy_non_strict_segment_difference"] = {
            key: normalized.get(key)
            for key in (
                "segment_difference_point_type",
                "segment_difference_evidence_id",
                "segment_difference_anchor_time",
                "segment_difference_confirmed_at",
                "segment_difference_available_at",
                "segment_difference_recursive_level",
                "segment_difference_divergence_kind",
            )
        }
        normalized["legacy_semantics"] = (
            "ONE_MINUTE_L1_EFFECTIVE_5M_NOT_STRICT_SEGMENT"
        )
        normalized.update(
            {
                "segment_difference_present": False,
                "segment_difference_status": "absent",
                "segment_difference_current": False,
                "segment_difference_evidence_status": "absent",
                "segment_difference_boundary_status": "absent",
                "segment_difference_valid_until": None,
                "segment_difference_point_type": None,
                "segment_difference_evidence_id": None,
                "segment_difference_anchor_time": None,
                "segment_difference_confirmed_at": None,
                "segment_difference_available_at": None,
                "segment_difference_recursive_level": None,
                "segment_difference_divergence_kind": None,
            }
        )
        segment_present = False
    if "segment_difference_status" not in normalized:
        # Old inbox rows did not persist the execution boundary or its
        # decision reasons.  Their evidence remains visible for audit, but it
        # must not be silently promoted to a currently valid segment.
        normalized["segment_difference_status"] = (
            "unknown" if segment_present else "absent"
        )
    if "segment_difference_current" not in normalized:
        normalized["segment_difference_current"] = (
            normalized.get("segment_difference_status") == "current"
        )
    normalized.setdefault(
        "segment_difference_evidence_status",
        "present" if segment_present else "absent",
    )
    normalized.setdefault(
        "segment_difference_boundary_status",
        (
            "absent"
            if not segment_present
            else "not_applicable"
            if normalized.get("side") == "sell"
            else normalized.get("segment_difference_status", "unknown")
        ),
    )
    normalized.setdefault("segment_difference_valid_until", None)
    normalized.setdefault("segment_difference_divergence_kind", None)
    if (
        normalized.get("source_frequency") == "1m"
        and normalized.get("trigger_frequency") == "1m"
    ):
        # 旧通知只作为审计历史保留；不能迁移成或冒充当前 5 分钟正式信号。
        normalized["legacy_semantics"] = (
            "ONE_MINUTE_NOTIFICATION_BEFORE_5M_TRADE_CONTRACT"
        )
        normalized.setdefault("segment_difference_present", False)
    return normalized


def _event_sort_time(event: Mapping[str, object]) -> str:
    return str(
        event.get("delivered_at")
        or event.get("delivery_updated_at")
        or event.get("detected_at")
        or event.get("signal_available_at")
        or event.get("signal_time")
        or ""
    )


def _text(value: object, default: str = "") -> str:
    rendered = str(value or "").strip()
    return rendered if rendered else default


def _integer(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [rendered for item in value if (rendered := _text(item))]


def _valid_position_recommendation(value: object, *, side: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if _account_coupled_position_recommendation(value):
        return False
    if not bool(
        value.get("side") == side
        and value.get("status")
        in {"RECOMMENDED", "CONDITIONAL", "UNRESOLVED", "NOT_ACTIONABLE", "BLOCKED"}
        and isinstance(value.get("basis"), str)
        and isinstance(value.get("label"), str)
        and str(value["label"]).strip()
        and isinstance(value.get("reason_codes"), list)
        and value.get("manual_confirmation_required") is True
        and value.get("automated_order_authorized") is False
    ):
        return False

    def ratio(raw: object) -> Decimal | None:
        if raw is None:
            return None
        try:
            parsed = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() and Decimal("0") <= parsed <= 1 else None

    status = str(value.get("status"))
    recommended_ratio = ratio(value.get("recommended_ratio"))
    recommended_percent = value.get("recommended_percent")
    if status in {"RECOMMENDED", "BLOCKED"}:
        if recommended_ratio is None or not isinstance(recommended_percent, str):
            return False
        try:
            percent = Decimal(recommended_percent)
        except (InvalidOperation, ValueError):
            return False
        if not percent.is_finite() or percent != recommended_ratio * Decimal("100"):
            return False
    elif value.get("recommended_ratio") is not None or recommended_percent is not None:
        return False

    options = value.get("conditional_options", [])
    if not isinstance(options, list):
        return False
    if status == "CONDITIONAL" and not options:
        return False
    for option in options:
        if (
            not isinstance(option, Mapping)
            or not isinstance(option.get("condition"), str)
            or not str(option["condition"]).strip()
            or ratio(option.get("recommended_ratio")) is None
            or not isinstance(option.get("recommended_percent"), str)
        ):
            return False
        try:
            option_percent = Decimal(str(option["recommended_percent"]))
            option_ratio = Decimal(str(option["recommended_ratio"]))
        except (InvalidOperation, ValueError):
            return False
        if not option_percent.is_finite() or option_percent != (
            option_ratio * Decimal("100")
        ):
            return False
    return True


def _market(value: object, code: str) -> str:
    explicit = _text(value).lower()
    if explicit:
        return explicit
    upper = code.upper()
    if upper.endswith(".US"):
        return "us"
    if upper.startswith("HK.") or upper.endswith(".HK"):
        return "hk"
    return "a"


def _event_id(value: object) -> str:
    raw = _text(value)
    if raw.startswith("sha256:") and len(raw) == 71:
        return raw
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _chart_urls(market: str, code: str) -> dict[str, str]:
    intervals = {"d": "D", "30m": "30", "5m": "5", "1m": "1"}
    common = {"market": market, "code": code, "layout": "single"}
    return {
        frequency: "/?" + urlencode({**common, "intervals": interval})
        for frequency, interval in intervals.items()
    }


def a_share_notification_event(
    *,
    event_id: str,
    document: Mapping[str, object],
    old_stage: str,
    new_stage: str,
    delivery_status: str,
    recorded_at: datetime,
    detected_at: datetime | str | None = None,
    delivery_reason: str | None = None,
) -> dict[str, object]:
    """Build the compact review projection for one strict A-share alert."""

    setup = document.get("setup_5m")
    setup = setup if isinstance(setup, Mapping) else {}
    trigger = document.get("segment_difference_1m") or document.get("trigger_1m")
    trigger = trigger if isinstance(trigger, Mapping) else {}
    if trigger and not is_one_minute_segment_level(
        "1m",
        _integer(trigger.get("recursive_level"), -1),
    ):
        raise ValueError(
            "realtime review segment difference must use physical 1m level L0"
        )
    code = _text(document.get("code"))
    if not code:
        raise ValueError("realtime review notification code is required")
    side = _text(document.get("side") or setup.get("side"))
    if side not in {"buy", "sell"}:
        raise ValueError("realtime review notification side is invalid")
    point_type = _text(setup.get("point_type") or document.get("point_type"))
    setup_recursive_level = _integer(setup.get("recursive_level"))
    if not is_five_minute_trade_level("5m", setup_recursive_level):
        raise ValueError("realtime review notification must use physical 5m level L0")
    market = _market(document.get("market"), code)
    selection_sources = _text_list(document.get("selection_sources"))
    signal_available_at = _aware_iso(
        setup.get("available_at")
        or setup.get("confirmed_at")
        or document.get("observed_at"),
        fallback=recorded_at,
    )
    structure_confirmed_at = _aware_iso(
        setup.get("confirmed_at") or signal_available_at,
        fallback=recorded_at,
    )
    structure_anchor_time = _optional_aware_iso(setup.get("anchor_at"))
    # 通知投影按最终可见价格和真实发现时间重新测算；规范决策文档仍保留原始
    # 结构判断。人工复核应优先看到前者，避免把锚点仓位误当成当前价仓位。
    position_recommendation = document.get(
        "notification_position_recommendation",
        document.get("position_recommendation"),
    )
    position_recommendation = (
        dict(position_recommendation)
        if isinstance(position_recommendation, Mapping)
        else None
    )
    detected_at = _aware_iso(
        detected_at
        or document.get("monitor_observed_at")
        or document.get("observed_at"),
        fallback=recorded_at,
    )
    recorded_at_text = _aware_iso(recorded_at, fallback=recorded_at)
    segment_status = segment_difference_review_status(
        document,
        trigger=trigger,
        evaluated_at=recorded_at_text,
    )
    segment_evidence_status = segment_difference_evidence_status(
        document,
        trigger=trigger,
    )
    segment_boundary_status = segment_difference_boundary_status(
        document,
        trigger=trigger,
        evaluated_at=recorded_at_text,
    )
    raw_boundary = document.get("entry_execution_boundary")
    segment_valid_until = (
        _optional_aware_iso(raw_boundary.get("entry_valid_until"))
        if trigger and isinstance(raw_boundary, Mapping)
        else None
    )
    setup_lock_state = _text(setup.get("lock_state"), "unknown")
    if setup_lock_state not in _SETUP_LOCK_STATES:
        setup_lock_state = "unknown"
    return {
        "schema": EVENT_SCHEMA,
        "notification_id": _event_id(event_id),
        "source": "A_SHARE_STRICT_DECISION_CORE",
        "market": market,
        "code": code,
        "name": _text(document.get("name"), code),
        "side": side,
        "point_type": point_type,
        "source_frequency": "5m",
        "trigger_frequency": None,
        "segment_difference_frequency": "1m",
        "segment_difference_present": bool(trigger),
        "segment_difference_status": segment_status,
        "segment_difference_current": segment_status == "current",
        "segment_difference_evidence_status": segment_evidence_status,
        "segment_difference_boundary_status": segment_boundary_status,
        "segment_difference_valid_until": segment_valid_until,
        "segment_difference_point_type": (
            _text(trigger.get("point_type")) if trigger else None
        ),
        "segment_difference_evidence_id": (
            _text(trigger.get("point_id")) if trigger else None
        ),
        "segment_difference_anchor_time": (
            _optional_aware_iso(trigger.get("anchor_at")) if trigger else None
        ),
        "segment_difference_confirmed_at": (
            _optional_aware_iso(
                trigger.get("confirmed_at") or trigger.get("available_at")
            )
            if trigger
            else None
        ),
        "segment_difference_available_at": (
            _optional_aware_iso(
                trigger.get("available_at") or trigger.get("confirmed_at")
            )
            if trigger
            else None
        ),
        "segment_difference_recursive_level": (
            _integer(trigger.get("recursive_level")) if trigger else None
        ),
        "segment_difference_divergence_kind": (
            (_text(trigger.get("divergence_kind")) or None) if trigger else None
        ),
        # ``signal_time`` / ``anchor_time`` remain as compatibility aliases.
        "signal_time": signal_available_at,
        "observed_at": detected_at,
        "recorded_at": recorded_at_text,
        "structure_anchor_time": structure_anchor_time,
        "structure_confirmed_at": structure_confirmed_at,
        "setup_lock_state": setup_lock_state,
        "signal_available_at": signal_available_at,
        "detected_at": detected_at,
        "delivery_updated_at": recorded_at_text,
        "delivered_at": (
            recorded_at_text if _is_delivered_status(delivery_status) else None
        ),
        "old_stage": _text(old_stage),
        "new_stage": _text(new_stage, "triggered"),
        "delivery_status": delivery_status,
        "delivery_reason": delivery_reason,
        "evidence_id": _text(setup.get("point_id")),
        "recursive_level": setup_recursive_level,
        "anchor_time": _text(setup.get("anchor_at")),
        "current_price": document.get("current_price"),
        "current_price_source": _text(
            document.get("current_price_source"),
            "latest_completed_1m_close",
        ),
        "signal_qualification": "30m_context_5m_trade_signal_1m_segment_optional",
        "position_recommendation": position_recommendation,
        "reference_price": setup.get("anchor_price")
        or setup.get("structure_anchor_price")
        or document.get("reference_price"),
        "invalidation_price": setup.get("invalidation_price")
        or document.get("structural_stop"),
        "big_direction": _text(
            (document.get("context_30m") or {}).get("direction")
            if isinstance(document.get("context_30m"), Mapping)
            else ""
        ),
        "mid_direction": "",
        "is_manual_attention": bool(
            set(selection_sources)
            & {
                "MANUAL_ATTENTION_MONITOR",
                "HOLDING_MONITOR",
                "VIRTUAL_HOLDING_MONITOR",
            }
        ),
        "selection_sources": [
            "MANUAL_ATTENTION_MONITOR"
            if value in {"HOLDING_MONITOR", "VIRTUAL_HOLDING_MONITOR"}
            else value
            for value in selection_sources
        ],
        "chart_urls": _chart_urls(market, code),
        "review_required": True,
        "automated_action_authorized": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


def monitor_notification_event(
    *,
    market: str,
    event: object,
    delivery_status: str,
    recorded_at: datetime,
    delivery_reason: str | None = None,
) -> dict[str, object]:
    """Build a review projection from a strict cross-market monitor event."""

    code = _text(getattr(event, "code", None))
    identity = _text(
        getattr(event, "delivery_identity", None) or getattr(event, "identity", None)
    )
    if not code or not identity:
        raise ValueError("holding monitor notification identity is incomplete")
    signal_role = _text(getattr(event, "signal_role", None), "TRADE_SIGNAL_5M")
    if signal_role not in {"TRADE_SIGNAL_5M", "SEGMENT_DIFFERENCE_1M"}:
        raise ValueError("holding monitor notification role is invalid")
    segment_update = signal_role == "SEGMENT_DIFFERENCE_1M"
    raw_side = _text(getattr(event, "side", None))
    side = raw_side
    if side not in {"buy", "sell"}:
        raise ValueError("holding monitor notification side is invalid")
    normalized_market = _market(market, code)
    signal_available_at = _aware_iso(
        (
            getattr(event, "setup_available_time", None)
            if segment_update
            else getattr(event, "signal_time", None)
        ),
        fallback=recorded_at,
    )
    structure_confirmed_at = _aware_iso(
        (
            getattr(event, "setup_confirmed_time", None)
            if segment_update
            else getattr(event, "confirmed_time", None)
        )
        or signal_available_at,
        fallback=recorded_at,
    )
    structure_anchor_time = _optional_aware_iso(
        (
            getattr(event, "setup_anchor_time", None)
            if segment_update
            else getattr(event, "anchor_time", None)
        )
    )
    detected_at = _aware_iso(
        getattr(event, "detected_time", None), fallback=recorded_at
    )
    recorded_at_text = _aware_iso(recorded_at, fallback=recorded_at)
    trigger_point_type = _text(getattr(event, "bs_type", None))
    recursive_level = _integer(getattr(event, "recursive_level", 0))
    if (
        not trigger_point_type
        or not _text(getattr(event, "evidence_id", None))
        or _text(getattr(event, "op_level", None), "5m") != "5m"
        or not is_five_minute_trade_level("5m", recursive_level)
    ):
        raise ValueError("holding monitor 5m trade evidence is incomplete")
    segment_point_type = _text(getattr(event, "segment_difference_point_type", None))
    segment_present = bool(segment_point_type)
    segment_divergence_kind = (
        _text(getattr(event, "segment_difference_divergence_kind", None)) or None
    )
    segment_recursive_level = _integer(
        getattr(event, "segment_difference_recursive_level", None),
        -1,
    )
    if segment_present and not is_one_minute_segment_level(
        "1m",
        segment_recursive_level,
    ):
        raise ValueError(
            "monitor segment difference must use physical 1m level L0"
        )
    if segment_update and not segment_present:
        raise ValueError("segment enrichment notification requires 1m evidence")
    segment_available_at = (
        _optional_aware_iso(
            getattr(event, "segment_difference_available_time", None)
        )
        if segment_present
        else None
    )
    segment_valid_until = None
    segment_status = "absent"
    if segment_present:
        if raw_side == "sell":
            segment_status = "current"
        elif segment_available_at is None:
            segment_status = "unknown"
        elif normalized_market != "a":
            # Only A-share raw 1m execution boundaries are currently attested.
            # Other markets retain the locator evidence without inventing a TTL.
            segment_status = "unavailable"
        else:
            try:
                segment_deadline = a_share_optional_entry_valid_until(
                    datetime.fromisoformat(segment_available_at)
                )
            except (TypeError, ValueError):
                segment_status = "unavailable"
            else:
                segment_valid_until = segment_deadline.isoformat(timespec="seconds")
                segment_status = (
                    "current"
                    if recorded_at.astimezone(CN) < segment_deadline.astimezone(CN)
                    else "expired"
                )
    setup_lock_state = _text(
        getattr(event, "setup_lock_state", None),
        "unknown",
    )
    if setup_lock_state not in _SETUP_LOCK_STATES:
        setup_lock_state = "unknown"
    return {
        "schema": EVENT_SCHEMA,
        "notification_id": _event_id(identity),
        "source": "CROSS_MARKET_ATTENTION_MONITOR",
        "market": normalized_market,
        "code": code,
        "name": _text(getattr(event, "name", None), code),
        "side": side,
        "point_type": trigger_point_type,
        "trigger_point_type": None,
        "source_frequency": "5m",
        "trigger_frequency": None,
        "segment_difference_frequency": "1m",
        "segment_difference_present": segment_present,
        "segment_difference_status": segment_status,
        "segment_difference_current": segment_status == "current",
        "segment_difference_evidence_status": (
            "present" if segment_present else "absent"
        ),
        "segment_difference_boundary_status": (
            "absent"
            if not segment_present
            else "not_applicable"
            if side == "sell"
            else segment_status
        ),
        "segment_difference_valid_until": segment_valid_until,
        "segment_difference_point_type": (
            segment_point_type if segment_present else None
        ),
        "segment_difference_evidence_id": (
            _text(getattr(event, "segment_difference_evidence_id", None))
            if segment_present
            else None
        ),
        "segment_difference_anchor_time": (
            _optional_aware_iso(getattr(event, "segment_difference_anchor_time", None))
            if segment_present
            else None
        ),
        "segment_difference_confirmed_at": (
            _optional_aware_iso(
                getattr(event, "segment_difference_confirmed_time", None)
            )
            if segment_present
            else None
        ),
        "segment_difference_available_at": segment_available_at,
        "segment_difference_recursive_level": (
            segment_recursive_level if segment_present else None
        ),
        "segment_difference_divergence_kind": (
            segment_divergence_kind if segment_present else None
        ),
        "signal_time": (
            segment_available_at
            if segment_update and segment_available_at is not None
            else signal_available_at
        ),
        "observed_at": detected_at,
        "recorded_at": recorded_at_text,
        "structure_anchor_time": structure_anchor_time,
        "structure_confirmed_at": structure_confirmed_at,
        "setup_lock_state": setup_lock_state,
        "signal_available_at": signal_available_at,
        "detected_at": detected_at,
        "delivery_updated_at": recorded_at_text,
        "delivered_at": (
            recorded_at_text if _is_delivered_status(delivery_status) else None
        ),
        "old_stage": "triggered" if segment_update else "monitoring",
        "new_stage": "segment_enriched" if segment_update else "triggered",
        "delivery_status": delivery_status,
        "delivery_reason": delivery_reason,
        "evidence_id": _text(getattr(event, "evidence_id", None)),
        "recursive_level": recursive_level,
        "anchor_time": _text(getattr(event, "anchor_time", None)),
        "current_price": getattr(event, "price", None),
        "current_price_source": _text(
            getattr(event, "price_source", None),
            "latest_completed_1m_close",
        ),
        "signal_qualification": (
            "confirmed_5m_trade_signal_with_new_1m_segment_enrichment"
            if segment_update
            else "confirmed_5m_trade_signal_with_optional_1m_segment"
        ),
        "reference_price": getattr(event, "structure_anchor_price", None)
        or getattr(event, "price", None),
        "invalidation_price": getattr(
            event,
            "structure_invalidation_price",
            None,
        ),
        "position_recommendation": (
            dict(getattr(event, "position_recommendation"))
            if isinstance(getattr(event, "position_recommendation", None), Mapping)
            else None
        ),
        "big_direction": _text(getattr(event, "big_dir", None)),
        "mid_direction": _text(getattr(event, "mid_dir", None)),
        "is_manual_attention": bool(getattr(event, "is_holding", False)),
        "selection_sources": [
            "MANUAL_ATTENTION_MONITOR"
            if bool(getattr(event, "is_holding", False))
            else "WATCHLIST_MONITOR"
        ],
        "chart_urls": _chart_urls(normalized_market, code),
        "review_required": True,
        "automated_action_authorized": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


class RealtimeReviewInbox:
    """Thread-safe bounded store shared by both realtime notification paths."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("realtime review inbox max_events must be positive")
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(CN))
        self._max_events = max_events
        self._lock = threading.RLock()
        self._load_requires_persist = False
        self._events = self._load()
        if self._load_requires_persist:
            self._persist()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("events"), list)
        ):
            return {}
        loaded: dict[str, dict[str, object]] = {}
        raw_events = payload["events"][-self._max_events :]
        for raw in raw_events:
            normalized = _normalize_event(
                raw,
                migrate_account_coupled_position=True,
                migrate_non_strict_segment=True,
            )
            if normalized != raw:
                self._load_requires_persist = True
            if not self._valid_event(normalized, allow_legacy=True):
                continue
            loaded[str(normalized["notification_id"])] = normalized
        if len(loaded) != len(raw_events):
            self._load_requires_persist = True
        return loaded

    @staticmethod
    def _valid_event(
        raw: object,
        *,
        allow_legacy: bool = False,
    ) -> bool:
        if not isinstance(raw, Mapping) or raw.get("schema") != EVENT_SCHEMA:
            return False
        notification_id = raw.get("notification_id")
        side = raw.get("side")
        point_type = raw.get("point_type")
        legacy_one_minute = bool(
            allow_legacy
            and raw.get("source_frequency") == "1m"
            and raw.get("trigger_frequency") == "1m"
        )
        recursive_level = raw.get("recursive_level")
        if (
            not isinstance(notification_id, str)
            or not notification_id.startswith("sha256:")
            or len(notification_id) != 71
            or raw.get("source") not in _SOURCES
            or raw.get("delivery_status") not in _DELIVERY_STATUSES
            or side not in {"buy", "sell"}
            or point_type not in CANONICAL_POINT_TYPE_SET
            or side != ("buy" if str(point_type).endswith("buy") else "sell")
            or raw.get("source_frequency") != "5m"
            and not legacy_one_minute
            or raw.get("setup_lock_state") not in _SETUP_LOCK_STATES
            or (
                not legacy_one_minute
                and (
                    type(recursive_level) is not int
                    or not is_five_minute_trade_level("5m", recursive_level)
                )
            )
            or not isinstance(raw.get("evidence_id"), str)
            or not str(raw.get("evidence_id") or "").strip()
            or not isinstance(raw.get("market"), str)
            or not isinstance(raw.get("code"), str)
            or not raw.get("code")
            or raw.get("review_required") is not True
            or raw.get("automated_action_authorized") is not False
            or raw.get("real_order_transport_enabled") is not False
            or raw.get("live_status") != "LIVE_DISABLED"
            or not isinstance(raw.get("chart_urls"), Mapping)
            or not _valid_position_recommendation(
                raw.get("position_recommendation"),
                side=raw.get("side"),
            )
            or (
                raw.get("position_recommendation_at_detection") is not None
                and not _valid_position_recommendation(
                    raw.get("position_recommendation_at_detection"),
                    side=raw.get("side"),
                )
            )
        ):
            return False
        try:
            parsed_times: dict[str, datetime] = {}
            for key in (
                "signal_time",
                "observed_at",
                "recorded_at",
                "structure_confirmed_at",
                "signal_available_at",
                "detected_at",
                "delivery_updated_at",
            ):
                parsed = datetime.fromisoformat(str(raw[key]))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return False
                parsed_times[key] = parsed
            for key in (
                "structure_anchor_time",
                "delivered_at",
                "first_recorded_at",
            ):
                value = raw.get(key)
                if value is None or value == "":
                    continue
                parsed = datetime.fromisoformat(str(value))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return False
                parsed_times[key] = parsed
        except (KeyError, TypeError, ValueError):
            return False
        anchor_at = parsed_times.get("structure_anchor_time")
        confirmed_at = parsed_times["structure_confirmed_at"]
        available_at = parsed_times["signal_available_at"]
        detected_at = parsed_times["detected_at"]
        if (
            available_at > detected_at
            or not legacy_one_minute
            and (
                anchor_at is not None
                and anchor_at > confirmed_at
                or confirmed_at > available_at
            )
        ):
            return False
        current_price = raw.get("current_price")
        if current_price is not None:
            try:
                parsed_price = float(current_price)
            except (TypeError, ValueError):
                return False
            if (
                parsed_price <= 0
                or not isfinite(parsed_price)
                or not isinstance(raw.get("current_price_source"), str)
                or not str(raw.get("current_price_source") or "").strip()
            ):
                return False
        segment_present = raw.get("segment_difference_present") is True
        segment_status = raw.get("segment_difference_status")
        segment_current = raw.get("segment_difference_current")
        segment_evidence_status = raw.get("segment_difference_evidence_status")
        segment_boundary_status = raw.get("segment_difference_boundary_status")
        segment_fields = (
            "segment_difference_point_type",
            "segment_difference_evidence_id",
            "segment_difference_anchor_time",
            "segment_difference_confirmed_at",
            "segment_difference_available_at",
            "segment_difference_recursive_level",
            "segment_difference_divergence_kind",
        )
        segment_valid_until = raw.get("segment_difference_valid_until")
        if legacy_one_minute:
            if segment_present or any(
                raw.get(key) not in (None, "") for key in segment_fields
            ):
                return False
            return True
        if (
            raw.get("trigger_frequency") is not None
            or raw.get("segment_difference_frequency") != "1m"
            or segment_status not in _SEGMENT_DIFFERENCE_STATUSES
            or not isinstance(segment_current, bool)
            or segment_current != (segment_status == "current")
            or segment_present != (segment_status != "absent")
            or segment_evidence_status not in _SEGMENT_DIFFERENCE_EVIDENCE_STATUSES
            or segment_evidence_status != ("present" if segment_present else "absent")
            or segment_boundary_status not in _SEGMENT_DIFFERENCE_BOUNDARY_STATUSES
            or not segment_present
            and segment_boundary_status != "absent"
            or segment_present
            and side == "sell"
            and segment_boundary_status != "not_applicable"
            or segment_present
            and side == "buy"
            and segment_boundary_status != segment_status
            or segment_present
            and (
                raw.get("segment_difference_point_type") not in CANONICAL_POINT_TYPE_SET
                or side
                != (
                    "buy"
                    if str(raw.get("segment_difference_point_type")).endswith("buy")
                    else "sell"
                )
                or not isinstance(raw.get("segment_difference_evidence_id"), str)
                or not raw.get("segment_difference_evidence_id")
                or not isinstance(raw.get("segment_difference_recursive_level"), int)
                or not is_one_minute_segment_level(
                    "1m",
                    raw.get("segment_difference_recursive_level"),
                )
                or raw.get("segment_difference_divergence_kind")
                not in {None, *_DIVERGENCE_KINDS}
            )
            or not segment_present
            and (
                any(raw.get(key) not in (None, "") for key in segment_fields)
                or segment_valid_until not in (None, "")
            )
        ):
            return False
        if segment_valid_until not in (None, ""):
            parsed_segment_valid_until = _aware_datetime(segment_valid_until)
            if parsed_segment_valid_until is None:
                return False
        if segment_present:
            try:
                segment_times: list[datetime] = []
                for key in (
                    "segment_difference_anchor_time",
                    "segment_difference_confirmed_at",
                    "segment_difference_available_at",
                ):
                    parsed = datetime.fromisoformat(str(raw[key]))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        return False
                    segment_times.append(parsed)
            except (KeyError, TypeError, ValueError):
                return False
            if not (
                segment_times[0] <= segment_times[1] <= segment_times[2] <= detected_at
            ):
                return False
        return True

    def record(self, event: Mapping[str, object]) -> None:
        normalized = _normalize_event(event)
        if not self._valid_event(normalized):
            raise ValueError("realtime review inbox event is invalid")
        with self._lock:
            identity = str(normalized["notification_id"])
            previous = self._events.get(identity)
            first_recorded_at = (
                previous.get("first_recorded_at")
                if isinstance(previous, Mapping)
                else None
            ) or normalized["recorded_at"]
            if isinstance(previous, Mapping):
                normalized["detected_at"] = (
                    previous.get("detected_at") or normalized["detected_at"]
                )
                normalized["delivered_at"] = normalized.get(
                    "delivered_at"
                ) or previous.get("delivered_at")
            self._events[identity] = {
                **normalized,
                "first_recorded_at": first_recorded_at,
            }
            self._trim()
            self._persist()

    def update_delivery(
        self,
        notification_ids: Sequence[str],
        *,
        status: str,
        reason: str | None = None,
    ) -> None:
        if status not in _DELIVERY_STATUSES:
            raise ValueError("realtime review delivery status is invalid")
        changed = False
        recorded_at = _now(self._clock).isoformat()
        with self._lock:
            for raw_identity in notification_ids:
                identity = _event_id(raw_identity)
                event = self._events.get(identity)
                if event is None:
                    continue
                event["delivery_status"] = status
                event["delivery_reason"] = reason
                event["recorded_at"] = recorded_at
                event["delivery_updated_at"] = recorded_at
                if _is_delivered_status(status):
                    event["delivered_at"] = recorded_at
                changed = True
            if changed:
                self._persist()

    def _trim(self) -> None:
        if len(self._events) <= self._max_events:
            return
        ordered = sorted(
            self._events.values(),
            key=lambda value: (
                _event_sort_time(value),
                str(value.get("notification_id") or ""),
            ),
            reverse=True,
        )[: self._max_events]
        self._events = {str(value["notification_id"]): value for value in ordered}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        events = sorted(
            self._events.values(),
            key=lambda value: (
                _event_sort_time(value),
                str(value.get("notification_id") or ""),
            ),
        )
        temporary.write_text(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "events": events,
                    "credentials_exposed": False,
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "automated_order_authorized": False,
                    "live_status": "LIVE_DISABLED",
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            events = sorted(
                (dict(value) for value in self._events.values()),
                key=lambda value: (
                    _event_sort_time(value),
                    str(value.get("notification_id") or ""),
                ),
                reverse=True,
            )
        return {
            "schema": SCHEMA,
            "events": events,
            "event_count": len(events),
            "pending_review_count": len(events),
            "delivery_counts": {
                status: sum(value.get("delivery_status") == status for value in events)
                for status in sorted(_DELIVERY_STATUSES)
            },
            "credentials_exposed": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }


__all__ = (
    "EVENT_SCHEMA",
    "SCHEMA",
    "RealtimeReviewInbox",
    "a_share_notification_event",
    "monitor_notification_event",
    "segment_difference_boundary_status",
    "segment_difference_evidence_status",
    "segment_difference_review_status",
)
