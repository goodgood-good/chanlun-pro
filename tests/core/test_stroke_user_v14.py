"""User-approved old-stroke decisions plus failures found by the full audit.

Sources read under D:/缠论: L062 37–118, L069 151–169, L077 139–148.
P-C/T0-E and the third-edge completion timing are user policy, not original
quotes. Full-span conflict with L066 256–280 remains explicitly documented.
"""
from datetime import datetime, timedelta
from fractions import Fraction
import json
from pathlib import Path
import pickle
import random

import pandas as pd
import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.types import Kline


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
CASES = json.loads((FIXTURES/'stroke_user_v14/user_choices.json').read_text(encoding='utf-8'))['cases']


def prices(name, mirror=False):
    result = [(Fraction(lo),Fraction(hi)) for lo,hi in CASES[name]]
    return reflect(result) if mirror else result


def reflect(values):
    return [(1000-hi,1000-lo) for lo,hi in values]


def bars(values):
    return [Kline(index=i,date=datetime(2026,1,5,9,30)+timedelta(minutes=i),
                  l=float(lo),h=float(hi),o=float(lo),c=float(hi),a=1.)
            for i,(lo,hi) in enumerate(values)]


def frame(values):
    return pd.DataFrame([dict(date=k.date,open=k.o,high=k.h,low=k.l,close=k.c,volume=k.a)
                         for k in bars(values)])


def cold(raw, closed_through='all'):
    merged,calc = CL_Kline_Process(),BiCalculator()
    merged.process_cl_klines(raw)
    calc.calculate_batch(merged.cl_klines,closed_through=closed_through)
    return merged,calc


def update(merged,calc,raw,closed_through='all'):
    merged.process_cl_klines(raw)
    calc.calculate(merged.cl_klines,source_revision=merged.structure_revision,
                   validated_incremental_prefix=True,closed_through=closed_through)


def geometry(bis):
    return [(b.start.k.index,b.end.k.index) for b in bis]


def state(calc):
    construction = calc.construction_state()
    construction.pop('processing_mode')
    return ([b.to_dict() for b in calc.bis],construction,
            calc.qualification_evidence,calc.completion_evidence,calc.continuation_evidence,
            calc.selection_revisions)


@pytest.mark.parametrize('mirror',[False,True])
@pytest.mark.parametrize('name,expected,done',[
    ('barrier',[(1,5),(5,15),(15,19)],[(1,5)]),
    ('restoration',[(1,18)],[]),
])
def test_user_selected_paths_replay_every_prefix(name,expected,done,mirror):
    raw = bars(prices(name,mirror))
    merged,live = CL_Kline_Process(),BiCalculator()
    for end in range(1,len(raw)+1):
        update(merged,live,raw[:end])
        _,batch = cold(raw[:end])
        assert state(live)==state(batch)
    assert geometry(live.bis)==expected
    assert geometry(live.confirmed_bis)==done
    assert not live.unresolved_regions
    assert len(live._resolver.nodes)==len(live.fxs)


@pytest.mark.parametrize('mirror',[False,True])
@pytest.mark.parametrize('relation',['above','equal','below'])
def test_third_qualified_edge_completes_first_and_two_conflicts_cannot_erase_it(relation,mirror):
    values = prices('completion_'+relation+'_extended')
    values += [(250,450),(350,550),(300,500)]
    if mirror:
        values=reflect(values)
    raw=bars(values)
    merged,live=CL_Kline_Process(),BiCalculator()
    finished=()
    for end in range(1,len(raw)+1):
        update(merged,live,raw[:end])
        _,batch=cold(raw[:end])
        assert state(live)==state(batch)
        if end==11:
            assert geometry(live.bis)==[(1,5),(5,9)]
            assert live.bis[0].has_qualified_successor()
            assert not live.bis[0].is_done()
        if end==15:
            finished=live.completion_evidence
            assert geometry(live.confirmed_bis)==[(1,5)]
            assert live.bis[0].locked_at==raw[14].date
            assert live.bis[0].completion_witness==(1,5,9,13)
            assert live.bis[0].completion_status=='completed'
        if end>=15:
            assert live.completion_evidence==finished
            assert geometry(live.confirmed_bis)==[(1,5)]
    assert geometry(live.bis)==[(1,5),(5,16)]
    assert live._resolver.last_decision.action=='await_completed_boundary'
    assert live.fxs[-1].k.index==19
    # Subsequent qualifying top/bottom resumes the same continuous tail.
    extra=[(350,550),(400,600),(450,650),(350,550)]
    if mirror:
        extra=reflect(extra)
    update(merged,live,bars(values+extra))
    assert geometry(live.bis)==[(1,5),(5,16),(16,23)]
    assert live.completion_evidence==finished


