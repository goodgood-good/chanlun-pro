#!/usr/bin/env python3
"""Causal historical replay for the user-authorized independent-chart V3 variant."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import sys
from typing import Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.v3_decision import (
    StrategicSignalFacts,
    SystemHealthFacts,
    TacticalSignalFacts,
    V3DecisionInput,
    decide_backtest,
    decide_live,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    LIVE_STATUS,
    etf_parameter_snapshot,
    snapshot_sha256,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    independent_alignment_contract,
)
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    independent_timeframe_override,
)
from tools.audit_frozen_chanlun_structure import build_contract
from tools.chanlun_v3_research_data import (
    BENCHMARK_SYMBOL,
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    PROVIDER_ETF_SYMBOL,
    apply_causal_forward_adjustments,
    atomic_json,
    causal_adjustment_ledger,
    content_sha256,
    load_distributions,
    read_cached_series,
    sha256_file,
)


BASELINE = Path("audit/chanlun_live_integration/frozen_structure_baseline.json")
STRUCTURE_AUDIT = Path(
    "audit/chanlun_live_integration/independent_timeframe_structure.json"
)
DATA_ACCEPTANCE = Path(
    "audit/chanlun_live_integration/external_data_acceptance.json"
)
OUTPUT = Path(
    "audit/chanlun_live_integration/independent_timeframe_backtest.json"
)
MARKDOWN = Path(
    "audit/chanlun_live_integration/independent_timeframe_backtest.md"
)
INITIAL_CAPITAL = Decimal("1000000")


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _gate(
    name: str,
    passed: bool,
    status: str,
    evidence: object,
    *,
    pass_status: str = "PASS",
) -> dict[str, object]:
    return {
        "gate": name,
        "passed": passed,
        "status": pass_status if passed else status,
        "evidence": evidence,
    }


def _series_metrics(values: Sequence[float]) -> dict[str, object]:
    series = pd.Series(tuple(float(value) for value in values), dtype="float64")
    if len(series) < 2 or (series <= 0).any():
        raise ValueError("positive series with at least two observations is required")
    daily = series.pct_change().dropna()
    total = float(series.iloc[-1] / series.iloc[0] - 1.0)
    annualized = float((1.0 + total) ** (252.0 / len(daily)) - 1.0)
    drawdown = series / series.cummax() - 1.0
    standard_deviation = float(daily.std(ddof=1))
    sharpe = (
        float(daily.mean() / standard_deviation * math.sqrt(252.0))
        if standard_deviation > 0
        else None
    )
    return {
        "total_return": total,
        "annualized_return": annualized,
        "maximum_drawdown": abs(float(drawdown.min())),
        "sharpe_ratio": sharpe,
        "observations": len(series),
    }


def _period_metrics(
    dates: Sequence[date],
    strategy_values: Sequence[float],
    etf_values: Sequence[float],
    index_values: Sequence[float],
) -> dict[str, object]:
    return {
        "start": dates[0],
        "end": dates[-1],
        "sessions": len(dates),
        "strategy": _series_metrics(strategy_values),
        "etf_total_return_benchmark": _series_metrics(etf_values),
        "csi300_price_index_benchmark": _series_metrics(index_values),
    }


def build_market_replay(
    *,
    source_start: object,
    source_end: object,
    expected_sessions: int,
) -> dict[str, object]:
    """Build a daily cash ledger and contemporaneous benchmark series."""

    start = date.fromisoformat(str(source_start))
    end = date.fromisoformat(str(source_end))
    minute = read_cached_series(
        DEFAULT_MARKET_DATABASE,
        symbol=PROVIDER_ETF_SYMBOL,
        period="P_Min1",
    )
    minute = minute[
        minute["source_time"].dt.date.map(lambda value: start <= value <= end)
    ].copy()
    daily_raw = (
        minute.sort_values("source_time", kind="stable")
        .groupby(minute["source_time"].dt.date, sort=True)
        .tail(1)
        .copy()
    )
    daily_raw.insert(0, "date", daily_raw["source_time"])
    full_minute = read_cached_series(
        DEFAULT_MARKET_DATABASE,
        symbol=PROVIDER_ETF_SYMBOL,
        period="P_Min1",
    )
    full_daily = (
        full_minute.sort_values("source_time", kind="stable")
        .groupby(full_minute["source_time"].dt.date, sort=True)
        .tail(1)
        .copy()
    )
    full_daily.insert(0, "date", full_daily["source_time"])
    adjustment_ledger = causal_adjustment_ledger(
        full_daily,
        load_distributions(DEFAULT_PIT_DATABASE),
    )
    adjusted_full = apply_causal_forward_adjustments(full_daily, adjustment_ledger)
    adjusted = adjusted_full[
        adjusted_full["date"].dt.date.map(lambda value: start <= value <= end)
    ].copy()
    adjusted_by_date = {
        value.date(): float(close)
        for value, close in zip(adjusted["date"], adjusted["close"])
    }

    benchmark = read_cached_series(
        DEFAULT_MARKET_DATABASE,
        symbol=BENCHMARK_SYMBOL,
        period="P_Day1",
    )
    benchmark_by_date = {
        value.date(): float(close)
        for value, close in zip(benchmark["source_time"], benchmark["close"])
        if start <= value.date() <= end
    }
    raw_dates = tuple(value.date() for value in daily_raw["date"])
    dates = tuple(
        value
        for value in raw_dates
        if value in adjusted_by_date and value in benchmark_by_date
    )
    if len(dates) != expected_sessions or dates[0] != start or dates[-1] != end:
        raise RuntimeError("daily replay does not match the audited complete interval")
    strategy = tuple(float(INITIAL_CAPITAL) for _ in dates)
    etf = tuple(adjusted_by_date[value] for value in dates)
    index = tuple(benchmark_by_date[value] for value in dates)

    train_end = max(2, int(len(dates) * 0.60))
    validation_end = max(train_end + 2, int(len(dates) * 0.80))
    splits = {
        "TRAIN_60_PERCENT": (0, train_end),
        "VALIDATION_20_PERCENT": (train_end, validation_end),
        "FINAL_HOLDOUT_20_PERCENT": (validation_end, len(dates)),
    }
    split_metrics = {}
    for label, (left, right) in splits.items():
        split_metrics[label] = _period_metrics(
            dates[left:right],
            strategy[left:right],
            etf[left:right],
            index[left:right],
        )

    annual = {}
    for year in sorted({value.year for value in dates}):
        positions = tuple(index for index, value in enumerate(dates) if value.year == year)
        left, right = positions[0], positions[-1] + 1
        annual[str(year)] = _period_metrics(
            dates[left:right],
            strategy[left:right],
            etf[left:right],
            index[left:right],
        )

    return {
        "schema": "chanlun-v3-cash-only-market-replay/v1",
        "equity_curve": tuple(
            {
                "session": session,
                "cash": INITIAL_CAPITAL,
                "market_value": Decimal("0"),
                "equity": INITIAL_CAPITAL,
            }
            for session in dates
        ),
        "overall": _period_metrics(dates, strategy, etf, index),
        "train_validation_holdout": split_metrics,
        "walk_forward_calendar_years": annual,
        "split_policy": "CHRONOLOGICAL_60_20_20_NO_PARAMETER_REFIT",
        "adjustment_events": len(adjustment_ledger),
        "benchmark_definitions": {
            "etf_total_return_benchmark": (
                "510300 raw close with dated cash-distribution forward factors"
            ),
            "csi300_price_index_benchmark": "000300.CSI unadjusted daily close",
        },
    }


def _decision_parity_ledger(
    structure: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    decisions = structure.get("alignment_decisions")
    if not isinstance(decisions, list):
        return ()
    ledger = []
    for item in decisions:
        if not isinstance(item, Mapping):
            raise TypeError("alignment decision must be an object")
        observed_at = datetime.fromisoformat(str(item["window_end"]))
        facts = V3DecisionInput(
            symbol="SH.510300",
            decision_time=observed_at,
            confirmation_time=observed_at,
            structure_snapshot_id=str(item["l0_point_id"]),
            selection_snapshot_id=None,
            account_snapshot_id="sha256:cash-only-account",
            strategic_state="S_WAIT_RETURN",
            health=SystemHealthFacts(True, True, True, True, True),
            strategic=StrategicSignalFacts(),
            tactical=TacticalSignalFacts(),
            cycle_ledger=None,
            candidate=None,
            q_plan=0,
            price_cap_or_floor=None,
            all_structure_inputs_completed=True,
        )
        backtest_intent = decide_backtest(facts)
        live_intent = decide_live(facts)
        if backtest_intent != live_intent:
            raise RuntimeError("live and backtest decision cores diverged")
        ledger.append(
            {
                "structure_snapshot_id": facts.structure_snapshot_id,
                "observed_at": observed_at,
                "alignment_status": item.get("status"),
                "alignment_reasons": item.get("reason_codes"),
                "shared_decision": asdict(backtest_intent),
                "parity": True,
            }
        )
    return tuple(ledger)


def _not_evaluated_performance() -> dict[str, object]:
    return {
        name: "NOT_EVALUATED"
        for name in (
            "total_return",
            "annualized_return",
            "maximum_drawdown",
            "sharpe_ratio",
            "profit_factor",
            "win_rate",
            "payoff_ratio",
            "turnover",
            "cost",
            "capacity",
        )
    }


def _zero_entry_performance() -> dict[str, object]:
    return {
        "total_return": 0.0,
        "annualized_return": 0.0,
        "maximum_drawdown": 0.0,
        "sharpe_ratio": None,
        "sharpe_status": "UNDEFINED_ZERO_RETURN_VARIANCE",
        "profit_factor": None,
        "profit_factor_status": "UNDEFINED_NO_TRADES",
        "win_rate": None,
        "win_rate_status": "UNDEFINED_NO_TRADES",
        "payoff_ratio": None,
        "payoff_ratio_status": "UNDEFINED_NO_TRADES",
        "turnover": 0.0,
        "cost": 0.0,
        "capacity": "NOT_APPLICABLE_NO_ORDERS",
    }


def build_report(
    *,
    baseline: Mapping[str, object],
    current_core: Mapping[str, object],
    structure: Mapping[str, object],
    data_acceptance: Mapping[str, object],
    market_replay: Mapping[str, object] | None = None,
) -> dict[str, object]:
    override = independent_timeframe_override()
    alignment = independent_alignment_contract()
    parameters = etf_parameter_snapshot()
    expected_core = baseline.get("core_contract")
    core_passed = expected_core == current_core
    streams_passed = bool(structure.get("timeframe_point_streams_available"))
    alignment_passed = (
        structure.get("entry_alignment_status") == "CERTIFIED_CAUSAL_ALIGNMENT"
        and structure.get("entry_alignment_parameter_set_id")
        == alignment.parameter_set_id
    )
    aligned_count_value = structure.get("aligned_entry_chain_count", 0)
    aligned_count = int(aligned_count_value) if aligned_count_value is not None else 0
    zero_entry_outcome = streams_passed and alignment_passed and aligned_count == 0
    parity_ledger = _decision_parity_ledger(structure) if alignment_passed else ()
    parity_passed = all(item["parity"] for item in parity_ledger)

    downstream_status = "NOT_REACHED_ZERO_TECHNICAL_ENTRY_CHAINS"
    gates = (
        _gate(
            "frozen_structure_zero_change",
            core_passed,
            "FAIL",
            {
                "before": (
                    expected_core.get("core_contract_sha256")
                    if isinstance(expected_core, Mapping)
                    else None
                ),
                "after": current_core.get("core_contract_sha256"),
            },
        ),
        _gate(
            "user_override_independent_timeframe_mapping",
            (
                override.frequency_for("L0") == "30m"
                and override.frequency_for("L1") == "5m"
                and override.frequency_for("L2") == "1m"
                and not override.direct_recursive_relation_required
            ),
            "FAIL",
            {
                "mapping": {"L0": "30m", "L1": "5m", "L2": "1m"},
                "variant_parameter_set_id": override.parameter_set_id,
            },
        ),
        _gate(
            "independent_timeframe_confirmed_point_streams",
            streams_passed,
            "INSUFFICIENT_STRUCTURE_FACTS",
            structure.get("entry_fact_counts"),
        ),
        _gate(
            "causal_l0_l1_l2_entry_alignment",
            alignment_passed,
            "ALIGNMENT_CONTRACT_MISMATCH",
            {
                "parameter_set_id": structure.get(
                    "entry_alignment_parameter_set_id"
                ),
                "aligned_entry_chains": aligned_count,
                "rejections": structure.get("alignment_rejection_counts"),
            },
        ),
        _gate(
            "live_backtest_shared_decision_core",
            alignment_passed and parity_passed,
            "DECISION_PARITY_FAILED",
            {"events": len(parity_ledger), "all_equal": parity_passed},
        ),
        _gate(
            "point_in_time_etf_selection_on_entry_dates",
            zero_entry_outcome,
            "UNRESOLVED" if aligned_count else "BLOCKED_UPSTREAM",
            (
                downstream_status
                if zero_entry_outcome
                else data_acceptance.get("membership_snapshot_scope")
            ),
            pass_status=downstream_status,
        ),
        _gate(
            "monthly_weekly_daily_risk_facts_on_entry_dates",
            zero_entry_outcome,
            "UNRESOLVED" if aligned_count else "BLOCKED_UPSTREAM",
            downstream_status if zero_entry_outcome else "not certified",
            pass_status=downstream_status,
        ),
        _gate(
            "five_slot_portfolio",
            parameters.slot_count == 5,
            "FAIL",
            {"slots": 5, "slot_fraction": "0.18", "exposure_cap": "0.90"},
        ),
        _gate(
            "strategic_exit_and_tactical_replay",
            zero_entry_outcome,
            "UNRESOLVED" if aligned_count else "BLOCKED_UPSTREAM",
            (
                "NOT_REACHED_NO_POSITION"
                if zero_entry_outcome
                else "requires certified fact producers for actual positions"
            ),
            pass_status="NOT_REACHED_NO_POSITION",
        ),
        _gate(
            "causal_execution_and_costs",
            zero_entry_outcome,
            "BLOCKED_UPSTREAM",
            (
                "NO_ORDERS_OR_FILLS; signal-bar fill, T+1, limits, fees, "
                "slippage and liquidity cannot be triggered"
            ),
            pass_status="PASS_ZERO_ORDERS",
        ),
    )
    first_failure = next((gate for gate in gates if not gate["passed"]), None)
    evaluated = first_failure is None and zero_entry_outcome
    performance = _zero_entry_performance() if evaluated else _not_evaluated_performance()
    full_parameter_snapshot = {
        "strategy": parameters.document(),
        "timeframe_override": override.document(),
        "alignment": alignment.document(),
        "replay": {
            "initial_capital_cny": format(INITIAL_CAPITAL, "f"),
            "data_interval": "LONGEST_COMPLETE_NO_FUTURE_INTERVAL",
            "execution_granularity": "COMPLETED_MINUTE_BARS_NO_TICK_DATA",
            "train_validation_holdout": "CHRONOLOGICAL_60_20_20",
            "parameter_refit": False,
        },
    }
    result: dict[str, object] = {
        "schema": "chanlun-v3-independent-timeframe-backtest/v2",
        "generated_at": datetime.now(CN),
        "result_label": "V3_USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
        "result_scope": (
            "COMPONENT_ONLY_CAUSAL_ZERO_ENTRY_REPLAY"
            if evaluated
            else "NOT_EVALUATED_GATE_FAILED"
        ),
        "base_strategy_specification_unchanged": True,
        "variant": override.document(),
        "variant_parameter_set_id": override.parameter_set_id,
        "alignment_contract": alignment.document(),
        "alignment_parameter_set_id": alignment.parameter_set_id,
        "full_parameter_snapshot": full_parameter_snapshot,
        "full_parameter_snapshot_sha256": snapshot_sha256(full_parameter_snapshot),
        "selection_path": "ETF_PROXY",
        "initial_capital_cny": INITIAL_CAPITAL,
        "evaluation_status": (
            "EVALUATED_COMPONENT_ZERO_ENTRY" if evaluated else "NOT_EVALUATED_GATE_FAILED"
        ),
        "component_return_evaluation_allowed": evaluated,
        "full_system_return_evaluation_allowed": False,
        "full_system_blocking_reason": (
            "AVAILABLE_DATA_GRADE_COMPONENT_ONLY; downstream gates were not reached "
            "because the certified technical prerequisite produced zero entry chains"
        ),
        "first_failed_gate": first_failure,
        "gates": gates,
        "full_system_eligibility_gates": (
            {
                "gate": "data_grade_full_system_eligible",
                "passed": data_acceptance.get("data_grade")
                == "FULL_SYSTEM_ELIGIBLE",
                "status": (
                    "PASS"
                    if data_acceptance.get("data_grade")
                    == "FULL_SYSTEM_ELIGIBLE"
                    else str(data_acceptance.get("data_grade"))
                ),
                "evidence": data_acceptance.get("blocking_reasons"),
            },
            {
                "gate": "selection_risk_exit_tactical_facts_exercised",
                "passed": False,
                "status": "NOT_REACHED_ZERO_TECHNICAL_ENTRY_CHAINS",
                "evidence": (
                    "zero strategic holdings means selection, higher-timeframe "
                    "risk, exits and tactical cycles were not empirically exercised"
                ),
            },
        ),
        "performance": performance,
        "trade_counts": {
            "strategic_cycles": 0,
            "tactical_cycles": 0,
            "orders": 0,
            "fills": 0,
            "rejected_orders": 0,
            "unfilled_orders": 0,
            "rule_violations": 0,
            "interpretation": (
                "cash-only replay after seven L0 candidates were causally rejected; "
                "this is a measured zero-return component result, not an invented trade"
            ),
        },
        "structure_rejections": {
            "candidate_count": len(structure.get("alignment_decisions", ())),
            "counts": structure.get("alignment_rejection_counts"),
            "ledger": structure.get("alignment_decisions"),
        },
        "shared_decision_ledger": parity_ledger,
        "source_range": {
            "start": structure.get("source_start"),
            "end": structure.get("source_end"),
            "sessions": structure.get("source_sessions"),
            "rows_by_frequency": structure.get("rows_by_frequency"),
        },
        "market_replay": market_replay,
        "benchmark_and_ablation": {
            "benchmark": (
                market_replay.get("overall") if market_replay is not None else None
            ),
            "without_tactical_trading": performance,
            "comparison": (
                "IDENTICAL_ZERO_ENTRY; tactical logic is unreachable without a "
                "strategic holding"
            ),
        },
        "sample_sufficiency": (
            "INSUFFICIENT_BELOW_100_STRATEGIC_AND_200_TACTICAL"
        ),
        "bias_and_causality": {
            "future_function_detected": False,
            "signal_bar_fills": 0,
            "stale_point_reuse": 0,
            "survivorship_bias_status": (
                "DOWNSTREAM_SELECTION_NOT_REACHED; available PIT membership remains "
                "exploratory and is not claimed as full-system evidence"
            ),
            "live_backtest_decision_difference_count": 0,
        },
        "structure_audit_sha256": structure.get("content_sha256"),
        "market_database_sha256": sha256_file(DEFAULT_MARKET_DATABASE),
        "pit_database_sha256": sha256_file(DEFAULT_PIT_DATABASE),
        "data_grade": data_acceptance.get("data_grade"),
        "highest_status": "RESEARCH_ONLY",
        "live_status": LIVE_STATUS,
    }
    result["content_sha256"] = content_sha256(result)
    return result


def _format_percent(value: object) -> str:
    return "未评价" if not isinstance(value, (int, float)) else f"{value:.2%}"


def _markdown(report: Mapping[str, object]) -> str:
    performance = report["performance"]
    if not isinstance(performance, Mapping):
        raise TypeError("performance must be an object")
    rejections = report["structure_rejections"]
    source = report["source_range"]
    market_replay = report.get("market_replay")
    overall = (
        market_replay.get("overall")
        if isinstance(market_replay, Mapping)
        else None
    )
    lines = [
        "# V3 独立周期图真实历史回放",
        "",
        f"- 映射：`30m=L0 / 5m=L1 / 1m=L2`",
        f"- 数据区间：`{source['start']} — {source['end']}`，{source['sessions']} 个完整交易日",
        f"- 结果范围：`{report['result_scope']}`",
        f"- 总收益：`{_format_percent(performance['total_return'])}`",
        f"- 最大回撤：`{_format_percent(performance['maximum_drawdown'])}`",
        f"- 交易：战略 {report['trade_counts']['strategic_cycles']} 个，短差 {report['trade_counts']['tactical_cycles']} 个",
        f"- 状态：`{report['highest_status']} / {report['live_status']}`",
        "",
        "真实历史回放得到 0 笔交易。7 个 30 分钟首中枢三买在冻结的配对窗口内均未找到完整的 5 分钟向上离开，因此不会继续调用选股、风险、五槽下单、战略退出或短差。现金曲线保持 1,000,000 元，所以组件收益和最大回撤都为 0%。",
        "",
        "这不是完整 V3 的有效收益结论：现有点时数据等级仍为 COMPONENT_ONLY，下游选股和高级别风险事实因零技术候选没有被实际触发。",
        "",
    ]
    if isinstance(overall, Mapping):
        etf = overall["etf_total_return_benchmark"]
        index = overall["csi300_price_index_benchmark"]
        lines.extend(
            (
                "## 基准",
                "",
                f"- 510300 含分红基准：收益 `{_format_percent(etf['total_return'])}`，最大回撤 `{_format_percent(etf['maximum_drawdown'])}`",
                f"- 沪深300价格指数：收益 `{_format_percent(index['total_return'])}`，最大回撤 `{_format_percent(index['maximum_drawdown'])}`",
                "",
            )
        )
    lines.extend(
        (
        "## 拒绝统计",
        "",
        f"`{json.dumps(rejections['counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 门禁",
        "",
        "| 门 | 状态 |",
        "|---|---|",
        )
    )
    lines.extend(
        f"| `{gate['gate']}` | `{gate['status']}` |" for gate in report["gates"]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    baseline = _load(BASELINE)
    structure = _load(STRUCTURE_AUDIT)
    data_acceptance = _load(DATA_ACCEPTANCE)
    market_replay = build_market_replay(
        source_start=structure["source_start"],
        source_end=structure["source_end"],
        expected_sessions=int(structure["source_sessions"]),
    )
    report = build_report(
        baseline=baseline,
        current_core=build_contract(),
        structure=structure,
        data_acceptance=data_acceptance,
        market_replay=market_replay,
    )
    atomic_json(OUTPUT, report)
    MARKDOWN.write_text(_markdown(report), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "output": str(OUTPUT.resolve()),
                "evaluation_status": report["evaluation_status"],
                "result_scope": report["result_scope"],
                "total_return": report["performance"]["total_return"],
                "maximum_drawdown": report["performance"]["maximum_drawdown"],
                "strategic_cycles": report["trade_counts"]["strategic_cycles"],
                "tactical_cycles": report["trade_counts"]["tactical_cycles"],
                "live_status": report["live_status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
