"""Idempotent lifecycle notifications for read-only trading signals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)
from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)


SCHEMA = "chanlun-signal-notifications"
STRATEGY_ID = STRICT_STRATEGY_ID
CN = ZoneInfo("Asia/Shanghai")
_NOTIFIABLE_TRANSITIONS = {
    ("armed", "triggered"),
    ("triggered", "executable"),
    ("armed", "invalidated"),
    ("triggered", "invalidated"),
    ("executable", "invalidated"),
    ("active", "closed"),
}
_TRIGGER_MAX_AGE = timedelta(minutes=2)
_PENDING_TRIGGER_MAX_AGE = timedelta(minutes=2)
_AUDIT_RECORD_LIMIT = 500
_STAGE_LABELS = {
    "observed": "结构观察",
    "approaching": "即将确认",
    "formed": "已形成",
    "armed": "已入观察池",
    "triggered": "1分钟已触发",
    "executable": "强提示待人工复核",
    "active": "持有跟踪",
    "invalidated": "结构已失效",
    "closed": "跟踪已结束",
}
_POINT_LABELS = {
    "1buy": "一类买点",
    "2buy": "二类买点",
    "3buy": "三类买点",
    "1sell": "一类卖点",
    "2sell": "二类卖点",
    "3sell": "三类卖点",
    "1buy_nest": "一类买点（区间套）",
    "3buy_nest": "三类买点（区间套）",
    "类1buy": "类一买",
    "类1sell": "类一卖",
}
_DIRECTION_LABELS = {
    "up": "向上",
    "down": "向下",
    "neutral": "震荡待定",
}
_DISPOSITION_LABELS = {
    "supportive": "有利",
    "neutral": "中性",
    "hostile": "不利",
}
_HOLDING_SOURCES = frozenset({"HOLDING_MONITOR", "VIRTUAL_HOLDING_MONITOR"})
_SETUP_POINT_ORDER = {
    "1buy": 0,
    "2buy": 1,
    "3buy": 2,
    "1sell": 3,
    "2sell": 4,
    "3sell": 5,
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


def _stage_label(stage: str | None) -> str:
    """Translate a stable lifecycle enum only at the presentation boundary."""

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
    """Identify one visible trigger even when several 5m setups consume it."""

    if new_stage not in {"triggered", "executable"}:
        return None
    trigger = _mapping(signal.get("trigger_1m"))
    trigger_type = str(trigger.get("point_type") or "")
    trigger_time = next(
        (
            str(trigger.get(key))
            for key in ("available_at", "confirmed_at", "anchor_at")
            if trigger.get(key)
        ),
        "",
    )
    trigger_identity = (
        f"{trigger_type}@{trigger_time}" if trigger_type and trigger_time else ""
    )
    if not trigger_identity:
        return None
    setup = _mapping(signal.get("setup_5m"))
    side = str(signal.get("side") or setup.get("side") or "")
    return (
        str(signal.get("code") or ""),
        side,
        trigger_identity,
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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CN)


def _trigger_time(signal: Mapping[str, object]) -> datetime | None:
    trigger = _mapping(signal.get("trigger_1m"))
    return next(
        (
            parsed
            for key in ("available_at", "confirmed_at")
            if (parsed := _parse_time(trigger.get(key))) is not None
        ),
        None,
    )


def _notification_eligibility_reason(
    signal: Mapping[str, object],
    *,
    new_stage: str,
) -> str | None:
    """Fail closed unless a transition is a current, executable decision.

    A lifecycle ``triggered`` value is only a structural fact.  It is not an
    instruction to notify: the serialized decision, warmup convergence and the
    one-minute execution window remain authoritative.
    """

    if new_stage in {"invalidated", "closed"}:
        return None
    if new_stage not in {"triggered", "executable"}:
        return "UNSUPPORTED_NOTIFICATION_STAGE"

    side = str(signal.get("side") or "")
    if side == "buy":
        if signal.get("entry_allowed") is not True:
            return "ENTRY_NOT_ALLOWED"
    elif side == "sell":
        if signal.get("exit_allowed") is not True:
            return "EXIT_NOT_ALLOWED"
    else:
        return "SIGNAL_SIDE_INVALID"

    if signal.get("physical_timeframe_recursive") is not True:
        return "PHYSICAL_TIMEFRAME_AUTHORITY_MISSING"
    warmup = _mapping(signal.get("warmup"))
    if warmup.get("converged") is not True:
        return "WARMUP_NOT_CONVERGED"
    conflict = _mapping(signal.get("conflict"))
    if conflict.get("hard_block") is True:
        return "STRUCTURE_CONFLICT"

    trigger = _mapping(signal.get("trigger_1m"))
    if (
        trigger.get("status") != "confirmed"
        or trigger.get("source_frequency") != "1m"
        or trigger.get("actionable") is not True
    ):
        return "ONE_MINUTE_TRIGGER_NOT_CONFIRMED"
    trigger_at = _trigger_time(signal)
    observed_at = _parse_time(signal.get("observed_at"))
    if trigger_at is None or observed_at is None:
        return "TRIGGER_TIME_UNAVAILABLE"
    age = observed_at - trigger_at
    if age < timedelta(0):
        return "TRIGGER_FROM_FUTURE"
    if age > _TRIGGER_MAX_AGE:
        return "TRIGGER_STALE"

    if side == "buy":
        if signal.get("sector_triggered") is not True:
            return "CURRENT_SECTOR_TRIGGER_REQUIRED"
        risk = _mapping(signal.get("higher_timeframe_risk"))
        if any(
            risk.get(key) != "GREEN"
            for key in ("market_gate", "sector_gate", "symbol_gate")
        ):
            return "HIGHER_TIMEFRAME_GATE_NOT_GREEN"
        boundary = _mapping(signal.get("entry_execution_boundary"))
        valid_until = _parse_time(boundary.get("entry_valid_until"))
        confirmation = _parse_time(boundary.get("confirmation_bar_closed_at"))
        if valid_until is None or confirmation != trigger_at:
            return "ENTRY_EXECUTION_BOUNDARY_INVALID"
        if observed_at > valid_until:
            return "ENTRY_WINDOW_EXPIRED"
    return None


def _signal_semantic_key(signal: Mapping[str, object]) -> tuple[str, ...]:
    setup = _mapping(signal.get("setup_5m"))
    trigger = _mapping(signal.get("trigger_1m"))
    return (
        str(signal.get("code") or ""),
        str(signal.get("side") or ""),
        str(setup.get("point_type") or signal.get("point_type") or ""),
        str(setup.get("available_at") or setup.get("confirmed_at") or ""),
        str(trigger.get("point_type") or ""),
        str(trigger.get("available_at") or trigger.get("confirmed_at") or ""),
    )


def _text(value: object, default: str = "—") -> str:
    rendered = str(value).strip() if value is not None else ""
    return rendered if rendered else default


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
    return "持仓股" if sources & _HOLDING_SOURCES else "候选股"


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
    """Render the operation-level structural defense without inventing a price.

    The 5-minute setup owns the structure that generated the alert, so its
    invalidation price is authoritative for both buy and sell signals.  A sell
    defense is an *upper* invalidation boundary, not a downside stop, hence the
    direction is stated explicitly in the notification.
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


