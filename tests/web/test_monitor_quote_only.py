from copy import deepcopy

import pytest

from cl_app.services import signal_monitor
from cl_app.services.monitor_exclusions import quote_only_symbols, ENVIRONMENT_KEY
from cl_app.services.monitor_scope import independent_monitor_periods
from tests.web.test_signal_monitor import setup as setup


QUOTE_ONLY = ['ny_futures:CO.GC00W','ny_futures:NY.CL00W','fx:FE.CNYUSD','fx:FE.USDCNY']


def test_quote_only_members_keep_watchlist_data_but_leave_the_monitor_pool_and_diagnostics(setup, monkeypatch):
    service, _, live, _ = setup
    watch = [{'market':key.split(':')[0],'code':key.split(':')[1],'name':key,'origin':'watchlist'}
             for key in [*QUOTE_ONLY,'a:SH.600001']]
    before = deepcopy(watch)
    monkeypatch.setenv(ENVIRONMENT_KEY, ','.join(QUOTE_ONLY))
    monkeypatch.setattr(signal_monitor,'watchlist_symbols',lambda:watch)
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[])
    state = service._load()
    state['errors'] = [{**row,'reasons':['SESSION_CALENDAR_UNAVAILABLE']} for row in watch[:-1]]
    state['schedule_errors'] = deepcopy(state['errors'])
    service._sync_watchlist()
    assert watch == before
    assert state['seed']['codes'] == ['a:SH.600001']
    assert state['quote_only_symbols'] == sorted(QUOTE_ONLY)
    assert not state['errors'] and not state['schedule_errors']
    for key in QUOTE_ONLY:
        assert independent_monitor_periods(state,key) == set()
    service.tick()
    assert all(symbol['market'] not in {'fx','ny_futures'}
               for run in live.started for symbol in run['symbols'])


@pytest.mark.parametrize('frequency',['1m','5m'])
def test_a_queued_quote_only_notice_cannot_send_even_with_stale_membership(setup, monkeypatch, frequency):
    service, _, _, _ = setup
    monkeypatch.setenv(ENVIRONMENT_KEY,QUOTE_ONLY[0])
    monkeypatch.setattr(signal_monitor,'notification_is_live',lambda *args:True)
    state=service._load()
    state['seed']={'codes':[QUOTE_ONLY[0]],'settings':{'frequencies':['5m'],'point_types':['1buy']}}
    event={'id':'old','market':'ny_futures','code':'CO.GC00W','frequency':frequency,
           'point_type':'1buy','stage':'confirmed','recorded_at':1}
    valid,pending=service._validate_notification_state([event],'current',True,set(),{})
    assert not valid and not pending
    assert valid.rejections[0]['reason']=='OUTSIDE_MONITOR_SCOPE'


def test_quote_only_outbox_validation_does_not_start_any_dependency_read(setup,monkeypatch):
    service,_,_,_=setup
    monkeypatch.setenv(ENVIRONMENT_KEY,QUOTE_ONLY[0])
    monkeypatch.setattr(service._notification_dependency_reads,'read',lambda *a,**k:pytest.fail('quote-only'))
    event={'id':'old','market':'ny_futures','code':'CO.GC00W','frequency':'1m',
           'point_type':'1buy','stage':'confirmed','recorded_at':1}
    valid,pending=service._validate_notification_events([event])
    assert not valid and not pending
    assert valid.rejections[0]['reason']=='OUTSIDE_MONITOR_SCOPE'


def test_quote_only_configuration_rejects_bad_symbols_and_tracks_explicit_changes(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY,','.join(QUOTE_ONLY))
    assert quote_only_symbols()==frozenset(QUOTE_ONLY)
    monkeypatch.setenv(ENVIRONMENT_KEY,'unknown:bad')
    with pytest.raises(ValueError):
        quote_only_symbols()
    monkeypatch.setenv(ENVIRONMENT_KEY,'')
    assert not quote_only_symbols()


def test_an_entire_quote_only_watchlist_does_not_create_a_realtime_pool(setup,monkeypatch):
    service,_,_,_=setup
    monkeypatch.setenv(ENVIRONMENT_KEY,QUOTE_ONLY[0])
    watch=[{'market':'ny_futures','code':'CO.GC00W','name':'Gold'}]
    monkeypatch.setattr(signal_monitor,'watchlist_symbols',lambda:watch)
    monkeypatch.setattr(signal_monitor,'holding_symbols',lambda:[])
    service._sync_watchlist()
    state=service._load()
    assert not state.get('seed') or not state['seed'].get('codes')
    assert state['quote_only_symbols']==[QUOTE_ONLY[0]]
