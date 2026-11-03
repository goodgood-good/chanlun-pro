from copy import deepcopy
import json

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, image_event


def notice(cycle=1,stage='warning',*,anchor=31.13,anchor_at=900):
    return {**image_event(),'id':'same-hypothesis','code':'SZ.301004','frequency':'1m',
            'signal_type':'consolidation_divergence','point_type':'consolidation_down',
            'hypothesis_id':'same-leg','lifecycle_cycle':cycle,'stage':stage,'point_status':stage,
            'selection_status':stage,'anchor_at':anchor_at,'anchor_price':anchor,'price_basis_revision':'basis',
            'recorded_at':1000.,'source_closed_at':1000.,'available_at':1000.}


def box(tmp_path,clock,transport=None):
    return DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport or ImageTransport(),
                         now=lambda:clock[0],signal_validator=lambda rows:(rows,[]))


def test_warning_state_oscillation_at_same_pivot_does_not_send_again_after_restart(tmp_path):
    clock=[1000.];transport=ImageTransport();sender=box(tmp_path,clock,transport)
    for cycle,stage in [(1,'warning'),(1,'invalidated'),(2,'warning'),(2,'invalidated'),(3,'warning')]:
        event=notice(cycle,stage);event['recorded_at']=event['available_at']=event['source_closed_at']=clock[0]
        sender.signal_events([event],since=0);sender.deliver_one();clock[0]+=5
        sender=box(tmp_path,clock,transport)
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE kind='signals' AND status='sent'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='suppressed'").fetchone()[0]==3
    sender.signal_events([notice(4,anchor_at=950)],since=0);sender.deliver_one();clock[0]+=5
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]==2
    for event in [notice(5,anchor=31.11,anchor_at=1000),notice(5,'confirmed',anchor=31.11,anchor_at=1000),
                  {**notice(6),'hypothesis_id':'different-leg','id':'new-hypothesis'}]:
        event['recorded_at']=event['available_at']=event['source_closed_at']=clock[0]
        sender.signal_events([event],since=0);sender.deliver_one();clock[0]+=5
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]==5


def test_previously_queued_duplicates_are_checked_again_before_transport(tmp_path):
    clock=[1000.];sender=box(tmp_path,clock)
    sender.signal_events([notice(1)],since=0)
    sender.signal_events([notice(2)],since=0)
    sender.deliver_one();clock[0]+=5;sender.deliver_one()
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='suppressed'").fetchone()[0]==1


def test_upgrade_seeds_only_successful_legacy_receipts(tmp_path):
    clock=[1000.];sender=box(tmp_path,clock)
    sender.signal_events([notice()],since=0);sender.deliver_one()
    with sender._connect() as db:
        db.execute("DELETE FROM markers WHERE id LIKE 'semantic-signal-v1:%'")
        db.execute("DELETE FROM delivery_settings WHERE key='semantic_signal_dedup_v1'")
    clock[0]+=5;sender=box(tmp_path,clock)
    sender.signal_events([notice(2)],since=0);sender.deliver_one()
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='suppressed'").fetchone()[0]==1


def test_same_price_can_notify_a_different_destination_but_not_repeat_within_it(tmp_path):
    clock=[1000.];sender=box(tmp_path,clock)
    for cycle,held in [(1,False),(2,True),(3,True)]:
        event={**notice(cycle),'signal_type':'native_point_preview','point_type':'1buy',
               'preview_key':'same-leg','holding':held,'stage':'approaching','point_status':'approaching',
               'selection_status':'approaching','recorded_at':clock[0]}
        sender.signal_events([event],since=0);sender.deliver_one();clock[0]+=5
    with sender._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='sent'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE status='suppressed'").fetchone()[0]==1
