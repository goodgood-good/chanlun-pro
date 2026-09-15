"""Distinguish batch snapshots, live continuation, and historical as-of replay.

Read-only probes of the current old-stroke implementation. Final-snapshot
endpoint clipping is intentionally demonstrated as an invalid replay technique;
this does not allege that a production caller currently uses that technique.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_stroke_architecture_v11 import file_record, read_originals
from review_stroke_rule_logic import (
    COMPLETION_RETRACTION, EQUAL_PREFIX, independent_definitions, long_origin_wait,
    production, raw_bars, reflected, snapshot,
)
from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.stroke_resolver import StrokeResolver


def process_cuts(prices, cuts):
    raw = raw_bars(prices)
    assert cuts and cuts[-1] == len(raw)
    merged, calc = CL_Kline_Process(), BiCalculator()
    advance_centers = []
    original_advance = StrokeResolver.advance

    def observe_advance(resolver, fx, valid, witness):
        advance_centers.append(fx.k.index)
        return original_advance(resolver, fx, valid, witness)

    states = []
    with patch.object(StrokeResolver, "advance", observe_advance):
        for count in cuts:
            merged.process_cl_klines(raw[:count])
            calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                           validated_incremental_prefix=True)
            states.append(dict(
                raw_bars=count, stroke_count=len(calc.bis),
                continued_count=len(calc.confirmed_bis),
                first_center=calc.bis[0].start.k.index if calc.bis else None,
                last_center=calc.bis[-1].end.k.index if calc.bis else None,
                continuation_blocked_at=calc.continuation_blocked_at,
            ))
    return dict(
        input_calls=len(cuts), cuts=cuts, advance_centers=advance_centers,
        observed_output_history=states, final=snapshot(merged, calc),
    )


def mode_comparison(prices, initial_history):
    size = len(prices)
    cuts = sorted(set([*range(7, size + 1, 7), size]))
    modes = dict(
        one_batch=process_cuts(prices, [size]),
        one_bar_at_a_time=process_cuts(prices, list(range(1, size + 1))),
        chunks=process_cuts(prices, cuts),
        batch_history_then_live=process_cuts(prices, [initial_history, *range(initial_history + 1, size + 1)]),
    )
    cold = production(prices)
    for mode in modes.values():
        assert mode["final"] == cold
    assert modes["one_batch"]["advance_centers"] == [x["center"] for x in cold["fractals"]]
    return dict(prices=prices, modes=modes, final_states_equal=True,
                batch_replays_every_fractal_in_order=True)


def disconnected_candidate_cut(prices, cut):
    """Exhaust all local connections across a fixed history boundary.

    This is a feasibility result for the currently formalized local predicate,
    not a claim that the original author prescribes disconnected strokes.
    """
    points, valid = independent_definitions(prices)
    before = [fx for fx in points if fx.k.index <= cut]
    after = [fx for fx in points if fx.k.index > cut]
    crossing = [[a.k.index, b.k.index] for a in before for b in after if valid(a, b)]
    highest_old_top = max(fx.val for fx in before if fx.type == "ding")
    lowest_old_bottom = min(fx.val for fx in before if fx.type == "di")
    later_high = max(after, key=lambda fx: prices[fx.k.index][0])
    later_low = min(after, key=lambda fx: prices[fx.k.index][1])
    assert not crossing
    assert prices[later_high.k.index][0] > highest_old_top
    assert prices[later_low.k.index][1] < lowest_old_bottom
    return dict(
        cut_after_center=cut, older_fractals=len(before), newer_fractals=[
            dict(center=fx.k.index, kind=fx.type, value=fx.val) for fx in after
        ], examined_pairs=len(before) * len(after), crossing_qualified_edges=crossing,
        highest_old_top=highest_old_top, lowest_old_bottom=lowest_old_bottom,
        later_high_barrier=dict(center=later_high.k.index, high=prices[later_high.k.index][0]),
        later_low_barrier=dict(center=later_low.k.index, low=prices[later_low.k.index][1]),
        scope="No old-to-new edge under the tested local predicate; not an author-proven full partition.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = read_originals(args.source_root)
    baseline_path = ROOT / "output/bi_code_review/v11_current_review_manifest.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    before_files = [file_record(x["path"]) for x in baseline["production_code"]]
    assert before_files == baseline["production_code"]
    wait = long_origin_wait(0)
    wait[12:15] = [(26, 8), (28, 24), (27, 12)]
    cycle = [(18, 14), (16, 12), (14, 10), (12, 8),
             (14, 10), (16, 12), (18, 14), (20, 16)]
    replacement = COMPLETION_RETRACTION[:2] + cycle * 256 + COMPLETION_RETRACTION[2:]
    equal = EQUAL_PREFIX + [(11, 7), (13, 9), (14, 10), (12, 8)]
    cases = []
    for name, values, initial in (("retained_wait", wait, 11),
                                  ("long_prefix_replacement", replacement, 2059),
                                  ("equal_junction_reselection", equal, 17)):
        for mirror in (False, True):
            case = mode_comparison(reflected(values, mirror), initial)
            if name == "long_prefix_replacement":
                case["independent_candidate_cut"] = disconnected_candidate_cut(
                    reflected(values, mirror), initial - 2,
                )
            cases.append(dict(name=name, mirror=mirror, **case))
            print(f"{name}, mirror={mirror}: four input modes agree", flush=True)
    before, final = production(equal[:17]), production(equal)
    # A geometric endpoint at bar 5 can be selected again only at bar 20.
    # Clipping the later graph at bar 16 cannot recover the observed bar-16 graph.
    clipped = [b for b in final["strokes"] if b["end"] <= 16]
    observed = [[b["start"], b["end"]] for b in before["strokes"]]
    projected = [[b["start"], b["end"]] for b in clipped]
    assert observed == [[1, 11], [11, 15]]
    assert projected == [[1, 5]] and observed != projected
    assert clipped[0]["selected_at"] == raw_bars(equal)[20].date
    result = dict(
        created_at=datetime.now(timezone.utc).isoformat(),
        original_sources=sources, production_code=before_files,
        production_unchanged=before_files == [file_record(x["path"]) for x in before_files],
        audit_code=[file_record(__file__), file_record(ROOT / "script/review_stroke_architecture_v11.py"),
                    file_record(ROOT / "script/review_stroke_rule_logic.py")],
        mode_cases=cases,
        as_of_replay_counterexample=dict(
            prices=equal, prefix_raw_bars=17, full_raw_bars=21, prefix_snapshot=before,
            full_snapshot=final, later_snapshot_clipped_by_endpoint=clipped,
            intentionally_wrong_projection_differs_from_prefix=True,
            existing_reselected_at_is_later_witness=True,
        ),
        limits=[
            "Batch and live share old-pen rules; modes differ in available input and state management.",
            "Batch replay in the current implementation is not an independent whole-chart selection oracle.",
            "Same-as-of geometry agreement does not imply original-text correctness or historical immutability.",
            "These probes feed immutable completed OHLC snapshots, not a reconstruction of actual historical ticks.",
            "The clipped-snapshot example is an intentionally invalid replay method, not an observed production leak.",
        ],
    )
    assert result["production_unchanged"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
