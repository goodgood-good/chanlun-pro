"""Long display history never replaces the frozen signal proof or sends slices."""
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from cl_app.services import notification_charts as charts, notification_periods, tradingview_snapshot as tv
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import PNG, ImageTransport, image_event
from tests.web.test_notification_multi_period import source, signal


def history(state='target_reached', *, token='1', failed_period=None, **extra):
    return {frequency: {'state': state if failed_period in (None, frequency) else 'target_reached',
        'scope': hashlib.sha256(frequency.encode()).hexdigest(), 'progress_token': token,
        'target_days': tv.VISIBLE_DAYS[frequency], 'target_reached': state == 'target_reached',
        'source_origin': state == 'source_exhausted', 'available_from': 1, 'available_to': 1000, **extra}
        for frequency in tv.PERIODS}


def background(frequency, cutoff):
    frame, snapshot, _ = source(frequency, cutoff)
    before = frame.copy(deep=True)
    before['date'] -= pd.Timedelta(minutes=240*int(frequency[:-1]))
    extended = pd.concat([before, frame], ignore_index=True)
    extended.attrs.update(frame.attrs)
    snapshot = deepcopy(snapshot)
    snapshot.update(analysis_scope='notification_background', source_started_at=int(extended.date.iloc[0].timestamp()))
    snapshot['chart'] = {k: [value*3 for value in values]*2 for k, values in snapshot['chart'].items()}
    return extended, snapshot


def test_history_layout_keeps_frozen_primary_and_uses_distinct_panel_cache_ids(signal, monkeypatch, tmp_path):
    event, frozen = signal
    before = deepcopy(frozen[1])
    contexts = Mock(); monkeypatch.setattr(notification_periods, 'context_evidence', contexts)
    calls = []
    def ready(event, root, frequency, attrs, *, anchor_frame, fetch):
        calls.append((frequency, fetch))
        frame, snapshot = background(frequency, event['source_closed_at'])
        coverage = history()[frequency]
        return frame, snapshot, coverage
    monkeypatch.setattr(notification_periods, 'background_evidence', ready)
    layout = tv.prepare_history_layout(event, tmp_path)
    panels = layout['panels']
    assert [p['settings']['frequency'] for p in panels] == ['1m','5m','5m','30m']
    assert len({p['settings']['cache_key'] for p in panels}) == 4
    proof = next(p for p in panels if p['settings']['signal_panel'])
    assert proof['history']['macd_dif'] == before['chart']['macd_dif']
    assert proof['history']['higher_macd_dif'] == before['chart']['higher_macd_dif']
    for panel in panels:
        if panel is proof: continue
        assert panel['settings']['background_only'] and panel['settings']['stop_price'] is None
        assert panel['settings']['marker'] is False and panel['settings']['label'] == ''
        assert panel['history']['strict_structure']['analysis_scope'] == 'notification_background'
    assert calls == [('1m',False),('5m',False),('30m',False)]
    assert frozen[1] == before
    contexts.assert_not_called()


def test_one_bounded_frequency_advances_and_completed_retry_never_redraws_the_proof(signal, monkeypatch, tmp_path):
    event, _ = signal; calls = []
    def pending(e, root, frequency, attrs, *, anchor_frame, fetch):
        calls.append((frequency, fetch))
        raise notification_periods.HistoryPending(history('pending')[frequency])
    monkeypatch.setattr(notification_periods, 'background_evidence', pending)
    monkeypatch.setattr(notification_periods, 'context_evidence', lambda e,r,f,a: source(f,e['source_closed_at'])[:2])
    layout = tv.prepare_history_layout(event, tmp_path, reused_images={'5m':'frozen-primary'}, history_period='1m')
    assert [frequency for frequency, fetch in calls if fetch] == ['1m']
    assert next(p for p in layout['panels'] if p['settings']['signal_panel'])['cached_image'] == 'frozen-primary'
    capture = Mock(); monkeypatch.setattr(tv, 'capture', capture)
    with pytest.raises(charts.ChartHistoryPending):
        charts.render_signal_chart(event, tmp_path, completion=True, history_period='5m')
    capture.assert_not_called()


