"""Independently enumerate internal gapped F1 candidates and their F2 outcome.

An internal fractal is a question, not a replacement segmentation. Boundary
ownership, direction, prior hypotheses and raw/standard gap differences are
retained explicitly for review against the locally read lessons.
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

from script.audit_all_segment_results import write_json
from script.review_segment_geometry import standard_append
from script.explain_interior_segment_candidates import protected_suffix
from script.check_segment_candidates import check_gap_invalidation
from script.check_segment_model import pen

OUT = ROOT / "output/segment_reaudit_20260919"


def turns(pens, start, stop, up):
    standard = []
    for i in range(start + 1, stop + 1, 2):
        p = pens[i]
        lo, hi = sorted((p["start_tick"], p["end_tick"]))
        if not standard_append(standard, (lo, hi, (i,), i, i), up) or len(standard) < 3:
            continue
        left, middle, right = standard[-3:]
        fractal = (middle[0] > max(left[0], right[0]) and middle[1] > max(left[1], right[1]) if up else
                   middle[0] < min(left[0], right[0]) and middle[1] < min(left[1], right[1]))
        if not fractal or max(left[0], middle[0]) <= min(left[1], middle[1]):
            continue
        pivot = middle[4 if up else 3]
        if start + 3 <= pivot <= stop:
            yield {"pivot": pivot, "end_pen": pivot - 1, "first_witness": i,
                   "first_sequence": [left, middle, right]}


def second_outcome(pens, pivot, stop, parent_up):
    standard = []
    extreme = pens[pivot]["start_tick"]
    for i in range(pivot + 1, stop + 1, 2):
        p = pens[i]
        lo, hi = sorted((p["start_tick"], p["end_tick"]))
        standard_append(standard, (lo, hi, (i,), i, i), not parent_up)
        if len(standard) >= 3:
            a, m, z = standard[-3:]
            found = (m[0] < min(a[0], z[0]) and m[1] < min(a[1], z[1]) if parent_up else
                     m[0] > max(a[0], z[0]) and m[1] > max(a[1], z[1]))
            if found:
                return {"status": "second_fractal_formed", "witness": i, "sequence": [a, m, z],
                        "same_pen_crosses_origin": hi > extreme if parent_up else lo < extreme}
        if hi > extreme if parent_up else lo < extreme:
            return {"status": "invalidated_before_second_fractal", "witness": i, "origin_price": extreme}
    return {"status": "no_second_fractal_before_saved_endpoint"}


def review(dataset, output=None):
    output = Path(output) if output is not None else OUT
    data = json.loads(gzip.decompress((output / "geometry" / f"{dataset['id']}.json.gz").read_bytes()))
    components = {c["component"]: c for c in data["components"]}
    records = []
    for row in data["rows"]:
        if "start_pen" not in row:
            continue
        c = components[row["component"]]
        pens, up = c["pens"], row["direction"] == "up"
        for candidate in turns(pens, row["start_pen"], row["end_pen"], up):
            pivot = candidate["pivot"]
            left, middle, right = candidate["first_sequence"]
            suffix = protected_suffix(pens, pivot, candidate["first_witness"], up)
            same_side = (suffix["status"] == "independent_right" and suffix["witness"] == candidate["first_witness"]
                         and suffix["middle"] == [middle[0], middle[1], list(middle[2])]
                         and suffix["right"] == [right[0], right[1], list(right[2])])
            endpoint = pens[candidate["end_pen"]]["end_tick"]
            valid_direction = endpoint > row["start_tick"] if up else endpoint < row["start_tick"]
            raw_lo, raw_hi = sorted((pens[pivot]["start_tick"], pens[pivot]["end_tick"]))
            raw_gap = max(left[0], raw_lo) > min(left[1], raw_hi)
            outcome = second_outcome(pens, pivot, row["end_pen"], up)
            prior = []
            if not same_side:
                resolution = "first_sequence_crosses_actual_boundary_or_loses_its_right_element"
            elif not valid_direction:
                resolution = "candidate_violates_segment_direction"
            elif not raw_gap:
                resolution = "raw_standard_gap_difference_requires_interpretation"
            elif outcome["status"] != "second_fractal_formed":
                resolution = outcome["status"]
            else:
                # An apparent B/C ending cannot be imposed inside an earlier
                # unresolved parent hypothesis (L078, lines 208-220).
                events = [e for e in c["decisions"] if e["start"] == row["start_pen"]
                          and e["check"] <= pivot <= e.get("witness_index", -1)
                          and e["kind"] == "_GapConfirmationInvalidated"]
                values = [pen(i, p["start_tick"], p["end_tick"]) for i, p in enumerate(pens)]
                for event in events:
                    try:
                        check_gap_invalidation(values, event["candidate_index"], row["direction"], event["witness_index"])
                        prior.append({"candidate": event["candidate_index"], "witness": event["witness_index"], "checked": True})
                    except AssertionError:
                        prior.append({"candidate": event["candidate_index"], "witness": event["witness_index"], "checked": False})
                resolution = "inside_earlier_hypothesis_independently_invalidated" if prior and all(x["checked"] for x in prior) else "needs_reference_and_hypothesis_review"
            records.append({"segment_id": row["segment_id"], "number": row["number"], "code": dataset["code"],
                            "frequency": dataset["frequency"], "candidate": candidate, "same_side_sequence": suffix,
                            "raw_gap": raw_gap, "second_outcome": outcome, "prior_hypotheses": prior,
                            "resolution": resolution, "source_refs": ["R67", "R71-boundary", "R77", "R78-second"]})
    write_json(output / "gapped_reviews" / f"{dataset['id']}.json.gz", records)
    return {"id": dataset["id"], "candidates": len(records), "counts": dict(Counter(r["resolution"] for r in records)),
            "needs_review": [r for r in records if r["resolution"] in {"raw_standard_gap_difference_requires_interpretation", "needs_reference_and_hypothesis_review"}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    inventory = json.loads((args.output / "geometry_results.json").read_text(encoding="utf-8"))
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for task in as_completed([pool.submit(review, d, args.output) for d in inventory["datasets"]]):
            results.append(task.result())
    result = {"datasets": results, "candidates": sum(r["candidates"] for r in results),
              "counts": dict(sum((Counter(r["counts"]) for r in results), Counter())),
              "needs_review": [r for d in results for r in d["needs_review"]]}
    write_json(args.output / "gapped_candidates.json", result)
    print(json.dumps({"candidates": result["candidates"], "counts": result["counts"], "needs_review": len(result["needs_review"])}), flush=True)


if __name__ == "__main__":
    main()
