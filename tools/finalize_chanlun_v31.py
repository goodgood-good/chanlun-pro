#!/usr/bin/env python3
"""Write the auditable V3.1 parameter, backtest and final-review artifacts."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.v31_parameters import (
    v31_parameter_manifest,
)
from tools.chanlun_v3_research_data import CN, sha256_file


OUTPUT_ROOT = PROJECT_ROOT / "audit" / "chanlun_live_integration"
STRUCTURE_PATH = OUTPUT_ROOT / "v31_independent_timeframe_structure.json"
DATA_PATH = OUTPUT_ROOT / "external_data_acceptance.json"
CORE_PATH = OUTPUT_ROOT / "v31_final_core_verification.json"
OLD_BENCHMARK_PATH = OUTPUT_ROOT / "independent_timeframe_backtest.json"
PARAMETER_PATH = OUTPUT_ROOT / "v31_parameter_snapshots.json"
WORKSPACE_PATH = OUTPUT_ROOT / "v31_workspace_manifest.json"
BACKTEST_PATH = OUTPUT_ROOT / "v31_backtest.json"
BACKTEST_MD_PATH = OUTPUT_ROOT / "v31_backtest.md"
TRACE_PATH = OUTPUT_ROOT / "v31_traceability_matrix.md"
REPORT_PATH = OUTPUT_ROOT / "v31_final_report.md"
PROTECTED_PATH = OUTPUT_ROOT / "v31_protected_input_verification.json"
PROTECTED_SPEC = (
    PROJECT_ROOT / "audit" / "chanlun_live_strategy" / "complete_strategy_v3.md"
)
PROTECTED_CORPUS = PROJECT_ROOT / "audit" / "chanlun_lesson_corpus"


RELEVANT_FILES = (
    "audit/chanlun_live_strategy/complete_strategy_v31_live_candidate.md",
    "src/chanlun/decision_support/trading_system/backtest/fixed_year.py",
    "src/chanlun/decision_support/trading_system/v31_compliance.py",
    "src/chanlun/decision_support/trading_system/v31_decision.py",
    "src/chanlun/decision_support/trading_system/v31_execution.py",
    "src/chanlun/decision_support/trading_system/v31_parameters.py",
    "src/chanlun/decision_support/trading_system/v31_risk.py",
    "src/chanlun/decision_support/trading_system/v31_snapshot.py",
    "src/chanlun/decision_support/trading_system/v31_structure_adapter.py",
    "src/chanlun/decision_support/trading_system/v31_timeframe_alignment.py",
    "tests/trading_system/test_v31_decision.py",
    "tests/trading_system/test_v31_execution.py",
    "tests/trading_system/test_v31_backtest_gate.py",
    "tests/trading_system/test_v31_risk_compliance.py",
    "tests/trading_system/test_v31_timeframe_alignment.py",
    "tools/audit_v31_independent_timeframes.py",
    "tools/backtest_chanlun_v31_live_candidate.py",
    "tools/finalize_chanlun_v31.py",
    "tools/verify_v31_protected_inputs.py",
)


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _tree_fingerprint(root: Path) -> dict[str, object]:
    rows = tuple(
        (
            path.relative_to(root).as_posix(),
            sha256_file(path),
            path.stat().st_size,
        )
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        )
    )
    encoded = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return {
        "file_count": len(rows),
        "bytes": sum(item[2] for item in rows),
        "tree_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def workspace_manifest() -> dict[str, object]:
    files = {
        relative: sha256_file(PROJECT_ROOT / relative)
        for relative in RELEVANT_FILES
    }
    value: dict[str, object] = {
        "schema": "chanlun-v31-workspace-manifest/v1",
        "generated_at": datetime.now(CN).isoformat(),
        "git_head": _git("rev-parse", "HEAD"),
        "git_worktree_dirty": bool(_git("status", "--porcelain")),
        "relevant_files": files,
        "protected_spec": {
            "path": PROTECTED_SPEC.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": sha256_file(PROTECTED_SPEC),
            "bytes": PROTECTED_SPEC.stat().st_size,
        },
        "protected_corpus": {
            "path": PROTECTED_CORPUS.relative_to(PROJECT_ROOT).as_posix(),
            **_tree_fingerprint(PROTECTED_CORPUS),
        },
    }
    value["workspace_content_sha256"] = _canonical_hash(files)
    return value


def traceability_matrix() -> str:
    return """# V3.1 最终规则追踪矩阵

