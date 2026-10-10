"""Renewable HTTP 401 recovery uses fake credentials and an injected transport."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from chanlun.exchange.exchange_usmart import USmartClient, USmartAPIError


def reply(status=200, *, code=0, token=None):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: {
        'code': code, 'msg': 'fixture', 'data': {'token': token} if token else {'value': 1}})


def client_for(post, *, renewable=True):
    client = USmartClient(channel='fixture', token='expired-fixture',
                         phone='fixture-phone' if renewable else '', login_password='fixture-password' if renewable else '',
                         private_key='fixture-private' if renewable else '', public_key='fixture-public' if renewable else '',
                         quote_host='https://quote.invalid', trade_host='https://login.invalid',
                         session=SimpleNamespace(post=post), rate_limiter=SimpleNamespace(wait=lambda **_: None))
    client._get_signer = lambda **_: SimpleNamespace(sign=lambda *_a, **_kw: 'fixture-signature',
                                                    encrypt_login_field=lambda _: 'fixture-encrypted')
    return client


def scripted_client(replies, *, renewable=True):
    queue = iter(replies)
    calls = []

    def post(url, **kwargs):
        calls.append({'url': url, **kwargs})
        return next(queue)

    return client_for(post, renewable=renewable), calls


def test_expired_token_is_renewed_once_and_only_failed_quote_is_replayed():
    client, calls = scripted_client([reply(401), reply(token='fresh-fixture'), reply()])
    payload = {'secuId': 'usORCL', 'start': 123, 'count': 300}
    assert client.quote('kline', payload) == {'value': 1}
    assert [c['url'].rsplit('/', 1)[-1] for c in calls] == ['kline', 'login', 'kline']
    assert calls[0]['data'] == calls[2]['data']
    assert json.loads(calls[2]['data']) == payload
    assert calls[0]['headers']['Authorization'] == 'expired-fixture'
    assert calls[2]['headers']['Authorization'] == 'fresh-fixture'
    assert calls[0]['headers']['X-Request-Id'] != calls[2]['headers']['X-Request-Id']
    assert 'Authorization' not in calls[1]['headers']


def test_fixed_token_without_login_credentials_still_reports_401():
    client, calls = scripted_client([reply(401)], renewable=False)
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline', {})
    assert raised.value.status_code == 401
    assert len(calls) == 1 and client.token == 'expired-fixture'


def test_dedicated_page_can_disable_transient_retry_without_bypassing_auth_recovery():
    client, calls = scripted_client([reply(401), reply(token='fresh-fixture'), reply(503)])
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline', {}, transient_retries=0)
    assert raised.value.status_code == 503 and len(calls) == 3
    assert [c['url'].rsplit('/', 1)[-1] for c in calls] == ['kline', 'login', 'kline']


def test_dedicated_page_performs_no_503_retry():
    client, calls = scripted_client([reply(503)])
    with pytest.raises(USmartAPIError):
        client.quote('kline', {}, transient_retries=0)
    assert len(calls) == 1


@pytest.mark.parametrize('value', [True, -1, 2, '0'])
def test_quote_retry_budget_rejects_unbounded_or_ambiguous_values(value):
    client, calls = scripted_client([])
    with pytest.raises(ValueError, match='transient_retries'):
        client.quote('kline', {}, transient_retries=value)
    assert calls == []


@pytest.mark.parametrize('last', [reply(401), reply(403), reply(200, code=401)])
def test_second_auth_failure_is_not_a_refresh_loop(last):
    client, calls = scripted_client([reply(401), reply(token='fresh-fixture'), last])
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline', {})
    assert len(calls) == 3
    assert sum(c['url'].endswith('/login') for c in calls) == 1
    assert all(secret not in repr(raised.value) + repr(vars(raised.value))
               for secret in ('expired-fixture', 'fresh-fixture', 'fixture-password'))


@pytest.mark.parametrize('failure', [reply(401), reply(503), reply(200, code=1001)])
def test_failed_refresh_login_is_not_retried(failure):
    client, calls = scripted_client([reply(401), failure])
    with pytest.raises(USmartAPIError):
        client.quote('kline', {})
    assert len(calls) == 2 and calls[-1]['url'].endswith('/login')


@pytest.mark.parametrize('failure', [reply(403), reply(429), reply(200, code=401)])
def test_only_http_401_can_start_credential_refresh(failure):
    client, calls = scripted_client([failure])
    with pytest.raises(USmartAPIError):
        client.quote('kline', {})
    assert len(calls) == 1 and client.token == 'expired-fixture'


def test_transient_and_auth_retry_budgets_remain_bounded(monkeypatch):
    monkeypatch.setattr('chanlun.exchange.exchange_usmart.time.sleep', lambda _: None)
    client, calls = scripted_client([reply(503), reply(401), reply(token='fresh-fixture'), reply(503)])
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline', {})
    assert raised.value.status_code == 503
    assert len(calls) == 4  # Three quotes plus one login, never another retry.


@pytest.mark.parametrize('renewed_token', ['fresh-fixture', 'expired-fixture'])
def test_late_concurrent_401_reuses_new_token_instead_of_invalidating_it(renewed_token):
    barrier, renewed = threading.Barrier(2), threading.Event()
    lock, calls = threading.Lock(), []

    def post(url, **kwargs):
        with lock:
            calls.append({'url': url, **kwargs})
            old_count = sum(c['headers'].get('Authorization') == 'expired-fixture' for c in calls)
        if url.endswith('/login'):
            renewed.set()
            return reply(token=renewed_token)
        if kwargs['headers']['Authorization'] == 'expired-fixture' and old_count <= 2:
            barrier.wait(timeout=3)
            if old_count == 2:
                assert renewed.wait(timeout=3)
            return reply(401)
        assert kwargs['headers']['Authorization'] == renewed_token
        return reply()

    client = client_for(post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.quote, 'kline', {'request': index}) for index in range(2)]
        assert [future.result(timeout=5) for future in futures] == [{'value': 1}] * 2
    assert len(calls) == 5 and sum(c['url'].endswith('/login') for c in calls) == 1
    assert client.token == renewed_token


def test_failed_refresh_is_shared_with_late_waiters_and_has_a_recovery_cooldown(monkeypatch):
    clock = [1000.]
    monkeypatch.setattr('chanlun.exchange.exchange_usmart.time.monotonic', lambda: clock[0])
    barrier, failed = threading.Barrier(2), threading.Event()
    lock, calls, logins = threading.Lock(), [], []

    def post(url, **kwargs):
        with lock:
            calls.append({'url': url, **kwargs})
            old_count = sum(c['headers'].get('Authorization') == 'expired-fixture' for c in calls)
        if url.endswith('/login'):
            logins.append(url)
            if len(logins) == 1:
                failed.set()
                return reply(503)
            return reply(token='fresh-fixture')
        if kwargs['headers']['Authorization'] == 'expired-fixture':
            if old_count <= 2:
                barrier.wait(timeout=3)
                if old_count == 2:
                    assert failed.wait(timeout=3)
            return reply(401)
        return reply()

    client = client_for(post)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.quote, 'kline', {}) for _ in range(2)]
        errors = []
        for future in futures:
            with pytest.raises(USmartAPIError) as raised:
                future.result(timeout=5)
            errors.append(raised.value)
    assert len(logins) == 1 and len(calls) == 3
    assert all(error.code == 'usmart_auth_refresh_failed' and error.status_code == 503 for error in errors)
    assert client.token == 'expired-fixture'
    assert all(secret not in repr(client._auth_refresh_failure)
               for secret in ('expired-fixture', 'fixture-password', 'fixture-phone'))
    with pytest.raises(USmartAPIError, match='token refresh failed'):
        client.quote('kline', {})
    assert len(logins) == 1  # New requests during the cooldown cannot storm login.
    clock[0] += 31
    with pytest.raises(USmartAPIError, match='token refresh failed'):
        client._refresh_rejected_token('expired-fixture', 0)
    assert len(logins) == 1  # Even a very late old-generation 401 cannot retry login.
    assert client.quote('kline', {}) == {'value': 1}
    assert len(logins) == 2 and client.token == 'fresh-fixture'


def test_replayed_401_cools_down_further_logins_without_blocking_successful_quotes(monkeypatch):
    clock = [1000.]
    monkeypatch.setattr('chanlun.exchange.exchange_usmart.time.monotonic', lambda: clock[0])
    client, calls = scripted_client([reply(401), reply(token='fresh-fixture'), reply(401),
                                    reply(401), reply(), reply(401), reply(token='next-fixture'), reply()])
    with pytest.raises(USmartAPIError): client.quote('kline', {})
    with pytest.raises(USmartAPIError, match='token refresh failed'): client.quote('kline', {})
    assert sum(c['url'].endswith('/login') for c in calls) == 1
    assert client.quote('kline', {}) == {'value': 1}  # Cooldown affects renewal, not reads.
    clock[0] += 31
    assert client.quote('kline', {}) == {'value': 1}
    assert sum(c['url'].endswith('/login') for c in calls) == 2
    client._remember_rejected_refresh('fresh-fixture', 1)
    assert client.token == 'next-fixture' and client._auth_refresh_failure is None
