"""Notification effects are isolated from the real robot in every test."""

from copy import deepcopy
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from cl_app.services.dingtalk_notifications import DeliveryError, DingTalkOutbox, DingTalkTransport, signal_status_lines, signal_key


class Transport:
    def __init__(self):
        self.messages = []
        self.error = False

    @staticmethod
    def configuration():
        return {"configured": True, "configuration_error": "", "keyword_configured": True}

    def send(self, text):
        if self.error:
            raise DeliveryError("模拟网络失败")
        self.messages.append(text)


def event(stage="approaching", *, identity="point", stamp=1000):
    return {"id": identity, "market": "a", "code": "SH.600088", "name": "样例",
            "frequency": "5m", "point_type": "3buy", "stage": stage, "change": "discovered",
            "recorded_at": stamp, "source_closed_at": stamp, "available_at": stamp, "anchor_price": 12.3}


def result():
    return {"run_id": "a"*32, "completed": 5, "total": 5, "selected": [], "observations": [],
            "errors": [{"market": "us", "code": "EXAMPLE.US"}], "finished_at": 1000, "source_current": True}


def test_native_center_notification_uses_confirmed_point_status():
    lines = signal_status_lines({
        "frequency": "5m", "point_type": "1buy", "point_status": "confirmed",
        "stage": "confirmed", "evidence_scope": "native_center_signal",
    })
    assert "已确认" in lines[0]
    assert "观察" not in lines[0]


def test_early_consolidation_states_dedupe_across_restart_and_allow_a_new_episode(tmp_path):
    transport, clock = Transport(), [1000.]
    path = tmp_path / "early.sqlite3"
    outbox = DingTalkOutbox(path, transport=transport, now=lambda: clock[0])
    base = {**event("warning"), "signal_type": "consolidation_divergence", "point_type": "consolidation_up",
            "point_status": "warning", "lifecycle_cycle": 0, "macd_area_a": 10., "macd_area_c": 2.,
            "comparison_form": "entry_departure", "core_position": "breakout"}
    for stage,cycle in [("warning",0),("invalidated",0),("warning",1),("confirmed",1)]:
        row = {**base, "stage":stage,"point_status":stage,"lifecycle_cycle":cycle,"recorded_at":clock[0]}
        outbox.signal_events([row],since=0);outbox.deliver_one()
        restored = DingTalkOutbox(path,transport=transport,now=lambda:clock[0])
        restored.signal_events([row],since=0)
        assert restored.snapshot()["pending"] == 0
        outbox = restored;clock[0] += 5
    assert len(transport.messages) == 4
    assert "提前预警（未确认）" in transport.messages[0] and "已确认" not in transport.messages[0]
    assert "预警已失效" in transport.messages[1]
    assert "已确认" in transport.messages[-1]


def test_existing_confirmation_markers_keep_their_original_key():
    row = {**event("confirmed"), "signal_type": "consolidation_divergence", "point_status": "confirmed",
           "selection_status": "confirmed", "point_type": "consolidation_up"}
    assert signal_key(row) == 'signal:point:["confirmed","confirmed",null]'


def test_verbose_chinese_notifications_are_split_by_utf8_size_without_losing_members(tmp_path):
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=Transport(),now=lambda:1000)
    rows=[{**event('confirmed',identity=str(i)), 'name':'很长的中文公司名称'*10,
           'code':'LONG'+str(i)+'X'*72} for i in range(12)]
    box.signal_events(rows,since=0)
    with box._connect() as db:
        contents=db.execute('SELECT content FROM outbox').fetchall()
        payloads=db.execute('SELECT events FROM signal_payloads').fetchall()
    assert len(contents)>2 and all(len(row[0].encode('utf-8'))<=3300 for row in contents)
    assert sorted(e['id'] for row in payloads for e in json.loads(row[0]))==sorted(str(i) for i in range(12))


