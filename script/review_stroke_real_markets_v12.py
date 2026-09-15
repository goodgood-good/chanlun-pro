"""Audit recorded market bars against current old-stroke implementation.

Read-only with respect to production code, market files, caches and services.
Chan text comes exclusively from the explicitly supplied source directory.
The independent checks are necessary conditions, not a whole-chart oracle.
"""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator, fractal_lock_witness
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.stroke_resolver import StrokeResolver
from chanlun.core.types import Kline
from review_stroke_architecture_v11 import file_record, read_originals
from review_stroke_refactor_v12 import check_local, signature


SOURCE_FRAMES = ROOT / "output/playwright/center_coverage/deep_review/source_frames"
SYMBOLS = ("SH.600088", "SH.600189", "SH.600519", "SZ.000001", "SZ.002299", "SH.601059")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def bars(frame):
    return [
        Kline(
            index=i,
            date=row.date.to_pydatetime(),
            h=float(row.high),
            l=float(row.low),
            o=float(row.open),
            c=float(row.close),
            a=float(row.volume),
        )
        for i, row in enumerate(frame.itertuples(index=False))
    ]


def cold(raw):
    merged, calc = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(raw)
    calc.calculate_batch(merged.cl_klines)
    return merged, calc


def point(fx):
    return {
        "fx": fx.index,
        "center": fx.k.index,
        "raw_center": fx.k.k_index,
        "date": fx.k.date,
        "kind": fx.type,
        "value": fx.val,
        "high": fx.k.h,
        "low": fx.k.l,
        "visible_at": fractal_lock_witness(fx),
    }


def bi_record(bi):
    return {
        "start": point(bi.start),
        "end": point(bi.end),
        "component": bi.component_index,
        "selected_at": bi.selected_at,
        "continuation_at": bi.continuation_at,
        "locked_at": bi.locked_at,
        "is_done": bi.is_done(),
        "selection_pending": bi.selection_pending,
        "completion_status": bi.completion_status,
    }


def quality(frame, frequency):
    dates = frame.date.dt.tz_convert("Asia/Shanghai")
    delta = dates.diff().dt.total_seconds() / 60
    same_session = dates.dt.date.eq(dates.shift(1).dt.date) & dates.dt.hour.lt(12).eq(
        dates.shift(1).dt.hour.lt(12)
    )
    values = frame[["open", "high", "low", "close", "volume"]]
    return {
        "bars": len(frame),
        "start": dates.iloc[0],
        "end": dates.iloc[-1],
        "duplicate_times": int(frame.date.duplicated().sum()),
        "monotonic": bool(frame.date.is_monotonic_increasing),
        "missing_values": int(values.isna().sum().sum()),
        "invalid_ohlc": int(
            (
                (frame.high < frame[["open", "close", "low"]].max(axis=1))
                | (frame.low > frame[["open", "close", "high"]].min(axis=1))
            ).sum()
        ),
        "same_session_deltas_minutes": delta[same_session].value_counts().to_dict(),
        "same_session_nonstandard_deltas": int(
            (delta[same_session] != int(frequency[:-1])).sum()
        ),
        "calendar_days_with_data": int(dates.dt.date.nunique()),
    }


def trace_frozen_fractals(calc):
    """Discovery trace on final merged geometry; actual raw prefixes verified separately."""
    resolver = StrokeResolver()
    events, boundary_events, actions = [], [], []
    action_counts = Counter()
    for fx in calc.fxs:
        old_active = resolver.path()
        old_edges = resolver.edges()
        old_continued = {(x.start, x.end): x for x in resolver.continuations}
        old_primary = {(x.start, x.end) for x in resolver.completions}
        old_boundaries = resolver.boundaries
        resolver.advance(fx, calc._check_stroke_validity, fractal_lock_witness(fx))
        decision = resolver.last_decision
        actions.append(decision.action)
        action_counts[decision.action] += 1
        new_edges = set(resolver.edges())
        removed = [
            edge
            for edge in old_edges
            if edge in old_continued and edge not in new_edges
        ]
        if removed:
            stable = 0
            for a, b in zip(old_active, resolver.path()):
                if a != b:
                    break
                stable += 1
            first = max(0, stable - 2)
            new_path = resolver.path()
            branch = new_path[stable] if stable < len(new_path) else None
            events.append(
                {
                    "endpoint": fx.index,
                    "witness": fractal_lock_witness(fx),
                    "decision": asdict(decision),
                    "old_active_length": len(old_active),
                    "stable_active_nodes": stable,
                    "before_tail": [point(calc.fxs[x]) for x in old_active[first:]],
                    "after_tail": [point(calc.fxs[x]) for x in new_path[first:]],
                    "removed_continued": [
                        {
                            "start": point(calc.fxs[a]),
                            "end": point(calc.fxs[b]),
                            "witness": old_continued[a, b].witnessed_at,
                            "was_primary": (a, b) in old_primary,
                        }
                        for a, b in removed
                    ],
                    "branch_initial_action": actions[branch]
                    if branch is not None
                    else None,
                    "branch": point(calc.fxs[branch]) if branch is not None else None,
                    "active_origin_same": bool(
                        old_active and new_path and old_active[0] == new_path[0]
                    ),
                }
            )
        if resolver.boundaries != old_boundaries:
            boundary_events.append(
                {
                    "endpoint": point(fx),
                    "action": decision.action,
                    "before_count": len(old_boundaries),
                    "after_count": len(resolver.boundaries),
                    "before_tail": [point(calc.fxs[x]) for x in old_active[-5:]],
                    "after_tail": [point(calc.fxs[x]) for x in resolver.path()[:5]],
                }
            )
    assert resolver.edges() == calc._resolver.edges()
    assert resolver.qualifications == calc._resolver.qualifications
    return {
        "actions": dict(action_counts),
        "retractions": events,
        "boundary_events": boundary_events,
    }


