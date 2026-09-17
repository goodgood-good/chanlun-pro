"""Notification effects are isolated from the real robot in every test."""

from copy import deepcopy
import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from cl_app.services.dingtalk_notifications import DeliveryError, DingTalkOutbox, DingTalkTransport


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
    assert "形成等待（未确认）" in transport.messages[0]
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
    assert payload["text"]["content"] == "应用关键词\n选股完成"
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
