from __future__ import annotations

import json
import pickle
import sys

import numpy as np
import pandas as pd

from chanlun.notifications import (
    ClaudeHookNotifier,
    discover_claude_notification_command,
)
from chanlun.recursive_bt.live_backtest import load_chart_cache_syms
from chanlun.recursive_bt.live_monitor import (
    JsonDeduper,
    MonitorEvent,
    _apply_runtime_overrides,
    _resolve_monitor_max_pos,
    collect_monitor_events,
    load_universe,
    refresh_optimization_report,
    regime_policy_status,
    runtime_override_notice_lines,
    send_runtime_override_notice,
    strategy_adoption_gate_status,
)
from chanlun.recursive_bt.market_runtime import (
    chart_prefix_to_code,
    code_to_chart_prefix,
    list_chart_cache_codes,
    market_rules_for_code,
    normalize_code,
)


class _Sig:
    def __init__(
        self,
        bs_type: str,
        date: str = "2026-06-10 10:00:00",
        price=10,
        nest_operable=None,
        nest_depth=0,
    ):
        self.bs_type = bs_type
        self.date = date
        self.price = price
        self.nest_operable = nest_operable
        self.nest_depth = nest_depth
        self.is_buy = bs_type.endswith("buy")
        self.is_sell = bs_type.endswith("sell")


class _State:
    def __init__(
        self,
        signals,
        big_dir="neutral",
        last_px=10,
        daily_resonance=False,
        mid_dir="",
        op_level="5m",
        mid_level="",
        big_level="30m",
    ):
        self._signals = signals
        self._big_dir = big_dir
        self._mid_dir = mid_dir
        self._daily_resonance = daily_resonance
        self.op_level = op_level
        self.mid_level = mid_level
        self.big_level = big_level
        self.last_px = last_px
        self.last5 = "2026-06-10 10:00:00"
        self.last30 = "2026-06-10 10:00:00"
        self.last_op = "2026-06-10 10:00:00"
        self.last_big = "2026-06-10 10:00:00"

    def refresh(self):
        return self._signals

    def big_dir(self):
        return self._big_dir

    def mid_dir(self):
        return self._mid_dir

    def in_d3(self):
        return self._daily_resonance


def _event(code="SH.600000"):
    return MonitorEvent(
        code=code,
        name=code,
        side="buy",
        kind="small_buy",
        bs_type="3buy",
        signal_time="2026-06-10 10:00:00",
        price=10.0,
        big_dir="up",
        reason="test",
    )


def test_discover_claude_notification_command_prefers_dingtalk(tmp_path):
    settings = {
        "hooks": {
            "Notification": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo generic"},
                        {"type": "command", "command": "python dingtalk-notify.py notification"},
                    ],
                }
            ]
        }
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")

    assert discover_claude_notification_command(path) == "python dingtalk-notify.py notification"


def test_claude_hook_notifier_invokes_command_with_message(tmp_path):
    hook = tmp_path / "hook.py"
    out = tmp_path / "message.txt"
    hook.write_text(
        "import json, pathlib, sys\n"
        "data = json.loads(sys.stdin.read())\n"
        "pathlib.Path(sys.argv[2]).write_text(data['message'], encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{hook}" notification "{out}"'

    ok = ClaudeHookNotifier(command=command, cwd=tmp_path).send("Title", ["line1", "line2"])

    assert ok is True
    assert out.read_text(encoding="utf-8") == "Title\nline1\nline2"


def test_claude_hook_notifier_treats_dingtalk_error_as_failed(tmp_path):
    hook = tmp_path / "hook_error.py"
    hook.write_text(
        "import sys\n"
        "sys.stderr.write('dingtalk-notify error: timeout')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{hook}" notification'

    assert ClaudeHookNotifier(command=command, cwd=tmp_path).send("Title", ["line"]) is False


def test_collect_monitor_events_uses_3buy_priority_and_exit():
    states = {
        "A": _State([_Sig("1buy", price=11)], big_dir="up"),
        "B": _State([_Sig("3buy", price=12)], big_dir="up"),
        "C": _State([_Sig("2sell", price=9)], big_dir="neutral"),
        "D": _State([], big_dir="down", last_px=8),
    }

    events = collect_monitor_events(
        states,
        holdings={"D"},
        max_pos=2,
        sell_scope="all",
    )

    assert [e.code for e in events if e.side == "buy"] == ["B"]
    assert [e.buy_ratio for e in events if e.side == "buy"] == [0.5]
    assert any(e.code == "C" and e.side == "sell" for e in events)
    assert any(e.code == "D" and e.side == "exit" for e in events)
    assert all(e.sell_ratio == 1.0 for e in events if e.side in {"sell", "exit"})


def test_collect_monitor_events_trend_3boost_increases_up_3buy_ratio():
    states = {
        "A": _State([_Sig("3buy", price=12)], big_dir="up"),
        "B": _State([_Sig("3buy", price=10)], big_dir="neutral"),
    }

    events = collect_monitor_events(
        states,
        max_pos=10,
        trend_3boost=True,
    )

    ratios = {e.code: e.buy_ratio for e in events if e.side == "buy"}
    assert ratios["A"] == 0.125
    assert ratios["B"] == 0.1


def test_collect_monitor_events_applies_confirmed_bs_point_ratio_multiplier():
    states = {
        "A": _State([_Sig("3buy", price=12)], big_dir="neutral"),
    }

    events = collect_monitor_events(
        states,
        max_pos=10,
        bs_point_ratio_multipliers={"3": 1.1},
    )

    assert events[0].buy_ratio == 0.11
    assert "bs3_ratio_x1.10" in events[0].reason


def test_classify_visible_regime_rules():
    from chanlun.recursive_bt.market_runtime import classify_visible_regime

    flat = [10.0] * 30
    assert classify_visible_regime(flat, lookback_days=20) == "range"
    bull = [10.0] * 20 + [10.0 * (1 + 0.003 * i) for i in range(1, 21)]
    assert classify_visible_regime(bull, lookback_days=20) == "bull"
    bear = [10.0] * 20 + [10.0 * (1 - 0.004 * i) for i in range(1, 21)]
    assert classify_visible_regime(bear, lookback_days=20) == "bear"
    assert classify_visible_regime([10.0], lookback_days=20) == "range"
    assert classify_visible_regime([], lookback_days=20) == "range"


def test_collect_monitor_events_applies_regime_ratio_multiplier():
    states = {
        "A": _State([_Sig("3buy", price=12)], big_dir="neutral"),
    }

    bear_events = collect_monitor_events(
        states,
        max_pos=10,
        regime_ratio_multipliers={"bear": {"3": 1.25}},
        current_regime="bear",
    )
    range_events = collect_monitor_events(
        states,
        max_pos=10,
        regime_ratio_multipliers={"bear": {"3": 1.25}},
        current_regime="range",
    )

    assert bear_events[0].buy_ratio == 0.125
    assert "regime_bear_x1.25" in bear_events[0].reason
    assert range_events[0].buy_ratio == 0.1
    assert "regime_" not in range_events[0].reason


def test_monitor_symbol_state_incremental_fetch_uses_tail_window():
    import pandas as pd
    from chanlun.recursive_bt.live_monitor import MonitorSymbolState

    full = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01 09:30:00", periods=300, freq="1min", tz="Asia/Shanghai"),
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 100,
        }
    )

    class _Ex:
        def __init__(self):
            self.calls = []

        def klines(self, code, frequency, start_date=None, *args, **kwargs):
            self.calls.append((frequency, start_date))
            if start_date is None:
                return full
            cutoff = pd.Timestamp(start_date, tz="Asia/Shanghai")
            return full[full["date"] >= cutoff].reset_index(drop=True)

    ex = _Ex()
    state = MonitorSymbolState("SH.600000", ex, op_level="1m", big_level="30m")
    state.refresh()
    first_round = [c for c in ex.calls if c[0] == "1m"]
    assert first_round and first_round[0][1] is None  # 首轮全量

    ex.calls.clear()
    state.refresh()
    second_round = [c for c in ex.calls if c[0] == "1m"]
    # 有锚点后改拉尾部窗口(锚点回退数日缓冲),不再全量
    assert second_round and second_round[0][1] is not None
    anchor = pd.Timestamp(second_round[0][1], tz="Asia/Shanghai")
    assert anchor <= state.last_op
    assert anchor >= state.last_op - pd.Timedelta(days=7)


