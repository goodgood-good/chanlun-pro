import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest

from chanlun.screening.runner import _save_evidence
from chanlun.screening.evidence import evidence_paths
from cl_app.services.notification_evidence import NotificationEvidence
from cl_app.services.notification_viewer import NotificationViewer, ChartUnavailable, ChartChanged, event_version, current_chart_settings
from cl_app.services.recent_notifications import _pack
from tests.web.test_recent_notifications import records as records, add, NOW
from tests.web.test_notification_multi_period import source


def frozen(root, code='SZ.002611', frequency='5m'):
    frame, snapshot, marker = source(frequency, NOW - 60)
    point = {'point_id':'point-' + code, 'point_type':'1buy', 'status':'confirmed',
             'anchor_at':marker['anchor_at'], 'anchor_tick':1100}
    snapshot.update(symbol=code, analysis_scope='native_centers', levels=[{'centers':[], 'points':[point]}])
    manifest = _save_evidence(root / 'runs' / ('e' * 32), code, frequency, frame, snapshot)
    event = {'id':code + frequency, 'market':'a', 'code':code, 'name':'历史样本', 'frequency':frequency,
             'signal_type':'buy_sell_point',
             'stage':'confirmed', 'point_status':'confirmed', 'point_type':'1buy', 'point_id':point['point_id'],
             'source_closed_at':NOW - 60, 'recorded_at':NOW - 50, 'available_at':NOW - 60,
             'anchor_at':marker['anchor_at'], 'anchor_price':11., 'evidence_run_id':'e' * 32, 'evidence':manifest}
    return event, snapshot


def test_chart_links_exist_without_a_generated_image_and_bind_each_batch_member(records):
    root, reader = records
    first, _ = frozen(root)
    second, _ = frozen(root, 'SH.600000', '1m')
    add(root, 'signals:batch', [first, second])
    detail = reader.detail('d.' + _pack('signals:batch'))
    assert detail['images'] == [] and len(detail['charts']) == 2
    assert [c['frequency'] for c in detail['charts']] == ['5m', '1m']
    assert all(c['available'] and '/charts/' in c['url'] for c in detail['charts'])
    assert reader.chart_event(detail['id'], 1)['code'] == 'SH.600000'
    assert reader.chart_event(detail['id'], 2) is None
    assert 'evidence_run_id' not in json.dumps(detail['charts'])


def test_archived_chart_is_readable_after_original_run_is_pruned_and_never_fetches_live_quotes(records, monkeypatch):
    root, _ = records
    event, snapshot = frozen(root)
    archive = NotificationEvidence(root / 'notification_evidence', now=lambda:NOW)
    archive.capture(event, root / 'runs', NOW)
    for path in evidence_paths(root / 'runs' / event['evidence_run_id'], event['code'], event['frequency']):
        path.unlink()
    monkeypatch.setattr('chanlun.screening.runner.snapshot_for', lambda *_a, **_k:pytest.fail('must not recompute historical structures'))
    viewer = NotificationViewer(root, now=lambda:NOW)
    version, panel = viewer.panel(event)
    assert viewer.available(event) and version == event_version(event)
    assert panel['history']['t'][-1] == event['source_closed_at']
    assert panel['history']['higher_macd_dif'] == snapshot['chart']['higher_macd_dif']
    assert panel['settings']['interactive'] is True
    assert panel['settings']['from'] >= panel['settings']['history_from']
    changed = {**event, 'anchor_price':12.}
    with pytest.raises(ChartChanged):
        viewer.panel(changed, version=version)
    with pytest.raises(ChartUnavailable):
        viewer.panel(changed)
    with pytest.raises(ChartUnavailable):
        viewer.panel({**event, 'point_id':'another-point-at-the-same-price'})


def test_chart_renderer_loads_the_exact_archive_after_transient_run_pruning(records, monkeypatch):
    import time
    from cl_app.services.notification_charts import load_chart_evidence
    root, _ = records
    event, original = frozen(root)
    archive=NotificationEvidence(root/'notification_evidence')
    archive.capture(event,root/'runs',time.time())
    for path in evidence_paths(root/'runs'/event['evidence_run_id'],event['code'],event['frequency']):
        path.unlink()
    monkeypatch.setattr('chanlun.screening.runner.snapshot_for',lambda *_:pytest.fail('must use saved proof'))
    frame,snapshot,signal=load_chart_evidence(event,root/'runs')
    assert snapshot==original and int(frame.date.max().timestamp())==event['source_closed_at']
    assert signal['point_id']==event['point_id']
    with pytest.raises((ValueError,OSError)):
        load_chart_evidence({**event,'anchor_price':99.},root/'runs')