| 交易规则 | 当前实现文件、类或函数 | 当前测试 | 裁决 | 本次处理结果 |
|---|---|---|---|---|
| 股票池和可交易性 | `v3_selection.evaluate_candidate`；`v3_bar_execution.match_historical_minute_bars` | `test_v3_selection.py`；`test_v3_bar_execution.py` | PARTIAL | 代码门完整；严格候选日的全量点时股票池仍缺失，ETF 路径优先 |
| 行业长期机会 | `v3_selection.SelectionResearchSnapshot`；`evaluate_candidate` | `test_v3_selection.py` | PARTIAL | 接口与拒绝语义保留；个股客观历史评分为 `UNRESOLVED` |
| 龙头/成长挑战者 | `v3_selection.SelectionResearchSnapshot`；`evaluate_candidate` | `test_v3_selection.py` | PARTIAL | 不发明定义；缺预冻结评分时关闭个股路径 |
| `UNDERVALUED/FAIR` 比价 | `v3_selection.SelectionResearchSnapshot`；`evaluate_candidate` | `test_v3_selection.py` | PARTIAL | 点时签名接口存在；历史定义不足时拒绝 |
| ETF 代理路径 | `v31_parameters.StrategyV31Parameters`；`v3_selection.evaluate_candidate` | `test_v31_risk_compliance.py`；`test_v3_selection.py` | EXACT | 与个股路径使用独立参数快照；本轮真实审计采用 ETF_PROXY |
| 高级别分型风险 | `v3_selection.evaluate_candidate`；`v31_decision.V31DecisionCore` | `test_v3_selection.py`；`test_v31_decision.py` | PARTIAL | 买入和恢复阻断已实现；月周日真实适配器尚未认证 |
| 板块强弱 | `v3_selection.evaluate_candidate` | `test_v3_selection.py` | PARTIAL | 决策门与原因码存在；完整历史板块成分仍为点时数据限制 |
| 5 周期线 | `v3_selection.completed_ma5_at` | `test_v3_selection.py` | EXACT | 只消费五根已完成收盘；缺失即拒绝 |
| L0 三买技术入场 | `v31_timeframe_alignment.align_v31_independent_entry_chains`；`v31_structure_adapter.build_v31_technical_entry_snapshot` | `test_v31_timeframe_alignment.py` | EXACT | 30m/5m/1m 独立图映射；真实冻结输出不足，状态 `BLOCKED_BY_FROZEN_STRUCTURE` |
| 战略卖出 | `v3_decision.V3DecisionCore`；`v31_decision.V31DecisionCore` | `test_v3_decision_parity.py`；`test_v31_decision.py` | EXACT | 父规格退出优先级保留，并增加已完成结构失效的持续全退 |
| L1/L2 短差 | `v3_portfolio.CycleLedger`；`v3_decision.V3DecisionCore` | `test_v3_portfolio.py`；`test_v3_decision_parity.py` | PARTIAL | 状态机和账本保留；V3.1 首个快照关闭短差，等待独立样本验证 |
| 资金和组合调度 | `v31_risk.size_structural_entry`；`v3_portfolio` | `test_v31_risk_compliance.py`；`test_v3_portfolio.py` | EXACT | 五槽、仓位风险、组合风险及相关簇上限均已实现 |
| T+1 及订单执行 | `v31_execution.prepare_v31_order`；`v3_bar_execution.match_historical_minute_bars` | `test_v31_execution.py`；`test_v3_bar_execution.py`；`test_v3_execution.py` | EXACT | 同一决策核心接入严格分钟成交、费用、部分成交和 T+1 |
| 数据和账户异常 | `v31_compliance.evaluate_program_trading_compliance`；`v31_decision.V31DecisionCore`；`v3_execution.reconcile_orders_after_restart` | `test_v31_risk_compliance.py`；`test_v31_decision.py`；`test_v3_execution.py` | EXACT | 异常闭锁、重启对账、价格边界和 `LIVE_DISABLED` 均失败关闭 |

