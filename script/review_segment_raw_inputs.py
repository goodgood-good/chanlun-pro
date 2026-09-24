"""Rebuild every frozen native segment from saved or reacquired original OHLC.

The calculation key binds each reacquired frame to the completed screening's
input fingerprint and source revision. A revised/unavailable input is recorded
as such; it is never quietly treated as the original cached input.
"""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pandas as pd

from chanlun.core.cl import CL
from chanlun.screening.cache import input_fingerprint
from chanlun.screening.rules import CN, trading_context, frame_quality
from chanlun.screening.markets import market_context
from chanlun.screening.runner import fetch_frame
from script.audit_all_segment_results import OUT, DATA, read_json, write_json, implementation
from script.review_segment_geometry import geometry, ticks
from script.validate_segment_corpus import check_structure, fresh_matches


def raw_geometry(line, quantum):
    return (int(line.start.k.date.timestamp()), ticks(line.start.val, quantum),
            int(line.end.k.date.timestamp()), ticks(line.end.val, quantum), line.done)


def context_for(dataset, request):
    settings, observed = request["settings"], datetime.fromisoformat(request["observed_at"])
    if dataset["market"] == "a":
        main = trading_context(observed, "5m", settings["recent_sessions"], settings["max_anchor_sessions"])
        if dataset["frequency"] == "5m":
            return main
        return trading_context(datetime.fromtimestamp(main["cutoff"], CN), dataset["frequency"],
                               settings["recent_sessions"], settings["max_anchor_sessions"])
    return market_context(dataset["market"], dataset["code"], observed, dataset["frequency"],
                          settings["recent_sessions"], settings["max_anchor_sessions"], completed_session=True)


