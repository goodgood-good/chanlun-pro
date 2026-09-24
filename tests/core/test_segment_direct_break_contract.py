"""User-approved normalized first-break continuation, 2026-09-21.

These are synthetic continuations of the established-predecessor fixture,
not claimed to be the author's answered diagrams. Both price directions,
pending/return/extension outcomes, and every prefix share one contract.
"""

from dataclasses import replace
import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes


BASE = [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 25, 8, 24, 20, 22, 19]


@pytest.mark.parametrize("mirror", [False, True])
def test_nonordinary_break_uses_the_effective_first_end(mirror):
    values = strokes(BASE + [23, 7], mirror)
    calc = XdCalculator()
    for stop in (13, 14):
        calc.calculate(values[:stop])
        assert not any(p.key[:2] == (3, 9) for p in calc.evidence)
    calc.calculate(values)
    proof = next(p for p in calc.evidence if p.key[:2] == (3, 9))
    assert proof.first_break_witness_index == 14
    assert proof.witness_index == 14
    receipt = proof.first_break_evidence
    assert receipt.effective_first.source_indices == (10, 12)
    assert (receipt.effective_first.low, receipt.effective_first.high) == (
        (-25, -20) if mirror else (20, 25)
    )
    assert (
        next(s for s in calc.xds if s.start_line.index == 3).locked_at
        == values[14].locked_at
    )
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_later_origin_return_does_not_cancel_effective_completion(mirror):
    values = strokes(BASE + [26, 7, 20, 5], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert any(p.key[:2] == (3, 9) and p.witness_index == 14 for p in calc.evidence)
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_nonordinary_break_evidence_cannot_claim_an_unobserved_earlier_escape(mirror):
    values = strokes(BASE + [23, 7], mirror)
    calc = XdCalculator()
    calc.calculate(values)
    proof = next(p for p in calc.evidence if p.key[:2] == (3, 9))
    calc.evidence = tuple(
        replace(p, first_break_witness_index=12) if p is proof else p
        for p in calc.evidence
    )
    with pytest.raises(AssertionError, match="first_break"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("tail", [[], [23, 7], [26, 7, 20, 5]])
def test_direct_break_contract_preserves_every_confirmed_prefix(tail, mirror):
    values = strokes(BASE + tail, mirror)
    calc = XdCalculator()
    prior = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        check_evidence(calc, values[:stop])
        now = {
            (s.start_line.index, s.end_line.index, s.type): s.locked_at
            for s in calc.xds
            if s.done
        }
        assert all(now.get(k) == v for k, v in prior.items())
        assert all(
            v == values[stop - 1].locked_at for k, v in now.items() if k not in prior
        )
        prior = now


@pytest.mark.parametrize("mirror", [False, True])
def test_earlier_effective_break_precedes_a_later_standard_boundary(mirror):
    # The old raw-end interpretation waited until a later P20 turn. With the
    # effective boundary the first reverse pen at P12 (-6->5) normalizes its
    # end to4, then +5 completes it at pen16, before that later alternative.
    points = [
        0,
        12,
        1,
        3,
        -8,
        4,
        2,
        4,
        2,
        5,
        -4,
        0,
        -6,
        5,
        -5,
        4,
        1,
        5,
        -4,
        -3,
        -6,
        -1,
        -4,
        1,
        -8,
    ]
    values = strokes(points, mirror)
    calc = XdCalculator()
    # This completion is now independent of an established predecessor and
    # takes precedence over the later reverse certificate in connected input.
    calc._build_segments(values, 9)
    proof = next(p for p in calc.evidence if p.start_index == 9)
    assert proof.end_index == 11 and proof.witness_index == 16
    check_evidence(calc, values)
    connected = XdCalculator()
    connected.calculate(values)
    parent = next(p for p in connected.evidence if p.start_index == 9)
    assert parent.end_index == 11 and parent.witness_index == 16
    assert parent.first_break_evidence.extension_index == 16
    check_evidence(connected, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("later_old_completion", [False, True])
def test_effective_break_precedes_the_later_contained_stem(
    mirror, later_old_completion
):
    points = [
        0,
        11,
        10,
        18,
        6,
        15,
        10,
        18,
        10,
        17,
        9,
        12,
        7,
        9,
        7,
        15,
        10,
        16,
        6,
        12,
        11,
        15,
        13,
        22,
    ]
    if later_old_completion:
        points[22] = 5
    values = strokes(points, mirror)
    calc = XdCalculator()
    calc.calculate(values)
    proof = calc.evidence[0]
    assert proof.start_index == 0 and proof.end_index == 2 and proof.witness_index == 9
    assert proof.first_break_evidence.effective_first.source_indices == (3, 5, 7)
    assert calc.xds[0].locked_at == values[9].locked_at
    check_evidence(calc, values)