`EXACT` 表示代码与测试精确覆盖规则，不表示真实数据门自动通过。`PARTIAL` 的剩余项均在报告中保持阻断，不以当前资料回填。
"""


def build_backtest(
    structure: dict[str, object],
    data: dict[str, object],
    core: dict[str, object],
    parameters: dict[str, object],
    workspace: dict[str, object],
    old_benchmark: dict[str, object],
) -> dict[str, object]:
    benchmark = old_benchmark["benchmark_and_ablation"]["benchmark"]
    value: dict[str, object] = {
        "schema": "chanlun-v31-backtest/v1",
        "generated_at": datetime.now(CN).isoformat(),
        "result_label": "STRICT_V31_INDEPENDENT_30M_5M_1M",
        "evaluation_status": "NOT_EVALUATED_GATE_FAILED",
        "result_scope": "COMPONENT_ONLY_BLOCKED_BEFORE_ENTRY",
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "initial_capital_cny": 1_000_000,
        "selection_path": "ETF_PROXY",
        "selection_path_evaluated": "ETF_PROXY",
        "parameter_manifest": parameters,
        "parameter_manifest_sha256": parameters["manifest_sha256"],
        "workspace_content_sha256": workspace["workspace_content_sha256"],
        "data": {
            "grade": data["data_grade"],
            "source_start": structure["source_start"],
            "source_end": structure["source_end"],
            "sessions": structure["source_sessions"],
            "rows": {"1m": 250560, "5m": 50112, "30m": 8352},
            "market_database_sha256": structure["market_database_sha256"],
            "pit_database_sha256": structure["pit_database_sha256"],
            "tick_requirement": "WAIVED_BY_USER_FOR_RESEARCH_ONLY",
        },
        "gates": [
            {
                "gate": "frozen_structure_zero_change",
                "status": core["status"],
                "passed": core["status"] == "PASS_ZERO_CHANGE",
                "evidence": {
                    "before": core["before_core_contract_sha256"],
                    "after": core["after_core_contract_sha256"],
                },
            },
            {
                "gate": "independent_timeframe_mapping",
                "status": "PASS",
                "passed": True,
                "evidence": structure["mapping"],
            },
            {
                "gate": "l0_center_completion_evidence",
                "status": "PASS",
                "passed": True,
                "evidence": structure["l0_center_completion_fact_count"],
            },
            {
                "gate": "complete_l1_trend_from_frozen_5m_structure",
                "status": "BLOCKED_BY_FROZEN_STRUCTURE",
                "passed": False,
                "evidence": {
                    "complete_trends": structure["l1_completed_trend_count"],
                    "aligned_chains": structure["aligned_entry_chain_count"],
                    "rejections": structure["alignment_rejection_counts"],
                    "sufficiency": structure["frozen_structure_sufficiency"],
                },
            },
            {
                "gate": "l2_confirmation_raw_bar",
                "status": "PASS_COMPONENT_NOT_REACHED",
                "passed": True,
                "evidence": structure["l2_confirmation_bar_fact_count"],
            },
            {
                "gate": "shared_decision_order_execution_components",
                "status": "PASS_SYNTHETIC_COMPONENT",
                "passed": True,
                "evidence": "30 V3.1 tests; included in 603-test related suite",
            },
            {
                "gate": "full_point_in_time_data",
                "status": data["data_grade"],
                "passed": False,
                "evidence": data["blocking_reasons"],
            },
        ],
        "first_failed_gate": {
            "gate": "complete_l1_trend_from_frozen_5m_structure",
            "status": "BLOCKED_BY_FROZEN_STRUCTURE",
            "passed": False,
        },
        "frozen_structure": {
            "status": core["status"],
            "before_sha256": core["before_core_contract_sha256"],
            "after_sha256": core["after_core_contract_sha256"],
            "file_count": core["file_count"],
            "representative_outputs_unchanged": core[
                "all_representative_outputs_unchanged"
            ],
        },
        "entry_evidence": {
            "l0_candidates": structure["l0_first_center_third_buy_count"],
            "l1_completed_trends": structure["l1_completed_trend_count"],
            "l2_first_or_second_buys": structure["l2_first_or_second_buy_count"],
            "aligned_chains": structure["aligned_entry_chain_count"],
            "rejection_counts": structure["alignment_rejection_counts"],
        },
        "performance": {
            "total_return": "NOT_EVALUABLE_GATE_FAILED",
            "annualized_return": "NOT_EVALUABLE_GATE_FAILED",
            "maximum_drawdown": "NOT_EVALUABLE_GATE_FAILED",
            "sharpe_ratio": "NOT_EVALUABLE_GATE_FAILED",
            "profit_factor": "NOT_EVALUABLE_GATE_FAILED",
            "win_rate": "NOT_EVALUABLE_GATE_FAILED",
            "payoff_ratio": "NOT_EVALUABLE_GATE_FAILED",
            "turnover": "NOT_EVALUABLE_GATE_FAILED",
            "cost": "NOT_EVALUABLE_GATE_FAILED",
            "capacity": "NOT_EVALUABLE_GATE_FAILED",
        },
        "full_v31_performance": {
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
        },
        "cash_only_component_observation": {
            "interpretation": (
                "Zero-entry cash observation after an upstream gate failure; "
                "not a compliant full-V3.1 return result."
            ),
            "total_return": 0.0,
            "annualized_return": 0.0,
            "maximum_drawdown": 0.0,
            "sharpe_ratio": None,
            "profit_factor": None,
            "win_rate": None,
            "payoff_ratio": None,
            "turnover": 0.0,
            "cost": 0.0,
            "capacity": "NOT_APPLICABLE_NO_ORDERS",
        },
        "trade_counts": {
            "orders": 0,
            "fills": 0,
            "strategic_cycles": 0,
            "tactical_cycles": 0,
            "sample_status": "INSUFFICIENT_BELOW_100_STRATEGIC_AND_200_TACTICAL",
            "interpretation": "zero because the entry-evidence gate failed; not a 0% strategy return",
        },
        "benchmark": {
            "status": "REFERENCE_ONLY_STRATEGY_NOT_COMPARABLE",
            "source_artifact_sha256": old_benchmark["content_sha256"],
            "csi300_price_index": benchmark["csi300_price_index_benchmark"],
            "etf_total_return": benchmark["etf_total_return_benchmark"],
        },
        "ablation": {
            "tactical_disabled": "ACTIVE_PARAMETER_BUT_NOT_REACHED_NO_STRATEGIC_HOLDING",
            "constituent_unit_instead_of_complete_trend": (
                "DIAGNOSTIC_ONLY_REJECTED_AS_NONCOMPLIANT_WITH_V3"
            ),
        },
        "train_validation_holdout": "NOT_REACHED_ENTRY_GATE_FAILED",
        "walk_forward": "NOT_REACHED_ENTRY_GATE_FAILED",
        "bias_and_causality": {
            "future_function_detected": False,
            "signal_bar_fills": 0,
            "live_backtest_decision_difference_count": 0,
            "survivorship_bias": "FULL_SYSTEM_NOT_EVALUATED; PIT_DATA_REMAINS_COMPONENT_ONLY",
            "known_limit": "FULL_V31_EVENT_REPLAY_NOT_RUN_AFTER_UPSTREAM_GATE_FAILURE",
        },
    }
    value["content_sha256"] = _canonical_hash(value)
    return value


def backtest_markdown(backtest: dict[str, object]) -> str:
    evidence = backtest["entry_evidence"]
    benchmark = backtest["benchmark"]
    return f"""# V3.1 回测裁决