def test_monitor_symbol_state_incremental_fetch_falls_back_without_start_date():
    import pandas as pd
    from chanlun.recursive_bt.live_monitor import MonitorSymbolState

    full = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01 09:30:00", periods=300, freq="1min", tz="Asia/Shanghai"),
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 100,
        }
    )

    class _LegacyEx:
        def __init__(self):
            self.calls = 0

        def klines(self, code, frequency):
            self.calls += 1
            return full

    ex = _LegacyEx()
    state = MonitorSymbolState("SH.600000", ex, op_level="1m", big_level="30m")
    state.refresh()
    state.refresh()  # 不支持 start_date 的 exchange 必须安全回退全量,不抛错
    assert ex.calls >= 4


def test_regime_ratio_review_status_counts_market_verdicts(tmp_path):
    from chanlun.recursive_bt.live_monitor import regime_ratio_review_status

    report = tmp_path / "regime_ratio_impact.json"
    report.write_text(
        json.dumps(
            {
                "verdicts": [
                    {"market": "a", "candidate": "bear3_boost", "verdict": "review_regime_ratio"},
                    {"market": "a", "candidate": "combo", "verdict": "review_regime_ratio"},
                    {"market": "a", "candidate": "weak1_reduce", "verdict": "watch_defensive"},
                    {"market": "us", "candidate": "weak1_reduce", "verdict": "keep_default"},
                ]
            }
        ),
        encoding="utf-8",
    )

    a_status = regime_ratio_review_status(str(report), "a")
    us_status = regime_ratio_review_status(str(report), "us")

    assert a_status["review_regime_ratio"] == 2
    assert a_status["watch_defensive"] == 1
    assert us_status["keep_default"] == 1
    assert regime_ratio_review_status(str(tmp_path / "missing.json"), "a") == {}


def test_current_visible_regime_drops_today_and_caches():
    import pandas as pd
    from chanlun.recursive_bt.live_monitor import _REGIME_CACHE, current_visible_regime

    _REGIME_CACHE.clear()
    today = pd.Timestamp("2026-06-11").date()
    # 最后一根是 today 当日未完成 bar(收盘 99.0),必须被丢弃
    dates = list(pd.date_range("2026-05-01", periods=21, freq="1D")) + [
        pd.Timestamp("2026-06-11 15:00:00")
    ]
    closes = [10.0] * 20 + [8.9, 99.0]

    class _ExToday:
        def __init__(self):
            self.calls = 0

        def klines(self, code, freq):
            self.calls += 1
            return pd.DataFrame({"date": dates, "close": closes})

    ex_today = _ExToday()
    regime = current_visible_regime(ex_today, "SH.000001", lookback_days=2, today=today)
    # 当日 99.0 被丢弃,可见序列止于 8.9:2 日回看 -11% => bear
    assert regime == "bear"
    assert current_visible_regime(ex_today, "SH.000001", lookback_days=2, today=today) == "bear"
    assert ex_today.calls == 1  # 第二次命中按日缓存

    class _Broken:
        def klines(self, code, freq):
            raise RuntimeError("boom")

    _REGIME_CACHE.clear()
    assert current_visible_regime(_Broken(), "SH.000001", lookback_days=2, today=today) == "range"


