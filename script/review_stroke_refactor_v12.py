"""Reproduce old-pen boundaries, processing modes and source-condition checks.

Only --source-root supplies Chan text. Fixtures and audits are engineering
evidence, not an oracle for the author's final whole-chart partition.
"""

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_stroke_architecture_v11 import file_record, read_originals
from review_stroke_rule_logic import (
    COMPLETION_RETRACTION, EQUAL_PREFIX, independent_definitions, long_origin_wait,
    raw_bars, reflected,
)
from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision


def signature(calc):
    construction = calc.construction_state()
    construction.pop("processing_mode")
    return {
        "construction": construction,
        "strokes": [{
            "centers": [b.start.k.index, b.end.k.index],
            "raw_centers": [b.start.k.k_index, b.end.k.k_index],
            "values": [b.start.val, b.end.val],
            "component": b.component_index,
            "selected_at": b.selected_at,
            "visible_at": b.fractal_visible_at,
            "successor_at": b.continuation_at,
            "locked_at": b.locked_at,
            "forming": b.forming,
            "status": b.completion_status,
        } for b in calc.bis],
        "choices": [asdict(v) for v in calc.qualification_evidence],
        "continuations": [asdict(v) for v in calc.continuation_evidence],
        "continuous_prefix_evidence": [asdict(v) for v in calc.completion_evidence],
        "revisions": [asdict(v) for v in calc.selection_revisions],
    }


def cold(raw):
    merged, calc = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(copy.deepcopy(raw))
    calc.calculate_batch(merged.cl_klines)
    return merged, calc


def update(merged, calc, raw):
    merged.process_cl_klines(raw)
    calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)


def check_local(calc):
    """No production stroke_rules or selector is called by this predicate."""
    prices = [(k.h, k.l) for k in calc.cl_klines]
    points, valid = independent_definitions(prices)
    by_center = {f.k.index: f for f in points}
    assert [(f.k.index, f.type, f.val) for f in calc.fxs] == [
        (f.k.index, f.type, f.val) for f in points
    ]
    for component in calc.stroke_components:
        for bi in component:
            assert valid(by_center[bi.start.k.index], by_center[bi.end.k.index])
            assert not bi.selection_pending or not bi.is_done()
            assert bi.fractal_visible_at <= bi.selected_at
            if bi.continuation_at is not None:
                assert bi.selected_at <= bi.continuation_at
            assert bi.completion_is_final is None
        for first, second in zip(component, component[1:]):
            assert first.end.k.index == second.start.k.index and first.type != second.type
    assert len(calc.stroke_components) == len(calc.unresolved_regions) + bool(calc.bis)
    assert len(calc.confirmed_bis) == len(calc.completion_evidence)
    return len(calc.bis)


def replay(prices, cuts, compare_each=True):
    raw = raw_bars(prices)
    merged, calc = CL_Kline_Process(), BiCalculator()
    modes, checked, comparisons = {}, 0, 0
    for end in cuts:
        update(merged, calc, raw[:end])
        modes[calc.processing_mode] = modes.get(calc.processing_mode, 0) + 1
        checked += check_local(calc)
        if compare_each:
            _, expected = cold(raw[:end])
            assert signature(calc) == signature(expected)
            comparisons += 1
    _, expected = cold(raw)
    assert signature(calc) == signature(expected)
    return calc, {"calls": len(cuts), "mode_counts": modes,
                  "checked_strokes": checked, "cold_comparisons": comparisons + 1}


def processing_modes():
    result = []
    for name, prices in (("short_crossing", COMPLETION_RETRACTION),
                         ("origin_wait", long_origin_wait(64)),
                         ("equal_junction", EQUAL_PREFIX + [(11, 7), (15, 11), (16, 12), (14, 10)])):
        for mirror in (False, True):
            values = reflected(prices, mirror)
            size = len(values)
            cases = {
                "one_batch": [size],
                "one_bar": list(range(1, size + 1)),
                "chunks_seven": sorted({*range(7, size, 7), size}),
                "history_then_live": [11, *range(12, size + 1)],
            }
            modes, reference = {}, None
            for mode, cuts in cases.items():
                calc, metrics = replay(values, cuts)
                if reference is None:
                    reference = signature(calc)
                assert signature(calc) == reference
                modes[mode] = metrics
            result.append({"name": name, "mirror": mirror, "modes": modes, "final": reference})
    return result


def long_histories():
    result = []
    cycle = [(18, 14), (16, 12), (14, 10), (12, 8), (14, 10), (16, 12), (18, 14), (20, 16)]
    for repeats in (0, 16, 64, 256):
        for mirror in (False, True):
            prices = COMPLETION_RETRACTION[:2] + cycle * repeats + COMPLETION_RETRACTION[2:]
            raw = raw_bars(reflected(prices, mirror))
            cut = 11 + 8 * repeats
            merged, calc = cold(raw[:cut])
            previous = [(b.start.k.index, b.end.k.index) for b in calc.bis]
            for end in range(cut + 1, len(raw) + 1):
                update(merged, calc, raw[:end])
            _, expected = cold(raw)
            assert signature(calc) == signature(expected)
            assert [(b.start.k.index, b.end.k.index) for b in calc.stroke_components[0]] == previous
            assert len(calc.bis) == len(previous) + 1
            check_local(calc)
            result.append({"repeats": repeats, "mirror": mirror, "bars": len(raw),
                           "prior_strokes": len(previous), "current_strokes": len(calc.bis),
                           "unresolved_regions": calc.construction_state()["unresolved_regions"]})
    return result


