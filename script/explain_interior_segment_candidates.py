"""Inspect every full-standard interior fractal under its actual boundary.

A standard element containing pens BEFORE its extreme supplier crosses the
hypothesized boundary when reused as that boundary's post-pivot element. L071
requires a fresh, same-side inclusion calculation. These records distinguish
that factual failure from unresolved questions about reference qualification.
"""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from script.audit_all_segment_results import OUT, read_json, write_json
from script.check_segment_model import pen
from script.check_segment_candidates import check_gap_invalidation


def protected_suffix(pens, pivot, stop, up):
    first = pens[pivot]
    low, high = sorted((first["start_tick"], first["end_tick"]))
    extreme = high if up else low
    sources = [pivot]
    for i in range(pivot + 1, stop + 1):
        p = pens[i]
        lo, hi = sorted((p["start_tick"], p["end_tick"]))
        original_direction = (p["end_tick"] > p["start_tick"]) == up
        if original_direction:
            if hi > extreme if up else lo < extreme:
                return {"status": "old_direction_extension", "witness": i, "extreme": extreme}
            continue
        if (low <= lo and hi <= high) or (lo <= low and high <= hi):
            choose = max if up else min
            low, high = choose(low, lo), choose(high, hi)
            sources.append(i)
            continue
        return {"status": "independent_right", "witness": i, "middle": [low, high, sources],
                "right": [lo, hi, [i]]}
    return {"status": "no_independent_right", "middle": [low, high, sources]}


def unfinished_second(pens, pivot, up):
    """Strict same-side F2 reconstruction, independent of production helpers."""
    first = pens[pivot]
    extreme = max(first['start_tick'], first['end_tick']) if up else min(first['start_tick'], first['end_tick'])
    standard = []
    for i in range(pivot + 1, len(pens)):
        p = pens[i]
        if (p['end_tick'] > p['start_tick']) != up:
            continue
        lo, hi = sorted((p['start_tick'], p['end_tick']))
        if standard and ((standard[-1][0] <= lo and hi <= standard[-1][1]) or
                         (lo <= standard[-1][0] and standard[-1][1] <= hi)):
            rising = not up if len(standard) == 1 else standard[-1][1] > standard[-2][1]
            pick = max if rising else min
            standard[-1] = (pick(standard[-1][0], lo), pick(standard[-1][1], hi))
        else:
            standard.append((lo, hi))
        if len(standard) >= 3:
            a, m, z = standard[-3:]
            found = (m[0] < min(a[0], z[0]) and m[1] < min(a[1], z[1]) if up else
                     m[0] > max(a[0], z[0]) and m[1] > max(a[1], z[1]))
            if found:
                return {"status": "second_fractal_found", "witness": i}
        if hi > extreme if up else lo < extreme:
            return {"status": "old_direction_extension", "witness": i}
    return {"status": "still_waiting_for_second_fractal"}