def test_background_browser_can_export_without_a_native_structure_or_signal_marker(monkeypatch):
    # A cached primary image stands for already verified pixels. Only the new
    # independent background opens TradingView; no provider requests are made.
    import base64
    event = {'market':'currency_spot','code':'BTC/USDT','name':'fixture','frequency':'1m',
             'source_closed_at':1791426600,'point_type':'1buy','stage':'confirmed','anchor_price':11.}
    frame, snapshot = background('1m', event['source_closed_at'])
    panel = tv._background_panel(event, frame, snapshot, history()['1m'])
    proof = {'settings':{'frequency':'1m','panel_id':'proof:1m','signal_panel':True},
             'cached_image':'data:image/png;base64,'+base64.b64encode(PNG).decode()}
    exported=[]
    png = tv.capture({'panels':[proof,panel], 'history_coverage':history()}, panel_sink=lambda key,data:exported.append((key,data)))
    assert png.startswith(b'\x89PNG') and len(png)>10000
    assert len(exported) == 1 and exported[0][0].startswith('history:1m:')
    assert png.coverage['periods'] == ['1m']
    assert png.coverage['missing_periods'] == ['5m','30m']
    assert [p['panel_id'] for p in png.coverage['panels']] == ['proof:1m','history:1m']


def test_failed_long_view_does_not_mark_history_complete_when_same_period_proof_succeeds(monkeypatch):
    png='data:image/png;base64,iVBORw0KGgo='
    failed=Mock();failed.goto.side_effect=ValueError('background export failed')
    compositor=Mock();compositor.evaluate.return_value=png
    browser=Mock();browser.new_context.return_value.new_page.side_effect=[failed,compositor]
    @contextmanager
    def playwright():yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_:browser))
    monkeypatch.setattr('playwright.sync_api.sync_playwright',playwright)
    panels=[{'settings':{'frequency':'1m','panel_id':'proof:1m','purpose':'frozen_signal','signal_panel':True},'cached_image':png}]
    for frequency in tv.PERIODS:
        panels.append({'settings':{'frequency':frequency,'panel_id':'history:'+frequency,
            'purpose':'independent_background','signal_panel':False},**({'cached_image':png} if frequency!='1m' else {})})
    result=tv.capture({'panels':panels,'history_coverage':history(),
                       'history_panel_ids':{f:'history:'+f for f in tv.PERIODS}})
    assert result.coverage['periods']==list(tv.PERIODS) and not result.coverage['missing_periods']
    assert result.coverage['missing_history_periods']==['1m']
    assert result.coverage['history_coverage']['1m']['state']=='render_error'


def chart_with_history(tag, coverage):
    return charts.RenderedChart(PNG+tag.encode(), periods=list(tv.PERIODS),
                                primary_frequency='1m', history_coverage=coverage)


def history_box(tmp_path, items):
    clock=[1000.]; transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True,
                      signal_validator=lambda rows:(rows,[]))
    queue=iter(items)
    def render(*_):
        value=next(queue)
        if isinstance(value,Exception): raise value
        return value
    box.charts.renderer=render;box.charts.check_public=False
    box.signal_events([{**image_event(), 'frequency':'1m'}],since=0);box.deliver_one()
    return box,transport,clock


def job(box):
    with box.charts._db() as db:
        return dict(db.execute('SELECT * FROM jobs').fetchone())


def test_successful_page_slices_do_not_exhaust_three_failure_attempts_or_send_interim_pngs(tmp_path):
    first=chart_with_history('initial',history('pending',token='0'))
    slices=[charts.ChartHistoryPending(history('pending',token=str(i))) for i in range(1,8)]
    box,transport,clock=history_box(tmp_path,[first,*slices,chart_with_history('complete',history())])
    box.charts.process_one();clock[0]+=5;box.deliver_one()
    assert len(transport.images)==1 and job(box)['attempts']==0
    for _ in slices:
        clock[0]=job(box)['next_try']+1;box.charts.process_one();box.deliver_one()
        assert job(box)['status']=='pending' and job(box)['attempts']==0
        assert len(transport.images)==1
    clock[0]=job(box)['next_try']+1;box.charts.process_one();box.deliver_one()
    assert job(box)['status']=='ready' and len(transport.images)==2
    with box._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'chart-complete:%'").fetchone()[0]==1
    assert box.charts.store.coverage(job(box)['initial_image_hash'])['missing_history_periods']==list(tv.PERIODS)


def test_source_errors_use_three_real_failures_and_honor_provider_retry_after(tmp_path):
    first=chart_with_history('initial',history('pending',failed_period='1m'))
    error=charts.ChartHistoryPending(history('source_error',failed_period='1m',next_retry=120))
    box,transport,clock=history_box(tmp_path,[first,error,error,error])
    box.charts.process_one();clock[0]+=5;box.deliver_one()
    for attempt in range(1,4):
        clock[0]=job(box)['next_try']+1;before=clock[0];box.charts.process_one()
        assert job(box)['attempts']==attempt
        if attempt<3:
            assert job(box)['status']=='pending' and job(box)['next_try']>=before+120
    assert job(box)['status']=='failed'
    assert len(transport.images)==1


