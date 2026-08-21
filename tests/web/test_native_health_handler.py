import json
import asyncio
import threading
import time

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application

from app import NativeHealthHandler, NativeReadinessRunner
from chanlun.tools.daemon_executor import DaemonExecutor


def test_readiness_runner_reuses_a_recent_deep_snapshot() -> None:
    calls = []

    class _FlaskApp:
        pass

    def health_snapshot(kind, market, forward_session):
        calls.append((kind, market, forward_session))
        return {"status": "ready", "market": market}, 200

    flask_app = _FlaskApp()
    flask_app.extensions = {"health_snapshot": health_snapshot}
    executor = DaemonExecutor(
        max_workers=1,
        thread_name_prefix="ReadinessCacheTest",
        max_pending=1,
    )
    try:
        runner = NativeReadinessRunner(
            flask_app,
            executor,
            cache_ttl_seconds=5,
            stale_if_busy_seconds=30,
        )
        first = runner.submit("a", None).result(timeout=1)
        second = runner.submit("a", None).result(timeout=1)

        assert first[0]["status"] == "ready"
        assert second[0]["readiness_snapshot_cached"] is True
        assert calls == [("readyz", "a", None)]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


class TestNativeHealthHandler(AsyncHTTPTestCase):
    def get_app(self):
        class _FlaskApp:
            extensions = {
                "health_snapshot": staticmethod(
                    lambda kind, market, forward_session: (
                        {
                            "status": "not_ready",
                            "kind": kind,
                            "market": market,
                            "forward_session": forward_session,
                        },
                        503,
                    )
                )
            }

        return Application(
            [(r"/readyz", NativeHealthHandler, {"flask_app": _FlaskApp()})]
        )

    def test_health_response_does_not_use_wsgi(self):
        response = self.fetch("/readyz?market=a")

        assert response.code == 503
        assert json.loads(response.body) == {
            "status": "not_ready",
            "kind": "readyz",
            "market": "a",
            "forward_session": None,
        }

    def test_forward_session_reaches_the_shared_health_snapshot(self):
        response = self.fetch("/readyz?market=a&forward_session=2026-07-30")

        assert response.code == 503
        assert json.loads(response.body)["forward_session"] == "2026-07-30"


class TestNativeHealthIsolation(AsyncHTTPTestCase):
    def get_app(self):
        self.deep_check_started = threading.Event()
        self.deep_check_release = threading.Event()

        class _FlaskApp:
            pass

        flask_app = _FlaskApp()

        def health_snapshot(kind, market, forward_session):
            if kind == "livez":
                return {"status": "alive", "revision": "test"}, 200
            if kind == "healthz":
                return {"status": "ok", "revision": "test"}, 200
            self.deep_check_started.set()
            self.deep_check_release.wait(timeout=1.0)
            return {
                "status": "ready",
                "market": market,
                "forward_session": forward_session,
            }, 200

        flask_app.extensions = {"health_snapshot": health_snapshot}
        self.health_executor = DaemonExecutor(
            max_workers=1,
            thread_name_prefix="ReadinessIsolationTest",
            max_pending=1,
        )
        runner = NativeReadinessRunner(flask_app, self.health_executor)
        return Application(
            [
                (
                    r"/(?:livez|healthz|readyz)",
                    NativeHealthHandler,
                    {
                        "flask_app": flask_app,
                        "readiness_runner": runner,
                        "readiness_timeout_seconds": 0.05,
                    },
                )
            ]
        )

    def tearDown(self):
        self.deep_check_release.set()
        self.health_executor.shutdown(wait=True, cancel_futures=True)
        super().tearDown()

    @gen_test
    async def test_slow_ready_check_never_blocks_liveness(self):
        ready_request = asyncio.ensure_future(
            self.http_client.fetch(
                self.get_url("/readyz?market=a"),
                raise_error=False,
            )
        )
        deadline = time.monotonic() + 0.5
        while not self.deep_check_started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert self.deep_check_started.is_set()

        started = time.monotonic()
        live = await self.http_client.fetch(
            self.get_url("/livez"),
            raise_error=False,
        )
        elapsed = time.monotonic() - started
        ready = await ready_request

        assert live.code == 200
        assert json.loads(live.body)["status"] == "alive"
        assert elapsed < 0.2
        assert ready.code == 503
        assert json.loads(ready.body)["reasons"] == ["health_snapshot_timeout"]

        busy = await self.http_client.fetch(
            self.get_url("/readyz?market=a"),
            raise_error=False,
        )
        assert busy.code == 503
        assert json.loads(busy.body)["reasons"] == ["health_snapshot_busy"]
