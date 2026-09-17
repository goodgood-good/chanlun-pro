"""Audit the raw-first gap policy accepted by the user on 2026-09-17.

Production now uses the original first reversal pen. A controlled calculator
switches only the two ordinary gap expressions back to standard middle
intervals; other reviewed changes stay in place. This comparison is therefore
not the complete pre-review implementation. The archived earlier audit is
output/segment_audit/pre_review_20260917/script/check_segment_gap_classification.py.
The user decision is an acceptance rule, not a newly discovered author quote.
"""

from dataclasses import asdict
import hashlib
from itertools import combinations
import json
from pathlib import Path
import random
import sys
from time import perf_counter
import types

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.cl import CL
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_equal_return import point_values, source_frame
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import CASES, geometry, strokes


def raw_first_policy():
    """Compatibility for research callers: this is now production itself."""
    return XdCalculator


def standard_middle_policy():
    source = (ROOT / "src/chanlun/core/xd_calculator.py").read_text(encoding="utf-8")
    edits = {
        "has_gap = not _overlap(left, _bi_to_cs_elem(first))":
            "has_gap = not _overlap(left, mid)",
        "has_gap = not _overlap(left, first)":
            "has_gap = not _overlap(left, middle)",
    }
    for old, new in edits.items():
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    module = types.ModuleType("_controlled_standard_middle_gap")
    sys.modules[module.__name__] = module
    exec(compile(source, "<controlled standard-middle gap classification>", "exec"), module.__dict__)
    return module.XdCalculator


def strict_first_break_facts(points):
    """Independent inequalities for this nine-point family, normalized up.

    This intentionally contains no production inclusion/fractal helper calls.
    It verifies the specific context being discussed, not arbitrary inputs.
    """
    assert len(points) == 9
    origin, left_high, left_low, high, low, rebound, inside, last_high, end = points
    assert origin < left_low < left_high < high
    assert origin < end < low < left_low
    assert low < inside < rebound < last_high < high
    # First and third reversal pens overlap; the third lies wholly in the
    # first, then the fifth passes its end before any return past its start.
    middle = (max(low, inside), high)
    right = (end, last_high)
    assert right[0] < middle[0] and right[1] < middle[1]
    return {
        "all_prices_distinct": len(set(points)) == len(points),
        "first_pen_strictly_breaks_left_end": low < left_low,
        "third_reverse_pen_strictly_inside_first": low < inside < rebound < high,
        "fifth_reverse_pen_first_passes_initial_end": end < low,
        "no_return_to_or_beyond_first_start": max(rebound, last_high) < high,
        "left": [left_low, left_high],
        "raw_first": [low, high],
        "standard_middle_under_current_seed": list(middle),
        "first_independent_right": list(right),
        "raw_gap": low > left_high,
        "standard_gap": middle[0] > left_high,
        "second_sequence_raw_elements_at_first_fractal": [[low, rebound], [inside, last_high]],
        "second_fractal_possible_at_that_prefix": False,
    }


def compare(values, other, verify_current=True):
    current, alternative = XdCalculator(), other()
    current_lines = current.calculate(values)
    alternative_lines = alternative.calculate(values)
    if verify_current:
        # The checker validates the accepted raw-first policy. A comparison
        # with a different policy is not an independent source verdict.
        check_evidence(current, values)
    return {
        "current_geometry": geometry(current_lines),
        "controlled_standard_middle_geometry": geometry(alternative_lines),
        "current_tail": asdict(current.tail_state),
        "controlled_standard_middle_tail": asdict(alternative.tail_state),
        "changed_geometry": geometry(current_lines) != geometry(alternative_lines),
    }


