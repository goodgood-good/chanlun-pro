from flask import Flask
import pytest

from cl_app.blueprints import symbols


@pytest.fixture
def catalog(monkeypatch):
    state = {'ready': False, 'status': 'loading', 'count': 0}
    rows, calls = [], []

    def read(market, *, allow_sync_fallback):
        assert allow_sync_fallback is False, 'the page must not wait for provider I/O'
        calls.append(market)
        if not rows:
            raise RuntimeError('isolated cold catalog')
        return rows

    monkeypatch.setattr(symbols, '_market_filtered_cache', {})
    monkeypatch.setattr(symbols, 'get_cached_processed_stocks', read)
    monkeypatch.setattr(symbols, 'get_symbol_readiness', lambda _market: state)
    app = Flask(__name__)
    app.config['LOGIN_DISABLED'] = True
    app.register_blueprint(symbols.symbols_bp)
    return app.test_client(), state, rows, calls


def row(code='SH.600519', name='Sample'):
    return {'code': code, 'name': name, 'code_lower': code.lower(), 'name_lower': name.lower(),
            'pinyin_initials': 'sample', 'type': 'stock_cn'}


def test_cold_catalog_never_caches_zero_and_next_ready_request_recovers(catalog):
    client, state, rows, _ = catalog
    cold = client.get('/symbols/list?market=a')
    assert cold.status_code == 202 and cold.json['ok'] is False and cold.json['total'] is None
    assert cold.headers['Cache-Control'] == 'private, no-store'
    assert 'a' not in symbols._market_filtered_cache
    state.update(ready=True, status='ready', count=1)
    rows.append(row())
    ready = client.get('/symbols/list?market=a')
    assert ready.status_code == 200 and ready.json['total'] == 1
    assert ready.headers['Cache-Control'] == 'private, no-cache'
    matched_none = client.get('/symbols/list?market=a&q=no-match')
    assert matched_none.status_code == 200 and matched_none.json['ok'] is True and matched_none.json['total'] == 0


def test_catalog_failure_is_distinct_and_does_not_retain_filtered_cache(catalog):
    client, state, rows, _ = catalog
    rows.append(row())
    assert client.get('/symbols/list?market=a').json['total'] == 1
    rows.clear()
    state.update(status='degraded', last_error='private provider detail')
    failed = client.get('/symbols/list?market=a')
    assert failed.status_code == 503 and failed.json['status'] == 'error'
    assert failed.headers['Cache-Control'] == 'private, no-store'
    assert 'private provider' not in failed.get_data(as_text=True)
    assert 'a' not in symbols._market_filtered_cache


def test_ready_catalog_with_no_displayable_rows_is_a_normal_empty_result(catalog):
    client, state, rows, _ = catalog
    rows.append(row('FUND.US', 'Fixture ETF'))
    state.update(ready=True, status='ready', count=1)
    response = client.get('/symbols/list?market=us')
    assert response.status_code == 200 and response.json['ok'] is True and response.json['total'] == 0


def test_ready_etag_revalidation_keeps_cache_policy(catalog):
    client, _state, rows, _ = catalog
    rows.append(row())
    first = client.get('/symbols/list?market=a')
    second = client.get('/symbols/list?market=a', headers={'If-None-Match': first.headers['ETag']})
    assert second.status_code == 304
    assert second.headers['Cache-Control'] == 'private, no-cache'
