"""Build an evidence matrix for the original-text Chanlun trading system.

This audit is intentionally stricter than the low-level logic matrix: it checks
whether the current worktree has executable evidence for stock selection, buy
and sell points, no-future replay, and position management.  Partial rows are
not failures of the code; they mark places where the active goal is not yet
fully proven by current artifacts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = Path("D:/chanlun_pro/reports")
DEFAULT_OUT_JSON = REPORT_DIR / "chanlun_original_trading_system_matrix.json"
DEFAULT_OUT_MD = REPORT_DIR / "chanlun_original_trading_system_matrix.md"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _has(path: str, needle: str) -> bool:
    return needle in _read(ROOT / path)


def _path_exists(path: str | Path) -> bool:
    return Path(path).exists()


def _summary_value(summary: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in summary:
            return summary.get(key)
    return None


def build_matrix() -> dict[str, Any]:
    original_index = _load_json(REPORT_DIR / "chanlun_original_index.json")
    logic_matrix = _load_json(REPORT_DIR / "chanlun_original_logic_matrix.json")
    v8_summary = _load_json(
        REPORT_DIR / "us_core3_mtf3_20260601_0610_v8_registry_layered_summary.json"
    )
    us_selection = _load_json(REPORT_DIR / "us_selection_source_audit.json")
    robustness = _load_json(REPORT_DIR / "chanlun_robustness_evidence_audit.json")
    no_future_policy = v8_summary.get("no_future_policy") or {}
    cascade = _load_json(REPORT_DIR / "tsla_cascade_confirmation_audit.json")

    image_stats = original_index.get("image_stats") or {}
    image_count = (
        image_stats.get("count")
        if isinstance(image_stats, dict)
        else original_index.get("image_count") or original_index.get("images") or 0
    )
    source_ok = (
        int(original_index.get("paragraph_count") or original_index.get("paragraphs") or 0)
        >= 20000
        and int(image_count or 0) >= 1000
        and int(logic_matrix.get("gap_count") or 0) == 0
    )
    v8_no_future_ok = (
        bool(no_future_policy.get("strict_no_future"))
        and no_future_policy.get("decision_time") == "visible bar close"
        and no_future_policy.get("execution_time") == "next bar open"
        and bool(no_future_policy.get("signal_seen_registry_complete"))
        and not bool(no_future_policy.get("stale_reappearing_signal_risk"))
    )
    cascade_events = cascade.get("events") if isinstance(cascade.get("events"), list) else []

    def _cascade_snapshot_no_future_ok(item: Mapping[str, Any]) -> bool:
        snaps = item.get("snapshots") if isinstance(item.get("snapshots"), list) else []
        by_label = {
            str(s.get("label")): s for s in snaps if isinstance(s, dict)
        }
        anchor = by_label.get("anchor_time")
        visible = by_label.get("visible_time")
        return bool(
            anchor is not None
            and visible is not None
            and anchor.get("matched_signal_present") is False
            and visible.get("matched_signal_present") is True
        )

    cascade_ok = (
        str(cascade.get("signals", "")).endswith("_v8_registry_layered_signals.csv")
        and int(cascade.get("min_level") or 0) >= 1
        and bool(cascade_events)
        and all(
            _cascade_snapshot_no_future_ok(item)
            for item in cascade_events
            if isinstance(item, dict)
        )
    )

    rows = [
        {
            "id": "SRC-FULL-ORIGINAL",
            "status": "pass" if source_ok else "gap",
            "requirement": "Full DOCX text, replies, and chart anchors are indexed before deriving rules.",
            "evidence": [
                "D:/chanlun_pro/reports/chanlun_original_index.json",
                "D:/chanlun_pro/reports/chanlun_original_logic_matrix.json",
            ],
            "finding": "" if source_ok else "Original evidence index or logic matrix is missing/incomplete.",
        },
        {
            "id": "LEVEL-30M-SAME-BELOW-CASCADE",
            "status": (
                "pass"
                if _has(
                    "src/chanlun/core/cl.py",
                    '"1m": [("5m", "kuozhan"), ("30m", "tongjibie")]',
                )
                and _has("src/chanlun/core/cl.py", '"5m": [("30m", "tongjibie")]')
                and _has("src/chanlun/core/zs_upgrade.py", "def tongjibie_zhongshu_ex")
                and _path_exists(REPORT_DIR / "tsla_tongjibie_candidate_audit.md")
                else "gap"
            ),
            "requirement": "30m uses same-level decomposition; below 30m uses non-same-level extension/expansion cascade.",
            "evidence": [
                "src/chanlun/core/cl.py:_UPGRADE_CHAIN",
                "src/chanlun/core/zs_upgrade.py:tongjibie_zhongshu_ex",
                "D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.md",
            ],
            "finding": "",
        },
        {
            "id": "NO-FUTURE-VISIBLE-REPLAY",
            "status": "pass" if v8_no_future_ok else "gap",
            "requirement": "Buy/sell points are generated by walk-forward visibility; anchor time is not tradable.",
            "evidence": [
                "src/chanlun/recursive_bt/live_backtest.py:_registry_scan_clock",
                "D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_summary.json",
                "D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_signals.csv",
            ],
            "finding": "" if v8_no_future_ok else "Latest v8 summary does not prove the strict no-future policy.",
        },
        {
            "id": "CASCADE-LAG-CONTROL",
            "status": "pass" if cascade_ok else "gap",
            "requirement": "Cascade analysis may reduce practical lag only after the lower-level signal itself is visible.",
            "evidence": [
                "scripts/audit_cascade_confirmation_tsla.py",
                "D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md",
            ],
            "finding": "" if cascade_ok else "Cascade report is stale or not based on v8 continuous-registry signals.",
        },
        {
            "id": "BSP-STRUCTURAL-INVALIDATION",
            "status": (
                "pass"
                if _has("src/chanlun/recursive_bt/engine.py", "def _structural_signal_fields")
                and _has("src/chanlun/recursive_bt/portfolio.py", "def _position_structural_invalidation")
                and _path_exists(REPORT_DIR / "tsla_wffull_window_v7_registry_trade_invalidation_audit.md")
                else "gap"
            ),
            "requirement": "First/second/third buy and sell points export structural boundaries; invalidated structures exit.",
            "evidence": [
                "src/chanlun/recursive_bt/engine.py:_structural_signal_fields",
                "src/chanlun/recursive_bt/portfolio.py:_position_structural_invalidation",
                "D:/chanlun_pro/reports/tsla_wffull_window_v7_registry_trade_invalidation_audit.md",
            ],
            "finding": "",
        },
        {
            "id": "SELECTION-THREE-SYSTEMS",
            "status": "partial",
            "requirement": "Stock selection should combine independent fundamental, comparison, and technical systems.",
            "evidence": [
                "src/chanlun/recursive_bt/chanlun_selector.py:OriginalChanlunASelector",
                "src/chanlun/recursive_bt/systems.py:main_v3",
                "tests/test_chanlun_selector.py",
                "tests/test_strategy_optimizer.py:test_a_selection_systems_define_three_independent_confirmations",
                "D:/chanlun_pro/reports/us_selection_source_audit.md",
            ],
            "finding": (
                "A-share selection has executable three-system evidence, but the current TSLA/core-9 US replay is "
                "technical-only.  US local data audit reports fundamental coverage "
                f"{(us_selection.get('fundamental_coverage') or {}).get('count', 0)}/"
                f"{(us_selection.get('fundamental_coverage') or {}).get('total', 0)} "
                "and comparison system gap. TSLA single-symbol results must not be described as a complete "
                "original-text stock selection system."
            ),
        },
        {
            "id": "POSITION-MULTI-LAYER",
            "status": (
                "pass"
                if (
                    _has("src/chanlun/recursive_bt/engine.py", "original_layered")
                    and _has("src/chanlun/recursive_bt/portfolio.py", "sell_ratio_policy")
                )
                else "partial"
            ),
            "requirement": "Position management should separate core/swing/scalp layers and adapt size to level, trend, and resonance.",
            "evidence": [
                "src/chanlun/recursive_bt/engine.py:recommended_buy_ratio",
                "src/chanlun/recursive_bt/engine.py:recommended_sell_ratio",
                "src/chanlun/recursive_bt/live_backtest.py:--sell-ratio-policy original_layered",
                "src/chanlun/recursive_bt/portfolio.py:core_shares/swing_shares/scalp_shares",
                "tests/test_backtest_live_parity.py",
            ],
            "finding": (
                "Executable layered sell policy now exists; legacy all_out remains the baseline unless "
                "`--sell-ratio-policy original_layered` is enabled for the replay."
            ),
        },
        {
            "id": "ROBUST-LOW-DD-HIGH-RETURN-EVIDENCE",
            "status": "gap",
            "requirement": "The final trading system must be proven on enough walk-forward samples to justify low drawdown and high return claims.",
            "evidence": [
                "D:/chanlun_pro/reports/us_tsla_mtf3_20260601_0610_v8_registry_layered_summary.json",
                "D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_summary.json",
                "D:/chanlun_pro/reports/us_core3_mtf3_20260601_0610_v8_registry_layered_trades.csv",
                "D:/chanlun_pro/reports/chanlun_robustness_evidence_audit.md",
                "scripts/prewarm_live_backtest_signal_cache.py",
                "src/chanlun/recursive_bt/live_backtest.py:_signal_checkpoint_settings",
                "D:/chanlun_pro/reports/live_backtest_signal_cache_prewarm_core3_202606_v8_registry_manifest.json",
                "D:/chanlun_pro/reports/live_backtest_signal_checkpoints_v8_l0min3_registry_core3_202606",
                "src/chanlun/recursive_bt/strategy_optimizer.py",
            ],
            "finding": (
                "The best strict original-registry report currently has "
                f"{int(robustness.get('best_strict_original_registry_trade_count') or _summary_value(v8_summary, 'trade_count') or 0)} "
                "trade(s), below the robustness floor. A per-symbol strict signal-cache prewarm path now exists "
                "and long scans can checkpoint/resume, but older core9/high-trade reports remain research evidence, "
                "not final proof, unless they are regenerated with complete v8 continuous registry."
            ),
        },
    ]
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    return {
        "version": 1,
        "rows": rows,
        "status_counts": status_counts,
        "gap_count": status_counts.get("gap", 0),
        "partial_count": status_counts.get("partial", 0),
    }


def render_markdown(payload: Mapping[str, Any]) -> str:
    rows = payload.get("rows") or []
    lines = [
        "# Chanlun Original Trading System Matrix",
        "",
        f"- Gap count: `{payload.get('gap_count', 0)}`",
        f"- Partial count: `{payload.get('partial_count', 0)}`",
        "",
        "| ID | Status | Requirement | Evidence | Finding |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        evidence = "<br>".join(f"`{item}`" for item in row.get("evidence", []))
        finding = row.get("finding") or ""
        lines.append(
            "| {id} | `{status}` | {req} | {evidence} | {finding} |".format(
                id=row.get("id", ""),
                status=row.get("status", ""),
                req=row.get("requirement", ""),
                evidence=evidence,
                finding=finding,
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--markdown", default=str(DEFAULT_OUT_MD))
    args = parser.parse_args()

    payload = build_matrix()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        md = Path(args.markdown)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(payload), encoding="utf-8")
    print(f"wrote={out}")
    if args.markdown:
        print(f"wrote={args.markdown}")
    print(
        "rows={rows} pass={passed} partial={partial} gap={gap}".format(
            rows=len(payload.get("rows") or []),
            passed=payload.get("status_counts", {}).get("pass", 0),
            partial=payload.get("partial_count", 0),
            gap=payload.get("gap_count", 0),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