def test_live_monitor_cli_accepts_regime_ratio_switches():
    from chanlun.recursive_bt.live_monitor import make_arg_parser

    args = make_arg_parser().parse_args(
        [
            "--regime-ratio-multipliers-json",
            '{"bear": {"3": 1.25}}',
            "--regime-source-code",
            "SH.000001",
            "--regime-lookback-days",
            "20",
        ]
    )

    assert args.regime_ratio_multipliers_json == '{"bear": {"3": 1.25}}'
    assert args.regime_source_code == "SH.000001"
    assert args.regime_lookback_days == 20


def test_collect_monitor_events_blocks_buy_when_mid_level_is_down():
    states = {
        "A": _State(
            [_Sig("3buy", price=11)],
            big_dir="up",
            mid_dir="down",
            op_level="1m",
            mid_level="5m",
            big_level="30m",
        ),
    }

    events = collect_monitor_events(states, max_pos=1)

    assert events == []


def test_live_monitor_cli_accepts_soft_mid_gate():
    from chanlun.recursive_bt.live_monitor import make_arg_parser

    args = make_arg_parser().parse_args(["--mid-gate", "soft"])

    assert args.mid_gate == "soft"


def test_live_monitor_cli_accepts_paper_switches():
    from chanlun.recursive_bt.live_monitor import make_arg_parser

    parser = make_arg_parser()

    assert parser.parse_args(["--paper-enabled"]).paper_enabled is True
    assert parser.parse_args(["--paper-enabled", "--no-paper"]).paper_enabled is False


