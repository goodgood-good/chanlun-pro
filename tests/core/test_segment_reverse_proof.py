"""L078 58-67: an independently completed reverse segment transmits its break.

The prices below are physical QQQ.US 1m pens, 2026-08-21..25 US/Eastern,
scaled by 1000. They are a market regression, not an author-answered figure.
The connected child proof must survive in the emitted segment chain.
"""

import random

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes
from tests.core.segment_test_routes import WithoutEffectiveOnlyCompletion


QQQ = [
    713775,
    712790,
    713310,
    712430,
    712870,
    712670,
    713890,
    713090,
    714310,
    702700,
    704815,
    702830,
    706545,
    704915,
    708844,
    707560,
    709336,
    707640,
    708980,
    706740,
    709530,
    707330,
    708050,
    706230,
    713130,
    711340,
    714040,
]


class ReverseProofOnly(WithoutEffectiveOnlyCompletion):
    """Exercise this sufficient route separately from the earlier L078 return.

    Production now has the still earlier normalized first-break route, covered
    by test_segment_normalized_first_completion.py. This isolates the reverse certificate's
    inheritance and time contract without claiming it is the sole completion.
    """

    def _try_completed_return_segment(
        self,
        values,
        start,
        direction,
        first,
        result,
        predecessor,
        *,
        latest_witness=None,
    ):
        return result


@pytest.mark.parametrize("mirror", [False, True])
def test_completed_reverse_proof_confirms_preceding_boundary(mirror):
    values = strokes(QQQ, mirror)
    independent = XdCalculator()
    independent._build_segments(values, 8)
    child = independent.evidence[0]
    assert (child.start_index, child.end_index, child.witness_index) == (8, 22, 25)
    check_evidence(independent, values)

    calc = ReverseProofOnly()
    calc.calculate(values)
    assert [(p.start_index, p.end_index) for p in calc.evidence] == [
        (0, 2),
        (3, 7),
        (8, 22),
    ]
    parent = calc.evidence[1]
    assert parent.witness_index == child.witness_index
    assert parent.reverse_segment.successor == calc.evidence[2] == child
    assert calc.xds[1].locked_at == calc.xds[2].locked_at == values[25].locked_at
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_reverse_proof_never_backdates_or_replaces_its_supporting_child(mirror):
    # New movement through the old first reversal's origin cannot undo an
    # already completed proof. Before that completion, it remains pending.
    values = strokes(QQQ + [710000, 716000, 713000, 718000], mirror)
    calc = ReverseProofOnly()
    previous = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        current = {
            (s.start_line.index, s.end_line.index): s.locked_at
            for s in calc.xds
            if s.done
        }
        assert all(current.get(k) == t for k, t in previous.items())
        assert all(
            t == values[stop - 1].locked_at
            for k, t in current.items()
            if k not in previous
        )
        if stop < 26:
            assert (3, 7) not in current
        else:
            assert current[(3, 7)] == current[(8, 22)] == values[25].locked_at
        check_evidence(calc, values[:stop])
        previous = current


@pytest.mark.parametrize("mirror", [False, True])
def test_reverse_proof_waits_for_every_original_pen_to_close(mirror):
    values = strokes(QQQ, mirror)
    values[25].locked_at = None
    calc = ReverseProofOnly()
    calc.calculate(values)
    assert not any(s.done and s.start_line.index in (3, 8) for s in calc.xds)
    values[25].locked_at = values[24].locked_at
    calc.calculate(values)
    assert {(s.start_line.index, s.end_line.index) for s in calc.xds if s.done} >= {
        (3, 7),
        (8, 22),
    }


def test_reverse_proof_checker_rejects_an_unrelated_or_omitted_child():
    values = strokes(QQQ)
    calc = ReverseProofOnly()
    calc.calculate(values)
    parent = calc.evidence[1]
    calc.evidence = (calc.evidence[0], parent)
    with pytest.raises(AssertionError, match="reverse_proof_successor"):
        check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_return_prevents_an_unfinished_reverse_proof(mirror):
    values = strokes(QQQ[:-1] + [715000], mirror)
    calc = ReverseProofOnly()
    calc.calculate(values)
    assert not any(p.key[:2] == (3, 7) for p in calc.evidence)
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_origin_does_not_cancel_a_completed_reverse_proof(mirror):
    values = strokes(QQQ[:-1] + [714310], mirror)
    calc = ReverseProofOnly()
    calc.calculate(values)
    parent = next(p for p in calc.evidence if p.key[:2] == (3, 7))
    assert parent.witness_index == 25 and parent.reverse_segment is not None
    check_evidence(calc, values)


def test_feature_progress_filter_matches_unfiltered_independent_probes(monkeypatch):
    import chanlun.core.xd_calculator as module

    rng = random.Random(20260821)
    cases = []
    for _ in range(600):
        points = [0]
        sign = rng.choice((-1, 1))
        for i in range(63):
            points.append(points[-1] + sign * (-1 if i % 2 else 1) * rng.randint(1, 20))
        values = strokes(points)
        calc = XdCalculator()
        calc.calculate(values)
        cases.append((values, calc.evidence, calc.tail_state))
    # Every candidate passes this negative filter. The expensive fixed-origin
    # probe must still establish an identical proof, including source indices.
    monkeypatch.setattr(
        module, "_next_feature_progress", lambda values: list(range(len(values)))
    )
    monkeypatch.setattr(
        module, "_next_feature_turn", lambda values: list(range(len(values)))
    )
    for values, expected, tail in cases:
        calc = XdCalculator()
        calc.calculate(values)
        assert calc.evidence == expected and calc.tail_state == tail
        check_evidence(calc, values)