@pytest.mark.parametrize("direction", ["up", "down"])
def test_holding_consolidation_notification_has_its_own_label_and_persistent_dedup(tmp_path, direction):
    transport = Transport()
    outbox = DingTalkOutbox(tmp_path / "pan.sqlite3", transport=transport, now=lambda: 1000)
    alert = {**event(stage="confirmed"), "signal_type": "consolidation_divergence",
             "point_type": "consolidation_" + direction, "point_status": "confirmed",
             "comparison_form": "entry_departure", "core_position": "breakout",
             "macd_area_a": 10., "macd_area_c": 5.}
    outbox.signal_events([alert, alert], since=0)
    outbox.deliver_one()
    assert len(transport.messages) == 1
    assert "盘整背驰" in transport.messages[0] and "持仓标的" in transport.messages[0]
    assert "MACD 同向柱面积" in transport.messages[0]
    restarted = DingTalkOutbox(tmp_path / "pan.sqlite3", transport=transport, now=lambda: 1000)
    restarted.signal_events([alert], since=0)
    restarted.deliver_one()
    assert len(transport.messages) == 1


def test_historical_consolidation_event_is_not_sent(tmp_path):
    transport = Transport()
    outbox = DingTalkOutbox(tmp_path / "pan.sqlite3", transport=transport, now=lambda: 1000)
    outbox.signal_events([{**event(stage="confirmed"), "external_notifications": False,
                          "signal_type": "consolidation_divergence"}], since=0)
    outbox.deliver_one()
    assert transport.messages == []


def test_retry_revalidates_and_expires_a_signal_that_has_left_its_window(tmp_path):
    clock = [1000]
    active = [True]
    transport = Transport()
    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=transport, now=lambda: clock[0],
                           signal_validator=lambda events: (events if active[0] else [], []))
    outbox.signal_events([event(stage="confirmed")], since=0)
    transport.error = True
    outbox.deliver_one()
    assert outbox.snapshot()["pending"] == 1
    active[0] = False
    transport.error = False
    clock[0] += 16
    outbox.deliver_one()
    assert transport.messages == []
    assert outbox.snapshot()["history"][0]["status"] == "expired"


def test_mixed_batch_sends_valid_members_without_waiting_or_resending_after_recovery(tmp_path):
    clock = [1000]
    uncertain = [True]
    transport = Transport()

    def validate(events):
        valid, pending = [], []
        for row in events:
            if row["id"] == "retired":
                continue
            (pending if row["id"] == "waiting" and uncertain[0] else valid).append(row)
        return valid, pending

    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=transport, now=lambda: clock[0], signal_validator=validate)
    events = [{**event(stage="confirmed", identity=identity), "name": identity} for identity in ("ready", "waiting", "retired")]
    outbox.signal_events(events, since=0)
    outbox.deliver_one()
    assert len(transport.messages) == 1
    assert "ready" in transport.messages[0]
    assert "waiting" not in transport.messages[0] and "retired" not in transport.messages[0]
    clock[0] += 5
    outbox.deliver_one()
    assert len(transport.messages) == 1
    with outbox._connect() as db:
        assert db.execute("SELECT attempts FROM outbox WHERE status='retry'").fetchone()[0] == 0
    uncertain[0] = False
    clock[0] += 5
    restarted = DingTalkOutbox(outbox.path, transport=transport, now=lambda: clock[0], signal_validator=validate)
    restarted.deliver_one()
    assert len(transport.messages) == 2
    assert "waiting" in transport.messages[1] and "ready" not in transport.messages[1]
    restarted.signal_events(events, since=0)
    clock[0] += 5
    restarted.deliver_one()
    assert len(transport.messages) == 2


def test_old_or_damaged_pending_notification_without_ids_cannot_bypass_validation(tmp_path):
    transport = Transport()
    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=transport, now=lambda: 1000,
                           signal_validator=lambda rows: (rows, []))
    with outbox._connect() as db:
        outbox._insert(db, "legacy", "signals", "old plain text", 1000, 1800)
    outbox.deliver_one()
    assert transport.messages == []
    assert outbox.snapshot()["history"][0]["status"] == "expired"


