"""Replay the user's 2026-09-17 chart decisions through continuous OHLC and BI.

The expected boundaries are user acceptance cases, not a new source oracle.
Synthetic bars have no market gaps. The production pen rule is left unchanged.
"""

from dataclasses import asdict
import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_equal_return import source_frame, point_values, physical_confirmed
from script.check_segment_model import check_evidence
from tests.core.test_segment_reviewed_rules import REVIEWED
from tests.core.test_segment_source_rules import geometry


def main(output=None):
    started = perf_counter()
    results = []
    for name, (points, expected) in REVIEWED.items():
        # Extra future bars lock the reviewed pens. Acceptance geometry is
        # checked on exactly that fixed pen prefix, not on future prices.
        sign = 1 if len(points) % 2 else -1
        extra = [points[-1] + sign * offset
                 for i in range(4) for offset in (10 - 2 * i, -2 - 2 * i)]
        for mirror in (False, True):
            physical = [50 + (-p if mirror else p) for p in points + extra]
            frame = source_frame(physical)
            batch = CL("SYNTHETIC.REVIEWED.RULES", "1m", {}, market="a")
            batch.process_klines(frame, last_bar_closed=True)
            values = batch.get_contiguous_bis()
            assert point_values(values) == physical[:len(values) + 1], name
            count = len(points) - 1
            assert len(values) >= count and all(p.locked_at is not None for p in values[:count]), name
            assert not batch.get_stroke_construction_state()["unresolved_regions"], name
            fixed = XdCalculator()
            assert geometry(fixed.calculate(values[:count])) == expected, (name, mirror)
            check_evidence(fixed, values[:count])
            full = XdCalculator()
            assert geometry(full.calculate(values)) == geometry(batch.get_xds())
            check_evidence(full, values)

            running = CL("SYNTHETIC.REVIEWED.RULES", "1m", {}, market="a")
            previous = {}
            for row in frame.itertuples(index=False):
                running.process_kline_values(row.date, row.open, row.high, row.low,
                                            row.close, row.volume, bar_closed=True)
                current = physical_confirmed(running.get_xds())
                assert all(current.get(key) == when for key, when in previous.items()), (name, mirror, row.date)
                previous = current
            assert geometry(running.get_xds()) == geometry(batch.get_xds()), (name, mirror)
            assert physical_confirmed(running.get_xds()) == physical_confirmed(batch.get_xds()), (name, mirror)
            results.append({
                "case": name, "mirror": mirror, "bars": len(frame),
                "constructed_pens": len(values), "reviewed_pens": count,
                "reviewed_points": physical[:count + 1],
                "reviewed_geometry": geometry(fixed.calculate(values[:count])),
                "reviewed_proofs": [asdict(proof) for proof in fixed.evidence],
                "full_geometry": geometry(batch.get_xds()),
                "all_reviewed_pens_locked": True, "market_price_gaps": False,
                "confirmed_boundaries_and_times_preserved_on_every_bar": True,
                "incremental_matches_batch": True,
            })
    result = {
        "scope": "user-reviewed acceptance and no-gap OHLC input reachability",
        "decision_source": "user conversation, 2026-09-17; not an author quotation",
        "stroke_rule": STRICT_STROKE_MODE,
        "cases": results,
        "causal_bar_prefixes": sum(r["bars"] for r in results),
        "seconds": perf_counter() - started, "failure": None,
        "all_input_theory_equivalence_proven": False,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "src/chanlun/core/bi_calculator.py", "src/chanlun/core/cl.py",
                "src/chanlun/core/strict_structure/base_profile.py",
                "script/check_segment_model.py", "script/check_segment_equal_return.py",
                "script/check_segment_reviewed_rules.py", "tests/core/test_segment_reviewed_rules.py",
            )
        },
    }
    output = Path(output) if output is not None else ROOT / "output/segment_audit/reviewed_rules_ohlc.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "cases": len(results),
                      "causal_bar_prefixes": result["causal_bar_prefixes"],
                      "seconds": result["seconds"], "failure": None}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    main(parser.parse_args().output)
