"""Weaker or equal same-type endpoints cannot bypass selection through a parent."""

import pytest

from script.reproduce_old_stroke_anomalies import PROTECTED_REVERSAL
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate
from tests.core.test_stroke_endpoint_candidates import _live_prefixes


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("later_top", [(16, 12), (18, 14)])
def test_unselected_parent_cannot_reintroduce_a_rejected_long_edge(mirror, later_top):
    prices = list(PROTECTED_REVERSAL)
    prices[19] = later_top
    before = None
    for count, calc in _live_prefixes(_bars(prices, mirror)):
        if count == 17:
            before = tuple(calc.completion_evidence)
        if count == 21:
            assert [(b.start.k.index, b.end.k.index) for b in calc.contiguous_bis] == [
                (1, 5), (5, 15), (15, 19),
            ]
            assert calc.completion_evidence == before
            # The old completed first pen remains while its two-edge tail
            # follows the user's short-conflict replacement policy.
            assert len(calc.stroke_components) == 1
            assert not calc.bis[-1].selection_pending and not calc.bis[-1].is_done()
            assert not any(d.revises_confirmed_selection for d in calc.selection_revisions)
            assert calc.continuation_blocked_at is None


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("later_top", [(16, 12), (18, 14)])
def test_protected_reversal_can_continue_when_a_later_qualified_bottom_arrives(mirror, later_top):
    prices = PROTECTED_REVERSAL + [(12, 8), (10, 6), (7, 3), (9, 5)]
    prices[19] = later_top
    for _, live in _live_prefixes(_bars(prices, mirror)):
        pass
    _, cold = _calculate(_bars(prices, mirror))
    assert [(b.start.k.index, b.end.k.index) for b in live.bis] == [
        (1, 5), (5, 15), (15, 19), (19, 23),
    ]
    assert live.completion_evidence == cold.completion_evidence
    assert live.continuation_blocked_at is None
