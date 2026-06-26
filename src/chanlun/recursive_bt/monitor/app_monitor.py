"""APScheduler integration for recursive live buy/sell point alerts.

The command-line monitor is intentionally static: it resolves symbols once and
then loops.  The web app needs a dynamic variant so watchlist edits are picked
up without restarting app.py.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import os
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from apscheduler.triggers.interval import IntervalTrigger

from chanlun import config as app_config
from chanlun import fun
from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.notifications import ClaudeHookNotifier, DingTalkWebhookNotifier
from chanlun.recursive_bt.engine.engine import recommended_buy_ratio
from chanlun.recursive_bt.select.chanlun_selector import (
    ASelectionConfig,
    OriginalChanlunASelector,
    SelectionCandidate,
)
from chanlun.recursive_bt.monitor.live_monitor import (
    JsonDeduper,
    MonitorEvent,
    MonitorSymbolState,
    apply_bs_point_ratio_multiplier,
    build_names,
    collect_monitor_events,
    fresh_monitor_events,
    load_ledger_positions,
    market_is_open,
    refresh_optimization_report,
    send_events,
    send_runtime_override_notice,
    _apply_runtime_overrides,
)
from chanlun.recursive_bt.engine.market_runtime import (
    CHART_CACHE_DIR,
    default_ledger_path,
    default_monitor_state_path,
    merge_codes,
    normalize_code,
    normalize_market,
)
from chanlun.recursive_bt.sim.paper import BT_DATA, MAX_POS, PaperBroker
from chanlun.zixuan import ZiXuan


WATCH_GROUP = "\u6211\u7684\u5173\u6ce8"
AUTO_SELECT_GROUP = "\u81ea\u52a8\u9009\u80a1"
INDEX_CODE = "SH.000001"


def _config_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _config_int(value, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_tuple(value, default: Iterable[str] = ()) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())
    try:
        return tuple(str(item).strip() for item in value if str(item).strip())
    except TypeError:
        return tuple(default)


def _buy_classes(value, default: Iterable[int] = (3, 2, 1)) -> tuple[int, ...]:
    raw = _config_tuple(value, default=(str(item) for item in default))
    classes: list[int] = []
    for item in raw:
        try:
            cls = int(item[0])
        except Exception:
            continue
        if cls in (1, 2, 3) and cls not in classes:
            classes.append(cls)
    return tuple(classes) or tuple(default)


def _recursive_monitor_config() -> dict:
    raw = getattr(app_config, "RECURSIVE_MONITOR_CONFIG", {})
    return raw if isinstance(raw, dict) else {}


def _market_settings(market: str) -> dict:
    root = _recursive_monitor_config()
    common = root.get("common") or {}
    specific = root.get(market) or {}
    settings = dict(common if isinstance(common, dict) else {})
    if isinstance(specific, dict):
        settings.update(specific)
    return _apply_runtime_overrides(settings, market)


def _config_path(value, default: Optional[str]) -> Optional[str]:
    if value is None:
        return default
    value = str(value).strip()
    return value or None


@dataclass
class DynamicMonitorConfig:
    market: str
    interval_seconds: int = 300
    start_delay_seconds: int = 10
    zx_groups: tuple[str, ...] = (WATCH_GROUP,)
    selection_groups: tuple[str, ...] = ()
    static_codes: tuple[str, ...] = ()
    include_a_selection_pool: bool = False
    a_selection_scan_limit: int = 0
    a_selection_max_codes: int = MAX_POS * 5
    a_selection_lookback_bars: int = 3
    a_selection_buy_classes: tuple[int, ...] = (3, 2, 1)
    a_selection_require_three_systems: bool = True
    a_selection_fund_data: str = "D:/chanlun_pro/bt_data_fund_all_a"
    a_selection_fundamental_roe_ann_min: float = 8.0
    bt_data: str = BT_DATA
    chart_cache_dir: str = CHART_CACHE_DIR
    ledger: Optional[str] = None
    state_file: Optional[str] = None
    max_pos: int = MAX_POS
    op_level: str = "5m"
    big_level: str = "30m"
    mid_level: str = ""
    regime_mode: str = "off"
    mid_gate: str = "strict"
    require_nest: bool = False
    nest_mode: str = "off"
    trend_3boost: bool = False
    sell_scope: str = "all"
    force: bool = False
    dry_run: bool = False
    dingtalk_webhook: str = ""
    dingtalk_keyword: str = ""
    paper_enabled: bool = False
    optimization_report_enabled: bool = False
    optimization_report_json: str = "D:/chanlun_pro/reports/strategy_optimization_report.json"
    optimization_report_markdown: str = "D:/chanlun_pro/reports/strategy_optimization_report.md"
    optimization_decision_json: str = "D:/chanlun_pro/reports/strategy_decision.json"
    optimization_decision_state_json: str = "D:/chanlun_pro/reports/strategy_decision_state.json"
    optimization_runtime_overrides_json: str = "D:/chanlun_pro/reports/strategy_runtime_overrides.json"
    strategy_attribution_json: str = "D:/chanlun_pro/reports/strategy_attribution_report.json"
    strategy_attribution_markdown: str = "D:/chanlun_pro/reports/strategy_attribution_report.md"
    bs_point_attribution_json: str = "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json"
    bs_point_attribution_markdown: str = "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.md"
    bs_point_ratio_state_json: str = "D:/chanlun_pro/reports/strategy_bs_point_ratio_state.json"
    bs_point_ratio_overrides_json: str = "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
    bs_point_regime_json: str = "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.json"
    bs_point_regime_markdown: str = "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md"
    bs_point_regime_policy_json: str = "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json"
    bs_point_regime_policy_markdown: str = "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.md"
    bs_point_regime_policy_min_trades: int = 30
    bs_point_ratio_confirmation_threshold: int = 3
    bs_point_ratio_multipliers: Optional[dict] = None
    runtime_override_audit_jsonl: str = "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl"
    optimization_report_dir: str = "D:/chanlun_pro/reports"
    optimization_report_include_discovered: bool = True
    decision_confirmation_threshold: int = 3
    runtime_overrides_enabled: bool = True
    runtime_override_event: Optional[dict] = None
    warmup_new_symbols: bool = True
    title: Optional[str] = None

    @classmethod
    def from_config(cls, market: str) -> "DynamicMonitorConfig":
        market = normalize_market(market)
        settings = _market_settings(market)
        zx_groups = _config_tuple(settings.get("zx_groups"), default=(WATCH_GROUP,))
        selection_groups = _config_tuple(
            settings.get("selection_groups"),
            default=(AUTO_SELECT_GROUP,) if market == "a" else (),
        )
        include_a_selection = (
            market == "a"
            and _config_bool(settings.get("enable_selection_pool"), True)
        )
        return cls(
            market=market,
            interval_seconds=_config_int(settings.get("interval_seconds"), 300),
            start_delay_seconds=_config_int(settings.get("start_delay_seconds"), 10),
            zx_groups=zx_groups,
            selection_groups=selection_groups,
            static_codes=_config_tuple(
                settings.get("codes", settings.get("static_codes")),
                default=(),
            ),
            include_a_selection_pool=include_a_selection,
            a_selection_scan_limit=_config_int(settings.get("selection_scan_limit"), 0),
            a_selection_max_codes=_config_int(
                settings.get("selection_max_codes"),
                MAX_POS * 5,
            ),
            a_selection_lookback_bars=_config_int(
                settings.get("selection_lookback_bars"),
                3,
            ),
            a_selection_buy_classes=_buy_classes(
                settings.get("selection_buy_classes"),
                default=(3, 2, 1),
            ),
            a_selection_require_three_systems=_config_bool(
                settings.get("selection_require_three_systems"),
                True,
            ),
            a_selection_fund_data=str(
                settings.get("fund_data") or "D:/chanlun_pro/bt_data_fund_all_a"
            ),
            a_selection_fundamental_roe_ann_min=float(
                settings.get("fundamental_roe_ann_min") or 8.0
            ),
            bt_data=str(settings.get("bt_data") or BT_DATA),
            chart_cache_dir=str(settings.get("chart_cache_dir") or CHART_CACHE_DIR),
            ledger=_config_path(settings.get("ledger"), default_ledger_path(market)),
            state_file=_config_path(
                settings.get("state_file"),
                default_monitor_state_path(market),
            ),
            max_pos=_config_int(settings.get("max_pos"), MAX_POS),
            op_level=str(settings.get("op_level") or "5m"),
            big_level=str(settings.get("big_level") or "30m"),
            mid_level=str(settings.get("mid_level") or ""),
            regime_mode=str(settings.get("regime_mode") or "off"),
            mid_gate=str(settings.get("mid_gate") or "strict"),
            require_nest=_config_bool(settings.get("require_nest"), False),
            nest_mode=str(settings.get("nest_mode") or "off"),
            trend_3boost=_config_bool(settings.get("trend_3boost"), False),
            sell_scope=str(settings.get("sell_scope") or "all"),
            force=_config_bool(settings.get("force"), False),
            dry_run=_config_bool(settings.get("dry_run"), False),
            dingtalk_webhook=str(settings.get("dingtalk_webhook") or ""),
            dingtalk_keyword=str(settings.get("dingtalk_keyword") or ""),
            paper_enabled=_config_bool(settings.get("paper_enabled"), False),
            optimization_report_enabled=_config_bool(
                settings.get("optimization_report_enabled"),
                False,
            ),
            optimization_report_json=str(
                settings.get("optimization_report_json")
                or "D:/chanlun_pro/reports/strategy_optimization_report.json"
            ),
            optimization_report_markdown=str(
                settings.get("optimization_report_markdown")
                or "D:/chanlun_pro/reports/strategy_optimization_report.md"
            ),
            optimization_decision_json=str(
                settings.get("optimization_decision_json")
                or "D:/chanlun_pro/reports/strategy_decision.json"
            ),
            optimization_decision_state_json=str(
                settings.get("optimization_decision_state_json")
                or "D:/chanlun_pro/reports/strategy_decision_state.json"
            ),
            optimization_runtime_overrides_json=str(
                settings.get("optimization_runtime_overrides_json")
                or "D:/chanlun_pro/reports/strategy_runtime_overrides.json"
            ),
            strategy_attribution_json=str(
                settings.get("strategy_attribution_json")
                or "D:/chanlun_pro/reports/strategy_attribution_report.json"
            ),
            strategy_attribution_markdown=str(
                settings.get("strategy_attribution_markdown")
                or "D:/chanlun_pro/reports/strategy_attribution_report.md"
            ),
            bs_point_attribution_json=str(
                settings.get("bs_point_attribution_json")
                or "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json"
            ),
            bs_point_attribution_markdown=str(
                settings.get("bs_point_attribution_markdown")
                or "D:/chanlun_pro/reports/strategy_bs_point_attribution_report.md"
            ),
            bs_point_ratio_state_json=str(
                settings.get("bs_point_ratio_state_json")
                or "D:/chanlun_pro/reports/strategy_bs_point_ratio_state.json"
            ),
            bs_point_ratio_overrides_json=str(
                settings.get("bs_point_ratio_overrides_json")
                or "D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json"
            ),
            bs_point_regime_json=str(
                settings.get("bs_point_regime_json")
                or "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.json"
            ),
            bs_point_regime_markdown=str(
                settings.get("bs_point_regime_markdown")
                or "D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md"
            ),
            bs_point_regime_policy_json=str(
                settings.get("bs_point_regime_policy_json")
                or "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json"
            ),
            bs_point_regime_policy_markdown=str(
                settings.get("bs_point_regime_policy_markdown")
                or "D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.md"
            ),
            bs_point_regime_policy_min_trades=_config_int(
                settings.get("bs_point_regime_policy_min_trades"),
                30,
            ),
            bs_point_ratio_confirmation_threshold=_config_int(
                settings.get("bs_point_ratio_confirmation_threshold"),
                3,
            ),
            bs_point_ratio_multipliers=dict(
                settings.get("bs_point_ratio_multipliers") or {}
            ),
            runtime_override_audit_jsonl=str(
                settings.get("runtime_override_audit_jsonl")
                or "D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl"
            ),
            optimization_report_dir=str(
                settings.get("optimization_report_dir")
                or "D:/chanlun_pro/reports"
            ),
            optimization_report_include_discovered=_config_bool(
                settings.get("optimization_report_include_discovered"),
                True,
            ),
            decision_confirmation_threshold=_config_int(
                settings.get("decision_confirmation_threshold"),
                3,
            ),
            runtime_overrides_enabled=_config_bool(
                settings.get("runtime_overrides_enabled"),
                True,
            ),
            runtime_override_event=settings.get("_runtime_override_event"),
            warmup_new_symbols=_config_bool(settings.get("warmup_new_symbols"), True),
            title=settings.get("title"),
        )


class DynamicRecursiveMonitor:
    """A scheduler-friendly monitor whose universe is resolved on every scan."""

    def __init__(
        self,
        config: DynamicMonitorConfig,
        *,
        zixuan_factory: Callable[[str], ZiXuan] = ZiXuan,
        exchange_factory: Callable[[Market], object] = get_exchange,
        state_factory: Callable[..., object] = MonitorSymbolState,
        a_selector_factory: Callable[[ASelectionConfig], object] = OriginalChanlunASelector,
        paper_broker_factory: Callable[..., PaperBroker] = PaperBroker,
        notifier: Optional[ClaudeHookNotifier] = None,
        deduper: Optional[JsonDeduper] = None,
    ) -> None:
        self.config = config
        self.market = normalize_market(config.market)
        self._zixuan_factory = zixuan_factory
        self._exchange_factory = exchange_factory
        self._state_factory = state_factory
        self._state_factory_supports_levels = self._supports_level_kwargs(state_factory)
        self._a_selector_factory = a_selector_factory
        self._paper_broker_factory = paper_broker_factory
        self._paper_broker: Optional[PaperBroker] = None
        self._runtime_override_notified = False
        self._lock = threading.Lock()
        self._ex = None
        self.states: dict[str, object] = {}
        self.names: dict[str, str] = {}
        self.last_selection_candidates: list[SelectionCandidate] = []
        if notifier is not None:
            self.notifier = notifier
        elif config.dingtalk_webhook:
            # 买卖点通知专用钉钉机器人(自定义关键词通道),优先于 Claude hook
            self.notifier = DingTalkWebhookNotifier(
                config.dingtalk_webhook,
                keyword=config.dingtalk_keyword,
                dry_run=config.dry_run,
            )
        else:
            self.notifier = ClaudeHookNotifier(
                cwd=os.getcwd(),
                dry_run=config.dry_run,
            )
        self.deduper = deduper or JsonDeduper(config.state_file)
        self.log = fun.get_logger()

    def _exchange(self):
        if self._ex is None:
            self._ex = self._exchange_factory(Market(self.market))
        return self._ex

    def _broker(self) -> Optional[PaperBroker]:
        if not (self.config.paper_enabled and self.config.ledger):
            return None
        if self._paper_broker is None:
            self._paper_broker = self._paper_broker_factory(
                self.config.ledger,
                market=self.market,
                max_pos=self.config.max_pos,
            )
        return self._paper_broker

    @staticmethod
    def _supports_level_kwargs(factory: Callable[..., object]) -> bool:
        try:
            params = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            return True
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
            return True
        return {"op_level", "big_level", "mid_level"}.issubset(params)

    def _new_state(self, code: str, ex):
        if not self._state_factory_supports_levels:
            return self._state_factory(code, ex)
        return self._state_factory(
            code,
            ex,
            op_level=self.config.op_level,
            big_level=self.config.big_level,
            mid_level=self.config.mid_level or None,
        )

    def _zixuan_codes(self, groups: Iterable[str]) -> tuple[list[str], dict[str, str]]:
        zx = self._zixuan_factory(self.market)
        codes: list[str] = []
        names: dict[str, str] = {}
        for group in groups:
            try:
                stocks = zx.zx_stocks(group)
            except Exception as exc:
                self.log.warning(
                    f"[recursive_app_monitor] read watchlist failed "
                    f"market={self.market} group={group} err={exc}"
                )
                continue
            for stock in stocks:
                code = normalize_code(self.market, stock.get("code", ""))
                if not code:
                    continue
                codes.append(code)
                names[code] = stock.get("name") or code
        return codes, names

    def _a_selection_candidates(self) -> list[SelectionCandidate]:
        if not (self.market == "a" and self.config.include_a_selection_pool):
            return []
        cfg = ASelectionConfig(
            bt_data=self.config.bt_data,
            fund_data=self.config.a_selection_fund_data,
            scan_limit=self.config.a_selection_scan_limit,
            max_codes=self.config.a_selection_max_codes,
            lookback_bars=self.config.a_selection_lookback_bars,
            buy_classes=self.config.a_selection_buy_classes,
            require_three_systems=self.config.a_selection_require_three_systems,
            fundamental_roe_ann_min=self.config.a_selection_fundamental_roe_ann_min,
        )
        try:
            selector = self._a_selector_factory(cfg)
            return list(selector.select())
        except Exception as exc:
            self.log.warning(f"[recursive_app_monitor] A selector failed: {exc}")
            return []

    def current_universe(self) -> tuple[list[str], dict[str, str]]:
        codes = list(self.config.static_codes)
        names: dict[str, str] = {}
        watch_codes, watch_names = self._zixuan_codes(self.config.zx_groups)
        codes.extend(watch_codes)
        names.update(watch_names)
        selected, selected_names = self._zixuan_codes(self.config.selection_groups)
        codes.extend(selected)
        names.update(selected_names)
        self.last_selection_candidates = self._a_selection_candidates()
        for candidate in self.last_selection_candidates:
            code = normalize_code(self.market, candidate.code)
            codes.append(code)
            if candidate.name:
                names.setdefault(code, candidate.name)
        if self.config.ledger:
            codes.extend(load_ledger_positions(self.config.ledger))
        codes = [code for code in merge_codes(codes, (), self.market) if code != INDEX_CODE]
        return codes, names

    def _selection_events(self, holdings: set[str]) -> list[MonitorEvent]:
        if self.market != "a" or not self.last_selection_candidates:
            return []
        if self.config.op_level != "5m":
            return []
        free = max(self.config.max_pos - len(holdings), 0) if self.config.max_pos > 0 else None
        events: list[MonitorEvent] = []
        for candidate in self.last_selection_candidates:
            code = normalize_code(self.market, candidate.code)
            if code in holdings:
                continue
            buy_ratio = recommended_buy_ratio(
                candidate.bs_type,
                max_pos=self.config.max_pos,
                big_dir=candidate.big_dir,
                nest_mode=self.config.nest_mode,
                trend_boost=self.config.trend_3boost,
            )
            buy_ratio, applied_multiplier = apply_bs_point_ratio_multiplier(
                buy_ratio,
                candidate.bs_type,
                self.config.bs_point_ratio_multipliers,
            )
            reason = "three systems + 5m buy point + 30m not_down"
            if applied_multiplier != 1.0:
                reason += f" + bs{candidate.buy_class}_ratio_x{applied_multiplier:.2f}"
            events.append(
                MonitorEvent(
                    code=code,
                    name=self.names.get(code, candidate.name or code),
                    side="buy",
                    kind="small_buy",
                    bs_type=candidate.bs_type,
                    signal_time=candidate.signal_time,
                    price=candidate.price,
                    big_dir=candidate.big_dir,
                    reason=reason,
                    buy_ratio=buy_ratio,
                )
            )
            if free is not None and len(events) >= free:
                break
        return events

    def _sync_states(self, codes: list[str], names: dict[str, str]) -> None:
        desired = set(codes)
        for code in list(self.states.keys()):
            if code not in desired:
                self.states.pop(code, None)
                self.names.pop(code, None)

        new_codes = []
        ex = self._exchange()
        for code in codes:
            if code not in self.states:
                self.states[code] = self._new_state(code, ex)
                new_codes.append(code)

        self.names.update(names)
        missing = [code for code in codes if code not in self.names]
        if missing:
            self.names.update(build_names(missing, ex))

        if self.config.warmup_new_symbols:
            for code in new_codes:
                try:
                    self.states[code].refresh()
                except Exception as exc:
                    self.log.warning(
                        f"[recursive_app_monitor] warmup failed "
                        f"market={self.market} code={code} err={exc}"
                    )

    def run_once(self) -> dict:
        with self._lock:
            ex = self._exchange()
            now = _dt.datetime.now()
            if not (self.config.force or market_is_open(ex, self.market, now)):
                return {"market": self.market, "skipped": True, "reason": "market_closed"}

            codes, names = self.current_universe()
            self._sync_states(codes, names)
            if not self.states:
                return {"market": self.market, "scan": 0, "events": 0, "sent": 0}

            holdings = load_ledger_positions(self.config.ledger) if self.config.ledger else set()
            events = collect_monitor_events(
                self.states,
                names=self.names,
                holdings=holdings,
                max_pos=self.config.max_pos,
                sell_scope=self.config.sell_scope,
                regime_mode=self.config.regime_mode,
                mid_gate=self.config.mid_gate,
                require_nest=self.config.require_nest,
                nest_mode=self.config.nest_mode,
                trend_3boost=self.config.trend_3boost,
                bs_point_ratio_multipliers=self.config.bs_point_ratio_multipliers,
            )
            events = self._selection_events(holdings) + events
            fresh = fresh_monitor_events(events, self.deduper)
            title = self.config.title or (
                f"\u7f20\u8bba\u5b9e\u65f6\u4e70\u5356\u70b9\u63d0\u9192({self.market})"
            )
            runtime_override_notice_sent = False
            if self.config.runtime_override_event and not self._runtime_override_notified:
                runtime_override_notice_sent = send_runtime_override_notice(
                    self.notifier,
                    title,
                    self.config.runtime_override_event,
                )
                self._runtime_override_notified = True
            sent = send_events(events, self.notifier, self.deduper, title)
            broker = self._broker()
            paper_queued = 0
            paper_snapshot = None
            paper_summary = None
            if broker is not None:
                now = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
                broker.fill_pending(
                    self.states,
                    now,
                )
                # 审计 D1b-HIGH-1: 下单与通知解耦——queue_events 无条件执行(含止损/退出卖单),
                # 通知失败只记 warning 不阻断下单;queue_events 自带 pending/positions 幂等。
                paper_queued = broker.queue_events(fresh)
                paper_snapshot = broker.record_snapshot(self.states, now)
                paper_summary = broker.performance_summary()
                broker.save()
            paper_equity = paper_snapshot["equity"] if paper_snapshot else None
            paper_return = paper_summary["total_return"] if paper_summary else None
            paper_max_drawdown = (
                paper_summary["max_drawdown"] if paper_summary else None
            )
            optimization_report = None
            if self.config.optimization_report_enabled:
                optimization_report = refresh_optimization_report(
                    output_json=self.config.optimization_report_json,
                    output_markdown=self.config.optimization_report_markdown,
                    output_decision=self.config.optimization_decision_json,
                    output_decision_state=self.config.optimization_decision_state_json,
                    output_runtime_overrides=self.config.optimization_runtime_overrides_json,
                    output_attribution_json=self.config.strategy_attribution_json,
                    output_attribution_markdown=self.config.strategy_attribution_markdown,
                    output_bs_point_json=self.config.bs_point_attribution_json,
                    output_bs_point_markdown=self.config.bs_point_attribution_markdown,
                    output_bs_point_ratio_state=self.config.bs_point_ratio_state_json,
                    output_bs_point_ratio_overrides=self.config.bs_point_ratio_overrides_json,
                    output_bs_point_regime_json=self.config.bs_point_regime_json,
                    output_bs_point_regime_markdown=self.config.bs_point_regime_markdown,
                    output_bs_point_regime_policy_json=self.config.bs_point_regime_policy_json,
                    output_bs_point_regime_policy_markdown=self.config.bs_point_regime_policy_markdown,
                    bs_point_regime_policy_min_trades=self.config.bs_point_regime_policy_min_trades,
                    runtime_override_audit_jsonl=self.config.runtime_override_audit_jsonl,
                    market=None,
                    include_discovered=self.config.optimization_report_include_discovered,
                    report_dir=self.config.optimization_report_dir,
                    current_config=dict(self.config.__dict__),
                    decision_confirmation_threshold=self.config.decision_confirmation_threshold,
                    bs_point_ratio_confirmation_threshold=self.config.bs_point_ratio_confirmation_threshold,
                )
            self.log.info(
                f"[recursive_app_monitor] market={self.market} scan={len(self.states)} "
                f"holdings={len(holdings)} events={len(events)} sent={sent} "
                f"paper_queued={paper_queued} paper_equity={paper_equity}"
            )
            return {
                "market": self.market,
                "scan": len(self.states),
                "holdings": len(holdings),
                "events": len(events),
                "sent": sent,
                "paper_queued": paper_queued,
                "paper_equity": paper_equity,
                "paper_return": paper_return,
                "paper_max_drawdown": paper_max_drawdown,
                "runtime_override_notice_sent": runtime_override_notice_sent,
                "optimization_report": optimization_report,
            }


def register_recursive_monitor_jobs(scheduler) -> dict[str, DynamicRecursiveMonitor]:
    """Register A-share and US recursive live monitor jobs in the web scheduler."""
    root_config = _recursive_monitor_config()
    # 默认改为 opt-in:避免与常驻 CLI live_monitor 双跑同一 ledger/state 互相覆盖写。
    # 需双跑请显式在 config 设 RECURSIVE_MONITOR_CONFIG['enabled']=True。
    if not _config_bool(root_config.get("enabled"), False):   # 默认 True→False
        fun.get_logger().info(
            "[recursive_app_monitor] disabled by default (set enabled=True to opt in; "
            "ensure it does not share ledger/state with a standalone live_monitor)"
        )
        return {}

    monitors: dict[str, DynamicRecursiveMonitor] = {}
    now = _dt.datetime.now()
    # 专用线程池:缠论重算 CPU 密集,放 IOLoop 会卡 web。两市场串行(max_workers=1)
    # 即可,既隔离阻塞又不与图表请求争 CPU 过度。
    executor_alias = "recursive_monitor"
    try:
        from apscheduler.executors.pool import ThreadPoolExecutor as _APThreadPool
        if "recursive_monitor" not in getattr(scheduler, "_executors", {}):
            scheduler.add_executor(_APThreadPool(max_workers=1), alias="recursive_monitor")
    except Exception as exc:
        fun.get_logger().warning(
            f"[recursive_app_monitor] dedicated executor unavailable, "
            f"falling back to default: {exc}"
        )
        # 回退:不指定 executor(下面 add_job 不传 executor 参数,用默认 executor)。
        executor_alias = None
    markets = _config_tuple(root_config.get("markets"), default=("a", "us"))
    for idx, market in enumerate(markets):
        market = normalize_market(market)
        settings = _market_settings(market)
        if not _config_bool(settings.get("enabled"), True):
            continue
        cfg = DynamicMonitorConfig.from_config(market)
        monitor = DynamicRecursiveMonitor(cfg)
        job_kwargs = {}
        if executor_alias is not None:
            job_kwargs["executor"] = executor_alias   # 专用线程池,避免阻塞 Tornado IOLoop
        scheduler.add_job(
            func=monitor.run_once,
            trigger=IntervalTrigger(seconds=max(cfg.interval_seconds, 30)),
            id=f"recursive_live_monitor_{market}",
            name=f"recursive-live-monitor-{market}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
            next_run_time=now + _dt.timedelta(seconds=cfg.start_delay_seconds + idx * 5),
            **job_kwargs,
        )
        monitors[market] = monitor

    setattr(scheduler, "recursive_live_monitors", monitors)
    fun.get_logger().info(
        f"[recursive_app_monitor] registered markets={','.join(monitors) or '-'}"
    )
    return monitors
