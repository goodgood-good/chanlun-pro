import json

from chanlun import config
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.monitor_view import paged_state
from cl_app.services.notification_text import event_priority, queued_priority
from tests.web.test_dingtalk_notifications import Transport, event
from tests.web.test_monitor_pagination import sample
from tests.web.test_notification_charts import ImageTransport, PNG, image_event


def test_holdings_are_first_and_filtering_precedes_server_pagination():
    state = sample(125)
    state['holding_symbols'] = ['a:SH.000000', 'a:SH.000001']
    state['holding_source_current'] = True
    result = paged_state(state, {'signals_size':25}, 'current', True,
                         expected_cutoffs={key:1000 for key in state['selection_checks']})
    assert list(result['signals'])[:2] == ['1', '0']
    assert all(row['holding'] for row in list(result['signals'].values())[:2])
    assert result['holding_summary'] == {'count':2,'signals':2,'ready':1,'updating':0,'limited':1,'pending':0,'source_current':True,
                                         'risk_bound_symbols':0,'risk_unbound_symbols':2,'risk_attention_symbols':0}
    selected = paged_state(state, {'signals_scope':'holdings','signals_page':99}, 'current', True)
    assert set(selected['signals']) == {'0','1'}
    assert selected['pagination']['signals']['page'] == 1
    assert selected['pagination']['signals']['total'] == 2
    assert selected['pagination']['signals']['overall'] == 125
    # Membership comes from the current group, rather than a stale signal flag.
    state['signals']['1']['holding'] = True
    state['holding_symbols'] = ['a:SH.000000']
    assert list(paged_state(state, {'signals_scope':'holdings'}, 'current', True)['signals']) == ['0']


def test_holdings_leave_stale_evidence_hidden_and_report_membership_uncertainty():
    state=sample(2)
    state.update(holding_symbols=['a:SH.000001'],holding_source_current=False)
    state['selection_checks']['a:SH.000001:5m']['revision']='old'
    result=paged_state(state, {'signals_scope':'holdings'}, 'current', True)
    assert not result['signals']
    assert result['holding_summary']['source_current'] is False
    assert result['holding_summary']['pending'] == 1


def test_risk_reference_coverage_counts_symbols_and_ignores_removed_or_inactive_plans():
    state=sample(2)
    state['holding_symbols']=['a:SH.000000','a:SH.000001']
    state['holding_risk_plans']={
        'first':{'market':'a','code':'SH.000000','frequency':'1m','active':True,'status':'active'},
        'second':{'market':'a','code':'SH.000000','frequency':'5m','active':True,'status':'requires_review'},
        'inactive':{'market':'a','code':'SH.000001','frequency':'5m','active':False,'status':'detached'},
        'removed':{'market':'us','code':'OLD.US','frequency':'5m','active':True,'status':'active'},
    }
    summary=paged_state(state,{},'current',True)['holding_summary']
    assert summary['risk_bound_symbols']==1
    assert summary['risk_unbound_symbols']==1
    assert summary['risk_attention_symbols']==1


def test_holding_messages_are_separate_and_sent_before_ordinary_messages(tmp_path):
    transport=Transport();box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000,
                                           signal_validator=lambda rows:(rows,[]))
    regular={**event('confirmed',identity='ordinary'),'name':'普通关注'}
    held={**event('confirmed',identity='held'),'name':'持仓样例','holding':True}
    box.signal_events([regular,held],since=0)
    with box._connect() as db:
        batches=[json.loads(row[0]) for row in db.execute('SELECT events FROM signal_payloads')]
    assert len(batches)==2 and all(len(rows)==1 for rows in batches)
    box.deliver_one()
    assert len(transport.messages)==1
    assert transport.messages[0].startswith('【我的持仓·已确认】持仓样例')
    assert '普通关注' not in transport.messages[0]


def test_an_unverified_holding_cannot_starve_ready_ordinary_messages(tmp_path):
    clock=[1000.];transport=Transport()
    def validate(rows):
        return ([row for row in rows if not row.get('holding')],[row for row in rows if row.get('holding')])
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],signal_validator=validate)
    box.signal_events([{**event('confirmed',identity='ordinary'),'name':'普通关注'},
                       {**event('confirmed',identity='held'),'holding':True}],since=0)
    box.deliver_one();assert not transport.messages
    clock[0]+=6;box.deliver_one()
    assert len(transport.messages)==1 and '普通关注' in transport.messages[0]


def test_holdings_chart_is_rendered_first_and_sent_before_ordinary_messages(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ALERT_CHART_PUBLIC_BASE_URL','https://example.test')
    clock=[1000.];transport=ImageTransport();rendered=[]
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:clock[0],charts=True,
                      signal_validator=lambda rows:(rows,[]))
    box.charts.check_public=False
    box.charts.renderer=lambda row,root:rendered.append(row['id']) or PNG
    regular=image_event(identity='ordinary')
    held={**image_event(identity='held'),'holding':True}
    box.charts.enqueue([regular,held],1000)
    box.charts.process_one()
    assert rendered==['held']
    box.signal_events([regular],since=0)
    box.deliver_one()
    assert not transport.messages and len(transport.images)==1
    assert '我的持仓' in transport.images[0][0]


def test_unverified_holding_chart_yields_and_malformed_payloads_have_no_priority(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ALERT_CHART_PUBLIC_BASE_URL','https://example.test')
    clock=[1000.];rendered=[]
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=ImageTransport(),now=lambda:clock[0],charts=True,
        signal_validator=lambda rows:([],rows) if rows[0].get('holding') else (rows,[]))
    box.charts.check_public=False
    box.charts.renderer=lambda row,root:rendered.append(row['id']) or PNG
    box.charts.enqueue([image_event(identity='ordinary'),{**image_event(identity='held'),'holding':True}],1000)
    box.charts.process_one();assert not rendered
    clock[0]+=6;box.charts.process_one();assert rendered==['ordinary']
    for payload in [None,'invalid','null','[1,2]','true']:
        assert queued_priority(payload)==0
    assert event_priority([{'signal_type':'holding_risk'}])>event_priority([{'holding':True}])>event_priority([{}])
