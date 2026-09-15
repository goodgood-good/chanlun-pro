"""Source-condition regressions for the v10 audit, with explicit proof limits.

L062 37–100: fractals, independent K and directional inclusion.
L065 82–100: the non-included preceding K is required for direction.
L069 151–184 and 205–223: temporary selection, completed chart, end fractal.
L077 253–283: equal candidates and earliest remaining same-type endpoint.
All sources are in D:/缠论/chanlun_lesson_corpus. Fixtures are synthetic.
An earlier qualified alternative is not treated as a universal finality theorem.
"""

import copy
import random

import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from script.review_stroke_rule_logic import (
    COMPLETION_RETRACTION, EQUAL_PREFIX, INITIAL_INCLUSION, ORIGIN_WAIT,
    independent_definitions, long_origin_wait, path_conditions, reflected,
)
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate
from tests.core.test_stroke_endpoint_candidates import _live_prefixes, _signature


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("end_high", [13, 14, 16])
def test_equal_junction_stays_earliest_when_the_mutable_top_extends(end_high,mirror):
    values=EQUAL_PREFIX+[(11,7),(end_high-1,end_high-5),(end_high,end_high-4),(end_high-2,end_high-6)]
    raw=_bars(reflected(values,mirror))
    for count,calc in _live_prefixes(raw):
        if count==17:
            assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,5),(5,15)]
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,5),(5,19)]
    assert not calc.unresolved_endpoint_choices and not calc.confirmed_bis
    assert calc.bis[0].selected_at==raw[6].date
    assert calc.bis[1].selected_at==raw[20].date


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_selection_dependency_does_not_backdate_a_later_resolution(mirror):
    values=EQUAL_PREFIX+[(10,6),(9.5,5.5),(9,5),(10,6)]
    raw=_bars(values,mirror)
    for count,calc in _live_prefixes(raw):
        if count==17:
            assert not calc.unresolved_endpoint_choices and not calc.confirmed_bis
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,5),(5,15),(15,19)]
    assert len(calc.continuation_evidence)==2 and len(calc.completion_evidence)==1
    assert calc.bis[0].continuation_at==raw[16].date
    assert calc.bis[0].locked_at==raw[20].date
    assert calc.bis[0].selected_at==raw[6].date


@pytest.mark.parametrize("mirror", [False, True])
def test_a_retained_reverse_connection_settles_the_earlier_equal_junction(mirror):
    values=EQUAL_PREFIX+[(11,7),(10.5,6.5),(10,6),(11,7)]
    raw=_bars(values,mirror)
    for count,calc in _live_prefixes(raw):
        if count==17:
            assert not calc.unresolved_endpoint_choices and not calc.confirmed_bis
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,5),(5,15),(15,19)]
    assert len(calc.continuation_evidence)==2 and len(calc.completion_evidence)==1
    assert calc.bis[0].continuation_at==raw[16].date
    assert calc.bis[0].locked_at==raw[20].date
    assert calc.bis[0].selected_at==raw[6].date


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("extended", [False, True])
def test_later_candidates_progress_in_the_same_continuous_chain(extended,mirror):
    values=reflected(long_origin_wait() if extended else ORIGIN_WAIT,mirror)
    for _,calc in _live_prefixes(_bars(values)):pass
    assert len(calc.stroke_components)==1 and not calc.unresolved_regions
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis[:2]]==[(1,11),(11,19)]
    assert len(calc.bis)==(69 if extended else 5)
    assert all(not b.selection_pending for b in calc.bis)
    assert calc.bis[-1].end.k.index==len(values)-2


@pytest.mark.parametrize("mirror", [False, True])
def test_short_opposite_observations_do_not_create_an_unrelated_component(mirror):
    values=ORIGIN_WAIT[:15]+[(14,10),(16,12),(18,14),(20,16),(21,17),(20,16)]
    for _,calc in _live_prefixes(_bars(values,mirror)):pass
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis]==[(1,11),(11,19)]
    assert len(calc.stroke_components)==1 and not calc.unresolved_regions


