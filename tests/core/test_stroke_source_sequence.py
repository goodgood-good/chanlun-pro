"""原文条件独立用例，不以 BiCalculator 的既有输出为预期。

拓扑依据：D:/缠论/教你炒股票108课天使版.pdf，第 1811 页图 4；
D:/缠论/chanlun_lesson_corpus/L077_一些概念的再分辨(2007-09-05232401).md，
247–283 行及第 1963 页图 4、5。价格为保持图示关系的合成数值，
不是作者给出的市场价格。第一个独立 K 线要求见 L062 64–82 行。
"""

import random
from types import SimpleNamespace

import pytest

from chanlun.core.stroke_sequence import SourceStrokeSelection, SourceStrokeSequence


SAME_BOTTOM = [
    (12, 8), (10, 6), (12, 8), (14, 10), (16, 12), (20, 16),
    (18, 14), (16, 12), (15, 11), (14, 10), (16, 12), (18, 14),
    (16, 12), (14, 10), (16, 12), (18, 14), (20, 16), (24, 20), (22, 18),
]

LOWER_BOTTOM = [
    (22, 18), (24, 20), (22, 18), (20, 16), (18, 14), (16, 12),
    (18, 14), (20, 16), (16, 12), (14, 10), (16, 12),
]

MONTHLY_RESELECTION = [
    (11, 7), (12, 8), (11, 7), (10, 6), (9, 5), (8, 4), (11, 7),
    (14, 10), (12, 8), (10, 6), (8, 4), (7, 3), (9, 5),
]

ANCHORED_REVERSAL = [
    (12, 8), (10, 6), (12, 8), (14, 10), (16, 12), (20, 16),
    (18, 14), (17, 13), (16, 12), (14, 10), (15, 11), (16, 12),
    (17, 13), (18, 14), (13, 9), (8, 4), (10, 6), (12, 8),
    (14, 10), (24, 20), (14, 10),
]

# L077 271–283: after the earliest top joins a bottom, the intermediate
# tops are crossed out. These synthetic prices also leave a later short
# top at 16 and a qualified top at 18; neither may restore the crossed-out
# top at 10 and swallow the reversal 8 -> 13 -> 18.
DISCARDED_TOP = [
    (92, 88), (89, 85), (92, 88), (91, 87), (93, 89), (94, 90),
    (95, 91), (96, 92), (99, 95), (96, 92), (99, 95), (98, 94),
    (95, 91), (92, 88), (93, 89), (95, 91), (96, 92), (93, 89),
    (96, 92), (93, 89), (92, 88), (90, 86), (93, 89), (90, 86), (91, 87),
]

# The bottom at 8 is absorbed when 1 -> 5 extends to the higher top at 10.
# A later complete 10 -> 14 -> 18 must stay adjacent in the retained list.
DISCARDED_BOTTOM_AFTER_EXTENSION = [
    (98, 94), (97, 93), (98, 94), (100, 96), (101, 97), (102, 98),
    (100, 96), (98, 94), (97, 93), (100, 96), (103, 99), (102, 98),
    (101, 97), (102, 98), (99, 95), (102, 98), (104, 100), (105, 101),
    (106, 102), (103, 99),
]


def original_conditions(prices, mirror=False):
    """直接转写三 K、间隔及极值条件，不调用生产布尔判定函数。"""
    if mirror:
        prices = [(100 - low, 100 - high) for high, low in prices]
    points = []
    for i in range(1, len(prices) - 1):
        left, middle, right = prices[i - 1:i + 2]
        kind = None
        if middle[0] > max(left[0], right[0]) and middle[1] > max(left[1], right[1]):
            kind = "ding"
        elif middle[0] < min(left[0], right[0]) and middle[1] < min(left[1], right[1]):
            kind = "di"
        if kind:
            points.append(SimpleNamespace(type=kind, k=SimpleNamespace(index=i),
                                          val=middle[0 if kind == "ding" else 1]))

    def qualifies(first, second):
        if first.type == second.type or second.k.index - first.k.index < 4:
            return False
        top, bottom = (first, second) if first.type == "ding" else (second, first)
        span = prices[first.k.index:second.k.index + 1]
        return max(p[0] for p in span) == top.val and min(p[1] for p in span) == bottom.val

    return points, qualifies


@pytest.mark.parametrize("mirror", [False, True])
def test_l065_smaller_turn_does_not_prevent_more_extreme_same_type_endpoint(mirror):
    points, qualifies = original_conditions(LOWER_BOTTOM, mirror)
    seq = SourceStrokeSequence()
    for fx in points:
        relation = seq.append(fx, qualifies)
    assert [points[i].k.index for i in relation.adjacent] == [1]
    assert not relation.excluded


