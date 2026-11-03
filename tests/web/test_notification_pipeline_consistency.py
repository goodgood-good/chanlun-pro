from copy import deepcopy

from chanlun.screening.timeframe_policy import POLICY
from cl_app.services.notification_policy import trade_notification_rejection
from cl_app.services.dingtalk_notifications import eligible_signal_events
from tests.web.test_notification_state_freshness import ready_event as ready_event
from tests.web.test_signal_monitor import setup as setup
from tests.web.test_native_point_previews import _monitor_preview


def test_waiting_trade_keeps_complete_rejection_evidence_instead_of_state_changed(ready_event):
    service, _, original = ready_event
    event = {**original, 'frequency':'5m', 'timeframe_policy':POLICY, 'stage':'approaching',
             'point_status':'confirmed', 'selection_status':'approaching',
             'lower_confirmation_state':'waiting'}
    state = service._load()
    state['seed']['settings']['timeframe_policy'] = POLICY
    state['signals'][event['id']] = deepcopy(event)
    assert trade_notification_rejection(event, policy_enabled=True) == 'TIMEFRAME_CONFIRMATION_REQUIRED', event
    valid, pending = service._validate_notification_state([event], 'current', True, set(), {})
    assert not valid and not pending
    rejection, = valid.rejections
    assert rejection['reason'] == 'TIMEFRAME_CONFIRMATION_REQUIRED'
    for key in ('point_type','signal_type','point_status','selection_status','lower_confirmation_state'):
        assert rejection[key] == event.get(key)
    assert not eligible_signal_events([event], since=0, stamp=service.now().timestamp())


def test_native_preview_keeps_early_warning_but_waiting_formal_point_is_recorded_internally(setup, monkeypatch):
    service, _, row, finish = _monitor_preview(setup, monkeypatch, point_type='1buy')
    state = service._load()
    state['seed']['settings']['timeframe_policy'] = POLICY
    waiting = deepcopy(row)
    waiting.update(timeframe_policy=POLICY, operating_role='trade', selection_status='approaching',
                   nested_confirmation={'state':'waiting','frequency':'1m'})
    waiting['point'].update(point_id='formal-main',status='confirmed',formal_point_issued=True,
        confirmed_at=row['source_closed_at'],missing_conditions=[],rule_basis='user_declared_native_center_macd_v6')
    assert trade_notification_rejection(next(iter(state['signals'].values())), policy_enabled=True) is None
    finish([waiting])
    recorded = next(e for e in state['events'] if e.get('point_id') == 'formal-main')
    assert recorded['stage'] == 'approaching' and recorded['point_status'] == 'confirmed'
    assert recorded['external_notifications'] is False
    assert recorded['notification_suppressed_reason'] == 'TIMEFRAME_CONFIRMATION_REQUIRED'
    ready = deepcopy(waiting)
    ready.update(selection_status='confirmed',selection_available_at=row['source_closed_at'],
                 nested_confirmation={'state':'confirmed','frequency':'1m'})
    finish([ready])
    completed = next(e for e in state['events'] if e.get('point_id') == 'formal-main' and e['stage']=='confirmed')
    assert completed.get('external_notifications',True)
