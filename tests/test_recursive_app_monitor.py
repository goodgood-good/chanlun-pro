import json

from chanlun import config as app_config
from chanlun.recursive_bt.app_monitor import (
    DynamicMonitorConfig,
    DynamicRecursiveMonitor,
    WATCH_GROUP,
    register_recursive_monitor_jobs,
)
from chanlun.recursive_bt.chanlun_selector import SelectionCandidate
from chanlun.recursive_bt.live_monitor import JsonDeduper
from chanlun.notifications import ClaudeHookNotifier


class FakeExchange:
    def now_trading(self):
        return True

    def stock_info(self, code):
        return {"name": code}


class FakeState:
    def __init__(self, code, _ex):
        self.code = code
        self.refresh_count = 0

    def refresh(self):
        self.refresh_count += 1
        return []

    def big_dir(self):
        return "neutral"


class FakeZiXuan:
    def __init__(self, stocks_by_group):
        self.stocks_by_group = stocks_by_group

    def zx_stocks(self, group):
        return self.stocks_by_group.get(group, [])


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, **kwargs):
        self.jobs.append(kwargs)


class FakeNotifier:
    def __init__(self):
        self.messages = []

    def send(self, title, lines):
        self.messages.append((title, lines))
        return True


class FakeSelector:
    def __init__(self, candidates):
        self.candidates = candidates

    def select(self):
        return self.candidates


class FakeSignal:
    def __init__(self, bs_type="3buy", price=10.0):
        self.bs_type = bs_type
        self.date = "2026-06-10 10:00:00"
        self.price = price
        self.is_buy = bs_type.endswith("buy")
        self.is_sell = bs_type.endswith("sell")


