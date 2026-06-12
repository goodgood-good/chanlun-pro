import json
import pickle

import pytest

from chanlun.recursive_bt.strategy_optimizer import (
    RuntimeSummarySource,
    a_selection_systems,
    build_bs_point_ratio_impact_report,
    build_bs_point_regime_attribution_report,
    build_bs_point_regime_policy_report,
    build_layer_attribution_report,
    build_mtf3_cache_coverage_report,
    build_market_regime_stress_report,
    build_regime_strategy_policy_report,
    build_sell_policy_impact_report,
    build_strategy_adoption_gate_report,
    build_action_suggestions,
    build_candidate_report,
    build_decision_artifact,
    build_decision_state,
    build_bs_point_attribution_report,
    build_bs_point_ratio_overrides,
    build_bs_point_ratio_state,
    build_optimization_report,
    build_runtime_observations,
    build_runtime_overrides,
    build_strategy_attribution_report,
    discover_backtest_summary_sources,
    default_strategy_candidates,
    ensure_paper_ledger_baseline,
    match_candidate_from_monitor_config,
    rank_candidates_by_evidence,
    rank_summary_records,
    render_optimization_markdown,
    render_bs_point_attribution_markdown,
    render_bs_point_ratio_impact_markdown,
    render_bs_point_regime_attribution_markdown,
    render_bs_point_regime_policy_markdown,
    render_layer_attribution_markdown,
    render_mtf3_cache_coverage_markdown,
    render_market_regime_stress_markdown,
    render_regime_strategy_policy_markdown,
    render_strategy_adoption_gate_markdown,
    render_strategy_attribution_markdown,
    render_sell_policy_impact_markdown,
    score_summary,
    score_runtime_sources,
    summary_from_paper_ledger,
    bs_point_ratio_multipliers_for_market,
    runtime_override_for_market,
    update_decision_state_file,
    update_bs_point_ratio_state_file,
    write_runtime_overrides_file,
    write_bs_point_attribution_report,
    write_layer_attribution_report,
    write_bs_point_ratio_overrides_file,
    write_strategy_attribution_report,
    write_optimization_report,
)



@pytest.fixture(autouse=True)
def _hermetic_default_runtime_sources(monkeypatch):
    """默认 runtime 源指向真实 D:/chanlun_pro 的 paper ledger / live-parity 报告，
    而常驻 live_monitor 实盘进程会持续改写它们——2026-06-12 首批真实 paper 交易
    落账后，本文件依赖 build_optimization_report 的测试随盘面状态漂移（瞬态
    review_runtime_gap）。测试一律密闭：默认源清空，各用例只用自己注入的 tmp 数据。"""
    from chanlun.recursive_bt import strategy_optimizer as _so
    monkeypatch.setattr(
        _so, "default_runtime_summary_sources", lambda markets=("a", "us"): []
    )


def test_a_selection_systems_define_three_independent_confirmations():
    systems = a_selection_systems()

    assert [system.key for system in systems] == [
        "fundamental",
        "comparison",
        "technical",
    ]
    assert all(system.role for system in systems)


def test_strategy_candidates_codify_current_market_defaults():
    a_ranked = rank_candidates_by_evidence("a")
    us_ranked = rank_candidates_by_evidence("us")

    assert a_ranked[0][0].id == "a_full_market_balanced"
    assert a_ranked[0][0].monitor_config()["max_pos"] == 30
    assert a_ranked[0][0].monitor_config()["mid_gate"] == "soft"
    assert a_ranked[0][0].monitor_config()["selection_require_three_systems"] is True

    assert us_ranked[0][0].id == "us_core9_default"
    assert us_ranked[0][0].monitor_config()["max_pos"] == 9
    assert us_ranked[0][0].monitor_config()["trend_3boost"] is True
    assert us_ranked[0][0].monitor_config()["nest_mode"] == "soft"


def test_score_summary_penalizes_drawdown_for_same_return():
    low_dd = score_summary(
        {"total_return": 0.4, "max_drawdown": 0.05, "sharpe": 2.0, "trade_count": 20}
    )
    high_dd = score_summary(
        {"total_return": 0.4, "max_drawdown": 0.20, "sharpe": 2.0, "trade_count": 20}
    )

    assert low_dd.score > high_dd.score


def test_rank_summary_records_uses_runtime_summary_schema():
    ranked = rank_summary_records(
        [
            ("a", "a", {"total": 0.5, "max_dd": 0.30, "sharpe": 1.0, "trade_count": 100}),
            ("b", "a", {"total": 0.45, "max_dd": 0.05, "sharpe": 1.0, "trade_count": 100}),
        ]
    )

    assert [item.candidate_id for item in ranked] == ["b", "a"]


