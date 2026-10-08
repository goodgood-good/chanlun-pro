"""Dependency lookups cannot make dispatch validate a superseded state copy."""
import pytest

from cl_app.services import signal_monitor
from tests.web.test_signal_monitor import setup, _seed, signal


@pytest.fixture
def ready_event(setup):
    service, _manual, live, _clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [{**signal(), 'source_closed_at': cutoff, 'evidence': {'nested': {'value': 1}}}]
    live.finish(cutoff)
    service.tick()
    event = next(row for row in service._load()['events'] if row['change'] == 'discovered')
    assert service._validate_notification_events([event])[0]
    return service, live, event


@pytest.mark.parametrize('lookup', ['status', 'calendar'])
@pytest.mark.parametrize('change', ['exit', 'failed', 'uncertain', 'removed_from_pool'])
def test_dispatch_uses_the_state_committed_during_dependency_lookup(ready_event, monkeypatch, lookup, change):
    service, live, event = ready_event

    def commit():
        with service._lock:
            state = service._load()
            if change == 'exit':
                state['signals'].clear()
            elif change == 'failed':
                state['selection_checks']['a:SH.600088:5m']['error'] = True
            elif change == 'uncertain':
                state['unverified_signals'][event['id']] = state['signals'][event['id']]
            else:
                state['seed']['codes'] = []
            service._save()

    if lookup == 'status':
        original = live.status
        def status():
            commit()
            return original()
        monkeypatch.setattr(live, 'status', status)
    else:
        original = signal_monitor.closed_bar_cutoff
        def calendar(*args, **kwargs):
            commit()
            return original(*args, **kwargs)
        monkeypatch.setattr(signal_monitor, 'closed_bar_cutoff', calendar)
    valid, pending = service._validate_notification_events([event])
    assert valid == []
    assert pending == ([event] if change in {'failed', 'uncertain'} else [])


def test_validated_result_does_not_share_mutable_proof_with_live_state(ready_event):
    service, _live, event = ready_event
    valid, pending = service._validate_notification_events([event])
    assert len(valid) == 1 and not pending
    service._load()['signals'][event['id']]['evidence']['nested']['value'] = 2
    assert valid[0]['evidence']['nested']['value'] == 1


def test_holding_lookup_cannot_validate_a_risk_binding_retired_during_that_lookup(ready_event, monkeypatch):
    service, _live, original = ready_event
    key = 'a:SH.600088:5m'
    event = {**original, 'signal_type': 'holding_risk', 'point_type': 'risk_stop',
             'binding_id': 'original-binding', 'available_at': 300}
    state = service._load()
    state['holding_source_current'] = True
    state['holding_risk_plans'][key] = {'active': True, 'binding_id': 'original-binding', 'stop_triggered_at': 300}

    def holdings():
        with service._lock:
            service._load()['holding_risk_plans'][key]['active'] = False
            service._save()
        return [{'market': 'a', 'code': 'SH.600088'}]

    monkeypatch.setattr(signal_monitor, 'holding_symbols', holdings)
    assert service._validate_notification_events([event]) == ([], [])
