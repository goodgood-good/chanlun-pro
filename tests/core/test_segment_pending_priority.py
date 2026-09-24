"""Pending F2 cannot be replaced by an unrelated local first-fractal shortcut."""

from fractions import Fraction

import pytest

from chanlun.core.segment_evidence import PendingBoundary
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_candidates import local_first_fractal
from script.check_segment_model import check_evidence
from script.check_segment_pending_priority import (
    PENDING_EXAMPLE,
    audit_pending,
    completing_suffix,
    second_sequence_state,
)
from tests.core.test_segment_source_rules import geometry, strokes


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("endpoint,middle_high", [(11, 14), (16, 20)])
def test_later_local_fractal_waits_for_the_original_second_sequence(
    mirror, endpoint, middle_high,
):
    # L067's original first-fractal gap remains (20,21). L078 requires the
    # unresolved predecessor context to be settled; a later local F1 at P11
    # has no independent authority to replace the original P7 boundary.
    points = PENDING_EXAMPLE[:-1] + [endpoint]
    values = strokes(points, mirror)
    local = local_first_fractal(values, 0, 11, 19)
    assert local is not None and not local.has_gap
    # The selected effective-end rule establishes this alternative at 17;
    # it still cannot bypass the already eligible earlier second-case search.
    assert local.witness == 17
    calc = XdCalculator()
    before = calc.calculate(values)
    assert geometry(before) == [(0, 7, False)]
    assert calc.tail_state.reason == "waiting-second-feature"
    assert calc.evidence == ()

    extension = [middle_high, Fraction(endpoint + middle_high, 2), 25]
    complete = strokes(points + extension, mirror)
    after = calc.calculate(complete)
    check_evidence(calc, complete)
    assert geometry(after) == [(0, 7, True), (7, 20, True), (20, 23, False)]
    parent, successor = calc.evidence
    assert parent.witness_index == successor.witness_index == 22
    assert successor.parent_key == parent.key
    assert successor.initial_gap == (middle_high < 17)
    assert successor.second_sequence == ()
    assert after[0].locked_at == after[1].locked_at == complete[22].locked_at
    assert successor.first_sequence[1].source_indices == (20,)
    # B's actual boundary is above/below its earlier INTERNAL extreme, not
    # an equal-price supplier ambiguity. Preserve the full constituent range.
    assert (after[1].high if mirror else after[1].low) == (-10 if mirror else 10)


class IgnorePendingSecond(XdCalculator):
    def _try_end(self, *args):
        result = super()._try_end(*args)
        if isinstance(result, PendingBoundary) and result.reason == "waiting-second-feature":
            return None
        return result


@pytest.mark.parametrize("mirror", [False, True])
def test_bypassing_pending_second_would_commit_a_conflicting_boundary(mirror):
    values = strokes(PENDING_EXAMPLE, mirror)
    bypass = IgnorePendingSecond()
    bypassed = bypass.calculate(values)
    assert (0, 11, True) in geometry(bypassed)
    # Its local geometry is consistent. This explicitly demonstrates why
    # checking each returned proof does not prove global candidate selection.
    check_evidence(bypass, values)
    complete = strokes(PENDING_EXAMPLE + [20, 18, 23], mirror)
    faithful = XdCalculator()
    proper = faithful.calculate(complete)
    check_evidence(faithful, complete)
    assert geometry(proper)[:2] == [(0, 7, True), (7, 20, True)]
    # The completed original L067 premises require P7, not the shortcut's P11.
    assert faithful.evidence[0].first_sequence[1].source_indices == (7,)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("ends_with_original_direction", [False, True])
def test_exact_completion_suffix_preserves_the_existing_pending_boundary(
    mirror, ends_with_original_direction,
):
    original = PENDING_EXAMPLE[:-1] if ends_with_original_direction else PENDING_EXAMPLE
    # Positive values exercise translation; the suffix is exact, not epsilon
    # rounding. Reflection covers the downward theorem.
    points = [200 - p if mirror else 100 + p for p in original]
    direction = "down" if mirror else "up"
    suffix = completing_suffix(points, 7, direction)
    assert len(suffix) == (4 if ends_with_original_direction else 5)
    assert min(points + suffix) > 0
    assert all(p > points[7] if mirror else p < points[7] for p in suffix)
    record = audit_pending(points, count_competitors=True)
    assert record["boundary_point"] == 7
    assert record["added_pens"] == len(suffix)


@pytest.mark.parametrize("mirror", [False, True])
def test_independent_pending_replay_distinguishes_completion_from_invalidation(mirror):
    points = [-p if mirror else p for p in PENDING_EXAMPLE]
    direction = "down" if mirror else "up"
    assert second_sequence_state(points, 7, direction)[0] == "pending"
    suffix = completing_suffix(points, 7, direction)
    assert second_sequence_state(points + suffix, 7, direction)[0] == "confirmed"
    assert second_sequence_state(points + [-32 if mirror else 32], 7, direction)[0] == "invalidated"


def test_completion_suffix_keeps_small_positive_prices_above_zero():
    points = [Fraction(p + 1, 1000) for p in PENDING_EXAMPLE]
    suffix = completing_suffix(points, 7, "up")
    assert min(suffix) > 0
    assert max(suffix) < points[7]
    record = audit_pending(points)
    assert record["boundary_point"] == 7
