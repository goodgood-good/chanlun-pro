"""Both L067 cases apply to a qualified chronological standard local turn.

Actual Q0001 pen prices are from the frozen BJ.920019 5m audit. Text references:
D:/缠论/chanlun_lesson_corpus/L067... lines 49-109; L071... lines 172-208;
L078... lines 208-220 and 280-313. Existing L079/L081 figures are separate
regressions and must retain their reference and pending-hypothesis constraints.
"""

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import geometry, strokes


Q0001 = [1303, 1434, 1382, 1395, 1339, 1427, 1408, 1420,
         1345, 1365, 1335, 1366, 1340, 1394, 1362, 1379, 1360]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("first_extreme", [1434, 1427])
def test_local_gap_requires_f2_even_below_or_equal_to_initial_one_pen_extreme(mirror, first_extreme):
    points = list(Q0001)
    points[1] = first_extreme
    values = strokes(points, mirror)
    calc = XdCalculator()
    before = calc.calculate(values[:12])
    # A directional live tail may end away from its internal extreme. The
    # actual boundary hypothesis is separate and still waits for F2.
    assert geometry(before) == [(0, 11 if first_extreme == 1434 else 5, False)]
    assert calc.tail_state.projection_reason == ("latest-same-direction" if first_extreme == 1434 else "directional-extreme")
    assert calc.tail_state.reason == "waiting-second-feature"
    assert calc.tail_state.candidate_index == 4
    assert not calc.evidence

    after = calc.calculate(values[:13])
    assert geometry(after) == [(0, 5, True), (5, 10, True), (10, 13, False)]
    parent, child = calc.evidence
    assert parent.reference_mode == "standard-local"
    assert parent.initial_gap and len(parent.second_sequence) == 3
    assert parent.first_sequence[0].source_indices == (3,)
    assert parent.witness_index == child.witness_index == 12
    assert child.parent_key == parent.key
    assert child.second_sequence == ()
    assert after[0].locked_at == after[1].locked_at == values[12].locked_at
    frozen = [(s.start_line.index, s.end_line.index, s.locked_at) for s in after if s.done]
    calc.calculate(values)
    assert geometry(calc.xds) == [(0, 5, True), (5, 10, True), (10, 13, True), (13, 16, False)]
    assert [(s.start_line.index, s.end_line.index, s.locked_at) for s in calc.xds[:2]] == frozen
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_local_gap_extension_before_f2_does_not_confirm_an_interior_boundary(mirror):
    values = strokes(Q0001[:9] + [1430], mirror)
    calc = XdCalculator()
    assert not any(s.done for s in calc.calculate(values))
    assert calc.evidence == ()
    assert geometry(calc.xds) == [(0, 9, False)]
    assert calc.tail_state.projection_reason == "latest-same-direction"


@pytest.mark.parametrize("mirror", [False, True])
def test_local_gap_cannot_lock_without_every_witness_pen(mirror):
    values = strokes(Q0001[:14], mirror)
    values[12].locked_at = None
    calc = XdCalculator()
    calc.calculate(values)
    assert not any(s.done for s in calc.xds)
    assert all(s.locked_at is None for s in calc.xds)
    values[12].locked_at = values[11].locked_at
    calc.calculate(values)
    assert all(s.done for s in calc.xds[:2])
    check_evidence(calc, values)
