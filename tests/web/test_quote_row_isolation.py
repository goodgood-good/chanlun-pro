"""Malformed individual quotes must not fail healthy rows or fabricate changes."""
from types import SimpleNamespace
import json

import pytest

from cl_app import create_app
from cl_app.blueprints import other, tv
from tests.web.test_ticks_none_rate import _tick


@pytest.fixture
def client():
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True,
                                 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': False})
    return app.test_client()


class BrokenPrice:
    @property
    def last(self):
        raise ValueError('isolated malformed quote')


@pytest.mark.parametrize('bad', [SimpleNamespace(rate=1), BrokenPrice()])
@pytest.mark.parametrize('route', ['ticks', 'tv'])
def test_bad_price_attribute_is_isolated_before_field_conversion(client, monkeypatch, bad, route):
    exchange = SimpleNamespace(ticks=lambda _: {'SH.600000': bad, 'SZ.000001': _tick('SZ.000001', 10, 2)},
                               now_trading=lambda *_: True)
    module = other if route == 'ticks' else tv
    monkeypatch.setattr(module, 'get_exchange', lambda _: exchange)
    response = (client.post('/ticks', data={'market': 'a', 'codes': json.dumps(['SH.600000', 'SZ.000001'])})
                if route == 'ticks' else client.get('/tv/quotes?symbols=a:SH.600000,a:SZ.000001'))
    assert response.status_code == 200
    value = response.get_json()
    if route == 'ticks':
        assert value['ok'] and value['ticks'] == [{'code': 'SZ.000001', 'price': 10., 'rate': 2.}]
    else:
        rows = {row['n']: row for row in value['d']}
        assert rows['a:SH.600000'] == {'s': 'error', 'n': 'a:SH.600000', 'v': {}}
        assert rows['a:SZ.000001']['s'] == 'ok' and rows['a:SZ.000001']['v']['lp'] == 10


@pytest.mark.parametrize('rate', [None, ''])
def test_tv_missing_change_is_not_reported_as_flat(client, monkeypatch, rate):
    exchange = SimpleNamespace(ticks=lambda _: {'SH.600000': _tick('SH.600000', 10, rate),
                                                 'SZ.000001': _tick('SZ.000001', 20, 0)})
    monkeypatch.setattr(tv, 'get_exchange', lambda _: exchange)
    response = client.get('/tv/quotes?symbols=a:SH.600000,a:SZ.000001').get_json()
    rows = {row['n']: row for row in response['d']}
    assert rows['a:SH.600000'] == {'s': 'error', 'n': 'a:SH.600000', 'v': {}}
    assert rows['a:SZ.000001']['s'] == 'ok'
    assert rows['a:SZ.000001']['v']['chp'] == 0


@pytest.mark.parametrize('market', ['a', 'us', 'currency_spot'])
def test_empty_tick_request_never_contacts_a_provider(client, monkeypatch, market):
    def forbidden(*args):
        pytest.fail('empty quote request reached a provider')
    monkeypatch.setattr(other, 'get_exchange', forbidden)
    value = client.post('/ticks', data={'market': market, 'codes': '[]'})
    assert value.status_code == 200
    assert value.get_json() == {'ok': True, 'market_state': 'unknown', 'now_trading': None, 'ticks': [], 'error': None}


@pytest.mark.parametrize('code', ['', '   '])
def test_blank_tick_code_is_a_request_error_before_provider_coordination(client, monkeypatch, code):
    monkeypatch.setattr(other, 'get_exchange', lambda _: pytest.fail('blank symbol reached provider'))
    response = client.post('/ticks', data={'market': 'us', 'codes': json.dumps([code])})
    assert response.status_code == 400
    assert response.get_json()['error']['code'] == 'empty_code'
