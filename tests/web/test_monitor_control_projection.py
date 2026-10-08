"""Monitor controls should project once and validate paging before mutation."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock

from flask import Flask
import pytest

from chanlun.screening.rules import CN
from cl_app.blueprints import monitor as blueprint
from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_monitor_pagination import sample


@pytest.fixture
def control(tmp_path, monkeypatch):
    jobs = Mock()
    jobs.root = tmp_path / "runs"
    jobs.status.return_value = {"status": "completed", "source_current": True,
                                "current_source_revision": "current", "source_revision": "current"}
    notifications = Mock()
    notifications.snapshot.return_value = {}
    notifications.transport.configuration.return_value = {"configured": True}
    service = SignalMonitor(tmp_path, screening=jobs, monitor=jobs, notifications=notifications,
                            now=lambda: datetime(2026, 9, 30, 10, 0, tzinfo=CN))
    state = service._load()
    state.update(sample())
    state["dingtalk_enabled"] = False
    monkeypatch.setattr(service, "start_runtime", lambda: None)
    original = service.snapshot
    calls = []

    def snapshot(*args, **kwargs):
        calls.append(dict(kwargs.get("page_options")) if kwargs.get("page_options") is not None else None)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "snapshot", snapshot)
    monkeypatch.setattr(blueprint, "service", service)
    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.register_blueprint(blueprint.monitor_bp)
    return app.test_client(), service, jobs, notifications, calls


@pytest.mark.parametrize("action", ["start", "pause", "check", "dingtalk"])
@pytest.mark.parametrize("paged", [False, True])
def test_control_materializes_only_the_requested_response_once(control, action, paged):
    client, service, jobs, notifications, calls = control
    query = "?view=paged&signals_page=2&signals_size=25&pool_q=stock%20124" if paged else ""
    response = client.post("/monitor/" + action + query, json={"enabled": True} if action == "dingtalk" else {})
    assert response.status_code == 200
    body = response.get_json()
    assert len(calls) == 1, "the control built a full snapshot and then discarded it"
    if paged:
        assert calls[0]["signals_page"] == "2"
        assert body["pagination"]["signals"]["page"] == 2 and len(body["signals"]) == 25
        assert body["pagination"]["pool"]["total"] == 1
        assert "selection_checks" not in body
    else:
        assert calls == [None] and len(body["signals"]) == 125
    state = service._load()
    if action == "start":
        assert state["enabled"] is True
    elif action == "pause":
        assert state["enabled"] is False
        jobs.cancel.assert_called_once()
        jobs.release_workers.assert_called_once()
    elif action == "check":
        assert state["force"] is True
    else:
        assert state["dingtalk_enabled"] is True
        notifications.begin_window.assert_called_once()


@pytest.mark.parametrize("action", ["start", "pause", "check", "dingtalk"])
def test_invalid_paging_is_rejected_before_any_control_effect(control, action, monkeypatch):
    client, service, jobs, notifications, calls = control
    if action == "start":
        service._load()["enabled"] = False
    before = deepcopy(service._load())
    saving = Mock(wraps=service._save)
    monkeypatch.setattr(service, "_save", saving)
    response = client.post("/monitor/" + action + "?view=paged&signals_size=5000",
                           json={"enabled": True} if action == "dingtalk" else {})
    assert response.status_code == 400
    assert service._load() == before, "a rejected page request already changed monitoring"
    saving.assert_not_called()
    jobs.cancel.assert_not_called()
    jobs.release_workers.assert_not_called()
    notifications.begin_window.assert_not_called()
    assert calls == []