def test_notifications_without_saved_evidence_still_locate_the_current_chart(records):
    root, reader = records
    event = {'id':'missing-chart', 'market':'us', 'code':'QQQ.US', 'frequency':'30m',
             'stage':'invalidated', 'point_type':'3buy', 'recorded_at':NOW - 50}
    add(root, 'signals:no-saved-data', [event])
    detail = reader.detail('d.' + _pack('signals:no-saved-data'))
    assert not detail['images']
    chart = detail['charts'][0]
    assert chart['market'] == 'us' and chart['available'] is False and chart['url'] is None
    query = parse_qs(urlsplit(chart['current_url']).query)
    assert query == {'market':['us'], 'code':['QQQ.US'], 'frequency':['30m'],
                     'layout':['single'], 'workspace_layout':['1']}
    assert '/current-chart/0?version=' in chart['view_url']


def test_current_chart_uses_exact_receipt_annotations_without_historical_evidence(records, monkeypatch):
    from cl_app import create_app
    from cl_app.blueprints import monitor
    root, reader = records
    event = {'id':'live-annotation', 'market':'us', 'code':'QQQ.US', 'frequency':'5m',
             'stage':'approaching', 'point_type':'3buy', 'recorded_at':NOW-80,
             'anchor_at':NOW-600, 'anchor_price':123.5, 'source_closed_at':NOW-60}
    add(root, 'signals:current', [event])
    identity = 'd.' + _pack('signals:current')
    detail = reader.detail(identity)
    settings = current_chart_settings(detail['signals'][0], detail)
    assert settings['anchor_at'] == NOW-600 and settings['anchor_price'] == 123.5
    assert settings['notification_at'] == NOW-45 and settings['notification_label'] == '通知发出'
    assert settings['stage_label'] == '预警未确认' and settings['resolution'] == '5'
    monkeypatch.setattr(monitor, 'service', SimpleNamespace(root=root))
    monkeypatch.setattr(monitor, 'reader_for', lambda _root:reader)
    monkeypatch.setattr(reader.chart_viewer, 'panel', lambda *_a, **_kw:pytest.fail('current chart must not load frozen evidence'))
    app = create_app({'TESTING':True, 'VALIDATE_WEB_SECURITY':False, 'LOGIN_DISABLED':False})
    client = app.test_client()
    url = detail['charts'][0]['view_url']
    assert client.get(url).status_code in (302, 401)
    app.config['LOGIN_DISABLED'] = True
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Frame-Options'] == 'SAMEORIGIN'
    html = response.get_data(as_text=True)
    assert 'notification_current_chart.js' in html and '"anchor_price": 123.5' in html
    assert '<nav' not in html and '<header' not in html and 'notification-chart-source' not in html
    assert client.get(url.replace(event_version(settings), '0'*64)).status_code == 409
    assert client.get(url.replace('/current-chart/0', '/current-chart/7')).status_code == 404


def test_unsent_and_incomplete_records_do_not_invent_delivery_or_reference_points():
    settings = current_chart_settings({'market':'a', 'code':'SZ.301004', 'frequency':'5m',
                                       'anchor_price':float('nan')}, {'status':'pending', 'created_at':NOW})
    assert settings['notification_at'] == NOW and settings['notification_label'] == '信号记录'
    assert settings['anchor_at'] is None and settings['anchor_price'] is None


def test_run_files_alone_do_not_make_an_event_without_an_evidence_manifest_viewable(records):
    root, reader = records
    event, _ = frozen(root)
    event['evidence'] = None
    add(root, 'signals:missing-manifest', [event])
    chart = reader.detail('d.' + _pack('signals:missing-manifest'))['charts'][0]
    assert chart['available'] is False and chart['url'] is None and chart['current_url']
    with pytest.raises(ChartUnavailable):
        reader.chart_viewer.panel(event)


def test_expired_missing_and_corrupt_sources_cannot_be_replaced_with_current_market_data(records):
    root, _ = records
    event, _ = frozen(root)
    archive = NotificationEvidence(root / 'notification_evidence', now=lambda:NOW)
    key = archive.capture(event, root / 'runs', NOW)
    record = archive.read(key)
    for path in evidence_paths(root / 'runs' / event['evidence_run_id'], event['code'], event['frequency']):
        path.unlink()
    viewer = NotificationViewer(root, now=lambda:NOW + 8*86400)
    assert viewer.available(event) is False
    with pytest.raises(ChartUnavailable):
        viewer.panel(event)
    obj = archive.root / 'objects' / record['evidence'][0]['objects']['snapshot']
    obj.write_bytes(b'corrupted')
    with pytest.raises(ChartUnavailable):
        NotificationViewer(root, now=lambda:NOW).panel(event)


