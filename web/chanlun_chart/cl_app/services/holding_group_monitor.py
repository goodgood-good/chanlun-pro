"""为经统一范围准入的跨市场关注标的提供只读缠论结构线索。

板块先行决策服务专用于 A 股；本服务为其余市场提供独立的辅助观察通道。A 股持仓
仍只进入 ``TradingScreeningService`` 及其 ``HumanAssistedDecisionCore``。
本服务不会读取账户，也不具备订单通道。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from math import isfinite
import os
from pathlib import Path
import threading
from time import perf_counter
from zoneinfo import ZoneInfo

from apscheduler.triggers.interval import IntervalTrigger

from chanlun import fun
from chanlun.exchange import get_exchange, market_now_trading
from chanlun.market import Market
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
    collect_strict_monitor_events,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)

from .job_names import JOB_DISPLAY_NAMES
from .realtime_review_inbox import monitor_notification_event
from .trading_screening_scope import (
    DEFAULT_VALIDATION_COHORT_SIZE,
    ScreeningScopeAuthorizationError,
    ScreeningUniverseAdmission,
    admit_screening_universe,
)


CN = ZoneInfo("Asia/Shanghai")
SCHEMA = "chanlun-holding-group-monitor"
JOB_ID = "holding_group_realtime_monitor"
DEDUPE_SCHEMA = "chanlun-holding-group-event-deduper"
RUNTIME_SCHEMA = "chanlun-holding-group-runtime-ledger"
_PENDING_NOTIFICATION_RETRY_TTL = timedelta(minutes=10)
_BUY_PROTECTION_REASONS = frozenset(
    {
        "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR",
        "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP",
    }
)
_MARKET_LABELS = {
    "a": "A股",
    "hk": "港股",
    "us": "美股",
    "fx": "外汇",
    "futures": "期货",
    "ny_futures": "纽约期货",
    "currency": "数字货币",
    "currency_spot": "数字货币现货",
}
_VALID_MARKETS = frozenset(value.value for value in Market)
_POINT_LABELS = {
    "1buy": "一类买点",
    "2buy": "二类买点",
    "3buy": "三类买点",
    "1sell": "一类卖点",
    "2sell": "二类卖点",
    "3sell": "三类卖点",
}
_EVENT_IDENTITY_KINDS = frozenset(
    {
        "strict_buy_point",
        "strict_sell_point",
        "strict_segment_difference_update",
        "strict_30m_context_warning",
    }
)
_LEGACY_PENDING_NOTIFICATION_FIELDS = frozenset(
    {
        "title",
        "lines",
        "identities",
        "codes",
        "charts",
        "transition_codes",
        "queued_at",
    }
)
_PENDING_NOTIFICATION_FIELDS = frozenset(
    {
        *_LEGACY_PENDING_NOTIFICATION_FIELDS,
        "review_events",
        "review_recorded",
        "transport_status",
        "transport_completed_at",
    }
)


def _canonical_delivery_identity(value: object) -> str:
    identity = str(value or "").strip()
    parts = identity.split("|")
    if len(parts) == 8 and parts[0] in _EVENT_IDENTITY_KINDS:
        return "|".join(parts[:-1])
    if len(parts) == 14 and parts[0] in _EVENT_IDENTITY_KINDS:
        return "|".join(parts[:-2])
    return identity


def _delivery_identity(event: object) -> str:
    """通知去重使用稳定发生身份，图表仍保留完整证据身份。"""

    return _canonical_delivery_identity(
        getattr(event, "delivery_identity", getattr(event, "identity", ""))
    )


def fresh_monitor_events(events: Iterable[object], deduper: object) -> list[object]:
    """按稳定事件身份完成批内去重，再返回尚未发布的事件。

    持久化去重器仍是唯一的跨批次发布闸门。
    """

    unique: list[object] = []
    identities: set[str] = set()
    for event in events:
        identity = _delivery_identity(event)
        if identity in identities:
            continue
        identities.add(identity)
        unique.append(event)
    unseen = getattr(deduper, "unseen", None)
    if not callable(unseen):
        raise TypeError("monitor event deduper must provide unseen(events)")
    return list(unseen(unique))


_DIRECTION_LABELS = {
    "up": "向上",
    "down": "向下",
    "neutral": "震荡待定",
    "": "未知",
}
_DIVERGENCE_LABELS = {
    "trend": "趋势背驰",
    "consolidation": "盘整背驰",
}
_POINT_ADVICE = {
    "1buy": "建议：确认反转后再复合买入条件",
    "2buy": "建议：回踩不破后再复合买入条件",
    "3buy": "建议：回抽确认后再复合买入条件",
    "1sell": "建议：优先复核卖出或退出条件",
    "2sell": "建议：反弹未转强时继续复核卖出条件",
    "3sell": "建议：优先检查退出条件",
}


class BoundedEventDeduper:
    """具有有界保留范围的持久化事件去重器。

    长期运行的应用内持仓监听器既不会无限增长，也能跨应用重启恢复，并重试尚未送达的通知。
    """

    def __init__(
        self,
        path: Path,
        *,
        retention_days: int = 180,
        max_records: int = 50_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days))
        self.max_records = max(100, int(max_records))
        self._clock = clock or (lambda: datetime.now(CN))
        self._lock = threading.RLock()
        self.records: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"schema", "records"}
            or payload.get("schema") != DEDUPE_SCHEMA
        ):
            return
        raw = payload["records"]
        if not isinstance(raw, Mapping) or any(
            not isinstance(identity, str)
            or not identity
            or not isinstance(observed_at, str)
            or not observed_at
            for identity, observed_at in raw.items()
        ):
            return
        migrated: dict[str, str] = {}
        for identity, observed_at in raw.items():
            stable_identity = _canonical_delivery_identity(identity)
            if observed_at >= migrated.get(stable_identity, ""):
                migrated[stable_identity] = observed_at
        self.records = migrated
        self._prune()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=CN)
        return value.astimezone(CN)

    def _prune(self) -> None:
        cutoff = self._now() - timedelta(days=self.retention_days)
        retained: list[tuple[str, str, datetime]] = []
        for identity, raw_time in self.records.items():
            try:
                parsed = datetime.fromisoformat(raw_time)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=CN)
                parsed = parsed.astimezone(CN)
            except (TypeError, ValueError):
                continue
            if parsed >= cutoff:
                retained.append((identity, raw_time, parsed))
        retained.sort(key=lambda row: row[2], reverse=True)
        self.records = {
            identity: raw_time
            for identity, raw_time, _parsed in retained[: self.max_records]
        }

    def unseen(self, events) -> list:
        with self._lock:
            return [
                event
                for event in events
                if _delivery_identity(event) not in self.records
            ]

    def mark(self, events) -> None:
        self.mark_identities(_delivery_identity(event) for event in events)

    def mark_identities(self, identities) -> None:
        with self._lock:
            observed_at = self._now().isoformat(timespec="seconds")
            changed = False
            for raw_identity in identities:
                identity = _canonical_delivery_identity(raw_identity)
                if identity and identity not in self.records:
                    self.records[identity] = observed_at
                    changed = True
            if not changed:
                return
            self._prune()
            self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"schema": DEDUPE_SCHEMA, "records": self.records},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class HoldingMonitorRuntimeLedger:
    """保存通知送达证据和方向转变状态的小型持久化账本。"""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(CN))
        self._lock = threading.RLock()
        loaded = self._load()
        # Disk-restored deliveries remain quarantined until the current group
        # universe has passed admission.  Health/page reads before the first
        # scheduled run therefore cannot expose or send old pending events.
        self._quarantined_pending_notifications = dict(
            loaded["pending_notifications"]
        )
        loaded["pending_notifications"] = {}
        self._state = loaded

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schema": RUNTIME_SCHEMA,
            "last_big_directions": {},
            "active_outages": {},
            "pending_notifications": {},
            "delivered_event_count": 0,
            "success_count": 0,
            "simulated_success_count": 0,
            "failure_count": 0,
            "expired_event_count": 0,
            "consecutive_failure_count": 0,
            "last_success_at": None,
            "last_simulated_at": None,
            "last_failure_at": None,
            "last_failure_reason": None,
            "last_expired_at": None,
        }

    @staticmethod
    def _normalize_pending(
        raw: object,
    ) -> dict[str, object] | None:
        if not isinstance(raw, Mapping):
            return None
        value = dict(raw)
        event_count = value.pop("event_count", None)
        if set(value) == _LEGACY_PENDING_NOTIFICATION_FIELDS:
            # Older queues recorded the review projection before persistence,
            # but did not retain enough material to rebuild it.  Preserve that
            # recovery contract while all newly queued batches carry the full
            # projection below.
            value.update(
                {
                    "review_events": [],
                    "review_recorded": True,
                    "transport_status": None,
                    "transport_completed_at": None,
                }
            )
        if set(value) != _PENDING_NOTIFICATION_FIELDS:
            return None
        if (
            not isinstance(value.get("title"), str)
            or not value.get("title")
            or any(
                not isinstance(value.get(key), list)
                for key in (
                    "lines",
                    "identities",
                    "codes",
                    "charts",
                    "transition_codes",
                    "review_events",
                )
            )
            or any(
                not isinstance(item, str)
                for key in ("lines", "identities", "codes", "transition_codes")
                for item in value[key]
            )
            or any(not isinstance(item, Mapping) for item in value["charts"])
            or any(
                not isinstance(item, Mapping) for item in value["review_events"]
            )
            or type(value.get("review_recorded")) is not bool
            or value.get("transport_status") not in {None, "delivered", "simulated"}
        ):
            return None
        identities = list(value["identities"])
        if (
            len(value["lines"]) != len(identities)
            or len(value["codes"]) != len(identities)
            or (event_count is not None and event_count != len(identities))
            or (
                value["review_events"]
                and len(value["review_events"]) != len(identities)
            )
        ):
            return None
        try:
            queued_at = datetime.fromisoformat(str(value["queued_at"]))
        except (TypeError, ValueError):
            return None
        if queued_at.tzinfo is None or queued_at.utcoffset() is None:
            return None
        transport_completed_at = value.get("transport_completed_at")
        if transport_completed_at is not None:
            try:
                completed = datetime.fromisoformat(str(transport_completed_at))
            except (TypeError, ValueError):
                return None
            if completed.tzinfo is None or completed.utcoffset() is None:
                return None
        return {
            "title": str(value["title"]),
            "lines": [str(item) for item in value["lines"]],
            "identities": [str(item) for item in identities],
            "codes": [str(item) for item in value["codes"]],
            "charts": [dict(item) for item in value["charts"]],
            "transition_codes": [
                str(item) for item in value["transition_codes"]
            ],
            "queued_at": str(value["queued_at"]),
            "review_events": [dict(item) for item in value["review_events"]],
            "review_recorded": value["review_recorded"] is True,
            "transport_status": value["transport_status"],
            "transport_completed_at": transport_completed_at,
        }

    def _load(self) -> dict[str, object]:
        empty = self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return empty
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != RUNTIME_SCHEMA
            or set(payload) != set(empty)
        ):
            return empty
        state = dict(payload)
        if (
            not isinstance(state["last_big_directions"], dict)
            or any(
                not isinstance(key, str) or value not in {"up", "down", "neutral"}
                for key, value in state["last_big_directions"].items()
            )
            or not isinstance(state["active_outages"], dict)
            or any(
                not isinstance(key, str) or value is not True
                for key, value in state["active_outages"].items()
            )
            or not isinstance(state["pending_notifications"], dict)
        ):
            return empty
        normalized_queues: dict[str, list[dict[str, object]]] = {}
        for market, raw_queue in state["pending_notifications"].items():
            if not isinstance(market, str):
                return empty
            batches = raw_queue if isinstance(raw_queue, list) else [raw_queue]
            normalized_batches = [
                self._normalize_pending(raw) for raw in batches
            ]
            if not batches or any(value is None for value in normalized_batches):
                return empty
            normalized_queues[market] = [
                value for value in normalized_batches if value is not None
            ]
        state["pending_notifications"] = normalized_queues
        for key in (
            "delivered_event_count",
            "success_count",
            "simulated_success_count",
            "failure_count",
            "expired_event_count",
            "consecutive_failure_count",
        ):
            value = state[key]
            if type(value) is not int or value < 0:
                return empty
        for key in (
            "last_success_at",
            "last_simulated_at",
            "last_failure_at",
            "last_failure_reason",
            "last_expired_at",
        ):
            value = state[key]
            if value is not None and (not isinstance(value, str) or not value):
                return empty
        return state

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=CN)
        return value.astimezone(CN)

    @staticmethod
    def identity(market: str, code: str) -> str:
        return f"{market}|{code}"

    def previous_direction(self, market: str, code: str) -> str | None:
        with self._lock:
            value = self._state["last_big_directions"].get(self.identity(market, code))
            return value if value in {"up", "down", "neutral"} else None

    def update_directions(self, market: str, values: Mapping[str, str]) -> None:
        with self._lock:
            directions = self._state["last_big_directions"]
            changed = False
            for code, direction in values.items():
                if direction in {"up", "down", "neutral"}:
                    identity = self.identity(market, code)
                    if directions.get(identity) != direction:
                        directions[identity] = direction
                        changed = True
            if changed:
                self._persist()

    def outage_active(self, market: str, code: str) -> bool:
        with self._lock:
            return bool(self._state["active_outages"].get(self.identity(market, code)))

    def set_outage(self, market: str, code: str, active: bool) -> None:
        with self._lock:
            outages = self._state["active_outages"]
            identity = self.identity(market, code)
            if active:
                if outages.get(identity) is True:
                    return
                outages[identity] = True
            else:
                if identity not in outages:
                    return
                outages.pop(identity, None)
            self._persist()

    def record_success(self, event_count: int) -> None:
        with self._lock:
            self._state["success_count"] += 1
            self._state["delivered_event_count"] += max(0, int(event_count))
            self._state["consecutive_failure_count"] = 0
            self._state["last_success_at"] = self._now().isoformat()
            self._persist()

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._state["failure_count"] += 1
            self._state["consecutive_failure_count"] += 1
            self._state["last_failure_at"] = self._now().isoformat()
            self._state["last_failure_reason"] = str(reason)[:160]
            self._persist()

    def record_simulated(self) -> None:
        with self._lock:
            self._state["simulated_success_count"] += 1
            self._state["consecutive_failure_count"] = 0
            self._state["last_simulated_at"] = self._now().isoformat()
            self._persist()

    def record_expired(self, event_count: int) -> None:
        with self._lock:
            self._state["expired_event_count"] += max(0, int(event_count))
            self._state["consecutive_failure_count"] = 0
            self._state["last_expired_at"] = self._now().isoformat()
            self._persist()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            success_count = int(self._state["success_count"])
            simulated_success_count = int(self._state["simulated_success_count"])
            failure_count = int(self._state["failure_count"])
            expired_event_count = int(self._state["expired_event_count"])
            consecutive_failures = int(self._state["consecutive_failure_count"])
            pending_count = sum(
                len(batch.get("identities", []))
                for queue in self._state["pending_notifications"].values()
                if isinstance(queue, list)
                for batch in queue
                if isinstance(batch, Mapping)
            )
            review_pending_count = sum(
                len(batch.get("identities", []))
                for queue in self._state["pending_notifications"].values()
                if isinstance(queue, list)
                for batch in queue
                if isinstance(batch, Mapping)
                and batch.get("transport_status") in {"delivered", "simulated"}
            )
            if consecutive_failures or pending_count:
                status = "degraded"
                reason = "LATEST_DELIVERY_FAILED_OR_PENDING"
            elif success_count:
                status = "verified"
                reason = "DELIVERY_SUCCESS_PROVEN"
            elif simulated_success_count:
                status = "simulated"
                reason = "DRY_RUN_DELIVERY_ONLY"
            elif failure_count:
                status = "degraded"
                reason = "DELIVERY_FAILED_WITHOUT_SUCCESS"
            else:
                status = "awaiting_first_delivery"
                reason = "NO_HOLDING_EVENT_DELIVERED_YET"
            return {
                "schema": RUNTIME_SCHEMA,
                "operationally_verified": bool(success_count),
                "status": status,
                "reason_code": reason,
                "delivered_event_count": int(self._state["delivered_event_count"]),
                "success_count": success_count,
                "simulated_success_count": simulated_success_count,
                "failure_count": failure_count,
                "expired_event_count": expired_event_count,
                "consecutive_failure_count": consecutive_failures,
                "pending_event_count": pending_count,
                "review_projection_pending_event_count": review_pending_count,
                "last_success_at": self._state["last_success_at"],
                "last_simulated_at": self._state["last_simulated_at"],
                "last_failure_at": self._state["last_failure_at"],
                "last_failure_reason": self._state["last_failure_reason"],
                "last_expired_at": self._state["last_expired_at"],
            }

    def pending_notification(self, market: str) -> dict[str, object] | None:
        with self._lock:
            queue = self._state["pending_notifications"].get(market)
            if not isinstance(queue, list) or not queue:
                return None
            normalized = self._normalize_pending(queue[0])
            if normalized is None:
                return None
            return {
                **normalized,
                "event_count": len(normalized["identities"]),
            }

    def pending_identities(self, market: str) -> set[str]:
        with self._lock:
            queue = self._state["pending_notifications"].get(market, [])
            return {
                str(identity)
                for batch in queue
                if isinstance(batch, Mapping)
                for identity in batch.get("identities", [])
            }

    def pending_markets(self) -> set[str]:
        with self._lock:
            return {
                str(market)
                for market, queue in self._state["pending_notifications"].items()
                if isinstance(queue, list) and queue
            }

    def set_pending_notification(
        self,
        market: str,
        payload: Mapping[str, object],
    ) -> None:
        normalized = self._normalize_pending(payload)
        if normalized is None:
            raise ValueError("pending holding notification is malformed")
        with self._lock:
            queue = self._state["pending_notifications"].setdefault(market, [])
            if queue:
                queue[0] = normalized
            else:
                queue.append(normalized)
            self._persist()

    def enqueue_pending_notification(
        self,
        market: str,
        payload: Mapping[str, object],
    ) -> None:
        normalized = self._normalize_pending(payload)
        if normalized is None:
            raise ValueError("pending holding notification is malformed")
        with self._lock:
            queue = self._state["pending_notifications"].setdefault(market, [])
            queued = {
                str(identity)
                for batch in queue
                for identity in batch.get("identities", [])
            }
            if any(identity in queued for identity in normalized["identities"]):
                raise ValueError("pending holding notification identity duplicated")
            queue.append(normalized)
            self._persist()

    def clear_pending_notification(self, market: str) -> None:
        with self._lock:
            queue = self._state["pending_notifications"].get(market)
            if not isinstance(queue, list) or not queue:
                return
            queue.pop(0)
            if not queue:
                self._state["pending_notifications"].pop(market, None)
            self._persist()

    def prune_pending_notifications(
        self,
        desired: set[tuple[str, str]],
    ) -> None:
        """Discard persisted runtime state outside the currently admitted scope.

        Pending delivery is operational work, not historical evidence.  A batch
        restored from disk must therefore prove that every one of its symbols is
        still admitted before it may be displayed or sent.  Mixed-scope batches
        are dropped whole so an old line/title/chart can never leak alongside an
        otherwise admitted event.
        """

        admitted = {
            (str(market).strip().lower(), str(code).strip())
            for market, code in desired
            if str(market).strip() and str(code).strip()
        }
        with self._lock:
            changed = bool(self._quarantined_pending_notifications)
            pending = self._state["pending_notifications"]
            source_pending = {
                market: list(queue) for market, queue in pending.items()
            }
            for market, queue in self._quarantined_pending_notifications.items():
                source_pending.setdefault(market, []).extend(queue)
            self._quarantined_pending_notifications = {}
            projected: dict[str, list[dict[str, object]]] = {}
            for market, queue in source_pending.items():
                market_text = str(market).strip().lower()
                retained = [
                    batch
                    for batch in queue
                    if isinstance(batch, Mapping)
                    and bool(batch.get("codes"))
                    and all(
                        (market_text, str(code).strip()) in admitted
                        for code in batch.get("codes", ())
                    )
                ]
                if retained:
                    projected[market_text] = retained
            if projected != pending:
                self._state["pending_notifications"] = projected
                changed = True

            for key in ("last_big_directions", "active_outages"):
                current = self._state[key]
                retained = {
                    identity: value
                    for identity, value in current.items()
                    if "|" in identity
                    and tuple(identity.split("|", 1)) in admitted
                }
                if retained != current:
                    self._state[key] = retained
                    changed = True
            if changed:
                self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _level_label(value: object) -> str:
    labels = {
        "1m": "1分钟",
        "5m": "5分钟",
        "30m": "30分钟",
        "d": "日线",
        "w": "周线",
        "m": "月线",
    }
    text = str(value or "").strip()
    return labels.get(text, text or "未知级别")


def build_non_a_monitor_universe(
    group_members: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    holding_group: str = "我的持仓",
    expanded_watchlist_markets: frozenset[str] = frozenset({"us"}),
) -> list[dict[str, object]]:
    """把全局分组合并为一个确定性的监听标的池。

    持仓组中的所有非 A 股成员都会保留；``expanded_watchlist_markets`` 中的市场还会
    纳入全部用户分组。同一标的即使出现在多个分组也只扫描一次，同时保留持仓身份和
    分组来源，供通知文案使用。
    """

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for group_name, members in group_members.items():
        group = str(group_name or "").strip()
        if (
            not group
            or isinstance(members, (str, bytes))
            or not isinstance(members, Sequence)
        ):
            continue
        for raw in members:
            if not isinstance(raw, Mapping):
                continue
            market = str(raw.get("market") or "").strip().lower()
            code = str(raw.get("code") or "").strip()
            if not market or not code or market == "a":
                continue
            is_holding = group == holding_group
            if not is_holding and market not in expanded_watchlist_markets:
                continue
            identity = (market, code)
            row = merged.setdefault(
                identity,
                {
                    "market": market,
                    "code": code,
                    "name": str(raw.get("name") or code).strip() or code,
                    "groups": set(),
                    "is_holding": False,
                },
            )
            row["groups"].add(group)
            row["is_holding"] = bool(row["is_holding"] or is_holding)
            candidate_name = str(raw.get("name") or "").strip()
            if candidate_name and row["name"] == code:
                row["name"] = candidate_name

    output: list[dict[str, object]] = []
    for identity in sorted(merged):
        row = merged[identity]
        is_holding = bool(row["is_holding"])
        output.append(
            {
                **row,
                "groups": sorted(row["groups"]),
                "monitoring_scope": "HOLDING" if is_holding else "WATCHLIST",
            }
        )
    return output


def _render_notification_time(value: object) -> str:
    if isinstance(value, datetime):
        parsed_time = value
    else:
        try:
            parsed_time = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return "暂不可用"
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        parsed_time = parsed_time.replace(tzinfo=CN)
    return parsed_time.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S")


def _notification_datetime(value: object) -> datetime | None:
    """Parse a structural event time using the monitor's market timezone."""

    if isinstance(value, datetime):
        parsed_time = value
    else:
        try:
            parsed_time = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        parsed_time = parsed_time.replace(tzinfo=CN)
    return parsed_time.astimezone(CN)


