"""Regressions from the October 20--26 delivery and monitor audit."""
from copy import deepcopy
import json
import sqlite3

import pytest

from cl_app.services.monitor_maintenance import prune_diagnostics, recover_warning_rechecks, notification_priority_periods, warning_recheck_symbols
from cl_app.services.notification_metrics import summarize
from cl_app.services.notification_charts import migrate_history_limits
from cl_app.services.monitor_view import paged_state
from cl_app.services.signal_monitor import SignalMonitor, _clear_period_retries
from tests.web.test_signal_monitor import setup as setup
from tests.web.test_native_point_previews import _monitor_preview
from tests.web.test_monitor_pagination import sample


def test_crossed_bar_uses_actual_source_for_delivery_delay_and_keeps_task_clock_separate():
    item = {'market':'a', 'frequency':'5m', 'source_closed_at':1300,
            'notification_timeline':{'source_closed_at':1000, 'scheduled_at':1295,
                'calculation_started_at':1302, 'fetched_at':1303, 'calculation_finished_at':1306,
                'detected_at':1307}}
    measures = summarize([{'kind':'signals', 'created':1307, 'send_started':1309, 'sent':1310,
                           'events':json.dumps([item])}])[0]['measures']
    assert measures['cutoff_to_sent']['average_seconds'] == 10
    assert measures['task_cutoff_to_sent']['average_seconds'] == 310
    assert measures['task_schedule_wait']['average_seconds'] == 295
    assert 'schedule_wait' not in measures  # The actual candle closed after dispatch.


def test_removed_symbol_diagnostics_are_pruned_without_hiding_current_or_startup_errors():
    kept = {'market':'a', 'code':'SH.600000', 'reasons':['POINT_PREVIEW_CHECK_PENDING']}
    gone = {**kept, 'code':'SH.600001'}
    state = {'seed':{'codes':['a:SH.600000']}, 'errors':[gone,kept], 'schedule_errors':[gone]}
    assert prune_diagnostics(state)
    assert state['errors'] == [kept] and state['schedule_errors'] == []
    assert not prune_diagnostics(state)
    startup = {'errors':[gone], 'last_error':'worker failed'}
    assert not prune_diagnostics(startup) and startup['errors'] == [gone]


def test_persisting_membership_change_clears_removed_symbol_errors(setup):
    service, _, _, _ = setup
    service.tick()
    state = service._load()
    state['seed'] = {'codes':[], 'symbols':[], 'run_id':'empty-pool',
                     'settings':deepcopy(state['settings']['screening'])}
    state['errors'] = [{'market':'a', 'code':'SH.999999', 'reasons':['POINT_PREVIEW_CHECK_PENDING']}]
    state['last_error'] = '部分标的行情或计算异常，请查看问题列表'
    service._save()
    service._state = None
    assert service._load()['errors'] == [] and service._load()['last_error'] == ''


def test_old_warning_recovery_is_targeted_and_cannot_reset_retry_budget_again():
    state = {'seed':{'codes':['a:one','a:two']}, 'unverified_signals':{
        'old':{'code':'one', 'frequency':'5m', 'signal_type':'native_point_preview'}},
        'retry_symbols':{'a:one':{'failures':3},'a:two':{'failures':3}}, 'next_check_at':9999}
    assert recover_warning_rechecks(state, _clear_period_retries)
    assert state['deferred_monitor_periods'] == {'a:one':['5m']} and state['next_check_at'] == 0
    assert state['retry_symbols'] == {'a:two':{'failures':3}}
    assert warning_recheck_symbols(state) == {'a:one'}
    state['retry_symbols']['a:one'] = {'failures':3}
    assert not recover_warning_rechecks(state, _clear_period_retries) and state['retry_symbols']['a:one']['failures'] == 3


