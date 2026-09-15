"""Verify discovered stroke events on actual raw-bar prefixes and save case evidence."""

import argparse
from collections import Counter
import json
from pathlib import Path
from time import perf_counter

from review_stroke_real_markets_v12 import (
    ROOT,
    bars,
    bi_record,
    cold,
    file_record,
    graph_cut,
    point,
    save,
)
from review_stroke_refactor_v12 import signature

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process


def edge_key(bi):
    return (bi.start.k.k_index, bi.end.k.k_index, bi.start.val, bi.end.val)


def segment_key(xd):
    return (xd.start.k.k_index, xd.end.k.k_index, xd.start.val, xd.end.val)


def snapshot(calc):
    return {
        "strokes": [bi_record(x) for x in calc.bis],
        "construction": calc.construction_state(),
        "last_decision": vars(calc._resolver.last_decision)
        if calc._resolver.last_decision
        else None,
    }


def window_data(merged, raw, first, last):
    return {
        "merged": [
            {
                "index": k.index,
                "raw_center": k.k_index,
                "date": k.date,
                "high": k.h,
                "low": k.l,
                "open": k.o,
                "close": k.c,
                "raw_indices": [s.index for s in k.klines],
            }
            for k in merged.cl_klines
            if first <= k.index <= last
        ],
        "raw": [
            {
                "index": k.index,
                "date": k.date,
                "open": k.o,
                "high": k.h,
                "low": k.l,
                "close": k.c,
                "volume": k.a,
            }
            for k in raw
            if any(
                first <= c.index <= last and k.index in [s.index for s in c.klines]
                for c in merged.cl_klines[max(0, first) : last + 1]
            )
        ],
    }


