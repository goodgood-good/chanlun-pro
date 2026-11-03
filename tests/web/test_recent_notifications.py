"""Read-only notification history is distinct from signal detection history."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import MultiDict

from cl_app.services.recent_notifications import RecentNotifications

NOW = 1791565200  # Shortly after midnight in China; yesterday remains visible.


@pytest.fixture
def records(tmp_path):
    path = tmp_path / 'dingtalk_outbox.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE outbox(id TEXT PRIMARY KEY,kind TEXT,content TEXT,status TEXT,
            created REAL,expires REAL,attempts INTEGER,next_try REAL,sent REAL,error TEXT,send_started REAL);
            CREATE TABLE signal_payloads(outbox_id TEXT PRIMARY KEY,events TEXT);
            CREATE TABLE inline_charts(signal_key TEXT PRIMARY KEY,url TEXT,created REAL,outbox_id TEXT);''')
    with sqlite3.connect(tmp_path / 'signal_analytics.sqlite3') as db:
        db.execute('''CREATE TABLE events(id TEXT PRIMARY KEY,signal_id TEXT,cycle INTEGER,market TEXT,
            frequency TEXT,point_type TEXT,signal_type TEXT,stage TEXT,change_kind TEXT,
            recorded REAL,cutoff REAL,delay_bars INTEGER,exit_reason TEXT,external INTEGER,revision TEXT)''')
    (tmp_path / 'state.json').write_text(json.dumps({'holding_symbols':['us:BBB.US'], 'events':[]}), encoding='utf-8')
    return tmp_path, RecentNotifications(tmp_path, now=lambda: NOW)


def event(code='AAA.US', frequency='1m', **kwargs):
    return {'id':code+'-'+frequency,'code':code,'name':code,'market':'us','frequency':frequency,
            'point_type':'3buy','stage':'confirmed','point_status':'confirmed','signal_type':'buy_sell_point',
            'recorded_at':NOW-80,'available_at':NOW-100,'source_closed_at':NOW-60,'anchor_price':123.5,**kwargs}


def add(root, identity, events=(), *, created=NOW-50, status='sent', kind='signals', content='保存的通知原文'):
    with sqlite3.connect(root/'dingtalk_outbox.sqlite3') as db:
        db.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (identity,kind,content,status,created,created+1800,1,created+10,created+5 if status=='sent' else None,'fixture error' if status=='failed' else '',None))
        db.execute('INSERT INTO signal_payloads VALUES (?,?)',(identity,json.dumps(list(events))))


def silent(root, value, *, identity='a'*64):
    with sqlite3.connect(root/'signal_analytics.sqlite3') as db:
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (identity,value['id'],0,value['market'],value['frequency'],value['point_type'],value['signal_type'],
                    value['stage'],'discovered',value['recorded_at'],value['source_closed_at'],0,'[]',0,'v'))
    (root/'state.json').write_text(json.dumps({'holding_symbols':['us:BBB.US'], 'events':[{**value,'external_notifications':False,
        'notification_suppressed_reason':'ALGORITHM_RECLASSIFICATION'}]}),encoding='utf-8')


def test_latest_day_is_delivery_receipts_not_detection_events(records):
    root, reader = records
    add(root,'signals:one',[event()])
    add(root,'signals:pending',[event('BBB.US')],status='retry')
    add(root,'signals:old',[event()],created=NOW-90000)
    silent(root,event('SILENT.US'))
    result=reader.list({})
    assert result['window']['hours']=='24'
    assert result['pagination']['total']==2
    assert result['summary']=={'sent':1,'pending':1,'failed':0,'expired':0,'suppressed':1,'holdings':1,'last_sent_at':NOW-45}
    assert {x['status'] for x in result['items']}=={'sent','pending'}
    detail=reader.detail(next(x['id'] for x in result['items'] if x['status']=='sent'))
    assert (detail['event_at'],detail['confirmed_at'],detail['created_at'],detail['sent_at'])==(NOW-80,NOW-100,NOW-50,NOW-45)
    assert detail['body']=='保存的通知原文'
    assert detail['evidence_lines']==['AAA.US · 1m · 三买 · 保存的定位价：123.5']
    suppressed=reader.list({'status':'suppressed'})
    assert suppressed['pagination']['total']==1 and suppressed['items'][0]['status']=='suppressed'
    hidden=reader.detail(suppressed['items'][0]['id'])
    assert hidden['sent_at'] is None and hidden['message']==''
    assert hidden['suppression_reason']=='ALGORITHM_RECLASSIFICATION'


def test_silent_filter_merges_semantic_suppression_receipts_with_detection_history(records):
    root,reader=records
    for i in range(22):
        add(root,'suppressed:'+str(i),[event('AAA.US')],status='suppressed',created=NOW-100-i*2)
        silent(root,event('AAA.US',recorded_at=NOW-101-i*2),identity=f'{i:064x}')
    first=reader.list({'status':'suppressed','size':15})
    assert first['pagination']['total']==44 and len(first['items'])==15
    assert first['summary']['suppressed']==44
    assert {row['status'] for row in first['items']}=={'suppressed'}
    second=reader.list({'status':'suppressed','size':15,'page':2})
    assert len(second['items'])==15
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}


def test_filters_must_match_the_same_signal_inside_a_grouped_message(records):
    root,reader=records
    add(root,'signals:group',[event('AAA.US','1m'),event('BBB.US','5m',signal_type='consolidation_divergence')])
    assert reader.list({'q':'AAA','frequency':'5m'})['pagination']['total']==0
    assert reader.list({'q':'AAA','scope':'holdings'})['pagination']['total']==0
    assert reader.list({'kind':'consolidation','frequency':'1m'})['pagination']['total']==0
    matched=reader.list({'q':'BBB','scope':'holdings','frequency':'5m','kind':'consolidation'})
    assert matched['pagination']['total']==1
    assert matched['items'][0]['code']=='BBB.US' and matched['items'][0]['kind']=='consolidation'
    assert reader.list({'q':"%' OR 1=1 --"})['pagination']['total']==0
    assert reader.list({'q':'三买'})['pagination']['total']==1


@pytest.mark.parametrize('point_type', ['1buy', '2buy', '3buy', '1sell', '2sell', '3sell'])
def test_point_type_filters_receipts_summaries_and_silent_history(records, point_type):
    root, reader = records
    for point in ('1buy', '2buy', '3buy', '1sell', '2sell', '3sell'):
        add(root, 'signals:' + point, [event(point_type=point)])
    silent(root, event('SILENT.US', point_type=point_type))
    result = reader.list({'point_type':point_type})
    assert result['pagination']['total'] == result['summary']['sent'] == 1
    assert result['items'][0]['point_type'] == point_type
    hidden = reader.list({'point_type':point_type, 'status':'suppressed'})
    assert hidden['pagination']['total'] == 1 and hidden['items'][0]['point_type'] == point_type


def test_point_type_filter_is_per_member_and_binds_page_cursors(records):
    root, reader = records
    for i in range(20):
        add(root, f'signals:{i}', [event('AAA.US', '1m', point_type='1buy'),
                                  event('BBB.US', '5m', point_type='3sell')], created=NOW-100-i)
    add(root, 'screening:summary', kind='screening', content='BBB.US 三卖')
    assert reader.list({'point_type':'3sell', 'frequency':'1m'})['pagination']['total'] == 0
    assert reader.list({'point_type':'3sell', 'q':'AAA'})['pagination']['total'] == 0
    first = reader.list({'point_type':'3sell', 'q':'BBB', 'size':15})
    assert first['pagination']['total'] == first['summary']['sent'] == 20
    assert all(row['code'] == 'BBB.US' for row in first['items'])
    second = reader.list({'point_type':'3sell', 'q':'BBB', 'size':15, 'cursor':first['pagination']['next_cursor']})
    assert len(second['items']) == 5
    assert not {row['id'] for row in first['items']} & {row['id'] for row in second['items']}
    with pytest.raises(ValueError):
        reader.list({'point_type':'1buy', 'q':'BBB', 'size':15, 'cursor':first['pagination']['next_cursor']})
    with pytest.raises(ValueError):
        reader.list({'point_type':'4buy'})


def test_delivered_consolidation_warning_is_not_relabelled_as_confirmed(records):
    root,reader=records
    warning=event(stage='warning',point_status='provisional',signal_type='consolidation_divergence',
                  point_type='consolidation_up',macd_area_ratio=0.42)
    add(root,'signals:warning',[warning])
    item=reader.list({'kind':'consolidation'})['items'][0]
    assert item['delivery_status']=='sent' and item['signal_stage']=='warning'
    assert item['confirmed_at'] is None
    detail=reader.detail(item['id'])
    assert any('0.4200' in line for line in detail['evidence_lines'])


def test_saved_stop_and_waiting_exit_plan_are_shown_without_exposing_proof_paths(records):
    root,reader=records
    saved=event('BABA.US',anchor_price=109.77,exit_plan={
        'source_frequency':'1m','side':'buy','known_at':NOW-100,'as_of':NOW-60,
        'evidence_path':r'D:\private\proof.json',
        'stop_loss':{'price':107.0,'price_tick':107000,'status':'reference','basis':'center_edge',
                     'trigger':'lt','sources':[r'D:\private\original.txt'],'derivation':'internal proof'},
        'take_profit':{'price':None,'price_tick':None,'status':'waiting','basis':'opposite_point'},
    })
    add(root,'signals:risk-reference',[saved])
    detail=reader.detail(reader.list({})['items'][0]['id'])
    lines='\n'.join(detail['evidence_lines'])
    assert '109.77' in lines and '结构止损参考价：107（跌破触发）' in lines
    assert '原止盈/退出计划：等待本周期已确认反向信号' in lines
    assert detail['signal']['exit_plan']['take_profit']['basis']=='opposite_point'
    assert detail['evidence_lines_title']=='保存的信号要点' and detail['structural_evidence_complete'] is False
    encoded=json.dumps(detail,ensure_ascii=False)
    assert 'private' not in encoded and 'derivation' not in encoded and 'sources' not in encoded


def test_absent_exit_plan_does_not_invent_stop_or_waiting_exit(records):
    root,reader=records
    add(root,'signals:no-risk-plan',[event()])
    detail=reader.detail(reader.list({})['items'][0]['id'])
    assert 'exit_plan' not in detail['signal']
    assert not any('止损' in line or '止盈' in line for line in detail['evidence_lines'])


def test_same_symbol_multiple_periods_keep_saved_plan_labels_distinct(records):
    root,reader=records
    first=event('BABA.US','1m',name='阿里巴巴',exit_plan={'stop_loss':{'price':107.0}})
    second=event('BABA.US','5m',name='阿里巴巴',exit_plan={'stop_loss':{'price':104.0}})
    add(root,'signals:multiple-period-plans',[first,second])
    detail=reader.detail(reader.list({})['items'][0]['id'])
    lines=detail['evidence_lines']
    assert '阿里巴巴 BABA.US · 1m · 三买 · 保存的结构止损参考价：107' in lines
    assert '阿里巴巴 BABA.US · 5m · 三买 · 保存的结构止损参考价：104' in lines


def test_grouped_message_does_not_claim_first_members_stage_period_or_point_type(records):
    root,reader=records
    first=event('AAA.US','1m',point_type='3buy',stage='confirmed')
    second=event('BBB.US','5m',point_type='1sell',stage='warning',point_status='provisional')
    add(root,'signals:mixed-states',[first,second])
    row=reader.list({})['items'][0]
    detail=reader.detail(row['id'])
    for value in (row,detail):
        assert value['frequency'] is None and value['point_type'] is None and value['signal_stage'] is None
        assert value['delivery_status']=='sent' and value['confirmed_at'] is None
    assert [(s['frequency'],s['point_type'],s['stage']) for s in detail['signals']]==[
        ('1m','3buy','confirmed'),('5m','1sell','warning')]


def test_grouped_message_exposes_only_nonempty_common_member_fields(records):
    root,reader=records
    add(root,'signals:common',[event('AAA.US'),event('BBB.US')])
    row=reader.list({})['items'][0]
    assert (row['frequency'],row['point_type'],row['signal_stage'])==('1m','3buy','confirmed')
    add(root,'signals:incomplete',[event('AAA.US'),event('BBB.US',frequency='',point_type='',stage='')],created=NOW-20)
    row=reader.list({})['items'][0]
    assert row['frequency'] is None and row['point_type'] is None and row['signal_stage'] is None


def test_cursor_is_stable_across_new_receipts_and_stops_at_the_end(records):
    root,reader=records
    for i in range(32):add(root,f'signals:{i:03}',[event()],created=NOW-10-i)
    first=reader.list({'size':'15'}); ids={x['id'] for x in first['items']}
    add(root,'signals:new',[event()],created=NOW+1)
    reader.now=lambda:NOW+10
    assert reader.list({'size':'15'})['items'][0]['created_at']==NOW+1
    second=reader.list({'size':'15','cursor':first['pagination']['next_cursor']})
    assert first['pagination']['has_newer'] is False and second['pagination']['has_newer'] is True
    assert not ids & {x['id'] for x in second['items']}
    third=reader.list({'size':'15','cursor':second['pagination']['next_cursor']})
    assert len(third['items'])==2 and not third['pagination']['has_more'] and third['pagination']['next_cursor'] is None
    with pytest.raises(ValueError):reader.list({'q':'changed','cursor':first['pagination']['next_cursor']})


def test_newer_indicator_uses_same_member_filters_and_current_delivery_state(records):
    root,reader=records
    add(root,'signals:old-match',[event('AAA.US','5m')],created=NOW-100)
    add(root,'signals:cross-member-new',[event('AAA.US','1m'),event('BBB.US','5m')],created=NOW-10)
    query={'q':'AAA','frequency':'5m','status':'sent','as_of':str(NOW-50)}
    assert reader.list(query)['pagination']['has_newer'] is False
    add(root,'signals:new-match',[event('AAA.US','5m')],created=NOW-10)
    assert reader.list(query)['pagination']['has_newer'] is True
    assert reader.list({**query,'status':'failed'})['pagination']['has_newer'] is False


def test_newer_silent_event_is_not_reported_as_new_delivery(records):
    root,reader=records
    silent(root,event(recorded_at=NOW-10))
    query={'as_of':str(NOW-50)}
    assert reader.list(query)['pagination']['has_newer'] is False
    assert reader.list({**query,'status':'suppressed'})['pagination']['has_newer'] is True


def test_sql_limits_payload_decoding_to_one_page(records,monkeypatch):
    root,reader=records
    for i in range(37):add(root,f'signals:bounded-{i:02}',[event()],created=NOW-10-i)
    original=reader._receipt_item
    decoded=[]

    def project(row,options=None):
        decoded.append(row['id'])
        return original(row,options)

    monkeypatch.setattr(reader,'_receipt_item',project)
    result=reader.list({'size':'15','page':'2'})
    assert result['pagination']['total']==37 and len(result['items'])==15
    assert len(decoded)==16  # One page plus one lookahead, never all37 payloads.


def test_suppressed_unknown_metadata_stays_unknown_and_cannot_be_claimed_as_sent(records):
    root,reader=records
    value=event('NOT_RETAINED.US')
    silent(root,value)
    (root/'state.json').write_text('{}',encoding='utf-8')
    result=reader.list({'status':'suppressed'})
    item=result['items'][0]
    assert item['code'] is None and item['metadata_complete'] is False
    assert item['sent_at'] is None and item['delivery_status']=='suppressed'
    assert reader.list({'status':'sent'})['items']==[]
    detail=reader.detail(item['id'])
    assert detail['body']=='' and '历史记录没有保留完整标的信息。' in detail['record_notes']


@pytest.mark.parametrize('args',[{'size':'10000'},{'hours':'1'},{'status':'unknown'},{'page':'0'},
    {'q':'a'*101},{'cursor':'../bad'}, {'as_of':'nan'}, {'size':'25','page_size':'25'},
    MultiDict([('size','25'),('size','50')])])
def test_invalid_queries_fail_before_reading_source(records,args):
    with pytest.raises(ValueError):records[1].list(args)


def test_images_are_existing_local_assets_linked_to_the_message(records,monkeypatch):
    root,reader=records
    png=b'\x89PNG\r\n\x1a\nfixture'
    name=hashlib.sha256(png).hexdigest();path=root/'chart_images'/(name+'.png');path.parent.mkdir();path.write_bytes(png)
    add(root,'chart:fixture',[event()],kind='chart',content=json.dumps({'title':'原始图','text':f'![图](https://example.test/public/alert-chart/{name}.png?expires=1&signature=old)'}))
    item=reader.list({})['items'][0]; detail=reader.detail(item['id'])
    assert detail['images'][0]['status']=='ready'
    assert detail['images'][0]['url'].startswith('/monitor/notifications/')
    assert reader.image_path(item['id'],0,name)==path.resolve()
    assert reader.image_path(item['id'],0) is None
    # Resolved paths escaping chart_images (e.g. a symlink) cannot be served.
    original=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda p,*a,**kw: root/'private.txt' if p==path else original(p,*a,**kw))
    assert reader.image_path(item['id'],0,name) is None


def test_late_grouped_image_cannot_change_the_content_of_an_existing_cached_url(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root,reader=records
    first,second=event('FIRST.US'),event('SECOND.US')
    add(root,'signals:two-images',[first,second])
    receipt=reader.list({})['items'][0]['id']
    (root/'chart_images').mkdir()

    def publish(value,payload):
        digest=hashlib.sha256(payload).hexdigest()
        path=root/'chart_images'/(digest+'.png');path.write_bytes(payload)
        add(root,'chart:'+signal_key(value),[value],kind='chart',
            content=json.dumps({'text':f'![chart](https://example.test/public/alert-chart/{digest}.png?expires=old)'}))
        return digest,path

    second_hash,second_path=publish(second,b'\x89PNG\r\n\x1a\nsecond')
    old_url=reader.detail(receipt)['images'][0]['url']
    assert old_url.endswith('/images/0.png?version='+second_hash)
    first_hash,first_path=publish(first,b'\x89PNG\r\n\x1a\nfirst')
    current=reader.detail(receipt)['images']
    assert current[0]['url'].endswith('/images/0.png?version='+first_hash)
    assert current[1]['url'].endswith('/images/1.png?version='+second_hash)
    assert reader.image_path(receipt,0,second_hash)==second_path.resolve()
    assert reader.image_path(receipt,0,first_hash)==first_path.resolve()
    # Existing but unrelated files do not become accessible by guessing hashes.
    unrelated='f'*64
    (root/'chart_images'/(unrelated+'.png')).write_bytes(b'unrelated')
    assert reader.image_path(receipt,0,unrelated) is None


def chart_asset(root, payload, periods=None, missing=None):
    digest = hashlib.sha256(payload).hexdigest()
    directory = root / 'chart_images'; directory.mkdir(exist_ok=True)
    path = directory / (digest + '.png'); path.write_bytes(payload)
    if periods is not None:
        (directory / (digest + '.json')).write_text(json.dumps({
            'schema': 'notification-chart-coverage-v1', 'image_sha256': digest,
            'periods': periods, 'missing_periods': missing, 'primary_frequency': '1m',
        }), encoding='utf-8')
    return digest, f'https://example.test/public/alert-chart/{digest}.png?expires=old', path


def chart_job(root, value, status='pending', missing=('5m','30m')):
    from cl_app.services.dingtalk_notifications import signal_key
    with sqlite3.connect(root / 'chart_jobs.sqlite3') as db:
        db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT,
            error TEXT,attempts INTEGER,next_try REAL,missing_periods TEXT,present_periods TEXT)''')
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                   (signal_key(value), json.dumps(value), status, '', 1, NOW+30, json.dumps(missing), '["1m"]'))


def test_partial_png_keeps_its_original_coverage_after_a_separate_complete_receipt(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event(); key = signal_key(value)
    add(root, 'signals:partial-origin', [value])
    partial_hash, partial_url, partial_path = chart_asset(root, b'partial fixture', ['1m'], ['5m','30m'])
    add(root, 'chart:'+key, [value], kind='chart', content=json.dumps({'text': f'![partial]({partial_url})'}))
    chart_job(root, value)
    identity = next(row['id'] for row in reader.list({'kind':'signal'})['items'])
    first = reader.detail(identity)['images'][0]
    assert first['coverage_status'] == 'partial' and first['periods'] == ['1m']
    assert first['status'] == 'ready' and first['delivery_status'] == 'sent'
    assert first['generation_status'] == 'pending' and '补图等待重试' in first['status_note']
    with sqlite3.connect(root/'dingtalk_outbox.sqlite3') as db:
        original_receipt = db.execute('SELECT * FROM outbox WHERE id=?', ('chart:'+key,)).fetchone()
    full_hash, full_url, full_path = chart_asset(root, b'complete fixture', ['1m','5m','30m'], [])
    add(root, 'chart-complete:'+key, [value], kind='chart', status='pending',
        content=json.dumps({'title':'补全图表', 'text':f'![complete]({full_url})'}))
    with sqlite3.connect(root/'chart_jobs.sqlite3') as db:
        db.execute("UPDATE jobs SET status='ready',missing_periods='[]',present_periods=?", (json.dumps(['1m','5m','30m']),))
    result = reader.detail(identity); original, complete = result['images']
    assert original['coverage_status'] == 'partial' and original['missing_periods'] == ['5m','30m']
    assert complete['coverage_status'] == 'complete' and complete['role'] == 'completion'
    assert complete['delivery_status'] == 'pending' and complete['sent_at'] is None
    assert '补全图待发送' in complete['status_note']
    assert reader.image_path(identity, 0, partial_hash) == partial_path.resolve()
    assert reader.image_path(identity, 0, full_hash) == full_path.resolve()
    assert len(result['related_chart_receipts']) == 2
    listed = reader.list({'kind':'chart'})['items']
    assert {row['chart_coverage_status'] for row in listed} == {'partial','complete'}
    assert next(row for row in listed if row['chart_role']=='completion')['delivery_status'] == 'pending'
    with sqlite3.connect(root/'dingtalk_outbox.sqlite3') as db:
        assert db.execute('SELECT * FROM outbox WHERE id=?', ('chart:'+key,)).fetchone() == original_receipt


def test_complete_chart_links_to_the_actual_inline_first_receipt_without_relabelling_it(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event(inline_chart=True); key = signal_key(value)
    _, first_url, _ = chart_asset(root, b'inline partial', ['1m'], ['5m','30m'])
    add(root, 'signals:inline-origin', [value], content=json.dumps({'text':f'![first]({first_url})'}))
    with sqlite3.connect(root/'dingtalk_outbox.sqlite3') as db:
        db.execute('INSERT INTO inline_charts VALUES (?,?,?,?)', (key, first_url, NOW-45, 'signals:inline-origin'))
    _, complete_url, _ = chart_asset(root, b'inline complete', ['1m','5m','30m'], [])
    add(root, 'chart-complete:'+key, [value], kind='chart', content=json.dumps({'text':f'![full]({complete_url})'}))
    chart_job(root, value, status='ready', missing=())
    item = reader.list({'kind':'chart'})['items'][0]
    details = reader.detail(item['id'])
    assert len(details['images']) == 2
    assert {image['coverage_status'] for image in details['images']} == {'partial','complete'}
    assert {receipt['kind'] for receipt in details['related_chart_receipts']} == {'chart','signals'}
    assert all(image['delivery_status']=='sent' for image in details['images'])


@pytest.mark.parametrize('status,word', [('pending','等待重试'),('running','正在重试'),('failed','重试失败'),('expired','已过期')])
def test_sent_partial_image_remains_viewable_while_its_job_retries_or_stops(records, status, word):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event()
    _, url, _ = chart_asset(root, b'retry partial', ['1m'], ['5m','30m'])
    add(root, 'chart:'+signal_key(value), [value], kind='chart', content=json.dumps({'text':f'![partial]({url})'}))
    chart_job(root, value, status=status)
    detail = reader.detail(reader.list({})['items'][0]['id'])
    assert detail['delivery_status'] == 'sent'
    assert detail['images'][0]['url'] and detail['images'][0]['status'] == 'ready'
    assert word in detail['images'][0]['status_note']


def test_legacy_ready_job_does_not_prove_a_full_chart_and_unrelated_completion_is_not_linked(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event()
    digest, url, _ = chart_asset(root, b'legacy unknown')
    add(root, 'chart:'+signal_key(value), [value], kind='chart', content=json.dumps({'text':f'![chart]({url})'}))
    chart_job(root, value, status='ready', missing=())
    other = event('OTHER.US'); _, other_url, _ = chart_asset(root, b'other complete', ['1m','5m','30m'], [])
    add(root, 'chart-complete:'+signal_key(other), [other], kind='chart', content=json.dumps({'text':f'![other]({other_url})'}))
    identity = reader.list({'q':'AAA'})['items'][0]['id']; result = reader.detail(identity)
    assert len(result['images']) == 1 and result['images'][0]['coverage_status'] == 'unknown'
    assert '周期完整性未记录' in result['images'][0]['status_note']
    metadata = root/'chart_images'/(digest+'.json')
    metadata.write_text(json.dumps({'schema':'notification-chart-coverage-v1','image_sha256':'f'*64,
        'periods':['1m','5m','30m'],'missing_periods':[],'primary_frequency':'1m'}),encoding='utf-8')
    assert reader.detail(identity)['images'][0]['coverage_status'] == 'unknown'


def test_grouped_receipt_keeps_another_members_unfinished_chart_visible(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; first, second = event('FIRST.US'), event('SECOND.US')
    add(root, 'signals:grouped-charts', [first, second])
    _, url, _ = chart_asset(root, b'first only', ['1m','5m','30m'], [])
    add(root, 'chart:'+signal_key(first), [first], kind='chart', content=json.dumps({'text':f'![first]({url})'}))
    chart_job(root, second, status='running')
    identity = reader.list({'kind':'signal'})['items'][0]['id']
    details = reader.detail(identity)
    images = details['images']
    assert len(images) == 2 and images[0]['url']
    assert details['image_count'] == 1
    assert images[1]['is_placeholder'] and images[1]['generation_status'] == 'running'
    assert 'SECOND.US' in images[1]['label'] and images[1]['url'] is None


def test_chart_progress_read_failure_cannot_hide_an_actual_sent_image(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event()
    _, url, _ = chart_asset(root, b'saved before failure', ['1m'], ['5m','30m'])
    add(root, 'chart:'+signal_key(value), [value], kind='chart', content=json.dumps({'text':f'![chart]({url})'}))
    (root/'chart_jobs.sqlite3').write_bytes(b'unreadable chart progress')
    detail = reader.detail(reader.list({})['items'][0]['id'])
    assert detail['delivery_status'] == 'sent' and detail['images'][0]['url']
    assert detail['images'][0]['coverage_status'] == 'partial'
    assert detail['images'][0]['generation_status'] == 'unavailable'


def test_real_chart_writer_and_reader_preserve_first_receipt_across_completion(tmp_path):
    from cl_app.services.dingtalk_notifications import DingTalkOutbox
    from cl_app.services.notification_charts import RenderedChart
    from tests.web.test_notification_charts import ImageTransport, image_event, PNG
    clock = [float(NOW)]
    transport = ImageTransport()
    box = DingTalkOutbox(tmp_path/'dingtalk_outbox.sqlite3', transport=transport, charts=True,
                        now=lambda: clock[0], signal_validator=lambda rows: (rows, []))
    reader = RecentNotifications(tmp_path, now=lambda: clock[0])
    value = {**image_event(stamp=NOW), 'frequency': '1m'}
    box.charts.check_public = False  # Fixture transport and local PNGs only.
    box.charts.renderer = lambda *_: RenderedChart(PNG, periods=['1m'], missing_periods=['5m','30m'], primary_frequency='1m')
    box.signal_events([value], since=0)
    box.deliver_one()
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    first = reader.list({'kind':'chart'})['items'][0]
    original = reader.detail(first['id'])
    assert first['delivery_status'] == 'sent' and first['chart_coverage_status'] == 'partial'
    assert original['images'][0]['generation_status'] == 'pending'
    first_url = original['images'][0]['url']
    box.charts.renderer = lambda *_: RenderedChart(PNG+b'complete', periods=['1m','5m','30m'], primary_frequency='1m')
    clock[0] += 31
    box.charts.process_one()
    refreshed = reader.detail(first['id'])
    assert refreshed['images'][0]['url'] == first_url
    assert refreshed['images'][0]['coverage_status'] == 'partial'
    assert refreshed['images'][1]['coverage_status'] == 'complete'
    assert refreshed['images'][1]['delivery_status'] == 'suppressed'
    box.deliver_one()
    final = reader.detail(first['id'])
    assert final['images'][1]['delivery_status'] == 'suppressed'
    assert len(transport.images) == 1
    box.charts.process_one(); box.deliver_one()
    assert len(transport.images) == 1 and reader.list({'kind':'chart'})['pagination']['total'] == 2


def test_three_available_periods_are_still_partial_when_the_requested_history_is_pending(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event()
    digest, url, _ = chart_asset(root, b'three short periods', ['1m','5m','30m'], [])
    metadata = root/'chart_images'/(digest+'.json')
    saved = json.loads(metadata.read_text(encoding='utf8'))
    ranges = {p: {'state':'target_reached','target_reached':True,'target_days':30 if p=='1m' else 90,
                  'available_from':NOW-86400,'available_to':NOW-60} for p in ('1m','5m','30m')}
    ranges['1m'].update(state='pending',target_reached=False,private_path='D:/must-not-expose',cursor={'secret':'no'})
    saved.update(schema='notification-chart-coverage-v2',history_coverage=ranges)
    metadata.write_text(json.dumps(saved),encoding='utf8')
    add(root,'chart:'+signal_key(value),[value],kind='chart',content=json.dumps({'text':f'![short]({url})'}))
    item=reader.list({})['items'][0]
    assert item['chart_coverage_status']=='partial' and item['chart_missing_periods']==[]
    assert item['chart_missing_history_periods']==['1m'] and item['chart_history_status']=='pending'
    image=reader.detail(item['id'])['images'][0]
    assert image['history_coverage']['1m']['state']=='pending'
    assert '历史范围尚未完成：1m' in image['status_note']
    assert 'private_path' not in image['history_coverage']['1m'] and 'cursor' not in image['history_coverage']['1m']


def test_source_exhaustion_is_distinct_from_meeting_the_requested_duration(records):
    from cl_app.services.dingtalk_notifications import signal_key
    root, reader = records; value = event()
    digest,url,_=chart_asset(root,b'actual short source',['1m','5m','30m'],[])
    metadata=root/'chart_images'/(digest+'.json');saved=json.loads(metadata.read_text(encoding='utf8'))
    ranges={p:{'state':'source_exhausted','source_origin':True,'target_reached':False,
               'target_days':30 if p=='1m' else 90,'source_label':'QMT 本机可用历史'} for p in ('1m','5m','30m')}
    saved.update(schema='notification-chart-coverage-v2',history_coverage=ranges)
    metadata.write_text(json.dumps(saved),encoding='utf8')
    add(root,'chart:'+signal_key(value),[value],kind='chart',content=json.dumps({'text':f'![source]({url})'}))
    item=reader.list({})['items'][0];image=reader.detail(item['id'])['images'][0]
    assert item['chart_history_status']=='complete' and item['chart_coverage_status']=='complete'
    assert image['history_coverage']['1m']['target_reached'] is False
    assert 'QMT 本机可用历史已取尽' in image['status_note'] and '不足目标范围' in image['status_note']


def test_history_routes_require_login_and_do_not_dispatch(records,monkeypatch):
    from cl_app import create_app
    root,reader=records
    add(root,'signals:login',[event()])
    monkeypatch.setattr('cl_app.blueprints.monitor.service',SimpleNamespace(root=root))
    def read_fixture(path):
        assert Path(path) == root
        return reader
    monkeypatch.setattr('cl_app.blueprints.monitor.reader_for',read_fixture)
    app=create_app(test_config={'TESTING':True,'VALIDATE_WEB_SECURITY':False,'LOGIN_DISABLED':False})
    client=app.test_client()
    assert client.get('/monitor/notifications').status_code in (302,401)
    assert client.get('/monitor/notifications/d.c2lnbmFsczpsb2dpbg').status_code in (302,401)
    assert client.get('/monitor/notifications/d.c2lnbmFsczpsb2dpbg/images/0.png').status_code in (302,401)
    app.config['LOGIN_DISABLED']=True  # Isolated fixture app only.
    response=client.get('/monitor/notifications?size=15')
    assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
    identity=response.get_json()['items'][0]['id']
    assert client.get('/monitor/notifications/'+identity).get_json()['body']=='保存的通知原文'
    assert client.get('/monitor/notifications/'+identity+'/images/0.png').status_code==404
    assert client.get('/monitor/notifications?hours=bad').status_code==400


def test_empty_installation_has_no_fake_receipts(tmp_path):
    before=set(tmp_path.iterdir())
    result=RecentNotifications(tmp_path,now=lambda:NOW).list({})
    assert result['items']==[] and result['pagination']['total']==0
    assert set(tmp_path.iterdir())==before


def test_history_get_does_not_migrate_or_mutate_delivery_tables(records):
    root,reader=records
    add(root,'signals:readonly',[event()])
    path=root/'dingtalk_outbox.sqlite3'
    with sqlite3.connect(path) as db:
        before=db.execute('select type,name,sql from sqlite_master order by name').fetchall()
        rows=db.execute('select * from outbox').fetchall()
    reader.list({})
    with sqlite3.connect(path) as db:
        assert db.execute('select type,name,sql from sqlite_master order by name').fetchall()==before
        assert db.execute('select * from outbox').fetchall()==rows


def test_writer_bootstrap_adds_history_indexes_without_changing_receipts(records):
    from cl_app.services.dingtalk_notifications import DingTalkOutbox
    root,_=records
    add(root,'signals:migration',[event()])
    path=root/'dingtalk_outbox.sqlite3'
    with sqlite3.connect(path) as db:
        before=db.execute('select * from outbox').fetchall()
    box=DingTalkOutbox(path,transport=SimpleNamespace(),now=lambda:NOW)
    with box._connect() as db:
        assert [tuple(row) for row in db.execute('select * from outbox')]==before
        plain=' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN SELECT id FROM outbox WHERE created>=? ORDER BY created DESC,id DESC LIMIT 25',(NOW-86400,)))
        filtered=' '.join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT id FROM outbox WHERE status='sent' AND created>=? ORDER BY created DESC,id DESC LIMIT 25",(NOW-86400,)))
        assert 'outbox_monitor_created' in plain
        assert 'outbox_monitor_status_created' in filtered
        effective="CASE WHEN status='sent' THEN COALESCE(sent,created) ELSE created END"
        ordered=' '.join(row[3] for row in db.execute(f'EXPLAIN QUERY PLAN SELECT id FROM outbox WHERE ({effective})>=? ORDER BY ({effective}) DESC,id DESC LIMIT 25',(NOW-86400,)))
        selected=' '.join(row[3] for row in db.execute(f"EXPLAIN QUERY PLAN SELECT id FROM outbox WHERE status='sent' AND ({effective})>=? ORDER BY ({effective}) DESC,id DESC LIMIT 25",(NOW-86400,)))
        assert 'outbox_monitor_effective_time' in ordered
        assert 'outbox_monitor_status_effective_time' in selected


def test_recent_delivery_window_includes_old_created_message_sent_now(records):
    root,reader=records
    add(root,'signals:recent-created',[event()],created=NOW-100)
    add(root,'signals:delayed',[event('DELAYED.US')],created=NOW-2*86400)
    add(root,'signals:pending-old',[event()],created=NOW-2*86400,status='pending')
    with sqlite3.connect(root/'dingtalk_outbox.sqlite3') as db:
        db.execute("UPDATE outbox SET sent=? WHERE id='signals:delayed'",(NOW-2,))
    result=reader.list({'hours':'24'})
    assert result['pagination']['total']==2 and result['summary']['sent']==2
    delayed=result['items'][0]
    assert delayed['code']=='DELAYED.US'
    assert delayed['created_at']==NOW-2*86400
    assert delayed['sent_at']==delayed['sort_at']==NOW-2
    assert result['window']['time_basis']==result['pagination']['order']=='sent_at_for_sent_else_created_at'
    assert [x['sort_at'] for x in result['items']]==sorted((x['sort_at'] for x in result['items']),reverse=True)
