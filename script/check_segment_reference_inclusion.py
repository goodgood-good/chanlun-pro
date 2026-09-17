"""Isolate reference inclusion from pivot selection; compare geometry and evidence."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import itertools
import json
from pathlib import Path
import random
import sys
from time import perf_counter
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import pandas as pd
from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator, _advance_left_reference
from script.check_segment_model import check_evidence, pen


OLD_REFERENCE_POLICY = '''def _advance_left_reference(reference, candidate, direction):
    previous = reference if isinstance(reference, dict) else _bi_to_cs_elem(reference)
    following = candidate if isinstance(candidate, dict) else _bi_to_cs_elem(candidate)
    advances = (following["high"] > previous["high"] if direction == "up"
                else following["low"] < previous["low"])
    if not advances:
        return previous
    if _has_inclusion(previous, following):
        return _merge_two(previous, following, direction)
    return following
'''


def old_policy_module():
    source = (ROOT / "src/chanlun/core/xd_calculator.py").read_text(encoding="utf-8")
    start = source.index("def _advance_left_reference(")
    end = source.index("\n\nclass XdCalculator:", start)
    restored = source[:start] + OLD_REFERENCE_POLICY + source[end:]
    module = types.ModuleType("segment_reference_filter_before_inclusion")
    sys.modules[module.__name__] = module
    exec(compile(restored, module.__name__, "exec"), module.__dict__)
    return module


def geometry(lines):
    return [
        (line.start_line.index, line.end_line.index, line.type, line.done,
         line.locked_at, line.formed_at, line.high, line.low, line.zs_high, line.zs_low)
        for line in lines
    ]


def compare(values, old_module):
    before, after = old_module.XdCalculator(), XdCalculator()
    old_lines, new_lines = before.calculate(values), after.calculate(values)
    assert geometry(old_lines) == geometry(new_lines), "geometry_or_confirmation_changed"
    assert before.tail_state == after.tail_state, "tail_state_changed"
    check_evidence(after, values)
    changes = []
    assert len(before.evidence) == len(after.evidence)
    for old, new in zip(before.evidence, after.evidence):
        a, b = asdict(old), asdict(new)
        old_left, new_left = a["first_sequence"][0], b["first_sequence"][0]
        a["first_sequence"] = a["first_sequence"][1:]
        b["first_sequence"] = b["first_sequence"][1:]
        assert a == b, "non_reference_evidence_changed"
        edge = "high" if new.direction == "up" else "low"
        assert old_left[edge] == new_left[edge], "directional_reference_edge_changed"
        assert set(old_left["source_indices"]).issubset(new_left["source_indices"])
        if old_left != new_left:
            changes.append({"key": new.key, "before": old_left, "after": new_left})
    return changes, len(new_lines)


def reference_sequences(old_module):
    intervals = list(itertools.combinations(range(4), 2))
    counts = {"prefixes": 0, "changed_intervals": 0, "changed_sources": 0}

    def source_indices(element):
        return tuple(p.index for p in element.get("merged_bis", [element["bi"]]))

    def visit(previous_old, previous_new, depth, direction):
        if depth == 6:
            return
        for low, high in intervals:
            item = pen(2 * depth + 1, high, low) if direction == "up" else pen(2 * depth + 1, low, high)
            old = old_module._advance_left_reference(previous_old or item, item, direction) if previous_old else {
                "bi": item, "low": low, "high": high,
            }
            new = _advance_left_reference(previous_new or item, item, direction) if previous_new else {
                "bi": item, "low": low, "high": high,
            }
            counts["prefixes"] += 1
            assert old["high" if direction == "up" else "low"] == new["high" if direction == "up" else "low"]
            assert new["low"] >= old["low"] if direction == "up" else new["high"] <= old["high"]
            old_sources, new_sources = source_indices(old), source_indices(new)
            assert set(old_sources).issubset(new_sources)
            counts["changed_intervals"] += (old["low"], old["high"]) != (new["low"], new["high"])
            counts["changed_sources"] += old_sources != new_sources
            visit(old, new, depth + 1, direction)

    for direction in ("up", "down"):
        visit(None, None, 0, direction)
    return counts


def main():
    old_module = old_policy_module()
    started = perf_counter()
    reference_checks = reference_sequences(old_module)
    rng = random.Random(19270922)
    changed = 0
    examples = []
    for _ in range(10000):
        points = [0]
        sign = rng.choice((-1, 1))
        for i in range(63):
            points.append(points[-1] + sign * (-1 if i % 2 else 1) * rng.randint(1, 40))
        values = [pen(i, a, b) for i, (a, b) in enumerate(zip(points, points[1:]))]
        changes, _ = compare(values, old_module)
        changed += len(changes)
        if changes and len(examples) < 3:
            examples.append({"points": points, "changes": changes})
    markets = []
    for filename, frequency, market in (
        ("SZ.002299_1m.parquet", "1m", "a"),
        ("SH.600519_5m.parquet", "5m", "a"),
        ("QQQ.US_30m.parquet", "30m", "us"),
    ):
        path = ROOT / "tests/fixtures" / filename
        cd = CL(filename.rsplit("_", 1)[0], frequency, {}, market=market)
        cd.process_klines(pd.read_parquet(path))
        changes, count = compare(cd.get_contiguous_bis(), old_module)
        markets.append({"file": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "segments": count, "changed_references": len(changes),
                        "geometry_times_and_ranges_unchanged": True, "changes": changes})
    result = {
        "comparison": "Restore only filtering-before-inclusion in the current calculator",
        "reference_sequences": reference_checks,
        "random_inputs": 10000, "random_points": 64, "seed": 19270922,
        "random_changed_references": changed, "random_examples": examples,
        "markets": markets, "seconds": perf_counter() - started, "failure": None,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
                         "src/chanlun/core/strict_structure/base_profile.py", "script/check_segment_model.py",
                         "script/check_segment_reference_inclusion.py")
        },
        "all_reference_eligibility_proven": False,
    }
    output = ROOT / "output/segment_audit/reference_inclusion.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {key: value for key, value in result.items() if key not in ("markets", "random_examples")}
    summary["markets"] = [{key: value for key, value in m.items() if key != "changes"} for m in markets]
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
