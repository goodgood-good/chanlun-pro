from copy import deepcopy
from pathlib import Path
import threading
import time

import pandas as pd
import pytest

from cl_app.services import higher_context
from cl_app.services.native_period_link import PERIODS,SCHEMA,relate,summarize
from chanlun.screening import cache,runner


def signal(kind='3buy',identity='base',anchor=80,status='confirmed'):
    return {'point_id':identity,'point_type':kind,'side':'buy' if kind.endswith('buy') else 'sell',
            'status':status,'available_at':100,'anchor_at':anchor,'leg_start':60,
            'anchor_price':100.,
            'invalidation_price':90 if kind.endswith('buy') else 110}


def periods():
    return {p:{'schema':SCHEMA,'frequency':p,'source_closed_at':100,'input_fingerprint':'input',
        'price_basis_revision':'basis','latest_close':100.,'price_quantum':.01,'direction':'up','position':'inside',
        'center':{'id':p,'zd':95.,'zg':105.,'ended':False},'signals':[signal()]} for p in PERIODS}


def row(frequency='5m',kind='3buy'):
    return {'id':'candidate','code':'SH.600000','market':'a','frequency':frequency,'source_closed_at':100,
        'input_fingerprint':'input','point':{**signal(kind),'price_basis_revision':'basis'}}


def linked(value=None,data=None):
    return relate(value or row(),data or periods(),{p:100 for p in PERIODS})


@pytest.mark.parametrize('period',PERIODS)
def test_every_operating_period_uses_the_other_two_with_distinct_roles(period):
    value=linked(row(period))
    assert value['category']=='aligned' and value['quality']=='complete'
    assert len(value['periods'])==3
    assert [p['frequency'] for p in value['periods'] if p['role']=='background']==list(PERIODS[PERIODS.index(period)+1:])
    assert value['higher_reversal_confirmed'] is False


def test_middle_period_buy_cannot_overrule_opposing_30m_context():
    data=periods();data['30m'].update(direction='down',signals=[signal('3sell','large-sell')])
    assert linked(row('1m'),data)['category']=='countertrend'
    data['30m']['signals']=[signal('1buy','large-preview',status='approaching')]
    result=linked(row('1m'),data)
    assert result['category']=='turning' and result['higher_reversal_confirmed'] is False


@pytest.mark.parametrize('kind,expected', [('3buy','aligned'), ('3sell','countertrend')])
def test_short_signals_use_the_scanners_center_background_and_their_own_side(kind, expected):
    data = periods()
    data['1m']['signals'] = [signal(kind)]
    data['30m']['background_30m'] = {
        'frequency':'30m','state':'verified','source_closed_at':100,
        'direction':'up','alignment':'aligned','center_id':'c','price_basis_revision':'basis',
    }
    assert linked(row('1m',kind),data)['background_30m']['alignment'] == expected
    data['30m']['background_30m']['direction'] = 'range'
    assert linked(row('1m',kind),data)['background_30m']['alignment'] == 'range'
    # A separate departure in the linkage summary must not upgrade the background.
    assert data['30m']['direction'] == 'up'
    data['30m']['price_basis_revision'] = 'changed'
    assert linked(row('1m',kind),data)['background_30m']['alignment'] == 'unverified'


def test_short_period_reverse_signal_is_risk_context_and_does_not_relabel_the_main_point():
    data=periods();data['1m']['signals']=[signal('1sell','small-sell')]
    primary=row('30m');saved=deepcopy(primary)
    assert linked(primary,data)['category']=='mixed'
    assert primary==saved


def test_matching_point_labels_outside_the_main_leg_do_not_create_timing_support():
    data=periods()
    for period in ['1m','5m']:data[period]['signals']=[signal(anchor=50)]
    assert linked(row('30m'),data)['category']=='independent'


def test_second_point_timing_requires_a_matching_first_point_at_its_price_anchor():
    primary=row('30m','2buy');data=periods();data['30m']['signals']=[signal('2buy')]
    assert linked(primary,data)['category']=='independent'
    data['5m']['signals']=[signal('1buy','timing')]
    result=linked(primary,data)
    assert result['category']=='aligned' and result['timing_anchor_verified']
    data['5m']['signals'][0]['anchor_price']=101.
    assert linked(primary,data)['timing_anchor_verified'] is False
    assert linked(primary,data)['category']=='independent'


