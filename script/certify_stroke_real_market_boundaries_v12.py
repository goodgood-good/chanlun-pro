"""Independent necessary-condition reachability certificates and downstream case data."""

from pathlib import Path
import json

import pandas as pd

from review_stroke_real_markets_v12 import ROOT, bars, cold, point, save
from verify_stroke_real_market_cases_v12 import snapshot, window_data

from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process


def initial_context_case(root):
    source = (
        ROOT
        / "output/playwright/center_coverage/deep_review/source_frames/SH.600189_5m.parquet"
    )
    frame = pd.read_parquet(source)
    variants = []
    for direction in ("up", "down", None):
        cl = CL(
            "SH.600189",
            "5m",
            {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"},
            market="a",
        )
        cl.cl_kline_processor = CL_Kline_Process(_initial_direction=direction)
        cl.process_klines(frame)
        calc = cl.bi_calculator
        variants.append(
            {
                "assumed_initial_direction": direction,
                "components": list(map(len, calc.stroke_components)),
                "segments": len(cl.get_xds()),
                "first_fractals": [point(x) for x in calc.fxs[:4]],
                "first_candles": [
                    {
                        "center": k.index,
                        "raw_center": k.k_index,
                        "date": k.date,
                        "high": k.h,
                        "low": k.l,
                        "source_indices": [s.index for s in k.klines],
                    }
                    for k in calc.cl_klines[:4]
                ],
                "physical_strokes": [
                    (
                        b.start.k.date,
                        b.start.val,
                        b.end.k.date,
                        b.end.val,
                        b.component_index,
                        b.is_done(),
                    )
                    for b in calc.bis
                ],
            }
        )
    assert variants[0]["physical_strokes"] == variants[1]["physical_strokes"]
    from review_stroke_real_markets_v12 import file_record

    save(
        root / "initial_context_case.json",
        {
            "dataset": "recorded_SH.600189_5m",
            "same_raw_input": file_record(source),
            "direction_assumptions_are_audit_only": True,
            "up_down_physical_strokes_identical": True,
            "variants": variants,
        },
    )


def certificate(calc):
    points, candles = calc.fxs, calc.cl_klines
    anchor = calc._resolver.components()[0][-2]
    reachable = {anchor}
    examined = {}
    while True:
        todo = sorted(reachable - set(examined))
        if not todo:
            break
        for start in todo:
            first = points[start]
            successors = []
            for end in range(start + 1, len(points)):
                second = points[end]
                if second.type == first.type or second.k.index - first.k.index < 4:
                    continue
                top, bottom = (
                    (first, second) if first.type == "ding" else (second, first)
                )
                interval = candles[first.k.index : second.k.index + 1]
                valid = (
                    top.k.h > bottom.k.h
                    and top.k.l > bottom.k.l
                    and max(k.h for k in interval) == top.val
                    and min(k.l for k in interval) == bottom.val
                )
                if valid:
                    successors.append(end)
            dominator = next(
                (
                    k
                    for k in candles[first.k.index + 1 :]
                    if (k.h > first.val if first.type == "ding" else k.l < first.val)
                ),
                None,
            )
            examined[start] = {
                "point": point(first),
                "valid_successors": successors,
                "first_price_invalidation": None
                if dominator is None
                else {
                    "center": dominator.index,
                    "date": dominator.date,
                    "high": dominator.h,
                    "low": dominator.l,
                },
            }
            reachable.update(successors)
    all_invalidated = all(
        v["first_price_invalidation"] is not None for v in examined.values()
    )
    return {
        "anchor": point(points[anchor]),
        "reachable": [examined[x] for x in sorted(examined)],
        "all_reachable_starts_price_invalidated": all_invalidated,
        "meaning": "With this selected prefix retained and historical bars unchanged, "
        "no appended future fractal can connect any reachable start under the checked local predicates.",
        "oracle_scope": "independent necessary pair predicates; no full-partition uniqueness claim",
    }


def main():
    root = ROOT / "output/bi_code_review/v12_real_market_audit"
    summaries = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    results = []
    for row in summaries:
        if not row["boundaries"]:
            continue
        frame = pd.read_parquet(row["path"])
        _, calc = cold(bars(frame))
        result = certificate(calc)
        assert result["all_reachable_starts_price_invalidated"]
        results.append({"dataset": row["dataset"], **result})
        print(
            f"certified {row['dataset']}: {len(result['reachable'])} reachable starts all invalidated",
            flush=True,
        )
    save(root / "boundary_certificates.json", results)
    verification = json.loads((root / "verification.json").read_text(encoding="utf-8"))
    by_dataset = {x["dataset"]: x for x in summaries}
    for event in verification["primary_events"]:
        if not event["removed_locked_segments"]:
            continue
        row = by_dataset[event["dataset"]]
        frame = pd.read_parquet(row["path"])
        count = event["raw_count"]
        raw = bars(frame.iloc[:count])
        _, before = cold(raw[:-1])
        merged, after = cold(raw)
        code, frequency = Path(row["path"]).stem.rsplit("_", 1)
        config = {
            "price_basis_revision": "recorded-raw",
            "structure_price_quantum": "0.01",
        }
        cl = CL(code, frequency, config, market="a")
        cl.process_klines_batch(frame.iloc[: count - 1])
        before_xds = [
            {
                "start": point(x.start),
                "end": point(x.end),
                "is_done": x.is_done(),
                "locked_at": x.locked_at,
            }
            for x in cl.get_xds()
        ]
        cl.process_klines(frame.iloc[:count])
        after_xds = [
            {
                "start": point(x.start),
                "end": point(x.end),
                "is_done": x.is_done(),
                "locked_at": x.locked_at,
            }
            for x in cl.get_xds()
        ]
        first = (
            min(x["start"]["k"]["index"] for x in event["removed_locked_segments"]) - 5
        )
        evidence = window_data(merged, raw, first, len(merged.cl_klines) - 1)
        save(
            root / "cases" / (event["dataset"] + "_segment_retraction.json"),
            {
                "dataset": event["dataset"],
                "raw_count": count,
                "event": event,
                "before": snapshot(before),
                "after": snapshot(after),
                "before_segments": before_xds,
                "after_segments": after_xds,
                **evidence,
            },
        )
    initial_context_case(root)


if __name__ == "__main__":
    main()
