"""Raw first-break containment must survive second-sequence normalization.

SH.600183's saved physical pen prices, indices 442 onward. L071 109-124,
L077 367-391 and L078 208-211 are the local text basis. These expected market
partitions are deductions, not an author-labelled market example.
"""

from dataclasses import replace
import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence, check_second_sequence
from tests.core.test_segment_source_rules import strokes, geometry


PREFIX = [
    80.42,
    77.58,
    80.0,
    75.90,
    77.09,
    76.0,
    82.12,
    80.95,
    82.29,
    80.69,
    81.99,
    81.27,
    82.54,
    81.28,
    83.68,
    80.61,
    82.74,
    81.21,
]
CONTINUATION = [84.46, 83.39, 85.85, 83.45, 99.94, 93.42, 94.97, 92.58]


@pytest.mark.parametrize("mirror", [False, True])
def test_covering_first_break_has_a_witnessed_effective_end_completion(mirror):
    # SZ.300755's opening: the apparent bottom has middle.high=23.60,
    # above left.high=23.49. The effective end shrinks from23.78 to23.60,
    # and the later23.76 supplies the new rule's actual completion evidence.
    points = [23.85, 23.0, 23.49, 22.61, 23.78, 22.9, 23.6, 23.0, 23.76]
    values = strokes(points, mirror)
    calc = XdCalculator()
    calc.calculate(values[:-1])
    assert not calc.evidence
    calc.calculate(values)
    proof = calc.evidence[0]
    assert proof.key[:2] == (0, 2) and proof.witness_index == 7
    receipt = proof.first_break_evidence
    assert receipt.extension_index == 7
    assert (receipt.effective_first.low, receipt.effective_first.high) == (
        (-23.6, -22.61) if mirror else (22.61, 23.6)
    )
    check_evidence(calc, values)
    calc.calculate(strokes(points + [23.2, 23.38, 21.25], mirror))
    assert calc.evidence[0] == proof
    assert calc.xds[0].locked_at == values[7].locked_at


@pytest.mark.parametrize("mirror", [False, True])
def test_contained_third_pen_cannot_confirm_a_normalized_second_fractal(mirror):
    values = strokes(PREFIX, mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert not calc.evidence
    assert not any(s.done for s in calc.xds)
    assert calc.tail_state.reason == "waiting-second-feature"


@pytest.mark.parametrize("mirror", [False, True])
def test_return_through_first_pen_origin_rejects_the_turn_and_keeps_searching(mirror):
    values = strokes(PREFIX + CONTINUATION, mirror)
    calc = XdCalculator()
    for end in range(len(PREFIX) - 1, len(values)):
        calc.calculate(values[:end])
        assert not calc.evidence
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 3, True), (3, 22, True), (22, 25, False)]
    parent, successor = calc.evidence
    assert parent.witness_index == successor.witness_index == 24
    (rejected,) = parent.second_sequence_breaks
    assert (
        rejected.first_pen_index,
        rejected.fractal_witness_index,
        rejected.outcome,
        rejected.outcome_witness_index,
    ) == (14, 16, "origin-return", 17)
    assert successor.parent_key == parent.key
    check_evidence(calc, values)
    calc.evidence = (replace(parent, second_sequence_breaks=()), successor)
    with pytest.raises(AssertionError, match="second_first_break_history"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_end_break_completes_the_same_candidate_without_a_third_fractal(mirror):
    values = strokes(PREFIX + [82.1, 80.2], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 3, True), (3, 14, True), (14, 19, False)]
    parent, successor = calc.evidence
    assert parent.witness_index == successor.witness_index == 18
    (decision,) = parent.second_sequence_breaks
    assert decision.outcome == "directional-extension"
    assert decision.fractal_witness_index == 16 and decision.outcome_witness_index == 18
    assert calc.xds[0].locked_at == values[18].locked_at
    check_evidence(calc, values)
    forged = replace(parent, witness_index=16, second_sequence_breaks=())
    with pytest.raises(AssertionError, match="second_first_break_unresolved"):
        check_second_sequence(forged, values)
    values[18].locked_at = None
    calc.calculate(values)
    assert not any(s.done for s in calc.xds)


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_edges_still_wait_for_the_first_strict_exit(mirror):
    points = PREFIX[:-1] + [80.61, 83.68, 80.61, 83.6]
    calc = XdCalculator()
    calc.calculate(strokes(points, mirror))
    assert not calc.evidence
    values = strokes(points + [80.2], mirror)
    calc.calculate(values)
    assert calc.evidence[0].witness_index == len(values) - 1
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_same_side_inclusion_allows_an_internal_first_pen_extreme(mirror):
    # The 80.61 raw low stays internal, while the normalized continuation
    # edge advances to 81.21. A later 80.90 can establish the direction.
    values = strokes(PREFIX + [82.1, 80.90], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert calc.evidence[0].witness_index == 18
    assert calc.evidence[1].end_index == 13
    check_evidence(calc, values)