def test_summary_from_paper_ledger_feeds_optimizer(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "cash": 1_000_000,
                "positions": {},
                "pending": [],
                "trades": [],
                "summary": {
                    "total_return": 0.12,
                    "max_drawdown": 0.03,
                    "trades": 22,
                    "win_rate": 0.55,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summary_from_paper_ledger(ledger)
    score = score_summary(summary, candidate_id="paper-a", market="a")

    assert summary["total_return"] == 0.12
    assert score.trade_count == 22
    assert score.score > 0


def test_baseline_only_paper_ledger_is_not_runtime_evidence(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "cash": 1_000_000,
                "positions": {},
                "pending": [],
                "trades": [],
                "equity_curve": [
                    {
                        "time": "2026-06-11 09:30:00",
                        "equity": 1_000_000,
                        "baseline": True,
                        "reason": "ledger_baseline",
                    }
                ],
                "summary": {
                    "total_return": 0.0,
                    "max_drawdown": 0.0,
                    "trades": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    scored, missing = score_runtime_sources(
        [RuntimeSummarySource("paper", "a", "paper_ledger", str(ledger))]
    )

    assert summary_from_paper_ledger(ledger) == {}
    assert scored == []
    assert missing[0]["reason"] == "baseline_only"


def test_no_activity_paper_ledger_is_not_runtime_evidence(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "cash": 1_000_000,
                "positions": {},
                "pending": [],
                "trades": [],
                "equity_curve": [
                    {"time": "2026-06-11 09:30:00", "equity": 1_000_000},
                    {"time": "2026-06-11 09:35:00", "equity": 1_000_000},
                ],
                "summary": {
                    "total_return": 0.0,
                    "max_drawdown": 0.0,
                    "trades": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    scored, missing = score_runtime_sources(
        [RuntimeSummarySource("paper", "a", "paper_ledger", str(ledger))]
    )

    assert summary_from_paper_ledger(ledger) == {}
    assert scored == []
    assert missing[0]["reason"] == "no_activity"


def test_build_candidate_report_is_serializable_and_ranked():
    report = build_candidate_report("us")

    assert report["market"] == "us"
    assert report["candidates"][0]["id"] == "us_core9_default"
    assert report["candidates"][0]["score"]["score"] >= report["candidates"][1]["score"]["score"]
    assert default_strategy_candidates("us")[0].market == "us"


def test_score_runtime_sources_handles_paper_and_backtest_summaries(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps({"summary": {"total_return": 0.2, "max_drawdown": 0.04, "trades": 12}}),
        encoding="utf-8",
    )
    backtest = tmp_path / "bt_summary.json"
    backtest.write_text(
        json.dumps({"market": "a", "total": 0.1, "max_dd": 0.02, "trade_count": 20}),
        encoding="utf-8",
    )

    scored, missing = score_runtime_sources(
        [
            RuntimeSummarySource("paper", "a", "paper_ledger", str(ledger)),
            RuntimeSummarySource("bt", "a", "backtest_summary", str(backtest)),
            RuntimeSummarySource(
                "bt_duplicate",
                "a",
                "backtest_summary",
                str(backtest).replace("\\", "/"),
            ),
            RuntimeSummarySource("missing", "a", "paper_ledger", str(tmp_path / "missing.json")),
        ]
    )

    assert [item.source.id for item in scored] == ["paper", "bt"]
    assert missing[0]["id"] == "missing"
    assert missing[0]["reason"] == "missing"


def test_discover_and_write_optimization_report(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "custom_us_summary.json").write_text(
        json.dumps(
            {
                "market": "us",
                "total": 0.33,
                "max_dd": 0.05,
                "sharpe": 3.0,
                "trade_count": 30,
            }
        ),
        encoding="utf-8",
    )
    output_json = tmp_path / "out" / "optimization.json"
    output_md = tmp_path / "out" / "optimization.md"
    output_decision = tmp_path / "out" / "decision.json"
    output_state = tmp_path / "out" / "decision_state.json"
    output_overrides = tmp_path / "out" / "runtime_overrides.json"

    discovered = discover_backtest_summary_sources(reports, markets=("us",))
    report = write_optimization_report(
        output_json,
        output_markdown=output_md,
        output_decision=output_decision,
        output_decision_state=output_state,
        output_runtime_overrides=output_overrides,
        market="us",
        report_dir=reports,
    )
    saved = json.loads(output_json.read_text(encoding="utf-8"))
    decision = json.loads(output_decision.read_text(encoding="utf-8"))
    state = json.loads(output_state.read_text(encoding="utf-8"))
    overrides = json.loads(output_overrides.read_text(encoding="utf-8"))
    markdown = output_md.read_text(encoding="utf-8")

    assert discovered[0].id == "custom_us"
    assert any(item["source"]["id"] == "custom_us" for item in report["runtime_ranking"])
    assert saved["runtime_ranking"][0]["score"]["score"] >= saved["runtime_ranking"][-1]["score"]["score"]
    assert decision["decisions"][0]["risk_state"] == "ok"
    assert decision["decisions"][0]["ready_to_apply"] is False
    assert state["market_states"][0]["status"] == "stable"
    assert overrides["override_count"] == 0
    assert "custom_us" in markdown
    assert "| Rank | Market | Source |" in render_optimization_markdown(report)
    assert "## Action Suggestions" in markdown


def test_build_optimization_report_merges_candidates_and_runtime(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "a_tmp_summary.json").write_text(
        json.dumps({"market": "a", "total": 0.01, "max_dd": 0.01, "trade_count": 11}),
        encoding="utf-8",
    )

    report = build_optimization_report("a", report_dir=reports)

    assert report["market"] == "a"
    assert report["candidate_ranking"][0]["id"] == "a_full_market_balanced"
    assert any(item["source"]["id"] == "a_tmp" for item in report["runtime_ranking"])
    assert report["recommendations"][0]["embedded_candidate"] == "a_full_market_balanced"
    assert report["action_suggestions"][0]["action"] == "keep_candidate"


def test_runtime_observations_flag_live_parity_lag_without_switching():
    report = {
        "candidate_ranking": [
            {
                "id": "us_core9_default",
                "market": "us",
                "score": {
                    "total_return": 0.196,
                    "max_drawdown": 0.015,
                    "trade_count": 450,
                },
            }
        ],
        "runtime_ranking": [
            {
                "source": {
                    "id": "us_live_parity_backtest",
                    "market": "us",
                    "kind": "backtest_summary",
                    "path": "summary.json",
                },
                "score": {
                    "total_return": 0.042,
                    "max_drawdown": 0.004,
                    "trade_count": 68,
                },
                "summary": {"excess": -0.109},
            }
        ],
        "recommendations": [
            {
                "market": "us",
                "embedded_candidate": "us_core9_default",
                "best_runtime_summary": "us_live_parity_backtest",
            }
        ],
    }

    observations = build_runtime_observations(report)
    actions = build_action_suggestions(report)

    assert observations[0]["observation"] == "live_parity_runtime_lag"
    assert observations[0]["severity"] == "watch"
    assert observations[0]["target_candidate"] == "us_core9_default"
    assert "runtime excess" in observations[0]["reason"]
    assert actions[0]["action"] == "keep_candidate"


def test_runtime_gap_review_requires_enough_paper_trades():
    report = {
        "candidate_ranking": [
            {
                "id": "us_core9_default",
                "market": "us",
                "score": {
                    "total_return": 0.196,
                    "max_drawdown": 0.015,
                    "trade_count": 450,
                },
                "monitor_config": {"max_pos": 9},
            }
        ],
        "runtime_ranking": [
            {
                "source": {
                    "id": "us_live_parity_backtest",
                    "market": "us",
                    "kind": "backtest_summary",
                    "path": "summary.json",
                },
                "score": {
                    "score": 0.25,
                    "total_return": 0.23,
                    "max_drawdown": 0.03,
                    "trade_count": 23,
                },
                "summary": {},
            },
            {
                "source": {
                    "id": "us_paper_ledger",
                    "market": "us",
                    "kind": "paper_ledger",
                    "path": "ledger.json",
                },
                "score": {
                    "score": -0.04,
                    "total_return": -0.001,
                    "max_drawdown": 0.001,
                    "trade_count": 2,
                },
                "summary": {},
            },
        ],
        "recommendations": [
            {
                "market": "us",
                "embedded_candidate": "us_core9_default",
                "best_runtime_summary": "us_live_parity_backtest",
            }
        ],
    }

    assert build_action_suggestions(report)[0]["action"] == "keep_candidate"
    assert (
        build_action_suggestions(report, runtime_gap_min_trades=2)[0]["action"]
        == "review_runtime_gap"
    )


def test_bs_point_attribution_summarizes_trade_classes_and_guidance(tmp_path):
    trades = tmp_path / "trades.csv"
    rows = [
        "code,entry_date,entry_px,exit_date,exit_px,ret,bs_type,exit_bs_type,reason,post_exit_bars,post_exit_ret_5,post_exit_ret_20,post_exit_ret_60,post_exit_mfe_20,post_exit_mae_20",
        "AAA,2026-01-01 09:30:00,10,2026-01-01 10:30:00,10.1,0.010,3,3sell,small sell,60,0.004,0.006,0.008,0.012,-0.002",
        "AAA,2026-01-02 09:30:00,10,2026-01-02 10:30:00,10.2,0.020,3,3sell,small sell,60,0.004,0.006,0.008,0.012,-0.002",
        "AAA,2026-01-03 09:30:00,10,2026-01-03 10:30:00,10.08,0.008,3,3sell,small sell,60,0.004,0.006,0.008,0.012,-0.002",
        "AAA,2026-01-04 09:30:00,10,2026-01-04 10:30:00,10.12,0.012,3,3sell,small sell,60,0.004,0.006,0.008,0.012,-0.002",
        "AAA,2026-01-05 09:30:00,10,2026-01-05 10:30:00,10.09,0.009,3,3sell,small sell,60,0.004,0.006,0.008,0.012,-0.002",
        "BBB,2026-01-01 09:30:00,10,2026-01-01 10:30:00,9.9,-0.010,2,,big_level_down,60,-0.002,-0.004,-0.006,0.001,-0.008",
        "BBB,2026-01-02 09:30:00,10,2026-01-02 10:30:00,9.8,-0.020,2,,big_level_down,60,-0.002,-0.004,-0.006,0.001,-0.008",
        "BBB,2026-01-03 09:30:00,10,2026-01-03 10:30:00,10.1,0.010,2,1sell,small sell,60,0.001,0.002,0.003,0.004,-0.001",
        "BBB,2026-01-04 09:30:00,10,2026-01-04 10:30:00,9.85,-0.015,2,,big_level_down,60,-0.002,-0.004,-0.006,0.001,-0.008",
        "BBB,2026-01-05 09:30:00,10,2026-01-05 10:30:00,9.95,-0.005,2,,big_level_down,60,-0.002,-0.004,-0.006,0.001,-0.008",
        "CCC,2026-01-01 09:30:00,10,2026-01-01 10:30:00,10.2,0.020,1,,final_close,0,0,0,0,0,0",
    ]
    trades.write_text("\n".join(rows) + "\n", encoding="utf-8")
    output_json = tmp_path / "bs.json"
    output_md = tmp_path / "bs.md"

    report = write_bs_point_attribution_report(
        output_json,
        output_markdown=output_md,
        markets=("a",),
        trade_paths={"a": trades},
        min_trades=5,
    )
    groups = {item["bs_class"]: item for item in report["markets"][0]["groups"]}
    guidance = {
        item["bs_class"]: item
        for item in report["markets"][0]["ratio_guidance"]
    }
    sell_groups = {item["bs_class"]: item for item in report["markets"][0]["sell_groups"]}
    sell_guidance = {
        item["bs_class"]: item
        for item in report["markets"][0]["sell_ratio_guidance"]
    }

    assert groups["3"]["trade_count"] == 5
    assert groups["3"]["win_rate"] == 1.0
    assert groups["3"]["avg_hold_hours"] == 1.0
    assert guidance["3"]["action"] == "allow_boost"
    assert guidance["3"]["ratio_multiplier"] == 1.10
    assert guidance["2"]["action"] == "reduce"
    assert guidance["2"]["ratio_multiplier"] == 0.75
    assert guidance["1"]["action"] == "watch"
    assert sell_groups["3"]["trade_count"] == 5
    assert sell_groups["3"]["post_exit_sample_count"] == 5
    assert sell_groups["3"]["avg_post_exit_ret_20"] == pytest.approx(0.006)
    assert sell_groups["big_down"]["trade_count"] == 4
    assert sell_groups["1"]["trade_count"] == 1
    assert sell_groups["final"]["trade_count"] == 1
    assert sell_guidance["3"]["action"] == "review_scale_out"
    assert sell_guidance["3"]["sell_ratio"] == 1.0
    assert sell_guidance["big_down"]["sell_ratio"] == 1.0
    assert json.loads(output_json.read_text(encoding="utf-8"))["version"] == 2
    assert "Chanlun Buy/Sell Point Attribution" in output_md.read_text(encoding="utf-8")
    assert "Sell Points" in output_md.read_text(encoding="utf-8")
    assert "Post 20" in output_md.read_text(encoding="utf-8")
    assert "allow_boost" in render_bs_point_attribution_markdown(report)


def test_layer_attribution_summarizes_layers_levels_and_guidance(tmp_path):
    summary = tmp_path / "summary.json"
    trades = tmp_path / "trades.csv"
    summary.write_text(
        json.dumps(
            {
                "total_return": 0.032,
                "buy_hold": -0.041,
                "max_drawdown": 0.018,
                "trade_count": 3,
                "signal_event_count": 4,
                "core_signal_level": 2,
                "swing_signal_level": 1,
            }
        ),
        encoding="utf-8",
    )
    rows = [
        "code,entry_date,entry_px,exit_date,exit_px,ret,entry_layer,exit_layer,entry_level,exit_level,reason",
        "TSLA,2026-06-09 17:16:00,390,2026-06-09 18:16:00,397,0.018,swing,swing,1,1,1sell",
        "TSLA,2026-06-10 15:30:00,380,2026-06-10 16:30:00,385,0.013,swing,swing,1,1,3sell",
        "TSLA,2026-06-10 17:00:00,384,2026-06-10 17:30:00,381,-0.008,scalp,scalp,0,0,1m sell",
    ]
    trades.write_text("\n".join(rows) + "\n", encoding="utf-8")
    output_json = tmp_path / "layer.json"
    output_md = tmp_path / "layer.md"

    report = write_layer_attribution_report(
        output_json,
        output_markdown=output_md,
        summary_path=summary,
        trade_path=trades,
        min_trades=2,
    )
    entry_layers = {
        item["bs_class"]: item
        for item in report["entry_layer_groups"]
    }
    entry_levels = {
        item["bs_class"]: item
        for item in report["entry_level_groups"]
    }
    exit_layers = {
        item["bs_class"]: item
        for item in report["exit_layer_groups"]
    }
    guidance = {
        item["layer"]: item
        for item in report["layer_guidance"]
    }

    assert report["summary"]["core_signal_level"] == 2
    assert report["summary"]["swing_signal_level"] == 1
    assert report["trade_count"] == 3
    assert entry_layers["swing"]["trade_count"] == 2
    assert entry_layers["swing"]["sample_state"] == "enough"
    assert entry_layers["scalp"]["sample_state"] == "thin"
    assert entry_levels["1"]["trade_count"] == 2
    assert exit_layers["swing"]["trade_count"] == 2
    assert guidance["swing"]["action"] == "keep_or_boost"
    assert guidance["scalp"]["action"] == "watch"
    assert json.loads(output_json.read_text(encoding="utf-8"))["version"] == 1
    markdown = output_md.read_text(encoding="utf-8")
    assert "Chanlun Layer Attribution Report" in markdown
    assert "Entry Layers" in markdown
    assert "keep_or_boost" in render_layer_attribution_markdown(report)


def test_bs_point_regime_attribution_joins_daily_regimes(tmp_path):
    summary = tmp_path / "summary.json"
    trades = tmp_path / "trades.csv"
    summary.write_text(
        json.dumps(
            {
                "market_regime_segments": {
                    "daily_regimes": [
                        {"date": "2026-01-01", "regime": "bull"},
                        {"date": "2026-01-02", "regime": "bull"},
                        {"date": "2026-01-03", "regime": "range"},
                        {"date": "2026-01-04", "regime": "bear"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    rows = [
        "code,entry_date,entry_px,exit_date,exit_px,ret,bs_type,exit_bs_type,reason,post_exit_bars,post_exit_ret_20,post_exit_mfe_20,post_exit_mae_20",
        "AAA,2026-01-01 09:30:00,10,2026-01-01 10:30:00,10.10,0.010,3,3sell,small sell,60,0.006,0.012,-0.002",
        "AAA,2026-01-02 09:30:00,10,2026-01-02 10:30:00,10.20,0.020,3,3sell,small sell,60,0.006,0.012,-0.002",
        "BBB,2026-01-03 09:30:00,10,2026-01-03 10:30:00,9.90,-0.010,2,2sell,small sell,60,-0.004,0.001,-0.008",
        "CCC,2026-01-04 09:30:00,10,2026-01-04 10:30:00,9.80,-0.020,1,,big_level_down,60,-0.006,0.001,-0.010",
        "DDD,2026-01-09 09:30:00,10,2026-01-09 10:30:00,10.05,0.005,3,3sell,small sell,60,0.001,0.002,-0.001",
    ]
    trades.write_text("\n".join(rows) + "\n", encoding="utf-8")

    report = build_bs_point_regime_attribution_report(
        markets=("a",),
        summary_paths={"a": summary},
        trade_paths={"a": trades},
        min_trades=2,
    )
    groups = {
        (item["regime"], item["bs_class"]): item
        for item in report["markets"][0]["buy_groups"]
    }
    guidance = {
        (item["regime"], item["bs_class"]): item
        for item in report["markets"][0]["buy_ratio_guidance"]
    }
    sell_groups = {
        (item["regime"], item["bs_class"]): item
        for item in report["markets"][0]["sell_groups"]
    }

    assert report["markets"][0]["daily_regime_count"] == 4
    assert groups[("bull", "3")]["trade_count"] == 2
    assert groups[("bull", "3")]["win_rate"] == 1.0
    assert groups[("range", "2")]["trade_count"] == 1
    assert groups[("bear", "1")]["trade_count"] == 1
    assert groups[("unknown", "3")]["trade_count"] == 1
    assert guidance[("bull", "3")]["action"] == "allow_regime_boost"
    assert guidance[("unknown", "3")]["action"] == "watch"
    assert sell_groups[("bear", "big_down")]["trade_count"] == 1
    markdown = render_bs_point_regime_attribution_markdown(report)
    assert "Buy/Sell Point Regime Attribution" in markdown
    assert "Sell Points By Regime" in markdown


def test_regime_ratio_impact_report_classifies_and_aggregates(tmp_path):
    from chanlun.recursive_bt.strategy_optimizer import (
        build_regime_ratio_impact_report,
        render_regime_ratio_impact_markdown,
    )

    def write_summary(name, total, max_dd, sharpe=5.0, trades=1000, mults=None):
        path = tmp_path / name
        payload = {
            "market": "a",
            "total": total,
            "max_dd": max_dd,
            "sharpe": sharpe,
            "trade_count": trades,
        }
        if mults:
            payload["regime_bs_ratio_multipliers"] = mults
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    default_mtf3 = write_summary("a_default.json", 0.6696, 0.0341)
    default_all_a = write_summary("b_default.json", 1.2812, 0.0699)
    boost_mtf3 = write_summary(
        "a_boost.json", 0.6877, 0.0345, mults={"bear": {"3": 1.25}}
    )
    boost_all_a = write_summary(
        "b_boost.json", 1.3300, 0.0690, mults={"bear": {"3": 1.25}}
    )
    reduce_mtf3 = write_summary(
        "a_reduce.json", 0.6652, 0.0315, mults={"bull": {"1": 0.5}, "range": {"1": 0.5}}
    )
    mixed_keep = write_summary("a_mixed_keep.json", 0.6650, 0.0341, mults={"bear": {"3": 1.25}})
    missing = tmp_path / "nope.json"

    windows = [
        {"market": "a", "candidate": "bear3_boost", "window": "a_mtf3_300",
         "default_summary": default_mtf3, "candidate_summary": boost_mtf3},
        {"market": "a", "candidate": "bear3_boost", "window": "a_all_5m30m_max30",
         "default_summary": default_all_a, "candidate_summary": boost_all_a},
        {"market": "a", "candidate": "weak1_reduce", "window": "a_mtf3_300",
         "default_summary": default_mtf3, "candidate_summary": reduce_mtf3},
        {"market": "a", "candidate": "mixed", "window": "w1",
         "default_summary": default_mtf3, "candidate_summary": boost_mtf3},
        {"market": "a", "candidate": "mixed", "window": "w2",
         "default_summary": default_mtf3, "candidate_summary": mixed_keep},
        {"market": "us", "candidate": "weak1_reduce", "window": "us_core9",
         "default_summary": default_mtf3, "candidate_summary": missing},
    ]

    report = build_regime_ratio_impact_report(windows=windows)

    rows = {(r["market"], r["candidate"], r["window"]): r for r in report["windows"]}
    assert rows[("a", "bear3_boost", "a_mtf3_300")]["action"] == "review_regime_ratio"
    assert rows[("a", "bear3_boost", "a_mtf3_300")]["multipliers"] == {"bear": {"3": 1.25}}
    assert rows[("a", "bear3_boost", "a_all_5m30m_max30")]["action"] == "review_regime_ratio"
    assert rows[("a", "weak1_reduce", "a_mtf3_300")]["action"] == "watch_defensive"

    verdicts = {(v["market"], v["candidate"]): v for v in report["verdicts"]}
    assert verdicts[("a", "bear3_boost")]["verdict"] == "review_regime_ratio"
    assert verdicts[("a", "bear3_boost")]["positive_windows"] == 2
    assert verdicts[("a", "weak1_reduce")]["verdict"] == "watch_defensive"
    assert verdicts[("a", "mixed")]["verdict"] == "keep_default"
    assert verdicts[("us", "weak1_reduce")]["verdict"] == "evidence_limited"
    assert any(m["kind"] == "candidate_summary" for m in report["missing_sources"])

    markdown = render_regime_ratio_impact_markdown(report)
    assert "bear3_boost" in markdown
    assert "review_regime_ratio" in markdown


def test_bs_point_regime_policy_keeps_ratio_changes_review_only():
    regime_report = {
        "markets": [
            {
                "market": "a",
                "buy_groups": [
                    {
                        "regime": "bull",
                        "bs_class": "3",
                        "trade_count": 35,
                        "sample_state": "enough",
                        "win_rate": 0.65,
                        "avg_return": 0.009,
                        "median_return": 0.004,
                        "max_drawdown": 0.04,
                    },
                    {
                        "regime": "range",
                        "bs_class": "1",
                        "trade_count": 50,
                        "sample_state": "enough",
                        "win_rate": 0.54,
                        "avg_return": 0.002,
                        "median_return": 0.001,
                        "max_drawdown": 0.20,
                    },
                    {
                        "regime": "bear",
                        "bs_class": "2",
                        "trade_count": 3,
                        "sample_state": "thin",
                        "win_rate": 0.67,
                        "avg_return": 0.010,
                        "median_return": 0.010,
                        "max_drawdown": 0.0,
                    },
                ],
                "buy_ratio_guidance": [
                    {
                        "regime": "bull",
                        "bs_class": "3",
                        "action": "allow_regime_boost",
                        "ratio_multiplier": 1.1,
                    },
                    {
                        "regime": "range",
                        "bs_class": "1",
                        "action": "keep",
                        "ratio_multiplier": 1.0,
                    },
                    {
                        "regime": "bear",
                        "bs_class": "2",
                        "action": "watch",
                        "ratio_multiplier": 1.0,
                    },
                ],
            }
        ],
        "missing_sources": [],
    }

    report = build_bs_point_regime_policy_report(regime_report, min_trades=30)
    policies = {
        (item["regime"], item["bs_class"]): item
        for item in report["policies"]
    }

    assert policies[("bull", "3")]["policy_action"] == "review_regime_ratio_boost"
    assert policies[("bull", "3")]["candidate_ratio_multiplier"] == 1.1
    assert policies[("range", "1")]["policy_action"] == "watch_positive_regime_edge"
    assert policies[("range", "1")]["candidate_ratio_multiplier"] == 1.0
    assert policies[("bear", "2")]["policy_action"] == "evidence_limited"
    markdown = render_bs_point_regime_policy_markdown(report)
    assert "Buy Point Regime Policy" in markdown
    assert "review_regime_ratio_boost" in markdown


def test_bs_point_ratio_state_requires_confirmation_before_override(tmp_path):
    report = {
        "version": 1,
        "markets": [
            {
                "market": "us",
                "ratio_guidance": [
                    {
                        "bs_class": "3",
                        "action": "allow_boost",
                        "ratio_multiplier": 1.1,
                        "reason": "strong class 3",
                        "trade_count": 68,
                        "win_rate": 0.8,
                        "avg_return": 0.006,
                        "max_drawdown": 0.02,
                    }
                ],
            }
        ],
    }
    state_path = tmp_path / "ratio_state.json"
    overrides_path = tmp_path / "ratio_overrides.json"

    first = update_bs_point_ratio_state_file(
        state_path,
        report,
        confirmation_threshold=3,
        now="2026-06-11T09:30:00",
    )
    second = update_bs_point_ratio_state_file(
        state_path,
        report,
        confirmation_threshold=3,
        now="2026-06-11T09:35:00",
    )
    third = update_bs_point_ratio_state_file(
        state_path,
        report,
        confirmation_threshold=3,
        now="2026-06-11T09:40:00",
    )
    overrides = write_bs_point_ratio_overrides_file(overrides_path, third)

    assert first["ratio_states"][0]["status"] == "confirming"
    assert second["ratio_states"][0]["confirmations"] == 2
    assert third["ratio_states"][0]["apply_allowed"] is True
    assert build_bs_point_ratio_overrides(first)["override_count"] == 0
    assert overrides["override_count"] == 1
    assert bs_point_ratio_multipliers_for_market(overrides_path, "us") == {"3": 1.1}


def test_bs_point_ratio_impact_report_compares_override_to_baseline(tmp_path):
    current = tmp_path / "us_current.json"
    baseline = tmp_path / "us_baseline.json"
    current.write_text(
        json.dumps(
            {
                "market": "us",
                "total": 0.046,
                "max_dd": 0.0047,
                "excess": -0.105,
                "trade_count": 68,
                "bs_point_ratio_overrides_enabled": True,
                "bs_point_ratio_multipliers": {"3": 1.1},
            }
        ),
        encoding="utf-8",
    )
    baseline.write_text(
        json.dumps(
            {
                "market": "us",
                "total": 0.042,
                "max_dd": 0.0043,
                "excess": -0.109,
                "trade_count": 68,
                "bs_point_ratio_overrides_enabled": False,
                "bs_point_ratio_multipliers": {},
            }
        ),
        encoding="utf-8",
    )

    report = build_bs_point_ratio_impact_report(
        markets=("us",),
        summary_paths={"us": current},
        baseline_summary_paths={"us": baseline},
    )
    item = report["markets"][0]

    assert item["action"] == "keep_override"
    assert item["delta_total_return"] == pytest.approx(0.004)
    assert item["delta_max_drawdown"] == pytest.approx(0.0004)
    assert item["bs_point_ratio_multipliers"] == {"3": 1.1}
    assert "keep_override" in render_bs_point_ratio_impact_markdown(report)


def test_bs_point_ratio_impact_report_handles_no_active_override(tmp_path):
    current = tmp_path / "a_current.json"
    current.write_text(
        json.dumps(
            {
                "market": "a",
                "total": 0.01,
                "max_dd": 0.02,
                "trade_count": 12,
                "bs_point_ratio_overrides_enabled": True,
                "bs_point_ratio_multipliers": {},
            }
        ),
        encoding="utf-8",
    )

    report = build_bs_point_ratio_impact_report(
        markets=("a",),
        summary_paths={"a": current},
    )

    assert report["markets"][0]["action"] == "no_active_override"
    assert report["markets"][0]["baseline_required"] is False
    assert report["markets"][0]["delta_total_return"] == pytest.approx(0.0)
    assert report["missing_sources"] == []


def test_sell_policy_impact_report_watches_higher_return_with_worse_drawdown(tmp_path):
    default = tmp_path / "us_default.json"
    candidate = tmp_path / "us_sell12.json"
    default.write_text(
        json.dumps(
            {
                "market": "us",
                "total": 0.046,
                "max_dd": 0.0047,
                "excess": -0.105,
                "trade_count": 68,
                "sell_classes": [1, 2, 3],
            }
        ),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps(
            {
                "market": "us",
                "total": 0.051,
                "max_dd": 0.0130,
                "excess": -0.099,
                "trade_count": 38,
                "sell_classes": [1, 2],
            }
        ),
        encoding="utf-8",
    )

    report = build_sell_policy_impact_report(
        markets=("us",),
        summary_paths={"us": default},
        candidate_summary_paths={"us": candidate},
    )
    item = report["markets"][0]

    assert item["action"] == "watch_drawdown"
    assert item["delta_total_return"] == pytest.approx(0.005)
    assert item["delta_max_drawdown"] == pytest.approx(0.0083)
    assert item["candidate_sell_classes"] == [1, 2]
    assert "watch_drawdown" in render_sell_policy_impact_markdown(report)


def test_sell_policy_impact_report_reviews_controlled_improvement(tmp_path):
    default = tmp_path / "a_default.json"
    candidate = tmp_path / "a_sell12.json"
    default.write_text(
        json.dumps({"market": "a", "total": 0.02, "max_dd": 0.02, "trade_count": 40}),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps(
            {
                "market": "a",
                "total": 0.026,
                "max_dd": 0.023,
                "trade_count": 38,
                "sell_classes": [1, 2],
                "sell_ratio_overrides": {"3": 0.5},
                "sell_ratio_override_scope": "up",
                "after_3sell_reentry_buy_classes": [3],
                "after_3sell_reentry_mid_buy_classes": [3],
            }
        ),
        encoding="utf-8",
    )

    report = build_sell_policy_impact_report(
        markets=("a",),
        summary_paths={"a": default},
        candidate_summary_paths={"a": candidate},
    )

    assert report["markets"][0]["action"] == "review_sell_policy"
    assert report["markets"][0]["default_sell_classes"] == [1, 2, 3]
    assert report["markets"][0]["candidate_sell_ratio_overrides"] == {"3": 0.5}
    assert report["markets"][0]["candidate_sell_ratio_override_scope"] == "up"
    assert report["markets"][0]["candidate_after_3sell_reentry_buy_classes"] == [3]
    assert report["markets"][0]["candidate_after_3sell_reentry_mid_buy_classes"] == [3]


def test_sell_policy_impact_report_watches_defensive_tradeoff(tmp_path):
    default = tmp_path / "a_default.json"
    candidate = tmp_path / "a_sell3_rebuy3.json"
    default.write_text(
        json.dumps({"market": "a", "total": 0.6696, "max_dd": 0.0341, "trade_count": 3504}),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps(
            {
                "market": "a",
                "total": 0.6570,
                "max_dd": 0.0230,
                "trade_count": 2984,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "after_3sell_reentry_buy_classes": [3],
            }
        ),
        encoding="utf-8",
    )

    report = build_sell_policy_impact_report(
        markets=("a",),
        summary_paths={"a": default},
        candidate_summary_paths={"a": candidate},
        candidate_label="sell3_rebuy3",
    )
    item = report["markets"][0]

    assert item["action"] == "watch_defensive"
    assert item["candidate_op_level"] == "1m"
    assert item["candidate_mid_level"] == "5m"
    assert item["candidate_big_level"] == "30m"


def test_market_regime_stress_report_compares_candidates_by_regime(tmp_path):
    default = tmp_path / "default.json"
    candidate = tmp_path / "candidate.json"
    default.write_text(
        json.dumps(
            {
                "market": "a",
                "total": 0.30,
                "max_dd": 0.05,
                "trade_count": 100,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "market_regime_segments": {
                    "segments": {
                        "bull": {
                            "days": 25,
                            "strategy_return": 0.12,
                            "benchmark_return": 0.15,
                            "excess_return": -0.03,
                            "max_drawdown": 0.02,
                            "sharpe": 4.0,
                            "trade_count": 40,
                        },
                        "range": {
                            "days": 30,
                            "strategy_return": 0.10,
                            "benchmark_return": 0.00,
                            "excess_return": 0.10,
                            "max_drawdown": 0.03,
                            "sharpe": 3.0,
                            "trade_count": 45,
                        },
                        "bear": {
                            "days": 8,
                            "strategy_return": -0.02,
                            "benchmark_return": -0.08,
                            "excess_return": 0.06,
                            "max_drawdown": 0.04,
                            "sharpe": -1.0,
                            "trade_count": 15,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps(
            {
                "market": "a",
                "total": 0.31,
                "max_dd": 0.04,
                "trade_count": 90,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "market_regime_segments": {
                    "segments": {
                        "bull": {
                            "days": 25,
                            "strategy_return": 0.115,
                            "benchmark_return": 0.15,
                            "excess_return": -0.035,
                            "max_drawdown": 0.015,
                            "sharpe": 4.2,
                            "trade_count": 35,
                        },
                        "range": {
                            "days": 30,
                            "strategy_return": 0.13,
                            "benchmark_return": 0.00,
                            "excess_return": 0.13,
                            "max_drawdown": 0.029,
                            "sharpe": 4.0,
                            "trade_count": 40,
                        },
                        "bear": {
                            "days": 8,
                            "strategy_return": -0.01,
                            "benchmark_return": -0.08,
                            "excess_return": 0.07,
                            "max_drawdown": 0.02,
                            "sharpe": -0.2,
                            "trade_count": 12,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_market_regime_stress_report(
        markets=("a",),
        summary_paths={"a": {"default": default, "candidate": candidate}},
        min_regime_days=10,
    )
    rows = {
        (row["strategy"], row["regime"]): row
        for row in report["markets"][0]["rows"]
    }

    assert rows[("default", "bull")]["action"] == "baseline"
    assert rows[("candidate", "bull")]["action"] == "defensive_improvement"
    assert rows[("candidate", "range")]["action"] == "improves_regime"
    assert rows[("candidate", "bear")]["action"] == "evidence_limited"
    assert report["markets"][0]["best_by_regime"]["range"]["strategy"] == "candidate"
    markdown = render_market_regime_stress_markdown(report)
    assert "Chanlun Market Regime Stress Report" in markdown
    assert "Best By Regime" in markdown
    assert "defensive_improvement" in markdown


def test_regime_strategy_policy_report_requires_repeated_positive_evidence():
    source_a = {
        "markets": [
            {
                "market": "a",
                "rows": [
                    {
                        "strategy": "default",
                        "regime": "range",
                        "action": "baseline",
                        "days": 30,
                        "strategy_return": 0.10,
                        "excess_return": 0.10,
                        "max_drawdown": 0.030,
                        "delta_strategy_return": 0.0,
                        "delta_max_drawdown": 0.0,
                        "regime_score": 0.040,
                    },
                    {
                        "strategy": "sell3_rebuy3_up",
                        "regime": "range",
                        "action": "defensive_improvement",
                        "days": 30,
                        "strategy_return": 0.105,
                        "excess_return": 0.105,
                        "max_drawdown": 0.025,
                        "delta_strategy_return": 0.005,
                        "delta_max_drawdown": -0.005,
                        "regime_score": 0.055,
                    },
                    {
                        "strategy": "default",
                        "regime": "bull",
                        "action": "baseline",
                        "days": 20,
                        "strategy_return": 0.08,
                        "excess_return": 0.02,
                        "max_drawdown": 0.015,
                        "delta_strategy_return": 0.0,
                        "delta_max_drawdown": 0.0,
                        "regime_score": 0.050,
                    },
                    {
                        "strategy": "candidate_without_default",
                        "regime": "bear",
                        "action": "watch",
                        "days": 12,
                        "strategy_return": 0.01,
                        "excess_return": 0.03,
                        "max_drawdown": 0.010,
                        "delta_strategy_return": 0.0,
                        "delta_max_drawdown": 0.0,
                        "regime_score": 0.010,
                    },
                ],
            }
        ]
    }
    source_b = {
        "markets": [
            {
                "market": "a",
                "rows": [
                    {
                        "strategy": "default",
                        "regime": "range",
                        "action": "baseline",
                        "days": 25,
                        "strategy_return": 0.09,
                        "excess_return": 0.09,
                        "max_drawdown": 0.020,
                        "delta_strategy_return": 0.0,
                        "delta_max_drawdown": 0.0,
                        "regime_score": 0.050,
                    },
                    {
                        "strategy": "sell3_rebuy3_up",
                        "regime": "range",
                        "action": "improves_regime",
                        "days": 25,
                        "strategy_return": 0.10,
                        "excess_return": 0.10,
                        "max_drawdown": 0.021,
                        "delta_strategy_return": 0.010,
                        "delta_max_drawdown": 0.001,
                        "regime_score": 0.058,
                    },
                ],
            }
        ]
    }

    single_source = build_regime_strategy_policy_report(
        {"primary": source_a},
        min_supporting_sources=2,
    )
    single_policies = {
        (item["market"], item["regime"]): item
        for item in single_source["policies"]
    }

    assert single_policies[("a", "range")]["policy_action"] == "watch_regime_candidate"
    assert single_policies[("a", "range")]["supporting_sources"] == 1
    assert single_policies[("a", "bull")]["policy_action"] == "keep_default"
    assert single_policies[("a", "bear")]["policy_action"] == "evidence_limited"

    multi_source = build_regime_strategy_policy_report(
        {"primary": source_a, "second_window": source_b},
        min_supporting_sources=2,
    )
    multi_policies = {
        (item["market"], item["regime"]): item
        for item in multi_source["policies"]
    }

    assert multi_policies[("a", "range")]["policy_action"] == "review_regime_candidate"
    assert multi_policies[("a", "range")]["recommended_strategy"] == "sell3_rebuy3_up"
    assert multi_policies[("a", "range")]["supporting_sources"] == 2
    markdown = render_regime_strategy_policy_markdown(multi_source)
    assert "Chanlun Regime Strategy Policy Report" in markdown
    assert "review_regime_candidate" in markdown


def test_action_suggestions_switch_unmatched_current_candidate():
    report = build_candidate_report("a")
    report = {
        "candidate_ranking": report["candidates"],
        "runtime_ranking": [],
        "recommendations": [
            {
                "market": "a",
                "embedded_candidate": "a_full_market_balanced",
                "best_runtime_summary": "",
            }
        ],
    }

    actions = build_action_suggestions(
        report,
        current_candidate_ids={"a": "custom_monitor_config"},
    )

    assert actions[0]["action"] == "switch_candidate"
    assert actions[0]["target_candidate"] == "a_full_market_balanced"
    assert actions[0]["monitor_config"]["max_pos"] == 30


def test_action_suggestions_degrade_when_paper_drawdown_worsens():
    candidate_report = build_candidate_report("a")
    report = {
        "candidate_ranking": candidate_report["candidates"],
        "runtime_ranking": [
            {
                "source": {
                    "id": "a_paper_ledger",
                    "market": "a",
                    "kind": "paper_ledger",
                    "path": "ledger.json",
                },
                "score": {
                    "score": -0.1,
                    "total_return": 0.05,
                    "max_drawdown": 0.12,
                    "sharpe": 0.0,
                    "trade_count": 20,
                },
                "summary": {},
            }
        ],
        "recommendations": [
            {
                "market": "a",
                "embedded_candidate": "a_full_market_balanced",
                "best_runtime_summary": "a_paper_ledger",
            }
        ],
    }

    actions = build_action_suggestions(
        report,
        current_candidate_ids={"a": "a_full_market_balanced"},
    )

    assert actions[0]["action"] == "degrade_candidate"
    assert actions[0]["target_candidate"] == "a_full_market_low_dd"
    assert "paper drawdown" in actions[0]["reason"]


def test_build_decision_artifact_marks_switches_ready_to_apply():
    artifact = build_decision_artifact(
        {
            "market": "a",
            "action_suggestions": [
                {
                    "market": "a",
                    "action": "switch_candidate",
                    "current_candidate": "custom_monitor_config",
                    "target_candidate": "a_full_market_balanced",
                    "best_runtime_summary": "a_live_parity_backtest",
                    "reason": "custom config",
                    "monitor_config": {"max_pos": 30},
                }
            ],
        }
    )

    assert artifact["version"] == 1
    assert artifact["decisions"][0]["risk_state"] == "switch_ready"
    assert artifact["decisions"][0]["ready_to_apply"] is True
    assert artifact["decisions"][0]["monitor_config"]["max_pos"] == 30


def test_decision_state_requires_repeated_ready_decisions(tmp_path):
    artifact = build_decision_artifact(
        {
            "market": "a",
            "action_suggestions": [
                {
                    "market": "a",
                    "action": "switch_candidate",
                    "current_candidate": "custom_monitor_config",
                    "target_candidate": "a_full_market_balanced",
                    "best_runtime_summary": "a_live_parity_backtest",
                    "reason": "custom config",
                    "monitor_config": {"max_pos": 30},
                }
            ],
        }
    )
    first = build_decision_state(
        artifact,
        confirmation_threshold=2,
        now="2026-06-11T10:00:00",
    )
    second = build_decision_state(
        artifact,
        first,
        confirmation_threshold=2,
        now="2026-06-11T10:05:00",
    )

    assert first["market_states"][0]["status"] == "confirming"
    assert first["market_states"][0]["apply_allowed"] is False
    assert second["market_states"][0]["confirmations"] == 2
    assert second["market_states"][0]["status"] == "apply_allowed"
    assert second["market_states"][0]["apply_allowed"] is True
    assert second["market_states"][0]["first_seen"] == "2026-06-11T10:00:00"

    path = tmp_path / "state.json"
    update_decision_state_file(
        path,
        artifact,
        confirmation_threshold=2,
        now="2026-06-11T10:00:00",
    )
    persisted = update_decision_state_file(
        path,
        artifact,
        confirmation_threshold=2,
        now="2026-06-11T10:05:00",
    )

    assert persisted["apply_allowed_count"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["market_states"][0]["status"] == "apply_allowed"


def test_runtime_overrides_only_include_apply_allowed_states(tmp_path):
    state = {
        "updated_at": "2026-06-11T10:05:00",
        "market_states": [
            {
                "market": "a",
                "action": "switch_candidate",
                "risk_state": "switch_ready",
                "target_candidate": "a_full_market_balanced",
                "decision_key": "a|switch_candidate|a_full_market_balanced",
                "confirmations": 3,
                "confirmation_threshold": 3,
                "apply_allowed": True,
                "reason": "confirmed",
                "monitor_config": {"max_pos": 30, "mid_gate": "soft"},
            },
            {
                "market": "us",
                "action": "keep_candidate",
                "risk_state": "ok",
                "target_candidate": "us_core9_default",
                "decision_key": "us|keep_candidate|us_core9_default",
                "confirmations": 3,
                "confirmation_threshold": 3,
                "apply_allowed": False,
                "reason": "stable",
                "monitor_config": {"max_pos": 9},
            },
        ],
    }
    path = tmp_path / "runtime_overrides.json"

    overrides = write_runtime_overrides_file(path, state)

    assert build_runtime_overrides(state)["override_count"] == 1
    assert overrides["overrides"][0]["market"] == "a"
    assert runtime_override_for_market(path, "a") == {"max_pos": 30, "mid_gate": "soft"}
    assert runtime_override_for_market(path, "us") == {}


def test_strategy_attribution_segments_equity_by_override_events(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "equity_curve": [
                    {"time": "2026-06-11 09:30:00", "equity": 100.0, "trades": 0},
                    {"time": "2026-06-11 10:00:00", "equity": 110.0, "trades": 1},
                    {"time": "2026-06-11 10:30:00", "equity": 105.0, "trades": 2},
                    {"time": "2026-06-11 11:00:00", "equity": 120.0, "trades": 3},
                ]
            }
        ),
        encoding="utf-8",
    )
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        json.dumps(
            {
                "event": "runtime_override_applied",
                "time": "2026-06-11 10:30:00",
                "market": "a",
                "action": "switch_candidate",
                "risk_state": "switch_ready",
                "target_candidate": "a_full_market_balanced",
                "decision_key": "a|switch_candidate|a_full_market_balanced",
                "reason": "confirmed",
                "applied_config": {"max_pos": 30},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_json = tmp_path / "attrib.json"
    output_md = tmp_path / "attrib.md"

    report = write_strategy_attribution_report(
        output_json,
        output_markdown=output_md,
        markets=("a",),
        audit_path=audit,
        ledger_paths={"a": ledger},
    )

    segments = report["markets"][0]["segments"]
    assert len(segments) == 2
    assert segments[0]["target_candidate"] == "a_full_market_balanced"
    assert segments[0]["action"] == "baseline"
    assert segments[1]["action"] == "switch_candidate"
    assert segments[1]["start_time"] == "2026-06-11 10:30:00"
    assert segments[1]["total_return"] == 120.0 / 105.0 - 1
    assert report["markets"][0]["summary"]["segment_count"] == 2
    assert json.loads(output_json.read_text(encoding="utf-8"))["version"] == 1
    assert "A Segments" in output_md.read_text(encoding="utf-8")
    assert "Runtime" not in render_strategy_attribution_markdown(report)


def test_strategy_attribution_reports_missing_or_empty_ledgers(tmp_path):
    report = build_strategy_attribution_report(
        markets=("a",),
        audit_path=tmp_path / "missing_audit.jsonl",
        ledger_paths={"a": tmp_path / "missing_ledger.json"},
    )

    assert report["markets"][0]["segments"] == []
    assert report["missing_sources"][0]["reason"] == "missing"


def test_strategy_attribution_can_initialize_baseline_ledger(tmp_path):
    ledger = tmp_path / "new_ledger.json"
    output_json = tmp_path / "attrib.json"

    baseline = ensure_paper_ledger_baseline(
        ledger,
        "a",
        now="2026-06-11 09:30:00",
    )
    duplicate = ensure_paper_ledger_baseline(
        ledger,
        "a",
        now="2026-06-11 09:31:00",
    )
    report = write_strategy_attribution_report(
        output_json,
        markets=("a",),
        audit_path=tmp_path / "missing_audit.jsonl",
        ledger_paths={"a": ledger},
        ensure_baseline_ledgers=True,
    )
    saved = json.loads(ledger.read_text(encoding="utf-8"))

    assert baseline["baseline"] is True
    assert baseline["reason"] == "ledger_baseline"
    assert duplicate is None
    assert len(saved["equity_curve"]) == 1
    assert saved["equity_curve"][0]["time"] == "2026-06-11 09:30:00"
    assert report["missing_sources"] == []
    assert report["markets"][0]["summary"]["segment_count"] == 1
    assert report["markets"][0]["segments"][0]["action"] == "baseline"


def test_mtf3_cache_coverage_report_separates_chart_and_bt_data(tmp_path):
    chart_cache = tmp_path / "chart_cache"
    chart_cache.mkdir()
    for freq in ("1m", "5m", "30m"):
        (chart_cache / f"v1_a_SH_600000_{freq}_abc.pkl").write_bytes(b"")
        (chart_cache / f"v1_us_QQQ_US_{freq}_abc.pkl").write_bytes(b"")
    (chart_cache / "v1_a_SZ_000001_5m_abc.pkl").write_bytes(b"")
    (chart_cache / "_us_mtf3_build_manifest.json").write_text(
        json.dumps(
            {
                "label": "us_core9_mtf3_chart_cache",
                "started_at": "2026-06-11T10:00:00",
                "completed_at": "2026-06-11T10:02:00",
                "codes": ["QQQ.US"],
                "frequencies": ["1m", "5m", "30m"],
                "counts": {"ok": 3, "skip": 0, "fail": 0},
                "entries": [
                    {"code": "QQQ.US", "frequency": "1m", "status": "ok"},
                    {"code": "QQQ.US", "frequency": "5m", "status": "ok"},
                    {"code": "QQQ.US", "frequency": "30m", "status": "ok"},
                ],
            }
        ),
        encoding="utf-8",
    )

    bt_data = tmp_path / "bt_data"
    bt_data.mkdir()
    (bt_data / "_build_manifest.json").write_text(
        json.dumps(
            {
                "label": "mtf3_all_a",
                "started_at": "2026-06-11T10:00:00",
                "completed_at": "2026-06-11T10:10:00",
                "requested_count": 2,
                "counts": {"ok": 1, "skip": 0, "fail": 1},
                "levels": {"small_tf": "1m", "mid_tf": "5m", "big_tf": "30m"},
                "metadata": {"limit": 300, "board_filter": "shsz"},
                "entries": [
                    {
                        "code": "SH.600000",
                        "status": "ok",
                        "has_mid_by_bar": True,
                    },
                    {
                        "code": "SZ.300001",
                        "status": "fail",
                        "reason": "insufficient_data",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with (bt_data / "SH.600000.pkl").open("wb") as fp:
        pickle.dump({"small_by_bar": {}, "big_dir_at": []}, fp)
    with (bt_data / "SZ.300001.pkl").open("wb") as fp:
        pickle.dump(
            {
                "small_by_bar": {},
                "big_dir_at": [],
                "mid_dir_at": [],
                "mid_by_bar": {},
            },
            fp,
        )
    mtf3_bt_data = tmp_path / "bt_data_mtf3"
    mtf3_bt_data.mkdir()
    with (mtf3_bt_data / "SH.600001.pkl").open("wb") as fp:
        pickle.dump(
            {
                "small_by_bar": {},
                "big_dir_at": [],
                "mid_dir_at": [],
                "mid_by_bar": {},
            },
            fp,
        )

    report = build_mtf3_cache_coverage_report(
        markets=("a", "us"),
        chart_cache_dir=chart_cache,
        bt_data_dir=bt_data,
        mtf3_bt_data_dir=mtf3_bt_data,
        bt_sample_size=10,
    )

    a_report = report["markets"][0]
    assert a_report["chart_cache_complete_mtf3_count"] == 1
    assert a_report["chart_cache_status"] == "small_sample_only"
    assert a_report["bt_data"]["file_count"] == 2
    assert a_report["bt_data"]["sample_5m30m_ready_count"] == 2
    assert a_report["bt_data"]["sample_mtf3_ready_count"] == 1
    assert a_report["bt_data"]["status"] == "mixed_mtf3"
    assert a_report["bt_data"]["build_manifest"]["label"] == "mtf3_all_a"
    assert a_report["bt_data"]["build_manifest"]["ok"] == 1
    assert a_report["bt_data"]["build_manifest"]["ok_has_mid_by_bar"] == 1
    assert a_report["bt_data"]["build_manifest"]["manifest_status"] == "completed_with_gaps"
    assert a_report["bt_data"]["build_manifest"]["failed_codes_sample"] == ["SZ.300001"]
    assert a_report["mtf3_bt_data"]["dir"] == str(mtf3_bt_data)
    assert a_report["mtf3_bt_data"]["sample_mtf3_ready_count"] == 1
    assert [action["id"] for action in a_report["recommended_next_actions"]] == [
        "build_a_mtf3_research_cache",
        "backtest_a_mtf3_reentry_candidate",
    ]
    us_report = report["markets"][1]
    assert us_report["chart_cache_complete_mtf3_count"] == 1
    assert us_report["chart_cache_build_manifest"]["label"] == "us_core9_mtf3_chart_cache"
    assert us_report["chart_cache_build_manifest"]["ok_complete_code_count"] == 1
    markdown = render_mtf3_cache_coverage_markdown(report)
    assert "BT MTF3 Sample" in markdown
    assert "Build Manifests" in markdown
    assert "mid_ok=1" in markdown
    assert "us chart_cache `us_core9_mtf3_chart_cache`" in markdown
    assert "mtf3_all_a 300 shsz" in markdown


def test_strategy_adoption_gate_blocks_mtf3_candidate_until_coverage_ready():
    coverage = {
        "markets": [
            {
                "market": "a",
                "chart_cache_complete_mtf3_count": 4,
                "bt_data": {
                    "sample_5m30m_ready_count": 300,
                    "sample_mtf3_ready_count": 0,
                },
                "mtf3_bt_data": {
                    "sample_mtf3_ready_count": 0,
                },
            }
        ]
    }
    mtf3_candidate = {
        "candidate_label": "sell3_rebuy_mid3",
        "markets": [
            {
                "market": "a",
                "action": "review_sell_policy",
                "candidate_after_3sell_reentry_mid_buy_classes": [3],
            }
        ],
    }
    mtf3_rebuy3_candidate = {
        "candidate_label": "sell3_rebuy3",
        "markets": [
            {
                "market": "a",
                "action": "watch_defensive",
                "candidate_op_level": "1m",
                "candidate_mid_level": "5m",
                "candidate_big_level": "30m",
                "candidate_after_3sell_reentry_buy_classes": [3],
            }
        ],
    }
    a_5m_candidate = {
        "candidate_label": "a_5m_sell3_rebuy3",
        "markets": [
            {
                "market": "a",
                "action": "review_sell_policy",
                "candidate_after_3sell_reentry_buy_classes": [3],
            }
        ],
    }

    report = build_strategy_adoption_gate_report(
        coverage,
        [mtf3_candidate, mtf3_rebuy3_candidate, a_5m_candidate],
    )

    by_label = {item["candidate_label"]: item for item in report["gates"]}
    assert by_label["sell3_rebuy_mid3"]["gate_action"] == "blocked_evidence"
    assert by_label["sell3_rebuy_mid3"]["evidence_ready"] is False
    assert by_label["sell3_rebuy3"]["gate_action"] == "watch_evidence_limited"
    assert by_label["sell3_rebuy3"]["evidence_ready"] is False
    assert by_label["a_5m_sell3_rebuy3"]["gate_action"] == "review_allowed"
    assert by_label["a_5m_sell3_rebuy3"]["evidence_ready"] is True
    markdown = render_strategy_adoption_gate_markdown(report)
    assert "blocked_evidence" in markdown
    assert "review_allowed" in markdown

    coverage["markets"][0]["mtf3_bt_data"]["sample_mtf3_ready_count"] = 90
    ready = build_strategy_adoption_gate_report(
        coverage,
        [mtf3_candidate, mtf3_rebuy3_candidate],
    )

    assert ready["gates"][0]["gate_action"] == "review_allowed"
    assert ready["gates"][1]["gate_action"] == "watch"


def test_match_candidate_from_monitor_config_identifies_defaults():
    assert (
        match_candidate_from_monitor_config(
            {
                "market": "us",
                "max_pos": 9,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "mid_gate": "soft",
                "nest_mode": "soft",
                "trend_3boost": True,
            }
        )
        == "us_core9_default"
    )
    assert (
        match_candidate_from_monitor_config(
            {
                "market": "a",
                "max_pos": 50,
                "op_level": "1m",
                "mid_level": "5m",
                "big_level": "30m",
                "mid_gate": "soft",
            }
        )
        == "a_full_market_low_dd"
    )
