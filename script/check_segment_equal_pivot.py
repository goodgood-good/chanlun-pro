"""Audit the distinction between stem price provenance and a valid segment end.

The L079/L075/L071 deduction is documented in segment_contained_pivot_audit.md.
The comparator restores the removed veto; it is not a theory oracle. The
finite search fixes an explicit four-point prefix and enumerates all further
alternating prices on six levels through thirteen points, with both mirrors.
"""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_equal_return import physical_confirmed, point_values, source_frame
from script.check_segment_model import check_evidence, pen, signature
from tests.core.test_segment_source_rules import CASES, geometry, strokes


class WithFormerStemSourceVeto(XdCalculator):
    def _try_end_r34(self, values, start, direction, high, low, check, pivot_start=None):
        if check >= start + 3 and values[check - 1].end.val == values[check - 3].end.val:
            return None
        return super()._try_end_r34(values, start, direction, high, low, check, pivot_start)


def different_stem_supplier(proof):
    stem = proof.pivot_stem
    if stem is None:
        return False
    source = stem.high_source_index if proof.direction == "up" else stem.low_source_index
    return source != proof.end_index


def fixed_prefix_search():
    checked = distinct = differences = 0
    first_examples = []
    started = perf_counter()

    def visit(points, values, previous, mirror):
        nonlocal checked, distinct, differences
        calc = XdCalculator()
        lines = calc.calculate(values)
        current = signature(lines)
        assert all(current.get(key) == when for key, when in previous.items()), points
        assert all(
            when == values[-1].locked_at
            for key, when in current.items() if key not in previous
        ), points
        check_evidence(calc, values)
        checked += 1
        if any(different_stem_supplier(proof) for proof in calc.evidence):
            distinct += 1
            before = geometry(WithFormerStemSourceVeto().calculate(values))
            if before != geometry(lines):
                differences += 1
                if len(first_examples) < 4:
                    first_examples.append({"points": points, "before": before, "after": geometry(lines)})
        if len(points) == 13:
            return
        up = (len(values) % 2 == 0) != mirror
        for price in range(6):
            if price > points[-1] if up else price < points[-1]:
                visit(points + [price], values + [pen(len(values), points[-1], price)], current, mirror)

    for mirror in (False, True):
        prefix = [5 - p for p in (0, 5, 1, 3)] if mirror else [0, 5, 1, 3]
        values = [pen(i, a, b) for i, (a, b) in enumerate(zip(prefix, prefix[1:]))]
        visit(prefix, values, {}, mirror)
    assert distinct > 0 and differences > 0
    return {
        "fixed_prefix": [0, 5, 1, 3], "levels": list(range(6)), "mirrors": True,
        "maximum_points": 13, "checked_prefixes": checked,
        "prefixes_with_distinct_stem_supplier": distinct,
        "prefixes_different_from_former_veto": differences,
        "examples": first_examples, "seconds": perf_counter() - started,
        "all_unrestricted_six_level_inputs_covered": False,
    }


def pipeline_example():
    records = []
    for mirror in (False, True):
        points = CASES["79_equal_contained_pivot_has_a_later_break_context"][0] + [9, 4, 8, 3, 7]
        points = [40 - p for p in points] if mirror else points
        frame = source_frame(points)
        cd = CL("SYNTHETIC.EQUAL.PIVOT", "1m", {}, market="a")
        cd.process_klines(frame, last_bar_closed=True)
        values = cd.get_contiguous_bis()
        assert point_values(values) == points[:len(values) + 1]
        assert not cd.get_stroke_construction_state()["unresolved_regions"]
        calc = XdCalculator()
        lines = calc.calculate(values)
        check_evidence(calc, values)
        assert geometry(lines) == geometry(cd.get_xds())
        assert geometry(lines)[:2] == [(0, 3, True), (3, 12, True)]
        previous = {}
        running = CL("SYNTHETIC.EQUAL.PIVOT", "1m", {}, market="a")
        for row in frame.itertuples(index=False):
            running.process_kline_values(
                row.date, row.open, row.high, row.low, row.close, row.volume, bar_closed=True,
            )
            current = physical_confirmed(running.get_xds())
            assert all(current.get(key) == when for key, when in previous.items())
            previous = current
        assert physical_confirmed(running.get_xds()) == physical_confirmed(cd.get_xds())
        assert geometry(running.get_xds()) == geometry(lines)
        records.append({
            "mirror": mirror, "bars": len(frame), "pens": len(values),
            "points": point_values(values), "before": geometry(WithFormerStemSourceVeto().calculate(values)),
            "after": geometry(lines), "proof": asdict(calc.evidence[1]),
            "all_bar_prefixes_preserve_confirmed_boundaries": True,
            "incremental_and_batch_agree": True, "market_price_gaps": False,
        })
    return records


def market_comparison():
    records = []
    for filename, frequency, market in (
        ("SZ.002299_1m.parquet", "1m", "a"),
        ("SH.600519_5m.parquet", "5m", "a"),
        ("QQQ.US_30m.parquet", "30m", "us"),
    ):
        path = ROOT / "tests/fixtures" / filename
        cd = CL(filename.rsplit("_", 1)[0], frequency, {}, market=market)
        cd.process_klines(pd.read_parquet(path))
        values = cd.get_contiguous_bis()
        calc = XdCalculator()
        after = calc.calculate(values)
        check_evidence(calc, values)
        before = WithFormerStemSourceVeto().calculate(values)
        old_keys = {(x.start_line.index, x.end_line.index, x.type) for x in before}
        new_keys = {(x.start_line.index, x.end_line.index, x.type) for x in after}
        records.append({
            "file": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "before_segments": len(before), "after_segments": len(after),
            "before_confirmed": sum(x.done for x in before), "after_confirmed": sum(x.done for x in after),
            "new_intervals": len(new_keys - old_keys), "removed_intervals": len(old_keys - new_keys),
            "same_geometry_and_times": geometry(before) == geometry(after)
            and [x.locked_at for x in before] == [x.locked_at for x in after],
            "distinct_stem_supplier_proofs": [asdict(p) for p in calc.evidence if different_stem_supplier(p)],
        })
    return records


def main():
    examples = []
    for name in (
        "79_equal_contained_pivot_has_a_later_break_context",
        "79_equal_contained_pivot_on_six_price_levels",
    ):
        for mirror in (False, True):
            points, expected = CASES[name]
            values = strokes(points, mirror)
            calc = XdCalculator()
            after = calc.calculate(values)
            assert geometry(after) == expected
            check_evidence(calc, values)
            examples.append({
                "name": name, "mirror": mirror, "points": points,
                "before": geometry(WithFormerStemSourceVeto().calculate(values)),
                "after": geometry(after), "evidence": [asdict(p) for p in calc.evidence],
            })
    bounded = fixed_prefix_search()
    print(json.dumps({"fixed_prefix_search": bounded}), flush=True)
    result = {
        "comparison": "current code versus restoring only the former stem-source veto",
        "failure": None, "all_input_theory_equivalence_proven": False,
        "examples": examples, "fixed_prefix_search": bounded,
        "pipeline": pipeline_example(), "markets": market_comparison(),
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "src/chanlun/core/strict_structure/base_profile.py", "src/chanlun/core/bi_calculator.py",
                "src/chanlun/core/cl.py", "script/check_segment_model.py",
                "script/check_segment_equal_return.py", "script/check_segment_equal_pivot.py",
            )
        },
    }
    output = ROOT / "output/segment_audit/equal_contained_pivot.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "markets": result["markets"], "pipeline": result["pipeline"]}))


if __name__ == "__main__":
    main()
