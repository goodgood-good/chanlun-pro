"""Causal confirmation when the second fractal precedes the first one.

The synthetic equal-high family keeps the currently declared earliest-price-
source convention. It does not adjudicate the unresolved equal-price boundary
rule. The current rule and its source premises are in docs/segment_rules.md.
"""

from datetime import timedelta
from fractions import Fraction

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from tests.core.test_segment_source_rules import geometry, strokes


def delayed_first_fractal_points(pairs):
    # At P9, upward features [14,20], [12,16], [15,18] already form
    # a strict bottom. The downward middle is still only [15,20]:
    # P5 returned exactly to P3, so [12,20] was contained as well.
    points = [Fraction(p) for p in (0, 10, 6, 20, 14, 20, 12, 16, 15, 18)]
    for j in range(pairs):
        epsilon = Fraction(1, 2 ** (j + 1))
        points.extend((16 - epsilon, 17 + epsilon))
    # Each extra downward pen stays inside the first middle. Only this
    # last pen supplies an independent right element below that middle.
    points.append(Fraction(13))
    return points


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("pairs", [0, 1, 32])
def test_earlier_second_fractal_does_not_backdate_parent_or_successor(pairs, mirror):
    values = strokes(delayed_first_fractal_points(pairs), mirror)
    calculator = XdCalculator()
    for count in range(3, len(values)):
        preview = calculator.calculate(values[:count])
        assert not any(line.done for line in preview)
    lines = calculator.calculate(values)
    assert geometry(lines)[:2] == [(0, 3, True), (3, 6, True)]
    parent, successor = calculator.evidence[:2]
    assert parent.second_sequence[-1].source_indices == (8,)
    assert parent.first_sequence[-1].source_indices == (len(values) - 1,)
    assert parent.witness_index == successor.witness_index == len(values) - 1
    assert successor.parent_key == parent.key
    assert successor.first_sequence == parent.second_sequence
    assert [line.locked_at for line in lines[:2]] == [values[-1].locked_at] * 2
    check_evidence(calculator, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("missing", [8, 10, 11])
@pytest.mark.parametrize("pending_kind", ["unlocked", "selection"])
def test_both_fractals_and_intervening_evidence_must_be_closed(
    mirror, missing, pending_kind
):
    values = strokes(delayed_first_fractal_points(1), mirror)
    calculator = XdCalculator()
    complete = calculator.calculate(values)
    original_geometry = geometry(complete)
    original_evidence = calculator.evidence
    closed_time = values[missing].locked_at
    if pending_kind == "unlocked":
        values[missing].locked_at = None
    else:
        values[missing].selection_pending = True
    preview = calculator.calculate(values)
    assert not any(line.done or line.locked_at is not None for line in preview)
    assert calculator.evidence == original_evidence
    values[missing].locked_at = closed_time
    values[missing].selection_pending = False
    repaired = calculator.calculate(values)
    assert geometry(repaired) == original_geometry
    assert [line.locked_at for line in repaired[:2]] == [values[-1].locked_at] * 2


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("late", [8, 10])
def test_confirmation_uses_evidence_availability_not_fractal_event_order(mirror, late):
    values = strokes(delayed_first_fractal_points(1), mirror)
    calculator = XdCalculator()
    calculator.calculate(values)
    original_evidence = calculator.evidence
    available_at = values[-1].locked_at + timedelta(days=1)
    values[late].locked_at = available_at
    lines = calculator.calculate(values)
    assert all(line.locked_at == available_at for line in lines if line.done)
    assert calculator.evidence == original_evidence