def explain_one(dataset, output=OUT):
    output = Path(output)
    path = output / "geometry" / (dataset["id"] + ".json.gz")
    d = json.loads(gzip.decompress(path.read_bytes()))
    components = {c["component"]: c for c in d["components"]}
    results = []
    for row in d["rows"]:
        for candidate in row.get("interior_standard_fractals", []):
            component = components[row["component"]]
            pivot = candidate["candidate_end"] + 1
            suffix = protected_suffix(component["pens"], pivot, candidate["witness"], row["direction"] == "up")
            m, right = candidate["first_sequence"][1:]
            same = (suffix["status"] == "independent_right" and suffix["witness"] == candidate["witness"]
                    and suffix["middle"] == list(m[:3]) and suffix["right"] == list(right[:3]))
            crosses = min(m[2]) < pivot
            events = [e for e in component["decisions"] if e["start"] == row["start_pen"]
                      and e["check"] <= pivot <= e.get("witness_index", -1)]
            if suffix["status"] == "old_direction_extension":
                reason = "candidate_extreme_crossed_before_its_own_right_element"
            elif suffix["status"] == "no_independent_right":
                reason = "no_same_side_right_element_at_claimed_witness"
            elif not same:
                reason = "same_side_sequence_differs_from_premerged_sequence"
            elif events:
                reason = "requires_review_of_prior_hypothesis_invalidation"
            else:
                reason = "requires_reference_qualification_review"
            resolution = None
            candidate_end = component['pens'][candidate['candidate_end']]['end_tick']
            valid_direction = candidate_end > row['start_tick'] if row['direction'] == 'up' else candidate_end < row['start_tick']
            if not valid_direction:
                resolution = {"status": "candidate_violates_segment_net_direction", "source": "R78-direction",
                              "segment_start_tick": row['start_tick'], "candidate_end_tick": candidate_end}
            elif not same:
                resolution = {"status": "claimed_fractal_not_reproducible_on_its_own_boundary_side", "source": "R71-boundary"}
            elif events:
                values = [pen(i, p['start_tick'], p['end_tick']) for i, p in enumerate(component['pens'])]
                checked = []
                for e in events:
                    if e['kind'] == '_GapConfirmationInvalidated':
                        try:
                            check_gap_invalidation(values, e['candidate_index'], row['direction'], e['witness_index'])
                            checked.append({"event": e, "independently_reconstructed": True})
                        except AssertionError as exc:
                            checked.append({"event": e, "independently_reconstructed": False, "error": str(exc)})
                    elif e['kind'] == '_CandidateExtension':
                        own = protected_suffix(component['pens'], e['check'], e['witness_index'], row['direction'] == 'up')
                        checked.append({"event": e, "independently_reconstructed": own['status'] == 'old_direction_extension', "replay": own})
                resolution = {"status": "inside_independently_invalidated_prior_hypothesis" if checked and all(x['independently_reconstructed'] for x in checked) else "unresolved",
                              "source": "R78-second", "events": checked}
            else:
                pending = [e for e in component['decisions'] if e['start'] == row['start_pen'] and e['check'] < pivot
                           and e['kind'] == 'PendingBoundary' and e.get('reason') == 'waiting-second-feature']
                if pending:
                    replay = unfinished_second(component['pens'], pending[0]['candidate_index'] + 1, row['direction'] == 'up')
                    resolution = {"status": "earlier_second_case_still_unresolved" if replay['status'] == 'still_waiting_for_second_fractal' else "unresolved",
                                  "source": "R67", "earlier_candidate": pending[0], "replay": replay}
            if resolution is None:
                resolution = {"status": "unresolved"}
            results.append({"segment_id": row["segment_id"], "number": row["number"], "code": d["code"],
                            "frequency": d["frequency"], "candidate": candidate,
                            "premerged_across_boundary": crosses, "same_side_suffix": suffix,
                            "same_side_sequence_matches": same, "intersecting_decisions": events,
                            "finding": reason, "resolution": resolution, "source_refs": ["R71-boundary", "R78-second", "R81"]})
    write_json(output / "interior_reviews" / (dataset["id"] + ".json.gz"), results)
    return {"id": dataset["id"], "candidates": len(results), "counts": dict(Counter(r["finding"] for r in results)),
            "resolution_counts": dict(Counter(r['resolution']['status'] for r in results)),
            "unresolved": [r for r in results if r['resolution']['status'] == 'unresolved']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    jobs = [d for d in read_json(args.output / "geometry_results.json")["datasets"]
            if d["flag_counts"].get("interior_full_standard_fractal")]
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for f in as_completed([pool.submit(explain_one, d, args.output) for d in jobs]):
            results.append(f.result())
    counts = sum((Counter(r["counts"]) for r in results), Counter())
    report = {"datasets": results, "candidate_count": sum(r["candidates"] for r in results), "counts": dict(counts),
              "resolution_counts": dict(sum((Counter(r['resolution_counts']) for r in results), Counter())),
              "unresolved": [x for r in results for x in r["unresolved"]]}
    write_json(args.output / "interior_explanations.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"datasets", "unresolved"}}))
    print(json.dumps(report["unresolved"][:3], ensure_ascii=False))


if __name__ == "__main__":
    main()
