"""Physical counterexamples for old-stroke endpoint selection and confirmation."""

import copy

import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.xd_calculator import XdCalculator
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate
from tests.core.test_stroke_range_audit import SECONDARY


SOURCE_69 = [(11, 7), (12, 8), (11, 7), (10, 6), (9, 5), (8, 4),
             (9, 5), (14, 10), (13, 9), (12, 8), (11, 7), (7, 3), (8, 4)]


def _signature(calc):
    return [(bi.start.k.index, bi.end.k.index, bi.start.k.k_index, bi.end.k.k_index,
             bi.start.val, bi.end.val, bi.is_done(), bi.forming, bi.locked_at)
            for bi in calc.bis]


def _live_prefixes(bars):
    merged, live = CL_Kline_Process(), BiCalculator()
    confirmed = {}
    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        merged.process_cl_klines(prefix)
        live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                       validated_incremental_prefix=True)
        _, cold = _calculate(prefix)
        assert _signature(live) == _signature(cold)
        assert live.qualification_evidence == cold.qualification_evidence
        assert live.completion_evidence == cold.completion_evidence
        assert live.continuation_evidence == cold.continuation_evidence
        assert live.unresolved_regions == cold.unresolved_regions
        assert live.selection_revisions == cold.selection_revisions
        assert len(live.qualification_evidence) == len(live.bis)
        assert all(e.fractal_visible_at <= e.selected_at <= bars[end - 1].date
                   for e in live.qualification_evidence)
        assert all(live._check_endpoint_geometry(b.start, b.end) for b in live.bis)
        assert len(live.stroke_components) <= 1
        now = {(bi.start.k.index, bi.end.k.index): (bi.start.val, bi.end.val, bi.locked_at)
               for bi in live.confirmed_bis}
        # Completion has a closed third-edge witness. Ordinary appends cannot
        # remove or redate it; short-conflict observations remain outside it.
        assert all(now.get(key) == record for key, record in confirmed.items())
        confirmed = now
        yield end, live


@pytest.mark.parametrize("mirror", [False, True])
def test_source69_near_break_does_not_lock_the_initial_candidate(mirror):
    for end, calc in _live_prefixes(_bars(SOURCE_69, mirror)):
        if end in (7, 9):
            assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(1, 5)]
            assert calc.confirmed_bis == []
        if end == len(SOURCE_69):
            assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(7, 11)]
            assert calc.confirmed_bis == []
            assert calc.continuation_blocked_at is None


@pytest.mark.parametrize("mirror", [False, True])
def test_short_lower_bottom_is_retained_as_an_alternative_start(mirror):
    for _, calc in _live_prefixes(_bars(SECONDARY, mirror)):
        pass
    assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(3, 9)]
    assert {fx.k.index for fx in calc.fxs} >= {1, 3, 4, 5, 9}
    assert not calc.bis[0].is_done()


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_extreme_keeps_the_earlier_endpoint(mirror):
    values = [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10),
              (13, 9), (12, 8), (13, 9), (14, 10), (13, 9)]
    for _, calc in _live_prefixes(_bars(values, mirror)):
        pass
    assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(1, 5)]


@pytest.mark.parametrize("mirror", [False, True])
def test_qualified_successor_observes_but_does_not_complete_first_stroke(mirror):
    values = [(12,8),(10,6),(11,7),(12,8),(13,9),(14,10),
              (13,9),(12,8),(11,7),(10,6),(11,7)]
    raw=_bars(values,mirror)
    _,calc=_calculate(raw)
    assert len(calc.bis)==2 and not calc.confirmed_bis
    assert calc.bis[0].has_qualified_successor()
    assert calc.bis[0].continuation_at==raw[10].date
    assert all(b.forming and not b.is_done() and b.locked_at is None for b in calc.bis)
    assert calc.pending_bi is calc.pending_bis[-1]


