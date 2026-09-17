"""Check every prefix of original-figure constrained price-order families.

The families encode explicit figure premises; unconstrained pairs can occur
in every strict/equal ordering. These are synthetic realizations, not recovered
historical market prices, and do not enumerate arbitrary future continuations.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from script.check_segment_candidates import SearchAuditCalculator
from script.check_segment_model import check_evidence, signature
from script.validate_segment_corpus import write_json
from tests.core.segment_figure_families import (
    lesson79_family, lesson79_lower_continuation_family, lesson81_family,
)
from tests.core.test_segment_source_rules import geometry, strokes


def families():
    for kind in ("upper", "lower", "equal9"):
        for touching in (False, True):
            expected = ([(0, 3, True), (3, 8, True), (8, 11, False)] if kind == "upper"
                        else [(0, 3, True), (3, 10, False)])
            yield f"L079-{kind}-touching-{touching}", lesson79_family(kind, touching), expected
            if kind != "upper":
                yield (f"L079-{kind}-touching-{touching}-completed",
                       lesson79_lower_continuation_family(kind, touching),
                       [(0, 3, True), (3, 10, True), (10, 13, False)])
    for kind, gap in (("lower7", "overlap"), ("lower7", "touch"), ("lower7", "gap"),
                      ("equal7", "overlap"), ("higher7", "overlap")):
        expected = ([(0, 3, True), (3, 6, True), (6, 9, False)] if kind == "lower7"
                    else [(0, 9, False)])
        yield f"L081-{kind}-{gap}", lesson81_family(kind, gap), expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started, result, counts = perf_counter(), [], Counter()
    for name, generated, expected in families():
        cases, prefixes = 0, 0
        for points in generated:
            for mirror in (False, True):
                values = strokes(points, mirror)
                calc, previous = SearchAuditCalculator(counts), {}
                for end in range(3, len(values) + 1):
                    current = signature(calc.calculate(values[:end]))
                    assert all(current.get(k) == t for k, t in previous.items()), (name, points, mirror, end)
                    assert all(t == values[end - 1].locked_at for k, t in current.items() if k not in previous), (name, points, mirror, end)
                    check_evidence(calc, values[:end])
                    previous = current
                    prefixes += 1
                assert geometry(calc.xds) == expected, (name, points, mirror)
                cases += 1
        result.append({"family": name, "mirrored_realizations": cases, "prefixes": prefixes})
    report = {
        "families": result, "realizations": sum(x["mirrored_realizations"] for x in result),
        "prefixes": sum(x["prefixes"] for x in result), "skipped_candidate_counts": counts,
        "seconds": perf_counter() - started, "failure": None,
        "scope": "L079/L081 explicitly constrained price order families plus specified continuations and mirrors",
        "all_input_theory_equivalence_proven": False,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "script/check_segment_model.py", "script/check_segment_candidates.py",
                "script/check_segment_order_families.py", "tests/core/segment_figure_families.py",
            )
        },
    }
    write_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
