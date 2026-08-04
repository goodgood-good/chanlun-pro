#!/usr/bin/env python3
"""Run the fail-closed gate for the complete Chanlun v3 strategy.

This is the only v3 backtest entry point.  It never substitutes a simpler
signal set.  If selection, five-slot portfolio, high-timeframe risk,
L0/L1/L2 direct recursion, L2 location, strategic exits, tactical facts, or
causal execution evidence is unavailable, return metrics remain explicitly
NOT_EVALUATED.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from chanlun.decision_support.trading_system.v3_parameters import (
    LIVE_STATUS,
    STRATEGY_V3_ID,
    etf_parameter_snapshot,
    parameter_snapshot_manifest,
)
from tools.audit_frozen_chanlun_structure import build_contract
from tools.chanlun_v3_research_data import (
    CN,
    DEFAULT_MARKET_DATABASE,
    DEFAULT_PIT_DATABASE,
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_BASELINE = Path(
    "audit/chanlun_live_integration/frozen_structure_baseline.json"
)
DEFAULT_RECURSIVE_AUDIT = Path(
    "audit/chanlun_live_integration/recursive_structure_availability.json"
)
DEFAULT_DATA_ACCEPTANCE = Path(
    "audit/chanlun_live_integration/external_data_acceptance.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/strict_v3_backtest.json"
)
DEFAULT_MARKDOWN = Path(
    "audit/chanlun_live_integration/strict_v3_backtest.md"
)
SPECIFICATION = Path(
    "audit/chanlun_live_strategy/complete_strategy_v3.md"
)
INITIAL_CAPITAL = 1_000_000


STRICT_FILES = (
    "src/chanlun/decision_support/trading_system/data_audit_v3.py",
    "src/chanlun/decision_support/trading_system/v3_bar_execution.py",
    "src/chanlun/decision_support/trading_system/v3_decision.py",
    "src/chanlun/decision_support/trading_system/v3_execution.py",
    "src/chanlun/decision_support/trading_system/v3_parameters.py",
    "src/chanlun/decision_support/trading_system/v3_portfolio.py",
    "src/chanlun/decision_support/trading_system/v3_selection.py",
    "src/chanlun/decision_support/trading_system/v3_structure_adapter.py",
    "tools/chanlun_v3_research_data.py",
    "tools/audit_frozen_chanlun_structure.py",
    "tools/audit_v3_recursive_structure_availability.py",
    "tools/audit_external_etf_pit_data.py",
    "tools/fetch_financial_data_query_bars.py",
    "tools/snapshot_external_etf_pit_data.py",
    "tools/backtest_chanlun_v3_strict.py",
)

REMOVED_DEGRADED_PATHS = (
    "tools/audit_chanlun_v3_local_data.py",
    "tools/validate_chanlun_v3_components.py",
    "tools/backtest_chanlun_v3_available_data_proxy.py",
    "tools/backtest_chanlun_v3_external_pit_proxy.py",
    "tests/trading_system/test_v3_available_data_proxy.py",
    "tests/trading_system/test_v3_external_pit_proxy.py",
    "audit/chanlun_live_integration/available_data_proxy_backtest.json",
    "audit/chanlun_live_integration/available_data_proxy_backtest.md",
    "audit/chanlun_live_integration/external_pit_proxy_backtest.json",
    "audit/chanlun_live_integration/external_pit_proxy_backtest.md",
    "audit/chanlun_live_integration/bar_proxy_parameter_snapshots.json",
    "audit/chanlun_live_integration/component_validation.json",
    "audit/chanlun_live_integration/local_data_audit.json",
    "audit/chanlun_live_integration/financial_data_query_capability_audit.json",
    "audit/chanlun_live_integration/financial_data_query_capability_audit.md",
)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _git_revision() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return process.stdout.strip() if process.returncode == 0 else "UNRESOLVED"


def _workspace_manifest() -> dict[str, object]:
    paths = (str(SPECIFICATION), *STRICT_FILES)
    files = tuple(
        {
            "path": path,
            "sha256": sha256_file(PROJECT_ROOT / path),
        }
        for path in paths
    )
    manifest: dict[str, object] = {
        "git_revision": _git_revision(),
        "files": files,
    }
    manifest["workspace_v3_sha256"] = content_sha256(manifest)
    return manifest


def _gate(
    gate: str,
    passed: bool,
    *,
    status_if_failed: str,
    evidence: object,
) -> dict[str, object]:
    return {
        "gate": gate,
        "status": "PASS" if passed else status_if_failed,
        "passed": passed,
        "evidence": evidence,
    }


def build_report(
    *,
    baseline: Mapping[str, object],
    current_core: Mapping[str, object],
    recursive: Mapping[str, object],
    data_acceptance: Mapping[str, object],
    removed_paths_absent: bool,
    workspace_manifest: Mapping[str, object],
) -> dict[str, object]:
    expected_core = baseline.get("core_contract")
    frozen_passed = expected_core == current_core
    parameters = etf_parameter_snapshot()
    recursive_levels = tuple(recursive.get("observed_recursive_levels", ()))
    recursive_passed = recursive.get("decision") == "STRICT_V3_STRUCTURE_FACTS_CERTIFIED"
    l2_locator_passed = bool(
        recursive.get("required_entry_point_counts", {}).get(
            "l2_level0_first_or_second_buy", 0
        )
    ) and recursive_passed
    data_passed = bool(
        data_acceptance.get("strict_full_v3_return_evaluation_allowed", False)
    )
    gates = (
        _gate(
            "frozen_structure_zero_change",
            frozen_passed,
            status_if_failed="FAIL",
            evidence={
                "expected": (
                    expected_core.get("core_contract_sha256")
                    if isinstance(expected_core, Mapping)
                    else None
                ),
                "actual": current_core.get("core_contract_sha256"),
            },
        ),
        _gate(
            "degraded_proxy_removed",
            removed_paths_absent,
            status_if_failed="FAIL",
            evidence=REMOVED_DEGRADED_PATHS,
        ),
        _gate(
            "five_slot_portfolio",
            parameters.slot_count == 5
            and str(parameters.slot_fraction) == "0.18"
            and str(parameters.account_exposure_cap) == "0.90",
            status_if_failed="FAIL",
            evidence={
                "slot_count": parameters.slot_count,
                "slot_fraction": parameters.slot_fraction,
                "account_exposure_cap": parameters.account_exposure_cap,
            },
        ),
        _gate(
            "direct_recursive_l0_l1_l2",
            recursive_passed,
            status_if_failed=str(
                recursive.get("decision", "BLOCKED_BY_FROZEN_STRUCTURE")
            ),
            evidence={
                "observed_levels": recursive_levels,
                "required_levels": {"L2": 0, "L1": 1, "L0": 2},
                "reason": recursive.get("reason"),
            },
        ),
        _gate(
            "selection_path_etf_proxy_point_in_time",
            bool(
                data_acceptance.get(
                    "strict_candidate_membership_snapshots_available", False
                )
            ),
            status_if_failed=(
                "BLOCKED_UPSTREAM" if not recursive_passed else "UNRESOLVED"
            ),
            evidence={
                "parameter_set_id": parameters.parameter_set_id,
                "membership_snapshots": data_acceptance.get("statistics", {}).get(
                    "candidate_membership_snapshots"
                ),
                "snapshot_scope": data_acceptance.get("membership_snapshot_scope"),
            },
        ),
        _gate(
            "high_timeframe_risk_fact_production",
            False,
            status_if_failed=(
                "BLOCKED_UPSTREAM" if not recursive_passed else "UNRESOLVED"
            ),
            evidence="monthly/weekly/daily fractal-to-center mapping is not certified",
        ),
        _gate(
            "l2_entry_locator",
            l2_locator_passed,
            status_if_failed=(
                "BLOCKED_UPSTREAM" if not recursive_passed else "UNRESOLVED"
            ),
            evidence=recursive.get("required_entry_point_counts"),
        ),
        _gate(
            "strategic_complete_exit_fact_production",
            False,
            status_if_failed=(
                "BLOCKED_UPSTREAM" if not recursive_passed else "UNRESOLVED"
            ),
            evidence=(
                "L0 third-sell, first L1 up-leg failure, half-position rebound "
                "failure, and L0 divergence must all be produced from one direct chain"
            ),
        ),
        _gate(
            "l1_l2_tactical_fact_production",
            False,
            status_if_failed=(
                "BLOCKED_UPSTREAM" if not recursive_passed else "UNRESOLVED"
            ),
            evidence=(
                "L1 phase, L2 location, adaptation pairs, every-prefix cost gate, "
                "protection and recovery facts are not certified on real data"
            ),
        ),
        _gate(
            "causal_research_execution_data",
            data_passed,
            status_if_failed="COMPONENT_ONLY",
            evidence={
                "data_grade": data_acceptance.get("data_grade"),
                "blocking_reasons": data_acceptance.get("blocking_reasons"),
                "tick_requirement": "WAIVED_BY_USER_FOR_RESEARCH_ONLY",
            },
        ),
    )
    passed = all(bool(row["passed"]) for row in gates)
    if passed:
        # Reaching this branch without a certified event replay would itself
        # be a defect.  Never silently turn gate readiness into a return.
        evaluation_status = "BLOCKED_BACKTEST_ENGINE_NOT_CERTIFIED"
    else:
        evaluation_status = "NOT_EVALUATED_GATE_FAILED"
    first_failure = next(
        (row for row in gates if not bool(row["passed"])),
        None,
    )
    not_evaluated = {
        "total_return": "NOT_EVALUATED",
        "annualized_return": "NOT_EVALUATED",
        "maximum_drawdown": "NOT_EVALUATED",
        "sharpe_ratio": "NOT_EVALUATED",
        "profit_factor": "NOT_EVALUATED",
        "win_rate": "NOT_EVALUATED",
        "payoff_ratio": "NOT_EVALUATED",
        "turnover": "NOT_EVALUATED",
        "cost": "NOT_EVALUATED",
        "capacity": "NOT_EVALUATED",
    }
    report: dict[str, object] = {
        "schema": "chanlun-v3-strict-backtest/v1",
        "generated_at": datetime.now(CN),
        "result_label": "STRICT_COMPLETE_V3",
        "strategy_id": STRATEGY_V3_ID,
        "selection_path": "ETF_PROXY",
        "initial_capital_cny": INITIAL_CAPITAL,
        "evaluation_status": evaluation_status,
        "return_evaluation_allowed": False,
        "first_failed_gate": first_failure,
        "gates": gates,
        "backtest_sequence": (
            {
                "stage": "single_symbol_short_interval_structure_consumption",
                "status": "FAILED_GATE",
                "reason": recursive.get("decision"),
            },
            {
                "stage": "small_pool_end_to_end_causal_replay",
                "status": "NOT_RUN_PREVIOUS_GATE",
            },
            {
                "stage": "prefix_and_live_backtest_parity",
                "status": "COMPONENT_TESTS_ONLY",
            },
            {
                "stage": "data_audit",
                "status": data_acceptance.get("data_grade", "UNRESOLVED"),
            },
            {
                "stage": "train_validation_holdout",
                "status": "NOT_RUN_PREVIOUS_GATE",
            },
            {
                "stage": "walk_forward_benchmark_ablation",
                "status": "NOT_RUN_PREVIOUS_GATE",
            },
        ),
        "performance": not_evaluated,
        "trade_counts": {
            "strategic_cycles": 0,
            "tactical_cycles": 0,
            "orders": 0,
            "fills": 0,
            "interpretation": "zero because evaluation was blocked, not a zero-return run",
            "sample_sufficiency": "INSUFFICIENT_BELOW_100_STRATEGIC_AND_200_TACTICAL",
        },
        "parameter_manifest": parameter_snapshot_manifest(),
        "workspace_manifest": workspace_manifest,
        "data": {
            "market_database_sha256": sha256_file(DEFAULT_MARKET_DATABASE),
            "pit_database_sha256": sha256_file(DEFAULT_PIT_DATABASE),
            "source_start": recursive.get("source_start"),
            "source_end": recursive.get("source_end"),
            "source_sessions": recursive.get("source_sessions"),
            "data_grade": data_acceptance.get("data_grade"),
        },
        "bias_and_causality": {
            "future_function_assessment": (
                "NO_KNOWN_LEAK_IN_EXECUTED_COMPONENTS; "
                "FULL_V3_EVENT_REPLAY_NOT_RUN"
            ),
            "survivorship_bias": "PIT_CSI300_SNAPSHOTS_AVAILABLE_FOR_CAPTURED_CANDIDATES",
            "minute_timestamp_adapter": recursive.get("source_timestamp_contract"),
            "live_backtest_difference": (
                "minute bars replace tick/quote execution by explicit user waiver; "
                "result remains RESEARCH_ONLY and LIVE_DISABLED"
            ),
        },
        "removed_degraded_paths": REMOVED_DEGRADED_PATHS,
        "unresolved": tuple(
            str(row["gate"])
            for row in gates
            if not bool(row["passed"])
        ),
        "highest_status": "RESEARCH_ONLY",
        "live_status": LIVE_STATUS,
    }
    report["content_sha256"] = content_sha256(report)
    return report


def _markdown(report: Mapping[str, object]) -> str:
    performance = report["performance"]
    gates = report["gates"]
    lines = [
        "# 完整 V3 严格回测裁决",
        "",
        f"- 结果标签：`{report['result_label']}`",
        f"- 评价状态：`{report['evaluation_status']}`",
        f"- 数据等级：`{report['data']['data_grade']}`",
        f"- 真仓状态：`{report['live_status']}`",
        "",
        "## 收益结论",
        "",
        "本轮没有合规收益率，也不把空仓误写成 0% 收益。严格门在真实订单回放前失败，"
        "因此总收益、年化收益、最大回撤、夏普与利润因子均为 `NOT_EVALUATED`。",
        "",
        f"- 总收益：`{performance['total_return']}`",
        f"- 年化收益：`{performance['annualized_return']}`",
        f"- 最大回撤：`{performance['maximum_drawdown']}`",
        f"- 夏普：`{performance['sharpe_ratio']}`",
        f"- 利润因子：`{performance['profit_factor']}`",
        "",
        "## 严格门",
        "",
        "| 门 | 状态 |",
        "|---|---|",
    ]
    lines.extend(f"| `{row['gate']}` | `{row['status']}` |" for row in gates)
    lines.extend(
        (
            "",
            "## 样本与安全状态",
            "",
            "- 战略周期：0（因门失败未评价，样本不足）",
            "- 短差周期：0（因门失败未评价，样本不足）",
            "- 没有发送真实订单、通知或连接真实账户。",
            "- 降级代理脚本及旧代理收益报告已移除。",
            "",
        )
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    baseline = _load(DEFAULT_BASELINE)
    recursive = _load(DEFAULT_RECURSIVE_AUDIT)
    data_acceptance = _load(DEFAULT_DATA_ACCEPTANCE)
    removed_paths_absent = all(
        not (PROJECT_ROOT / path).exists() for path in REMOVED_DEGRADED_PATHS
    )
    report = build_report(
        baseline=baseline,
        current_core=build_contract(),
        recursive=recursive,
        data_acceptance=data_acceptance,
        removed_paths_absent=removed_paths_absent,
        workspace_manifest=_workspace_manifest(),
    )
    atomic_json(DEFAULT_OUTPUT, report)
    DEFAULT_MARKDOWN.write_text(_markdown(report), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "output": str(DEFAULT_OUTPUT.resolve()),
                "markdown": str(DEFAULT_MARKDOWN.resolve()),
                "evaluation_status": report["evaluation_status"],
                "total_return": report["performance"]["total_return"],
                "maximum_drawdown": report["performance"]["maximum_drawdown"],
                "live_status": report["live_status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
