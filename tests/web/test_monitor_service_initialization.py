"""Concurrent first access must retain one owned monitor dependency instance."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace
import threading

import pytest

from cl_app.services import dingtalk_notifications, signal_analytics, signal_monitor
from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_dingtalk_notifications import Transport


DEPENDENCIES = [
    ("analytics", signal_analytics, "SignalAnalytics"),
    ("monitor", signal_monitor, "ScreeningManager"),
    ("notifications", dingtalk_notifications, "DingTalkOutbox"),
]


@pytest.mark.parametrize("name,module,constructor", DEPENDENCIES)
def test_concurrent_first_access_returns_one_instance(tmp_path, monkeypatch, name, module, constructor):
    service = SignalMonitor(tmp_path)
    entered, release = threading.Event(), threading.Event()
    instances = []
    guard = threading.Lock()

    def create(*args, **kwargs):
        with guard:
            instance = SimpleNamespace(index=len(instances))
            instances.append(instance)
        if instance.index == 0:
            entered.set()
            assert release.wait(5)
        return instance

    monkeypatch.setattr(module, constructor, create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(getattr, service, name)
        try:
            assert entered.wait(2)
            second = pool.submit(getattr, service, name)
            try:
                second.result(timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release.set()
        first_value, second_value = first.result(timeout=2), second.result(timeout=2)
    assert first_value is second_value is getattr(service, name)
    assert len(instances) == 1


@pytest.mark.parametrize("name,module,constructor", DEPENDENCIES)
def test_failed_initialization_can_be_retried(tmp_path, monkeypatch, name, module, constructor):
    service = SignalMonitor(tmp_path)
    calls = []
    expected = object()

    def create(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("controlled initialization failure")
        return expected

    monkeypatch.setattr(module, constructor, create)
    with pytest.raises(OSError):
        getattr(service, name)
    assert getattr(service, name) is expected
    assert getattr(service, name) is expected
    assert len(calls) == 2


def test_slow_notification_initialization_does_not_block_other_dependencies_or_state(tmp_path, monkeypatch):
    service = SignalMonitor(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return object()

    monkeypatch.setattr(dingtalk_notifications, "DingTalkOutbox", create)

    def independent():
        with service._lock:
            state = service._load()
        return state, service.analytics, service.monitor

    with ThreadPoolExecutor(max_workers=2) as pool:
        initializing = pool.submit(lambda: service.notifications)
        try:
            assert entered.wait(2)
            state, analytics, monitor = pool.submit(independent).result(timeout=0.5)
            assert state is not None and analytics is not None and monitor is not None
        finally:
            release.set()
        initializing.result(timeout=2)


def test_page_first_access_cannot_replace_the_started_notification_workers(tmp_path, monkeypatch):
    service = SignalMonitor(tmp_path)
    original = dingtalk_notifications.DingTalkOutbox
    entered, release = threading.Event(), threading.Event()
    instances = []
    guard = threading.Lock()
    monkeypatch.setattr(service, "_loop", lambda: service._stop.wait(5))
    monkeypatch.setattr(service, "notifications_enabled", lambda: False)

    def create(*args, **kwargs):
        box = original(*args, **kwargs, transport=Transport())
        with guard:
            index = len(instances)
            instances.append(box)
        if index == 0:
            entered.set()
            assert release.wait(5)
        return box

    monkeypatch.setattr(dingtalk_notifications, "DingTalkOutbox", create)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            page = pool.submit(lambda: service.notifications)
            try:
                assert entered.wait(2)
                starting = pool.submit(service.start_runtime)
                try:
                    starting.result(timeout=0.2)
                except TimeoutError:
                    pass
            finally:
                release.set()
            box = page.result(timeout=2)
            starting.result(timeout=2)
        assert service.notifications is box
        assert box._thread is not None and box._thread.is_alive()
        assert box.charts._thread is not None and box.charts._thread.is_alive()
        assert len(instances) == 1
    finally:
        release.set()
        service.stop_runtime()
        for box in instances:
            box.stop()