def run(output):
    summaries = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    modes, events, cuts, cases = [], [], [], []
    for row in summaries:
        started = perf_counter()
        dataset = row["dataset"]
        record_path = output / (dataset + ".json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        frame = pd.read_parquet(row["path"])
        raw = bars(frame)
        merged, expected = cold(raw)
        live_merged, live = CL_Kline_Process(), BiCalculator()
        counts = sorted({*range(257, len(raw), 257), len(raw)})
        mode_counts = Counter()
        for count in counts:
            live_merged.process_cl_klines(raw[:count])
            live.calculate(
                live_merged.cl_klines,
                source_revision=live_merged.structure_revision,
                validated_incremental_prefix=True,
            )
            mode_counts[live.processing_mode] += 1
        assert signature(live) == signature(expected), dataset
        modes.append(
            {
                "dataset": dataset,
                "chunks": len(counts),
                "mode_counts": dict(mode_counts),
                "batch_incremental_equal": True,
            }
        )
        if expected.unresolved_regions:
            cut = graph_cut(expected, expected.unresolved_regions[0])
            record["first_boundary_graph_cut"] = cut
            cuts.append({"dataset": dataset, **cut})
            save(record_path, record)
        primary = [
            e
            for e in record["trace"]["retractions"]
            if any(x["was_primary"] for x in e["removed_continued"])
        ]
        code, frequency = Path(row["path"]).stem.rsplit("_", 1)
        config = {
            "price_basis_revision": "recorded-raw",
            "structure_price_quantum": "0.01",
        }
        for event in primary:
            witness = pd.Timestamp(event["witness"])
            count = int(frame.date.searchsorted(witness, side="right"))
            before_merged, before = cold(raw[: count - 1])
            after_merged, after = cold(raw[:count])
            before_done = {edge_key(x): bi_record(x) for x in before.bis if x.is_done()}
            after_keys = {edge_key(x) for x in after.bis}
            removed = [v for k, v in before_done.items() if k not in after_keys]
            actual = {
                "dataset": dataset,
                "raw_count": count,
                "witness": witness,
                "discovery_endpoint": event["endpoint"],
                "verified_primary_retraction": bool(removed),
                "removed": removed,
                "before_primary_count": len(before.confirmed_bis),
                "after_primary_count": len(after.confirmed_bis),
            }
            cl = CL(
                code, frequency, config, market="us" if code.endswith(".US") else "a"
            )
            cl.process_klines_batch(frame.iloc[: count - 1])
            old_segments = {
                segment_key(x): x.to_dict() for x in cl.get_xds() if x.is_done()
            }
            cl.process_klines(frame.iloc[:count])
            assert signature(cl.bi_calculator) == signature(after)
            current_segments = {segment_key(x) for x in cl.get_xds()}
            actual["removed_locked_segments"] = [
                v for k, v in old_segments.items() if k not in current_segments
            ]
            events.append(actual)
            selected = (
                dataset == "recorded_SH.600088_30m" and event == primary[0]
            ) or (dataset == "recorded_SH.600519_5m" and event == primary[0])
            if selected:
                center_first = min(x["center"] for x in event["before_tail"]) - 2
                center_last = event["after_tail"][-1]["center"] + 2
                # Use only raw information available at this case's observation time.
                evidence = window_data(
                    after_merged, raw[:count], center_first, center_last
                )
                stage_cut = int(
                    frame.date.searchsorted(
                        pd.Timestamp(event["branch"]["visible_at"]), side="right"
                    )
                )
                _, middle = cold(raw[:stage_cut])
                # Replay every actual bar from the beginning through the selected case.
                lm, lc = CL_Kline_Process(), BiCalculator()
                comparisons = 0
                for n in range(1, count + 1):
                    lm.process_cl_klines(raw[:n])
                    lc.calculate(
                        lm.cl_klines,
                        source_revision=lm.structure_revision,
                        validated_incremental_prefix=True,
                    )
                    _, reference = cold(raw[:n])
                    assert signature(lc) == signature(reference)
                    comparisons += 1
                item = {
                    "dataset": dataset,
                    "input": file_record(row["path"]),
                    "raw_count": count,
                    "stage_raw_count": stage_cut,
                    "discovery": event,
                    "verified": actual,
                    "before": snapshot(before),
                    "middle": snapshot(middle),
                    "after": snapshot(after),
                    "raw_prefix_cold_live_comparisons": comparisons,
                    **evidence,
                }
                cases.append(item)
                save(output / "cases" / (dataset + "_retraction.json"), item)
        print(
            f"verified {dataset}: {len(counts)} chunks, {len(primary)} primary events, "
            f"{perf_counter() - started:.2f}s",
            flush=True,
        )
        save(
            output / "verification.json",
            {"modes": modes, "primary_events": events, "prefix_cuts": cuts},
        )
    return modes, events, cuts, cases


def scope_case(output):
    """Quantify historical-window dependence; shorter histories are explicit batch inputs."""
    summary = json.loads(
        (output / "recorded_SH.600189_5m.json").read_text(encoding="utf-8")
    )["summary"]
    frame = pd.read_parquet(summary["path"])
    raw = bars(frame)
    merged, calc = cold(raw)
    boundary = calc.unresolved_regions[0]
    seq = calc._resolver.sequence
    observed = calc._resolver.observed_at(boundary.observed_by)
    count = int(frame.date.searchsorted(pd.Timestamp(observed), side="right"))
    _, before = cold(raw[: count - 1])
    am, after = cold(raw[:count])
    windows = []
    for start in (0, 1, 2, 3, 10, 30, 48, 60, 100, len(frame) - 1000):
        cl = CL(
            "SH.600189",
            "5m",
            {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"},
            market="a",
        )
        cl.process_klines_batch(frame.iloc[start:])
        c = cl.bi_calculator
        windows.append(
            {
                "raw_start": start,
                "start_time": frame.date.iloc[start],
                "bars": len(frame) - start,
                "candidates": len(c.bis),
                "components": list(map(len, c.stroke_components)),
                "first_contiguous": len(c.contiguous_bis),
                "segments": len(cl.get_xds()),
                "last_contiguous_time": c.contiguous_bis[-1].end.k.date
                if c.contiguous_bis
                else None,
            }
        )
    points = [point(f) for f in seq.points[: boundary.observed_by + 1]]
    item = {
        "dataset": "recorded_SH.600189_5m",
        "input": file_record(summary["path"]),
        "raw_count": count,
        "witness": observed,
        "before": snapshot(before),
        "after": snapshot(after),
        "first_boundary_graph_cut": graph_cut(calc, boundary),
        "windows": windows,
        "points": points,
        **window_data(am, raw[:count], 0, 19),
    }
    save(output / "cases/recorded_SH.600189_5m_boundary.json", item)
    print("verified window-dependence case", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/bi_code_review/v12_real_market_audit",
    )
    args = parser.parse_args()
    run(args.output)
    scope_case(args.output)
    manifest = json.loads((args.output / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["core_files"]:
        assert file_record(item["path"]) == item
    manifest["scripts"] = [
        file_record(Path(__file__)),
        file_record(ROOT / "script/review_stroke_real_markets_v12.py"),
    ]
    manifest.pop("script", None)
    manifest["verification"] = file_record(args.output / "verification.json")
    save(args.output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
