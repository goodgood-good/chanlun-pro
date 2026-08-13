"""为全市场关注分组提供只读的缠论结构线索。

板块先行决策服务专用于 A 股；本服务为其余市场提供独立的辅助观察通道。A 股持仓
仍只进入 ``TradingScreeningService`` 及其 ``HumanAssistedDecisionCore``。
本服务不会读取账户，也不具备订单通道。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
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

from .job_names import JOB_DISPLAY_NAMES


CN = ZoneInfo("Asia/Shanghai")
SCHEMA = "chanlun-holding-group-monitor"
JOB_ID = "holding_group_realtime_monitor"
DEDUPE_SCHEMA = "chanlun-holding-group-event-deduper"
RUNTIME_SCHEMA = "chanlun-holding-group-runtime-ledger"
_PENDING_NOTIFICATION_MAX_AGE = timedelta(minutes=2)
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


def fresh_monitor_events(events: Iterable[object], deduper: object) -> list[object]:
    """按稳定事件身份完成批内去重，再返回尚未发布的事件。

    持久化去重器仍是唯一的跨批次发布闸门。
    """

    unique: list[object] = []
    identities: set[str] = set()
    for event in events:
        identity = str(getattr(event, "identity"))
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
_POINT_ADVICE = {
    "1buy": "建议：确认反转后考虑分批增持",
    "2buy": "建议：回踩不破后考虑分批增持",
    "3buy": "建议：回抽确认后考虑分批增持",
    "1sell": "建议：优先考虑减仓",
    "2sell": "建议：反弹未转强时考虑继续减仓",
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
        self.records = dict(raw)
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
            return [event for event in events if event.identity not in self.records]

    def mark(self, events) -> None:
        self.mark_identities(event.identity for event in events)

    def mark_identities(self, identities) -> None:
        with self._lock:
            observed_at = self._now().isoformat(timespec="seconds")
            changed = False
            for raw_identity in identities:
                identity = str(raw_identity or "").strip()
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
        self._state = self._load()

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
        for market, raw in state["pending_notifications"].items():
            if (
                not isinstance(market, str)
                or not isinstance(raw, Mapping)
                or set(raw)
                != {
                    "title",
                    "lines",
                    "identities",
                    "codes",
                    "charts",
                    "transition_codes",
                    "queued_at",
                }
                or not isinstance(raw.get("title"), str)
                or not raw.get("title")
                or any(
                    not isinstance(raw.get(key), list)
                    for key in (
                        "lines",
                        "identities",
                        "codes",
                        "charts",
                        "transition_codes",
                    )
                )
                or any(
                    not isinstance(value, str)
                    for key in (
                        "lines",
                        "identities",
                        "codes",
                        "transition_codes",
                    )
                    for value in raw[key]
                )
                or any(not isinstance(value, Mapping) for value in raw["charts"])
            ):
                return empty
            try:
                queued_at = datetime.fromisoformat(str(raw["queued_at"]))
            except (TypeError, ValueError):
                return empty
            if queued_at.tzinfo is None or queued_at.utcoffset() is None:
                return empty
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
                len(value.get("identities", []))
                for value in self._state["pending_notifications"].values()
                if isinstance(value, Mapping)
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
                "last_success_at": self._state["last_success_at"],
                "last_simulated_at": self._state["last_simulated_at"],
                "last_failure_at": self._state["last_failure_at"],
                "last_failure_reason": self._state["last_failure_reason"],
                "last_expired_at": self._state["last_expired_at"],
            }

    def pending_notification(self, market: str) -> dict[str, object] | None:
        with self._lock:
            raw = self._state["pending_notifications"].get(market)
            if not isinstance(raw, Mapping):
                return None
            title = raw.get("title")
            lines = raw.get("lines")
            identities = raw.get("identities")
            codes = raw.get("codes")
            charts = raw.get("charts")
            queued_at = raw.get("queued_at")
            transition_codes = raw.get("transition_codes")
            if not (
                isinstance(title, str)
                and title
                and isinstance(lines, list)
                and all(isinstance(value, str) for value in lines)
                and isinstance(identities, list)
                and all(isinstance(value, str) for value in identities)
                and isinstance(codes, list)
                and all(isinstance(value, str) for value in codes)
                and isinstance(charts, list)
                and all(isinstance(value, Mapping) for value in charts)
                and isinstance(transition_codes, list)
                and all(isinstance(value, str) for value in transition_codes)
                and isinstance(queued_at, str)
                and queued_at
            ):
                return None
            return {
                "title": title,
                "lines": list(lines),
                "identities": list(identities),
                "codes": list(codes),
                "charts": [dict(value) for value in charts],
                "transition_codes": list(transition_codes),
                "queued_at": queued_at,
                "event_count": len(identities),
            }

    def set_pending_notification(
        self,
        market: str,
        payload: Mapping[str, object],
    ) -> None:
        required = {
            "title",
            "lines",
            "identities",
            "codes",
            "charts",
            "transition_codes",
            "queued_at",
            "event_count",
        }
        if set(payload) != required:
            raise ValueError("pending holding notification fields changed")
        if not all(
            isinstance(payload[key], list)
            for key in (
                "lines",
                "identities",
                "codes",
                "charts",
                "transition_codes",
            )
        ) or any(not isinstance(value, Mapping) for value in payload["charts"]):
            raise ValueError("pending holding notification is malformed")
        with self._lock:
            self._state["pending_notifications"][market] = {
                "title": str(payload["title"]),
                "lines": [str(value) for value in payload["lines"]],
                "identities": [str(value) for value in payload["identities"]],
                "codes": [str(value) for value in payload["codes"]],
                "charts": [dict(value) for value in payload["charts"]],
                "transition_codes": [
                    str(value) for value in payload["transition_codes"]
                ],
                "queued_at": str(payload["queued_at"]),
            }
            self._persist()

    def clear_pending_notification(self, market: str) -> None:
        with self._lock:
            if market not in self._state["pending_notifications"]:
                return
            self._state["pending_notifications"].pop(market, None)
            self._persist()

    def prune_pending_notifications(
        self,
        desired: set[tuple[str, str]],
    ) -> None:
        """丢弃已从手工分组移除标的对应的待发通知记录。"""

        with self._lock:
            pending_notifications = self._state["pending_notifications"]
            changed = False
            for market, raw in tuple(pending_notifications.items()):
                if not isinstance(raw, Mapping):
                    pending_notifications.pop(market, None)
                    changed = True
                    continue
                raw_identities = raw.get("identities", [])
                raw_lines = raw.get("lines", [])
                raw_codes = raw.get("codes", [])
                raw_charts = raw.get("charts", [])
                if not all(
                    isinstance(value, list)
                    for value in (raw_identities, raw_lines, raw_codes, raw_charts)
                ):
                    pending_notifications.pop(market, None)
                    changed = True
                    continue
                identities = list(raw_identities)
                lines = list(raw_lines)
                codes = list(raw_codes)
                charts = list(raw_charts)
                keep = [
                    index
                    for index, code in enumerate(codes)
                    if (str(market), str(code)) in desired
                    and index < len(identities)
                    and index < len(lines)
                ]
                if len(keep) == len(codes):
                    continue
                if not keep:
                    pending_notifications.pop(market, None)
                else:
                    retained_codes = [str(codes[index]) for index in keep]
                    transition_codes = set(raw.get("transition_codes", []))
                    pending_notifications[market] = {
                        "title": str(raw.get("title") or "持仓结构雷达"),
                        "lines": [str(lines[index]) for index in keep],
                        "identities": [str(identities[index]) for index in keep],
                        "codes": retained_codes,
                        "charts": [
                            dict(charts[index])
                            for index in keep
                            if index < len(charts)
                            and isinstance(charts[index], Mapping)
                        ],
                        "transition_codes": [
                            code for code in retained_codes if code in transition_codes
                        ],
                        "queued_at": str(raw.get("queued_at") or ""),
                    }
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


def _notification_line(event: object) -> str:
    """把一条持仓提醒渲染为紧凑且无歧义的一行。"""

    side = str(getattr(event, "side", "") or "")
    is_holding = getattr(event, "is_holding", True) is True
    point = str(getattr(event, "bs_type", "") or "")
    point_label = _POINT_LABELS.get(point, "")
    if side == "buy":
        event_label = point_label or (f"买点（{point}）" if point else "买点")
    elif side == "sell":
        event_label = point_label or (f"卖点（{point}）" if point else "卖点")
    elif side == "exit":
        event_label = "转弱风险"
    else:
        event_label = "结构提示"

    op_level = _level_label(getattr(event, "op_level", ""))
    big_level = _level_label(getattr(event, "big_level", ""))
    structure_label = (
        f"{big_level}{event_label}" if side == "exit" else f"{op_level}{event_label}"
    )
    code = str(getattr(event, "code", "") or "")
    name = str(getattr(event, "name", "") or "")
    parts = [f"{code} {name}".strip(), structure_label]
    signal_time = str(getattr(event, "signal_time", "") or "")
    if signal_time:
        parts.append(signal_time)
    try:
        price = float(getattr(event, "price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price:
        parts.append(f"参考价 {price:.3f}")

    big_dir = _DIRECTION_LABELS.get(
        str(getattr(event, "big_dir", "") or ""),
        str(getattr(event, "big_dir", "") or "未知"),
    )
    parts.append(f"{big_level}{big_dir}")
    mid_level = str(getattr(event, "mid_level", "") or "")
    if mid_level:
        mid_dir = _DIRECTION_LABELS.get(
            str(getattr(event, "mid_dir", "") or ""),
            str(getattr(event, "mid_dir", "") or "未知"),
        )
        parts.append(f"{_level_label(mid_level)}{mid_dir}")
    if side == "exit":
        advice = "建议：收紧风险并优先检查退出条件"
    elif point in _POINT_ADVICE:
        advice = _POINT_ADVICE[point]
        if not is_holding:
            advice = advice.replace("增持", "买入")
            if side == "sell":
                advice = "建议：不新开仓；已有仓位时" + advice.removeprefix("建议：")
    elif side == "buy":
        action = "增持" if is_holding else "买入"
        advice = f"建议：人工确认后再考虑{action}"
    elif side == "sell":
        advice = (
            "建议：人工确认后考虑减仓"
            if is_holding
            else "建议：不新开仓；已有仓位时考虑减仓"
        )
    else:
        advice = "建议：人工复核后再操作"
    parts.append(advice)
    return "｜".join(parts)


def _notification_title(market: str, events: Sequence[object]) -> str:
    """单条事件时把准确买卖点直接放进通知预览。"""

    scopes = {
        "持仓股" if getattr(event, "is_holding", True) is True else "关注股"
        for event in events
    }
    scope = next(iter(scopes)) if len(scopes) == 1 else "持仓/关注"
    prefix = f"买卖通知｜{scope}"
    if len(events) != 1:
        return (
            f"{prefix}｜{_MARKET_LABELS.get(market, market.upper())}｜"
            f"{len(events)}条信号"
        )
    event = events[0]
    side = str(getattr(event, "side", "") or "")
    point = str(getattr(event, "bs_type", "") or "")
    point_label = _POINT_LABELS.get(point, "")
    if side == "buy":
        label = point_label or (f"买点（{point}）" if point else "买点")
        level = _level_label(getattr(event, "op_level", ""))
    elif side == "sell":
        label = point_label or (f"卖点（{point}）" if point else "卖点")
        level = _level_label(getattr(event, "op_level", ""))
    elif side == "exit":
        label = "转弱风险"
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
    op_level: str = "1m"
    mid_level: str = "5m"
    big_level: str = "30m"

    def __post_init__(self) -> None:
        for field_name in (
            "interval_seconds",
            "start_delay_seconds",
            "max_workers",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if len({self.op_level, self.mid_level, self.big_level}) != 3:
            raise ValueError("holding monitor levels must be distinct")


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
    """增量观察全部已声明持仓，并发送新事件。"""

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
        self._log = fun.get_logger()

    def _deliver(
        self,
        title: str,
        lines: list[str],
        *,
        event_count: int,
        charts: Sequence[Mapping[str, object]] = (),
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
        return {
            "title": _notification_title(market, events),
            "lines": [_notification_line(event) for event in events],
            "identities": [str(getattr(event, "identity")) for event in events],
            "codes": [str(getattr(event, "code", "")) for event in events],
            "charts": [
                {
                    "market": market,
                    "code": str(getattr(event, "code", "")),
                    "name": str(
                        getattr(event, "name", "") or getattr(event, "code", "")
                    ),
                    "artifact_key": str(getattr(event, "identity")),
                    "observed_at": str(getattr(event, "signal_time", "")),
                    "point_type": str(getattr(event, "bs_type", "")),
                    "signal_time": str(getattr(event, "signal_time", "")),
                    "evidence_id": str(getattr(event, "evidence_id", "")),
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
                if str(getattr(event, "kind", "")) == "big_down_exit"
            ],
            "queued_at": self._now().isoformat(),
            "event_count": len(events),
        }

    @staticmethod
    def _merge_pending_notification(
        pending: Mapping[str, object],
        additional: Mapping[str, object],
    ) -> dict[str, object]:
        identities = [str(value) for value in pending["identities"]]
        lines = [str(value) for value in pending["lines"]]
        codes = [str(value) for value in pending["codes"]]
        transitions = [str(value) for value in pending["transition_codes"]]
        charts = [dict(value) for value in pending["charts"]]
        seen = set(identities)
        extra_identities = list(additional["identities"])
        extra_lines = list(additional["lines"])
        extra_codes = list(additional["codes"])
        extra_charts = list(additional["charts"])
        extra_transitions = set(additional["transition_codes"])
        for index, (identity, line, code) in enumerate(
            zip(extra_identities, extra_lines, extra_codes)
        ):
            identity = str(identity)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            identities.append(identity)
            lines.append(str(line))
            codes.append(str(code))
            if code in extra_transitions:
                transitions.append(str(code))
            if index < len(extra_charts) and isinstance(extra_charts[index], Mapping):
                charts.append(dict(extra_charts[index]))
        return {
            "title": str(pending["title"]),
            "lines": lines,
            "identities": identities,
            "codes": codes,
            "charts": charts,
            "transition_codes": list(dict.fromkeys(transitions)),
            "queued_at": str(pending["queued_at"]),
            "event_count": len(identities),
        }

    def _pending_notification_expired(
        self,
        pending: Mapping[str, object],
    ) -> bool:
        try:
            queued_at = datetime.fromisoformat(str(pending["queued_at"]))
        except (KeyError, TypeError, ValueError):
            return True
        if queued_at.tzinfo is None or queued_at.utcoffset() is None:
            return True
        age = self._now() - queued_at.astimezone(CN)
        return age < timedelta(0) or age > _PENDING_NOTIFICATION_MAX_AGE

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
                "sent_count": 0,
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
                "sent_count": 0,
            }
        if not is_open:
            return {
                "market": market,
                "status": "not_due",
                "reason_code": "MARKET_CLOSED",
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
                "sent_count": 0,
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
            for event in events:
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
                "sent_count": 0,
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
        current_directions: dict[str, str] = {}
        for code, state in states.items():
            # 部分预热不具权威性；若持久化其高级别方向，会让首个完全有效的向下转变看似旧状态，
            # 从而抑制对应持仓退出通知。
            if code in failed_codes or code in warming_codes:
                continue
            try:
                direction = str(state.big_dir() or "neutral")
            except Exception:
                direction = "neutral"
            current_directions[code] = direction

        # 严格收集器在方向持续向下时会对每根已完成 30m K 线产生大级别向下退出。持仓提醒
        # 必须表达状态转变，而非重复播报同一状态；上一方向会持久化，应用重启不会重复提醒
        # 未变化的下行段。
        events = [
            event
            for event in events
            if not (
                str(getattr(event, "kind", "")) == "big_down_exit"
                and self._runtime_ledger.previous_direction(
                    market, str(getattr(event, "code", ""))
                )
                == "down"
            )
        ]
        deduper = self._deduper(market)
        sent_count = 0
        notification_failed = False
        failed_delivery_codes: set[str] = set()
        failed_transition_codes: set[str] = set()

        # 处理新观测事件前先重试持久化发件箱。``StrictPhysicalMonitorState`` 对结构点只发出
        # 一次，HTTP 失败不能寄希望于收集器重复发出。
        pending = self._runtime_ledger.pending_notification(market)
        if pending is not None and self._pending_notification_expired(pending):
            # 未能及时投递的交易线索已不可操作，应把该次出现标为已消费，避免重复状态事件
            # （如仍向下的 30m 方向）在本周期稍后复活同一过期提醒。
            deduper.mark_identities(pending["identities"])
            self._runtime_ledger.record_expired(int(pending["event_count"]))
            self._runtime_ledger.clear_pending_notification(market)
            pending = None
        if pending is not None:
            delivered = self._deliver(
                str(pending["title"]),
                list(pending["lines"]),
                event_count=int(pending["event_count"]),
                charts=tuple(pending.get("charts", ())),
            )
            if delivered:
                deduper.mark_identities(pending["identities"])
                self._runtime_ledger.clear_pending_notification(market)
                sent_count += int(pending["event_count"])
                pending = None
            else:
                notification_failed = True
                failed_delivery_codes.update(pending["codes"])
                failed_transition_codes.update(pending["transition_codes"])

        fresh = fresh_monitor_events(events, deduper)
        if pending is not None:
            pending_identities = set(pending["identities"])
            additional_events = [
                event
                for event in fresh
                if str(getattr(event, "identity")) not in pending_identities
            ]
            if additional_events:
                pending = self._merge_pending_notification(
                    pending,
                    self._event_notification_payload(market, additional_events),
                )
                self._runtime_ledger.set_pending_notification(market, pending)
                failed_delivery_codes.update(pending["codes"])
                failed_transition_codes.update(pending["transition_codes"])
        elif fresh:
            payload = self._event_notification_payload(market, fresh)
            delivered = self._deliver(
                str(payload["title"]),
                list(payload["lines"]),
                event_count=int(payload["event_count"]),
                charts=tuple(payload.get("charts", ())),
            )
            if delivered:
                deduper.mark(fresh)
                sent_count += len(fresh)
            else:
                notification_failed = True
                failed_delivery_codes.update(payload["codes"])
                failed_transition_codes.update(payload["transition_codes"])
                self._runtime_ledger.set_pending_notification(market, payload)

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
                result = {
                    "schema": SCHEMA,
                    "observed_at": observed_at.isoformat(),
                    "status": "error",
                    "reason_code": "HOLDING_GROUP_UNAVAILABLE",
                    "declared_count": 0,
                    "monitored_count": 0,
                    "covered_count": 0,
                    "active_count": 0,
                    "closed_count": 0,
                    "awaiting_count": 0,
                    "failed_count": 1,
                    "event_count": 0,
                    "sent_count": 0,
                    "positions": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "completed_at": self._now().isoformat(),
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                }
                return self._publish_result(result)

            grouped: dict[str, list[dict[str, object]]] = {}
            for row in valid:
                grouped.setdefault(row["market"], []).append(row)
            desired = {(row["market"], row["code"]) for row in valid}
            self._runtime_ledger.prune_pending_notifications(desired)
            for identity in tuple(self._states):
                if identity not in desired:
                    self._states.pop(identity, None)

            market_results: list[dict[str, object]] = []
            if grouped:
                worker_count = min(self._config.max_workers, len(grouped))
                if worker_count == 1:
                    market_results = [
                        self._run_market(market, rows, observed_at)
                        for market, rows in sorted(grouped.items())
                    ]
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
                        market_results = [future.result() for future in futures]

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
            overall_status = (
                "degraded"
                if failed_count
                else "warming_up"
                if awaiting_count
                else "ready"
            )
            overall_reason = (
                "HOLDING_MONITOR_DEGRADED"
                if failed_count
                else "MULTI_TIMEFRAME_WARMUP_INCOMPLETE"
                if awaiting_count
                else "READY"
            )
            result = {
                "schema": SCHEMA,
                "observed_at": observed_at.isoformat(),
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