def test_warning_recovery_preserves_other_period_failure_budgets():
    minute = {'failures':3, 'periods':['1m']}
    trade = {'failures':3, 'periods':['5m']}
    state = {'seed':{'codes':['a:one']}, 'unverified_signals':{
        'old':{'code':'one','frequency':'5m','signal_type':'native_point_preview'}},
        'retry_symbols':{'a:one':{'by_period':{'1m':minute,'5m':trade}}}}
    recover_warning_rechecks(state, _clear_period_retries)
    assert state['retry_symbols']['a:one'] == minute
    assert state['deferred_monitor_periods'] == {'a:one':['5m']}


@pytest.mark.parametrize('kind', ['native_point_preview', 'consolidation_divergence'])
def test_verified_basis_change_retires_old_warning_without_market_invalidation_or_external_notice(setup, monkeypatch, kind):
    service, live, _, finish = _monitor_preview(setup, monkeypatch)
    state = service._load()
    original = next(iter(state['signals'].values()))
    original['signal_type'] = kind
    identity = original['id']
    preserved = deepcopy(original)
    monkeypatch.setattr(service, '_prepare_warning_checks', lambda result: {
        identity:{'original':deepcopy(preserved), 'assessment':{'state':'pending',
            'reason_code':'PRICE_BASIS_CHANGED', 'checked_at':preserved['source_closed_at']}, 'continued':None}})
    finish([])
    state = service._load()
    exit_event = next(e for e in state['events'] if e['id']==identity and e['stage']=='left')
    assert exit_event['exit_reasons'] == ['PRICE_BASIS_CHANGED']
    assert exit_event['external_notifications'] is False
    assert exit_event.get('evidence') == preserved.get('evidence')
    assert identity not in state['unverified_signals'] and identity not in state['signals']
    assert state['known'][identity]['stage'] == 'left'
    assert not any(e['id']==identity and e['stage']=='invalidated' for e in state['events'])
    assert not state['retry_symbols'] and not state['errors']


def fair_plan(size=120):
    symbols = [{'market':'a','code':str(i).zfill(4)} for i in range(size)]
    periods = {f"a:{s['code']}":['5m'] for s in symbols}
    return {'symbols':symbols, 'periods':periods, 'targets':{key+':5m':300 for key in periods},
        'state':{'monitor_pending_deadlines':{key+':5m':{'deadline':360} for key in periods}},
        'period_costs':{'5m':{'seconds':2.4}}, 'recent':set(periods), 'urgent':set(),
        'now':361, 'cursor':0, 'decisions':{}}


def test_retrying_unverified_evidence_gets_a_bounded_early_recovery_turn():
    plan=fair_plan()
    plan['recent']=set()
    plan['state'].update(seed={'codes':list(plan['periods'])},
        holding_symbols=['a:0000'],
        unverified_signals={'old':{'market':'a','code':'0119','frequency':'5m'}},
        retry_symbols={'a:0119':{'failures':1,'periods':['5m'],'retry_at':360}})
    before=deepcopy(plan['state']['retry_symbols'])
    plan['urgent']=warning_recheck_symbols(plan['state'])
    tasks,_,_=SignalMonitor._fair_batch(plan,2)
    assert [task['key'] for task in tasks][:2]==['a:0000','a:0119']
    assert plan['state']['retry_symbols']==before


def test_recovery_priority_cannot_import_another_period_or_removed_symbol():
    state={'seed':{'codes':['a:one']},'unverified_signals':{
        'one':{'code':'one','frequency':'5m'},'gone':{'code':'gone','frequency':'5m'}},
        'retry_symbols':{'a:one':{'by_period':{'1m':{'failures':1}}},
                         'a:gone':{'failures':1,'periods':['5m']}}}
    assert warning_recheck_symbols(state)==set()


