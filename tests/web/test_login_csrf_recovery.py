"""A stale login form preserves its safe destination, never its password."""

from html.parser import HTMLParser

import pytest
from werkzeug.security import generate_password_hash

import cl_app
from chanlun.security import WebLoginAccount


PASSWORD = 'isolated-login-recovery-password'


class LoginFields(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.inputs = {}
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name'):
            self.inputs[values['name']] = values

    def value(self, name):
        return self.inputs[name].get('value', '')


@pytest.fixture
def login_app(monkeypatch):
    accounts = (WebLoginAccount('test-user', generate_password_hash(PASSWORD)),)
    monkeypatch.setattr(cl_app, '_configured_login_accounts', lambda: accounts)
    app = cl_app.create_app(test_config={
        'TESTING': True, 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': True,
        'SECRET_KEY': 'isolated-login-csrf-recovery-key',
    })
    try:
        yield app
    finally:
        app.extensions['shutdown_runtime_services']()


def expired_form(app, destination, username='  ＴＥＳＴ－ＵＳＥＲ  ', *, query_only=False):
    client = app.test_client()
    token = LoginFields(client.get('/login', query_string={'next': destination})).value('csrf_token')
    app.config['WTF_CSRF_TIME_LIMIT'] = -1
    data = {'username': username, 'password': PASSWORD, 'csrf_token': token}
    if not query_only:
        data['next'] = destination
    response = client.post('/login', query_string={'next': destination} if query_only else None, data=data)
    app.config['WTF_CSRF_TIME_LIMIT'] = 43200
    assert response.status_code == 400 and 'expired' in response.get_data(as_text=True).lower()
    fields = LoginFields(response)
    assert 'value' not in fields.inputs['password']
    assert PASSWORD not in response.get_data(as_text=True)
    return client, fields


@pytest.mark.parametrize('query_only', [False, True])
def test_expired_login_then_bad_password_then_success_preserves_local_destination(login_app, query_only):
    destination = '/symbols?market=us'
    client, fields = expired_form(login_app, destination, query_only=query_only)
    assert fields.value('username') == 'test-user'
    assert fields.value('next') == destination
    failed = client.post('/login', data={
        'username': fields.value('username'), 'password': 'wrong-fixture-password',
        'next': fields.value('next'), 'csrf_token': fields.value('csrf_token'),
    })
    assert failed.status_code == 200 and '用户名或密码错误' in failed.get_data(as_text=True)
    retry = LoginFields(failed)
    assert retry.value('username') == 'test-user' and retry.value('next') == destination
    assert 'value' not in retry.inputs['password']
    success = client.post('/login', data={
        'username': retry.value('username'), 'password': PASSWORD,
        'next': retry.value('next'), 'csrf_token': retry.value('csrf_token'),
    })
    assert success.status_code == 302 and success.headers['Location'] == destination


@pytest.mark.parametrize('destination', [
    'https://outside.invalid/path', '//outside.invalid/path', '/%2Foutside.invalid',
    '/\\outside.invalid', 'javascript:alert(1)', '/symbols\n',
])
def test_expired_form_cannot_restore_an_external_or_malformed_destination(login_app, destination):
    client, fields = expired_form(login_app, destination)
    assert fields.value('next') == '/'
    response = client.post('/login', data={
        'username': fields.value('username'), 'password': PASSWORD,
        'next': fields.value('next'), 'csrf_token': fields.value('csrf_token'),
    })
    assert response.status_code == 302 and response.headers['Location'] == '/'


def test_recovery_normalizes_invalid_username_without_echoing_control_characters(login_app):
    _client, fields = expired_form(login_app, '/setting', username='test\x00user')
    assert fields.value('username') == '' and fields.value('next') == '/setting'


def test_expired_api_request_keeps_the_existing_json_error_contract(login_app):
    client = login_app.test_client()
    token = LoginFields(client.get('/login')).value('csrf_token')
    login_app.config['WTF_CSRF_TIME_LIMIT'] = -1
    response = client.post('/setting/save', data={'csrf_token': token, 'username': 'test-user', 'next': '/symbols'})
    assert response.status_code == 400
    assert response.get_json() == {
        'ok': False, 's': 'error', 'code': 'csrf_failed', 'errmsg': 'CSRF token is missing or expired.',
    }