def generated(seed_count):
    comparisons = checked = 0
    primary_revisions = []
    for seed in range(seed_count):
        rng = random.Random(seed)
        prices, high = [], 100
        for _ in range(90):
            high += rng.choice((-6, -4, -2, 1, 3, 5))
            prices.append((high, high - rng.choice((2, 4, 6, 9))))
        calc, metrics = replay(prices, list(range(1, len(prices) + 1)))
        comparisons += metrics["cold_comparisons"]
        checked += metrics["checked_strokes"]
        revisions = [asdict(r) for r in calc.selection_revisions if r.revises_confirmed_selection]
        if revisions:
            primary_revisions.append({"seed": seed, "events": revisions})
        assert not calc.audit_endpoint_adjacency()
    return {"seeds": seed_count, "bars_per_seed": 90, "cold_comparisons": comparisons,
            "independent_local_stroke_checks": checked, "primary_evidence_revisions": primary_revisions}


def intrabar():
    comparisons = 0
    for seed in range(8):
        rng = random.Random(seed + 700)
        prices, high = list(COMPLETION_RETRACTION), 10
        for _ in range(45):
            high += rng.choice((-3, -1, 1, 3))
            prices.append((high, high - rng.choice((2, 4, 6))))
        raw = raw_bars(prices)
        merged, calc = CL_Kline_Process(), BiCalculator()
        for count in range(1, len(raw) + 1):
            for scale in (0.25, 0.7, 1.0):
                prefix = copy.deepcopy(raw[:count])
                tail = prefix[-1]
                middle = (tail.h + tail.l) / 2
                tail.h = middle + (tail.h - middle) * scale
                tail.l = middle + (tail.l - middle) * scale
                tail.o = tail.c = middle
                update(merged, calc, prefix)
                _, expected = cold(prefix)
                assert signature(calc) == signature(expected)
                check_local(calc)
                comparisons += 1
    return {"seeds": 8, "cold_comparisons": comparisons}


def markets():
    import pandas as pd

    result = []
    for name, frequency, market in (("SZ.002299_1m.parquet", "1m", "a"),
                                     ("SH.600519_5m.parquet", "5m", "a"),
                                     ("SH.600519_30m.parquet", "30m", "a"),
                                     ("QQQ.US_30m.parquet", "30m", "us")):
        path = ROOT / "tests/fixtures" / name
        frame = pd.read_parquet(path)
        config = {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"}
        begin = perf_counter()
        batch = CL(name.split("_")[0], frequency, config, market=market).process_klines_batch(frame)
        batch_seconds = perf_counter() - begin
        live = CL(name.split("_")[0], frequency, config, market=market)
        cuts = sorted({*range(257, len(frame), 257), len(frame)})
        for count in cuts:
            live.process_klines(frame.iloc[:count])
        assert signature(live.bi_calculator) == signature(batch.bi_calculator)
        assert [(x.to_dict()) for x in live.get_xds()] == [(x.to_dict()) for x in batch.get_xds()]
        calc = batch.bi_calculator
        check_local(calc)
        assert not calc.audit_endpoint_ranges() and not calc.audit_endpoint_adjacency()
        assert live.get_native_centers() == batch.get_native_centers()
        assert live.get_stroke_observation_centers() == batch.get_stroke_observation_centers()
        result.append({**file_record(path), "raw_bars": len(frame), "batch_seconds": batch_seconds,
                       "incremental_chunks": len(cuts), "candidates": len(calc.bis),
                       "component_stroke_counts": list(map(len, calc.stroke_components)),
                       "continuous_prefix_strokes": len(calc.contiguous_bis),
                       "segments": len(batch.get_xds()),
                       "construction": calc.construction_state(),
                       "errors": []})
        print(f"checked {name}: {len(frame)} bars, {len(calc.bis)} candidates", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("D:/缠论"))
    parser.add_argument("--output", type=Path, default=ROOT / "output/bi_code_review/v12_validation.json")
    parser.add_argument("--seeds", type=int, default=40)
    args = parser.parse_args()
    sources = read_originals(args.source_root)
    started = perf_counter()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "stroke_profile": STRICT_STROKE_MODE,
              "profile_revision": strict_base_config_revision(), "sources": sources,
              "proof_limit": "Local predicates, causal replay and scope consistency; not a proof of final global uniqueness."}
    for name, run in (("processing_modes", processing_modes), ("long_histories", long_histories),
                      ("generated", lambda: generated(args.seeds)), ("intrabar", intrabar), ("markets", markets)):
        report[name] = run()
        print(f"passed {name}", flush=True)
    report["production_files"] = [file_record(p) for p in sorted((ROOT / "src/chanlun/core").rglob("*.py"))]
    report["elapsed_seconds"] = perf_counter() - started
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                      "elapsed_seconds": report["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