def _notification_event_value(event: object, name: str) -> object:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _notification_evidence_datetime(event: object) -> datetime | None:
    """Return when every fact required by this monitor event was observable."""

    segment_update = (
        str(_notification_event_value(event, "signal_role") or "")
        == "SEGMENT_DIFFERENCE_1M"
        or str(_notification_event_value(event, "new_stage") or "")
        == "segment_enriched"
    )
    if not segment_update:
        return _notification_datetime(
            _notification_event_value(event, "signal_time")
            or _notification_event_value(event, "signal_available_at")
            or _notification_event_value(event, "structure_confirmed_at")
        )
    setup_at = _notification_datetime(
        _notification_event_value(event, "setup_available_time")
        or _notification_event_value(event, "signal_available_at")
        or _notification_event_value(event, "structure_confirmed_at")
        or _notification_event_value(event, "setup_confirmed_time")
    )
    segment_at = _notification_datetime(
        _notification_event_value(event, "segment_difference_available_time")
        or _notification_event_value(event, "segment_difference_available_at")
        or _notification_event_value(event, "signal_time")
    )
    if setup_at is None:
        return segment_at
    if segment_at is None:
        return setup_at
    return max(setup_at, segment_at)


def _refresh_event_position_recommendation(
    event: object,
    *,
    detected_at: object,
    market: str,
) -> None:
    """用最终通知价格和真实发现时刻重算结构风险参考比例。"""

    side = str(getattr(event, "side", "") or "")
    if side not in {"buy", "sell"}:
        return
    big_direction = str(getattr(event, "big_dir", "") or "neutral")
    point_type = str(getattr(event, "bs_type", "") or "")
    recommendation = (
        "CAUTION"
        if big_direction == "neutral"
        or side == "buy" and big_direction == "down"
        or side == "sell" and big_direction == "up"
        else "READY"
    )
    realtime_quote_unavailable = bool(
        side == "buy"
        and str(getattr(event, "realtime_quote_status", "") or "")
        == "unavailable"
    )
    try:
        document = build_position_recommendation(
            side=side,
            recommendation=recommendation,
            risk_multiplier={
                "1buy": "0.50",
                "2buy": "1.00",
                "3buy": "0.75",
            }.get(point_type, "0"),
            context_risk_scale=(
                "1.00" if side == "buy" and big_direction == "up" else "0.50"
            ),
            # 已完成K线价只允许保留保守的 0% 保护，不能在实时价缺失时发布
            # 新的非零比例；后者在下方统一投影为 UNRESOLVED。
            entry_price=getattr(event, "price", None),
            structural_stop=getattr(event, "structure_invalidation_price", None),
            exit_action="none",
            structure_anchor_price=getattr(event, "structure_anchor_price", None),
        ).document()
    except (TypeError, ValueError):
        return
    if realtime_quote_unavailable and document.get("status") in {
        "RECOMMENDED",
        "UNRESOLVED",
    }:
        document.update(
            {
                "status": "UNRESOLVED",
                "basis": "REALTIME_PRICE_UNAVAILABLE",
                "recommended_ratio": None,
                "recommended_percent": None,
                "label": "结构风险参考：实时价格未取得，暂不生成买入比例",
                "reason_codes": ["REALTIME_PRICE_UNAVAILABLE"],
            }
        )
    setattr(event, "position_recommendation", document)


