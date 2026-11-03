"""Prefetch follows the actual normal live admission, not a fixed item count."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from chanlun.screening import source_prefetch as prefetch
from chanlun.screening.runner import write_json
from chanlun.screening.timeframe_policy import POLICY
from cl_app.services.signal_monitor import SignalMonitor
from tests.test_screening_source_prefetch import fixture_values, idle_owner, MAIN, REVISION, symbols
from tests.web.test_screening_worker_runtime import dispatcher as dispatcher


def plan(capacity):
    all_symbols = symbols(160)
    keys = [f"a:{s['code']}" for s in all_symbols]
    chosen = min(64, capacity * 4)
    state = {'enabled': True, 'holding_symbols': [],
             'seed': {'run_id': 'seed', 'codes': keys, 'settings': {}}}
    fair = {'symbols': all_symbols, 'periods': {key: ['5m'] for key in keys},
            'targets': {key + ':5m': MAIN for key in keys}, 'recent': set(keys),
            'urgent': set(), 'now': MAIN + 10, 'state': {}, 'cursor': 0}
    launch = {'mode': 'live', 'settings': {'symbols': all_symbols[:chosen], 'workers': capacity,
              'recent_sessions': 5, 'max_anchor_sessions': 20, 'timeframe_policy': POLICY,
              'frequencies': ['5m']}, 'options': {}, 'fair_plan': fair,
              'fair_cursor': 1, 'critical_cursor': 0}
    def select(value, workers):
        limit = SignalMonitor._batch_limit(value, workers)
        return ([{'key': f"a:{s['code']}", 'symbol': s, 'frequency': '5m'}
                 for s in value['symbols'][:limit]], 9, 9)
    return launch, state, select


def make(launch, state, select):
    return prefetch.make_hint(launch, state, prefetch.control_value(state, 1, 'one'), select,
                              batch_limit=SignalMonitor._batch_limit)


@pytest.mark.parametrize('capacity', [9, 10, 11, 12, 15, 16])
def test_full_normal_limited_batch_gets_a_bounded_hint(capacity):
    launch, state, select = plan(capacity)
    before = deepcopy(launch)
    hint = make(launch, state, select)
    expected = min(64, capacity * 4)
    assert hint['admission']['count'] == hint['admission']['limit'] == expected
    assert len(hint['symbols']) == expected
    assert launch == before


@pytest.mark.parametrize('change', ['holding_only', 'notification_only', 'tail', 'legacy', 'background', 'unknown_limit'])
def test_urgent_only_small_or_unsupported_batches_do_not_prefetch(change):
    launch, state, select = plan(9)
    current = {f"a:{s['code']}" for s in launch['settings']['symbols']}
    if change == 'holding_only': state['holding_symbols'] = list(current)
    elif change == 'notification_only': launch['fair_plan']['urgent'] = current
    elif change == 'tail': launch['settings']['symbols'] = launch['settings']['symbols'][:3]
    elif change == 'legacy': launch['settings'].pop('timeframe_policy')
    elif change == 'background': launch['mode'] = 'background'
    else:
        assert prefetch.make_hint(launch, state, prefetch.control_value(state, 1, 'one'), select) is None
        return
    assert make(launch, state, select) is None


def test_measured_live_slice_limit_is_reused_instead_of_guessing_four_waves():
    launch, state, select = plan(9)
    launch['fair_plan']['period_costs'] = {'5m': {'seconds': 11}}
    launch['settings']['symbols'] = launch['settings']['symbols'][:18]
    hint = make(launch, state, select)
    assert hint['admission']['count'] == hint['admission']['limit'] == 18
    assert len(hint['symbols']) == 18


@pytest.mark.parametrize('owner_count', [36, 40, 44, 48, 60])
def test_completed_limited_owner_can_reuse_its_existing_preparation_child(idle_owner, owner_count):
    root, directory, request, pool = idle_owner
    new_root, new_directory, new_request = fixture_values(root.parent, owner_count=owner_count)
    assert new_root == root and new_directory == directory
    pid = pool.preparation_worker.process.pid
    prefetch.run_idle_prefetch(root, directory, new_request, pool, REVISION, 'one',
                              revision_reader=lambda: REVISION, now=lambda: MAIN + 10)
    status = json.loads((root / prefetch.STATUS_FILE).read_text())
    assert status['stop_reason'] == 'completed' and status['prepared_symbols'] == 4
    assert pool.preparation_worker.process.pid == pid and pool.preparation_worker.identity is None


@pytest.mark.parametrize('damage', ['incomplete', 'wrong_count', 'ordinary_is_holding', 'ordinary_wrong_period', 'priority_only'])
def test_unmatched_admission_cannot_start_host_prefetch(tmp_path, damage):
    root, directory, request = fixture_values(tmp_path, owner_count=36)
    if damage == 'incomplete': write_json(directory / 'status.json', {'status':'completed','total':36,'completed':35})
    elif damage == 'wrong_count': request['settings']['symbols'].pop()
    elif damage == 'ordinary_is_holding': request['holding_snapshot'] = {'keys': ['a:SH.600000']}
    elif damage == 'ordinary_wrong_period': request['settings']['symbols'][0]['_monitor_periods'] = ['1m']
    else: request['source_prefetch']['admission']['kind'] = 'priority_only'
    worker = SimpleNamespace(closed=False, identity=None, process=SimpleNamespace(is_alive=lambda:True))
    pool = SimpleNamespace(busy=False, closed=False, preparation_worker=worker)
    def forbidden(*args, **kwargs): pytest.fail('unmatched admission reached preparation')
    prefetch.run_idle_prefetch(root, directory, request, pool, REVISION, 'one',
        revision_reader=lambda: REVISION, now=lambda: MAIN + 10, prepare=forbidden)
    status = json.loads((root / prefetch.STATUS_FILE).read_text())
    assert status['stop_reason'] in {'owner_not_idle_completed_batch', 'prefetch_unavailable'}
    assert status['receipt_count'] == 0


def mixed_request(tmp_path):
    root, directory, request = fixture_values(tmp_path, owner_count=36)
    request['settings']['symbols'][-3:] = [
        {'market': 'us', 'code': code, 'name': 'fixture'}
        for code in ('AAPL.US', 'NVDA.US', 'MSFT.US')]
    admission = request['source_prefetch']['admission']
    admission['ordinary_5m_keys'] = admission['ordinary_5m_keys'][:33]
    admission['members_sha256'] = prefetch.members_digest(request['settings'])
    assert prefetch.admission_matches_request(request['source_prefetch'], request)
    return root, directory, request


@pytest.mark.parametrize('damage', ['code', 'period', 'market', 'order'])
def test_all_members_are_bound_even_when_nonordinary_count_does_not_change(tmp_path, damage):
    root, directory, request = mixed_request(tmp_path)
    if damage == 'code': request['settings']['symbols'][-1]['code'] = 'QQQ.US'
    elif damage == 'period': request['settings']['symbols'][-1]['_monitor_periods'] = ['1m']
    elif damage == 'market': request['settings']['symbols'][-1]['market'] = 'hk'
    else: request['settings']['symbols'][-1], request['settings']['symbols'][-2] = request['settings']['symbols'][-2], request['settings']['symbols'][-1]
    assert len(request['settings']['symbols']) == 36
    assert not prefetch.admission_matches_request(request['source_prefetch'], request)
    worker = SimpleNamespace(closed=False, identity=None, process=SimpleNamespace(is_alive=lambda: True))
    pool = SimpleNamespace(busy=False, closed=False, preparation_worker=worker)
    def forbidden(*args, **kwargs): pytest.fail('changed nonordinary member reached preparation')
    prefetch.run_idle_prefetch(root, directory, request, pool, REVISION, 'one',
        revision_reader=lambda: REVISION, now=lambda: MAIN + 10, prepare=forbidden)
    assert json.loads((root / prefetch.STATUS_FILE).read_text())['stop_reason'] == 'owner_not_idle_completed_batch'


@pytest.mark.parametrize('damage', ['code', 'period'])
def test_dispatch_recomputes_all_final_members_before_accepting_hint(dispatcher, tmp_path, damage):
    manager, popen = dispatcher
    _, _, request = mixed_request(tmp_path / 'fixture')
    hint = request['source_prefetch']
    hint['control']['generation'] = 'generation-one'
    chosen = deepcopy(request['settings']['symbols'])
    if damage == 'code': chosen[-1]['code'] = 'QQQ.US'
    due = {f"{symbol['market']}:{symbol['code']}": ['5m'] for symbol in chosen}
    if damage == 'period': due[f"us:{chosen[-1]['code']}"] = ['1m']
    with pytest.raises(ValueError, match='admission differs'):
        manager.start({'scope': 'symbols', 'symbols': chosen, 'workers': 9,
                       'timeframe_policy': POLICY, 'frequencies': ['1m', '5m', '30m']},
                      due_periods=due, holding_snapshot=[], source_prefetch=hint)
    assert popen.call_count == 0