def test_default_recursive_monitor_config_tracks_latest_live_candidates():
    a_cfg = app_config.RECURSIVE_MONITOR_CONFIG["a"]

    assert a_cfg["bt_data"].endswith("bt_data_all_a")
    assert a_cfg["fund_data"].endswith("bt_data_fund_all_a")
    assert a_cfg["selection_scan_limit"] == 0
    assert a_cfg["selection_max_codes"] >= a_cfg["max_pos"] * 3
    assert a_cfg["max_pos"] == 30
    assert a_cfg["op_level"] == "1m"
    assert a_cfg["mid_level"] == "5m"
    assert a_cfg["big_level"] == "30m"
    assert a_cfg["mid_gate"] == "soft"
    assert a_cfg.get("nest_mode", "off") == "off"
    assert a_cfg.get("trend_3boost", False) is False
    assert a_cfg["paper_enabled"] is True
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["optimization_report_enabled"] is True
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["optimization_decision_json"].endswith(
        "strategy_decision.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["optimization_decision_state_json"].endswith(
        "strategy_decision_state.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["optimization_runtime_overrides_json"].endswith(
        "strategy_runtime_overrides.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["runtime_override_audit_jsonl"].endswith(
        "strategy_runtime_override_audit.jsonl"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["strategy_attribution_json"].endswith(
        "strategy_attribution_report.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["strategy_attribution_markdown"].endswith(
        "strategy_attribution_report.md"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_attribution_json"].endswith(
        "strategy_bs_point_attribution_report.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_attribution_markdown"].endswith(
        "strategy_bs_point_attribution_report.md"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_ratio_state_json"].endswith(
        "strategy_bs_point_ratio_state.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_ratio_overrides_json"].endswith(
        "strategy_bs_point_ratio_overrides.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_regime_json"].endswith(
        "strategy_bs_point_regime_attribution_report.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_regime_markdown"].endswith(
        "strategy_bs_point_regime_attribution_report.md"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_regime_policy_json"].endswith(
        "strategy_bs_point_regime_policy_report.json"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_regime_policy_markdown"].endswith(
        "strategy_bs_point_regime_policy_report.md"
    )
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_regime_policy_min_trades"] == 30
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["bs_point_ratio_confirmation_threshold"] == 3
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["decision_confirmation_threshold"] == 3
    assert app_config.RECURSIVE_MONITOR_CONFIG["common"]["runtime_overrides_enabled"] is True

    us_cfg = app_config.RECURSIVE_MONITOR_CONFIG["us"]
    assert us_cfg["max_pos"] == 9
    assert us_cfg["op_level"] == "1m"
    assert us_cfg["mid_level"] == "5m"
    assert us_cfg["big_level"] == "30m"
    assert us_cfg["mid_gate"] == "soft"
    assert us_cfg["nest_mode"] == "soft"
    assert us_cfg["trend_3boost"] is True
    assert us_cfg["paper_enabled"] is True


def test_dynamic_monitor_follows_watchlist_changes(tmp_path):
    stocks = {
        WATCH_GROUP: [
            {"code": "TSLA.US", "name": "Tesla"},
            {"code": "AAPL", "name": "Apple"},
        ]
    }
    cfg = DynamicMonitorConfig(
        market="us",
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan(stocks),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        notifier=ClaudeHookNotifier(dry_run=True),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    monitor.run_once()
    assert sorted(monitor.states) == ["AAPL.US", "TSLA.US"]

    stocks[WATCH_GROUP] = [{"code": "MSFT", "name": "Microsoft"}]
    monitor.run_once()
    assert sorted(monitor.states) == ["MSFT.US"]


def test_dynamic_monitor_includes_static_codes(tmp_path):
    cfg = DynamicMonitorConfig(
        market="us",
        static_codes=("SPY", "QQQ.US"),
        zx_groups=(),
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        notifier=ClaudeHookNotifier(dry_run=True),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    codes, _names = monitor.current_universe()

    assert codes == ["SPY.US", "QQQ.US"]


def test_a_monitor_includes_selection_pool(tmp_path):
    candidates = [
        SelectionCandidate(
            code="SH.600000",
            name="Pudong Bank",
            bs_type="3buy",
            signal_time="2026-06-10 10:00:00",
            price=10.0,
            big_dir="up",
            signal_index=120,
        ),
        SelectionCandidate(
            code="SZ.000001",
            name="Ping An Bank",
            bs_type="2buy",
            signal_time="2026-06-10 10:00:00",
            price=11.0,
            big_dir="neutral",
            signal_index=120,
        ),
    ]

    cfg = DynamicMonitorConfig(
        market="a",
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        include_a_selection_pool=True,
        max_pos=10,
        nest_mode="soft",
        trend_3boost=True,
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({WATCH_GROUP: []}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        a_selector_factory=lambda _cfg: FakeSelector(candidates),
        notifier=ClaudeHookNotifier(dry_run=True),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    codes, _names = monitor.current_universe()
    assert codes == ["SH.600000", "SZ.000001"]


def test_a_monitor_sends_selector_buy_event(tmp_path):
    candidates = [
        SelectionCandidate(
            code="SH.600000",
            name="Pudong Bank",
            bs_type="3buy",
            signal_time="2026-06-10 10:00:00",
            price=10.0,
            big_dir="up",
            signal_index=120,
        )
    ]
    notifier = FakeNotifier()
    cfg = DynamicMonitorConfig(
        market="a",
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        include_a_selection_pool=True,
        max_pos=10,
        trend_3boost=True,
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({WATCH_GROUP: []}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        a_selector_factory=lambda _cfg: FakeSelector(candidates),
        notifier=notifier,
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    result = monitor.run_once()

    assert result["sent"] == 1
    assert "SH.600000" in notifier.messages[0][1][0]
    assert "建议买入=12.5%" in notifier.messages[0][1][0]


def test_a_monitor_applies_bs_point_ratio_multiplier_to_selector_event(tmp_path):
    candidates = [
        SelectionCandidate(
            code="SH.600000",
            name="Pudong Bank",
            bs_type="3buy",
            signal_time="2026-06-10 10:00:00",
            price=10.0,
            big_dir="up",
            signal_index=120,
        )
    ]
    notifier = FakeNotifier()
    cfg = DynamicMonitorConfig(
        market="a",
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        include_a_selection_pool=True,
        max_pos=10,
        trend_3boost=True,
        bs_point_ratio_multipliers={"3": 1.1},
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({WATCH_GROUP: []}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        a_selector_factory=lambda _cfg: FakeSelector(candidates),
        notifier=notifier,
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    result = monitor.run_once()

    assert result["sent"] == 1
    assert "13.8%" in notifier.messages[0][1][0]
    assert "bs3_ratio_x1.10" in notifier.messages[0][1][0]


def test_a_monitor_uses_selector_as_universe_only_for_1m_entry(tmp_path):
    candidates = [
        SelectionCandidate(
            code="SH.600000",
            name="Pudong Bank",
            bs_type="3buy",
            signal_time="2026-06-10 10:00:00",
            price=10.0,
            big_dir="up",
            signal_index=120,
        )
    ]
    notifier = FakeNotifier()
    cfg = DynamicMonitorConfig(
        market="a",
        force=True,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        include_a_selection_pool=True,
        max_pos=10,
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({WATCH_GROUP: []}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        a_selector_factory=lambda _cfg: FakeSelector(candidates),
        notifier=notifier,
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    result = monitor.run_once()

    assert result["sent"] == 0
    assert "SH.600000" in monitor.states
    assert notifier.messages == []


def test_dynamic_monitor_passes_configured_levels_to_state_factory(tmp_path):
    created = []

    class LevelAwareState(FakeState):
        def __init__(
            self,
            code,
            ex,
            op_level="5m",
            big_level="30m",
            mid_level=None,
        ):
            super().__init__(code, ex)
            self.op_level = op_level
            self.big_level = big_level
            self.mid_level = mid_level or ""
            created.append((code, op_level, big_level, mid_level))

    cfg = DynamicMonitorConfig(
        market="a",
        force=True,
        static_codes=("SH.600000",),
        include_a_selection_pool=False,
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        dry_run=True,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({WATCH_GROUP: []}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=LevelAwareState,
        notifier=ClaudeHookNotifier(dry_run=True),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    result = monitor.run_once()

    assert result["scan"] == 1
    assert created == [("SH.600000", "1m", "30m", "5m")]


def test_dynamic_monitor_paper_executes_fresh_events_to_ledger(tmp_path):
    class BuyOnceState(FakeState):
        def __init__(self, code, ex):
            super().__init__(code, ex)
            self.last_open = 10.0
            self.last_px = 10.0
            self.prev_close = 9.9

        def refresh(self):
            self.refresh_count += 1
            self.last_open = 10.0
            self.last_px = 10.0
            self.prev_close = 9.9
            if self.refresh_count == 1:
                return [FakeSignal("3buy", price=10.0)]
            return []

        def big_dir(self):
            return "up"

    ledger = tmp_path / "ledger.json"
    cfg = DynamicMonitorConfig(
        market="us",
        force=True,
        static_codes=("AAPL.US",),
        zx_groups=(),
        state_file=str(tmp_path / "state.json"),
        ledger=str(ledger),
        max_pos=2,
        paper_enabled=True,
        dry_run=True,
        warmup_new_symbols=False,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=BuyOnceState,
        notifier=FakeNotifier(),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    first = monitor.run_once()
    data = json.loads(ledger.read_text(encoding="utf-8"))

    assert first["sent"] == 1
    assert first["paper_queued"] == 1
    assert first["paper_equity"] > 0
    assert first["paper_return"] == 0
    assert first["paper_max_drawdown"] == 0
    assert data["pending"][0]["code"] == "AAPL.US"
    assert data["pending"][0]["target_weight"] == 0.5
    assert len(data["equity_curve"]) == 1

    second = monitor.run_once()
    data = json.loads(ledger.read_text(encoding="utf-8"))

    assert second["paper_queued"] == 0
    assert second["paper_equity"] > 0
    assert data["pending"] == []
    assert data["positions"]["AAPL.US"]["shares"] > 0
    assert len(data["equity_curve"]) == 2
    assert data["summary"]["latest_equity"] == data["equity_curve"][-1]["equity"]


def test_dynamic_monitor_refreshes_optimization_report(tmp_path):
    report_json = tmp_path / "reports" / "strategy.json"
    report_md = tmp_path / "reports" / "strategy.md"
    decision_json = tmp_path / "reports" / "decision.json"
    decision_state_json = tmp_path / "reports" / "decision_state.json"
    runtime_overrides_json = tmp_path / "reports" / "runtime_overrides.json"
    attribution_json = tmp_path / "reports" / "attribution.json"
    attribution_md = tmp_path / "reports" / "attribution.md"
    bs_point_json = tmp_path / "reports" / "bs_point.json"
    bs_point_md = tmp_path / "reports" / "bs_point.md"
    bs_ratio_state_json = tmp_path / "reports" / "bs_ratio_state.json"
    bs_ratio_overrides_json = tmp_path / "reports" / "bs_ratio_overrides.json"
    bs_point_regime_json = tmp_path / "reports" / "bs_point_regime.json"
    bs_point_regime_md = tmp_path / "reports" / "bs_point_regime.md"
    bs_point_regime_policy_json = tmp_path / "reports" / "bs_point_regime_policy.json"
    bs_point_regime_policy_md = tmp_path / "reports" / "bs_point_regime_policy.md"
    cfg = DynamicMonitorConfig(
        market="us",
        force=True,
        static_codes=("AAPL.US",),
        zx_groups=(),
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        max_pos=9,
        op_level="1m",
        mid_level="5m",
        big_level="30m",
        mid_gate="soft",
        nest_mode="soft",
        trend_3boost=True,
        dry_run=True,
        optimization_report_enabled=True,
        optimization_report_json=str(report_json),
        optimization_report_markdown=str(report_md),
        optimization_decision_json=str(decision_json),
        optimization_decision_state_json=str(decision_state_json),
        optimization_runtime_overrides_json=str(runtime_overrides_json),
        strategy_attribution_json=str(attribution_json),
        strategy_attribution_markdown=str(attribution_md),
        bs_point_attribution_json=str(bs_point_json),
        bs_point_attribution_markdown=str(bs_point_md),
        bs_point_ratio_state_json=str(bs_ratio_state_json),
        bs_point_ratio_overrides_json=str(bs_ratio_overrides_json),
        bs_point_regime_json=str(bs_point_regime_json),
        bs_point_regime_markdown=str(bs_point_regime_md),
        bs_point_regime_policy_json=str(bs_point_regime_policy_json),
        bs_point_regime_policy_markdown=str(bs_point_regime_policy_md),
        bs_point_regime_policy_min_trades=30,
        optimization_report_dir=str(tmp_path / "empty_reports"),
        optimization_report_include_discovered=False,
        decision_confirmation_threshold=2,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        notifier=ClaudeHookNotifier(dry_run=True),
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    result = monitor.run_once()
    report = json.loads(report_json.read_text(encoding="utf-8"))
    decision = json.loads(decision_json.read_text(encoding="utf-8"))
    decision_state = json.loads(decision_state_json.read_text(encoding="utf-8"))
    runtime_overrides = json.loads(runtime_overrides_json.read_text(encoding="utf-8"))
    attribution = json.loads(attribution_json.read_text(encoding="utf-8"))
    bs_point = json.loads(bs_point_json.read_text(encoding="utf-8"))
    bs_ratio_overrides = json.loads(bs_ratio_overrides_json.read_text(encoding="utf-8"))
    bs_point_regime = json.loads(bs_point_regime_json.read_text(encoding="utf-8"))
    bs_point_regime_policy = json.loads(
        bs_point_regime_policy_json.read_text(encoding="utf-8")
    )

    assert report_json.exists()
    assert report_md.exists()
    assert decision_json.exists()
    assert decision_state_json.exists()
    assert runtime_overrides_json.exists()
    assert attribution_json.exists()
    assert attribution_md.exists()
    assert bs_point_json.exists()
    assert bs_point_md.exists()
    assert bs_ratio_state_json.exists()
    assert bs_ratio_overrides_json.exists()
    assert bs_point_regime_json.exists()
    assert bs_point_regime_md.exists()
    assert bs_point_regime_policy_json.exists()
    assert bs_point_regime_policy_md.exists()
    assert result["optimization_report"]["json"] == str(report_json)
    assert result["optimization_report"]["decision_json"] == str(decision_json)
    assert result["optimization_report"]["decision_state_json"] == str(decision_state_json)
    assert result["optimization_report"]["runtime_overrides_json"] == str(runtime_overrides_json)
    assert result["optimization_report"]["attribution_json"] == str(attribution_json)
    assert result["optimization_report"]["bs_point_attribution_json"] == str(bs_point_json)
    assert result["optimization_report"]["bs_point_ratio_overrides_json"] == str(bs_ratio_overrides_json)
    assert result["optimization_report"]["bs_point_regime_json"] == str(bs_point_regime_json)
    assert result["optimization_report"]["bs_point_regime_policy_json"] == str(bs_point_regime_policy_json)
    assert result["optimization_report"]["candidate_count"] == 6
    assert result["optimization_report"]["recommendations"]
    assert result["optimization_report"]["action_suggestions"]
    assert result["optimization_report"]["decisions"]
    assert report["candidate_ranking"][0]["id"] == "a_full_market_balanced"
    us_action = next(
        item for item in report["action_suggestions"] if item["market"] == "us"
    )
    us_decision = next(item for item in decision["decisions"] if item["market"] == "us")
    assert us_action["action"] == "keep_candidate"
    assert us_action["current_candidate"] == "us_core9_default"
    assert us_decision["risk_state"] == "ok"
    assert decision_state["confirmation_threshold"] == 2
    assert decision_state["market_states"][1]["status"] == "stable"
    assert runtime_overrides["override_count"] == 0
    assert attribution["version"] == 1
    assert bs_point["version"] == 2
    assert bs_point_regime["version"] == 1
    assert bs_point_regime_policy["version"] == 1
    assert bs_ratio_overrides["version"] == 1


def test_dynamic_monitor_sends_runtime_override_notice_once(tmp_path):
    event = {
        "market": "us",
        "action": "switch_candidate",
        "risk_state": "switch_ready",
        "target_candidate": "us_core9_default",
        "confirmations": 3,
        "confirmation_threshold": 3,
        "reason": "confirmed",
        "applied_config": {"max_pos": 9, "mid_gate": "soft"},
    }
    notifier = FakeNotifier()
    cfg = DynamicMonitorConfig(
        market="us",
        force=True,
        static_codes=("AAPL.US",),
        zx_groups=(),
        state_file=str(tmp_path / "state.json"),
        ledger=str(tmp_path / "ledger.json"),
        dry_run=True,
        runtime_override_event=event,
    )
    monitor = DynamicRecursiveMonitor(
        cfg,
        zixuan_factory=lambda _market: FakeZiXuan({}),
        exchange_factory=lambda _market: FakeExchange(),
        state_factory=FakeState,
        notifier=notifier,
        deduper=JsonDeduper(tmp_path / "dedupe.json"),
    )

    first = monitor.run_once()
    second = monitor.run_once()

    assert first["runtime_override_notice_sent"] is True
    assert second["runtime_override_notice_sent"] is False
    assert len(notifier.messages) == 1
    assert "策略覆盖" in notifier.messages[0][0]


def test_dynamic_monitor_config_reads_project_config(monkeypatch):
    monkeypatch.setattr(
        app_config,
        "RECURSIVE_MONITOR_CONFIG",
        {
            "enabled": True,
            "markets": ["a", "us"],
            "common": {
                "interval_seconds": 123,
                "start_delay_seconds": 7,
                "zx_groups": ["核心关注"],
                "dry_run": True,
                "optimization_report_enabled": True,
                "optimization_report_json": "D:/custom/strategy.json",
                "optimization_report_markdown": "D:/custom/strategy.md",
                "optimization_decision_json": "D:/custom/decision.json",
                "optimization_decision_state_json": "D:/custom/decision_state.json",
                "optimization_runtime_overrides_json": "D:/custom/runtime_overrides.json",
                "runtime_override_audit_jsonl": "D:/custom/runtime_override_audit.jsonl",
                "strategy_attribution_json": "D:/custom/attribution.json",
                "strategy_attribution_markdown": "D:/custom/attribution.md",
                "bs_point_attribution_json": "D:/custom/bs_point.json",
                "bs_point_attribution_markdown": "D:/custom/bs_point.md",
                "bs_point_ratio_state_json": "D:/custom/bs_ratio_state.json",
                "bs_point_ratio_overrides_json": "D:/custom/bs_ratio_overrides.json",
                "bs_point_regime_json": "D:/custom/bs_point_regime.json",
                "bs_point_regime_markdown": "D:/custom/bs_point_regime.md",
                "bs_point_regime_policy_json": "D:/custom/bs_point_regime_policy.json",
                "bs_point_regime_policy_markdown": "D:/custom/bs_point_regime_policy.md",
                "bs_point_regime_policy_min_trades": 40,
                "bs_point_ratio_confirmation_threshold": 4,
                "optimization_report_dir": "D:/custom/reports",
                "optimization_report_include_discovered": False,
                "decision_confirmation_threshold": 2,
                "runtime_overrides_enabled": False,
            },
            "a": {
                "enabled": True,
                "selection_groups": ["自动选股"],
                "enable_selection_pool": True,
                "selection_scan_limit": 88,
                "selection_max_codes": 21,
                "selection_lookback_bars": 5,
                "selection_buy_classes": [3, 1],
                "selection_require_three_systems": True,
                "fund_data": "D:/custom/fund_data",
                "fundamental_roe_ann_min": 12.5,
                "bt_data": "D:/custom/bt_data",
                "chart_cache_dir": "D:/custom/chart_cache",
                "ledger": "D:/custom/ledger.json",
                "state_file": "D:/custom/state.json",
                "max_pos": 6,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "regime_mode": "adaptive",
                "mid_gate": "bull_relaxed",
                "require_nest": True,
                "nest_mode": "soft",
                "trend_3boost": True,
                "sell_scope": "positions",
                "force": True,
                "paper_enabled": True,
                "title": "自定义提醒",
            },
            "us": {
                "enabled": True,
                "codes": ["SPY.US", "QQQ", "AAPL.US"],
                "max_pos": 3,
                "nest_mode": "soft",
                "trend_3boost": True,
            },
        },
    )

    cfg = DynamicMonitorConfig.from_config("a")

    assert cfg.interval_seconds == 123
    assert cfg.zx_groups == ("核心关注",)
    assert cfg.a_selection_max_codes == 21
    assert cfg.a_selection_buy_classes == (3, 1)
    assert cfg.a_selection_require_three_systems is True
    assert cfg.a_selection_fund_data == "D:/custom/fund_data"
    assert cfg.a_selection_fundamental_roe_ann_min == 12.5
    assert cfg.bt_data == "D:/custom/bt_data"
    assert cfg.ledger == "D:/custom/ledger.json"
    assert cfg.max_pos == 6
    assert cfg.op_level == "1m"
    assert cfg.mid_level == "5m"
    assert cfg.big_level == "30m"
    assert cfg.regime_mode == "adaptive"
    assert cfg.mid_gate == "bull_relaxed"
    assert cfg.require_nest is True
    assert cfg.nest_mode == "soft"
    assert cfg.trend_3boost is True
    assert cfg.sell_scope == "positions"
    assert cfg.force is True
    assert cfg.dry_run is True
    assert cfg.paper_enabled is True
    assert cfg.optimization_report_enabled is True
    assert cfg.optimization_report_json == "D:/custom/strategy.json"
    assert cfg.optimization_report_markdown == "D:/custom/strategy.md"
    assert cfg.optimization_decision_json == "D:/custom/decision.json"
    assert cfg.optimization_decision_state_json == "D:/custom/decision_state.json"
    assert cfg.optimization_runtime_overrides_json == "D:/custom/runtime_overrides.json"
    assert cfg.strategy_attribution_json == "D:/custom/attribution.json"
    assert cfg.strategy_attribution_markdown == "D:/custom/attribution.md"
    assert cfg.bs_point_attribution_json == "D:/custom/bs_point.json"
    assert cfg.bs_point_attribution_markdown == "D:/custom/bs_point.md"
    assert cfg.bs_point_ratio_state_json == "D:/custom/bs_ratio_state.json"
    assert cfg.bs_point_ratio_overrides_json == "D:/custom/bs_ratio_overrides.json"
    assert cfg.bs_point_regime_json == "D:/custom/bs_point_regime.json"
    assert cfg.bs_point_regime_markdown == "D:/custom/bs_point_regime.md"
    assert cfg.bs_point_regime_policy_json == "D:/custom/bs_point_regime_policy.json"
    assert cfg.bs_point_regime_policy_markdown == "D:/custom/bs_point_regime_policy.md"
    assert cfg.bs_point_regime_policy_min_trades == 40
    assert cfg.bs_point_ratio_confirmation_threshold == 4
    assert cfg.runtime_override_audit_jsonl == "D:/custom/runtime_override_audit.jsonl"
    assert cfg.optimization_report_dir == "D:/custom/reports"
    assert cfg.optimization_report_include_discovered is False
    assert cfg.decision_confirmation_threshold == 2
    assert cfg.runtime_overrides_enabled is False
    assert cfg.title == "自定义提醒"

    us_cfg = DynamicMonitorConfig.from_config("us")
    assert us_cfg.static_codes == ("SPY.US", "QQQ", "AAPL.US")
    assert us_cfg.max_pos == 3
    assert us_cfg.nest_mode == "soft"
    assert us_cfg.trend_3boost is True


def test_register_recursive_monitor_jobs_adds_a_and_us(monkeypatch):
    monkeypatch.setattr(
        app_config,
        "RECURSIVE_MONITOR_CONFIG",
        {
            "enabled": True,
            "markets": ["a", "us"],
            "common": {
                "interval_seconds": 300,
                "start_delay_seconds": 10,
                "zx_groups": [WATCH_GROUP],
                "dry_run": True,
            },
            "a": {
                "enabled": True,
                "enable_selection_pool": True,
                "selection_groups": ["自动选股"],
            },
            "us": {
                "enabled": True,
                "enable_selection_pool": False,
                "selection_groups": [],
            },
        },
    )
    scheduler = FakeScheduler()

    monitors = register_recursive_monitor_jobs(scheduler)

    assert sorted(monitors) == ["a", "us"]
    assert [job["id"] for job in scheduler.jobs] == [
        "recursive_live_monitor_a",
        "recursive_live_monitor_us",
    ]
    assert scheduler.recursive_live_monitors is monitors
