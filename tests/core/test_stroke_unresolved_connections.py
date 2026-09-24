"""Preserve evidence when old-pen candidates cannot yet be connected.

Sources: D:/缠论/chanlun_lesson_corpus, L062 body 64–82 (adjacency and
independent K), L066 author reply 277–280 (endpoint extremes), L069 body
28–49 and 151–184 (initial reselection and unfinished choice). Components
are an engineering representation of missing proof, not a new pen rule.
All prices here are synthetic; tests do not assert a final global partition.
"""

import copy
import json

import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import adapt_lines
from chanlun.core.xd_calculator import XdCalculator
from script.review_stroke_rule_logic import (
    COMPLETION_RETRACTION, EQUAL_PREFIX, long_origin_wait, reflected,
)
from tests.core.strict_structure.real_history import strict_config
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate
from tests.core.test_bi_source_updates import _frame


def geometry(values):
    return [(b.start.k.index, b.end.k.index) for b in values]


def state(calc):
    # Processing mode is execution metadata, not a difference in source rules.
    construction = calc.construction_state()
    construction.pop("processing_mode")
    return (
        [(b.to_dict(), b.is_done()) for b in calc.bis], construction,
        calc.qualification_evidence, calc.continuation_evidence,
        calc.completion_evidence, calc.selection_revisions,
    )


def update(merged, calc, bars):
    merged.process_cl_klines(bars)
    calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("later_top", [28, 34])
@pytest.mark.parametrize("legs", [0, 8, 64])
def test_short_conflicts_preserve_a_continuous_progressing_chain(legs,later_top,mirror):
    values=long_origin_wait(legs)
    values[12:15]=[(later_top-2,8),(later_top,later_top-4),(later_top-1,12)]
    values=reflected(values,mirror)
    _,calc=_calculate(_bars(values))
    assert len(calc.stroke_components)==1 and not calc.unresolved_regions
    assert geometry(calc.bis[:2])==[(1,11),(11,19)]
    assert len(calc.bis)==5+legs
    assert calc.bis[-1].end.k.index==len(values)-2
    assert all(b.completion_is_final==b.is_done() for b in calc.bis)
    assert geometry(calc.contiguous_bis)==geometry(calc.bis)


@pytest.mark.parametrize("mirror", [False, True])
def test_later_extension_does_not_swallow_a_long_candidate_sequence(mirror):
    prices = long_origin_wait(64)
    prices[12:15] = [(26, 8), (28, 24), (27, 12)]
    bars = _bars(prices, mirror)
    merged, calc = CL_Kline_Process(), BiCalculator()
    update(merged, calc, bars)
    before = geometry(calc.stroke_components[-1])
    extended = _bars(prices + [(23, 19), (27, 23), (29, 25), (27, 23)], mirror)
    update(merged, calc, extended)
    assert geometry(calc.stroke_components[0][:2]) == [(1, 11), (11, 19)]
    assert geometry(calc.stroke_components[-1])[:len(before)] == before
    assert calc.bis[-1].start.k.index == before[-1][1]
    assert (11, len(prices) + 2) not in geometry(calc.bis)
    _, cold = _calculate(extended)
    assert state(calc) == state(cold)


@pytest.mark.parametrize("mirror", [False, True])
def test_uncompleted_initial_reselection_retains_facts_without_a_fake_boundary(mirror):
    raw=_bars(COMPLETION_RETRACTION,mirror)
    _,calc=_calculate(raw)
    assert geometry(calc.bis)==[(13,17)]
    assert not calc.completion_evidence and not calc.unresolved_regions
    assert all(not b.is_done() and b.locked_at is None for b in calc.bis)
    assert {f.k.index for f in calc.fxs}>={1,5,9,11,13,17}
    assert XdCalculator().calculate(calc.bis)==[]
    json.dumps(calc.construction_state())


@pytest.mark.parametrize("mirror", [False, True])
def test_later_qualified_successors_complete_the_reselected_continuous_pen(mirror):
    values=[(19,15),(20,16),(18,14),(16,12),(14,10),(12,8),(14,10),
            (16,12),(18,14),(22,18),(15,9),(10,6),(12,8),(14,10),
            (16,12),(21,17),(17,13),(14,10),(11,7),(9,5),(10,6)]
    raw=_bars(values,mirror)
    merged,calc=CL_Kline_Process(),BiCalculator()
    update(merged,calc,raw[:17])
    assert geometry(calc.bis)==[(1,11),(11,15)] and not calc.confirmed_bis
    update(merged,calc,raw)
    assert geometry(calc.bis)==[(1,11),(11,15),(15,19)]
    assert geometry(calc.confirmed_bis)==[(1,11)]
    assert not calc.unresolved_regions
    corrected=_bars(values[:-1]+[(8,4)],mirror)
    update(merged,calc,corrected)
    _,batch=_calculate(corrected)
    assert state(calc)==state(batch)


@pytest.mark.parametrize("mirror", [False, True])
def test_intrabar_reversal_restores_components_and_their_evidence(mirror):
    bars = _bars(COMPLETION_RETRACTION, mirror)
    corrected = _bars(COMPLETION_RETRACTION[:-1] + [(7, 3)], mirror)
    merged, calc = CL_Kline_Process(), BiCalculator()
    for values in (bars[:-1], bars, corrected, bars):
        update(merged, calc, copy.deepcopy(values))
        _, cold = _calculate(copy.deepcopy(values))
        assert state(calc) == state(cold)


@pytest.mark.parametrize("prices", [COMPLETION_RETRACTION, long_origin_wait(8), EQUAL_PREFIX])
@pytest.mark.parametrize("step", [1, 7])
def test_explicit_batch_and_live_processing_have_the_same_causal_result(prices, step):
    bars = _bars(prices)
    merged, calc = CL_Kline_Process(), BiCalculator()
    cuts = sorted({*range(step, len(bars) + 1, step), len(bars)})
    modes = set()
    for count in cuts:
        update(merged, calc, bars[:count])
        modes.add(calc.processing_mode)
        cold_merged, cold = _calculate(bars[:count])
        batch = BiCalculator()
        batch.calculate_batch(cold_merged.cl_klines)
        assert state(calc) == state(cold) == state(batch)
    assert "batch" in modes and "incremental" in modes
    calc.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                   validated_incremental_prefix=True)
    assert calc.processing_mode == "unchanged"
    prefix_merged, prefix = _calculate(bars[:7])
    calc.calculate_batch(prefix_merged.cl_klines)
    assert state(calc) == state(prefix)
    assert not calc.unresolved_regions


def test_full_cl_batch_resets_old_context_and_can_resume_live_updates():
    frame=_frame(COMPLETION_RETRACTION,False)
    frame['date']=frame.date.dt.tz_localize('UTC')
    live=CL('TST','1m',strict_config(),market='a').process_klines(frame)
    lock=live._strict_evidence_lock
    live.process_klines_batch(frame.head(11),last_bar_closed=False)
    assert live._strict_evidence_lock is lock
    live.process_klines(frame)
    batch=CL('TST','1m',strict_config(),market='a').process_klines_batch(frame,last_bar_closed=False)
    assert state(live.bi_calculator)==state(batch.bi_calculator)
    assert live.get_native_centers()==batch.get_native_centers()
    assert not live.get_stroke_construction_state()['unresolved_regions']
    units=adapt_lines(live.get_bis(),0,SourceKind.STROKE_OBSERVATION,'0.01',
                      frame.iloc[-1].date,live._strict_registry())
    assert len(units)==len(live.get_bis()) and all(not unit.locked for unit in units)