def test_no_progress_backs_off_and_original_notification_deadline_stops_history(tmp_path):
    coverage=history('pending',token='unchanged',failed_period='1m')
    box,transport,clock=history_box(tmp_path,[chart_with_history('initial',coverage),
        *[charts.ChartHistoryPending(coverage) for _ in range(6)]])
    box.charts.process_one();clock[0]+=5;box.deliver_one();delays=[]
    for _ in range(5):
        clock[0]=job(box)['next_try']+1;before=clock[0];box.charts.process_one()
        delays.append(job(box)['next_try']-before)
    assert delays==[30,60,120,240,300] and job(box)['attempts']==0
    clock[0]=2801;box.charts.process_one()
    assert job(box)['status']=='expired' and len(transport.images)==1


@pytest.mark.parametrize('state',['invalid','unsupported'])
def test_terminal_history_limit_is_explicit_and_never_spends_the_complete_receipt(tmp_path,state):
    box,transport,clock=history_box(tmp_path,[chart_with_history('initial',history(state,failed_period='1m'))])
    box.charts.process_one();clock[0]+=5;box.deliver_one()
    assert job(box)['status']=='failed' and json.loads(job(box)['history_coverage'])['1m']['state']==state
    with box._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'chart-complete:%'").fetchone()[0]==0
    assert len(transport.images)==1


def test_proved_short_source_origin_can_finish_without_claiming_target_length(tmp_path):
    resolved=history();resolved['1m'].update(state='source_exhausted',target_reached=False,source_origin=True,
        target_days=30,source_label='QMT 本机可用历史')
    box,transport,clock=history_box(tmp_path,[chart_with_history('initial',resolved)])
    box.charts.process_one();clock[0]+=5;box.deliver_one()
    assert job(box)['status']=='ready'
    coverage=box.charts.store.coverage(job(box)['initial_image_hash'])
    assert coverage['missing_history_periods']==[] and coverage['history_coverage']['1m']['target_reached'] is False
    assert '不足目标范围' in tv.history_description('1m',resolved['1m'])


def test_anchor_persistence_cannot_hide_source_failure(signal, monkeypatch, tmp_path):
    event, _ = signal
    auxiliary = next(f for f in tv.PERIODS if f != event['frequency'])
    def prepare(e, root, frequency, attrs, *, anchor_frame, fetch):
        value = history('source_error' if fetch else 'pending')[frequency]
        if fetch:
            value['next_retry'] = 300
            raise notification_periods.HistorySourceError(value)
        raise notification_periods.HistoryPending(value)
    monkeypatch.setattr(notification_periods, 'background_evidence', prepare)
    monkeypatch.setattr(notification_periods, 'context_evidence', lambda e,r,f,a: source(f,e['source_closed_at'])[:2])
    layout = tv.prepare_history_layout(event, tmp_path, history_period=auxiliary)
    assert layout['history_coverage'][auxiliary]['state'] == 'source_error'
    assert layout['history_coverage'][auxiliary]['next_retry'] == 300


def test_one_invalid_history_does_not_block_other_periods_or_their_supplement(tmp_path):
    initial = charts.RenderedChart(PNG+b'one', periods=['1m'], missing_periods=['5m','30m'],
                                  primary_frequency='1m', history_coverage=history('pending'))
    partial = history('pending', token='more'); partial['1m']['state']='invalid'
    final = history(); final['1m']['state']='invalid'
    box, transport, clock = history_box(tmp_path, [initial, charts.ChartHistoryPending(partial),
                                                  chart_with_history('all-periods', final)])
    box.charts.process_one(); clock[0]+=5; box.deliver_one()
    clock[0]=job(box)['next_try']+1; box.charts.process_one()
    assert job(box)['status']=='pending'
    clock[0]=job(box)['next_try']+1; box.charts.process_one(); box.deliver_one()
    assert len(transport.images)==2 and job(box)['status']=='failed'
    assert json.loads(job(box)['missing_periods'])==[]
    assert json.loads(job(box)['missing_history_periods'])==['1m']


def test_legacy_three_period_receipt_cannot_suppress_new_long_history_image(tmp_path):
    legacy=charts.RenderedChart(PNG+b'legacy', periods=list(tv.PERIODS), primary_frequency='1m')
    box,transport,clock=history_box(tmp_path,[legacy,chart_with_history('long-history',history())])
    box.charts.process_one(); clock[0]+=5; box.deliver_one()
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET status='pending',next_try=0,history_coverage=?", (json.dumps(history('pending')),))
    box.charts.process_one(); clock[0]+=5; box.deliver_one()
    assert job(box)['status']=='ready' and len(transport.images)==2