def _display_percent(value: object) -> tuple[str, str | None]:
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


def _position_reasons(event: object) -> set[str]:
    recommendation = getattr(event, "position_recommendation", None)
    return (
        {
            str(value)
            for value in recommendation.get("reason_codes", ())
            if isinstance(value, str)
        }
        if isinstance(recommendation, Mapping)
        else set()
    )


def _notification_position_line(event: object) -> str:
    side = str(getattr(event, "side", "") or "")
    recommendation = getattr(event, "position_recommendation", None)
    if not isinstance(recommendation, Mapping):
        return (
            "风险参考：买入比例待核对结构价格与风险参数"
            if side == "buy"
            else "风险参考：退出比例待核对结构级别"
        )
    status = str(recommendation.get("status") or "")
    percent = str(recommendation.get("recommended_percent") or "").strip()
    if status == "RECOMMENDED" and percent:
        displayed, model_value = _display_percent(percent)
        detail = f"精确模型值 {model_value}%" if model_value else "模型比较值"
        return (
            f"风险参考：结构模型比例上限 {displayed}%（{detail}）"
            if side == "buy"
            else f"风险参考：结构退出比例 {displayed}%（仅作结构模型比较）"
        )
    if status == "CONDITIONAL" and side == "sell":
        return (
            "风险参考：卖点与目标结构的级别关系待人工核对；同级或更高级别卖点"
            "按完整退出规则复核，低级别或不同结构仅作段差处理；"
            "关系未确认前不生成退出比例"
        )
    if status == "UNRESOLVED" and side == "buy" and (
        getattr(event, "realtime_quote_status", "") == "unavailable"
    ):
        return (
            "风险参考：暂不计算（实时价格未取得；"
            "不使用已完成K线价格生成买入比例）"
        )
    if status == "BLOCKED" and side == "buy":
        reasons = _position_reasons(event)
        reason = (
            "偏离结构锚点过远，不追价"
            if "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR" in reasons
            else "当前价已到达或跌破结构防守位"
            if "CURRENT_PRICE_AT_OR_BELOW_STRUCTURAL_STOP" in reasons
            else "具体限制原因未完整保存，请核对诊断证据"
        )
        return f"风险参考：本条买入不纳入操作计划（{reason}）"
    return (
        "风险参考：买入比例待核对结构价格与风险参数"
        if side == "buy"
        else "风险参考：退出比例待核对结构级别"
    )


def _event_operation_status(event: object) -> str:
    side = str(getattr(event, "side", "") or "")
    if str(getattr(event, "signal_role", "") or "") == (
        "SEGMENT_DIFFERENCE_1M"
    ):
        return "1分钟区间套定位新出现，仅补充精确位置"
    recommendation = getattr(event, "position_recommendation", None)
    status = (
        str(recommendation.get("status") or "")
        if isinstance(recommendation, Mapping)
        else ""
    )
    if side == "buy" and _position_reasons(event) & _BUY_PROTECTION_REASONS:
        return "禁止买入（0%保护）"
    if side == "buy":
        if getattr(event, "realtime_quote_status", "") == "unavailable":
            return "实时价格未取得，待人工复核"
        return "可人工复核执行" if status == "RECOMMENDED" else "仅观察，待人工复核"
    if side == "sell":
        return "卖出或退出复核"
    if side == "risk":
        return "环境转弱，仅作参考"
    return "待人工复核"


