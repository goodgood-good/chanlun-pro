"""Compare shared suffix scans with a frozen pre-review calculator and no reuse.

This checks implementation equivalence, not whether a shared interpretation is
the only reading of the source. Theory and user decisions have separate tests.
"""

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence, signature
from tests.core.test_segment_local_search import (
    CountedPrice, WithoutSuffixReuse, local_containment_points,
)
from tests.core.test_segment_source_rules import CASES, geometry, strokes
from tests.core.test_segment_reviewed_rules import REVIEWED


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("differential", "performance"), default="differential")
    parser.add_argument("--walks", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=19270929)
    parser.add_argument("--baseline", type=Path, default=ROOT / "output/segment_audit/reaudit_20260917/before/src/chanlun/core/xd_calculator.py")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("_segment_scan_before", args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    started = perf_counter()
    result = {"mode": args.mode, "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
              "all_input_theory_equivalence_proven": False}
    if args.mode == "differential":
        checked = 0
        reused_checked = 0
        modes = Counter()

        def compare(points, mode, without_reuse=False):
            nonlocal checked, reused_checked
            values = strokes(points)
            before, after = baseline.XdCalculator(), XdCalculator()
            old, new = before.calculate(values), after.calculate(values)
            assert geometry(old) == geometry(new), (mode, points, geometry(old), geometry(new))
            assert signature(old) == signature(new), (mode, points, "confirmation times")
            assert before.evidence == after.evidence, (mode, points, "source evidence")
            check_evidence(after, values)
            if without_reuse:
                direct = WithoutSuffixReuse()
                assert geometry(direct.calculate(values)) == geometry(new)
                assert direct.evidence == after.evidence
                reused_checked += 1
            checked += 1
            modes[mode] += 1

        for cases in (CASES, REVIEWED):
            for points, _ in cases.values():
                for mirror in (False, True):
                    compare([-p for p in points] if mirror else points, "listed", True)
        for kind in ("origin_retest", "local_first_break"):
            for outcome in ("pending", "extension", "completed"):
                for mirror in (False, True):
                    points = local_containment_points(kind, 12, outcome)
                    points = [-p for p in points] if mirror else points
                    for end in range(4, len(points) + 1):
                        compare(points[:end], "adversarial-prefix", True)
        rng = random.Random(args.seed)
        for j in range(args.walks):
            mode = ("levels-4", "levels-6", "unbounded")[j % 3]
            if mode == "unbounded":
                points = [0]
                up = rng.choice((False, True))
            else:
                levels = int(mode[-1])
                points = [rng.randrange(levels)]
                up = rng.choice((False, True)) if 0 < points[0] < levels - 1 else points[0] == 0
            for _ in range(63):
                if mode == "unbounded":
                    points.append(points[-1] + (1 if up else -1) * rng.randint(1, 40))
                else:
                    points.append(rng.choice(range(points[-1] + 1, levels) if up else range(points[-1])))
                up = not up
            compare(points, mode, j % 20 == 0)
        result.update(inputs=checked, modes=modes, seed=args.seed,
                      without_suffix_reuse_comparisons=reused_checked,
                      geometry_confirmation_and_sources_equal=True)
    else:
        records = []
        for kind in ("origin_retest", "local_first_break"):
            for outcome in ("pending", "extension", "completed"):
                for repeats in (100, 200, 400, 800):
                    points = local_containment_points(kind, repeats, outcome)
                    record = {"kind": kind, "outcome": outcome, "repeats": repeats, "pens": len(points) - 1}
                    for label, calculator_type in (("before", baseline.XdCalculator), ("after", XdCalculator)):
                        counted = strokes([CountedPrice(p) for p in points])
                        CountedPrice.comparisons = 0
                        calculator = calculator_type()
                        calculator.calculate(counted)
                        comparisons = CountedPrice.comparisons
                        values = strokes(points)
                        timings = []
                        for _ in range(3):
                            begun = perf_counter()
                            calculator = calculator_type()
                            calculator.calculate(values)
                            timings.append(perf_counter() - begun)
                        record[label] = {"comparisons": comparisons, "median_seconds": statistics.median(timings),
                                         "segments": len(calculator.xds),
                                         "materialized_source_indices": sum(len(e.source_indices) for p in calculator.evidence
                                                                            for e in (*p.first_sequence, *p.second_sequence))}
                    records.append(record)
        result["benchmarks"] = records
        result["timing_note"] = "Three fresh calculators on identical ordinary int inputs; timings separate from counted comparisons. Full provenance tuples are retained and their output size is reported."
    result.update(seconds=perf_counter() - started, failure=None)
    result["implementation_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                     "src/chanlun/core/strict_structure/base_profile.py", "script/check_segment_model.py",
                     "script/check_segment_scan_reuse.py", "tests/core/test_segment_local_search.py")
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("implementation_sha256", "benchmarks")}))


if __name__ == "__main__":
    main()