def review_raw(dataset, request, source_revision, output, hashes):
    started = perf_counter()
    output = Path(output)
    result = {"id": dataset["id"], "code": dataset["code"], "market": dataset["market"],
              "frequency": dataset["frequency"], "implementation_sha256": hashes,
              "status": "unreviewed", "segment_checks": [], "errors": []}
    try:
        saved = json.loads(gzip.decompress(Path(dataset["file"]).read_bytes()))
        snap, chart = saved["snapshot"], saved["chart"]
        target = output / "raw_inputs" / (dataset["id"] + ".parquet")
        context = context_for(dataset, request)
        if target.exists():
            frame = pd.read_parquet(target)
            result["input_origin"] = "frozen_raw_input"
        elif dataset.get("saved_raw_file"):
            path = Path(dataset["saved_raw_file"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != dataset["saved_raw_sha256"]:
                raise ValueError("Saved screening evidence changed after inventory freeze")
            frame = pd.read_parquet(path)
            result["input_origin"] = "completed_screening_raw_evidence"
        else:
            frame = fetch_frame(dataset["code"], dataset["frequency"], {**context, "symbol": dataset["code"]})
            result["input_origin"] = "configured_provider_reacquisition"
        if frame is None or frame.empty:
            raise ValueError("Original OHLC could not be obtained")
        fingerprint = input_fingerprint(frame, dataset["code"], dataset["frequency"])
        key = hashlib.sha256(f"{fingerprint}:{source_revision}".encode()).hexdigest()
        result.update(input_fingerprint=fingerprint, input_key_match=key == dataset["calculation_key"],
                      bars=len(frame), source_started_at=int(frame.date.iloc[0].timestamp()),
                      source_closed_at=int(frame.date.iloc[-1].timestamp()), quality=frame_quality(frame, context))
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(target, index=False)
        result.update(raw_file=str(target), raw_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        # Reproduce the ACTUAL screening entrypoint first. It currently calls
        # process_klines(frame), whose default is last_bar_closed=False.
        cl = CL(dataset["code"], dataset["frequency"], {}, market=dataset["market"])
        cl.process_klines(frame, last_bar_closed=False)
        closed = CL(dataset["code"], dataset["frequency"], {}, market=dataset["market"])
        closed.process_klines(frame, last_bar_closed=True)
        values = cl.get_contiguous_bis()
        check_structure(cl.xd_calculator, values)
        fresh_matches(cl.xd_calculator, values)
        quantum = str(snap["structure_price_quantum"])
        actual_pens = [(int(b.start.k.date.timestamp()), ticks(b.start.val, quantum), int(b.end.k.date.timestamp()),
                        ticks(b.end.val, quantum), b.locked_at is not None and not getattr(b, "selection_pending", False)) for b in values]
        expected_pens = [geometry(b, quantum) for b in chart["bis"] if b.get("component_index", 0) == 0]
        result["saved_pens_match"] = actual_pens == expected_pens
        actual = {raw_geometry(x, quantum): x for x in cl.get_xds()}
        closed_by_geometry = {raw_geometry(x, quantum)[:4]: x for x in closed.get_xds()}
        result["closed_input_segments"] = [{"geometry": raw_geometry(x, quantum),
                                            "confirmed_at": int(x.locked_at.timestamp()) if x.locked_at else None}
                                           for x in closed.get_xds()]
        unit_by_key = {(u["start_time"], u["start_tick"], u["end_time"], u["end_tick"]): u for u in snap.get("screening_segments", [])}
        result["rebuilt_segments"] = len(actual)
        for number, row in enumerate(chart["xds"], 1):
            key = geometry(row, quantum)
            line = actual.get(key)
            check = {"segment_id": f"{dataset['id']}:S{number:04}", "geometry_match": line is not None}
            unit = unit_by_key.get(key[:4])
            if line is not None:
                actual_time = int(line.locked_at.timestamp()) if line.locked_at is not None else None
                expected_time = unit.get("confirmed_at") if unit else None
                check.update(rebuilt_confirmed_at=actual_time, saved_confirmed_at=expected_time,
                             confirmation_time_match=actual_time == expected_time,
                             witness_pen=line.construction_evidence.witness_index if line.construction_evidence else None)
            closed_line = closed_by_geometry.get(key[:4])
            check["closed_input_geometry_match"] = closed_line is not None
            if closed_line is not None:
                when = int(closed_line.locked_at.timestamp()) if closed_line.locked_at else None
                old_when = unit.get("confirmed_at") if unit else None
                check.update(closed_input_confirmed_at=when, closed_input_confirmed=closed_line.done,
                             closed_input_state_changed=closed_line.done != key[4],
                             closed_input_delay_seconds=old_when - when if when is not None and old_when is not None else None)
            result["segment_checks"].append(check)
        result["geometry_mismatches"] = sum(not c["geometry_match"] for c in result["segment_checks"])
        result["clock_mismatches"] = sum(c.get("confirmation_time_match") is False for c in result["segment_checks"])
        result["closed_clock_differences"] = sum(c.get("closed_input_delay_seconds") not in (None, 0) for c in result["segment_checks"])
        result["closed_state_changes"] = sum(c.get("closed_input_state_changed", False) for c in result["segment_checks"])
        result["closed_geometry_changes"] = sum(not c["closed_input_geometry_match"] for c in result["segment_checks"])
        result["clock_contract"] = {"cached_entrypoint": "process_klines(frame): default false",
                                    "closed_data_comparison": "process_klines(frame, last_bar_closed=True)",
                                    "comparison_is_deployed_change": False}
        result["pen_confirmation_times"] = [{"index": b.index, "confirmed_at": int(b.locked_at.timestamp()) if b.locked_at else None,
                                              "selection_pending": getattr(b, "selection_pending", False)} for b in values]
        if not result["input_key_match"]:
            result["status"] = "input_revision_or_metadata_difference"
        elif not result["saved_pens_match"] or result["geometry_mismatches"] or result["clock_mismatches"] or len(actual) != len(chart["xds"]):
            result["status"] = "rebuild_mismatch"
        else:
            result["status"] = "matched_original_input_and_segments"
    except Exception as exc:
        result.update(status="raw_review_failed", errors=[repr(exc)])
    result["seconds"] = perf_counter() - started
    write_json(output / "raw_reviews" / (dataset["id"] + ".json.gz"), result)
    return {k: v for k, v in result.items() if k not in {"segment_checks", "pen_confirmation_times", "closed_input_segments"}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=OUT)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    inventory = read_json(args.output / "inventory.json")
    revision = read_json(DATA / "screening" / inventory["reference_run"] / "status.json")["source_revision"]
    hashes = {**implementation(), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
              ["src/chanlun/core/cl.py", "src/chanlun/cl_utils/strict_chart_runtime.py", "src/chanlun/screening/runner.py"]}}
    results, pending = [], []
    for dataset in inventory["datasets"][:args.limit]:
        target = args.output / "raw_reviews" / (dataset["id"] + ".json.gz")
        if args.resume and target.exists():
            prior = json.loads(gzip.decompress(target.read_bytes()))
            if prior["implementation_sha256"] == hashes and prior["status"] == "matched_original_input_and_segments":
                results.append({k: v for k, v in prior.items() if k not in {"segment_checks", "pen_confirmation_times", "closed_input_segments"}})
                continue
        pending.append(dataset)
    print(json.dumps({"phase": "raw_rebuild", "pending": len(pending), "reused_review_records": len(results)}), flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(review_raw, d, inventory["request"], revision, args.output, hashes) for d in pending]
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 100 == 0 or len(results) == len(inventory["datasets"][:args.limit]):
                print(json.dumps({"done": len(results), "states": dict(Counter(r["status"] for r in results)),
                                  "bars": sum(r.get("bars", 0) for r in results)}), flush=True)
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
    report = {"implementation_sha256": hashes, "datasets": results, "completed": len(results),
              "states": dict(Counter(r["status"] for r in results)), "bars": sum(r.get("bars", 0) for r in results),
              "geometry_mismatches": sum(r.get("geometry_mismatches", 0) for r in results),
              "clock_mismatches": sum(r.get("clock_mismatches", 0) for r in results),
              "closed_clock_differences": sum(r.get("closed_clock_differences", 0) for r in results),
              "closed_state_changes": sum(r.get("closed_state_changes", 0) for r in results),
              "closed_geometry_changes": sum(r.get("closed_geometry_changes", 0) for r in results)}
    write_json(args.output / ("raw_pilot.json" if args.limit else "raw_results.json"), report)
    print(json.dumps({k: v for k, v in report.items() if k != "datasets"}), flush=True)


if __name__ == "__main__":
    main()
