"""Corpus-wide segment invariance and live-tail interface checks.

These complement full closed-bar replay: default next-bar availability,
intra-bar replacement, serialization, independent skipped-candidate inspection,
and order-preserving price transforms. No new theory interpretation is added.
"""

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import copy
from dataclasses import asdict, replace, fields, is_dataclass
from fractions import Fraction
from decimal import Decimal, ROUND_FLOOR
import argparse
import hashlib
import json
from pathlib import Path
import pickle
import sys
from time import perf_counter
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from chanlun.core.xd_calculator import XdCalculator
from chanlun.core.segment_evidence import FeatureEvidence
from script.check_segment_candidates import SearchAuditCalculator
from script.validate_segment_corpus import (
    check_structure, hashes, line_state, new_cl, pen_state, physical, read_frame, write_json,
)
from tests.core.test_segment_local_search import WithoutSuffixReuse


def full_hashes():
    return {**hashes(), **{name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "script/check_segment_candidates.py", "script/check_segment_corpus_interfaces.py",
        "src/chanlun/core/stroke_observations.py", "tests/core/test_segment_local_search.py",
    )}}


def projected_components(cd):
    """Check every chart-only component against fresh local construction."""
    observations = {x.component_index: x for x in cd.stroke_observations.observations}
    expected_count = 0
    for component_index, values in enumerate(cd.bi_calculator.stroke_components):
        if component_index == 0 or not values:
            continue
        local = []
        for index, pen in enumerate(values):
            item = copy(pen)
            item.index = index
            item.locked_at = pen.continuation_at
            item.forming = item.locked_at is None
            item.selection_pending = False
            local.append(item)
        fresh = XdCalculator()
        fresh.calculate(local)
        check_structure(fresh, local)
        actual = observations[component_index].segments
        assert len(actual) == len(fresh.xds), "conditional_segment_count"
        for a, b in zip(actual, fresh.xds):
            assert (a.start.k.k_index, a.end.k.k_index, a.type, a.high, a.low) == (
                b.start.k.k_index, b.end.k.k_index, b.type, b.high, b.low), "conditional_geometry"
            assert not a.done and a.locked_at is None and a.selection_pending, "conditional_false_confirmation"
            assert a.scope_confirmed_at == b.locked_at, "conditional_local_time"
        expected_count += 1
    assert len(observations) == expected_count, "orphan_conditional_component"
    assert len(cd.get_chart_xds()) == len(cd.get_xds()) + sum(len(x.segments) for x in observations.values())
    return expected_count


def ordinal_check(values, original):
    if not values:
        return 0
    prices = sorted({p for x in values for p in (x.start.val, x.end.val)})
    rank = {p: i for i, p in enumerate(prices)}
    # Strictly increasing nonlinear mapping and reflection preserve equality
    # classes and ordering exactly; no floating-point tolerance is introduced.
    for mirror in (False, True):
        def convert(price):
            mapped = Fraction(rank[price] ** 3 + rank[price], 7) - 100000
            return -mapped if mirror else mapped

        def flip(direction):
            return {"up": "down", "down": "up"}[direction] if mirror else direction

        mapped = []
        for pen in values:
            item = copy(pen)
            item.start, item.end = copy(pen.start), copy(pen.end)
            item.start.val, item.end.val = convert(pen.start.val), convert(pen.end.val)
            item.type = flip(pen.type)
            item.high, item.low = max(item.start.val, item.end.val), min(item.start.val, item.end.val)
            mapped.append(item)
        transformed = XdCalculator()
        transformed.calculate(mapped)
        check_structure(transformed, mapped)
        assert len(original.xds) == len(transformed.xds), "ordinal_count"
        for a, b in zip(original.xds, transformed.xds):
            assert (a.start_line.index, a.end_line.index, flip(a.type), a.done, a.locked_at) == (
                b.start_line.index, b.end_line.index, b.type, b.done, b.locked_at), "ordinal_geometry"
            assert (convert(a.start.val), convert(a.end.val)) == (b.start.val, b.end.val), "ordinal_endpoint"

        def element(e):
            return replace(e, low=convert(e.high if mirror else e.low),
                           high=convert(e.low if mirror else e.high),
                           low_source_index=e.high_source_index if mirror else e.low_source_index,
                           high_source_index=e.low_source_index if mirror else e.high_source_index)

        def proof(p):
            # Certificates now contain their own independent proofs. Transform
            # their price suppliers and directions as well, keeping indices and
            # clocks unchanged; omitting nested receipts is not a valid oracle.
            if isinstance(p,FeatureEvidence):
                return element(p)
            if is_dataclass(p):
                return replace(p,**{f.name:proof(getattr(p,f.name)) for f in fields(p)})
            if isinstance(p,tuple):
                return tuple(proof(x) for x in p)
            if isinstance(p,list):
                return [proof(x) for x in p]
            if isinstance(p,str) and p in ('up','down'):
                return flip(p)
            return p

        assert tuple(proof(p) for p in original.evidence) == transformed.evidence, "ordinal_proof"
        assert replace(original.tail_state, direction=flip(original.tail_state.direction)
                       if original.tail_state.direction else None) == transformed.tail_state, "ordinal_tail"
    return 2