def source_seed_constraints():
    """Constrain a proposed seed reversal with the author's L079 interval 36.

    Values are synthetic realizations of the figure's strict inequalities.
    The interval endpoint labels come from L079 lines 259-265, not its red
    editorial note about inclusion direction. This checks the stated context;
    it does not generalize a universal seed rule from four realizations.
    """
    source_figures = []
    for name in ("79_upper", "79_lower"):
        for mirror in (False, True):
            original = CASES[name][0]
            points = [-p for p in original] if mirror else original
            first = tuple(sorted((points[3], points[4])))
            third = tuple(sorted((points[5], points[6])))
            interval_36 = tuple(sorted((points[3], points[6])))
            take = max if mirror else min
            opposite = min if mirror else max
            original_direction = tuple(take(a, b) for a, b in zip(first, third))
            reversed_direction = tuple(opposite(a, b) for a, b in zip(first, third))
            assert original_direction == interval_36
            assert reversed_direction != interval_36
            calc = XdCalculator()
            calc.calculate(strokes(points))
            middle = calc.evidence[0].first_sequence[1]
            assert (middle.low, middle.high) == interval_36
            assert middle.source_indices == (3, 5)
            source_figures.append({
                "case": name, "mirror": mirror,
                "raw_34": first, "raw_56": third,
                "author_interval_36_in_this_realization": interval_36,
                "original_direction_fold": original_direction,
                "reversed_direction_fold": reversed_direction,
                "production_middle": asdict(middle),
            })

    disputed = []
    for mirror in (False, True):
        points = [0, 10, 6, 14, 4, 12, 11, 13, 3]
        if mirror:
            points = [-p for p in points]
        first = tuple(sorted((points[3], points[4])))
        third = tuple(sorted((points[5], points[6])))
        take = min if mirror else max
        opposite = max if mirror else min
        current = tuple(take(a, b) for a, b in zip(first, third))
        reversed_seed = tuple(opposite(a, b) for a, b in zip(first, third))
        extreme = 0 if mirror else 1
        assert current[extreme] == points[3]
        assert reversed_seed[extreme] != points[3]
        disputed.append({
            "mirror": mirror, "hypothesized_boundary_price": points[3],
            "current_fold": current, "reversed_seed_fold": reversed_seed,
            "reversed_seed_preserves_hypothesized_fractal_extreme": False,
        })
    return {
        "L079_author_interval_36": source_figures,
        "disputed_boundary_price": disputed,
        "universal_seed_rule_proven": False,
        "gap_policy_accepted_by_user": True,
        "unique_author_reading_proven": False,
    }


