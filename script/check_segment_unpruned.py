"""Compare search optimization with an explicit every-candidate scan.

Candidate qualification and L078 invalidation priority remain the declared
rules. Only optional first-sequence pruning and suffix reuse are disabled.
This is an optimization-equivalence check, not a separate theory oracle.
"""

import argparse
import hashlib
import json
from pathlib import Path
import random
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from chanlun.core.xd_calculator import XdCalculator, _CandidateExtension
from chanlun.core.segment_evidence import PendingBoundary
from script.validate_segment_corpus import line_state, new_cl, read_frame, write_json
from tests.core.test_segment_local_search import WithoutSuffixReuse, local_containment_points
from tests.core.test_segment_source_rules import CASES, strokes
from tests.core.test_segment_reviewed_rules import REVIEWED


class EveryCandidate(WithoutSuffixReuse):
    @staticmethod
    def keep_scanning(result):
        if isinstance(result, _CandidateExtension):
            return None
        if isinstance(result, PendingBoundary) and result.reason == "waiting-first-feature":
            return None
        return result

    def _try_end(self, *args):
        return self.keep_scanning(super()._try_end(*args))

    def _try_end_r34(self, *args):
        return self.keep_scanning(super()._try_end_r34(*args))


def compare(values):
    fast, full = XdCalculator(), EveryCandidate()
    fast.calculate(values)
    full.calculate(values)
    assert line_state(fast.xds) == line_state(full.xds), "pruned_geometry_or_clock"
    assert fast.evidence == full.evidence, "pruned_evidence"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--walks", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=2026091702)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    started, checks, current, failure = perf_counter(), {}, None, None
    try:
        count = 0
        for name, (points, _) in {**CASES, **REVIEWED}.items():
            for mirror in (False, True):
                values = strokes(points, mirror)
                for end in range(3, len(values) + 1):
                    current = {"kind": "listed", "case": name, "points": points,
                               "mirror": mirror, "prefix_pens": end}
                    compare(values[:end])
                    count += 1
        checks["listed_prefixes"] = count
        count = 0
        for kind in ("origin_retest", "local_first_break"):
            for outcome in ("pending", "extension", "completed"):
                for mirror in (False, True):
                    points = local_containment_points(kind, 80, outcome)
                    current = {"kind": kind, "outcome": outcome, "mirror": mirror, "points": points}
                    compare(strokes(points, mirror))
                    count += 1
        checks["long_containment"] = count
        rng = random.Random(args.seed)
        for index in range(args.walks):
            points = [0]
            sign = rng.choice((-1, 1))
            for i in range(63):
                points.append(points[-1] + sign * (-1 if i % 2 else 1) * rng.randint(1, 12))
            current = {"kind": "random", "index": index, "points": points}
            compare(strokes(points))
        checks["random_walks"] = args.walks
        datasets = json.loads(args.inventory.read_text(encoding="utf-8"))["datasets"]
        for dataset in datasets:
            current = {"kind": "market", "id": dataset["id"], "file": dataset["file"]}
            cd = new_cl(dataset)
            cd.process_klines(read_frame(ROOT / dataset["file"]), last_bar_closed=True)
            compare(cd.get_contiguous_bis())
        checks["market_datasets"] = len(datasets)
    except Exception as exc:
        failure = {**current, "reason": repr(exc)}
    result = {
        "checks": checks, "seed": args.seed, "failure": failure,
        "seconds": perf_counter() - started,
        "scope": "ordinary/local first-feature candidate pruning and suffix reuse only; rule-mandated gap invalidation retained",
        "all_input_theory_equivalence_proven": False,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "script/check_segment_unpruned.py", "tests/core/test_segment_local_search.py",
                "script/validate_segment_corpus.py",
            )
        },
    }
    write_json(args.output, result)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(failure))


if __name__ == "__main__":
    main()
