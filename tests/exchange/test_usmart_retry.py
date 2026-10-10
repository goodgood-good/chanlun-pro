"""Transient quote failures retry a single read, preserving failure boundaries."""
import json
from types import SimpleNamespace

import pytest
import requests

from chanlun.exchange.exchange_usmart import USmartClient, USmartAPIError


def response(status=200, code=0, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: {'code':code,'msg':'fixture','data':{'value':1}})


def client_for(monkeypatch, replies):
    calls, delays = [], []
    def post(url, **kwargs):
        calls.append(kwargs)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    client = USmartClient(channel='fixture', token='fixture-token',
                         phone='', login_password='', private_key='', public_key='',
                         session=SimpleNamespace(post=post),
                         rate_limiter=SimpleNamespace(wait=lambda **_: None))
    monkeypatch.setattr(client, '_get_signer', lambda: SimpleNamespace(sign=lambda *_a, **_k: 'fixture-signature'))
    monkeypatch.setattr('chanlun.exchange.exchange_usmart.time.sleep', delays.append)
    return client, calls, delays


@pytest.mark.parametrize('failure', [response(502),response(503),response(504),requests.Timeout(),requests.ConnectionError()])
def test_transient_quote_failure_retries_once_with_new_signature_identity(monkeypatch, failure):
    client, calls, delays = client_for(monkeypatch, [failure,response()])
    payload = {'secuId':'usMSFT','start':123,'count':300}
    assert client.quote('kline',payload) == {'value':1}
    assert len(calls) == 2 and delays == [0.25]
    assert all(json.loads(call['data']) == payload for call in calls)
    assert calls[0]['headers']['X-Request-Id'] != calls[1]['headers']['X-Request-Id']


def test_persistent_503_is_not_empty_history_or_unbounded_retry(monkeypatch):
    client, calls, delays = client_for(monkeypatch,[response(503),response(503)])
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline',{})
    assert raised.value.status_code == 503
    assert len(calls) == 2 and len(delays) == 1


@pytest.mark.parametrize('failure', [response(401),response(403),response(429),response(200,1001)])
def test_auth_permission_and_business_errors_do_not_repeat(monkeypatch, failure):
    client, calls, delays = client_for(monkeypatch,[failure])
    with pytest.raises(USmartAPIError):
        client.quote('kline',{})
    assert len(calls) == 1 and delays == []


def test_long_retry_after_is_reported_instead_of_blocking_worker(monkeypatch):
    client, calls, delays = client_for(monkeypatch,[response(503,headers={'Retry-After':'30'})])
    with pytest.raises(USmartAPIError) as raised:
        client.quote('kline',{})
    assert raised.value.retry_after == 30
    assert len(calls) == 1 and delays == []


def test_login_failure_is_never_retried_by_quote_wrapper(monkeypatch):
    client, calls, delays = client_for(monkeypatch,[])
    client.token = ''
    attempts = []
    def login():
        attempts.append(True)
        raise USmartAPIError('login unavailable',status_code=503)
    monkeypatch.setattr(client,'login',login)
    with pytest.raises(USmartAPIError):
        client.quote('kline',{})
    assert len(attempts) == 1 and calls == delays == []


@pytest.mark.parametrize('process,priority',[('MainProcess','interactive'),('SpawnProcess-4','background')])
def test_every_quote_attempt_uses_shared_admission_before_sending(monkeypatch,process,priority):
    client,calls,_=client_for(monkeypatch,[response(503),response()])
    admissions=[]
    def wait(**kwargs):
        assert len(calls)==len(admissions)
        admissions.append(kwargs)
    client._rate_limiter=SimpleNamespace(wait=wait)
    monkeypatch.setattr('chanlun.exchange.lb_priority.multiprocessing.current_process',
                        lambda:SimpleNamespace(name=process))
    assert client.quote('kline',{})=={'value':1}
    assert len(admissions)==len(calls)==2
    assert all(item=={'host':client.quote_host,'channel':client.channel,'endpoint':'kline','priority':priority}
               for item in admissions)


def test_failed_admission_never_sends_or_immediately_retries(monkeypatch):
    from chanlun.exchange.usmart_rate_limit import USmartRateLimitTimeout
    client,calls,delays=client_for(monkeypatch,[])
    admissions=[]
    def wait(**_):
        admissions.append(True)
        raise USmartRateLimitTimeout()
    client._rate_limiter=SimpleNamespace(wait=wait)
    with pytest.raises(USmartRateLimitTimeout):
        client.quote('kline',{})
    assert len(admissions)==1 and calls==delays==[]


def test_main_process_prewarm_reaches_the_background_quote_queue(monkeypatch):
    from chanlun.exchange.lb_priority import lb_low_priority
    client,_,_=client_for(monkeypatch,[response(),response()])
    priorities=[]
    client._rate_limiter=SimpleNamespace(wait=lambda **kwargs:priorities.append(kwargs['priority']))
    with lb_low_priority():
        client.quote('kline',{})
    client.quote('kline',{})
    assert priorities==['background','interactive']