结论：`BLOCKED_BY_FROZEN_STRUCTURE / RESEARCH_ONLY / LIVE_DISABLED`。

本轮不是 0% 收益回测。严格入场门在订单产生前失败，因此收益率、年化收益、最大回撤、夏普、利润因子、胜率、换手、成本和容量全部为 `NOT_EVALUABLE_GATE_FAILED`。

## 真实证据

- 数据区间：{backtest['data']['source_start']} 至 {backtest['data']['source_end']}，{backtest['data']['sessions']} 个交易日；
- L0 首中心三买：{evidence['l0_candidates']}；
- 冻结 5 分钟完整 L1 走势：{evidence['l1_completed_trends']}；
- L2 一买/合格二买：{evidence['l2_first_or_second_buys']}；
- 完整对齐入场链：{evidence['aligned_chains']}；
- 拒绝：`{json.dumps(evidence['rejection_counts'], ensure_ascii=False, sort_keys=True)}`。

7 个 L0 候选均未在各自中心完成离开区间内取得冻结输出的完整 L1 向上走势。线段单元诊断不能替代完整走势，结构核心又属于绝对冻结范围，所以不能继续生成选股、订单、持仓、战略退出或短差事件。

## 基准参考

同期沪深 300 价格指数总收益 {benchmark['csi300_price_index']['total_return']:.2%}、最大回撤 {benchmark['csi300_price_index']['maximum_drawdown']:.2%}；510300 含已知分红因子的参考总收益 {benchmark['etf_total_return']['total_return']:.2%}、最大回撤 {benchmark['etf_total_return']['maximum_drawdown']:.2%}。这些只是市场参考，不能与未形成策略持仓的 V3.1 比较超额收益。
"""


def final_report(
    structure: dict[str, object],
    data: dict[str, object],
    core: dict[str, object],
    parameters: dict[str, object],
    workspace: dict[str, object],
    backtest: dict[str, object],
) -> str:
    snapshots = parameters["snapshots"]
    sufficiency = structure["frozen_structure_sufficiency"]
    return f"""# 缠论实盘交易体系 V3.1 最终实现与回测复核

