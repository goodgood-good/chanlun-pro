"""Price-order transformations must include the raw gap-price provenance."""

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_corpus_interfaces import ordinal_check
from tests.core.test_segment_source_rules import strokes


@pytest.mark.parametrize("points", [
    [0, 10, 6, 14, 4, 12, 11, 13, 3],
    [-4, 8, 0, 12, 7, 11, 9, 10, 8.5, 10.5, 6, 9, 7, 10],
    [5, 21, 5, 11, 6, 21, 8, 19, 15, 19, 6, 15, 13, 23],
])
def test_order_and_reflection_preserve_complete_gap_evidence(points):
    values = strokes(points)
    calc = XdCalculator()
    calc.calculate(values)
    assert any(p.gap_context for p in calc.evidence)
    assert ordinal_check(values, calc) == 2
