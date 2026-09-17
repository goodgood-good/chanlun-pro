"""Original-figure regressions, assuming the supplied alternating pens are legal.

Prices are synthetic order-preserving realizations, not market data. Source
locations and the distinction between author rules and deductions are recorded
in docs/segment_construction_audit.md. Market price gaps are outside this audit;
feature-interval gaps are essential to L067 and remain covered.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.core.xd_calculator import XdCalculator, _has_inclusion, _process_inclusion


BASE = datetime(2026, 9, 16, tzinfo=timezone.utc)


def strokes(points, mirror=False):
    if mirror:
        points = [-p for p in points]
    return [
        SimpleNamespace(
            index=i,
            component_index=0,
            start=SimpleNamespace(val=a),
            end=SimpleNamespace(val=b),
            type="up" if b > a else "down",
            high=max(a, b),
            low=min(a, b),
            locked_at=BASE + timedelta(minutes=i),
            selection_pending=False,
        )
        for i, (a, b) in enumerate(zip(points, points[1:]))
    ]


def geometry(lines):
    return [(x.start_line.index, x.end_line.index + 1, x.done) for x in lines]


CASES = {
    # Review chart 01 selected raw-first classification. This is a deduction,
    # not an author's separately answered figure.
    "67_gap_after_inclusion": ([-4, 8, 0, 12, 7, 11, 9, 10, 8.5], [(0, 3, True), (3, 8, False)]),
    "67_first": ([0, 10, 6, 14, 8, 12, 5], [(0, 3, True), (3, 6, False)]),
    "71_strong": ([0, 10, 6, 14, 4, 12, 2], [(0, 3, True), (3, 6, False)]),
    "67_second_no_fill": (
        [0, 10, 6, 18, 13, 16, 11, 14, 12, 17],
        [(0, 3, True), (3, 6, True), (6, 9, False)],
    ),
    "70_fill_still_pending": ([0, 10, 6, 18, 13, 16, 9], [(0, 3, False)]),
    "81_equal": ([0, 10, 6, 18, 13, 16, 11, 16, 12, 20], [(0, 9, False)]),
    "81_higher7": ([0, 10, 6, 18, 13, 16, 11, 17, 12, 20], [(0, 9, False)]),
    "81_lower7": (
        [0, 10, 6, 18, 13, 16, 11, 15, 12, 20],
        [(0, 3, True), (3, 6, True), (6, 9, False)],
    ),
    "79_upper": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 15, 24, 8],
        [(0, 3, True), (3, 8, True), (8, 11, False)],
    ),
    "79_lower": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 8],
        [(0, 3, True), (3, 10, False)],
    ),
    "79_only9_changed": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 15, 28, 8],
        [(0, 3, True), (3, 8, True), (8, 11, False)],
    ),
    "79_equal9and7": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 18, 28, 8],
        [(0, 3, True), (3, 10, False)],
    ),
    "81_reference": ([0, 8, 3, 6, 2, 12, 7, 11, 4, 15], [(0, 5, True), (5, 8, False)]),
    # L077 pp1966-1967: A's one-pen rebound does not break A; a segment
    # rebound does. These values preserve the two illustrations' relations.
    "77_pen_rebound": ([40, 25, 32, 10, 38, 23, 30, 14, 29, 5], [(0, 9, False)]),
    "77_segment_rebound": (
        [40, 25, 32, 10, 38, 23, 30, 14, 29, 22, 35, 20, 27, 5],
        [(0, 3, True), (3, 10, True), (10, 13, False)],
    ),
    # L067/L077 deductions: the parent's strict second fractal fixes B too.
    "77_second_fractal_own_gap": (
        [0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14],
        [(0, 3, True), (3, 6, True), (6, 9, False)],
    ),
    "77_completed_successor_new_low": (
        [0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14, 10],
        [(0, 3, True), (3, 6, True), (6, 9, False)],
    ),
    "77_second_fractal_nonextreme_successor": (
        [40, 25, 32, 0, 15, 10, 12, 5, 18, 4, 13, 6, 11, 5],
        [(0, 3, True), (3, 10, True), (10, 13, False)],
    ),
    "78_invalidated_gap_absorbs_prior_local_turn": (
        [0, 14, 10, 30, 21, 25, 17, 27, 21, 29, 17, 26, 16, 32],
        [(0, 13, False)],
    ),
    "78_new_turn_after_gap_invalidation": (
        [0, 14, 10, 30, 21, 25, 17, 27, 21, 29, 17, 26, 16, 32, 22, 28, 15],
        [(0, 13, True), (13, 16, False)],
    ),
    # L079 lower figure plus L071's complete overlapping reverse triple.
    # The unbroken contained 7-8 / 9-10 stem shifts the candidate to P10;
    # it does not replace its effective left reference with the interior 8-9.
    "79_lower_completed_reverse_triple": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 8, 10, 5],
        [(0, 3, True), (3, 10, True), (10, 13, False)],
    ),
    "79_contained_pivot_after_second_feature": (
        [0, 100, 60, 180, 130, 160, 110, 140, 120, 131, 125, 138, 132, 135, 101, 112, 90],
        [(0, 3, True), (3, 6, True), (6, 13, True), (13, 16, False)],
    ),
    "79_contained_pivot_on_six_price_levels": (
        [0, 5, 1, 3, 2, 5, 3, 4, 1, 2, 0],
        # The initial one-pen high is equalled at the qualified P5 turn.
        # This synthetic equal-origin case differs from L079's strict lower high.
        [(0, 5, True), (5, 10, False)],
    ),
    "79_equal_contained_pivot_has_a_later_break_context": (
        [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 23, 28, 8, 10, 5],
        [(0, 3, True), (3, 12, True), (12, 15, False)],
    ),
    "79_equal_contained_pivot_on_six_price_levels": (
        [0, 5, 1, 3, 2, 5, 3, 4, 3, 4, 1, 2, 0],
        [(0, 5, True), (5, 12, False)],
    ),
}


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("name", CASES)
def test_original_figure_divisions(name, mirror):
    points, expected = CASES[name]
    values = strokes(points, mirror)
    lines = XdCalculator().calculate(values)
    assert geometry(lines) == expected
    for line in lines:
        assert line.start_line is values[line.start_line.index]
        assert line.end_line is values[line.end_line.index]
        assert line.high == max(
            b.high for b in values[line.start_line.index : line.end_line.index + 1]
        )


@pytest.mark.parametrize("mirror", [False, True])
def test_second_sequence_own_gap_requires_no_third_sequence(mirror):
    values = strokes([0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14], mirror)
    lines = XdCalculator().calculate(values)
    first = lines[0]
    assert first.done and first.end_line.index + 1 == 3
    assert first.locked_at == values[8].locked_at
    # L077 pp1965: the second fractal is C breaking B, so B is complete as
    # well as A. Requiring B to collect another sequence reintroduces the
    # explicitly removed third-sequence recursion.
    assert geometry(lines) == [(0, 3, True), (3, 6, True), (6, 9, False)]
    assert lines[1].locked_at == values[8].locked_at
    parent, successor = (line.construction_evidence for line in lines[:2])
    assert successor.parent_key == parent.key
    assert successor.first_sequence == parent.second_sequence
    assert successor.initial_gap and not successor.second_sequence


@pytest.mark.parametrize("mirror", [False, True])
def test_second_fractal_completed_successor_cannot_extend_after_a_later_new_extreme(
    mirror,
):
    # The low at P10 cannot reopen B after C supplied the parent's second
    # fractal at P9. L077 pp1965 and L078's already-broken predecessor rule.
    values = strokes([0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14, 10], mirror)
    lines = XdCalculator().calculate(values)
    assert geometry(lines) == [(0, 3, True), (3, 6, True), (6, 9, False)]
    assert lines[1].locked_at == values[8].locked_at


@pytest.mark.parametrize("missing", [0, 3, 4, 6, 8])
def test_inherited_second_fractal_cannot_confirm_without_its_parent_evidence(missing):
    values = strokes([0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14])
    values[missing].locked_at = None
    lines = XdCalculator().calculate(values)
    assert not any(line.done for line in lines)
    assert geometry(lines) == [(0, 3, False), (3, 6, False), (6, 9, False)]


def test_inherited_confirmation_waits_for_the_latest_parent_support_time():
    values = strokes([0, 10, 6, 18, 13, 16, 11, 12, 11.5, 14])
    # The first pen belongs only to A, outside B's own body and feature pens.
    values[0].locked_at = BASE + timedelta(days=1)
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    assert [line.locked_at for line in lines[:2]] == [values[0].locked_at] * 2
    values[0].selection_pending = True
    assert not any(line.done for line in calculator.calculate(values))


@pytest.mark.parametrize("pairs", [1, 8, 32])
@pytest.mark.parametrize("mirror", [False, True])
def test_repeated_contracting_gap_pairs_preserve_each_completed_boundary(pairs, mirror):
    # L077 pp1965 describes repeated contracting gaps. This is a finite,
    # exact-price deduction, not a claim about an infinite market trajectory.
    from fractions import Fraction

    points = [Fraction(p) for p in CASES["77_second_fractal_own_gap"][0]]
    for _ in range(pairs - 1):
        shoulder, peak = points[-3], points[-1]
        first_low = (shoulder + peak) / 2
        first_high = (first_low + peak) / 2
        next_low = (shoulder + first_low) / 2
        next_high = (next_low + first_low) / 2
        points.extend(
            [
                first_low,
                first_high,
                next_low,
                next_high,
                (next_low + next_high) / 2,
                (first_low + first_high) / 2,
            ]
        )
    values = strokes(points, mirror)
    calculator = XdCalculator()
    for count in range(1, pairs + 1):
        prefix = values[: 6 * count + 3]
        lines = calculator.calculate(prefix)
        expected = [(i, i + 3, True) for i in range(0, 6 * count, 3)]
        expected.append((6 * count, 6 * count + 3, False))
        assert geometry(lines) == expected
        for j in range(count):
            parent, child = calculator.evidence[2 * j : 2 * j + 2]
            assert child.parent_key == parent.key and child.initial_gap
            assert lines[2 * j].locked_at == lines[2 * j + 1].locked_at
            assert lines[2 * j].locked_at == values[6 * j + 8].locked_at


@pytest.mark.parametrize("repeats", [24, 25, 26, 35, 200])
@pytest.mark.parametrize("mirror", [False, True])
def test_containment_confirmation_has_no_fixed_search_horizon(repeats, mirror):
    # The middle feature keeps containing the following elements. Its final
    # right shoulder proves the same P3 boundary regardless of waiting length.
    points = [0, 10, 6, 20, 8] + [v for _ in range(repeats) for v in (18, 9)] + [17, 5]
    values = strokes(points, mirror)
    before = XdCalculator().calculate(values[:-1])
    assert not any(x.done for x in before)
    first = XdCalculator().calculate(values)[0]
    assert first.done and first.end_line.index + 1 == 3
    assert first.locked_at == values[-1].locked_at


@pytest.mark.parametrize("missing", range(6))
def test_every_pen_used_in_the_break_proof_must_be_confirmed(missing):
    values = strokes(CASES["67_first"][0])
    values[missing].locked_at = None
    lines = XdCalculator().calculate(values)
    assert not any(x.done for x in lines)


def test_selection_pending_pen_cannot_provide_formal_proof():
    values = strokes(CASES["67_first"][0])
    values[3].selection_pending = True
    assert not any(x.done for x in XdCalculator().calculate(values))


def test_confirmation_uses_latest_support_time_even_if_not_last_pen():
    values = strokes(CASES["67_first"][0])
    values[3].locked_at = BASE + timedelta(days=1)
    assert XdCalculator().calculate(values)[0].locked_at == values[3].locked_at


def test_in_place_append_and_confirmation_changes_invalidate_cached_result():
    complete = strokes(CASES["67_first"][0])
    values = complete[:3]
    calculator = XdCalculator()
    calculator.calculate(values)
    values.extend(complete[3:])
    assert geometry(calculator.calculate(values)) == CASES["67_first"][1]
    values[3].locked_at = None
    assert not calculator.calculate(values)[0].done
    values[3].locked_at = complete[5].locked_at
    assert calculator.calculate(values)[0].done


def test_replaced_historical_pen_is_not_hidden_by_identity_suffix():
    values = strokes(CASES["67_first"][0])
    calculator = XdCalculator()
    calculator.calculate(values)
    changed = values.copy()
    changed[0] = SimpleNamespace(**vars(changed[0]))
    changed[0].locked_at = None
    assert not calculator.calculate(changed)[0].done


def test_containment_equality_is_absolute_not_rounded_or_tolerant():
    a = {"low": Decimal("1.001"), "high": Decimal("2.000")}
    b = {"low": Decimal("1.000"), "high": Decimal("1.999")}
    assert not _has_inclusion(a, b)


@pytest.mark.parametrize("number_kind", ["decimal", "integer", "fraction"])
def test_finite_exact_price_scaling_does_not_overflow_through_float(number_kind):
    points = CASES["67_first"][0]
    if number_kind == "decimal":
        scaled = [Decimal(v).scaleb(400) for v in points]
    elif number_kind == "integer":
        scaled = [v * 10**400 for v in points]
    else:
        from fractions import Fraction

        scaled = [Fraction(v * 10**400, 3) for v in points]
    assert geometry(XdCalculator().calculate(strokes(scaled))) == CASES["67_first"][1]


@pytest.mark.parametrize(
    "invalid",
    [
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_nonfinite_price_is_rejected_without_exact_type_conversion(invalid):
    values = strokes(CASES["67_first"][0])
    values[1].high = invalid
    with pytest.raises(ValueError, match="requires finite prices"):
        XdCalculator().calculate(values)


def test_nonfinite_in_place_edit_is_validated_before_snapshot_comparison():
    values = strokes([Decimal(p) for p in CASES["67_first"][0]])
    calculator = XdCalculator()
    original = calculator.calculate(values)
    values[1].high = Decimal("sNaN")
    with pytest.raises(ValueError, match="requires finite prices"):
        calculator.calculate(values)
    assert calculator.xds is original


@pytest.mark.parametrize("name", CASES)
def test_figure_prefixes_are_causal_and_batch_incremental_equivalent(name):
    points, _ = CASES[name]
    values = strokes(points)
    calculator = XdCalculator()
    frozen = {}
    for count in range(3, len(values) + 1):
        lines = calculator.calculate(values[:count])
        assert geometry(lines) == geometry(XdCalculator().calculate(values[:count]))
        current = {
            (x.start_line.index, x.end_line.index): x.locked_at for x in lines if x.done
        }
        assert all(current.get(key) == time for key, time in frozen.items())
        for time in current.values():
            assert time <= values[count - 1].locked_at
        frozen = current


@pytest.mark.parametrize("name", CASES)
def test_exact_positive_affine_price_change_preserves_source_divisions(name):
    points, _ = CASES[name]
    transformed = [
        Decimal(str(v)) * Decimal("0.00000000001") + Decimal("2") for v in points
    ]
    assert geometry(XdCalculator().calculate(strokes(transformed))) == geometry(
        XdCalculator().calculate(strokes(points))
    )


@pytest.mark.parametrize("mirror", [False, True])
def test_inclusion_uses_last_noncontained_direction_without_back_merging(mirror):
    ranges = [(10, 15), (5, 12), (4, 18)]
    if mirror:
        ranges = [(-h, -l) for l, h in ranges]
    elements = [
        {"low": low, "high": high, "bi": SimpleNamespace(index=i)}
        for i, (low, high) in enumerate(ranges)
    ]
    result = _process_inclusion(elements, "down" if mirror else "up")
    expected = [(10, 15), (4, 12)]
    if mirror:
        expected = [(-h, -l) for l, h in expected]
    assert [(x["low"], x["high"]) for x in result] == expected
    assert [b.index for b in result[-1]["merged_bis"]] == [1, 2]


@pytest.mark.parametrize("mirror", [False, True])
def test_second_feature_proof_is_standard_in_both_price_coordinates(mirror):
    values = strokes([40, 25, 32, 0, 15, 10, 12, 5, 18, 4, 13, 6, 11, 5], mirror)
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    proof = calculator.evidence[0]
    assert proof.rule == "second-feature-fractal"
    a, b, c = proof.second_sequence
    if mirror:
        assert b.low < min(a.low, c.low) and b.high < min(a.high, c.high)
    else:
        assert b.high > max(a.high, c.high) and b.low > max(a.low, c.low)
    assert b.source_indices == (10,)
    # B retains this strict F2 context, including the earlier inclusion
    # (pens 6 and 8). Independent first-sequence record filtering would use
    # a different middle and lose the already-proven non-extreme endpoint.
    assert geometry(lines) == [(0, 3, True), (3, 10, True), (10, 13, False)]
    successor = calculator.evidence[1]
    assert successor.parent_key == proof.key
    assert successor.first_sequence == proof.second_sequence
    assert successor.first_sequence[0].source_indices == (6, 8)
    assert lines[1].locked_at == lines[0].locked_at == values[12].locked_at


def test_lesson79_feature_interval_is_not_a_replacement_physical_pen():
    values = strokes(CASES["79_upper"][0])
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    first = calculator.evidence[0]
    middle = first.first_sequence[1]
    assert middle.source_indices == (3, 5)
    assert (middle.low_source_index, middle.high_source_index) == (3, 5)
    assert first.end_index == 2
    # The next segment still has all five original pens, with internal P4=32.
    assert lines[1].end_line.index - lines[1].start_line.index + 1 == 5
    assert lines[1].end.val == 30 and lines[1].high == 32


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize(
    "name, actual_range",
    [("79_upper", (12, 32)), ("77_second_fractal_nonextreme_successor", (0, 18))],
)
def test_exported_segment_interval_keeps_internal_extremes_after_confirmation(
    name, actual_range, mirror,
):
    # L078 pp1976: downstream analysis uses the actual whole-segment range,
    # even when the proved endpoint is not its highest/lowest price. Closing
    # an unchanged proof must not narrow that range in the exported object.
    from chanlun.core.types import BI, FX

    values = strokes(CASES[name][0], mirror)
    physical = []
    for value in values:
        start = FX("di" if value.type == "up" else "ding", None, [], value.start.val)
        end = FX("ding" if value.type == "up" else "di", None, [], value.end.val)
        item = BI(start, end, value.type, value.index)
        item.locked_at = value.locked_at
        physical.append(item)
    last_lock = physical[-1].locked_at
    expected = (-actual_range[1], -actual_range[0]) if mirror else actual_range
    calculator = XdCalculator()
    for closed in (False, True):
        physical[-1].locked_at = last_lock if closed else None
        line = calculator.calculate(physical)[1]
        assert line.done is closed
        exported = line.to_dict()
        assert (exported["low"], exported["high"]) == expected
        assert (exported["zs_low"], exported["zs_high"]) == expected
        assert line.end_line.index + 1 == CASES[name][1][1][1]


@pytest.mark.parametrize("mirror", [False, True])
def test_pre_boundary_reference_preserves_its_standard_inclusion_sources(mirror):
    # L065 chronological inclusion in the same pre-boundary context. The
    # failed turn at P3 is absorbed: [6,10] and [4,14] standardize to [6,14].
    # Its high still belongs to pen 3, but its low and history belong to pen 1.
    calculator = XdCalculator()
    lines = calculator.calculate(strokes([0, 10, 6, 14, 4, 18, 13, 16, 10], mirror))
    assert geometry(lines) == [(0, 5, True), (5, 8, False)]
    left = calculator.evidence[0].first_sequence[0]
    assert (left.low, left.high) == ((-14, -6) if mirror else (6, 14))
    assert left.source_indices == (1, 3)
    assert (left.low_source_index, left.high_source_index) == (
        (3, 1) if mirror else (1, 3)
    )


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("strong_first", [False, True])
def test_failed_candidate_keeps_contained_reference_contributions(mirror, strong_first):
    # L065/L071: pens 3 and 5 already share one post-boundary inclusion
    # context before P7 extends the original segment. Pen 5's lower high
    # prevents a new reference pivot, not its contribution to that element.
    from script.check_segment_model import check_evidence

    points = (
        [0, 10, 6, 14, 4, 12, 8, 15, 9, 13, 7] if strong_first
        else [0, 10, 6, 14, 8, 12, 9, 15, 11, 13, 10]
    )
    values = strokes(points, mirror)
    calc = XdCalculator()
    assert geometry(calc.calculate(values)) == [(0, 7, True), (7, 10, False)]
    left = calc.evidence[0].first_sequence[0]
    lower = 8 if strong_first else 9
    assert (left.low, left.high) == ((-14, -lower) if mirror else (lower, 14))
    assert left.source_indices == ((1, 3, 5) if strong_first else (3, 5))
    assert (left.low_source_index, left.high_source_index) == ((3, 5) if mirror else (5, 3))
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("count", [1, 8, 32])
def test_reference_keeps_the_entire_resolved_containment_chain(mirror, count):
    from fractions import Fraction
    from script.check_segment_model import check_evidence

    points = [0, 10, 6, 14, 4]
    for i in range(count):
        points.extend((12, 8 + Fraction(3 * i, count)))
    lower = points[-1]
    points.extend((15, 9, 13, 7))
    values = strokes(points, mirror)
    calc = XdCalculator()
    calc.calculate(values)
    proof = calc.evidence[0]
    assert proof.end_index == 4 + 2 * count
    left = proof.first_sequence[0]
    assert left.source_indices == tuple(range(1, 4 + 2 * count, 2))
    assert (left.low, left.high) == ((-14, -lower) if mirror else (lower, 14))
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_second_feature_extreme_uses_declared_earlier_boundary_convention(mirror):
    # These are synthetic exact ties, not an author's extra tie-order rule.
    # F2 merges pens 6 and 8 into [11,13]; both supply its low. Keep the
    # earlier source in the parent, inherited successor and later continuation.
    points = [0, 10, 6, 18, 13, 16, 11, 14, 11, 13, 12, 14, 12.5, 13, 10]
    values = strokes(points, mirror)
    calculator = XdCalculator()
    early = calculator.calculate(values[:11])
    assert geometry(early) == [(0, 3, True), (3, 6, True), (6, 11, False)]
    parent, successor = calculator.evidence[:2]
    middle = parent.second_sequence[1]
    assert middle.source_indices == (6, 8)
    chosen = middle.high_source_index if mirror else middle.low_source_index
    assert chosen == 6
    assert successor.first_sequence == parent.second_sequence
    assert successor.end_index == chosen - 1
    original_time = early[1].locked_at
    extended = calculator.calculate(values)
    assert geometry(extended) == [
        (0, 3, True), (3, 6, True), (6, 11, True), (11, 14, False)
    ]
    assert extended[1].locked_at == original_time == values[10].locked_at


@pytest.mark.parametrize("mirror", [False, True])
def test_contained_pivot_keeps_its_sources_and_effective_left_reference(mirror):
    values = strokes(CASES["79_lower_completed_reverse_triple"][0], mirror)
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    proof = lines[1].construction_evidence
    assert proof.end_index == 9 and proof.witness_index == 12
    assert proof.first_sequence[0].source_indices == (6,)
    stem = proof.pivot_stem
    assert stem.source_indices == (7, 9)
    assert (stem.low, stem.high) == ((-28, -18) if mirror else (18, 28))
    assert (stem.low_source_index, stem.high_source_index) == (
        (9, 7) if mirror else (7, 9)
    )
    assert lines[1].high == (32 if not mirror else -12)
    # The interior pen 8 is omitted only from this reference role. It remains
    # in the actual body and must provide closed confirmation evidence.
    values[8].locked_at = None
    pending = calculator.calculate(values)
    assert not pending[1].done
    assert pending[1].construction_evidence == proof


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("endpoint", [25, 26])
def test_contained_pivot_still_requires_a_strict_effective_extremum(endpoint, mirror):
    points = list(CASES["79_lower_completed_reverse_triple"][0])
    points[10] = endpoint
    calculator = XdCalculator()
    lines = calculator.calculate(strokes(points, mirror))
    assert len(calculator.evidence) == 1
    assert not lines[1].done


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("repeats", [1, 8, 32])
@pytest.mark.parametrize("rising_lows", [False, True])
def test_same_price_contained_stem_keeps_value_source_separate_from_segment_end(
    mirror, repeats, rising_lows,
):
    # L079 lower-figure induction + L075 equal-bound inclusion + L071's
    # strict first break. The earlier same-price point has no first break;
    # the later one does. A stem's price supplier is not the middle feature
    # defining the actual boundary. This does not change the F2 tie policy.
    from fractions import Fraction
    from script.check_segment_model import check_evidence

    points = [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28]
    for i in range(repeats):
        low = 21 + Fraction(7 * (i + 1), repeats + 1) if rising_lows else 21
        points.extend((low, 28))
    points.extend((8, 10, 5))
    values = strokes(points, mirror)
    boundary = 10 + repeats * 2
    calc = XdCalculator()
    before = calc.calculate(values[:-1])
    assert len(calc.evidence) == 1 and not before[1].done
    after = calc.calculate(values)
    assert geometry(after) == [(0, 3, True), (3, boundary, True), (boundary, boundary + 3, False)]
    proof = calc.evidence[1]
    stem = proof.pivot_stem
    assert stem.source_indices == tuple(range(7, boundary, 2))
    supplier = stem.low_source_index if mirror else stem.high_source_index
    assert supplier == 9 and supplier != proof.end_index
    middle = proof.first_sequence[1]
    assert middle.source_indices == (boundary,)
    actual_source = middle.low_source_index if mirror else middle.high_source_index
    assert actual_source == boundary == proof.end_index + 1
    assert proof.first_sequence[0].source_indices == (6,)
    assert after[1].locked_at == values[-1].locked_at
    check_evidence(calc, values)
    # The early supplier remains a genuine body dependency, even though it
    # does not select the physical end in this later break context.
    values[9].locked_at = None
    waiting = calc.calculate(values)
    assert not waiting[1].done and waiting[1].construction_evidence == proof


def test_pending_reasons_distinguish_missing_second_sequence_and_short_input():
    calculator = XdCalculator()
    calculator.calculate(strokes([0, 10, 6]))
    assert calculator.tail_state.reason == "insufficient-pens"
    calculator.calculate(strokes(CASES["70_fill_still_pending"][0]))
    assert calculator.tail_state.reason == "waiting-second-feature"
    assert calculator.tail_state.candidate_index == 2


@pytest.mark.parametrize("mirror", [False, True])
def test_new_original_extreme_does_not_resurrect_a_turn_inside_invalidated_gap(mirror):
    # L078 lines 208-220: without a second fractal, the new old-direction
    # extreme makes the provisional pieces one continuing segment. A local
    # P9 turn that could not confirm before that extension cannot be borrowed
    # retrospectively merely because its earlier parent hypothesis failed.
    points = CASES["78_invalidated_gap_absorbs_prior_local_turn"][0]
    values = strokes(points, mirror)
    calculator = XdCalculator()
    assert geometry(calculator.calculate(values[:-1])) == [(0, 3, False)]
    assert calculator.tail_state.reason == "waiting-second-feature"
    assert geometry(calculator.calculate(values)) == [(0, 13, False)]
    assert not calculator.evidence


@pytest.mark.parametrize("mirror", [False, True])
def test_after_gap_invalidation_only_a_later_turn_can_supply_a_new_boundary(mirror):
    values = strokes(CASES["78_new_turn_after_gap_invalidation"][0], mirror)
    calculator = XdCalculator()
    lines = calculator.calculate(values)
    assert geometry(lines) == [(0, 13, True), (13, 16, False)]
    proof = calculator.evidence[0]
    assert proof.end_index == 12 and proof.witness_index == 15
    assert lines[0].locked_at == values[15].locked_at


@pytest.mark.parametrize("corruption", ["direction", "disconnected", "nan", "range"])
def test_invalid_pen_input_is_rejected_before_cached_state_is_replaced(corruption):
    values = strokes(CASES["67_first"][0])
    calculator = XdCalculator()
    original = calculator.calculate(values)
    bad = strokes(CASES["67_first"][0])
    if corruption == "direction":
        bad[1].type = "up"
    elif corruption == "disconnected":
        bad[1].start.val = 11
        bad[1].high = 11
    elif corruption == "nan":
        bad[1].end.val = float("nan")
    else:
        bad[1].high += 1
    with pytest.raises(ValueError, match="segment input"):
        calculator.calculate(bad)
    assert calculator.xds is original


def test_unchanged_source_reuses_output_but_later_results_do_not_clear_it():
    values = strokes(CASES["67_first"][0])
    calculator = XdCalculator()
    original = calculator.calculate(values)
    assert calculator.calculate(list(values)) is original
    calculator.calculate(values[:3])
    assert geometry(original) == CASES["67_first"][1]


def test_fixed_price_walks_preserve_causal_boundaries_and_input_geometry():
    import random

    rng = random.Random(1927)
    for _ in range(160):
        points = [50]
        for i in range(32):
            points.append(points[-1] + (1 if i % 2 == 0 else -1) * rng.randint(1, 20))
        values = strokes(points)
        frozen = {}
        calculator = XdCalculator()
        for count in range(3, len(values) + 1):
            lines = calculator.calculate(values[:count])
            current = {
                (x.start_line.index, x.end_line.index): x.locked_at
                for x in lines
                if x.done
            }
            assert all(current.get(key) == time for key, time in frozen.items())
            for left, right in zip(lines, lines[1:]):
                assert left.end_line.index + 1 == right.start_line.index
                assert left.type != right.type
            for line in lines:
                a, b = line.start_line.index, line.end_line.index
                assert b - a >= 2 and (b - a) % 2 == 0
                assert max(values[a].low, values[a + 2].low) <= min(
                    values[a].high, values[a + 2].high
                )
                assert line.type == values[a].type == values[b].type
            frozen = current


@pytest.mark.parametrize("mirror", [False, True])
def test_first_pen_break_followed_by_new_extreme_and_no_three_pen_overlap(mirror):
    # L077 pp1964: the failed reverse origin cannot borrow a later triple.
    values = strokes([0, 10, 6, 14, 4, 18, 16], mirror)
    lines = XdCalculator().calculate(values)
    assert not any(x.done for x in lines)
    assert lines[0].end_line.index + 1 == 5


@pytest.mark.parametrize("mirror", [False, True])
def test_failed_reverse_continues_segment_with_already_proven_predecessor(mirror):
    # L078 pp1972-1974: B's prior break is an explicit prerequisite.
    values = strokes([0, 16, 10, 20, 10, 14, 5, 24, 8, 22, 3], mirror)
    lines = XdCalculator().calculate(values)
    assert geometry(lines) == [(0, 3, True), (3, 10, False)]
    if mirror:
        assert lines[1].low < lines[1].start.val
    else:
        assert lines[1].high > lines[1].start.val


def test_several_new_extremes_and_a_single_drop_do_not_confirm_a_segment():
    values = strokes([0, 10, 6, 14, 9, 18, 12, 22, 4])
    assert geometry(XdCalculator().calculate(values)) == [(0, 7, False)]


@pytest.mark.parametrize("missing", [3, 4, 6, 7, 8])
def test_second_sequence_auxiliary_evidence_cannot_be_unlocked(missing):
    values = strokes(CASES["67_second_no_fill"][0])
    values[missing].locked_at = None
    assert not any(x.done for x in XdCalculator().calculate(values))


@pytest.mark.parametrize("mirror", [False, True])
def test_later_middle_inclusion_does_not_change_raw_first_classification(mirror):
    # Reviewed L071 reading: raw [7,12] overlaps left [0,8]. Inclusion with
    # [9,11] yields [9,12], but that later gap does not reclassify the boundary.
    values = strokes([-4, 8, 0, 12, 7, 11, 9, 10, 8.5], mirror)
    calculator = XdCalculator()
    assert geometry(calculator.calculate(values)) == [(0, 3, True), (3, 8, False)]
    proof = calculator.evidence[0]
    assert not proof.initial_gap and not proof.second_sequence
    left, middle, _ = proof.first_sequence
    assert max(left.low, middle.low) > min(left.high, middle.high)


@pytest.mark.parametrize("mirror", [False, True])
def test_non_extreme_first_pen_break_uses_the_original_first_interval(mirror):
    values = strokes([-10, 30, 0, 10, 6, 14, 4, 13, 11, 12, 10.5], mirror)
    calculator = XdCalculator()
    assert geometry(calculator.calculate(values)) == [(0, 5, True), (5, 10, False)]
    proof = calculator.evidence[0]
    assert proof.rule == "first-pen-break"
    assert not proof.initial_gap and not proof.second_sequence


def test_serialized_calculator_rebinds_evidence_to_current_input_objects():
    import pickle

    values = strokes(CASES["79_upper"][0])
    calculator = XdCalculator()
    calculator.calculate(values)
    restored = pickle.loads(pickle.dumps(calculator))
    lines = restored.calculate(values)
    assert geometry(lines) == CASES["79_upper"][1]
    assert lines[0].start_line is values[0]
    assert lines[1].construction_evidence == restored.evidence[1]


@pytest.mark.parametrize("kind", ["new_extremes", "nested_pivots"])
def test_unbounded_candidate_search_does_not_rescan_linear_stems(kind):
    class CountedPrice(int):
        comparisons = 0

        def __lt__(self, other):
            type(self).comparisons += 1
            return int.__lt__(self, other)

        def __gt__(self, other):
            type(self).comparisons += 1
            return int.__gt__(self, other)

    def work(repeats):
        if kind == "new_extremes":
            points = [0, 10, 6, 14]
            for _ in range(repeats):
                high = points[-1]
                points.extend((high - 6, high - 2, high - 5, high + 4))
        else:
            scale = repeats + 1
            points = [p * scale for p in [30, 20, 29, 12, 32, 14, 26, 18, 30]]
            for j in range(1, repeats + 1):
                points.extend((18 * scale + 8 * j, 30 * scale - 4 * j))
            points.extend((8 * scale, 10 * scale, 5 * scale))
        values = strokes([CountedPrice(p) for p in points])
        CountedPrice.comparisons = 0
        lines = XdCalculator().calculate(values)
        if kind == "new_extremes":
            assert geometry(lines) == [(0, len(values), False)]
        else:
            endpoint = 8 + 2 * repeats
            assert geometry(lines) == [(0, 3, True), (3, endpoint, True),
                                       (endpoint, endpoint + 3, False)]
            assert lines[1].construction_evidence.pivot_stem is not None
        return CountedPrice.comparisons

    # Count public numeric operations rather than machine-dependent wall time.
    # Each failed contained reversal is followed by a new extreme; growing the
    # history must not make all earlier feature references run again each time.
    small, large = work(150), work(300)
    assert large < 2.5 * small, (small, large)