@pytest.mark.parametrize('mirror',[False,True])
def test_gap_three_cannot_complete_first_pen(mirror):
    values=prices('completion_above')
    values += [(310,510),(400,600),(350,550)]
    if mirror:
        values=reflect(values)
    _,calc=cold(bars(values))
    assert not calc.confirmed_bis
    assert geometry(calc.bis)==[(1,12)]


def test_price_reflection_has_identical_pair_qualification():
    values=[(100,106),(101,108),(100,106),(99,105),(103,107),(102,104),(103,105)]
    _,up=cold(bars(values))
    _,down=cold(bars(reflect(values)))
    assert geometry(up.bis)==geometry(down.bis)==[]


def test_intrabar_shape_cannot_commit_and_close_signal_works_without_new_prices():
    values=prices('completion_above_extended')[:15]
    data=frame(values)
    live=CL('TEST','1m',market='a').process_klines(data)
    assert geometry(live.get_bis())==[(1,5),(5,9),(9,13)]
    assert not live.bi_calculator.confirmed_bis
    revised=data.copy()
    revised.loc[14,'high']=610.
    live.process_klines(revised)
    assert not live.bi_calculator.confirmed_bis
    assert geometry(live.get_bis())==[(1,5),(5,9)]
    reference=CL('TEST','1m',market='a').process_klines_batch(revised,last_bar_closed=False)
    assert state(live.bi_calculator)==state(reference.bi_calculator)
    closed=CL('TEST','1m',market='a').process_klines(data)
    closed.process_klines(data.iloc[-1:],last_bar_closed=True)
    assert geometry(closed.bi_calculator.confirmed_bis)==[(1,5)]
    assert closed.get_bis()[0].completion_is_final is True
    confirmed=closed.bi_calculator.completion_evidence
    closed.process_klines(data.iloc[-1:])
    assert closed.bi_calculator.completion_evidence==confirmed


@pytest.mark.parametrize('bar_closed',[False,True])
def test_public_batch_and_incremental_use_the_same_close_contract(bar_closed):
    data=frame(prices('completion_above_extended'))
    live=CL('TEST','1m',market='a')
    for end,row in enumerate(data.itertuples(),1):
        live.process_kline_values(row.date,row.open,row.high,row.low,row.close,row.volume,bar_closed=bar_closed)
        batch=CL('TEST','1m',market='a').process_klines_batch(data.iloc[:end],last_bar_closed=bar_closed)
        assert state(live.bi_calculator)==state(batch.bi_calculator)
    restored=pickle.loads(pickle.dumps(live))
    assert state(restored.bi_calculator)==state(live.bi_calculator)


def test_history_revision_rebuilds_all_structures_instead_of_discarding_old_update():
    original=frame(prices('barrier'))
    revised=original.copy()
    revised.loc[5,'high']=570.
    for single in (False,True):
        live=CL('TEST','1m',market='a').process_klines_batch(original)
        if single:
            r=revised.iloc[5]
            live.process_kline_values(r.date,r.open,r.high,r.low,r.close,r.volume)
        else:
            live.process_klines(revised)
        batch=CL('TEST','1m',market='a').process_klines_batch(revised)
        assert live.get_src_klines()[5].h==570.
        assert state(live.bi_calculator)==state(batch.bi_calculator)