@pytest.mark.parametrize("mirror", [False, True])
def test_user_path_b_reconnects_despite_an_unselected_internal_higher_top(mirror):
    values=[(19,15),(20,16),(18,14),(16,12),(14,10),(12,8),
            (14,10),(16,12),(18,14),(22,18),(15,9),(10,6),
            (12,8),(14,10),(16,12),(21,17),(17,13),(14,10),
            (11,7),(9,5),(10,6)]
    for end,calc in _live_prefixes(_bars(values,mirror)):
        if end==13:
            assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,11)]
            assert not calc.confirmed_bis
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,11),(11,15),(15,19)]
    assert [(b.start.k.index,b.end.k.index) for b in calc.confirmed_bis]==[(1,11)]
    # The L066 diagnostic remains truthful about the policy's source discrepancy.
    assert calc.audit_endpoint_ranges()


def test_candidate_checkpoint_replays_when_a_newly_visible_fractal_disappears():
    initial = _bars(SOURCE_69[:9])
    merged, live = CL_Kline_Process(), BiCalculator()
    merged.process_cl_klines(initial)
    live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)
    rewritten = copy.deepcopy(initial)
    rewritten[-1].h, rewritten[-1].l = 15, 11
    rewritten[-1].o, rewritten[-1].c = 11, 15
    merged.process_cl_klines(rewritten)
    live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)
    _, cold = _calculate(rewritten)
    assert _signature(live) == _signature(cold)


def test_range_index_rebuilds_after_reset_and_history_shrink():
    original, calc = _calculate(_bars(SOURCE_69))
    calc.calculate([])
    assert calc.bis == calc.pending_bis == []
    assert calc.continuation_blocked_at is None
    calc.calculate(original.cl_klines[:7])
    _, cold = _calculate(_bars(SOURCE_69[:7]))
    assert _signature(calc) == _signature(cold)


@pytest.mark.parametrize("mirror", [False, True])
def test_uncompleted_initial_scope_can_reselect_with_source69_four_fractal_evidence(mirror):
    values=[(19,15),(20,16),(18,14),(16,12),(14,10),(12,8),
            (14,10),(16,12),(18,14),(22,18),(15,9),(10,6),
            (16,12),(24,20),(18,14),(14,10),(10,6),(8,4),(10,6)]
    raw=_bars(values,mirror)
    for end,calc in _live_prefixes(raw):
        if 15<=end<19:
            assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,11)]
            assert not calc.confirmed_bis
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(13,17)]
    assert calc.selection_revisions[-1].action=='reselect_initial'
    assert calc.selection_revisions[-1].observed_at==raw[18].date
    assert not calc.confirmed_bis and not calc.unresolved_regions
    assert {f.k.index for f in calc.fxs}>={1,5,9,11,13,17}


@pytest.mark.parametrize("mirror", [False, True])
def test_short_conflict_reselects_only_the_uncompleted_two_edge_tail(mirror):
    values=[(12,8),(10,6),(12,8),(14,10),(16,12),(20,16),
            (18,14),(17,13),(16,12),(14,10),(15,11),(16,12),
            (17,13),(18,14),(13,9),(8,4),(10,6)]
    raw=_bars(values,mirror)
    merged,live=CL_Kline_Process(),BiCalculator()
    merged.process_cl_klines(raw[:15])
    live.calculate(merged.cl_klines,source_revision=merged.structure_revision,validated_incremental_prefix=True)
    before=list(live.bis)
    assert [b.is_done() for b in before]==[True,False,False]
    finished=live.completion_evidence
    xd=XdCalculator()
    assert not xd.calculate(before)[0].is_done()
    merged.process_cl_klines(raw)
    live.calculate(merged.cl_klines,source_revision=merged.structure_revision,validated_incremental_prefix=True)
    assert [(b.start.k.index,b.end.k.index) for b in live.bis]==[(1,5),(5,15)]
    assert live.completion_evidence==finished
    assert live.bis[0] is before[0]
    assert xd.calculate(live.bis)==[]
    _,batch=_calculate(raw)
    assert _signature(live)==_signature(batch)