def test_chart_endpoints_require_login_and_exact_receipt_version_symbol_and_period(records, monkeypatch):
    from cl_app import create_app
    from cl_app.blueprints import monitor
    root, reader = records
    event, _ = frozen(root)
    add(root, 'signals:view', [event])
    identity = 'd.' + _pack('signals:view')
    monkeypatch.setattr(monitor, 'service', SimpleNamespace(root=root))
    monkeypatch.setattr(monitor, 'reader_for', lambda _root:reader)
    app = create_app({'TESTING':True, 'VALIDATE_WEB_SECURITY':False, 'LOGIN_DISABLED':False})
    client = app.test_client()
    assert client.get(f'/monitor/notifications/{identity}/charts/0').status_code in (302,401)
    app.config['LOGIN_DISABLED'] = True
    descriptor = reader.detail(identity)['charts'][0]
    page = client.get(descriptor['url'] + '&embedded=1')
    assert page.status_code == 200 and page.headers['Cache-Control'] == 'private, no-store'
    assert page.headers['X-Frame-Options'] == 'SAMEORIGIN'
    assert '<script nonce="' in page.get_data(as_text=True)
    assert 'notification-view-status' in page.get_data(as_text=True)
    version = parse_qs(urlsplit(descriptor['url']).query)['version'][0]
    base = f'/monitor/notifications/{identity}/charts/0/{version}/tv/'
    assert client.get(base + 'config').get_json()['supported_resolutions'] == ['5']
    assert client.get(base + 'symbols?symbol=a:SZ.002611').status_code == 200
    assert client.get(base + 'symbols?symbol=a:SH.600000').status_code == 404
    history = client.get(base + 'history?symbol=a:SZ.002611&resolution=5&firstDataRequest=true')
    assert history.status_code == 200 and history.get_json()['t'][-1] == NOW - 60
    monkeypatch.setattr('cl_app.blueprints.tv._TV_HISTORY_GZIP_MIN_BYTES', 1)
    compressed = client.get(base + 'history?symbol=a:SZ.002611&resolution=5&firstDataRequest=true',
                            headers={'Accept-Encoding':'gzip'})
    assert compressed.headers.get('Content-Encoding') == 'gzip'
    import gzip
    assert json.loads(gzip.decompress(compressed.data))['t'][-1] == NOW - 60
    assert client.get(base + 'history?symbol=a:SZ.002611&resolution=1&firstDataRequest=true').status_code == 404
    assert client.get(base + 'history?symbol=a:SZ.002611&resolution=5&firstDataRequest=false').get_json() == {'s':'no_data'}
    assert client.get(base.replace(version, '0'*64) + 'config').status_code == 409
    assert client.get(f'/monitor/notifications/{identity}/charts/8').status_code == 404
    with sqlite3.connect(root / 'dingtalk_outbox.sqlite3') as db:
        before = db.execute('SELECT * FROM outbox').fetchall()
    client.get(descriptor['url'])
    with sqlite3.connect(root / 'dingtalk_outbox.sqlite3') as db:
        assert db.execute('SELECT * FROM outbox').fetchall() == before


def test_silent_chart_uses_only_the_exact_saved_occurrence(records):
    from tests.web.test_recent_notifications import silent
    root, reader = records
    event, _ = frozen(root)
    silent(root, event)
    descriptor = reader.detail('s.' + 'a'*64)['charts'][0]
    assert descriptor['available']
    state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
    state['events'][0]['recorded_at'] += 60
    (root / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    assert reader.chart_event('s.' + 'a'*64, 0) is None


def test_native_preview_invalidation_chart_marks_the_check_and_preserves_original_candidate():
    from cl_app.services.tradingview_snapshot import _prepare_panel
    frame, snapshot, marker = source('5m', NOW - 60)
    event = {'market':'a', 'code':'SZ.002611', 'frequency':'5m', 'source_closed_at':NOW - 60,
             'point_type':'1buy', 'stage':'invalidated', 'signal_type':'native_point_preview',
             'anchor_price':10., 'invalidation_reason':'原比较结构已替换'}
    marker.update(anchor_at=NOW - 60, reference_anchor_at=NOW - 360,
                  reference_anchor_price=10., reference_basis_current=True)
    panel = _prepare_panel(event, frame, snapshot, marker)
    assert panel['settings']['marker'] and panel['settings']['anchor_price'] == 11.
    assert panel['settings']['reference_anchor_price'] == 10.
    assert panel['settings']['invalidation_reason'] == event['invalidation_reason']
    assert '预警失效复核' in panel['settings']['label']
