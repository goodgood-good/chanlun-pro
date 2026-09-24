"""L065 226-244 / L067 49-118 / L071 100-124 source-derived examples.

Sources are the corresponding lesson files under D:/缠论/chanlun_lesson_corpus.
These are deductions, not author-labelled figures. The audit probe remains a
cross-check that the production correction implements the reviewed policy.
"""

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.probe_closed_gap_continuation import ClosedGapContinuationProbe
from tests.core.test_segment_source_rules import strokes, geometry


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("first_end,extension", [(8, 7), (10, 9)])
def test_l065_overlap_and_touch_are_distinct_from_far_edge_crossing(
    mirror, first_end, extension
):
    # L065: d3 <= g1 (8 <= 10 or 10 <= 10), mirrored for a down segment.
    # L071: third reversal ends past the first ending before a strict return
    # beyond its origin. This is a deduction, not an author-labelled figure.
    points = [100, 110, 106, 114, 100 + first_end, 114, 100 + extension]
    values = strokes(points, mirror)
    current = XdCalculator()
    current.calculate(values[:-1])
    assert not current.evidence
    current.calculate(values)
    proposed = ClosedGapContinuationProbe()
    proposed.calculate(values[:-1])
    assert not proposed.evidence
    proposed.calculate(values)
    assert geometry(proposed.xds) == [(0, 3, True), (3, 6, False)]
    assert proposed.xds[0].locked_at == values[5].locked_at
    assert proposed.evidence[0].first_pen_continuation.reference_context == "record"
    assert current.evidence == proposed.evidence
    assert geometry(current.xds) == geometry(proposed.xds)


@pytest.mark.parametrize("mirror", [False, True])
def test_gap_without_contact_does_not_receive_the_l065_route(mirror):
    values = strokes([100, 110, 106, 114, 112, 114, 111], mirror)
    proposed = ClosedGapContinuationProbe()
    proposed.calculate(values)
    assert not proposed.evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_return_still_prevents_the_l071_completion(mirror):
    values = strokes([100, 110, 106, 114, 108, 115, 107], mirror)
    proposed = ClosedGapContinuationProbe()
    proposed.calculate(values)
    assert not any(p.end_index == 2 for p in proposed.evidence)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("last", [108.5, 107])
def test_normalized_gap_is_not_bypassed_by_a_later_contained_exit(mirror, last):
    # Raw [107,112] intersects left [100,108], but inclusion produces
    # [109,112]. The later exit must not erase that second-case context.
    values = strokes([96, 108, 100, 112, 107, 111, 109, 112, last], mirror)
    proposed = ClosedGapContinuationProbe()
    proposed.calculate(values)
    assert not any(p.end_index == 2 for p in proposed.evidence)