def test_deferred_groups_cannot_starve_a_later_ready_notification(tmp_path):
    clock = [1000.]
    uncertain = {"waiting-a", "waiting-b"}
    transport = Transport()

    def validate(events):
        return ([row for row in events if row["id"] not in uncertain],
                [row for row in events if row["id"] in uncertain])

    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=transport,
                           now=lambda: clock[0], signal_validator=validate)
    for identity in ("waiting-a", "waiting-b", "ready"):
        outbox.signal_events([{**event("confirmed", identity=identity, stamp=clock[0]),
                               "name": identity}], since=0)
        clock[0] += 1
    for _ in range(3):
        outbox.deliver_one()
        clock[0] += 4
    assert len(transport.messages) == 1
    assert "ready" in transport.messages[0]
    assert "waiting-" not in transport.messages[0]
    assert outbox.snapshot()["pending"] == 2
    # Unknown signals retain their durable queue entries and recover once,
    # rather than being dropped to make the ready group appear faster.
    uncertain.clear()
    restarted = DingTalkOutbox(outbox.path, transport=transport,
                              now=lambda: clock[0], signal_validator=validate)
    for _ in range(3):
        restarted.deliver_one()
        clock[0] += 4
    assert len(transport.messages) == 3
    assert restarted.snapshot()["pending"] == 0


@pytest.fixture
def box(tmp_path):
    clock = [1000.]
    transport = Transport()
    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=transport, now=lambda: clock[0])
    return outbox, transport, clock


def test_completion_is_durable_and_deduplicated_after_restart(box):
    outbox, transport, clock = box
    outbox.screening_completed(result(), [])
    outbox.screening_completed(result(), [])
    assert outbox.snapshot()["pending"] == 1
    outbox.deliver_one()
    assert len(transport.messages) == 1
    assert "数据异常：1" in transport.messages[0]
    restored = DingTalkOutbox(outbox.path, transport=transport, now=lambda: clock[0])
    restored.screening_completed(result(), [])
    clock[0] += 10
    restored.deliver_one()
    assert len(transport.messages) == 1
    assert restored.snapshot()["sent"] == 1


def test_success_clears_historical_failure_banner_but_active_retry_is_visible(box):
    outbox, transport, clock = box
    transport.error = True
    outbox.screening_completed(result(), [])
    for _ in range(6):
        outbox.deliver_one()
        clock[0] += 700
    assert outbox.snapshot()["failed"] == 1
    assert outbox.snapshot()["last_error"]
    transport.error = False
    outbox.screening_completed({**result(), "run_id": "b" * 32}, [])
    outbox.deliver_one()
    snapshot = outbox.snapshot()
    assert snapshot["sent"] == 1 and snapshot["failed"] == 1
    assert snapshot["last_error"] == ""
    clock[0] += 10
    outbox.signal_events([event(identity="new", stamp=clock[0])], since=0)
    transport.error = True
    outbox.deliver_one()
    assert outbox.snapshot()["pending"] == 1
    assert outbox.snapshot()["last_error"]


def test_initial_pool_does_not_repeat_but_confirmation_transition_notifies(box):
    outbox, transport, clock = box
    initial = event()
    outbox.screening_completed(result(), [initial])
    outbox.signal_events([initial], since=999)
    assert outbox.snapshot()["pending"] == 1
    changed = event("confirmed")
    outbox.signal_events([changed], since=999)
    outbox.signal_events([changed], since=999)
    assert outbox.snapshot()["pending"] == 2
    outbox.deliver_one()
    clock[0] += 5
    outbox.deliver_one()
    assert len(transport.messages) == 2
    assert "三买 · 已确认" in transport.messages[1]


