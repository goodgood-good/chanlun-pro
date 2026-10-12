"""Noon scans use important periods and one shared finite retry budget."""
from copy import deepcopy
from datetime import timedelta

from cl_app.services import monitor_schedule
from cl_app.services.signal_monitor import monitor_settings
from tests.web.test_signal_monitor import setup
from tests.web.test_monitor_incremental_priority import monitor, publish_candidate, consume_increment


def test_noon_uses_5m_30m_without_changing_afternoon_or_live_1m(setup):
    service, manual, live, clock = setup
    clock[0]=clock[0].replace(hour=11,minute=35)
    service._load()['monitor_frequencies']=['1m','5m','30m']
    original=deepcopy(service._load()['settings']['screening'])
    service.tick()
    assert manual.started[0]['frequencies']==['5m','30m']
    assert manual.state['deadline_at']==(clock[0]+timedelta(minutes=20)).timestamp()
    assert service._load()['settings']['screening']==original
    assert '1m' in monitor_settings(service._load(),manual.started[0])['frequencies']


def test_noon_retry_reuses_original_deadline_instead_of_resetting_twenty_minutes(setup):
    service, manual, live, clock = setup
    clock[0]=clock[0].replace(hour=11,minute=35)
    service.tick()
    deadline=manual.state['deadline_at']
    manual.state.update(status='failed',error='PermissionError: failed retirement')
    assert service._load()['daily']['morning']['retry_at']==(clock[0]+timedelta(minutes=1)).timestamp()
    clock[0]+=timedelta(minutes=1)
    service.tick()
    assert len(manual.started)==2 and manual.state['deadline_at']==deadline
    manual.state.update(status='failed',error='PermissionError: failed retirement')
    clock[0]+=timedelta(minutes=20)
    service.tick()
    assert len(manual.started)==2
    assert service._load()['daily']['morning']['status']=='timed_out'


def test_late_noon_start_cannot_run_past_1255(setup):
    service, manual, _, clock=setup
    clock[0]=clock[0].replace(hour=12,minute=50)
    service.tick()
    assert manual.state['deadline_at']==clock[0].replace(minute=55).timestamp()


def test_budget_stop_is_visible_as_partial_and_never_whole_market_completion(setup):
    service, manual, _, clock=setup
    clock[0]=clock[0].replace(hour=11,minute=35)
    service.tick()
    manual.state.update(status='cancelled',stop_reason='time_budget',completed=6000,total=10000,
                        finished_at=manual.state['deadline_at'])
    service._record_daily_run(manual.status())
    state=service._load()
    assert state['daily']['morning']['status']=='partial'
    view=monitor_schedule.describe(state,manual.status(),clock[0])
    assert view['status']=='partial' and view['completed_phases']==0
    assert '6000 / 10000' in view['detail'] and '待重试' not in view['detail']
    clock[0]+=timedelta(minutes=21)
    service.tick()
    assert len(manual.started)==1


def test_time_budget_keeps_audited_candidates_and_imports_the_last_committed_prefix(monitor):
    service, manual, live, clock=monitor
    stock=publish_candidate(manual)
    manual.state.update(status='cancelled',stop_reason='time_budget')
    consume_increment(service,manual,clock)
    seed=service._load()['seed']
    assert seed['codes']==['a:SH.600088','a:SZ.000001']
    assert seed['incremental_screening_symbols']['a:SZ.000001']['code']==stock['code']
    assert not service._load()['signals'], 'screening membership still requires independent live confirmation'
    consume_increment(service,manual,clock)
    assert 'a:SZ.000001' in service._load()['seed']['codes']


def test_failed_noon_start_does_not_extend_the_budget(setup,monkeypatch):
    service,manual,_,clock=setup
    clock[0]=clock[0].replace(hour=11,minute=35)
    def unavailable(*args,**kwargs):
        raise OSError('fixture startup unavailable')
    monkeypatch.setattr(manual,'start',unavailable)
    service.tick()
    deadline=service._load()['daily']['morning']['deadline_at']
    clock[0]+=timedelta(minutes=21)
    service.tick()
    assert service._load()['daily']['morning']['deadline_at']==deadline
    assert service._load()['daily']['morning']['status']=='timed_out'


def test_noon_phase_is_valid_when_manual_settings_use_one_period_macd_filter(setup):
    from cl_app.services.screening import validate_settings
    service,manual,_,clock=setup
    clock[0]=clock[0].replace(hour=11,minute=35)
    original=service._load()['settings']['screening']
    original.update(frequencies=['5m'],minimum_operating_frequency='5m')
    service.tick()
    assert validate_settings(manual.started[0])['frequencies']==['5m','30m']
    assert original['minimum_operating_frequency']=='5m'


def test_requested_policy_handoff_restarts_daily_scan_once_after_source_revision_changes(setup):
    service,manual,_,clock=setup
    clock[0]=clock[0].replace(hour=15,minute=11)
    service.tick()
    manual.state.update(status='cancelled',stop_reason='policy_update',source_current=False,
                        source_revision='old',current_source_revision='new')
    service._load()['daily']['afternoon']['attempts']=3
    service.tick()
    assert len(manual.started)==2
    assert service._load()['daily']['afternoon']['status']=='started'
    assert service._load()['daily']['afternoon']['source_recovery_revision']=='new'
    manual.state.update(status='cancelled',stop_reason='policy_update',source_current=False,
                        source_revision='old',current_source_revision='new')
    service._load()['daily']['afternoon']['attempts']=3
    service.tick()
    assert len(manual.started)==2