def main():
    started = perf_counter()
    other = standard_middle_policy()
    controls = []
    for inside in (9, 10, 11):
        points = [0, 10, 6, 14, 4, 12, inside, 13, 3]
        facts = strict_first_break_facts(points)
        assert facts["standard_gap"] == (inside > 10)
        for mirror in (False, True):
            record = compare(strokes(points, mirror), other)
            assert record["changed_geometry"] == (inside > 10)
            controls.append({"points": [-p for p in points] if mirror else points,
                             "mirror": mirror, "up_normalized_facts": facts, **record})

    # Exhaust all strictly ordered values for this one specified order family.
    # This is not an enumeration of all nine-point patterns.
    family = 0
    for levels in (9, 10, 11):
        for prices in combinations(range(levels), 9):
            points = [prices[i] for i in (0, 4, 3, 8, 2, 6, 5, 7, 1)]
            facts = strict_first_break_facts(points)
            assert facts["all_prices_distinct"] and facts["standard_gap"]
            assert not facts["raw_gap"]
            for mirror in (False, True):
                record = compare(strokes(points, mirror), other)
                assert record["current_geometry"] == [(0, 3, True), (3, 8, False)]
                assert record["controlled_standard_middle_geometry"] == [(0, 3, False)]
                family += 1

    extension_points = [0, 10, 6, 14, 4, 12, 11, 13, 3, 15, 8]
    extensions = []
    for mirror in (False, True):
        record = compare(strokes(extension_points, mirror), other)
        assert record["current_geometry"] == [(0, 3, True), (3, 8, False)]
        assert record["controlled_standard_middle_geometry"] == [(0, 9, False)]
        extensions.append({"mirror": mirror, **record})

    listed = []
    for name, (points, _) in CASES.items():
        for mirror in (False, True):
            record = compare(strokes(points, mirror), other)
            if record["changed_geometry"]:
                listed.append({"case": name, "mirror": mirror, **record})

    # Show this issue reaches the real pen path with fixed, locked inputs.
    points = [0, 10, 6, 14, 4, 12, 11, 13, 3, 12, 2, 11, 1, 10, 0, 9, -1, 8, -2, 7]
    pipelines = []
    for mirror in (False, True):
        physical = [40 - p for p in points] if mirror else [20 + p for p in points]
        frame = source_frame(physical)
        cd = CL("SYNTHETIC.FIRST.BREAK.GAP", "1m", {}, market="a")
        cd.process_klines(frame, last_bar_closed=True)
        values = cd.get_contiguous_bis()
        assert point_values(values) == physical[:len(values) + 1]
        assert len(values) >= 8 and all(p.locked_at is not None for p in values[:8])
        assert not cd.get_stroke_construction_state()["unresolved_regions"]
        fixed = compare(values[:8], other)
        assert fixed["current_geometry"] == [(0, 3, True), (3, 8, False)]
        assert fixed["controlled_standard_middle_geometry"] == [(0, 3, False)]
        full = compare(values, other)
        assert geometry(cd.get_xds()) == full["current_geometry"]
        pipelines.append({"mirror": mirror, "bars": len(frame), "pens": len(values),
                          "first_eight_pens_locked": True, "market_price_gaps": False,
                          "first_nine_physical_prices": physical[:9],
                          "fixed_eight_pen_comparison": fixed, "full_input_comparison": full})

    rng = random.Random(19270923)
    changed = 0
    for _ in range(10000):
        value = rng.randrange(32)
        points = [value]
        direction = rng.choice((-1, 1))
        for __ in range(63):
            value += direction * rng.randrange(1, 17)
            points.append(value)
            direction = -direction
        changed += compare(strokes(points), other)["changed_geometry"]

    markets = []
    for filename, frequency, market in (
        ("SZ.002299_1m.parquet", "1m", "a"),
        ("SH.600519_5m.parquet", "5m", "a"),
        ("QQQ.US_30m.parquet", "30m", "us"),
    ):
        path = ROOT / "tests/fixtures" / filename
        cd = CL(filename.rsplit("_", 1)[0], frequency, {}, market=market)
        cd.process_klines(pd.read_parquet(path))
        values = cd.get_contiguous_bis()
        record = compare(values, other)
        markets.append({"file": filename, "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "pens": len(values), **record})

    result = {
        "scope": "accepted raw-first policy versus controlled standard-middle classification",
        "decision_source": "user conversation, 2026-09-17; chart 01 selects B",
        "audit_script_mutates_production": False,
        "production_gap_policy": "raw-first, accepted by user",
        "stroke_rule": STRICT_STROKE_MODE,
        "source_seed_constraints": source_seed_constraints(),
        "controls": controls, "strict_order_family_mirrors": family,
        "extensions": extensions,
        "listed_case_variants": len(CASES) * 2, "changed_listed_cases": listed,
        "pipelines": pipelines,
        "random_complete_inputs": 10000, "random_seed": 19270923,
        "random_changed_geometry": changed,
        "markets": markets,
        "alternative_source_equivalence_proven": False,
        "current_source_equivalence_proven": False,
        "failure": None,
        "seconds": perf_counter() - started,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                "src/chanlun/core/bi_calculator.py", "src/chanlun/core/cl.py",
                "src/chanlun/core/strict_structure/base_profile.py",
                "script/check_segment_model.py", "script/check_segment_equal_return.py",
                "script/check_segment_gap_classification.py", "tests/core/test_segment_source_rules.py",
            )
        },
    }
    output = ROOT / "output/segment_audit/gap_classification_reviewed.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: result[key] for key in (
        "strict_order_family_mirrors", "listed_case_variants", "random_complete_inputs",
        "random_changed_geometry", "seconds", "audit_script_mutates_production",
    )}
    summary["changed_listed_cases"] = [(item["case"], item["mirror"]) for item in listed]
    summary["source_seed_constraints"] = {
        "author_figure_realizations": len(result["source_seed_constraints"]["L079_author_interval_36"]),
        "disputed_boundary_mirrors": len(result["source_seed_constraints"]["disputed_boundary_price"]),
        "gap_policy_accepted_by_user": True,
        "unique_author_reading_proven": False,
    }
    summary["pipelines"] = [{key: row[key] for key in ("mirror", "bars", "pens")} for row in pipelines]
    summary["markets"] = [{"file": row["file"], "current_segments": len(row["current_geometry"]),
                           "controlled_standard_middle_segments": len(row["controlled_standard_middle_geometry"]),
                           "changed_geometry": row["changed_geometry"]} for row in markets]
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