def test_auction_input_is_not_a_stroke_bar():
    data=frame(prices('barrier'))
    auction=data.iloc[0].to_dict()
    auction.update(date=pd.Timestamp('2026-01-05 09:25'),high=999.)
    live=CL('TEST','1m',market='a').process_klines_batch(pd.DataFrame([auction,*data.to_dict('records')]))
    batch=CL('TEST','1m',market='a').process_klines_batch(data)
    assert len(live.get_src_klines())==len(data)
    assert state(live.bi_calculator)==state(batch.bi_calculator)


@pytest.mark.parametrize('name',['SH.600519_5m','SH.600519_1m','SZ.002299_1m'])
def test_real_market_prefix_is_continuous_and_completed_history_is_monotone(name):
    data=pd.read_parquet(FIXTURES/'stroke_v13'/f'{name}.parquet')
    raw=[Kline(index=i,date=r.date.to_pydatetime(),h=r.high,l=r.low,o=r.open,c=r.close,a=r.volume)
         for i,r in enumerate(data.itertuples())]
    merged,live=CL_Kline_Process(),BiCalculator()
    finished=()
    for end in range(1,len(raw)+1):
        update(merged,live,raw[:end])
        assert live.completion_evidence[:len(finished)]==finished
        finished=live.completion_evidence
        assert len(live.stroke_components)<=1
        if end%127==0 or end==len(raw):
            _,batch=cold(raw[:end])
            assert state(live)==state(batch)
    assert live.confirmed_bis


def test_seeded_shape_reflection_and_completed_prefix_invariants():
    rng=random.Random(20260915)
    for _ in range(250):
        lo=400
        values=[]
        for _ in range(rng.randrange(20,81)):
            lo+=rng.choice((-30,-20,-10,10,20,30))
            values.append((lo,lo+200))
        raw=bars(values)
        merged,live=CL_Kline_Process(),BiCalculator()
        finished=()
        for end in range(1,len(raw)+1):
            update(merged,live,raw[:end])
            assert live.completion_evidence[:len(finished)]==finished
            finished=live.completion_evidence
        _,batch=cold(raw)
        _,mirror=cold(bars(reflect(values)))
        assert state(live)==state(batch)
        assert geometry(live.bis)==geometry(mirror.bis)
        assert geometry(live.confirmed_bis)==geometry(mirror.confirmed_bis)


@pytest.mark.parametrize('name',['SH.601059_1m','SH.600036_5m','SH.600519_5m'])
def test_saved_completed_prefix_counterexamples_preserve_their_confirmation(name):
    data=pd.read_csv(FIXTURES/'stroke_user_v14'/f'{name}_raw_prefix.csv',parse_dates=['date'])
    raw=[Kline(i,r.date.to_pydatetime(),r.high,r.low,r.open,r.close,r.volume)
         for i,r in enumerate(data.itertuples(index=False))]
    merged,live=CL_Kline_Process(),BiCalculator()
    completed=()
    for end in range(1,len(raw)+1):
        update(merged,live,raw[:end])
        assert live.completion_evidence[:len(completed)]==completed
        completed=live.completion_evidence
        _,batch=cold(raw[:end])
        assert state(live)==state(batch)
    assert completed
    assert live._resolver.rejected_paths
    assert live._resolver.last_decision.action=='await_completed_boundary'


def test_closed_history_can_resume_live_prices_without_redating_earlier_completion():
    data=frame(prices('completion_above_extended'))
    loaded=CL('TEST','1m',market='a').process_klines_batch(data.iloc[:15])
    loaded=pickle.loads(pickle.dumps(loaded))
    loaded.process_klines(data.iloc[15:])
    replay=CL('TEST','1m',market='a')
    for i,r in enumerate(data.itertuples(index=False)):
        replay.process_kline_values(r.date,r.open,r.high,r.low,r.close,r.volume,bar_closed=i<15)
    assert state(loaded.bi_calculator)==state(replay.bi_calculator)
    assert loaded.get_bis()[0].locked_at==data.iloc[14].date
    uniform=CL('TEST','1m',market='a').process_klines_batch(data,last_bar_closed=False)
    assert geometry(loaded.get_bis())==geometry(uniform.get_bis())
    assert uniform.get_bis()[0].locked_at==data.iloc[15].date
