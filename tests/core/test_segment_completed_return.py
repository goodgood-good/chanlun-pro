"""L078 160-211: a completed return that does not cross the breaking pen origin.

Read against D:/缠论/.../L078_继续说线段的划分(2007-09-06222831).md,
and the original DOCX word/document.xml body paragraphs39863-39877 (zero based).
QQQ is a market deduction, not an author-answered diagram. This completion
route must compete with, rather than wait for, the independently ended reverse
segment. Its internal return is not relabelled as the next emitted segment.
"""

import pytest
from dataclasses import replace

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_reverse_proof import QQQ
from tests.core.test_segment_source_rules import strokes
from tests.core.segment_test_routes import WithoutEffectiveOnlyCompletion


class ReturnProofOnly(WithoutEffectiveOnlyCompletion):
    """Exercise the independent return certificate when the earlier route is suppressed."""


@pytest.mark.parametrize("mirror", [False, True])
def test_completed_return_confirms_before_reverse_segment_itself_ends(mirror):
    values = strokes(QQQ, mirror)
    standalone = XdCalculator()
    standalone._build_segments(values[:19], 9)
    check_evidence(standalone, values[:19])
    support = standalone.evidence[0]
    assert support.key[:2] == (9, 15) and support.witness_index == 18
    calc = ReturnProofOnly()
    calc.calculate(values[:19])
    assert [p.key[:2] for p in calc.evidence] == [(0, 2), (3, 7), (8, 10), (11, 15)]
    parent = calc.evidence[1]
    assert parent.witness_index == 18
    assert parent.return_segment.return_proofs[0] == support
    assert all(s.locked_at == values[18].locked_at for s in calc.xds[1:4])
    check_evidence(calc, values[:19])


@pytest.mark.parametrize("mirror", [False, True])
def test_return_confirmation_preserves_proof_and_time_on_every_prefix(mirror):
    values = strokes(QQQ + [710000, 716000, 713000, 718000], mirror)
    calc = ReturnProofOnly()
    prior = {}
    proofs = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        check_evidence(calc, values[:stop])
        now = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        current = {p.key: p for p in calc.evidence if p.key in now}
        assert all(now.get(k) == t for k, t in prior.items())
        assert all(current.get(k) == p for k, p in proofs.items())
        assert all(
            t == values[stop - 1].locked_at for k, t in now.items() if k not in prior
        )
        if stop >= 19:
            assert any(
                p.key[:2] == (3, 7) and p.witness_index == 18 for p in calc.evidence
            )
        prior, proofs = now, current


@pytest.mark.parametrize("mirror", [False, True])
def test_return_does_not_confirm_when_it_crossed_the_break_origin(mirror):
    points = list(QQQ)
    points[10] = 715000
    values = strokes(points, mirror)
    calc = ReturnProofOnly()
    calc.calculate(values)
    assert not any(p.key[:2] == (3, 7) for p in calc.evidence)
    check_evidence(calc, values)


def test_return_checker_rejects_parent_dependent_support():
    values = strokes(QQQ[:20])
    calc = ReturnProofOnly()
    calc.calculate(values)
    parent = calc.evidence[1]
    cert = parent.return_segment
    circular = replace(cert.return_proofs[0], predecessor_key=parent.key)
    altered = replace(parent, return_segment=replace(cert, return_proofs=(circular,)))
    calc.evidence = (calc.evidence[0], altered, *calc.evidence[2:])
    with pytest.raises(AssertionError, match="return_proof_circular"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("crosses", [False, True])
def test_return_origin_equality_is_not_a_strict_crossing(mirror, crosses):
    points = [
        30000,
        20000,
        29000,
        12000,
        32000,
        14000,
        26000,
        18000,
        30000,
        21000,
        25000,
        8000,
        24000,
        20000,
        25001 if crosses else 25000,
        21000,
        24000,
        19000,
    ]
    values = strokes(points, mirror)
    calc = ReturnProofOnly()
    calc.calculate(values)
    found = [p for p in calc.evidence if p.key[:2] == (3, 9)]
    assert bool(found) is not crosses
    if found:
        assert found[0].return_segment is not None and found[0].witness_index == 16
    check_evidence(calc, values)


def test_return_cannot_supply_its_own_missing_predecessor():
    values = strokes(QQQ)
    calc = ReturnProofOnly()
    calc._build_segments(values, 3)
    assert not any(p.key[:2] == (3, 7) for p in calc.evidence)
    check_evidence(calc, values)


def test_unclosed_internal_pen_prevents_return_confirmation():
    values = strokes(QQQ[:20])
    values[13].locked_at = None
    calc = ReturnProofOnly()
    calc.calculate(values)
    assert not any(s.done and s.start_line.index == 3 for s in calc.xds)
