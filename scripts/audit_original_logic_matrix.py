"""Build a source-grounded Chanlun original-logic coverage matrix.

The matrix is intentionally compact: it points to paragraph/image anchors in
the local DOCX index and to implementation files/tests, without copying the
source text wholesale.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = Path("D:/chanlun_pro/reports/chanlun_original_index.json")
DEFAULT_JSON = Path("D:/chanlun_pro/reports/chanlun_original_logic_matrix.json")
DEFAULT_MD = Path("D:/chanlun_pro/reports/chanlun_original_logic_matrix.md")


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.S) is not None


def _match_count(index: dict, group: str) -> int:
    return len(index.get("matches", {}).get(group, []))


def _first_idxs(index: dict, group: str, n: int = 5) -> list[int]:
    return [int(x["idx"]) for x in index.get("matches", {}).get(group, [])[:n]]


def build_matrix(index_path: Path = DEFAULT_INDEX) -> dict:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    live = _read("src/chanlun/recursive_bt/live_backtest.py")
    engine = _read("src/chanlun/recursive_bt/engine.py")
    cl = _read("src/chanlun/core/cl.py")
    zs_upgrade = _read("src/chanlun/core/zs_upgrade.py")
    bt_tests = _read("tests/test_backtest_live_parity.py")
    zs_branch_tests = _read("tests/core/test_zs_branch.py")

    requirements = [
        {
            "id": "SRC-ALL-DOCX",
            "requirement": "原文正文、重要回复、图表均有本地证据锚点。",
            "source_evidence": {
                "paragraph_count": index.get("paragraph_count"),
                "image_count": index.get("image_stats", {}).get("count"),
                "image_anchor_paragraphs": len(index.get("image_anchor_paragraphs", [])),
                "lesson_headers": len(index.get("lesson_headers", [])),
                "reply_like_anchor_count": index.get("reply_like_anchor_count"),
            },
            "implementation_evidence": ["scripts/audit_chanlun_original.py"],
            "status": "pass"
            if index.get("paragraph_count", 0) > 20000
            and index.get("image_stats", {}).get("count", 0) >= 1000
            and len(index.get("image_anchor_paragraphs", [])) >= index.get("image_stats", {}).get("count", 0)
            else "gap",
        },
        {
            "id": "ZS-L0-THREE-SEG",
            "requirement": "最低递归级别中枢本体按三段次级别走势类型重叠；4 段只能是显式 legacy 确认门控。",
            "source_evidence": {
                "groups": {"center": _first_idxs(index, "center"), "same_level": _first_idxs(index, "same_level")},
            },
            "implementation_evidence": [
                "src/chanlun/recursive_bt/live_backtest.py:DEFAULT_RECURSIVE_L0_MIN_ZS_LINES",
                "src/chanlun/recursive_bt/engine.py:CL_CFG.recursive_l0_min_zs_lines",
                "tests/test_backtest_live_parity.py:test_live_backtest_cli_defaults_to_walk_forward_signal_mode",
                "tests/core/test_zs_branch.py:test_calculator_min3_confirms_three_segment_center_on_next_leave",
            ],
            "status": "pass"
            if _has(r"DEFAULT_RECURSIVE_L0_MIN_ZS_LINES\s*=\s*3", live)
            and _has(r'"recursive_l0_min_zs_lines"\s*:\s*3', engine)
            and _has(r"assert args\.recursive_l0_min_zs_lines == 3", bt_tests)
            and "test_calculator_min3_confirms_three_segment_center_on_next_leave" in zs_branch_tests
            else "gap",
        },
        {
            "id": "LEVEL-30M-TJB",
            "requirement": "30m 级别采用同级别分解，30m 以下采用非同级别扩展/扩张级联。",
            "source_evidence": {
                "groups": {"same_level": _first_idxs(index, "same_level"), "center_change": _first_idxs(index, "center_change")},
            },
            "implementation_evidence": [
                "src/chanlun/core/cl.py:_UPGRADE_CHAIN",
                "src/chanlun/core/zs_upgrade.py:tongjibie_zhongshu_ex",
                "tests/core/test_zs_upgrade.py",
            ],
            "status": "pass"
            if '"1m": [("5m", "kuozhan"), ("30m", "tongjibie")]' in cl
            and '"5m": [("30m", "tongjibie")]' in cl
            and "def tongjibie_zhongshu_ex" in zs_upgrade
            else "gap",
        },
        {
            "id": "NO-FUTURE-VISIBLE",
            "requirement": "买卖点按实时数据逐根生成；锚点、可见点、下一根成交三分离，禁止窗口前旧信号二次触发。",
            "source_evidence": {
                "groups": {"bsp": _first_idxs(index, "bsp"), "cascade": _first_idxs(index, "cascade")},
            },
            "implementation_evidence": [
                "src/chanlun/recursive_bt/live_backtest.py:_registry_scan_clock",
                "src/chanlun/recursive_bt/live_backtest.py:_walk_forward_signals_by_main_bar",
                "tests/test_backtest_live_parity.py:test_walk_forward_full_history_registry_suppresses_stale_reappearing_signal",
            ],
            "status": "pass"
            if "def _registry_scan_clock" in live
            and "emit_start_idx" in live
            and "stale_reappearing_signal_risk" in live
            and "test_walk_forward_full_history_registry_suppresses_stale_reappearing_signal" in bt_tests
            else "gap",
        },
        {
            "id": "BSP-STRUCTURAL-STOPS",
            "requirement": "二买/二卖/三买/三卖必须导出原文结构边界，失效后不能机械持有。",
            "source_evidence": {
                "groups": {"bsp": _first_idxs(index, "bsp"), "divergence": _first_idxs(index, "divergence")},
            },
            "implementation_evidence": [
                "src/chanlun/recursive_bt/engine.py:_structural_signal_fields",
                "tests/test_backtest_live_parity.py:test_structural_signal_fields_preserves_explicit_second_class_stop",
            ],
            "status": "pass"
            if "structural_stop_below" in engine
            and "structural_stop_above" in engine
            and "test_structural_signal_fields_preserves_explicit_second_class_stop" in bt_tests
            else "gap",
        },
    ]

    return {
        "index": str(index_path),
        "docx": index.get("docx"),
        "keyword_match_counts": {
            group: _match_count(index, group)
            for group in index.get("keyword_groups", {})
        },
        "requirements": requirements,
        "gap_count": sum(1 for r in requirements if r["status"] != "pass"),
    }


def write_markdown(matrix: dict, out: Path = DEFAULT_MD) -> None:
    lines = [
        "# Chanlun Original Logic Matrix",
        "",
        f"- DOCX: `{matrix.get('docx')}`",
        f"- Index: `{matrix.get('index')}`",
        f"- Gap count: `{matrix.get('gap_count')}`",
        "",
        "## Keyword Match Counts",
        "",
        "| Group | Hits |",
        "| --- | ---: |",
    ]
    for group, count in matrix.get("keyword_match_counts", {}).items():
        lines.append(f"| `{group}` | `{count}` |")
    lines.extend(["", "## Requirements", "", "| ID | Status | Requirement | Evidence |", "| --- | --- | --- | --- |"])
    for req in matrix.get("requirements", []):
        evidence = "<br>".join(f"`{x}`" for x in req.get("implementation_evidence", []))
        lines.append(
            f"| `{req['id']}` | `{req['status']}` | {req['requirement']} | {evidence} |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    matrix = build_matrix()
    DEFAULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_JSON.write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(matrix)
    print(f"wrote={DEFAULT_JSON}")
    print(f"wrote={DEFAULT_MD}")
    print(f"gap_count={matrix['gap_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
