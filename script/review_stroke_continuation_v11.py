"""Review retained-origin waiting in v11 without changing production code.

Only the user's local source corpus supplies Chan rules. Synthetic tail chains
are checked independently for necessary local conditions; they are not claimed
to prove a unique attachment to the unresolved earlier part of the chart.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from script.review_stroke_rule_logic import (
    COMPLETION_RETRACTION, file_record, input_properties, long_origin_wait,
    path_conditions, production, reflected, selection_trace, streaming,
)
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE, strict_base_config_revision


def summarize_trace(event):
    return {**{key: event[key] for key in (
        "center", "kind", "value", "selected", "candidate_parent", "candidate_origin", "action", "pending",
    )}, "eligible_count": len(event["eligible"]), "adjacent": event["irreducible"],
        "live_retained": [point for point in event["live_starts"] if point["center"] in event["selected"]]}


def inspect_wait(height, legs, mirror):
    values = long_origin_wait(legs)
    values[12:15] = [(height - 2, 8), (height, height - 4), (height - 1, 12)]
    prices = reflected(values, mirror)
    properties = input_properties(prices)
    assert not properties["gaps"] and not properties["inclusions"]
    current = production(prices)
    tail_centers = list(range(15, len(prices) - 1, 4))
    local_checks = path_conditions(prices, tail_centers)
    assert all(edge["locally_legal"] and edge["longest_qualified_subdivision"] == 1 for edge in local_checks)
    # The additional interval inequality is checked explicitly instead of
    # assuming that the production pair checker defines the expected result.
    for start, end in zip(tail_centers, tail_centers[1:]):
        high, low = sorted((prices[start], prices[end]), key=lambda bar: bar[0], reverse=True)
        assert end - start >= 4 and high[0] > low[0] and high[1] > low[1]
    cropped = production(prices[14:])
    crop_edges = [(b["raw_start"] + 14, b["raw_end"] + 14) for b in cropped["strokes"]]
    assert crop_edges == list(zip(tail_centers, tail_centers[1:]))
    trace = selection_trace(prices)
    replay = streaming(prices, current)
    assert replay["final_matches_batch"]
    return dict(
        height_before_reflection=height, extended_legs=legs, mirror=mirror,
        raw_bars=len(prices), prices=prices, properties=properties,
        current=current, later_local_checks=local_checks,
        cropped_tail_stroke_count=len(crop_edges),
        first_later_connection=summarize_trace(next(e for e in trace if e["center"] == 19)),
        last_fractal=summarize_trace(trace[-1]),
        replay=dict(prefixes=replay["prefixes"], final_matches_batch=True,
                    output_change_prefixes=[e["raw_bars"] for e in replay["changes"]]),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = []
    for prefix, kind, ranges in (
        ("L062_", "author_body", [(37, 118)]),
        ("L065_", "body_with_separately_identified_compiler_notes", [(31, 100), (166, 214)]),
        ("L066_", "author_reply_not_lesson_body", [(256, 280)]),
        ("L069_", "author_body", [(28, 49), (151, 184), (205, 223)]),
        ("L077_", "body_with_separately_identified_compiler_notes", [(118, 148), (169, 295)]),
    ):
        matches = list((args.source_root / "chanlun_lesson_corpus").glob(prefix + "*.md"))
        assert len(matches) == 1
        lines = matches[0].read_text(encoding="utf-8").splitlines()
        sources.append(dict(**file_record(matches[0]), kind=kind, line_ranges=ranges,
                            excerpts=[dict(start=a, end=b, text="\n".join(
                                line for line in lines[a - 1:b] if line and not line.startswith("<!--")))
                                for a, b in ranges]))

    baseline_path = ROOT / "output/bi_code_review/v11_source_manifest.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current_code = [file_record(row["path"]) for row in baseline["production"]]
    assert all(now["sha256"] == old["sha256"] for now, old in zip(current_code, baseline["production"]))
    grid = [(h, 64) for h in (24, 26, 28, 30, 31, 34)] + [(28, 0), (28, 256)]
    cases = []
    for height, legs in grid:
        for mirror in (False, True):
            case = inspect_wait(height, legs, mirror)
            cases.append(case)
            print(json.dumps(dict(height=height, legs=legs, mirror=mirror,
                                  bars=case["raw_bars"], selected=len(case["current"]["strokes"]),
                                  later_qualified=len(case["later_local_checks"]),
                                  blocked=case["current"]["pending_continuation"],
                                  replay_equal=case["replay"]["final_matches_batch"])), flush=True)

    released = []
    for mirror in (False, True):
        values = long_origin_wait(256)
        values[12:15] = [(26, 8), (28, 24), (27, 12)]
        values += [(23, 19), (27, 23), (29, 25), (27, 23)]
        prices = reflected(values, mirror)
        current = production(prices)
        replay = streaming(prices, current)
        assert replay["final_matches_batch"]
        released.append(dict(mirror=mirror, prices=prices, current=current,
                             interior_later_chain_count=260,
                             selected_long_connection_checks=path_conditions(prices, [1, 11, 1059]),
                             replay=dict(prefixes=replay["prefixes"], final_matches_batch=True)))

    completion = []
    for mirror in (False, True):
        prices = reflected(COMPLETION_RETRACTION, mirror)
        final = production(prices)
        replay = streaming(prices, final)
        assert replay["final_matches_batch"]
        completion.append(dict(mirror=mirror, prices=prices,
                               prefix_11=production(prices[:11]), prefix_19=final,
                               replay=dict(prefixes=replay["prefixes"], final_matches_batch=True)))

    report = dict(
        created_at=datetime.now(timezone.utc).isoformat(), profile=STRICT_STROKE_MODE,
        profile_revision=strict_base_config_revision(), sources=sources,
        production_code=current_code, baseline_manifest=file_record(baseline_path),
        production_unchanged_since_previous_validation=True,
        audit_code=[file_record(Path(__file__)), file_record(ROOT / "script/review_stroke_rule_logic.py")],
        retained_wait_cases=cases, later_price_release=released, completion_retraction=completion,
        observations=[
            "For height 28/30, a live earliest retained extremum blocks later qualified components; the chosen tail itself is no longer live.",
            "The stalled case reports no pending-continuation boundary despite available adjacent tail relations.",
            "For height 31/34, the new component is selected while the earlier selected prefix disappears from current output.",
            "Existing successor evidence still retracts; completion_is_final remains unknown.",
        ],
        limits=[
            "Local eligibility and absence of a complete subdivision do not prove a unique global partition.",
            "The cropped tail comparison illustrates missing continuation coverage, not permission to concatenate disconnected paths.",
            "Synthetic boundary cases do not estimate the frequency of these issues in real markets.",
            "Production and live-service configuration were not changed by this audit.",
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(cases=len(cases) + len(released) + len(completion),
                          incremental_prefixes=sum(c["replay"]["prefixes"] for c in cases + released + completion),
                          all_replays_match=True, output=str(args.output))))


if __name__ == "__main__":
    main()