def _action_advice(
    signal: Mapping[str, object],
    *,
    point_type: object,
    scope: str,
    new_stage: str,
) -> str:
    if new_stage == "invalidated":
        return "建议：取消该结构计划"
    if new_stage == "closed":
        return "建议：结束跟踪"

    point = str(point_type or "").strip()
    side = str(signal.get("side") or "").strip()
    if not side:
        side = "buy" if "buy" in point else "sell" if "sell" in point else ""
    if side == "buy":
        action = "增持" if scope == "持仓股" else "买入"
        if point.startswith("1buy"):
            condition = "确认反转后"
        elif point.startswith("2buy"):
            condition = "回踩不破后"
        elif point.startswith("3buy"):
            condition = "回抽确认后"
        else:
            condition = "人工确认后"
        return f"建议：{condition}考虑分批{action}"
    if side == "sell":
        if point.startswith("3sell"):
            return "建议：优先检查退出条件"
        if point.startswith("2sell"):
            return "建议：反弹未转强时考虑继续减仓"
        return "建议：优先考虑减仓"
    return "建议：人工复核后再操作"


def format_notification(
    signal: Mapping[str, object],
    old_stage: str,
    new_stage: str,
) -> tuple[str, list[str]]:
    context = _mapping(signal.get("context_30m"))
    setup = _mapping(signal.get("setup_5m"))
    trigger = _mapping(signal.get("trigger_1m"))
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
    scope = _scope_label(signal)
    effective_point_type = setup.get("point_type") or signal.get("point_type")
    old_stage_label = _stage_label(old_stage)
    new_stage_label = _stage_label(new_stage)
    if new_stage in {"invalidated", "closed"}:
        headline = new_stage_label
    else:
        headline = f"5分钟{setup_point}"
        if trigger.get("point_type"):
            headline += f"（1分钟{trigger_point}确认）"
    title = f"买卖通知｜{scope}｜{code}｜{headline}"

    direction = _localized(context.get("direction"), _DIRECTION_LABELS)
    disposition = _localized(
        context.get("disposition"),
        _DISPOSITION_LABELS,
        "",
    )
    context_text = f"30分钟{direction}"
    if disposition:
        context_text += f"（{disposition}）"
    confirmed_at = _text(
        trigger.get("available_at")
        or trigger.get("confirmed_at")
        or signal.get("observed_at"),
        "时间未知",
    )
    defense_price = _defense_price_text(signal, setup)
    lines = [
        f"{name}｜{old_stage_label}→{new_stage_label}｜{confirmed_at}",
        f"结构：{context_text}｜5分钟{setup_point}｜1分钟{trigger_point}",
        (f"板块：{_text(sector.get('sector_name'))}｜防守价：{defense_price}"),
        _action_advice(
            signal,
            point_type=effective_point_type,
            scope=scope,
            new_stage=new_stage,
        ),
    ]
    return title, lines


