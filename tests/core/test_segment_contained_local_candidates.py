"""Keep the actual L079 figure distinct from changes to its earlier shape."""

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import geometry, strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_lesson79_lower_relation_rejects_an_ordinary_local_top(mirror):
    # In the actual figure P5 < P7, so 45 contains 67 before 89 is examined.
    values = strokes([8,2,7,1,11,3,6,4,10,5,9,0],mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0,3,True),(3,10,False)]
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_reversing_the_drawn_five_seven_order_changes_the_feature_context(mirror):
    # This was incorrectly assigned the same answer by the old order-family
    # generator. P5 > P7 makes 45/67 independent; a complete gapless standard
    # top [3,6], [5,10], [0,9] now exists. This is a derived variant, not an
    # extra author-answered figure.
    values = strokes([8,2,7,1,11,4,6,3,10,5,9,0],mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0,3,True),(3,8,True),(8,11,False)]
    assert calc.evidence[1].reference_mode == "standard-local"
    check_evidence(calc, values)
