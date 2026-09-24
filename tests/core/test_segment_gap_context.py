"""QQQ minute regression and the L067/L071/L075/L078 gap-context distinction.

The strong-reversal exception is an implementation deduction constrained by the
reviewed figure 01. Ordinary overlapping reversals retain L067's standard gap.
Local source paths and the distinction from author quotations are in the fix
report. Synthetic prices are not described as the author's original figures.
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import geometry, strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_ordinary_overlap_must_wait_for_a_second_fractal_after_inclusion(mirror):
    values = strokes([-4, 8, 0, 12, 7, 11, 9, 10, 8.5], mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0, 3, False)]
    assert calc.tail_state.reason == "waiting-second-feature"
    assert not calc.evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_new_extreme_invalidates_unconfirmed_ordinary_overlap(mirror):
    values = strokes([-4, 8, 0, 12, 7, 11, 9, 10, 8.5, 13, 8], mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0, 9, False)]
    assert not calc.evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_second_fractal_releases_parent_and_successor_with_explicit_gap_evidence(mirror):
    points = [-4, 8, 0, 12, 7, 11, 9, 10, 8.5, 10.5, 6, 9, 7, 10]
    values = strokes(points, mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0, 3, True), (3, 10, True), (10, 13, False)]
    parent, child = calc.evidence[:2]
    assert parent.initial_gap and parent.gap_context.standard_gap
    assert not parent.gap_context.raw_gap
    assert parent.gap_context.basis == "standard-first-feature"
    assert parent.gap_context.raw_first.source_indices == (3,)
    assert parent.witness_index == 12
    assert child.parent_key == parent.key and child.gap_context is None
    assert calc.xds[0].locked_at == calc.xds[1].locked_at == values[12].locked_at
    check_evidence(calc, values)
    # Both source interpretations must be retained. A checker that only follows
    # the effective boolean would accept this corrupted raw interval fact.
    bad = replace(parent.gap_context, raw_gap=True)
    calc.evidence = (replace(parent, gap_context=bad), child)
    with pytest.raises(AssertionError, match="raw_gap_evidence"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("first_low,protected", [(4, True), (6, True), (6.001, False), (7, False)])
def test_boundary_coverage_includes_equal_edge_but_excludes_plain_overlap(mirror, first_low, protected):
    values = strokes([0, 10, 6, 14, first_low, 12, 11, 13, 3], mirror)
    calc = XdCalculator()
    actual = geometry(calc.calculate(values))
    if protected:
        assert actual == [(0, 3, True), (3, 8, False)]
        gap = calc.evidence[0].gap_context
        assert gap.basis == "protected-first-reversal"
        assert not gap.raw_gap and gap.standard_gap
        assert not calc.evidence[0].initial_gap
        check_evidence(calc, values)
    else:
        assert actual == [(0, 3, False)]
        assert calc.tail_state.reason == "waiting-second-feature"


@pytest.fixture(scope="module")
def qqq():
    frame = pd.read_parquet(Path(__file__).parents[1] / "fixtures/QQQ.US_1m_20260818_20260917.parquet")
    cl = CL("QQQ.US", "1m", {"structure_price_quantum": "0.001"}, market="us")
    cl.process_klines(frame, last_bar_closed=False)
    return cl


@pytest.mark.parametrize("mirror", [False, True])
def test_local_origin_retest_keeps_its_qualification_when_standard_gap_requires_second_fractal(mirror):
    # Reduced from the independent 64-point random check, seed 19270923,
    # case 1780. P5 retests P1=21; its first reversal only overlaps its local
    # shoulder. Subsequent inclusion creates a standard gap [11,15].
    values = strokes([5, 21, 5, 11, 6, 21, 8, 19, 15, 19, 6, 15, 13, 23], mirror)
    calc = XdCalculator()
    assert not any(x.done for x in calc.calculate(values[:10]))
    assert geometry(calc.calculate(values)) == [(0, 5, True), (5, 10, True), (10, 13, False)]
    proof = calc.evidence[0]
    assert proof.rule == "second-feature-fractal"
    assert proof.first_sequence[0].source_indices == (3,)
    assert proof.gap_context.basis == "standard-first-feature"
    assert proof.initial_gap and not proof.gap_context.raw_gap
    assert proof.witness_index == 12
    check_evidence(calc, values)


def test_qqq_included_gap_is_confirmed_only_by_its_second_sequence(qqq):
    parent = next(p for p in qqq.xd_calculator.evidence if p.key == (256, 260, "up"))
    assert parent.initial_gap
    assert parent.gap_context.basis == "standard-first-feature"
    assert not parent.gap_context.raw_gap and parent.gap_context.standard_gap
    assert parent.gap_context.raw_first.source_indices == (261,)
    assert [(e.low, e.high) for e in parent.second_sequence] == [
        (707.98, 709.08), (707.72, 708.44), (707.76, 708.88),
    ]
    assert parent.witness_index == 272
    child = next(p for p in qqq.xd_calculator.evidence if p.parent_key == parent.key)
    assert child.key == (261, 269, "down")
    check_evidence(qqq.xd_calculator, qqq.get_contiguous_bis())


def test_qqq_long_segment_is_replaced_by_causally_connected_segments(qqq):
    actual = [(x.start_line.index, x.end_line.index) for x in qqq.get_xds()
              if 261 <= x.start_line.index <= 363]
    assert actual == [(261, 269), (270, 296), (297, 301), (302, 316),
                      (317, 347), (348, 354), (355, 363)]
    # The parent's post-boundary formation spans through pen 325. It cannot
    # authorize retrospective child boundaries at 319 and 324 inside itself.
    parent=next(p for p in qqq.xd_calculator.evidence if p.key[:2]==(302,316))
    assert max(parent.first_sequence[-1].source_indices)==325
    # This regression fixes the gapped region above. Independently completed
    # reverse proofs elsewhere in the window can add other valid boundaries.
    check_evidence(qqq.xd_calculator, qqq.get_contiguous_bis())
    assert [(x.start_line.index, x.end_line.index, x.done) for x in qqq.get_xds()[-2:]] == [
        (487, 503, False), (504, 508, False),
    ]
