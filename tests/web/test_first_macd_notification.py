"""An early warning reaches text delivery before asynchronous chart work."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from cl_app.services.notification_text import signal_status_lines
from cl_app.services.signal_monitor import _signal, _consolidation_signal
from tests.web.test_consolidation_messages import combined, alert
from tests.web.test_signal_monitor import setup, _preview_service
from tests.core.strict_structure.test_first_macd_contraction import extending_point, points


@pytest.mark.parametrize('stage',['warning','confirmed','invalidated'])
def test_consolidation_text_is_sent_without_waiting_for_any_image(combined,stage):
    box,transport,clock,rendered=combined
    row=alert(stage,inline=False)
    box.signal_events([row],since=0)
    box.deliver_one()
    assert len(transport.messages)==1 and not rendered and not transport.images
    assert box.charts.snapshot()['pending']==1
    box.charts.process_one();clock[0]+=5;box.deliver_one()
    assert len(transport.images)==1 and len(transport.messages)==1
    box.signal_events([row],since=0);box.deliver_one()
    assert len(transport.messages)==1


def test_image_failure_does_not_delay_or_erase_the_warning_text(combined):
    box,transport,clock,_=combined
    box.charts.renderer=lambda *_:(_ for _ in ()).throw(ValueError('fixture render failure'))
    box.signal_events([alert(inline=False)],since=0);box.deliver_one()
    for _ in range(3):box.charts.process_one();clock[0]+=31
    assert len(transport.messages)==1 and not transport.images
    assert box.charts.snapshot()['failed']==1
    assert box.snapshot()['sent']==1


def test_first_point_notification_exposes_the_actual_and_estimated_area():
    parts=extending_point('1',False);point,=points(parts)
    row=_signal({'market':'a','code':'SH.600000','frequency':'5m','point':point,
                 'source_closed_at':point['available_at']})
    lines='\n'.join(signal_status_lines(row))
    assert '开始缩短' in lines and '按×2估算' in lines
    assert row['estimated_area_c']==2*row['macd_area_c']
    assert '连续两次' not in lines and '局部价格转折' not in lines
    withdrawn={**row,'stage':'invalidated','point_status':'invalidated','invalidation_reason':'动能恢复'}
    assert '当前结构条件满足' not in '\n'.join(signal_status_lines(withdrawn))


def test_consolidation_warning_shows_the_doubled_area_estimate(combined):
    box,transport,_,_=combined
    row={**alert(inline=False),'preview_trigger':'macd_first_contraction',
         'macd_area_a':100.,'macd_area_c':20.,'macd_area_ratio':.2,'estimate_factor':2,'estimated_area_c':40.}
    box.signal_events([row],since=0);box.deliver_one()
    assert '2C/A：40.0%' in transport.messages[0]
    assert '开始缩短' in transport.messages[0] and '局部价格转折' not in transport.messages[0]


def test_warning_recovers_on_the_first_new_closed_bar_after_invalidation(setup,monkeypatch):
    from tests.web.warning_helpers import refute_warning
    refute_warning(monkeypatch)
    service,live,cutoff,warning=_preview_service(setup,monkeypatch)
    service.check_now();service.tick();live.consolidation=[];live.finish(cutoff);service.tick()
    invalid=service._load()['events'][0]
    assert invalid['stage']=='invalidated'
    cutoff+=300
    clock=setup[3];clock[0]=datetime.fromtimestamp(cutoff+1,clock[0].tzinfo)
    current=deepcopy(warning);current['source_closed_at']=cutoff;current['alert']['available_at']=cutoff
    service.check_now();service.tick();live.consolidation=[current];live.finish(cutoff);service.tick()
    recovered=service._load()['events'][0]
    assert recovered['stage']=='warning' and recovered['external_notifications']
    assert not recovered['warning_recovery_pending']
    assert recovered['lifecycle_cycle']==invalid['lifecycle_cycle']+1
