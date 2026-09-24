"""Check that the search auditor can recognize a wrongly skipped valid turn."""

import pytest

from dataclasses import replace

from script.check_segment_candidates import (
    SearchAuditCalculator,
    check_gap_invalidation,
    local_first_fractal,
    skipped_local_fractal,
)
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import CASES, strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_skip_auditor_detects_lesson79_local_turn_if_scanner_were_to_omit_it(mirror):
    values = strokes(CASES["79_upper"][0], mirror)
    counts = {"interior_candidates": 0}
    # A fictitious skip from B's first scan through pen 10 would lose the
    # actual P8 boundary. The production scanner does not make that skip.
    proof = skipped_local_fractal(values, 3, 6, 10, counts)
    assert (proof.candidate, proof.witness, proof.has_gap) == (8, 10, False)
    assert counts["interior_candidates"] == 2


@pytest.mark.parametrize("mirror", [False, True])
def test_skip_auditor_includes_the_shifted_contained_pivot(mirror):
    values = strokes(CASES["79_lower_completed_reverse_triple"][0], mirror)
    proof = local_first_fractal(values, 3, 10, 12)
    assert (proof.candidate, proof.witness, proof.pivot_sources) == (10, 12, (7, 9))
    assert local_first_fractal(values, 3, 10, 11) is None


@pytest.mark.parametrize("mirror", [False, True])
def test_contained_price_source_cannot_replace_the_actual_break_context(mirror):
    from chanlun.core.xd_calculator import XdCalculator

    values = strokes(CASES["79_equal_contained_pivot_has_a_later_break_context"][0], mirror)
    assert local_first_fractal(values, 3, 10, 14) is None
    found = local_first_fractal(values, 3, 12, 14)
    assert (found.candidate, found.witness, found.pivot_sources) == (12, 14, (7, 9, 11))
    calc = XdCalculator()
    calc.calculate(values)
    check_evidence(calc, values)
    first, proof = calc.evidence
    supplier = proof.pivot_stem.low_source_index if mirror else proof.pivot_stem.high_source_index
    assert supplier == 9 and proof.end_index == 11
    calc.evidence = (first, replace(proof, end_index=supplier))
    with pytest.raises(AssertionError, match="pivot_stem_sources"):
        check_evidence(calc, values)


