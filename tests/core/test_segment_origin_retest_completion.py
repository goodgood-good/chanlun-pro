"""Complete an existing origin-retest qualification without losing equality.

L065 226-244 and L071 100-124 provide the contact/continuation premises. The
initial-extreme qualification is an existing project interpretation, not an
additional author quotation. L079 local-reference negative controls remain.
"""

import pytest
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import strokes


STEM = [0, 100, 60, 180, 130, 160, 110, 140, 110, 130, 120, 140]
LOWER = STEM.copy()
LOWER[7] = 145
CASES = [
    ("equal_return", STEM + [125, 140, 100], 13),
    ("touch_reference_edge", STEM + [130, 140, 129], 13),
    ("included_effective_end", STEM + [125, 135, 130, 140, 128], 15),
    ("strict_origin_return", STEM + [125, 141, 100], None),
    ("normalized_gap", STEM + [125, 138, 132, 140, 131], None),
    ("below_initial_extreme", LOWER + [125, 140, 100], None),
]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("name,points,witness", CASES)
def test_qualified_origin_retest_has_the_same_completion_contract(
    name, points, witness, mirror
):
    values = strokes(points, mirror)
    calc = XdCalculator()
    prior = {}
    clocks = {}
    for stop in range(3, len(values) + 1):
        calc.calculate(values[:stop])
        check_evidence(calc, values[:stop])
        proofs = {p.key: p for p in calc.evidence}
        times = {s.construction_evidence.key: s.locked_at for s in calc.xds if s.done}
        assert all(proofs.get(k) == p for k, p in prior.items())
        assert all(times.get(k) == t for k, t in clocks.items())
        assert all(
            t == values[stop - 1].locked_at for k, t in times.items() if k not in clocks
        )
        prior = {k: proofs[k] for k in times}
        clocks = times
    proof = next(
        (p for p in calc.evidence if p.start_index == 6 and p.end_index == 10), None
    )
    if witness is None:
        assert proof is None
    else:
        assert proof is not None and proof.witness_index == witness
        assert proof.first_pen_continuation.reference_context == "origin-retest"
        assert proof.first_break_evidence.extension_index == witness
        assert calc.xds[2].locked_at == values[witness].locked_at


@pytest.mark.parametrize("mirror", [False, True])
def test_standard_boundary_keeps_priority_over_later_origin_at_same_witness(mirror):
    # SH.601326's local price ordering: the B8 standard candidate and a later
    # contained origin retest both finish at B14. Preserve the earlier boundary
    # and its standard reference instead of selecting the later same-price one.
    values = strokes(
        [
            330,
            342,
            336,
            340,
            334,
            341,
            338,
            342,
            338,
            342,
            338,
            341,
            339,
            342,
            336,
            343,
        ],
        mirror,
    )
    calc = XdCalculator()
    calc.calculate(values[:14])
    check_evidence(calc, values[:14])
    proof = calc.evidence[0]
    assert (proof.start_index, proof.end_index, proof.witness_index) == (0, 6, 13)
    assert proof.reference_mode == "standard-local"
    assert proof.first_pen_continuation.reference_context == "record"
    calc.calculate(values)
    check_evidence(calc, values)
    assert calc.evidence[0] == proof
