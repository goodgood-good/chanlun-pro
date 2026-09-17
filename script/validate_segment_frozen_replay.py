"""Full immutable-corpus replay through the existing validated CL input API.

Only the public entrypoint changes: already validated append-only OHLC rows use
process_validated_incremental_klines. This skips repeated historical HTF-MACD
input comparisons, not price processing, pen construction or segment checks.
Completed scalar-entrypoint results from the same implementation can be reused;
their original hashes and entrypoint are preserved in the aggregate report.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import pandas as pd

from chanlun.core.cl import CL
import script.validate_segment_corpus as audit


WRAPPER = "script/validate_segment_frozen_replay.py"


def wrapper_hash():
    return hashlib.sha256((ROOT / WRAPPER).read_bytes()).hexdigest()


class FrozenInputCL(CL):
    """Adapter for this audit's prevalidated immutable, chronological inputs."""

    def process_kline_values(self, date, open_, high, low, close, volume=0.0, *, bar_closed=False):
        existing = self.kline_processor.klines
        stamp = pd.Timestamp(date)
        if existing and stamp <= existing[-1].date:
            # Revised or repeated input does not qualify for a trusted append.
            return super().process_kline_values(date, open_, high, low, close, volume,
                                               bar_closed=bar_closed)
        frame = pd.DataFrame([dict(date=stamp, open=open_, high=high, low=low,
                                   close=close, volume=volume)])
        return self.process_validated_incremental_klines(frame, last_bar_closed=bar_closed)


def make_cl(dataset):
    return FrozenInputCL(dataset["code"], dataset["frequency"], {}, market=dataset["market"])


def run_one(dataset, output):
    audit.new_cl = make_cl
    result = audit.run_dataset(dataset, output, replay=True)
    result["replay_entrypoint"] = "process_validated_incremental_klines"
    result["implementation_sha256"][WRAPPER] = wrapper_hash()
    audit.write_json(Path(output) / "datasets" / (dataset["id"] + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--scalar-results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    info = json.loads(args.inventory.read_text(encoding="utf-8"))
    before = {**audit.hashes(), WRAPPER: wrapper_hash()}
    started, results, pending = perf_counter(), [], []
    for dataset in sorted(info["datasets"], key=lambda x: -x["bars"]):
        old = None
        sources = [args.output] if args.resume else []
        if args.scalar_results:
            sources.append(args.scalar_results)
        for source in sources:
            saved = source / "datasets" / (dataset["id"] + ".json")
            if not saved.exists():
                continue
            candidate = json.loads(saved.read_text(encoding="utf-8"))
            if candidate.get("status") != "passed" or candidate["replayed_bars"] != dataset["bars"]:
                continue
            if candidate["content_sha256"] != dataset["content_sha256"]:
                continue
            if any(candidate["implementation_sha256"].get(k) != v for k, v in audit.hashes().items()):
                continue
            if WRAPPER in candidate["implementation_sha256"] and candidate["implementation_sha256"][WRAPPER] != before[WRAPPER]:
                continue
            old = {**candidate, "result_file": str(saved),
                   "replay_entrypoint": candidate.get("replay_entrypoint", "process_kline_values")}
            break
        if old:
            results.append(old)
        else:
            pending.append(dataset)
    print(json.dumps({"datasets": len(info["datasets"]), "reused_complete_datasets": len(results),
                      "pending_datasets": len(pending)}), flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, dataset, args.output) for dataset in pending]
        for future in as_completed(futures):
            result = future.result()
            result["result_file"] = str(args.output / "datasets" / (result["id"] + ".json"))
            results.append(result)
            print(json.dumps({k: result[k] for k in ("id", "file", "status", "replayed_bars", "seconds", "failure")}, default=str), flush=True)
    after = {**audit.hashes(), WRAPPER: wrapper_hash()}
    report = {
        "scope": info["scope"], "inventory_file": str(args.inventory),
        "inventory_sha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        "completed_datasets": len(results), "failures": sum(r["failure"] is not None for r in results),
        "replayed_bars": sum(r["replayed_bars"] for r in results),
        "fixed_prefixes": sum(r["fixed_prefixes"] for r in results),
        "changed_pen_states": sum(r["changed_pen_states"] for r in results),
        "revision_checks": sum(r["revision_checks"] for r in results),
        "replay_entrypoints": {method: sum(r["replay_entrypoint"] == method for r in results)
                               for method in {r["replay_entrypoint"] for r in results}},
        "seconds": perf_counter() - started, "implementation_sha256": before,
        "end_sha256": after, "datasets": sorted(results, key=lambda r: r["file"]),
        "all_input_theory_equivalence_proven": False,
        "market_price_gap_special_rules": "not assessed; feature-interval gaps are included",
    }
    assert before == after, "code changed during audit"
    assert len(results) == len(info["datasets"]), "incomplete corpus"
    audit.write_json(args.output / "results.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "datasets"}), flush=True)
    raise SystemExit(bool(report["failures"]))


if __name__ == "__main__":
    main()