def test_confirmed_main_point_and_waiting_lower_confirmation_have_separate_notifications(box):
    from cl_app.services.signal_monitor import _signal
    outbox, transport, clock = box
    candidate = {"code": "TSM.US", "market": "us", "frequency": "5m", "source_closed_at": 1000,
                 "point": {"point_id": "tsm", "point_type": "3buy", "status": "confirmed",
                           "confirmed_at": 900, "available_at": 900, "missing_conditions": [], "anchor_price": 428.98},
                 "selection_status": "approaching", "selection_available_at": 1000,
                 "selection_missing_conditions": ["lower_1m_confirmation"],
                 "nested_confirmation": {"state": "waiting", "frequency": "1m"},
                 "confirmation_segment": {"state": "in_progress", "segment": {"direction": "up"}}}
    original = deepcopy(candidate)
    row = {**_signal(candidate), "recorded_at": 1000, "change": "discovered"}
    assert row["point_status"] == "confirmed" and row["stage"] == "approaching"
    assert row["available_at"] == 900 and row["selection_available_at"] == 1000
    assert row["missing_conditions"] == []
    assert candidate == original
    outbox.signal_events([row, row], since=999)
    outbox.deliver_one()
    assert len(transport.messages) == 1
    text = transport.messages[0]
    assert "5m 三买 · 已确认" in text
    assert "1m 区间内结构确认：等待确认" in text
    assert "后继向上线段：进行中（尚未完成）" in text
    assert "三买 · 形成等待" not in text
    clock[0] += 10
    ready = {**row, "stage": "confirmed", "selection_status": "confirmed",
             "lower_confirmation_state": "confirmed", "recorded_at": clock[0], "change": "changed"}
    outbox.signal_events([ready, ready], since=999)
    outbox.deliver_one()
    assert len(transport.messages) == 2
    assert "5m 三买 · 已确认" in transport.messages[1]
    assert "1m 区间内结构确认：已确认" in transport.messages[1]


def test_network_failure_retries_without_being_counted_as_sent(box):
    outbox, transport, clock = box
    outbox.signal_events([event()], since=999)
    transport.error = True
    outbox.deliver_one()
    assert outbox.snapshot()["sent"] == 0
    assert outbox.snapshot()["pending"] == 1
    assert outbox.snapshot()["last_error"]
    transport.error = False
    clock[0] += 14
    outbox.deliver_one()
    assert not transport.messages
    clock[0] += 2
    outbox.deliver_one()
    assert len(transport.messages) == 1
    assert "筛选条件待齐" in transport.messages[0]
    assert outbox.snapshot()["last_error"] == ""


def test_repeated_failure_stops_and_expired_live_alerts_are_not_delivered(box):
    outbox, transport, clock = box
    outbox.screening_completed(result(), [])
    transport.error = True
    for advance in (0, 15, 30, 60, 120, 240):
        clock[0] += advance
        outbox.deliver_one()
    assert outbox.snapshot()["failed"] == 1
    transport.error = False
    clock[0] = 3000
    outbox.signal_events([event(identity="old", stamp=1000)], since=999)
    assert outbox.snapshot()["pending"] == 0
    outbox.signal_events([event(stamp=3000)], since=999)
    clock[0] = 4801
    outbox.deliver_one()
    assert not transport.messages
    assert outbox.snapshot()["history"][0]["status"] == "expired"


def test_event_batching_rate_limit_and_old_history_filter(box):
    outbox, transport, clock = box
    outbox.signal_events([event(identity=str(i)) for i in range(13)] + [event(identity="past", stamp=998)], since=999)
    assert outbox.snapshot()["pending"] == 3
    outbox.deliver_one()
    outbox.deliver_one()
    assert len(transport.messages) == 1
    for _ in range(2):
        clock[0] += 4
        outbox.deliver_one()
    assert len(transport.messages) == 3
    assert all(len(t.encode("utf-8")) < 3500 for t in transport.messages)
    assert outbox.snapshot()["pending"] == 0


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("CHANLUN_DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=private-test-token")
    monkeypatch.setenv("CHANLUN_DINGTALK_KEYWORD", "应用关键词")
    monkeypatch.delenv("CHANLUN_DINGTALK_SECRET", raising=False)
    response = Mock(status_code=200)
    response.json.return_value = {"errcode": 0}
    session = Mock()
    session.post.return_value = response
    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(requests, "Session", lambda: manager)
    return session, response


def test_transport_requires_provider_success_and_preserves_chinese_payload(http):
    session, response = http
    transport = DingTalkTransport()
    transport.send("选股完成")
    call = session.post.call_args
    payload = json.loads(call.kwargs["data"].decode("utf-8"))
    assert payload["text"]["content"] == "应用关键词 · 选股完成"
    assert payload["at"]["isAtAll"] is False
    assert call.kwargs["allow_redirects"] is False
    response.json.return_value = {"errcode": 310000, "errmsg": "private-test-token"}
    with pytest.raises(DeliveryError, match="errcode=310000") as exc:
        transport.send("提醒")
    assert "private-test-token" not in str(exc.value)
    response.json.return_value = {"errmsg": "ok"}
    with pytest.raises(DeliveryError):
        transport.send("提醒")


