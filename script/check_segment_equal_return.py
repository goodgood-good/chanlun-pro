"""Reproduce the exact-return boundary without treating either reading as truth.

L071 lines 100-124 describes a first pen break and subsequent extension. L065
lines 34-64, L067 lines 49-76 and L075 lines 406-424 require chronological
standardization, including equal-bound intervals. The equality between the
rebound high and the first reversal's start needs an explicit reconciliation.
This script records the current result and verifies input reachability through
the unchanged production BI path; it does not assert a new author rule.
"""

from dataclasses import asdict
import hashlib
import json
from math import ceil
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import geometry, strokes


def source_frame(points):
    """Continuous OHLC bars with long monotone legs and exact turning prices.

    Each candle opens at the previous close. Consecutive ranges overlap at
    that value. No market gap, imported BI or modified pen rule is used.
    """
    direction = 1 if points[1] > points[0] else -1
    path = [points[0] + 2 * direction, *points]
    prices = [path[0]]
    for first, last in zip(path, path[1:]):
        count = max(8, ceil(abs(last - first) * 4))
        prices.extend(first + (last - first) * i / count for i in range(1, count + 1))
    records = []
    for i, (first, last) in enumerate(zip(prices, prices[1:])):
        records.append({
            "code": "SYNTHETIC.EQUAL.RETURN",
            "date": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=i),
            "open": first, "high": max(first, last), "low": min(first, last),
            "close": last, "volume": 100,
        })
    result = pd.DataFrame(records)
    assert all(result.open.iloc[i] == result.close.iloc[i - 1] for i in range(1, len(result)))
    return result


def point_values(values):
    return [values[0].start.val, *(value.end.val for value in values)] if values else []


def physical_confirmed(lines):
    return {
        (line.start.k.k_index, line.end.k.k_index, line.type): line.locked_at
        for line in lines if line.done
    }


def compare():
    direct = []
    for rebound in (13, 14, 15):
        points = [0, 10, 6, 14, 4, rebound, 3]
        for mirror in (False, True):
            values = strokes(points, mirror)
            calc = XdCalculator()
            lines = calc.calculate(values)
            check_evidence(calc, values)
            # These are independently evaluated input facts, not an oracle
            # deciding whether the author's word "break" includes a touch.
            left = (6, 10)
            first = (4, 14)
            third = (3, rebound)
            facts = {
                "first_strictly_contains_left": first[0] < left[0] and first[1] > left[1],
                "third_strictly_passes_first_end": third[0] < first[0],
                "three_reverse_pens_overlap": max(first[0], third[0]) <= min(first[1], third[1]),
                "rebound_strictly_passes_first_start": rebound > first[1],
                "rebound_equals_first_start": rebound == first[1],
                "first_third_inclusion": (
                    first[0] <= third[0] and third[1] <= first[1]
                ) or (third[0] <= first[0] and first[1] <= third[1]),
            }
            assert all(facts[key] for key in (
                "first_strictly_contains_left", "third_strictly_passes_first_end",
                "three_reverse_pens_overlap",
            ))
            direct.append({
                "points": [-p for p in points] if mirror else points,
                "mirror": mirror, "input_facts": facts,
                "current_geometry": geometry(lines),
                "tail": asdict(calc.tail_state),
                "evidence": [asdict(proof) for proof in calc.evidence],
            })

    repeated = [0, 10, 6, 14, 4]
    for low in (3, 2, 1, -1, -2, -3, -4, -5):
        repeated.extend((14, low))
    completed = repeated + [12, -6, 11, -7, 10]
    pipeline = []
    for mirror in (False, True):
        phases = {}
        for name, points in (("equal_returns_only", repeated), ("lower_right_added", completed)):
            points = [40 - p for p in points] if mirror else points
            frame = source_frame(points)
            cd = CL("SYNTHETIC.EQUAL.RETURN", "1m", {}, market="a")
            cd.process_klines(frame, last_bar_closed=True)
            values = cd.get_contiguous_bis()
            assert point_values(values) == points[:len(values) + 1]
            assert len(values) >= 18 and all(value.locked_at is not None for value in values[:6])
            assert not cd.get_stroke_construction_state()["unresolved_regions"]
            calc = XdCalculator()
            lines = calc.calculate(values)
            assert geometry(lines) == geometry(cd.get_xds())
            check_evidence(calc, values)
            phases[name] = {
                "bars": len(frame), "pens": len(values), "points": point_values(values),
                "first_six_pens_locked": True, "market_price_gaps": False,
                "unresolved_stroke_regions": [], "current_geometry": geometry(lines),
                "evidence": [asdict(proof) for proof in calc.evidence],
            }

        frame = source_frame([40 - p for p in completed] if mirror else completed)
        running = CL("SYNTHETIC.EQUAL.RETURN", "1m", {}, market="a")
        previous = {}
        for row in frame.itertuples(index=False):
            running.process_kline_values(
                row.date, row.open, row.high, row.low, row.close, row.volume, bar_closed=True,
            )
            current = physical_confirmed(running.get_xds())
            assert all(current.get(key) == when for key, when in previous.items())
            previous = current
        assert geometry(running.get_xds()) == geometry(cd.get_xds())
        assert physical_confirmed(running.get_xds()) == physical_confirmed(cd.get_xds())
        pipeline.append({
            "mirror": mirror, "phases": phases, "causal_bar_prefixes": len(frame),
            "confirmed_physical_prefix_and_batch_agreement": True,
        })

    result = {
        "scope": "input reachability and current behavior; not a source equivalence verdict",
        "stroke_rule": STRICT_STROKE_MODE,
        "unqualified_L071_shortcut_justified_at_exact_return": False,
        "production_logic_changed": False,
        "direct": direct, "pipeline": pipeline,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "src/chanlun/core/bi_calculator.py", "src/chanlun/core/cl.py",
                "src/chanlun/core/strict_structure/base_profile.py",
                "script/check_segment_model.py", "script/check_segment_equal_return.py",
            )
        },
    }
    output = ROOT / "output/segment_audit/equal_return_boundary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "direct_variants": len(direct), "pipeline": pipeline}))


if __name__ == "__main__":
    compare()
