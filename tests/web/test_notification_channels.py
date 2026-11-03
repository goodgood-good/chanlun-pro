"""Exercise real routing with isolated settings and a fake DingTalk endpoint."""
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from cl_app.services.dingtalk_notifications import DingTalkOutbox, DeliveryError
from cl_app.services.notification_channels import DingTalkChannels
from tests.web.test_dingtalk_notifications import event, result


def address(token):
    return 'https://oapi.dingtalk.com/robot/send?access_token=' + token


@pytest.fixture(autouse=True)
def destinations(monkeypatch):
    for prefix in ('CHANLUN_DINGTALK_', 'CHANLUN_DINGTALK_HOLDINGS_'):
        for field in ('WEBHOOK', 'SECRET', 'KEYWORD'):
            monkeypatch.delenv(prefix + field, raising=False)


@pytest.fixture
def routed(tmp_path, monkeypatch):
    calls, clock, failures = [], [1000.], set()

    def post(url, **kwargs):
        token = parse_qs(urlsplit(url).query)['access_token'][0]
        body = json.loads(kwargs['data'].decode('utf-8'))
        calls.append((token, body, url))
        response = Mock(status_code=200)
        response.json.return_value = {'errcode': 310000 if token in failures else 0}
        return response

    session = Mock()
    session.post.side_effect = post
    manager = Mock(__enter__=Mock(return_value=session), __exit__=Mock(return_value=False))
    monkeypatch.setattr(requests, 'Session', lambda: manager)
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite3', now=lambda: clock[0])
    box.channels.save({'holdings': {'webhook': address('held-test-token'), 'keyword': '持仓通道', 'secret': 'held-secret'},
                       'others': {'webhook': address('ordinary-test-token'), 'keyword': '普通通道'}})
    return box, calls, clock, failures


def drain(box, clock, count=8):
    for _ in range(count):
        box.deliver_one()
        clock[0] += 5


def test_stock_risk_charts_and_task_receipts_use_independent_destinations(routed):
    box, calls, clock, _ = routed
    held = {**event('confirmed', identity='held'), 'holding': True, 'name': '持仓样例'}
    ordinary = {**event('confirmed', identity='ordinary'), 'code': 'SZ.000001', 'name': '其他样例'}
    risk = {**held, 'id': 'risk', 'signal_type': 'holding_risk', 'point_type': 'risk_stop',
            'stage': 'triggered', 'stop_price': 10, 'trigger_price': 9.9}
    box.signal_events([held, ordinary, risk], since=0)
    box.screening_completed(result(), [])
    box.enqueue_chart(held, 'https://example.test/held.png')
    box.enqueue_chart(ordinary, 'https://example.test/ordinary.png')
    drain(box, clock)
    assert len(calls) == 6
    held_calls = [body for token, body, _ in calls if token == 'held-test-token']
    other_calls = [body for token, body, _ in calls if token == 'ordinary-test-token']
    assert len(held_calls) == len(other_calls) == 3
    assert '其他样例' not in json.dumps(held_calls, ensure_ascii=False)
    assert '持仓样例' not in json.dumps(other_calls, ensure_ascii=False)
    assert '选股完成' in json.dumps(other_calls, ensure_ascii=False)
    assert all('sign=' in url for token, _, url in calls if token == 'held-test-token')
    assert all('sign=' not in url for token, _, url in calls if token == 'ordinary-test-token')
    assert all('普通通道' not in json.dumps(body, ensure_ascii=False) for body in held_calls)


def test_new_holding_splits_a_queued_batch_and_retry_after_restart_never_resends_other_route(routed):
    box, calls, clock, failures = routed
    rows = [{**event('confirmed', identity=key), 'code': code, 'name': key}
            for key, code in [('held-now', 'SH.600088'), ('ordinary', 'SZ.000001')]]
    box.signal_events(rows, since=0)
    assert box.snapshot()['pending'] == 1
    validator = lambda events: ([{**row, 'holding': row['id'] == 'held-now'} for row in events], [])
    box.signal_validator = validator
    box.deliver_one()
    assert calls[0][0] == 'held-test-token'
    assert 'ordinary' not in json.dumps(calls[0][1])
    clock[0] += 5
    failures.add('ordinary-test-token')
    box.deliver_one()
    failures.clear()
    clock[0] += 20
    restarted = DingTalkOutbox(box.path, now=lambda: clock[0], signal_validator=validator)
    drain(restarted, clock)
    restarted.signal_events(rows, since=0)
    drain(restarted, clock)
    assert [token for token, _, _ in calls] == ['held-test-token', 'ordinary-test-token', 'ordinary-test-token']
    assert restarted.snapshot()['sent'] == 2
    assert restarted.snapshot()['pending'] == 0


@pytest.mark.parametrize('chart', [False, True])
@pytest.mark.parametrize('current_holding', [False, True])
def test_membership_changes_route_using_fresh_validation_including_charts(routed, chart, current_holding):
    box, calls, clock, _ = routed
    row = {**event('confirmed'), 'holding': not current_holding}
    if chart:
        box.enqueue_chart(row, 'https://example.test/chart.png')
    else:
        box.signal_events([row], since=0)
    box.signal_validator = lambda events: ([{**e, 'holding': current_holding} for e in events], [])
    drain(box, clock)
    assert [token for token, _, _ in calls] == ['held-test-token' if current_holding else 'ordinary-test-token']