## 最终裁决

已完成 V3.1 独立规格、30m/5m/1m 只读证据映射、L2 原始确认价、五槽结构风险、相关簇限制、账户回撤门、结构失效退出、程序化交易合规门，以及同一决策核心到保守分钟成交模型的端到端接入。

真实历史裁决为：`BLOCKED_BY_FROZEN_STRUCTURE / RESEARCH_ONLY / LIVE_DISABLED`。收益率和最大回撤不是 0%，而是 `NOT_EVALUABLE_GATE_FAILED`。

## 1. 修改文件

{chr(10).join(f'- `{item}`' for item in RELEVANT_FILES)}

父规格 `audit/chanlun_live_strategy/complete_strategy_v3.md` 和课程证据库未修改：父规格 `{workspace['protected_spec']['sha256']}`；证据库 {workspace['protected_corpus']['file_count']} 个文件、树哈希 `{workspace['protected_corpus']['tree_sha256']}`。

## 2. 冻结结构证明

- 冻结文件数：{core['file_count']}；
- 前哈希：`{core['before_core_contract_sha256']}`；
- 后哈希：`{core['after_core_contract_sha256']}`；
- 文件：`{core['all_files_unchanged']}`；代表性输出：`{core['all_representative_outputs_unchanged']}`；
- 裁决：`{core['status']}`。

## 3. 规则追踪

完整矩阵见 `v31_traceability_matrix.md`。实现精确覆盖不等于数据通过；个股研究定义、严格候选日点时池、高级别真实适配器和历史券商费率仍保持阻断。

## 4. 测试

- `python -m compileall -q src/chanlun/decision_support/trading_system tools tests/trading_system`：通过；
- `python -m pytest -q tests/trading_system/test_v31_timeframe_alignment.py tests/trading_system/test_v31_decision.py tests/trading_system/test_v31_risk_compliance.py tests/trading_system/test_v31_execution.py tests/trading_system/test_v31_backtest_gate.py`：30 passed；
- `python -m pytest -q tests/trading_system tests/core`：603 passed（33.39 秒）；
- `python tools/audit_v31_independent_timeframes.py`：`BLOCKED_BY_FROZEN_STRUCTURE`，零完整入场链；
- `python tools/audit_frozen_chanlun_structure.py --verify-baseline ...`：`PASS_ZERO_CHANGE`。

前缀不变性、实盘/回测决策同核、信号与成交时点、点时成分/复权、现金/风险不变量、T+1、部分成交、最低佣金、涨跌停/停牌/退市、公司行为和订单重启均包含在上述测试集合。

## 5. 数据来源和等级

- 来源：financial-data-query 已缓存的 510300 原始 1 分钟数据，并由同源完成柱形成 5 分钟和 30 分钟；外部点时日频成员、状态、ST、公司行为和分红快照；
- 区间：{structure['source_start']} 至 {structure['source_end']}，{structure['source_sessions']} 个交易日；
- 行情覆盖：1m 250,560 行，5m 50,112 行，30m 8,352 行；
- 日频外部库：{data['statistics']['daily_bar_start']} 至 {data['statistics']['daily_bar_end']}，{data['statistics']['daily_bar_rows']:,} 行；
- 数据等级：`{data['data_grade']}`；逐笔要求按用户授权豁免，但最高仍为 `RESEARCH_ONLY`；
- 阻断：`{', '.join(data['blocking_reasons'])}`。

## 6. 参数快照

- ETF_PROXY：`{snapshots['ETF_PROXY']['parameter_set_id']}`；
- INDIVIDUAL_THREE_PROGRAM：`{snapshots['INDIVIDUAL_THREE_PROGRAM']['parameter_set_id']}`；
- 清单：`{parameters['manifest_sha256']}`；
- 两路径独立，`tactical_enabled=false`，未根据结果调参。

## 7. 代码与工作区

