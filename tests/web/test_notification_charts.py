import base64
import json
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_charts import ChartStore
from tests.web.test_dingtalk_notifications import Transport,event
from tests.test_holding_consolidation_pipeline import holding_run as holding_run

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZkAAAAASUVORK5CYII=')


class ImageTransport(Transport):
    def __init__(self):
        super().__init__();self.images=[]
    def send_markdown(self,title,text):
        self.images.append((title,text))


def image_event(stamp=1000,identity='point'):
    return {**event('confirmed',identity=identity,stamp=stamp),"point_status":"confirmed","point_id":"p",
            "evidence":{"status":"complete"},"evidence_run_id":"a"*32}


def test_signed_png_access_is_exact_expiring_and_cannot_escape_store(tmp_path):
    clock=[1000.];store=ChartStore(tmp_path,now=lambda:clock[0])
    url=store.publish(PNG);u=urlsplit(url);name=u.path.rsplit('/',1)[-1][:-4];q=parse_qs(u.query)
    assert store.resolve(name,q['expires'][0],q['signature'][0]).read_bytes()==PNG
    assert store.resolve('../secret',q['expires'][0],q['signature'][0]) is None
    assert store.resolve(name,q['expires'][0],'0'*64) is None
    assert store.resolve(name,str(int(q['expires'][0])+1),q['signature'][0]) is None
    clock[0]=int(q['expires'][0])+1
    assert store.resolve(name,q['expires'][0],q['signature'][0]) is None


def test_text_is_sent_before_rendering_and_chart_recovery_does_not_duplicate_or_spin(tmp_path):
    clock=[1000.];transport=ImageTransport()
    outbox=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True,
                         signal_validator=lambda rows:(rows,[]))
    rendered=[]
    outbox.charts.renderer=lambda row,root:rendered.append(row.copy()) or PNG
    outbox.charts.check_public=False
    row=image_event()
    outbox.signal_events([row],since=0);outbox.deliver_one()
    assert len(transport.messages)==1 and not rendered and not transport.images
    outbox.charts.process_one()
    assert len(rendered)==1 and outbox.snapshot()['pending']==0
    assert outbox.snapshot()['chart_delivery']['pending']==1
    clock[0]+=5;outbox.deliver_one()
    assert len(transport.images)==1 and '![信号快照]' in transport.images[0][1]
    assert '[查看' not in transport.images[0][1] and '/?market=' not in transport.images[0][1]
    assert 'http' not in transport.messages[0]
    assert outbox.snapshot()['sent']==1 and outbox.snapshot()['chart_delivery']['sent']==1
    outbox.charts._wake.clear();outbox.charts.recover()
    assert not outbox.charts._wake.is_set()
    outbox.charts.process_one()
    assert len(rendered)==1


def test_unknown_evidence_waits_without_exhausting_image_attempts_and_invalidations_have_no_image(tmp_path):
    clock=[1000.];pending=[True]
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',now=lambda:clock[0],charts=True,
                      signal_validator=lambda rows:([],rows) if pending[0] else (rows,[]))
    row=image_event();box.charts.enqueue([row,{**row,'id':'invalid','stage':'invalidated'}],1000)
    for _ in range(5):box.charts.process_one();clock[0]+=5
    with box.charts._db() as db:
        jobs=db.execute('SELECT status,attempts FROM jobs').fetchall()
    assert len(jobs)==1 and jobs[0]['status']=='pending' and jobs[0]['attempts']==0


def test_pending_text_has_priority_over_ready_image(tmp_path):
    transport=ImageTransport();box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000)
    box.enqueue_chart(image_event(),'https://example.test/image.png')
    box.signal_events([event('confirmed',identity='new-text',stamp=1000)],since=0)
    box.deliver_one()
    assert len(transport.messages)==1 and not transport.images


def test_waiting_image_checks_yield_to_a_ready_new_image(tmp_path):
    clock=[1000.];rendered=[]
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',now=lambda:clock[0],charts=True,
        signal_validator=lambda rows:([],rows) if rows[0]['id'].startswith('waiting') else (rows,[]))
    box.charts.renderer=lambda row,_:rendered.append(row['id']) or PNG
    box.charts.check_public=False
    for index,identity in enumerate(['waiting-1','waiting-2','waiting-3','ready-new']):
        box.charts.enqueue([image_event(stamp=1000+index*.1,identity=identity)],1000+index*.1)
    for index in range(4):
        clock[0]=1001+index*2;box.charts.process_one()
    assert rendered==['ready-new']
    assert box.charts.snapshot()['pending']==3


