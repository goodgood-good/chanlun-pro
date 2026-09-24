"""A pending local interpretation cannot hide an already complete standard one.

Derived from the retained SH.600495 prices. L067 49-76 supplies the standard
reference; L065 226-244 and L071 100-124 supply contact and continuation.
This is a deduction, not a labelled example from the author.
"""

import pytest
from chanlun.core.xd_calculator import XdCalculator
from script.probe_same_candidate_standard import SameCandidateStandardProbe
from tests.core.test_segment_source_rules import strokes


POINTS = [474, 486, 481, 486, 476, 483, 479, 484, 471, 486, 480, 486, 478]


@pytest.mark.parametrize("mirror", [False, True])
def test_current_candidate_checks_the_surviving_standard_reference(mirror):
    values = strokes(POINTS, mirror)
    actual = XdCalculator()
    actual.calculate(values)
    probe = SameCandidateStandardProbe()
    probe.calculate(values[:-1])
    assert not probe.evidence
    probe.calculate(values)
    proof = probe.evidence[0]
    assert (proof.start_index, proof.end_index, proof.witness_index) == (0, 8, 11)
    assert proof.reference_mode == "standard-local"
    assert proof.first_pen_continuation.reference.source_indices == (5, 7)
    assert proof.first_break_evidence.effective_first.source_indices == (9,)
    assert probe.xds[0].locked_at == values[11].locked_at
    assert actual.evidence == probe.evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_same_candidate_cannot_cross_an_earlier_origin_return(mirror):
    points = POINTS.copy()
    points[-2] = 487
    calc = SameCandidateStandardProbe()
    calc.calculate(strokes(points, mirror))
    assert not any(p.end_index == 8 for p in calc.evidence)


@pytest.mark.parametrize("mirror", [False, True])
def test_same_candidate_keeps_a_gap_created_by_inclusion(mirror):
    points = POINTS[:11] + [485, 484, 486, 482]
    calc = SameCandidateStandardProbe()
    calc.calculate(strokes(points, mirror))
    assert not any(p.end_index == 8 for p in calc.evidence)


@pytest.mark.parametrize("mirror", [False, True])
def test_pending_context_and_completed_proof_are_stable_over_every_prefix(mirror):
    values = strokes(POINTS + [489, 463, 476, 457, 479], mirror)
    calc = SameCandidateStandardProbe()
    prior = {}
    times = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        now = {p.key: p for p in calc.evidence}
        clocks = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        assert all(now.get(k) == p for k, p in prior.items())
        assert all(clocks.get(k) == t for k, t in times.items())
        assert all(
            t == values[stop - 1].locked_at for k, t in clocks.items() if k not in times
        )
        prior = {k: now[k] for k in clocks}
        times = clocks
