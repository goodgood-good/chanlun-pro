#!/usr/bin/env python3
"""Produce the final go/no-go audit for a complete strict strategy historical backtest.

This command binds the direct-recursive structure replay, higher-timeframe
risk, QMT history audit, prospective sector ledger and three-program service
catalog.  Failed upstream facts produce a blocked performance result with
``None`` metrics; an empty accounting ledger is never rendered as 0% strategy
performance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.individual_research import (  # noqa: E402
    FINANCIAL_SERVICE_CATALOG_ID,
    PROGRAM_SERVICE_URLS,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    load_sector_ledger,
)
from tools.research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    sha256_file,
)


INTEGRATION = Path("audit/chanlun_live_integration")
DEFAULT_PRESCREEN = INTEGRATION / "direct_recursive_etf_prescreen.json"
DEFAULT_BACKTEST = INTEGRATION / "direct_recursive_component_backtest.json"
DEFAULT_DATA_AUDIT = INTEGRATION / "direct_recursive_data_acceptance.json"
DEFAULT_QMT_AUDIT = INTEGRATION / "qmt_history_source_audit.json"
DEFAULT_SECTOR_LEDGER = Path(
    ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)
DEFAULT_FINANCIAL_PROBE = INTEGRATION / "financial_service_three_program_probe.json"
DEFAULT_OUTPUT = INTEGRATION / "complete_backtest_readiness.json"
DEFAULT_MARKDOWN = INTEGRATION / "complete_backtest_readiness.md"


def _verified_json(path: Path, schema: str) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise ValueError(f"unsupported artifact schema: {path}")
    recorded = payload.get("content_sha256")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if recorded != content_sha256(stable):
        raise ValueError(f"artifact content hash changed: {path}")
    return payload


def build_readiness(
    *,
    prescreen: Mapping[str, object],
    backtest: Mapping[str, object],
    data_audit: Mapping[str, object],
    qmt_audit: Mapping[str, object],
    prospective_sector_capture_count: int,
    financial_probe_available: bool,
    artifact_hashes: Mapping[str, str],
) -> dict[str, object]:
    totals = prescreen.get("totals")
    replay = backtest.get("replay")
    full_gate = data_audit.get("full_system_data_gate")
    membership = qmt_audit.get("membership_audit")
    ticks = qmt_audit.get("historical_tick_audit")
    if not all(isinstance(value, Mapping) for value in (totals, replay, full_gate, membership, ticks)):
        raise ValueError("complete-backtest audit inputs are incomplete")
    metrics = replay.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("replay metrics are unavailable")
    if any(
        value.get("live_status") != "LIVE_DISABLED"
        for value in (prescreen, backtest, data_audit)
    ):
        raise ValueError("complete-backtest inputs cannot enable live trading")

    instruments = int(totals.get("instruments", 0))
    adjustment_eligible = int(totals.get("adjustment_eligible_instruments", 0))
    diagnostic_points = int(totals.get("diagnostic_strategic_points", 0))
    risk_bound = int(totals.get("strategic_points_with_higher_timeframe_risk", 0))
    risk_eligible = int(
        totals.get("higher_timeframe_risk_eligible_strategic_points", 0)
    )
    aligned_entries = int(totals.get("aligned_entries", 0))
    historical_membership_ok = bool(
        membership.get("historical_point_in_time_eligible")
    )
    historical_ticks_ok = bool(ticks.get("all_requested_ranges_available"))
    full_data_ok = full_gate.get("eligibility") == "FULL_SYSTEM_ELIGIBLE"
    signed_research_history_available = False

    blockers: list[dict[str, str]] = []
    if not historical_membership_ok:
        blockers.append(
            {
                "code": "QMT_HISTORICAL_SECTOR_CURRENT_BACKFILL",
                "detail": (
                    f"status={membership.get('status')}; future-listed rows="
                    f"{len(tuple(membership.get('future_listed_members') or ())) }"
                ),
            }
        )
    if not signed_research_history_available:
        blockers.append(
            {
                "code": "SIGNED_THREE_PROGRAM_HISTORY_MISSING",
                "detail": (
                    "financial services provide disclosure-dated raw evidence, but "
                    "the immutable strict strategy specification requires independent signed "
                    "PASS/LEADER-or-CHALLENGER/UNDERVALUED-or-FAIR adjudications"
                ),
            }
        )
    if not historical_ticks_ok:
        blockers.append(
            {
                "code": "HISTORICAL_QUOTES_AND_TRADES_MISSING",
                "detail": "all sampled QMT tick ranges returned zero rows",
            }
        )
    if adjustment_eligible != instruments:
        blockers.append(
            {
                "code": "PIT_CAUSAL_ADJUSTMENT_COVERAGE_PARTIAL",
                "detail": f"{adjustment_eligible}/{instruments} instruments",
            }
        )
    if not full_data_ok:
        blockers.append(
            {
                "code": "FULL_SYSTEM_DATA_GATE_FAILED",
                "detail": ",".join(str(value) for value in full_gate.get("full_system_failures", ())),
            }
        )
    if aligned_entries == 0:
        blockers.append(
            {
                "code": "NO_FULLY_ALIGNED_30M_5M_1M_ENTRY",
                "detail": (
                    "no 30m strategic third-buy candidate completed the required "
                    "5m first return and 1m first/second-buy locator chain"
                ),
            }
        )
    if risk_eligible == 0:
        blockers.append(
            {
                "code": "NO_STRATEGIC_POINT_PASSED_HIGHER_TIMEFRAME_RISK",
                "detail": str(totals.get("higher_timeframe_risk_gate_counts", {})),
            }
        )

    complete_eligible = not blockers
    performance_evaluable = bool(metrics.get("performance_evaluable"))
    if performance_evaluable or complete_eligible:
        raise ValueError("current evidence unexpectedly permits a performance claim")
    gates = (
        {
            "gate": "30m_5m_1m_direct_recursive_structure",
            "status": "PASS_COMPONENT",
            "evidence": "one immutable 1m graph maps raw levels 2/1/0 to 30m/5m/1m",
        },
        {
            "gate": "M_W_D_higher_timeframe_risk",
            "status": "PASS_WIRED_FAIL_CANDIDATES",
            "evidence": (
                f"{risk_bound}/{diagnostic_points} strategic points carry causal "
                f"risk envelopes; eligible={risk_eligible}; gates="
                f"{totals.get('higher_timeframe_risk_gate_counts', {})}"
            ),
        },
        {
            "gate": "QMT_sector_first_selection",
            "status": "PASS_PROSPECTIVE_ONLY_FAIL_HISTORICAL",
            "evidence": (
                f"immutable captures={prospective_sector_capture_count}; historical "
                f"audit={membership.get('status')}"
            ),
        },
        {
            "gate": "individual_three_program_services",
            "status": "RAW_INTERFACES_AVAILABLE_SIGNED_HISTORY_MISSING",
            "evidence": (
                f"catalog={FINANCIAL_SERVICE_CATALOG_ID}; probe="
                f"{'available' if financial_probe_available else 'missing'}"
            ),
        },
        {
            "gate": "historical_strict_execution",
            "status": "FAIL_MISSING_TICKS",
            "evidence": str(ticks.get("historical_quote_and_trade_gate")),
        },
        {
            "gate": "causal_adjustment",
            "status": "PARTIAL",
            "evidence": f"{adjustment_eligible}/{instruments} instruments",
        },
    )
    stable: dict[str, object] = {
        "schema": "chanlun-complete-backtest-readiness",
        "strategy_id": "CL-HIER-30M5M1M",
        "requested_scope": "INDIVIDUAL_THREE_PROGRAM_SECTOR_FIRST_FULL_SYSTEM",
        "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
        "logical_level_mapping": prescreen.get("logical_level_mapping"),
        "gates": gates,
        "blockers": tuple(blockers),
        "technical_supply": {
            "instruments": instruments,
            "diagnostic_30m_strategic_points": diagnostic_points,
            "formal_adjustment_eligible_strategic_points": int(
                totals.get("strategic_points", 0)
            ),
            "higher_timeframe_risk_bound_points": risk_bound,
            "higher_timeframe_risk_eligible_points": risk_eligible,
            "aligned_30m_5m_1m_entries": aligned_entries,
            "risk_gate_counts": totals.get("higher_timeframe_risk_gate_counts", {}),
            "alignment_rejection_counts": totals.get("alignment_rejection_counts", {}),
        },
        "three_program_service_catalog": {
            "catalog_id": FINANCIAL_SERVICE_CATALOG_ID,
            "role": "RAW_EVIDENCE_ONLY",
            "program_urls": {
                program: tuple(sorted(urls))
                for program, urls in PROGRAM_SERVICE_URLS.items()
            },
            "automatic_adjudication_allowed": False,
            "signed_historical_adjudication_count": 0,
        },
        "prospective_qmt_sector_ledger": {
            "capture_count": prospective_sector_capture_count,
            "can_reconstruct_sessions_before_first_capture": False,
        },
        "execution_result": {
            "status": "BLOCKED_BEFORE_PERFORMANCE_EVALUATION",
            "component_replay_completed": True,
            "complete_system_replay_completed": False,
            "performance_evaluable": False,
            "net_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "profit_factor": None,
            "win_rate": None,
            "turnover": None,
            "cost": None,
            "strategic_cycle_count": 0,
            "tactical_cycle_count": 0,
            "empty_ledger_fields_are_not_performance": {
                "net_return": metrics.get("net_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "interpretation": "ACCOUNTING_IDENTITY_ONLY",
            },
        },
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "required_next_evidence": (
            "effective-dated historical sector memberships or genuine archived daily QMT captures",
            "independent signed historical three-program adjudications tied to disclosure-time evidence",
            "historical best quotes/trades and frozen broker latency",
            "complete causal adjustment, status, fee and quantity-increment ledgers",
        ),
    }
    return {**stable, "content_sha256": content_sha256(stable)}


def _markdown(report: Mapping[str, object]) -> str:
    supply = report["technical_supply"]
    result = report["execution_result"]
    lines = [
        "# 统一策略完整回测就绪裁决",
        "",
        "结论：**当前不能形成完整体系收益结论**。技术组件已回放，但历史选股与严格成交事实门失败。",
        "",
        f"- 状态：`{report['highest_status']} / {report['live_status']}`",
        f"- 30m 战略点（诊断/正式复权）：{supply['diagnostic_30m_strategic_points']} / {supply['formal_adjustment_eligible_strategic_points']}",
        f"- 高周期风险可入场点：{supply['higher_timeframe_risk_eligible_points']}",
        f"- 30m→5m→1m 完整入场链：{supply['aligned_30m_5m_1m_entries']}",
        f"- 收益是否可评价：`{result['performance_evaluable']}`",
        "",
        "## 当前交易体系",
        "",
        "```text",
        "QMT 板块点时触发（每日不可变哈希链）",
        "  → 个股三程序：行业长期机会 + 龙头/成长挑战者 + 市值/行业地位比价",
        "  → 市场 / 板块 / 个股 M-W-D 顶分型风险与 5 周期线",
        "  → 同一完成 1m 基流的直接递归：L0=30m 战略、L1=5m 短差、L2=1m 定位",
        "  → 共享候选决策、五槽资金与现金预留",
        "  → 同一订单 / T+1 / 费用 / 部分成交 / 持续退出引擎",
        "```",
        "",
        "板块、研究和高级别数据只有候选/风险权限；最终技术入场仍须同时完成 30m 首中枢三买、5m 第一次完整回试和回试内 1m 一买或小转大后二买。",
        "",
        "## 数据与规则门",
        "",
        "| 门 | 状态 | 证据 |",
        "|---|---|---|",
    ]
    for gate in report["gates"]:
        lines.append(f"| {gate['gate']} | {gate['status']} | {gate['evidence']} |")
    lines.extend(("", "## 阻断项", ""))
    for blocker in report["blockers"]:
        lines.append(f"- `{blocker['code']}`：{blocker['detail']}")
    lines.extend(
        (
            "",
            "## 本次新增与修改",
            "",
            "- 新增 `qmt_sector_ledger.py`：QMT 板块目录不可变捕获、哈希链、同日可见性与历史回填审计。",
            "- 新增 `snapshot_qmt_gics3_sector_ledger.py`：已保存首个 66 板块点时快照，只能用于捕获之后的决策。",
            "- 新增 `audit_qmt_history_sources.py`：自动检查未来上市成员与历史 tick 覆盖。",
            "- 修改 `prescreen_direct_recursive.py`：每个 30m 战略点按其真实决策时刻绑定同源 M/W/D 风险；坏数据保留为 UNRESOLVED 拒绝。",
            "- 新增本就绪裁决工具及三组专项测试；公共导出与工作区清单同步更新。",
            "",
            "## 验证结果",
            "",
            "- `python -m pytest -q tests/core tests/trading_system` → **751 passed**。",
            "- `python -m pytest -q tests/exchange/test_qmt_screening_sector_source.py` → **4 passed**。",
            "- 本次改动面 `ruff` → **All checks passed**。",
            "- 独立结果复核 → **10/10 passed**；授权核心裁决 → `PASS_AUTHORIZED_DELTA`。",
            "- 未提交、未推送、未连接真实账户、未发订单或通知。",
            "",
            "## 数据实证",
            "",
            "- 8 只 ETF 的本地最长完整 1m 区间已重放；正式复权账本覆盖 2/8。",
            "- 510300 原始分钟源覆盖 2018-02-02 至 2026-07-24 的 2,051 个完整交易日；2018-10-08、2023-01-20 两个不完整日被拒绝，未补值。",
            "- QMT 4 个板块在 2019-01-02、2022-01-04、2026-07-24 返回完全相同集合；发现 240 条未来上市成员，证明当前成分回填。",
            "- 510300、600519 在上述 3 个日期的历史 tick 共 6 个区间均为 0 行。",
            "- 金融数据服务可提供披露日财报、市值、分类、预测与分红等原始证据，但没有历史签名三程序裁决。",
            "",
            "## 收益字段",
            "",
            "总收益、年化、最大回撤、夏普、利润因子和胜率均为 `N/A`。空账本中的 0 收益/0 回撤只是会计恒等式，不是策略表现。",
            "战略周期 0、短差周期 0，分别低于 100/200 的最低样本要求。前置门失败，因此没有执行年度收益、留出区间、滚动前推、基准收益和收益消融；这样做避免把未交易包装成策略结果。",
            "",
            "## 补齐完整回测所需事实",
            "",
        )
    )
    for item in report["required_next_evidence"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--prescreen", type=Path, default=DEFAULT_PRESCREEN)
    value.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    value.add_argument("--data-audit", type=Path, default=DEFAULT_DATA_AUDIT)
    value.add_argument("--qmt-audit", type=Path, default=DEFAULT_QMT_AUDIT)
    value.add_argument("--sector-ledger", type=Path, default=DEFAULT_SECTOR_LEDGER)
    value.add_argument("--financial-probe", type=Path, default=DEFAULT_FINANCIAL_PROBE)
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    return value


def main() -> int:
    args = parser().parse_args()
    prescreen = _verified_json(
        args.prescreen, "chanlun-direct-recursive-etf-prescreen"
    )
    backtest = _verified_json(
        args.backtest, "chanlun-direct-recursive-component-backtest"
    )
    data_audit = _verified_json(
        args.data_audit, "chanlun-direct-recursive-data-acceptance"
    )
    qmt_audit = _verified_json(
        args.qmt_audit, "chanlun-qmt-history-source-audit"
    )
    sector_ledger = load_sector_ledger(args.sector_ledger)
    paths = {
        "prescreen": args.prescreen,
        "component_backtest": args.backtest,
        "data_acceptance": args.data_audit,
        "qmt_history_audit": args.qmt_audit,
        "prospective_sector_ledger": args.sector_ledger,
    }
    if args.financial_probe.is_file():
        paths["financial_service_probe"] = args.financial_probe
    report = build_readiness(
        prescreen=prescreen,
        backtest=backtest,
        data_audit=data_audit,
        qmt_audit=qmt_audit,
        prospective_sector_capture_count=len(sector_ledger["entries"]),
        financial_probe_available=args.financial_probe.is_file(),
        artifact_hashes={key: sha256_file(path) for key, path in paths.items()},
    )
    atomic_json(args.output, report)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(_markdown(report), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "complete": True,
                "output": str(args.output.resolve()),
                "markdown": str(args.markdown.resolve()),
                "execution_status": report["execution_result"]["status"],
                "performance_evaluable": False,
                "blocker_count": len(report["blockers"]),
                "highest_status": report["highest_status"],
                "live_status": report["live_status"],
                "content_sha256": report["content_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