def _notification_line(
    event: object,
    *,
    detected_at: object | None = None,
    ordinal: int | None = None,
    total: int | None = None,
) -> str:
    """把一条事件压缩为结论优先、可直接复核的通知块。"""

    side = str(getattr(event, "side", "") or "")
    segment_update = str(getattr(event, "signal_role", "") or "") == (
        "SEGMENT_DIFFERENCE_1M"
    )
    point = str(getattr(event, "bs_type", "") or "")
    point_label = _POINT_LABELS.get(point, f"结构点（{point}）" if point else "结构提示")
    op_level = _level_label(getattr(event, "op_level", ""))
    big_level = _level_label(getattr(event, "big_level", ""))
    recursive_level = int(getattr(event, "recursive_level", 0) or 0)
    code = str(getattr(event, "code", "") or "")
    name = str(getattr(event, "name", "") or "")
    marker = f"[{ordinal}/{total}] " if ordinal and total and total > 1 else ""
    parts = [
        f"{marker}{code} {name}".strip()
        + f"｜状态：{_event_operation_status(event)}"
    ]

    signal_time = str(getattr(event, "signal_time", "") or "")
    confirmed_time = str(getattr(event, "confirmed_time", "") or signal_time)
    event_detected_at = detected_at or getattr(event, "detected_time", "") or signal_time
    signal_at = _notification_evidence_datetime(event)
    detected = _notification_datetime(event_detected_at)
    delay_text = "暂不可用"
    if signal_at is not None and detected is not None and detected >= signal_at:
        delay_seconds = int((detected - signal_at).total_seconds())
        minutes, seconds = divmod(delay_seconds, 60)
        delay_text = (
            f"{seconds}秒"
            if minutes == 0
            else f"{minutes}分钟"
            if seconds == 0
            else f"{minutes}分{seconds}秒"
        )

    try:
        price = float(getattr(event, "price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    price_source = str(getattr(event, "price_source", "") or "")
    price_label = {
        "realtime_tick": "当前价",
        "latest_completed_1m_close": "最近1分钟收盘价",
        "latest_completed_5m_close": "最近5分钟收盘价",
    }.get(price_source, "最近已完成K线收盘价")
    price_text = f"{price_label}：{price:.3f}" if price > 0 and isfinite(price) else "当前价：暂不可用"
    price_at = _render_notification_time(getattr(event, "price_observed_at", ""))
    if price_source == "realtime_tick" and price_at != "暂不可用":
        price_text += f"（获取 {price_at}）"
    if side in {"buy", "sell"}:
        try:
            anchor_price = float(getattr(event, "structure_anchor_price", 0) or 0)
            defense_price = float(getattr(event, "structure_invalidation_price", 0) or 0)
        except (TypeError, ValueError):
            anchor_price = 0.0
            defense_price = 0.0
        anchor_text = (
            f"锚点：{anchor_price:.3f}"
            if anchor_price > 0 and isfinite(anchor_price)
            else "锚点：暂不可用"
        )
        if anchor_price > 0 and isfinite(anchor_price) and price > 0 and isfinite(price):
            anchor_text += f"（{(price - anchor_price) / anchor_price:+.2%}）"
        defense_text = (
            f"防守位：{defense_price:.3f}"
            if defense_price > 0 and isfinite(defense_price)
            else "防守位：暂不可用"
        )
        if defense_price > 0 and isfinite(defense_price):
            defense_text += "（跌破买入结构失效）" if side == "buy" else "（突破卖出结构失效）"
        price_text += f"｜{anchor_text}｜{defense_text}"
    parts.append("价格：" + price_text)

    if side in {"buy", "sell"}:
        parts.append(_notification_position_line(event))
        time_parts = [
            (
                "5分钟操作确认 "
                + _render_notification_time(
                    getattr(event, "setup_confirmed_time", "") or confirmed_time
                )
            )
            if segment_update
            else f"操作确认 {_render_notification_time(confirmed_time)}"
        ]
        rendered_signal_time = _render_notification_time(signal_time)
        if segment_update:
            time_parts.append(f"1分钟定位可用 {rendered_signal_time}")
        elif rendered_signal_time != _render_notification_time(confirmed_time):
            time_parts.append(f"信号可用 {rendered_signal_time}")
        time_parts.append(
            f"监听发现 {_render_notification_time(event_detected_at)}（延迟 {delay_text}）"
        )
        parts.append("时间：" + "｜".join(time_parts))

        segment_point = str(getattr(event, "segment_difference_point_type", "") or "")
        if segment_point:
            segment_label = _POINT_LABELS.get(segment_point, f"结构点（{segment_point}）")
            divergence_label = _DIVERGENCE_LABELS.get(
                str(getattr(event, "segment_difference_divergence_kind", "") or "")
            )
            if divergence_label:
                segment_label = f"{segment_label}（{divergence_label}）"
            # 递归层级属于结构引擎内部血缘，不是 1 分钟主交易级别。
            # 对外只说明它是 5 分钟操作点的区间套精确定位，避免再次把
            # 1 分钟结构血缘误读成独立交易信号。
            segment_text = f"1分钟区间套定位：{segment_label}"
        else:
            segment_text = "1分钟区间套定位未完成（不影响5分钟主信号）"
        big_dir = _DIRECTION_LABELS.get(
            str(getattr(event, "big_dir", "") or ""),
            str(getattr(event, "big_dir", "") or "未知"),
        )
        parts.append(
            f"依据：{op_level}{point_label}（L{recursive_level}）｜"
            f"{big_level}{big_dir}｜{segment_text}"
        )
    else:
        parts.append(
            f"时间：结构确认 {_render_notification_time(confirmed_time)}｜"
            f"监听发现 {_render_notification_time(event_detected_at)}"
        )
        big_dir = _DIRECTION_LABELS.get(
            str(getattr(event, "big_dir", "") or ""),
            str(getattr(event, "big_dir", "") or "未知"),
        )
        parts.append(f"依据：{big_level}{big_dir}｜环境风险提示（不是买卖点）")

    position_reasons = _position_reasons(event)
    position_recommendation = getattr(event, "position_recommendation", None)
    position_status = (
        str(position_recommendation.get("status") or "")
        if isinstance(position_recommendation, Mapping)
        else ""
    )
    if segment_update:
        advice = (
            "操作：1分钟区间套证据已补充；先核对定位窗口，只有当前有效时"
            "才进入精确执行候选，仍须在其他交易软件手工决定"
        )
    elif side == "buy" and position_reasons & _BUY_PROTECTION_REASONS:
        advice = "操作：不追价，等待新的5分钟结构，仅在其他交易软件手工复核"
    elif side == "buy" and getattr(
        event, "realtime_quote_status", ""
    ) == "unavailable":
        advice = (
            "操作：5分钟买点已达到操作确认，但实时价格未取得；"
            "不使用已完成K线价格生成买入比例，等待实时价格证据后再复核"
        )
    elif side == "buy" and position_status == "BLOCKED":
        advice = "操作：本条买入不纳入操作计划；等待新的5分钟结构后再复核"
    elif side == "buy" and position_status != "RECOMMENDED":
        advice = (
            "操作：5分钟买点已达到操作确认，但结构价格或防守信息不足；"
            "暂不生成买入比例，补齐证据后再复核"
        )
    elif side == "risk":
        advice = "操作：仅核对30分钟逆风环境；等待5分钟卖点达到操作确认后再决定卖出"
    elif point in _POINT_ADVICE:
        advice = _POINT_ADVICE[point].replace("建议：", "操作：", 1)
        advice += "（在其他软件手工确认；系统不自动下单）"
    elif side == "buy":
        advice = "操作：人工确认后再复合买入条件（系统不自动下单）"
    elif side == "sell":
        advice = "操作：人工确认后复核卖出或退出条件（系统不自动下单）"
    else:
        advice = "操作：人工复核后再处理"
    parts.append(advice)
    return "\n".join(parts)


def _notification_bucket(event: object) -> str:
    side = str(getattr(event, "side", "") or "")
    if str(getattr(event, "signal_role", "") or "") == (
        "SEGMENT_DIFFERENCE_1M"
    ):
        return "segment_sell" if side == "sell" else "segment_buy"
    if side == "buy":
        if _position_reasons(event) & _BUY_PROTECTION_REASONS:
            return "buy_protected"
        recommendation = getattr(event, "position_recommendation", None)
        if isinstance(recommendation, Mapping) and recommendation.get("status") == "RECOMMENDED":
            return "buy_actionable"
        return "buy_review"
    if side in {"sell", "risk"}:
        return side
    return "other"


def _notification_event_batches(
    events: Sequence[object],
    *,
    maximum: int,
) -> list[list[object]]:
    """分开买、卖、保护与风险消息，并限制每条通知的事件数量。"""

    order = {
        "sell": 0,
        "risk": 1,
        "buy_actionable": 2,
        "buy_protected": 3,
        "buy_review": 4,
        "segment_sell": 5,
        "segment_buy": 6,
        "other": 7,
    }
    grouped: dict[str, list[object]] = {}
    for event in events:
        grouped.setdefault(_notification_bucket(event), []).append(event)
    batches: list[list[object]] = []
    for bucket in sorted(grouped, key=lambda value: (order.get(value, 99), value)):
        rows = sorted(
            grouped[bucket],
            key=lambda event: (
                getattr(event, "is_holding", True) is not True,
                str(getattr(event, "code", "") or ""),
                str(getattr(event, "bs_type", "") or ""),
            ),
        )
        batches.extend(
            rows[index : index + maximum]
            for index in range(0, len(rows), maximum)
        )
    return batches


def _pending_notification_delivery_priority(
    pending: Mapping[str, object],
) -> int:
    """Keep holding risk order intact after hand-off to the shared outbox.

    The holding ledger intentionally keeps its durable schema stable, so the
    priority is reconstructed from the persisted review projection.  Legacy
    rows without that projection still retain a fail-safe classification from
    their structural identities.
    """

    review_events = pending.get("review_events")
    documents = (
        [value for value in review_events if isinstance(value, Mapping)]
        if isinstance(review_events, list)
        else []
    )
    identities = pending.get("identities")
    identity_values = (
        [str(value) for value in identities]
        if isinstance(identities, list)
        else []
    )
    sides = {str(value.get("side") or "") for value in documents}
    if documents and all(
        value.get("new_stage") == "segment_enriched" for value in documents
    ):
        return 6
    if "sell" in sides or any(
        value.startswith("strict_sell_point|") for value in identity_values
    ):
        return 0

    transition_codes = pending.get("transition_codes")
    if (
        isinstance(transition_codes, list)
        and bool(transition_codes)
    ) or any(
        value.startswith("strict_30m_context_warning|")
        for value in identity_values
    ):
        return 1

    if "buy" not in sides and not any(
        value.startswith("strict_buy_point|") for value in identity_values
    ):
        return 5
    for document in documents:
        recommendation = document.get("position_recommendation")
        if not isinstance(recommendation, Mapping):
            continue
        reasons = recommendation.get("reason_codes")
        reason_values = (
            {str(value) for value in reasons}
            if isinstance(reasons, list)
            else set()
        )
        if reason_values & _BUY_PROTECTION_REASONS:
            return 3
    if any(
        isinstance(document.get("position_recommendation"), Mapping)
        and document["position_recommendation"].get("status") == "RECOMMENDED"
        for document in documents
    ):
        return 2
    return 4


def _notification_title(market: str, events: Sequence[object]) -> str:
    """把消息用途和人工动作放在标题最前面。"""

    scopes = {
        "人工关注" if getattr(event, "is_holding", True) is True else "普通关注"
        for event in events
    }
    scope = next(iter(scopes)) if len(scopes) == 1 else "关注线索"
    buckets = {_notification_bucket(event) for event in events}
    kind = (
        "1分钟卖出精确定位补充"
        if buckets == {"segment_sell"}
        else "1分钟买入精确定位补充"
        if buckets == {"segment_buy"}
        else
        "新买点·待人工确认"
        if buckets == {"buy_actionable"}
        else "买点确认·0%保护"
        if buckets == {"buy_protected"}
        else "买点观察·待人工复核"
        if buckets == {"buy_review"}
        else "卖出复核"
        if buckets == {"sell"}
        else "环境风险提示"
        if buckets == {"risk"}
        else "买卖点复核"
    )
    prefix = f"买卖通知｜{kind}｜{scope}"
    if len(events) != 1:
        return (
            f"{prefix}｜{_MARKET_LABELS.get(market, market.upper())}｜"
            f"{len(events)}条"
        )
    event = events[0]
    side = str(getattr(event, "side", "") or "")
    point = str(getattr(event, "bs_type", "") or "")
    point_label = _POINT_LABELS.get(point, "")
    if str(getattr(event, "signal_role", "") or "") == (
        "SEGMENT_DIFFERENCE_1M"
    ):
        segment_point = str(
            getattr(event, "segment_difference_point_type", "") or ""
        )
        segment_label = _POINT_LABELS.get(segment_point, "1分钟结构点")
        divergence_label = _DIVERGENCE_LABELS.get(
            str(getattr(event, "segment_difference_divergence_kind", "") or "")
        )
        if divergence_label:
            segment_label = f"{segment_label}（{divergence_label}）"
        parent_label = point_label or "5分钟买卖点"
        return (
            f"{prefix}｜{getattr(event, 'code', '')}｜"
            f"5分钟{parent_label}＋1分钟{segment_label}"
        )
    if side == "buy":
        label = point_label or (f"买点（{point}）" if point else "买点")
        level = _level_label(getattr(event, "op_level", ""))
    elif side == "sell":
        label = point_label or (f"卖点（{point}）" if point else "卖点")
        level = _level_label(getattr(event, "op_level", ""))
    elif side == "risk":
        label = "环境转弱风险（不是买卖点）"
        level = _level_label(getattr(event, "big_level", ""))
    else:
        label = "结构提示"
        level = _level_label(getattr(event, "op_level", ""))
    return f"{prefix}｜{getattr(event, 'code', '')}｜{level}{label}"


@dataclass(frozen=True, slots=True)
class HoldingGroupMonitorConfig:
    interval_seconds: int = 60
    start_delay_seconds: int = 8
    max_workers: int = 4
    max_events_per_notification: int = 3
    max_symbols: int = DEFAULT_VALIDATION_COHORT_SIZE
    large_scope_authorized: bool = False
    op_level: str = "5m"
    mid_level: str = "1m"
    big_level: str = "30m"

    def __post_init__(self) -> None:
        for field_name in (
            "interval_seconds",
            "start_delay_seconds",
            "max_workers",
            "max_events_per_notification",
            "max_symbols",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if type(self.large_scope_authorized) is not bool:
            raise TypeError("large_scope_authorized must be a bool")
        # Reuse the Web screening admission authority instead of maintaining a
        # second, weaker interpretation of the >20-symbol authorization gate.
        admit_screening_universe(
            max_symbols=self.max_symbols,
            large_scope_authorized=self.large_scope_authorized,
        )
        if len({self.op_level, self.mid_level, self.big_level}) != 3:
            raise ValueError("holding monitor levels must be distinct")
        if (self.op_level, self.mid_level, self.big_level) != (
            "5m",
            "1m",
            "30m",
        ):
            raise ValueError(
                "holding monitor requires 5m trade, 1m segment and 30m context levels"
            )


def _default_market_open(exchange: object, market: str, _now: datetime) -> bool:
    """使用适配器自己的日历/交易时段；状态未知时仍尝试扫描。

    若无法证明交易时段状态，部分数据库适配器会有意返回 ``None``。若把它视为休市，
    会静默抑制持仓监听，因此实际数据请求仍作为最终且可观察的闸门。
    """

    value = market_now_trading(exchange, market)
    if value is False:
        return False

    # 盈立会把若干美股盘前盘后状态报告为开市，但其 K 线接口当前提供本监听使用的常规
    # 交易时段序列。若纽约 06:00 用昨日 16:00 K 线扫描，会被误认为实时。辅助结构通道
    # 因此绑定常规交易时段；已知假日和特殊交易日仍由适配器状态关闭。
    if market == "us":
        local = _now.astimezone(ZoneInfo("America/New_York"))
        minute = local.hour * 60 + local.minute
        return local.weekday() < 5 and 9 * 60 + 30 <= minute < 16 * 60
    return value is not False


class HoldingGroupMonitorService:
    """增量观察范围获准的持仓/关注标的，并发送新事件。"""

    def __init__(
        self,
        *,
        positions_provider: Callable[[], Sequence[Mapping[str, object]]],
        notifier: object | None,
        state_root: Path,
        config: HoldingGroupMonitorConfig | None = None,
        exchange_provider: Callable[[Market], object] = get_exchange,
        market_open_provider: Callable[[object, str, datetime], bool] = (
            _default_market_open
        ),
        state_factory: Callable[..., object] = StrictPhysicalMonitorState,
        event_collector: Callable[..., list] = collect_strict_monitor_events,
        deduper_factory: Callable[[Path], object] = BoundedEventDeduper,
        clock: Callable[[], datetime] | None = None,
        review_inbox: object | None = None,
    ) -> None:
        if not callable(positions_provider):
            raise TypeError("positions_provider must be callable")
        if notifier is not None and not callable(getattr(notifier, "send", None)):
            raise TypeError("notifier must expose send")
        self._positions_provider = positions_provider
        self._notifier = notifier
        self._state_root = Path(state_root)
        self._config = config or HoldingGroupMonitorConfig()
        self._exchange_provider = exchange_provider
        self._market_open_provider = market_open_provider
        self._state_factory = state_factory
        self._event_collector = event_collector
        self._deduper_factory = deduper_factory
        self._clock = clock or (lambda: datetime.now(CN))
        if review_inbox is not None and (
            not callable(getattr(review_inbox, "record", None))
            or not callable(getattr(review_inbox, "update_delivery", None))
        ):
            raise TypeError("review_inbox must expose record and update_delivery")
        self._review_inbox = review_inbox
        self._runtime_ledger = HoldingMonitorRuntimeLedger(
            self._state_root / "holding_group_runtime.json",
            clock=self._clock,
        )
        self._states: dict[tuple[str, str], object] = {}
        self._exchanges: dict[str, object] = {}
        self._dedupers: dict[str, object] = {}
        # 多市场预热扫描可能耗时数秒，应与短时元数据锁分离，使页面和就绪请求无需等待
        # 市场输入输出完成。
        self._run_lock = threading.Lock()
        self._lock = threading.RLock()
        self._job_registered = False
        self._scheduler: object | None = None
        self._last_result: dict[str, object] | None = None
        self._admitted_identities: frozenset[tuple[str, str]] = frozenset()
        self._log = fun.get_logger()

    def _record_review_events(
        self,
        market: str,
        events: Sequence[object],
        *,
        status: str,
        reason: str | None = None,
    ) -> bool:
        if self._review_inbox is None:
            return True
        recorded_at = self._now()
        documents = [
            monitor_notification_event(
                market=market,
                event=event,
                delivery_status=status,
                delivery_reason=reason,
                recorded_at=recorded_at,
            )
            for event in events
        ]
        return self._record_review_documents(documents)

    def _record_review_documents(
        self,
        documents: Sequence[Mapping[str, object]],
    ) -> bool:
        if self._review_inbox is None:
            return True
        complete = True
        for document in documents:
            try:
                self._review_inbox.record(document)
            except Exception:
                complete = False
                self._log.exception(
                    "failed to record holding review notification market=%s code=%s",
                    str(document.get("market") or ""),
                    str(document.get("code") or ""),
                )
        return complete

    def _update_review_delivery(
        self,
        identities: Sequence[object],
        *,
        status: str,
        reason: str | None = None,
    ) -> bool:
        if self._review_inbox is None:
            return True
        try:
            self._review_inbox.update_delivery(
                [str(value) for value in identities],
                status=status,
                reason=reason,
            )
            return True
        except Exception:
            self._log.exception(
                "failed to update holding review notification delivery"
            )
            return False

    def _pending_notification_expired(
        self,
        pending: Mapping[str, object],
    ) -> bool:
        review_events = pending.get("review_events")
        detected_times: list[datetime] = []
        if isinstance(review_events, list) and review_events:
            for event in review_events:
                if not isinstance(event, Mapping):
                    return True
                detected_time = _notification_datetime(event.get("detected_at"))
                if detected_time is not None:
                    detected_times.append(detected_time)
        retry_started_at = _notification_datetime(pending.get("queued_at"))
        if retry_started_at is None and detected_times:
            retry_started_at = min(detected_times)
        if retry_started_at is None:
            return True
        # This is only a bounded external-delivery retry TTL. It never expires
        # the underlying 5m structure or changes its position recommendation.
        expires_at = retry_started_at + _PENDING_NOTIFICATION_RETRY_TTL
        return self._now() > expires_at

    def _attempt_pending_notification(
        self,
        market: str,
        pending: dict[str, object],
    ) -> tuple[bool, int, int]:
        """Resolve one durable batch as review -> transport -> projection.

        Returns ``(completed, delivered_now, expired_now)``.  Expiry applies
        only before external transport; a successfully transported batch is
        always retained until its review projection is reconciled.
        """

        review_recorded = pending.get("review_recorded") is True
        if not review_recorded:
            review_documents = pending.get("review_events", [])
            if not isinstance(review_documents, list) or not (
                self._record_review_documents(review_documents)
            ):
                self._runtime_ledger.record_failure(
                    "REVIEW_INBOX_RECORD_FAILED"
                )
                return False, 0, 0
            pending["review_recorded"] = True
            self._runtime_ledger.set_pending_notification(market, pending)

        identities = list(pending["identities"])
        event_count = int(pending["event_count"])
        transport_status = pending.get("transport_status")
        if (
            transport_status not in {"delivered", "simulated"}
            and self._pending_notification_expired(pending)
        ):
            if not self._update_review_delivery(
                identities,
                status="expired",
                reason="NOTIFICATION_DELIVERY_EXPIRED",
            ):
                self._runtime_ledger.record_failure(
                    "REVIEW_INBOX_UPDATE_FAILED"
                )
                return False, 0, 0
            self._deduper(market).mark_identities(identities)
            self._runtime_ledger.record_expired(event_count)
            self._runtime_ledger.clear_pending_notification(market)
            return True, 0, event_count

        sent_now = 0
        if transport_status not in {"delivered", "simulated"}:
            delivered = self._deliver(
                str(pending["title"]),
                list(pending["lines"]),
                event_count=event_count,
                charts=tuple(pending.get("charts", ())),
                delivery_priority=_pending_notification_delivery_priority(pending),
            )
            if not delivered:
                self._update_review_delivery(
                    identities,
                    status="failed",
                    reason="NOTIFICATION_DELIVERY_FAILED",
                )
                return False, 0, 0
            transport_status = (
                "simulated"
                if getattr(self._notifier, "dry_run", False) is True
                else "delivered"
            )
            pending["transport_status"] = transport_status
            pending["transport_completed_at"] = self._now().isoformat()
            # Checkpoint external success before the secondary projection so
            # a restart can never resend an already delivered DingTalk batch.
            self._runtime_ledger.set_pending_notification(market, pending)
            sent_now = event_count

        if not self._update_review_delivery(
            identities,
            status=str(transport_status),
        ):
            self._runtime_ledger.record_failure(
                "REVIEW_INBOX_UPDATE_FAILED"
            )
            return False, sent_now, 0
        self._deduper(market).mark_identities(identities)
        self._runtime_ledger.clear_pending_notification(market)
        return True, sent_now, 0

    def _drain_pending_notifications(self, market: str) -> dict[str, object]:
        sent_count = 0
        expired_count = 0
        failed_codes: set[str] = set()
        failed_transition_codes: set[str] = set()
        pending = self._runtime_ledger.pending_notification(market)
        while pending is not None:
            completed, delivered_now, expired_now = (
                self._attempt_pending_notification(market, pending)
            )
            sent_count += delivered_now
            expired_count += expired_now
            if not completed:
                failed_codes.update(str(value) for value in pending["codes"])
                failed_transition_codes.update(
                    str(value) for value in pending["transition_codes"]
                )
                break
            pending = self._runtime_ledger.pending_notification(market)
        return {
            "sent_count": sent_count,
            "expired_count": expired_count,
            "notification_failed": pending is not None,
            "failed_codes": failed_codes,
            "failed_transition_codes": failed_transition_codes,
        }

    def _deliver(
        self,
        title: str,
        lines: list[str],
        *,
        event_count: int,
        charts: Sequence[Mapping[str, object]] = (),
        delivery_priority: int = 50,
    ) -> bool:
        if self._notifier is None or not bool(
            getattr(self._notifier, "available", True)
        ):
            self._runtime_ledger.record_failure("NOTIFIER_UNAVAILABLE")
            return False
        try:
            send_rich = getattr(self._notifier, "send_rich", None)
            delivered = bool(
                send_rich(
                    title,
                    lines,
                    {
                        "charts": list(charts),
                        "delivery_priority": delivery_priority,
                        "require_evidence_match": any(
                            value.get("evidence_required") is True for value in charts
                        ),
                    },
                )
                if callable(send_rich) and charts
                else self._notifier.send(title, lines)
            )
        except Exception as exc:
            self._runtime_ledger.record_failure(
                f"{type(exc).__name__}: {str(exc)[:120]}"
            )
            return False
        if delivered:
            if getattr(self._notifier, "dry_run", False) is True:
                self._runtime_ledger.record_simulated()
            else:
                self._runtime_ledger.record_success(event_count)
            return True
        self._runtime_ledger.record_failure("NOTIFIER_RETURNED_FALSE")
        return False

    def _event_notification_payload(
        self,
        market: str,
        events: Sequence[object],
    ) -> dict[str, object]:
        queued_at = self._now()
        queued_at_text = queued_at.isoformat()
        review_events = [
            monitor_notification_event(
                market=market,
                event=event,
                delivery_status="pending",
                recorded_at=queued_at,
            )
            for event in events
            if str(getattr(event, "side", "") or "") in {"buy", "sell"}
        ]
        return {
            "title": _notification_title(market, events),
            "lines": [
                _notification_line(
                    event,
                    detected_at=(
                        getattr(event, "detected_time", "") or queued_at
                    ),
                    ordinal=index,
                    total=len(events),
                )
                for index, event in enumerate(events, start=1)
            ],
            "identities": [_delivery_identity(event) for event in events],
            "codes": [str(getattr(event, "code", "")) for event in events],
            "charts": [
                {
                    "market": market,
                    "code": str(getattr(event, "code", "")),
                    "name": str(
                        getattr(event, "name", "") or getattr(event, "code", "")
                    ),
                    "artifact_key": str(getattr(event, "identity")),
                    "observed_at": str(
                        getattr(event, "detected_time", "") or queued_at_text
                    ),
                    "point_type": str(
                        getattr(event, "segment_difference_point_type", "")
                        if str(getattr(event, "signal_role", "") or "")
                        == "SEGMENT_DIFFERENCE_1M"
                        else getattr(event, "bs_type", "")
                    ),
                    "signal_time": str(getattr(event, "signal_time", "")),
                    "evidence_id": str(
                        getattr(event, "segment_difference_evidence_id", "")
                        if str(getattr(event, "signal_role", "") or "")
                        == "SEGMENT_DIFFERENCE_1M"
                        else getattr(event, "evidence_id", "")
                    ),
                    "recursive_level": int(
                        getattr(event, "segment_difference_recursive_level", 0)
                        if str(getattr(event, "signal_role", "") or "")
                        == "SEGMENT_DIFFERENCE_1M"
                        else getattr(event, "recursive_level", 0)
                    ),
                    "anchor_time": str(
                        getattr(event, "segment_difference_anchor_time", "")
                        if str(getattr(event, "signal_role", "") or "")
                        == "SEGMENT_DIFFERENCE_1M"
                        else getattr(event, "anchor_time", "")
                    ),
                    "frequency": str(
                        getattr(event, "mid_level", "1m") or "1m"
                        if str(getattr(event, "signal_role", "") or "")
                        == "SEGMENT_DIFFERENCE_1M"
                        else getattr(event, "op_level", "5m") or "5m"
                    ),
                    "evidence_required": bool(
                        str(getattr(event, "bs_type", ""))
                        and str(getattr(event, "signal_time", ""))
                    ),
                }
                for event in events
            ],
            "transition_codes": [
                str(getattr(event, "code", ""))
                for event in events
                if str(getattr(event, "kind", ""))
                == "strict_30m_context_warning"
            ],
            "queued_at": queued_at_text,
            "event_count": len(events),
            "review_events": review_events,
            "review_recorded": False,
            "transport_status": None,
            "transport_completed_at": None,
        }

    def _publish_result(self, result: Mapping[str, object]) -> dict[str, object]:
        published = dict(result)
        with self._lock:
            self._last_result = published
        return dict(published)

    def _notification_delivery_snapshot(self) -> dict[str, object]:
        value = self._runtime_ledger.snapshot()
        if self._notifier is None or not bool(
            getattr(self._notifier, "available", True)
        ):
            mode = "UNAVAILABLE"
        elif getattr(self._notifier, "dry_run", False) is True:
            mode = "DRY_RUN"
        else:
            mode = "LIVE_TRANSPORT"
        value["delivery_mode"] = mode
        return value

    def admitted_identities(self) -> tuple[tuple[str, str], ...]:
        """Return the last scope-proven cross-market universe for page filtering."""

        with self._lock:
            return tuple(sorted(self._admitted_identities))

    def _replace_admitted_identities(
        self,
        desired: set[tuple[str, str]],
    ) -> None:
        with self._lock:
            self._admitted_identities = frozenset(desired)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("holding monitor clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("holding monitor clock must be timezone-aware")
        return value.astimezone(CN)

    def _deduper(self, market: str):
        value = self._dedupers.get(market)
        if value is None:
            value = self._deduper_factory(
                self._state_root / f"holding_group_monitor_{market}.json"
            )
            self._dedupers[market] = value
        return value

    def _exchange(self, market: str):
        value = self._exchanges.get(market)
        if value is None:
            value = self._exchange_provider(Market(market))
            self._exchanges[market] = value
        return value

    @staticmethod
    def _normalize_positions(
        rows: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        valid_by_identity: dict[tuple[str, str], dict[str, object]] = {}
        invalid: list[dict[str, object]] = []
        for raw in rows:
            market = str(raw.get("market") or "").strip().lower()
            code = str(raw.get("code") or "").strip()
            name = str(raw.get("name") or code).strip() or code
            identity = (market, code)
            if not market or not code:
                invalid.append(
                    {
                        "market": market,
                        "code": code,
                        "name": name,
                        "status": "error",
                        "reason_code": "INVALID_HOLDING_IDENTITY",
                    }
                )
                continue
            if market not in _VALID_MARKETS:
                invalid.append(
                    {
                        "market": market,
                        "code": code,
                        "name": name,
                        "status": "error",
                        "reason_code": "UNSUPPORTED_MARKET",
                    }
                )
                continue
            raw_groups = raw.get("groups")
            groups = {
                str(value).strip()
                for value in (
                    raw_groups
                    if isinstance(raw_groups, (list, tuple, set, frozenset))
                    else ()
                )
                if str(value).strip()
            }
            is_holding = raw.get("is_holding", True) is True
            existing = valid_by_identity.get(identity)
            if existing is None:
                valid_by_identity[identity] = {
                    "market": market,
                    "code": code,
                    "name": name,
                    "groups": sorted(groups),
                    "is_holding": is_holding,
                    "monitoring_scope": ("HOLDING" if is_holding else "WATCHLIST"),
                }
                continue
            existing["groups"] = sorted(set(existing.get("groups", ())) | groups)
            combined_holding = bool(existing.get("is_holding") or is_holding)
            existing["is_holding"] = combined_holding
            existing["monitoring_scope"] = (
                "HOLDING" if combined_holding else "WATCHLIST"
            )
            if existing["name"] == code and name != code:
                existing["name"] = name
        valid = list(valid_by_identity.values())
        valid.sort(key=lambda row: (row["market"], row["code"]))
        invalid.sort(key=lambda row: (str(row["market"]), str(row["code"])))
        return valid, invalid

    @staticmethod
    def _scope_identity(row: Mapping[str, object]) -> str:
        return HoldingMonitorRuntimeLedger.identity(
            str(row.get("market") or "").strip().lower(),
            str(row.get("code") or "").strip(),
        )

    def _admit_positions(
        self,
        rows: Sequence[dict[str, object]],
    ) -> tuple[list[dict[str, object]], ScreeningUniverseAdmission]:
        """Admit holdings first and defer ordinary group members deterministically."""

        by_identity = {self._scope_identity(row): row for row in rows}
        mandatory = tuple(
            identity
            for identity, row in by_identity.items()
            if row.get("is_holding") is True
        )
        optional = tuple(
            identity
            for identity, row in by_identity.items()
            if row.get("is_holding") is not True
        )
        admission = admit_screening_universe(
            mandatory_codes=mandatory,
            signal_codes=optional,
            max_symbols=self._config.max_symbols,
            large_scope_authorized=self._config.large_scope_authorized,
        )
        return (
            [by_identity[identity] for identity in admission.admitted_codes],
            admission,
        )

    def _sync_market_states(
        self,
        market: str,
        rows: Sequence[Mapping[str, object]],
        exchange: object,
    ) -> dict[str, object]:
        desired = {row["code"] for row in rows}
        for identity in tuple(self._states):
            if identity[0] == market and identity[1] not in desired:
                self._states.pop(identity, None)
        states: dict[str, object] = {}
        for row in rows:
            code = row["code"]
            identity = (market, code)
            state = self._states.get(identity)
            if state is None:
                state = self._state_factory(
                    code,
                    exchange,
                    op_level=self._config.op_level,
                    mid_level=self._config.mid_level,
                    big_level=self._config.big_level,
                )
                self._states[identity] = state
            states[code] = state
        return states

    def _market_failure_rows(
        self,
        rows: Sequence[Mapping[str, object]],
        reason_code: str,
        error: Exception | None = None,
    ) -> list[dict[str, object]]:
        detail = None
        if error is not None:
            detail = f"{type(error).__name__}: {str(error)[:160]}"
        return [
            {
                **dict(row),
                "status": "error",
                "reason_code": reason_code,
                "error": detail,
                "op_level": self._config.op_level,
                "mid_level": self._config.mid_level,
                "big_level": self._config.big_level,
            }
            for row in rows
        ]

    def _run_market(
        self,
        market: str,
        rows: Sequence[Mapping[str, object]],
        observed_at: datetime,
    ) -> dict[str, object]:
        delivery = self._drain_pending_notifications(market)
        sent_count = int(delivery["sent_count"])
        expired_count = int(delivery["expired_count"])
        notification_failed = delivery["notification_failed"] is True
        failed_delivery_codes = set(delivery["failed_codes"])
        failed_transition_codes = set(delivery["failed_transition_codes"])
        try:
            exchange = self._exchange(market)
        except Exception as exc:
            return {
                "market": market,
                "status": "error",
                "reason_code": "MARKET_ADAPTER_UNAVAILABLE",
                "positions": self._market_failure_rows(
                    rows, "MARKET_ADAPTER_UNAVAILABLE", exc
                ),
                "event_count": 0,
                "sent_count": sent_count,
                "expired_count": expired_count,
            }
        try:
            is_open = bool(self._market_open_provider(exchange, market, observed_at))
        except Exception as exc:
            return {
                "market": market,
                "status": "error",
                "reason_code": "MARKET_SESSION_UNAVAILABLE",
                "positions": self._market_failure_rows(
                    rows, "MARKET_SESSION_UNAVAILABLE", exc
                ),
                "event_count": 0,
                "sent_count": sent_count,
                "expired_count": expired_count,
            }
        if not is_open:
            return {
                "market": market,
                "status": "degraded" if notification_failed else "not_due",
                "reason_code": (
                    "NOTIFICATION_DELIVERY_FAILED"
                    if notification_failed
                    else "MARKET_CLOSED"
                ),
                "positions": [
                    {
                        **dict(row),
                        "status": "market_closed",
                        "reason_code": "MARKET_CLOSED",
                        "op_level": self._config.op_level,
                        "mid_level": self._config.mid_level,
                        "big_level": self._config.big_level,
                    }
                    for row in rows
                ],
                "event_count": 0,
                "sent_count": sent_count,
                "expired_count": expired_count,
            }

        states = self._sync_market_states(market, rows, exchange)
        names = {str(row["code"]): str(row["name"]) for row in rows}
        holding_codes = {
            str(row["code"]) for row in rows if row.get("is_holding", True) is True
        }
        try:
            events = self._event_collector(
                states,
                names=names,
                holdings=holding_codes,
            )
            detected_time = self._now().isoformat(timespec="seconds")
            for event in events:
                setattr(event, "detected_time", detected_time)
                setattr(
                    event,
                    "is_holding",
                    str(getattr(event, "code", "")) in holding_codes,
                )
        except Exception as exc:
            return {
                "market": market,
                "status": "error",
                "reason_code": "STRUCTURE_EVALUATION_FAILED",
                "positions": self._market_failure_rows(
                    rows, "STRUCTURE_EVALUATION_FAILED", exc
                ),
                "event_count": 0,
                "sent_count": sent_count,
                "expired_count": expired_count,
            }

        refresh_failures = {
            code: int(getattr(state, "consecutive_refresh_failures", 0) or 0)
            for code, state in states.items()
        }
        warmup_incomplete = {
            code: int(getattr(state, "consecutive_warmup_incomplete", 0) or 0)
            for code, state in states.items()
        }
        refresh_failed_codes = {
            code for code, count in refresh_failures.items() if count > 0
        }
        warming_codes = {
            code
            for code, state in states.items()
            if code not in refresh_failed_codes
            and getattr(state, "warmup_ready", True) is not True
        }
        stalled_warmup_codes = {
            code for code in warming_codes if warmup_incomplete.get(code, 0) >= 3
        }
        failed_codes = refresh_failed_codes | stalled_warmup_codes
        # 绝不发布由过期状态或不完整多周期预热计算的线索。刷新失败后收集器仍可能返回
        # 最近缓存事件，因此这里必须有显式关闭失败闸门，不能假设收集器行为。
        events = [
            event
            for event in events
            if str(getattr(event, "code", "")) not in failed_codes | warming_codes
        ]
        for event in events:
            # 严格监听在可选 1 分钟通道不可用时会明确回退到最近 5 分钟
            # 收盘价。这里只为旧事件补默认值，不能把真实的回退来源重新标成
            # 1 分钟，否则通知与人工复核会伪造行情精度。
            if not str(getattr(event, "price_source", "") or "").strip():
                setattr(event, "price_source", "latest_completed_1m_close")
            if str(getattr(event, "side", "") or "") == "buy":
                # 买点的比例和“可复核执行”文案必须有本轮实时 tick 才能成立。
                # 已完成 K 线价格仍可作为结构位置证据展示，但不能替代当前价。
                setattr(event, "realtime_quote_status", "unavailable")
        tick_reader = getattr(exchange, "ticks", None)
        if events and callable(tick_reader):
            try:
                event_codes = sorted(
                    {
                        str(getattr(event, "code", ""))
                        for event in events
                        if str(getattr(event, "code", ""))
                    }
                )
                ticks = tick_reader(event_codes) or {}
                quote_observed_at = self._now().isoformat(timespec="seconds")
                if not isinstance(ticks, Mapping):
                    raise TypeError("exchange ticks must return a mapping")
                for event in events:
                    tick = ticks.get(str(getattr(event, "code", "")))
                    try:
                        live_price = float(getattr(tick, "last"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if live_price > 0 and isfinite(live_price):
                        event.price = live_price
                        event.price_source = "realtime_tick"
                        event.price_observed_at = quote_observed_at
                        if str(getattr(event, "side", "") or "") == "buy":
                            event.realtime_quote_status = "verified"
            except Exception as exc:
                # Quote enrichment is informative. A transient tick failure
                # must not erase a structurally valid event or its review row.
                self._log.warning(
                    "holding notification realtime quote unavailable market=%s: %s: %s",
                    market,
                    type(exc).__name__,
                    str(exc)[:120],
                )
        for event in events:
            _refresh_event_position_recommendation(
                event,
                detected_at=(getattr(event, "detected_time", "") or self._now()),
                market=market,
            )
        current_directions: dict[str, str] = {}
        for code, state in states.items():
            # 部分预热不具权威性；若持久化其高级别方向，会让首个完全有效的向下转变看似旧状态，
            # 从而抑制对应持仓环境转弱提示。
            if code in failed_codes or code in warming_codes:
                continue
            try:
                direction = str(state.big_dir() or "neutral")
            except Exception:
                direction = "neutral"
            current_directions[code] = direction

        # 30分钟方向只是一条环境风险提示，不是退出信号。提示仍只表达状态转变；
        # 上一方向会持久化，应用重启不会重复提醒未变化的下行段。
        events = [
            event
            for event in events
            if not (
                str(getattr(event, "kind", ""))
                == "strict_30m_context_warning"
                and self._runtime_ledger.previous_direction(
                    market, str(getattr(event, "code", ""))
                )
                == "down"
            )
        ]
        deduper = self._deduper(market)
        pending = self._runtime_ledger.pending_notification(market)

        queued_identities = self._runtime_ledger.pending_identities(market)
        fresh = [
            event
            for event in fresh_monitor_events(events, deduper)
            if _delivery_identity(event) not in queued_identities
        ]
        if fresh:
            # Keep different structural availability times in separate durable
            # batches. One stale event must never expire a newer event that was
            # discovered in the same scan.
            grouped_fresh: dict[str, list[object]] = {}
            for event in fresh:
                signal_time = _notification_evidence_datetime(event)
                signal_key = (
                    signal_time.isoformat()
                    if signal_time is not None
                    else f"invalid:{_delivery_identity(event)}"
                )
                grouped_fresh.setdefault(signal_key, []).append(event)
            payloads = [
                self._event_notification_payload(market, batch)
                for key in sorted(grouped_fresh)
                for batch in _notification_event_batches(
                    grouped_fresh[key],
                    maximum=self._config.max_events_per_notification,
                )
            ]
            for payload in payloads:
                self._runtime_ledger.enqueue_pending_notification(market, payload)
            # If no older failed batch is blocking the queue, resolve all newly
            # durable batches immediately in structural-time order.
            if pending is None:
                current_delivery = self._drain_pending_notifications(market)
                sent_count += int(current_delivery["sent_count"])
                expired_count += int(current_delivery["expired_count"])
                notification_failed = (
                    notification_failed
                    or current_delivery["notification_failed"] is True
                )
                failed_delivery_codes.update(current_delivery["failed_codes"])
                failed_transition_codes.update(
                    current_delivery["failed_transition_codes"]
                )
            else:
                for payload in payloads:
                    failed_delivery_codes.update(payload["codes"])
                    failed_transition_codes.update(payload["transition_codes"])

        # 健康状态仍在就绪和页面状态中可见，但面向用户的钉钉通道只用于真实结构事件。
        health_alert_count = 0
        issue_counts = {
            code: (
                refresh_failures.get(code, 0)
                if code in refresh_failed_codes
                else warmup_incomplete.get(code, 0)
                if code in stalled_warmup_codes
                else 0
            )
            for code in states
        }
        for code, failure_count in issue_counts.items():
            outage_active = self._runtime_ledger.outage_active(market, code)
            if failure_count >= 3 and not outage_active:
                self._runtime_ledger.set_outage(market, code, True)
            elif (
                failure_count == 0
                and outage_active
                and getattr(states[code], "warmup_ready", True) is True
            ):
                self._runtime_ledger.set_outage(market, code, False)

        if notification_failed:
            current_directions = {
                code: direction
                for code, direction in current_directions.items()
                if code not in failed_transition_codes
            }
        self._runtime_ledger.update_directions(market, current_directions)

        positions: list[dict[str, object]] = []
        event_codes = {str(event.code) for event in events}
        for row in rows:
            code = row["code"]
            if code in failed_codes:
                status = "error"
                reason = (
                    "MULTI_TIMEFRAME_WARMUP_STALLED"
                    if code in stalled_warmup_codes
                    else "MARKET_DATA_OR_STRUCTURE_REFRESH_FAILED"
                )
            elif code in failed_delivery_codes:
                status = "error"
                reason = "NOTIFICATION_DELIVERY_FAILED"
            elif code in warming_codes:
                status = "warming_up"
                reason = "MULTI_TIMEFRAME_WARMUP_INCOMPLETE"
            else:
                status = "monitoring"
                reason = "MONITORING_ACTIVE"
            positions.append(
                {
                    **dict(row),
                    "status": status,
                    "reason_code": reason,
                    "event_present": code in event_codes,
                    "refresh_failure_count": refresh_failures.get(code, 0),
                    "warmup_incomplete_count": warmup_incomplete.get(code, 0),
                    "warmup_ready": code not in warming_codes,
                    "segment_difference_ready": bool(
                        getattr(states[code], "segment_difference_ready", False)
                    ),
                    "segment_difference_reason": str(
                        getattr(
                            states[code],
                            "segment_difference_reason",
                            "UNAVAILABLE",
                        )
                    ),
                    "op_level": self._config.op_level,
                    "mid_level": self._config.mid_level,
                    "big_level": self._config.big_level,
                }
            )
        has_failure = bool(failed_codes or notification_failed)
        return {
            "market": market,
            "status": "degraded" if has_failure else "monitoring",
            "reason_code": (
                "HOLDING_MARKET_MONITOR_DEGRADED"
                if has_failure
                else "MONITORING_ACTIVE"
            ),
            "positions": positions,
            "event_count": len(events),
            "sent_count": sent_count,
            "expired_count": expired_count,
            "health_alert_count": health_alert_count,
        }

    def run_once(self) -> dict[str, object]:
        with self._run_lock:
            started = perf_counter()
            observed_at = self._now()
            try:
                raw_positions = self._positions_provider()
                if not isinstance(raw_positions, Sequence):
                    raise TypeError("positions provider must return a sequence")
                valid, invalid = self._normalize_positions(raw_positions)
            except Exception as exc:
                # Without a fresh universe proof no restored symbol is trusted.
                # Purge before any notification, exchange or structure access.
                self._runtime_ledger.prune_pending_notifications(set())
                self._states.clear()
                self._replace_admitted_identities(set())
                result = {
                    "schema": SCHEMA,
                    "observed_at": observed_at.isoformat(),
                    "status": "error",
                    "reason_code": "HOLDING_GROUP_UNAVAILABLE",
                    "scope_authorized": False,
                    "scope_limit": self._config.max_symbols,
                    "large_scope_authorized": self._config.large_scope_authorized,
                    "requested_count": 0,
                    "mandatory_count": 0,
                    "deferred_count": 0,
                    "declared_count": 0,
                    "monitored_count": 0,
                    "covered_count": 0,
                    "active_count": 0,
                    "closed_count": 0,
                    "awaiting_count": 0,
                    "failed_count": 1,
                    "event_count": 0,
                    "sent_count": 0,
                    "expired_count": 0,
                    "positions": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "completed_at": self._now().isoformat(),
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "notification_delivery": self._notification_delivery_snapshot(),
                }
                return self._publish_result(result)

            requested_count = len(valid) + len(invalid)
            try:
                valid, admission = self._admit_positions(valid)
            except ScreeningScopeAuthorizationError as exc:
                # Mandatory holdings are atomic.  If they do not fit, do not
                # silently drop one and do not touch any market-data provider.
                self._runtime_ledger.prune_pending_notifications(set())
                self._states.clear()
                self._replace_admitted_identities(set())
                return self._publish_result(
                    {
                        "schema": SCHEMA,
                        "observed_at": observed_at.isoformat(),
                        "status": "error",
                        "reason_code": "HOLDING_MONITOR_SCOPE_EXCEEDED",
                        "scope_authorized": False,
                        "scope_limit": self._config.max_symbols,
                        "large_scope_authorized": (
                            self._config.large_scope_authorized
                        ),
                        "requested_count": requested_count,
                        "mandatory_count": sum(
                            row.get("is_holding") is True for row in valid
                        ),
                        "deferred_count": 0,
                        "declared_count": 0,
                        "monitored_count": 0,
                        "covered_count": 0,
                        "active_count": 0,
                        "closed_count": 0,
                        "awaiting_count": 0,
                        "failed_count": 1,
                        "event_count": 0,
                        "sent_count": 0,
                        "expired_count": 0,
                        "positions": [],
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                        "completed_at": self._now().isoformat(),
                        "duration_ms": round(
                            (perf_counter() - started) * 1000,
                            3,
                        ),
                        "notification_delivery": (
                            self._notification_delivery_snapshot()
                        ),
                        "real_account_accessed": False,
                        "real_order_transport_enabled": False,
                        "automated_order_authorized": False,
                        "live_status": "LIVE_DISABLED",
                    }
                )

            desired = {(row["market"], row["code"]) for row in valid}
            self._runtime_ledger.prune_pending_notifications(desired)
            self._replace_admitted_identities(desired)
            for identity in tuple(self._states):
                if identity not in desired:
                    self._states.pop(identity, None)
            scope_fields = {
                "scope_authorized": True,
                "scope_limit": self._config.max_symbols,
                "large_scope_authorized": self._config.large_scope_authorized,
                "requested_count": requested_count,
                "mandatory_count": len(admission.mandatory_codes),
                "deferred_count": len(admission.deferred_signal_codes),
            }

            with self._lock:
                first_result_pending = self._last_result is None
            if first_result_pending:
                # 首次多周期暖机可能持续数分钟。先发布已验证的监听范围，页面便能
                # 立即展示全部标的及“等待首次检查”，而不是在整轮完成前误显空列表。
                bootstrap_positions = [
                    {
                        **dict(row),
                        "status": "awaiting_first_run",
                        "reason_code": "HOLDING_MONITOR_AWAITING_FIRST_RUN",
                        "event_present": False,
                        "op_level": self._config.op_level,
                        "mid_level": self._config.mid_level,
                        "big_level": self._config.big_level,
                    }
                    for row in valid
                ] + [dict(row) for row in invalid]
                bootstrap_positions.sort(
                    key=lambda row: (str(row["market"]), str(row["code"]))
                )
                bootstrap_failed = len(invalid)
                bootstrap_awaiting = len(valid)
                self._publish_result(
                    {
                        "schema": SCHEMA,
                        "observed_at": observed_at.isoformat(),
                        **scope_fields,
                        "status": (
                            "degraded"
                            if bootstrap_failed
                            else "warming_up"
                            if bootstrap_awaiting
                            else "ready"
                        ),
                        "reason_code": (
                            "HOLDING_MONITOR_DEGRADED"
                            if bootstrap_failed
                            else "HOLDING_MONITOR_AWAITING_FIRST_RUN"
                            if bootstrap_awaiting
                            else "READY"
                        ),
                        "declared_count": len(bootstrap_positions),
                        "monitored_count": 0,
                        "covered_count": len(valid),
                        "active_count": 0,
                        "closed_count": 0,
                        "awaiting_count": bootstrap_awaiting,
                        "failed_count": bootstrap_failed,
                        "event_count": 0,
                        "sent_count": 0,
                        "positions": bootstrap_positions,
                        "completed_at": None,
                        "duration_ms": round(
                            (perf_counter() - started) * 1000,
                            3,
                        ),
                    }
                )

            grouped: dict[str, list[dict[str, object]]] = {}
            for row in valid:
                grouped.setdefault(row["market"], []).append(row)

            market_results: list[dict[str, object]] = []
            for market in sorted(
                self._runtime_ledger.pending_markets() - set(grouped)
            ):
                delivery = self._drain_pending_notifications(market)
                notification_failed = delivery["notification_failed"] is True
                market_results.append(
                    {
                        "market": market,
                        "status": "degraded" if notification_failed else "not_due",
                        "reason_code": (
                            "NOTIFICATION_DELIVERY_FAILED"
                            if notification_failed
                            else "PENDING_NOTIFICATION_RESOLVED"
                        ),
                        "positions": [],
                        "event_count": 0,
                        "sent_count": int(delivery["sent_count"]),
                        "expired_count": int(delivery["expired_count"]),
                        "health_alert_count": 0,
                    }
                )
            if grouped:
                worker_count = min(self._config.max_workers, len(grouped))
                if worker_count == 1:
                    market_results.extend(
                        [
                            self._run_market(market, rows, observed_at)
                            for market, rows in sorted(grouped.items())
                        ]
                    )
                else:
                    with ThreadPoolExecutor(
                        max_workers=worker_count,
                        thread_name_prefix="HoldingGroupMonitor",
                    ) as executor:
                        futures = [
                            executor.submit(
                                self._run_market,
                                market,
                                rows,
                                observed_at,
                            )
                            for market, rows in sorted(grouped.items())
                        ]
                        market_results.extend(
                            future.result() for future in futures
                        )

            positions = list(invalid)
            for market_result in market_results:
                positions.extend(market_result["positions"])
            positions.sort(key=lambda row: (str(row["market"]), str(row["code"])))
            failed_count = sum(row.get("status") == "error" for row in positions)
            active_count = sum(row.get("status") == "monitoring" for row in positions)
            closed_count = sum(
                row.get("status") == "market_closed" for row in positions
            )
            awaiting_count = sum(
                row.get("status") in {"awaiting_first_run", "warming_up"}
                for row in positions
            )
            covered_count = len(positions) - failed_count
            delivery_degraded = any(
                row.get("status") == "degraded" for row in market_results
            )
            overall_status = (
                "degraded"
                if failed_count or delivery_degraded
                else "warming_up"
                if awaiting_count
                else "ready"
            )
            overall_reason = (
                "HOLDING_MONITOR_DEGRADED"
                if failed_count or delivery_degraded
                else "MULTI_TIMEFRAME_WARMUP_INCOMPLETE"
                if awaiting_count
                else "READY"
            )
            result = {
                "schema": SCHEMA,
                "observed_at": observed_at.isoformat(),
                **scope_fields,
                "status": overall_status,
                "reason_code": overall_reason,
                "declared_count": len(positions),
                # ``monitored_count`` 现在表示正在主动扫描，而非仅已声明且未失败；闭市市场
                # 仍纳入覆盖并单独报告。
                "monitored_count": active_count,
                "covered_count": covered_count,
                "active_count": active_count,
                "closed_count": closed_count,
                "awaiting_count": awaiting_count,
                "failed_count": failed_count,
                "event_count": sum(
                    int(row.get("event_count", 0)) for row in market_results
                ),
                "sent_count": sum(
                    int(row.get("sent_count", 0)) for row in market_results
                ),
                "expired_count": sum(
                    int(row.get("expired_count", 0)) for row in market_results
                ),
                "health_alert_count": sum(
                    int(row.get("health_alert_count", 0)) for row in market_results
                ),
                "positions": positions,
                "markets": market_results,
                "completed_at": self._now().isoformat(),
                "duration_ms": round((perf_counter() - started) * 1000, 3),
                "notification_delivery": self._notification_delivery_snapshot(),
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }
            return self._publish_result(result)

    def register_job(self, scheduler: object) -> str:
        with self._lock:
            if self._job_registered:
                return JOB_ID
            scheduler.add_job(
                self.run_once,
                trigger=IntervalTrigger(seconds=max(30, self._config.interval_seconds)),
                id=JOB_ID,
                name=JOB_DISPLAY_NAMES[JOB_ID],
                executor="realtime_monitor",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
                next_run_time=self._now()
                + timedelta(seconds=self._config.start_delay_seconds),
            )
            self._scheduler = scheduler
            self._job_registered = True
            return JOB_ID

    def request_refresh(self) -> bool:
        """把一次分组成员变更合并为一次立即执行的调度请求。"""

        with self._lock:
            scheduler = self._scheduler
            registered = self._job_registered
        if not registered or scheduler is None:
            return False
        modify_job = getattr(scheduler, "modify_job", None)
        if not callable(modify_job):
            return False
        try:
            modify_job(JOB_ID, next_run_time=self._now())
            wakeup = getattr(scheduler, "wakeup", None)
            if callable(wakeup):
                wakeup()
            return True
        except Exception as exc:
            self._log.warning(
                "holding monitor immediate refresh request failed: %s", exc
            )
            return False

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            last = None if self._last_result is None else dict(self._last_result)
            notifier_configured = bool(
                self._notifier is not None
                and getattr(self._notifier, "available", True)
            )
            stale_after_seconds = max(180, self._config.interval_seconds * 3)
            last_completed_at = None if last is None else last.get("completed_at")
            stale = False
            if isinstance(last_completed_at, str):
                try:
                    completed = datetime.fromisoformat(last_completed_at)
                    if completed.tzinfo is None or completed.utcoffset() is None:
                        raise ValueError("completed_at must be timezone-aware")
                    stale = (
                        self._now() - completed.astimezone(CN)
                    ).total_seconds() > stale_after_seconds
                except ValueError:
                    stale = True
            if not self._job_registered:
                ready = False
                status = "not_registered"
                reason = "HOLDING_MONITOR_JOB_NOT_REGISTERED"
            elif not notifier_configured:
                ready = False
                status = "not_ready"
                reason = "HOLDING_NOTIFICATION_NOT_CONFIGURED"
            elif last is None:
                ready = False
                status = "awaiting_first_run"
                reason = "HOLDING_MONITOR_AWAITING_FIRST_RUN"
            elif last.get("failed_count"):
                ready = False
                status = "degraded"
                reason = "HOLDING_MONITOR_DEGRADED"
            elif stale:
                ready = False
                status = "stale"
                reason = "HOLDING_MONITOR_STALE"
            elif last.get("awaiting_count"):
                ready = False
                status = "warming_up"
                reason = "MULTI_TIMEFRAME_WARMUP_INCOMPLETE"
            else:
                ready = True
                status = "ready"
                reason = "READY"
            return {
                "schema": SCHEMA,
                "required": True,
                "ready": ready,
                "status": status,
                "reason_code": reason,
                "job_registered": self._job_registered,
                "notification_configured": notifier_configured,
                "interval_seconds": self._config.interval_seconds,
                "max_events_per_notification": self._config.max_events_per_notification,
                "scope_limit": self._config.max_symbols,
                "large_scope_authorized": self._config.large_scope_authorized,
                "scope_authorized": False
                if last is None
                else last.get("scope_authorized", False),
                "requested_count": 0
                if last is None
                else last.get("requested_count", 0),
                "mandatory_count": 0
                if last is None
                else last.get("mandatory_count", 0),
                "deferred_count": 0
                if last is None
                else last.get("deferred_count", 0),
                "op_level": self._config.op_level,
                "mid_level": self._config.mid_level,
                "big_level": self._config.big_level,
                "last_run_at": None if last is None else last.get("observed_at"),
                "last_completed_at": last_completed_at,
                "last_run_duration_ms": (
                    None if last is None else last.get("duration_ms")
                ),
                "stale": stale,
                "stale_after_seconds": stale_after_seconds,
                "declared_count": 0 if last is None else last.get("declared_count", 0),
                "monitored_count": 0
                if last is None
                else last.get("monitored_count", 0),
                "covered_count": 0 if last is None else last.get("covered_count", 0),
                "active_count": 0 if last is None else last.get("active_count", 0),
                "closed_count": 0 if last is None else last.get("closed_count", 0),
                "awaiting_count": 0 if last is None else last.get("awaiting_count", 0),
                "failed_count": 0 if last is None else last.get("failed_count", 0),
                "notification_delivery": self._notification_delivery_snapshot(),
                "positions": [] if last is None else last.get("positions", []),
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }


__all__ = [
    "BoundedEventDeduper",
    "JOB_ID",
    "SCHEMA",
    "HoldingGroupMonitorConfig",
    "HoldingGroupMonitorService",
    "HoldingMonitorRuntimeLedger",
    "build_non_a_monitor_universe",
]
