"""Bounded exhaustive checks of causal segment construction.

Finite price alphabets cover equality and containment patterns; they do not
prove equivalence to every possible original-theory figure. This checker does
not call production feature/fractal helpers when validating returned evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random
import sys
from time import perf_counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chanlun.core.xd_calculator import XdCalculator

BASE = datetime(2026, 9, 16, tzinfo=timezone.utc)


def pen(index, a, b):
    return SimpleNamespace(
        index=index,
        start=SimpleNamespace(val=a),
        end=SimpleNamespace(val=b),
        type="up" if a < b else "down",
        high=max(a, b),
        low=min(a, b),
        locked_at=BASE + timedelta(minutes=index),
        selection_pending=False,
    )


def check_evidence(calc, strokes):
    for position, proof in enumerate(calc.evidence):
        a, b, witness = proof.start_index, proof.end_index, proof.witness_index
        assert 0 <= a <= b < witness < len(strokes), "evidence_bounds"
        assert b - a >= 2 and (b - a) % 2 == 0, "evidence_span"
        assert strokes[a].type == proof.direction == strokes[b].type, "body_direction"
        assert max(strokes[a].low, strokes[a + 2].low) <= min(
            strokes[a].high, strokes[a + 2].high
        ), "initial_overlap"
        assert (
            strokes[b].end.val > strokes[a].start.val
            if proof.direction == "up"
            else strokes[b].end.val < strokes[a].start.val
        ), "net_direction"
        left, middle, right = proof.first_sequence
        if proof.direction == "up":
            assert middle.high > max(left.high, right.high), "first_top"
        else:
            assert middle.low < min(left.low, right.low), "first_bottom"
        check_first_reference(proof, strokes)
        check_first_sequence(proof, strokes)
        assert max(left.source_indices) < min(middle.source_indices), "left_ownership"
        assert max(middle.source_indices) < min(right.source_indices), "right_ownership"
        features = (*proof.first_sequence, *proof.second_sequence)
        if proof.pivot_stem is not None:
            features += (proof.pivot_stem,)
        assert all(
            a <= i <= witness
            for elem in features
            for i in elem.source_indices
        ), "source_bounds"
        assert all(
            strokes[i].type != proof.direction
            for elem in proof.first_sequence
            for i in elem.source_indices
        ), "first_direction"
        for element in features:
            assert list(element.source_indices) == sorted(
                set(element.source_indices)
            ), "source_order"
            assert element.low <= element.high, "interval_order"
            assert element.low_source_index in element.source_indices, "low_ownership"
            assert element.high_source_index in element.source_indices, "high_ownership"
            assert element.low == strokes[element.low_source_index].low, "low_price"
            assert element.high == strokes[element.high_source_index].high, "high_price"
        endpoint_source = (
            middle.high_source_index
            if proof.direction == "up"
            else middle.low_source_index
        )
        assert endpoint_source - 1 == b, "pivot_source"
        # Ordinary classification follows the reviewed original-first-pen
        # reading. An inherited proof retains its parent's strict standard
        # second sequence; its own gap never requests a third sequence.
        gap_middle = middle if proof.parent_key is not None else strokes[b + 1]
        actual_gap = max(left.low, gap_middle.low) > min(left.high, gap_middle.high)
        assert proof.initial_gap == actual_gap, "boundary_gap"
        if proof.parent_key is not None:
            assert position > 0, "inherited_parent_missing"
            parent = calc.evidence[position - 1]
            assert parent.key == proof.parent_key, "inherited_parent_identity"
            assert parent.end_index + 1 == a, "inherited_parent_continuity"
            assert parent.direction != proof.direction, "inherited_parent_direction"
            assert parent.second_sequence == proof.first_sequence, "inherited_context"
            assert parent.witness_index == witness, "inherited_witness"
            assert proof.rule == "parent-second-feature", "inherited_rule"
            assert not proof.second_sequence, "inherited_third_sequence"
            # The parent's independent reconstruction already established
            # both coordinates of this strict second fractal. Its own gap
            # does not introduce a third-sequence requirement (L067/L077).
            continue
        assert proof.rule != "parent-second-feature", "unauthenticated_inheritance"
        if actual_gap:
            assert proof.rule == "second-feature-fractal", "gap_rule"
            x, y, z = proof.second_sequence
            if proof.direction == "up":
                assert y.low < min(x.low, z.low) and y.high < min(x.high, z.high), (
                    "second_bottom"
                )
            else:
                assert y.low > max(x.low, z.low) and y.high > max(x.high, z.high), (
                    "second_top"
                )
            check_second_sequence(proof, strokes)
            assert position + 1 < len(calc.evidence), "second_fractal_successor_missing"
            successor = calc.evidence[position + 1]
            assert successor.parent_key == proof.key, "second_fractal_not_propagated"
        else:
            assert not proof.second_sequence, "unexpected_second_sequence"


def check_first_reference(proof, strokes):
    """Reconstruct the eligible standard reference before an ordinary pivot.

    L081 eligibility and L065 inclusion are the declared interpretation being
    checked. A local first-break context has a different reference role.
    """
    left, middle, _ = proof.first_sequence
    if proof.rule == "parent-second-feature":
        return
    up = proof.direction == "up"
    past = range(proof.start_index + 1, min(middle.source_indices), 2)
    extreme = (
        max(strokes[i].high for i in past) if up else min(strokes[i].low for i in past)
    )
    if proof.rule == "first-pen-break" or not (
        middle.high > extreme if up else middle.low < extreme
    ):
        check_local_reference(proof, strokes)
        return
    assert proof.pivot_stem is None, "ordinary_reference_with_unexplained_pivot_stem"
    expected = None
    for i in past:
        new = (strokes[i].low, strokes[i].high, (i,))
        if expected is None:
            expected = new
            continue
        contained = (expected[0] <= new[0] and expected[1] >= new[1]) or (
            new[0] <= expected[0] and new[1] >= expected[1]
        )
        if contained:
            choose = max if up else min
            expected = (
                choose(expected[0], new[0]),
                choose(expected[1], new[1]),
                expected[2] + new[2],
            )
        elif new[1] > expected[1] if up else new[0] < expected[0]:
            expected = new
    assert (left.low, left.high, left.source_indices) == expected, (
        "first_reference_reconstruction"
    )


def check_local_reference(proof, strokes):
    """Prove every omitted local reference pen belongs to the retained stem.

    Tuple arithmetic is independent of the production stem index and merger.
    The outer actual reference must still form a strict local extremum; a
    contained stem cannot authorize an arbitrary earlier shoulder.
    """
    left = proof.first_sequence[0]
    stem = proof.pivot_stem
    if stem is None:
        expected_left = proof.end_index - 1
    else:
        sources = stem.source_indices
        assert len(sources) >= 2, "pivot_stem_too_short"
        origin = sources[0]
        assert origin > proof.start_index, "pivot_stem_without_outside_reference"
        assert sources == tuple(range(origin, proof.end_index + 1, 2)), "pivot_stem_sources"
        own = [strokes[i] for i in sources]
        assert all(item.type == proof.direction for item in own), "pivot_stem_direction"
        assert all(
            earlier.low <= later.low and later.high <= earlier.high
            for earlier, later in zip(own, own[1:])
        ), "pivot_stem_not_nested"
        if origin - 2 >= proof.start_index:
            before = strokes[origin - 2]
            assert not (before.low <= own[0].low and own[0].high <= before.high), (
                "pivot_stem_not_maximal"
            )
        choose = min if proof.direction == "up" else max
        expected = (choose(item.low for item in own), choose(item.high for item in own))
        assert (stem.low, stem.high) == expected, "pivot_stem_interval"
        assert stem.low_source_index == next(i.index for i in own if i.low == stem.low), (
            "pivot_stem_low_supplier"
        )
        assert stem.high_source_index == next(i.index for i in own if i.high == stem.high), (
            "pivot_stem_high_supplier"
        )
        edge = stem.high if proof.direction == "up" else stem.low
        assert strokes[proof.end_index].end.val == edge, "pivot_stem_boundary_price"
        # The stem's earliest equal-price supplier is descriptive provenance.
        # The actual boundary comes from the post-boundary middle feature,
        # which check_evidence validates separately as pivot_source.
        expected_left = origin - 1
    raw_left = strokes[expected_left]
    assert (left.low, left.high, left.source_indices) == (
        raw_left.low, raw_left.high, (expected_left,)
    ), "local_reference_scope"
    first = strokes[proof.end_index + 1]
    if proof.rule == "origin-extreme-retest":
        up = proof.direction == "up"
        body = strokes[proof.start_index:proof.end_index + 1:2]
        assert (
            first.high == body[0].high == max(item.high for item in body)
            if up else first.low == body[0].low == min(item.low for item in body)
        ), "origin_retest_extreme"
        assert (
            first.high > left.high if up else first.low < left.low
        ), "origin_retest_local_turn"
        assert max(first.low, left.low) <= min(first.high, left.high), (
            "origin_retest_first_overlap"
        )
    else:
        assert first.high > left.high and first.low < left.low, "local_first_break"


def check_second_sequence(proof, strokes):
    """Rebuild from all original pens, independently of production helpers.

    Pure tuples retain interval ownership. A first valid fractal is decisive;
    an earlier old-direction extreme without a fractal closes this context.
    The checks exercise the stated interpretation of L065/L067/L078/L081,
    rather than proving that interpretation equivalent to every original chart.
    """
    up = proof.direction == "up"
    pivot = proof.end_index + 1
    extreme = strokes[pivot].high if up else strokes[pivot].low
    standard = []
    for i in range(pivot + 1, proof.witness_index + 1):
        pen_value = strokes[i]
        if pen_value.type != proof.direction:
            continue
        new = (pen_value.low, pen_value.high, (i,))
        if standard:
            previous = standard[-1]
            contained = (previous[0] <= new[0] and previous[1] >= new[1]) or (
                new[0] <= previous[0] and new[1] >= previous[1]
            )
            if contained:
                rising = not up if len(standard) == 1 else previous[1] > standard[-2][1]
                choose = max if rising else min
                standard[-1] = (
                    choose(previous[0], new[0]),
                    choose(previous[1], new[1]),
                    previous[2] + new[2],
                )
            else:
                standard.append(new)
        else:
            standard.append(new)
        if len(standard) >= 3:
            x, y, z = standard[-3:]
            found = (
                y[0] < min(x[0], z[0]) and y[1] < min(x[1], z[1])
                if up
                else y[0] > max(x[0], z[0]) and y[1] > max(x[1], z[1])
            )
            if found:
                saved = [
                    (e.low, e.high, e.source_indices) for e in proof.second_sequence
                ]
                assert saved == standard[-3:], "second_sequence_reconstruction"
                return
        crossed = pen_value.high > extreme if up else pen_value.low < extreme
        assert not crossed, "second_sequence_borrowed_after_invalidation"
    raise AssertionError("second_sequence_not_reconstructible")


def check_first_sequence(proof, strokes):
    """Rebuild the protected pivot's complete post-boundary feature chain.

    L071 keeps the left reference separate but subjects the middle and right
    to inclusion. Start at the actual boundary's first reverse pen, consume
    every physical pen, and stop at the first independent right element.
    Tuple arithmetic is independent of production feature/pivot helpers.

    An inherited boundary retains its parent's STRICT second-sequence context
    (L077/L078). The parent reconstruction checks it; restarting that context
    as an ordinary first sequence would impose a different inclusion rule.
    """
    if proof.parent_key is not None:
        return
    pivot = proof.end_index + 1
    first = strokes[pivot]
    up = proof.direction == "up"
    assert pivot + 2 <= proof.witness_index, "first_sequence_short_reverse"
    third = strokes[pivot + 2]
    assert max(first.low, third.low) <= min(first.high, third.high), (
        "first_sequence_reverse_overlap"
    )
    expected = (first.low, first.high, (pivot,))
    for i in range(pivot + 1, proof.witness_index + 1):
        item = strokes[i]
        if item.type == proof.direction:
            crossed = item.high > first.high if up else item.low < first.low
            assert not crossed, "first_sequence_borrowed_after_extension"
            continue
        low, high, sources = expected
        included = (low <= item.low and high >= item.high) or (
            item.low <= low and item.high >= high
        )
        if included:
            choose = max if up else min
            expected = (choose(low, item.low), choose(high, item.high), sources + (i,))
            continue
        saved = [(e.low, e.high, e.source_indices) for e in proof.first_sequence[1:]]
        assert saved == [expected, (item.low, item.high, (i,))], (
            "first_sequence_reconstruction"
        )
        outward = item.low < low and item.high < high if up else (
            item.low > low and item.high > high
        )
        assert outward, "first_sequence_right_direction"
        return
    raise AssertionError("first_sequence_not_reconstructible")


def signature(lines):
    return {
        (line.start_line.index, line.end_line.index, line.type): line.locked_at
        for line in lines
        if line.done
    }


def first_failure(points):
    values = [pen(i, a, b) for i, (a, b) in enumerate(zip(points, points[1:]))]
    prior = {}
    for end in range(3, len(values) + 1):
        calc = XdCalculator()
        lines = calc.calculate(values[:end])
        current = signature(lines)
        try:
            assert all(current.get(key) == time for key, time in prior.items()), (
                "confirmed_prefix_changed"
            )
            assert all(
                time == values[end - 1].locked_at
                for key, time in current.items()
                if key not in prior
            ), "confirmation_backdated"
            check_evidence(calc, values[:end])
        except AssertionError as exc:
            return {
                "reason": str(exc),
                "points": points[: end + 1],
                "prefix_pens": end,
                "previous": str(prior),
                "current": str(current),
                "evidence": [asdict(p) for p in calc.evidence],
            }
        prior = current
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", type=int, default=4)
    parser.add_argument("--points", type=int, default=12)
    parser.add_argument(
        "--walks",
        type=int,
        default=0,
        help="Use this many random walks instead of bounded enumeration",
    )
    parser.add_argument("--seed", type=int, default=19270916)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output/segment_audit/bounded_model.json"
    )
    args = parser.parse_args()
    if args.points < 4 or args.levels < 2 or args.walks < 0:
        parser.error("points must be >= 4, levels >= 2 and walks >= 0")
    hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in (
            "src/chanlun/core/xd_calculator.py",
            "src/chanlun/core/segment_evidence.py",
            "script/check_segment_model.py",
        )
    }
    started = perf_counter()
    checked = 0
    failure = None

    def visit(points, values, prior):
        nonlocal checked, failure
        if len(values) >= 3:
            calc = XdCalculator()
            lines = calc.calculate(values)
            current = signature(lines)
            checked += 1
            try:
                assert all(current.get(key) == time for key, time in prior.items()), (
                    "confirmed_prefix_changed"
                )
                assert all(
                    time == values[-1].locked_at
                    for key, time in current.items()
                    if key not in prior
                ), "confirmation_backdated"
                check_evidence(calc, values)
            except AssertionError:
                failure = first_failure(points)
                return
            prior = current
        if len(points) >= args.points:
            return
        up = len(values) % 2 == 0
        for price in range(args.levels):
            if price > points[-1] if up else price < points[-1]:
                visit(
                    points + [price],
                    values + [pen(len(values), points[-1], price)],
                    prior,
                )
                if failure:
                    return

    if args.walks:
        rng = random.Random(args.seed)
        for _ in range(args.walks):
            points = [0]
            sign = rng.choice((-1, 1))
            for i in range(args.points - 1):
                points.append(
                    points[-1] + sign * (-1 if i % 2 else 1) * rng.randint(1, 40)
                )
            failure = first_failure(points)
            checked += failure["prefix_pens"] - 2 if failure else args.points - 3
            if failure:
                break
    else:
        for first in range(args.levels - 1):
            visit([first], [], {})
            if failure:
                break
    result = {
        "mode": "random" if args.walks else "exhaustive",
        "levels": None if args.walks else args.levels,
        "walks": args.walks or None,
        "seed": args.seed if args.walks else None,
        "first_direction": "both" if args.walks else "up",
        "maximum_points": args.points,
        "checked_prefixes": checked,
        "seconds": perf_counter() - started,
        "failure": failure,
        "implementation_sha256": hashes,
        "all_input_equivalence_proven": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(1 if failure else 0)


if __name__ == "__main__":
    main()