def graph_cut(calc, boundary):
    """Direct crossings plus paths that preserve all but the last selected endpoint."""
    seq = calc._resolver.sequence
    crossing = []
    for end in range(boundary.right, len(seq.points)):
        for start in seq.relations[end].eligible:
            if start <= boundary.left:
                crossing.append(
                    {
                        "start": point(seq.points[start]),
                        "end": point(seq.points[end]),
                        "adjacent": start in seq.relations[end].adjacent,
                    }
                )
    path = calc._resolver.components()[0]
    anchor = path[-2]
    reachable = {anchor}
    predecessors = {}
    for end in range(anchor + 1, len(seq.points)):
        for start in seq.relations[end].eligible:
            if start in reachable:
                reachable.add(end)
                predecessors[end] = start
                break
    return {
        "left": point(seq.points[boundary.left]),
        "right": point(seq.points[boundary.right]),
        "direct_crossing_count": len(crossing),
        "direct_crossings": crossing,
        "prefix_anchor": point(seq.points[anchor]),
        "prefix_reachable_nodes": [point(seq.points[x]) for x in sorted(reachable)],
        "prefix_live_reachable_nodes": [
            point(seq.points[x]) for x in sorted(reachable & seq.live_starts())
        ],
        "prefix_can_ever_resume_by_append_only": bool(reachable & seq.live_starts()),
    }


def process(path, label, output):
    started = perf_counter()
    frame = pd.read_parquet(path)
    code, frequency = path.stem.rsplit("_", 1)
    raw = bars(frame)
    merged, calc = cold(raw)
    check_local(calc)
    assert not calc.audit_endpoint_ranges()
    assert not calc.audit_endpoint_adjacency()
    discovery = trace_frozen_fractals(calc)
    config = {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"}
    cl = CL(code, frequency, config, market="us" if code.endswith(".US") else "a")
    cl.process_klines_batch(frame)
    assert signature(cl.bi_calculator) == signature(calc)
    first = calc.contiguous_bis
    summary = {
        "dataset": label,
        **file_record(path),
        "quality": quality(frame, frequency),
        "merged_bars": len(merged.cl_klines),
        "fractals": len(calc.fxs),
        "candidates": len(calc.bis),
        "component_sizes": list(map(len, calc.stroke_components)),
        "boundaries": len(calc.unresolved_regions),
        "first_contiguous": len(first),
        "primary_done": len(calc.confirmed_bis),
        "segments": len(cl.get_xds()),
        "locked_segments": sum(x.is_done() for x in cl.get_xds()),
        "first_contiguous_end": point(first[-1].end) if first else None,
        "last_candidate_end": point(calc.bis[-1].end) if calc.bis else None,
        "local_continued_retraction_events": len(discovery["retractions"]),
        "primary_retraction_events": sum(
            any(x["was_primary"] for x in e["removed_continued"])
            for e in discovery["retractions"]
        ),
        "active_bypass_events": sum(
            e["branch_initial_action"] == "retain_qualified_reversal"
            and e["active_origin_same"]
            for e in discovery["retractions"]
        ),
        "initial_excluded_raw_count": merged.initial_excluded_raw_count,
        "local_condition_violations": 0,
        "seconds": round(perf_counter() - started, 3),
    }
    save(
        output / (label + ".json"),
        {
            "summary": summary,
            "strokes": list(map(bi_record, calc.bis)),
            "construction": calc.construction_state(),
            "trace_kind": "final_merged_fractals_discovery_only",
            "trace": discovery,
            "first_boundary_graph_cut": graph_cut(calc, calc.unresolved_regions[0])
            if calc.unresolved_regions
            else None,
        },
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "dataset",
                    "candidates",
                    "boundaries",
                    "first_contiguous",
                    "active_bypass_events",
                    "primary_retraction_events",
                    "seconds",
                )
            }
        ),
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/bi_code_review/v12_real_market_audit",
    )
    parser.add_argument("--datasets", nargs="*")
    args = parser.parse_args()
    sources = read_originals(args.source_root)
    core_paths = sorted((ROOT / "src/chanlun/core").rglob("*.py"))
    before = list(map(file_record, core_paths))
    selected = [
        (SOURCE_FRAMES / f"{code}_{freq}.parquet", f"recorded_{code}_{freq}")
        for code in SYMBOLS
        for freq in ("1m", "5m", "30m")
    ]
    selected += [
        (p, f"fixture_{p.stem}")
        for p in sorted((ROOT / "tests/fixtures").glob("*.parquet"))
    ]
    summaries = []
    for path, label in selected:
        if args.datasets and label not in args.datasets:
            continue
        summaries.append(process(path, label, args.output))
        save(args.output / "summary.json", summaries)
    after = list(map(file_record, core_paths))
    assert before == after
    save(
        args.output / "manifest.json",
        {
            "created_at": datetime.now(timezone.utc),
            "sources": sources,
            "core_files": before,
            "core_unchanged": True,
            "script": file_record(Path(__file__)),
            "dataset_count": len(summaries),
            "raw_bars": sum(x["quality"]["bars"] for x in summaries),
        },
    )


if __name__ == "__main__":
    main()
