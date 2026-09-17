"""Reproduce source-figure, causal replay and local-market segment checks.

Run from the repository root with the project's Python environment. The HEAD
implementation is a comparison baseline, never a theoretical oracle. Synthetic
walk invariants check engineering properties, not all-input Chan equivalence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
from time import perf_counter
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from tests.core.test_segment_source_rules import CASES, geometry, strokes


def previous_calculator():
    source = subprocess.check_output(
        ["git", "show", "HEAD:src/chanlun/core/xd_calculator.py"],
        cwd=ROOT,
    ).decode("utf-8")
    module = types.ModuleType("segment_audit_previous")
    sys.modules[module.__name__] = module
    exec(compile(source, "HEAD:xd_calculator.py", "exec"), module.__dict__)
    return module.XdCalculator


def causal_walks(count):
    rng = random.Random(1927)
    prefixes = 0
    for case in range(count):
        points = [50]
        for index in range(32):
            points.append(
                points[-1] + (1 if index % 2 == 0 else -1) * rng.randint(1, 20)
            )
        values = strokes(points)
        frozen = {}
        calculator = XdCalculator()
        for end in range(3, len(values) + 1):
            lines = calculator.calculate(values[:end])
            current = {
                (x.start_line.index, x.end_line.index): x.locked_at
                for x in lines
                if x.done
            }
            if not all(current.get(key) == when for key, when in frozen.items()):
                raise AssertionError((case, end, points, frozen, current))
            for a, b in zip(lines, lines[1:]):
                assert a.end_line.index + 1 == b.start_line.index and a.type != b.type
            assert geometry(lines) == geometry(XdCalculator().calculate(values[:end]))
            frozen = current
            prefixes += 1
    return {"seed": 1927, "walks": count, "prefixes": prefixes, "passed": True}


def market_checks(old):
    import pandas as pd

    result = []
    for filename, frequency, market in (
        ("SZ.002299_1m.parquet", "1m", "a"),
        ("SH.600519_5m.parquet", "5m", "a"),
        ("QQQ.US_30m.parquet", "30m", "us"),
    ):
        path = ROOT / "tests" / "fixtures" / filename
        frame = pd.read_parquet(path)
        cd = CL(filename.rsplit("_", 1)[0], frequency, {}, market=market)
        cd.process_klines(frame)
        values = cd.get_contiguous_bis()
        previous = old().calculate(values)
        current = cd.get_xds()
        fixed = {}
        for end in sorted(set(range(3, len(values) + 1, 37)) | {len(values)}):
            now = XdCalculator().calculate(values[:end])
            locked = {
                (x.start_line.index, x.end_line.index): x.locked_at
                for x in now
                if x.done
            }
            assert all(locked.get(key) == when for key, when in fixed.items())
            fixed = locked
        result.append(
            {
                "file": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bars": len(frame),
                "pens": len(values),
                "previous_segments": len(previous),
                "current_segments": len(current),
                "previous_confirmed": sum(x.done for x in previous),
                "current_confirmed": sum(x.done for x in current),
                "fixed_pen_prefix_replay": "passed",
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--walks", type=int, default=3000)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output/segment_audit/audit_results.json"
    )
    args = parser.parse_args()
    old = previous_calculator()
    cases = []
    for name, (points, expected) in CASES.items():
        calculator = XdCalculator()
        current = geometry(calculator.calculate(strokes(points)))
        assert current == expected, name
        cases.append(
            {
                "name": name,
                "points": points,
                "expected": expected,
                "previous": geometry(old().calculate(strokes(points))),
                "current": current,
                "current_evidence": [asdict(p) for p in calculator.evidence],
                "tail_state": asdict(calculator.tail_state),
            }
        )
    timings = []
    for repeat in (200, 2000, 20000):
        points = [0, 10, 6, 20, 8] + [v for _ in range(repeat) for v in (18, 9)]
        for suffix, expected in (([], False), ([17, 5], True), ([21, 15], False)):
            values = strokes(points + suffix)
            started = perf_counter()
            lines = XdCalculator().calculate(values)
            seconds = perf_counter() - started
            assert lines[0].done is expected
            timings.append(
                {
                    "pens": len(values),
                    "suffix": suffix,
                    "seconds": seconds,
                    "confirmed": lines[0].done,
                }
            )
    result = {
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py",
                "src/chanlun/core/segment_evidence.py",
                "src/chanlun/core/strict_structure/base_profile.py",
                "src/chanlun/core/types/line.py",
                "src/chanlun/core/cl.py",
            )
        },
        "baseline_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT
        )
        .decode()
        .strip(),
        "source_scope": "local original under D:/\u7f20\u8bba; figures realized as synthetic legal-pen inputs",
        "market_price_gaps": "not covered; feature-interval gaps retained",
        "all_input_theory_equivalence_proven": False,
        "unreconstructed_original_market_examples": [
            "L069:32-33",
            "L076:74/75 question",
            "L068:17-18",
        ],
        "figures": cases,
        "causal_walks": causal_walks(args.walks),
        "long_containment": timings,
        "local_market_replay": market_checks(old),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "causal_walks": result["causal_walks"],
                "market": result["local_market_replay"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
