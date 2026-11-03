from cl_app.services.monitor_fairness import live_batch_waves, service_pressure
from chanlun.screening.timeframe_policy import POLICY
from tests.web.test_notification_monitor_repair import fair_plan
from tests.web.test_monitor_live_priority import mixed as mixed
from tests.web.test_monitor_fair_recheck import configure_pool
from datetime import datetime
from chanlun.screening.rules import CN
import pytest


def test_long_tasks_bound_the_prequeued_live_slice_without_a_one_wave_barrier():
    plan = fair_plan()
    plan['period_costs']['5m']['seconds'] = 12
    assert live_batch_waves(plan) == 2
    plan['period_costs']['5m']['seconds'] = 40
    assert live_batch_waves(plan) == 1


def test_pressure_counts_only_independently_enabled_periods():
    state = {'seed':{'settings':{'timeframe_policy':POLICY,'frequencies':['1m','5m','30m']}},
             'holding_source_current':True,'holding_symbols':[],
             'monitor_period_costs':{'1m':{'seconds':5,'samples':10},
                 '5m':{'seconds':3,'samples':10},'30m':{'seconds':8,'samples':10}}}
    pressure = service_pressure(state, {}, {}, {}, {'a:one','a:two'}, 1000, 16)
    assert pressure['recurring_worker_seconds'] == .02
    state['holding_symbols'] = ['a:one']
    pressure = service_pressure(state, {}, {}, {}, {'a:one','a:two'}, 1000, 16)
    assert pressure['recurring_worker_seconds'] == round(.02 + 5/60 + 8/1800, 3)


def test_closing_backlog_estimates_due_work_without_claiming_recurring_market_demand():
    state = {'seed':{'settings':{'frequencies':['5m']}},
             'monitor_period_costs':{'5m':{'seconds':3,'samples':10}}}
    pressure = service_pressure(state, {'a:one':['5m']}, {'a:one:5m':1000}, {},
        {'a:one'}, 1600, 16, recurring_keys=set())
    assert pressure['recurring_worker_seconds'] == 0
    assert pressure['deadline_worker_seconds'] > 0


@pytest.mark.parametrize('age,live_mode', [(600,True),(1900,False)])
def test_unexpired_closing_checks_keep_live_capacity_until_market_delivery_deadline(mixed, monkeypatch, age, live_mode):
    from cl_app.services import signal_monitor
    service, jobs, clock, closed, _, _ = configure_pool(mixed,0,40)
    clock[0] = datetime.fromtimestamp(closed['a']+age,CN)
    state = service._load()
    state['force'] = True
    mixed[1].state['status'] = 'running'
    monkeypatch.setattr(signal_monitor,'cpu_capacity',lambda:16)
    service._schedule_monitor(clock[0],None)
    assert jobs.started
    assert (state['active_monitor_mode']=='live') is live_mode
    assert (jobs.started[-1]['workers']>2) is live_mode
