"""Classify existing backtest summaries by no-future proof strength.

High-return reports are useful research artifacts, but the active goal requires
strict walk-forward evidence: original 3-line centers, visible-time decisions,
next-bar fills, and a complete continuous first-seen signal registry.  This
script makes that distinction explicit across local summary files.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any, Mapping


REPORT_DIR = Path("D:/chanlun_pro/reports")
DEFAULT_OUT_JSON = REPORT_DIR / "chanlun_robustness_evidence_audit.json"
DEFAULT_OUT_MD = REPORT_DIR / "chanlun_robustness_evidence_audit.md"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _float(data: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = data.get(key)
            if value is not None:
                return float(value)
        except Exception:
            continue
    return 0.0


def _int(data: Mapping[str, Any], *keys: str) -> int:
    return int(_float(data, *keys))


def classify(path: Path, data: Mapping[str, Any]) -> dict[str, Any]:
    policy = data.get("no_future_policy") if isinstance(data.get("no_future_policy"), dict) else {}
    strict = bool(policy.get("strict_no_future"))
    registry_complete = bool(
        data.get("signal_seen_registry_complete")
        if data.get("signal_seen_registry_complete") is not None
        else policy.get("signal_seen_registry_complete")
    )
    stale_risk = bool(policy.get("stale_reappearing_signal_risk"))
    min3 = int(data.get("recursive_l0_min_zs_lines") or 0) == 3
    walk_forward = data.get("signal_mode") == "walk_forward" or policy.get("signal_mode") == "walk_forward"
    warmup = data.get("signal_warmup_bars")
    if warmup is None:
        warmup = policy.get("signal_warmup_bars")
    try:
        warmup_i = int(warmup)
    except Exception:
        warmup_i = None

    if strict and walk_forward and registry_complete and not stale_risk and min3:
        grade = "strict_original_registry"
    elif strict and walk_forward and not registry_complete:
        grade = "strict_but_registry_incomplete"
    elif walk_forward and warmup_i is not None and warmup_i >= 0:
        grade = "bounded_warmup_walk_forward"
    else:
        grade = "legacy_or_unknown"

    return {
        "path": str(path),
        "name": path.name,
        "grade": grade,
        "market": data.get("market"),
        "symbols": data.get("universe_size") or len(data.get("symbol_codes") or []),
        "trade_count": _int(data, "trade_count", "trades", "n"),
        "total_return": _float(data, "total_return", "total"),
        "buy_hold": _float(data, "buy_hold", "bh"),
        "max_drawdown": _float(data, "max_drawdown", "max_dd"),
        "strict_no_future": strict,
        "walk_forward": walk_forward,
        "recursive_l0_min_zs_lines": data.get("recursive_l0_min_zs_lines"),
        "signal_seen_registry_complete": registry_complete,
        "stale_reappearing_signal_risk": stale_risk,
        "signal_warmup_bars": warmup_i,
        "sell_ratio_policy": data.get("sell_ratio_policy"),
    }


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    paths: list[Path] = []
    for pattern in args.patterns:
        paths.extend(Path(p) for p in glob.glob(pattern))
    paths = sorted(set(paths), key=lambda p: str(p).lower())
    rows = [classify(path, _load(path)) for path in paths]
    rows = [
        row
        for row in rows
        if args.include_legacy or row["grade"] != "legacy_or_unknown" or str(row["name"]).startswith(("us_", "tsla_"))
    ]
    grade_counts: dict[str, int] = {}
    for row in rows:
        grade_counts[row["grade"]] = grade_counts.get(row["grade"], 0) + 1
    strict_rows = [row for row in rows if row["grade"] == "strict_original_registry"]
    best_strict_trades = max((int(row["trade_count"]) for row in strict_rows), default=0)
    return {
        "version": 1,
        "patterns": list(args.patterns),
        "min_trades_for_robust_claim": int(args.min_trades),
        "rows": rows,
        "grade_counts": grade_counts,
        "strict_original_registry_report_count": len(strict_rows),
        "best_strict_original_registry_trade_count": best_strict_trades,
        "robust_claim_supported": best_strict_trades >= int(args.min_trades),
        "finding": (
            "Strict original-registry evidence is still too small for a robust low-drawdown/high-return claim."
            if best_strict_trades < int(args.min_trades)
            else "Strict original-registry evidence meets the configured trade-count floor."
        ),
    }


def render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Chanlun Robustness Evidence Audit",
        "",
        f"- Strict original-registry reports: `{payload.get('strict_original_registry_report_count')}`",
        f"- Best strict original-registry trade count: `{payload.get('best_strict_original_registry_trade_count')}`",
        f"- Trade-count floor: `{payload.get('min_trades_for_robust_claim')}`",
        f"- Robust claim supported: `{payload.get('robust_claim_supported')}`",
        "",
        "## Grade Counts",
        "",
        "| Grade | Count |",
        "| --- | ---: |",
    ]
    for grade, count in sorted((payload.get("grade_counts") or {}).items()):
        lines.append(f"| `{grade}` | {count} |")
    lines.extend(
        [
            "",
            "## Reports",
            "",
            "| Grade | Trades | Return | Max DD | Registry | Stale Risk | Policy | File |",
            "| --- | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    order = {
        "strict_original_registry": 0,
        "strict_but_registry_incomplete": 1,
        "bounded_warmup_walk_forward": 2,
        "legacy_or_unknown": 3,
    }
    rows = sorted(
        payload.get("rows") or [],
        key=lambda row: (order.get(row.get("grade"), 9), -int(row.get("trade_count") or 0), row.get("name") or ""),
    )
    for row in rows[:80]:
        lines.append(
            "| `{grade}` | {trades} | {ret:.2%} | {dd:.2%} | {registry} | {stale} | {policy} | `{name}` |".format(
                grade=row.get("grade", ""),
                trades=int(row.get("trade_count") or 0),
                ret=float(row.get("total_return") or 0.0),
                dd=float(row.get("max_drawdown") or 0.0),
                registry=row.get("signal_seen_registry_complete"),
                stale=row.get("stale_reappearing_signal_risk"),
                policy=row.get("sell_ratio_policy") or "",
                name=row.get("name", ""),
            )
        )
    lines.extend(["", "## Finding", "", str(payload.get("finding") or ""), ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--patterns",
        nargs="*",
        default=[
            "D:/chanlun_pro/reports/us_*summary.json",
            "D:/chanlun_pro/reports/tsla_*summary.json",
        ],
    )
    parser.add_argument("--min-trades", type=int, default=30)
    parser.add_argument("--include-legacy", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--markdown", default=str(DEFAULT_OUT_MD))
    args = parser.parse_args(argv)

    payload = build_audit(args)
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
        "strict_reports={reports} best_trades={trades} robust={robust}".format(
            reports=payload["strict_original_registry_report_count"],
            trades=payload["best_strict_original_registry_trade_count"],
            robust=payload["robust_claim_supported"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
