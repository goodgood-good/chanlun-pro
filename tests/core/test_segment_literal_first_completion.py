"""L071 76-106: raw first-break completion must survive equal-origin inclusion.

The original's sufficient three-pen route does not require the retracing pen
to stop one tick short of its origin. Exact equality is not a strict return
beyond the origin. These are deductions from the locally read text, not
author-answered figures. Negative/mirror/prefix coverage protects the boundary.
"""

import pytest
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes
from tests.core.test_segment_reverse_proof import QQQ


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("return_price", [13, 14])
def test_first_break_third_pen_extends_the_end_with_equal_or_lower_origin(
    mirror, return_price
):
    values = strokes([0, 10, 6, 14, 4, return_price, 3], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert [(p.start_index, p.end_index, p.witness_index) for p in calc.evidence] == [
        (0, 2, 5)
    ]
    assert calc.xds[0].locked_at == values[5].locked_at
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_qqq_first_break_completion_does_not_depend_on_one_tick(mirror):
    values = strokes(QQQ[:9] + [702700, 714310, 701000], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    parent = next(p for p in calc.evidence if p.start_index == 3)
    assert parent.end_index == 7 and parent.witness_index == 10
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_strict_origin_return_still_invalidates_the_first_break(mirror):
    values = strokes([0, 10, 6, 14, 4, 15, 3], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert not any(p.key[:2] == (0, 2) for p in calc.evidence)
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_delayed_ordinary_first_fractal_cannot_replace_an_earlier_literal_proof(mirror):
    # Seed20260926: an ordinary candidate only gains its separate right at B59.
    # B54 already overlaps its qualified left [-58,-43], and B56 ends above
    # B54's -54 end after an equal-origin retrace. L065/L071 therefore finishes
    # B47-B53 at B56. The formerly expected B47-B55 at B58 was also too late.
    # This is a source-derived example, not an author-labelled market figure.
    points = [
        0,
        -7,
        0,
        -28,
        -6,
        -37,
        -17,
        -18,
        6,
        -10,
        5,
        -27,
        -6,
        -44,
        -7,
        -45,
        -35,
        -38,
        -20,
        -50,
        -23,
        -32,
        -17,
        -29,
        1,
        -27,
        8,
        -30,
        -13,
        -51,
        -33,
        -67,
        -60,
        -74,
        -36,
        -59,
        -36,
        -69,
        -52,
        -86,
        -76,
        -81,
        -69,
        -79,
        -42,
        -64,
        -26,
        -56,
        -40,
        -58,
        -19,
        -49,
        -43,
        -64,
        -54,
        -64,
        -36,
        -64,
        -27,
        -57,
        -31,
    ]
    values = strokes(points, mirror)
    calc = XdCalculator()
    previous = {}
    proofs = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        check_evidence(calc, values[:stop])
        now = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        current = {p.key: p for p in calc.evidence if p.key in now}
        assert all(now.get(k) == t for k, t in previous.items())
        assert all(current.get(k) == p for k, p in proofs.items())
        assert all(
            t == values[stop - 1].locked_at for k, t in now.items() if k not in previous
        )
        previous, proofs = now, current
    proof = next(p for p in calc.evidence if p.start_index == 46)
    assert proof.end_index == 52 and proof.witness_index == 55
    assert proof.first_pen_continuation.reference_context == "record"
    receipt = proof.first_break_evidence
    assert receipt.first_pen_index == 53 and receipt.extension_index == 55