@pytest.mark.parametrize("corruption", ["missing", "wrong_sources", "wrong_interval"])
def test_evidence_auditor_rejects_an_unexplained_skipped_reference(corruption):
    from chanlun.core.xd_calculator import XdCalculator

    values = strokes(CASES["79_lower_completed_reverse_triple"][0])
    calculator = XdCalculator()
    calculator.calculate(values)
    check_evidence(calculator, values)
    first, proof = calculator.evidence
    stem = proof.pivot_stem
    stem = (
        None if corruption == "missing"
        else replace(stem, source_indices=(5, 9)) if corruption == "wrong_sources"
        else replace(stem, high=30)
    )
    calculator.evidence = (first, replace(proof, pivot_stem=stem))
    with pytest.raises(AssertionError):
        check_evidence(calculator, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(
    "corruption", ["missing_source", "wrong_fold", "truncated_middle", "later_right"]
)
def test_evidence_auditor_rebuilds_the_entire_first_feature_chain(corruption, mirror):
    from chanlun.core.xd_calculator import XdCalculator
    from chanlun.core.segment_evidence import FeatureEvidence

    # L065/L071: the post-boundary elements 3,5,7 form one chronological
    # contained middle; pen 9 is its first independent right element. All
    # corruptions retain real price suppliers and a visually valid top/bottom.
    values = strokes([0, 10, 6, 20, 4, 18, 9, 17, 10, 16, 5, 15, 3], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    check_evidence(calc, values)
    proof = calc.evidence[0]
    left, middle, right = proof.first_sequence
    assert middle.source_indices == (3, 5, 7)
    if corruption == "missing_source":
        middle = replace(middle, source_indices=(3, 7))
    elif corruption in ("wrong_fold", "truncated_middle"):
        # Keep the pen-5 edge instead of applying the later pen-7 inclusion.
        changes = {"high": -9, "high_source_index": 5} if mirror else {
            "low": 9, "low_source_index": 5
        }
        if corruption == "truncated_middle":
            changes["source_indices"] = (3, 5)
        middle = replace(middle, **changes)
    else:
        later = values[11]
        right = FeatureEvidence(later.low, later.high, (11,), 11, 11)
    calc.evidence = (replace(
        proof, first_sequence=(left, middle, right), witness_index=11,
    ),)
    with pytest.raises(AssertionError, match="first_sequence_reconstruction"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("high", [19, 20, 21])
def test_skip_auditor_distinguishes_a_local_peak_from_equal_or_lower_middle(
    high, mirror
):
    values = strokes([0, 30, 4, 20, 15, high, 5, 16, 2], mirror)
    proof = local_first_fractal(values, 0, 5, 7)
    if high == 21:
        assert (proof.candidate, proof.witness) == (5, 7)
    else:
        assert proof is None


@pytest.mark.parametrize("mirror", [False, True])
def test_skip_auditor_requires_the_whole_contained_stem_and_right_shoulder(mirror):
    values = strokes([0, 10, 6, 20, 4, 18, 9, 17, 10, 16, 5], mirror)
    assert local_first_fractal(values, 0, 3, len(values) - 2) is None
    proof = local_first_fractal(values, 0, 3, len(values) - 1)
    assert (proof.candidate, proof.witness) == (3, 9)


@pytest.mark.parametrize("mirror", [False, True])
def test_skip_auditor_recognizes_completion_at_the_selected_effective_edge(mirror):
    # The old fixed-end convention rejected this. The selected normalized
    # policy completes [6,10], [2,10], [8,12] at pen 11: its 12 exceeds the
    # effective 10, without waiting for the physical first pen's 14.
    points = [13, 8, 9, 6, 10, 2, 14, 9, 14, 8, 10, 8, 12, 1]
    values = strokes(points, mirror)
    assert local_first_fractal(values, 0, 5, 10) is None
    assert local_first_fractal(values, 0, 5, 11).witness == 11
    assert local_first_fractal(values, 0, 5, 12).witness == 11
    # Later data cannot postpone that already observed effective completion.
    continued = strokes(points[:-1] + [9, 15], mirror)
    assert local_first_fractal(continued, 0, 5, 12).witness == 11
    proof = local_first_fractal(continued, 0, 5, 13)
    assert proof is not None and proof.witness == 11


@pytest.mark.parametrize('mirror',[False,True])
def test_effective_completion_retains_the_declared_absorbed_reference(mirror):
    # R04: the raw [0,4] is already absorbed into the declared [2,4].
    # The new completion is legal using [2,4] itself, not by replacing it
    # with the absorbed raw [0,4]. Preserve that distinction in the receipt.
    points=[0,-7,4,2,4,0,5,-6,5,1,3,0,4,-4,6,3,10,2,8,0]
    values=strokes(points,mirror)
    raw=local_first_fractal(values,1,6,12)
    assert raw is not None and raw.witness==10
    reference=(-4,-2) if mirror else (2,4)
    declared=local_first_fractal(values,1,6,12,reference)
    assert declared.witness==10 and declared.left==reference
    from collections import Counter
    counts=Counter()
    calculator=SearchAuditCalculator(counts)
    calculator.calculate(values)
    proof=next(p for p in calculator.evidence if p.start_index==1)
    assert (proof.first_sequence[0].low,proof.first_sequence[0].high)==reference
    assert proof.first_sequence[0].source_indices==(2,4)
    check_evidence(calculator,values)


@pytest.mark.parametrize('mirror',[False,True])
def test_interior_candidate_also_needs_a_surviving_or_protected_reference(mirror):
    values=strokes([27,38,30,35,33,38,29,38,32,34,33,37,32,35,29,40],mirror)
    assert local_first_fractal(values,0,11,13) is not None
    from collections import Counter
    counts=Counter()
    earlier=skipped_local_fractal(values,0,5,13,counts,None,True)
    assert (earlier.candidate,earlier.witness)==(5,11)
    # The later turn is still ineligible under its actual standard shoulder.
    reference=(-38,-33) if mirror else (33,38)
    assert local_first_fractal(values,0,11,13,reference) is None


@pytest.mark.parametrize("mirror", [False, True])
def test_invalidation_auditor_rejects_a_resurrected_interior_boundary(mirror):
    values = strokes(CASES["78_new_turn_after_gap_invalidation"][0], mirror)
    counts = dict.fromkeys(
        (
            "extension_ranges",
            "pending_first_ranges",
            "interior_candidates",
            "invalidated_contexts",
            "invalidation_boundary_checks",
        ),
        0,
    )
    calculator = SearchAuditCalculator(counts)
    calculator.calculate(values)
    assert counts["invalidated_contexts"] == 1
    assert calculator.evidence[0].end_index == 12
    # Preserve the new witness time but move the boundary back into the
    # invalidated hypothesis, reproducing the old semantic error. Causal
    # timestamps alone would not detect this illegal context reuse.
    calculator.evidence = (replace(calculator.evidence[0], end_index=8),)
    with pytest.raises(AssertionError, match="boundary_inside_invalidated_context"):
        calculator.check_invalidation_boundaries(values)


@pytest.mark.parametrize("mirror", [False, True])
def test_invalidation_auditor_checks_second_fractal_before_the_same_pen_extension(
    mirror,
):
    values = strokes(CASES["81_lower7"][0], mirror)
    direction = values[0].type
    with pytest.raises(AssertionError, match="second_fractal_precedes_invalidation"):
        check_gap_invalidation(values, 2, direction, 8)


@pytest.mark.parametrize('mirror',[False,True])
def test_invalidation_auditor_rejects_a_normalized_f2_when_its_physical_turn_returned(mirror):
    # A normalized shape alone was incorrectly treated as completed by the
    # old pruning auditor. Its own outgoing turn returns before continuing.
    points=[0,2,-7,1,-3,5,2,10,6,13,10,21,14,17,9,10,8,20,10,11,7,18,12,22]
    values=strokes(points,mirror)
    check_gap_invalidation(values,10,'down' if mirror else 'up',22)
