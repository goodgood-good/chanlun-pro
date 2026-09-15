"""Read-only architecture audit of v11 old strokes.

Original definitions are read only from --source-root. Synthetic inputs,
diagnostic graph checks, and deliberately weakened selectors are engineering
experiments; none is an oracle for the author's final whole-chart partition.
The weakened selectors exist only in this process and never enter production.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import random
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_stroke_rule_logic import (
    COMPLETION_RETRACTION, independent_definitions, input_properties, long_origin_wait,
    path_conditions, production, raw_bars, reflected, snapshot,
)
from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core import stroke_sequence
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def read_originals(root):
    specifications = [
        (62, "author_body", [(37, 118)]),
        (65, "body_with_separately_identified_compiler_notes", [(31, 100), (166, 214)]),
        (66, "author_reply_not_lesson_body", [(256, 280)]),
        (69, "author_body", [(28, 49), (151, 184), (205, 223)]),
        (77, "body_with_separately_identified_compiler_notes", [(118, 148), (169, 295)]),
    ]
    result = []
    for number, kind, spans in specifications:
        candidates = list((root / "chanlun_lesson_corpus").glob(f"L{number:03d}_*.md"))
        if len(candidates) != 1:
            raise ValueError(f"expected one local source for lesson {number}")
        path = candidates[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        result.append(dict(
            **file_record(path), kind=kind, line_ranges=spans,
            excerpts=[dict(start=a, end=b, lines=[
                {"line": i, "text": lines[i - 1]} for i in range(a, b + 1)
                if lines[i - 1] and not lines[i - 1].startswith("<!--")
            ]) for a, b in spans],
        ))
    return result


def audit_only_no_wait_selector():
    original = textwrap.dedent(inspect.getsource(stroke_sequence.SourceStrokeSelection.append))
    gate = "if parent >= 0 and (connected or not retained_can_continue):"
    if original.count(gate) != 1:
        raise ValueError("production wait gate changed; review the experiment before running")
    namespace = dict(vars(stroke_sequence))
    exec(compile(original.replace(gate, "if parent >= 0:"),
                 "<audit-only-origin-wait-disabled>", "exec"), namespace)
    return type("AuditOnlyNoWait", (stroke_sequence.SourceStrokeSelection,),
                {"append": namespace["append"]})


def selector_trace(prices, selector_class, check_range=True):
    points, local_valid = independent_definitions(prices)

    def geometry_only(first, second):
        if first.type == second.type or second.k.index - first.k.index < 4:
            return False
        top, bottom = (first, second) if first.type == "ding" else (second, first)
        return all(a > b for a, b in zip(prices[top.k.index], prices[bottom.k.index]))

    seq, selection = stroke_sequence.SourceStrokeSequence(), selector_class()
    changes = []
    for fx in points:
        previous = [points[i].k.index for i in selection.path()]
        relation = seq.append(fx, local_valid if check_range else geometry_only)
        decision = selection.append(relation, seq)
        if decision.changed:
            changes.append(dict(
                endpoint_center=fx.k.index,
                observed_raw_bar=fx.k.index + 1,
                before=previous,
                after=[points[i].k.index for i in selection.path()],
                action=decision.action,
            ))
    centers = [points[i].k.index for i in selection.path()]
    edges = path_conditions(prices, centers)
    for edge in edges:
        a, b = edge["start"], edge["end"]
        edge["endpoint_high"] = max(points[i].val for i in selection.path()
                                     if points[i].k.index in (a, b))
        edge["endpoint_low"] = min(points[i].val for i in selection.path()
                                    if points[i].k.index in (a, b))
        edge["interval_high"] = max(h for h, _ in prices[a:b + 1])
        edge["interval_low"] = min(low for _, low in prices[a:b + 1])
    return dict(path=centers, edges=edges, changes=changes,
                pending_continuation=selection.pending_continuation)


def controlled_experiments():
    prices = long_origin_wait(0)
    prices[12:15] = [(26, 8), (28, 24), (27, 12)]
    weakened = audit_only_no_wait_selector()
    result = []
    for mirror in (False, True):
        values = reflected(prices, mirror)
        properties = input_properties(values)
        assert not properties["gaps"] and not properties["inclusions"]
        baseline = selector_trace(values, stroke_sequence.SourceStrokeSelection)
        real = production(values)
        assert baseline["path"] == [real["strokes"][0]["start"],
                                    *[b["end"] for b in real["strokes"]]]
        no_wait = selector_trace(values, weakened)
        no_range = selector_trace(values, stroke_sequence.SourceStrokeSelection, False)
        assert baseline["path"] == [1, 5, 9]
        assert no_wait["path"] == [15, 19, 23, 27, 31]
        assert no_range["path"] == [1, 11, 19, 23, 27, 31]
        assert [e["start"] for e in no_range["edges"] if not e["locally_legal"]] == [11]
        result.append(dict(mirror=mirror, prices=values, properties=properties,
                           production=real, baseline=baseline,
                           audit_only_wait_disabled=no_wait,
                           audit_only_range_disabled=no_range))
    return result


def historical_prefix_reselection():
    """Extend the old qualified prefix, then append the same eight-bar suffix.

    This demonstrates the unbounded scope of CURRENT selection retraction.
    It does not label every prior candidate author-proven permanently complete.
    """
    cycle = [(18, 14), (16, 12), (14, 10), (12, 8),
             (14, 10), (16, 12), (18, 14), (20, 16)]
    result = []
    for repeats in (0, 16, 64, 256):
        prices = COMPLETION_RETRACTION[:2] + cycle * repeats + COMPLETION_RETRACTION[2:]
        for mirror in (False, True):
            values = reflected(prices, mirror)
            properties = input_properties(values)
            assert not properties["gaps"] and not properties["inclusions"]
            before_size = 11 + repeats * 8
            before, after = production(values[:before_size]), production(values)
            raw = raw_bars(values)
            merged, calc = CL_Kline_Process(), BiCalculator()
            checked = []
            for count in range(1, len(raw) + 1):
                merged.process_cl_klines(raw[:count])
                calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                               validated_incremental_prefix=True)
                if count in (before_size, len(raw)):
                    assert snapshot(merged, calc) == (before if count == before_size else after)
                    checked.append(count)
            assert len(before["strokes"]) == 2 * repeats + 2
            assert sum(b["is_done"] for b in before["strokes"]) == 2 * repeats + 1
            assert [(b["start"], b["end"]) for b in after["strokes"]] == [
                (13 + repeats * 8, 17 + repeats * 8),
            ]
            assert not before["range_audit"] and not before["adjacency_audit"]
            assert not after["range_audit"] and not after["adjacency_audit"]
            result.append(dict(
                repeats=repeats, mirror=mirror, prices=values, properties=properties,
                before_raw_bars=before_size, appended_raw_bars=len(values) - before_size,
                before=before, after=after, incremental_prefixes=len(raw),
                cold_comparisons_at=checked, cold_incremental_equal=True,
            ))
    return result


def generated_prices(seed, variable_width):
    rng = random.Random(seed)
    high, low = 1000, 996
    prices = [(high, low)]
    for leg in range(40):
        direction = 1 if leg % 2 else -1
        for _ in range(rng.randint(1, 8)):
            if variable_width:
                candidates = [
                    (high + direction * a, low + direction * b)
                    for a in (1, 2, 3) for b in (1, 2, 3)
                    if 1 <= high + direction * a - low - direction * b <= 16
                    and high + direction * a >= low and low + direction * b <= high
                ]
                high, low = rng.choice(candidates)
            else:
                delta = direction * rng.choice((1, 2, 3))
                high, low = high + delta, low + delta
            prices.append((high, low))
    return prices


def inspect_generated_cases(seeds, variable_width):
    """Check local relations and search one specific single-parent failure.

    Set-based graph reachability checks the SAME-ENDPOINT subdivision predicate,
    not original-text global adjacency. Independent price checks enumerate all
    earlier fractals; they do not use production price frontiers or stroke_rules.
    """
    counts = dict(seeds=seeds, variable_width=variable_width, fractal_events=0,
                  local_pairs_checked=0, omitted_predecessors_checked=0,
                  local_relation_mismatches=0, single_parent_counterexamples=[])
    for seed in range(seeds):
        prices = generated_prices(seed, variable_width)
        properties = input_properties(prices)
        assert not properties["gaps"] and not properties["inclusions"]
        points, valid = independent_definitions(prices)
        sequence = stroke_sequence.SourceStrokeSequence()
        selection = stroke_sequence.SourceStrokeSelection()
        reachable = []
        for j, fx in enumerate(points):
            eligible = tuple(i for i in range(j) if valid(points[i], fx))
            counts["local_pairs_checked"] += j
            # An explicit set for each prior endpoint independently certifies
            # whether a longer qualified path has these exact two endpoints.
            indirect = set().union(*(reachable[i] for i in eligible))
            expected_irreducible = tuple(i for i in eligible if i not in indirect)
            reachable.append(indirect | set(eligible))
            relation = sequence.append(fx, valid)
            if relation.eligible != eligible or relation.adjacent != expected_irreducible:
                counts["local_relation_mismatches"] += 1
                raise AssertionError((seed, j, eligible, relation))
            retained = selection.path()
            if retained:
                _, omitted = selection._retained_predecessors(
                    relation.adjacent, retained, endpoint=j, sequence=sequence,
                )
                counts["omitted_predecessors_checked"] += len(omitted)
                if omitted:
                    forbidden = set(range(retained[0], retained[-1] + 1)) - set(retained)
                    # Can a rejected candidate be reached from this origin by
                    # another history avoiding the current omission interior?
                    paths = {retained[0]: [retained[0]]}
                    for end in range(retained[0] + 1, j):
                        if end in forbidden:
                            continue
                        for start in sequence.relations[end].adjacent:
                            if start in paths:
                                paths[end] = paths[start] + [end]
                                break
                    for reason in omitted:
                        if reason.candidate in paths:
                            counts["single_parent_counterexamples"].append(dict(
                                seed=seed, endpoint=j, retained=retained,
                                candidate=reason.candidate,
                                stored_path=selection.path(reason.candidate),
                                alternative_path=paths[reason.candidate], prices=prices,
                            ))
            selection.append(relation, sequence)
            counts["fractal_events"] += 1
    return counts


def historical_evidence():
    path = ROOT / "output/bi_code_review/v11_dependencies_final.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    events = [dict(input=m["input"], **c) for m in data["markets"] for c in m["cases"]]
    return dict(
        kind="previous_run_records_reinspected_this_audit_not_rerun",
        artifact=file_record(path), revision_events=len(events),
        events_removing_confirmed_segments=sum(bool(c["removed_confirmed_segments"]) for c in events),
        events_changing_confirmed_segments=sum(bool(c["changed_confirmed_segments"]) for c in events),
        events_removing_formal_centers=sum(bool(c["removed_formal_centers"]) for c in events),
        recorded_errors=sum(len(c["errors"]) for c in events),
        removed_segment_examples=[c for c in events if c["removed_confirmed_segments"]],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--constant-seeds", type=int, default=2000)
    parser.add_argument("--variable-seeds", type=int, default=5000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = read_originals(args.source_root)
    prior = ROOT / "output/bi_code_review/v11_current_review_manifest.json"
    previous = json.loads(prior.read_text(encoding="utf-8"))
    production_files = [file_record(x["path"]) for x in previous["production_code"]]
    if production_files != previous["production_code"]:
        raise ValueError("production files differ from the v11 review baseline")
    source_hashes = {x["path"]: x["sha256"] for x in sources}
    for source in previous["original_sources"]:
        if source["path"] in source_hashes:
            assert source_hashes[source["path"]] == source["sha256"]
    experiments = controlled_experiments()
    print("Controlled ablations reproduced in both price directions.", flush=True)
    prefix_reselection = historical_prefix_reselection()
    print("Historical-prefix replacement reproduced in eight mirrored cases.", flush=True)
    searches = []
    for count, variable in ((args.constant_seeds, False), (args.variable_seeds, True)):
        searches.append(inspect_generated_cases(count, variable))
        print(json.dumps({k: v for k, v in searches[-1].items()
                          if k != "single_parent_counterexamples"}), flush=True)
    assert production_files == [file_record(x["path"]) for x in production_files]
    result = dict(
        created_at=datetime.now(timezone.utc).isoformat(), profile=STRICT_STROKE_MODE,
        original_sources=sources, production_code=production_files,
        production_unchanged=True, prior_review=file_record(prior),
        audit_code=[file_record(__file__), file_record(ROOT / "script/review_stroke_rule_logic.py")],
        controlled_experiments=experiments, generated_case_checks=searches,
        historical_prefix_reselection=prefix_reselection,
        previous_dependency_evidence=historical_evidence(),
        limits=[
            "Original quotations, compiler notes, and engineering deductions are distinct.",
            "No new-pen definition; no internet or other directory used as original authority.",
            "Passing local geometry and same-endpoint subdivision checks does not prove whole-chart adjacency.",
            "Failure to find the specified single-parent counterexample is not a proof of selector completeness.",
            "Weakened selectors are diagnostic ablations, never proposed source-faithful implementations.",
            "Historical dependency data is re-read and hashed, not a newly executed market replay.",
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2) + "\n",
                           encoding="utf-8")
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