def test_live_monitor_cli_accepts_optimization_report_switches():
    from chanlun.recursive_bt.live_monitor import make_arg_parser

    parser = make_arg_parser()
    args = parser.parse_args(
        [
            "--optimization-report-enabled",
            "--optimization-report-json",
            "D:/tmp/strategy.json",
            "--optimization-report-markdown",
            "D:/tmp/strategy.md",
            "--optimization-decision-json",
            "D:/tmp/decision.json",
            "--optimization-decision-state-json",
            "D:/tmp/decision_state.json",
            "--optimization-runtime-overrides-json",
            "D:/tmp/runtime_overrides.json",
            "--strategy-attribution-json",
            "D:/tmp/attribution.json",
            "--strategy-attribution-markdown",
            "D:/tmp/attribution.md",
            "--market-regime-stress-json",
            "D:/tmp/regime.json",
            "--market-regime-stress-markdown",
            "D:/tmp/regime.md",
            "--market-regime-min-days",
            "11",
            "--regime-policy-json",
            "D:/tmp/regime_policy.json",
            "--regime-policy-markdown",
            "D:/tmp/regime_policy.md",
            "--regime-policy-min-supporting-sources",
            "3",
            "--mtf3-cache-coverage-json",
            "D:/tmp/mtf3.json",
            "--mtf3-cache-coverage-markdown",
            "D:/tmp/mtf3.md",
            "--mtf3-cache-chart-cache-dir",
            "D:/tmp/chart_cache",
            "--mtf3-cache-bt-data-dir",
            "D:/tmp/bt_data_all_a",
            "--mtf3-cache-mtf3-bt-data-dir",
            "D:/tmp/bt_data_mtf3",
            "--mtf3-cache-bt-sample-size",
            "17",
            "--sell3-rebuy3-impact-json",
            "D:/tmp/sell3_rebuy3.json",
            "--sell3-rebuy3-impact-markdown",
            "D:/tmp/sell3_rebuy3.md",
            "--sell3-rebuy3-up-impact-json",
            "D:/tmp/sell3_rebuy3_up.json",
            "--sell3-rebuy3-up-impact-markdown",
            "D:/tmp/sell3_rebuy3_up.md",
            "--regime-ratio-impact-json",
            "D:/tmp/regime_ratio_impact.json",
            "--regime-ratio-impact-markdown",
            "D:/tmp/regime_ratio_impact.md",
            "--sell3-rebuy-mid3-impact-json",
            "D:/tmp/sell3_mid.json",
            "--sell3-rebuy-mid3-impact-markdown",
            "D:/tmp/sell3_mid.md",
            "--a-5m-sell3-rebuy3-impact-json",
            "D:/tmp/a_5m.json",
            "--a-5m-sell3-rebuy3-impact-markdown",
            "D:/tmp/a_5m.md",
            "--strategy-adoption-gate-json",
            "D:/tmp/adoption_gate.json",
            "--strategy-adoption-gate-markdown",
            "D:/tmp/adoption_gate.md",
            "--bs-point-attribution-json",
            "D:/tmp/bs_point.json",
            "--bs-point-attribution-markdown",
            "D:/tmp/bs_point.md",
            "--bs-point-ratio-state-json",
            "D:/tmp/bs_ratio_state.json",
            "--bs-point-ratio-overrides-json",
            "D:/tmp/bs_ratio_overrides.json",
            "--bs-point-regime-json",
            "D:/tmp/bs_point_regime.json",
            "--bs-point-regime-markdown",
            "D:/tmp/bs_point_regime.md",
            "--bs-point-regime-policy-json",
            "D:/tmp/bs_point_regime_policy.json",
            "--bs-point-regime-policy-markdown",
            "D:/tmp/bs_point_regime_policy.md",
            "--bs-point-regime-policy-min-trades",
            "33",
            "--bs-point-ratio-confirmation-threshold",
            "4",
            "--runtime-override-audit-jsonl",
            "D:/tmp/audit.jsonl",
            "--decision-confirmation-threshold",
            "2",
            "--no-runtime-overrides",
            "--optimization-report-dir",
            "D:/tmp/reports",
            "--no-optimization-report-discover",
        ]
    )

    assert args.optimization_report_enabled is True
    assert args.optimization_report_json == "D:/tmp/strategy.json"
    assert args.optimization_report_markdown == "D:/tmp/strategy.md"
    assert args.optimization_decision_json == "D:/tmp/decision.json"
    assert args.optimization_decision_state_json == "D:/tmp/decision_state.json"
    assert args.optimization_runtime_overrides_json == "D:/tmp/runtime_overrides.json"
    assert args.strategy_attribution_json == "D:/tmp/attribution.json"
    assert args.strategy_attribution_markdown == "D:/tmp/attribution.md"
    assert args.market_regime_stress_json == "D:/tmp/regime.json"
    assert args.market_regime_stress_markdown == "D:/tmp/regime.md"
    assert args.market_regime_min_days == 11
    assert args.regime_policy_json == "D:/tmp/regime_policy.json"
    assert args.regime_policy_markdown == "D:/tmp/regime_policy.md"
    assert args.regime_policy_min_supporting_sources == 3
    assert args.mtf3_cache_coverage_json == "D:/tmp/mtf3.json"
    assert args.mtf3_cache_coverage_markdown == "D:/tmp/mtf3.md"
    assert args.mtf3_cache_chart_cache_dir == "D:/tmp/chart_cache"
    assert args.mtf3_cache_bt_data_dir == "D:/tmp/bt_data_all_a"
    assert args.mtf3_cache_mtf3_bt_data_dir == "D:/tmp/bt_data_mtf3"
    assert args.mtf3_cache_bt_sample_size == 17
    assert args.sell3_rebuy3_impact_json == "D:/tmp/sell3_rebuy3.json"
    assert args.sell3_rebuy3_impact_markdown == "D:/tmp/sell3_rebuy3.md"
    assert args.sell3_rebuy3_up_impact_json == "D:/tmp/sell3_rebuy3_up.json"
    assert args.sell3_rebuy3_up_impact_markdown == "D:/tmp/sell3_rebuy3_up.md"
    assert args.regime_ratio_impact_json == "D:/tmp/regime_ratio_impact.json"
    assert args.regime_ratio_impact_markdown == "D:/tmp/regime_ratio_impact.md"
    assert args.sell3_rebuy_mid3_impact_json == "D:/tmp/sell3_mid.json"
    assert args.sell3_rebuy_mid3_impact_markdown == "D:/tmp/sell3_mid.md"
    assert args.a_5m_sell3_rebuy3_impact_json == "D:/tmp/a_5m.json"
    assert args.a_5m_sell3_rebuy3_impact_markdown == "D:/tmp/a_5m.md"
    assert args.strategy_adoption_gate_json == "D:/tmp/adoption_gate.json"
    assert args.strategy_adoption_gate_markdown == "D:/tmp/adoption_gate.md"
    assert args.bs_point_attribution_json == "D:/tmp/bs_point.json"
    assert args.bs_point_attribution_markdown == "D:/tmp/bs_point.md"
    assert args.bs_point_ratio_state_json == "D:/tmp/bs_ratio_state.json"
    assert args.bs_point_ratio_overrides_json == "D:/tmp/bs_ratio_overrides.json"
    assert args.bs_point_regime_json == "D:/tmp/bs_point_regime.json"
    assert args.bs_point_regime_markdown == "D:/tmp/bs_point_regime.md"
    assert args.bs_point_regime_policy_json == "D:/tmp/bs_point_regime_policy.json"
    assert args.bs_point_regime_policy_markdown == "D:/tmp/bs_point_regime_policy.md"
    assert args.bs_point_regime_policy_min_trades == 33
    assert args.bs_point_ratio_confirmation_threshold == 4
    assert args.runtime_override_audit_jsonl == "D:/tmp/audit.jsonl"
    assert args.decision_confirmation_threshold == 2
    assert args.runtime_overrides_enabled is False
    assert args.optimization_report_dir == "D:/tmp/reports"
    assert args.optimization_report_include_discovered is False
    assert parser.parse_args(["--optimization-report-enabled", "--no-optimization-report"]).optimization_report_enabled is False


def test_strategy_adoption_gate_status_counts_market_gates(tmp_path):
    path = tmp_path / "adoption_gate.json"
    path.write_text(
        json.dumps(
            {
                "gates": [
                    {"market": "a", "gate_action": "review_allowed"},
                    {"market": "a", "gate_action": "watch_evidence_limited"},
                    {"market": "a", "gate_action": "blocked_evidence"},
                    {"market": "a", "gate_action": "keep_default"},
                    {"market": "us", "gate_action": "review_allowed"},
                ]
            }
        ),
        encoding="utf-8",
    )

    status = strategy_adoption_gate_status(path, "a")

    assert status["review_allowed"] == 1
    assert status["watch_evidence_limited"] == 1
    assert status["blocked"] == 1
    assert status["keep_default"] == 1
    assert status["total"] == 4
    assert strategy_adoption_gate_status(tmp_path / "missing.json", "a") == {}


