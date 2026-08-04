#!/usr/bin/env python3
"""Freeze and report the full-market sector-first V3 stock universe."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    load_snapshot,
)
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (  # noqa: E402
    load_sector_ledger,
)
from chanlun.decision_support.trading_system.v3_sector_first_scope import (  # noqa: E402
    build_sector_first_scope,
    current_gics_diagnostic_summary,
)
from tools.chanlun_v3_research_data import (  # noqa: E402
    atomic_json,
    content_sha256,
    sha256_file,
)


DEFAULT_SNAPSHOT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_GICS_LEDGER = Path(
    ".cache/chanlun_v3_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/v3_sector_first_full_market_scope.json"
)
DEFAULT_MARKDOWN = Path(
    "audit/chanlun_live_integration/v3_sector_first_full_market_scope.md"
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    value.add_argument("--gics-ledger", type=Path, default=DEFAULT_GICS_LEDGER)
    value.add_argument("--start", type=_parse_date, default=date(2025, 7, 25))
    value.add_argument("--end", type=_parse_date, default=date(2026, 7, 24))
    value.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    value.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    return value


def _markdown(document: Mapping[str, object]) -> str:
    counts = document["counts"]
    gics = document["current_gics_diagnostic"]
    assert isinstance(counts, Mapping) and isinstance(gics, Mapping)
    rejected = document["rejected_symbols"]
    return "\n".join(
        (
            "# V3 全市场板块优先标的范围",
            "",
            "主路径不是 8 只 ETF，而是点时申万一级板块触发后的全部当时成分股。",
            "",
            f"- 回测区间：`{document['requested_start']} ~ {document['requested_end']}`",
            f"- 板块体系：`{document['taxonomy']}`（31 个一级行业）",
            f"- 期间证券：**{counts['intersecting_security_count']}**",
            f"- 有点时行业归属、进入个股数据池：**{counts['selected_symbol_count']}**",
            f"- 无点时行业归属、显式拒绝：**{counts['rejected_symbol_count']}**",
            f"- 期末已分类证券：**{counts['end_classified_symbol_count']}**",
            f"- 行业变更事实：**{counts['membership_change_count']}**",
            "- 主选股路径：`INDIVIDUAL_THREE_PROGRAM`",
            "- ETF：仅作独立代理/消融对照，不进入个股主池",
            "- 状态：`RESEARCH_ONLY / LIVE_DISABLED`",
            "",
            "## 决策顺序",
            "",
            "```text",
            "全市场 SW1 板块点时扫描",
            "  → 触发板块在该决策时刻的成分股",
            "  → 行业长期机会 / 龙头或成长挑战者 / 市值—行业地位比价",
            "  → 市场、板块、个股月/周/日风险与5周期线",
            "  → 同一1m图递归：L0=30m战略、L1=5m短差、L2=1m定位",
            "  → 共享资金、T+1、费用、订单与成交核心",
            "```",
            "",
            "## 当前 GICS3 快照",
            "",
            f"当前诊断快照有 {gics.get('sector_count', 0)} 个板块、"
            f"{gics.get('unique_member_count', 0)} 个成员；它没有历史有效日期，"
            "因此不会回填进本回测。",
            "",
            "## 显式拒绝",
            "",
            (
                "无点时行业归属代码：" + ", ".join(str(value) for value in rejected)
                if rejected
                else "无。"
            ),
            "",
        )
    )


def _markdown_zh(document: Mapping[str, object]) -> str:
    """Render the scope report without depending on the host console encoding."""

    counts = document["counts"]
    gics = document["current_gics_diagnostic"]
    assert isinstance(counts, Mapping) and isinstance(gics, Mapping)
    rejected = tuple(str(value) for value in document["rejected_symbols"])
    lines = (
        "# V3 全市场板块优先标的范围",
        "",
        "主路径不是固定 ETF，也不是预先挑选的股票列表；而是先扫描全市场点时板块，再展开触发板块在该决策时刻的成分股。",
        "",
        f"- 回测区间：`{document['requested_start']} ~ {document['requested_end']}`",
        f"- 板块体系：`{document['taxonomy']}`（{counts['sector_count']} 个申万一级行业）",
        f"- 区间内证券：**{counts['intersecting_security_count']}**",
        f"- 有点时行业归属、进入个股数据池：**{counts['selected_symbol_count']}**",
        f"- 缺少点时行业归属、显式拒绝：**{counts['rejected_symbol_count']}**",
        f"- 区间起点已分类证券：**{counts['start_classified_symbol_count']}**",
        f"- 区间终点已分类证券：**{counts['end_classified_symbol_count']}**",
        f"- 行业归属变更事实：**{counts['membership_change_count']}**",
        "- 主选股路径：`INDIVIDUAL_THREE_PROGRAM`",
        "- ETF：仅作独立代理/消融对照，不进入个股主池",
        "- 状态：`RESEARCH_ONLY / LIVE_DISABLED`",
        "",
        "## 决策顺序",
        "",
        "```text",
        "全市场 SW1 板块点时扫描",
        "  → 触发板块在该决策时刻的点时成分股",
        "  → 行业长期机会 / 龙头或成长挑战者 / 市值—行业地位比价",
        "  → 市场、板块、个股月/周/日风险与 5 周期线",
        "  → 同一 1m 图直接递归：L0=30m 战略、L1=5m 短差、L2=1m 定位",
        "  → 共享资金、T+1、费用、订单与成交核心",
        "```",
        "",
        "## 当前 GICS3 快照",
        "",
        (
            f"当前诊断快照有 {gics.get('sector_count', 0)} 个板块、"
            f"{gics.get('unique_member_count', 0)} 个唯一成员；它没有历史有效日期，"
            "因此只用于当前诊断，不会回填进历史回测。"
        ),
        "",
        "## 显式拒绝",
        "",
        (
            "缺少点时行业归属的代码：" + ", ".join(rejected)
            if rejected
            else "无。"
        ),
        "",
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    snapshot_path = args.snapshot.resolve()
    snapshot = load_snapshot(snapshot_path)
    scope = build_sector_first_scope(
        snapshot,
        requested_start=args.start,
        requested_end=args.end,
    )
    document = scope.document()
    gics = (
        current_gics_diagnostic_summary(load_sector_ledger(args.gics_ledger))
        if args.gics_ledger.is_file()
        else current_gics_diagnostic_summary({"entries": ()})
    )
    stable = dict(document)
    stable.pop("content_sha256")
    stable["pit_snapshot"] = {
        "path": str(snapshot_path),
        "file_sha256": sha256_file(snapshot_path),
    }
    stable["current_gics_diagnostic"] = gics
    final = {**stable, "content_sha256": content_sha256(stable)}
    atomic_json(args.output, final)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(_markdown_zh(final), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "complete": True,
                "output": str(args.output.resolve()),
                "markdown": str(args.markdown.resolve()),
                "sectors": final["counts"]["sector_count"],
                "period_securities": final["counts"]["intersecting_security_count"],
                "selected_stocks": final["counts"]["selected_symbol_count"],
                "rejected_stocks": final["counts"]["rejected_symbol_count"],
                "selection_path": final["selection_path"],
                "live_status": final["live_status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
