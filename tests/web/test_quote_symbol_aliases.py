"""Equivalent provider symbols share quotes while caller identities stay exact."""
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from cl_app import create_app
from cl_app.blueprints import other, tv
from cl_app.services.external_tick_backoff import ExternalMarketTickBackoff
from tests.web.test_ticks_none_rate import _tick


@pytest.fixture
def client():
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True,
                                 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': False})
    yield app.test_client()
    app.extensions['shutdown_runtime_services']()


@pytest.mark.parametrize('market,requested,returned', [
    ('us', 'AAPL.US', 'US.AAPL'), ('hk', 'HK.700', '00700.HK'),
    ('a', 'BJ.920091', '920091.BJ'), ('a', 'SH.600000', '600000.SH')])
def test_tv_quote_matches_provider_alias_without_changing_the_requested_symbol(client, monkeypatch, market, requested, returned):
    exchange = SimpleNamespace(ticks=lambda _: {returned: _tick(returned, 101., 1.5)})
    monkeypatch.setattr(tv, 'get_exchange', lambda _: exchange)
    name = market + ':' + requested
    response = client.get('/tv/quotes', query_string={'symbols': name}).get_json()
    assert len(response['d']) == 1
    quote = response['d'][0]
    assert quote['s'] == 'ok' and quote['n'] == name and quote['v']['lp'] == 101.


@pytest.mark.parametrize('market,first,second,returned', [
    ('us', 'AAPL.US', 'US.AAPL', 'US.AAPL'), ('hk', '00700.HK', 'HK.700', '00700.HK')])
def test_recent_quote_is_reused_for_an_equivalent_code(client, monkeypatch, market, first, second, returned):
    provider = Mock(return_value={returned: _tick(returned, 101., 1.5)})
    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=provider, now_trading=lambda *_: True))
    original = client.post('/ticks', data={'market': market, 'codes': json.dumps([first])})
    reused = client.post('/ticks', data={'market': market, 'codes': json.dumps([second])})
    assert original.status_code == reused.status_code == 200
    assert reused.get_json()['ticks'] == [{'code': second, 'price': 101., 'rate': 1.5}]
    assert provider.call_count == 1
    assert reused.get_json()['quote_state'] == 'shared'


@pytest.mark.parametrize('route', ['ticks', 'tv'])
def test_both_requested_aliases_receive_one_quote_each(client, monkeypatch, route):
    exchange = SimpleNamespace(ticks=lambda _: {'US.AAPL': _tick('US.AAPL', 101., 1.5)}, now_trading=lambda *_: True)
    monkeypatch.setattr(other if route == 'ticks' else tv, 'get_exchange', lambda _: exchange)
    if route == 'ticks':
        response = client.post('/ticks', data={'market': 'us', 'codes': json.dumps(['AAPL.US', 'US.AAPL'])})
        assert response.status_code == 200
        assert response.get_json()['ticks'] == [
            {'code': name, 'price': 101., 'rate': 1.5} for name in ('AAPL.US', 'US.AAPL')]
    else:
        response = client.get('/tv/quotes?symbols=us:AAPL.US,us:US.AAPL').get_json()
        assert [(row['n'], row['s']) for row in response['d']] == [('us:AAPL.US', 'ok'), ('us:US.AAPL', 'ok')]


def test_exact_code_wins_and_ambiguous_aliases_do_not_invent_a_quote(client, monkeypatch):
    exchange = SimpleNamespace(ticks=lambda _: {'AAPL.US': _tick('AAPL.US', 101., 1.),
                                                 'US.AAPL': _tick('US.AAPL', 102., 2.)})
    monkeypatch.setattr(tv, 'get_exchange', lambda _: exchange)
    response = client.get('/tv/quotes?symbols=us:AAPL.US,us:AAPL').get_json()
    assert response['d'][0]['s'] == 'ok' and response['d'][0]['v']['lp'] == 101.
    assert response['d'][1] == {'s': 'error', 'n': 'us:AAPL', 'v': {}}


def test_cached_identical_alias_rows_can_serve_a_third_equivalent_spelling(client, monkeypatch):
    provider = Mock(return_value={'US.AAPL': _tick('US.AAPL', 101., 1.5)})
    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=provider, now_trading=lambda *_: True))
    assert client.post('/ticks', data={'market': 'us', 'codes': json.dumps(['AAPL.US', 'US.AAPL'])}).status_code == 200
    response = client.post('/ticks', data={'market': 'us', 'codes': json.dumps(['AAPL'])})
    assert response.get_json()['ticks'] == [{'code': 'AAPL', 'price': 101., 'rate': 1.5}]
    assert provider.call_count == 1 and response.get_json()['quote_state'] == 'shared'


@pytest.mark.parametrize('market,code', [('us', 'US.'), ('us', '.US'), ('hk', 'HK.')])
def test_market_marker_without_a_symbol_is_rejected_before_probe_coordination(client, monkeypatch, market, code):
    monkeypatch.setattr(other, 'get_exchange', lambda _: pytest.fail('empty canonical symbol reached provider'))
    response = client.post('/ticks', data={'market': market, 'codes': json.dumps([code])})
    assert response.status_code == 400 and response.get_json()['error']['code'] == 'empty_code'


def test_equivalent_spelling_does_not_merge_distinct_markets(client, monkeypatch):
    def exchange(market):
        price = 100. if market.value == 'us' else 200.
        return SimpleNamespace(ticks=lambda _: {'ABC': _tick('ABC', price, 1.)}, now_trading=lambda *_: True)
    monkeypatch.setattr(other, 'get_exchange', exchange)
    for market, expected in [('us', 100.), ('hk', 200.)]:
        response = client.post('/ticks', data={'market': market, 'codes': '["ABC"]'})
        assert response.get_json()['ticks'][0]['price'] == expected


def test_concurrent_alias_requests_share_the_same_inflight_probe(client, monkeypatch):
    started, waiting, release = threading.Event(), threading.Event(), threading.Event()
    backoff = ExternalMarketTickBackoff()
    client.application.extensions['external_market_tick_backoff'] = backoff
    original_wait = backoff.wait_for_success

    def wait_for_result(*args, **kwargs):
        waiting.set()
        return original_wait(*args, **kwargs)

    def fetch(_codes):
        started.set()
        assert release.wait(5)
        return {'US.AAPL': _tick('US.AAPL', 101., 1.5)}

    provider = Mock(side_effect=fetch)
    monkeypatch.setattr(backoff, 'wait_for_success', wait_for_result)
    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=provider, now_trading=lambda *_: True))

    def request(code):
        with client.application.test_client() as thread_client:
            response = thread_client.post('/ticks', data={'market': 'us', 'codes': json.dumps([code])})
            assert response.status_code == 200
            return response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(request, 'AAPL.US')
        try:
            assert started.wait(5)
            second = executor.submit(request, 'US.AAPL')
            assert waiting.wait(5)
        finally:
            release.set()
        assert first.result(timeout=10)['ticks'][0]['code'] == 'AAPL.US'
        shared = second.result(timeout=10)
    assert shared['ticks'][0]['code'] == 'US.AAPL' and shared['quote_state'] == 'shared'
    assert provider.call_count == 1


def test_invalid_provider_container_is_not_a_successful_closed_market_quote(client, monkeypatch):
    exchange = SimpleNamespace(ticks=lambda _: [], now_trading=lambda *_: False)
    monkeypatch.setattr(other, 'get_exchange', lambda _: exchange)
    response = client.post('/ticks', data={'market': 'us', 'codes': '["AAPL.US"]'})
    assert response.status_code == 503 and response.get_json()['ok'] is False