def test_inline_consolidation_image_uses_the_holdings_robot(routed):
    box, calls, clock, _ = routed
    box.charts = Mock()
    row = {**event('warning'), 'signal_type': 'consolidation_divergence',
           'point_type': 'consolidation_down', 'direction': 'down', 'inline_chart': True}
    box.signal_events([row], since=0)
    box.enqueue_chart(row, 'https://example.test/consolidation.png')
    drain(box, clock)
    assert len(calls) == 1 and calls[0][0] == 'held-test-token'
    assert calls[0][1]['msgtype'] == 'markdown'
    assert 'consolidation.png' in calls[0][1]['markdown']['text']


def test_missing_holding_address_never_falls_back_and_does_not_block_other_route(routed):
    box, calls, clock, _ = routed
    box.channels.save({'holdings': {'webhook': ''}})
    box.signal_events([{**event('confirmed', identity='held'), 'holding': True},
                       {**event('confirmed', identity='other'), 'code': 'SZ.000001'}], since=0)
    box.deliver_one()
    assert not calls
    box.deliver_one()
    assert [token for token, _, _ in calls] == ['ordinary-test-token']
    box.channels.save({'holdings': {'webhook': address('new-held-token')}})
    clock[0] += 16
    box.deliver_one()
    assert calls[-1][0] == 'new-held-token'
    assert box.snapshot()['sent'] == 2


def test_channel_changes_are_reloaded_without_recreating_transport_and_keep_secrets_private(routed):
    box, calls, _, _ = routed
    transport = box.transport.for_channel('holdings')
    status = box.channels.snapshot()
    assert 'held-test-token' not in json.dumps(status)
    assert 'held-secret' not in json.dumps(status)
    DingTalkChannels(box.channels.path).save({'holdings': {'webhook': address('replacement-token')}})
    transport.send('消息')
    assert calls[0][0] == 'replacement-token'
    assert 'sign=' in calls[0][2]
    assert box.channels.read()['holdings']['secret'] == 'held-secret'


@pytest.mark.parametrize('body', [None, {}, {'unexpected': {}}, {'holdings': []},
    {'holdings': {'enabled': True}}, {'holdings': {'webhook': False}},
    {'holdings': {'webhook': 'https://evil.test/robot/send?access_token=secret'}},
    {'holdings': {'webhook': address('x') + '&access_token=y'}},
    {'holdings': {'webhook': address('ordinary-test-token')}},
    {'holdings': {'keyword': 'a' * 81}}])
def test_invalid_settings_do_not_modify_existing_configuration(routed, body):
    box, calls, _, _ = routed
    before = box.channels.path.read_bytes()
    with pytest.raises(ValueError):
        box.channels.save(body)
    assert box.channels.path.read_bytes() == before
    assert not calls


def test_failed_atomic_save_keeps_old_settings_and_corrupt_settings_do_not_use_environment(routed, monkeypatch):
    box, calls, _, _ = routed
    original = box.channels.path.read_bytes()
    monkeypatch.setattr('cl_app.services.notification_channels.os.replace', Mock(side_effect=OSError('disk')))
    with pytest.raises(OSError):
        box.channels.save({'holdings': {'keyword': '新关键词'}})
    assert box.channels.path.read_bytes() == original
    box.channels.path.write_text('{', encoding='utf-8')
    monkeypatch.setenv('CHANLUN_DINGTALK_WEBHOOK', address('fallback-token'))
    with pytest.raises(DeliveryError, match='暂不可读取'):
        box.transport.send('消息')
    assert not calls


def test_existing_ordinary_environment_settings_survive_saving_holdings(tmp_path, monkeypatch):
    monkeypatch.setenv('CHANLUN_DINGTALK_WEBHOOK', address('original-token'))
    monkeypatch.setenv('CHANLUN_DINGTALK_SECRET', 'original-secret')
    settings = DingTalkChannels(tmp_path / 'channels.json')
    settings.save({'holdings': {'webhook': address('holding-token')}})
    assert settings.read()['others']['webhook'] == address('original-token')
    assert settings.read()['others']['secret'] == 'original-secret'
    assert settings.read()['holdings']['secret'] == ''


def test_authenticated_configuration_api_is_masked_and_persistent(routed, monkeypatch):
    from cl_app import create_app
    box, calls, _, _ = routed
    service = Mock(notifications=box)
    monkeypatch.setattr('cl_app.blueprints.monitor.service', service)
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True, 'WTF_CSRF_ENABLED': False,
                                 'VALIDATE_WEB_SECURITY': False})
    client = app.test_client()
    response = client.get('/monitor/notification-channels')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert 'held-test-token' not in response.text and 'held-secret' not in response.text
    response = client.post('/monitor/notification-channels', json={'holdings': {'keyword': '新关键词'}})
    assert response.status_code == 200 and response.json['channels']['holdings']['keyword'] == '新关键词'
    assert DingTalkChannels(box.channels.path).read()['holdings']['keyword'] == '新关键词'
    assert client.post('/monitor/notification-channels', json={'holdings': {'webhook': 'invalid'}}).status_code == 400
    assert not calls
    service.start_runtime.assert_not_called()
    service.configure_notifications.assert_not_called()


def test_notification_address_endpoints_require_login_and_csrf(monkeypatch):
    from cl_app import create_app
    service = Mock()
    monkeypatch.setattr('cl_app.blueprints.monitor.service', service)
    app = create_app(test_config={'TESTING': True, 'VALIDATE_WEB_SECURITY': False})
    client = app.test_client()
    assert client.get('/monitor/notification-channels').status_code in (302, 401)
    assert client.post('/monitor/notification-channels', json={'holdings': {'keyword': 'x'}}).status_code in (400, 401)
    app.config['LOGIN_DISABLED'] = True
    assert client.post('/monitor/notification-channels', json={'holdings': {'keyword': 'x'}}).status_code == 400
    service.notifications.channels.save.assert_not_called()
