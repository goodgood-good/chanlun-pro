"""strategy_optimizer 命令行入口 —— 从 _impl 拆出(原巨文件最后一段)。

argparse 解析 + main 调度各报告 write_*。原 strategy_optimizer.py 5585 行至此全部
拆为职责模块(models/candidates/utils/scoring/constants/reports_*/decision/cli)。
"""
from __future__ import annotations

import argparse
from typing import Optional

from chanlun.recursive_bt.strategy_optimizer.constants import (
    A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    US_2026Q1_MTF3_DEFAULT_SUMMARY,
    US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    US_MTF3_DEFAULT_SUMMARY,
    US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
)
from chanlun.recursive_bt.strategy_optimizer.decision import (
    update_bs_point_ratio_state_file,
    write_bs_point_ratio_overrides_file,
    write_optimization_report,
)
from chanlun.recursive_bt.strategy_optimizer.reports_attribution import (
    write_bs_point_attribution_report,
    write_bs_point_regime_attribution_report,
    write_bs_point_regime_policy_report,
)
from chanlun.recursive_bt.strategy_optimizer.reports_mtf3 import (
    write_mtf3_cache_coverage_report,
)
from chanlun.recursive_bt.strategy_optimizer.reports_regime import (
    write_bs_point_ratio_impact_report,
    write_market_regime_stress_report,
    write_regime_ratio_impact_report,
    write_regime_strategy_policy_report,
    write_sell_policy_impact_report,
    write_strategy_adoption_gate_report,
)
from chanlun.recursive_bt.strategy_optimizer.reports_strategy import (
    write_strategy_attribution_report,
)


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build Chanlun strategy optimization report")
    parser.add_argument("--market", choices=("a", "us"))
    parser.add_argument("--report-dir", default="D:/chanlun_pro/reports")
    parser.add_argument(
        "--output-json",
        default="D:/chanlun_pro/reports/strategy_optimization_report.json",
    )
    parser.add_argument(
        "--output-markdown",
        default="D:/chanlun_pro/reports/strategy_optimization_report.md",
    )
    parser.add_argument(
        "--output-decision",
        default="D:/chanlun_pro/reports/strategy_decision.json",
    )
    parser.add_argument(
        "--output-decision-state",
        default="D:/chanlun_pro/reports/strategy_decision_state.json",
    )
    parser.add_argument(
        "--output-runtime-overrides",
        default="D:/chanlun_pro/reports/strategy_runtime_overrides.json",
    )
    parser.add_argument(
        "--output-attribution-json",
        default="D:/chanlun_pro/reports/strategy_attribution_report.json",
    )
    parser.add_argument(
        "--output-attribution-markdown",
        default="D:/chanlun_pro/reports/strategy_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_attribution_report.json",
    )
    parser.add_argument(
        "--output-bs-point-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-ratio-state",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_state.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-overrides",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_overrides.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-impact-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_impact_report.json",
    )
    parser.add_argument(
        "--output-bs-point-ratio-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_ratio_impact_report.md",
    )
    parser.add_argument(
        "--output-bs-point-regime-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.json",
    )
    parser.add_argument(
        "--output-bs-point-regime-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_attribution_report.md",
    )
    parser.add_argument(
        "--output-bs-point-regime-policy-json",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.json",
    )
    parser.add_argument(
        "--output-bs-point-regime-policy-markdown",
        default="D:/chanlun_pro/reports/strategy_bs_point_regime_policy_report.md",
    )
    parser.add_argument("--bs-point-regime-policy-min-trades", type=int, default=30)
    parser.add_argument(
        "--output-regime-ratio-impact-json",
        default="D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.json",
    )
    parser.add_argument(
        "--output-regime-ratio-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_regime_ratio_impact_report.md",
    )
    parser.add_argument(
        "--output-market-regime-stress-json",
        default="D:/chanlun_pro/reports/strategy_market_regime_stress_report.json",
    )
    parser.add_argument(
        "--output-market-regime-stress-markdown",
        default="D:/chanlun_pro/reports/strategy_market_regime_stress_report.md",
    )
    parser.add_argument(
        "--output-us-2026q1-regime-stress-json",
        default=(
            "D:/chanlun_pro/reports/"
            "strategy_market_regime_stress_us_2026q1_report.json"
        ),
    )
    parser.add_argument(
        "--output-us-2026q1-regime-stress-markdown",
        default=(
            "D:/chanlun_pro/reports/"
            "strategy_market_regime_stress_us_2026q1_report.md"
        ),
    )
    parser.add_argument(
        "--output-regime-policy-json",
        default="D:/chanlun_pro/reports/strategy_regime_policy_report.json",
    )
    parser.add_argument(
        "--output-regime-policy-markdown",
        default="D:/chanlun_pro/reports/strategy_regime_policy_report.md",
    )
    parser.add_argument("--regime-policy-min-supporting-sources", type=int, default=2)
    parser.add_argument(
        "--us-2026q1-mtf3-default-summary",
        default=US_2026Q1_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-2026q1-mtf3-sell3-rebuy3-summary",
        default=US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-2026q1-mtf3-sell3-rebuy-mid3-summary",
        default=US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument("--market-regime-min-days", type=int, default=10)
    parser.add_argument(
        "--output-sell-policy-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell_policy_impact_report.json",
    )
    parser.add_argument(
        "--output-sell-policy-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell_policy_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3half-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3half_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3half-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3half_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3half-up-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3half_up_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3half-up-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3half_up_impact_report.md",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-candidate-summary",
        default=A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy3-default-summary",
        default=US_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy3-candidate-summary",
        default=US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-sell3-rebuy3-up-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy3-up-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy3_up_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-up-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy3-up-candidate-summary",
        default=A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-sell3-rebuy-mid3-impact-json",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.json",
    )
    parser.add_argument(
        "--output-sell3-rebuy-mid3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_sell3_rebuy_mid3_impact_report.md",
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy-mid3-default-summary",
        default=A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--a-mtf3-sell3-rebuy-mid3-candidate-summary",
        default=A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy-mid3-default-summary",
        default=US_MTF3_DEFAULT_SUMMARY,
    )
    parser.add_argument(
        "--us-mtf3-sell3-rebuy-mid3-candidate-summary",
        default=US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY,
    )
    parser.add_argument(
        "--output-a-5m-sell3-rebuy3-impact-json",
        default="D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.json",
    )
    parser.add_argument(
        "--output-a-5m-sell3-rebuy3-impact-markdown",
        default="D:/chanlun_pro/reports/strategy_a_5m_sell3_rebuy3_impact_report.md",
    )
    parser.add_argument(
        "--a-5m-sell3-rebuy3-default-summary",
        default="D:/chanlun_pro/reports/a_bt_all_a_5m30m_default_summary.json",
    )
    parser.add_argument(
        "--a-5m-sell3-rebuy3-candidate-summary",
        default="D:/chanlun_pro/reports/a_bt_all_a_5m30m_sell3_rebuy3_summary.json",
    )
    parser.add_argument(
        "--output-mtf3-cache-coverage-json",
        default="D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.json",
    )
    parser.add_argument(
        "--output-mtf3-cache-coverage-markdown",
        default="D:/chanlun_pro/reports/strategy_mtf3_cache_coverage_report.md",
    )
    parser.add_argument(
        "--output-strategy-adoption-gate-json",
        default="D:/chanlun_pro/reports/strategy_adoption_gate_report.json",
    )
    parser.add_argument(
        "--output-strategy-adoption-gate-markdown",
        default="D:/chanlun_pro/reports/strategy_adoption_gate_report.md",
    )
    parser.add_argument(
        "--mtf3-cache-chart-cache-dir",
        default="D:/chanlun_pro/chart_cache",
    )
    parser.add_argument(
        "--mtf3-cache-bt-data-dir",
        default="D:/chanlun_pro/bt_data_all_a",
    )
    parser.add_argument(
        "--mtf3-cache-mtf3-bt-data-dir",
        default="D:/chanlun_pro/bt_data_mtf3_all_a",
    )
    parser.add_argument("--mtf3-cache-bt-sample-size", type=int, default=300)
    parser.add_argument(
        "--runtime-override-audit",
        default="D:/chanlun_pro/reports/strategy_runtime_override_audit.jsonl",
    )
    parser.add_argument(
        "--no-attribution-baseline",
        action="store_true",
        help="Do not create baseline paper-ledger snapshots for attribution",
    )
    parser.add_argument("--decision-confirmation-threshold", type=int, default=3)
    parser.add_argument("--bs-point-ratio-confirmation-threshold", type=int, default=3)
    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Only use default paper ledgers and default live-parity summaries",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = make_arg_parser().parse_args(argv)
    write_optimization_report(
        args.output_json,
        output_markdown=args.output_markdown,
        output_decision=args.output_decision,
        output_decision_state=args.output_decision_state,
        output_runtime_overrides=args.output_runtime_overrides,
        market=args.market,
        include_discovered=not args.no_discover,
        report_dir=args.report_dir,
        decision_confirmation_threshold=args.decision_confirmation_threshold,
    )
    write_strategy_attribution_report(
        args.output_attribution_json,
        output_markdown=args.output_attribution_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        audit_path=args.runtime_override_audit,
        ensure_baseline_ledgers=not args.no_attribution_baseline,
    )
    bs_point_report = write_bs_point_attribution_report(
        args.output_bs_point_json,
        output_markdown=args.output_bs_point_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    bs_ratio_state = update_bs_point_ratio_state_file(
        args.output_bs_point_ratio_state,
        bs_point_report,
        confirmation_threshold=args.bs_point_ratio_confirmation_threshold,
    )
    write_bs_point_ratio_overrides_file(
        args.output_bs_point_ratio_overrides,
        bs_ratio_state,
    )
    write_bs_point_ratio_impact_report(
        args.output_bs_point_ratio_impact_json,
        output_markdown=args.output_bs_point_ratio_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    bs_point_regime_report = write_bs_point_regime_attribution_report(
        args.output_bs_point_regime_json,
        output_markdown=args.output_bs_point_regime_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    write_bs_point_regime_policy_report(
        args.output_bs_point_regime_policy_json,
        output_markdown=args.output_bs_point_regime_policy_markdown,
        bs_point_regime_report=bs_point_regime_report,
        min_trades=args.bs_point_regime_policy_min_trades,
    )
    market_regime_report = write_market_regime_stress_report(
        args.output_market_regime_stress_json,
        output_markdown=args.output_market_regime_stress_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        min_regime_days=args.market_regime_min_days,
    )
    write_regime_ratio_impact_report(
        args.output_regime_ratio_impact_json,
        output_markdown=args.output_regime_ratio_impact_markdown,
    )
    wrote_us_2026q1_regime_stress = False
    us_2026q1_regime_report = None
    if args.market in (None, "", "us"):
        us_2026q1_regime_report = write_market_regime_stress_report(
            args.output_us_2026q1_regime_stress_json,
            output_markdown=args.output_us_2026q1_regime_stress_markdown,
            markets=("us",),
            summary_paths={
                "us": {
                    "default": args.us_2026q1_mtf3_default_summary,
                    "sell3_rebuy3": args.us_2026q1_mtf3_sell3_rebuy3_summary,
                    "sell3_rebuy_mid3": args.us_2026q1_mtf3_sell3_rebuy_mid3_summary,
                }
            },
            min_regime_days=args.market_regime_min_days,
        )
        wrote_us_2026q1_regime_stress = True
    regime_policy_sources = {"primary": market_regime_report}
    if us_2026q1_regime_report is not None:
        regime_policy_sources["us_2026q1_weak"] = us_2026q1_regime_report
    write_regime_strategy_policy_report(
        args.output_regime_policy_json,
        output_markdown=args.output_regime_policy_markdown,
        report_sources=regime_policy_sources,
        min_supporting_sources=args.regime_policy_min_supporting_sources,
    )
    write_sell_policy_impact_report(
        args.output_sell_policy_impact_json,
        output_markdown=args.output_sell_policy_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
    )
    write_sell_policy_impact_report(
        args.output_sell3half_impact_json,
        output_markdown=args.output_sell3half_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        candidate_label="sell3half",
    )
    write_sell_policy_impact_report(
        args.output_sell3half_up_impact_json,
        output_markdown=args.output_sell3half_up_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        candidate_label="sell3half_up",
    )
    sell3_rebuy3_report = write_sell_policy_impact_report(
        args.output_sell3_rebuy3_impact_json,
        output_markdown=args.output_sell3_rebuy3_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        summary_paths={
            "a": args.a_mtf3_sell3_rebuy3_default_summary,
            "us": args.us_mtf3_sell3_rebuy3_default_summary,
        },
        candidate_summary_paths={
            "a": args.a_mtf3_sell3_rebuy3_candidate_summary,
            "us": args.us_mtf3_sell3_rebuy3_candidate_summary,
        },
        candidate_label="sell3_rebuy3",
    )
    sell3_rebuy3_up_report = None
    if args.market in (None, "", "a"):
        sell3_rebuy3_up_report = write_sell_policy_impact_report(
            args.output_sell3_rebuy3_up_impact_json,
            output_markdown=args.output_sell3_rebuy3_up_impact_markdown,
            markets=("a",),
            summary_paths={"a": args.a_mtf3_sell3_rebuy3_up_default_summary},
            candidate_summary_paths={
                "a": args.a_mtf3_sell3_rebuy3_up_candidate_summary,
            },
            candidate_label="sell3_rebuy3_up",
        )
    sell3_rebuy_mid3_report = write_sell_policy_impact_report(
        args.output_sell3_rebuy_mid3_impact_json,
        output_markdown=args.output_sell3_rebuy_mid3_impact_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        summary_paths={
            "a": args.a_mtf3_sell3_rebuy_mid3_default_summary,
            "us": args.us_mtf3_sell3_rebuy_mid3_default_summary,
        },
        candidate_summary_paths={
            "a": args.a_mtf3_sell3_rebuy_mid3_candidate_summary,
            "us": args.us_mtf3_sell3_rebuy_mid3_candidate_summary,
        },
        candidate_label="sell3_rebuy_mid3",
    )
    adoption_candidate_reports = [sell3_rebuy3_report, sell3_rebuy_mid3_report]
    if sell3_rebuy3_up_report is not None:
        adoption_candidate_reports.insert(1, sell3_rebuy3_up_report)
    wrote_a_5m_sell3_rebuy3 = False
    if args.market in (None, "", "a"):
        a_5m_sell3_rebuy3_report = write_sell_policy_impact_report(
            args.output_a_5m_sell3_rebuy3_impact_json,
            output_markdown=args.output_a_5m_sell3_rebuy3_impact_markdown,
            markets=("a",),
            summary_paths={"a": args.a_5m_sell3_rebuy3_default_summary},
            candidate_summary_paths={
                "a": args.a_5m_sell3_rebuy3_candidate_summary,
            },
            candidate_label="a_5m_sell3_rebuy3",
        )
        adoption_candidate_reports.append(a_5m_sell3_rebuy3_report)
        wrote_a_5m_sell3_rebuy3 = True
    mtf3_coverage_report = write_mtf3_cache_coverage_report(
        args.output_mtf3_cache_coverage_json,
        output_markdown=args.output_mtf3_cache_coverage_markdown,
        markets=(args.market,) if args.market else ("a", "us"),
        chart_cache_dir=args.mtf3_cache_chart_cache_dir,
        bt_data_dir=args.mtf3_cache_bt_data_dir,
        mtf3_bt_data_dir=args.mtf3_cache_mtf3_bt_data_dir or None,
        bt_sample_size=args.mtf3_cache_bt_sample_size,
    )
    write_strategy_adoption_gate_report(
        args.output_strategy_adoption_gate_json,
        output_markdown=args.output_strategy_adoption_gate_markdown,
        coverage_report=mtf3_coverage_report,
        candidate_reports=adoption_candidate_reports,
    )
    print(f"summary={args.output_json}")
    print(f"markdown={args.output_markdown}")
    print(f"decision={args.output_decision}")
    print(f"decision_state={args.output_decision_state}")
    print(f"runtime_overrides={args.output_runtime_overrides}")
    print(f"attribution={args.output_attribution_json}")
    print(f"attribution_markdown={args.output_attribution_markdown}")
    print(f"bs_point_attribution={args.output_bs_point_json}")
    print(f"bs_point_attribution_markdown={args.output_bs_point_markdown}")
    print(f"bs_point_ratio_state={args.output_bs_point_ratio_state}")
    print(f"bs_point_ratio_overrides={args.output_bs_point_ratio_overrides}")
    print(f"bs_point_ratio_impact={args.output_bs_point_ratio_impact_json}")
    print(f"bs_point_ratio_impact_markdown={args.output_bs_point_ratio_impact_markdown}")
    print(f"bs_point_regime={args.output_bs_point_regime_json}")
    print(f"bs_point_regime_markdown={args.output_bs_point_regime_markdown}")
    print(f"bs_point_regime_policy={args.output_bs_point_regime_policy_json}")
    print(
        "bs_point_regime_policy_markdown="
        f"{args.output_bs_point_regime_policy_markdown}"
    )
    print(f"regime_ratio_impact={args.output_regime_ratio_impact_json}")
    print(
        "regime_ratio_impact_markdown="
        f"{args.output_regime_ratio_impact_markdown}"
    )
    print(f"market_regime_stress={args.output_market_regime_stress_json}")
    print(
        "market_regime_stress_markdown="
        f"{args.output_market_regime_stress_markdown}"
    )
    if wrote_us_2026q1_regime_stress:
        print(f"us_2026q1_regime_stress={args.output_us_2026q1_regime_stress_json}")
        print(
            "us_2026q1_regime_stress_markdown="
            f"{args.output_us_2026q1_regime_stress_markdown}"
        )
    print(f"regime_policy={args.output_regime_policy_json}")
    print(f"regime_policy_markdown={args.output_regime_policy_markdown}")
    print(f"sell_policy_impact={args.output_sell_policy_impact_json}")
    print(f"sell_policy_impact_markdown={args.output_sell_policy_impact_markdown}")
    print(f"sell3half_impact={args.output_sell3half_impact_json}")
    print(f"sell3half_impact_markdown={args.output_sell3half_impact_markdown}")
    print(f"sell3half_up_impact={args.output_sell3half_up_impact_json}")
    print(f"sell3half_up_impact_markdown={args.output_sell3half_up_impact_markdown}")
    print(f"sell3_rebuy3_impact={args.output_sell3_rebuy3_impact_json}")
    print(f"sell3_rebuy3_impact_markdown={args.output_sell3_rebuy3_impact_markdown}")
    print(f"sell3_rebuy3_up_impact={args.output_sell3_rebuy3_up_impact_json}")
    print(
        "sell3_rebuy3_up_impact_markdown="
        f"{args.output_sell3_rebuy3_up_impact_markdown}"
    )
    print(f"sell3_rebuy_mid3_impact={args.output_sell3_rebuy_mid3_impact_json}")
    print(f"sell3_rebuy_mid3_impact_markdown={args.output_sell3_rebuy_mid3_impact_markdown}")
    if wrote_a_5m_sell3_rebuy3:
        print(f"a_5m_sell3_rebuy3_impact={args.output_a_5m_sell3_rebuy3_impact_json}")
        print(
            "a_5m_sell3_rebuy3_impact_markdown="
            f"{args.output_a_5m_sell3_rebuy3_impact_markdown}"
        )
    print(f"mtf3_cache_coverage={args.output_mtf3_cache_coverage_json}")
    print(f"mtf3_cache_coverage_markdown={args.output_mtf3_cache_coverage_markdown}")
    print(f"strategy_adoption_gate={args.output_strategy_adoption_gate_json}")
    print(f"strategy_adoption_gate_markdown={args.output_strategy_adoption_gate_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
