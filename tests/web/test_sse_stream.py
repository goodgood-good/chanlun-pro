"""Task6: SSE handler 路由构建(flag) + 鉴权拒绝。"""
import asyncio

import tornado.testing
import tornado.web

from cl_app import create_app
from cl_app.handlers import sse_stream
from cl_app.handlers.sse_stream import SseStreamHandler, build_routes
from cl_app.services.sse_hub import SseHub


def test_connection_limit_ignores_unrelated_cookies():
    app = create_app(test_config={"VALIDATE_WEB_SECURITY": False})
    hub = SseHub(max_loops=10, max_connections_per_client=2)
    serializer = app.session_interface.get_signing_serializer(app)
    session_cookie = serializer.dumps({"_user_id": "cl_pro", "_id": "browser-a"})
    cookie_headers = [
        f"session={session_cookie}; junk=one",
        f"session={session_cookie}; junk=two",
        f"session={session_cookie}; junk=three",
    ]
    identities = [
        sse_stream._client_identity(app, header, "127.0.0.1")
        for header in cookie_headers
    ]

    assert len(set(identities)) == 1
    assert hub.subscribe("k1", object(), lambda _key: object(), identities[0]) is True
    assert hub.subscribe("k2", object(), lambda _key: object(), identities[1]) is True
    assert hub.subscribe("k3", object(), lambda _key: object(), identities[2]) is False


def test_client_identity_ignores_forged_auth_cookie():
    app = create_app(test_config={"VALIDATE_WEB_SECURITY": False})
    serializer = app.session_interface.get_signing_serializer(app)
    session_cookie = serializer.dumps({"_user_id": "cl_pro", "_id": "browser-a"})

    first = sse_stream._client_identity(
        app,
        f"session={session_cookie}; remember_token=forged-one",
        "127.0.0.1",
    )
    second = sse_stream._client_identity(
        app,
        f"session={session_cookie}; remember_token=forged-two",
        "127.0.0.1",
    )

    assert first == second


def test_slow_client_does_not_block_fast_client_and_is_unsubscribed(monkeypatch):
    async def scenario():
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        class Client:
            def __init__(self, slow):
                self.slow = slow
                self.flushed = False
                self.unsubscribed = False

            def write(self, _data):
                pass

            async def flush(self):
                if self.slow:
                    slow_started.set()
                    await release_slow.wait()
                self.flushed = True

            async def _send(self, data_str):
                await SseStreamHandler._send(self, data_str)

            def _unsub(self):
                self.unsubscribed = True

        slow = Client(slow=True)
        fast = Client(slow=False)

        class FakeHub:
            def clients_of(self, _cache_key):
                return [slow, fast]

        monkeypatch.setattr(sse_stream, "_hub", FakeHub())
        monkeypatch.setattr(
            sse_stream, "recompute_chart_data", lambda *_args: {"t": [1]}
        )
        monkeypatch.setattr(
            sse_stream, "decide_push", lambda _last, _data: (True, "sig")
        )
        monkeypatch.setattr(sse_stream, "_refresh_interval_ms", lambda _market: 60_000)
        monkeypatch.setattr(sse_stream, "_SEND_TIMEOUT_SECONDS", 0.05, raising=False)

        handler = object.__new__(SseStreamHandler)
        handler._pool = None
        periodic = handler._make_start_loop("a", "SH.600000", "1m", {})("key")
        try:
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            await asyncio.sleep(0.15)
            observed = fast.flushed, slow.unsubscribed
        finally:
            release_slow.set()
            await asyncio.sleep(0.05)
            periodic.stop()
        return observed

    assert asyncio.run(scenario()) == (True, True)


def test_build_routes_flag_on(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", True, raising=False)
    app = create_app(test_config={
        "TESTING": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
    routes = build_routes(app)
    assert any("/tv/stream" in str(r[0]) for r in routes)


def test_build_routes_flag_off(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", False, raising=False)
    app = create_app(test_config={
        "TESTING": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
    assert build_routes(app) == []


class SseAuthTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.flask_app = create_app(test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
        })
        return tornado.web.Application(
            [(r"/tv/stream", SseStreamHandler,
              {"flask_app": self.flask_app, "pool": None})]
        )

    def test_unauthorized_when_pwd_set(self):
        resp = self.fetch("/tv/stream?symbol=a:SH.000001&resolution=1")
        self.assertEqual(resp.code, 401)