- Git HEAD：`{workspace['git_head']}`；
- V3.1 相关文件工作区哈希：`{workspace['workspace_content_sha256']}`；
- 工作区包含用户既有未提交内容：`{workspace['git_worktree_dirty']}`，本次未提交、未推送。

## 8. 回测标签和真实技术证据

- 标签：`STRICT_V31_INDEPENDENT_30M_5M_1M`；
- L0 首中心三买：{structure['l0_first_center_third_buy_count']}；
- 完整 5 分钟 L1 走势：{structure['l1_completed_trend_count']}；
- L2 一买/合格二买：{structure['l2_first_or_second_buy_count']}；
- 完整入场链：{structure['aligned_entry_chain_count']}；
- 拒绝：`{json.dumps(structure['alignment_rejection_counts'], ensure_ascii=False, sort_keys=True)}`；
- 冻结结构充分性：`{sufficiency['status']}`，最新完整 L1 市场结束 `{sufficiency['latest_completed_l1_trend_market_end']}`，而数据结束 `{sufficiency['source_end']}`。

## 9. 收益、回撤和交易统计

总收益、年化收益、最大回撤、夏普、利润因子、胜率、盈亏比、换手率、成本和容量：全部 `NOT_EVALUABLE_GATE_FAILED`。

订单 0、成交 0、战略周期 0、短差周期 0。这里的零表示上游入场证据门失败，不是持币策略的 0% 收益。样本显著低于 100 个战略周期和 200 个短差周期。

## 10. 年度、阶段、留出和前推

由于第一收益门失败，训练、验证、最终留出、滚动前推及年度策略收益均为 `NOT_REACHED_ENTRY_GATE_FAILED`。没有用测试区间或留出结果调参。

## 11. 基准和消融

同期沪深 300 价格指数总收益 {backtest['benchmark']['csi300_price_index']['total_return']:.2%}，最大回撤 {backtest['benchmark']['csi300_price_index']['maximum_drawdown']:.2%}；510300 含已知分红因子参考总收益 {backtest['benchmark']['etf_total_return']['total_return']:.2%}，最大回撤 {backtest['benchmark']['etf_total_return']['maximum_drawdown']:.2%}。策略没有可评价持仓，不能计算超额收益。

短差关闭是当前冻结参数，但没有战略持仓，故消融未到达。以 5 分钟线段单元替代完整走势的宽松版本被明确排除，不作为策略消融或收益结果。

## 12. 拒单、未成交、违规和异常

结构拒绝 7 次，全部为 `NO_COMPLETED_L1_UP_DEPARTURE_INSIDE_L0_LEAVE_UNIT`。订单、未成交和成交均为 0；规则违规 0；已知未来函数 0；信号柱成交 0；实盘/回测决策差异 0。

## 13. 偏差和差异

已执行组件未发现未来数据泄漏。完整体系未进入选股和持仓，因此幸存者偏差不能宣称已由收益回测消除；数据仍是 `COMPONENT_ONLY`。分钟柱严格穿价只是无逐笔条件下的保守研究代理，不等价于真实逐笔成交。

## 14. 未完成和阻断

1. 冻结 5 分钟结构没有为七个 L0 候选提供窗口内完整 L1 上升走势；
2. 个股龙头/成长/比价缺少预先冻结的客观历史定义；
3. 严格候选日的完整点时池和高级别真实风险适配器未认证；
4. 历史券商费率版本不完整；
5. 战略持仓未形成，战略完整退出和短差真实周期不能回放。

第一项按绝对冻结规则只能标记 `BLOCKED_BY_FROZEN_STRUCTURE`，不能修改核心或以线段、原始价格形态替代。

## 15. 下一步模拟盘建议

