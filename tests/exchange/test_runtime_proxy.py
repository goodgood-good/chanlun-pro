from types import SimpleNamespace

import ccxt
import pytest
import requests

from chanlun import utils
from chanlun.http_proxy import ConfiguredProxySession
from cl_app.blueprints import setting
from cl_app.services.notification_binance_history import BinanceSpotPageSource
from flask import Flask


class CaptureAdapter(requests.adapters.BaseAdapter):
    def __init__(self):
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers['Content-Type'] = 'application/json'
        response._content = b'{}'
        return response

    def close(self):
        pass


@pytest.mark.parametrize('saved,expected', [
    (None, {'host': 'default.invalid', 'port': 7890}),
    ({}, {'host': 'default.invalid', 'port': 7890}),
    ({'host': '', 'port': ''}, {'host': '', 'port': ''}),
    ({'host': 'saved.invalid', 'port': 8888}, {'host': 'saved.invalid', 'port': '8888'}),
])
def test_proxy_defaults_are_used_only_before_an_explicit_choice(monkeypatch, saved, expected):
    monkeypatch.setattr(utils, 'db', SimpleNamespace(cache_get=lambda _key, **_kwargs: saved))
    monkeypatch.setattr(utils.config, 'PROXY_HOST', 'default.invalid')
    monkeypatch.setattr(utils.config, 'PROXY_PORT', 7890)
    assert utils.config_get_proxy() == expected


@pytest.mark.parametrize('factory', [ccxt.binance, ccxt.binanceusdm])
def test_existing_ccxt_client_uses_saved_route_for_each_new_request(monkeypatch, factory):
    saved = [{'host': 'old.invalid', 'port': '7890'}]
    monkeypatch.setattr(utils, 'db', SimpleNamespace(cache_get=lambda _key, **_kwargs: saved[0]))
    monkeypatch.setenv('HTTPS_PROXY', 'http://unwanted-env.invalid:99')
    session = ConfiguredProxySession()
    capture = CaptureAdapter()
    session.mount('https://', capture)
    client = factory({'session': session, 'proxies': {'https': 'http://stale-client.invalid:11'}})
    assert client.session is session
    try:
        client.fetch('https://market-data.invalid/one')
        saved[0] = {'host': 'new.invalid', 'port': '8080'}
        client.fetch('https://market-data.invalid/two')
        saved[0] = {'host': '', 'port': ''}
        client.fetch('https://market-data.invalid/three')
        assert [call[1]['proxies'] for call in capture.calls] == [
            {'http': 'http://old.invalid:7890', 'https': 'http://old.invalid:7890'},
            {'http': 'http://new.invalid:8080', 'https': 'http://new.invalid:8080'},
            {},
        ]
        assert client.session is session
    finally:
        session.close()


def test_saved_settings_apply_to_existing_and_new_history_sessions(monkeypatch):
    saved = [None]
    storage = SimpleNamespace(cache_get=lambda key, **_kwargs: saved[0],
                              cache_set_many=lambda values: saved.__setitem__(0, values['req_proxy']))
    monkeypatch.setattr(utils, 'db', storage)
    monkeypatch.setattr(setting, 'db', storage)
    monkeypatch.setattr(utils.config, 'PROXY_HOST', 'default.invalid')
    monkeypatch.setattr(utils.config, 'PROXY_PORT', 7890)
    history = BinanceSpotPageSource()
    session = history._get_session()
    adapter = CaptureAdapter()
    session.mount('https://', adapter)
    session.get('https://market-data.invalid/first')
    app = Flask(__name__)
    with app.test_request_context('/setting/save', method='POST', data={'proxy_host': '', 'proxy_port': ''}):
        assert setting.setting_save.__wrapped__() == {'ok': True}
    assert history._get_session() is session
    session.get('https://market-data.invalid/next')
    other = ConfiguredProxySession()
    other.mount('https://', adapter)
    other.get('https://market-data.invalid/other-worker')
    assert [entry[1]['proxies'] for entry in adapter.calls] == [
        {'http': 'http://default.invalid:7890', 'https': 'http://default.invalid:7890'}, {}, {},
    ]
    session.close()
    other.close()


def test_settings_page_displays_effective_default_proxy(monkeypatch):
    monkeypatch.setattr(utils, 'db', SimpleNamespace(cache_get=lambda _key, **_kwargs: None))
    monkeypatch.setattr(utils.config, 'PROXY_HOST', 'default.invalid')
    monkeypatch.setattr(utils.config, 'PROXY_PORT', 7890)
    monkeypatch.setattr(setting, 'render_template', lambda _template, **values: values)
    assert setting.setting.__wrapped__() == {'proxy_host': 'default.invalid', 'proxy_port': 7890}


def test_database_error_cannot_change_explicit_direct_access_into_default_proxy(monkeypatch):
    from chanlun.persistence.db import DB

    class BrokenSession:
        def __enter__(self):
            raise RuntimeError('isolated configuration read failed')
        def __exit__(self, *_args):
            return False

    broken = SimpleNamespace(Session=BrokenSession)
    monkeypatch.setattr(utils, 'db', SimpleNamespace(cache_get=lambda key, **kwargs: DB.cache_get(broken, key, **kwargs)))
    monkeypatch.setattr(utils.config, 'PROXY_HOST', 'default.invalid')
    monkeypatch.setattr(utils.config, 'PROXY_PORT', 7890)
    assert DB.cache_get(broken, 'optional-cache') is None
    session = ConfiguredProxySession()
    capture = CaptureAdapter()
    session.mount('https://', capture)
    with pytest.raises(RuntimeError, match='configuration read failed'):
        session.get('https://market-data.invalid/not-sent')
    assert capture.calls == []
    session.close()
