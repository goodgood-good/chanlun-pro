"""Construct continuations that complete a pending second-feature boundary.

This is a conditional causal argument: an eligible first fractal and its
observation origin are assumed. It does not prove eligibility for every history,
settle equal boundary suppliers, or model all exchange tick/price constraints.
The independent tuple replay uses every raw second-sequence pen.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_candidates import local_first_fractal
from script.check_segment_model import check_evidence, pen, signature


PENDING_EXAMPLE = [
    0, 18, 15, 18, 12, 20, 13, 31, 21, 22, 19, 29,
    10, 22, 10, 22, 21, 29, 17, 21, 16,
]


def second_sequence_state(points, boundary, direction):
    """Return the first strict F2 event, or all still-pending standard elements."""
    sign = 1 if direction == "up" else -1
    prices = [sign * value for value in points]
    ceiling = prices[boundary]
    standard = []
    # A's first reverse pen is boundary; B's first feature is boundary + 1.
    for i in range(boundary + 1, len(prices) - 1, 2):
        low, high = prices[i], prices[i + 1]
        assert low < high, "second_feature_direction"
        if standard:
            old_low, old_high, sources = standard[-1]
            includes = (old_low <= low and high <= old_high) or (
                low <= old_low and old_high <= high
            )
            if includes:
                # The first context is B-down. Thereafter a non-contained
                # standard pair fixes the chronological merge direction.
                rises = len(standard) >= 2 and old_high > standard[-2][1]
                choose = max if rises else min
                standard[-1] = (
                    choose(old_low, low), choose(old_high, high), sources + (i,),
                )
            else:
                standard.append((low, high, (i,)))
        else:
            standard.append((low, high, (i,)))
        if len(standard) >= 3:
            a, b, c = standard[-3:]
            if b[0] < min(a[0], c[0]) and b[1] < min(a[1], c[1]):
                return "confirmed", i, standard
        if high > ceiling:
            return "invalidated", i, standard
    return "pending", None, standard


def completing_suffix(points, boundary, direction):
    """At most five alternating pens, with no new original-direction extreme.

    After an optional up pen, place a strict lower feature below all prior
    values, then a strict higher one. The previous standard tail is the left
    element. Values are exact rationals in the scalar geometry model; an
    affine translation can make the entire constructed example positive.
    """
    sign = 1 if direction == "up" else -1
    normalized = [Fraction(sign * value) for value in points]
    ceiling = normalized[boundary]
    suffix = []
    if normalized[-1] < normalized[-2]:
        # The last pen is B-down; supply the next B feature first.
        assert normalized[-1] < ceiling
        suffix.append((normalized[-1] + ceiling) / 2)
    floor = min(normalized)
    step = min(Fraction(1), floor / 8) if floor > 0 else Fraction(1)
    suffix.extend((floor - 4 * step, floor - 2 * step,
                   floor - 3 * step, floor - step))
    return [sign * value for value in suffix]


def audit_pending(points, count_competitors=False):
    values = [pen(i, a, b) for i, (a, b) in enumerate(zip(points, points[1:]))]
    calc = XdCalculator()
    lines = calc.calculate(values)
    if calc.tail_state.reason != "waiting-second-feature":
        return None
    tail = calc.tail_state
    start, end, direction = tail.start_index, tail.candidate_index, tail.direction
    boundary = end + 1
    before_status, _, before_standard = second_sequence_state(points, boundary, direction)
    assert before_status == "pending", "reported_pending_state_not_reconstructible"
    suffix = completing_suffix(points, boundary, direction)
    extended = list(points) + suffix
    status, witness, after_standard = second_sequence_state(extended, boundary, direction)
    assert status == "confirmed", "constructed_suffix_did_not_complete_second_fractal"
    assert witness >= len(values), "completion_precedes_new_information"
    previous = signature(lines)
    event_seen = False
    for stop in range(len(points) + 1, len(extended) + 1):
        current_points = extended[:stop]
        current_values = [
            pen(i, a, b) for i, (a, b) in enumerate(zip(current_points, current_points[1:]))
        ]
        next_calc = XdCalculator()
        result = next_calc.calculate(current_values)
        check_evidence(next_calc, current_values)
        now = signature(result)
        assert all(now.get(key) == when for key, when in previous.items()), (
            "confirmed_boundary_changed"
        )
        assert all(
            when == current_values[-1].locked_at
            for key, when in now.items() if key not in previous
        ), "confirmation_backdated"
        previous = now
        if stop - 2 >= witness:
            assert (start, end, direction) in now, "pending_candidate_lost_on_completion"
            event_seen = True
    assert event_seen
    competitors = []
    if count_competitors:
        for candidate in range(boundary + 2, len(values) - 2, 2):
            local = local_first_fractal(values, start, candidate, len(values) - 1)
            if local is not None and not local.has_gap:
                competitors.append(asdict(local))
    return {
        "start": start, "boundary_point": boundary, "direction": direction,
        "original_pens": len(values), "added_pens": len(suffix),
        "completion_witness": witness, "before_standard": before_standard,
        "after_standard": after_standard,
        "competing_local_first_fractals": competitors,
        "suffix": suffix,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--walks", type=int, default=10000)
    parser.add_argument("--points", type=int, default=64)
    parser.add_argument("--seed", type=int, default=19270921)
    parser.add_argument("--output", type=Path, default=ROOT / "output/segment_audit/pending_priority.json")
    args = parser.parse_args()
    if args.walks < 1 or args.points < 4:
        parser.error("walks must be positive and points must be at least four")
    hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "src/chanlun/core/xd_calculator.py", "src/chanlun/core/segment_evidence.py",
            "script/check_segment_model.py", "script/check_segment_candidates.py",
            "script/check_segment_pending_priority.py",
        )
    }
    started = perf_counter()
    counts = dict.fromkeys(("walks", "pending_contexts", "added_prefixes",
                           "contexts_with_competitors", "local_competitors"), 0)
    examples = []
    rng = random.Random(args.seed)
    failure = None
    points = None
    try:
        for _ in range(args.walks):
            points = [0]
            direction = rng.choice((-1, 1))
            for i in range(args.points - 1):
                points.append(points[-1] + direction * (-1 if i % 2 else 1) * rng.randint(1, 40))
            counts["walks"] += 1
            record = audit_pending(points, count_competitors=True)
            if record is None:
                continue
            counts["pending_contexts"] += 1
            counts["added_prefixes"] += record["added_pens"]
            competitors = record["competing_local_first_fractals"]
            counts["local_competitors"] += len(competitors)
            counts["contexts_with_competitors"] += bool(competitors)
            if competitors and len(examples) < 3:
                examples.append({"points": points, **record})
        assert counts["pending_contexts"] > 0, "empty_pending_check"
    except (AssertionError, ValueError) as exc:
        failure = {"reason": str(exc), "points": points}
    result = {
        "scope": "conditional completion continuations of an already eligible pending first fractal",
        "random_points": args.points, "seed": args.seed, "counts": counts,
        "examples": examples, "seconds": perf_counter() - started, "failure": failure,
        "implementation_sha256": hashes,
        "all_initial_candidate_eligibility_proven": False,
        "exchange_tick_or_price_bounds_modeled": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "examples"}, ensure_ascii=False))
    raise SystemExit(1 if failure else 0)


if __name__ == "__main__":
    main()
