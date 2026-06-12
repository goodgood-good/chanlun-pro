"""Audit TSLA 30m same-level decomposition candidates from raw chart-cache bars.

This is a diagnostic script, not a trading signal source.  It records every
three-leg overlap candidate and the non-overlapping groups actually admitted by
the same-level decomposition used for trading.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.zs_upgrade import (
    _swing_alternating_segs,
    _tongjibie_candidate_groups,
    _tongjibie_groups,
    kuozhan_zhongshu,
    tongjibie_zhongshu_ex,
)
from chanlun.recursive_bt.engine import collect_signals
from chanlun.recursive_bt.live_backtest import _cl_config, load_chart_cache_klines


def _date(obj) -> str | None:
    k = getattr(obj, "k", None)
    d = getattr(k, "date", None)
    return None if d is None else str(d)


def _line_date(ln, attr: str) -> str | None:
    fx = getattr(ln, attr, None)
    return _date(fx)


def _l1_row(i: int, z, lower_index: dict[int, int]) -> dict:
    comp = list(getattr(z, "expanded_with", []) or [])
    return {
        "index": i,
        "zd": float(getattr(z, "zd", 0.0) or 0.0),
        "zg": float(getattr(z, "zg", 0.0) or 0.0),
        "dd": float(getattr(z, "dd", 0.0) or 0.0),
        "gg": float(getattr(z, "gg", 0.0) or 0.0),
        "done": bool(getattr(z, "done", False)),
        "line_num": int(getattr(z, "line_num", 0) or len(getattr(z, "lines", []) or [])),
        "expanded_with_count": len(comp),
        "expanded_with_l0_indices": [
            lower_index[id(child)] for child in comp if id(child) in lower_index
        ],
        "start_time": _line_date(getattr(z, "start", None), "start"),
        "end_time": _line_date(getattr(z, "end", None), "end"),
    }


def _seg_row(i: int, seg) -> dict:
    return {
        "index": i,
        "dir": getattr(seg, "dir", ""),
        "start_time": _date(getattr(seg, "start", None)),
        "end_time": _date(getattr(seg, "end", None)),
        "low": float(getattr(seg, "zs_low", 0.0) or 0.0),
        "high": float(getattr(seg, "zs_high", 0.0) or 0.0),
        "child_zhongshu_count": len(getattr(seg, "zss", []) or []),
    }


def _group_interval(segs, group: tuple[int, int]) -> dict:
    s, e = group
    tri = segs[s:e + 1]
    return {
        "start": int(s),
        "end": int(e),
        "dirs": "".join(str(getattr(seg, "dir", ""))[0].upper() for seg in tri),
        "zd": float(max(seg.zs_low for seg in tri)),
        "zg": float(min(seg.zs_high for seg in tri)),
        "start_time": _date(getattr(tri[0], "start", None)),
        "end_time": _date(getattr(tri[-1], "end", None)),
    }


def _candidate_reason(group: tuple[int, int], selected: set[tuple[int, int]]) -> str:
    if tuple(group) in selected:
        return "selected"
    s, e = group
    for ss, ee in selected:
        if not (e < ss or ee < s):
            return "overlaps_selected_prefix"
    return "skipped_by_non_overlapping_scan"


def _third_signal_diagnostic(segs, group: tuple[int, int], z) -> dict:
    s, e = group
    last = segs[e] if e < len(segs) else None
    after = segs[e + 1] if e + 1 < len(segs) else None
    if after is None:
        return {
            "group": [int(s), int(e)],
            "status": "no_after_segment",
            "leave_dir": getattr(last, "dir", None),
        }
    leave_dir = getattr(last, "dir", "")
    after_dir = getattr(after, "dir", "")
    after_end = getattr(after, "end", None)
    after_end_val = getattr(after_end, "val", None)
    if after_end is None or after_end_val is None:
        return {
            "group": [int(s), int(e)],
            "status": "after_segment_unfinished",
            "leave_dir": leave_dir,
            "after_dir": after_dir,
        }
    if leave_dir == "up":
        ok = after_dir == "down" and after_end_val >= z.zg
        return {
            "group": [int(s), int(e)],
            "expected": "3buy",
            "status": "ok" if ok else "retest_breaks_core_or_wrong_direction",
            "leave_dir": leave_dir,
            "after_dir": after_dir,
            "core_edge": float(z.zg),
            "after_end_time": _date(after.end),
            "after_end_val": float(after_end_val),
        }
    if leave_dir == "down":
        ok = after_dir == "up" and after_end_val <= z.zd
        return {
            "group": [int(s), int(e)],
            "expected": "3sell",
            "status": "ok" if ok else "retest_breaks_core_or_wrong_direction",
            "leave_dir": leave_dir,
            "after_dir": after_dir,
            "core_edge": float(z.zd),
            "after_end_time": _date(after.end),
            "after_end_val": float(after_end_val),
        }
    return {
        "group": [int(s), int(e)],
        "status": "unknown_leave_direction",
        "leave_dir": leave_dir,
        "after_dir": after_dir,
    }


def build_audit(args: argparse.Namespace) -> dict:
    df = load_chart_cache_klines(args.market, args.code, args.op_level, args.chart_cache)
    df = df.sort_values("date").reset_index(drop=True)
    if args.end:
        end = pd.Timestamp(args.end)
        df = df[pd.to_datetime(df["date"]) <= end].reset_index(drop=True)

    cd = CL(
        args.code,
        args.op_level,
        _cl_config(args.recursive_l0_min_zs_lines, skip_legacy_mmd=True),
    )
    cd.process_klines(df)

    levels = cd.get_recursive_branch_levels()
    lv0 = levels[0] if levels else None
    xds = list(cd.get_xds())
    l0_zss = lv0.zss if lv0 is not None else []
    l0_index = {id(z): i for i, z in enumerate(l0_zss)}
    l1 = kuozhan_zhongshu(l0_zss, xds)
    l1_index = {id(z): i for i, z in enumerate(l1)}
    segs = _swing_alternating_segs(l1)
    candidates = _tongjibie_candidate_groups(segs)
    chosen = _tongjibie_groups(segs)
    l2, meta = tongjibie_zhongshu_ex(l1, xds)

    selected = {tuple(g) for g in meta.get("groups", []) or chosen}
    chain_levels = cd.get_kuozhan_levels()
    signals = [
        {
            "level": int(sig.level),
            "bs_type": sig.bs_type,
            "anchor_time": str(sig.date),
            "price": float(sig.price),
        }
        for sig in collect_signals(cd)
        if int(sig.level) == 2
    ]

    return {
        "code": args.code,
        "market": args.market,
        "op_level": args.op_level,
        "source": "raw chart-cache klines",
        "upgrade_chain": [
            {"target_level": target, "method": method}
            for target, method in getattr(CL, "_UPGRADE_CHAIN", {}).get(args.op_level, [])
        ],
        "method_contract": {
            "below_30m": "kuozhan: non-same-level extension/expansion from lower centers",
            "at_30m": "tongjibie: same-level decomposition from three alternating lower-level trend segments",
            "base_30m_chart": "no upgrade chain; render current 30m BI/centers/buy-sell/divergence",
        },
        "end": args.end,
        "rows": int(len(df)),
        "recursive_l0_min_zs_lines": int(args.recursive_l0_min_zs_lines),
        "recursive_level_counts": [
            {
                "level": int(getattr(lv, "level", i)),
                "zss": len(getattr(lv, "zss", []) or []),
                "zslxs": len(getattr(lv, "zslxs", []) or []),
            }
            for i, lv in enumerate(levels)
        ],
        "get_kuozhan_levels_counts": [
            {
                "level": int(item.get("level", i + 1)),
                "zss": len(item.get("zss") or []),
                "bsp": len(item.get("bsp") or []),
                "bcs": len(item.get("bcs") or []),
            }
            for i, item in enumerate(chain_levels)
        ],
        "l1_zhongshu_count": int(len(l1)),
        "tongjibie_segment_count": int(len(segs)),
        "candidate_count": int(len(candidates)),
        "chosen_count": int(len(chosen)),
        "l2_zhongshu_count": int(len(l2)),
        "l1_zhongshu": [_l1_row(i, z, l0_index) for i, z in enumerate(l1)],
        "segments": [_seg_row(i, seg) for i, seg in enumerate(segs)],
        "candidate_groups": [
            {
                **_group_interval(segs, g),
                "selected": tuple(g) in selected,
                "reason": _candidate_reason(g, selected),
            }
            for g in candidates
        ],
        "chosen_groups": [_group_interval(segs, g) for g in chosen],
        "l2_zhongshu": [
            {
                "group": list(g) if i < len(meta.get("groups", [])) else None,
                "zd": float(z.zd),
                "zg": float(z.zg),
                "dd": float(z.dd),
                "gg": float(z.gg),
                "done": bool(getattr(z, "done", False)),
                "expanded_with_l1_indices": [
                    l1_index[id(child)]
                    for child in (getattr(z, "expanded_with", []) or [])
                    if id(child) in l1_index
                ],
                "third_signal_diagnostic": _third_signal_diagnostic(segs, g, z),
            }
            for i, (z, g) in enumerate(zip(l2, meta.get("groups", []) or []))
        ],
        "l2_signals": signals,
    }


def _pct_bool(value: bool) -> str:
    return "yes" if value else "no"


def render_markdown(payload: dict) -> str:
    chain = payload.get("upgrade_chain") or []
    lines = [
        "# TSLA 30m Same-Level Decomposition Audit",
        "",
        f"Code: `{payload.get('code')}`",
        f"Market: `{payload.get('market')}`",
        f"Operation Level: `{payload.get('op_level')}`",
        f"Rows: `{payload.get('rows')}`",
        f"End: `{payload.get('end')}`",
        f"Source: `{payload.get('source')}`",
        "",
        "## Original-Level Contract",
        "",
        "| Scope | Method |",
        "| --- | --- |",
        "| Below 30m | `kuozhan` / non-same-level extension-expansion |",
        "| 30m | `tongjibie` / same-level decomposition |",
        "| 30m K chart | base current-level BI, centers, buy-sell, divergence |",
        "",
        "## Active Upgrade Chain",
        "",
        "| Step | Target Level | Method |",
        "| ---: | --- | --- |",
    ]
    if chain:
        for idx, item in enumerate(chain, start=1):
            lines.append(f"| {idx} | `{item.get('target_level')}` | `{item.get('method')}` |")
    else:
        lines.append("| 0 | base chart only | no upgrade chain |")

    lines.extend(
        [
            "",
            "## Counts",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| L1/5m kuozhan centers | {payload.get('l1_zhongshu_count', 0)} |",
            f"| Tongjibie alternating segments | {payload.get('tongjibie_segment_count', 0)} |",
            f"| Tongjibie candidate groups | {payload.get('candidate_count', 0)} |",
            f"| Selected non-overlapping groups | {payload.get('chosen_count', 0)} |",
            f"| L2/30m tongjibie centers | {payload.get('l2_zhongshu_count', 0)} |",
            f"| L2/30m signals | {len(payload.get('l2_signals') or [])} |",
            "",
            "## Selected 30m Groups",
            "",
            "| Group | Dirs | ZD | ZG | Start | End |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for group in payload.get("chosen_groups", []) or []:
        lines.append(
            "| {start}-{end} | `{dirs}` | {zd:.3f} | {zg:.3f} | `{start_time}` | `{end_time}` |".format(
                start=int(group.get("start", 0)),
                end=int(group.get("end", 0)),
                dirs=group.get("dirs", ""),
                zd=float(group.get("zd") or 0.0),
                zg=float(group.get("zg") or 0.0),
                start_time=group.get("start_time"),
                end_time=group.get("end_time"),
            )
        )

    lines.extend(
        [
            "",
            "## All 30m Candidates",
            "",
            "| Group | Dirs | ZD | ZG | Selected | Reason |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for group in payload.get("candidate_groups", []) or []:
        lines.append(
            "| {start}-{end} | `{dirs}` | {zd:.3f} | {zg:.3f} | {selected} | {reason} |".format(
                start=int(group.get("start", 0)),
                end=int(group.get("end", 0)),
                dirs=group.get("dirs", ""),
                zd=float(group.get("zd") or 0.0),
                zg=float(group.get("zg") or 0.0),
                selected=_pct_bool(bool(group.get("selected"))),
                reason=group.get("reason", ""),
            )
        )

    lines.extend(
        [
            "",
            "## L2/30m Centers",
            "",
            "| Group | ZD | ZG | DD | GG | Done | L1 Children | Third Signal Diagnostic |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for item in payload.get("l2_zhongshu", []) or []:
        diag = item.get("third_signal_diagnostic") or {}
        children = ",".join(str(x) for x in item.get("expanded_with_l1_indices") or [])
        group = item.get("group")
        group_text = "" if group is None else "-".join(str(x) for x in group)
        lines.append(
            "| `{group}` | {zd:.3f} | {zg:.3f} | {dd:.3f} | {gg:.3f} | {done} | `{children}` | {status} |".format(
                group=group_text,
                zd=float(item.get("zd") or 0.0),
                zg=float(item.get("zg") or 0.0),
                dd=float(item.get("dd") or 0.0),
                gg=float(item.get("gg") or 0.0),
                done=_pct_bool(bool(item.get("done"))),
                children=children,
                status=diag.get("status", ""),
            )
        )

    lines.extend(
        [
            "",
            "## L2/30m Signals",
            "",
            "| Level | Type | Anchor Time | Price |",
            "| ---: | --- | --- | ---: |",
        ]
    )
    for sig in payload.get("l2_signals", []) or []:
        lines.append(
            "| {level} | `{bs_type}` | `{anchor_time}` | {price:.3f} |".format(
                level=int(sig.get("level", 0)),
                bs_type=sig.get("bs_type", ""),
                anchor_time=sig.get("anchor_time", ""),
                price=float(sig.get("price") or 0.0),
            )
        )
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            "- The active 1m chain first builds L1/5m via `kuozhan`, which is the non-same-level extension/expansion path.",
            "- The 30m layer is built from alternating lower-level trend segments via `tongjibie`; selected groups are non-overlapping three-segment overlaps, so 30m is not treated as an extended lower-level center.",
            "- This audit is structural only; trading still uses walk-forward visibility and next-bar fills from the live backtest signal CSV.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default="us")
    parser.add_argument("--code", default="TSLA.US")
    parser.add_argument("--op-level", default="1m")
    parser.add_argument("--chart-cache", default="D:/chanlun_pro/chart_cache")
    parser.add_argument("--end", default="2026-06-10T20:00:00+00:00")
    parser.add_argument("--recursive-l0-min-zs-lines", type=int, default=3)
    parser.add_argument(
        "--out",
        default="D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.json",
    )
    parser.add_argument(
        "--md",
        default="D:/chanlun_pro/reports/tsla_tongjibie_candidate_audit.md",
    )
    args = parser.parse_args()

    payload = build_audit(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        md = Path(args.md)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render_markdown(payload), encoding="utf-8")
    print(f"wrote={out}")
    if args.md:
        print(f"markdown={args.md}")
    print(
        "l1={l1_zhongshu_count} segs={tongjibie_segment_count} "
        "candidates={candidate_count} chosen={chosen_count} l2={l2_zhongshu_count} "
        "signals={n}".format(**payload, n=len(payload["l2_signals"]))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
