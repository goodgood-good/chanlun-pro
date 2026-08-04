#!/usr/bin/env python3
"""Summarize the fixed eight-symbol CSI300 broad-ETF V3.1 prescreen."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sys
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for value in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from tools.chanlun_v3_research_data import (
    CN,
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_UNIVERSE = Path(
    "audit/chanlun_live_integration/csi300_broad_etf_universe_v1.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/v31_csi300_broad_etf_prescreen_summary.json"
)
DEFAULT_MARKDOWN = Path(
    "audit/chanlun_live_integration/v31_csi300_broad_etf_prescreen_summary.md"
)


@dataclass(frozen=True, slots=True)
class FrozenSplit:
    name: str
    start: date
    end: date
    sessions: int


FROZEN_SPLITS = (
    FrozenSplit(
        "TRAIN_60_PERCENT",
        date(2018, 10, 9),
        date(2021, 5, 6),
        626,
    ),
    FrozenSplit(
        "VALIDATION_20_PERCENT",
        date(2021, 5, 7),
        date(2022, 3, 15),
        209,
    ),
    FrozenSplit(
        "FINAL_HOLDOUT_20_PERCENT",
        date(2022, 3, 16),
        date(2023, 1, 19),
        209,
    ),
)


def _load_verified_universe(path: Path) -> tuple[Mapping[str, object], tuple[str, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    if (
        payload.get("schema") != "chanlun-csi300-broad-etf-universe/v1"
        or payload.get("content_sha256") != content_sha256(stable)
    ):
        raise RuntimeError("frozen CSI300 broad-ETF universe is invalid")
    symbols = tuple(str(item["symbol"]) for item in payload["instruments"])
    if len(symbols) != 8 or len(set(symbols)) != 8:
        raise RuntimeError("frozen CSI300 broad-ETF universe must contain eight symbols")
    return payload, symbols


def _parse_at(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise RuntimeError("candidate decision timestamp must be timezone-aware")
    return parsed.astimezone(CN)


def _empty_counts() -> dict[str, object]:
    return {
        "candidates": 0,
        "pass": 0,
        "reject": 0,
        "rejection_counts": {},
    }


def _decision_counts(
    decisions: Iterable[Mapping[str, object]],
    *,
    bucket: str,
) -> dict[str, dict[str, object]]:
    if bucket not in {"year", "split"}:
        raise ValueError("unsupported decision-count bucket")
    output: dict[str, dict[str, object]] = (
        {item.name: _empty_counts() for item in FROZEN_SPLITS}
        if bucket == "split"
        else {}
    )
    rejection_counters: dict[str, Counter[str]] = {
        label: Counter() for label in output
    }
    for item in decisions:
        at = _parse_at(item["alignment_decision_at"])
        if bucket == "year":
            label = str(at.year)
        else:
            label = next(
                (
                    split.name
                    for split in FROZEN_SPLITS
                    if split.start <= at.date() <= split.end
                ),
                "OUTSIDE_FROZEN_SPLIT_RANGE",
            )
        counts = output.setdefault(label, _empty_counts())
        reasons = rejection_counters.setdefault(label, Counter())
        counts["candidates"] = int(counts["candidates"]) + 1
        status = str(item["status"])
        if status == "PASS":
            counts["pass"] = int(counts["pass"]) + 1
        elif status == "REJECT":
            counts["reject"] = int(counts["reject"]) + 1
            reasons.update(str(reason) for reason in item["reason_codes"])
        else:
            raise RuntimeError(f"unsupported alignment status: {status}")
    for label, counts in output.items():
        counts["rejection_counts"] = dict(sorted(rejection_counters[label].items()))
    return dict(sorted(output.items()))


def _merge_decision_counts(
    reports: Iterable[Mapping[str, object]],
    field: str,
) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    reasons: dict[str, Counter[str]] = {}
    for report in reports:
        for label, raw in report[field].items():
            counts = merged.setdefault(label, _empty_counts())
            counter = reasons.setdefault(label, Counter())
            for key in ("candidates", "pass", "reject"):
                counts[key] = int(counts[key]) + int(raw[key])
            counter.update(raw["rejection_counts"])
    for label, counts in merged.items():
        counts["rejection_counts"] = dict(sorted(reasons[label].items()))
    return dict(sorted(merged.items()))


def _artifact_path(directory: Path, symbol: str) -> Path:
    return directory / f"v31_cached_symbol_prescreen_{symbol[:6]}.json"


def _load_symbol_report(path: Path, symbol: str) -> tuple[dict[str, object], dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stable = {key: value for key, value in payload.items() if key != "content_sha256"}
    if payload.get("content_sha256") != content_sha256(stable):
        raise RuntimeError(f"prescreen artifact content hash is invalid: {path}")
    raw_reports = payload.get("symbol_reports", ())
    if len(raw_reports) != 1 or raw_reports[0].get("provider_symbol") != symbol:
        raise RuntimeError(f"prescreen artifact identity mismatch: {path}")
    report = dict(raw_reports[0])
    decisions = tuple(report.get("candidate_alignment_decisions", ()))
    if len(decisions) != int(report["l0_first_center_third_buy_count"]):
        raise RuntimeError(f"candidate decision coverage is incomplete: {symbol}")
    ordered = tuple(
        sorted(
            decisions,
            key=lambda item: (
                _parse_at(item["l0_available_at"]),
                str(item["l0_point_id"]),
            ),
        )
    )
    if ordered != decisions or len({item["l0_point_id"] for item in decisions}) != len(decisions):
        raise RuntimeError(f"candidate decisions are not deterministic and unique: {symbol}")
    rejection_counts = Counter(
        str(reason)
        for item in decisions
        if item["status"] == "REJECT"
        for reason in item["reason_codes"]
    )
    if dict(sorted(rejection_counts.items())) != report["alignment_rejection_counts"]:
        raise RuntimeError(f"candidate rejection ledger does not reconcile: {symbol}")
    passes = sum(item["status"] == "PASS" for item in decisions)
    if passes != int(report["raw_price_structurally_aligned_chain_count"]):
        raise RuntimeError(f"candidate pass ledger does not reconcile: {symbol}")
    return payload, report


def build_summary(
    *,
    universe_path: Path,
    artifact_directory: Path,
) -> dict[str, object]:
    universe, symbols = _load_verified_universe(universe_path)
    instrument_reports: list[dict[str, object]] = []
    input_artifacts: list[dict[str, object]] = []
    common_mapping = None
    common_contract = None
    common_parameter_id = None
    common_implementation = None
    common_implementation_manifest = None
    for symbol in symbols:
        path = _artifact_path(artifact_directory, symbol)
        payload, report = _load_symbol_report(path, symbol)
        for current, value, label in (
            (common_mapping, payload["mapping"], "mapping"),
            (common_contract, payload["alignment_contract"], "alignment contract"),
            (
                common_parameter_id,
                payload["alignment_parameter_set_id"],
                "alignment parameter set",
            ),
            (
                common_implementation,
                payload["implementation_sha256"],
                "implementation hash",
            ),
            (
                common_implementation_manifest,
                payload["implementation_manifest"],
                "implementation manifest",
            ),
        ):
            if current is not None and current != value:
                raise RuntimeError(f"prescreen {label} differs across symbols")
        common_mapping = payload["mapping"]
        common_contract = payload["alignment_contract"]
        common_parameter_id = payload["alignment_parameter_set_id"]
        common_implementation = payload["implementation_sha256"]
        common_implementation_manifest = payload["implementation_manifest"]
        adjustment = report["adjustment_gate"]
        data_grade = (
            "COMPONENT_ONLY"
            if adjustment["formal_chain_eligibility"]
            else "RESEARCH_ONLY"
        )
        decisions = tuple(report["candidate_alignment_decisions"])
        instrument_reports.append(
            {
                "provider_symbol": symbol,
                "project_code": report["project_code"],
                "source_start": report["source_start"],
                "source_end": report["source_end"],
                "source_sessions": report["source_sessions"],
                "rows_by_frequency": report["rows_by_frequency"],
                "data_grade": data_grade,
                "adjustment_status": adjustment["status"],
                "l0_candidates": report["l0_first_center_third_buy_count"],
                "l1_completed_trends": report["l1_completed_trend_count"],
                "l2_locators": report["l2_first_or_second_buy_count"],
                "raw_structurally_aligned_chains": report[
                    "raw_price_structurally_aligned_chain_count"
                ],
                "legal_chains": report["structurally_legal_chain_count"],
                "rejection_counts": report["alignment_rejection_counts"],
                "candidate_decision_count": len(decisions),
                "candidate_alignment_decisions": decisions,
                "candidate_counts_by_year": _decision_counts(
                    decisions, bucket="year"
                ),
                "candidate_counts_by_split": _decision_counts(
                    decisions, bucket="split"
                ),
                "decision": report["decision"],
                "highest_status": report["highest_status"],
                "live_status": report["live_status"],
            }
        )
        input_artifacts.append(
            {
                "provider_symbol": symbol,
                "path": str(path.resolve()),
                "file_sha256": sha256_file(path),
                "content_sha256": payload["content_sha256"],
            }
        )

    total_rejections = Counter(
        reason
        for report in instrument_reports
        for reason, count in report["rejection_counts"].items()
        for _ in range(int(count))
    )
    total_candidates = sum(int(item["l0_candidates"]) for item in instrument_reports)
    total_legal = sum(int(item["legal_chains"]) for item in instrument_reports)
    result: dict[str, object] = {
        "schema": "chanlun-v31-csi300-broad-etf-prescreen-summary/v1",
        "generated_at": datetime.now(CN),
        "scope": "STRICT_STRUCTURAL_PRESCREEN_NOT_PORTFOLIO_BACKTEST",
        "universe_artifact": str(universe_path.resolve()),
        "universe_artifact_sha256": sha256_file(universe_path),
        "universe_content_sha256": universe["content_sha256"],
        "universe_selection_rule": universe["selection_rule"],
        "universe_symbols": symbols,
        "input_artifacts": tuple(input_artifacts),
        "mapping": common_mapping,
        "alignment_contract": common_contract,
        "alignment_parameter_set_id": common_parameter_id,
        "prescreen_implementation_sha256": common_implementation,
        "prescreen_implementation_manifest": common_implementation_manifest,
        "candidate_traceability": {
            "complete": True,
            "required_fields": (
                "l0_point_id",
                "l0_available_at",
                "alignment_decision_at",
                "window_start",
                "window_end",
                "status",
                "reason_codes",
                "chain",
            ),
        },
        "split_policy": {
            "name": "CHRONOLOGICAL_60_20_20_NO_PARAMETER_REFIT",
            "anchor_symbol": "510300.SH",
            "splits": tuple(
                {
                    "name": item.name,
                    "start": item.start,
                    "end": item.end,
                    "sessions": item.sessions,
                }
                for item in FROZEN_SPLITS
            ),
        },
        "instrument_reports": tuple(instrument_reports),
        "totals": {
            "instruments": len(instrument_reports),
            "component_only_instruments": sum(
                item["data_grade"] == "COMPONENT_ONLY"
                for item in instrument_reports
            ),
            "research_only_raw_diagnostic_instruments": sum(
                item["data_grade"] == "RESEARCH_ONLY"
                for item in instrument_reports
            ),
            "l0_candidates": total_candidates,
            "l1_completed_trends": sum(
                int(item["l1_completed_trends"]) for item in instrument_reports
            ),
            "l2_locators": sum(
                int(item["l2_locators"]) for item in instrument_reports
            ),
            "raw_structurally_aligned_chains": sum(
                int(item["raw_structurally_aligned_chains"])
                for item in instrument_reports
            ),
            "legal_chains": total_legal,
            "rejection_counts": dict(sorted(total_rejections.items())),
            "candidate_counts_by_year": _merge_decision_counts(
                instrument_reports, "candidate_counts_by_year"
            ),
            "candidate_counts_by_split": _merge_decision_counts(
                instrument_reports, "candidate_counts_by_split"
            ),
        },
        "portfolio_backtest_gate": (
            "BLOCKED_NO_STRUCTURALLY_LEGAL_ENTRY_CHAIN"
            if total_legal == 0
            else "STRUCTURALLY_LEGAL_CHAINS_AVAILABLE"
        ),
        "complete_system_return_claim_allowed": False,
        "sample_sufficiency": {
            "strategic_cycles": 0,
            "required_strategic_cycles": 100,
            "tactical_cycles": 0,
            "required_tactical_cycles": 200,
            "status": "INSUFFICIENT_SAMPLE",
        },
        "frozen_core_modified": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    stable = {key: value for key, value in result.items() if key != "generated_at"}
    result["content_sha256"] = content_sha256(stable)
    return result


def render_markdown(summary: Mapping[str, object]) -> str:
    rows = []
    for item in summary["instrument_reports"]:
        rows.append(
            "| {provider_symbol} | {source_start}—{source_end} | {source_sessions} | "
            "{rows} | {l0_candidates} | {l1_completed_trends} | {l2_locators} | "
            "{raw_structurally_aligned_chains} | {legal_chains} | {data_grade} |".format(
                rows=item["rows_by_frequency"]["1m"], **item
            )
        )
    rejection_rows = [
        f"| `{reason}` | {count} |"
        for reason, count in summary["totals"]["rejection_counts"].items()
    ]
    year_rows = [
        f"| {label} | {item['candidates']} | {item['pass']} | {item['reject']} |"
        for label, item in summary["totals"]["candidate_counts_by_year"].items()
    ]
    split_rows = [
        f"| {label} | {item['candidates']} | {item['pass']} | {item['reject']} |"
        for label, item in summary["totals"]["candidate_counts_by_split"].items()
    ]
    candidate_rows = []
    for report in summary["instrument_reports"]:
        for item in report["candidate_alignment_decisions"]:
            reasons = "<br>".join(f"`{value}`" for value in item["reason_codes"])
            chain = "present" if item["chain"] is not None else "null"
            candidate_rows.append(
                f"| {report['provider_symbol']} | `{item['l0_point_id']}` | "
                f"{item['l0_available_at']} | {item['alignment_decision_at']} | "
                f"{item['window_start']}—{item['window_end']} | {item['status']} | "
                f"{reasons} | `{chain}` |"
            )
    return "\n".join(
        (
            "# V3.1 固定沪深300宽基 ETF 池严格结构预筛",
            "",
            "结论：固定 8 只池没有形成一条合法 L0→L1→L2 入场链，"
            "因此本产物不能进入组合收益评价，也不能给出完整 V3 收益结论。",
            "",
            f"- 最高状态：`{summary['highest_status']}`",
            f"- 实盘状态：`{summary['live_status']}`",
            f"- 组合回测门：`{summary['portfolio_backtest_gate']}`",
            f"- 候选逐条可追溯：`{summary['candidate_traceability']['complete']}`",
            "",
            "## 标的结果",
            "",
            "| 标的 | 完整区间 | 会话 | 1m行 | L0 | 完成L1 | L2 | 原始链 | 合法链 | 数据等级 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            *rows,
            "",
            "`COMPONENT_ONLY` 只表示技术结构组件具备因果公司行为账本；"
            "不表示完整选股、高级别风险、五槽组合、战略退出和短差已经通过数据门。",
            "",
            "## 聚合拒绝原因",
            "",
            "| 原因码 | 数量 |",
            "|---|---:|",
            *rejection_rows,
            "",
            "## 年度候选计数",
            "",
            "| 年度 | 候选 | 通过 | 拒绝 |",
            "|---|---:|---:|---:|",
            *year_rows,
            "",
            "## 冻结训练 / 验证 / 留出计数",
            "",
            "切分为 510300 完整会话锚定的 60/20/20，且不按结果重拟合参数。",
            "",
            "| 区间 | 候选 | 通过 | 拒绝 |",
            "|---|---:|---:|---:|",
            *split_rows,
            "",
            "## 逐候选判断账本",
            "",
            "| 标的 | L0 point_id | L0可见时点 | 判断时点 | 对齐窗口 | 状态 | 原因 | chain |",
            "|---|---|---|---|---|---|---|---|",
            *candidate_rows,
            "",
            "## 可复核性",
            "",
            "每只标的的输入 artifact 路径、文件哈希和内容哈希均记录在 JSON 汇总中；"
            "每个 L0 候选的窗口、可见/判断时点、状态、原因和 chain/null 保存在对应单标的 artifact。",
            "",
            f"汇总内容哈希：`{summary['content_sha256']}`",
            "",
        )
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument(
        "--artifact-directory",
        type=Path,
        default=Path("audit/chanlun_live_integration"),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = build_summary(
        universe_path=args.universe,
        artifact_directory=args.artifact_directory,
    )
    atomic_json(args.output, summary)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "markdown": str(args.markdown.resolve()),
                "totals": summary["totals"],
                "content_sha256": summary["content_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
