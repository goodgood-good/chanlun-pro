"""Verify v13 changes against frozen v12 market evidence and raw prefixes.

All Chan sources are actually read from --source-root. Local validity checks
are independent necessary predicates; neither this script nor its output is
a proof of the author's unique final whole-chart partition.
"""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from review_stroke_architecture_v11 import read_originals, file_record
from review_stroke_real_markets_v12 import bars, cold, save, trace_frozen_fractals
from review_stroke_refactor_v12 import signature, check_local
from verify_stroke_real_market_cases_v12 import edge_key, segment_key


def runtime(frame, code, frequency):
    return CL(
        code,
        frequency,
        {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"},
        market="a",
    ).process_klines_batch(frame)


def xd_signature(values):
    return [
        (
            segment_key(x),
            x.locked_at,
            x.formed_at,
            x.forming,
            getattr(x, "selection_pending", False),
            getattr(x, "observation_scope_id", None),
        )
        for x in values
    ]


def initial_consensus(raw, actual):
    variants = []
    for direction in ("up", "down"):
        merged, calc = CL_Kline_Process(_initial_direction=direction), BiCalculator()
        merged.process_cl_klines(raw)
        calc.calculate_batch(merged.cl_klines)
        variants.append(calc)

    def physical(fx):
        return fx.k.k_index, fx.type, fx.val

    common = {physical(f) for f in variants[0].fxs} & {
        physical(f) for f in variants[1].fxs
    }
    missed = common - {physical(f) for f in actual.fxs}
    if missed:
        raise AssertionError(f"agreed physical fractals missing: {missed}")
    return {
        "common_fractals": len(common),
        "missed_agreed_fractals": len(missed),
        "up_down_strokes_equal": [edge_key(b) for b in variants[0].bis]
        == [edge_key(b) for b in variants[1].bis],
    }


def verify_datasets(baseline, output):
    rows = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
    summaries = []
    for row in rows:
        started = perf_counter()
        path = Path(row["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        code, frequency = path.stem.rsplit("_", 1)
        frame = pd.read_parquet(path)
        expected = runtime(frame, code, frequency)
        calc = expected.bi_calculator
        check_local(calc)
        assert not calc.audit_endpoint_ranges()
        assert not calc.audit_endpoint_adjacency()
        raw = bars(frame)
        merged, live = CL_Kline_Process(), BiCalculator()
        cuts = sorted({*range(257, len(raw), 257), len(raw)})
        for count in cuts:
            merged.process_cl_klines(raw[:count])
            live.calculate(
                merged.cl_klines,
                source_revision=merged.structure_revision,
                validated_incremental_prefix=True,
            )
        assert signature(live) == signature(calc), row["dataset"]
        assert live._resolver.rejected_paths == calc._resolver.rejected_paths
        # Full CL also covers per-scope segments and isolated center projections.
        streamed = CL(
            code,
            frequency,
            {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"},
            market="a",
        )
        cl_cuts = sorted({*range(1021, len(frame), 1021), len(frame)})
        for count in cl_cuts:
            streamed.process_klines(frame.head(count))
        assert signature(streamed.bi_calculator) == signature(calc), row["dataset"]
        assert xd_signature(streamed.get_chart_xds()) == xd_signature(
            expected.get_chart_xds()
        )
        observed_centers = expected.get_conditional_centers()
        assert [(o.scope_id, r) for o, r in streamed.get_conditional_centers()] == [
            (o.scope_id, r) for o, r in observed_centers
        ]
        trace = trace_frozen_fractals(calc)
        removed_primary = [
            e
            for e in trace["retractions"]
            if any(x["was_primary"] for x in e["removed_continued"])
        ]
        assert not removed_primary, (row["dataset"], removed_primary)
        consensus = (
            initial_consensus(raw, calc)
            if expected.cl_kline_processor.initial_context_unresolved
            else None
        )
        summary = {
            "dataset": row["dataset"],
            "path": str(path),
            "sha256": row["sha256"],
            "bars": len(frame),
            "before": {
                key: row[key]
                for key in (
                    "candidates",
                    "component_sizes",
                    "first_contiguous",
                    "primary_done",
                    "segments",
                    "locked_segments",
                )
            },
            "after": {
                "candidates": len(calc.bis),
                "component_sizes": [len(c) for c in calc.stroke_components],
                "first_contiguous": len(calc.contiguous_bis),
                "primary_done": len(calc.confirmed_bis),
                "formal_segments": len(expected.get_xds()),
                "chart_segments": len(expected.get_chart_xds()),
                "conditional_centers": sum(len(r.centers) for _, r in observed_centers),
                "rejected_path_events": len(calc._resolver.rejected_paths),
            },
            "bi_incremental_calls": len(cuts),
            "cl_incremental_calls": len(cl_cuts),
            "batch_incremental_equal": True,
            "initial_consensus": consensus,
            "frozen_trace_primary_retractions": len(removed_primary),
            "frozen_trace_all_continuation_retractions": len(trace["retractions"]),
            "necessary_predicates_passed": True,
            "seconds": round(perf_counter() - started, 3),
        }
        save(
            output / (row["dataset"] + ".json"),
            {
                "summary": summary,
                "selection": signature(calc),
                "rejected_paths": [asdict(d) for d in calc._resolver.rejected_paths],
                "frozen_fractal_trace": trace,
            },
        )
        summaries.append(summary)
        save(output / "summary.json", summaries)
        print(
            f"verified {row['dataset']}: {len(calc.bis)} pens, {len(expected.get_chart_xds())} chart segments, {len(calc.stroke_components)} scopes",
            flush=True,
        )
    return rows, summaries


def verify_prior_events(baseline, output, datasets):
    previous = json.loads((baseline / "verification.json").read_text(encoding="utf-8"))
    paths = {r["dataset"]: Path(r["path"]) for r in datasets}
    frames, results = {}, []
    for event in previous["primary_events"]:
        dataset, count = event["dataset"], event["raw_count"]
        if dataset not in frames:
            frames[dataset] = pd.read_parquet(paths[dataset])
        frame = frames[dataset].head(count)
        raw = bars(frame)
        _, before = cold(raw[:-1])
        _, after = cold(raw)
        # A v12 stroke need not have existed as a confirmed v13 stroke earlier.
        # Measure actual v13 prefix transitions instead of claiming it survived.
        before_done = {edge_key(b) for b in before.confirmed_bis}
        removed = before_done - {edge_key(b) for b in after.bis}
        assert not removed, (dataset, count, removed)
        code, frequency = paths[dataset].stem.rsplit("_", 1)
        live = runtime(frame.iloc[:-1], code, frequency)
        before_segments = {segment_key(x) for x in live.get_xds() if x.is_done()}
        live.process_klines(frame.iloc[-1:])
        assert signature(live.bi_calculator) == signature(after)
        removed_segments = before_segments - {segment_key(x) for x in live.get_xds()}
        assert not removed_segments
        results.append(
            {
                "dataset": dataset,
                "raw_count": count,
                "before_current_done_pens": len(before_done),
                "removed_current_done_pens": len(removed),
                "before_current_done_segments": len(before_segments),
                "removed_current_done_segments": len(removed_segments),
                "batch_incremental_equal": True,
            }
        )
    save(output / "prior_event_replay.json", results)
    print(f"verified all {len(results)} prior raw-prefix events", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output/bi_code_review/v13_validation"
    )
    args = parser.parse_args()
    sources = read_originals(args.source_root)
    save(args.output / "source_evidence.json", sources)
    baseline = ROOT / "output/bi_code_review/v12_real_market_audit"
    rows, summaries = verify_datasets(baseline, args.output)
    events = verify_prior_events(baseline, args.output, rows)
    save(
        args.output / "verification.json",
        {
            "profile": STRICT_STROKE_MODE,
            "source_root": str(args.source_root.resolve()),
            "source_files": [
                {k: s[k] for k in ("path", "sha256", "kind", "line_ranges")}
                for s in sources
            ],
            "datasets": len(summaries),
            "primary_datasets": sum(
                s["dataset"].startswith("recorded_") for s in summaries
            ),
            "bars": sum(s["bars"] for s in summaries),
            "bi_incremental_calls": sum(s["bi_incremental_calls"] for s in summaries),
            "cl_incremental_calls": sum(s["cl_incremental_calls"] for s in summaries),
            "previous_events_replayed": len(events),
            "all_checks_passed": True,
            "limits": "Necessary predicates and implementation regressions only; conditional ranges remain unresolved; no global uniqueness or permanent completion proof.",
            "production_files": [
                file_record(p) for p in (ROOT / "src/chanlun/core").rglob("*.py")
            ],
        },
    )


if __name__ == "__main__":
    main()