class SignalNotificationDispatcher:
    def __init__(
        self,
        notifier: object,
        *,
        state_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        send = getattr(notifier, "send", None)
        if not callable(send):
            raise TypeError("notifier must expose send")
        self._notifier = notifier
        self._state_path = None if state_path is None else Path(state_path)
        self._clock = clock or (lambda: datetime.now(CN))
        self._lock = threading.RLock()
        state = self._load_state()
        self._delivered = set(state["delivered_event_ids"])
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
        self._pending_trigger_events = dict(state["pending_trigger_events"])

    def _load_state(self) -> dict[str, object]:
        empty = {
            "delivered_event_ids": (),
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
            "pending_trigger_events": {},
        }
        if self._state_path is None:
            return empty
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return empty
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("delivered_event_ids"), list)
            or set(payload)
            != {
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
        ):
            return empty
        values = payload["delivered_event_ids"]
        if not all(
            isinstance(value, str) and value.startswith("sha256:") and len(value) == 71
            for value in values
        ):
            return empty
        delivered = tuple(values)
        if delivered != tuple(sorted(set(delivered))):
            return empty
        for key in ("success_count", "failure_count", "suppressed_count"):
            if type(payload[key]) is not int or payload[key] < 0:
                return empty
        for key in (
            "last_success_at",
            "last_failure_at",
            "last_suppressed_at",
        ):
            value = payload[key]
            if value is not None and _parse_time(value) is None:
                return empty
        for key in (
            "last_success_event_id",
            "last_failure_reason",
            "last_suppressed_reason",
        ):
            value = payload[key]
            if value is not None and (not isinstance(value, str) or not value):
                return empty
        last_success_event_id = payload["last_success_event_id"]
        if last_success_event_id is not None and (
            not last_success_event_id.startswith("sha256:")
            or len(last_success_event_id) != 71
        ):
            return empty
        raw_audit = payload["event_audit"]
        if not isinstance(raw_audit, list) or not all(
            isinstance(value, Mapping) for value in raw_audit
        ):
            return empty
        event_audit = tuple(dict(value) for value in raw_audit[-_AUDIT_RECORD_LIMIT:])
        raw_pending = payload["pending_trigger_events"]
        if not isinstance(raw_pending, Mapping):
            return empty
        pending_trigger_events: dict[str, dict[str, str]] = {}
        for event_id, value in raw_pending.items():
            if (
                not isinstance(event_id, str)
                or not event_id.startswith("sha256:")
                or len(event_id) != 71
                or not isinstance(value, Mapping)
                or set(value) != {"old_stage", "new_stage", "queued_at"}
                or value.get("old_stage") not in {"armed", "triggered"}
                or value.get("new_stage") not in {"triggered", "executable"}
                or _parse_time(value.get("queued_at")) is None
            ):
                return empty
            pending_trigger_events[event_id] = {
                "old_stage": str(value["old_stage"]),
                "new_stage": str(value["new_stage"]),
                "queued_at": str(value["queued_at"]),
            }

        return {
            "delivered_event_ids": delivered,
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
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "delivered_event_ids": sorted(self._delivered),
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
        """Expose durable delivery evidence without revealing credentials."""

        with self._lock:
            degraded = bool(
                self._last_failure_at is not None
                and (
                    self._last_success_at is None
                    or self._last_failure_at > self._last_success_at
                )
            )
            verified = bool(self._delivered)
            if degraded:
                status = "degraded"
                reason = "LATEST_NOTIFICATION_DELIVERY_FAILED"
            elif verified:
                status = "verified"
                reason = "DELIVERY_SUCCESS_PROVEN"
            else:
                status = "awaiting_first_delivery"
                reason = "NO_NOTIFICATION_EVENT_DUE_OR_DELIVERED"
            return {
                "schema": "chanlun-signal-notification-readiness",
                "configured": True,
                "operationally_verified": verified,
                "status": status,
                "reason_code": reason,
                "delivered_event_count": len(self._delivered),
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "last_success_at": self._last_success_at,
                "last_success_event_id": self._last_success_event_id,
                "last_failure_at": self._last_failure_at,
                "last_failure_reason": self._last_failure_reason,
                "suppressed_count": self._suppressed_count,
                "last_suppressed_at": self._last_suppressed_at,
                "last_suppressed_reason": self._last_suppressed_reason,
                "event_audit_record_count": len(self._event_audit),
                "pending_trigger_event_count": len(self._pending_trigger_events),
                "credentials_exposed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }

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
        trigger = _mapping(document.get("trigger_1m"))
        raw_reasons = document.get("decision_reasons")
        reasons = (
            raw_reasons
            if isinstance(raw_reasons, (list, tuple, set, frozenset))
            else ()
        )
        recorded_at = self._now().isoformat()
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
                "trigger_point_type": str(trigger.get("point_type") or ""),
                "trigger_point_id": str(trigger.get("point_id") or ""),
                "trigger_available_at": str(
                    trigger.get("available_at") or trigger.get("confirmed_at") or ""
                ),
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

    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        with self._lock:
            before = _signals_by_id(previous)
            after = _signals_by_id(current)
            grouped: dict[
                tuple[str, ...],
                list[tuple[str, str, str, Mapping[str, object]]],
            ] = {}
            dirty_state = False
            dispatch_now = self._now()
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
                actual_old_stage = _stage(before.get(signal_id))
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
                if transition in _NOTIFIABLE_TRANSITIONS:
                    old_stage = str(actual_old_stage)
                    new_stage = str(actual_new_stage)
                elif pending is not None:
                    old_stage = str(pending["old_stage"])
                    new_stage = str(pending["new_stage"])
                else:
                    continue
                signal_event_id = notification_event_id(
                    signal_id,
                    old_stage,
                    new_stage,
                )
                rejection = _notification_eligibility_reason(
                    document,
                    new_stage=new_stage,
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
                occurrence_key = _trigger_occurrence_key(document, new_stage)
                group_key = (
                    ("trigger", *occurrence_key)
                    if occurrence_key is not None
                    else ("signal", signal_event_id)
                )
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
                    or _stage(old_document) not in {"triggered", "executable"}
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
                grouped.setdefault(("signal", signal_event_id), []).append(
                    (
                        signal_id,
                        old_stage,
                        "invalidated",
                        document,
                    )
                )

            for group_key in sorted(grouped):
                candidates = grouped[group_key]
                event_id = (
                    _trigger_occurrence_event_id(group_key[1:])
                    if group_key[0] == "trigger"
                    else group_key[1]
                )
                if event_id in self._delivered:
                    if self._pending_trigger_events.pop(event_id, None) is not None:
                        dirty_state = True
                    continue
                candidates.sort(
                    key=lambda value: (
                        not bool(
                            set(value[3].get("selection_sources") or ())
                            & _HOLDING_SOURCES
                        ),
                        not bool(
                            value[3].get("entry_allowed")
                            or value[3].get("exit_allowed")
                        ),
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
                title, lines = format_notification(
                    notification_document,
                    old_stage,
                    new_stage,
                )
                try:
                    send_rich = getattr(self._notifier, "send_rich", None)
                    if callable(send_rich):
                        code = _text(notification_document.get("code"), "")
                        name = _text(notification_document.get("name"), code)
                        context = {
                            "require_evidence_match": new_stage
                            in {"triggered", "executable"},
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
                                        _mapping(
                                            notification_document.get("trigger_1m")
                                        ).get("point_type"),
                                        "",
                                    ),
                                    "signal_time": _text(
                                        _mapping(
                                            notification_document.get("trigger_1m")
                                        ).get("available_at")
                                        or _mapping(
                                            notification_document.get("trigger_1m")
                                        ).get("confirmed_at"),
                                        "",
                                    ),
                                    "evidence_id": _text(
                                        _mapping(
                                            notification_document.get("trigger_1m")
                                        ).get("point_id"),
                                        "",
                                    ),
                                    "evidence_required": new_stage
                                    in {"triggered", "executable"},
                                }
                            ],
                        }
                        sent = bool(send_rich(title, lines, context))
                    else:
                        sent = bool(self._notifier.send(title, lines))
                except Exception as exc:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = type(exc).__name__
                    if group_key[0] == "trigger":
                        self._pending_trigger_events.setdefault(
                            event_id,
                            {
                                "old_stage": old_stage,
                                "new_stage": new_stage,
                                "queued_at": dispatch_now.isoformat(),
                            },
                        )
                    self._record_audit(
                        status="failed",
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason=type(exc).__name__,
                    )
                    self._persist()
                    raise
                if not sent:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = "NOTIFIER_RETURNED_FALSE"
                    if group_key[0] == "trigger":
                        self._pending_trigger_events.setdefault(
                            event_id,
                            {
                                "old_stage": old_stage,
                                "new_stage": new_stage,
                                "queued_at": dispatch_now.isoformat(),
                            },
                        )
                    self._record_audit(
                        status="failed",
                        event_id=event_id,
                        old_stage=old_stage,
                        new_stage=new_stage,
                        document=notification_document,
                        reason="NOTIFIER_RETURNED_FALSE",
                    )
                    self._persist()
                    continue
                self._delivered.add(event_id)
                self._pending_trigger_events.pop(event_id, None)
                self._success_count += 1
                self._last_success_at = self._now().isoformat()
                self._last_success_event_id = event_id
                self._record_audit(
                    status="delivered",
                    event_id=event_id,
                    old_stage=old_stage,
                    new_stage=new_stage,
                    document=notification_document,
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
