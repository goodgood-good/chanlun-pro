# -*- coding: utf-8 -*-
"""Audit cascade confirmation lag for TSLA recursive Chanlun signals.

The report explains why a buy/sell point anchor is not tradable until a later
visible bar in strict walk-forward replay.  It reuses the current trading
signal source: raw 1m chart-cache bars -> CL.get_kuozhan_levels().
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from chanlun.core.cl import CL
from chanlun.recursive_bt.engine import collect_signals
from chanlun.recursive_bt.live_backtest import _cl_config
from chanlun.recursive_bt.market_runtime import load_chart_cache_klines


DEFAULT_SIGNALS = (
    "D:/chanlun_pro/reports/"
    "us_tsla_mtf3_20260601_0610_v8_registry_layered_signals.csv"
)
DEFAULT_OUT_JSON = "D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.json"
DEFAULT_OUT_MD = "D:/chanlun_pro/reports/tsla_cascade_confirmation_audit.md"


def _ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        return pd.Timestamp(value)
    except Exception:
        return None


def _same_time(a: Any, b: Any) -> bool:
    ta = _ts(a)
    tb = _ts(b)
    if ta is None or tb is None:
        return False
    return ta == tb


def _date(obj: Any) -> str:
    k = getattr(obj, "k", None)
    d = getattr(k, "date", None)
    return "" if d is None else str(pd.Timestamp(d))


def _fx_summary(fx: Any) -> dict[str, Any]:
    return {
        "time": _date(fx),
        "value": _float(getattr(fx, "val", None)),
    }


def _line_summary(line: Any) -> dict[str, Any]:
    if line is None:
        return {}
    return {
        "type": str(getattr(line, "type", getattr(line, "_type", "")) or ""),
        "start": _fx_summary(getattr(line, "start", None)),
        "end": _fx_summary(getattr(line, "end", None)),
        "low": _float(getattr(line, "low", getattr(line, "zs_low", None))),
        "high": _float(getattr(line, "high", getattr(line, "zs_high", None))),
    }


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _center_summary(zs: Any) -> dict[str, Any]:
    if zs is None:
        return {}
    comp = list(getattr(zs, "expanded_with", []) or [])
    return {
        "zd": _float(getattr(zs, "zd", None)),
        "zg": _float(getattr(zs, "zg", None)),
        "dd": _float(getattr(zs, "dd", None)),
        "gg": _float(getattr(zs, "gg", None)),
        "done": bool(getattr(zs, "done", False)),
        "line_num": int(getattr(zs, "line_num", 0) or len(getattr(zs, "lines", []) or [])),
        "component_count": len(comp),
        "start_line": _line_summary(getattr(zs, "start", None)),
        "end_line": _line_summary(getattr(zs, "end", None)),
    }


def _load_signal_rows(path: str | Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))
    for row in rows:
        for key in ("level", "anchor_bar", "visible_bar", "next_fill_bar", "anchor_to_visible_bars"):
            if row.get(key) not in (None, ""):
                try:
                    row[key] = int(row[key])
                except Exception:
                    pass
        for key in ("signal_price", "next_fill_open"):
            if row.get(key) not in (None, ""):
                row[key] = _float(row[key])
    return rows


def _event_bar_context(event: Mapping[str, Any], dates: list[pd.Timestamp]) -> dict[str, Any]:
    anchor = _ts(event.get("anchor_time"))
    visible = _ts(event.get("visible_time"))
    out: dict[str, Any] = {}
    if anchor is None or visible is None:
        return out
    anchor_idx = next((i for i, t in enumerate(dates) if t >= anchor), None)
    visible_idx = next((i for i, t in enumerate(dates) if t >= visible), None)
    if anchor_idx is not None:
        out["loaded_anchor_bar"] = int(anchor_idx)
    if visible_idx is not None:
        out["loaded_visible_bar"] = int(visible_idx)
    if anchor_idx is not None and visible_idx is not None:
        out["loaded_anchor_to_visible_bars"] = int(visible_idx - anchor_idx)
    return out


def _build_cl(
    code: str,
    op_level: str,
    df: pd.DataFrame,
    recursive_l0_min_zs_lines: int,
) -> CL:
    cd = CL(
        code,
        op_level,
        _cl_config(recursive_l0_min_zs_lines, skip_legacy_mmd=True),
    )
    cd.process_klines(df.reset_index(drop=True))
    return cd


def _matching_bsp(cd: CL, event: Mapping[str, Any]):
    want_level = int(event.get("level") or 0)
    want_type = str(event.get("bs_type") or "")
    want_anchor = event.get("anchor_time")
    for lv in cd.get_kuozhan_levels() or []:
        level = int(lv.get("level", 0) or 0)
        if level != want_level:
            continue
        for point in lv.get("bsp") or []:
            fx = getattr(point, "anchor_fx", None)
            if (
                str(getattr(point, "bs_type", "")) == want_type
                and _same_time(_date(fx), want_anchor)
            ):
                return point
    return None


def _signal_present(cd: CL, event: Mapping[str, Any]) -> bool:
    want_level = int(event.get("level") or 0)
    want_type = str(event.get("bs_type") or "")
    want_anchor = event.get("anchor_time")
    for sig in collect_signals(cd):
        if (
            int(getattr(sig, "level", 0) or 0) == want_level
            and str(getattr(sig, "bs_type", "")) == want_type
            and _same_time(getattr(sig, "date", None), want_anchor)
        ):
            return True
    return False


def _counts_for_cd(cd: CL) -> dict[str, Any]:
    levels = cd.get_recursive_branch_levels() or []
    kuozhan = cd.get_kuozhan_levels() or []
    xds = list(cd.get_xds())
    bis = list(cd.get_bis())
    return {
        "bi_count": len(bis),
        "xd_count": len(xds),
        "last_xd": _line_summary(xds[-1]) if xds else {},
        "recursive_branch_levels": [
            {
                "level": int(getattr(lv, "level", i)),
                "zss": len(getattr(lv, "zss", []) or []),
                "live_zss": len(getattr(lv, "live_zss", []) or []),
                "zslxs": len(getattr(lv, "zslxs", []) or []),
            }
            for i, lv in enumerate(levels)
        ],
        "kuozhan_levels": [
            {
                "level": int(lv.get("level", i + 1)),
                "zss": len(lv.get("zss") or []),
                "bsp": len(lv.get("bsp") or []),
                "bcs": len(lv.get("bcs") or []),
            }
            for i, lv in enumerate(kuozhan)
        ],
    }


def _snapshot(
    df: pd.DataFrame,
    code: str,
    op_level: str,
    at: pd.Timestamp,
    event: Mapping[str, Any],
    recursive_l0_min_zs_lines: int,
    label: str,
) -> dict[str, Any]:
    dates = pd.to_datetime(df["date"])
    sub = df.loc[dates <= at].reset_index(drop=True)
    if sub.empty:
        return {
            "label": label,
            "time": str(at),
            "rows": 0,
            "matched_signal_present": False,
            "reason": "no rows at or before timestamp",
        }
    cd = _build_cl(code, op_level, sub, recursive_l0_min_zs_lines)
    return {
        "label": label,
        "time": str(at),
        "rows": int(len(sub)),
        "matched_signal_present": _signal_present(cd, event),
        **_counts_for_cd(cd),
    }


def _find_first_present(
    df: pd.DataFrame,
    code: str,
    op_level: str,
    event: Mapping[str, Any],
    recursive_l0_min_zs_lines: int,
    scan_max_bars: int,
) -> dict[str, Any]:
    if scan_max_bars <= 0:
        return {"status": "disabled", "scan_max_bars": int(scan_max_bars)}
    dates = list(pd.to_datetime(df["date"]))
    anchor = _ts(event.get("anchor_time"))
    visible = _ts(event.get("visible_time"))
    if anchor is None or visible is None:
        return {"status": "missing_time"}
    lo = next((i for i, t in enumerate(dates) if t >= anchor), None)
    hi = next((i for i, t in enumerate(dates) if t >= visible), None)
    if lo is None or hi is None:
        return {"status": "outside_loaded_data"}
    span = hi - lo
    if span < 0:
        return {"status": "invalid_order", "anchor_index": lo, "visible_index": hi}
    if span > scan_max_bars:
        return {
            "status": "skipped_span_too_long",
            "anchor_index": lo,
            "visible_index": hi,
            "span_bars": int(span),
            "scan_max_bars": int(scan_max_bars),
        }
    for i in range(lo, hi + 1):
        cd = _build_cl(code, op_level, df.iloc[: i + 1], recursive_l0_min_zs_lines)
        if _signal_present(cd, event):
            return {
                "status": "found",
                "first_visible_time": str(dates[i]),
                "first_visible_index": int(i),
                "bars_after_anchor": int(i - lo),
                "matches_signal_csv": _same_time(dates[i], event.get("visible_time")),
            }
    return {
        "status": "not_found_before_csv_visible",
        "anchor_index": lo,
        "visible_index": hi,
        "span_bars": int(span),
    }


def _event_structure(cd: CL, event: Mapping[str, Any]) -> dict[str, Any]:
    p = _matching_bsp(cd, event)
    if p is None:
        return {"matched": False}
    return {
        "matched": True,
        "bs_type": str(getattr(p, "bs_type", "")),
        "level": int(getattr(p, "level", event.get("level") or 0) or 0),
        "anchor": _fx_summary(getattr(p, "anchor_fx", None)),
        "center": _center_summary(getattr(p, "zs", None)),
        "signal_segment": _line_summary(getattr(p, "signal_seg", None)),
        "rule": _rule_text(str(getattr(p, "bs_type", ""))),
    }


def _rule_text(bs_type: str) -> str:
    if bs_type == "3buy":
        return "third buy: upward leave from center, first lower-level retest ends at/above ZG"
    if bs_type == "3sell":
        return "third sell: downward leave from center, first lower-level retest ends at/below ZD"
    if bs_type == "1buy":
        return "first buy: down leave segment shows trend divergence"
    if bs_type == "1sell":
        return "first sell: up leave segment shows trend divergence"
    return "buy/sell point"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    df = load_chart_cache_klines(args.market, args.code, args.op_level, args.chart_cache)
    df = df.sort_values("date").reset_index(drop=True)
    if args.end:
        end = pd.Timestamp(args.end)
        df = df.loc[pd.to_datetime(df["date"]) <= end].reset_index(drop=True)
    if args.start:
        start = pd.Timestamp(args.start)
        df = df.loc[pd.to_datetime(df["date"]) >= start].reset_index(drop=True)
    if df.empty:
        raise ValueError("no klines loaded for requested window")

    events = [
        row
        for row in _load_signal_rows(args.signals)
        if int(row.get("level") or 0) >= int(args.min_level)
    ]
    full_cd = _build_cl(args.code, args.op_level, df, args.recursive_l0_min_zs_lines)
    full_counts = _counts_for_cd(full_cd)
    dates = list(pd.to_datetime(df["date"]))
    event_reports = []
    for event in events:
        visible = _ts(event.get("visible_time"))
        if visible is None:
            continue
        visible_idx = next((i for i, t in enumerate(dates) if t >= visible), None)
        pre_visible = dates[max(0, visible_idx - 1)] if visible_idx is not None else visible
        anchor = _ts(event.get("anchor_time")) or visible
        event = {**dict(event), **_event_bar_context(event, dates)}
        event_reports.append(
            {
                "event": event,
                "structure": _event_structure(full_cd, event),
                "snapshots": [
                    _snapshot(
                        df,
                        args.code,
                        args.op_level,
                        anchor,
                        event,
                        args.recursive_l0_min_zs_lines,
                        "anchor_time",
                    ),
                    _snapshot(
                        df,
                        args.code,
                        args.op_level,
                        pre_visible,
                        event,
                        args.recursive_l0_min_zs_lines,
                        "before_visible",
                    ),
                    _snapshot(
                        df,
                        args.code,
                        args.op_level,
                        visible,
                        event,
                        args.recursive_l0_min_zs_lines,
                        "visible_time",
                    ),
                ],
                "first_present_scan": _find_first_present(
                    df,
                    args.code,
                    args.op_level,
                    event,
                    args.recursive_l0_min_zs_lines,
                    args.scan_max_bars,
                ),
            }
        )
    return {
        "version": 1,
        "code": args.code,
        "market": args.market,
        "op_level": args.op_level,
        "chart_cache": str(args.chart_cache),
        "signals": str(args.signals),
        "min_level": int(args.min_level),
        "recursive_l0_min_zs_lines": int(args.recursive_l0_min_zs_lines),
        "rows": int(len(df)),
        "start": str(dates[0]),
        "end": str(dates[-1]),
        "full_counts": full_counts,
        "events": event_reports,
        "interpretation": {
            "no_future_rule": "orders may use visible_time only and fill at next_fill_time",
            "cascade_rule": (
                "lower-level visible signals may trade smaller layers; a higher-level "
                "anchor remains non-tradable until the higher-level structure is visible"
            ),
        },
    }


def _fmt_pct(value: Any) -> str:
    val = _float(value)
    return "" if val is None else f"{val:.4g}"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# TSLA Chanlun Cascade Confirmation Audit",
        "",
        f"Code: `{report.get('code')}`",
        f"Source: `{report.get('signals')}`",
        f"Minimum audited level: `L{report.get('min_level')}`",
        f"Bars: `{report.get('rows')}` from `{report.get('start')}` to `{report.get('end')}`",
        f"L0 center minimum lines: `{report.get('recursive_l0_min_zs_lines')}`",
        "",
        "## Core Point",
        "",
        (
            "A Chanlun anchor time is the structural point on the final chart. "
            "It is not a tradable time in strict replay.  The tradable event is "
            "`visible_time`, and execution is delayed to `next_fill_time`."
        ),
        "",
        "Cascade analysis reduces practical lag by allowing lower-level visible "
        "signals to manage smaller layers, while 30m/L2 signals still require "
        "their own same-level confirmation.",
        "",
        (
            "The default source is the v8 continuous-registry signal CSV, so stale "
            "signals first seen before the trade window are absent from this audit."
        ),
        "",
        "## Events",
        "",
        "| Level | Type | Anchor | Visible | Next Fill | Anchor->Visible Bars | Scan |",
        "| ---: | --- | --- | --- | --- | ---: | --- |",
    ]
    for item in report.get("events", []) or []:
        event = item.get("event") or {}
        scan = item.get("first_present_scan") or {}
        scan_text = scan.get("status", "")
        if scan.get("first_visible_time"):
            scan_text = f"{scan_text}: {scan.get('first_visible_time')}"
        lines.append(
            "| {level} | {typ} | `{anchor}` | `{visible}` | `{fill}` | {lag} | {scan} |".format(
                level=event.get("level", ""),
                typ=event.get("bs_type", ""),
                anchor=event.get("anchor_time", ""),
                visible=event.get("visible_time", ""),
                fill=event.get("next_fill_time", ""),
                lag=event.get("anchor_to_visible_bars", "")
                or event.get("loaded_anchor_to_visible_bars", ""),
                scan=scan_text,
            )
        )
    lines.extend(["", "## Snapshot Proof", ""])
    for idx, item in enumerate(report.get("events", []) or [], 1):
        event = item.get("event") or {}
        structure = item.get("structure") or {}
        center = structure.get("center") or {}
        seg = structure.get("signal_segment") or {}
        lines.extend(
            [
                f"### Event {idx}: L{event.get('level')} {event.get('bs_type')}",
                "",
                f"Rule: {structure.get('rule', '')}",
                (
                    "Center: "
                    f"ZD={_fmt_pct(center.get('zd'))}, ZG={_fmt_pct(center.get('zg'))}, "
                    f"components={center.get('component_count', '')}, done={center.get('done', '')}"
                ),
                (
                    "Signal segment: "
                    f"{seg.get('type', '')} from `{(seg.get('start') or {}).get('time', '')}` "
                    f"to `{(seg.get('end') or {}).get('time', '')}`"
                ),
                "",
                "| Checkpoint | Time | Present? | BI | XD | Kuozhan Counts |",
                "| --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for snap in item.get("snapshots", []) or []:
            kz = snap.get("kuozhan_levels") or []
            kz_text = ", ".join(
                f"L{k.get('level')}: zs={k.get('zss')} bsp={k.get('bsp')} bc={k.get('bcs')}"
                for k in kz
            )
            lines.append(
                "| {label} | `{time}` | {present} | {bi} | {xd} | {kz} |".format(
                    label=snap.get("label", ""),
                    time=snap.get("time", ""),
                    present="yes" if snap.get("matched_signal_present") else "no",
                    bi=snap.get("bi_count", ""),
                    xd=snap.get("xd_count", ""),
                    kz=kz_text,
                )
            )
        lines.append("")
    lines.extend(
        [
            "## Conclusion",
            "",
            "- The report verifies the replay distinction between `anchor_time` and `visible_time`.",
            "- A lower-level cascade can be used for smaller swing/scalp layers only after its own signal is visible.",
            "- Using the higher-level anchor before `visible_time` would be a future signal.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], output_json: str | Path, output_markdown: str | Path | None) -> None:
    output_json = Path(output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_markdown:
        output_markdown = Path(output_markdown)
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(render_markdown(report), encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="us")
    parser.add_argument("--code", default="TSLA.US")
    parser.add_argument("--op-level", default="1m")
    parser.add_argument("--chart-cache", default="D:/chanlun_pro/chart_cache")
    parser.add_argument("--signals", default=DEFAULT_SIGNALS)
    parser.add_argument(
        "--min-level",
        type=int,
        default=1,
        help="Audit only recursive cascade signals at this level or above; default excludes L0 scalp rows.",
    )
    parser.add_argument("--start")
    parser.add_argument("--end", default="2026-06-10T20:00:00+00:00")
    parser.add_argument("--recursive-l0-min-zs-lines", type=int, default=3)
    parser.add_argument(
        "--scan-max-bars",
        type=int,
        default=0,
        help="Optional exact bar-by-bar scan cap; 0 keeps the report fast and uses checkpoint proof.",
    )
    parser.add_argument("--out", default=DEFAULT_OUT_JSON)
    parser.add_argument("--markdown", default=DEFAULT_OUT_MD)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_report(args)
    write_report(report, args.out, args.markdown)
    print(f"wrote={args.out}")
    if args.markdown:
        print(f"wrote={args.markdown}")
    print(f"events={len(report['events'])}")
    for item in report["events"]:
        event = item["event"]
        scan = item["first_present_scan"]
        print(
            "L{level} {typ} anchor={anchor} visible={visible} scan={scan}".format(
                level=event.get("level"),
                typ=event.get("bs_type"),
                anchor=event.get("anchor_time"),
                visible=event.get("visible_time"),
                scan=scan.get("status"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
