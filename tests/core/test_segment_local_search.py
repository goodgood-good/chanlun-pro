"""Adversarial equal-price containment: accuracy and search growth together."""

from collections import Counter

import pytest

from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_candidates import SearchAuditCalculator
from script.check_segment_model import check_evidence, signature
from tests.core.test_segment_source_rules import geometry, strokes


def local_containment_points(kind, repeats, outcome):
    high, lower = 3 * repeats + 10, 2 * repeats + 5
    if kind == "origin_retest":
        points = [0, high, 1, lower]
        for i in range(repeats):
            points.extend((2 + 2 * i, high, 3 + 2 * i, lower))
        if outcome == "extension":
            points.extend((2 * repeats + 2, high + 1))
        elif outcome == "completed":
            points.append(0)
    else:
        # The local high is below the first one-pen extreme. Only the existing
        # strong first-break rule qualifies it; the reviewed retest rule cannot.
        high, lower = 2 * repeats + 20, 2 * repeats + 15
        points = [0, 3 * repeats + 30, 1, lower, 5, high]
        for i in range(repeats):
            points.extend((-i, lower, 5, high))
        if outcome == "extension":
            points.extend((5, high + 1))
        elif outcome == "completed":
            points.extend((-repeats, lower, -repeats - 1))
    return points


class CountedPrice(int):
    comparisons = 0

    def __lt__(self, other):
        type(self).comparisons += 1
        return int.__lt__(self, other)

    def __gt__(self, other):
        type(self).comparisons += 1
        return int.__gt__(self, other)

    def __le__(self, other):
        type(self).comparisons += 1
        return int.__le__(self, other)

    def __ge__(self, other):
        type(self).comparisons += 1
        return int.__ge__(self, other)


class WithoutSuffixReuse(XdCalculator):
    def _scan_first_feature(self, *args):
        previous = self._feature_scan_cache
        self._feature_scan_cache = None
        try:
            return super()._scan_first_feature(*args)
        finally:
            self._feature_scan_cache = previous


@pytest.mark.parametrize("mirror", [False, True])
def test_cached_equal_prices_keep_the_actual_suppliers_numeric_types(mirror):
    from decimal import Decimal
    from fractions import Fraction

    convert = (int, Decimal, Fraction)
    points = local_containment_points("local_first_break", 9, "completed")
    values = strokes([convert[i % 3](-p if mirror else p) for i, p in enumerate(points)])
    calculator = XdCalculator()
    calculator.calculate(values)
    check_evidence(calculator, values)
    for proof in calculator.evidence:
        for feature in (*proof.first_sequence, *proof.second_sequence):
            assert type(feature.low) is type(values[feature.low_source_index].low)
            assert type(feature.high) is type(values[feature.high_source_index].high)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("kind", ["origin_retest", "local_first_break"])
@pytest.mark.parametrize("number_type", ["int", "decimal", "fraction", "unhashable"])
def test_reused_suffix_preserves_each_boundary_price_supplier(kind, mirror, number_type):
    from decimal import Decimal
    from fractions import Fraction

    class UnhashablePrice(int):
        __hash__ = None

    convert = {"int": int, "decimal": lambda x: Decimal(x) / 8,
               "fraction": lambda x: Fraction(x, 8), "unhashable": UnhashablePrice}[number_type]
    points = local_containment_points(kind, 9, "completed")
    values = strokes([convert(-p if mirror else p) for p in points])
    cached, uncached = XdCalculator(), WithoutSuffixReuse()
    assert geometry(cached.calculate(values)) == geometry(uncached.calculate(values))
    assert signature(cached.xds) == signature(uncached.xds)
    assert cached.evidence == uncached.evidence
    check_evidence(cached, values)
    same_direction = [p for p in cached.evidence if p.direction == values[0].type]
    if kind == "local_first_break":
        assert len(same_direction) > 1
    for proof in same_direction:
        middle = proof.first_sequence[1]
        supplier = middle.low_source_index if mirror else middle.high_source_index
        assert supplier == proof.end_index + 1


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("kind", ["origin_retest", "local_first_break"])
@pytest.mark.parametrize("outcome", ["pending", "extension", "completed"])
def test_local_contained_search_does_not_repeat_the_same_future(kind, outcome, mirror):
    def work(repeats):
        points = local_containment_points(kind, repeats, outcome)
        values = strokes([CountedPrice(-p if mirror else p) for p in points])
        CountedPrice.comparisons = 0
        calculator = XdCalculator()
        lines = calculator.calculate(values)
        count = CountedPrice.comparisons
        if outcome == "completed":
            assert geometry(lines)[0] == (0, 5, True)
            assert geometry(lines)[-1][1:] == (len(values), False)
            # In the strong-break family, confirming the first segment also
            # releases shorter subsequent proofs. They are checked below;
            # a long contained first feature does not imply only two segments.
        else:
            expected_end = len(values) - 2 if kind == "origin_retest" and outcome == "pending" else len(values)
            assert geometry(lines) == [(0, expected_end, False)]
        check_evidence(calculator, values)
        return count

    small, large = work(80), work(160)
    assert large < 2.5 * small, (small, large)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("kind", ["origin_retest", "local_first_break"])
@pytest.mark.parametrize("outcome", ["pending", "extension", "completed"])
def test_local_search_preserves_prefix_proofs_and_checks_skipped_candidates(kind, outcome, mirror):
    values = strokes(local_containment_points(kind, 5, outcome), mirror)
    calculator = SearchAuditCalculator(Counter())
    prior = {}
    for end in range(3, len(values) + 1):
        lines = calculator.calculate(values[:end])
        current = signature(lines)
        assert all(current.get(key) == when for key, when in prior.items())
        assert all(when == values[end - 1].locked_at
                   for key, when in current.items() if key not in prior)
        check_evidence(calculator, values[:end])
        prior = current
    if outcome == "pending":
        assert calculator.counts["local_pending_first_ranges"] > 0
    elif outcome == "extension":
        assert calculator.counts["local_extension_ranges"] > 0


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("pretend", ["pending", "extension"])
def test_local_skip_auditor_rejects_a_hidden_completed_fractal(monkeypatch, mirror, pretend):
    from chanlun.core.segment_evidence import PendingBoundary
    from chanlun.core.xd_calculator import _CandidateExtension

    values = strokes([0, 30, 1, 18, 5, 20, 0, 18, -1, 21], mirror)
    original = XdCalculator._try_end_r34

    def omit_local_fractal(self, *args):
        if args[5] == 5:
            return (PendingBoundary("waiting-first-feature", 4) if pretend == "pending"
                    else _CandidateExtension(8))
        return original(self, *args)

    monkeypatch.setattr(XdCalculator, "_try_end_r34", omit_local_fractal)
    calculator = SearchAuditCalculator(Counter())
    with pytest.raises(AssertionError, match="skipped_local_first_fractal"):
        calculator.calculate(values)