@pytest.mark.parametrize('fault',['missing','future','stale','basis','quote','primary_identity','primary_input'])
def test_incomplete_or_misaligned_evidence_never_becomes_alignment(fault):
    data=periods()
    if fault=='missing':data.pop('30m')
    elif fault=='future':data['30m']['source_closed_at']=101
    elif fault=='stale':data['30m']['source_closed_at']=99
    elif fault=='basis':data['30m']['price_basis_revision']='other'
    elif fault=='quote':data['30m']['latest_close']=110
    elif fault=='primary_identity':data['5m']['signals']=[]
    else:data['5m']['input_fingerprint']='other'
    assert linked(data=data)['category']=='unverified'


def test_new_lower_price_breaking_a_parent_reference_removes_support_before_parent_closes():
    data=periods()
    data['30m'].update(source_closed_at=90,latest_close=110.,signals=[signal('3buy','parent')])
    data['30m']['signals'][0].update(invalidation_price=105,available_at=90)
    result=relate(row(),data,{'1m':100,'5m':100,'30m':90})
    assert result['category']=='mixed'
    assert result['periods'][-1]['boundary_broken'] is True
    assert result['periods'][-1]['signal'] is None


def test_crossing_parent_core_is_only_a_reversal_candidate():
    data=periods();data['30m'].update(direction='down',signals=[])
    data['30m']['center'].update(zd=80,zg=90)
    result=linked(row('1m'),data)
    assert result['category']=='turning' and result['higher_reversal_confirmed'] is False


def test_response_does_not_expose_mutable_cached_period_facts():
    data=periods();before=deepcopy(data);response=linked(data=data)
    response['periods'][0]['center']['zd']=0
    response['periods'][0]['signal']['status']='broken'
    assert data==before


def test_unknown_large_structure_is_not_treated_as_support():
    data=periods();data['30m'].update(direction='unknown',center=None,signals=[])
    result=linked(data=data)
    assert result['category']=='unverified'
    assert result['periods'][-1]['reason']=='structure_insufficient'


def test_price_and_clock_conflicts_explain_why_verified_inputs_cannot_be_related():
    data=periods();data['1m']['latest_close']=100.01
    result=linked(data=data)
    assert result['periods'][0]['reason']=='period_close_mismatch'
    assert result['category']=='unverified'
    data['30m']['source_closed_at']=99
    assert linked(data=data)['periods'][-1]['reason']=='period_cutoff_mismatch'


