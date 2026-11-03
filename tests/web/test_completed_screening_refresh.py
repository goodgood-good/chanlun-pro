"""A rule upgrade refreshes completed screening with bounded retry attempts."""
from copy import deepcopy
from datetime import timedelta

import pytest

from tests.web.test_signal_monitor import setup


def completed(setup, *, morning=False):
    service,manual,live,clock=setup
    clock[0]=clock[0].replace(hour=11 if morning else 15,minute=35 if morning else 11)
    cutoff=int(clock[0].replace(hour=11 if morning else 15,minute=30 if morning else 0).timestamp())
    service.tick();manual.finish(cutoff);service.tick()
    phase='morning' if morning else 'afternoon'
    assert service._load()['daily'][phase]['status']=='completed'
    return service,manual,live,clock,cutoff,phase


def stale(manual):
    manual.state.update(source_current=False,frozen_native_review=True,source_revision='old',current_source_revision='new')


def test_completed_old_rule_run_refreshes_once_and_keeps_live_notifications_enabled(setup):
    service,manual,_,_,cutoff,phase=completed(setup)
    service._load()['dingtalk_enabled']=True
    stale(manual);service.tick()
    assert len(manual.started)==2
    assert manual.state['external_notifications'] is False
    assert service._load()['dingtalk_enabled'] is True
    assert service._load()['daily'][phase]['refresh_reason']=='rules_updated'
    service.tick();assert len(manual.started)==2
    manual.finish(cutoff);service.tick()
    stale(manual);service.tick()
    assert len(manual.started)==2, 'one completed old snapshot was refreshed repeatedly for the same revision'


def test_new_manual_completion_is_adopted_before_a_later_rule_upgrade(setup):
    service,manual,_,_,cutoff,phase=completed(setup)
    previous=service._load()['daily'][phase]['run_id']
    manual.start(deepcopy(manual.state['settings']));manual.finish(cutoff);service.tick()
    assert service._load()['daily'][phase]['run_id']==manual.state['run_id']!=previous
    stale(manual);service.tick()
    assert len(manual.started)==3


@pytest.mark.parametrize('minutes',[5,21])
def test_noon_rule_refresh_has_no_twenty_minute_deadline(setup,minutes):
    service,manual,_,clock,_,phase=completed(setup,morning=True)
    clock[0]+=timedelta(minutes=minutes)
    stale(manual);service.tick()
    assert len(manual.started)==2
    assert 'deadline_at' not in service._load()['daily'][phase]
    assert 'deadline_at' not in manual.state and manual.started[-1]['frequencies']==['5m','30m']


def test_custom_manual_scope_is_not_replaced_by_a_full_market_refresh(setup):
    service,manual,_,_,_,_=completed(setup)
    stale(manual);manual.state['settings']['point_types']=['1buy']
    service.tick()
    assert len(manual.started)==1


def test_rule_refresh_failures_still_obey_the_retry_limit(setup):
    service,manual,_,clock,_,phase=completed(setup)
    stale(manual);service.tick()
    service._load()['daily'][phase]['attempts']=3
    manual.state.update(status='failed',source_current=True,source_revision='new',current_source_revision='new',error='data unavailable')
    clock[0]+=timedelta(minutes=20)
    service.tick()
    assert len(manual.started)==2