def test_public_image_failure_keeps_text_successful_and_retries_image_only(tmp_path,monkeypatch):
    transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000,charts=True)
    box.charts.renderer=lambda *_:PNG
    monkeypatch.setattr('requests.get',lambda *args,**kwargs:SimpleNamespace(status_code=404,content=b'Not found'))
    box.signal_events([image_event()],since=0);box.deliver_one();box.charts.process_one()
    assert len(transport.messages)==1 and not transport.images
    assert box.snapshot()['sent']==1 and box.snapshot()['chart_delivery']['pending']==0
    with box.charts._db() as db:
        job=db.execute('SELECT status,error FROM jobs').fetchone()
    assert job['status']=='pending' and '公网访问校验失败' in job['error']


def test_new_notification_window_cancels_text_and_images_including_inflight_render(tmp_path):
    clock=[1000.];transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True)
    first=image_event();box.signal_events([first],since=0);box.deliver_one()
    old=image_event(identity='old-pending');box.signal_events([old],since=0)
    def render(*_):
        clock[0]+=30;box.begin_window(clock[0]);return PNG
    box.charts.renderer=render;box.charts.check_public=False
    box.charts.process_one();box.deliver_one()
    box.charts.recover()
    assert len(transport.messages)==1 and not transport.images
    assert box.snapshot()['pending']==0 and box.snapshot()['chart_delivery']['pending']==0
    fresh=image_event(stamp=clock[0],identity='fresh');box.signal_events([fresh],since=clock[0]);box.deliver_one()
    assert len(transport.messages)==2


def test_notification_switch_cannot_resurrect_a_retired_lease_during_validation(tmp_path):
    clock=[1000.];transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0])
    def validate(rows):
        clock[0]+=1;box.begin_window(clock[0]);return [],rows
    box.signal_validator=validate;box.signal_events([image_event()],since=0);box.deliver_one()
    assert not transport.messages and box.snapshot()['pending']==0


def test_already_inflight_delivery_keeps_its_actual_provider_receipt(tmp_path):
    clock=[1000.];transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True)
    original=transport.send
    def send(content):
        clock[0]+=1;box.begin_window(clock[0]);original(content)
    transport.send=send
    box.signal_events([image_event()],since=0);box.deliver_one()
    assert len(transport.messages)==1 and box.snapshot()['sent']==1
    assert box.charts.snapshot()['pending']==0


def test_image_revalidation_keeps_the_delivered_snapshot_cutoff(tmp_path):
    row=image_event()
    transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000,
                      signal_validator=lambda rows:([{**rows[0],'source_closed_at':2000,'evidence_run_id':'b'*32}],[]))
    box.enqueue_chart(row,'https://example.test/snapshot.png')
    box.deliver_one()
    assert len(transport.images)==1
    with box._connect() as db:
        saved=json.loads(db.execute('SELECT events FROM signal_payloads').fetchone()[0])[0]
    assert saved['source_closed_at']==row['source_closed_at'] and saved['evidence_run_id']==row['evidence_run_id']


def test_queued_legacy_chart_keeps_inline_image_but_loses_navigation_link(tmp_path):
    transport=ImageTransport();box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000)
    box.enqueue_chart(image_event(),'https://example.test/snapshot.png')
    with box._connect() as db:
        content=json.loads(db.execute('SELECT content FROM outbox').fetchone()[0])
        content['text']+='\n\n[查看当前图表](http://old.test/?market=a)'
        db.execute('UPDATE outbox SET content=?',(json.dumps(content),))
    box.deliver_one()
    assert '![信号快照](https://example.test/snapshot.png)' in transport.images[0][1]
    assert '[查看' not in transport.images[0][1]


def test_holding_risk_receipt_enqueues_an_image_with_its_frozen_trigger(tmp_path):
    clock=[1000.];transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True)
    row={**image_event(),'signal_type':'holding_risk','stage':'triggered','point_status':'triggered',
         'point_type':'risk_stop','stop_price':10,'trigger_price':9.8}
    rendered=[];box.charts.renderer=lambda e,_:rendered.append(e) or PNG;box.charts.check_public=False
    box.signal_events([row],since=0);box.deliver_one();box.charts.process_one()
    clock[0]+=5;box.deliver_one()
    assert rendered[0]['trigger_price']==9.8 and len(transport.images)==1
    assert 'http' not in transport.messages[0] and '[查看' not in transport.images[0][1]


