"""An expired provider request cannot overwrite a newer market result."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import json
import threading

import pytest

from cl_app import create_app
from cl_app.blueprints import other
from cl_app.services.external_tick_backoff import ExternalMarketTickBackoff
from cl_app.services.readiness import ReadinessRegistry
from tests.web.test_ticks_none_rate import _tick


@pytest.mark.parametrize('old_outcome', ['success', 'empty', 'failure'])
@pytest.mark.parametrize('new_outcome', ['success', 'failure'])
def test_late_request_preserves_newer_health_and_never_returns_old_quote(monkeypatch, old_outcome, new_outcome):
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True,
                                 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': False})
    clock = [100.]
    backoff = ExternalMarketTickBackoff(clock=lambda: clock[0], stale_probe_seconds=30)
    registry = ReadinessRegistry(clock=lambda: clock[0])
    app.extensions['external_market_tick_backoff'] = backoff
    app.extensions['readiness'] = registry
    started, release = threading.Event(), threading.Event()
    calls = []
    lock = threading.Lock()

    def fetch(_codes):
        with lock:
            ordinal = len(calls)
            calls.append(ordinal)
        outcome = old_outcome if ordinal == 0 else new_outcome
        if ordinal == 0:
            started.set()
            assert release.wait(5)
        if outcome == 'failure':
            raise TimeoutError('isolated provider failure')
        return {} if outcome == 'empty' else {'AAPL.US': _tick('AAPL.US', 100. if ordinal == 0 else 200., 1.)}

    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=fetch, now_trading=lambda *_: True))

    def request():
        with app.test_client() as client:
            result = client.post('/ticks', data={'market': 'us', 'codes': json.dumps(['AAPL.US'])})
            return result.status_code, result.get_json()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            old = executor.submit(request)
            try:
                assert started.wait(5)
                clock[0] = 131.
                current_status, current = request()
                expected_health = registry.ticks_snapshot('us')
            finally:
                release.set()
            old_status, old_result = old.result(timeout=5)
        assert len(calls) == 2
        assert registry.ticks_snapshot('us') == expected_health
        if new_outcome == 'success':
            assert current_status == 200 and current['ticks'][0]['price'] == 200.
            assert old_status == 200 and old_result['ticks'][0]['price'] == 200.
            assert old_result['quote_state'] == 'shared'
        else:
            assert current_status == 503 and expected_health['ready'] is False
            assert old_status == 200 and old_result['quote_state'] == 'deferred'
            assert old_result['ticks'] == []
            decision = backoff.acquire('us')
            assert not decision.allowed and decision.failure_count == 1 and decision.retry_after_seconds == 15
    finally:
        release.set()
        app.extensions['shutdown_runtime_services']()


def test_rejected_backoff_observer_does_not_rewrite_provider_failure_time(monkeypatch):
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True,
                                 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': False})
    clock = [100.]
    registry = ReadinessRegistry(clock=lambda: clock[0])
    app.extensions['readiness'] = registry
    app.extensions['external_market_tick_backoff'] = ExternalMarketTickBackoff(clock=lambda: clock[0])
    provider = Mock(side_effect=TimeoutError('isolated outage'))
    publish = Mock(wraps=registry.record_ticks_failure)
    monkeypatch.setattr(registry, 'record_ticks_failure', publish)
    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=provider))
    try:
        with app.test_client() as client:
            body = {'market': 'us', 'codes': '["AAPL.US"]'}
            assert client.post('/ticks', data=body).status_code == 503
            clock[0] = 101.
            assert client.post('/ticks', data=body).status_code == 503
        assert provider.call_count == publish.call_count == 1
    finally:
        app.extensions['shutdown_runtime_services']()


def test_completion_callbacks_run_only_for_the_request_that_still_owns_the_probe():
    clock = [100.]
    backoff = ExternalMarketTickBackoff(clock=lambda: clock[0])
    first = backoff.acquire('us')
    clock[0] = 131.
    current = backoff.acquire('us')
    published = []
    assert not backoff.record_success('us', probe_id=first.probe_id, on_accept=lambda: published.append('old-success'))
    backoff.record_failure('us', probe_id=first.probe_id, on_accept=lambda: published.append('old-failure'))
    assert not published
    backoff.record_failure('us', probe_id=current.probe_id, on_accept=lambda: published.append('current-failure'))
    assert published == ['current-failure']
    clock[0] += 15
    retry = backoff.acquire('us')
    assert backoff.record_success('us', probe_id=retry.probe_id, on_accept=lambda: published.append('recovered'))
    assert published == ['current-failure', 'recovered']


def test_late_failure_cannot_fail_a_new_request_that_is_still_running(monkeypatch):
    app = create_app(test_config={'TESTING': True, 'LOGIN_DISABLED': True,
                                 'VALIDATE_WEB_SECURITY': False, 'WTF_CSRF_ENABLED': False})
    clock = [100.]
    registry = ReadinessRegistry(clock=lambda: clock[0])
    app.extensions['readiness'] = registry
    app.extensions['external_market_tick_backoff'] = ExternalMarketTickBackoff(clock=lambda: clock[0])
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []
    lock = threading.Lock()

    def fetch(_codes):
        with lock:
            ordinal = len(calls)
            calls.append(ordinal)
        entered[ordinal].set()
        assert release[ordinal].wait(5)
        if ordinal == 0:
            raise TimeoutError('isolated old request failure')
        return {'AAPL.US': _tick('AAPL.US', 200., 1.)}

    monkeypatch.setattr(other, 'get_exchange', lambda _: SimpleNamespace(ticks=fetch, now_trading=lambda *_: True))

    def request():
        with app.test_client() as client:
            return client.post('/ticks', data={'market': 'us', 'codes': '["AAPL.US"]'}).get_json()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(request)
            try:
                assert entered[0].wait(5)
                clock[0] = 131.
                second = executor.submit(request)
                assert entered[1].wait(5)
                release[0].set()
                assert first.result(timeout=5)['quote_state'] == 'deferred'
                assert registry.ticks_snapshot('us')['status'] == 'unknown'
            finally:
                for gate in release:
                    gate.set()
            assert second.result(timeout=5)['ticks'][0]['price'] == 200.
            assert registry.ticks_snapshot('us')['ready'] is True
    finally:
        for gate in release:
            gate.set()
        app.extensions['shutdown_runtime_services']()
