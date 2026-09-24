"""L079 containment remains a reference-eligibility constraint.

The answered lower figure is covered in test_segment_source_rules. These two
extensions are deductions from its containment rule (local L079 286-295), not
additional author-labelled figures. Contact alone cannot renew an internal
boundary whose supposed reference is still owned by the contained stem.
"""

import pytest
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes, geometry


@pytest.mark.parametrize("mirror", [False, True])
def test_contained_contact_does_not_create_a_new_independent_reference(mirror):
    values = strokes([30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 24, 27, 23], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 3, True), (3, 12, False)]
    assert len(calc.evidence) == 1
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_contained_turn_waits_for_the_actual_new_break_context(mirror):
    values = strokes(
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 23, 28, 8, 10, 5], mirror
    )
    calc = XdCalculator()
    calc.calculate(values[:-1])
    assert len(calc.evidence) == 1
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 3, True), (3, 12, True), (12, 15, False)]
    proof = calc.evidence[1]
    assert (proof.end_index, proof.witness_index) == (11, 14)
    assert proof.first_break_evidence.first_pen_index == 12
    check_evidence(calc, values)