def test_transport_never_exposes_webhook_on_network_failure(http):
    session, _ = http
    session.post.side_effect = requests.Timeout("https://oapi.dingtalk.com/robot/send?access_token=private-test-token")
    with pytest.raises(DeliveryError) as exc:
        DingTalkTransport().send("提醒")
    assert "private-test-token" not in str(exc.value)
    assert "Timeout" in str(exc.value)


def test_native_chart_markdown_keeps_heading_image_and_robot_keyword(http):
    session,_=http
    DingTalkTransport().send_markdown('三买图表','### 三买\n\n![信号快照](https://example.test/chart.png)')
    payload=json.loads(session.post.call_args.kwargs['data'].decode('utf-8'))
    assert payload['msgtype']=='markdown' and payload['markdown']['title']=='三买图表'
    assert payload['markdown']['text'].startswith('### 三买\n\n![信号快照]')
    assert '应用关键词' in payload['markdown']['text']


def test_signing_and_destination_validation(http, monkeypatch):
    session, _ = http
    monkeypatch.setenv("CHANLUN_DINGTALK_SECRET", "test-signing-secret")
    DingTalkTransport().send("提醒")
    query = parse_qs(urlsplit(session.post.call_args.args[0]).query)
    assert query["timestamp"][0].isdigit() and query["sign"][0]
    assert query["access_token"] == ["private-test-token"]
    monkeypatch.setenv("CHANLUN_DINGTALK_WEBHOOK", "https://example.com/robot/send?access_token=token")
    assert not DingTalkTransport.configuration()["configured"]
    with pytest.raises(DeliveryError):
        DingTalkTransport().send("提醒")


def test_notification_enablement_does_not_change_or_restart_current_screening(tmp_path, monkeypatch):
    from datetime import datetime
    from chanlun.screening.rules import CN
    from cl_app.services.signal_monitor import SignalMonitor
    clock = datetime(2026, 9, 17, 12, 0, tzinfo=CN)
    manual = Mock()
    manual.status.return_value = {"status": "running", "run_id": "b"*32}
    live = Mock()
    live.status.return_value = {"status": "idle"}
    outbox = DingTalkOutbox(tmp_path / "outbox.sqlite3", transport=Transport(), now=lambda: clock.timestamp())
    service = SignalMonitor(tmp_path, screening=manual, monitor=live, notifications=outbox, now=lambda: clock)
    monkeypatch.setattr(service, "start_runtime", lambda: None)
    before = deepcopy(service.snapshot()["settings"])
    service.configure_notifications({"enabled": True})
    service.tick()
    assert service.snapshot()["settings"] == before
    manual.start.assert_not_called()
    live.start.assert_not_called()
    done = {**result(), "status": "completed", "run_id": "b"*32, "finished_at": clock.timestamp()}
    manual.status.return_value = done
    manual.results.return_value = done
    service.tick()
    service.tick()
    assert outbox.snapshot()["pending"] == 1
    restored = SignalMonitor(tmp_path, screening=manual, monitor=live, notifications=outbox, now=lambda: clock)
    assert restored.snapshot()["dingtalk"]["enabled"]
    restored.tick()
    assert outbox.snapshot()["pending"] == 1


def test_notification_control_remains_authenticated_and_csrf_protected(monkeypatch):
    from cl_app import create_app
    fake = Mock()
    monkeypatch.setattr("cl_app.blueprints.monitor.service", fake)
    app = create_app(test_config={"TESTING": True, "VALIDATE_WEB_SECURITY": False})
    client = app.test_client()
    assert client.post("/monitor/dingtalk", json={"enabled": True}).status_code in (400, 401)
    app.config["LOGIN_DISABLED"] = True
    assert client.post("/monitor/dingtalk", json={"enabled": True}).status_code == 400
    fake.configure_notifications.assert_not_called()
    app.config["WTF_CSRF_ENABLED"] = False
    fake.configure_notifications.return_value = {"dingtalk": {"enabled": True}}
    assert client.post("/monitor/dingtalk", json={"enabled": True}).get_json()["dingtalk"]["enabled"]
