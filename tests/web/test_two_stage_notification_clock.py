from copy import deepcopy

import pytest

from chanlun.screening.timeframe_policy import POLICY
from cl_app.services.signal_monitor import _signal
from cl_app.services.notification_freshness import notification_deadline, notification_is_live
from cl_app.services.notification_text import signal_status_lines
from cl_app.services.dingtalk_notifications import DingTalkOutbox, _retain_recorded_times
from tests.web.test_dingtalk_notifications import Transport


def trade():
    event = _signal({'code':'SH.600088','frequency':'5m','timeframe_policy':POLICY,
        'operating_role':'trade','selection_status':'confirmed','selection_available_at':4000,
        'source_closed_at':4000,'nested_confirmation':{'state':'confirmed','frequency':'1m'},
        'point':{'point_id':'two-stage','point_type':'2buy','status':'confirmed',
                 'available_at':1000,'confirmed_at':1000,'anchor_at':900,'anchor_price':12.3}})
    return {**event,'recorded_at':4001,'change':'changed'}


def test_first_completed_trade_has_its_own_market_window_and_one_delivery(tmp_path):
    event=trade();transport=Transport()
    assert notification_deadline(event)==5800 and notification_is_live(event,4001)
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:4001)
    box.signal_events([event],since=0)
    box.deliver_one()
    assert len(transport.messages)==1
    box.signal_events([event],since=0)
    box.deliver_one()
    assert len(transport.messages)==1


@pytest.mark.parametrize('change',[
    {'timeframe_policy':None},{'lower_confirmation_state':'waiting'},
    {'selection_status':'approaching'},{'operating_role':'short_term'},
    {'frequency':'1m'},{'selection_available_at':999},{'selection_available_at':float('nan')},
    {'available_at':4002},
])
def test_only_a_consistent_two_stage_confirmation_uses_the_trade_clock(change):
    assert not notification_is_live({**trade(),**change},4003)


def test_quote_refresh_and_restart_cannot_rejuvenate_the_trade():
    original=trade()
    refreshed={**original,'source_closed_at':9000,'available_at':9000,'selection_available_at':9000}
    merged,=_retain_recorded_times([refreshed],[original])
    assert notification_deadline(merged)==5800
    assert not notification_is_live(merged,9000)
    assert not notification_is_live({**original,'recorded_at':9000,'source_closed_at':9000},9000)
    assert notification_deadline({**original,'notification_expires_at':2800})==2800


def test_short_trade_uses_frozen_background_and_marks_missing_background():
    event={'frequency':'1m','point_type':'1buy','stage':'confirmed','operating_role':'short_term',
        'higher_context':{'background_30m':{'state':'verified','direction':'down','alignment':'countertrend'}}}
    assert '逆势风险' in '\n'.join(signal_status_lines(event))
    assert '背景待核验' in '\n'.join(signal_status_lines({**event,'higher_context':None}))
    frozen=deepcopy(event);frozen['background_30m']={'direction':'up','alignment':'aligned'}
    text='\n'.join(signal_status_lines(frozen))
    assert '顺势' in text and '逆势风险' not in text
