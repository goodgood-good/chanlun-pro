"""Persistent background preparation is separate from the delivered proof."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange.exchange_usmart import _timestamp_to_api_time
from chanlun.persistence.file_lock import interprocess_file_lock
from cl_app.services import notification_periods as periods
from cl_app.services.notification_history import HistoryArchive
from tests.web.test_notification_history import source_frame, Pages


@pytest.fixture(autouse=True)
def native(monkeypatch):
    monkeypatch.setattr(config,'EXCHANGE_US','usmart')
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')


def candles(start='2026-09-08', end='2026-10-09', frequency='30m'):
    import exchange_calendars as xcals
    calendar=xcals.get_calendar('XNYS',start=start,end=end)
    stamps=[]
    for row in calendar.schedule.loc[start:end].itertuples():
        stamps.extend(pd.date_range(row.open+pd.Timedelta(frequency.replace('m','min')),
                                    row.close,freq=frequency.replace('m','min')))
    return source_frame(stamps,frequency)


def page(frame,frequency='30m'):
    return {'market':'us','symbol':'NVDA','type':{'1m':1,'5m':2,'30m':5}[frequency],'right':1,
            'list':[{'latestTime':_timestamp_to_api_time(row.date.tz_convert('US/Eastern')),
                     **{key:float(getattr(row,key)) for key in ('open','high','low','close','volume')}}
                    for row in frame.itertuples()]}


def inputs(tmp_path):
    frame=candles()
    anchor=frame.tail(220).reset_index(drop=True)
    event={'id':'immutable-event','evidence_run_id':'e'*32,'market':'us','code':'NVDA.US',
           'frequency':'1m','source_closed_at':int(frame.date.max().timestamp())}
    return event,tmp_path/'runs',anchor,frame


def call(inp, **kwargs):
    event,runs,anchor,_=inp
    return periods.background_evidence(event,runs,'30m',anchor.attrs,**kwargs)


def test_slices_persist_anchor_and_atomic_state_then_cache_verified_display(tmp_path,monkeypatch):
    inp=inputs(tmp_path)
    event,runs,anchor,frame=inp
    frozen=deepcopy(anchor)
    pages=Pages(page(frame.tail(300)),page(frame.iloc[:-300]),page(frame.iloc[:0]))
    with pytest.raises(periods.HistoryPending) as initial:
        call(inp,anchor_frame=anchor,fetch=False,page_source=pages)
    assert initial.value.coverage['pages']==0 and not pages.calls
    scope=initial.value.coverage['scope']
    archive=HistoryArchive(runs.parent/'notification_history',scope)
    saved_anchor,_,_,_=archive.read()
    pd.testing.assert_frame_equal(saved_anchor,anchor)
    # Later readers cannot replace the initial anchor with revised source data.
    replacement=anchor.copy(deep=True)
    replacement.loc[0,'close']=11.5
    tokens=[]
    for count in (1,2):
        with pytest.raises(periods.HistoryPending) as pending:
            call(inp,anchor_frame=replacement,page_source=pages,max_pages=1)
        assert pending.value.coverage['pages']==count
        tokens.append(pending.value.coverage['progress_token'])
    assert len(set(tokens))==2
    returned,snapshot,coverage=call(inp,page_source=pages,max_pages=1)
    assert coverage['state']=='source_exhausted' and coverage['source_origin']
    assert not coverage['target_reached'] and coverage['scope']==scope
    assert snapshot['analysis_scope']=='native_centers' and snapshot['display_scope']=='notification_background'
    assert all(not level['points'] and not level['divergences'] for level in snapshot['levels'])
    assert len(snapshot['chart']['higher_macd_hist'])==len(returned)==len(frame)
    before=deepcopy(snapshot)
    no_requests=Pages()
    repeated,repeated_snapshot,repeated_coverage=call(inp,page_source=no_requests,fetch=False)
    pd.testing.assert_frame_equal(returned,repeated)
    assert repeated_snapshot==before and repeated_coverage==coverage
    assert not no_requests.calls
    pd.testing.assert_frame_equal(anchor,frozen)
    manifest=json.loads((archive.root/'state.json').read_text())
    kept={manifest['anchor']['name'],manifest['result']['frame']['name'],
          manifest['display_frame']['name'],manifest['snapshot']['name']}
    assert {p.name for p in archive.root.iterdir() if p.suffix in ('.parquet','.gz')}==kept


def test_first_read_without_anchor_has_no_provider_side_effect(tmp_path,monkeypatch):
    inp=inputs(tmp_path)
    monkeypatch.setattr(periods,'context_evidence',lambda *_:pytest.fail('read-only call fetched history'))
    with pytest.raises(periods.HistoryPending) as caught:
        call(inp,fetch=False)
    assert caught.value.coverage['reason']=='not_prepared'


def test_missing_anchor_fetch_uses_one_slice_then_yields_before_extension(tmp_path,monkeypatch):
    inp=inputs(tmp_path)
    calls=[]
    monkeypatch.setattr(periods,'context_evidence',lambda *_:(calls.append(True) or inp[2],{}))
    pages=Pages()
    with pytest.raises(periods.HistoryPending) as caught:
        call(inp,fetch=True,page_source=pages)
    assert calls==[True] and not pages.calls and caught.value.coverage['pages']==0


def test_provider_failure_differs_from_normal_pagination_and_keeps_safe_cursor(tmp_path):
    inp=inputs(tmp_path)
    pages=Pages(page(inp[3].tail(300)),RuntimeError('token=secret'))
    with pytest.raises(periods.HistorySourceError) as caught:
        call(inp,anchor_frame=inp[2],page_source=pages)
    coverage=caught.value.coverage
    assert coverage['state']=='source_error' and coverage['pages']==1
    assert 'secret' not in str(coverage)
    assert not coverage['source_origin']


def test_bad_ohlc_prefix_is_retained_and_strict_validation_still_rejects(tmp_path):
    inp=inputs(tmp_path)
    frame=inp[3].copy(deep=True)
    frame.loc[0,'low']=10.1  # Above the unchanged opening price, outside frozen tail.
    pages=Pages(page(frame.tail(300)),page(frame.iloc[:-300]),page(frame.iloc[:0]))
    with pytest.raises(periods.HistoryUnavailable) as caught:
        call(inp,anchor_frame=inp[2],page_source=pages)
    assert not isinstance(caught.value,periods.HistoryPending)
    coverage=caught.value.coverage
    assert coverage['state']=='invalid' and coverage['validation']['errors']==['INVALID_BARS']
    archive=HistoryArchive(inp[1].parent/'notification_history',coverage['scope'])
    _,saved,snapshot,display=archive.read()
    assert saved.frame.iloc[0].low==10.1
    assert snapshot is None and display is None


def test_invalid_archive_fails_closed_instead_of_restarting_or_loading_foreign_prices(tmp_path):
    inp=inputs(tmp_path)
    with pytest.raises(periods.HistoryPending) as caught:
        call(inp,anchor_frame=inp[2],fetch=False,page_source=Pages())
    archive=HistoryArchive(inp[1].parent/'notification_history',caught.value.coverage['scope'])
    manifest=json.loads((archive.root/'state.json').read_text())
    (archive.root/manifest['anchor']['name']).write_bytes(b'corrupted')
    with pytest.raises(periods.HistoryUnavailable) as failed:
        call(inp,fetch=False,page_source=Pages())
    assert failed.value.coverage['reason']=='history_object_changed'


def test_active_preparer_does_not_block_or_duplicate_work(tmp_path):
    inp=inputs(tmp_path)
    scope=periods._history_scope(inp[0],'30m',inp[2].attrs)
    archive=HistoryArchive(inp[1].parent/'notification_history',scope)
    with interprocess_file_lock(archive.lock_path):
        with pytest.raises(periods.HistoryPending) as caught:
            call(inp,anchor_frame=inp[2],fetch=False)
    assert caught.value.coverage['reason']=='already_preparing'


def test_calendar_expands_to_actual_history_before_730_days_without_dropping_it():
    frame=candles('2023-01-03','2026-10-09')
    event={'market':'us','code':'NVDA.US','source_closed_at':int(frame.date.max().timestamp())}
    context=periods._history_context(event,'30m')
    assert frame.date.min().timestamp()<context['session_periods'][0][0]
    result=periods._background_frame(frame,context,event,'30m')
    assert len(result)==len(frame) and result.date.min()==frame.date.min()
    # A missing verified bar must not disappear behind calendar expansion.
    with pytest.raises(periods.HistoryUnavailable) as error:
        periods._background_frame(frame.drop(index=100),context,event,'30m')
    assert error.value.coverage['validation']['errors']==['DATA_GAPS']


def test_background_preserves_current_indicators_and_geometry_but_removes_point_outputs(monkeypatch):
    import numpy as np
    from chanlun.screening.runner import snapshot_for
    frame=candles()
    frame['close']=10.5+np.sin(np.arange(len(frame))/3)
    frame['open']=frame.close
    frame['high']=frame.close+.3
    frame['low']=frame.close-.3
    frame.attrs['screening_market']='us'
    event={'market':'us','code':'NVDA.US'}
    existing=snapshot_for(frame,'NVDA.US','30m')
    result=periods._background_snapshot(frame,event,'30m')
    for name in ('macd_dif','macd_dea','macd_hist','higher_macd_dif','higher_macd_dea','higher_macd_hist'):
        assert result['chart'][name]==existing['chart'][name]
    for name in ('fxs','bis','xds'):
        assert result['chart'][name]==existing['chart'][name]
    assert result['chart']['fxs']
    assert result['screening_segments']==existing['screening_segments']
    assert [l['centers'] for l in result['levels']]==[l['centers'] for l in existing['levels']]
    assert all(not l['points'] and not l['divergences'] for l in result['levels'])
    assert not result['point_exit_plans'] and not result['formal_point_issued'] and not result['tradable']


def test_archive_cleanup_only_removes_expired_owned_objects(tmp_path):
    import os
    inp=inputs(tmp_path)
    with pytest.raises(periods.HistoryPending) as caught:
        call(inp,anchor_frame=inp[2],fetch=False,page_source=Pages())
    archive=HistoryArchive(inp[1].parent/'notification_history',caught.value.coverage['scope'])
    (archive.root/'unrelated.txt').write_text('keep')
    os.utime(archive.root/'state.json',(1,1))
    HistoryArchive.cleanup(archive.root.parent,now=9*86400)
    assert {p.name for p in archive.root.iterdir()}=={'prepare.lock','unrelated.txt'}


def test_archive_cleanup_never_follows_external_directory_symlink(tmp_path):
    import os
    external=tmp_path/'outside'
    external.mkdir()
    (external/'state.json').write_text('keep')
    os.utime(external/'state.json',(1,1))
    root=tmp_path/'history'
    root.mkdir()
    link=root/('e'*64)
    try:
        link.symlink_to(external,target_is_directory=True)
    except OSError:
        pytest.skip('Windows developer-mode symlink creation is unavailable')
    try:
        HistoryArchive.cleanup(root,now=9*86400)
        assert (external/'state.json').read_text()=='keep'
        assert not (external/'prepare.lock').exists()
    finally:
        link.unlink()


def test_qmt_route_keeps_local_origin_scope_and_independent_geometry(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from chanlun.screening.rules import CN, trading_context
    import chanlun.exchange.exchange_qmt as qmt
    cutoff=pd.Timestamp('2026-10-09 15:00',tz=CN)
    context=trading_context(cutoff.to_pydatetime(),'30m')
    stamps=[]
    for day in context['trading_days'][-30:]:
        for opening in ('09:30','13:00'):
            start=pd.Timestamp(day+' '+opening,tz=CN)
            stamps.extend(start+pd.Timedelta(minutes=offset) for offset in (30,60,90,120))
    frame=source_frame(stamps,'30m')
    frame['code']='SH.513100'
    frame.attrs.update(price_basis_provider='qmt',price_basis_adjustment='front_ratio',
                       price_basis_revision='fixture-frozen-factor-ledger')
    calls=[]
    def local(*a,**k):
        calls.append(k)
        return frame.copy(deep=True)
    monkeypatch.setattr(qmt,'ExchangeQMT',lambda:SimpleNamespace(_klines_once=local))
    event={'id':'qmt-event','evidence_run_id':'e'*32,'market':'a','code':'SH.513100',
           'frequency':'30m','source_closed_at':int(cutoff.timestamp())}
    anchor=frame.tail(200).copy()
    with pytest.raises(periods.HistoryPending):
        periods.background_evidence(event,tmp_path/'runs','30m',frame.attrs,anchor_frame=anchor,fetch=False)
    assert not calls
    returned,snapshot,coverage=periods.background_evidence(event,tmp_path/'runs','30m',frame.attrs)
    assert coverage['source_scope']=='qmt_local' and len(coverage['scope'])==64
    assert coverage['scope']!='qmt_local' and coverage['state']=='source_exhausted'
    assert coverage['source_label']=='QMT 本机可用历史'
    assert len(returned)==len(frame)
    assert snapshot['display_scope']=='notification_background'
    assert calls[0]['args']['local_only'] and calls[0]['args']['skip_download']


def test_spot_route_uses_actual_binance_spot_configuration(tmp_path,monkeypatch):
    from cl_app.services import notification_binance_history as spot
    from cl_app.services.notification_history import HistoryResult
    monkeypatch.setattr(config,'EXCHANGE_CURRENCY_SPOT','binance_spot')
    inp=inputs(tmp_path)
    frame=inp[2].copy(deep=True)
    frame['code']='BTC/USDT'
    frame.attrs.update(price_basis_provider='binance',price_basis_adjustment='none')
    event={**inp[0],'market':'currency_spot','code':'BTC/USDT'}
    calls=[]
    def prepare(*args,**kwargs):
        calls.append(kwargs)
        return HistoryResult(frame,{'reason':'page_budget','can_resume':True}, {'checksum':'fixture'})
    monkeypatch.setattr(spot,'prepare_binance_notification_history',prepare)
    with pytest.raises(periods.HistoryPending):
        periods.background_evidence(event,inp[1],'30m',frame.attrs,anchor_frame=frame,fetch=False)
    assert len(calls)==1 and calls[0]['fetch'] is False
