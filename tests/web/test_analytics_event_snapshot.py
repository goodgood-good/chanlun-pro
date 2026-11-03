from copy import deepcopy
import sqlite3

from cl_app.services.signal_analytics import SignalAnalytics, snapshot_events
from cl_app.services.signal_monitor import SignalMonitor


def event():
    return {'id':'a','lifecycle_cycle':2,'market':'a','frequency':'5m','point_type':'1buy',
            'signal_type':'native_point_preview','stage':'approaching','change':'new',
            'recorded_at':1001,'source_closed_at':1000,'confirmation_delay_bars':3,
            'exit_reasons':['reason'],'external_notifications':False,
            'point':{'large_geometry':[{'unused':[1,2,3]}]*100},'evidence':{'path':'saved'}}


def test_projected_events_persist_identical_statistics(tmp_path):
    original=SignalAnalytics(tmp_path/'full',now=lambda:1000)
    projected=SignalAnalytics(tmp_path/'projected',now=lambda:1000)
    events=[event(),{'id':'b','frequency':'1m','point_type':'2buy','stage':'confirmed','recorded_at':1002}]
    original.record(deepcopy(events),revision='r')
    projected.record(snapshot_events(events),revision='r')
    def rows(analytics):
        with sqlite3.connect(analytics.path) as db:return db.execute('SELECT * FROM events ORDER BY id').fetchall()
    assert rows(original)==rows(projected)
    assert original.last_signature==projected.last_signature


def test_mutable_statistic_fields_are_isolated_but_geometry_is_not_traversed():
    class Uncopyable:
        def __deepcopy__(self,memo):raise AssertionError('unused geometry copied')
    source=event();source['point']=Uncopyable();captured=snapshot_events([source])
    source['exit_reasons'].append('later')
    assert captured[0]['exit_reasons']==['reason']
    assert 'point' not in captured[0] and 'evidence' not in captured[0]


def test_custom_analytics_consumer_keeps_full_independent_payload(tmp_path):
    class Custom(SignalAnalytics):
        pass
    monitor=SignalMonitor.__new__(SignalMonitor);monitor._analytics=Custom(tmp_path,now=lambda:1000)
    source=event();captured=monitor._snapshot_analytics_events([source])
    assert captured==[source] and 'point' in captured[0]
    source['point']['large_geometry'][0]['unused'].append(4)
    assert captured!=[source]


def test_standard_analytics_uses_projection_and_instance_override_does_not(tmp_path):
    monitor=SignalMonitor.__new__(SignalMonitor);monitor._analytics=SignalAnalytics(tmp_path,now=lambda:1000)
    source=event();assert 'point' not in monitor._snapshot_analytics_events([source])[0]
    monitor._analytics.record=lambda events,*args,**kwargs:None
    assert 'point' in monitor._snapshot_analytics_events([source])[0]
