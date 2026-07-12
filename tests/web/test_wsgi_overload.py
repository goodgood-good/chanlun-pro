import asyncio
import json
from types import SimpleNamespace

import pytest

import app as desktop_app
from app import BoundedWSGIContainer
from chanlun.tools.daemon_executor import DaemonExecutor


class _Connection:
    def __init__(self):
        self.start_line = None
        self.headers = None
        self.body = None
        self.finished = False

    def write_headers(self, start_line, headers, chunk=None):
        self.start_line = start_line
        self.headers = headers
        self.body = chunk

    def finish(self):
        self.finished = True


def _request():
    return SimpleNamespace(
        connection=_Connection(),
        method="GET",
        uri="/ticks",
        remote_ip="127.0.0.1",
        request_time=lambda: 0.001,
    )


def test_bounded_wsgi_container_returns_503_when_capacity_is_exhausted(monkeypatch):
    executor = DaemonExecutor(1, "BoundedWsgiTest")
    container = BoundedWSGIContainer(lambda _env, _start: [], executor, 1)
    scheduled = []
    loop = SimpleNamespace(
        spawn_callback=lambda callback, request: scheduled.append((callback, request))
    )
    monkeypatch.setattr(desktop_app.IOLoop, "current", lambda: loop)
    monkeypatch.setattr(container, "_log", lambda _status, _request: None)

    first = _request()
    second = _request()
    try:
        container(first)
        container(second)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    assert len(scheduled) == 1
    assert second.connection.start_line.code == 503
    assert second.connection.headers["Retry-After"] == "1"
    assert second.connection.finished is True
    assert json.loads(second.connection.body) == {
        "ok": False,
        "code": "server_busy",
        "errmsg": "Server is busy. Retry shortly.",
    }


def test_bounded_wsgi_container_releases_capacity_after_request_failure():
    executor = DaemonExecutor(1, "BoundedWsgiReleaseTest")
    container = BoundedWSGIContainer(lambda _env, _start: [], executor, 1)

    async def fail(_request):
        raise RuntimeError("request failed")

    container.handle_request = fail
    assert container._request_slots.acquire(blocking=False) is True
    with container._active_condition:
        container._active_requests = 1
    try:
        with pytest.raises(RuntimeError, match="request failed"):
            asyncio.run(container._handle_bounded_request(_request()))
        assert container._request_slots.acquire(blocking=False) is True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def test_bounded_wsgi_container_reports_inflight_drain_timeout():
    executor = DaemonExecutor(1, "BoundedWsgiDrainTest")
    container = BoundedWSGIContainer(lambda _env, _start: [], executor, 1)
    try:
        with container._active_condition:
            container._active_requests = 1
        assert container.wait_for_idle(0.01) is False
        with container._active_condition:
            container._active_requests = 0
            container._active_condition.notify_all()
        assert container.wait_for_idle(0.01) is True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
