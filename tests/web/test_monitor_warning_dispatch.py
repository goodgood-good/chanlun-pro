"""Exercise the real monitor admission boundary with warning proof requests."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from cl_app.services.screening import ScreeningManager, read_state_json, validate_settings
from cl_app.services.signal_monitor import SignalMonitor


def settings():
    return validate_settings({'scope': 'symbols', 'frequencies': ['1m', '5m'],
                              'symbols': [{'market': 'a', 'code': 'SH.600000', 'name': 'sample'}]})


def test_real_monitor_start_preserves_internal_proof_without_polluting_public_preferences(tmp_path, monkeypatch):
    manager = ScreeningManager(tmp_path, reuse_workers=True, warm_results=False)
    dispatch = Mock(return_value=None)
    monkeypatch.setattr('cl_app.services.screening_worker_runtime.dispatch', dispatch)
    monkeypatch.setattr(manager, 'status', lambda: {'status': 'idle'})
    monkeypatch.setattr(manager, '_status', lambda directory: read_state_json(directory / 'status.json'))
    body = {**settings(), '_warning_evidence_periods': {'a:SH.600000': ['5m']}}
    original = deepcopy(body)
    result = manager.start(body, due_periods={'a:SH.600000': ['5m']})
    assert result['status'] == 'starting'
    request = read_state_json(tmp_path / result['run_id'] / 'request.json')
    assert request['warning_evidence_periods'] == {'a:SH.600000': ['5m']}
    assert '_warning_evidence_periods' not in request['settings']
    assert request['settings']['symbols'][0]['_monitor_periods'] == ['5m']
    assert dispatch.call_args.args[2] == request
    assert body == original
    with pytest.raises(ValueError, match='未知选股条件'):
        validate_settings(body)
    with pytest.raises(ValueError, match='未知选股条件'):
        ScreeningManager(tmp_path / 'manual').start(body)


@pytest.mark.parametrize('proof', [None, [], {'a:SH.600001': ['5m']},
    {'a:SH.600000': []}, {'a:SH.600000': ['bad']}, {'a:SH.600000': [{}]}])
def test_monitor_rejects_invalid_proofs_before_any_worker_or_state_write(tmp_path, proof):
    manager = ScreeningManager(tmp_path, reuse_workers=True, warm_results=False)
    with pytest.raises(ValueError, match='预警复核证据周期不合法'):
        manager.start({**settings(), '_warning_evidence_periods': proof})
    assert not (tmp_path / 'latest.json').exists()


def test_warning_proofs_follow_final_reselected_symbols_and_due_periods():
    launch = {'settings': settings(), 'options': {'due_periods': {'a:SH.600000': ['5m']}}}
    launch['settings']['_warning_evidence_periods'] = {'a:SH.600001': ['1m']}
    warning = {'market': 'a', 'code': 'SH.600000', 'frequency': '5m',
               'stage': 'warning', 'signal_type': 'native_point_preview'}
    state = {'signals': {'other': {**warning, 'code': 'SH.600001'}}, 'unverified_signals': {
        'current': warning, 'not_due': {**warning, 'frequency': '1m'},
        'old_invalidation': {**warning, 'frequency': '1m', 'stage': 'invalidated', 'invalidated_at': 1}}}
    SignalMonitor._bind_warning_evidence(launch, state, 3000)
    assert launch['settings']['_warning_evidence_periods'] == {'a:SH.600000': ['5m']}
    launch['options']['due_periods'] = {'a:SH.600000': ['30m']}
    SignalMonitor._bind_warning_evidence(launch, state, 3000)
    assert '_warning_evidence_periods' not in launch['settings']
