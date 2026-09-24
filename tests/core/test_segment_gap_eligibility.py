"""A future first fractal cannot retrospectively establish F2 priority.

L06764-118 defines both cases on an existing first feature fractal. The
chronological selection of already complete earlier evidence is a causal
implementation deduction; the still-existing F2 priority is not removed.
"""

import pytest
from collections import Counter
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from script.check_segment_candidates import SearchAuditCalculator
from tests.core.test_segment_source_rules import strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_later_first_fractal_cannot_revoke_a_completed_literal_boundary(mirror):
    # Seed20260926. Candidate B10 gains its gapped FIRST fractal only at B16.
    # Before then, B12's contained-stem first break has already completed at
    # B14. Once B16 arrives it cannot rewrite that previous confirmation.
    points = [
        0,
        18,
        -15,
        -13,
        -34,
        -16,
        -39,
        -10,
        -41,
        -27,
        -46,
        -44,
        -46,
        -19,
        -46,
        -13,
        -45,
        -43,
    ]
    values = strokes(points, mirror)
    calc = XdCalculator()
    previous = {}
    proofs = {}
    for end in range(3, len(values) + 1):
        calc.calculate(values[:end])
        check_evidence(calc, values[:end])
        now = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        current = {p.key: p for p in calc.evidence if p.key in now}
        assert all(now.get(k) == t for k, t in previous.items())
        assert all(current.get(k) == p for k, p in proofs.items())
        assert all(
            t == values[end - 1].locked_at for k, t in now.items() if k not in previous
        )
        previous, proofs = now, current
    assert [(p.start_index, p.end_index, p.witness_index) for p in calc.evidence] == [
        (1, 11, 14)
    ]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "invalidated"])
def test_future_f2_completion_or_invalidation_keeps_an_earlier_first_case(
    mirror, outcome
):
    points = [
        0,
        18,
        -15,
        -13,
        -34,
        -16,
        -39,
        -10,
        -41,
        -27,
        -46,
        -44,
        -46,
        -19,
        -46,
        -13,
        -45,
        -43 if outcome == "completed" else -12,
        -47,
    ]
    values = strokes(points, mirror)
    counts = Counter()
    calc = SearchAuditCalculator(counts)
    previous = {}
    for end in range(3, len(values) + 1):
        calc.calculate(values[:end])
        check_evidence(calc, values[:end])
        now = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        assert all(now.get(k) == t for k, t in previous.items())
        previous = now
    assert calc.evidence[0].key[:2] == (1, 11) and calc.evidence[0].witness_index == 14
    if outcome == "invalidated":
        assert counts["pre_fractal_completions_preserved"] > 0


@pytest.mark.parametrize("mirror", [False, True])
def test_existing_first_fractal_keeps_f2_priority_over_a_simultaneous_alternative(
    mirror,
):
    points = [
        0,
        18,
        -15,
        -13,
        -34,
        -16,
        -39,
        -10,
        -41,
        -27,
        -46,
        -44,
        -46,
        -19,
        -46,
        -20,
        -45,
        -13,
    ]
    values = strokes(points, mirror)
    calc = XdCalculator()
    calc.calculate(values)
    assert not calc.evidence
    assert calc.tail_state.reason == "waiting-second-feature"
