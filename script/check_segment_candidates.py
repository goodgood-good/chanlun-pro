"""Audit pruning of contained first-feature stems, independently of their scanner.

This checks one search-completeness obligation, not all original-theory candidate
selection. L065/L071 supply chronological inclusion; L067 requires a fractal;
L077/L079 supply the protected local first-break context. Source locations and
the current rule boundary are in docs/segment_rules.md.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import random
import sys
from time import perf_counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from chanlun.core.segment_evidence import PendingBoundary, SegmentEvidence
from chanlun.core.xd_calculator import (
    XdCalculator,
    _CandidateExtension,
    _GapConfirmationInvalidated,
)
from script.check_segment_model import pen, check_first_sequence


@dataclass(frozen=True)
class LocalFractal:
    candidate: int
    witness: int
    left: tuple
    middle: tuple
    right: tuple
    has_gap: bool
    pivot_sources: tuple[int, ...] = ()


def local_first_fractal(values, start, candidate, last, reference_interval=None):
    """Rebuild a strict local first-break fractal using raw interval tuples.

    A returned gapped fractal still needs its own second-sequence proof. Even
    that first fractal cannot hide wholly inside the pruned included stem.
    This function does not use production inclusion, pivot or fractal helpers.
    """
    if candidate < start + 3 or candidate + 2 > last:
        return None
    up = values[start].type == "up"
    left, first = values[candidate - 2], values[candidate]
    if reference_interval is not None:
        left = SimpleNamespace(low=reference_interval[0],high=reference_interval[1])
    pivot_sources = ()
    own = values[start:candidate:2]
    origin_retest = (
        first.high == own[0].high == max(p.high for p in own)
        if up else first.low == own[0].low == min(p.low for p in own)
    )

    def eligible(reference):
        turns = first.high > reference.high if up else first.low < reference.low
        strong = first.low < reference.low if up else first.high > reference.high
        overlap = max(first.low, reference.low) <= min(first.high, reference.high)
        return turns and (strong or (origin_retest and overlap))

    if not eligible(left):
        if reference_interval is not None:
            return None
        own = candidate - 1
        origin = own
        while origin - 2 >= start:
            earlier, later = values[origin - 2], values[origin]
            if earlier.low > later.low or earlier.high < later.high:
                break
            origin -= 2
        if origin == own or origin <= start:
            return None
        left = values[origin - 1]
        if not eligible(left):
            return None
        pivot_sources = tuple(range(origin, candidate, 2))
    third = values[candidate + 2]
    if max(first.low, third.low) > min(first.high, third.high):
        return None
    endpoint, anchor = first.start.val, values[start].start.val
    if not (endpoint > anchor if up else endpoint < anchor):
        return None
    low, high = first.low, first.high
    for i in range(candidate + 1, last + 1):
        item = values[i]
        if item.type == values[start].type:
            if item.high > first.high if up else item.low < first.low:
                return None
            continue
        strong = first.low < left.low if up else first.high > left.high
        effective_exit = item.end.val < low if up else item.end.val > high
        included = (low <= item.low and high >= item.high) or (
            item.low <= low and item.high >= high
        )
        # Under the selected rule an equal-origin incoming pen can contain
        # the effective middle while already extending its end. The direct
        # completion must be observed before including that same witness.
        if included and strong and effective_exit:
            return LocalFractal(candidate, i, (left.low,left.high), (low,high),
                                (item.low,item.high), False, pivot_sources)
        if included:
            choose = max if up else min
            low, high = choose(low, item.low), choose(high, item.high)
            continue
        outward = (
            item.low < low and item.high < high
            if up
            else item.low > low and item.high > high
        )
        if not outward:
            return None
        # Ordinary fractals need both coordinates. A protected covering first
        # break uses the evolving effective end under the user's convention;
        # the independent outward right just reconstructed provides that exit.
        full = (low > left.low and high > left.high if up
                else low < left.low and high < left.high)
        witness = i
        if not full:
            covering = (first.high > left.high and first.low <= left.low if up
                        else first.low < left.low and first.high >= left.high)
            if not covering:
                return None
            assert effective_exit
        return LocalFractal(
            candidate,
            witness,
            (left.low, left.high),
            (low, high),
            (item.low, item.high),
            # A covering first reversal retains its protected raw interval;
            # an origin-retest with ordinary overlap uses the standard middle.
            (max(left.low, first.low) > min(left.high, first.high)
             if first.low <= left.low and first.high >= left.high
             else max(left.low, low) > min(left.high, high)),
            pivot_sources,
        )
    return None


def skipped_local_fractal(values, start, first, last, counts, first_reference=None, canonical_after_first=False):
    standard=[]
    next_feature=start+1
    up=values[start].type=='up'
    for candidate in range(first, last - 1, 2):
        counts["interior_candidates"] += 1
        if canonical_after_first:
            while next_feature<candidate:
                item=(values[next_feature].low,values[next_feature].high)
                if standard and ((standard[-1][0]<=item[0] and item[1]<=standard[-1][1]) or
                                 (item[0]<=standard[-1][0] and standard[-1][1]<=item[1])):
                    rising=up if len(standard)==1 else standard[-1][1]>standard[-2][1]
                    choose=max if rising else min
                    standard[-1]=(choose(standard[-1][0],item[0]),choose(standard[-1][1],item[1]))
                else:
                    standard.append(item)
                next_feature+=2
        proof = local_first_fractal(values, start, candidate, last,
                                    first_reference if candidate == first else None)
        if first_reference is not None and candidate == first and proof is None:
            raw = local_first_fractal(values,start,candidate,last)
            if raw is not None:
                counts['different_reference_candidates_rejected']=counts.get('different_reference_candidates_rejected',0)+1
        if proof is not None and canonical_after_first and candidate>first and not proof.pivot_sources:
            declared=local_first_fractal(values,start,candidate,last,standard[-1])
            if declared is None:
                counts['different_reference_candidates_rejected']=counts.get('different_reference_candidates_rejected',0)+1
            proof=declared
        if proof is not None:
            return proof
    return None


def check_gap_invalidation(values, candidate_end, direction, witness):
    """An original-direction extension before F2 closes this hypothesis.

    Reconstruct every second-sequence element with tuples, including the
    'fractal before extension on the same pen' order required by L081.
    """
    up = direction == "up"
    pivot = candidate_end + 1
    extreme = values[pivot].high if up else values[pivot].low
    standard = []
    blocked_until = -1
    for i in range(pivot + 1, witness + 1):
        current = values[i]
        if current.type != direction:
            continue
        low, high = current.low, current.high
        if standard:
            previous = standard[-1]
            included = (previous[0] <= low and previous[1] >= high) or (
                low <= previous[0] and high >= previous[1]
            )
            if included:
                rising = not up if len(standard) == 1 else previous[1] > standard[-2][1]
                choose = max if rising else min
                merged_low,merged_high=choose(previous[0],low),choose(previous[1],high)
                standard[-1] = (merged_low,merged_high,
                                previous[2] if merged_low==previous[0] else i,
                                previous[3] if merged_high==previous[1] else i)
            else:
                standard.append((low, high, i, i))
        else:
            standard.append((low, high, i, i))
        if len(standard) >= 3 and i >= blocked_until:
            left, middle, right = standard[-3:]
            fractal = (
                middle[0] < min(left[0], right[0])
                and middle[1] < min(left[1], right[1])
                if up
                else middle[0] > max(left[0], right[0])
                and middle[1] > max(left[1], right[1])
            )
            if fractal:
                # Rebuild the physical successor separately. A full F2 whose
                # standard middle combines both sides of the turn cannot
                # borrow continuation from before its actual price source.
                first=middle[2] if up else middle[3]
                edge=values[first].end.val
                outcome=None
                for j in range(first+1,witness+1):
                    later=values[j]
                    if later.type==direction:
                        if later.end.val>edge if up else later.end.val<edge:
                            outcome=('extension',j)
                            break
                        edge=later.end.val
                    elif later.end.val<values[first].start.val if up else later.end.val>values[first].start.val:
                        outcome=('return',j)
                        break
                if outcome and outcome[0]=='return':
                    blocked_until=max(i,outcome[1])
                else:
                    assert not outcome, "second_fractal_precedes_invalidation"
        crossed = high > extreme if up else low < extreme
        if crossed:
            assert i == witness, "invalidation_missed_earlier_extension"
            return
    raise AssertionError("invalidation_without_original_direction_extension")


def check_first_gap_eligibility(values, start, candidate_end, direction, features):
    """Reconstruct the actual FIRST gapped fractal before granting F2 priority.

    Uses tuple/price comparisons and the independent first-sequence checker,
    not production inclusion, gap classification, or feature-scanning helpers.
    """
    left,middle,right=features
    up=direction=='up'
    assert (middle.high>max(left.high,right.high) and middle.low>max(left.low,right.low)
            if up else middle.high<min(left.high,right.high) and middle.low<min(left.low,right.low)), 'first_gap_eligibility_not_a_fractal'
    assert max(left.low,middle.low)>min(left.high,middle.high), 'first_gap_eligibility_no_gap'
    assert max(left.source_indices)<min(middle.source_indices) and max(middle.source_indices)<min(right.source_indices), 'first_gap_eligibility_order'
    pivot=middle.high_source_index if up else middle.low_source_index
    assert pivot==candidate_end+1, 'first_gap_eligibility_pivot'
    witness=max(right.source_indices)
    for e in features:
        assert all(start<=i<=witness and values[i].type!=direction for i in e.source_indices), 'first_gap_eligibility_sources'
        assert e.low_source_index in e.source_indices and e.high_source_index in e.source_indices, 'first_gap_eligibility_ownership'
        assert e.low==values[e.low_source_index].low and e.high==values[e.high_source_index].high, 'first_gap_eligibility_price'
    proof=SegmentEvidence(start,candidate_end,direction,'second-feature-fractal',witness,features,initial_gap=True)
    check_first_sequence(proof,values)
    return witness


class SearchAuditCalculator(XdCalculator):
    def __init__(self, counts):
        super().__init__()
        self.counts = counts
        self.conflict = None
        self.invalidations = []

    def calculate(self, values):
        self.conflict = None
        self.invalidations = []
        # A diagnostic replay needs the actual decisions even if its caller
        # supplies the same objects again. Production caching is unchanged.
        self._input_snapshot = None
        lines = super().calculate(values)
        recovery = self._origin_recovery
        if recovery is not None:
            hidden = skipped_local_fractal(
                values, recovery.initial_index, recovery.initial_index + 3,
                recovery.witness_index, self.counts,
            )
            if hidden is not None and not hidden.has_gap:
                self.conflict = {"reason": "origin_recovery_skipped_local_first_fractal", "proof": asdict(hidden)}
                raise AssertionError("skipped_local_first_fractal")
        self.check_invalidation_boundaries(values)
        return lines

    def check_invalidation_boundaries(self, values):
        for event in self.invalidations:
            for proof in self.evidence:
                if proof.start_index != event["segment_start"]:
                    continue
                self.counts["invalidation_boundary_checks"] += 1
                eligible = event.get("first_fractal_witness")
                if eligible is not None and proof.witness_index < eligible:
                    self.counts['pre_fractal_completions_preserved'] = self.counts.get('pre_fractal_completions_preserved',0)+1
                    continue
                if proof.end_index < event["witness"]:
                    self.conflict = {
                        "reason": "boundary_inside_invalidated_context",
                        "invalidation": event,
                        "proof": asdict(proof),
                        "points": [values[0].start.val] + [v.end.val for v in values],
                    }
                    raise AssertionError("boundary_inside_invalidated_context")

    def _record_invalidation(self, result, values, start, direction):
        if not isinstance(result, _GapConfirmationInvalidated):
            return
        event = {
            "segment_start": start,
            "candidate_end": result.candidate_index,
            "direction": direction,
            "witness": result.witness_index,
        }
        try:
            assert result.candidate_index is not None, "invalidation_missing_candidate"
            assert result.witness_index is not None, "invalidation_missing_witness"
            check_gap_invalidation(
                values, result.candidate_index, direction, result.witness_index
            )
            if result.first_sequence:
                eligible=check_first_gap_eligibility(values,start,result.candidate_index,direction,result.first_sequence)
                event.update(first_fractal_witness=eligible,first_sequence=[asdict(e) for e in result.first_sequence])
        except AssertionError as exc:
            self.conflict = {
                "reason": str(exc),
                "invalidation": event,
                "points": [values[0].start.val] + [v.end.val for v in values],
            }
            raise
        self.invalidations.append(event)
        self.counts["invalidated_contexts"] += 1

    def _try_end_r34(self, *args):
        result = super()._try_end_r34(*args)
        self._record_invalidation(result, args[0], args[1], args[2])
        return result

    def _try_end(self, *args):
        result = super()._try_end(*args)
        values, start, _, direction, _, _, first, references = args[:8]
        self._record_invalidation(result, values, start, args[3])
        return result

    def _resolve_first_search_outcome(self, values, start, direction, first, result,
                                      standard, context, incoming_reference, pivot_origins=None,
                                      predecessor=None):
        result = super()._resolve_first_search_outcome(
            values,start,direction,first,result,standard,context,incoming_reference,pivot_origins,predecessor,
        )
        # The declared left reference has already undergone same-side
        # inclusion. L067 49-55 / L071 184-208 do not permit replacing it
        # with an absorbed raw pen merely to manufacture a local shape.
        # The scanner's incoming reference is context, not its result; all
        # subsequent interval operations here remain independently rebuilt.
        reference = None
        for item in [incoming_reference]:
            current = (item['low'],item['high']) if isinstance(item,dict) else (item.low,item.high)
            if reference is None:
                reference=current
                continue
            contained=(reference[0]<=current[0] and current[1]<=reference[1]) or (current[0]<=reference[0] and reference[1]<=current[1])
            if contained:
                choose=max if direction=='up' else min
                reference=(choose(reference[0],current[0]),choose(reference[1],current[1]))
            elif current[1]>reference[1] if direction=='up' else current[0]<reference[0]:
                reference=current
        if context == 'standard' and standard:
            reference=(standard[-1]['low'],standard[-1]['high'])
        return self._check_pruned_first_context(
            result, values, start, first, context,
            reference if context in {'ordinary','standard'} else None,
        )

    def _check_pruned_first_context(self, result, values, start, first, context, first_reference=None):
        if isinstance(result, _CandidateExtension):
            # The extension pen is the next original-direction pen. Any
            # completed local right shoulder would precede it.
            last = result.witness_index - 1
            self.counts["extension_ranges"] += 1
            if context == "local":
                self.counts["local_extension_ranges"] = self.counts.get("local_extension_ranges", 0) + 1
            disposition = "extension"
        elif (
            isinstance(result, PendingBoundary)
            and result.reason == "waiting-first-feature"
        ):
            last = len(values) - 1
            self.counts["pending_first_ranges"] += 1
            if context == "local":
                self.counts["local_pending_first_ranges"] = self.counts.get("local_pending_first_ranges", 0) + 1
            disposition = "waiting-first-feature"
        else:
            return result
        proof = skipped_local_fractal(values, start, first, last, self.counts, first_reference, True)
        if proof is not None:
            self.conflict = {
                "reason": "skipped_local_first_fractal",
                "segment_start": start,
                "scan_from": first,
                "scan_through": last,
                "disposition": disposition,
                "context": context,
                "local_fractal": asdict(proof),
                "points": [values[0].start.val] + [v.end.val for v in values],
            }
            raise AssertionError("skipped_local_first_fractal")
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", type=int, default=4)
    parser.add_argument("--points", type=int, default=12)
    parser.add_argument("--walks", type=int, default=0)
    parser.add_argument("--seed", type=int, default=19270918)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/segment_audit/candidate_search.json",
    )
    args = parser.parse_args()
    if args.points < 4 or args.levels < 2 or args.walks < 0:
        parser.error("points must be >= 4, levels >= 2 and walks >= 0")
    counts = {
        "examined_inputs": 0,
        "extension_ranges": 0,
        "pending_first_ranges": 0,
        "interior_candidates": 0,
        "invalidated_contexts": 0,
        "invalidation_boundary_checks": 0,
        "local_extension_ranges": 0,
        "local_pending_first_ranges": 0,
    }
    failure = None
    started = perf_counter()

    def examine(values):
        nonlocal failure
        counts["examined_inputs"] += 1
        calc = SearchAuditCalculator(counts)
        try:
            calc.calculate(values)
        except AssertionError:
            if calc.conflict is None:
                raise
            failure = calc.conflict

    def visit(points, values, sign):
        if len(values) >= 3:
            examine(values)
            if failure:
                return
        if len(points) >= args.points:
            return
        up = len(values) % 2 == 0
        for price in range(args.levels):
            if price > points[-1] if up else price < points[-1]:
                visit(
                    points + [price],
                    values + [pen(len(values), sign * points[-1], sign * price)],
                    sign,
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
                    points[-1] + sign * (-1 if i % 2 else 1) * rng.randint(1, 12)
                )
            examine([pen(i, a, b) for i, (a, b) in enumerate(zip(points, points[1:]))])
            if failure:
                break
    else:
        for sign in (1, -1):
            for first in range(args.levels - 1):
                visit([first], [], sign)
                if failure:
                    break
            if failure:
                break
    result = {
        "mode": "random-whole-inputs" if args.walks else "exhaustive-prefixes",
        "levels": None if args.walks else args.levels,
        "maximum_points": args.points,
        "walks": args.walks or None,
        "seed": args.seed if args.walks else None,
        "first_direction": "both",
        "counts": counts,
        "seconds": perf_counter() - started,
        "failure": failure,
        "scope": "contained first-feature pruning and no boundary inside an invalidated second-sequence context",
        "all_input_theory_equivalence_proven": False,
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/chanlun/core/xd_calculator.py",
                "src/chanlun/core/segment_evidence.py",
                "script/check_segment_candidates.py",
                "script/check_segment_model.py",
            )
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(1 if failure else 0)


if __name__ == "__main__":
    main()