def test_risk_stop_chart_marks_the_breach_bar_and_rejects_mismatched_low(holding_run):
    from decimal import Decimal
    from cl_app.services.tradingview_snapshot import prepare
    from chanlun.screening.evidence import load_evidence
    import pytest
    _,directory,record,frame,stock,_,_=holding_run
    row=record['rows'][0]
    _,snapshot,_=load_evidence(directory,row['code'],row['frequency'],row['evidence'])
    quantum=Decimal(snapshot['structure_price_quantum'])
    low=float(frame.low.iloc[-1]);stop_tick=int(Decimal(str(low))/quantum)+1
    event={**image_event(stamp=row['source_closed_at']),'market':stock['market'],'code':row['code'],
           'frequency':row['frequency'],'source_closed_at':row['source_closed_at'],
           'signal_type':'holding_risk','stage':'triggered','point_type':'risk_stop',
           'evidence':row['evidence'],'evidence_run_id':directory.name,
           'available_at':row['source_closed_at'],'bound_cutoff':int(frame.date.iloc[-2].timestamp()),
           'price_basis_revision':snapshot['price_basis_revision'],'trigger_price':low,
           'structure_price_quantum':str(quantum),'stop_price':float(stop_tick*quantum),
           'stop_loss':{'price_tick':stop_tick}}
    payload=prepare(event,directory.parent)
    assert payload['settings']['marker'] and payload['settings']['anchor_price']==low
    assert payload['settings']['anchor_at']==row['source_closed_at']
    assert '结构止损触发' in payload['settings']['label']
    with pytest.raises(ValueError,match='stop does not match'):
        prepare({**event,'trigger_price':low-1},directory.parent)


def test_snapshot_routes_block_live_data_and_local_path_escape():
    from cl_app.services.tradingview_snapshot import ORIGIN,route_request
    from unittest.mock import Mock
    for path,method in (('/static/../services/notification_text.py','GET'),
                        ('/static/%2e%2e/services/notification_text.py','GET'),('/tv/1.1/charts','POST'),
                        ('/unknown','GET')):
        route=Mock(request=SimpleNamespace(url=ORIGIN+path,method=method))
        route_request(route,{'settings':{'resolution':'5'}})
        route.abort.assert_called_once();route.fulfill.assert_not_called()


def test_image_storage_failure_does_not_stop_the_text_delivery_worker(tmp_path,monkeypatch):
    from contextlib import contextmanager
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=ImageTransport(),now=lambda:1000,charts=True)
    @contextmanager
    def unavailable():
        raise OSError('storage offline')
        yield
    monkeypatch.setattr(box.charts,'_db',unavailable)
    try:
        box.start(lambda:False)
        assert box._thread.is_alive()
        assert '文字通知继续' in box.charts.snapshot()['last_error']
    finally:
        box.stop()


def test_public_chart_endpoint_requires_signature_not_a_login_cookie(tmp_path,monkeypatch):
    from cl_app import create_app
    store=ChartStore(tmp_path)
    url=store.publish(PNG);u=urlsplit(url)
    monkeypatch.setattr('cl_app.blueprints.monitor.service',SimpleNamespace(notifications=SimpleNamespace(charts=SimpleNamespace(store=store))))
    app=create_app(test_config={'TESTING':True,'VALIDATE_WEB_SECURITY':False})
    client=app.test_client()
    response=client.get(u.path+'?'+u.query)
    assert response.status_code==200 and response.mimetype=='image/png' and response.data==PNG
    assert client.get(u.path).status_code==404


def test_recorded_chart_uses_verified_frozen_data_and_rejects_mismatched_marker(holding_run):
    from cl_app.services.notification_charts import render_signal_chart
    from cl_app.services.signal_monitor import _consolidation_signal
    import pytest
    _,directory,record,_,_,_,_=holding_run
    row=record['rows'][0]
    candidate=next(c for c in row['consolidation_alerts'] if c['alert']['status']=='confirmed')
    event={**_consolidation_signal({**row,**candidate}),'evidence_run_id':directory.name}
    png=render_signal_chart(event,directory.parent)
    assert png.startswith(b'\x89PNG') and len(png)>10000
    with pytest.raises(ValueError,match='marker disagrees'):
        render_signal_chart({**event,'anchor_price':event['anchor_price']+1},directory.parent)
    with pytest.raises(ValueError,match='different cutoffs'):
        render_signal_chart({**event,'source_closed_at':event['source_closed_at']+1},directory.parent)