def check_one(dataset, output):
    result = {"id": dataset["id"], "file": dataset["file"], "failure": None,
              "live_tail_updates": 0, "conditional_components": 0, "transforms": 0,
              "skipped_candidate_counts": {}, "implementation_sha256": full_hashes()}
    started, stage = perf_counter(), "read"
    try:
        frame = read_frame(ROOT / dataset["file"]).reset_index(drop=True)
        cd = new_cl(dataset)
        cd.process_klines(frame, last_bar_closed=True)
        values = cd.get_contiguous_bis()
        stage = "candidate_search"
        counts = Counter()
        audit = SearchAuditCalculator(counts)
        audit.calculate(values)
        assert line_state(audit.xds) == line_state(cd.get_xds())
        assert audit.evidence == cd.xd_calculator.evidence
        result["skipped_candidate_counts"] = dict(counts)
        stage = "without_suffix_reuse"
        plain = WithoutSuffixReuse()
        plain.calculate(values)
        assert line_state(plain.xds) == line_state(cd.get_xds()), "cache_geometry"
        assert plain.evidence == cd.xd_calculator.evidence, "cache_proof"
        stage = "price_order"
        result["transforms"] = ordinal_check(values, cd.xd_calculator)
        stage = "conditional_components"
        result["conditional_components"] += projected_components(cd)
        stage = "live_tail"
        begin = max(0, len(frame) - 24)
        live = new_cl(dataset)
        if begin:
            live.process_klines(frame.iloc[:begin], last_bar_closed=False)
        restored = pickle.loads(pickle.dumps(live))
        prior = physical(live.get_xds())
        for end in range(begin + 1, len(frame) + 1):
            prefix = frame.iloc[:end].copy()
            row = prefix.iloc[-1]
            # A partial candle inside its final range, then its final OHLCV.
            draft_close = (float(row.open) + float(row.close)) / 2
            if dataset.get('structure_price_quantum') is not None:
                # The simulated partial candle must obey the retained price
                # grid too; a floating midpoint can invent an invalid half tick.
                quantum=Decimal(str(dataset['structure_price_quantum']))
                middle=(Decimal(str(row.open))+Decimal(str(row.close)))/(2*quantum)
                draft_close=float(middle.to_integral_value(rounding=ROUND_FLOOR)*quantum)
            prefix.loc[end - 1, ["high", "low", "close", "volume"]] = [
                max(float(row.open), draft_close), min(float(row.open), draft_close),
                draft_close, float(row.volume) / 2,
            ]
            for target in (prefix, frame.iloc[:end]):
                update = target.iloc[-1]
                for running in (live, restored):
                    running.process_kline_values(update.date, update.open, update.high, update.low,
                                                 update.close, update.volume, bar_closed=False)
                fresh = new_cl(dataset)
                fresh.process_klines(target, last_bar_closed=False)
                for running in (live, restored):
                    assert pen_state(running.get_contiguous_bis()) == pen_state(fresh.get_contiguous_bis()), ("live_pen_difference", end)
                    assert line_state(running.get_xds()) == line_state(fresh.get_xds()), ("live_segment_difference", end)
                    assert running.xd_calculator.evidence == fresh.xd_calculator.evidence, ("live_proof_difference", end)
                    check_structure(running.xd_calculator, running.get_contiguous_bis())
                current = physical(live.get_xds())
                assert all(current.get(k) == t for k, t in prior.items()), ("intrabar_confirmed_revocation", end)
                prior = current
                result["conditional_components"] += projected_components(live)
                result["live_tail_updates"] += 1
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = {"stage": stage, "reason": repr(exc), "traceback": traceback.format_exc()}
        if "live" in locals():
            result["failure"]["pens"] = pen_state(live.get_contiguous_bis())
            result["failure"]["segments"] = line_state(live.get_xds())
            result["failure"]["proofs"] = [asdict(p) for p in live.xd_calculator.evidence]
    result["seconds"] = perf_counter() - started
    write_json(Path(output) / "datasets" / (dataset["id"] + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dataset")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    datasets = json.loads(args.inventory.read_text(encoding="utf-8"))["datasets"]
    if args.dataset:
        datasets = [d for d in datasets if args.dataset in json.dumps(d)]
    started, results, before = perf_counter(), [], full_hashes()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(check_one, d, args.output) for d in datasets]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({k: result[k] for k in ("id", "file", "status", "failure", "seconds")}, default=str), flush=True)
    result = {"datasets": results, "completed_datasets": len(results),
              "filtered": args.dataset, "failures": sum(r["failure"] is not None for r in results),
              "live_tail_updates": sum(r["live_tail_updates"] for r in results),
              "conditional_components": sum(r["conditional_components"] for r in results),
              "transforms": sum(r["transforms"] for r in results),
              "seconds": perf_counter() - started, "implementation_sha256": before,
              "end_sha256": full_hashes()}
    assert before == full_hashes(), "code changed during audit"
    write_json(args.output / "results.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "datasets"}), flush=True)
    raise SystemExit(bool(result["failures"]))


if __name__ == "__main__":
    main()