def test_overdue_live_batch_keeps_workers_fed_and_every_symbol_gets_a_turn():
    plan = fair_plan()
    served = []
    while plan['symbols']:
        tasks, cursor, critical = SignalMonitor._fair_batch(plan, 16)
        assert 0 < len(tasks) <= 64
        if len(plan['symbols']) >= 64:
            assert len(tasks) == 64
        chosen = {t['key'] for t in tasks}
        served.extend(chosen)
        plan['symbols'] = [s for s in plan['symbols'] if f"a:{s['code']}" not in chosen]
        plan['cursor'] = cursor
        plan['state']['monitor_critical_cursor'] = critical
    assert len(served) == len(set(served)) == 120


def test_notification_priority_breaks_equal_deadlines_but_never_overtakes_older_work():
    plan = fair_plan(32)
    plan['state']['notification_priority_periods'] = ['a:0031:5m']
    tasks, _, _ = SignalMonitor._fair_batch(plan, 1)
    assert tasks[0]['key'] == 'a:0031'
    plan['state']['monitor_pending_deadlines']['a:0000:5m']['deadline'] = 350
    tasks, _, _ = SignalMonitor._fair_batch(plan, 1)
    assert tasks[0]['key'] == 'a:0000'


def test_nonadmitted_old_retry_does_not_shrink_a_fresh_live_batch():
    plan = fair_plan()
    for record in plan['state']['monitor_pending_deadlines'].values():
        record['deadline'] = 500
    plan['state']['monitor_pending_deadlines']['a:removed:5m'] = {'deadline':1}
    tasks, _, _ = SignalMonitor._fair_batch(plan, 16)
    assert len(tasks) == 64


def test_notification_priority_respects_current_membership_and_preferences():
    signal = {'market':'a', 'code':'one', 'frequency':'5m', 'stage':'approaching', 'point_type':'3sell'}
    state = {'signals':{'old':signal}, 'holding_source_current':True, 'holding_symbols':[]}
    assert notification_priority_periods(state) == []
    state['holding_symbols'] = ['a:one']
    assert notification_priority_periods(state) == ['a:one:5m']
    state['dingtalk_enabled'] = False
    assert notification_priority_periods(state) == []


def test_unverified_market_contract_is_counted_as_uncovered_with_reason_without_mutating_pool():
    state = sample()
    symbol = {'market':'fx','code':'FE.USDCNY','name':'FX'}
    state['seed']['symbols'].append(symbol)
    state['schedule_errors'] = [{**symbol, 'reasons':['SESSION_CALENDAR_UNAVAILABLE']}]
    original = deepcopy(state)
    result = paged_state(state, {'pool_q':'FE.USDCNY'}, 'current', True)
    row, = result['seed']['symbols']
    assert row['coverage_state'] == 'unsupported' and '交易时段' in row['coverage_message']
    assert result['coverage']['unsupported'] == 1 and result['coverage']['total'] == 126
    assert state == original


def test_history_limit_migration_keeps_validation_failures_and_missing_panels_failed():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE settings(k TEXT PRIMARY KEY,v REAL)')
    db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,error TEXT,history_coverage TEXT,present_periods TEXT,missing_periods TEXT,initial_image_hash TEXT)')
    history = {'1m':{'state':'target_reached'},'5m':{'state':'target_reached'},'30m':{'state':'unsupported'}}
    for identity in ('limited','invalid','missing','no_image'):
        coverage = deepcopy(history)
        if identity == 'invalid':
            coverage['30m']['state'] = 'invalid'
        present = ['1m','5m'] if identity == 'missing' else ['1m','5m','30m']
        missing = ['30m'] if identity == 'missing' else []
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (identity,'failed','历史范围受限：30m来源暂不支持',
            json.dumps(coverage),json.dumps(present),json.dumps(missing),'' if identity=='no_image' else 'image'))
    migrate_history_limits(db)
    migrate_history_limits(db)
    assert dict(db.execute('SELECT id,status FROM jobs')) == {
        'limited':'limited','invalid':'failed','missing':'failed','no_image':'failed'}
