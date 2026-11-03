"""Unconfirmed points survive real frozen evidence and the notification path."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun.core.strict_structure.native_point_preview import PREVIEW_RULE, is_point_preview
from chanlun.screening import runner, rules
from cl_app.services import signal_monitor
from cl_app.services.screening import ScreeningManager
from cl_app.services.monitor_incremental import audited_members
from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_status_key
from cl_app.services.notification_charts import load_chart_evidence
from cl_app.services.notification_text import signal_content
from tests.web.test_signal_monitor import setup as setup, signal


@pytest.mark.parametrize('fixture,code,period,length,gain',[
    ('tests/fixtures/native_30m/SH.600183_30m.parquet','SH.600183','30m',1415,100),
    ('tests/fixtures/SH.600519_5m.parquet','SH.600519','5m',5475,10),
])
def test_recorded_unconfirmed_point_reaches_selection_pool_and_illustrated_notification(tmp_path,monkeypatch,fixture,code,period,length,gain):
    frame=pd.read_parquet(Path(fixture)).iloc[:length].copy()
    frame.attrs.update(structure_price_quantum='0.01',price_basis_revision='recorded-raw')
    context=rules.trading_context(frame.date.iloc[-1]+timedelta(minutes=1),period,20,60)
    monkeypatch.setattr(runner,'fetch_frame',lambda *args,**kw:frame.copy())
    directory=tmp_path/('a'*32);directory.mkdir()
    calculated=runner.scan_symbol({'market':'a','code':code,'name':'recorded'},
        {'frequencies':[period],'point_types':list(rules.POINT_TYPES),'strategy':runner.DIRECT_STRATEGY,
         'max_anchor_gain_pct':gain}, {period:context}, directory)
    row=calculated['rows'][0]
    assert not row['data_errors'] and not row['selected']
    candidate,=row['observations']
    assert is_point_preview(candidate['point']) and candidate['audit']=={'cold_rebuild':True,'closed_bar_replay':True}
    assert row['point_preview_checked'] is True
    (directory/'results.jsonl').write_text(json.dumps(calculated)+'\n',encoding='utf-8')
    saved=ScreeningManager(tmp_path)._read_results(directory,{'completed':1,'settings':{'strategy':'direct_structural'}})
    assert not saved['errors'] and not saved['selected']
    preview,=saved['observations']
    assert preview['evidence_verified'] is True and preview['exit_plan']['stop_loss']['status']=='provisional'
    members,periods=audited_members(saved)
    assert members['a:'+code]['code']==code and periods['a:'+code]==[period]
    event={**signal_monitor._signal(preview),'change':'discovered','recorded_at':context['cutoff']+1,
           'evidence_run_id':directory.name,'run_id':directory.name,'first_warned_at':context['cutoff']+1}
    _,snapshot,marker=load_chart_evidence(event,tmp_path)
    assert marker['status']=='approaching' and marker['confirmed_at'] is None
    text=signal_content([event],event['recorded_at'])
    assert '提前预警（未确认）' in text and '预警失效参考' in text and '确认时间：' not in text
    monkeypatch.setattr('cl_app.services.notification_charts.public_base',lambda:'https://fixture.invalid')
    box=DingTalkOutbox(tmp_path/'box.sqlite',now=lambda:event['recorded_at'],charts=True)
    box.charts.enqueue([event],event['recorded_at'])
    assert box.charts.snapshot()['pending']==1
    broken=deepcopy(event);broken['stage']='confirmed';broken['signal_type']='signal'
    with pytest.raises(ValueError,match='does not match'):
        load_chart_evidence(broken,tmp_path)


def _monitor_preview(setup,monkeypatch,point_type='3buy',new_member=False,stop=None):
    service,manual,live,clock=setup
    held={'market':'a','code':'SH.600088','name':'held','origin':'watchlist'}
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[held])
    box=Mock();box.snapshot.return_value={};service._notifications=box
    service._load().update(dingtalk_enabled=True,notification_since=0)
    service.tick()
    state=service._load();state['holding_notification_since']['a:SH.600088']=0
    state.setdefault('notification_symbol_since',{})['a:SH.600088']=clock[0].timestamp() if new_member else 0
    cutoff=rules.trading_context(clock[0],'5m',1,1)['cutoff']
    original=live.committed_results
    live.preview_checked=True
    def committed(*args,**kwargs):
        value=original(*args,**kwargs)
        for period in value['verified_periods']:
            period['point_preview_checked']=live.preview_checked
        return value
    live.committed_results=committed
    row=signal(stage='approaching')
    row['source_closed_at']=cutoff
    if stop is not None:
        row['exit_plan']={'stop_loss':{'price':stop,'status':'provisional','trigger':'lt'},'take_profit':{'status':'waiting'}}
        row['point']['anchor_price']=stop
    row['point'].update(point_id='preview',point_type=point_type,side='sell' if point_type.endswith('sell') else 'buy',
        rule_basis=PREVIEW_RULE,evidence_scope='native_center_signal',formal_point_issued=False,confirmed_at=None,
        missing_conditions=['unfinished_segment_lock'],preview_key='same-local-structure',available_at=cutoff)
    if point_type == '3buy':
        from tests.web.test_first_center_monitor import first_proof
        row['point']['center_id'] = 'center'
        row['center_sequence'] = first_proof(cutoff)
    def finish(rows,*,checked=True,errors=()):
        if live.state.get('status') not in {'running','starting'}:
            service.check_now();service.tick()
        live.preview_checked=checked;live.rows=rows;live.errors=list(errors)
        live.finish(cutoff);service.tick()
    finish([row])
    return service,live,row,finish


def test_preview_updates_once_then_confirmation_links_to_the_original_warning(setup,monkeypatch):
    service,live,row,finish=_monitor_preview(setup,monkeypatch)
    events=lambda:[event for event in service._load()['events'] if event.get('preview_key')=='same-local-structure']
    first,=events()
    assert first['stage']=='approaching' and first.get('external_notifications',True)
    assert service._validate_notification_events([first])[0]
    moved=deepcopy(row);moved['point']['anchor_price']=11.9
    finish([moved]);assert len(events())==1
    confirmed=deepcopy(row)
    confirmed['point'].update(point_id='formal',status='confirmed',rule_basis='user_declared_native_center_macd_v6',
        formal_point_issued=True,confirmed_at=row['source_closed_at'],missing_conditions=[])
    finish([confirmed])
    assert [event['stage'] for event in events()]==['confirmed','approaching']
    assert events()[0]['first_warned_at']==first['first_warned_at']
    assert events()[0]['confirmed_preview_id']==first['id']
    assert service._validate_notification_events([first])==([],[])
    assert service._validate_notification_events([events()[0]])[0]


def test_unknown_structure_waits_then_verified_invalidation_and_reactivation_are_deduplicated(setup,monkeypatch):
    service,live,row,finish=_monitor_preview(setup,monkeypatch)
    events=lambda:[event for event in service._load()['events'] if event.get('signal_type')=='native_point_preview']
    first=events()[0]
    finish([],checked=False)
    assert len(events())==1 and first['id'] in service._load()['unverified_signals']
    assert service._validate_notification_events([first])==([],[first])
    from tests.web.warning_helpers import refute_warning
    refute_warning(monkeypatch)
    finish([],checked=True)
    expired=events()[0]
    assert expired['stage']=='invalidated' and service._validate_notification_events([expired])[0]
    finish([],checked=True);assert len(events())==2
    finish([row]);restored=events()[0]
    assert restored['stage']=='approaching' and restored['lifecycle_cycle']==1
    assert signal_status_key(restored)!=signal_status_key(first)


def test_incomplete_preview_check_retries_same_cutoff_with_bounded_attempts(setup, monkeypatch):
    from datetime import timedelta
    service, live, row, finish = _monitor_preview(setup, monkeypatch)
    clock = setup[3]
    monkeypatch.setattr(signal_monitor, 'closed_bar_cutoff', lambda *args: row['source_closed_at'])
    monkeypatch.setattr(service, '_schedule_daily', lambda *args: None)
    finish([], checked=False)
    key = 'a:SH.600088'
    for failures in range(1, signal_monitor.MONITOR_MAX_FAILURES + 1):
        retry = signal_monitor._period_retries(service._load()['retry_symbols'].get(key, {})).get('5m')
        assert retry is not None and retry['failures'] == failures
        started = len(live.started)
        service.tick()
        assert len(live.started) == started
        if retry['retry_at'] is None:
            clock[0] += timedelta(minutes=5)
            service.tick()
            assert len(live.started) == started
        else:
            clock[0] += timedelta(seconds=retry['retry_at'] - clock[0].timestamp() + 1)
            service.tick()
            assert len(live.started) == started + 1
            finish([], checked=False)
    assert service._load()['unverified_signals']
    finish([row])  # An explicit retry may recover after the bounded attempts.
    assert key not in service._load()['retry_symbols']


def test_old_third_buy_preview_can_be_invalidated_without_first_center_admission(setup,monkeypatch):
    from tests.web.warning_helpers import refute_warning
    refute_warning(monkeypatch)
    service, _live, _row, finish = _monitor_preview(setup, monkeypatch)
    first = next(e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview')
    service._load()['signals'][first['id']].pop('center_sequence')
    finish([], checked=True)
    expired = next(e for e in service._load()['events'] if e.get('stage') == 'invalidated')
    valid, pending = service._validate_notification_events([expired])
    assert len(valid) == 1 and not pending
    assert not valid[0].get('center_sequence')


@pytest.mark.parametrize('point_type',['1sell','2sell','3sell'])
def test_nonholding_sell_previews_remain_visible_without_delivery(setup,monkeypatch,point_type):
    service,_,_,_=_monitor_preview(setup,monkeypatch,point_type)
    event=next(event for event in service._load()['events'] if event.get('signal_type')=='native_point_preview')
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[])
    assert any(row['point_type'] == point_type for row in service._load()['signals'].values())
    for frequency in ('1m', '5m', '30m'):
        assert service._validate_notification_events([{**event, 'frequency': frequency}]) == ([], [])


def test_newly_admitted_preview_can_warn_on_current_bar_without_waiting_for_another_bar(setup,monkeypatch):
    service,_,_,_=_monitor_preview(setup,monkeypatch,new_member=True)
    event=next(event for event in service._load()['events'] if event.get('signal_type')=='native_point_preview')
    assert event['available_at']<service._load()['notification_symbol_since']['a:SH.600088']
    assert event.get('external_notifications',True)
    assert service._validate_notification_events([event])[0]


def test_a_new_extreme_updates_candidate_without_a_new_warning_cycle(setup,monkeypatch):
    service,_,row,finish=_monitor_preview(setup,monkeypatch,point_type='1buy',stop=12.3)
    first=next(e for e in service._load()['events'] if e.get('signal_type')=='native_point_preview')
    moved=deepcopy(row);moved['point']['anchor_price']=12.1;moved['exit_plan']['stop_loss']['price']=12.1
    finish([moved])
    events=[e for e in service._load()['events'] if e.get('signal_type')=='native_point_preview']
    current=service._load()['signals'][first['id']]
    assert len(events)==1 and current['candidate_updated'] is True and current['lifecycle_cycle']==0
    assert signal_status_key(current)==signal_status_key(first)
    assert current['first_warned_at']==first['first_warned_at']
    assert '候选拐点更新（未确认）' in signal_content([current],first['recorded_at'])
    finish([moved])
    assert len([e for e in service._load()['events'] if e.get('signal_type')=='native_point_preview'])==1


def test_warning_recheck_runs_outside_state_lock_and_continues_without_another_notice(setup,monkeypatch):
    service, _, row, finish = _monitor_preview(setup,monkeypatch)
    first = next(e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview')
    calls = []
    def recheck(previous, period, run, root, cache):
        assert not service._lock._is_owned()
        calls.append(run)
        outcome = {'state':'observing', 'reason_code':'STRUCTURE_OBSERVING'}
        return outcome, {**previous, 'source_closed_at':period['source_closed_at'], 'preview_trigger_active':False}
    monkeypatch.setattr('cl_app.services.warning_lifecycle.recheck_warning', recheck)
    finish([], checked=True)
    assert calls and service._load()['signals'][first['id']]['preview_trigger_active'] is False
    assert len([e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview']) == 1
    finish([row])
    assert service._load()['signals'][first['id']]['first_warned_at'] == first['first_warned_at']
    assert len([e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview']) == 1


def test_warning_changed_during_evidence_read_does_not_accept_the_stale_assessment(setup,monkeypatch):
    service, _, _, finish = _monitor_preview(setup,monkeypatch)
    first = next(e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview')
    def recheck(previous, period, run, root, cache):
        with service._lock:
            service._load()['signals'][previous['id']]['anchor_price'] = 99.
        return {'state':'invalidated','reason_code':'STRUCTURE_REPLACED','reason':'changed'}, None
    monkeypatch.setattr('cl_app.services.warning_lifecycle.recheck_warning', recheck)
    finish([], checked=True)
    assert first['id'] in service._load()['unverified_signals']
    assert not any(e.get('stage') == 'invalidated' for e in service._load()['events'])


def test_structurally_confirmed_preview_keeps_link_until_later_selection_confirmation(setup,monkeypatch):
    from chanlun.core.strict_structure.identity import stable_structure_id
    service, _, row, finish = _monitor_preview(setup,monkeypatch)
    # Use the real deterministic preview identity; the basic fixture uses a
    # short stand-in identity for unrelated scheduling tests.
    old = next(e for e in service._load()['events'] if e.get('signal_type') == 'native_point_preview')
    row['point']['point_id'] = stable_structure_id(PREVIEW_RULE, row['point']['preview_key'])
    identity = signal_monitor._signal_identity(row)
    state = service._load()
    state['signals'][identity] = {**state['signals'].pop(old['id']), 'id':identity, 'point_id':row['point']['point_id']}
    state['known'][identity] = state['known'].pop(old['id'])
    monkeypatch.setattr('cl_app.services.warning_lifecycle.recheck_warning',
        lambda *_: ({'state':'confirmed','reason_code':'CONFIRMED'}, None))
    finish([], checked=True)
    assert identity not in service._load()['signals']
    confirmed = deepcopy(row)
    confirmed['point'].update(point_id='formal', status='confirmed', rule_basis='user_declared_native_center_macd_v6',
        formal_point_issued=True, confirmed_at=row['source_closed_at'], missing_conditions=[])
    finish([confirmed])
    event = next(e for e in service._load()['events'] if e.get('stage') == 'confirmed')
    assert event['confirmed_preview_id'] == identity and event['first_warned_at'] == old['first_warned_at']


def test_unchanged_closed_input_reuses_preview_review_but_a_new_bar_is_reviewed_again(tmp_path,monkeypatch):
    original=pd.read_parquet(Path('tests/fixtures/native_30m/SH.600183_30m.parquet'))
    original.attrs.update(structure_price_quantum='0.01',price_basis_revision='recorded-raw')
    frames=[original.iloc[:1415].copy()]
    monkeypatch.setattr(runner,'fetch_frame',lambda *args,**kw:frames[0].copy())
    reviewed=[]
    review=runner._review_native_center_signals
    def counted(snapshot,frame,candidates,*args):
        if candidates:
            reviewed.append(snapshot['source_closed_at'])
        return review(snapshot,frame,candidates,*args)
    monkeypatch.setattr(runner,'_review_native_center_signals',counted)
    def compute(name):
        context=rules.trading_context(frames[0].date.iloc[-1]+timedelta(minutes=1),'30m',20,60)
        return runner.scan_symbol({'market':'a','code':'SH.600183','name':'recorded'},
            {'frequencies':['30m'],'point_types':list(rules.POINT_TYPES),'strategy':runner.DIRECT_STRATEGY,
             'max_anchor_gain_pct':100},{'30m':context},tmp_path/name,cache_root=tmp_path/'cache',revision='preview-tests')['rows'][0]
    first=compute('first');second=compute('second')
    assert len(first['observations'])==len(second['observations'])==1 and len(reviewed)==1
    assert second['calculation_reused'] and second['replays_reused']==1
    frames[0]=original.iloc[:1416].copy()
    third=compute('third')
    assert len(third['observations'])==1 and len(reviewed)==2
    assert third['replays_reused']==0
