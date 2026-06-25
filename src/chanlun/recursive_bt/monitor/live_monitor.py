"""Realtime alerting for the recursive Chanlun paper/live strategy.

Run:
    PYTHONPATH="src;web/chanlun_chart;." python -u -m chanlun.recursive_bt.monitor.live_monitor

The monitor reuses ``paper.SymbolState`` so buy/sell point detection stays on
the same path as the paper/live trading loop. DingTalk delivery is delegated to
the Claude Code Notification hook command.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

import pandas as pd

from chanlun.market import Market
from chanlun import config as app_config
from chanlun import fun
from chanlun.core.cl import CL
from chanlun.exchange import get_exchange
from chanlun.notifications import ClaudeHookNotifier, DingTalkWebhookNotifier
from chanlun.recursive_bt.select.chanlun_selector import ASelectionConfig, OriginalChanlunASelector
from chanlun.recursive_bt.engine.engine import (
    CL_CFG,
    buy_class,
    collect_branch_signals,
    latest_prev_day_close,
    recommended_buy_ratio,
    recommended_sell_ratio,
)
from chanlun.recursive_bt.sim.paper import (
    BT_DATA, MAX_POS, PaperBroker, drop_unclosed_last_bar, in_session,
)
from chanlun.recursive_bt.engine.market_runtime import (
    INDEX_BY_MARKET,
    classify_visible_regime,
    default_ledger_path,
    default_monitor_state_path,
    load_chart_cache_klines,
    list_chart_cache_codes,
    merge_codes,
    normalize_code,
    parse_regime_ratio_multipliers,
    normalize_market,
    parse_codes,
)

INDEX_CODE = "SH.000001"
RUNTIME_OVERRIDE_AUDIT_JSONL = (
    "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl"
)
RUNTIME_OVERRIDE_KEYS = {
    "max_pos",
    "op_level",
    "mid_level",
    "big_level",
    "mid_gate",
    "regime_mode",
    "nest_mode",
    "trend_3boost",
    "sell_scope",
    "enable_selection_pool",
    "regime_ratio_multipliers",
    "regime_source_code",
    "regime_lookback_days",
    "selection_require_three_systems",
    "selection_max_codes",
}


def apply_bs_point_ratio_multiplier(
    ratio: float,
    bs_type: str,
    multipliers: Mapping[str, float] | None = None,
) -> tuple[float, float]:
    cls = str(buy_class(bs_type) or "")
    if not cls or not multipliers:
        return round(float(ratio or 0.0), 4), 1.0
    try:
        multiplier = float(multipliers.get(cls, 1.0) or 1.0)
    except Exception:
        multiplier = 1.0
    multiplier = min(max(multiplier, 0.0), 2.0)
    adjusted = min(max(float(ratio or 0.0) * multiplier, 0.0), 1.0)
    return round(adjusted, 4), multiplier


def _monitor_market_config(
    market: str,
    runtime_overrides_enabled: bool | None = None,
    runtime_overrides_path: str | None = None,
) -> dict:
    root = getattr(app_config, "RECURSIVE_MONITOR_CONFIG", {})
    if not isinstance(root, dict):
        return {}
    common = root.get("common") or {}
    specific = root.get(normalize_market(market)) or {}
    settings = dict(common if isinstance(common, dict) else {})
    if isinstance(specific, dict):
        settings.update(specific)
    if runtime_overrides_enabled is not None:
        settings["runtime_overrides_enabled"] = runtime_overrides_enabled
    if runtime_overrides_path:
        settings["optimization_runtime_overrides_json"] = runtime_overrides_path
    return _apply_runtime_overrides(settings, market)


def _apply_runtime_overrides(settings: dict, market: str) -> dict:
    out = dict(settings)
    if not _config_bool(settings.get("runtime_overrides_enabled"), True):
        return _apply_bs_point_ratio_overrides(out, market)
    path = str(
        settings.get("optimization_runtime_overrides_json")
        or "D:/chanlun_pro/reports/strategy_runtime_overrides.json"
    )
    try:
        from chanlun.recursive_bt.strategy_optimizer import runtime_override_entry_for_market

        entry = runtime_override_entry_for_market(path, normalize_market(market))
    except Exception as exc:
        fun.get_logger().warning(f"[live_monitor] load runtime override failed: {exc}")
        return _apply_bs_point_ratio_overrides(out, market)
    override = dict((entry or {}).get("monitor_config") or {})
    if not override:
        return _apply_bs_point_ratio_overrides(out, market)
    applied_config = {}
    for key, value in override.items():
        if key in RUNTIME_OVERRIDE_KEYS:
            out[key] = value
            applied_config[key] = value
    out["_runtime_override_path"] = path
    event = record_runtime_override_application(
        settings,
        normalize_market(market),
        override_path=path,
        override_entry=entry,
        applied_config=applied_config,
    )
    if event:
        out["_runtime_override_event"] = event
    return _apply_bs_point_ratio_overrides(out, market)


def _apply_bs_point_ratio_overrides(settings: dict, market: str) -> dict:
    out = dict(settings)
    try:
        from chanlun.recursive_bt.strategy_optimizer import (
            bs_point_ratio_multipliers_for_market,
        )

        ratio_path = str(
            out.get("bs_point_ratio_overrides_json")
            or "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
        )
        multipliers = bs_point_ratio_multipliers_for_market(ratio_path, normalize_market(market))
        if multipliers:
            out["bs_point_ratio_multipliers"] = multipliers
    except Exception as exc:
        fun.get_logger().warning(f"[live_monitor] load bs point ratio overrides failed: {exc}")
    return out


def record_runtime_override_application(
    settings: dict,
    market: str,
    *,
    override_path: str,
    override_entry: dict,
    applied_config: dict,
    now: str | None = None,
) -> dict | None:
    if not applied_config:
        return None
    audit_path = str(
        settings.get("runtime_override_audit_jsonl") or RUNTIME_OVERRIDE_AUDIT_JSONL
    )
    decision_key = str(
        override_entry.get("decision_key")
        or f"{market}|{override_entry.get('action', '')}|{override_entry.get('target_candidate', '')}"
    )
    event_key = f"{market}|{decision_key}"
    if _audit_log_has_event(audit_path, event_key):
        return None
    event = {
        "time": now or _dt.datetime.now().isoformat(timespec="seconds"),
        "event_key": event_key,
        "event": "runtime_override_applied",
        "market": market,
        "action": str(override_entry.get("action") or ""),
        "risk_state": str(override_entry.get("risk_state") or ""),
        "target_candidate": str(override_entry.get("target_candidate") or ""),
        "decision_key": decision_key,
        "confirmations": int(override_entry.get("confirmations") or 0),
        "confirmation_threshold": int(override_entry.get("confirmation_threshold") or 0),
        "reason": str(override_entry.get("reason") or ""),
        "override_path": override_path,
        "applied_config": applied_config,
    }
    _append_audit_event(audit_path, event)
    return event


def _audit_log_has_event(path: str, event_key: str) -> bool:
    audit = Path(path)
    if not audit.exists():
        return False
    try:
        for line in audit.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("event_key") == event_key:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _append_audit_event(path: str, event: dict) -> None:
    audit = Path(path)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _config_int(value, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_buy_classes(value, default=(3, 2, 1)) -> tuple[int, ...]:
    if value is None:
        return tuple(default)
    raw = value.replace(";", ",").split(",") if isinstance(value, str) else value
    out: list[int] = []
    for item in raw:
        try:
            cls = int(str(item).strip()[0])
        except Exception:
            continue
        if cls in (1, 2, 3) and cls not in out:
            out.append(cls)
    return tuple(out) or tuple(default)


def _resolve_monitor_max_pos(requested_max_pos: int | None, universe_size: int) -> int:
    if requested_max_pos is not None and requested_max_pos > 0:
        return int(requested_max_pos)
    if universe_size > 0:
        return max(1, min(MAX_POS, int(universe_size)))
    return MAX_POS


@dataclass
class MonitorEvent:
    code: str
    name: str
    side: str
    kind: str
    bs_type: str
    signal_time: str
    price: float
    big_dir: str
    reason: str
    buy_ratio: float = 0.0
    sell_ratio: float = 0.0
    op_level: str = "5m"
    big_level: str = "30m"
    mid_level: str = ""
    mid_dir: str = ""

    @property
    def identity(self) -> str:
        # 去重身份只用信号的稳定部分(kind|code|bs_type|signal_time)。
        # reason 含易变片段(nest depth/比例乘数/regime),纳入会让同一买卖点在
        # 两轮间因乘数/regime 变化而 identity 漂移→重复发、重复入单。
        return "|".join(
            [
                self.kind,
                self.code,
                self.bs_type or "-",
                self.signal_time or "-",
            ]
        )

    def line(self) -> str:
        side_name = {"buy": "买点", "sell": "卖点", "exit": "退出"}.get(
            self.side, self.side
        )
        price = f"{self.price:.3f}" if self.price else "-"
        bs = f" {self.bs_type}" if self.bs_type else ""
        levels = f"{self.big_level}={self.big_dir}"
        if self.mid_level and self.mid_dir:
            levels = f"{levels} {self.mid_level}={self.mid_dir}"
        ratio = ""
        if self.side == "buy":
            ratio = f" 建议买入={_format_ratio(self.buy_ratio)}"
        elif self.side in {"sell", "exit"}:
            ratio = f" 建议卖出={_format_ratio(self.sell_ratio)}"
        return (
            f"[{side_name}{bs}] {self.code} {self.name} "
            f"time={self.signal_time} price={price} {levels}{ratio} "
            f"{self.reason}"
        )


class JsonDeduper:
    """Persistent identity de-duplication for DingTalk alerts."""

    def __init__(self, path: os.PathLike | str) -> None:
        self.path = Path(path)
        self.records: dict[str, str] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.records = {
                        str(k): str(v) for k, v in data.get("records", data).items()
                    }
            except Exception as exc:
                fun.get_logger().warning(f"[live_monitor] load state failed: {exc}")

    def unseen(self, events: Iterable[MonitorEvent]) -> List[MonitorEvent]:
        return [event for event in events if event.identity not in self.records]

    def mark(self, events: Iterable[MonitorEvent]) -> None:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        changed = False
        for event in events:
            if event.identity not in self.records:
                self.records[event.identity] = now
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"records": self.records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


def load_ledger_positions(ledger_path: os.PathLike | str) -> set[str]:
    path = Path(ledger_path)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fun.get_logger().warning(f"[live_monitor] read ledger failed: {exc}")
        return set()
    positions = data.get("positions") or {}
    return {str(code) for code in positions.keys()}


def load_universe(
    market: str = "a",
    bt_data: os.PathLike | str = BT_DATA,
    pool_size: int = MAX_POS * 5,
    explicit_codes: Optional[List[str]] = None,
    extra_codes: Optional[Iterable[str]] = None,
    chart_cache_dir: os.PathLike | str = "D:/chanlun_pro/chart_cache",
) -> List[str]:
    market = normalize_market(market)
    if explicit_codes:
        codes = [normalize_code(market, code) for code in explicit_codes]
    elif market == "us":
        codes = list_chart_cache_codes(market, chart_cache_dir)
        if not codes:
            codes = ["QQQ.US", "TSLA.US"]
        if pool_size > 0:
            codes = codes[:pool_size]
    else:
        files = glob.glob(f"{bt_data}/*.pkl")
        codes = sorted(
            os.path.basename(file)[:-4]
            for file in files
            if os.path.basename(file)[:-4] != INDEX_CODE
        )
        if pool_size > 0:
            codes = codes[:pool_size]
    return [
        code
        for code in merge_codes(codes, extra_codes or (), market)
        if code != INDEX_CODE
    ]


def _signal_time(sig) -> str:
    return str(getattr(sig, "date", "") or "")


def _signal_price(sig, fallback: float) -> float:
    try:
        return float(getattr(sig, "price", 0) or fallback or 0)
    except Exception:
        return float(fallback or 0)


def _format_ratio(value: float) -> str:
    try:
        ratio = float(value)
    except Exception:
        return "-"
    if ratio <= 0:
        return "-"
    return f"{ratio * 100:.1f}%"


def _state_daily_resonance(state: object) -> bool:
    in_d3 = getattr(state, "in_d3", None)
    if not callable(in_d3):
        return False
    try:
        return bool(in_d3())
    except Exception:
        return False


def _state_level(state: object, attr: str, default: str = "") -> str:
    return str(getattr(state, attr, default) or default)


def _state_mid_dir(state: object) -> str:
    mid_dir = getattr(state, "mid_dir", None)
    if not callable(mid_dir):
        return ""
    try:
        return str(mid_dir() or "")
    except Exception:
        return ""


def _nest_signal_ok(sig) -> bool:
    cls = buy_class(getattr(sig, "bs_type", ""))
    if cls in (1, 2):
        return getattr(sig, "nest_operable", None) is True
    return True


def _state_last_time(state: object) -> str:
    for attr in ("last_big", "last30", "last_op", "last5"):
        value = getattr(state, attr, None)
        if value is not None:
            return str(value)
    return ""


def collect_monitor_events(
    states: Dict[str, object],
    names: Optional[dict[str, str]] = None,
    holdings: Optional[set[str]] = None,
    max_pos: int = MAX_POS,
    sell_scope: str = "all",
    regime_mode: str = "off",
    mid_gate: str = "strict",
    require_nest: bool = False,
    nest_mode: str = "off",
    trend_3boost: bool = False,
    bs_point_ratio_multipliers: Mapping[str, float] | None = None,
    regime_ratio_multipliers: Mapping[str, Mapping[str, float]] | None = None,
    current_regime: str = "",
) -> List[MonitorEvent]:
    """Refresh states once and return strategy-compatible alert events."""
    if require_nest:
        nest_mode = "filter"
    holdings = holdings or set()
    names = names or {}
    sigs: dict[str, list] = {}
    log = fun.get_logger()
    REFRESH_FAIL_ALERT_THRESHOLD = 3
    for code, state in states.items():
        try:
            sigs[code] = state.refresh()
            if getattr(state, "consecutive_refresh_failures", 0):
                state.consecutive_refresh_failures = 0   # 恢复即清零
        except Exception as exc:
            sigs[code] = []
            n = getattr(state, "consecutive_refresh_failures", 0) + 1
            try:
                state.consecutive_refresh_failures = n
            except Exception:
                n = 1
            # 首次失败 warning;到阈值时升级为一次性醒目告警(避免刷屏:仅在跨过阈值那一刻喊)
            if n == REFRESH_FAIL_ALERT_THRESHOLD:
                log.warning(
                    f"[live_monitor][ALERT] code={code} 已连续 {n} 轮 refresh 失败,"
                    f"该标的信号可能持续漏发: {exc}"
                )
            else:
                log.warning(f"[live_monitor] refresh failed code={code}: {exc}")

    buys: list[MonitorEvent] = []
    others: list[MonitorEvent] = []
    for code, signals in sigs.items():
        state = states[code]
        name = names.get(code, code)
        big_dir = state.big_dir()
        mid_dir = _state_mid_dir(state)
        op_level = _state_level(state, "op_level", "5m")
        big_level = _state_level(state, "big_level", "30m")
        mid_level = _state_level(state, "mid_level", "")
        for sig in signals:
            bs_type = str(getattr(sig, "bs_type", ""))
            if getattr(sig, "is_buy", False):
                cls = buy_class(bs_type)
                if nest_mode == "filter" and not _nest_signal_ok(sig):
                    continue
                if big_dir == "down":
                    continue
                mid_relaxed = False
                mid_soft = False
                if mid_dir == "down":
                    mid_soft = mid_gate == "soft"
                    mid_relaxed = (
                        regime_mode == "adaptive"
                        and mid_gate == "bull_relaxed"
                        and big_dir == "up"
                        and cls == 3
                    )
                if mid_dir == "down" and not (mid_relaxed or mid_soft):
                    continue
                daily_resonance = _state_daily_resonance(state)
                reason = f"{op_level} buy point + {big_level} not_down"
                if mid_level:
                    if mid_soft:
                        reason += f" + {mid_level} soft_down_discount"
                    else:
                        reason += (
                            f" + {mid_level} relaxed_by_{big_level}_up"
                            if mid_relaxed else f" + {mid_level} not_down"
                        )
                if nest_mode in {"filter", "soft"} and cls in (1, 2):
                    reason += f" + interval_nest(depth={getattr(sig, 'nest_depth', 0) or 0})"
                buy_ratio = recommended_buy_ratio(
                    bs_type,
                    max_pos=max_pos,
                    big_dir=big_dir,
                    daily_resonance=daily_resonance,
                    regime_mode=regime_mode,
                    mid_dir=mid_dir if mid_relaxed else "",
                    nest_mode=nest_mode,
                    nest_operable=getattr(sig, "nest_operable", None),
                    nest_depth=int(getattr(sig, "nest_depth", 0) or 0),
                    trend_boost=trend_3boost,
                )
                if mid_soft:
                    buy_ratio = round(buy_ratio * 0.5, 4)
                buy_ratio, applied_multiplier = apply_bs_point_ratio_multiplier(
                    buy_ratio,
                    bs_type,
                    bs_point_ratio_multipliers,
                )
                if applied_multiplier != 1.0:
                    reason += f" + bs{cls}_ratio_x{applied_multiplier:.2f}"
                if regime_ratio_multipliers and current_regime:
                    buy_ratio, regime_multiplier = apply_bs_point_ratio_multiplier(
                        buy_ratio,
                        bs_type,
                        regime_ratio_multipliers.get(current_regime),
                    )
                    if regime_multiplier != 1.0:
                        reason += f" + regime_{current_regime}_x{regime_multiplier:.2f}"
                buys.append(
                    MonitorEvent(
                        code=code,
                        name=name,
                        side="buy",
                        kind="small_buy",
                        bs_type=bs_type,
                        signal_time=_signal_time(sig),
                        price=_signal_price(sig, state.last_px),
                        big_dir=big_dir,
                        reason=reason,
                        buy_ratio=buy_ratio,
                        op_level=op_level,
                        big_level=big_level,
                        mid_level=mid_level,
                        mid_dir=mid_dir,
                    )
                )
            elif getattr(sig, "is_sell", False):
                if sell_scope == "positions" and code not in holdings:
                    continue
                sell_ratio = recommended_sell_ratio(bs_type, big_dir=big_dir)
                others.append(
                    MonitorEvent(
                        code=code,
                        name=name,
                        side="sell",
                        kind="small_sell",
                        bs_type=bs_type,
                        signal_time=_signal_time(sig),
                        price=_signal_price(sig, state.last_px),
                        big_dir=big_dir,
                        reason=f"{op_level} sell point",
                        sell_ratio=sell_ratio,
                        op_level=op_level,
                        big_level=big_level,
                        mid_level=mid_level,
                        mid_dir=mid_dir,
                    )
                )

    if max_pos > 0:
        free = max(max_pos - len(holdings), 0)
        buys.sort(key=lambda event: (-buy_class(event.bs_type), event.code))
        buys = buys[:free]

    for code in sorted(holdings):
        state = states.get(code)
        if state is None:
            continue
        big_dir = state.big_dir()
        mid_dir = _state_mid_dir(state)
        op_level = _state_level(state, "op_level", "5m")
        big_level = _state_level(state, "big_level", "30m")
        mid_level = _state_level(state, "mid_level", "")
        if big_dir != "down":
            continue
        others.append(
            MonitorEvent(
                code=code,
                name=names.get(code, code),
                side="exit",
                kind="big_down_exit",
                bs_type="",
                signal_time=_state_last_time(state),
                price=float(state.last_px or 0),
                big_dir=big_dir,
                reason=f"holding exit: {big_level} turned down",
                sell_ratio=recommended_sell_ratio("", big_dir=big_dir),
                op_level=op_level,
                big_level=big_level,
                mid_level=mid_level,
                mid_dir=mid_dir,
            )
        )

    return buys + others


def send_events(
    events: List[MonitorEvent],
    notifier: ClaudeHookNotifier,
    deduper: JsonDeduper,
    title: str,
) -> int:
    fresh = fresh_monitor_events(events, deduper)
    if not fresh:
        return 0
    lines = [event.line() for event in fresh]
    ok = notifier.send(title, lines)
    if ok:
        deduper.mark(fresh)
        return len(fresh)
    return 0


def unique_monitor_events(events: Iterable[MonitorEvent]) -> list[MonitorEvent]:
    unique: list[MonitorEvent] = []
    seen: set[str] = set()
    for event in events:
        if event.identity in seen:
            continue
        seen.add(event.identity)
        unique.append(event)
    return unique


def fresh_monitor_events(
    events: Iterable[MonitorEvent],
    deduper: JsonDeduper,
) -> list[MonitorEvent]:
    return deduper.unseen(unique_monitor_events(events))


def rescan_selection_pool(selector, states: Dict[str, object], names: dict, ex, args,
                          initial_codes=None) -> int:
    """每日重选:selector(静态 bt_data 缓存口径)重新选池——日级选股+1m 实时择时。
    新候选创建 state 并 warmup(现有信号只登记不触发);不在新结果且非持仓、
    非初始自选的旧候选移出,防止池子无界增长。返回新增标的数。"""
    log = fun.get_logger()
    try:
        fresh = [c.code for c in selector.select()]
    except Exception as exc:
        log.warning(f"[live_monitor] selection rescan failed: {exc}")
        return 0
    holdings = load_ledger_positions(getattr(args, "ledger", ""))
    keep = set(initial_codes or ()) | set(holdings)
    target = [
        code
        for code in merge_codes(fresh, holdings, args.market)
        if code != INDEX_CODE
    ]
    target_set = set(target) | keep
    added = 0
    for code in target:
        if code in states:
            continue
        try:
            state = MonitorSymbolState(
                code,
                ex,
                op_level=args.op_level,
                big_level=args.big_level,
                mid_level=args.mid_level,
            )
            state.refresh()
            states[code] = state
            added += 1
        except Exception as exc:
            log.warning(f"[live_monitor] rescan add failed code={code}: {exc}")
            continue
        if code not in names:
            try:
                info = ex.stock_info(code)
            except Exception:
                info = None
            names[code] = (info or {}).get("name") or code
    for code in list(states):
        if code not in target_set:
            states.pop(code, None)
    if added or len(states) != len(target_set):
        log.info(
            f"[live_monitor] selection rescan: pool={len(states)} added={added}"
        )
    return added


def warmup_states(states: Dict[str, object]) -> None:
    log = fun.get_logger()
    for code, state in states.items():
        try:
            state.refresh()
        except Exception as exc:
            log.warning(f"[live_monitor] warmup failed code={code}: {exc}")


def _next_level_seconds(now: _dt.datetime, level: str) -> float:
    minutes = 5
    if str(level).endswith("m"):
        try:
            minutes = max(int(str(level)[:-1]), 1)
        except ValueError:
            minutes = 5
    nxt = (
        now.replace(second=5, microsecond=0)
        + _dt.timedelta(minutes=minutes - now.minute % minutes)
    )
    return max((nxt - _dt.datetime.now()).total_seconds(), 10)


class ChartCacheExchange:
    """Small exchange adapter for monitor smoke tests and offline evaluation."""

    def __init__(self, market: str, cache_dir: str) -> None:
        self.market = normalize_market(market)
        self.cache_dir = cache_dir

    def klines(self, code: str, frequency: str, *_, **__):
        return load_chart_cache_klines(self.market, code, frequency, self.cache_dir)

    def now_trading(self) -> bool:
        return True

    def stock_info(self, code: str):
        return {"code": code, "name": code}


class MonitorSymbolState:
    """Incremental Chanlun state for configurable live monitor levels."""

    def __init__(
        self,
        code: str,
        ex,
        op_level: str = "5m",
        big_level: str = "30m",
        mid_level: Optional[str] = None,
    ) -> None:
        self.code = code
        self.ex = ex
        self.op_level = op_level
        self.big_level = big_level
        self.mid_level = mid_level or ""
        self.cd_op = CL(code, op_level, dict(CL_CFG))
        self.cd_big = CL(code, big_level, dict(CL_CFG))
        self.cd_mid = CL(code, self.mid_level, dict(CL_CFG)) if self.mid_level else None
        self.cdd = CL(code, "d", dict(CL_CFG))
        self.last_op = None
        self.last_big = None
        self.last_mid = None
        self.last5 = None
        self.last30 = None
        self.lastd = None
        self.d3_until = None
        self.last_open = 0.0
        self.last_px = 0.0
        self.prev_close = 0.0
        self.seen = set()
        self.consecutive_refresh_failures = 0
        # 新鲜窗口=40个op bar(下限60min):覆盖买卖点右侧确认的滞后窗口(含1m偶发
        # >30min的深度确认),超窗才视为深度回溯修正不发。1m级别旧30min窗偏紧会吞真新信号。
        op_minutes = int(op_level[:-1]) if str(op_level).endswith("m") else 5
        self.signal_freshness = pd.Timedelta(minutes=max(op_minutes * 40, 60))

    # 首轮 warmup 限窗(天):监控信号只需笔级买点+笔方向,限窗可大幅缩短初始化时间。
    WARMUP_DAYS_BY_FREQ = {"1m": 30, "5m": 120}

    def _fetch_klines(self, frequency: str, last):
        """首轮按级别限窗 warmup;有锚点后只拉尾部窗口(锚点回退 5 天覆盖节假日/停牌缓冲)。"""
        if last is None:
            warmup_days = self.WARMUP_DAYS_BY_FREQ.get(str(frequency))
            if warmup_days:
                start = (
                    pd.Timestamp.now() - pd.Timedelta(days=warmup_days)
                ).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    return self.ex.klines(self.code, frequency, start_date=start)
                except TypeError:
                    pass
            return self.ex.klines(self.code, frequency)
        start = (last - pd.Timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            return self.ex.klines(self.code, frequency, start_date=start)
        except TypeError:
            return self.ex.klines(self.code, frequency)

    def _process_level(self, cd: CL, frequency: str, last_attr: str, min_bars: int):
        last = getattr(self, last_attr)
        df = drop_unclosed_last_bar(self._fetch_klines(frequency, last), frequency)
        if df is None or len(df) == 0:
            return None
        # min_bars 健全性检查只对首轮全量生效;增量尾窗本来就短,不适用
        if last is None and len(df) < min_bars:
            return None
        new = df if last is None else df[df["date"] > last]
        if len(new):
            cd.process_klines(new.reset_index(drop=True))
            ts = df["date"].iloc[-1]
            setattr(self, last_attr, ts)
            if frequency == "5m":
                self.last5 = ts
            elif frequency == "30m":
                self.last30 = ts
        return df

    def refresh(self) -> List:
        df_op = self._process_level(self.cd_op, self.op_level, "last_op", 100)
        if df_op is None:
            return []
        self._process_level(self.cd_big, self.big_level, "last_big", 50)
        if self.cd_mid is not None:
            self._process_level(self.cd_mid, self.mid_level, "last_mid", 50)
        self._refresh_daily_window()
        self.last_open = float(df_op["open"].iloc[-1])
        self.last_px = float(df_op["close"].iloc[-1])
        # 涨跌停基准=前一交易日收盘(窗口内无昨日数据时为 0=不判),
        # 不能用前一根分钟 bar 收盘——日内累计涨停会被漏判。
        self.prev_close = latest_prev_day_close(df_op)
        out = []
        for sig in collect_branch_signals(self.cd_op, use_xd=False, annotate_nest=True):
            key = (sig.date, sig.bs_type)
            if key in self.seen:
                continue
            self.seen.add(key)
            if self.last_op is None:
                continue
            # 新鲜判定=本轮新出现且确认bar在新鲜窗口内;分型/笔需右侧结构确认,
            # 信号date与last_op之间存在自然滞后,用时间窗口而非零滞后判断新信号。
            if self.last_op - sig.date <= self.signal_freshness:
                out.append(sig)
        return out

    def _refresh_daily_window(self) -> None:
        df = self._fetch_klines("d", self.lastd)
        if df is None or len(df) == 0:
            return
        if self.lastd is None and len(df) < 100:
            return
        new = df if self.lastd is None else df[df["date"] > self.lastd]
        if not len(new):
            return
        self.cdd.process_klines(new.reset_index(drop=True))
        self.lastd = df["date"].iloc[-1]
        for sig in collect_branch_signals(self.cdd, use_xd=False):
            if sig.bs_type == "3buy":
                until = sig.date + pd.Timedelta(days=11)
                if self.d3_until is None or until > self.d3_until:
                    self.d3_until = until

    def big_dir(self) -> str:
        return self._dir_from_cd(self.cd_big)

    def mid_dir(self) -> str:
        if self.cd_mid is None:
            return ""
        return self._dir_from_cd(self.cd_mid)

    def _dir_from_cd(self, cd: CL) -> str:
        bis = list(cd.get_bis())
        if not bis:
            return "neutral"
        return "up" if bis[-1].type == "up" else "down"

    def in_d3(self) -> bool:
        if self.d3_until is None or self.last_op is None:
            return False
        return self.last_op <= self.d3_until


def build_states(
    codes: Iterable[str],
    market: str = "a",
    data_source: str = "online",
    chart_cache_dir: str = "D:/chanlun_pro/chart_cache",
    op_level: str = "5m",
    big_level: str = "30m",
    mid_level: Optional[str] = None,
):
    if data_source == "chart_cache":
        ex = ChartCacheExchange(market, chart_cache_dir)
    else:
        ex = get_exchange(Market(normalize_market(market)))
    return {
        code: MonitorSymbolState(
            code,
            ex,
            op_level=op_level,
            big_level=big_level,
            mid_level=mid_level,
        )
        for code in codes
    }, ex


def build_names(codes: Iterable[str], ex) -> dict[str, str]:
    names = {}
    for code in codes:
        try:
            info = ex.stock_info(code)
        except Exception:
            info = None
        names[code] = (info or {}).get("name") or code
    return names


def regime_ratio_review_status(path: str, market: str) -> dict:
    """读取 regime 比例 impact 报告,按市场统计 verdict 分布。只读巡检,不应用乘数。"""
    if not path or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {
        "review_regime_ratio": 0,
        "watch_regime_ratio": 0,
        "watch_defensive": 0,
        "watch_positive_tradeoff": 0,
        "keep_default": 0,
        "evidence_limited": 0,
    }
    matched = False
    for verdict in data.get("verdicts", []) or []:
        if str(verdict.get("market", "")) != market:
            continue
        key = str(verdict.get("verdict", ""))
        if key in out:
            out[key] += 1
            matched = True
    return out if matched else {}


_REGIME_CACHE: dict[tuple, str] = {}


def current_visible_regime(
    ex,
    code: str,
    lookback_days: int = 20,
    today: Optional[_dt.date] = None,
) -> str:
    """实盘点时行情状态:用截至前一交易日收盘的日线判定 bull/range/bear。
    当日未完成日线必须丢弃(盘中收盘不可见);取数失败或数据不足返回 range,
    即不调整买入比例。同一 (code, 日期) 当天只取一次数。"""
    today = today or _dt.date.today()
    key = (str(code), str(today), int(lookback_days))
    cached = _REGIME_CACHE.get(key)
    if cached:
        return cached
    try:
        klines = ex.klines(code, "d")
        if klines is None or len(klines) == 0:
            return "range"
        dates = pd.to_datetime(klines["date"])
        closes = klines["close"][[d.date() < today for d in dates]]
        regime = classify_visible_regime(list(closes), lookback_days)
    except Exception as exc:
        fun.get_logger().warning(
            f"[live_monitor] regime source fetch failed code={code}: {exc}"
        )
        return "range"
    # 滚动清理:只保留「今日」的 regime 缓存(按 date 维度),防止常驻多日无界增长。
    today_str = str(today)
    stale = [k for k in _REGIME_CACHE if k[1] != today_str]
    for k in stale:
        _REGIME_CACHE.pop(k, None)
    _REGIME_CACHE[key] = regime
    return regime


def market_is_open(ex, market: str, now: _dt.datetime) -> bool:
    market = normalize_market(market)
    if market == "a":
        return in_session(now)
    try:
        return bool(ex.now_trading())
    except Exception as exc:
        # now_trading 异常(如长桥 ctx 自愈期)时,不粗暴判休市致整轮漏扫,
        # 回退到本地时段近似(宁可多扫一轮空转,也不漏真盘中信号)。
        fun.get_logger().warning(
            f"[live_monitor] now_trading failed, fallback to session table: {exc}"
        )
        return _us_session_fallback(now)


def _us_session_fallback(now: _dt.datetime) -> bool:
    """美股常规时段的北京时间近似(给 now_trading 异常时兜底)。
    夏令时 21:30-04:00、冬令时 22:30-05:00,跨午夜;此处取并集放宽
    (21:30-05:00),宁可多扫空转也不漏。不处理节假日(异常兜底,容忍误开)。"""
    if now.weekday() >= 5 and not (now.weekday() == 5 and now.hour < 6):
        # 周六全天与周日(美股周五夜对应北京周六凌晨,放行周六早晨)
        if now.weekday() == 6:
            return False
    hm = now.hour * 100 + now.minute
    return hm >= 2130 or hm <= 500


def refresh_optimization_report(
    *,
    output_json: str,
    output_markdown: str | None = None,
    output_decision: str | None = None,
    output_decision_state: str | None = None,
    output_runtime_overrides: str | None = None,
    output_attribution_json: str | None = None,
    output_attribution_markdown: str | None = None,
    output_bs_point_json: str | None = None,
    output_bs_point_markdown: str | None = None,
    output_bs_point_ratio_state: str | None = None,
    output_bs_point_ratio_overrides: str | None = None,
    output_bs_point_regime_json: str | None = None,
    output_bs_point_regime_markdown: str | None = None,
    output_bs_point_regime_policy_json: str | None = None,
    output_bs_point_regime_policy_markdown: str | None = None,
    bs_point_regime_policy_min_trades: int = 30,
    output_market_regime_stress_json: str | None = None,
    output_market_regime_stress_markdown: str | None = None,
    market_regime_min_days: int = 10,
    output_regime_policy_json: str | None = None,
    output_regime_policy_markdown: str | None = None,
    regime_policy_min_supporting_sources: int = 2,
    output_mtf3_cache_coverage_json: str | None = None,
    output_mtf3_cache_coverage_markdown: str | None = None,
    mtf3_cache_chart_cache_dir: str | Path | None = None,
    mtf3_cache_bt_data_dir: str | Path | None = None,
    mtf3_cache_mtf3_bt_data_dir: str | Path | None = None,
    mtf3_cache_bt_sample_size: int = 300,
    output_sell3_rebuy_mid3_impact_json: str | None = None,
    output_sell3_rebuy_mid3_impact_markdown: str | None = None,
    output_sell3_rebuy3_impact_json: str | None = None,
    output_sell3_rebuy3_impact_markdown: str | None = None,
    output_sell3_rebuy3_up_impact_json: str | None = None,
    output_sell3_rebuy3_up_impact_markdown: str | None = None,
    output_a_5m_sell3_rebuy3_impact_json: str | None = None,
    output_a_5m_sell3_rebuy3_impact_markdown: str | None = None,
    output_regime_ratio_impact_json: str | None = None,
    output_regime_ratio_impact_markdown: str | None = None,
    output_strategy_adoption_gate_json: str | None = None,
    output_strategy_adoption_gate_markdown: str | None = None,
    runtime_override_audit_jsonl: str | None = None,
    attribution_ledger_paths: Mapping[str, str | Path] | None = None,
    bs_point_trade_paths: Mapping[str, str | Path] | None = None,
    market: str | None = None,
    include_discovered: bool = True,
    report_dir: str = "D:/chanlun_pro/reports",
    current_config: dict | None = None,
    decision_confirmation_threshold: int = 3,
    bs_point_ratio_confirmation_threshold: int = 3,
) -> dict:
    from chanlun.recursive_bt.strategy_optimizer import (
        A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
        A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
        A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
        A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
        US_2026Q1_MTF3_DEFAULT_SUMMARY,
        US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
        US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
        US_MTF3_DEFAULT_SUMMARY,
        US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
        US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
        build_market_regime_stress_report,
        build_decision_artifact,
        build_bs_point_ratio_state,
        match_candidate_from_monitor_config,
        update_bs_point_ratio_state_file,
        write_bs_point_attribution_report,
        write_bs_point_regime_attribution_report,
        write_bs_point_regime_policy_report,
        write_bs_point_ratio_overrides_file,
        write_market_regime_stress_report,
        write_mtf3_cache_coverage_report,
        write_regime_strategy_policy_report,
        write_strategy_attribution_report,
        write_strategy_adoption_gate_report,
        write_sell_policy_impact_report,
        write_regime_ratio_impact_report,
        write_optimization_report,
    )

    try:
        current_candidate_ids = None
        if current_config is not None:
            current_market = normalize_market(
                market or str(current_config.get("market") or "a")
            )
            matched = match_candidate_from_monitor_config(current_config, current_market)
            current_candidate_ids = {
                current_market: matched or "custom_monitor_config"
            }
        report = write_optimization_report(
            output_json,
            output_markdown=output_markdown,
            output_decision=output_decision,
            output_decision_state=output_decision_state,
            output_runtime_overrides=output_runtime_overrides,
            market=market,
            include_discovered=include_discovered,
            report_dir=report_dir,
            current_candidate_ids=current_candidate_ids,
            decision_confirmation_threshold=decision_confirmation_threshold,
        )
    except Exception as exc:
        fun.get_logger().warning(
            f"[live_monitor] optimization report refresh failed: {exc}"
        )
        return {"error": type(exc).__name__, "message": str(exc)}
    attribution_report = None
    if output_attribution_json:
        try:
            attribution_report = write_strategy_attribution_report(
                output_attribution_json,
                output_markdown=output_attribution_markdown,
                markets=(market,) if market else ("a", "us"),
                audit_path=runtime_override_audit_jsonl or RUNTIME_OVERRIDE_AUDIT_JSONL,
                ledger_paths=attribution_ledger_paths,
                ensure_baseline_ledgers=True,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] attribution report refresh failed: {exc}"
            )
    bs_point_report = None
    if output_bs_point_json:
        try:
            bs_point_report = write_bs_point_attribution_report(
                output_bs_point_json,
                output_markdown=output_bs_point_markdown,
                markets=(market,) if market else ("a", "us"),
                trade_paths=bs_point_trade_paths,
            )
            bs_point_state = None
            if output_bs_point_ratio_state:
                bs_point_state = update_bs_point_ratio_state_file(
                    output_bs_point_ratio_state,
                    bs_point_report,
                    confirmation_threshold=bs_point_ratio_confirmation_threshold,
                )
            if output_bs_point_ratio_overrides:
                if bs_point_state is None:
                    bs_point_state = build_bs_point_ratio_state(
                        bs_point_report,
                        confirmation_threshold=bs_point_ratio_confirmation_threshold,
                    )
                write_bs_point_ratio_overrides_file(
                    output_bs_point_ratio_overrides,
                    bs_point_state,
                )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] bs point attribution refresh failed: {exc}"
            )
    market_regime_report = None
    if output_market_regime_stress_json:
        try:
            market_regime_report = write_market_regime_stress_report(
                output_market_regime_stress_json,
                output_markdown=output_market_regime_stress_markdown,
                markets=(market,) if market else ("a", "us"),
                min_regime_days=market_regime_min_days,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] market regime stress refresh failed: {exc}"
            )
    bs_point_regime_report = None
    if output_bs_point_regime_json:
        try:
            bs_point_regime_report = write_bs_point_regime_attribution_report(
                output_bs_point_regime_json,
                output_markdown=output_bs_point_regime_markdown,
                markets=(market,) if market else ("a", "us"),
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] bs point regime attribution refresh failed: {exc}"
            )
    bs_point_regime_policy_report = None
    if output_bs_point_regime_policy_json and bs_point_regime_report is not None:
        try:
            bs_point_regime_policy_report = write_bs_point_regime_policy_report(
                output_bs_point_regime_policy_json,
                output_markdown=output_bs_point_regime_policy_markdown,
                bs_point_regime_report=bs_point_regime_report,
                min_trades=bs_point_regime_policy_min_trades,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] bs point regime policy refresh failed: {exc}"
            )
    regime_policy_report = None
    if output_regime_policy_json and market_regime_report is not None:
        try:
            regime_policy_sources = {"primary": market_regime_report}
            if market in (None, "", "us"):
                regime_policy_sources["us_2026q1_weak"] = (
                    build_market_regime_stress_report(
                        markets=("us",),
                        summary_paths={
                            "us": {
                                "default": US_2026Q1_MTF3_DEFAULT_SUMMARY,
                                "sell3_rebuy3": (
                                    US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY
                                ),
                                "sell3_rebuy_mid3": (
                                    US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY
                                ),
                            }
                        },
                        min_regime_days=market_regime_min_days,
                    )
                )
            regime_policy_report = write_regime_strategy_policy_report(
                output_regime_policy_json,
                output_markdown=output_regime_policy_markdown,
                report_sources=regime_policy_sources,
                min_supporting_sources=regime_policy_min_supporting_sources,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] regime policy refresh failed: {exc}"
            )
    mtf3_coverage_report = None
    if output_mtf3_cache_coverage_json:
        try:
            mtf3_coverage_report = write_mtf3_cache_coverage_report(
                output_mtf3_cache_coverage_json,
                output_markdown=output_mtf3_cache_coverage_markdown,
                markets=(market,) if market else ("a", "us"),
                chart_cache_dir=mtf3_cache_chart_cache_dir or "D:/chanlun_pro/chart_cache",
                bt_data_dir=mtf3_cache_bt_data_dir or "D:/chanlun_pro/bt_data_all_a",
                mtf3_bt_data_dir=mtf3_cache_mtf3_bt_data_dir or None,
                bt_sample_size=mtf3_cache_bt_sample_size,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] mtf3 cache coverage refresh failed: {exc}"
            )
    adoption_gate_report = None
    adoption_candidate_reports = []
    if output_sell3_rebuy3_impact_json:
        try:
            adoption_candidate_reports.append(
                write_sell_policy_impact_report(
                    output_sell3_rebuy3_impact_json,
                    output_markdown=output_sell3_rebuy3_impact_markdown,
                    markets=(market,) if market else ("a", "us"),
                    summary_paths={
                        "a": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
                        "us": US_MTF3_DEFAULT_SUMMARY,
                    },
                    candidate_summary_paths={
                        "a": A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
                        "us": US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
                    },
                    candidate_label="sell3_rebuy3",
                )
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] sell3 rebuy3 impact refresh failed: {exc}"
            )
    if output_sell3_rebuy3_up_impact_json and market in (None, "a"):
        try:
            adoption_candidate_reports.append(
                write_sell_policy_impact_report(
                    output_sell3_rebuy3_up_impact_json,
                    output_markdown=output_sell3_rebuy3_up_impact_markdown,
                    markets=("a",),
                    summary_paths={
                        "a": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
                    },
                    candidate_summary_paths={
                        "a": A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
                    },
                    candidate_label="sell3_rebuy3_up",
                )
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] sell3 rebuy3 up impact refresh failed: {exc}"
            )
    if output_sell3_rebuy_mid3_impact_json:
        try:
            adoption_candidate_reports.append(
                write_sell_policy_impact_report(
                    output_sell3_rebuy_mid3_impact_json,
                    output_markdown=output_sell3_rebuy_mid3_impact_markdown,
                    markets=(market,) if market else ("a", "us"),
                    summary_paths={
                        "a": A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
                        "us": US_MTF3_DEFAULT_SUMMARY,
                    },
                    candidate_summary_paths={
                        "a": A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
                        "us": US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
                    },
                    candidate_label="sell3_rebuy_mid3",
                )
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] sell3 rebuy mid3 impact refresh failed: {exc}"
            )
    if output_a_5m_sell3_rebuy3_impact_json and market in (None, "a"):
        try:
            adoption_candidate_reports.append(
                write_sell_policy_impact_report(
                    output_a_5m_sell3_rebuy3_impact_json,
                    output_markdown=output_a_5m_sell3_rebuy3_impact_markdown,
                    markets=("a",),
                    summary_paths={
                        "a": "D:/chanlun_pro/reports/a_bt_all_a_5m30m_default_summary.json"
                    },
                    candidate_summary_paths={
                        "a": "D:/chanlun_pro/reports/a_bt_all_a_5m30m_sell3_rebuy3_summary.json"
                    },
                    candidate_label="a_5m_sell3_rebuy3",
                )
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] A 5m sell3 rebuy3 impact refresh failed: {exc}"
            )
    regime_ratio_impact_report = None
    if output_regime_ratio_impact_json:
        try:
            regime_ratio_impact_report = write_regime_ratio_impact_report(
                output_regime_ratio_impact_json,
                output_markdown=output_regime_ratio_impact_markdown,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] regime ratio impact refresh failed: {exc}"
            )
    if output_strategy_adoption_gate_json and mtf3_coverage_report is not None:
        try:
            adoption_gate_report = write_strategy_adoption_gate_report(
                output_strategy_adoption_gate_json,
                output_markdown=output_strategy_adoption_gate_markdown,
                coverage_report=mtf3_coverage_report,
                candidate_reports=adoption_candidate_reports,
            )
        except Exception as exc:
            fun.get_logger().warning(
                f"[live_monitor] strategy adoption gate refresh failed: {exc}"
            )
    decision_artifact = build_decision_artifact(report)
    decision_state = None
    if output_decision_state:
        try:
            decision_state = json.loads(Path(output_decision_state).read_text(encoding="utf-8"))
        except Exception:
            decision_state = None
    return {
        "json": output_json,
        "markdown": output_markdown,
        "decision_json": output_decision,
        "decision_state_json": output_decision_state,
        "runtime_overrides_json": output_runtime_overrides,
        "attribution_json": output_attribution_json,
        "attribution_markdown": output_attribution_markdown,
        "bs_point_attribution_json": output_bs_point_json,
        "bs_point_attribution_markdown": output_bs_point_markdown,
        "bs_point_ratio_state_json": output_bs_point_ratio_state,
        "bs_point_ratio_overrides_json": output_bs_point_ratio_overrides,
        "bs_point_regime_json": output_bs_point_regime_json,
        "bs_point_regime_markdown": output_bs_point_regime_markdown,
        "bs_point_regime_missing_count": len(
            (bs_point_regime_report or {}).get("missing_sources", []) or []
        ),
        "bs_point_regime_policy_json": output_bs_point_regime_policy_json,
        "bs_point_regime_policy_markdown": output_bs_point_regime_policy_markdown,
        "bs_point_regime_policy_count": len(
            (bs_point_regime_policy_report or {}).get("policies", []) or []
        ),
        "market_regime_stress_json": output_market_regime_stress_json,
        "market_regime_stress_markdown": output_market_regime_stress_markdown,
        "regime_policy_json": output_regime_policy_json,
        "regime_policy_markdown": output_regime_policy_markdown,
        "regime_policy_count": len((regime_policy_report or {}).get("policies", []) or []),
        "mtf3_cache_coverage_json": output_mtf3_cache_coverage_json,
        "mtf3_cache_coverage_markdown": output_mtf3_cache_coverage_markdown,
        "mtf3_cache_chart_cache_dir": str(mtf3_cache_chart_cache_dir or ""),
        "mtf3_cache_bt_data_dir": str(mtf3_cache_bt_data_dir or ""),
        "mtf3_cache_mtf3_bt_data_dir": str(mtf3_cache_mtf3_bt_data_dir or ""),
        "mtf3_cache_bt_sample_size": int(mtf3_cache_bt_sample_size),
        "sell3_rebuy_mid3_impact_json": output_sell3_rebuy_mid3_impact_json,
        "sell3_rebuy3_impact_json": output_sell3_rebuy3_impact_json,
        "sell3_rebuy3_up_impact_json": output_sell3_rebuy3_up_impact_json,
        "a_5m_sell3_rebuy3_impact_json": output_a_5m_sell3_rebuy3_impact_json,
        "regime_ratio_impact_json": output_regime_ratio_impact_json,
        "regime_ratio_impact_verdicts": len(
            (regime_ratio_impact_report or {}).get("verdicts", []) or []
        ),
        "strategy_adoption_gate_json": output_strategy_adoption_gate_json,
        "strategy_adoption_gate_markdown": output_strategy_adoption_gate_markdown,
        "candidate_count": len(report.get("candidate_ranking", []) or []),
        "runtime_count": len(report.get("runtime_ranking", []) or []),
        "missing_count": len(report.get("missing_sources", []) or []),
        "recommendations": report.get("recommendations", []),
        "action_suggestions": report.get("action_suggestions", []),
        "decisions": decision_artifact.get("decisions", []),
        "decision_state": decision_state,
        "attribution_segments": sum(
            len(item.get("segments", []) or [])
            for item in ((attribution_report or {}).get("markets", []) or [])
        ),
        "attribution_missing_count": len((attribution_report or {}).get("missing_sources", []) or []),
        "bs_point_missing_count": len((bs_point_report or {}).get("missing_sources", []) or []),
        "adoption_gate_count": len((adoption_gate_report or {}).get("gates", []) or []),
    }


def _current_monitor_config_from_args(args) -> dict:
    return {
        "market": getattr(args, "market", "a"),
        "max_pos": getattr(args, "max_pos", None),
        "op_level": getattr(args, "op_level", ""),
        "mid_level": getattr(args, "mid_level", ""),
        "big_level": getattr(args, "big_level", ""),
        "mid_gate": getattr(args, "mid_gate", ""),
        "regime_mode": getattr(args, "regime_mode", ""),
        "nest_mode": getattr(args, "nest_mode", ""),
        "trend_3boost": getattr(args, "trend_3boost", False),
    }


def runtime_override_notice_lines(event: dict) -> list[str]:
    config = event.get("applied_config") or {}
    config_text = ", ".join(f"{key}={value}" for key, value in sorted(config.items()))
    return [
        "[策略覆盖已应用]",
        f"market={event.get('market')} action={event.get('action')} risk={event.get('risk_state')}",
        f"target={event.get('target_candidate')} confirmations={event.get('confirmations')}/{event.get('confirmation_threshold')}",
        f"reason={event.get('reason')}",
        f"config={config_text}",
    ]


def send_runtime_override_notice(notifier, title: str, event: dict | None) -> bool:
    if not event:
        return False
    return bool(notifier.send(f"{title} 策略覆盖", runtime_override_notice_lines(event)))


_LAST_OPT_REFRESH_TS: float = 0.0


def _optimization_refresh_due(min_interval_seconds: float) -> bool:
    """优化报告刷新节流:全套报告每轮重写耗时较长,按时间间隔节流,把扫描周期留给信号。
    首轮(进程启动后)总是刷新;--once 单次模式因此不受影响。"""
    global _LAST_OPT_REFRESH_TS
    now_ts = time.time()
    if now_ts - _LAST_OPT_REFRESH_TS < max(float(min_interval_seconds or 0), 0.0):
        return False
    _LAST_OPT_REFRESH_TS = now_ts
    return True


def run_once(args, states: Dict[str, object], notifier, deduper, names=None, broker=None,
             exchange=None) -> int:
    holdings = load_ledger_positions(args.ledger)
    regime_multipliers = getattr(args, "regime_ratio_multipliers", None) or {}
    current_regime = ""
    if regime_multipliers and exchange is not None:
        current_regime = current_visible_regime(
            exchange,
            getattr(args, "regime_source_code", "") or INDEX_BY_MARKET.get(args.market, ""),
            int(getattr(args, "regime_lookback_days", 20) or 20),
        )
    events = collect_monitor_events(
        states,
        names=names,
        holdings=holdings,
        max_pos=args.max_pos,
        sell_scope=args.sell_scope,
        regime_mode=args.regime_mode,
        mid_gate=args.mid_gate,
        require_nest=args.require_nest,
        nest_mode=args.nest_mode,
        trend_3boost=args.trend_3boost,
        bs_point_ratio_multipliers=getattr(args, "bs_point_ratio_multipliers", None),
        regime_ratio_multipliers=regime_multipliers,
        current_regime=current_regime,
    )
    fresh = fresh_monitor_events(events, deduper)
    sent = send_events(events, notifier, deduper, args.title)
    paper_queued = 0
    paper_snapshot = None
    paper_summary = None
    if broker is not None:
        now = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
        broker.fill_pending(
            states,
            now,
        )
        if sent > 0:
            paper_queued = broker.queue_events(fresh)
        paper_snapshot = broker.record_snapshot(states, now)
        paper_summary = broker.performance_summary()
        broker.save()
    optimization_report = None
    refresh_due = _optimization_refresh_due(
        getattr(args, "optimization_report_min_interval", 600)
    )
    if getattr(args, "optimization_report_enabled", False) and refresh_due:
        optimization_report = refresh_optimization_report(
            output_json=args.optimization_report_json,
            output_markdown=args.optimization_report_markdown,
            output_decision=args.optimization_decision_json,
            output_decision_state=args.optimization_decision_state_json,
            output_runtime_overrides=args.optimization_runtime_overrides_json,
            output_attribution_json=args.strategy_attribution_json,
            output_attribution_markdown=args.strategy_attribution_markdown,
            output_bs_point_json=args.bs_point_attribution_json,
            output_bs_point_markdown=args.bs_point_attribution_markdown,
            output_bs_point_ratio_state=args.bs_point_ratio_state_json,
            output_bs_point_ratio_overrides=args.bs_point_ratio_overrides_json,
            output_bs_point_regime_json=args.bs_point_regime_json,
            output_bs_point_regime_markdown=args.bs_point_regime_markdown,
            output_bs_point_regime_policy_json=args.bs_point_regime_policy_json,
            output_bs_point_regime_policy_markdown=args.bs_point_regime_policy_markdown,
            bs_point_regime_policy_min_trades=args.bs_point_regime_policy_min_trades,
            output_market_regime_stress_json=args.market_regime_stress_json,
            output_market_regime_stress_markdown=args.market_regime_stress_markdown,
            market_regime_min_days=args.market_regime_min_days,
            output_regime_policy_json=args.regime_policy_json,
            output_regime_policy_markdown=args.regime_policy_markdown,
            regime_policy_min_supporting_sources=args.regime_policy_min_supporting_sources,
            output_mtf3_cache_coverage_json=args.mtf3_cache_coverage_json,
            output_mtf3_cache_coverage_markdown=args.mtf3_cache_coverage_markdown,
            mtf3_cache_chart_cache_dir=args.mtf3_cache_chart_cache_dir,
            mtf3_cache_bt_data_dir=args.mtf3_cache_bt_data_dir,
            mtf3_cache_mtf3_bt_data_dir=args.mtf3_cache_mtf3_bt_data_dir,
            mtf3_cache_bt_sample_size=args.mtf3_cache_bt_sample_size,
            output_sell3_rebuy3_impact_json=args.sell3_rebuy3_impact_json,
            output_sell3_rebuy3_impact_markdown=args.sell3_rebuy3_impact_markdown,
            output_sell3_rebuy3_up_impact_json=args.sell3_rebuy3_up_impact_json,
            output_sell3_rebuy3_up_impact_markdown=args.sell3_rebuy3_up_impact_markdown,
            output_regime_ratio_impact_json=args.regime_ratio_impact_json,
            output_regime_ratio_impact_markdown=args.regime_ratio_impact_markdown,
            output_sell3_rebuy_mid3_impact_json=args.sell3_rebuy_mid3_impact_json,
            output_sell3_rebuy_mid3_impact_markdown=args.sell3_rebuy_mid3_impact_markdown,
            output_a_5m_sell3_rebuy3_impact_json=args.a_5m_sell3_rebuy3_impact_json,
            output_a_5m_sell3_rebuy3_impact_markdown=args.a_5m_sell3_rebuy3_impact_markdown,
            output_strategy_adoption_gate_json=args.strategy_adoption_gate_json,
            output_strategy_adoption_gate_markdown=args.strategy_adoption_gate_markdown,
            runtime_override_audit_jsonl=args.runtime_override_audit_jsonl,
            market=None,
            include_discovered=args.optimization_report_include_discovered,
            report_dir=args.optimization_report_dir,
            current_config=_current_monitor_config_from_args(args),
            decision_confirmation_threshold=args.decision_confirmation_threshold,
            bs_point_ratio_confirmation_threshold=args.bs_point_ratio_confirmation_threshold,
        )
    paper_status = ""
    if paper_snapshot is not None and paper_summary is not None:
        paper_status = (
            f" paper_queued={paper_queued} pending={len(broker.pending)}"
            f" paper_equity={paper_snapshot['equity']:.0f}"
            f" paper_return={paper_summary['total_return']:.2%}"
            f" paper_dd={paper_summary['max_drawdown']:.2%}"
        )
    optimization_status = ""
    if optimization_report is not None:
        action = _market_action_from_report(
            optimization_report,
            normalize_market(getattr(args, "market", "a")),
        )
        optimization_status = (
            f" opt_runtime={optimization_report.get('runtime_count', 0)}"
            f" opt_missing={optimization_report.get('missing_count', 0)}"
            f" opt_action={action}"
            f" opt_apply={_apply_count_from_report(optimization_report)}"
        )
        gate_status = strategy_adoption_gate_status(
            getattr(args, "strategy_adoption_gate_json", ""),
            normalize_market(getattr(args, "market", "a")),
        )
        if gate_status:
            optimization_status += (
                f" opt_gate_allowed={gate_status.get('review_allowed', 0)}"
                f" opt_gate_limited={gate_status.get('watch_evidence_limited', 0)}"
                f" opt_gate_blocked={gate_status.get('blocked', 0)}"
            )
        regime_status = regime_policy_status(
            getattr(args, "regime_policy_json", ""),
            normalize_market(getattr(args, "market", "a")),
        )
        if regime_status:
            optimization_status += (
                f" opt_regime_review={regime_status.get('review_regime_candidate', 0)}"
                f" opt_regime_watch={regime_status.get('watch_regime_candidate', 0)}"
                f" opt_regime_default={regime_status.get('keep_default', 0)}"
                f" opt_regime_limited={regime_status.get('evidence_limited', 0)}"
            )
        ratio_status = regime_ratio_review_status(
            getattr(args, "regime_ratio_impact_json", ""),
            normalize_market(getattr(args, "market", "a")),
        )
        if ratio_status:
            watch_total = (
                ratio_status.get("watch_regime_ratio", 0)
                + ratio_status.get("watch_defensive", 0)
                + ratio_status.get("watch_positive_tradeoff", 0)
            )
            optimization_status += (
                f" opt_rratio_review={ratio_status.get('review_regime_ratio', 0)}"
                f" opt_rratio_watch={watch_total}"
            )
    failing = sum(
        1 for s in states.values()
        if getattr(s, "consecutive_refresh_failures", 0) >= 3
    )
    print(
        f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] "
        f"scan={len(states)} holdings={len(holdings)} events={len(events)} sent={sent}"
        f"{' refresh_failing=' + str(failing) if failing else ''}"
        f"{paper_status}{optimization_status}"
    )
    return sent


def _market_action_from_report(report: dict, market: str) -> str:
    for item in report.get("action_suggestions", []) or []:
        if str(item.get("market") or "").strip().lower() == market:
            return str(item.get("action") or "")
    return ""


def _apply_count_from_report(report: dict) -> int:
    state = report.get("decision_state") or {}
    try:
        return int(state.get("apply_allowed_count") or 0)
    except Exception:
        return 0


def strategy_adoption_gate_status(path: str | os.PathLike, market: str) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    wanted = normalize_market(market)
    counts = {
        "review_allowed": 0,
        "watch_evidence_limited": 0,
        "blocked": 0,
        "keep_default": 0,
    }
    for item in data.get("gates", []) or []:
        if str(item.get("market") or "").strip().lower() != wanted:
            continue
        action = str(item.get("gate_action") or "")
        if action == "review_allowed":
            counts["review_allowed"] += 1
        elif action == "watch_evidence_limited":
            counts["watch_evidence_limited"] += 1
        elif action.startswith("blocked"):
            counts["blocked"] += 1
        elif action == "keep_default":
            counts["keep_default"] += 1
    counts["total"] = sum(counts.values())
    return counts if counts["total"] else {}


def regime_policy_status(path: str | os.PathLike, market: str) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    wanted = normalize_market(market)
    counts = {
        "review_regime_candidate": 0,
        "watch_regime_candidate": 0,
        "keep_default": 0,
        "evidence_limited": 0,
    }
    for item in data.get("policies", []) or []:
        if str(item.get("market") or "").strip().lower() != wanted:
            continue
        action = str(item.get("policy_action") or "")
        if action in counts:
            counts[action] += 1
    counts["total"] = sum(counts.values())
    return counts if counts["total"] else {}


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Realtime Chanlun buy/sell alert monitor")
    parser.add_argument("--market", choices=("a", "us"), default="a")
    parser.add_argument("--data-source", choices=("online", "chart_cache"), default="online")
    parser.add_argument("--codes", help="Comma separated codes or a text file path")
    parser.add_argument("--bt-data")
    parser.add_argument("--chart-cache")
    parser.add_argument("--pool-size", type=int)
    parser.add_argument("--ledger")
    parser.add_argument("--state-file")
    parser.add_argument("--max-pos", type=int)
    parser.add_argument("--op-level", help="Signal/operation level, e.g. 1m or 5m")
    parser.add_argument("--mid-level", help="Optional middle gate level, e.g. 5m")
    parser.add_argument("--big-level", help="Large gate level, e.g. 30m")
    parser.add_argument("--regime-mode", choices=("off", "adaptive"))
    parser.add_argument("--mid-gate", choices=("strict", "soft", "bull_relaxed"))
    parser.add_argument("--require-nest", action="store_true", default=None)
    parser.add_argument("--nest-mode", choices=("off", "filter", "soft"))
    parser.add_argument("--trend-3boost", action="store_true", default=None)
    parser.add_argument("--sell-scope", choices=("all", "positions"))
    parser.add_argument("--title")
    parser.add_argument("--once", action="store_true", help="Scan once and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        default=None,
        help="Scan even outside trading session",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Print alerts instead of DingTalk",
    )
    parser.add_argument(
        "--paper-enabled",
        dest="paper_enabled",
        action="store_true",
        default=None,
        help="Queue fresh alerts into the local paper ledger",
    )
    parser.add_argument(
        "--no-paper",
        dest="paper_enabled",
        action="store_false",
        help="Disable local paper ledger execution",
    )
    parser.add_argument(
        "--optimization-report-enabled",
        dest="optimization_report_enabled",
        action="store_true",
        default=None,
        help="Refresh strategy optimization report after each scan",
    )
    parser.add_argument(
        "--no-optimization-report",
        dest="optimization_report_enabled",
        action="store_false",
        help="Disable strategy optimization report refresh",
    )
    parser.add_argument("--optimization-report-json")
    parser.add_argument("--optimization-report-markdown")
    parser.add_argument("--optimization-decision-json")
    parser.add_argument("--optimization-decision-state-json")
    parser.add_argument("--optimization-runtime-overrides-json")
    parser.add_argument("--strategy-attribution-json")
    parser.add_argument("--strategy-attribution-markdown")
    parser.add_argument("--market-regime-stress-json")
    parser.add_argument("--market-regime-stress-markdown")
    parser.add_argument("--market-regime-min-days", type=int)
    parser.add_argument("--regime-policy-json")
    parser.add_argument("--regime-policy-markdown")
    parser.add_argument("--regime-policy-min-supporting-sources", type=int)
    parser.add_argument("--mtf3-cache-coverage-json")
    parser.add_argument("--mtf3-cache-coverage-markdown")
    parser.add_argument("--mtf3-cache-chart-cache-dir")
    parser.add_argument("--mtf3-cache-bt-data-dir")
    parser.add_argument("--mtf3-cache-mtf3-bt-data-dir")
    parser.add_argument("--mtf3-cache-bt-sample-size", type=int)
    parser.add_argument("--sell3-rebuy3-impact-json")
    parser.add_argument("--sell3-rebuy3-impact-markdown")
    parser.add_argument("--sell3-rebuy3-up-impact-json")
    parser.add_argument("--sell3-rebuy3-up-impact-markdown")
    parser.add_argument("--regime-ratio-impact-json")
    parser.add_argument("--regime-ratio-impact-markdown")
    parser.add_argument(
        "--optimization-report-min-interval",
        type=float,
        default=None,
        help="Minimum seconds between full optimization-report refreshes (default 600)",
    )
    parser.add_argument(
        "--dingtalk-webhook",
        default="",
        help="DingTalk robot webhook for buy/sell alerts (overrides config)",
    )
    parser.add_argument(
        "--dingtalk-keyword",
        default="",
        help="DingTalk robot custom keyword injected into every alert",
    )
    parser.add_argument(
        "--regime-ratio-multipliers-json",
        default="",
        help='Inline JSON or file path, e.g. {"bear": {"3": 1.25}}: point-in-time '
        "regime buy-ratio multipliers (uses previous-day index close)",
    )
    parser.add_argument("--regime-source-code", default="")
    parser.add_argument("--regime-lookback-days", type=int, default=20)
    parser.add_argument("--sell3-rebuy-mid3-impact-json")
    parser.add_argument("--sell3-rebuy-mid3-impact-markdown")
    parser.add_argument("--a-5m-sell3-rebuy3-impact-json")
    parser.add_argument("--a-5m-sell3-rebuy3-impact-markdown")
    parser.add_argument("--strategy-adoption-gate-json")
    parser.add_argument("--strategy-adoption-gate-markdown")
    parser.add_argument("--bs-point-attribution-json")
    parser.add_argument("--bs-point-attribution-markdown")
    parser.add_argument("--bs-point-ratio-state-json")
    parser.add_argument("--bs-point-ratio-overrides-json")
    parser.add_argument("--bs-point-regime-json")
    parser.add_argument("--bs-point-regime-markdown")
    parser.add_argument("--bs-point-regime-policy-json")
    parser.add_argument("--bs-point-regime-policy-markdown")
    parser.add_argument("--bs-point-regime-policy-min-trades", type=int)
    parser.add_argument("--bs-point-ratio-confirmation-threshold", type=int)
    parser.add_argument("--runtime-override-audit-jsonl")
    parser.add_argument(
        "--runtime-overrides-enabled",
        dest="runtime_overrides_enabled",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--no-runtime-overrides",
        dest="runtime_overrides_enabled",
        action="store_false",
    )
    parser.add_argument("--decision-confirmation-threshold", type=int)
    parser.add_argument("--optimization-report-dir")
    parser.add_argument(
        "--no-optimization-report-discover",
        dest="optimization_report_include_discovered",
        action="store_false",
        default=None,
    )
    parser.add_argument("--hook-command", help="Override Claude Notification hook command")
    parser.add_argument("--settings-path", help="Override Claude Code settings.json path")
    parser.add_argument("--warmup", dest="warmup", action="store_true")
    parser.add_argument("--no-warmup", dest="warmup", action="store_false")
    parser.set_defaults(warmup=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = make_arg_parser().parse_args(argv)
    args.market = normalize_market(args.market)
    market_config = _monitor_market_config(
        args.market,
        args.runtime_overrides_enabled,
        args.optimization_runtime_overrides_json,
    )
    args.bt_data = args.bt_data or str(market_config.get("bt_data") or BT_DATA)
    args.chart_cache = args.chart_cache or str(
        market_config.get("chart_cache_dir") or "D:/chanlun_pro/chart_cache"
    )
    args.pool_size = (
        args.pool_size
        if args.pool_size is not None
        else _config_int(market_config.get("selection_max_codes"), MAX_POS * 5)
    )
    config_max_pos = market_config.get("max_pos")
    args.max_pos = (
        args.max_pos
        if args.max_pos is not None
        else (_config_int(config_max_pos, 0) if config_max_pos is not None else 0)
    )
    args.op_level = str(args.op_level or market_config.get("op_level") or "5m")
    args.mid_level = (
        str(args.mid_level or market_config.get("mid_level") or "") or None
    )
    args.big_level = str(args.big_level or market_config.get("big_level") or "30m")
    args.regime_mode = str(
        args.regime_mode or market_config.get("regime_mode") or "off"
    )
    args.mid_gate = str(args.mid_gate or market_config.get("mid_gate") or "strict")
    args.require_nest = (
        args.require_nest
        if args.require_nest is not None
        else _config_bool(market_config.get("require_nest"), False)
    )
    args.nest_mode = str(args.nest_mode or market_config.get("nest_mode") or "off")
    args.trend_3boost = (
        args.trend_3boost
        if args.trend_3boost is not None
        else _config_bool(market_config.get("trend_3boost"), False)
    )
    args.sell_scope = args.sell_scope or str(market_config.get("sell_scope") or "all")
    args.force = (
        args.force
        if args.force is not None
        else _config_bool(market_config.get("force"), False)
    )
    args.dry_run = (
        args.dry_run
        if args.dry_run is not None
        else _config_bool(market_config.get("dry_run"), False)
    )
    args.paper_enabled = (
        args.paper_enabled
        if args.paper_enabled is not None
        else _config_bool(market_config.get("paper_enabled"), False)
    )
    args.optimization_report_enabled = (
        args.optimization_report_enabled
        if args.optimization_report_enabled is not None
        else _config_bool(market_config.get("optimization_report_enabled"), False)
    )
    args.optimization_report_json = str(
        args.optimization_report_json
        or market_config.get("optimization_report_json")
        or "D:/chanlun_pro/reports/strategy_optimization_report.json"
    )
    args.optimization_report_markdown = str(
        args.optimization_report_markdown
        or market_config.get("optimization_report_markdown")
        or "D:/chanlun_pro/reports/strategy_optimization_report.md"
    )
    args.optimization_decision_json = str(
        args.optimization_decision_json
        or market_config.get("optimization_decision_json")
        or "D:/chanlun_pro/reports/strategy_decision.json"
    )
    args.optimization_decision_state_json = str(
        args.optimization_decision_state_json
        or market_config.get("optimization_decision_state_json")
        or "D:/chanlun_pro/reports/strategy_decision_state.json"
    )
    args.optimization_runtime_overrides_json = str(
        args.optimization_runtime_overrides_json
        or market_config.get("optimization_runtime_overrides_json")
        or "D:/chanlun_pro/reports/strategy_runtime_overrides.json"
    )
    args.strategy_attribution_json = str(
        args.strategy_attribution_json
        or market_config.get("strategy_attribution_json")
        or "D:/chanlun_pro/reports/strategy_attribution_report.json"
    )
    args.strategy_attribution_markdown = str(
        args.strategy_attribution_markdown
        or market_config.get("strategy_attribution_markdown")
        or "D:/chanlun_pro/reports/strategy_attribution_report.md"
    )
    args.market_regime_stress_json = str(
        args.market_regime_stress_json
        or market_config.get("market_regime_stress_json")
        or "D:/chanlun_pro/reports/strategy_market_regime_stress_report.json"
    )
    args.market_regime_stress_markdown = str(
        args.market_regime_stress_markdown
        or market_config.get("market_regime_stress_markdown")
        or "D:/chanlun_pro/reports/strategy_market_regime_stress_report.md"
    )
    args.market_regime_min_days = (
        args.market_regime_min_days
        if args.market_regime_min_days is not None
        else _config_int(market_config.get("market_regime_min_days"), 10)
    )
    args.regime_policy_json = str(
        args.regime_policy_json
        or market_config.get("regime_policy_json")
        or "D:/chanlun_pro/reports/strategy_regime_policy_report.json"
    )
    args.regime_policy_markdown = str(
        args.regime_policy_markdown
        or market_config.get("regime_policy_markdown")
        or "D:/chanlun_pro/reports/strategy_regime_policy_report.md"
    )
    args.regime_policy_min_supporting_sources = (
        args.regime_policy_min_supporting_sources
        if args.regime_policy_min_supporting_sources is not None
        else _config_int(market_config.get("regime_policy_min_supporting_sources"), 2)
    )
    args.mtf3_cache_coverage_json = str(
        args.mtf3_cache_coverage_json
        or market_config.get("mtf3_cache_coverage_json")
        or "D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.json"
    )
    args.mtf3_cache_coverage_markdown = str(
        args.mtf3_cache_coverage_markdown
        or market_config.get("mtf3_cache_coverage_markdown")
        or "D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.md"
    )
    args.mtf3_cache_chart_cache_dir = str(
        args.mtf3_cache_chart_cache_dir
        or market_config.get("mtf3_cache_chart_cache_dir")
        or "D:/chanlun_pro/chart_cache"
    )
    args.mtf3_cache_bt_data_dir = str(
        args.mtf3_cache_bt_data_dir
        or market_config.get("mtf3_cache_bt_data_dir")
        or "D:/chanlun_pro/bt_data_all_a"
    )
    args.mtf3_cache_mtf3_bt_data_dir = str(
        args.mtf3_cache_mtf3_bt_data_dir
        or market_config.get("mtf3_cache_mtf3_bt_data_dir")
        or "D:/chanlun_pro/bt_data_mtf3_all_a"
    )
    args.mtf3_cache_bt_sample_size = (
        args.mtf3_cache_bt_sample_size
        if args.mtf3_cache_bt_sample_size is not None
        else _config_int(market_config.get("mtf3_cache_bt_sample_size"), 300)
    )
    args.sell3_rebuy3_impact_json = str(
        args.sell3_rebuy3_impact_json
        or market_config.get("sell3_rebuy3_impact_json")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.json"
    )
    args.sell3_rebuy3_impact_markdown = str(
        args.sell3_rebuy3_impact_markdown
        or market_config.get("sell3_rebuy3_impact_markdown")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.md"
    )
    args.sell3_rebuy3_up_impact_json = str(
        args.sell3_rebuy3_up_impact_json
        or market_config.get("sell3_rebuy3_up_impact_json")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.json"
    )
    args.regime_ratio_multipliers = parse_regime_ratio_multipliers(
        getattr(args, "regime_ratio_multipliers_json", "")
        or market_config.get("regime_ratio_multipliers")
        or {}
    )
    args.regime_source_code = str(
        getattr(args, "regime_source_code", "")
        or market_config.get("regime_source_code")
        or INDEX_BY_MARKET.get(args.market, "")
    )
    args.regime_lookback_days = int(
        getattr(args, "regime_lookback_days", 0)
        or market_config.get("regime_lookback_days")
        or 20
    )
    args.optimization_report_min_interval = float(
        getattr(args, "optimization_report_min_interval", None)
        or market_config.get("optimization_report_min_interval")
        or 600
    )
    args.regime_ratio_impact_json = str(
        getattr(args, "regime_ratio_impact_json", None)
        or market_config.get("regime_ratio_impact_json")
        or "D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.json"
    )
    args.regime_ratio_impact_markdown = str(
        getattr(args, "regime_ratio_impact_markdown", None)
        or market_config.get("regime_ratio_impact_markdown")
        or "D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.md"
    )
    args.sell3_rebuy3_up_impact_markdown = str(
        args.sell3_rebuy3_up_impact_markdown
        or market_config.get("sell3_rebuy3_up_impact_markdown")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.md"
    )
    args.sell3_rebuy_mid3_impact_json = str(
        args.sell3_rebuy_mid3_impact_json
        or market_config.get("sell3_rebuy_mid3_impact_json")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.json"
    )
    args.sell3_rebuy_mid3_impact_markdown = str(
        args.sell3_rebuy_mid3_impact_markdown
        or market_config.get("sell3_rebuy_mid3_impact_markdown")
        or "D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.md"
    )
    args.a_5m_sell3_rebuy3_impact_json = str(
        args.a_5m_sell3_rebuy3_impact_json
        or market_config.get("a_5m_sell3_rebuy3_impact_json")
        or "D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.json"
    )
    args.a_5m_sell3_rebuy3_impact_markdown = str(
        args.a_5m_sell3_rebuy3_impact_markdown
        or market_config.get("a_5m_sell3_rebuy3_impact_markdown")
        or "D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.md"
    )
    args.strategy_adoption_gate_json = str(
        args.strategy_adoption_gate_json
        or market_config.get("strategy_adoption_gate_json")
        or "D:/chanlun_pro/reports/strategy_adoption_gate_report.json"
    )
    args.strategy_adoption_gate_markdown = str(
        args.strategy_adoption_gate_markdown
        or market_config.get("strategy_adoption_gate_markdown")
        or "D:/chanlun_pro/reports/strategy_adoption_gate_report.md"
    )
    args.bs_point_attribution_json = str(
        args.bs_point_attribution_json
        or market_config.get("bs_point_attribution_json")
        or "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json"
    )
    args.bs_point_attribution_markdown = str(
        args.bs_point_attribution_markdown
        or market_config.get("bs_point_attribution_markdown")
        or "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.md"
    )
    args.bs_point_ratio_state_json = str(
        args.bs_point_ratio_state_json
        or market_config.get("bs_point_ratio_state_json")
        or "D:/chanlun_pro/reports/strategy_bs_point_ratio_state.json"
    )
    args.bs_point_ratio_overrides_json = str(
        args.bs_point_ratio_overrides_json
        or market_config.get("bs_point_ratio_overrides_json")
        or "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
    )
    args.bs_point_regime_json = str(
        args.bs_point_regime_json
        or market_config.get("bs_point_regime_json")
        or "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.json"
    )
    args.bs_point_regime_markdown = str(
        args.bs_point_regime_markdown
        or market_config.get("bs_point_regime_markdown")
        or "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md"
    )
    args.bs_point_regime_policy_json = str(
        args.bs_point_regime_policy_json
        or market_config.get("bs_point_regime_policy_json")
        or "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json"
    )
    args.bs_point_regime_policy_markdown = str(
        args.bs_point_regime_policy_markdown
        or market_config.get("bs_point_regime_policy_markdown")
        or "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.md"
    )
    args.bs_point_regime_policy_min_trades = (
        args.bs_point_regime_policy_min_trades
        if args.bs_point_regime_policy_min_trades is not None
        else _config_int(market_config.get("bs_point_regime_policy_min_trades"), 30)
    )
    args.runtime_override_audit_jsonl = str(
        args.runtime_override_audit_jsonl
        or market_config.get("runtime_override_audit_jsonl")
        or RUNTIME_OVERRIDE_AUDIT_JSONL
    )
    args.runtime_overrides_enabled = (
        args.runtime_overrides_enabled
        if args.runtime_overrides_enabled is not None
        else _config_bool(market_config.get("runtime_overrides_enabled"), True)
    )
    args.bs_point_ratio_multipliers = dict(
        market_config.get("bs_point_ratio_multipliers") or {}
    )
    args.decision_confirmation_threshold = (
        args.decision_confirmation_threshold
        if args.decision_confirmation_threshold is not None
        else _config_int(market_config.get("decision_confirmation_threshold"), 3)
    )
    args.bs_point_ratio_confirmation_threshold = (
        args.bs_point_ratio_confirmation_threshold
        if args.bs_point_ratio_confirmation_threshold is not None
        else _config_int(market_config.get("bs_point_ratio_confirmation_threshold"), 3)
    )
    args.optimization_report_dir = str(
        args.optimization_report_dir
        or market_config.get("optimization_report_dir")
        or "D:/chanlun_pro/reports"
    )
    args.optimization_report_include_discovered = (
        args.optimization_report_include_discovered
        if args.optimization_report_include_discovered is not None
        else _config_bool(market_config.get("optimization_report_include_discovered"), True)
    )
    args.ledger = args.ledger or str(market_config.get("ledger") or default_ledger_path(args.market))
    args.state_file = args.state_file or str(
        market_config.get("state_file") or default_monitor_state_path(args.market)
    )
    args.title = args.title or str(
        market_config.get("title") or f"缠论实时买卖点提醒({args.market})"
    )
    explicit_codes = parse_codes(args.market, args.codes)
    holdings = load_ledger_positions(args.ledger)
    selector = None
    if args.market == "a" and not explicit_codes:
        selector = OriginalChanlunASelector(
            ASelectionConfig(
                bt_data=args.bt_data,
                fund_data=str(market_config.get("fund_data") or "D:/chanlun_pro/bt_data_fund_all_a"),
                max_codes=args.pool_size,
                lookback_bars=_config_int(market_config.get("selection_lookback_bars"), 3),
                buy_classes=_config_buy_classes(market_config.get("selection_buy_classes")),
                require_three_systems=_config_bool(
                    market_config.get("selection_require_three_systems"),
                    True,
                ),
                fundamental_roe_ann_min=float(
                    market_config.get("fundamental_roe_ann_min") or 8.0
                ),
            )
        )
        codes = [
            code for code in merge_codes([c.code for c in selector.select()], holdings, args.market)
            if code != INDEX_CODE
        ]
    else:
        codes = load_universe(
            args.market,
            args.bt_data,
            pool_size=args.pool_size,
            explicit_codes=explicit_codes,
            extra_codes=holdings,
            chart_cache_dir=args.chart_cache,
        )
    if not codes:
        print("No monitor symbols found. Set --codes or prepare bt_data cache.")
        return 2
    args.max_pos = _resolve_monitor_max_pos(args.max_pos, len(codes))

    states, ex = build_states(
        codes,
        args.market,
        args.data_source,
        args.chart_cache,
        op_level=args.op_level,
        big_level=args.big_level,
        mid_level=args.mid_level,
    )
    names = build_names(codes, ex)
    dingtalk_webhook = str(
        getattr(args, "dingtalk_webhook", "")
        or market_config.get("dingtalk_webhook")
        or ""
    )
    if dingtalk_webhook:
        # 买卖点通知专用钉钉机器人(自定义关键词通道),优先于 Claude hook
        notifier = DingTalkWebhookNotifier(
            dingtalk_webhook,
            keyword=str(
                getattr(args, "dingtalk_keyword", "")
                or market_config.get("dingtalk_keyword")
                or ""
            ),
            dry_run=args.dry_run,
        )
    else:
        notifier = ClaudeHookNotifier(
            command=args.hook_command,
            settings_path=args.settings_path,
            cwd=os.getcwd(),
            dry_run=args.dry_run,
        )
    send_runtime_override_notice(
        notifier,
        args.title,
        market_config.get("_runtime_override_event"),
    )
    deduper = JsonDeduper(args.state_file)
    broker = (
        PaperBroker(args.ledger, market=args.market, max_pos=args.max_pos)
        if args.paper_enabled
        else None
    )

    warmup = (not args.once) if args.warmup is None else args.warmup
    if warmup:
        print(f"Warmup {len(states)} symbols; current latest signals are registered only.")
        warmup_states(states)

    if args.once:
        if args.force or market_is_open(ex, args.market, _dt.datetime.now()):
            return 0 if run_once(args, states, notifier, deduper, names, broker, exchange=ex) >= 0 else 1
        print("Outside trading session; use --force to scan anyway.")
        return 0

    print(
        f"Realtime monitor started: market={args.market} "
        f"levels={args.big_level}"
        f"{'+' + args.mid_level if args.mid_level else ''}+{args.op_level} "
        f"symbols={len(states)} state={args.state_file}"
    )
    # 永久保留集只含手工自选(holdings 由 rescan 动态并入),不含 selection 选出的池——
    # selection 候选应可被每日 rescan 淘汰,避免启动时大池全集常驻数月。
    if selector is not None:
        # selection 模式:仅显式 --codes 与持仓永久保留,selector 选出的可淘汰
        initial_codes = set(explicit_codes or ())
    else:
        # 非 selection 模式(显式 codes / load_universe):codes 即用户指定,全部保留
        initial_codes = set(codes)
    last_rescan_date = _dt.date.today()  # 启动时已选过一轮
    while True:
        now = _dt.datetime.now()
        if (
            selector is not None
            and now.date() != last_rescan_date
            and now.time() >= _dt.time(9, 0)
        ):
            # 每日开盘前重选(静态缓存日级口径):新候选入池 warmup,旧候选淘汰
            rescan_selection_pool(
                selector, states, names, ex, args, initial_codes=initial_codes
            )
            last_rescan_date = now.date()
        if args.force or market_is_open(ex, args.market, now):
            run_once(args, states, notifier, deduper, names, broker, exchange=ex)
        time.sleep(_next_level_seconds(now, args.op_level))


if __name__ == "__main__":
    raise SystemExit(main())