def test_regime_policy_status_counts_market_policies(tmp_path):
    path = tmp_path / "regime_policy.json"
    path.write_text(
        json.dumps(
            {
                "policies": [
                    {"market": "a", "policy_action": "review_regime_candidate"},
                    {"market": "a", "policy_action": "watch_regime_candidate"},
                    {"market": "a", "policy_action": "keep_default"},
                    {"market": "a", "policy_action": "evidence_limited"},
                    {"market": "us", "policy_action": "watch_regime_candidate"},
                ]
            }
        ),
        encoding="utf-8",
    )

    status = regime_policy_status(path, "a")

    assert status["review_regime_candidate"] == 1
    assert status["watch_regime_candidate"] == 1
    assert status["keep_default"] == 1
    assert status["evidence_limited"] == 1
    assert status["total"] == 4
    assert regime_policy_status(tmp_path / "missing.json", "a") == {}


def test_refresh_optimization_report_returns_action_suggestions(tmp_path):
    report_json = tmp_path / "strategy.json"
    report_md = tmp_path / "strategy.md"
    decision_json = tmp_path / "decision.json"
    decision_state_json = tmp_path / "decision_state.json"
    runtime_overrides_json = tmp_path / "runtime_overrides.json"
    attribution_json = tmp_path / "attribution.json"
    attribution_md = tmp_path / "attribution.md"
    bs_point_json = tmp_path / "bs_point.json"
    bs_point_md = tmp_path / "bs_point.md"
    bs_ratio_state_json = tmp_path / "bs_ratio_state.json"
    bs_ratio_overrides_json = tmp_path / "bs_ratio_overrides.json"
    bs_point_regime_json = tmp_path / "bs_point_regime.json"
    bs_point_regime_md = tmp_path / "bs_point_regime.md"
    bs_point_regime_policy_json = tmp_path / "bs_point_regime_policy.json"
    bs_point_regime_policy_md = tmp_path / "bs_point_regime_policy.md"
    regime_json = tmp_path / "regime.json"
    regime_md = tmp_path / "regime.md"
    regime_policy_json = tmp_path / "regime_policy.json"
    regime_policy_md = tmp_path / "regime_policy.md"
    mtf3_json = tmp_path / "mtf3.json"
    mtf3_md = tmp_path / "mtf3.md"
    sell3_rebuy3_json = tmp_path / "sell3_rebuy3.json"
    sell3_rebuy3_md = tmp_path / "sell3_rebuy3.md"
    sell3_rebuy3_up_json = tmp_path / "sell3_rebuy3_up.json"
    sell3_rebuy3_up_md = tmp_path / "sell3_rebuy3_up.md"
    sell3_mid_json = tmp_path / "sell3_mid.json"
    sell3_mid_md = tmp_path / "sell3_mid.md"
    a_5m_json = tmp_path / "a_5m.json"
    a_5m_md = tmp_path / "a_5m.md"
    adoption_gate_json = tmp_path / "adoption_gate.json"
    adoption_gate_md = tmp_path / "adoption_gate.md"
    chart_cache = tmp_path / "chart_cache"
    chart_cache.mkdir()
    bt_data_all_a = tmp_path / "bt_data_all_a"
    bt_data_all_a.mkdir()
    with (bt_data_all_a / "SH.600000.pkl").open("wb") as fp:
        pickle.dump(
            {
                "small_by_bar": {},
                "big_dir_at": [],
            },
            fp,
        )
    mtf3_bt_data = tmp_path / "bt_data_mtf3"
    mtf3_bt_data.mkdir()
    with (mtf3_bt_data / "SH.600000.pkl").open("wb") as fp:
        pickle.dump(
            {
                "small_by_bar": {},
                "big_dir_at": [],
                "mid_dir_at": [],
                "mid_by_bar": {},
            },
            fp,
        )
    audit_jsonl = tmp_path / "audit.jsonl"
    ledger_a = tmp_path / "paper_a.json"
    ledger_us = tmp_path / "paper_us.json"
    trades_a = tmp_path / "missing_a_trades.csv"
    trades_us = tmp_path / "missing_us_trades.csv"

    result = refresh_optimization_report(
        output_json=str(report_json),
        output_markdown=str(report_md),
        output_decision=str(decision_json),
        output_decision_state=str(decision_state_json),
        output_runtime_overrides=str(runtime_overrides_json),
        output_attribution_json=str(attribution_json),
        output_attribution_markdown=str(attribution_md),
        output_bs_point_json=str(bs_point_json),
        output_bs_point_markdown=str(bs_point_md),
        output_bs_point_ratio_state=str(bs_ratio_state_json),
        output_bs_point_ratio_overrides=str(bs_ratio_overrides_json),
        output_bs_point_regime_json=str(bs_point_regime_json),
        output_bs_point_regime_markdown=str(bs_point_regime_md),
        output_bs_point_regime_policy_json=str(bs_point_regime_policy_json),
        output_bs_point_regime_policy_markdown=str(bs_point_regime_policy_md),
        bs_point_regime_policy_min_trades=30,
        output_market_regime_stress_json=str(regime_json),
        output_market_regime_stress_markdown=str(regime_md),
        market_regime_min_days=5,
        output_regime_policy_json=str(regime_policy_json),
        output_regime_policy_markdown=str(regime_policy_md),
        regime_policy_min_supporting_sources=2,
        output_mtf3_cache_coverage_json=str(mtf3_json),
        output_mtf3_cache_coverage_markdown=str(mtf3_md),
        mtf3_cache_chart_cache_dir=str(chart_cache),
        mtf3_cache_bt_data_dir=str(bt_data_all_a),
        mtf3_cache_mtf3_bt_data_dir=str(mtf3_bt_data),
        mtf3_cache_bt_sample_size=5,
        output_sell3_rebuy3_impact_json=str(sell3_rebuy3_json),
        output_sell3_rebuy3_impact_markdown=str(sell3_rebuy3_md),
        output_sell3_rebuy3_up_impact_json=str(sell3_rebuy3_up_json),
        output_sell3_rebuy3_up_impact_markdown=str(sell3_rebuy3_up_md),
        output_regime_ratio_impact_json=str(tmp_path / "regime_ratio_impact.json"),
        output_regime_ratio_impact_markdown=str(tmp_path / "regime_ratio_impact.md"),
        output_sell3_rebuy_mid3_impact_json=str(sell3_mid_json),
        output_sell3_rebuy_mid3_impact_markdown=str(sell3_mid_md),
        output_a_5m_sell3_rebuy3_impact_json=str(a_5m_json),
        output_a_5m_sell3_rebuy3_impact_markdown=str(a_5m_md),
        output_strategy_adoption_gate_json=str(adoption_gate_json),
        output_strategy_adoption_gate_markdown=str(adoption_gate_md),
        runtime_override_audit_jsonl=str(audit_jsonl),
        attribution_ledger_paths={"a": ledger_a, "us": ledger_us},
        bs_point_trade_paths={"a": trades_a, "us": trades_us},
        include_discovered=False,
        report_dir=str(tmp_path / "reports"),
        decision_confirmation_threshold=2,
        current_config={
            "market": "us",
            "max_pos": 9,
            "op_level": "1m",
            "mid_level": "5m",
            "big_level": "30m",
            "mid_gate": "soft",
            "nest_mode": "soft",
            "trend_3boost": True,
        },
    )
    saved = json.loads(report_json.read_text(encoding="utf-8"))
    decision = json.loads(decision_json.read_text(encoding="utf-8"))
    state = json.loads(decision_state_json.read_text(encoding="utf-8"))
    overrides = json.loads(runtime_overrides_json.read_text(encoding="utf-8"))
    attribution = json.loads(attribution_json.read_text(encoding="utf-8"))
    regime_policy = json.loads(regime_policy_json.read_text(encoding="utf-8"))
    mtf3 = json.loads(mtf3_json.read_text(encoding="utf-8"))
    bs_ratio_overrides = json.loads(bs_ratio_overrides_json.read_text(encoding="utf-8"))
    us_action = next(
        item for item in result["action_suggestions"] if item["market"] == "us"
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
    assert regime_json.exists()
    assert regime_md.exists()
    assert regime_policy_json.exists()
    assert regime_policy_md.exists()
    assert mtf3_json.exists()
    assert mtf3_md.exists()
    assert sell3_rebuy3_json.exists()
    assert sell3_rebuy3_md.exists()
    assert sell3_rebuy3_up_json.exists()
    assert sell3_rebuy3_up_md.exists()
    assert sell3_mid_json.exists()
    assert sell3_mid_md.exists()
    assert a_5m_json.exists()
    assert a_5m_md.exists()
    assert adoption_gate_json.exists()
    assert adoption_gate_md.exists()
    assert result["decision_json"] == str(decision_json)
    assert result["decision_state_json"] == str(decision_state_json)
    assert result["runtime_overrides_json"] == str(runtime_overrides_json)
    assert result["attribution_json"] == str(attribution_json)
    assert result["bs_point_attribution_json"] == str(bs_point_json)
    assert result["bs_point_ratio_state_json"] == str(bs_ratio_state_json)
    assert result["bs_point_ratio_overrides_json"] == str(bs_ratio_overrides_json)
    assert result["bs_point_regime_json"] == str(bs_point_regime_json)
    assert result["bs_point_regime_policy_json"] == str(bs_point_regime_policy_json)
    assert result["bs_point_regime_policy_count"] >= 1
    assert result["market_regime_stress_json"] == str(regime_json)
    assert result["regime_policy_json"] == str(regime_policy_json)
    assert result["regime_policy_count"] >= 1
    assert result["mtf3_cache_coverage_json"] == str(mtf3_json)
    assert result["mtf3_cache_bt_data_dir"] == str(bt_data_all_a)
    assert result["mtf3_cache_mtf3_bt_data_dir"] == str(mtf3_bt_data)
    assert result["mtf3_cache_bt_sample_size"] == 5
    assert result["sell3_rebuy3_impact_json"] == str(sell3_rebuy3_json)
    assert result["sell3_rebuy3_up_impact_json"] == str(sell3_rebuy3_up_json)
    assert result["regime_ratio_impact_json"] == str(tmp_path / "regime_ratio_impact.json")
    assert (tmp_path / "regime_ratio_impact.json").exists()
    assert result["regime_ratio_impact_verdicts"] >= 1
    assert result["strategy_adoption_gate_json"] == str(adoption_gate_json)
    assert result["adoption_gate_count"] >= 1
    assert result["attribution_segments"] == 2
    assert result["attribution_missing_count"] == 0
    assert result["bs_point_missing_count"] == 2
    assert ledger_a.exists()
    assert ledger_us.exists()
    assert us_action["action"] == "keep_candidate"
    assert result["decisions"][1]["risk_state"] == "ok"
    assert result["decision_state"]["confirmation_threshold"] == 2
    assert saved["action_suggestions"]
    assert decision["decisions"][1]["target_candidate"] == "us_core9_default"
    assert state["market_states"][1]["status"] == "stable"
    assert overrides["override_count"] == 0
    assert bs_ratio_overrides["override_count"] == 0
    assert attribution["version"] == 1
    assert any(
        item["policy_action"] == "watch_regime_candidate"
        for item in regime_policy["policies"]
        if item["market"] == "a"
    )
    assert mtf3["markets"][0]["bt_data"]["dir"] == str(bt_data_all_a)
    assert mtf3["markets"][0]["bt_data"]["sample_5m30m_ready_count"] == 1
    assert mtf3["markets"][0]["mtf3_bt_data"]["dir"] == str(mtf3_bt_data)
    assert mtf3["markets"][0]["mtf3_bt_data"]["sample_mtf3_ready_count"] == 1


def test_apply_runtime_overrides_only_uses_confirmed_monitor_config(tmp_path):
    runtime_overrides = tmp_path / "runtime_overrides.json"
    ratio_overrides = tmp_path / "ratio_overrides.json"
    audit_log = tmp_path / "override_audit.jsonl"
    runtime_overrides.write_text(
        json.dumps(
            {
                "version": 1,
                "overrides": [
                    {
                        "market": "a",
                        "action": "switch_candidate",
                        "risk_state": "switch_ready",
                        "target_candidate": "a_full_market_balanced",
                        "decision_key": "a|switch_candidate|a_full_market_balanced",
                        "confirmations": 3,
                        "confirmation_threshold": 3,
                        "reason": "confirmed",
                        "monitor_config": {
                            "max_pos": 50,
                            "mid_gate": "soft",
                            "unknown": "ignored",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ratio_overrides.write_text(
        json.dumps(
            {
                "version": 1,
                "overrides": [
                    {
                        "market": "a",
                        "bs_point_ratio_multipliers": {"3": 1.1},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    settings = _apply_runtime_overrides(
        {
            "max_pos": 30,
            "mid_gate": "strict",
            "optimization_runtime_overrides_json": str(runtime_overrides),
            "bs_point_ratio_overrides_json": str(ratio_overrides),
            "runtime_override_audit_jsonl": str(audit_log),
            "runtime_overrides_enabled": True,
        },
        "a",
    )
    again = _apply_runtime_overrides(
        {
            "max_pos": 30,
            "optimization_runtime_overrides_json": str(runtime_overrides),
            "bs_point_ratio_overrides_json": str(ratio_overrides),
            "runtime_override_audit_jsonl": str(audit_log),
            "runtime_overrides_enabled": True,
        },
        "a",
    )
    disabled = _apply_runtime_overrides(
        {
            "max_pos": 30,
            "optimization_runtime_overrides_json": str(runtime_overrides),
            "bs_point_ratio_overrides_json": str(ratio_overrides),
            "runtime_override_audit_jsonl": str(audit_log),
            "runtime_overrides_enabled": False,
        },
        "a",
    )

    assert settings["max_pos"] == 50
    assert settings["mid_gate"] == "soft"
    assert settings["bs_point_ratio_multipliers"] == {"3": 1.1}
    assert "unknown" not in settings
    assert settings["_runtime_override_event"]["event"] == "runtime_override_applied"
    assert "_runtime_override_event" not in again
    assert len(audit_log.read_text(encoding="utf-8").splitlines()) == 1
    assert disabled["max_pos"] == 30
    assert disabled["bs_point_ratio_multipliers"] == {"3": 1.1}


def test_runtime_override_notice_formats_and_sends():
    notifier = ClaudeHookNotifier(dry_run=True)
    event = {
        "market": "a",
        "action": "switch_candidate",
        "risk_state": "switch_ready",
        "target_candidate": "a_full_market_balanced",
        "confirmations": 3,
        "confirmation_threshold": 3,
        "reason": "confirmed",
        "applied_config": {"max_pos": 30, "mid_gate": "soft"},
    }

    assert "策略覆盖已应用" in runtime_override_notice_lines(event)[0]
    assert send_runtime_override_notice(notifier, "Title", event) is True
    assert send_runtime_override_notice(notifier, "Title", None) is False


def test_collect_monitor_events_relaxes_mid_gate_in_adaptive_bull_mode():
    states = {
        "A": _State(
            [_Sig("3buy", price=11)],
            big_dir="up",
            mid_dir="down",
            op_level="1m",
            mid_level="5m",
            big_level="30m",
        ),
    }

    events = collect_monitor_events(
        states,
        max_pos=10,
        regime_mode="adaptive",
        mid_gate="bull_relaxed",
    )

    assert len(events) == 1
    assert events[0].side == "buy"
    assert events[0].buy_ratio == 0.05
    assert "5m relaxed_by_30m_up" in events[0].line()


def test_collect_monitor_events_soft_mid_gate_discounts_down_middle_level():
    states = {
        "A": _State(
            [_Sig("3buy", price=11)],
            big_dir="up",
            mid_dir="down",
            op_level="1m",
            mid_level="5m",
            big_level="30m",
        ),
    }

    events = collect_monitor_events(
        states,
        max_pos=10,
        mid_gate="soft",
        trend_3boost=True,
    )

    assert len(events) == 1
    assert events[0].side == "buy"
    assert events[0].buy_ratio == 0.0625
    assert "5m soft_down_discount" in events[0].line()


def test_collect_monitor_events_can_require_interval_nest_for_12_buys():
    states = {
        "A": _State([_Sig("1buy", price=11, nest_operable=False)], big_dir="up"),
        "B": _State([_Sig("1buy", price=12, nest_operable=True, nest_depth=2)], big_dir="up"),
        "C": _State([_Sig("3buy", price=13)], big_dir="up"),
    }

    events = collect_monitor_events(
        states,
        max_pos=3,
        require_nest=True,
    )

    assert [e.code for e in events if e.side == "buy"] == ["C", "B"]
    assert any("interval_nest(depth=2)" in e.reason for e in events)


def test_collect_monitor_events_soft_nest_reduces_12_buy_ratio():
    states = {
        "A": _State([_Sig("1buy", price=11, nest_operable=False, nest_depth=1)], big_dir="up"),
    }

    events = collect_monitor_events(
        states,
        max_pos=10,
        nest_mode="soft",
    )

    assert len(events) == 1
    assert events[0].side == "buy"
    assert events[0].buy_ratio == 0.0488
    assert "interval_nest(depth=1)" in events[0].reason


def test_monitor_event_line_includes_trade_ratios():
    buy = _event()
    buy.buy_ratio = 0.1
    buy.op_level = "1m"
    buy.mid_level = "5m"
    buy.mid_dir = "up"
    sell = MonitorEvent(
        code="SH.600000",
        name="SH.600000",
        side="sell",
        kind="small_sell",
        bs_type="2sell",
        signal_time="2026-06-10 10:00:00",
        price=10.0,
        big_dir="neutral",
        reason="test",
        sell_ratio=1.0,
    )

    assert "建议买入=10.0%" in buy.line()
    assert "30m=up 5m=up" in buy.line()
    assert "建议卖出=100.0%" in sell.line()


def test_json_deduper_persists_seen_identities(tmp_path):
    path = tmp_path / "state.json"
    event = _event()
    deduper = JsonDeduper(path)

    assert deduper.unseen([event]) == [event]
    deduper.mark([event])

    reloaded = JsonDeduper(path)
    assert reloaded.unseen([event]) == []


def test_market_runtime_supports_a_and_us_codes():
    assert normalize_code("us", "qqq") == "QQQ.US"
    assert normalize_code("a", "sh.600000") == "SH.600000"
    assert code_to_chart_prefix("us", "QQQ.US") == "us_QQQ_US"
    assert chart_prefix_to_code("us", "us_QQQ_US") == "QQQ.US"
    assert code_to_chart_prefix("a", "SH.600000") == "a_SH_600000"
    assert chart_prefix_to_code("a", "a_SH_600000") == "SH.600000"


def test_market_runtime_uses_board_specific_a_share_limits():
    assert market_rules_for_code("a", "SH.600000").limit_pct == 0.10
    assert market_rules_for_code("a", "SZ.300001").limit_pct == 0.20
    assert market_rules_for_code("a", "BJ.920001").limit_pct == 0.30


def test_live_monitor_resolves_auto_max_pos():
    assert _resolve_monitor_max_pos(None, 2) == 2
    assert _resolve_monitor_max_pos(0, 2) == 2
    assert _resolve_monitor_max_pos(None, 50) == 10
    assert _resolve_monitor_max_pos(7, 2) == 7


def test_us_universe_can_load_from_chart_cache(tmp_path):
    cache = tmp_path / "chart_cache"
    cache.mkdir()
    (cache / "v6_us_QQQ_US_5m_x.pkl").write_bytes(b"not-used")

    assert list_chart_cache_codes("us", cache) == ["QQQ.US"]
    assert load_universe("us", chart_cache_dir=cache, pool_size=10) == ["QQQ.US"]


def test_load_chart_cache_syms_builds_us_backtest_symbol(tmp_path):
    import pickle

    cache = tmp_path / "chart_cache"
    cache.mkdir()
    n = 160
    ts = (np.arange(n) * 300 + 1_700_000_000).tolist()
    data = {
        "t": ts,
        "o": np.linspace(100, 120, n).tolist(),
        "h": np.linspace(101, 121, n).tolist(),
        "l": np.linspace(99, 119, n).tolist(),
        "c": np.linspace(100.5, 120.5, n).tolist(),
        "v": np.full(n, 1000).tolist(),
    }
    with open(cache / "v6_us_QQQ_US_5m_x.pkl", "wb") as fp:
        pickle.dump({"data": data}, fp)

    syms = load_chart_cache_syms("us", ["QQQ.US"], str(cache), pool_size=1)

    assert list(syms) == ["QQQ.US"]
    assert syms["QQQ.US"]["rules"].t_plus == 0
    assert len(syms["QQQ.US"]["dates"]) == n


def test_us_mtf3_chart_cache_builder_writes_backtest_ready_files(tmp_path):
    from chanlun.recursive_bt.us_mtf3_cache import build_us_mtf3_chart_cache

    class FakeExchange:
        def klines(self, code, frequency, start_date=None, end_date=None, args=None):
            n = {"1m": 160, "5m": 140, "30m": 120}[frequency]
            step = {"1m": "1min", "5m": "5min", "30m": "30min"}[frequency]
            dates = pd.date_range(
                "2026-01-01 09:30:00",
                periods=n,
                freq=step,
                tz="UTC",
            )
            return pd.DataFrame(
                {
                    "date": dates,
                    "open": np.linspace(100, 120, n),
                    "high": np.linspace(101, 121, n),
                    "low": np.linspace(99, 119, n),
                    "close": np.linspace(100.5, 120.5, n),
                    "volume": np.full(n, 1000),
                }
            )

    manifest = build_us_mtf3_chart_cache(
        ["AAPL.US", "MSFT.US"],
        out_dir=tmp_path,
        start_date="2026-01-01",
        end_date="2026-02-01",
        exchange=FakeExchange(),
        cache_tag="test",
    )

    assert manifest["counts"] == {"ok": 6, "skip": 0, "fail": 0}
    assert (tmp_path / "_us_mtf3_build_manifest.json").exists()
    assert list_chart_cache_codes("us", tmp_path, "1m") == ["AAPL.US", "MSFT.US"]
    assert list_chart_cache_codes("us", tmp_path, "5m") == ["AAPL.US", "MSFT.US"]
    assert list_chart_cache_codes("us", tmp_path, "30m") == ["AAPL.US", "MSFT.US"]
    syms = load_chart_cache_syms(
        "us",
        ["AAPL.US"],
        str(tmp_path),
        pool_size=1,
        op_level="1m",
        mid_level="5m",
        big_level="30m",
    )
    assert list(syms) == ["AAPL.US"]
    assert "mid_by_bar" in syms["AAPL.US"]