@pytest.mark.parametrize("mirror", [False, True])
def test_l077_equal_same_type_candidates_remain_in_order_for_the_next_opposite(mirror):
    points, qualifies = original_conditions(SAME_BOTTOM, mirror)
    seq = SourceStrokeSequence()
    for fx in points:
        relation = seq.append(fx, qualifies)
    assert [points[i].k.index for i in relation.adjacent] == [9, 13]
    assert points[min(relation.adjacent)].k.index == 9


@pytest.mark.parametrize("mirror", [False, True])
def test_partial_reversal_remains_a_candidate_without_pretending_full_selection(mirror):
    points, qualifies = original_conditions(ANCHORED_REVERSAL, mirror)
    seq = SourceStrokeSequence()
    for fx in points:
        relation = seq.append(fx, qualifies)
        if fx.k.index == 15:
            assert [points[i].k.index for i in relation.eligible] == [5]
            assert [points[i].k.index for i in relation.adjacent] == [5]
            assert not relation.excluded
            assert [tuple(points[i].k.index for i in proof) for proof in relation.open_reversals] == [(5, 9, 13)]
            # The end at 15 cannot connect back to 13. Keep the incomplete
            # reversal for contextual selection, without declaring a full
            # subdivision or a completed graph from these facts alone.
            at = {p.k.index: p for p in points}
            assert not qualifies(at[13], at[15])
    assert not hasattr(seq, "selected")
    assert not hasattr(seq, "completions")


@pytest.mark.parametrize("mirror", [False, True])
def test_rewind_restores_only_the_last_fractals_added_definition_evidence(mirror):
    points, qualifies = original_conditions(ANCHORED_REVERSAL, mirror)
    live, cold = SourceStrokeSequence(), SourceStrokeSequence()
    for fx in points:
        live.append(fx, qualifies)
        live.rewind_last()
        live.append(fx, qualifies)
        cold.append(fx, qualifies)
        assert live.relations == cold.relations
        assert live.turns == cold.turns
        assert live.frontiers == cold.frontiers


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("prices,expected", [
    (LOWER_BOTTOM, [1, 9]),
    (SAME_BOTTOM, [1, 5, 9, 17]),
    (MONTHLY_RESELECTION, [7, 11]),
])
def test_source_figures_construct_without_bi_state_or_existing_outputs(prices, expected, mirror):
    # L065 p1811 图4；L077 p1963 图5；L069 正文28–49行的相对关系。
    # Only price/type/index fields are supplied: no BI, done or lock metadata.
    points, qualifies = original_conditions(prices, mirror)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        selection.append(sequence.append(fx, qualifies), sequence)
    assert [points[i].k.index for i in selection.path()] == expected


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("prices,expected", [
    (DISCARDED_TOP, [1, 8, 13, 18, 23]),
    (DISCARDED_BOTTOM_AFTER_EXTENSION, [1, 10, 14, 18]),
])
def test_l077_crossed_out_interior_fractal_cannot_bypass_retained_reversals(prices, expected, mirror):
    points, qualifies = original_conditions(prices, mirror)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        decision = selection.append(sequence.append(fx, qualifies), sequence)
    assert [points[i].k.index for i in selection.path()] == expected
    if prices is DISCARDED_TOP:
        proofs = [(points[p.discarded].k.index, points[p.stroke_start].k.index,
                   points[p.stroke_end].k.index) for p in decision.omitted]
        assert (10, 8, 13) in proofs
        assert (16, 13, 18) in proofs