@pytest.mark.parametrize("mirror", [False, True])
def test_current_successor_evidence_is_not_claimed_as_irreversible_completion(mirror):
    for count,calc in _live_prefixes(_bars(COMPLETION_RETRACTION,mirror)):
        if count==11:
            assert calc.bis[0].has_qualified_successor()
            assert calc.bis[0].completion_status=='continued'
            assert calc.bis[0].completion_is_final is False
    assert calc.selection_revisions[-1].action=='reselect_initial'
    assert not calc.confirmed_bis and not calc.unresolved_regions
    assert all(b.completion_is_final is False for b in calc.bis)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("context", [False, True])
def test_missing_initial_direction_uses_common_geometry_without_an_invented_default(context, mirror):
    prices = ([(108, 98)] if context else []) + INITIAL_INCLUSION
    merged, calc = _calculate(_bars(prices, mirror))
    assert len(calc.bis) == int(context)
    assert merged.initial_context_unresolved is not context
    if not context:
        assert merged.initial_excluded_raw_count == 2
        assert all(k.klines[0].index >= 2 for k in merged.cl_klines if not k.initial_context_alternatives)
        assert all(not f.k.initial_context_alternatives for f in calc.fxs)


@pytest.mark.parametrize("seed", [4, 11, 28, 49])
def test_initial_ambiguity_remains_symmetric_and_replays_during_intrabar_updates(seed):
    rng = random.Random(seed)
    prices, high = list(INITIAL_INCLUSION[:3]), 109.5
    for _ in range(45):
        high += rng.choice((-3, -2, -1, 1, 2, 3))
        prices.append((high, high - rng.choice((2, 4, 6))))
    raw = _bars(prices)
    merged, live = CL_Kline_Process(), BiCalculator()
    for end in range(1, len(raw) + 1):
        for stage in (0.0, 0.5, 1.0):
            prefix = copy.deepcopy(raw[:end])
            last = prefix[-1]
            middle = (last.h + last.l) / 2
            last.h, last.l = middle + (last.h - middle) * stage, middle + (last.l - middle) * stage
            last.o, last.c = last.l, last.h
            merged.process_cl_klines(prefix)
            live.calculate(merged.cl_klines, source_revision=merged.structure_revision,
                           validated_incremental_prefix=True)
            cold_merged, cold = _calculate(prefix)
            assert [live._kline_sig(k) for k in merged.cl_klines] == [cold._kline_sig(k) for k in cold_merged.cl_klines]
            assert _signature(live) == _signature(cold)
            assert live.continuation_evidence == cold.continuation_evidence
            mirror_prices = [(300 - k.l, 300 - k.h) for k in prefix]
            mirror_merged, mirrored = _calculate(_bars(mirror_prices))
            # Initial left context can have two genuinely different geometries;
            # compare that whole evidence set, not its display representative.
            assert [set((300 - c.l, 300 - c.h) for c in (k.initial_context_alternatives or (k,)))
                    for k in cold_merged.cl_klines] == [
                        set((c.h, c.l) for c in (k.initial_context_alternatives or (k,)))
                        for k in mirror_merged.cl_klines]
            assert [(b.start.k.k_index, b.end.k.k_index) for b in live.bis] == [(b.start.k.k_index, b.end.k.k_index) for b in mirrored.bis]


def test_end_fractal_and_old_distance_remain_independent_necessary_conditions():
    # A two-K swing is not a completed end fractal; six unmerged K are not
    # enough for two complete old-pen fractals with an independent K between.
    for values in ([(12, 8), (10, 6), (12, 8), (14, 10), (16, 12)],
                   [(12, 8), (10, 6), (12, 8), (14, 10), (16, 12), (14, 10)]):
        points, legal = independent_definitions(values)
        assert not any(legal(a, b) for i, a in enumerate(points) for b in points[i + 1:])
        _, calc = _calculate(_bars(values))
        assert not calc.bis and not calc.continuation_evidence
