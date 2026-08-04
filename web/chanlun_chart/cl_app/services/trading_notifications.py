"""Idempotent lifecycle notifications for read-only trading signals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.runtime_config import (
    STRICT_STRATEGY_ID,
)


SCHEMA_VERSION = "chanlun-signal-notifications/v1"
STRATEGY_ID = STRICT_STRATEGY_ID
CN = ZoneInfo("Asia/Shanghai")
_NOTIFIABLE_TRANSITIONS = {
    (None, "triggered"),
    (None, "executable"),
    ("armed", "triggered"),
    ("triggered", "executable"),
    ("armed", "invalidated"),
    ("triggered", "invalidated"),
    ("active", "closed"),
}
_STAGE_LABELS = {
    "observed": "结构观察",
    "approaching": "即将确认",
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
    value = signal.get("lifecycle_stage")
    return value if isinstance(value, str) else None


def _stage_label(stage: str | None) -> str:
    """Translate a stable lifecycle enum only at the presentation boundary."""

    if stage is None or stage == "None":
        return "首次发现"
    return _STAGE_LABELS.get(stage, "未知状态")


def notification_event_id(signal_id: str, old_stage: str, new_stage: str) -> str:
    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "signal_id": signal_id,
            "old_stage": old_stage,
            "new_stage": new_stage,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


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
    setup_point = _point_label(
        setup.get("point_type") or signal.get("point_type"),
        "结构信号",
    )
    trigger_point = _point_label(trigger.get("point_type"))
    scope = _scope_label(signal)
    effective_point_type = (
        trigger.get("point_type")
        or setup.get("point_type")
        or signal.get("point_type")
    )
    old_stage_label = _stage_label(old_stage)
    new_stage_label = _stage_label(new_stage)
    if new_stage in {"invalidated", "closed"}:
        headline = new_stage_label
    elif trigger.get("point_type"):
        headline = f"1分钟{trigger_point}"
    else:
        headline = f"5分钟{setup_point}"
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
        trigger.get("confirmed_at") or signal.get("observed_at"),
        "时间未知",
    )
    lines = [
        f"{name}｜{old_stage_label}→{new_stage_label}｜{confirmed_at}",
        f"结构：{context_text}｜5分钟{setup_point}｜1分钟{trigger_point}",
        (
            f"板块：{_text(sector.get('sector_name'))}｜"
            f"失效价：{_text(signal.get('structural_stop'))}"
        ),
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

    def _load_state(self) -> dict[str, object]:
        empty = {
            "delivered_event_ids": (),
            "success_count": 0,
            "failure_count": 0,
            "last_success_at": None,
            "last_success_event_id": None,
            "last_failure_at": None,
            "last_failure_reason": None,
        }
        if self._state_path is None:
            return empty
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return empty
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != SCHEMA_VERSION
            or not isinstance(payload.get("delivered_event_ids"), list)
        ):
            return empty
        values = payload["delivered_event_ids"]
        if not all(
            isinstance(value, str)
            and value.startswith("sha256:")
            and len(value) == 71
            for value in values
        ):
            return empty
        delivered = tuple(sorted(set(values)))

        def optional_text(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None

        def nonnegative_int(key: str, fallback: int) -> int:
            value = payload.get(key, fallback)
            if isinstance(value, bool):
                return fallback
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return fallback

        return {
            "delivered_event_ids": delivered,
            # Every legacy ID was persisted only after notifier.send returned
            # True, so the ID count is an exact lower bound, not an inference.
            "success_count": nonnegative_int("success_count", len(delivered)),
            "failure_count": nonnegative_int("failure_count", 0),
            "last_success_at": optional_text("last_success_at"),
            "last_success_event_id": optional_text("last_success_event_id"),
            "last_failure_at": optional_text("last_failure_at"),
            "last_failure_reason": optional_text("last_failure_reason"),
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
                    "schema_version": SCHEMA_VERSION,
                    "delivered_event_ids": sorted(self._delivered),
                    "success_count": self._success_count,
                    "failure_count": self._failure_count,
                    "last_success_at": self._last_success_at,
                    "last_success_event_id": self._last_success_event_id,
                    "last_failure_at": self._last_failure_at,
                    "last_failure_reason": self._last_failure_reason,
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
                "schema": "chanlun-signal-notification-readiness/v1",
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
                "credentials_exposed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }

    def dispatch_changes(
        self,
        previous: Mapping[str, object],
        current: Mapping[str, object],
    ) -> None:
        with self._lock:
            before = _signals_by_id(previous)
            for signal_id, document in sorted(_signals_by_id(current).items()):
                old_stage = _stage(before.get(signal_id))
                new_stage = _stage(document)
                transition = (old_stage, new_stage)
                if transition not in _NOTIFIABLE_TRANSITIONS:
                    continue
                event_id = notification_event_id(
                    signal_id,
                    str(old_stage),
                    str(new_stage),
                )
                if event_id in self._delivered:
                    continue
                title, lines = format_notification(
                    document,
                    str(old_stage),
                    str(new_stage),
                )
                try:
                    sent = bool(self._notifier.send(title, lines))
                except Exception as exc:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = type(exc).__name__
                    self._persist()
                    raise
                if not sent:
                    self._failure_count += 1
                    self._last_failure_at = self._now().isoformat()
                    self._last_failure_reason = "NOTIFIER_RETURNED_FALSE"
                    self._persist()
                    continue
                self._delivered.add(event_id)
                self._success_count += 1
                self._last_success_at = self._now().isoformat()
                self._last_success_event_id = event_id
                self._persist()


__all__ = [
    "SCHEMA_VERSION",
    "STRATEGY_ID",
    "SignalNotificationDispatcher",
    "format_notification",
    "notification_event_id",
]
