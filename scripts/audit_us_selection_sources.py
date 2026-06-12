"""Audit local evidence for applying the original three-system selector to US names.

The original-text selector needs three independent confirmations:
fundamental, comparison/relative value, and technical Chanlun timing.  This
script only inspects local artifacts; it does not fetch or invent missing US
fundamentals.  Missing data remains a real gap for TSLA/core-9 selection.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


REPORT_DIR = Path("D:/chanlun_pro/reports")
DEFAULT_OUT_JSON = REPORT_DIR / "us_selection_source_audit.json"
DEFAULT_OUT_MD = REPORT_DIR / "us_selection_source_audit.md"
DEFAULT_CHART_CACHE = Path("D:/chanlun_pro/chart_cache")
DEFAULT_FUND_DIRS = (
    Path("D:/chanlun_pro/us_fundamentals"),
    Path("D:/chanlun_pro/bt_data_fund_us"),
    Path("D:/chanlun_pro/us_fund_data"),
)
US_CORE9 = (
    "SPY.US",
    "QQQ.US",
    "AAPL.US",
    "MSFT.US",
    "NVDA.US",
    "AMZN.US",
    "META.US",
    "GOOGL.US",
    "TSLA.US",
)
TECHNICAL_REPORTS = (
    REPORT_DIR / "us_core9_mtf3_default_summary.json",
    REPORT_DIR / "us_core9_mtf3_regime_weak1reduce_summary.json",
    REPORT_DIR / "us_tsla_mtf3_wffull_window_upgrade_l0min3_include_l0_v7_registry_layered_summary.json",
)


def _prefix(code: str) -> str:
    return f"us_{code[:-3].replace('.', '_')}_US"


def _files_for(cache_dir: Path, code: str, freq: str) -> list[str]:
    pattern = f"v*_{_prefix(code)}_{freq}_*.pkl"
    return sorted(str(p) for p in cache_dir.glob(pattern))


def _fund_files(fund_dirs: Iterable[Path], code: str) -> list[str]:
    stem = code.replace(".", "_")
    names = (
        f"{code}.json",
        f"{code}.pkl",
        f"{stem}.json",
        f"{stem}.pkl",
    )
    out: list[str] = []
    for directory in fund_dirs:
        for name in names:
            path = directory / name
            if path.exists():
                out.append(str(path))
    return sorted(out)


def _report_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {
        "path": str(path),
        "exists": True,
        "trade_count": data.get("trade_count") or data.get("trades"),
        "total_return": data.get("total_return") or data.get("total"),
        "max_drawdown": data.get("max_drawdown") or data.get("max_dd"),
        "strict_no_future": (data.get("no_future_policy") or {}).get("strict_no_future"),
        "signal_seen_registry_complete": data.get("signal_seen_registry_complete"),
    }


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    chart_cache = Path(args.chart_cache)
    fund_dirs = tuple(Path(p) for p in args.fund_dirs)
    codes = tuple(c.strip().upper() for c in args.codes.split(",") if c.strip())
    technical_rows = []
    for code in codes:
        freq_files = {
            freq: _files_for(chart_cache, code, freq)
            for freq in ("1m", "5m", "30m", "d")
        }
        technical_rows.append(
            {
                "code": code,
                "freq_file_counts": {k: len(v) for k, v in freq_files.items()},
                "has_mtf3_cache": all(freq_files[freq] for freq in ("1m", "5m", "30m")),
                "sample_files": {
                    freq: files[-1] if files else ""
                    for freq, files in freq_files.items()
                },
                "fundamental_files": _fund_files(fund_dirs, code),
            }
        )

    technical_ok = all(row["has_mtf3_cache"] for row in technical_rows)
    fundamental_coverage = sum(1 for row in technical_rows if row["fundamental_files"])
    comparison_ok = fundamental_coverage == len(technical_rows)
    reports = [_report_status(path) for path in TECHNICAL_REPORTS]
    return {
        "version": 1,
        "codes": list(codes),
        "chart_cache": str(chart_cache),
        "fund_dirs": [str(path) for path in fund_dirs],
        "technical_cache_complete": bool(technical_ok),
        "technical_rows": technical_rows,
        "fundamental_coverage": {
            "count": int(fundamental_coverage),
            "total": len(technical_rows),
            "complete": fundamental_coverage == len(technical_rows),
        },
        "comparison_value_complete": bool(comparison_ok),
        "technical_reports": reports,
        "selection_system_status": {
            "technical": "pass" if technical_ok else "gap",
            "fundamental": "pass" if fundamental_coverage == len(technical_rows) else "gap",
            "comparison": "pass" if comparison_ok else "gap",
        },
        "conclusion": (
            "US core selection is technical-only in the current local evidence; "
            "fundamental and comparison systems require point-in-time US data."
            if fundamental_coverage < len(technical_rows)
            else "US three-system local data is complete."
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    status = payload.get("selection_system_status") or {}
    lines = [
        "# US Selection Source Audit",
        "",
        f"- Chart cache: `{payload.get('chart_cache')}`",
        f"- Technical cache complete: `{payload.get('technical_cache_complete')}`",
        f"- Fundamental coverage: `{(payload.get('fundamental_coverage') or {}).get('count')}` / `{(payload.get('fundamental_coverage') or {}).get('total')}`",
        f"- Technical system: `{status.get('technical')}`",
        f"- Fundamental system: `{status.get('fundamental')}`",
        f"- Comparison system: `{status.get('comparison')}`",
        "",
        "## Core-9 Rows",
        "",
        "| Code | 1m | 5m | 30m | Daily | Fundamental Files |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in payload.get("technical_rows") or []:
        counts = row.get("freq_file_counts") or {}
        lines.append(
            "| {code} | {m1} | {m5} | {m30} | {daily} | {fund} |".format(
                code=row.get("code", ""),
                m1=counts.get("1m", 0),
                m5=counts.get("5m", 0),
                m30=counts.get("30m", 0),
                daily=counts.get("d", 0),
                fund=len(row.get("fundamental_files") or []),
            )
        )
    lines.extend(
        [
            "",
            "## Technical Reports",
            "",
            "| Path | Exists | Trades | Return | Max DD | Strict No-Future | Registry Complete |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for report in payload.get("technical_reports") or []:
        lines.append(
            "| `{path}` | {exists} | {trades} | {ret} | {dd} | {strict} | {registry} |".format(
                path=report.get("path", ""),
                exists=report.get("exists"),
                trades=report.get("trade_count", ""),
                ret=report.get("total_return", ""),
                dd=report.get("max_drawdown", ""),
                strict=report.get("strict_no_future", ""),
                registry=report.get("signal_seen_registry_complete", ""),
            )
        )
    lines.extend(["", "## Conclusion", "", str(payload.get("conclusion") or ""), ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chart-cache", default=str(DEFAULT_CHART_CACHE))
    parser.add_argument("--fund-dirs", nargs="*", default=[str(path) for path in DEFAULT_FUND_DIRS])
    parser.add_argument("--codes", default=",".join(US_CORE9))
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
    coverage = payload["fundamental_coverage"]
    print(
        "technical={technical} fundamental={fund}/{total} comparison={comparison}".format(
            technical=payload["technical_cache_complete"],
            fund=coverage["count"],
            total=coverage["total"],
            comparison=payload["comparison_value_complete"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
