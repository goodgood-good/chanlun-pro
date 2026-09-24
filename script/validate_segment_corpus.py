"""Audit every locally stored raw OHLC dataset, without fetching market data.

File/content deduplication is explicit in inventory.json. Every distinct data
window is retained. CSV tables of already merged bars, derived prices or scan
results are listed as excluded rather than mislabelled as raw market histories.
This verifies engineering invariants and the declared evidence interpretation;
it is not an independent oracle for every possible original-theory diagram.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence


RAW_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
HASH_PATHS = (
    "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
    "src/chanlun/core/bi_calculator.py", "src/chanlun/core/stroke_sequence.py",
    "src/chanlun/core/cl.py", "src/chanlun/core/strict_structure/base_profile.py",
    "script/check_segment_model.py", "script/validate_segment_corpus.py",
)


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in HASH_PATHS}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def read_frame(path):
    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    # UTC preserves physical instants and gives Parquet/CSV the same date dtype.
    frame["date"] = pd.to_datetime(frame.date, utc=True)
    return frame


def inventory(output):
    names = subprocess.check_output([
        "rg", "--files", "--hidden", "--no-ignore", "-g", "*.parquet", "-g", "*.csv",
        "-g", "!.git/**", "-g", "!node_modules/**", "-g", "!.venv/**", "-g", "!venv/**",
    ], cwd=ROOT).decode("utf-8").splitlines()
    datasets, excluded, files = {}, [], []
    for name in sorted(names, key=lambda x: (not x.startswith("tests"), x)):
        path = ROOT / name
        if path.suffix == ".csv":
            columns = pd.read_csv(path, nrows=0).columns
            if not set(RAW_COLUMNS).issubset(columns):
                excluded.append({"file": name, "reason": "not a dated raw OHLCV table", "columns": list(columns)})
                continue
        # Frozen regressions may add a date range or a descriptive window
        # suffix. The leading code/frequency still identify the raw dataset.
        match = re.match(r"(?P<code>.+?)_(?P<frequency>\d+(?:m|h|d|w))(?:_.*)?$", path.stem)
        if not match:
            raise ValueError(f"Unknown dataset identity: {name}")
        code, frequency = match.group("code", "frequency")
        market = "us" if code.endswith(".US") else "a"
        frame = read_frame(path)
        assert set(RAW_COLUMNS).issubset(frame), name
        assert frame.date.is_monotonic_increasing and frame.date.is_unique, name
        normalized = frame[RAW_COLUMNS].copy()
        for column in RAW_COLUMNS[1:]:
            normalized[column] = normalized[column].astype("float64")
        digest = hashlib.sha256((code + "|" + frequency + "|" + market).encode())
        digest.update(pd.util.hash_pandas_object(normalized, index=False).values.tobytes())
        data_hash = digest.hexdigest()
        file_record = {"file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "content_sha256": data_hash, "bars": len(frame)}
        files.append(file_record)
        if data_hash not in datasets:
            datasets[data_hash] = {"id": data_hash[:16], "file": name, "code": code,
                                  "frequency": frequency, "market": market, "bars": len(frame),
                                  "start": str(frame.date.iloc[0]), "end": str(frame.date.iloc[-1]),
                                  "content_sha256": data_hash, "files": []}
        datasets[data_hash]["files"].append(file_record)
    result = {
        "scope": "all dated raw OHLCV Parquet/CSV files found recursively under repository root",
        "normalization": "UTC instants and float64 OHLCV, same code/frequency/market; no row sorting or window merging",
        "raw_files": len(files), "file_hashes": len({x["sha256"] for x in files}),
        "unique_datasets": len(datasets), "unique_window_bars": sum(x["bars"] for x in datasets.values()),
        "datasets": list(datasets.values()), "excluded": excluded,
        "implementation_sha256": hashes(),
    }
    write_json(output / "inventory.json", result)
    return result


def line_state(lines):
    return tuple((x.start_line.index, x.end_line.index, x.type, x.done, x.locked_at,
                  x.start.val, x.end.val, x.high, x.low) for x in lines)


def physical(lines):
    return {(x.start.k.k_index, x.end.k.k_index, x.type): x.locked_at for x in lines if x.done}


def pen_state(values):
    return tuple((x.start.k.k_index, x.end.k.k_index, x.type, x.start.val, x.end.val,
                  x.high, x.low, x.locked_at, x.selection_pending) for x in values)


def check_structure(calc, values):
    check_evidence(calc, values)
    proof_by_key = {p.key: p for p in calc.evidence}
    previous_time = None
    for index, line in enumerate(calc.xds):
        a, b = line.start_line.index, line.end_line.index
        assert b - a >= 2 and (b - a) % 2 == 0, "segment_span"
        assert line.type == values[a].type == values[b].type, "segment_direction"
        assert line.high == max(v.high for v in values[a:b + 1]), "segment_range_high"
        assert line.low == min(v.low for v in values[a:b + 1]), "segment_range_low"
        if index:
            prev = calc.xds[index - 1]
            assert prev.end_line.index + 1 == a and prev.type != line.type, "segment_adjacency"
        if not line.done:
            assert line.locked_at is None, "pending_with_lock"
            continue
        proof = proof_by_key[(a, b, line.type)]
        dependency_start = proof.origin_evidence.initial_index if proof.origin_evidence else a
        dependencies = values[dependency_start:proof.witness_index + 1]
        assert all(x.locked_at is not None and not x.selection_pending for x in dependencies), "unavailable_evidence"
        expected = max(x.locked_at for x in dependencies)
        if previous_time is not None:
            expected = max(expected, previous_time)
        assert line.locked_at >= expected, "backdated_dependency"
        previous_time = line.locked_at
    assert calc._feature_scan_cache is None, "cross_call_feature_cache"


def fresh_matches(calc, values):
    fresh = XdCalculator()
    fresh.calculate(values)
    assert line_state(calc.xds) == line_state(fresh.xds), "warm_cold_segment_difference"
    assert calc.evidence == fresh.evidence, "warm_cold_evidence_difference"
    assert calc.tail_state == fresh.tail_state, "warm_cold_tail_difference"


def fixed_prefixes(values):
    calc, previous, count = XdCalculator(), {}, 0
    for end in range(len(values) + 1):
        prefix = values[:end]
        calc.calculate(prefix)
        current = {(x.start_line.index, x.end_line.index, x.type): x.locked_at for x in calc.xds if x.done}
        assert all(current.get(k) == t for k, t in previous.items()), ("fixed_prefix_revocation", end)
        check_structure(calc, prefix)
        fresh_matches(calc, prefix)
        previous = current
        count += 1
    return count


def new_cl(dataset):
    return CL(dataset["code"], dataset["frequency"], {}, market=dataset["market"])


def revisions(frame, dataset):
    """Each real data window exercises old-row replacement and same-input restoration."""
    if len(frame) < 20:
        return 0
    running = new_cl(dataset)
    running.process_klines(frame, last_bar_closed=True)
    index = len(frame) // 2
    changed = frame.copy()
    lo, hi = float(changed.loc[index, "low"]), float(changed.loc[index, "high"])
    changed.loc[index, "high"] = hi + max(abs(hi) / 50, hi - lo, .01)
    for target in (changed, frame):
        running.process_klines(target.iloc[[index]], last_bar_closed=True)
        fresh = new_cl(dataset)
        fresh.process_klines(target, last_bar_closed=True)
        assert pen_state(running.get_contiguous_bis()) == pen_state(fresh.get_contiguous_bis()), "revision_pen_difference"
        assert line_state(running.get_xds()) == line_state(fresh.get_xds()), "revision_segment_difference"
        assert running.xd_calculator.evidence == fresh.xd_calculator.evidence, "revision_evidence_difference"
        check_structure(running.xd_calculator, running.get_contiguous_bis())
    # Explicitly replace the observation window; identities are compared with
    # a fresh instance in the same window, not with the deleted older history.
    window = frame.iloc[len(frame) // 3:].reset_index(drop=True)
    running.process_klines_batch(window, last_bar_closed=True)
    fresh = new_cl(dataset)
    fresh.process_klines(window, last_bar_closed=True)
    assert pen_state(running.get_contiguous_bis()) == pen_state(fresh.get_contiguous_bis()), "window_pen_difference"
    assert line_state(running.get_xds()) == line_state(fresh.get_xds()), "window_segment_difference"
    assert running.xd_calculator.evidence == fresh.xd_calculator.evidence, "window_evidence_difference"
    return 3


def run_dataset(dataset, output, replay=True):
    output = Path(output)
    started = perf_counter()
    result = {**dataset, "failure": None, "fixed_prefixes": 0, "replayed_bars": 0,
              "changed_pen_states": 0, "revision_checks": 0,
              "implementation_sha256": hashes()}
    location = output / "datasets" / (dataset["id"] + ".json")
    stage = "read"
    try:
        frame = read_frame(ROOT / dataset["file"]).reset_index(drop=True)
        stage = "batch"
        batch = new_cl(dataset)
        batch.process_klines(frame, last_bar_closed=True)
        values = batch.get_contiguous_bis()
        check_structure(batch.xd_calculator, values)
        fresh_matches(batch.xd_calculator, values)
        result.update(pens=len(values), segments=len(batch.get_xds()),
                      confirmed_segments=sum(x.done for x in batch.get_xds()),
                      unresolved_regions=len(batch.get_stroke_construction_state()["unresolved_regions"]))
        stage = "fixed_pen_prefixes"
        result["fixed_prefixes"] = fixed_prefixes(values)
        stage = "history_revision"
        result["revision_checks"] = revisions(frame, dataset)
        if replay:
            stage = "bar_replay"
            running, previous, prior_pens = new_cl(dataset), {}, None
            for index, row in enumerate(frame.itertuples(index=False), 1):
                result["replayed_bars"] = index
                running.process_kline_values(row.date, row.open, row.high, row.low,
                                             row.close, row.volume, bar_closed=True)
                current = physical(running.get_xds())
                assert all(current.get(k) == t for k, t in previous.items()), ("bar_confirmed_revocation", index, previous, current)
                assert all(t == row.date for k, t in current.items() if k not in previous), ("bar_confirmation_backdated", index)
                current_pens = pen_state(running.get_contiguous_bis())
                if current_pens != prior_pens:
                    result["changed_pen_states"] += 1
                    check_structure(running.xd_calculator, running.get_contiguous_bis())
                    fresh_matches(running.xd_calculator, running.get_contiguous_bis())
                previous, prior_pens = current, current_pens
                if index % 1000 == 0:
                    result["progress_seconds"] = perf_counter() - started
                    write_json(output / "progress" / (dataset["id"] + ".json"), result)
            stage = "stream_batch_equivalence"
            assert pen_state(running.get_contiguous_bis()) == pen_state(values), "stream_batch_pen_difference"
            assert line_state(running.get_xds()) == line_state(batch.get_xds()), "stream_batch_segment_difference"
            assert running.xd_calculator.evidence == batch.xd_calculator.evidence, "stream_batch_evidence_difference"
            assert running.xd_calculator.tail_state == batch.xd_calculator.tail_state, "stream_batch_tail_difference"
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = {"stage": stage, "reason": repr(exc), "traceback": traceback.format_exc()}
        # Original raw data is retained; the exact failure bar/index above and
        # file hash identify a reproducible prefix without duplicating history.
        if "running" in locals():
            result["failure"]["pens"] = pen_state(running.get_contiguous_bis())
            result["failure"]["segments"] = line_state(running.get_xds())
            result["failure"]["proofs"] = [asdict(p) for p in running.xd_calculator.evidence]
    result["seconds"] = perf_counter() - started
    write_json(location, result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "output/segment_audit/full_validation_20260917/corpus")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--batch-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dataset", help="Limit to a dataset id, file fragment or code for diagnosis")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["CHANLUN_LOG_DIR"] = str(args.output / "logs")
    info = inventory(args.output)
    print(json.dumps({k: info[k] for k in ("raw_files", "file_hashes", "unique_datasets", "unique_window_bars")}), flush=True)
    if args.inventory_only:
        return
    started, results, pending = perf_counter(), [], []
    initial_hashes = hashes()
    for dataset in sorted(info["datasets"], key=lambda x: -x["bars"]):
        if args.dataset and args.dataset not in json.dumps(dataset):
            continue
        cached = args.output / "datasets" / (dataset["id"] + ".json")
        old = json.loads(cached.read_text(encoding="utf-8")) if args.resume and cached.exists() else None
        if old and old.get("status") == "passed" and old["implementation_sha256"] == initial_hashes and (
            args.batch_only or old["replayed_bars"] == dataset["bars"]
        ):
            results.append(old)
        else:
            pending.append(dataset)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_dataset, d, args.output, not args.batch_only): d for d in pending}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({k: result[k] for k in ("id", "file", "status", "bars", "replayed_bars", "seconds", "failure")}, default=str), flush=True)
    result = {
        "scope": info["scope"], "filtered": args.dataset, "batch_only": args.batch_only,
        "completed_datasets": len(results), "failures": sum(x["failure"] is not None for x in results),
        "replayed_bars": sum(x["replayed_bars"] for x in results),
        "fixed_prefixes": sum(x["fixed_prefixes"] for x in results),
        "changed_pen_states": sum(x["changed_pen_states"] for x in results),
        "revision_checks": sum(x["revision_checks"] for x in results),
        "seconds": perf_counter() - started, "datasets": sorted(results, key=lambda x: x["file"]),
        "implementation_sha256": initial_hashes, "end_sha256": hashes(),
        "all_input_theory_equivalence_proven": False,
        "market_price_gap_special_rules": "not assessed; feature-interval gaps are included",
    }
    assert initial_hashes == hashes(), "code changed during audit"
    write_json(args.output / "results.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "datasets"}), flush=True)
    raise SystemExit(bool(result["failures"]))


if __name__ == "__main__":
    main()
