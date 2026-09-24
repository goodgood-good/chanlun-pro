"""Bounded exhaustive checks of causal segment construction.

Finite price alphabets cover equality and containment patterns; they do not
prove equivalence to every possible original-theory figure. This checker does
not call production feature/fractal helpers when validating returned evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
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
        if position:
            predecessor = calc.evidence[position - 1]
            if (predecessor.parent_key is None and not predecessor.second_sequence
                    and predecessor.reverse_segment is None and predecessor.return_segment is None):
                formation_sources = []
                if predecessor.first_sequence:
                    formation_sources.extend(predecessor.first_sequence[-1].source_indices)
                if predecessor.first_break_evidence is not None:
                    formation_sources.append(predecessor.first_break_evidence.extension_index)
                assert not formation_sources or b >= max(formation_sources), 'successor_splits_predecessor_formation'
        origin = proof.origin_evidence
        if origin is not None:
            assert position == 0 and origin.selected_index == a, "origin_recovery_scope"
            assert origin.initial_index + 1 == a, "origin_recovery_extreme"
            assert a + 2 <= origin.witness_index <= witness, "origin_recovery_witness"
            first = strokes[origin.initial_index]
            up = first.type == "up"
            assert first.end.val == strokes[a].start.val, "origin_recovery_price"
            for i in range(origin.initial_index + 2, origin.witness_index + 1):
                p = strokes[i]
                if p.type == first.type:
                    assert not (p.end.val > first.end.val if up else p.end.val < first.end.val), "established_origin_reanchored"
                else:
                    crossed = p.end.val < first.start.val if up else p.end.val > first.start.val
                    assert crossed == (i == origin.witness_index), "origin_crossing_order"
        if proof.first_pen_continuation is not None:
            check_first_pen_continuation(calc.evidence, position, strokes)
            continue
        assert proof.rule != 'first-pen-continuation', 'first_continuation_certificate_missing'
        if proof.return_segment is not None:
            check_return_segment(calc.evidence, position, strokes)
            continue
        assert proof.rule != 'completed-return-segment', 'return_proof_certificate_missing'
        if proof.reverse_segment is not None:
            check_reverse_segment(calc.evidence, position, strokes)
            continue
        assert proof.rule != 'completed-reverse-segment', 'reverse_proof_certificate_missing'
        left, middle, right = proof.first_sequence
        inherited_break = proof.rule == 'established-predecessor-reverse-break'
        if inherited_break:
            assert position > 0, 'established_predecessor_missing'
            previous = calc.evidence[position - 1]
            assert proof.predecessor_key == previous.key, 'established_predecessor_identity'
            assert previous.end_index + 1 == a and previous.direction != proof.direction, 'established_predecessor_continuity'
            assert not proof.parent_key and not proof.second_sequence, 'established_predecessor_wrong_context'
            assert proof.first_break_witness_index is not None, 'reverse_continuation_witness_missing'
            direct = proof.direct_first_break
            assert direct is not None, 'raw_first_break_certificate_missing'
            assert (direct.first_pen_index, direct.reference_index, direct.overlap_third_index) == (b + 1, b - 1, b + 3), 'raw_first_break_source_indices'
            assert direct.predecessor_key == previous.key, 'raw_first_break_predecessor'
            assert direct.extension_index == proof.first_break_witness_index, 'raw_first_break_certificate_witness'
        elif proof.direction == "up":
            assert middle.high > max(left.high, right.high), "first_top"
        else:
            assert middle.low < min(left.low, right.low), "first_bottom"
        if not inherited_break:
            assert proof.direct_first_break is None, 'unexpected_raw_first_break_certificate'
        check_first_reference(proof, strokes)
        check_first_sequence(proof, strokes)
        assert max(left.source_indices) < min(middle.source_indices), "left_ownership"
        assert max(middle.source_indices) < min(right.source_indices), "right_ownership"
        features = (*proof.first_sequence, *proof.second_sequence)
        features += tuple(e for decision in proof.second_sequence_breaks for e in decision.fractal)
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
        # Reconstruct BOTH price facts, without calling the production gap
        # helper. A covering reversal is distinct from ordinary overlap.
        standard_gap = max(left.low, middle.low) > min(left.high, middle.high)
        actual_gap = standard_gap
        if proof.parent_key is None:
            gap = proof.gap_context
            assert gap is not None, "gap_context_missing"
            first = strokes[b + 1]
            assert (gap.raw_first.low, gap.raw_first.high, gap.raw_first.source_indices,
                    gap.raw_first.low_source_index, gap.raw_first.high_source_index) == (
                        first.low, first.high, (b + 1,), b + 1, b + 1
                    ), "raw_first_evidence"
            raw_gap = max(left.low, first.low) > min(left.high, first.high)
            assert gap.raw_gap == raw_gap, "raw_gap_evidence"
            assert gap.standard_gap == standard_gap, "standard_gap_evidence"
            covers = first.low <= left.low and first.high >= left.high
            turns = first.high > left.high if proof.direction == "up" else first.low < left.low
            protected = covers and turns
            assert gap.basis == ("protected-first-reversal" if protected else "standard-first-feature"), (
                "gap_classification_basis"
            )
            actual_gap = raw_gap if protected else standard_gap
        else:
            assert proof.gap_context is None, "inherited_first_gap_context"
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
            assert not proof.second_sequence_breaks, "unexpected_second_sequence_breaks"


def check_reverse_segment(evidence, position, strokes):
    """Check a connected backward proof without invoking a production scanner.

    The next iteration of check_evidence independently reconstructs the exact
    child's ordinary/F2 feature proof. No child may assume this parent's key,
    use a recovered origin, or silently change after establishing the parent.
    """
    proof = evidence[position]
    cert = proof.reverse_segment
    assert proof.rule == 'completed-reverse-segment', 'reverse_proof_rule'
    assert proof.return_segment is None, 'reverse_proof_mixed_certificate'
    assert proof.first_pen_continuation is None, 'reverse_proof_mixed_continuation'
    assert position > 0, 'reverse_proof_predecessor_missing'
    previous = evidence[position - 1]
    assert previous.key == proof.predecessor_key == cert.predecessor_key, 'reverse_proof_predecessor'
    assert previous.end_index + 1 == proof.start_index and previous.direction != proof.direction, 'reverse_proof_continuity'
    first = proof.end_index + 1
    assert (cert.first_pen_index, cert.reference_index, cert.overlap_third_index) == (first, first - 2, first + 2), 'reverse_proof_source_indices'
    reference, reversal, third = (strokes[i] for i in (first - 2, first, first + 2))
    up = proof.direction == 'up'
    assert reversal.type != proof.direction, 'reverse_proof_direction'
    assert reversal.low < reference.low if up else reversal.high > reference.high, 'reverse_proof_first_break'
    assert max(reference.low, reversal.low) <= min(reference.high, reversal.high), 'reverse_proof_initial_gap'
    assert max(third.low, reversal.low) <= min(third.high, reversal.high), 'reverse_proof_first_third_overlap'
    assert not proof.first_sequence and not proof.second_sequence and not proof.second_sequence_breaks, 'reverse_proof_mislabelled_fractal'
    assert proof.parent_key is None and proof.origin_evidence is None and proof.pivot_stem is None, 'reverse_proof_wrong_context'
    assert proof.gap_context is None and not proof.initial_gap, 'reverse_proof_gap_context'
    assert proof.direct_first_break is None and proof.first_break_witness_index is None, 'reverse_proof_mislabelled_extension'
    assert position + 1 < len(evidence), 'reverse_proof_successor_missing'
    child = evidence[position + 1]
    assert cert.successor == child, 'reverse_proof_successor_changed'
    assert child.start_index == first and child.direction != proof.direction, 'reverse_proof_successor_continuity'
    assert child.origin_evidence is None and child.predecessor_key is None and child.parent_key is None, 'reverse_proof_circular_child'
    assert child.reverse_segment is None and child.return_segment is None and child.direct_first_break is None, 'reverse_proof_circular_certificate'
    assert proof.witness_index == child.witness_index, 'reverse_proof_witness'
    for i in range(first + 1, proof.witness_index + 1, 2):
        assert not (strokes[i].end.val > reversal.start.val if up else strokes[i].end.val < reversal.start.val), 'reverse_proof_origin_return'


def check_return_segment(evidence, position, strokes):
    """Validate L078's completed internal return independently of the parent.

    Unlike the reverse-child certificate, its return is not asserted to be the
    next emitted segment. Its own immutable standalone proof is reconstructed
    from original pens by the same independent feature checker, with no parent
    prerequisite or recovered observation origin available to that proof.
    """
    proof = evidence[position]
    cert = proof.return_segment
    assert proof.rule == 'completed-return-segment', 'return_proof_rule'
    assert proof.first_pen_continuation is None, 'return_proof_mixed_continuation'
    assert position > 0, 'return_proof_predecessor_missing'
    previous = evidence[position - 1]
    assert previous.key == proof.predecessor_key == cert.predecessor_key, 'return_proof_predecessor'
    assert previous.end_index + 1 == proof.start_index and previous.direction != proof.direction, 'return_proof_continuity'
    first = proof.end_index + 1
    assert (cert.first_pen_index, cert.reference_index, cert.overlap_third_index) == (first, first - 2, first + 2), 'return_proof_source_indices'
    reference, reversal, third = (strokes[i] for i in (first - 2, first, first + 2))
    up = proof.direction == 'up'
    assert reversal.type != proof.direction, 'return_proof_direction'
    assert reversal.low < reference.low if up else reversal.high > reference.high, 'return_proof_first_break'
    assert max(reference.low, reversal.low) <= min(reference.high, reversal.high), 'return_proof_initial_gap'
    assert max(third.low, reversal.low) <= min(third.high, reversal.high), 'return_proof_first_third_overlap'
    assert not proof.first_sequence and not proof.second_sequence and not proof.second_sequence_breaks, 'return_proof_mislabelled_fractal'
    assert proof.parent_key is None and proof.origin_evidence is None and proof.pivot_stem is None, 'return_proof_wrong_context'
    assert proof.gap_context is None and not proof.initial_gap and proof.reverse_segment is None, 'return_proof_mixed_context'
    assert proof.direct_first_break is None and proof.first_break_witness_index is None, 'return_proof_mislabelled_extension'
    chain = cert.return_proofs
    assert 1 <= len(chain) <= 2, 'return_proof_chain_length'
    support = chain[0]
    assert support.start_index == first + 1 and support.direction == proof.direction, 'return_proof_fixed_origin'
    assert support.predecessor_key is None and support.origin_evidence is None and support.parent_key is None, 'return_proof_circular_support'
    assert all(p.return_segment is None and p.reverse_segment is None and p.direct_first_break is None for p in chain), 'return_proof_circular_certificate'
    assert bool(support.second_sequence) == (len(chain) == 2), 'return_proof_inheritance'
    assert proof.witness_index == max(p.witness_index for p in chain), 'return_proof_witness'
    for i in range(first + 1, proof.witness_index + 1, 2):
        assert not (strokes[i].end.val > reversal.start.val if up else strokes[i].end.val < reversal.start.val), 'return_proof_crossed_origin'
    check_evidence(SimpleNamespace(evidence=chain), strokes)


def check_first_break_evidence(proof, strokes):
    """Independently rebuild the selected effective edge and its first exit.

    This implements the user-approved normalized comparison using interval
    tuples only. It deliberately does not call the production scan helper.
    """
    receipt = proof.first_break_evidence
    assert receipt is not None, 'first_break_effective_receipt_missing'
    first, extension = receipt.first_pen_index, receipt.extension_index
    assert first == proof.end_index + 1, 'first_break_effective_origin'
    assert first + 2 <= extension <= proof.witness_index, 'first_break_effective_bounds'
    assert proof.first_break_witness_index == extension, 'first_break_effective_witness'
    raw = strokes[first]
    up = proof.direction == 'up'
    low, high, sources = raw.low, raw.high, (first,)
    low_source = high_source = first
    for i in range(first + 1, extension + 1):
        item = strokes[i]
        if item.type == proof.direction:
            assert not (item.high > high if up else item.low < low), 'first_break_effective_origin_returned'
            continue
        outward = item.end.val < low if up else item.end.val > high
        if outward:
            assert i == extension, 'first_break_effective_not_first_exit'
            saved = receipt.effective_first
            assert (saved.low, saved.high, saved.source_indices,
                    saved.low_source_index, saved.high_source_index) == (
                        low, high, sources, low_source, high_source), 'first_break_effective_interval'
            return
        assert i < extension, 'first_break_effective_did_not_extend'
        assert (low <= item.low and item.high <= high or
                item.low <= low and high <= item.high), 'first_break_effective_inclusion'
        if item.low > low if up else item.low < low:
            low, low_source = item.low, i
        if item.high > high if up else item.high < high:
            high, high_source = item.high, i
        sources += (i,)
    raise AssertionError('first_break_effective_unresolved')


def check_first_pen_continuation(evidence, position, strokes):
    """Literal L071 completion; reference reconstruction remains independent.

    A raw first/third certificate must not be required to resemble a standard
    three-element fractal, nor may it manufacture an eligible left reference.
    """
    proof = evidence[position]
    cert = proof.first_pen_continuation
    assert proof.rule == 'first-pen-continuation', 'first_continuation_rule'
    first = proof.end_index + 1
    assert (cert.first_pen_index,cert.third_pen_index) == (first,first+2), 'first_continuation_indices'
    assert first+2<=cert.extension_index<=proof.witness_index, 'first_continuation_witness'
    assert not proof.first_sequence and not proof.second_sequence and not proof.second_sequence_breaks, 'first_continuation_not_a_fractal'
    assert proof.return_segment is None and proof.reverse_segment is None and proof.direct_first_break is None, 'first_continuation_mixed_certificate'
    assert not proof.initial_gap and proof.gap_context is None and proof.parent_key is None, 'first_continuation_gap_context'
    raw, third, left = strokes[first],strokes[first+2],cert.reference
    up = proof.direction == 'up'
    assert max(raw.low,third.low)<=min(raw.high,third.high), 'first_continuation_reverse_overlap'
    assert max(raw.low,left.low)<=min(raw.high,left.high), 'first_continuation_reference_gap'
    if cert.reference_context in {'record','origin-retest'}:
        # L065's contact criterion, checked without the production helper.
        assert (raw.low <= left.high if up else raw.high >= left.low), 'first_continuation_not_a_break'
        effective = proof.first_break_evidence.effective_first
        covering = (raw.high > left.high and raw.low <= left.low if up
                    else raw.low < left.low and raw.high >= left.high)
        middle = raw if covering else effective
        assert max(left.low, middle.low) <= min(left.high, middle.high), 'first_continuation_normalized_gap'
    else:
        assert raw.low<left.low if up else raw.high>left.high, 'first_continuation_not_a_break'
    assert all(proof.start_index<=i<first for i in left.source_indices), 'first_continuation_reference_bounds'
    assert left.low_source_index in left.source_indices and left.high_source_index in left.source_indices, 'first_continuation_reference_ownership'
    assert left.low==strokes[left.low_source_index].low and left.high==strokes[left.high_source_index].high, 'first_continuation_reference_price'
    turns = raw.high>left.high if up else raw.low<left.low
    context=cert.reference_context
    assert context in {'record','local','established','origin-retest'}, 'first_continuation_reference_context'
    if context=='origin-retest':
        standard=[]
        for i in range(proof.start_index+1,first,2):
            item=(strokes[i].low,strokes[i].high)
            if standard:
                old=standard[-1]
                included=(old[0]<=item[0] and item[1]<=old[1]) or (item[0]<=old[0] and old[1]<=item[1])
                if included:
                    rising=up if len(standard)==1 else old[1]>standard[-2][1]
                    choose=max if rising else min
                    standard[-1]=(choose(old[0],item[0]),choose(old[1],item[1]))
                    continue
            standard.append(item)
        assert len(standard)<2 or not (raw.high>standard[-1][1] if up else raw.low<standard[-1][0]), 'origin_retest_overrides_eligible_standard_reference'
    if context=='established':
        assert position>0 and proof.predecessor_key==evidence[position-1].key, 'first_continuation_predecessor'
        assert evidence[position-1].end_index+1==proof.start_index and evidence[position-1].direction!=proof.direction, 'first_continuation_predecessor_continuity'
        assert not turns, 'first_continuation_wrong_established_context'
    else:
        assert turns and proof.predecessor_key is None, 'first_continuation_turn'
    def feature(p):
        return SimpleNamespace(low=p.low,high=p.high,source_indices=(p.index,),low_source_index=p.index,high_source_index=p.index)
    reference_view=replace(proof,rule=('established-predecessor-reverse-break' if context=='established'
                                      else 'origin-extreme-retest' if context=='origin-retest'
                                      else 'first-pen-break' if context=='local' else 'first-feature-fractal'),
                           first_sequence=(left,feature(raw),feature(third)))
    check_first_reference(reference_view,strokes)
    check_first_break_evidence(proof,strokes)
    assert proof.first_break_evidence.extension_index==cert.extension_index, 'first_continuation_effective_exit'
    assert strokes[cert.extension_index].type==raw.type, 'first_continuation_exit_direction'


def check_first_reference(proof, strokes):
    """Reconstruct the eligible standard reference before an ordinary pivot.

    L081 eligibility and L065 inclusion are the declared interpretation being
    checked. A local first-break context has a different reference role.
    """
    left, middle, _ = proof.first_sequence
    if proof.rule == 'established-predecessor-reverse-break':
        raw = strokes[proof.end_index - 1]
        first = strokes[proof.end_index + 1]
        assert (left.low, left.high, left.source_indices) == (raw.low, raw.high, (raw.index,)), 'reverse_break_reference'
        assert (first.low < raw.low if proof.direction == 'up' else first.high > raw.high), 'reverse_break_not_strong'
        return
    if proof.reference_mode == "standard-local":
        assert proof.parent_key is None and proof.pivot_stem is None, "local_standard_context"
        assert bool(proof.initial_gap) == bool(proof.second_sequence), "local_standard_gap_requires_second_sequence"
        standard = []
        up = proof.direction == "up"
        for i in range(proof.start_index + 1, min(middle.source_indices), 2):
            item = (strokes[i].low, strokes[i].high, (i,))
            if standard:
                old = standard[-1]
                contained = (old[0] <= item[0] and old[1] >= item[1]) or (item[0] <= old[0] and item[1] >= old[1])
                if contained:
                    rising = up if len(standard) == 1 else old[1] > standard[-2][1]
                    choose = max if rising else min
                    standard[-1] = (choose(old[0], item[0]), choose(old[1], item[1]), old[2] + item[2])
                    continue
            standard.append(item)
        assert len(standard) >= 2, "local_standard_shoulders"
        assert standard[-1] == (left.low, left.high, left.source_indices), "local_standard_reference"
        raw = strokes[min(middle.source_indices)]
        assert (raw.high > left.high if up else raw.low < left.low), "local_standard_strict_turn"
        return
    assert proof.reference_mode == "record", "unknown_reference_mode"
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
    # A local origin retest can now acquire a STANDARD gap after inclusion.
    # Its ending rule is then second-feature-fractal, while the reference
    # qualification is still an origin retest. Reconstruct that qualification
    # from prices; do not mistake every second-fractal proof for a strong break.
    strong = first.high > left.high and first.low < left.low
    if proof.rule == "origin-extreme-retest" or (
        proof.rule == "second-feature-fractal" and not strong
    ):
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

    Pure tuples retain interval ownership. An F2 hidden inside a contained raw
    first break must wait for its first physical exit (L071 109-124). Returning
    through its origin rejects that turn; extending its end establishes the
    direction without requiring a third feature fractal.
    The checks exercise the stated interpretation of L065/L067/L078/L081,
    rather than proving that interpretation equivalent to every original chart.
    """
    up = proof.direction == "up"
    pivot = proof.end_index + 1
    extreme = strokes[pivot].high if up else strokes[pivot].low
    standard = []
    ignored_until = -1
    decisions = []
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
        if len(standard) >= 3 and i >= ignored_until:
            x, y, z = standard[-3:]
            found = (
                y[0] < min(x[0], z[0]) and y[1] < min(x[1], z[1])
                if up
                else y[0] > max(x[0], z[0]) and y[1] > max(x[1], z[1])
            )
            if found:
                # Use endpoints and interval exits, independently of the
                # production feature helper and raw-break scanner.
                price = y[0] if up else y[1]
                raw_start = next(j for j in y[2] if (
                    strokes[j].low if up else strokes[j].high
                ) == price)
                first = strokes[raw_start]
                resolution, resolved_at = None, None
                same_side_ends = [first.end.val]
                for j in range(raw_start + 1, proof.witness_index + 1):
                    endpoint = strokes[j].end.val
                    if strokes[j].type == first.type:
                        edge = min(same_side_ends) if up else max(same_side_ends)
                        if endpoint > edge if up else endpoint < edge:
                            resolution, resolved_at = 'directional-extension', j
                            break
                        same_side_ends.append(endpoint)
                    elif endpoint < first.start.val if up else endpoint > first.start.val:
                        resolution, resolved_at = 'origin-return', j
                        break
                assert resolution is not None, "second_first_break_unresolved"
                if resolution == "origin-return" or resolved_at > i:
                    decisions.append((list(standard[-3:]), raw_start, raw_start - 2,
                                      i, resolution, resolved_at))
                if resolution == "origin-return":
                    ignored_until = max(i, resolved_at)
                    crossed = pen_value.high > extreme if up else pen_value.low < extreme
                    assert not crossed, "second_sequence_borrowed_after_invalidation"
                    continue
                saved = [
                    (e.low, e.high, e.source_indices) for e in proof.second_sequence
                ]
                assert saved == standard[-3:], "second_sequence_reconstruction"
                recorded = [([(e.low, e.high, e.source_indices) for e in d.fractal],
                             d.first_pen_index, d.reference_index, d.fractal_witness_index,
                             d.outcome, d.outcome_witness_index)
                            for d in proof.second_sequence_breaks]
                assert recorded == decisions, "second_first_break_history"
                assert max(i, resolved_at) <= proof.witness_index, "second_first_break_witness"
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
        left, middle, right = proof.first_sequence
        full = (middle.low > max(left.low, right.low) if up
                else middle.high < min(left.high, right.high))
        if proof.rule == 'established-predecessor-reverse-break':
            check_first_break_evidence(proof,strokes)
            assert proof.witness_index >= max(i,proof.first_break_witness_index), 'first_break_confirmation_before_evidence'
        elif full:
            assert proof.first_break_witness_index is None, "unnecessary_first_break_witness"
            assert proof.first_break_evidence is None, 'unnecessary_first_break_effective_receipt'
        else:
            covers = (first.high > left.high and first.low <= left.low if up
                      else first.low < left.low and first.high >= left.high)
            assert covers, "first_fractal_missing_opposite_coordinate"
            check_first_break_evidence(proof,strokes)
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
