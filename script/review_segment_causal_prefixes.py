"""Replay every saved segment immediately before and at its claimed close.

The raw input is authenticated against the exact saved calculation key. Replay
uses growing raw-candle prefixes; its clock never consumes a later candle.
This checks production causality, separately from the local-text geometry audit.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
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
from chanlun.cl_utils.price_metadata import strict_snapshot_price_metadata, strict_cl_config
from chanlun.screening.cache import input_fingerprint
from chanlun.screening.runner import fetch_frame
from chanlun.screening.markets import storage_symbol
from script.audit_all_segment_results import implementation, write_json
from script.review_segment_raw_inputs import context_for, raw_geometry
from script.review_segment_geometry import geometry


OUT = ROOT / "output/segment_reaudit_20260919"


def read(path):
    raw = Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)


def hashes():
    return {**implementation(), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in [
        "src/chanlun/core/cl.py", "src/chanlun/core/bi_calculator.py", "src/chanlun/core/stroke_resolver.py",
        "src/chanlun/cl_utils/strict_chart_runtime.py", "src/chanlun/screening/runner.py",
    ]}}


def obtain_frame(dataset, request, revision, output):
    attempts = []

    def matches(frame):
        fingerprint = input_fingerprint(frame, dataset["code"], dataset["frequency"])
        return hashlib.sha256(f"{fingerprint}:{revision}".encode()).hexdigest() == dataset["calculation_key"], fingerprint

    local = output / "raw_inputs" / f"{dataset['id']}.parquet"
    choices = [(local, None, "frozen_for_this_review")]
    for field, digest_field, label in [
        ("exact_prior_raw_file", "exact_prior_raw_sha256", "previous_frozen_input_with_exact_current_key"),
        ("saved_raw_file", "saved_raw_sha256", "saved_screening_evidence"),
    ]:
        if dataset.get(field):
            choices.append((Path(dataset[field]), dataset.get(digest_field), label))
    for path, digest, origin in choices:
        if not path.is_file():
            continue
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest and checksum != digest:
            raise ValueError(f"Original input checksum changed: {path}")
        data = pd.read_parquet(path)
        matched, fingerprint = matches(data)
        attempts.append({"file": str(path), "key_match": matched})
        if matched:
            return data, {"path": str(path), "sha256": checksum, "origin": origin, "fingerprint": fingerprint, "attempts": attempts}

    # Monitor cache windows can differ from the main screening's window. Bind
    # to an actual saved monitor input rather than replacing it with a new one.
    filename = f"{storage_symbol(dataset['code'], dataset['market'])}_{dataset['frequency']}.parquet"
    monitor = Path("D:/chanlun_pro/signal_monitor/runs")
    for path in sorted(monitor.glob(f"*/evidence/{filename}"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = pd.read_parquet(path)
        if int(data.date.iloc[-1].timestamp()) != dataset["source_closed_at"]:
            continue
        matched, fingerprint = matches(data)
        attempts.append({"file": str(path), "key_match": matched})
        if matched:
            return data, {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "origin": "saved_monitor_evidence", "fingerprint": fingerprint, "attempts": attempts}

    context = context_for(dataset, request)
    context.update(cutoff=dataset["source_closed_at"], symbol=dataset["code"])
    data = fetch_frame(dataset["code"], dataset["frequency"], context)
    if data is None or data.empty:
        raise ValueError("Original raw input unavailable")
    matched, fingerprint = matches(data)
    if not matched:
        raise ValueError(f"Reacquired raw input differs from the frozen calculation key ({len(attempts)} saved inputs checked)")
    local.parent.mkdir(parents=True, exist_ok=True)
    data.to_parquet(local, index=False)
    return data, {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
                  "origin": "provider_reacquired_and_key_matched", "fingerprint": fingerprint, "attempts": attempts}


def review(dataset, request, revision, output, implementation_sha256):
    started = perf_counter()
    output = Path(output)
    result = {"id": dataset["id"], "code": dataset["code"], "frequency": dataset["frequency"],
              "market": dataset["market"], "status": "unreviewed", "errors": [], "rows": [],
              "implementation_sha256": implementation_sha256}
    try:
        data, source = obtain_frame(dataset, request, revision, output)
        result.update(source=source, input_key_match=True, bars=len(data))
        saved = read(dataset["file"])
        metadata = strict_snapshot_price_metadata(data)
        quantum = str(metadata.structure_price_quantum)
        config = strict_cl_config(structure_price_quantum=metadata.structure_price_quantum, price_basis_revision=metadata.price_basis_revision)
        complete = CL(dataset["code"], dataset["frequency"], config, market=dataset["market"])
        complete.process_klines(data, last_bar_closed=True)
        final_lines = complete.get_xds()
        final = {raw_geometry(s, quantum)[:4]: s for s in final_lines}
        expected = [geometry(s, quantum) for s in saved["chart"]["xds"]]
        actual = [raw_geometry(s, quantum) for s in final_lines]
        result["full_rebuild_match"] = actual == expected
        if actual != expected:
            result["errors"].append({"kind": "full_rebuild_geometry_or_state_differs"})
        cached_units = {(u["start_time"], u["start_tick"], u["end_time"], u["end_tick"]): u
                        for u in saved["snapshot"].get("screening_segments", [])}
        events = defaultdict(list)
        for number, item in enumerate(expected, 1):
            key = item[:4]
            line, unit = final.get(key), cached_units.get(key)
            row = {"segment_id": f"{dataset['id']}:S{number:04d}", "number": number,
                   "geometry": key, "confirmed": item[4], "checks": {"rebuilt_geometry_and_state": line is not None and line.done == item[4]}}
            if line is not None:
                when = int(line.locked_at.timestamp()) if line.locked_at is not None else None
                row.update(confirmed_at=when, proof=asdict(line.construction_evidence) if line.construction_evidence else None)
                row["checks"]["complete_saved_clock_metadata"] = unit is not None and unit.get("confirmed_at") == when
                row["checks"]["pending_has_no_confirmation_time"] = item[4] or when is None
                if item[4] and when is not None:
                    events[when].append(row)
            result["rows"].append(row)
        values = complete.get_contiguous_bis()
        result["pens"] = [{"index": b.index, "start_time": int(b.start.k.date.timestamp()), "end_time": int(b.end.k.date.timestamp()),
                           "start_price": b.start.val, "end_price": b.end.val, "locked": b.is_done(),
                           "confirmed_at": int(b.locked_at.timestamp()) if b.locked_at is not None else None,
                           "selected_at": int(b.selected_at.timestamp()) if b.selected_at is not None else None}
                          for b in values]
        offset = complete._bar_close_offset(data)
        source_closes = [int((t + offset).timestamp()) for t in data.date]
        live = CL(dataset["code"], dataset["frequency"], config, market=dataset["market"])
        advanced, checkpoints, committed, observed_errors = 0, 0, {}, []

        def state():
            nonlocal checkpoints
            checkpoints += 1
            current = {raw_geometry(s, quantum)[:4]: int(s.locked_at.timestamp()) for s in live.get_xds() if s.done}
            missing = [k for k, when in committed.items() if current.get(k) != when]
            if missing:
                observed_errors.append({"kind": "confirmed_segment_changed_at_later_checkpoint",
                                        "as_of": int(live._strict_as_of().timestamp()), "keys": missing})
            for key, at in current.items():
                if at > int(live._strict_as_of().timestamp()):
                    observed_errors.append({"kind": "future_confirmation", "key": key, "confirmed_at": at})
            committed.update(current)
            return current

        for at, rows in sorted(events.items()):
            pos = bisect_left(source_closes, at)
            if pos == len(data) or source_closes[pos] != at:
                for row in rows:
                    row["checks"]["confirmation_on_source_close"] = False
                continue
            if advanced < pos:
                # Every growing frame is an exact prefix of the authenticated
                # immutable raw input, matching the chart's validated API.
                live.process_validated_incremental_klines(data.iloc[:pos], last_bar_closed=True)
                advanced = pos
            before = state() if advanced else {}
            live.process_validated_incremental_klines(data.iloc[:pos + 1], last_bar_closed=True)
            advanced = pos + 1
            after = state()
            for row in rows:
                key = tuple(row["geometry"])
                row["checks"].update(confirmation_on_source_close=True,
                                     not_confirmed_before_claimed_close=key not in before,
                                     confirmed_at_claimed_close=after.get(key) == at)
                row["before_confirmed_at"] = before.get(key)
                row["prefix_confirmed_at"] = after.get(key)
                row["raw_prefix_rows_before"] = pos
                row["raw_prefix_rows_at"] = pos + 1
        if advanced < len(data):
            live.process_validated_incremental_klines(data, last_bar_closed=True)
        last_state = state()
        incremental = [raw_geometry(s, quantum) for s in live.get_xds()]
        if incremental != actual:
            observed_errors.append({"kind": "final_incremental_and_batch_geometry_or_state_differ"})
        result["errors"].extend(observed_errors)
        for row in result["rows"]:
            key = tuple(row["geometry"])
            if row["confirmed"]:
                row["checks"]["confirmation_survives_to_window_end"] = last_state.get(key) == row.get("confirmed_at")
            row["verdict"] = "passed" if all(row["checks"].values()) else "needs_review"
        result.update(checkpoints=checkpoints, confirmed_segments=sum(r["confirmed"] for r in result["rows"]),
                      reviewed_segments=len(result["rows"]), failed_segments=sum(r["verdict"] != "passed" for r in result["rows"]),
                      status="passed" if not result["errors"] and all(r["verdict"] == "passed" for r in result["rows"]) else "needs_review")
    except Exception as exc:
        result["status"] = "input_or_replay_error"
        result["errors"].append({"kind": "exception", "error": f"{type(exc).__name__}: {exc}"})
    result["seconds"] = round(perf_counter() - started, 4)
    write_json(output / "causal_reviews" / f"{dataset['id']}.json.gz", result)
    return {k: v for k, v in result.items() if k not in {"rows", "pens"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    inv = read(args.output / "inventory_with_inputs.json")
    datasets = inv["datasets"]
    extra = args.output / "supplement.json"
    if extra.exists():
        datasets = datasets + read(extra)["datasets"]
    jobs = datasets[:args.limit]
    versions = hashes()
    rows, pending = [], []
    for d in jobs:
        previous = args.output / "causal_reviews" / f"{d['id']}.json.gz"
        if args.resume and previous.exists():
            old = read(previous)
            if old.get("implementation_sha256") == versions and old.get("status") == "passed":
                rows.append({k: v for k, v in old.items() if k not in {"rows", "pens"}})
                continue
        pending.append(d)
    print(json.dumps({"phase": "raw_confirmation_prefixes", "total": len(jobs), "pending": len(pending)}), flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(review, d, inv["request"], inv["reference_source_revision"], args.output, versions) for d in pending]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            if row["status"] != "passed" or len(rows) % 100 == 0 or len(rows) == len(jobs):
                print(json.dumps({"done": len(rows), "total": len(jobs), "states": dict(Counter(r["status"] for r in rows)),
                                  "last": {k: row[k] for k in ["code", "frequency", "status", "errors"]}}, ensure_ascii=False), flush=True)
    assert hashes() == versions, "Production changed during the replay"
    summary = {"implementation_sha256": versions, "completed": len(rows), "datasets": rows,
               "states": dict(Counter(r["status"] for r in rows)),
               **{k: sum(r.get(k, 0) for r in rows) for k in ["bars", "reviewed_segments", "confirmed_segments", "checkpoints", "failed_segments"]}}
    write_json(args.output / ("causal_pilot.json" if args.limit else "causal_results.json"), summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in {"datasets", "implementation_sha256"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