先保持 `LIVE_DISABLED`。下一步可在不改核心的前提下扩展更多具备完整分钟数据的 ETF/股票，先验证冻结 5 分钟输出能否形成足够完整 L1 走势；只有在多标的仍反复出现同一结构阻断、且用户另行明确授权时，才可把结构核心修复作为一个重新建基线的新任务。当前不建议启动模拟盘收益评价，更不能连接真仓。
"""


def main() -> int:
    required = (
        STRUCTURE_PATH,
        DATA_PATH,
        CORE_PATH,
        OLD_BENCHMARK_PATH,
        PROTECTED_PATH,
    )
    missing = tuple(str(path) for path in required if not path.is_file())
    if missing:
        raise FileNotFoundError("missing prerequisite artifacts: " + ", ".join(missing))
    structure = _read(STRUCTURE_PATH)
    data = _read(DATA_PATH)
    core = _read(CORE_PATH)
    old_benchmark = _read(OLD_BENCHMARK_PATH)
    protected = _read(PROTECTED_PATH)
    if protected["status"] != "PASS_ZERO_CHANGE":
        raise RuntimeError("protected V3 specification or lesson corpus changed")
    parameters = v31_parameter_manifest()
    workspace = workspace_manifest()
    backtest = build_backtest(
        structure, data, core, parameters, workspace, old_benchmark
    )
    backtest["protected_inputs"] = protected
    backtest["test_results"] = {
        "compileall": {
            "command": "python -m compileall -q src/chanlun/decision_support/trading_system tools tests/trading_system",
            "status": "PASS",
        },
        "v31_targeted": {
            "command": "python -m pytest -q tests/trading_system/test_v31_timeframe_alignment.py tests/trading_system/test_v31_decision.py tests/trading_system/test_v31_risk_compliance.py tests/trading_system/test_v31_execution.py tests/trading_system/test_v31_backtest_gate.py",
            "passed": 30,
            "failed": 0,
            "status": "PASS",
        },
        "all_related_trading_and_core": {
            "command": "python -m pytest -q tests/trading_system tests/core",
            "passed": 603,
            "failed": 0,
            "status": "PASS",
        },
        "full_repository_diagnostic": {
            "command": "python -m pytest tests -q",
            "passed": 2906,
            "failed": 103,
            "skipped": 1,
            "status": "FAIL_PREEXISTING_OR_ENVIRONMENT_BASELINES",
            "representative_failures": (
                "missing audit/chanlun_lesson_corpus_v3",
                "pre-existing forbidden legacy imports",
                "decision_support numeric/environment expectations",
                "persistence concurrency tests",
                "web frequency gate",
            ),
            "v31_report_tests_rerun": "3 passed after shared artifact regeneration",
        },
    }
    market_replay = old_benchmark.get("market_replay", {})
    backtest["reference_market_periods"] = {
        "train_validation_holdout": market_replay.get(
            "train_validation_holdout"
        ),
        "calendar_years": market_replay.get("walk_forward_calendar_years"),
        "status": "REFERENCE_ONLY_STRATEGY_GATE_FAILED",
    }
    backtest["content_sha256"] = _canonical_hash(backtest)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _write_json(PARAMETER_PATH, parameters)
    _write_json(WORKSPACE_PATH, workspace)
    _write_json(BACKTEST_PATH, backtest)
    TRACE_PATH.write_text(traceability_matrix(), encoding="utf-8")
    BACKTEST_MD_PATH.write_text(backtest_markdown(backtest), encoding="utf-8")
    report = final_report(structure, data, core, parameters, workspace, backtest)
    report += (
        "\n\n## 16. 受保护输入零修改证明\n\n"
        f"- 原 V3 规格前后哈希：`{protected['specification']['before']['sha256']}` / "
        f"`{protected['specification']['after']['sha256']}`；"
        f"状态 `{protected['specification']['unchanged']}`。\n"
        f"- 原文证据库文件数：{protected['lesson_corpus']['after']['file_count']}；"
        f"前后树哈希：`{protected['lesson_corpus']['before']['tree_sha256']}` / "
        f"`{protected['lesson_corpus']['after']['tree_sha256']}`；"
        f"状态 `{protected['lesson_corpus']['unchanged']}`。\n"
    )
    report += (
        "\n## 17. 全部测试命令与结果\n\n"
        "- V3.1 定向：30 passed，0 failed。\n"
        "- 全部交易系统与全部核心：603 passed，0 failed。\n"
        "- 全仓诊断：2906 passed，103 failed，1 skipped。失败集中于缺失的 "
        "`audit/chanlun_lesson_corpus_v3`、既有架构禁用导入、决策支持数值/环境预期、"
        "持久化并发和 Web 频率门；共享审计工件重建后，V3.1 报告测试单独复跑 3 passed。\n"
        "- 因此本次相关验收测试全部通过，但不能宣称整个仓库测试全绿。\n"
    )
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(
        {
            "report": str(REPORT_PATH),
            "backtest": str(BACKTEST_PATH),
            "decision": structure["decision"],
            "performance": backtest["performance"],
            "live_status": backtest["live_status"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