def test_context_loading_is_shared_bounded_and_does_not_block_the_primary_result(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();calls=[];data=periods()
    def load(paths,market,code,period,cutoff,revision):
        calls.append(period);entered.set();assert release.wait(3)
        return data[period],tmp_path/'immutable-input'
    service=higher_context.PeriodContexts([tmp_path],loader=load)
    monkeypatch.setattr(higher_context,'expected_clocks',lambda *_:{p:100 for p in PERIODS})
    try:
        started=time.monotonic();first=service.context(row(),'revision')
        assert time.monotonic()-started<.25 and first['category']=='unverified'
        assert entered.wait(1)
        for _ in range(5):service.context(row(),'revision')
        assert len(service.inflight)==3
        release.set()
        until=time.monotonic()+3
        while service.inflight and time.monotonic()<until:time.sleep(.01)
        assert service.context(row(),'revision')['category']=='aligned'
        assert sorted(calls)==sorted(PERIODS)
    finally:
        release.set();service.close()


@pytest.fixture
def recorded_cache(tmp_path):
    frame=pd.read_parquet(Path('tests/fixtures/SH.600519_5m.parquet')).iloc[:5475].copy()
    frame.attrs.update(structure_price_quantum='0.01',price_basis_revision='recorded-raw',screening_market='a')
    snapshot=runner.snapshot_for(frame,'SH.600519','5m')
    revision=higher_context.COMPILED_REVISION
    path=tmp_path/'SH.600519_5m.json.gz'
    cache.write_calculation(path,cache.calculation_key(frame,'SH.600519','5m',revision),
        {'code':'SH.600519','frequency':'5m','data_errors':[],'reason_counts':{}},snapshot,{},
        source_frame=frame,calculation_revision=revision)
    return path,frame,snapshot,revision


def test_cached_native_context_uses_verified_inputs_without_requesting_quotes(recorded_cache,monkeypatch):
    path,frame,snapshot,revision=recorded_cache
    monkeypatch.setattr(runner,'fetch_frame',lambda *_a,**_k:pytest.fail('context must reuse existing calculations'))
    value,_=higher_context.load_period([path],'a','SH.600519','5m',snapshot['source_closed_at'],revision)
    assert value['signals'] and value['source_closed_at']==snapshot['source_closed_at']
    assert value['input_fingerprint']==cache.input_fingerprint(frame,'SH.600519','5m')


@pytest.mark.parametrize('suffix,accepted', [(':native-strength-v2', True),
    (':holding-consolidation-v1', True), (':unknown-strength-version', False)])
def test_context_recognizes_only_explicitly_supported_calculation_modes(recorded_cache, suffix, accepted):
    path,frame,snapshot,revision=recorded_cache
    mode=revision+suffix
    cache.write_calculation(path,cache.calculation_key(frame,'SH.600519','5m',mode),
        {'code':'SH.600519','frequency':'5m','data_errors':[],'reason_counts':{}},snapshot,{},
        source_frame=frame,calculation_revision=mode)
    if not accepted:
        with pytest.raises(ValueError):
            higher_context.load_period([path],'a','SH.600519','5m',snapshot['source_closed_at'],revision)
    else:
        value,_=higher_context.load_period([path],'a','SH.600519','5m',snapshot['source_closed_at'],revision)
        assert value['signals'] and value['input_fingerprint']==cache.input_fingerprint(frame,'SH.600519','5m')


def test_a_recovered_close_does_not_restore_a_previously_broken_reference(recorded_cache):
    _,frame,snapshot,_=recorded_cache
    original=summarize(snapshot,float(frame.close.iloc[-1]),input_fingerprint='input',frame=frame)
    point=next(p for p in original['signals'] if p['status']=='approaching')
    revised=frame.copy();after=[i for i,value in enumerate(frame.date) if int(value.timestamp())>point['anchor_at']]
    assert after
    column='low' if point['side']=='buy' else 'high'
    revised.iloc[after[0],revised.columns.get_loc(column)]=point['invalidation_price']+(-1 if point['side']=='buy' else 1)
    current=summarize(snapshot,float(revised.close.iloc[-1]),input_fingerprint='revised',frame=revised)
    assert point['point_id'] not in {p['point_id'] for p in current['signals']}


def test_newer_cached_candles_are_truncated_before_historical_context_rebuild(recorded_cache,monkeypatch):
    path,frame,_,revision=recorded_cache
    cutoff=int(frame.date.iloc[-2].timestamp());seen=[]
    original=higher_context.snapshot_for
    def build(part,*args,**kwargs):
        seen.append(int(part.date.iloc[-1].timestamp()))
        assert all(int(stamp.timestamp())<=cutoff for stamp in part.date)
        return original(part,*args,**kwargs)
    monkeypatch.setattr(higher_context,'snapshot_for',build)
    value,_=higher_context.load_period([path],'a','SH.600519','5m',cutoff,revision)
    assert value['source_closed_at']==cutoff and seen==[cutoff]


def test_wrong_calculation_revision_is_not_a_valid_context(recorded_cache):
    path,_,snapshot,_=recorded_cache
    with pytest.raises(ValueError):
        higher_context.load_period([path],'a','SH.600519','5m',snapshot['source_closed_at'],'other')


def test_later_appended_candles_do_not_rebuild_an_already_verified_historical_prefix(tmp_path):
    original=tmp_path/'immutable';original.write_bytes(b'input')
    calls=[]
    def load(*_args):
        calls.append(1)
        return {**periods()['5m'],'_calculated_through':200},original
    service=higher_context.PeriodContexts([tmp_path],loader=load)
    key=('revision','a','SH.600000','5m',100)
    def settle():
        until=time.monotonic()+2
        while service.inflight and time.monotonic()<until:time.sleep(.01)
        assert not service.inflight
    try:
        service.get(key);settle()
        (tmp_path/'SH.600000_5m.json.gz').write_bytes(b'a later current calculation')
        service.entries[key]['checked']-=6
        service.get(key);settle()
        assert calls==[1]
        assert service.get(key)['source_closed_at']==100
    finally:service.close()


def test_linkage_detail_cannot_overflow_a_batch_or_drop_its_primary_signals():
    from cl_app.services.notification_text import signal_content
    from tests.web.test_dingtalk_notifications import event
    rows=[{**event('confirmed',identity=str(i)),'code':'FIX'+str(i),
        'higher_context':{'schema':SCHEMA,'label':'联动支持','periods':[
            {'frequency':'30m','role':'background','label':'中枢上移'*100}], 'notes':['辅助证据'*300]}} for i in range(5)]
    content=signal_content(rows,1000)
    assert len(content.encode('utf-8'))<4000
    assert all('FIX'+str(i) in content for i in range(5))
    assert content.count('周期联动：联动支持')==5
    assert rows[0]['higher_context']['periods'], 'formatting mutated the evidence payload'
