"""Acceptance cases supplied by the user for review charts 01--05, 2026-09-17.

REVIEWED preserves the historical user decisions, not author quotations. Later
source-based corrections are explicit below; they never rewrite that history.
"""

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import CASES, geometry, strokes


REVIEWED = {
    "01_B": ([0, 10, 6, 14, 4, 12, 11, 13, 3], [(0, 3, True), (3, 8, False)]),
    "01_B_extended": ([0, 10, 6, 14, 4, 12, 11, 13, 3, 15, 8],
                       [(0, 3, True), (3, 8, False)]),
    "02_P11_A": ([0, 10, 6, 18, 13, 16, 11, 14, 11, 13, 12, 14],
                  [(0, 3, True), (3, 6, True), (6, 11, False)]),
    "02_P14_user": ([0, 10, 6, 18, 13, 16, 11, 14, 11, 13, 12, 14, 12.5, 13, 10],
                     [(0, 3, True), (3, 6, True), (6, 11, True), (11, 14, False)]),
    "03_P5_13": ([0, 10, 6, 14, 4, 13, 3], [(0, 3, True), (3, 6, False)]),
    "03_P5_14": ([0, 10, 6, 14, 4, 14, 3], [(0, 5, False)]),
    "03_P5_15": ([0, 10, 6, 14, 4, 15, 3], [(0, 5, False)]),
}

# Later full-rule audit, L07176-106: in 14->4->14->3 the third pen extends
# the first end before any STRICT origin return. Equal-origin inclusion must
# not erase this sufficient completion. Keep the prior decision above for
# comparison; docs/segment_rules.md describes the current rule.
SOURCE_REVISIONS_20260921 = {"03_P5_14": [(0, 3, True), (3, 6, False)]}


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("name", REVIEWED)
def test_reviewed_cases_with_explicit_later_source_corrections(name, mirror):
    points, expected = REVIEWED[name]
    expected = SOURCE_REVISIONS_20260921.get(name, expected)
    values = strokes(points, mirror)
    calculator = XdCalculator()
    assert geometry(calculator.calculate(values)) == expected
    check_evidence(calculator, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_reviewed_equal_boundary_keeps_first_two_segments_when_third_completes(mirror):
    values = strokes(REVIEWED["02_P14_user"][0], mirror)
    calculator = XdCalculator()
    earlier = calculator.calculate(values[:11])
    frozen = [(line.start_line.index, line.end_line.index, line.locked_at)
              for line in earlier if line.done]
    lines = calculator.calculate(values)
    assert [(line.start_line.index, line.end_line.index, line.locked_at)
            for line in lines[:2]] == frozen
    assert lines[2].done and lines[2].locked_at == values[13].locked_at


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("name", ["79_upper", "79_lower", "79_equal9and7",
                                  "79_lower_completed_reverse_triple", "81_reference"])
def test_review_does_not_remove_existing_reference_or_contained_stem_rules(name, mirror):
    points, expected = CASES[name]
    assert geometry(XdCalculator().calculate(strokes(points, mirror))) == expected


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("initial_high", [13, 13.5, 14, 14.5, 15])
def test_origin_retest_does_not_authorize_a_lower_internal_turn(initial_high, mirror):
    points = list(REVIEWED["02_P14_user"][0])
    points[7] = initial_high
    values = strokes(points, mirror)
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    expected = ([(0, 3, True), (3, 6, True), (6, 11, True), (11, 14, False)]
                if initial_high <= 14 else
                [(0, 3, True), (3, 6, True), (6, 13, False)])
    assert geometry(lines) == expected
    check_evidence(calculator, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_retest_proof_cannot_be_relabelled_as_a_strong_first_break(mirror):
    from dataclasses import replace

    values = strokes(REVIEWED["02_P14_user"][0], mirror)
    calculator = XdCalculator()
    calculator.calculate(values)
    before, parent, proof = calculator.evidence
    assert proof.rule == "origin-extreme-retest"
    assert proof.first_sequence[0].source_indices == (9,)
    calculator.evidence = (before, parent, replace(proof, rule="first-pen-break"))
    with pytest.raises(AssertionError, match="local_first_break"):
        check_evidence(calculator, values)
