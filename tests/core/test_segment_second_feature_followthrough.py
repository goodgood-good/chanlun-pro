"""A second standard fractal does not need another first-pen extension.

SH.600183's saved physical pen prices, indices 442 onward. The local source is
D:/缠论/chanlun_lesson_corpus/L067...85-109 and L077...388-418. L078's
directional/reverse-proof conditions remain separate. These market partitions
are deductions, not an author-labelled market example. Earlier expectations
incorrectly required a later first-pen exit after a qualified F2 already formed.
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
def test_contained_third_pen_confirms_a_complete_standard_second_fractal(mirror):
    values = strokes(PREFIX, mirror)
    calc = XdCalculator()
    calc.calculate(values)
    parent, successor = calc.evidence
    assert parent.witness_index == successor.witness_index == 16
    assert geometry(calc.xds) == [(0, 3, True), (3, 14, True), (14, 17, False)]
    # C has three overlapping physical pens 14..16. Its last extreme need
    # not pass its first extreme; F2 already establishes C's break of B.
    assert max(p.low for p in values[14:17]) <= min(p.high for p in values[14:17])
    assert values[16].low > values[14].low if not mirror else values[16].high < values[14].high
    assert parent.second_sequence_breaks == ()
    assert successor.parent_key == parent.key
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_later_origin_return_does_not_reject_an_already_completed_second_fractal(mirror):
    values = strokes(PREFIX + CONTINUATION, mirror)
    calc = XdCalculator()
    calc.calculate(values[:len(PREFIX) - 2])
    assert not calc.evidence
    calc.calculate(values[:len(PREFIX) - 1])
    frozen = tuple(calc.evidence)
    clocks = [x.locked_at for x in calc.xds[:2]]
    for end in range(len(PREFIX), len(values) + 1):
        calc.calculate(values[:end])
        assert calc.evidence[:2] == frozen
        assert [x.locked_at for x in calc.xds[:2]] == clocks
        check_evidence(calc, values[:end])


@pytest.mark.parametrize("mirror", [False, True])
def test_later_end_break_cannot_postpone_the_existing_confirmation(mirror):
    values = strokes(PREFIX + [82.1, 80.2], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 3, True), (3, 14, True), (14, 19, False)]
    parent, successor = calc.evidence
    assert parent.witness_index == successor.witness_index == 16
    assert parent.second_sequence_breaks == ()
    assert calc.xds[0].locked_at == values[16].locked_at
    check_evidence(calc, values)
    forged = replace(parent, witness_index=15)
    with pytest.raises(AssertionError, match="second_sequence_not_reconstructible"):
        check_second_sequence(forged, values)
    values[16].locked_at = None
    calc.calculate(values)
    assert not any(s.done for s in calc.xds)


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_first_pen_edge_does_not_add_a_new_confirmation_gate(mirror):
    points = PREFIX[:-1] + [80.61, 83.68, 80.61, 83.6]
    calc = XdCalculator()
    calc.calculate(strokes(points, mirror))
    assert calc.evidence[0].witness_index == 16
    frozen = calc.evidence[:2]
    values = strokes(points + [80.2], mirror)
    calc.calculate(values)
    assert calc.evidence[:2] == frozen
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_same_side_inclusion_allows_an_internal_first_pen_extreme(mirror):
    # The 80.61 raw low remains real price evidence. C's third pen ending
    # 81.21 is enough for F2; the later 80.90 is not required for A/B to end.
    values = strokes(PREFIX + [82.1, 80.90], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert calc.evidence[0].witness_index == 16
    assert calc.evidence[1].end_index == 13
    check_evidence(calc, values)
