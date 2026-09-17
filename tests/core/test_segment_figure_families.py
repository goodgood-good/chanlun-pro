import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.segment_figure_families import (
    lesson79_family, lesson79_lower_continuation_family, lesson81_family,
)
from tests.core.test_segment_source_rules import geometry, strokes


@pytest.mark.parametrize("kind", ["upper", "lower", "equal9"])
@pytest.mark.parametrize("touching_left", [False, True])
@pytest.mark.parametrize("mirror", [False, True])
def test_lesson79_constrained_order_family(kind, touching_left, mirror):
    expected = (
        [(0, 3, True), (3, 8, True), (8, 11, False)]
        if kind == "upper"
        else [(0, 3, True), (3, 10, False)]
    )
    checked = 0
    for points in lesson79_family(kind, touching_left):
        assert (
            geometry(XdCalculator().calculate(strokes(points, mirror))) == expected
        ), points
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("kind", ["lower", "equal9"])
@pytest.mark.parametrize("touching_left", [False, True])
@pytest.mark.parametrize("mirror", [False, True])
def test_lesson79_pending_lower_figure_after_a_complete_reverse_triple(
    kind, touching_left, mirror,
):
    checked = 0
    for points in lesson79_lower_continuation_family(kind, touching_left):
        values = strokes(points, mirror)
        calculator = XdCalculator()
        before = calculator.calculate(values[:-2])
        assert geometry(before) == [(0, 3, True), (3, 10, False)], points
        after = calculator.calculate(values)
        assert geometry(after) == [(0, 3, True), (3, 10, True), (10, 13, False)], points
        assert after[1].locked_at == values[-1].locked_at, points
        assert after[1].construction_evidence.pivot_stem.source_indices == (7, 9), points
        check_evidence(calculator, values)
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("kind", ["lower", "equal9"])
@pytest.mark.parametrize("touching_left", [False, True])
@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("raised_low", [False, True])
def test_lesson79_equal_price_containment_before_the_first_valid_break(
    kind, touching_left, mirror, raised_low,
):
    from fractions import Fraction

    checked = 0
    for original in lesson79_lower_continuation_family(kind, touching_left):
        low = Fraction(original[9] + original[10], 2) if raised_low else original[9]
        # Add one nested same-direction pen with the same endpoint price,
        # before the strong reverse triple. Price provenance remains at 9;
        # the actual middle feature starts at 12 and determines the boundary.
        points = original[:11] + [low, original[10]] + original[11:]
        values = strokes(points, mirror)
        calc = XdCalculator()
        before = calc.calculate(values[:-1])
        assert len(calc.evidence) == 1 and not before[1].done, points
        after = calc.calculate(values)
        assert geometry(after) == [(0, 3, True), (3, 12, True), (12, 15, False)], points
        proof = calc.evidence[1]
        assert proof.pivot_stem.source_indices == (7, 9, 11), points
        supplier = proof.pivot_stem.low_source_index if mirror else proof.pivot_stem.high_source_index
        assert supplier == 9 and proof.end_index == 11, points
        assert proof.first_sequence[1].source_indices == (12,), points
        assert after[1].locked_at == values[-1].locked_at, points
        check_evidence(calc, values)
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(
    "kind,second_gap",
    [
        ("lower7", "overlap"),
        ("lower7", "touch"),
        ("lower7", "gap"),
        ("equal7", "overlap"),
        ("higher7", "overlap"),
    ],
)
def test_lesson81_order_family_and_second_fractal_own_gap(kind, second_gap, mirror):
    expected = (
        [(0, 3, True), (3, 6, True), (6, 9, False)]
        if kind == "lower7"
        else [(0, 9, False)]
    )
    checked = 0
    for points in lesson81_family(kind, second_gap):
        values = strokes(points, mirror)
        calculator = XdCalculator()
        assert not any(line.done for line in calculator.calculate(values[:-1])), points
        lines = calculator.calculate(values)
        assert geometry(lines) == expected, points
        if kind == "lower7":
            parent, successor = calculator.evidence
            assert successor.parent_key == parent.key, points
            assert successor.initial_gap == (second_gap == "gap"), points
            assert lines[0].locked_at == lines[1].locked_at == values[-1].locked_at
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("mirror", [False, True])
def test_lesson79_shaped_first_break_keeps_its_raw_no_gap_classification(mirror):
    # Synthetic extension of review chart 01, not an author's extra figure:
    # [8,9] overlaps raw [1,11], despite the later standard [1,5] gap.
    points = [10, 8, 9, 1, 11, 4, 5, 3, 7, 2, 6, 0]
    assert geometry(XdCalculator().calculate(strokes(points, mirror))) == [
        (0, 3, True), (3, 8, True), (8, 11, False)
    ]


@pytest.mark.parametrize("mirror", [False, True])
def test_second_sequence_cannot_borrow_elements_after_its_candidate_was_invalidated(
    mirror,
):
    # L069 pp1873 and L076 pp1955: an ABC pullback followed by a new old
    # extreme, before three standard second-sequence elements, fails. Later
    # pens can form a different boundary, but cannot resurrect this P3 turn.
    points = [0, 10, 6, 18, 13, 16, 11, 20, 12, 19, 9]
    calc = XdCalculator()
    assert geometry(calc.calculate(strokes(points, mirror))) == [
        (0, 7, True),
        (7, 10, False),
    ]
    assert all(proof.end_index != 2 for proof in calc.evidence)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("candidate_high", [19, 20, 21])
def test_local_first_pen_break_still_requires_a_strict_feature_extremum(
    mirror, candidate_high
):
    # L067's fractal plus L071/L079's protected first-pen break. Protecting
    # containment does not turn a lower/equal middle high into a top (or the
    # mirrored higher/equal middle low into a bottom). These are deductions,
    # not prices read from an original market chart.
    points = [0, 30, 4, 20, 15, candidate_high, 5, 16, 2]
    expected = [(0, 5, True), (5, 8, False)] if candidate_high > 20 else [(0, 7, False)]
    assert geometry(XdCalculator().calculate(strokes(points, mirror))) == expected


@pytest.mark.parametrize("mirror", [False, True])
def test_pending_second_sequence_cannot_be_bypassed_by_an_unrelated_local_fractal(
    mirror,
):
    # L078 pp1973-1974: the provisional reverse segment has not yet confirmed
    # its predecessor's break. A local proof in a different hypothetical
    # context cannot be used to sidestep that outstanding second sequence.
    points = [0, 10, 6, 18, 13, 14, 11, 17, 4, 14, 13, 17, 9, 13, 9]
    calc = XdCalculator()
    assert geometry(calc.calculate(strokes(points, mirror))) == [(0, 3, False)]
    assert calc.tail_state.reason == "waiting-second-feature"