@pytest.mark.parametrize("mirror", [False, True])
def test_fractal_rewind_restores_selection_and_contextual_omission(mirror):
    points, qualifies = original_conditions(DISCARDED_TOP, mirror)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    cold_sequence, cold_selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        selection.append(sequence.append(fx, qualifies), sequence)
        selection.rewind_last()
        sequence.rewind_last()
        decision = selection.append(sequence.append(fx, qualifies), sequence)
        expected = cold_selection.append(cold_sequence.append(fx, qualifies), cold_sequence)
        assert decision == expected
        assert selection.path() == cold_selection.path()


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("prices,expected", [
    (LOWER_BOTTOM, [1, 9]),
    (SAME_BOTTOM, [1, 5, 9, 17]),
    (MONTHLY_RESELECTION, [7, 11]),
    # This extended synthetic case adds short conflicts beyond the L077
    # figure. Under the user's path-B rule, 18 -> 21 removes 13 and 18;
    # the completed 1 -> 8 survives. The historical model above stays tested.
    (DISCARDED_TOP, [1, 8, 21]),
    (DISCARDED_BOTTOM_AFTER_EXTENSION, [1, 10, 14, 18]),
])
def test_production_adapter_obeys_source_figures_and_user_short_conflict_policy(prices, expected, mirror):
    from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate

    _, calc = _calculate(_bars(prices, mirror))
    assert [calc.bis[0].start.k.index, *(bi.end.k.index for bi in calc.bis)] == expected
    assert not calc.audit_endpoint_ranges()
    assert not calc.audit_endpoint_adjacency()


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("later_top,expected", [(19, [1, 11]), (20, [1, 11]), (22, [7, 11])])
def test_short_lower_bottom_with_later_top_below_equal_or_above_previous_top(later_top, expected, mirror):
    # P1 -> A5 qualifies; A5 -> B7 is too near; C11 is below A5.
    # L066's range condition excludes P -> C only when B exceeds P.
    # L077 retains the earlier eligible top otherwise. The B > P branch
    # has the relative relationships of the L069 monthly example.
    prices = [
        (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (12, 8),
        (18, 10), (later_top, later_top - 4), (later_top - 1, 12),
        (14, 8), (10, 6), (8, 4), (10, 6),
    ]
    points, qualifies = original_conditions(prices, mirror)
    at = {fx.k.index: fx for fx in points}
    assert not qualifies(at[5], at[7])
    assert qualifies(at[7], at[11])
    assert qualifies(at[1], at[11]) == (later_top <= 20)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        selection.append(sequence.append(fx, qualifies), sequence)
    assert [points[i].k.index for i in selection.path()] == expected


@pytest.mark.parametrize("mirror", [False, True])
def test_higher_top_and_lower_bottom_cannot_waive_the_old_stroke_distance(mirror):
    # B7 > P1 and C9 < A5, but B7 -> C9 is also too short. L069 does
    # not authorize inventing this pen while violating L062's distance.
    prices = [
        (19, 15), (20, 16), (18, 14), (16, 12), (14, 10), (12, 8),
        (18, 10), (22, 18), (21, 6), (8, 4), (10, 6),
    ]
    points, qualifies = original_conditions(prices, mirror)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        relation = sequence.append(fx, qualifies)
        selection.append(relation, sequence)
    assert not relation.eligible
    assert [points[i].k.index for i in selection.path()] == [1, 5]
    assert selection.pending_continuation


@pytest.mark.parametrize("center,expected", [(3, []), (4, []), (5, [1, 5])])
def test_l062_independent_k_between_complete_fractals(center, expected):
    prices = [(11, 7), (10, 6)]
    prices.extend((9 + i, 5 + i) for i in range(2, center + 1))
    prices.append((prices[-1][0] - 1, prices[-1][1] - 1))
    points, qualifies = original_conditions(prices)
    sequence, selection = SourceStrokeSequence(), SourceStrokeSelection()
    for fx in points:
        selection.append(sequence.append(fx, qualifies), sequence)
    assert [points[i].k.index for i in selection.path()] == expected


@pytest.mark.parametrize("seed", [1, 8, 19, 41])
def test_definition_relations_match_independent_exhaustive_price_and_path_checks(seed):
    rng = random.Random(seed)
    price, prices = 100, []
    for _ in range(80):
        price += rng.choice((-3, -2, -1, 1, 2, 3))
        prices.append((price + 2, price - 2))
    points, qualifies = original_conditions(prices)
    sequence = SourceStrokeSequence()
    direct, reach = [], []
    for endpoint, fx in enumerate(points):
        eligible = [i for i in range(endpoint) if qualifies(points[i], fx)]
        direct.append(eligible)
        ancestors = set(eligible)
        for parent in eligible:
            ancestors.update(reach[parent])
        reach.append(ancestors)
        expected = [i for i in eligible if not any(i in reach[j] for j in eligible)]
        actual = sequence.append(fx, qualifies)
        assert list(actual.eligible) == eligible
        assert list(actual.adjacent) == expected
        for first, _ in actual.excluded:
            proof = sequence.decomposition(first, endpoint)
            assert len(proof) >= 4 and proof[0] == first and proof[-1] == endpoint
            assert all(a in direct[b] for a, b in zip(proof, proof[1:]))
